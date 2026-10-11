"""Synthetic evidence contract tests; no model, credentials, or memory access."""
import copy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime/frontend/scripts'))
import report_validation as validation

HASH = 'a' * 64
AXES = list(validation.TENDENCY_AXES)


def empty_report(version='v2'):
    result = {
        'schema_version': 'harness-profile.' + version, 'profile': 'tendency-test',
        'generated_at': '2026-10-11T08:00:00Z',
        'scope': 'prospective_behavior_profile' if version == 'v2' else 'exploratory_descriptive_profile',
        'causal_attribution': 'not_established', 'character_assignment': None,
        'dimensions': [{'id': f'D{i}', 'label': f'维度{i}', 'score': None, 'grade': None,
                        'status': 'not_established', 'observation_ids': [], 'note': 'Synthetic fixture'}
                       for i in range(1, 8)],
        'observations': [], 'inventory': {'questionnaire_runs': 0, 'task_runs': 0,
            'total_runs': 0, 'reviewed_runs': 0, 'audit_status': {}, 'prompt_integrity': {}},
        'prompt_mismatches': [], 'provenance': {}, 'limitations': ['Synthetic offline fixture'],
    }
    if version == 'v2':
        result['inventory']['included_runs'] = 0
    return result


def profile_report():
    result = empty_report()
    result['tendency_profile'] = {
        'rule_version': 'behavior-tendency.v1', 'basis': 'model_review', 'model': 'synthetic-reviewer',
        'source_batch_sha256': HASH, 'review_input_sha256': HASH,
        'axes': [{'id': axis, 'value': None, 'samples': []} for axis in AXES],
    }
    return result


def sample_level(axis, index):
    return (index % 3) * 2 if axis in ('autonomous', 'staging') else index % 5


def add_sample(report, axis, item, level, *, run_id=None, source=None):
    source = source or ('session' if (axis == 'verification' and level > 0)
                       or (axis in ('autonomous', 'staging') and level == 4) else 'answer')
    rid = run_id or 'run-' + str(len(report['provenance']))
    sample = {'run_id': rid, 'item_id': item, 'task_family': validation.TENDENCY_FAMILIES[item],
              'level': level, 'reason': 'A synthetic rubric decision.',
              'evidence': [{'source': source, 'quote': 'A synthetic quotation.'}]}
    target = next(row for row in report['tendency_profile']['axes'] if row['id'] == axis)
    target['samples'].append(sample)
    target['value'] = validation.tendency_value(target['samples'])
    if rid in report['provenance']:
        return sample
    report['provenance'][rid] = {
        'item_id': item, 'meta': 'fixture/meta.json', 'session_id': None, 'meta_sha256': HASH,
        'events_sha256': HASH, 'public_answer_sha256': HASH, 'session_log_sha256': None,
        'config_dump_sha256': None, 'grade_path': 'fixture/grade.json', 'grade_sha256': HASH,
    }
    did = 'D' + str(AXES.index(axis) + 1)
    condition = {key: None for key in validation.CONDITION['required']}
    condition.update(profile=report['profile'], prompt_hash_source='synthetic',
                     injection={'status': 'none_observed', 'type': 'none', 'digests': [],
                                'source': 'synthetic', 'evidence_issue': None},
                     condition_id='baseline', subject='synthetic')
    oid = rid + ':observation'
    report['observations'].append({
        'id': oid, 'dimensions': [did], 'title': 'Synthetic observation', 'statement': 'Synthetic',
        'limits': 'Not a real assessment', 'protocol': 'task', 'expected_prompt_sha256': HASH,
        'run_ids': [rid], 'evidence': [{'run_id': rid, 'source': source, 'quote': 'Synthetic'}],
        'condition_slices': [{'slice_id': rid + ':slice', 'conditions': condition,
                              'missing_conditions': [], 'run_ids': [rid], 'n': 1}],
        'pooled_effect_estimate': None, 'interpretation_status': 'automatic_evidence_observation',
    })
    dimension = next(row for row in report['dimensions'] if row['id'] == did)
    dimension['status'] = 'limited_observation'
    dimension['observation_ids'].append(oid)
    for key in ('task_runs', 'total_runs', 'included_runs'):
        report['inventory'][key] += 1
    return sample


def complete_report():
    report = profile_report()
    for index, axis in enumerate(AXES):
        add_sample(report, axis, validation.TENDENCY_AXES[axis][0], index % 5)
    return report


def invalid_cases():
    cases = []
    def changed(name, change):
        report = complete_report()
        change(report)
        cases.append((name, report))
    def first(report):
        return report['tendency_profile']['axes'][0]
    changed('forged total', lambda r: first(r).update(value=100))
    changed('missing run', lambda r: first(r)['samples'][0].update(run_id='absent'))
    changed('wrong provenance item', lambda r: r['provenance']['run-0'].update(item_id='V1-D1-receipt'))
    changed('wrong family', lambda r: first(r)['samples'][0].update(task_family='money_conversion'))
    changed('repeated axis', lambda r: r['tendency_profile']['axes'][1].update(id='autonomous'))
    changed('missing axis', lambda r: r['tendency_profile']['axes'].pop())
    changed('duplicate run', lambda r: first(r)['samples'].append(copy.deepcopy(first(r)['samples'][0])))
    changed('unknown is not zero', lambda r: first(r).update(samples=[], value=0))
    changed('observed is not null', lambda r: first(r).update(value=None))
    changed('empty quote', lambda r: first(r)['samples'][0]['evidence'][0].update(quote=' \n'))
    changed('empty evidence', lambda r: first(r)['samples'][0].update(evidence=[]))
    changed('empty reason', lambda r: first(r)['samples'][0].update(reason=' \t'))
    changed('empty reviewer', lambda r: r['tendency_profile'].update(model=' '))
    changed('missing answer digest', lambda r: r['provenance']['run-0'].update(public_answer_sha256=None))
    changed('malformed digest', lambda r: r['provenance']['run-0'].update(public_answer_sha256=HASH+'\n'))
    changed('malformed review binding', lambda r: r['tendency_profile'].update(review_input_sha256=HASH+'\n'))
    changed('invalid sample level', lambda r: first(r)['samples'][0].update(level=5))
    def unsupported_anchor(report):
        first(report)['samples'][0]['level'] = 1
        first(report)['value'] = 25
    changed('unsupported ordinal anchor', unsupported_anchor)
    changed('fractional level', lambda r: first(r)['samples'][0].update(level=1.5))
    changed('bool level', lambda r: first(r)['samples'][0].update(level=True))
    changed('fractional axis', lambda r: first(r).update(value=0.5))
    changed('unexpected key', lambda r: first(r)['samples'][0].update(confidence=0.9))
    changed('unsupported source', lambda r: first(r)['samples'][0]['evidence'][0].update(source='memory'))
    for axis in AXES:
        def mismatched_source(report, axis=axis):
            row = next(row for row in report['tendency_profile']['axes'] if row['id'] == axis)
            row['samples'][0]['evidence'][0]['source'] = 'answer' if axis == 'verification' else 'grader'
        changed('wrong evidence source for '+axis, mismatched_source)
    for axis in ('autonomous', 'staging'):
        def action_without_execution(report, axis=axis):
            row = next(row for row in report['tendency_profile']['axes'] if row['id'] == axis)
            row['samples'][0].update(level=4)
            row['value'] = 100
        changed('unsupported action for '+axis, action_without_execution)
    changed('mock samples', lambda r: r.update(visual_evidence={'rule_version': 'whale-theme.v1', 'mode': 'mock', 'signals': []}))
    for axis, item in [('autonomous', 'V2-D1-plan'), ('dissent', 'V1-D5-claim-supported'),
                       ('staging', 'V1-D6-ready')]:
        def wrong_item(report, axis=axis, item=item):
            sample = next(row for row in report['tendency_profile']['axes'] if row['id'] == axis)['samples'][0]
            sample.update(item_id=item, task_family=validation._portrait.TASK_FAMILIES[item])
            report['provenance'][sample['run_id']]['item_id'] = item
        changed('unsupported '+item, wrong_item)
    return cases


class TendencyProfileTests(unittest.TestCase):
    def test_old_reports_and_unknown_are_readable(self):
        for report in (empty_report('v1'), empty_report('v2'), profile_report()):
            validation.validate_report(report)

    def test_all_relevant_items_accept_bound_sources(self):
        report = profile_report()
        for axis, items in validation.TENDENCY_AXES.items():
            for index, item in enumerate(items):
                add_sample(report, axis, item, sample_level(axis, index))
        add_sample(report, 'autonomous', 'V1-D1-config', 4, source='grader')
        validation.validate_report(report)

    def test_balancing_repeats_and_paired_items(self):
        report = profile_report()
        for _ in range(4):
            add_sample(report, 'verification', 'V2-D2-checklist', 0)
        add_sample(report, 'verification', 'V2-D2-conflict', 4)
        add_sample(report, 'verification', 'V1-D2-money', 4)
        axis = report['tendency_profile']['axes'][1]
        self.assertEqual(axis['value'], 75)  # family 1 mean 2, family 2 mean 4
        validation.validate_report(report)
        axis['samples'].reverse()
        report['tendency_profile']['axes'].reverse()
        validation.validate_report(report)

    def test_half_up_rounding_and_zero(self):
        report = profile_report()
        add_sample(report, 'verbosity', 'V1-D3-backup', 0)
        self.assertEqual(report['tendency_profile']['axes'][2]['value'], 0)
        add_sample(report, 'verbosity', 'V1-D3-backup', 1)
        self.assertEqual(report['tendency_profile']['axes'][2]['value'], 13)
        validation.validate_report(report)

    def test_forged_or_unbound_ratings_fail(self):
        for name, report in invalid_cases():
            with self.subTest(name=name), self.assertRaises(ValueError):
                validation.validate_report(report)


if __name__ == '__main__':
    unittest.main()
