"""Process-level review locks prevent duplicate billing; all scoring is mocked."""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime/bin'))
import plugin_cli
import tendency_review


WORKER = r'''
import json,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import plugin_cli,tendency_review
folder=Path(sys.argv[2]); mode=sys.argv[3]
plugin_cli.installed_bundle_for_plan=lambda plan:None
plugin_cli.saved_tendency_profile=lambda folder:None
def score(*args,**kwargs):
    with (folder/'calls.jsonl').open('a',encoding='utf-8') as log:
        log.write(json.dumps({'mock':True})+'\n')
    if mode=='hold':
        (folder/'started').write_text('started',encoding='utf-8')
        deadline=time.monotonic()+15
        while not (folder/'release').exists():
            if time.monotonic()>deadline:raise RuntimeError('mock gate timeout')
            time.sleep(0.01)
    return {'status':'completed','model_started':True,'provider_requests':1}
tendency_review.score_batch=score
print(json.dumps(plugin_cli.score_report(folder)),flush=True)
'''


class TendencyLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name).resolve()
        (self.folder / 'plan.json').write_text('{}', encoding='utf-8')
        self.completed = {'status': 'completed', 'model_started': True, 'provider_requests': 1}

    def mock_score(self):
        return patch.object(tendency_review, 'score_batch', return_value=self.completed)

    def worker(self, mode):
        return subprocess.Popen([sys.executable, '-B', '-c', WORKER,
            str(ROOT / 'runtime/bin'), str(self.folder), mode],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8'})

    def assert_busy(self, result):
        self.assertEqual(result['status'], 'busy')
        self.assertEqual(result['error_code'], 'review_in_progress')
        self.assertEqual(result['provider_requests'], 0)
        self.assertFalse(result['model_started'])

    def test_concurrent_processes_make_only_one_mock_request_and_then_cache(self):
        first = self.worker('hold')
        try:
            deadline = time.monotonic() + 8
            while not (self.folder / 'started').exists() and first.poll() is None:
                if time.monotonic() > deadline:
                    self.fail('first mock process did not acquire its lock')
                time.sleep(0.01)
            second = self.worker('fast')
            output, error = second.communicate(timeout=8)
            self.assertEqual(second.returncode, 0, error)
            self.assert_busy(json.loads(output))
            self.assertEqual(len((self.folder / 'calls.jsonl').read_text().splitlines()), 1)
            (self.folder / 'release').write_text('release', encoding='utf-8')
            output, error = first.communicate(timeout=8)
            self.assertEqual(first.returncode, 0, error)
            self.assertEqual(json.loads(output)['provider_requests'], 1)
            third = self.worker('fast')
            output, error = third.communicate(timeout=8)
            self.assertEqual(third.returncode, 0, error)
            cached = json.loads(output)
            self.assertTrue(cached['cached'])
            self.assertEqual(cached['provider_requests'], 0)
            self.assertEqual(len((self.folder / 'calls.jsonl').read_text().splitlines()), 1)
        finally:
            (self.folder / 'release').write_text('release', encoding='utf-8')
            if first.poll() is None:
                first.terminate()
            first.communicate(timeout=8)

    def test_lock_covers_cache_lookup_retry_archive_and_save(self):
        source = self.folder / 'tendency-review.json'
        source.write_text(json.dumps({'status': 'unavailable'}), encoding='utf-8')
        original = source.read_bytes()
        with plugin_cli.tendency_review_lock(self.folder) as acquired:
            self.assertTrue(acquired)
            with patch.object(plugin_cli, 'saved_tendency_profile', side_effect=AssertionError('read cache while busy')), \
                 patch.object(plugin_cli, 'read_saved_file', side_effect=AssertionError('read source while busy')), \
                 patch.object(tendency_review, 'score_batch', side_effect=AssertionError('charged while busy')):
                self.assert_busy(plugin_cli.score_report(self.folder))
                self.assert_busy(plugin_cli.score_report(self.folder, retry=True))
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse((self.folder / 'review-attempts').exists())

    def test_call_and_save_errors_release_lock_for_next_attempt(self):
        for location in ('score', 'save'):
            with self.subTest(location=location):
                with patch.object(plugin_cli, 'installed_bundle_for_plan', return_value=None), \
                     patch.object(tendency_review, 'score_batch',
                        side_effect=RuntimeError('mock scoring error') if location == 'score' else None,
                        return_value=self.completed), \
                     patch.object(plugin_cli.assessment, 'save',
                        side_effect=RuntimeError('mock save error') if location == 'save' else None):
                    with self.assertRaises(RuntimeError):
                        plugin_cli.score_report(self.folder)
                with plugin_cli.tendency_review_lock(self.folder) as acquired:
                    self.assertTrue(acquired)

    def test_lock_file_remains_same_inode_after_normal_release(self):
        with plugin_cli.tendency_review_lock(self.folder) as acquired:
            self.assertTrue(acquired)
            first = (self.folder / 'tendency-review.lock').stat()
        with plugin_cli.tendency_review_lock(self.folder) as acquired:
            self.assertTrue(acquired)
            second = (self.folder / 'tendency-review.lock').stat()
        self.assertEqual((first.st_dev, first.st_ino), (second.st_dev, second.st_ino))

    def test_symlink_and_hardlink_lock_rejected_without_touching_target_or_provider(self):
        target = self.folder / 'target'
        target.write_text('preserve me', encoding='utf-8')
        lock = self.folder / 'tendency-review.lock'
        try:
            lock.symlink_to(target)
        except OSError:
            self.skipTest('symlinks unavailable')
        with patch.object(tendency_review, 'score_batch') as score:
            with self.assertRaisesRegex(ValueError, 'non-symlink'):
                plugin_cli.score_report(self.folder)
            self.assertEqual(target.read_text(), 'preserve me')
            lock.unlink()
            try:
                os.link(target, lock)
            except OSError:
                self.skipTest('hardlinks unavailable')
            with self.assertRaisesRegex(ValueError, 'non-linked'):
                plugin_cli.score_report(self.folder)
            score.assert_not_called()
            self.assertEqual(target.read_text(), 'preserve me')

    @unittest.skipUnless(os.name == 'posix', 'POSIX replacement and FIFO semantics')
    def test_fifo_and_inode_replacement_rejected_before_model_call(self):
        lock = self.folder / 'tendency-review.lock'
        os.mkfifo(lock)
        with self.mock_score() as score:
            with self.assertRaisesRegex(ValueError, 'regular'):
                plugin_cli.score_report(self.folder)
            lock.unlink()
            lock.touch()
            real_open = os.open
            def replace_after_open(path, flags, mode):
                descriptor = real_open(path, flags, mode)
                lock.unlink()
                lock.write_text('replacement', encoding='utf-8')
                return descriptor
            with patch.object(plugin_cli.os, 'open', side_effect=replace_after_open):
                with self.assertRaisesRegex(ValueError, 'changed while opening'):
                    plugin_cli.score_report(self.folder)
            score.assert_not_called()

    @unittest.skipUnless(os.name == 'posix', 'POSIX flock error semantics')
    def test_unexpected_lock_error_is_not_treated_as_an_unlocked_operation(self):
        import fcntl
        with patch.object(fcntl, 'flock', side_effect=OSError(errno.EIO, 'mock lock error')), \
             self.mock_score() as score:
            with self.assertRaises(OSError):
                plugin_cli.score_report(self.folder)
            score.assert_not_called()
        with plugin_cli.tendency_review_lock(self.folder) as acquired:
            self.assertTrue(acquired)

    def test_windows_backend_uses_nonblocking_byte_lock_and_releases(self):
        class WindowsOS:
            name = 'nt'
            def __getattr__(self, name):
                return getattr(os, name)
        backend = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=Mock())
        with patch.object(plugin_cli, 'os', WindowsOS()), patch.dict(sys.modules, {'msvcrt': backend}):
            with plugin_cli.tendency_review_lock(self.folder) as acquired:
                self.assertTrue(acquired)
            self.assertEqual([call.args[1:] for call in backend.locking.call_args_list], [(2, 1), (0, 1)])
            self.assertEqual((self.folder / 'tendency-review.lock').read_bytes(), b'')
            backend.locking.reset_mock()
            backend.locking.side_effect = OSError(errno.EACCES, 'mock byte lock held')
            with self.mock_score() as score:
                self.assert_busy(plugin_cli.score_report(self.folder))
                score.assert_not_called()
            backend.locking.assert_called_once()


if __name__ == '__main__':
    unittest.main()
