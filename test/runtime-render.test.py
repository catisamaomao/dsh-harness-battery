"""Saved reports use the installed UI; source evidence and model execution stay untouched."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'runtime/bin'))
import plugin_cli

spec = importlib.util.spec_from_file_location('saved_portrait_fixtures',ROOT/'test/portrait-signals.test.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


def saved_report(version='v1'):
    report = {
        'schema_version':'harness-profile.'+version, 'profile':'旧报告迁移测试',
        'generated_at':'2026-10-11T08:00:00Z',
        'scope':'exploratory_descriptive_profile' if version=='v1' else 'prospective_behavior_profile',
        'causal_attribution':'not_established', 'character_assignment':None,
        'dimensions':[{'id':f'D{i}', 'label':f'维度{i}', 'score':None, 'grade':None,
                       'status':'not_established', 'observation_ids':[], 'note':'尚无观察'} for i in range(1,8)],
        'observations':[], 'inventory':{'questionnaire_runs':0, 'task_runs':0, 'total_runs':0,
            'reviewed_runs':0, 'audit_status':{}, 'prompt_integrity':{}},
        'prompt_mismatches':[], 'provenance':{}, 'limitations':['这是空的测试报告，不是模型结果。'],
    }
    if version=='v2': report['inventory']['included_runs']=0
    return report


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.data = self.root/'data'
        self.data.mkdir(mode=0o700)
        self.batch_id = 'a'*32
        self.folder = self.data/'batches'/self.batch_id
        self.folder.mkdir(parents=True)
        self.source = self.folder/'report.json'
        self.source.write_text(json.dumps(saved_report(),ensure_ascii=False),encoding='utf-8')
        (self.folder/'report.html').write_text('old cover',encoding='utf-8')
        (self.folder/'session.jsonl').write_text('original session fixture',encoding='utf-8')

    def link(self, destination, link, directory=False):
        try: link.symlink_to(destination,target_is_directory=directory)
        except OSError as error:
            if os.name=='nt' and getattr(error,'winerror',None) in (5,1314):
                self.skipTest('Windows does not grant symbolic-link privileges')
            raise

    def recorded_batch(self, *, memory=False, mode='live'):
        stage = self.root/'recorded'
        batch_path = fixtures.make_batch(stage,['V1-D1-config','V1-D2-money'],mode=mode)
        batch = json.loads(batch_path.read_text(encoding='utf-8'))
        self.batch_id = batch['batch_id']
        self.folder = self.data/'batches'/self.batch_id
        stage.rename(self.folder)
        self.source = self.folder/'report.json'
        if memory:
            plan_path = self.folder/'plan.json'
            plan = json.loads(plan_path.read_text(encoding='utf-8'))
            plan.update(subject_expectation='user-memory-snapshot',memorix_enabled=True,
                        memory_snapshot=str(self.root/'absent-original-memory'),memory_snapshot_sha256='d'*64)
            fixtures.assessment.save(plan_path,plan)
            (self.folder/'plan.sha256').write_text(fixtures.assessment.sha(plan_path)+'\n',encoding='utf-8')
            batch.update(subject='user-memory-snapshot',plan_sha256=fixtures.assessment.sha(plan_path))
            for record in batch['runs']:
                meta_path = self.folder/record['paths']['meta']
                meta = json.loads(meta_path.read_text(encoding='utf-8'))
                meta['subject']='user-memory-snapshot'
                meta['conditions'].update(memorix_enabled=True,memory_snapshot_sha256='d'*64)
                fixtures.assessment.save(meta_path,meta)
                record.update(subject='user-memory-snapshot',conditions=meta['conditions'])
                record['sha256']['meta']=fixtures.assessment.sha(meta_path)
            fixtures.assessment.save(self.folder/'batch.json',batch)
        report = fixtures.report_builder.build_report(self.folder/'batch.json')
        del report['visual_evidence']
        for provenance in report['provenance'].values(): provenance.pop('item_id',None)
        self.source.write_text(json.dumps(report,ensure_ascii=False),encoding='utf-8')
        (self.folder/'report.md').write_text('preserved original markdown',encoding='utf-8')
        (self.folder/'report.html').write_text('preserved original HTML',encoding='utf-8')
        return report

    def embedded(self, result):
        html = Path(result['report_html']).read_text(encoding='utf-8')
        return json.loads(re.search(r'id="profile-source-data">(.*?)</script>',html,re.S).group(1))

    def test_both_legacy_versions_render_without_rewriting_evidence_or_calling_models(self):
        for version in ('v1','v2'):
            with self.subTest(version=version):
                self.source.write_text(json.dumps(saved_report(version),ensure_ascii=False),encoding='utf-8')
                originals = {p:(p.read_bytes(),p.stat().st_mtime_ns) for p in
                             (self.source,self.folder/'report.html',self.folder/'session.jsonl')}
                with patch('assessment.run_batch',side_effect=AssertionError('model execution')), \
                     patch('plugin_cli.make_report',side_effect=AssertionError('evidence regeneration')):
                    result = plugin_cli.render_report(self.folder)
                page = Path(result['report_html'])
                self.assertEqual(page,self.folder/'view/index.html')
                self.assertIn('旧报告迁移测试',page.read_text(encoding='utf-8'))
                self.assertFalse(result['model_started'])
                self.assertFalse(result['credentials_read'])
                self.assertFalse(result['memory_read'])
                for path,snapshot in originals.items():
                    self.assertEqual((path.read_bytes(),path.stat().st_mtime_ns),snapshot)
                self.assertEqual([p.name for p in page.parent.iterdir()],['index.html'])
                if version=='v2':
                    self.assertEqual(result['visual_evidence_refresh'],'unavailable_without_batch')
                    self.assertNotIn('visual_evidence',self.embedded(result))

    def test_saved_live_batch_derives_visual_evidence_without_reopening_original_memory(self):
        self.recorded_batch(memory=True)
        originals = {p:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.folder.rglob('*') if p.is_file()}
        with patch('assessment.run_batch',side_effect=AssertionError('model execution')), \
             patch('assessment.load_plan',side_effect=AssertionError('memory preflight')), \
             patch('memorix_snapshot.freeze_snapshot',side_effect=AssertionError('memory source read')), \
             patch('dsh_adapter.load_host_token',side_effect=AssertionError('credential read')):
            result=plugin_cli.render_report(self.folder)
        report=self.embedded(result)
        self.assertEqual(result['visual_evidence_refresh'],'derived_from_saved_batch')
        self.assertEqual(report['visual_evidence']['mode'],'live')
        signals=report['visual_evidence']['signals']
        self.assertEqual(len({s['task_family'] for s in signals if s['code']=='verification'}),2)
        self.assertTrue(any('原报告 SHA256' in text for text in report['limitations']))
        self.assertFalse(result['model_started'])
        self.assertFalse(result['memory_read'])
        for path,before in originals.items(): self.assertEqual((path.read_bytes(),path.stat().st_mtime_ns),before)

    def test_saved_mock_batch_remains_without_theme_signals(self):
        self.recorded_batch(mode='mock')
        result=plugin_cli.render_report(self.folder)
        self.assertEqual(result['visual_evidence_refresh'],'derived_from_saved_batch')
        self.assertEqual(self.embedded(result)['visual_evidence'],
                         {'rule_version':'whale-theme.v1','mode':'mock','signals':[]})

    def test_tampered_saved_evidence_is_rejected_and_existing_view_remains(self):
        self.recorded_batch()
        page=Path(plugin_cli.render_report(self.folder)['report_html']); before=page.read_bytes()
        batch=json.loads((self.folder/'batch.json').read_text(encoding='utf-8'))
        evidence=self.folder/batch['runs'][0]['paths']['events']
        evidence.write_bytes(evidence.read_bytes()+b'\n')
        with self.assertRaisesRegex(ValueError,'hash mismatch'):
            plugin_cli.render_report(self.folder)
        self.assertEqual(page.read_bytes(),before)

    def test_unbound_plan_and_uninstalled_bundle_are_rejected(self):
        self.recorded_batch()
        plan_path=self.folder/'plan.json'; raw=plan_path.read_bytes()
        plan=json.loads(raw); plan['bundle_sha256']='e'*64
        fixtures.assessment.save(plan_path,plan)
        with self.assertRaisesRegex(ValueError,'SHA256 mismatch'): plugin_cli.render_report(self.folder)
        batch_path=self.folder/'batch.json'; batch=json.loads(batch_path.read_text(encoding='utf-8'))
        batch.update(plan_sha256=fixtures.assessment.sha(plan_path),bundle_sha256='e'*64)
        fixtures.assessment.save(batch_path,batch)
        with self.assertRaisesRegex(ValueError,'no installed frozen question pack'):
            plugin_cli.render_report(self.folder)

    def test_unrelated_saved_report_cannot_borrow_another_batchs_evidence(self):
        original=self.recorded_batch()
        run=next(iter(original['provenance'].values())); run['meta_sha256']='e'*64
        self.source.write_text(json.dumps(original),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'provenance differs'):
            plugin_cli.render_report(self.folder)

    def test_partial_batch_or_symlink_plan_is_not_upgraded(self):
        self.source.write_text(json.dumps(saved_report('v2')),encoding='utf-8')
        (self.folder/'plan.json').write_text('{}',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'both required'): plugin_cli.render_report(self.folder)
        (self.folder/'batch.json').write_text('{}',encoding='utf-8')
        outside=self.root/'outside-plan.json'; outside.write_text('{}',encoding='utf-8')
        (self.folder/'plan.json').unlink(); self.link(outside,self.folder/'plan.json')
        with self.assertRaisesRegex(ValueError,'non-symlink'): plugin_cli.render_report(self.folder)

    def test_invalid_report_is_rejected_without_replacing_existing_view(self):
        page = Path(plugin_cli.render_report(self.folder)['report_html'])
        before = page.read_bytes()
        for raw in (b'{',b'{}',json.dumps({**saved_report(),'character_assignment':'explorer'}).encode()):
            with self.subTest(raw=raw[:30]):
                self.source.write_bytes(raw)
                with self.assertRaises(ValueError): plugin_cli.render_report(self.folder)
                self.assertEqual(page.read_bytes(),before)
                self.assertEqual(self.source.read_bytes(),raw)

    def test_report_size_is_checked_before_builder_reads_it(self):
        self.source.write_bytes(b' '*2_000_001)
        with patch('plugin_cli.build_frontend',side_effect=AssertionError('oversized report reached builder')):
            with self.assertRaisesRegex(ValueError,'2 MB'): plugin_cli.render_report(self.folder)
        self.assertFalse((self.folder/'view').exists())

    def test_json_symlink_is_rejected_without_reading_or_modifying_target(self):
        outside = self.root/'outside.json'
        outside.write_bytes(self.source.read_bytes()); self.source.unlink(); self.link(outside,self.source)
        before = outside.read_bytes()
        with patch('plugin_cli.build_frontend',side_effect=AssertionError('symlink report reached builder')):
            with self.assertRaisesRegex(ValueError,'non-symlink'): plugin_cli.render_report(self.folder)
        self.assertEqual(outside.read_bytes(),before)

    def test_view_directory_symlink_is_rejected(self):
        outside = self.root/'outside-view'; outside.mkdir()
        self.link(outside,self.folder/'view',True)
        with self.assertRaisesRegex(ValueError,'symlink'): plugin_cli.render_report(self.folder)
        self.assertEqual(list(outside.iterdir()),[])

    def test_view_html_symlink_is_rejected_without_overwriting_target(self):
        outside = self.root/'outside.html'; outside.write_text('untouched',encoding='utf-8')
        (self.folder/'view').mkdir(); self.link(outside,self.folder/'view/index.html')
        with self.assertRaisesRegex(ValueError,'symlink'): plugin_cli.render_report(self.folder)
        self.assertEqual(outside.read_text(encoding='utf-8'),'untouched')

    def test_cli_render_requires_only_saved_report_and_runs_offline(self):
        result = subprocess.run([sys.executable,'-B',str(ROOT/'runtime/bin/plugin_cli.py'),'render',
            '--data-dir',str(self.data),'--batch-id',self.batch_id],capture_output=True,encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        record = json.loads(result.stdout)
        self.assertEqual(record['command'],'render')
        self.assertEqual(Path(record['report_html']),self.folder/'view/index.html')
        self.assertFalse(record['model_started'])

    def test_replaced_batch_directory_is_rejected(self):
        outside = self.root/'outside-batch'
        self.folder.rename(outside); self.link(outside,self.folder,True)
        with self.assertRaisesRegex(ValueError,'symlink'):
            plugin_cli.batch_folder(self.data,self.batch_id)
        with self.assertRaisesRegex(ValueError,'symlink'):
            plugin_cli.render_report(self.folder)
        self.assertFalse((outside/'view').exists())


if __name__=='__main__': unittest.main()
