#!/usr/bin/env python3
"""Frozen, bounded DSH assessment batches. plan is offline; run makes real model calls."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import sys
import uuid

from validation_run import bundle_loader
from isolation_support import inventory

ROOT=Path(__file__).resolve().parents[1]


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, payload):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n', encoding='utf-8')
    temp.replace(path)


def create_plan(output_dir, *, bundle_dir=None, items=None, condition='project_rules', repeats=1,
                mode='live', max_requests=8, max_output_tokens=4096, timeout=240, seed=20261011,
                model='deepseek-flash', reasoning_effort='high', label=None, memory_snapshot=None):
    bundle=Path(bundle_dir or ROOT/'validation').resolve()
    frozen=bundle_loader()(bundle)
    if mode not in ('live','mock') or condition not in {c['id'] for c in frozen['conditions']}:
        raise ValueError('unsupported mode or condition')
    for name,value,limit in [('repeats',repeats,10),('max_requests',max_requests,30),
                             ('max_output_tokens',max_output_tokens,16384),('timeout',timeout,900)]:
        if type(value) is not int or not 1 <= value <= limit:
            raise ValueError(f'{name} must be an integer in 1..{limit}')
    available={i['id']:i for i in frozen['items']}
    wanted=list(available) if not items else list(items)
    if len(set(wanted))!=len(wanted) or not wanted or any(i not in available for i in wanted):
        raise ValueError('empty, duplicate or unknown item selection')
    memory = None
    if memory_snapshot is not None:
        memory = Path(memory_snapshot)
        memory_inventory = inventory(memory, credentials_forbidden=True)
        if not all((memory/name).is_file() for name in ('memorix.db','.project-aliases.json')):
            raise ValueError('frozen memory snapshot requires memorix.db and .project-aliases.json')
        memory = memory.resolve()
        if memory.is_relative_to(Path.home()/'.dsh'):
            raise ValueError('use a frozen memory copy, not the live DSH data directory')
    destination=Path(output_dir).resolve()
    if destination==ROOT or destination.is_relative_to(bundle) or destination.is_relative_to(Path.home()/'.dsh'):
        raise ValueError('assessment destination must not overwrite source bundle or real DSH home')
    if memory is not None and destination.is_relative_to(ROOT):
        raise ValueError('memory-enabled raw evidence must use a private output directory outside the repository')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('assessment output directory must be new or empty')
    jobs=[{'item_id':item,'condition_id':condition,'repetition':rep} for rep in range(1,repeats+1) for item in wanted]
    random.Random(seed).shuffle(jobs)
    for i,job in enumerate(jobs,1):job['slot_id']=f'run-{i:03}'
    plan={'schema_version':'harness-assessment-plan.v1','batch_id':uuid.uuid4().hex,
          'created_at':now(),'label':label or '受控 Harness 行为测试','mode':mode,
          'purpose':'pipeline_acceptance_and_exploratory_behavior',
          'bundle_path':str(bundle),'bundle_sha256':frozen['bundle_sha256'],
          'model':model,'reasoning_effort':reasoning_effort,'seed':seed,'schedule':jobs,
          'max_requests_per_run':max_requests,'max_output_tokens_per_request':max_output_tokens,
          'timeout_seconds_per_run':timeout,'max_provider_requests':len(jobs)*max_requests,
          'retries':0,'stop_on_infrastructure_failure':True,
          'execution_protocol_version':'dsh-assessment.v1',
          'subject_expectation':'isolated-headless',
          'limitations':['This controlled profile is not established equivalent to the user desktop harness.',
              'One fresh session per item; neither answers nor reports are written into user memory.',
              'Descriptive observations only; repetitions are not independent validation of personality.',
              'Workspace paths may differ; this batch is not a causal A/B comparison.']}
    if memory is not None:
        plan.update(subject_expectation='user-memory-snapshot',memorix_enabled=True,
                    memory_snapshot=str(memory),memory_snapshot_sha256=memory_inventory['sha256'])
        plan['limitations'].append('Copied Memorix MCP is available; actual retrieval is recorded separately. Other desktop tools and context layers are not reproduced.')
    destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    if memory is not None:
        destination.chmod(0o700)
    save(destination/'plan.json',plan)
    (destination/'plan.sha256').write_text(sha(destination/'plan.json')+'\n', encoding='utf-8')
    return destination/'plan.json'


def load_plan(path):
    path=Path(path).resolve()
    if sha(path)!=path.with_name('plan.sha256').read_text(encoding='utf-8').strip():
        raise ValueError('frozen run plan changed; create a new batch rather than rewriting it')
    plan=json.loads(path.read_text(encoding='utf-8'))
    if plan.get('schema_version')!='harness-assessment-plan.v1' or not plan.get('schedule'):
        raise ValueError('unsupported or empty run plan')
    bundle_loader()(Path(plan['bundle_path']),plan['bundle_sha256'])
    if plan.get('memorix_enabled'):
        if inventory(Path(plan['memory_snapshot']),credentials_forbidden=True)['sha256'] != plan['memory_snapshot_sha256']:
            raise ValueError('frozen memory snapshot changed; create a new plan')
    return plan


def collect_record(batch_root, run_dir, job, result, grade=None):
    paths={}; hashes={}
    files={'meta':'meta.json','prompt':'prompt.txt','answer':'answer.txt','events':'public-tools.jsonl',
           'session':'session.jsonl','native_events':'events.jsonl','request_audit':'request-audit.json',
           'candidate_archive':'candidate-archive.json'}
    if grade is not None:
        save(run_dir/'grade.json',grade);files['grade']='grade.json'
    for key,name in files.items():
        file=run_dir/name
        if file.is_file():paths[key]=file.relative_to(batch_root).as_posix();hashes[key]=sha(file)
    status=result.get('status','blocked')
    if status in ('ok','success','complete'):status='completed'
    if result.get('censored') is True:
        status='failed'
    if status=='completed' and (result.get('exit_code')!=0 or not (run_dir/'answer.txt').is_file()
                                or not (run_dir/'answer.txt').read_text(encoding='utf-8').strip()):
        status='failed'
    execution_status = json.loads((run_dir/'meta.json').read_text(encoding='utf-8')).get('status') if (run_dir/'meta.json').is_file() else None
    return {'run_id':result.get('run_id') or job['slot_id'],'slot_id':job['slot_id'],
        'item_id':job['item_id'],'condition_id':job['condition_id'],'repetition':job['repetition'],
        'status':status,'execution_status':execution_status,'paths':paths,'sha256':hashes,
        'candidate_sha256':result.get('candidate_sha256'),
        'conditions':result.get('conditions') or result.get('measurement_conditions') or {},
        'subject':result.get('subject','isolated-headless'),
        'model_started':result.get('model_started',False),
        'termination_reason':result.get('termination_reason'),
        'error':result.get('error') or result.get('blocked_reason')}


def run_batch(plan_path, *, adapter=None, grader=None, progress=None, cancelled=None):
    path=Path(plan_path).resolve();plan=load_plan(path);root=path.parent
    manifest_path=root/'batch.json'
    if manifest_path.exists():
        raise ValueError('batch already started; automatic resume/retry is disabled; preserve it and create a new plan')
    if adapter is None:
        from dsh_adapter import run_item
        adapter=run_item
    if grader is None:
        from assessment_grading import grade_item
        grader=grade_item
    manifest={'schema_version':'harness-assessment-batch.v1','batch_id':plan['batch_id'],
              'profile':plan['label'],'subject':plan['subject_expectation'],'mode':plan['mode'],
              'bundle_sha256':plan['bundle_sha256'],'created_at':now(),
              'plan_path':'plan.json','plan_sha256':sha(path),'planned_runs':len(plan['schedule']),
              'status':'running','runs':[],'limitations':plan['limitations']}
    save(manifest_path,manifest)
    by_id={i['id']:i for i in bundle_loader()(Path(plan['bundle_path']),plan['bundle_sha256'])['items']}
    for job in plan['schedule']:
        if cancelled and cancelled():
            manifest['status']='cancelled';break
        run_dir=root/'runs'/job['slot_id'];run_dir.mkdir(parents=True)
        grade=None
        try:
            load_plan(path)
            memory_args = ({'memorix_enabled':True,'memory_snapshot':Path(plan['memory_snapshot'])}
                           if plan.get('memorix_enabled') else {})
            result=adapter(Path(plan['bundle_path']),job['item_id'],job['condition_id'],run_dir,
                max_requests=plan['max_requests_per_run'],max_output_tokens=plan['max_output_tokens_per_request'],
                timeout=plan['timeout_seconds_per_run'],model=plan['model'],
                reasoning_effort=plan['reasoning_effort'],mode=plan['mode'],**memory_args)
            if result.get('candidate_sha256') and result.get('workspace_path'):
                from archive_assessment import archive_candidate
                archive_candidate(Path(result['workspace_path']),run_dir,result['candidate_sha256'])
            if result.get('status') in ('ok','success','completed','complete') and by_id[job['item_id']]['protocol']=='task':
                grade=grader(Path(plan['bundle_path']),job['item_id'],Path(result['workspace_path']),plan['bundle_sha256'])
            load_plan(path)
        except KeyboardInterrupt:
            result={'status':'failed','termination_reason':'user_cancelled','cancelled':True,
                    'error':'User cancelled; partial evidence is retained.',
                    'model_started':False,'subject':plan['subject_expectation']}
            if (run_dir/'meta.json').is_file():
                existing=json.loads((run_dir/'meta.json').read_text(encoding='utf-8'))
                result.update(model_started=existing.get('model_started',False),run_id=existing.get('run_id'),
                              conditions=existing.get('conditions',{}),candidate_sha256=existing.get('candidate_sha256'),
                              subject=existing.get('subject',plan['subject_expectation']))
            else:
                save(run_dir/'meta.json',dict(result,schema_version='dsh-adapter-run.v1',
                    run_id=job['slot_id'],item_id=job['item_id'],condition_id=job['condition_id'],exit_code=None))
        except (OSError,ValueError,KeyError,RuntimeError) as exc:
            result={'status':'blocked','error':str(exc),'model_started':False,'subject':plan['subject_expectation']}
            if (run_dir/'meta.json').is_file():
                existing=json.loads((run_dir/'meta.json').read_text(encoding='utf-8'))
                result.update(model_started=existing.get('model_started',False),run_id=existing.get('run_id'),
                              conditions=existing.get('conditions',{}),candidate_sha256=existing.get('candidate_sha256'),
                              subject=existing.get('subject',plan['subject_expectation']))
            else:
                save(run_dir/'meta.json',dict(result,schema_version='dsh-adapter-run.v1',
                    error_stage='orchestration_before_adapter',
                    run_id=job['slot_id'],item_id=job['item_id'],condition_id=job['condition_id'],exit_code=None))
            save(run_dir/'orchestration-error.json',{'error':str(exc),'recorded_at':now()})
        record=collect_record(root,run_dir,job,result,grade)
        manifest['runs'].append(record)
        save(manifest_path,manifest)
        if progress:
            progress(record,manifest)
        else:
            print(f"{job['slot_id']} {job['item_id']}: {record['status']}",flush=True)
        if result.get('cancelled'):
            manifest['status']='cancelled';break
        if record['status']!='completed':
            manifest['status']='stopped_on_failure';break
    else:
        manifest['status']='completed'
    manifest['finished_at']=now()
    manifest['not_started_slots']=[j['slot_id'] for j in plan['schedule'][len(manifest['runs']):]]
    save(manifest_path,manifest)
    return manifest_path


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__);sub=ap.add_subparsers(dest='command',required=True)
    p=sub.add_parser('plan');p.add_argument('--out',required=True,type=Path);p.add_argument('--items',nargs='*')
    p.add_argument('--bundle',type=Path,default=ROOT/'validation')
    p.add_argument('--condition',choices=['baseline','project_rules'],default='project_rules')
    p.add_argument('--repeats',type=int,default=1);p.add_argument('--mode',choices=['live','mock'],default='live')
    p.add_argument('--max-requests',type=int,default=8);p.add_argument('--max-output-tokens',type=int,default=4096)
    p.add_argument('--timeout',type=int,default=240);p.add_argument('--seed',type=int,default=20261011)
    p.add_argument('--label');p.add_argument('--model',default='deepseek-flash')
    p.add_argument('--reasoning-effort',default='high')
    p.add_argument('--memory-snapshot',type=Path)
    r=sub.add_parser('run');r.add_argument('--plan',required=True,type=Path)
    args=ap.parse_args(argv)
    try:
        if args.command=='plan':
            output=create_plan(args.out,bundle_dir=args.bundle,items=args.items,condition=args.condition,repeats=args.repeats,
                mode=args.mode,max_requests=args.max_requests,max_output_tokens=args.max_output_tokens,
                timeout=args.timeout,seed=args.seed,label=args.label,model=args.model,reasoning_effort=args.reasoning_effort,
                memory_snapshot=args.memory_snapshot)
            print(output);return 0
        output=run_batch(args.plan);print(output)
        return 0 if json.loads(output.read_text(encoding='utf-8'))['status']=='completed' else 2
    except (OSError,ValueError,ImportError) as error:
        print('assessment: '+str(error),file=sys.stderr);return 2


if __name__=='__main__':
    raise SystemExit(main())
