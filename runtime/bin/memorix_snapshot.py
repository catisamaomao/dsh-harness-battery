#!/usr/bin/env python3
"""Make a consistent, isolated Memorix runtime; never export memory text in evidence.

The source SQLite connection is read-only and the backup worker additionally has
an OS deny-write rule for the original data. MCP uses only the copied database,
an empty HOME, no inherited credentials, and an OS network denial. Maintenance
may update the disposable copy; no claim of a read-only Memorix implementation.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

from isolation_support import clean_env, digest_bytes, ensure_run_dirs, inventory, sandbox_policy

SOURCE=Path(__file__).resolve().parent
_LOADED_SOURCE_SHA256=digest_bytes(Path(__file__).read_bytes())
DEFAULT_DATA=Path.home()/'.dsh/memorix-data'
DEFAULT_MODULES=Path.home()/'.dsh/mcp-servers/node_modules'
NODE=Path(os.environ.get('DSH_HARNESS_NODE') or shutil.which('node') or '/usr/local/bin/node')

BACKUP_WORKER=r'''
import sqlite3,sys,json,os
from pathlib import Path
source,target=map(Path,sys.argv[1:3])
try:
 fd=os.open(source,os.O_WRONLY)
except PermissionError:pass
else:
 os.close(fd)
 raise ValueError('source write-open was not denied by the OS')
with sqlite3.connect(source.as_uri()+'?mode=ro&cache=private',uri=True,timeout=10) as original:
 original.execute('PRAGMA query_only=ON')
 with sqlite3.connect(target) as copy:
  original.backup(copy,pages=256,sleep=0.01)
  result=copy.execute('PRAGMA integrity_check').fetchone()[0]
  if result!='ok':raise ValueError('backup integrity check failed')
  copy.execute('PRAGMA journal_mode=DELETE')
print(json.dumps({'consistent_backup':True,'integrity_check':'ok','source_connection':'mode=ro/query_only','source_write_open_denied':True}))
'''


def regular(path):
    path=Path(path)
    if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError('snapshot input must be a regular non-symlink file')
    return path


def file_state(root):
    names=('memorix.db','memorix.db-wal','memorix.db-shm','.project-aliases.json')
    return {name:digest_bytes(regular(Path(root)/name).read_bytes()) for name in names if (Path(root)/name).exists()}


def consistent_backup(source_data,target_data,active_root):
    source_data=Path(source_data).resolve();target_data=Path(target_data)
    db=regular(source_data/'memorix.db')
    alias=regular(source_data/'.project-aliases.json')
    alias_bytes=alias.read_bytes()
    before=file_state(source_data)
    target_data.mkdir(parents=True,exist_ok=False)
    policy=sandbox_policy(active_root,[SOURCE.parent])
    sqlite_lib=Path('/opt/homebrew/opt/sqlite/lib')
    if sqlite_lib.is_dir():
        policy+='(allow file-read* file-map-executable (subpath '+json.dumps(str(sqlite_lib.resolve()))+'))\n'
    policy+='(allow file-read* (subpath '+json.dumps(str(source_data))+'))\n'
    policy+='(deny file-write* (subpath '+json.dumps(str(source_data))+'))\n'
    policy_path=Path(active_root)/'backup.sb';policy_path.write_text(policy)
    result=subprocess.run(['/usr/bin/sandbox-exec','-f',str(policy_path),str(Path(sys.executable).resolve()),
                          '-B','-c',BACKUP_WORKER,str(db),str(target_data/'memorix.db')],
                         cwd=active_root,env=clean_env(active_root),capture_output=True,text=True,timeout=60)
    if result.returncode:
        raise ValueError('read-only SQLite backup failed; source details withheld')
    evidence=json.loads(result.stdout)
    if alias.read_bytes()!=alias_bytes:raise ValueError('project aliases changed during snapshot')
    (target_data/'.project-aliases.json').write_bytes(alias_bytes)
    after=file_state(source_data)
    evidence.update(source_file_hashes_before=before,source_file_hashes_after=after,
                    source_files_unchanged_observed=before==after,
                    snapshot_database_sha256=digest_bytes((target_data/'memorix.db').read_bytes()),
                    original_alias_sha256=digest_bytes(alias_bytes),
                    policy_sha256=digest_bytes(policy.encode()))
    return evidence


def resolve_package(name,from_package,modules_root):
    if not isinstance(name,str) or name.startswith(('/', '.')) or '..' in name.split('/'):
        raise ValueError('invalid dependency name')
    for ancestor in (Path(from_package),*Path(from_package).parents):
        candidate=ancestor/'node_modules'/name
        if (candidate/'package.json').is_file():
            if candidate.is_symlink() or not candidate.resolve().is_relative_to(modules_root):
                raise ValueError('dependency escapes installed node_modules')
            return candidate
    raise ValueError('required runtime dependency is unavailable: '+name)


def dependency_closure(modules_root):
    modules_root=Path(modules_root).resolve();queue=[modules_root/'memorix'];found={};omitted=set()
    while queue:
        package=queue.pop()
        if package in found:continue
        if package.is_symlink() or not package.resolve().is_relative_to(modules_root):
            raise ValueError('runtime package root escapes installed node_modules')
        manifest=json.loads(regular(package/'package.json').read_text())
        found[package]=manifest
        optional=set(manifest.get('optionalDependencies',{}));omitted.update(optional)
        dependencies=dict(manifest.get('dependencies',{}))
        for name in manifest.get('peerDependencies',{}):
            if not manifest.get('peerDependenciesMeta',{}).get(name,{}).get('optional'):
                dependencies.setdefault(name,'peer')
        for name in dependencies:
            if name not in optional:queue.append(resolve_package(name,package,modules_root))
    return found,sorted(omitted)


def copy_runtime(modules_root,target_modules):
    modules_root=Path(modules_root).resolve();target_modules=Path(target_modules)
    packages,omitted=dependency_closure(modules_root)
    files={};source_files=[]
    for package,manifest in sorted(packages.items()):
        for parent,dirs,names in os.walk(package,followlinks=False):
            dirs[:]=sorted(d for d in dirs if d not in ('node_modules','.git'))
            for d in dirs:
                if (Path(parent)/d).is_symlink():raise ValueError('runtime package directory symlink rejected')
            for name in sorted(names):
                source=regular(Path(parent)/name)
                if name=='.env' or name=='.credentials.yaml':raise ValueError('runtime package contains environment credentials')
                relative=source.relative_to(modules_root)
                destination=target_modules/relative;destination.parent.mkdir(parents=True,exist_ok=True)
                data=source.read_bytes();destination.write_bytes(data);destination.chmod(source.stat().st_mode&0o777)
                digest=digest_bytes(data);files[relative.as_posix()]=digest;source_files.append((source,digest))
    for source,expected in source_files:
        if digest_bytes(regular(source).read_bytes())!=expected:raise ValueError('runtime dependency changed during copy')
    return {'package_count':len(packages),'file_count':len(files),
            'runtime_sha256':digest_bytes(json.dumps(files,sort_keys=True,separators=(',',':')).encode()),
            'memorix_version':packages[modules_root/'memorix']['version'],
            'omitted_optional_packages':omitted,'sqlite_driver':'node:sqlite'}


def remap_namespace(data,workspace,source_workspace):
    aliases_path=Path(data)/'.project-aliases.json';aliases=json.loads(aliases_path.read_text())
    if aliases.get('version')!=1:raise ValueError('unsupported project alias version')
    groups=aliases.get('groups',[])
    matches=[group for group in groups if str(Path(source_workspace).resolve()) in group.get('rootPaths',[])]
    if len(matches)!=1:raise ValueError('source memory workspace must identify exactly one alias namespace')
    group=matches[0];canonical=group['canonical']
    if canonical not in group.get('aliases',[]):raise ValueError('canonical namespace absent from aliases')
    local_alias='local/'+Path(workspace).name
    if any(g is not group and (local_alias==g.get('canonical') or local_alias in g.get('aliases',[])) for g in groups):
        raise ValueError('isolated workspace alias collides with another namespace')
    # Keep the canonical ID but remove original absolute roots from the runtime's aliases.
    for g in groups:g['rootPaths']=[str(Path(workspace).resolve())] if g is group else []
    if local_alias not in group['aliases']:group['aliases'].append(local_alias)
    aliases_path.write_text(json.dumps(aliases,ensure_ascii=False,indent=2)+'\n')
    with sqlite3.connect('file:'+str(Path(data)/'memorix.db')+'?mode=ro',uri=True) as db:
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts={}
        for table in ('observations','knowledge_claims','long_term_memories'):
            if table in tables:counts[table]=db.execute('SELECT count(*) FROM '+table).fetchone()[0]
        ids=[row[0] for row in db.execute('SELECT id FROM observations WHERE projectId=?',(canonical,))]
    return {'namespace':canonical,'counts':counts,'namespace_observation_ids':ids,
            'mapped_alias_sha256':digest_bytes(aliases_path.read_bytes())}


def freeze_snapshot(output_dir,*,source_data=DEFAULT_DATA,source_workspace=None):
    """Freeze DB+aliases outside the repository; return body-free provenance."""
    output=Path(output_dir).absolute()
    for part in (output,*output.parents):
        if part.is_symlink() and not (part==Path('/tmp') and part.resolve()==Path('/private/tmp')):
            raise ValueError('memory snapshot destination may not follow symlinks')
    output=output.resolve();source=Path(source_data).resolve()
    if output.exists():raise ValueError('memory snapshot destination already exists')
    if output.is_relative_to(SOURCE.parent) or output.is_relative_to(Path.home()/'.dsh') or output.is_relative_to(source):
        raise ValueError('private memory snapshots must be outside source/runtime/repository')
    with tempfile.TemporaryDirectory(prefix='memorix-freeze-') as temporary:
        active=ensure_run_dirs(Path(temporary));target=active/'data'
        evidence=consistent_backup(source,target,active)
        output.parent.mkdir(parents=True,exist_ok=True,mode=0o700);shutil.move(str(target),output)
        output.chmod(0o700)
        for path in output.iterdir():path.chmod(0o600)
    source_workspace=Path(source_workspace or Path.home()/'.dsh/memory-workspace').resolve()
    aliases=json.loads((output/'.project-aliases.json').read_text())
    groups=[g for g in aliases.get('groups',[]) if str(source_workspace) in g.get('rootPaths',[])]
    if len(groups)!=1:raise ValueError('frozen source workspace has no unique namespace')
    with sqlite3.connect((output/'memorix.db').as_uri()+'?mode=ro',uri=True) as db:
        counts={table:db.execute('SELECT count(*) FROM '+table).fetchone()[0]
                for table in ('observations','knowledge_claims','long_term_memories')}
    return {'schema_version':'memorix-frozen-snapshot.v1','status':'frozen','snapshot_path':str(output),
            'memory_snapshot_sha256':inventory(output)['sha256'],'namespace':groups[0]['canonical'],
            'source_workspace':str(source_workspace),'counts':counts,'backup':evidence,
            'model_started':False,'memory_body_in_evidence':False}


def prepare_memorix(active_root,*,source_data=DEFAULT_DATA,modules_root=DEFAULT_MODULES,
                    source_workspace=None,node=NODE):
    active_root=Path(active_root).resolve()
    if not active_root.is_dir():raise ValueError('active root must already exist')
    runtime=active_root/'memorix'
    if runtime.exists():raise ValueError('refusing to overwrite a Memorix runtime')
    for original in (Path(source_data).resolve(),Path(modules_root).resolve()):
        if runtime.is_relative_to(original) or original.is_relative_to(runtime):
            raise ValueError('memory runtime and original source overlap')
    runtime.mkdir();ensure_run_dirs(runtime)
    data=runtime/'data';workspace=runtime/'memory-workspace';workspace.mkdir()
    env=clean_env(runtime)
    subprocess.run(['/usr/bin/git','init','-q',str(workspace)],env=env,check=True,capture_output=True)
    backup=consistent_backup(source_data,data,runtime)
    source_workspace=source_workspace or Path.home()/'.dsh/memory-workspace'
    namespace=remap_namespace(data,workspace,source_workspace)
    modules=runtime/'mcp-servers/node_modules'
    dependencies=copy_runtime(modules_root,modules)
    env.update(MEMORIX_DATA_DIR=str(data),MEMORIX_EMBEDDING='off',MEMORIX_AUTO_UPDATE='off',
               MEMORIX_CODEGRAPH_EXTERNAL_CONTEXT='off',MEMORIX_SQLITE_DRIVER='node',
               MEMORIX_PROJECT_ROOT=str(workspace),__MEMORIX_HEAP='1')
    command=[str(Path(node).resolve()),str(modules/'memorix/dist/cli/index.js'),
             'serve','--cwd',str(workspace),'--mode','micro']
    evidence={'schema_version':'memorix-runtime-snapshot.v1','status':'prepared',
              'helper_source_sha256':_LOADED_SOURCE_SHA256,'source_hash_scope':'module_import_time',
              'subject_component':'user-memorix-snapshot','model_started':False,
              'runtime_root':str(runtime),'data_dir':str(data),'workspace':str(workspace),
              'source_data':str(Path(source_data).resolve()),'source_workspace':str(Path(source_workspace).resolve()),
              'command':command,'env':env,'backup':backup,'dependencies':dependencies,**namespace,
              'node_sha256':digest_bytes(regular(Path(node).resolve()).read_bytes()),
              'differences':['isolated empty HOME and copied required runtime dependencies',
                 'node:sqlite replaces optional native driver; embeddings and update check disabled',
                 'all external networking denied by OS sandbox',
                 'MCP maintenance and write tools can only update disposable snapshot',
                 'project alias root mapped to isolated memory-workspace with same canonical namespace']}
    (runtime/'snapshot.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')
    return evidence


class McpClient:
    def __init__(self,snapshot,policy_path):
        self.snapshot=snapshot;self.next_id=0;self.buffers={'out':b'','err':b''}
        self.process=subprocess.Popen(['/usr/bin/sandbox-exec','-f',str(policy_path),*snapshot['command']],
            cwd=snapshot['workspace'],env=snapshot['env'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,start_new_session=True)
        self.selector=selectors.DefaultSelector()
        self.selector.register(self.process.stdout,selectors.EVENT_READ,'out')
        self.selector.register(self.process.stderr,selectors.EVENT_READ,'err')

    def send(self,message):
        self.process.stdin.write(json.dumps(message).encode()+b'\n');self.process.stdin.flush()

    def request(self,method,params=None,timeout=25):
        self.next_id+=1;request_id=self.next_id
        self.send({'jsonrpc':'2.0','id':request_id,'method':method,'params':params or {}})
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            for key,_ in self.selector.select(max(0,deadline-time.monotonic())):
                data=os.read(key.fileobj.fileno(),65536)
                if not data:
                    self.selector.unregister(key.fileobj)
                    if key.data=='out':raise ValueError('isolated MCP exited before response')
                    continue
                self.buffers[key.data]+=data
                if len(self.buffers[key.data])>4*1024*1024:raise ValueError('MCP output byte budget exceeded')
            while b'\n' in self.buffers['out']:
                line,self.buffers['out']=self.buffers['out'].split(b'\n',1)
                message=json.loads(line)
                if message.get('id')==request_id and 'method' not in message:
                    if 'error' in message:raise ValueError('MCP request rejected: '+method)
                    return message['result']
                if message.get('method')=='roots/list' and 'id' in message:
                    self.send({'jsonrpc':'2.0','id':message['id'],'result':{'roots':[
                        {'uri':Path(self.snapshot['workspace']).as_uri(),'name':'memory-workspace'}]}})
        raise ValueError('MCP request timed out: '+method)

    def close(self):
        try:os.killpg(self.process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        self.process.communicate(timeout=5);self.selector.close()


def verify_retrieval(snapshot):
    root=Path(snapshot['runtime_root']);data=Path(snapshot['data_dir'])
    policy=sandbox_policy(root,[SOURCE.parent,Path.home()/'.dsh',snapshot['source_data']])
    policy_path=root/'mcp.sb';policy_path.write_text(policy)
    before=file_state(snapshot['source_data']);runtime_before=inventory(data)['sha256']
    client=McpClient(snapshot,policy_path)
    try:
        init=client.request('initialize',{'protocolVersion':'2024-11-05','capabilities':{},
                            'clientInfo':{'name':'harness-memory-snapshot-verifier','version':'1'}})
        client.send({'jsonrpc':'2.0','method':'notifications/initialized'})
        tools=client.request('tools/list')['tools'];names=sorted(t['name'] for t in tools)
        if not {'memorix_search','memorix_detail'}.issubset(names):raise ValueError('expected retrieval tools missing')
        with sqlite3.connect('file:'+str(data/'memorix.db')+'?mode=ro',uri=True) as db:
            row=db.execute('SELECT id,title,narrative FROM observations WHERE projectId=? ORDER BY id LIMIT 1',
                           (snapshot['namespace'],)).fetchone()
        if row is None:raise ValueError('selected namespace has no observation to verify retrieval')
        ident,title,narrative=row
        search=client.request('tools/call',{'name':'memorix_search','arguments':{
            'query':title,'scope':'project','quality':'fast','status':'all','limit':1,
            'purpose':'Verify a copied existing record is retrievable; no memory-effect evaluation.'}})
        detail=client.request('tools/call',{'name':'memorix_detail','arguments':{
            'refs':[{'id':ident,'projectId':snapshot['namespace']}],
            'purpose':'Verify a copied existing record is retrievable; no memory-effect evaluation.'}})
        detail_text='\n'.join(c.get('text','') for c in detail.get('content',[]) if c.get('type')=='text')
        search_text='\n'.join(c.get('text','') for c in search.get('content',[]) if c.get('type')=='text')
        expected_ref=f"obs:{ident}@{snapshot['namespace']}"
        search_matches=any(expected_ref in line and title in line for line in search_text.splitlines())
        if search.get('isError') or not title or not search_matches:
            raise ValueError('project-scoped MCP search did not find the copied record')
        if detail.get('isError') or not title or title not in detail_text or (narrative and narrative not in detail_text):
            raise ValueError('MCP detail did not reproduce the copied record content')
        evidence={'status':'verified','protocol_version':init.get('protocolVersion'),
                  'tools_exposed':names,'namespace':snapshot['namespace'],'observation_id':ident,
                  'detail_matches_snapshot_record':True,'search_is_error':bool(search.get('isError')),
                  'search_matches_snapshot_record':True,
                  'search_response_sha256':digest_bytes(json.dumps(search,sort_keys=True).encode()),
                  'detail_response_sha256':digest_bytes(json.dumps(detail,sort_keys=True).encode()),
                  'policy_sha256':digest_bytes(policy.encode()),'network':'denied',
                  'original_data_read_write':'denied','credentials_in_child_env':False,
                  'memory_body_in_evidence':False,'model_started':False}
    finally:client.close()
    after=file_state(snapshot['source_data'])
    evidence['original_files_unchanged_observed']=before==after
    evidence['runtime_data_before_sha256']=runtime_before
    evidence['runtime_data_after_sha256']=inventory(data)['sha256']
    (root/'retrieval-evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')
    return evidence


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--active-root',type=Path)
    ap.add_argument('--freeze-out',type=Path)
    ap.add_argument('--verify-retrieval',action='store_true');args=ap.parse_args()
    if args.freeze_out:
        print(json.dumps(freeze_snapshot(args.freeze_out),ensure_ascii=False));return
    if not args.active_root:ap.error('--active-root or --freeze-out is required')
    result=prepare_memorix(args.active_root)
    if args.verify_retrieval:result['retrieval']=verify_retrieval(result)
    print(json.dumps({k:result.get(k) for k in ('status','runtime_root','namespace','counts','retrieval')},ensure_ascii=False))


if __name__=='__main__':main()
