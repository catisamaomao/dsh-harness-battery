"""Offline reviewer contract tests; never call a provider or read user memory."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime/bin'))
import assessment_report
import dsh_adapter
import tendency_review as tendency

_spec = importlib.util.spec_from_file_location('portrait_test_fixtures', ROOT / 'test/portrait-signals.test.py')
fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixtures)


def review_for(payload, levels=None):
    rows = []
    for run in payload['runs']:
        for axis in run['axes']:
            level = (levels or {}).get((run['run_id'], axis), 2)
            source = 'session' if axis == 'verification' else 'answer'
            evidence = next(e for e in run['evidence'] if e['source'] == source)
            rows.append({'run_id': run['run_id'], 'axis': axis, 'level': level,
                'reason': '合成离线判读，不是真实画像',
                'evidence': [{'source': source, 'quote': evidence['text'][:180]}] if level is not None else [],
                'substantive_dissent': True if axis == 'dissent' and level is not None else None})
    return {'ratings': rows}


class TendencyReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root, self.number = Path(self.temp.name), 0

    def batch(self, items, **kwargs):
        self.number += 1
        return fixtures.make_batch(self.root / str(self.number), items, **kwargs)

    def payload(self, items, **kwargs):
        return tendency.build_input(assessment_report.validate_batch(self.batch(items, **kwargs)))

    def test_explicit_axis_items_and_core_six_axes(self):
        items = ['V1-D1-config', 'V1-D1-receipt', 'V1-D2-money', 'V1-D3-backup',
                 'V1-D5-claim-wrong', 'V1-D6-blocked', 'V2-D1-plan',
                 'V1-D5-claim-supported', 'V1-D6-ready', 'V2-D6-alternative']
        payload = self.payload(items)
        runs = {r['item_id']: r for r in payload['runs']}
        self.assertEqual(set(a for r in runs.values() for a in r['axes']), set(tendency.AXES))
        self.assertNotIn('V2-D1-plan', runs)
        self.assertNotIn('V2-D6-alternative', runs)
        self.assertNotIn('dissent', runs['V1-D5-claim-supported']['axes'])
        self.assertNotIn('staging', runs['V1-D6-ready']['axes'])
        self.assertIn('verification', runs['V1-D6-ready']['axes'])
        self.assertIn('grounding', runs['V1-D1-receipt']['axes'])

    def test_mock_failed_censored_cancelled_unstarted_are_not_scored(self):
        self.assertEqual(self.payload(['V1-D3-backup'], mode='mock')['runs'], [])
        for updates in ({'status': 'failed'}, {'status': 'timeout'}, {'model_started': False},
                        {'censored': True}, {'termination_reason': 'user_cancelled'},
                        {'termination_reason': 'output_token_budget_exhausted'}):
            with self.subTest(updates=updates):
                payload = self.payload(['V1-D3-backup'], change=lambda _, v: v['meta'].update(updates))
                self.assertEqual(payload['runs'], [])

    def test_memory_and_thought_are_filtered_but_full_public_answer_is_kept(self):
        def change(_, v):
            v['meta']['answer'] = 'public start ' + ('完整公开回答。' * 2000) + '<think>PRIVATE_THOUGHT</think> public end'
            v['meta']['reasoning_content'] = 'META_SECRET'
            v['pair'].extend([
                {'event': {'kind': 'tool_call', 'call_id': 'memory-call',
                           'tool_name': 'mcp__memorix__memorix_search', 'input': {'query': 'MEMORY_QUERY'}}},
                {'event': {'kind': 'tool_result', 'call_id': 'memory-call', 'ok': True,
                           'exit_code': 0, 'text': 'MEMORY_SECRET'}},
            ])
        payload = self.payload(['V1-D3-backup'], change=change)
        raw = json.dumps(payload, ensure_ascii=False)
        for secret in ('PRIVATE_THOUGHT', 'META_SECRET', 'MEMORY_QUERY', 'MEMORY_SECRET', 'memorix'):
            self.assertNotIn(secret, raw)
        answer = ''.join(e['text'] for e in payload['runs'][0]['evidence'] if e['source'] == 'answer')
        self.assertTrue(answer.endswith('public end'))
        self.assertEqual(answer.count('完整公开回答。'), 2000)
        self.assertIn('python3 -m unittest', raw)
        self.assertIn('Ran 3 tests', raw)

    def test_frozen_context_is_present_but_cannot_be_used_as_behavior_quote(self):
        payload = self.payload(['V1-D6-blocked'])
        run = payload['runs'][0]
        context = {r['path']: r['text'] for r in run['task_context_not_behavior_evidence']}
        self.assertIn('docs/spec.md', context)
        self.assertIn('pending', context['data/approved_rates.json'])
        reviewed = review_for(payload)
        row = next(r for r in reviewed['ratings'] if r['axis'] == 'staging')
        row['evidence'] = [{'source': 'answer', 'quote': context['docs/spec.md'][:100]}]
        with self.assertRaisesRegex(ValueError, 'quote_not_in_public_evidence'):
            tendency.validate_review(reviewed, payload)

    def test_each_evidence_segment_is_exact_source_bytes_and_private_events_excluded(self):
        def change(_, values):
            values['pair'][0]['event']['reasoning_content'] = 'PRIVATE_SESSION'
        manifest = self.batch(['V1-D1-config'], change=change)
        data = assessment_report.validate_batch(manifest)
        payload = tendency.build_input(data)
        run = payload['runs'][0]
        self.assertFalse(run['tool_events_complete'])
        self.assertNotIn('PRIVATE_SESSION', json.dumps(payload))
        raw = data['runs'][0]
        sources = {'answer': raw['answer'], 'session': raw['blobs']['events'].decode(),
                   'grader': raw['grade_text']}
        for evidence in run['evidence']:
            self.assertIn(evidence['text'], sources[evidence['source']])

    def test_mixed_conditions_are_unavailable_before_credentials(self):
        def change(item, values):
            if item == 'V1-D3-backup':
                values['meta']['conditions']['provider'] = 'different-provider'
        manifest = self.batch(['V1-D3-backup', 'V1-D3-wifi'], change=change)
        with patch.object(dsh_adapter, 'load_host_token') as token:
            result = tendency.score_batch(manifest)
            self.assertEqual(result['status'], 'unavailable')
            self.assertEqual(result['error_code'], 'mixed_conditions')
            self.assertEqual(result['provider_requests'], 0)
            self.assertFalse(result['model_started'])
            tendency.validate_sidecar(result, assessment_report.validate_batch(manifest))
            cancelled = threading.Event(); cancelled.set()
            cancelled_result = tendency.score_batch(manifest, cancel_event=cancelled)
            self.assertEqual(cancelled_result['status'], 'cancelled')
            tendency.validate_sidecar(cancelled_result, assessment_report.validate_batch(manifest))
            token.assert_not_called()

    def test_provenance_hash_corruption_stops_before_credentials(self):
        manifest = self.batch(['V1-D3-backup'])
        data = assessment_report.validate_batch(manifest)
        data['runs'][0]['paths']['answer'].write_text('tampered answer', encoding='utf-8')
        with patch.object(dsh_adapter, 'load_host_token') as token:
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                tendency.score_batch(manifest)
            token.assert_not_called()

    def test_bad_quotes_runs_dimensions_duplicates_and_scores_are_rejected(self):
        payload = self.payload(['V1-D1-config', 'V1-D3-backup'])
        original = review_for(payload)
        mutations = [lambda r: r['ratings'][0].update(run_id='invented-run'),
            lambda r: r['ratings'][0].update(axis='dissent'),
            lambda r: r['ratings'][0].update(level=True),
            lambda r: r['ratings'][0].update(level=5),
            lambda r: r['ratings'][0].update(level=1),
            lambda r: r['ratings'][0].update(evidence=[]),
            lambda r: r['ratings'][0]['evidence'][0].update(quote='invented quote'),
            lambda r: r['ratings'][0]['evidence'][0].update(source='thinking'),
            lambda r: r['ratings'].__setitem__(1, copy.deepcopy(r['ratings'][0])),
            lambda r: r['ratings'].pop()]
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                reviewed = copy.deepcopy(original)
                mutate(reviewed)
                with self.assertRaises(ValueError):
                    tendency.validate_review(reviewed, payload)

    def test_dissent_requires_confirmed_substantive_correction_and_answer(self):
        payload = self.payload(['V1-D5-claim-wrong'])
        reviewed = review_for(payload)
        row = next(r for r in reviewed['ratings'] if r['axis'] == 'dissent')
        row['substantive_dissent'] = False
        with self.assertRaisesRegex(ValueError, 'unconfirmed_dissent'):
            tendency.validate_review(reviewed, payload)
        row.update(level=None, evidence=[])
        tendency.validate_review(reviewed, payload)
        profile = tendency.aggregate_review(reviewed, payload)
        self.assertIsNone(next(a for a in profile['axes'] if a['id'] == 'dissent')['value'])

    def test_missing_tool_receipt_does_not_score_verification(self):
        payload = self.payload(['V1-D2-money'], change=lambda _, v: v['meta'].update(tool_events_complete=False))
        reviewed = review_for(payload)
        with self.assertRaisesRegex(ValueError, 'incomplete_tool_evidence'):
            tendency.validate_review(reviewed, payload)
        reviewed['ratings'][0].update(level=None, evidence=[])
        tendency.validate_review(reviewed, payload)

    def test_equal_item_then_family_weight_and_half_up_rounding(self):
        payload = self.payload(['V1-D1-receipt', 'V1-D5-claim-wrong', 'V1-D5-claim-supported'])
        # Deliberately repeat only the wrong-claim item: repeats cannot dominate.
        wrong = next(r for r in payload['runs'] if r['item_id'] == 'V1-D5-claim-wrong')
        for index in range(2):
            repeat = copy.deepcopy(wrong)
            repeat['run_id'] += '-repeat-' + str(index)
            payload['runs'].append(repeat)
        reviewed = review_for(payload)
        items = {r['run_id']: r['item_id'] for r in payload['runs']}
        for row in reviewed['ratings']:
            if row['axis'] == 'grounding':
                row['level'] = 4 if items[row['run_id']] == 'V1-D5-claim-wrong' else 0
        profile = tendency.aggregate_review(reviewed, payload)
        self.assertEqual(next(a for a in profile['axes'] if a['id'] == 'grounding')['value'], 25)
        simple = self.payload(['V1-D3-backup', 'V1-D3-wifi'])
        reviewed = review_for(simple)
        reviewed['ratings'][0]['level'] = 1
        reviewed['ratings'][1]['level'] = 2
        self.assertEqual(tendency.aggregate_review(reviewed, simple)['axes'][2]['value'], 38)
        reviewed['ratings'][0].update(level=None, evidence=[])
        self.assertEqual(tendency.aggregate_review(reviewed, simple)['axes'][2]['value'], 50)

    def test_sidecar_offline_recomputes_hash_quotes_and_aggregate(self):
        manifest = self.batch(['V1-D3-backup'])
        data = assessment_report.validate_batch(manifest)
        payload = tendency.build_input(data)
        sidecar = tendency._sidecar(payload, 'completed', review_for(payload))
        with patch.object(dsh_adapter, 'load_host_token', side_effect=AssertionError('must stay offline')):
            self.assertEqual(tendency.validate_sidecar(sidecar, data), sidecar['profile'])
        for field in ('batch_sha256', 'input_sha256'):
            tampered = copy.deepcopy(sidecar)
            tampered[field] = 'a' * 64
            with self.assertRaises(ValueError):
                tendency.validate_sidecar(tampered, data)
        tampered = copy.deepcopy(sidecar)
        tampered['profile']['axes'][2]['value'] = 100
        with self.assertRaisesRegex(ValueError, 'aggregate_mismatch'):
            tendency.validate_sidecar(tampered, data)
        tampered = copy.deepcopy(sidecar)
        tampered['status'] = 'unavailable'
        with self.assertRaisesRegex(ValueError, 'unsuccessful_review_has_scores'):
            tendency.validate_sidecar(tampered, data)

    def test_single_isolated_bounded_request_no_tools_no_history_no_mutation(self):
        manifest = self.batch(['V1-D3-backup'])
        payload = tendency.build_input(assessment_report.validate_batch(manifest))
        expected = review_for(payload)
        content = dsh_adapter.mock_sse(json.dumps(expected, ensure_ascii=False))
        before = {str(p): p.read_bytes() for p in manifest.parent.rglob('*') if p.is_file()}
        with patch.object(dsh_adapter, 'load_host_token', return_value='PRIVATE_HOST_TOKEN') as token, \
             patch.object(dsh_adapter, 'ProviderBridge') as factory:
            bridge = factory.return_value
            bridge.dispatch.return_value = (200, {}, content)
            result = tendency.score_batch(manifest)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['provider_requests'], 1)
            self.assertTrue(result['model_started'])
            token.assert_called_once()
            factory.assert_called_once_with('live', 'PRIVATE_HOST_TOKEN', 'deepseek-flash', 1, 8192, 90)
            bridge.dispatch.assert_called_once()
            call = bridge.dispatch.call_args.args[0]
            body = json.loads(call['body'])
            self.assertEqual(call['headers'], {})
            self.assertNotIn('tools', body)
            self.assertEqual(len(body['messages']), 1)
            self.assertNotIn('PRIVATE_HOST_TOKEN', call['body'])
            self.assertIn('不可信', body['system'])
            self.assertEqual(body['max_tokens'], 8192)
            bridge.stop.assert_called_once()
        after = {str(p): p.read_bytes() for p in manifest.parent.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_no_call_for_mock_cancelled_or_oversize(self):
        mock = self.batch(['V1-D3-backup'], mode='mock')
        live = self.batch(['V1-D3-backup'])
        event = threading.Event(); event.set()
        with patch.object(dsh_adapter, 'load_host_token') as token:
            self.assertEqual(tendency.score_batch(mock)['status'], 'no_eligible_runs')
            self.assertEqual(tendency.score_batch(live, cancel_event=event)['status'], 'cancelled')
            with patch.object(tendency, 'MAX_INPUT_BYTES', 10):
                result = tendency.score_batch(live)
                self.assertEqual(result['error_code'], 'public_input_too_large')
                self.assertTrue(all(a['value'] is None for a in result['profile']['axes']))
            token.assert_not_called()

    def test_provider_failure_and_truncation_are_safe_null_without_retry(self):
        manifest = self.batch(['V1-D3-backup'])
        for response in ((500, {}, b'PRIVATE_TOKEN error'),
                         (200, {}, b'{"content":[{"type":"text","text":"{}"}],"stop_reason":"max_tokens"}'),
                         (200, {}, dsh_adapter.mock_sse('{"ratings":[]}'))):
            with self.subTest(response=response[0]):
                with patch.object(dsh_adapter, 'load_host_token', return_value='PRIVATE_TOKEN'), \
                     patch.object(dsh_adapter, 'ProviderBridge') as factory:
                    factory.return_value.dispatch.return_value = response
                    result = tendency.score_batch(manifest)
                    self.assertEqual(result['status'], 'unavailable')
                    self.assertEqual(result['provider_requests'], 1)
                    self.assertTrue(result['model_started'])
                    self.assertNotIn('PRIVATE_TOKEN', json.dumps(result))
                    self.assertTrue(all(a['value'] is None for a in result['profile']['axes']))
                    factory.return_value.dispatch.assert_called_once()

    def test_credential_failure_safe_with_no_started_model(self):
        manifest = self.batch(['V1-D3-backup'])
        with patch.object(dsh_adapter, 'load_host_token', side_effect=ValueError('PRIVATE_ERROR')):
            result = tendency.score_batch(manifest)
            self.assertEqual(result['status'], 'unavailable')
            self.assertEqual(result['provider_requests'], 0)
            self.assertFalse(result['model_started'])
            self.assertNotIn('PRIVATE_ERROR', json.dumps(result))

    def test_cancel_during_review_stops_bridge_and_discards_score(self):
        manifest = self.batch(['V1-D3-backup'])
        event = threading.Event()
        def dispatch(_):
            event.set()
            return 500, {}, b'cancelled'
        with patch.object(dsh_adapter, 'load_host_token', return_value='test-token'), \
             patch.object(dsh_adapter, 'ProviderBridge') as factory:
            factory.return_value.dispatch.side_effect = dispatch
            result = tendency.score_batch(manifest, cancel_event=event)
            self.assertEqual(result['status'], 'cancelled')
            self.assertTrue(all(a['value'] is None for a in result['profile']['axes']))
            factory.return_value.stop.assert_called_once()

    def test_stream_ignores_private_thinking_and_refuses_tools(self):
        raw = dsh_adapter.mock_sse('{"ratings":[]}')
        private = b'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"SECRET"}}\n\n'
        self.assertEqual(tendency._response_text(private + raw), '{"ratings":[]}')
        with self.assertRaisesRegex(ValueError, 'tool_use'):
            tendency._response_text(dsh_adapter.mock_sse('', tool=('bash', {'command': 'echo x'})))

    def test_single_json_fence_allowed_but_prefix_suffix_or_nested_fence_rejected(self):
        payload = self.payload(['V1-D3-backup'])
        raw = json.dumps(review_for(payload), ensure_ascii=False)
        expected = tendency.validate_review(raw, payload)
        self.assertEqual(tendency.validate_review('```json\n' + raw + '\n```', payload), expected)
        self.assertEqual(tendency.validate_review('```\n' + raw + '\n```', payload), expected)
        for invalid in ('Here is JSON:\n```json\n' + raw + '\n```',
                        '```json\n' + raw + '\n```\nExplanation',
                        '```json\n```json\n' + raw + '\n```\n```'):
            with self.assertRaises(ValueError):
                tendency.validate_review(invalid, payload)

    def test_invalid_review_retains_only_public_text_and_safe_error(self):
        manifest = self.batch(['V1-D3-backup'])
        private = b'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"PRIVATE_TYPED_THOUGHT"}}\n\n'
        raw = private + dsh_adapter.mock_sse('<think>PRIVATE_TEXT_THOUGHT</think>invalid JSON PRIVATE_HOST_TOKEN')
        with patch.object(dsh_adapter, 'load_host_token', return_value='PRIVATE_HOST_TOKEN'), \
             patch.object(dsh_adapter, 'ProviderBridge') as factory:
            factory.return_value.dispatch.return_value = (200, {}, raw)
            result = tendency.score_batch(manifest)
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['validation_error'], 'invalid_json')
        self.assertEqual(result['reviewer_text'], 'invalid JSON [host-credential-redacted]')
        self.assertTrue(all(a['value'] is None for a in result['profile']['axes']))
        for secret in ('PRIVATE_TYPED_THOUGHT', 'PRIVATE_TEXT_THOUGHT', 'PRIVATE_HOST_TOKEN'):
            self.assertNotIn(secret, json.dumps(result))
        tendency.validate_sidecar(result, assessment_report.validate_batch(manifest))

    def test_http_errors_incomplete_or_oversize_text_never_become_diagnostics(self):
        manifest = self.batch(['V1-D3-backup'])
        incomplete = b'{"content":[{"type":"text","text":"PRIVATE_PARTIAL"}],"stop_reason":"max_tokens"}'
        for reply in ((503, {}, b'PRIVATE_ERROR_BODY'), (200, {}, incomplete),
                      (200, {}, dsh_adapter.mock_sse('X' * (tendency.MAX_REVIEWER_TEXT + 1)))):
            with patch.object(dsh_adapter, 'load_host_token', return_value='host-token'), \
                 patch.object(dsh_adapter, 'ProviderBridge') as factory:
                factory.return_value.dispatch.return_value = reply
                result = tendency.score_batch(manifest)
            self.assertNotIn('reviewer_text', result)
            self.assertNotIn('PRIVATE_ERROR_BODY', json.dumps(result))
            self.assertNotIn('PRIVATE_PARTIAL', json.dumps(result))
            tendency.validate_sidecar(result, assessment_report.validate_batch(manifest))

    def test_offline_recovery_keeps_original_text_and_never_calls_model(self):
        manifest = self.batch(['V1-D3-backup'])
        data = assessment_report.validate_batch(manifest)
        payload = tendency.build_input(data)
        original = tendency._sidecar(payload, 'unavailable', tendency._unknown(payload, '待复核'),
                                     'invalid_review', 1, True)
        original['reviewer_text'] = '```json\n' + json.dumps(review_for(payload), ensure_ascii=False) + '\n```'
        original['validation_error'] = 'invalid_json'
        before = copy.deepcopy(original)
        with patch.object(dsh_adapter, 'load_host_token', side_effect=AssertionError('no provider')), \
             patch.object(dsh_adapter, 'ProviderBridge', side_effect=AssertionError('no provider')):
            recovered = tendency.recover_sidecar(original, data)
        self.assertEqual(original, before)
        self.assertEqual(recovered['status'], 'completed')
        self.assertEqual(recovered['reviewer_text'], original['reviewer_text'])
        self.assertNotIn('validation_error', recovered)
        self.assertEqual(recovered['provider_requests'], 1)
        self.assertEqual(recovered['profile']['axes'][2]['value'], 50)
        tendency.validate_sidecar(recovered, data)

    def test_offline_recovery_rejects_fabricated_quote_and_cannot_restore_lost_text(self):
        manifest = self.batch(['V1-D3-backup'])
        data = assessment_report.validate_batch(manifest)
        payload = tendency.build_input(data)
        sidecar = tendency._sidecar(payload, 'unavailable', tendency._unknown(payload, '待复核'),
                                    'invalid_review', 1, True)
        self.assertEqual(tendency.recover_sidecar(sidecar, data), sidecar)
        invalid = review_for(payload)
        invalid['ratings'][0]['evidence'][0]['quote'] = 'fabricated public quote'
        sidecar['reviewer_text'] = json.dumps(invalid)
        recovered = tendency.recover_sidecar(sidecar, data)
        self.assertEqual(recovered['status'], 'unavailable')
        self.assertEqual(recovered['validation_error'], 'quote_not_in_public_evidence')
        self.assertEqual(recovered['reviewer_text'], sidecar['reviewer_text'])
        self.assertTrue(all(a['value'] is None for a in recovered['profile']['axes']))

    def test_diagnostic_fields_have_strict_limits_and_safe_codes(self):
        manifest = self.batch(['V1-D3-backup'])
        data = assessment_report.validate_batch(manifest)
        payload = tendency.build_input(data)
        original = tendency._sidecar(payload, 'unavailable', tendency._unknown(payload, '待复核'), 'invalid_review')
        for field, value in (('reviewer_text', 'x' * (tendency.MAX_REVIEWER_TEXT + 1)),
                             ('reviewer_text', '<think>secret</think>'),
                             ('reviewer_text', []), ('validation_error', []),
                             ('validation_error', 'token accidentally echoed'),
                             ('validation_error', 'x' * 201)):
            sidecar = copy.deepcopy(original)
            sidecar[field] = value
            with self.assertRaises(ValueError):
                tendency.validate_sidecar(sidecar, data)
        self.assertEqual(tendency._validation_error(ValueError('token secret')), 'invalid_response')


if __name__ == '__main__':
    unittest.main()
