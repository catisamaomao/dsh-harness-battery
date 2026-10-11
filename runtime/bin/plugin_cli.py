#!/usr/bin/env python3
"""Stable JSONL interface for the DeepSeek Harness plugin. Live execution is explicit."""
import sys
sys.dont_write_bytecode = True
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import threading
import uuid

import assessment
from batteries import BATTERIES, battery_path, catalogue

RUNTIME = Path(__file__).resolve().parents[1]
INSTALL_ROOT = RUNTIME.parent
MIN_PYTHON = (3, 10)


def emit(command, **payload):
    print(json.dumps({'type': 'result', 'command': command, **payload}, ensure_ascii=False), flush=True)


def status():
    from dsh_adapter import APP, validate_app_path
    app_error = None
    try:
        validate_app_path(APP)
    except (ValueError, OSError) as error:
        app_error = str(error)
    try:
        from compression import zstd  # Python 3.14 standard-library fallback.
        has_zstd = True
    except ImportError:
        has_zstd = bool(shutil.which('zstd'))
    checks = {'python': sys.version_info >= MIN_PYTHON,
              'platform': platform.system() == 'Darwin',
              'app': app_error is None,
              'seatbelt': Path('/usr/bin/sandbox-exec').is_file(),
              'git': Path('/usr/bin/git').is_file(), 'zstd': has_zstd}
    return {'status': 'ready' if all(checks.values()) else 'unavailable',
            'supported_platform': checks['platform'], 'can_run': all(checks.values()),
            'checks': checks, 'python_version': platform.python_version(),
            'minimum_python': '3.10', 'app_path': str(APP), 'app_error': app_error,
            'batteries': [{'id': name, 'item_count': catalogue(RUNTIME, name)['item_count']}
                          for name in BATTERIES],
            'model_started': False, 'credentials_read': False, 'memory_read': False,
            'limitations': ['macOS Seatbelt is required for execution; other platforms can browse questions and saved reports.',
                            'Python 3.10+ and zstd (or Python 3.14+) are required.']}


def private_data_root(value):
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise ValueError('data-dir must be an absolute private directory')
    for entry in (raw, *raw.parents):
        if entry.is_symlink() and not (entry == Path('/tmp') and entry.resolve() == Path('/private/tmp')):
            raise ValueError('data-dir must not follow a symlink')
    root = raw.resolve()
    protected = [INSTALL_ROOT.resolve(), (Path.home()/'.dsh').resolve()]
    if root == Path.home() or any(root.is_relative_to(p) or p.is_relative_to(root) for p in protected):
        raise ValueError('data-dir overlaps the installed plugin or real DSH home')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = root.stat()
    if os.name == 'posix' and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError('data-dir must belong to this user and have mode 0700')
    return root


def batch_folder(root, identifier):
    if not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{32}', identifier):
        raise ValueError('invalid batch-id')
    path = root/'batches'/identifier
    for entry in (path, path.parent):
        if entry.is_symlink():
            raise ValueError('batch path must not follow a symlink')
    if not path.is_dir():
        raise ValueError('unknown batch-id')
    return path


def make_plan(args):
    root = private_data_root(args.data_dir)
    bundle = battery_path(RUNTIME, args.battery)
    # Validate choices and bounded budgets before any requested memory backup.
    selected = catalogue(RUNTIME, args.battery)
    ids = [row['id'] for row in selected['items']]
    wanted = args.items or ids
    if len(set(wanted)) != len(wanted) or not all(item in ids for item in wanted):
        raise ValueError('duplicate or unknown question selection')
    for value, limit in ((args.max_requests, 30), (args.max_output_tokens, 16384), (args.timeout, 900)):
        if not 1 <= value <= limit:
            raise ValueError('execution budget exceeds the supported bounds')
    if args.include_memory and not status()['can_run']:
        raise ValueError('memory snapshot requires the supported macOS execution environment')
    snapshot = None
    request_id = uuid.uuid4().hex
    if args.include_memory:
        from memorix_snapshot import freeze_snapshot
        parent = root/'memory-snapshots'
        if parent.is_symlink():
            raise ValueError('snapshot parent must not be a symlink')
        parent.mkdir(mode=0o700, exist_ok=True)
        snapshot = parent/request_id
        evidence = freeze_snapshot(snapshot)
        assessment.save(parent/(request_id+'.json'), evidence)
    batches = root/'batches'
    if batches.is_symlink():
        raise ValueError('batch parent must not be a symlink')
    batches.mkdir(mode=0o700, exist_ok=True)
    stage = batches/request_id
    plan_path = assessment.create_plan(stage, bundle_dir=bundle, items=wanted,
        mode=args.mode, condition='baseline', repeats=1, max_requests=args.max_requests,
        max_output_tokens=args.max_output_tokens, timeout=args.timeout, memory_snapshot=snapshot,
        label='鲸相 · '+('记忆快照行为测试' if snapshot else '隔离配置行为测试'))
    plan = assessment.load_plan(plan_path)
    destination = batches/plan['batch_id']
    stage.rename(destination)
    return {'status': 'planned', 'batch_id': plan['batch_id'], 'plan_path': str(destination/'plan.json'),
            'battery': args.battery, 'mode': args.mode, 'item_count': len(plan['schedule']),
            'max_provider_requests': plan['max_provider_requests'],
            'max_output_tokens_per_request': plan['max_output_tokens_per_request'],
            'timeout_seconds_per_run': plan['timeout_seconds_per_run'],
            'include_memory': bool(snapshot), 'memory_copied': bool(snapshot),
            'model_started': False, 'credentials_read': False,
            'bundle_sha256': plan['bundle_sha256']}


def repair_moved_plan(folder):
    """Do not silently rewrite frozen plans after moving an installed plugin."""
    plan = json.loads((folder/'plan.json').read_text())
    expected = plan.get('bundle_path')
    if not expected or not Path(expected).is_dir():
        raise ValueError('this plan belongs to a moved installation; create a new plan (saved reports remain readable)')


def make_report(folder):
    for name in ('report.json','report.json.tmp','report.md','report.html','frontend'):
        if (folder/name).is_symlink():
            raise ValueError('report output must not follow a symlink')
    from assessment_report import build_report, render_markdown
    plan = json.loads((folder/'plan.json').read_text())
    # A saved run can use the current identical bundle after moving the installation.
    bundle = next((battery_path(RUNTIME, name) for name in BATTERIES
                   if catalogue(RUNTIME, name)['bundle_sha256'] == plan.get('bundle_sha256')), None)
    if bundle is None:
        raise ValueError('no installed frozen question pack matches this saved batch')
    report = build_report(folder/'batch.json', bundle_dir=bundle)
    assessment.save(folder/'report.json', report)
    md_path = folder/'report.md'
    if md_path.is_symlink():
        raise ValueError('report output must not follow a symlink')
    md_path.write_text(render_markdown(report), encoding='utf-8')
    scripts = RUNTIME/'frontend/scripts'
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location('harness_plugin_report_builder', scripts/'build.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with contextlib.redirect_stdout(io.StringIO()):
            module.build(folder/'report.json', folder/'frontend')
    finally:
        sys.path.remove(str(scripts))
    target = folder/'report.html'
    if target.is_symlink():
        raise ValueError('report output must not follow a symlink')
    shutil.copyfile(folder/'frontend/index.html', target)
    return {'report_json': str(folder/'report.json'), 'report_markdown': str(md_path),
            'report_html': str(target), 'included_runs': report['inventory']['included_runs']}


def _run_locked(args):
    root = private_data_root(args.data_dir)
    folder = batch_folder(root, args.batch_id)
    repair_moved_plan(folder)
    plan = assessment.load_plan(folder/'plan.json')
    if plan['mode'] != args.mode or (args.mode == 'live' and not args.allow_live):
        raise ValueError('run mode must match the plan; live execution requires explicit --allow-live')
    check = status()
    if not check['can_run']:
        raise ValueError('execution prerequisites unavailable: '+', '.join(k for k,v in check['checks'].items() if not v))
    flag = folder/'cancel-requested.json'
    stop = threading.Event()
    interrupted = threading.Event()
    previous = {}
    def interrupt(signum, frame):
        if interrupted.is_set():
            return
        interrupted.set()
        raise KeyboardInterrupt
    for kind in (signal.SIGINT, signal.SIGTERM):
        previous[kind] = signal.signal(kind, interrupt)
    def watch():
        while not stop.wait(0.2):
            if flag.exists():
                os.kill(os.getpid(), signal.SIGTERM)
                return
    watcher = threading.Thread(target=watch, daemon=True)
    assessment.save(folder/'runner.json', {'pid': os.getpid(), 'status': 'running', 'batch_id': args.batch_id})
    watcher.start()
    def progress(record, manifest):
        print(json.dumps({'type':'progress', 'command':'run', 'batch_id':args.batch_id,
            'completed':len(manifest['runs']), 'planned':manifest['planned_runs'],
            'item_id':record['item_id'], 'run_status':record['status']}, ensure_ascii=False), flush=True)
    try:
        assessment.run_batch(folder/'plan.json', progress=progress, cancelled=flag.exists)
    except KeyboardInterrupt:
        # Handles a signal between run slots or during manifest persistence.
        manifest_path = folder/'batch.json'
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            manifest.update(status='cancelled', finished_at=assessment.now(),
                not_started_slots=[j['slot_id'] for j in plan['schedule'][len(manifest['runs']):]])
            assessment.save(manifest_path, manifest)
        else:
            raise ValueError('cancelled before batch startup; plan retained') from None
    finally:
        stop.set()
        watcher.join(timeout=1)
        for kind, handler in previous.items():
            signal.signal(kind, handler)
        assessment.save(folder/'runner.json', {'pid':os.getpid(), 'status':'stopped', 'batch_id':args.batch_id})
    manifest = json.loads((folder/'batch.json').read_text())
    return {'status':manifest['status'], 'ok':manifest['status']=='completed', 'batch_id':args.batch_id,
            'batch_path':str(folder/'batch.json'), 'mode':plan['mode'],
            'completed_runs':sum(r['status']=='completed' for r in manifest['runs']),
            'planned_runs':manifest['planned_runs'],
            'model_started':any(r.get('model_started') for r in manifest['runs'])}


def run(args):
    root = private_data_root(args.data_dir)
    lock_path = root/'runner.lock'
    if lock_path.is_symlink():
        raise ValueError('runner lock must not follow a symlink')
    if platform.system() != 'Darwin':
        raise ValueError('assessment execution requires macOS Seatbelt')
    import fcntl
    with lock_path.open('a+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('another assessment is still running; cancel it and wait for shutdown') from None
        try:
            return _run_locked(args)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def main(argv=None):
    os.umask(0o077)
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='command', required=True)
    for name in ('status', 'check'):
        sub.add_parser(name).add_argument('--json', action='store_true')
    for name in ('catalogue', 'questions'):
        command = sub.add_parser(name)
        command.add_argument('--battery', choices=BATTERIES, default='core')
        command.add_argument('--json', action='store_true')
    plan = sub.add_parser('plan')
    plan.add_argument('--data-dir', required=True)
    plan.add_argument('--battery', choices=BATTERIES, default='core')
    plan.add_argument('--mode', choices=('mock','live'), required=True)
    plan.add_argument('--items', nargs='+')
    plan.add_argument('--include-memory', action='store_true')
    plan.add_argument('--max-requests', type=int, default=30)
    plan.add_argument('--max-output-tokens', type=int, default=16384)
    plan.add_argument('--timeout', type=int, default=600)
    for name in ('run','report','cancel'):
        command = sub.add_parser(name)
        command.add_argument('--data-dir', required=True)
        command.add_argument('--batch-id', required=True)
        if name == 'run':
            command.add_argument('--mode', choices=('mock','live'), required=True)
            command.add_argument('--allow-live', action='store_true')
    args = ap.parse_args(argv)
    try:
        if args.command in ('status','check'):
            result = status()
        elif args.command in ('catalogue','questions'):
            result = catalogue(RUNTIME, args.battery)
        elif args.command == 'plan':
            result = make_plan(args)
        elif args.command == 'run':
            result = run(args)
        else:
            folder = batch_folder(private_data_root(args.data_dir), args.batch_id)
            if args.command == 'report':
                result = {'status':'ready', 'batch_id':args.batch_id, **make_report(folder)}
            else:
                assessment.save(folder/'cancel-requested.json', {'requested_at':assessment.now()})
                result = {'status':'cancel_requested','batch_id':args.batch_id,'model_started':False}
        emit(args.command, **result)
        return 0
    except (OSError, ValueError, KeyError, ImportError) as error:
        emit(args.command, status='error', ok=False, error=str(error))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
