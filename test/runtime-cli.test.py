"""Portable public runtime contracts; no model, credentials, or user memory access."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'runtime/bin'))
import assessment
import plugin_cli
from batteries import catalogue


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name).resolve()/'data'

    def args(self, **updates):
        values = dict(data_dir=str(self.data), battery='core', mode='mock',
                      items=['V1-D3-backup'], include_memory=False, max_requests=2,
                      max_output_tokens=256, timeout=30)
        values.update(updates)
        return argparse.Namespace(**values)

    def plan(self, **updates):
        return plugin_cli.make_plan(self.args(**updates))

    def test_catalogue_retains_both_original_frozen_packs(self):
        original = catalogue(plugin_cli.RUNTIME, 'core')
        extended = catalogue(plugin_cli.RUNTIME, 'extended')
        self.assertEqual((original['item_count'], extended['item_count']), (9,23))
        self.assertEqual(original['bundle_sha256'], '4cf2c3d7ea272ddec2e4423be3cb4dd3894c61b9f8f70dab09ec25e34b782f61')
        self.assertEqual(extended['bundle_sha256'], '7dcf6d3d842020820d69912357feeab1e3fdbe82fcf2f1cf60f8f608f548bfdc')

    def test_status_never_reads_credentials_or_user_memory(self):
        with patch('dsh_adapter.load_host_token', side_effect=AssertionError('credential read')), \
             patch('memorix_snapshot.freeze_snapshot', side_effect=AssertionError('memory read')):
            result = plugin_cli.status()
        self.assertFalse(result['model_started'])
        self.assertFalse(result['credentials_read'])
        self.assertFalse(result['memory_read'])

    def test_plan_is_offline_bounded_and_outside_installation(self):
        with patch('dsh_adapter.run_item', side_effect=AssertionError('model started')), \
             patch('memorix_snapshot.freeze_snapshot', side_effect=AssertionError('memory read')):
            result = self.plan(battery='extended', items=None)
        self.assertEqual(result['item_count'], 23)
        self.assertEqual(result['max_provider_requests'], 46)
        self.assertFalse(result['model_started'])
        self.assertTrue(Path(result['plan_path']).is_relative_to(self.data))
        self.assertEqual(Path(result['plan_path']).parent.name, result['batch_id'])
        self.assertEqual(len(assessment.load_plan(result['plan_path'])['schedule']), 23)
        self.assertTrue(all(job['condition_id']=='baseline' for job in assessment.load_plan(result['plan_path'])['schedule']))

    def test_cli_plan_requires_explicit_mode(self):
        proc = subprocess.run([sys.executable, '-B', str(ROOT/'runtime/bin/plugin_cli.py'),
            'plan', '--data-dir', str(self.data)], capture_output=True, text=True, encoding='utf-8')
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(self.data.exists())

    def test_catalogue_and_plan_ignore_legacy_default_file_encoding(self):
        original_open = Path.open
        def legacy_open(path, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
            if 'b' not in mode and encoding in (None, 'locale'):
                encoding = 'cp1252'
            return original_open(path, mode, buffering, encoding, errors, newline)
        with patch.object(Path, 'open', legacy_open):
            result = catalogue(plugin_cli.RUNTIME, 'extended')
            self.assertEqual(result['item_count'], 23)
            plan = self.plan()
            saved = assessment.load_plan(plan['plan_path'])
            self.assertIn('鲸相', saved['label'])
        self.assertIn('鲸相', Path(plan['plan_path']).read_bytes().decode('utf-8'))

    def test_cli_emits_utf8_despite_legacy_stdio_environment(self):
        env = {**os.environ, 'PYTHONUTF8':'0', 'PYTHONIOENCODING':'cp1252'}
        proc = subprocess.run([sys.executable, '-B', str(ROOT/'runtime/bin/plugin_cli.py'),
            'catalogue', '--battery', 'extended'], capture_output=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode('utf-8', errors='replace'))
        result = json.loads(proc.stdout.decode('utf-8'))
        self.assertEqual(result['item_count'], 23)
        self.assertEqual(result['items'], catalogue(plugin_cli.RUNTIME, 'extended')['items'])
        self.assertTrue(any(ord(char) > 127 for char in result['items'][0]['prompt']))

    def test_invalid_budget_cannot_read_memory(self):
        with patch('memorix_snapshot.freeze_snapshot', side_effect=AssertionError('memory read')):
            with self.assertRaises(ValueError):
                self.plan(include_memory=True, max_requests=31)

    def test_memory_is_copied_only_on_explicit_choice(self):
        copied = []
        def fake_snapshot(destination):
            copied.append(destination)
            destination.mkdir(parents=True)
            (destination/'memorix.db').write_bytes(b'fake test fixture')
            (destination/'.project-aliases.json').write_text('{}', encoding='utf-8')
            return {'status':'frozen', 'model_started':False}
        with patch('memorix_snapshot.freeze_snapshot', side_effect=fake_snapshot), \
             patch('plugin_cli.status', return_value={'can_run':True}):
            result = self.plan(include_memory=True)
        self.assertEqual(len(copied), 1)
        self.assertTrue(result['memory_copied'])
        self.assertTrue(assessment.load_plan(result['plan_path'])['memorix_enabled'])
        self.assertFalse(result['model_started'])

    def test_installation_real_dsh_and_symlink_data_roots_rejected(self):
        for path in (ROOT, ROOT/'runtime/data', Path.home()/'.dsh/plugins/test-data'):
            with self.assertRaises(ValueError):
                plugin_cli.private_data_root(str(path))
        target = Path(self.temp.name)/'target'
        target.mkdir()
        link = Path(self.temp.name)/'link'
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest('symlink creation is unavailable on this host')
        with self.assertRaises(ValueError):
            plugin_cli.private_data_root(str(link))

    def test_batch_id_cannot_traverse_data_root(self):
        root = plugin_cli.private_data_root(str(self.data))
        with self.assertRaises(ValueError):
            plugin_cli.batch_folder(root, '../../runtime')

    def test_custom_app_cannot_overlap_protected_roots(self):
        from dsh_adapter import validate_app_path
        app = Path(self.temp.name)/'Example.app'
        (app/'Contents/MacOS').mkdir(parents=True)
        (app/'Contents/MacOS/DeepSeek Harness').write_text('test fixture', encoding='utf-8')
        self.assertEqual(validate_app_path(app), app.resolve())
        with patch('dsh_adapter.SOURCE', app/'plugin/bin'):
            with self.assertRaisesRegex(ValueError, 'overlaps protected'):
                validate_app_path(app)

    def test_live_requires_explicit_permission_and_matching_frozen_mode(self):
        result = self.plan(mode='live')
        args = argparse.Namespace(data_dir=str(self.data), batch_id=result['batch_id'], mode='live', allow_live=False)
        with patch('assessment.run_batch', side_effect=AssertionError('adapter started')):
            with self.assertRaisesRegex(ValueError, 'explicit'):
                plugin_cli._run_locked(args)
            args.mode='mock'
            with self.assertRaisesRegex(ValueError, 'mode'):
                plugin_cli._run_locked(args)

    def test_other_platforms_cannot_start_model(self):
        result = self.plan()
        args = argparse.Namespace(data_dir=str(self.data), batch_id=result['batch_id'], mode='mock', allow_live=False)
        with patch('plugin_cli.status', return_value={'can_run':False,'checks':{'platform':False}}), \
             patch('assessment.run_batch', side_effect=AssertionError('adapter started')):
            with self.assertRaisesRegex(ValueError, 'platform'):
                plugin_cli._run_locked(args)

    def test_pre_cancelled_batch_keeps_plan_and_makes_diagnostic_report(self):
        result = self.plan()
        folder = Path(result['plan_path']).parent
        with patch('dsh_adapter.run_item', side_effect=AssertionError('adapter started')):
            path = assessment.run_batch(result['plan_path'], cancelled=lambda: True)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['status'], 'cancelled')
        report = plugin_cli.make_report(folder)
        self.assertEqual(report['included_runs'], 0)
        self.assertTrue(Path(report['report_html']).is_file())
        self.assertTrue(Path(result['plan_path']).is_file())

    def test_cancelled_item_seals_partial_evidence_and_stops_next_question(self):
        result = self.plan(items=['V1-D3-backup','V1-D3-wifi'])
        def interrupted(bundle, item, condition, output, **kwargs):
            assessment.save(output/'meta.json', {
                'schema_version':'dsh-adapter-run.v1','run_id':'cancelled-item',
                'item_id':item,'condition_id':condition,'status':'failed',
                'model_started':False,'mock':True,'subject':'isolated-headless',
                'termination_reason':'user_cancelled'})
            (output/'partial.txt').write_text('preserved partial output', encoding='utf-8')
            raise KeyboardInterrupt
        with contextlib.redirect_stdout(io.StringIO()):
            path = assessment.run_batch(result['plan_path'], adapter=interrupted)
        manifest = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(manifest['status'], 'cancelled')
        self.assertEqual(len(manifest['runs']), 1)
        self.assertEqual(len(manifest['not_started_slots']), 1)
        self.assertTrue((path.parent/'runs/run-001/partial.txt').is_file())
        self.assertEqual(plugin_cli.make_report(path.parent)['included_runs'], 0)

    def test_readonly_relocated_installation_can_list_and_plan(self):
        import shutil
        copied = Path(self.temp.name)/'relocated plugin'
        shutil.copytree(ROOT/'runtime', copied/'runtime')
        for p in (copied, *copied.rglob('*')):
            p.chmod(0o555 if p.is_dir() else 0o444)
        def restore():
            for p in (copied, *copied.rglob('*')):
                p.chmod(0o755 if p.is_dir() else 0o644)
        self.addCleanup(restore)
        before = {str(p.relative_to(copied)):p.stat().st_mtime_ns for p in copied.rglob('*') if p.is_file()}
        cli = copied/'runtime/bin/plugin_cli.py'
        commands = [['catalogue','--battery','extended'], ['plan','--data-dir',str(self.data),
                    '--battery','core','--mode','mock','--items','V1-D3-wifi']]
        for command in commands:
            proc = subprocess.run([sys.executable,'-B',str(cli),*command], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(proc.returncode, 0, proc.stdout+proc.stderr)
            self.assertEqual(json.loads(proc.stdout)['type'], 'result')
        after = {str(p.relative_to(copied)):p.stat().st_mtime_ns for p in copied.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_report_rejects_symlink_before_writing(self):
        result = self.plan()
        folder = Path(result['plan_path']).parent
        assessment.run_batch(result['plan_path'], cancelled=lambda: True)
        protected = Path(self.temp.name)/'protected.txt'
        protected.write_text('unchanged', encoding='utf-8')
        try:
            (folder/'report.json').symlink_to(protected)
        except OSError:
            self.skipTest('symlink creation is unavailable on this host')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            plugin_cli.make_report(folder)
        self.assertEqual(protected.read_text(encoding='utf-8'), 'unchanged')

    @unittest.skipUnless(os.name == 'posix', 'POSIX flock behavior')
    def test_global_run_lock_blocks_a_second_batch(self):
        import fcntl
        root = plugin_cli.private_data_root(str(self.data))
        with (root/'runner.lock').open('w') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('plugin_cli.platform.system', return_value='Darwin'), \
                 patch('plugin_cli._run_locked', side_effect=AssertionError('concurrent batch started')):
                with self.assertRaisesRegex(ValueError, 'still running'):
                    plugin_cli.run(argparse.Namespace(data_dir=str(self.data)))

    def test_nonmac_entrypoint_does_not_launch_even_mock(self):
        with patch('plugin_cli.platform.system', return_value='Linux'), \
             patch('plugin_cli._run_locked', side_effect=AssertionError('worker started')):
            with self.assertRaisesRegex(ValueError, 'macOS Seatbelt'):
                plugin_cli.run(argparse.Namespace(data_dir=str(self.data)))

    @unittest.skipUnless(os.name == 'posix', 'POSIX process-group behavior')
    def test_cancel_kills_descendant_process_group(self):
        marker = Path(self.temp.name)/'grandchild.pid'
        child_code = 'import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"],start_new_session=True);open(sys.argv[1],"w").write(str(p.pid));time.sleep(60)'
        worker_code = 'import signal,sys;sys.path.insert(0,sys.argv[1]);from process_control import run_child;signal.signal(signal.SIGTERM,lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()));run_child([sys.executable,"-c",sys.argv[2],sys.argv[3]],capture_output=True)'
        worker = subprocess.Popen([sys.executable,'-B','-c',worker_code,str(ROOT/'runtime/bin'),child_code,str(marker)],
                                  stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        self.addCleanup(lambda: worker.poll() is None and worker.kill())
        deadline = time.monotonic()+10
        while not marker.exists() and time.monotonic()<deadline:
            time.sleep(0.05)
        self.assertTrue(marker.exists())
        pid=int(marker.read_text(encoding='utf-8'))
        worker.send_signal(signal.SIGTERM)
        _, stderr = worker.communicate(timeout=10)
        # Grandchildren inherit the capture pipe: communicate cannot finish if
        # they survive. A timeout here would expose an incomplete tree cleanup.
        self.assertNotIn(b'TimeoutExpired', stderr)
        self.assertIn(b'KeyboardInterrupt', stderr)


if __name__ == '__main__':
    unittest.main()
