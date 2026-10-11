#!/usr/bin/env python3
"""Grade stopped model outputs in an inherited OS sandbox, without credentials/network."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from isolation_support import clean_env, digest_bytes, ensure_run_dirs, inventory, sandbox_policy
from validation_run import bundle_loader
from process_control import run_child


def grade_item(bundle_dir, item_id, workspace, expected_bundle_sha256):
    bundle, workspace = Path(bundle_dir).resolve(), Path(workspace).resolve()
    bundle_loader()(bundle, expected_bundle_sha256)
    # Reject symlinks/special files before copying or executing candidate code.
    inventory(workspace)
    before = inventory(workspace)['sha256']
    with tempfile.TemporaryDirectory(prefix='harness-grade-isolated-') as temp:
        root = Path(temp).resolve()
        active = ensure_run_dirs(root/'active')
        candidate = active/'candidate'
        shutil.copytree(workspace, candidate, ignore=shutil.ignore_patterns('.git','__pycache__'))
        isolated_bundle = root/'bundle'
        shutil.copytree(bundle, isolated_bundle, ignore=shutil.ignore_patterns('__pycache__'))
        # The copied grader and criteria are read-only to candidate processes.
        protected = [Path(__file__).resolve().parents[1], Path.home()/'.dsh', workspace]
        policy = sandbox_policy(active, protected)
        policy += '(allow file-read* (subpath '+json.dumps(str(isolated_bundle))+'))\n'
        policy_path = root/'grade.sb'; policy_path.write_text(policy, encoding='utf-8')
        command = ['/usr/bin/sandbox-exec','-f',str(policy_path),str(Path(sys.executable).resolve()),'-B',
                   str(isolated_bundle/'graders/grade.py'),'--item',item_id,'--workspace',str(candidate),
                   '--expected-bundle-sha256',expected_bundle_sha256]
        try:
            proc = run_child(command,cwd=active,env=clean_env(active),capture_output=True,text=True,timeout=60, encoding='utf-8')
        except subprocess.TimeoutExpired as error:
            raise ValueError('independent grader timed out; no artifact result established') from error
        if proc.returncode not in (0,1):
            raise ValueError('sandboxed grader failed: '+proc.stderr[-1000:])
        try:
            result=json.loads(proc.stdout)
        except ValueError as error:
            raise ValueError('grader did not produce structured evidence') from error
        if result.get('bundle_sha256') != expected_bundle_sha256 or result.get('item_id') != item_id:
            raise ValueError('grader returned wrong bundle/item identity')
        if result.get('artifact_status') not in ('pass','fail','not_applicable'):
            raise ValueError('invalid artifact status')
        result['execution_boundary'] = {
            'backend':'macos-seatbelt', 'network':'denied', 'original_candidate':'unmodified_read_denied',
            'grader_source':'verified_frozen_readonly_copy', 'policy_sha256':digest_bytes(policy.encode()),
            'worker_inherits_policy':True, 'model_running':False}
    if inventory(workspace)['sha256'] != before:
        raise ValueError('original candidate changed during isolated grading')
    bundle_loader()(bundle, expected_bundle_sha256)
    return result
