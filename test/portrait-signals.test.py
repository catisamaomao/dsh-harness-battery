"""Presentation evidence contracts using synthetic, hash-bound offline batches.

No provider, credentials, user memory, or candidate process is accessed. The
fixtures exercise the real frozen bundle loader, report builder and validators.
"""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'runtime/bin'))
import assessment
import assessment_report as report_builder
import portrait_signals as signals

spec = importlib.util.spec_from_file_location('report_validation', ROOT/'runtime/frontend/scripts/report_validation.py')
validation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation)


def public_pair(command='python3 -m unittest', text='Ran 3 tests in 0.01s\nOK', ok=True):
    events = [
        {'kind':'tool_call','call_id':'synthetic-call','tool_name':'bash','input':{'command':command}},
        {'kind':'tool_result','call_id':'synthetic-call','ok':ok,'exit_code':0 if ok else 1,'text':text},
    ]
    return [{'event':event,'raw':json.dumps(event, ensure_ascii=False),'line':index}
            for index,event in enumerate(events,1)]


def make_batch(destination, items, *, mode='live', repeats=1, change=None):
    """Create wholly synthetic source files, with real frozen prompt binding."""
    bundle = ROOT/'runtime/batteries/extended-v2'
    plan_path = assessment.create_plan(destination, bundle_dir=bundle, items=items,
        condition='baseline', mode=mode, repeats=repeats, label='Synthetic portrait fixture')
    plan = assessment.load_plan(plan_path)
    frozen = assessment.bundle_loader()(bundle)
    by_id = {item['id']:item for item in frozen['items']}
    manifest = {'schema_version':'harness-assessment-batch.v1','batch_id':plan['batch_id'],
        'profile':plan['label'],'subject':'isolated-headless','mode':mode,
        'bundle_sha256':frozen['bundle_sha256'],'plan_path':'plan.json',
        'plan_sha256':assessment.sha(plan_path),'planned_runs':len(plan['schedule']),
        'status':'completed','runs':[],'not_started_slots':[]}
    for job in plan['schedule']:
        item = by_id[job['item_id']]
        run_dir = destination/'runs'/job['slot_id']; run_dir.mkdir(parents=True)
        prompt = (bundle/item['prompt_path']).read_bytes()
        pair = public_pair()
        meta = {'schema_version':'dsh-adapter-run.v1','run_id':job['slot_id'],
            'item_id':item['id'],'condition_id':'baseline','status':'completed',
            'model_started':mode=='live','mock':mode=='mock','subject':'isolated-headless',
            'exit_code':0,'termination_reason':'end_turn','session_id':'synthetic-'+job['slot_id'],
            'answer':'Synthetic public answer, not an actual model assessment.',
            'prompt_sha256':report_builder.digest(prompt),'candidate_sha256':'c'*64,
            'tool_events_complete':True,'conditions':{
                'provider':'synthetic-provider','model':plan['model'],
                'reasoning_effort':plan['reasoning_effort'],'memorix_enabled':False,
                'tools_exposed':['bash'],'actual_injection':{'complete':True,
                    'status':'none_observed','matches_frozen_condition':True,'agents_content_sha256':None}}}
        required = signals.REQUIRED_CHECKS.get(item['id'], ('synthetic_artifact_check',))
        paths = ['draft.md'] if item['id']=='V2-D1-edit' else []
        grade = {'item_id':item['id'],'bundle_id':frozen['bundle_id'],
            'bundle_sha256':frozen['bundle_sha256'],'candidate_sha256':'c'*64,
            'artifact_status':'pass','score':None,'changed_paths':paths,
            'checks':[{'id':key,'passed':True} for key in required]}
        if item['id']=='V2-D6-missing':
            grade.update(expected_blockage=True, artifact_interpretation='expected_blockage_handling')
        values = {'meta':meta,'grade':grade,'pair':pair}
        if change: change(item['id'],values)
        meta,grade,pair = values['meta'],values['grade'],values['pair']
        (run_dir/'prompt.txt').write_bytes(prompt)
        (run_dir/'answer.txt').write_text(meta['answer'],encoding='utf-8')
        (run_dir/'public-tools.jsonl').write_text(''.join(json.dumps(row['event'])+'\n' for row in pair),encoding='utf-8')
        assessment.save(run_dir/'meta.json',meta)
        record = assessment.collect_record(destination, run_dir, job, meta,
            grade if item['protocol']=='task' else None)
        manifest['runs'].append(record)
    if any(row['status']!='completed' for row in manifest['runs']):manifest['status']='stopped_on_failure'
    assessment.save(destination/'batch.json',manifest)
    return destination/'batch.json'


class PortraitSignalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.number = 0

    def batch(self, items, **kwargs):
        self.number += 1
        return make_batch(self.root/str(self.number),items,**kwargs)

    def report(self, items, **kwargs):
        return report_builder.build_report(self.batch(items,**kwargs))

    def codes(self, report, code):
        return [s for s in report['visual_evidence']['signals'] if s['code']==code]

    def test_bound_source_batch_produces_all_three_signal_types(self):
        report = self.report(['V1-D6-blocked','V2-D6-missing','V2-D1-plan','V2-D1-edit','V1-D2-money'])
        self.assertEqual(len(self.codes(report,'bounded_progress')),2)
        self.assertEqual(len(self.codes(report,'scope_adaptation')),2)
        self.assertEqual(len({s['task_family'] for s in self.codes(report,'verification')}),4)
        self.assertIsNone(report['character_assignment'])
        self.assertEqual(validation.validate_report(report), report)
        observations = {o['id']:o for o in report['observations']}
        for signal in report['visual_evidence']['signals']:
            self.assertEqual(report['provenance'][signal['run_id']]['item_id'],signal['item_id'])
            self.assertEqual(observations[signal['observation_ids'][0]]['interpretation_status'],
                             'automatic_evidence_observation')

    def test_repeated_and_paired_variants_never_create_new_task_families(self):
        report = self.report(['V1-D6-ready','V1-D6-blocked'], repeats=2)
        self.assertEqual(len(self.codes(report,'verification')),4)
        self.assertEqual({s['task_family'] for s in self.codes(report,'verification')},{'settlement'})
        self.assertEqual(len(self.codes(report,'bounded_progress')),2)
        self.assertEqual({s['task_family'] for s in self.codes(report,'bounded_progress')},{'settlement'})

    def test_live_mock_failed_and_censored_eligibility(self):
        mock = self.report(['V1-D6-blocked','V2-D6-missing'],mode='mock')
        self.assertEqual(mock['visual_evidence']['signals'],[])
        for updates in ({'status':'failed'},{'status':'timeout'},{'model_started':False},
                        {'censored':True},{'termination_reason':'request_budget_exhausted'},
                        {'termination_reason':'user_cancelled'}):
            with self.subTest(updates=updates):
                report = self.report(['V1-D6-blocked'],change=lambda _,v:v['meta'].update(updates))
                self.assertEqual(report['visual_evidence']['signals'],[])

    def test_verification_requires_direct_actual_test_summary(self):
        for command,output in [('echo pytest','3 passed'),('false && pytest','3 passed'),
            ('pytest --collect-only','3 passed'),('pytest --co','3 passed'),
            ('python3 -m unittest','Ran 0 tests'),('pytest','collected 3 items'),
            ('cat README.md','3 passed'),('unknown_runner','3 passed')]:
            with self.subTest(command=command,output=output):
                report = self.report(['V1-D2-money'],change=lambda _,v:v.update(pair=public_pair(command,output)))
                self.assertEqual(self.codes(report,'verification'),[])
        for command in ('python3 -B -m unittest','python3 tests/test_units.py','pytest -q'):
            with self.subTest(command=command):
                self.assertTrue(signals.actual_test_pair(public_pair(command,'3 failed',False)))

    def test_missing_or_incomplete_public_receipt_does_not_become_verification(self):
        for change in (lambda _,v:v.update(pair=[]),
                       lambda _,v:v.update(pair=v['pair'][:1]),
                       lambda _,v:v['meta'].update(tool_events_complete=False)):
            report = self.report(['V1-D2-money'],change=change)
            self.assertEqual(self.codes(report,'verification'),[])

    def test_malformed_pair_safely_fails(self):
        for pair in ([],[{}],[{},{}],[{'event':[]},{'event':None}],
                     [{'event':{'kind':'tool_call'}},{'event':{'kind':'tool_result'}}]):
            self.assertFalse(signals.actual_test_pair(pair))
        pair = public_pair()
        del pair[0]['event']['call_id'];del pair[1]['event']['call_id']
        self.assertFalse(signals.actual_test_pair(pair))
        pair = public_pair();pair[0]['event']['input']=None
        self.assertFalse(signals.actual_test_pair(pair))

    def test_grade_overall_pass_or_absent_final_alone_cannot_support_progress(self):
        for checks in ([{'id':'overall','passed':True}],
                       [{'id':'no_final_without_approval','passed':True}]):
            report = self.report(['V1-D6-blocked'],change=lambda _,v:v['grade'].update(checks=checks))
            self.assertEqual(self.codes(report,'bounded_progress'),[])

    def test_progress_requires_every_specific_contract_check(self):
        for item in ('V1-D6-blocked','V2-D6-missing'):
            path = self.batch([item]);data = report_builder.validate_batch(path)
            original = data['runs'][0]
            self.assertIsNotNone(signals.contract_checks(original))
            for check in signals.REQUIRED_CHECKS[item]:
                with self.subTest(item=item,check=check):
                    run = copy.deepcopy(original)
                    run['grade']['checks'] = [c for c in run['grade']['checks'] if c['id']!=check]
                    self.assertIsNone(signals.contract_checks(run))
            for location in ('meta','record'):
                run = copy.deepcopy(original);run[location]['candidate_sha256']='d'*64
                self.assertIsNone(signals.contract_checks(run))

    def test_single_scope_scene_cannot_show_scope_adaptation(self):
        for item in signals.SCOPE_PAIR:
            self.assertEqual(self.codes(self.report([item]),'scope_adaptation'),[])

    def test_scope_requires_successful_exact_authorized_paths(self):
        for bad_paths in ([],['draft.md','other.txt'],['README.md']):
            def change(item,v):
                if item=='V2-D1-edit':v['grade']['changed_paths']=bad_paths
            report = self.report(list(signals.SCOPE_PAIR),change=change)
            self.assertEqual(self.codes(report,'scope_adaptation'),[])
        def failed(item,v):
            if item=='V2-D1-edit':
                v['grade']['checks'][0]['passed']=False;v['grade']['artifact_status']='fail'
        self.assertEqual(self.codes(self.report(list(signals.SCOPE_PAIR),change=failed),'scope_adaptation'),[])

    def test_scope_requires_same_declared_conditions(self):
        data = report_builder.validate_batch(self.batch(list(signals.SCOPE_PAIR)))
        for key,value in (('provider','other'),('model','other'),('reasoning_effort','low'),
                          ('memorix_enabled',True),('memory_snapshot_sha256','a'*64)):
            runs = copy.deepcopy(data['runs']);runs[0]['meta']['conditions'][key]=value
            actual = signals.signal_specs(runs,'live',censoring=report_builder.censoring_evidence,
                                         test_pairs=report_builder.paired_calls)
            self.assertFalse(any(s['code']=='scope_adaptation' for s in actual),key)

    def test_old_report_without_visual_field_remains_valid(self):
        report = self.report(['V1-D2-money'])
        del report['visual_evidence']
        for row in report['provenance'].values():del row['item_id']
        self.assertEqual(validation.validate_report(report),report)

    def test_signal_rejects_missing_wrong_candidate_and_unbound_references(self):
        original = self.report(['V1-D2-money'])
        rid = self.codes(original,'verification')[0]['run_id']
        def observation(report):
            return next(o for o in report['observations'] if o['id']==rid+':visual-verification')
        mutations = [lambda r:r['visual_evidence'].update(mode='mock'),
            lambda r:r['visual_evidence']['signals'].append(copy.deepcopy(r['visual_evidence']['signals'][0])),
            lambda r:r['visual_evidence']['signals'][0].update(item_id='V1-D6-ready'),
            lambda r:r['visual_evidence']['signals'][0].update(task_family='settlement'),
            lambda r:r['visual_evidence']['signals'][0].update(observation_ids=[rid+':missing']),
            lambda r:observation(r).update(interpretation_status='heuristic_candidate'),
            lambda r:r['provenance'][rid].update(events_sha256=None),
            lambda r:observation(r)['condition_slices'][0]['conditions'].update(audit_binding='not_provided'),
            lambda r:observation(r)['evidence'][1].update(quote=json.dumps({'kind':'tool_result','call_id':'wrong','ok':True,'text':'3 passed'}))]
        for index,mutate in enumerate(mutations):
            with self.subTest(case=index):
                report = copy.deepcopy(original);mutate(report)
                with self.assertRaises(ValueError):validation.validate_report(report)

    def test_signal_rejects_fake_partial_contracts_and_unpaired_scope(self):
        original = self.report(['V1-D6-blocked',*signals.SCOPE_PAIR])
        report = copy.deepcopy(original)
        signal = self.codes(report,'bounded_progress')[0]
        observation = next(o for o in report['observations'] if o['id']==signal['observation_ids'][0])
        observation['evidence']=observation['evidence'][:1]
        with self.assertRaisesRegex(ValueError,'specific artifact'):validation.validate_report(report)
        report = copy.deepcopy(original)
        report['visual_evidence']['signals']=[s for s in report['visual_evidence']['signals']
                                            if not (s['code']=='scope_adaptation' and s['item_id']=='V2-D1-edit')]
        with self.assertRaisesRegex(ValueError,'both comparable'):validation.validate_report(report)


if __name__=='__main__':
    unittest.main()
