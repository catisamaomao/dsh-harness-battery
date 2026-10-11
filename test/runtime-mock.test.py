"""Opt-in installed DSH smoke test: fake provider only, no credentials or memory."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'runtime/bin'))


@unittest.skipUnless(os.environ.get('DSH_RUNTIME_MOCK_SMOKE') == '1' and sys.platform == 'darwin',
                     'opt in on macOS with DSH_RUNTIME_MOCK_SMOKE=1; never uses a real provider')
class InstalledMockTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='dsh-plugin-mock-')
        self.addCleanup(self.temp.cleanup)
        self.data=Path(self.temp.name).resolve()/'data'
        self.cli=ROOT/'runtime/bin/plugin_cli.py'

    def cli_call(self,*args):
        process=subprocess.run([sys.executable,'-B',str(self.cli),*args],capture_output=True,text=True,timeout=180, encoding='utf-8')
        self.assertEqual(process.returncode,0,process.stdout+process.stderr)
        return json.loads(process.stdout.splitlines()[-1])

    def test_real_app_fake_provider_task_grade_and_report(self):
        plan=self.cli_call('plan','--data-dir',str(self.data),'--battery','extended','--mode','mock',
                           '--items','V2-D1-plan','--max-requests','2','--timeout','90')
        run=self.cli_call('run','--data-dir',str(self.data),'--batch-id',plan['batch_id'],'--mode','mock')
        self.assertEqual(run['status'],'completed')
        self.assertFalse(run['model_started'])
        batch=json.loads(Path(run['batch_path']).read_text(encoding='utf-8'))
        folder=Path(run['batch_path']).parent
        grade=json.loads((folder/batch['runs'][0]['paths']['grade']).read_text(encoding='utf-8'))
        self.assertEqual(grade['artifact_status'],'pass')
        self.assertEqual(grade['execution_boundary']['network'],'denied')
        self.assertTrue(all(row['condition_id']=='baseline' for row in batch['runs']))
        report=self.cli_call('report','--data-dir',str(self.data),'--batch-id',plan['batch_id'])
        self.assertEqual(report['included_runs'],0)
        self.assertTrue(Path(report['report_html']).is_file())

    def test_normal_dsh_exit_cleans_background_tool_group(self):
        from dsh_adapter import run_item
        output=Path(self.temp.name).resolve()/'background-run'
        result=run_item(ROOT/'runtime/batteries/extended-v2','V2-D3-moving','baseline',output,
            mode='mock',max_requests=2,timeout=90,
            mock_command='sleep 60 >/dev/null 2>&1 & echo $! > mock-background.pid')
        self.assertEqual(result['status'],'completed',result.get('error'))
        self.assertFalse(result['model_started'])
        pid=(Path(result['workspace_path'])/'mock-background.pid').read_text(encoding='utf-8').strip()
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            probe=subprocess.run(['/bin/ps','-o','stat=','-p',pid],capture_output=True,text=True, encoding='utf-8')
            if not probe.stdout.strip() or probe.stdout.strip().startswith('Z'):break
            time.sleep(.05)
        self.assertTrue(not probe.stdout.strip() or probe.stdout.strip().startswith('Z'),probe.stdout)

    def test_actual_dsh_tool_tree_is_cancelled_and_evidence_is_retained(self):
        # Run this in a subprocess so test-runner signal handlers remain unchanged.
        output=Path(self.temp.name).resolve()/'cancelled-run'
        pid_path=Path(self.temp.name).resolve()/'tool.pid'
        child=r'''
import json,os,signal,sys,threading,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from dsh_adapter import run_item
out=Path(sys.argv[3]); published=Path(sys.argv[4]); stop=threading.Event()
def interrupt(*args):raise KeyboardInterrupt
signal.signal(signal.SIGTERM,interrupt)
def monitor():
 while not stop.wait(.05):
  try:
   preparation=json.loads((out/'preparation.json').read_text())
   marker=Path(preparation['workspace_path'])/'mock-process.pid'
   if marker.exists():
    published.write_text(marker.read_text());os.kill(os.getpid(),signal.SIGTERM);return
  except (OSError,KeyError,ValueError):pass
thread=threading.Thread(target=monitor,daemon=True);thread.start()
try:
 run_item(Path(sys.argv[2]),'V2-D3-moving','baseline',out,mode='mock',max_requests=2,timeout=90,
          mock_command="python3 -c 'import os,time;open(\"mock-process.pid\",\"w\").write(str(os.getpid()));time.sleep(60)'")
except KeyboardInterrupt:pass
finally:stop.set();thread.join(timeout=1)
'''
        process=subprocess.run([sys.executable,'-B','-c',child,str(ROOT/'runtime/bin'),
                 str(ROOT/'runtime/batteries/extended-v2'),str(output),str(pid_path)],
                 capture_output=True,text=True,timeout=30, encoding='utf-8')
        self.assertEqual(process.returncode,0,process.stderr)
        self.assertTrue(pid_path.is_file(),'mock tool did not reach its cancellation barrier')
        meta=json.loads((output/'meta.json').read_text(encoding='utf-8'))
        self.assertEqual(meta['termination_reason'],'user_cancelled')
        self.assertTrue(meta['cancelled'])
        self.assertFalse(meta['model_started'])
        self.assertEqual(meta['provider_requests'],1)
        self.assertTrue((output/'preparation.json').is_file())
        self.assertTrue((output/'request-audit.json').is_file())
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            probe=subprocess.run(['/bin/ps','-o','stat=','-p',pid_path.read_text(encoding='utf-8').strip()],capture_output=True,text=True, encoding='utf-8')
            if not probe.stdout.strip() or probe.stdout.strip().startswith('Z'):break
            time.sleep(.05)
        self.assertTrue(not probe.stdout.strip() or probe.stdout.strip().startswith('Z'),probe.stdout)


if __name__=='__main__':unittest.main()
