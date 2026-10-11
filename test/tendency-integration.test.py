"""Saved tendency reviews are offline, source-bound, and never billed twice.

All reports and judgments in this file are synthetic. No provider, user memory,
credentials, or live subject session is used by this integration suite.
"""
import copy
from contextlib import ExitStack, contextmanager, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime/bin'))
import assessment_report
import dsh_adapter
import plugin_cli
import tendency_review

_spec = importlib.util.spec_from_file_location(
    'tendency_saved_report_fixtures', ROOT / 'test/runtime-render.test.py')
saved = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(saved)


class TendencyIntegrationTests(unittest.TestCase):
    # Reuse the same saved, hash-bound batch fixture without inheriting its tests.
    setUp = saved.RenderTests.setUp
    recorded_batch = saved.RenderTests.recorded_batch
    embedded = saved.RenderTests.embedded
    link = saved.RenderTests.link

    def sidecar(self, *, status='completed'):
        data = assessment_report.validate_batch(self.folder / 'batch.json')
        payload = tendency_review.build_input(data)
        if status == 'completed':
            ratings = []
            for run in payload['runs']:
                for axis in run['axes']:
                    source = 'session' if axis == 'verification' else 'answer'
                    public = next(row for row in run['evidence'] if row['source'] == source)
                    ratings.append({'run_id': run['run_id'], 'axis': axis, 'level': 2,
                        'reason': 'Synthetic offline review; not a real assessment.',
                        'evidence': [{'source': source, 'quote': public['text'][:180]}],
                        'substantive_dissent': True if axis == 'dissent' else None})
            review, error = {'ratings': ratings}, None
        else:
            review = tendency_review._unknown(payload, 'Synthetic unavailable review')
            error = 'provider_unavailable' if status == 'unavailable' else None
        return tendency_review._sidecar(payload, status, review, error)

    def save_sidecar(self, value):
        path = self.folder / 'tendency-review.json'
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return path

    def snapshot(self):
        return {path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.folder.rglob('*') if path.is_file()
                and 'view' not in path.relative_to(self.folder).parts}

    def assert_preserved(self, before):
        for path, value in before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), value, str(path))

    @contextmanager
    def offline(self, *, permit_score=False):
        targets = [
            'assessment.run_batch',
            'assessment.load_plan',
            'memorix_snapshot.freeze_snapshot',
            'dsh_adapter.load_host_token',
            'dsh_adapter.ProviderBridge',
        ]
        if not permit_score:
            targets.append('tendency_review.score_batch')
        with ExitStack() as stack:
            blocked = [stack.enter_context(patch(name,
                side_effect=AssertionError('Forbidden integration call: ' + name)))
                for name in targets]
            yield
            for method in blocked:
                method.assert_not_called()

    def test_saved_review_renders_offline_without_rewriting_report_or_memory(self):
        self.recorded_batch(memory=True)
        expected = self.sidecar()
        self.save_sidecar(expected)
        before = self.snapshot()
        with self.offline():
            result = plugin_cli.render_report(self.folder)
        rendered = self.embedded(result)
        self.assertEqual(rendered['tendency_profile'], expected['profile'])
        self.assertEqual(result['visual_evidence_refresh'], 'derived_from_saved_batch')
        self.assertFalse(result['model_started'])
        self.assertFalse(result['credentials_read'])
        self.assertFalse(result['memory_read'])
        self.assertNotIn('tendency_profile', json.loads(self.source.read_text(encoding='utf-8')))
        self.assertFalse((self.root / 'absent-original-memory').exists())
        self.assert_preserved(before)

    def test_completed_and_unavailable_reviews_are_cached_without_provider_retry(self):
        self.recorded_batch(memory=True)
        for status in ('completed', 'unavailable'):
            with self.subTest(status=status):
                self.save_sidecar(self.sidecar(status=status))
                before = self.snapshot()
                with self.offline():
                    result = plugin_cli.score_report(self.folder)
                self.assertEqual(result['status'], status)
                self.assertTrue(result['cached'])
                self.assertFalse(result['model_started'])
                self.assertEqual(result['provider_requests'], 0)
                self.assert_preserved(before)

    def test_explicit_retry_archives_previous_bytes_and_records_only_one_new_request(self):
        self.recorded_batch(memory=True)
        previous = self.sidecar(status='unavailable')
        previous.update(provider_requests=1, model_started=True)
        source = self.save_sidecar(previous)
        original_bytes = source.read_bytes()
        before = self.snapshot()
        del before[source]
        expected = self.sidecar()
        expected.update(provider_requests=1, model_started=True)
        with self.offline(permit_score=True), \
                patch.object(tendency_review, 'score_batch', return_value=expected) as scorer:
            result = plugin_cli.score_report(self.folder, retry=True)
            scorer.assert_called_once()
        self.assertFalse(result['cached'])
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['model_started'])
        self.assertEqual(result['provider_requests'], 1)
        archived = list((self.folder / 'review-attempts').iterdir())
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), original_bytes)
        self.assertEqual(json.loads(source.read_text(encoding='utf-8')), expected)
        self.assert_preserved(before)
        with self.offline():
            cached = plugin_cli.score_report(self.folder, retry=True)
        self.assertTrue(cached['cached'])
        self.assertFalse(cached['model_started'])
        self.assertEqual(cached['provider_requests'], 0)
        self.assertEqual(list((self.folder / 'review-attempts').iterdir()), archived)

    def test_successful_cached_review_ignores_retry_without_creating_attempt(self):
        self.recorded_batch()
        completed = self.sidecar()
        completed.update(provider_requests=1, model_started=True)
        self.save_sidecar(completed)
        before = self.snapshot()
        with self.offline():
            result = plugin_cli.score_report(self.folder, retry=True)
        self.assertTrue(result['cached'])
        self.assertFalse(result['model_started'])
        self.assertEqual(result['provider_requests'], 0)
        self.assertFalse((self.folder / 'review-attempts').exists())
        self.assert_preserved(before)

    def test_cli_retry_still_requires_allow_live_and_does_not_touch_prior_attempt(self):
        self.recorded_batch()
        self.save_sidecar(self.sidecar(status='unavailable'))
        before = self.snapshot()
        for flags in ([], ['--retry-live']):
            with self.subTest(flags=flags):
                output = io.StringIO()
                with self.offline(), redirect_stdout(output), \
                        patch.object(plugin_cli, 'score_report') as scorer:
                    status = plugin_cli.main(['score', '--data-dir', str(self.data),
                        '--batch-id', self.batch_id, *flags])
                self.assertEqual(status, 2)
                result = json.loads(output.getvalue())
                self.assertEqual(result['status'], 'error')
                self.assertIn('--allow-live', result['error'])
                scorer.assert_not_called()
                self.assertFalse((self.folder / 'review-attempts').exists())
                self.assert_preserved(before)

    def test_cli_explicit_retry_reaches_one_scoring_call_and_preserves_prior_attempt(self):
        self.recorded_batch()
        previous = self.save_sidecar(self.sidecar(status='unavailable')).read_bytes()
        expected = self.sidecar()
        expected.update(provider_requests=1, model_started=True)
        output = io.StringIO()
        with self.offline(permit_score=True), redirect_stdout(output), \
                patch.object(tendency_review, 'score_batch', return_value=expected) as scorer:
            status = plugin_cli.main(['score', '--data-dir', str(self.data),
                '--batch-id', self.batch_id, '--allow-live', '--retry-live'])
            scorer.assert_called_once()
        self.assertEqual(status, 0, output.getvalue())
        result = json.loads(output.getvalue())
        self.assertFalse(result['cached'])
        self.assertEqual(result['provider_requests'], 1)
        archived = list((self.folder / 'review-attempts').iterdir())
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), previous)

    def test_retry_rejects_invalid_prior_binding_before_archiving_or_charging(self):
        self.recorded_batch()
        invalid = self.sidecar(status='unavailable')
        invalid['input_sha256'] = 'e' * 64
        self.save_sidecar(invalid)
        before = self.snapshot()
        with self.offline(), self.assertRaisesRegex(ValueError, 'binding_mismatch'):
            plugin_cli.score_report(self.folder, retry=True)
        self.assertFalse((self.folder / 'review-attempts').exists())
        self.assert_preserved(before)

    def test_retry_rejects_attempt_directory_symlink_or_file_before_charging(self):
        self.recorded_batch()
        self.save_sidecar(self.sidecar(status='unavailable'))
        before = self.snapshot()
        destination = self.root / 'outside-attempts'
        destination.mkdir()
        attempts = self.folder / 'review-attempts'
        self.link(destination, attempts, directory=True)
        with self.offline(), self.assertRaisesRegex(ValueError, 'real directory'):
            plugin_cli.score_report(self.folder, retry=True)
        self.assertEqual(list(destination.iterdir()), [])
        self.assert_preserved(before)
        attempts.unlink()
        attempts.write_text('not a directory', encoding='utf-8')
        with self.offline(), self.assertRaisesRegex(ValueError, 'real directory'):
            plugin_cli.score_report(self.folder, retry=True)
        self.assertEqual(attempts.read_text(encoding='utf-8'), 'not a directory')
        self.assert_preserved(before)

    def test_retry_never_overwrites_an_existing_archive_on_name_collision(self):
        self.recorded_batch()
        self.save_sidecar(self.sidecar(status='unavailable'))
        attempts = self.folder / 'review-attempts'
        attempts.mkdir()
        existing = attempts / ('a' * 32 + '.json')
        existing.write_text('previous archived attempt', encoding='utf-8')
        before = self.snapshot()
        with self.offline(), patch.object(plugin_cli.uuid, 'uuid4') as unique:
            unique.return_value.hex = 'a' * 32
            with self.assertRaises(FileExistsError):
                plugin_cli.score_report(self.folder, retry=True)
        self.assert_preserved(before)

    def test_explicit_score_saves_one_review_and_second_call_uses_cache(self):
        self.recorded_batch(memory=True)
        expected = self.sidecar()
        before = self.snapshot()
        with self.offline(permit_score=True), \
                patch.object(tendency_review, 'score_batch', return_value=expected) as scorer:
            first = plugin_cli.score_report(self.folder)
            second = plugin_cli.score_report(self.folder)
            scorer.assert_called_once()
            self.assertEqual(Path(scorer.call_args.args[0]), self.folder / 'batch.json')
        self.assertFalse(first['cached'])
        self.assertEqual(first['status'], 'completed')
        self.assertTrue(second['cached'])
        self.assertFalse(second['model_started'])
        self.assertEqual(json.loads((self.folder / 'tendency-review.json').read_text(encoding='utf-8')),
                         expected)
        self.assert_preserved(before)

    def test_unavailable_review_does_not_become_scores_or_trigger_retry_on_render(self):
        self.recorded_batch()
        self.save_sidecar(self.sidecar(status='unavailable'))
        before = self.snapshot()
        with self.offline():
            rendered = self.embedded(plugin_cli.render_report(self.folder))
        self.assertNotIn('tendency_profile', rendered)
        self.assert_preserved(before)

    def test_embedded_scores_require_a_completed_bound_sidecar_on_every_render(self):
        self.recorded_batch()
        report = assessment_report.build_report(self.folder / 'batch.json')
        report['tendency_profile'] = self.sidecar()['profile']
        self.source.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
        for state in ('missing', 'unavailable'):
            with self.subTest(sidecar=state):
                if state == 'unavailable':
                    self.save_sidecar(self.sidecar(status='unavailable'))
                before = self.snapshot()
                with self.offline():
                    rendered = self.embedded(plugin_cli.render_report(self.folder))
                self.assertNotIn('tendency_profile', rendered)
                self.assertIn('tendency_profile', json.loads(self.source.read_text(encoding='utf-8')))
                self.assert_preserved(before)

    def test_tampered_sidecar_binding_quotes_and_aggregate_preserve_existing_view(self):
        self.recorded_batch()
        original = self.sidecar()
        self.save_sidecar(original)
        with self.offline():
            page = Path(plugin_cli.render_report(self.folder)['report_html'])
        before = page.read_bytes()
        mutations = {
            'batch hash': lambda value: value.update(batch_sha256='e' * 64),
            'input hash': lambda value: value.update(input_sha256='e' * 64),
            'invented quotation': lambda value: value['review']['ratings'][0]['evidence'][0].update(
                quote='This fabricated quotation never occurred in the fixture.'),
            'forged aggregate': lambda value: value['profile']['axes'][0].update(value=100),
        }
        for name, mutate in mutations.items():
            with self.subTest(tamper=name):
                changed = copy.deepcopy(original)
                mutate(changed)
                self.save_sidecar(changed)
                with self.offline(), self.assertRaises(ValueError):
                    plugin_cli.render_report(self.folder)
                self.assertEqual(page.read_bytes(), before)
                with self.offline(), self.assertRaises(ValueError):
                    plugin_cli.score_report(self.folder)

    def test_tampered_source_evidence_is_rejected_before_render_or_cache_return(self):
        self.recorded_batch()
        self.save_sidecar(self.sidecar())
        with self.offline():
            page = Path(plugin_cli.render_report(self.folder)['report_html'])
        before = page.read_bytes()
        batch = json.loads((self.folder / 'batch.json').read_text(encoding='utf-8'))
        evidence = self.folder / batch['runs'][0]['paths']['events']
        evidence.write_bytes(evidence.read_bytes() + b'\n')
        for action in (plugin_cli.render_report, plugin_cli.score_report):
            with self.subTest(action=action.__name__), self.offline(), \
                    self.assertRaisesRegex(ValueError, 'hash mismatch'):
                action(self.folder)
        self.assertEqual(page.read_bytes(), before)

    def test_existing_visual_report_cannot_borrow_ratings_with_different_provenance(self):
        self.recorded_batch()
        report = assessment_report.build_report(self.folder / 'batch.json')
        self.assertIn('visual_evidence', report)
        self.source.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
        self.save_sidecar(self.sidecar())
        with self.offline():
            page = Path(plugin_cli.render_report(self.folder)['report_html'])
        before = page.read_bytes()
        next(iter(report['provenance'].values()))['meta_sha256'] = 'e' * 64
        self.source.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
        with self.offline(), self.assertRaises(ValueError):
            plugin_cli.render_report(self.folder)
        self.assertEqual(page.read_bytes(), before)

    def test_sidecar_symlink_is_rejected_without_reading_or_overwriting_target(self):
        self.recorded_batch()
        outside = self.root / 'outside-review.json'
        outside.write_text(json.dumps(self.sidecar()), encoding='utf-8')
        self.link(outside, self.folder / 'tendency-review.json')
        before = (outside.read_bytes(), outside.stat().st_mtime_ns)
        for action in (plugin_cli.render_report, plugin_cli.score_report):
            with self.subTest(action=action.__name__), self.offline(), \
                    self.assertRaisesRegex(ValueError, 'symlink'):
                action(self.folder)
        self.assertEqual((outside.read_bytes(), outside.stat().st_mtime_ns), before)

    def test_temporary_sidecar_symlink_is_rejected_before_scoring(self):
        self.recorded_batch()
        outside = self.root / 'outside-temp.json'
        outside.write_text('untouched', encoding='utf-8')
        self.link(outside, self.folder / 'tendency-review.json.tmp')
        with self.offline(), self.assertRaisesRegex(ValueError, 'symlink'):
            plugin_cli.score_report(self.folder)
        self.assertEqual(outside.read_text(encoding='utf-8'), 'untouched')
        self.assertFalse((self.folder / 'tendency-review.json').exists())

    def test_mock_score_and_render_never_call_credentials_or_provider(self):
        self.recorded_batch(mode='mock')
        before = self.snapshot()
        with self.offline(permit_score=True):
            result = plugin_cli.score_report(self.folder)
            rendered = self.embedded(plugin_cli.render_report(self.folder))
        self.assertEqual(result['status'], 'no_eligible_runs')
        self.assertFalse(result['model_started'])
        self.assertNotIn('tendency_profile', rendered)
        sidecar = json.loads((self.folder / 'tendency-review.json').read_text(encoding='utf-8'))
        self.assertTrue(all(axis['value'] is None for axis in sidecar['profile']['axes']))
        with self.offline():
            self.assertTrue(plugin_cli.score_report(self.folder)['cached'])
        self.assert_preserved(before)


if __name__ == '__main__':
    unittest.main()
