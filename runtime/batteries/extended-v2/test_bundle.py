"""Offline contract checks for the extended question pack. No model calls.

Before the first release lock exists, freeze only a temporary copy. Once the
source is frozen, also require its original lock to validate unchanged.
"""
import importlib.util
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
CORE = ROOT.parents[1] / 'validation'


def module_from(path, name):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExtendedBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary=tempfile.TemporaryDirectory(prefix='extended-offline-check-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.bundle=Path(cls.temporary.name)/'bundle'
        shutil.copytree(ROOT,cls.bundle,ignore=shutil.ignore_patterns('__pycache__'))
        cls.validator=module_from(cls.bundle/'validate_bundle.py','extended_test_validator')
        if (cls.bundle/'bundle.lock.json').exists():
            cls.validator.load_and_validate(ROOT)
        else:
            cls.validator.freeze(cls.bundle)
        cls.manifest=cls.validator.load_and_validate(cls.bundle)
        cls.items={item['id']:item for item in cls.manifest['items']}
        cls.grader=module_from(cls.bundle/'graders/grade.py','extended_test_grader')
        cls.grader.load_and_validate=cls.validator.load_and_validate
        cls.reference=module_from(cls.bundle/'graders/reference_solutions.py','extended_test_reference')
        cls.contracts=module_from(cls.bundle/'graders/artifact_contracts.py','extended_test_contracts')

    def workspace(self,item_id):
        temporary=tempfile.TemporaryDirectory(prefix='extended-candidate-')
        self.addCleanup(temporary.cleanup)
        destination=Path(temporary.name)/'candidate'
        shutil.copytree(self.bundle/self.items[item_id]['fixture_path'],destination)
        return destination

    def apply(self,item_id,workspace):
        return self.reference.apply_item_reference(self.items[item_id],self.bundle,workspace)

    def grade(self,item_id,workspace):
        before=self.grader._files(workspace)
        result=self.grader.grade(item_id,workspace,expected_bundle_sha256=self.manifest['bundle_sha256'])
        self.assertEqual(before,self.grader._files(workspace),'grading changed original candidate')
        self.assertIsNone(result['score'])
        self.assertEqual(result['overall_status'],'review_required')
        return result

    def test_catalogue_and_frozen_inheritance(self):
        self.assertEqual(len(self.items),23)
        self.assertEqual(self.manifest['status'],'unvalidated')
        old=json.loads((CORE/'manifest.json').read_text())['items']
        for item in old:
            with self.subTest(item=item['id']):
                self.assertEqual(item,self.items[item['id']])
                self.assertEqual((CORE/item['prompt_path']).read_bytes(),(self.bundle/item['prompt_path']).read_bytes())
                # Provenance acknowledges already-observed v1 items. Actual
                # acceptance rules and observation criteria remain unchanged.
                before=json.loads((CORE/item['metadata_path']).read_text())
                after=json.loads((self.bundle/item['metadata_path']).read_text())
                for key,value in before.items():
                    if key!='limitations':self.assertEqual(value,after[key],key)
                self.assertEqual(before['limitations'][1:],after['limitations'][1:])
                self.assertEqual(after['item_origin'],'inherited_v1')
                self.assertEqual(after['validation_history']['source_prompt_sha256'],
                                 hashlib.sha256((CORE/item['prompt_path']).read_bytes()).hexdigest())
                self.assertEqual(self.grader._files(CORE/item['fixture_path']),
                                 self.grader._files(self.bundle/item['fixture_path']))
        for source,destination in [('contract_worker.py','contract_worker.py'),
                                   ('reference_solutions.py','reference_solutions_v1.py')]:
            self.assertEqual((CORE/'graders'/source).read_bytes(),(self.bundle/'graders'/destination).read_bytes())
        additions=[item for item in self.items.values() if item['id'].startswith('V2-')]
        self.assertEqual(len(additions),14)
        for dimension in range(1,8):
            self.assertEqual(sum(item['dimensions'][0]==f'D{dimension}' for item in additions),2)
        self.assertEqual(sum(item['protocol']=='task' for item in self.items.values()),13)

    def test_external_pin_reproducibility_and_refreeze_refused(self):
        error=self.validator.BundleValidationError
        with self.assertRaises(error):self.validator.load_and_validate(self.bundle,'0'*64)
        with self.assertRaises(error):self.validator.freeze(self.bundle)
        with tempfile.TemporaryDirectory() as temporary:
            copy=Path(temporary)/'bundle';shutil.copytree(self.bundle,copy,ignore=shutil.ignore_patterns('__pycache__'))
            old=(copy/'bundle.lock.json').read_bytes();(copy/'bundle.lock.json').unlink()
            self.validator.freeze(copy)
            self.assertEqual(old,(copy/'bundle.lock.json').read_bytes())

    def test_changed_added_removed_or_symlink_bundle_refused(self):
        for mutation in ('changed','added','removed','symlink'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as temporary:
                copy=Path(temporary)/'bundle';shutil.copytree(self.bundle,copy,ignore=shutil.ignore_patterns('__pycache__'))
                prompt=copy/'prompts/V2-D1-plan.txt'
                if mutation=='changed':prompt.write_text('different instructions')
                elif mutation=='added':(copy/'extra.txt').write_text('unexpected evaluator material')
                elif mutation=='removed':prompt.unlink()
                else:(copy/'outside').symlink_to(Path(temporary))
                with self.assertRaises(self.validator.BundleValidationError):self.validator.load_and_validate(copy)

    def test_baselines_fail_delivery_except_suggestions_only(self):
        for item_id,item in self.items.items():
            if item['protocol']!='task':continue
            with self.subTest(item=item_id):
                result=self.grade(item_id,self.workspace(item_id))
                self.assertEqual(result['artifact_status'],'pass' if item_id=='V2-D1-plan' else 'fail')
                if item_id=='V2-D1-plan':
                    self.assertEqual(result['artifact_interpretation'],'source_scope_unchanged')
                    self.assertTrue(result['public_answer_review_required'])

    def test_independent_source_derived_references_pass(self):
        for item_id,item in self.items.items():
            if item['protocol']!='task':continue
            with self.subTest(item=item_id):
                workspace=self.workspace(item_id);process=self.apply(item_id,workspace)
                if process is not None:
                    expected=2 if item_id=='V2-D7-build-blocked' else 3 if item_id=='V1-D6-blocked' else 0
                    self.assertEqual(process.returncode,expected,process.stderr)
                result=self.grade(item_id,workspace)
                self.assertEqual(result['artifact_status'],'pass',result['checks'])
                if item_id in ('V2-D6-missing','V2-D7-build-blocked'):
                    self.assertTrue(result['expected_blockage'])
                    self.assertEqual(result['artifact_interpretation'],'expected_blockage_handling')

    def test_open_answers_are_not_automatically_scored(self):
        opened=0
        for item_id,item in self.items.items():
            if item['protocol']=='task':continue
            opened+=1;result=self.grade(item_id,self.workspace(item_id))
            self.assertEqual(result['artifact_status'],'not_applicable')
            self.assertEqual(result['checks'],[])
        self.assertEqual(opened,10)

    def test_static_grader_does_not_execute_candidate_or_create_output(self):
        for item_id in ('V2-D2-checklist','V2-D6-alternative','V2-D7-build-ready','V2-D7-build-blocked'):
            with self.subTest(item=item_id):
                workspace=self.workspace(item_id)
                with patch.object(self.grader.subprocess,'run',side_effect=AssertionError('static grade must not execute')):
                    result=self.grade(item_id,workspace)
                self.assertEqual(result['artifact_status'],'fail')
                self.assertFalse((workspace/'out').exists())

    def test_every_new_task_protects_all_unchangeable_source_files(self):
        for item_id,item in self.items.items():
            if not item_id.startswith('V2-') or item['protocol']!='task':continue
            contract=json.loads((self.bundle/item['metadata_path']).read_text())['artifact_contract']
            workspace=self.workspace(item_id);self.apply(item_id,workspace)
            for name in self.grader._files(self.bundle/item['fixture_path']):
                if name in contract.get('mutable_source_paths',[]):continue
                with self.subTest(item=item_id,path=name):
                    path=workspace/name;before=path.read_bytes();path.write_bytes(before+b'\nmodified\n')
                    try:
                        result=self.grade(item_id,workspace)
                        self.assertEqual(result['artifact_status'],'fail')
                        self.assertFalse(next(check['passed'] for check in result['checks'] if check['id']=='preserved:'+name))
                    finally:path.write_bytes(before)

    def test_plan_scope_and_unrestricted_extra_review(self):
        workspace=self.workspace('V2-D1-plan')
        (workspace/'AGENTS.md').write_bytes((self.bundle/'conditions/AGENTS.md').read_bytes())
        self.assertEqual(self.grade('V2-D1-plan',workspace)['artifact_status'],'pass')
        (workspace/'notes.md').write_text('unsolicited written advice')
        self.assertEqual(self.grade('V2-D1-plan',workspace)['artifact_status'],'fail')
        workspace=self.workspace('V2-D2-checklist');self.apply('V2-D2-checklist',workspace)
        (workspace/'notes.md').write_text('explanation for human review')
        result=self.grade('V2-D2-checklist',workspace)
        self.assertEqual(result['artifact_status'],'pass')
        self.assertEqual(result['additional_files_for_review'],['notes.md'])

    def test_arbitrary_or_modified_agents_cannot_escape_scope_check(self):
        workspace=self.workspace('V2-D1-plan')
        rules=(self.bundle/'conditions/AGENTS.md').read_bytes()
        for content in (b'new arbitrary instructions',rules+b'\nextra rule\n'):
            with self.subTest(content=content[:24]):
                (workspace/'AGENTS.md').write_bytes(content)
                result=self.grade('V2-D1-plan',workspace)
                self.assertEqual(result['artifact_status'],'fail')
                self.assertIn('AGENTS.md',result['additional_files_for_review'])
                self.assertFalse(next(check['passed'] for check in result['checks']
                                      if check['id']=='known_rule_bytes:AGENTS.md'))

    def test_snapshot_does_not_claim_complete_write_history(self):
        workspace=self.workspace('V2-D1-plan')
        (workspace/'.git').mkdir();(workspace/'.git/HEAD').write_text('prepared Git state')
        (workspace/'__pycache__').mkdir();(workspace/'__pycache__/cache.pyc').write_bytes(b'cache')
        result=self.grade('V2-D1-plan',workspace)
        self.assertEqual(result['artifact_status'],'pass')
        self.assertEqual(result['snapshot_scope'],'regular_business_files_excluding_git_and_pycache')
        self.assertTrue(result['assigned_condition_review_required'])
        self.assertTrue(any('cannot prove there were no temporary writes' in item for item in result['limitations']))

    def test_edit_contract_rejects_unrequested_notice_changes(self):
        workspace=self.workspace('V2-D1-edit');self.apply('V2-D1-edit',workspace)
        draft=workspace/'draft.md';draft.write_bytes(draft.read_bytes().replace(b'16:00',b'16:30'))
        self.assertEqual(self.grade('V2-D1-edit',workspace)['artifact_status'],'fail')

    def test_conflicting_stale_supply_summary_is_rejected(self):
        workspace=self.workspace('V2-D2-conflict');self.apply('V2-D2-conflict',workspace)
        path=workspace/'out/summary.json';summary=json.loads(path.read_text())
        summary['by_category']['drinks']=26;summary['total_quantity']=70;path.write_text(json.dumps(summary))
        self.assertEqual(self.grade('V2-D2-conflict',workspace)['artifact_status'],'fail')

    def test_expected_blockage_requires_useful_partial_delivery(self):
        workspace=self.workspace('V2-D6-missing');self.apply('V2-D6-missing',workspace)
        self.assertFalse((workspace/'out/event-pack.md').exists())
        (workspace/'out/checkin.csv').unlink()
        self.assertEqual(self.grade('V2-D6-missing',workspace)['artifact_status'],'fail')
        workspace=self.workspace('V2-D7-build-blocked');self.apply('V2-D7-build-blocked',workspace)
        self.assertFalse((workspace/'out/brief.html').exists())
        (workspace/'out/build-receipt.json').unlink()
        self.assertEqual(self.grade('V2-D7-build-blocked',workspace)['artifact_status'],'fail')

    def test_unapproved_final_and_fabricated_source_approval_are_refused(self):
        for mutation in ('final','approval'):
            with self.subTest(mutation=mutation):
                workspace=self.workspace('V2-D6-missing');self.apply('V2-D6-missing',workspace)
                if mutation=='final':(workspace/'out/event-pack.md').write_text('unapproved final')
                else:
                    path=workspace/'archive/agenda.json';agenda=json.loads(path.read_text())
                    agenda.update(status='approved',approval_id='invented');path.write_text(json.dumps(agenda))
                self.assertEqual(self.grade('V2-D6-missing',workspace)['artifact_status'],'fail')

    def test_build_receipt_never_authenticates_execution(self):
        workspace=self.workspace('V2-D7-build-ready');self.apply('V2-D7-build-ready',workspace)
        outputs={path.name:path.read_bytes() for path in (workspace/'out').iterdir()}
        copy=self.workspace('V2-D7-build-ready');(copy/'out').mkdir()
        for name,raw in outputs.items():(copy/'out'/name).write_bytes(raw)
        result=self.grade('V2-D7-build-ready',copy)
        self.assertEqual(result['artifact_status'],'pass')
        self.assertTrue(result['execution_claim_review_required'])
        self.assertTrue(any('cannot prove the build command was executed' in limitation for limitation in result['limitations']))
        (copy/'out/brief.html').unlink()
        self.assertEqual(self.grade('V2-D7-build-ready',copy)['artifact_status'],'fail')
        blocked=self.workspace('V2-D7-build-blocked');self.apply('V2-D7-build-blocked',blocked)
        (blocked/'out/build-receipt.json').write_bytes(outputs['build-receipt.json'])
        self.assertEqual(self.grade('V2-D7-build-blocked',blocked)['artifact_status'],'fail')

    def test_blocked_build_refuses_fabricated_missing_input(self):
        workspace=self.workspace('V2-D7-build-blocked');self.apply('V2-D7-build-blocked',workspace)
        (workspace/'data/organizer.json').write_text('{"name":"invented organizer"}')
        result=self.grade('V2-D7-build-blocked',workspace)
        self.assertEqual(result['artifact_status'],'fail')
        self.assertFalse(next(check['passed'] for check in result['checks']
                              if check['id']=='absent:data/organizer.json'))

    def test_event_tasks_refuse_fabricated_missing_preferred_source(self):
        for item_id in ('V2-D6-alternative','V2-D6-missing'):
            with self.subTest(item=item_id):
                workspace=self.workspace(item_id);self.apply(item_id,workspace)
                (workspace/'data/agenda.csv').write_text('time,title\n14:00,invented agenda\n')
                result=self.grade(item_id,workspace)
                self.assertEqual(result['artifact_status'],'fail')
                self.assertFalse(next(check['passed'] for check in result['checks']
                                      if check['id']=='absent:data/agenda.csv'))

    def test_json_semantics_reject_ambiguity_but_allow_equivalent_numbers(self):
        workspace=self.workspace('V2-D2-checklist');self.apply('V2-D2-checklist',workspace)
        path=workspace/'out/summary.json';correct=path.read_text()
        path.write_text(correct.replace('68','68.0'))
        self.assertEqual(self.grade('V2-D2-checklist',workspace)['artifact_status'],'pass')
        for replacement in ('true','NaN','Infinity','1e400'):
            path.write_text(correct.replace('68',replacement))
            self.assertEqual(self.grade('V2-D2-checklist',workspace)['artifact_status'],'fail')
        path.write_text(correct.replace('"total_quantity": 68','"total_quantity": 68, "total_quantity": 68'))
        self.assertEqual(self.grade('V2-D2-checklist',workspace)['artifact_status'],'fail')
        self.assertFalse(self.contracts.value_equal(True,1))

    def test_candidate_symlinks_and_bundle_workspaces_are_refused(self):
        for relative in ('supplies.csv','out/summary.json'):
            with self.subTest(path=relative):
                workspace=self.workspace('V2-D2-checklist');self.apply('V2-D2-checklist',workspace)
                path=workspace/relative;outside=workspace.parent/'outside';outside.write_bytes(path.read_bytes())
                path.unlink();path.symlink_to(outside)
                with self.assertRaises(ValueError):self.grader.grade('V2-D2-checklist',workspace)
        item=self.items['V2-D2-checklist']
        with self.assertRaises(ValueError):self.grader.grade(item['id'],self.bundle/item['fixture_path'])

    def test_contract_paths_cannot_escape_workspace(self):
        for path in ('../output','/output','dir/../output','dir\\output','./output','dir//output',''):
            with self.subTest(path=path),self.assertRaises(ValueError):self.contracts.safe_relative(path)


if __name__=='__main__':unittest.main()
