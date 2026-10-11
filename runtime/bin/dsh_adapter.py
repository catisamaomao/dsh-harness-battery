#!/usr/bin/env python3
"""Run installed DSH with an OS-confined tool tree and a budgeted host provider bridge.

The child has no network permission and never receives the real provider token.
`mock` exercises installed DSH without an external model call; live is explicit.
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import audit_injection
from isolation_support import clean_env, digest_bytes, inventory, sandbox_policy
from validation_run import prepare
from process_control import run_child, kill_tree, bind_process, WorkerRegistry

APP = Path(os.environ.get('DSH_HARNESS_APP', '/Applications/DeepSeek Harness.app')).expanduser().resolve()
ELECTRON = APP / 'Contents/MacOS/DeepSeek Harness'
CLI = APP / 'Contents/Resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js'
MODULES = APP / 'Contents/Resources/app.asar/dsh/node_modules'
ENDPOINT = 'https://api.deepseek.com/anthropic/v1/messages'
SOURCE = Path(__file__).resolve().parent
_LOADED_SOURCE_SHA256=digest_bytes(Path(__file__).read_bytes())


def validate_app_path(app):
    app=Path(app)
    if not app.is_absolute() or not app.is_dir() or app.suffix != '.app':
        raise ValueError('DSH_HARNESS_APP must identify an installed macOS .app bundle')
    actual=app.resolve()
    for protected in (SOURCE.parent.resolve(), (Path.home()/'.dsh').resolve()):
        if actual.is_relative_to(protected) or protected.is_relative_to(actual):
            raise ValueError('DSH app path overlaps protected plugin source or user DSH home')
    if not (actual/'Contents/MacOS/DeepSeek Harness').is_file():
        raise ValueError('DeepSeek Harness executable is missing from app bundle')
    return actual


def patch_rows(model, effort, max_tokens):
    disabled = ['session-title-llm', 'session-log-deepseek', 'session-telemetry-otel', 'otel',
                'deepseek-llm-api-extensions', 'plugin-package-inventory-deepseek', 'deepseek-account',
                'llm-deepseek-account', 'llm-pi-ai', 'llm-retry', 'web-search-deepseek', 'web-fetch-http',
                'tool-web', 'tool-plugin-manager', 'plugin-manager', 'config-editor', 'tool-subagent',
                'tool-subagent-fork', 'tool-workflow', 'goal-round-driver']
    return ([{'id': i, 'disabled': True} for i in disabled] + [
        {'id': 'agent-default-model', 'name': '@deepseek-ai/dsh-agent-default-model',
         'config': {'provider': 'deepseek-official', 'model': model, 'reasoningEffort': effort}},
        {'id': 'llm-deepseek', 'name': '@deepseek-ai/dsh-llm-deepseek-api-key',
         'config': {'baseURL': 'http://harness-provider.invalid', 'apiKeyEnv': 'DSH_BRIDGE_DUMMY_KEY',
                    'maxTokens': max_tokens, 'reasoningEffort': effort}},
        {'id': 'sandbox-policy', 'name': '@deepseek-ai/dsh-sandbox-policy',
         'config': {'mode': 'danger-full-access'}},
        {'id': 'approval', 'name': '@deepseek-ai/dsh-user-approval', 'config': {'policy': 'never'}}])


def adapter_policy(active, protected):
    # Internal DSH file sandbox is bypassed to avoid unsupported nested seatbelt;
    # the entire CLI and every descendant remain inside this stricter outer policy.
    return sandbox_policy(active, protected) + '\n'.join([
        f'(allow file-read* file-map-executable (subpath {json.dumps(str(APP))}))',
        '(allow process-info* (target self))',
        '(allow signal (target self))',
        '(allow mach-lookup (global-name "com.apple.system.logger"))',
    ]) + '\n'


def verify_python_runtime(policyfile,env,workspace):
    code='''import csv,decimal,io,json,sys,unittest
class RuntimeProbe(unittest.TestCase):
 def runTest(self):
  self.assertEqual(decimal.Decimal("1.1")+decimal.Decimal("2.2"),decimal.Decimal("3.3"))
  self.assertEqual(list(csv.reader(io.StringIO("a,b\\n"))),[["a","b"]])
result=unittest.TextTestRunner().run(unittest.TestSuite([RuntimeProbe()]))
if not result.wasSuccessful():sys.exit(1)
print(json.dumps({"python_executable":sys.executable,"python_version":sys.version.split()[0],"modules":["csv","decimal","json","unittest"],"unittest_probe_passed":True}))
'''
    checked=run_child(['/usr/bin/sandbox-exec','-f',str(policyfile),'python3','-B','-c',code],
                           cwd=workspace,env=env,capture_output=True,text=True,timeout=15, encoding='utf-8')
    if checked.returncode:raise ValueError('sandboxed Python/unittest runtime preflight failed')
    return json.loads(checked.stdout)


def native_termination(events):
    endings=[e.get('reason',{}).get('kind') for e in events if e.get('type')=='status' and e.get('phase')=='turn_end']
    return 'output_token_budget_exhausted' if endings and endings[-1]=='max-tokens' else None


def load_host_token(path):
    """Read only on host; stdout is captured privately, never copied to run artifacts."""
    script = r'''const fs=require('node:fs');const yaml=require(process.argv[1]);
try { const p=process.argv[2],s=fs.statSync(p);if((s.mode&63)!==0)process.exit(4);
const d=yaml.parse(fs.readFileSync(p,'utf8'));const g=d.records?.['deepseek-account-platform/default'];
if(g?.kind!=='grant'||typeof g.payload?.token!=='string'||!g.payload.token||g.payload.token.startsWith('dsh_mock_'))process.exit(5);
const u=new URL(g.payload.issuer);if(u.protocol!=='https:'||['localhost','127.0.0.1','[::1]'].includes(u.hostname))process.exit(6);
process.stdout.write(JSON.stringify({token:g.payload.token})); }catch{process.exit(7)}'''
    proc = subprocess.run([str(ELECTRON), '--expose-internals', '-e', script,
                           str(MODULES / 'yaml'), str(path)],
                          env={'PATH': '/usr/bin:/bin', 'ELECTRON_RUN_AS_NODE': '1'},
                          capture_output=True, text=True, timeout=15, encoding='utf-8')
    if proc.returncode:
        raise ValueError('host provider credential unavailable; no credential details were logged')
    try:
        return json.loads(proc.stdout)['token']
    except (ValueError, KeyError):
        raise ValueError('host provider credential could not be decoded') from None


def mock_sse(text, *, tool=None):
    events = [('message_start', {'type':'message_start','message':{'id':'mock-local','type':'message',
               'role':'assistant','model':'deepseek-flash','content':[], 'stop_reason':None,
               'usage':{'input_tokens':1,'output_tokens':0}}})]
    if tool:
        name, arguments = tool
        events += [('content_block_start', {'type':'content_block_start','index':0,
                    'content_block':{'type':'tool_use','id':'mock_tool_1','name':name,'input':{}}}),
                   ('content_block_delta', {'type':'content_block_delta','index':0,
                    'delta':{'type':'input_json_delta','partial_json':json.dumps(arguments)}})]
    else:
        events += [('content_block_start', {'type':'content_block_start','index':0,
                    'content_block':{'type':'text','text':''}}),
                   ('content_block_delta', {'type':'content_block_delta','index':0,
                    'delta':{'type':'text_delta','text':text}})]
    events += [('content_block_stop', {'type':'content_block_stop','index':0}),
               ('message_delta', {'type':'message_delta','delta':{'stop_reason':'tool_use' if tool else 'end_turn',
                   'stop_sequence':None},'usage':{'output_tokens':1}}), ('message_stop', {'type':'message_stop'})]
    return ''.join('event: '+name+'\ndata: '+json.dumps(data)+'\n\n' for name,data in events).encode()


class ProviderBridge:
    def __init__(self, mode, token, model, max_requests, max_tokens, timeout, mock_command=None,mock_tool=None):
        if mode not in ('mock','live') or not 1 <= max_requests <= 100 or not 1 <= max_tokens <= 32768:
            raise ValueError('invalid provider mode or bounded request/token budget')
        self.mode, self.token, self.model = mode, token, model
        self.max_requests, self.max_tokens, self.timeout = max_requests, max_tokens, timeout
        self.mock_command = mock_command
        self.mock_tool = mock_tool
        self.audit, self.calls, self.conn = [], 0, None
        self.stopped = threading.Event()
        self.workers = None
        self.cleanup_result = None

    def dispatch(self, message):
        if not isinstance(message, dict):
            raise ValueError('provider frame must be an object')
        if message.get('url') != 'http://harness-provider.invalid/v1/messages' or message.get('method') != 'POST':
            raise ValueError('provider bridge only accepts the fixed Messages operation')
        raw = message.get('body')
        if not isinstance(raw, str) or len(raw.encode()) > 2*1024*1024:
            raise ValueError('provider input byte budget exceeded')
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError('provider Messages body must be an object')
        if body.get('model') != self.model or not isinstance(body.get('messages'), list):
            raise ValueError('provider request differs from pinned model or Messages schema')
        if not isinstance(body.get('max_tokens'), int) or not 1 <= body['max_tokens'] <= self.max_tokens:
            raise ValueError('provider output token budget exceeded')
        if self.calls >= self.max_requests or self.stopped.is_set():
            raise ValueError('provider request budget exhausted or run stopped')
        self.calls += 1
        record = {'request_number':self.calls,'request_sha256':digest_bytes(raw.encode()),
                  'model':body['model'],'max_tokens':body['max_tokens'],'mode':self.mode,
                  'tools':[t.get('name') for t in body.get('tools',[]) if isinstance(t,dict)]}
        self.audit.append(record)
        if self.mode == 'mock':
            tool = None
            if self.calls == 1 and self.mock_tool:
                name,arguments=self.mock_tool
                if name not in record['tools']:raise ValueError('requested mock tool was not exposed')
                tool=(name,arguments)
            elif self.calls == 1 and self.mock_command:
                name = next((n for n in record['tools'] if n == 'bash' or n.endswith('__bash')), None)
                if not name:
                    raise ValueError('mock startup expected an exposed bash tool')
                tool = (name, {'command':self.mock_command, 'description':'Run local mock transport boundary probe'})
            data = mock_sse('MOCK TRANSPORT CHECK ONLY. No model was called.', tool=tool)
            record.update(status=200,response_sha256=digest_bytes(data),mock=True)
            return 200, {'content-type':'text/event-stream'}, data
        headers = {'content-type':'application/json','accept':'text/event-stream',
                   'anthropic-version':'2023-06-01','x-dsh-auth-token':self.token}
        for name in ('anthropic-beta','x-deepseek-harness-user-id','x-deepseek-harness-session-id'):
            value = message.get('headers',{}).get(name)
            if isinstance(value,str) and len(value) < 300:
                headers[name] = value
        conn = http.client.HTTPSConnection('api.deepseek.com', timeout=min(self.timeout,90))
        self.conn = conn
        try:
            conn.request('POST','/anthropic/v1/messages',body=raw.encode(),headers=headers)
            response = conn.getresponse()
            data = response.read(16*1024*1024+1)
            if len(data)>16*1024*1024:
                raise ValueError('provider response byte budget exceeded')
            # Never forward an accidentally echoed host token, including HTTP error bodies.
            data = data.replace(self.token.encode(),b'[host-credential-redacted]')
            record.update(status=response.status,response_sha256=digest_bytes(data),response_bytes=len(data))
            allowed = {k.lower():v for k,v in response.getheaders()
                       if k.lower() in ('content-type','request-id','x-request-id','retry-after')}
            return response.status, allowed, data
        finally:
            conn.close()
            self.conn = None

    def serve(self, reader_fd, writer_fd):
        try:
            with os.fdopen(reader_fd,'rb') as source, os.fdopen(writer_fd,'wb',buffering=0) as target:
                while True:
                    line = source.readline(3*1024*1024)
                    if not line: break
                    message = {}
                    try:
                        if not line.endswith(b'\n'):raise ValueError('provider frame exceeds byte budget')
                        message = json.loads(line)
                        if isinstance(message,dict) and message.get('type')=='worker_started':
                            self.register_worker(message)
                            continue
                        status, headers, data = self.dispatch(message)
                        reply = {'id':message['id'],'status':status,'headers':headers,
                                 'body':base64.b64encode(data).decode()}
                    except Exception as error:
                        reason = str(error)
                        if self.token:reason = reason.replace(self.token,'[redacted]')
                        self.audit.append({'rejected':True,'reason':reason[:300]})
                        reply = {'id':message.get('id') if isinstance(message,dict) else None, 'error':'Provider bridge rejected request: '+reason[:300]}
                    target.write(json.dumps(reply).encode()+b'\n')
        except (BrokenPipeError,OSError):
            pass

    def bind_worker_root(self, process):
        self.workers=WorkerRegistry(bind_process(process))

    def register_worker(self, message):
        pid,detached,gated=message.get('pid'),message.get('detached'),message.get('gated',False)
        if type(pid) is not int or pid<=1 or pid==os.getpid() or type(detached) is not bool or type(gated) is not bool:
            raise ValueError('invalid child-worker registration')
        if self.workers is None:
            raise ValueError('worker root identity has not been established')
        self.workers.register(pid,detached,gated=gated,resume=not self.stopped.is_set())
        if self.stopped.is_set():
            self.kill_workers()

    def monitor_workers(self):
        while not self.stopped.wait(0.01):
            try:
                self.workers.refresh()
            except (ValueError,RuntimeError,OSError):
                self.audit.append({'worker_tracking':'incomplete','reason':'descendant identity refresh unavailable'})
                return

    def kill_workers(self):
        if self.workers is not None:
            self.cleanup_result=self.workers.cleanup()
        return self.cleanup_result

    def stop(self):
        self.stopped.set()
        if self.conn:
            self.conn.close()


def public_session(raw):
    """Strip reasoning while preserving public answers, calls and tool receipts."""
    kept, tools, calls, receipts = [], [], set(), set()
    for line in raw.splitlines():
        event = json.loads(line)
        kind = event.get('type')
        if 'thinking' in (kind or ''):continue
        if kind == 'assistant/message':
            msg = event.get('data',{}).get('message',{})
            if isinstance(msg.get('content'),list):
                msg['content'] = [c for c in msg['content'] if c.get('type') not in ('thinking','redacted_thinking')]
        if kind in ('request/context','request/header'):continue
        kept.append(event)
        data = event.get('data',{})
        if kind == 'tool/call':
            call = data.get('call',data)
            call_id=call.get('id') or data.get('callId')
            arguments=call.get('input',call.get('arguments',data.get('input')))
            if isinstance(arguments,str):
                try:arguments=json.loads(arguments)
                except ValueError:pass
            if not call_id or call_id in calls:raise ValueError('missing or duplicate public tool call id')
            calls.add(call_id)
            tools.append({'kind':'tool_call','call_id':call_id,
                          'tool_name':call.get('name') or data.get('name'),
                          'input':arguments, 'source_type':kind})
        elif kind == 'tool/result':
            msg = data.get('message',{})
            content = msg.get('content',[])
            text = ''.join(c.get('text','') for c in content if c.get('type')=='text')
            call_id=msg.get('toolCallId') or data.get('callId')
            if not call_id or call_id in receipts:raise ValueError('missing or duplicate public tool receipt id')
            receipts.add(call_id)
            tools.append({'kind':'tool_result','call_id':call_id,
                          'ok':not msg['isError'] if isinstance(msg.get('isError'),bool) else None,
                          'exit_code':None,'text':text,'source_type':kind})
    if calls!=receipts:raise ValueError('authoritative log has incomplete tool call/result pairs')
    return kept, tools


def candidate_digest(workspace):
    """Use the frozen independent grader's file-only convention."""
    inventory(workspace)  # Reject symlinks and special files before reading.
    files={p.relative_to(workspace).as_posix():digest_bytes(p.read_bytes())
           for p in Path(workspace).rglob('*') if p.is_file()
           and not {'.git','__pycache__'}.intersection(p.relative_to(workspace).parts)}
    return digest_bytes(json.dumps(files,sort_keys=True,separators=(',',':')).encode())


def actual_instruction_evidence(raw,expected_hash,audit):
    observed=[]
    for line in raw.splitlines():
        event=json.loads(line)
        if not audit_injection.instruction_blocks(event):continue
        data=event['data'];source=data.get('source',{})
        if source.get('kind')!='agent-instructions':raise ValueError('unexpected workspace instruction source')
        text=''.join(c.get('text','') for c in data.get('content',[]) if c.get('type')=='text')
        headers=audit_injection.FROM_RE.findall(text)
        # Exact current DSH framing, including the file's terminal newline.
        matches=re.findall(r'Instructions from: AGENTS\.md\n\n(.*?)\n</system-reminder>',text,re.S)
        if headers!=['AGENTS.md'] or len(matches)!=1:raise ValueError('unsupported or extra workspace instructions')
        observed.append(digest_bytes(matches[0].encode()))
    expected=[] if expected_hash is None else [expected_hash]
    if observed!=expected:raise ValueError('actual workspace instruction content differs from frozen condition')
    return {'status':'observed' if observed else 'none_observed','complete':True,'source':'authoritative_session_exact_bytes',
            'agents_content_sha256':observed[0] if observed else None,
            'matches_frozen_condition':True,'audit':audit}


def prepare_output_path(output_dir,bundle_dir):
    requested=Path(output_dir).absolute()
    for p in (requested,*requested.parents):
        if p.is_symlink() and not (p==Path('/tmp') and p.resolve()==Path('/private/tmp')):
            raise ValueError('output path may not use symlinks')
    out=requested.resolve()
    protected=[Path.home()/'.dsh',Path.home()/'dsh-battery-home',Path(bundle_dir).resolve(),SOURCE]
    if any(out==p or p in out.parents for p in protected):
        raise ValueError('output path overlaps a protected runtime or source directory')
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError('output directory must be new or empty')
    out.mkdir(parents=True,exist_ok=True)
    return out


def run_item(bundle_dir,item_id,condition_id,output_dir,*,max_requests=8,max_output_tokens=4096,
             timeout=180,model='deepseek-flash',reasoning_effort='high',mode='live',
             profile_snapshot=None,memory_snapshot=None,credential_path=None,mock_command=None,
             memorix_enabled=False,mock_tool=None):
    if (mode not in ('live','mock') or type(max_requests) is not int or not 1<=max_requests<=100
            or type(max_output_tokens) is not int or not 1<=max_output_tokens<=32768
            or not isinstance(timeout,(int,float)) or not 1<=timeout<=1800
            or not isinstance(model,str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,100}',model)
            or reasoning_effort not in ('off','low','high','max')):
        raise ValueError('invalid mode, model, reasoning effort or bounded execution budgets')
    if memorix_enabled and memory_snapshot is None:raise ValueError('Memorix requires a frozen memory_snapshot directory')
    out = prepare_output_path(output_dir,bundle_dir)
    result = {'schema_version':'dsh-adapter-run.v1','run_id':uuid.uuid4().hex,'item_id':item_id,
              'condition_id':condition_id,'subject':'isolated-headless','profile':'isolated-headless',
              'status':'blocked','model_started':False,'mock':mode=='mock','exit_code':None,
              'session_id':None,'answer':'','paths':{},'sha256':{},'conditions':{},'tool_events_complete':False,
              'started_at':datetime.now(timezone.utc).isoformat(),
              'adapter_source_sha256':_LOADED_SOURCE_SHA256,'source_hash_scope':'module_import_time',
              'budgets':{'max_requests':max_requests,'max_output_tokens_per_request':max_output_tokens,'timeout_seconds':timeout},
              'configuration_differences':['shipped base+headless; daily desktop/realshell equivalence is not established',
                  'external web/MCP/plugins, title inference, telemetry and subagents disabled',
                  'provider transport is host-only anonymous pipes; child/tools have no network',
                  'internal DSH permission preset delegates; outer OS sandbox remains enforced']}
    bridge = None
    memory_runtime=None
    try:
        if profile_snapshot is not None:
            raise ValueError('external profile runtime snapshot adaptation is not yet validated; refusing silent core substitution')
        if mode not in ('live','mock') or not 1<=timeout<=1800:
            raise ValueError('invalid execution mode or wall-clock budget')
        validate_app_path(APP)
        prep = prepare(bundle_dir,item_id,condition_id,memory_snapshot=memory_snapshot)
        (out/'preparation.json').write_text(json.dumps(prep,ensure_ascii=False,indent=2)+'\n', encoding='utf-8')
        if prep['status']!='prepared':raise ValueError(prep.get('blocked_reason','preparation failed'))
        active = Path(prep['active_root']); control = Path(prep['run_dir'])/'control'; workspace=Path(prep['workspace_path'])
        result.update(workspace_path=str(workspace),run_dir=prep['run_dir'],bundle_sha256=prep['bundle_sha256'])
        bootstrap = active/'adapter-bootstrap';bootstrap.mkdir()
        preload = bootstrap/'provider-preload.cjs';shutil.copyfile(SOURCE/'dsh_adapter_preload.cjs',preload)
        patchfile = bootstrap/'adapter.patch.json'
        rows=patch_rows(model,reasoning_effort,max_output_tokens)
        if memorix_enabled:
            import memorix_snapshot as memorix
            memory_runtime=memorix.prepare_memorix(active,source_data=Path(prep['memory_path']))
            result['subject']=result['profile']='user-memory-snapshot'
            result['configuration_differences'][1]='external web and non-Memorix MCP/plugins, title inference, telemetry and subagents disabled'
            result['configuration_differences'].extend(memory_runtime['differences'])
            memory_runtime['runtime_start_sha256']=inventory(Path(memory_runtime['data_dir']))['sha256']
            (out/'memory-runtime.json').write_text(json.dumps(memory_runtime,ensure_ascii=False,indent=2)+'\n', encoding='utf-8')
            runtime_root=Path(memory_runtime['runtime_root'])
            for name in ('snapshot.json','backup.sb'):
                if (runtime_root/name).exists():shutil.move(runtime_root/name,control/('memory-'+name))
            rows.append({'insert':[{'id':'mcp-memorix-local','name':'@deepseek-ai/dsh-mcp-client','config':{
                'serverName':'memorix','transport':'stdio','command':memory_runtime['command'][0],
                'args':memory_runtime['command'][1:],'env':memory_runtime['env'],'cwd':memory_runtime['workspace'],
                'failOnStartupError':True,'toolCallTimeoutMs':30000,'reconnect':{'enabled':False}}}]})
        patchfile.write_text(json.dumps(rows), encoding='utf-8')
        protected = [SOURCE.parent, control, Path.home()/'.dsh']
        policy = adapter_policy(active,protected)
        policyfile=control/'adapter.sb';policyfile.write_text(policy, encoding='utf-8')
        env=clean_env(active)
        env['PATH']=str(Path(sys.executable).resolve().parent)+os.pathsep+env['PATH']
        env.update(ELECTRON_RUN_AS_NODE='1',DSH_BRIDGE_DUMMY_KEY='bridge-placeholder-not-a-secret',
                   DSH_PERMISSION_MODE='danger-full-access',DSH_TELEMETRY_MODE='DISABLED')
        result['runtime_preflight']=verify_python_runtime(policyfile,env,workspace)
        result['runtime_preflight']['path']=env['PATH']
        command=['/usr/bin/sandbox-exec','-f',str(policyfile),str(ELECTRON),'--expose-internals',str(CLI),
                 '--profile','validation','--from-default-profile','headless','--patch',str(patchfile)]
        dump=run_child(command+['--dump-config'],cwd=workspace,env=env,capture_output=True,timeout=30)
        (out/'dump.yml').write_bytes(dump.stdout)
        if dump.returncode:raise ValueError('isolated DSH configuration startup failed: '+dump.stderr.decode(errors='replace')[-600:])
        creation_flag=command.index('--from-default-profile');del command[creation_flag:creation_flag+2]
        token=load_host_token(credential_path or Path.home()/'.dsh/.credentials.yaml') if mode=='live' else None
        bridge=ProviderBridge(mode,token,model,max_requests,max_output_tokens,timeout,mock_command,mock_tool)
        parent_read,child_write=os.pipe();child_read,parent_write=os.pipe()
        env.update(DSH_BRIDGE_INPUT_FD=str(child_read),DSH_BRIDGE_OUTPUT_FD=str(child_write))
        # Require is a direct Node argument; NODE_OPTIONS is absent from tool environment.
        command[5:5]=['--require',str(preload)]
        prompt=Path(prep['prompt_path']).read_bytes();(out/'prompt.txt').write_bytes(prompt)
        started=time.monotonic()
        proc=subprocess.Popen(command+['--json','--','-'],cwd=workspace,env=env,
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                pass_fds=(child_read,child_write),start_new_session=True)
        try:
            bridge.bind_worker_root(proc)
        except BaseException:
            # Popen still owns this unreaped child; no prompt has been sent.
            proc.kill()
            proc.communicate(timeout=5)
            os.close(parent_read);os.close(parent_write)
            os.close(child_read);os.close(child_write)
            raise
        os.close(child_read);os.close(child_write)
        monitor=threading.Thread(target=bridge.monitor_workers,daemon=True);monitor.start()
        thread=threading.Thread(target=bridge.serve,args=(parent_read,parent_write),daemon=True);thread.start()
        timed_out=False
        try:
            try:stdout,stderr=proc.communicate(prompt,timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out=True;kill_tree(proc);stdout,stderr=proc.communicate()
            except KeyboardInterrupt:
                bridge.stop()
                try:kill_tree(proc)
                except ProcessLookupError:pass
                stdout,stderr=proc.communicate(timeout=5)
                (out/'events.jsonl').write_bytes(stdout)
                (out/'stderr.txt').write_bytes(stderr)
                raise
        finally:
            # Cancellation and parsing failures must never leave a live inference/tool tree.
            bridge.stop()
            monitor.join(timeout=2)
            try:kill_tree(proc)
            except ProcessLookupError:pass
            if proc.poll() is None:proc.communicate(timeout=5)
            bridge.kill_workers()
            thread.join(timeout=2)
            bridge.kill_workers()
        result['worker_cleanup']=bridge.cleanup_result
        result.update(exit_code=proc.returncode,elapsed_seconds=round(time.monotonic()-started,3),
                      model_started=mode=='live' and bridge.calls>0,provider_requests=bridge.calls)
        if timed_out:result['status']='timeout'
        (out/'events.jsonl').write_bytes(stdout);(out/'stderr.txt').write_bytes(stderr)
        (out/'request-audit.json').write_text(json.dumps(bridge.audit,ensure_ascii=False,indent=2)+'\n', encoding='utf-8')
        events=[json.loads(line) for line in stdout.decode().splitlines() if line.strip()]
        termination=native_termination(events)
        if termination:result.update(termination_reason=termination,censored=True)
        sessions={e['sessionId'] for e in events if e.get('type')=='session' and e.get('sessionId')}
        answers=[e.get('text','') for e in events if e.get('type')=='final']
        if len(sessions)!=1:raise ValueError('run did not establish one unique DSH session')
        sid=next(iter(sessions));result['session_id']=sid
        session_dirs=list((active/'dsh-home/sessions').glob('*/'+sid))
        if len(session_dirs)!=1:raise ValueError('authoritative DSH session directory missing or ambiguous')
        raw=audit_injection.read_session_log(session_dirs[0]);audit=audit_injection.analyze_log(raw)
        if audit['status']!='ok' or audit.get('observed_prompt_sha256')!=digest_bytes(prompt):
            raise ValueError('authoritative session prompt does not match the frozen item')
        records,tool_events=public_session(raw)
        (out/'session.jsonl').write_text(''.join(json.dumps(e,ensure_ascii=False)+'\n' for e in records), encoding='utf-8')
        (out/'public-tools.jsonl').write_text(''.join(json.dumps(e,ensure_ascii=False)+'\n' for e in tool_events), encoding='utf-8')
        logs=sorted(session_dirs[0].glob('session.v*.jsonl.zstd'),key=lambda p:int(p.name.split('.')[1][1:]))
        shutil.copyfile(logs[-1],out/'session.raw.jsonl.zstd')
        answer=answers[-1].strip() if answers else ''
        public_answers=[''.join(c.get('text','') for c in e.get('data',{}).get('message',{}).get('content',[]) if c.get('type')=='text').strip()
                        for e in records if e.get('type')=='assistant/message']
        public_answers=[a for a in public_answers if a]
        if answer and (not public_answers or public_answers[-1]!=answer):raise ValueError('public answer differs from authoritative session')
        (out/'answer.txt').write_text(answer, encoding='utf-8')
        actual_rules=actual_instruction_evidence(raw,prep['condition_file_sha256'],audit)
        result.update(answer=answer,prompt_sha256=digest_bytes(prompt),candidate_sha256=candidate_digest(workspace),
                      workspace_inventory_sha256=inventory(workspace)['sha256'],tool_events_complete=True,
                      status='timeout' if timed_out else 'completed' if proc.returncode==0 and answer and not termination else 'failed')
        result['conditions']={k:prep.get(k) for k in ('memory_snapshot_sha256','workspace_content_sha256','git_content_sha256','config_source','memory_source')}
        result['conditions'].update(provider='deepseek-account-via-host-bridge',model=model,reasoning_effort=reasoning_effort,
            max_output_tokens=max_output_tokens,subject=result['subject'],policy_sha256=digest_bytes(policy.encode()),
            tools_exposed=bridge.audit[0].get('tools',[]) if bridge.audit else [],
            actual_injection=actual_rules,memory_runtime_status='copied_not_retrieved' if memory_snapshot else 'absent',
            model_visible_workspace_path=str(workspace))
        result['conditions']['memorix_enabled']=memorix_enabled
        if memory_runtime:
            prefix='mcp__memorix__'
            retrieval_tools=[prefix+n for n in ('memorix_search','memorix_detail','memorix_context_pack','memorix_project_context')]
            write_tools=[prefix+n for n in ('memorix_store','memorix_resolve')]
            exposed=set(result['conditions']['tools_exposed'])
            available={prefix+'memorix_search',prefix+'memorix_detail'}.issubset(exposed)
            result['conditions'].update(memory_available=available,
                memory_runtime_status='mcp_available' if available else 'mcp_unavailable',
                memory_runtime_start_sha256=memory_runtime['runtime_start_sha256'],
                memory_runtime_code_sha256=memory_runtime['dependencies']['runtime_sha256'],
                memory_namespace=memory_runtime['namespace'],memory_retrieval_tools=retrieval_tools,
                memory_write_tools=write_tools)
            retrieval_ids=[e['call_id'] for e in tool_events if e['kind']=='tool_call' and e['tool_name'] in retrieval_tools]
            write_ids=[e['call_id'] for e in tool_events if e['kind']=='tool_call' and e['tool_name'] in write_tools]
            result['memory_usage']={'retrieval_calls':len(retrieval_ids),'write_calls':len(write_ids),
                'retrieval_call_ids':retrieval_ids,'write_call_ids':write_ids,'content_effect':'unverified'}
            if not available:raise ValueError('requested Memorix retrieval tools were not actually exposed')
    except KeyboardInterrupt:
        result.update(status='failed',cancelled=True,termination_reason='user_cancelled',
                      error='User cancelled the assessment; partial evidence is retained.')
        raise
    except Exception as error:
        result['error']=str(error)
        if bridge and bridge.calls:result['model_started']=mode=='live';result['provider_requests']=bridge.calls
        if result.get('exit_code') is not None and result['status']!='timeout':result['status']='failed'
    finally:
        if bridge:
            bridge.stop()
            result.update(model_started=mode=='live' and bridge.calls>0,provider_requests=bridge.calls)
            if bridge.cleanup_result is not None:result['worker_cleanup']=bridge.cleanup_result
            (out/'request-audit.json').write_text(json.dumps(bridge.audit,ensure_ascii=False,indent=2)+'\n', encoding='utf-8')
        if result['status']=='timeout':result['termination_reason']='wall_clock_budget_exhausted';result['censored']=True
        elif bridge and any(row.get('rejected') and 'request budget exhausted' in row.get('reason','') for row in bridge.audit):
            result['termination_reason']='request_budget_exhausted';result['censored']=True
        for key,name in [('prompt','prompt.txt'),('answer','answer.txt'),('events','public-tools.jsonl'),
                         ('native_events','events.jsonl'),('session','session.jsonl'),('raw_session','session.raw.jsonl.zstd'),
                         ('request_audit','request-audit.json'),('stderr','stderr.txt'),('dump','dump.yml'),
                         ('memory_runtime','memory-runtime.json')]:
            if (out/name).exists():result['paths'][key]=name;result['sha256'][key]=digest_bytes((out/name).read_bytes())
        result['paths']['meta']='meta.json'
        (out/'meta.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n', encoding='utf-8')
    return result


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--bundle',type=Path,default=SOURCE.parent/'validation');ap.add_argument('--item',required=True)
    ap.add_argument('--condition',choices=('baseline','project_rules'),default='baseline')
    ap.add_argument('--output-dir',type=Path,required=True);ap.add_argument('--mode',choices=('mock','live'),default='mock')
    ap.add_argument('--max-requests',type=int,default=8);ap.add_argument('--max-output-tokens',type=int,default=4096)
    ap.add_argument('--timeout',type=int,default=180);ap.add_argument('--model',default='deepseek-flash')
    ap.add_argument('--reasoning-effort',choices=('off','low','high','max'),default='high')
    ap.add_argument('--memory-snapshot',type=Path);ap.add_argument('--mock-command')
    args=ap.parse_args(argv)
    result=run_item(args.bundle,args.item,args.condition,args.output_dir,max_requests=args.max_requests,
        max_output_tokens=args.max_output_tokens,timeout=args.timeout,model=args.model,
        reasoning_effort=args.reasoning_effort,mode=args.mode,memory_snapshot=args.memory_snapshot,mock_command=args.mock_command)
    print(json.dumps({k:result.get(k) for k in ('status','session_id','error','model_started','provider_requests','workspace_path')},ensure_ascii=False))
    return 0 if result['status']=='completed' else 2


if __name__=='__main__':sys.exit(main())
