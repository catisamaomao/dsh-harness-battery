#!/usr/bin/env python3
"""Run local OS boundary probes only. A pass never establishes DSH/model readiness."""
import argparse
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

from isolation_support import (IsolationError, clean_env, digest_bytes, ensure_run_dirs,
                               sandbox_policy, write_evidence)


PROBE = r'''
import errno,json,os,socket,subprocess,sys
spec=json.loads(sys.argv[1])
child_mode=len(sys.argv)>2
checks=[]
denied={errno.EPERM,errno.EACCES}
def test(name,operation,expect_denied):
    try:
        operation()
        checks.append({'name':name,'expected':'denied' if expect_denied else 'allowed',
                       'observed':'allowed','passed':not expect_denied})
    except OSError as e:
        checks.append({'name':name,'expected':'denied' if expect_denied else 'allowed',
                       'observed':'denied' if e.errno in denied else 'error',
                       'errno':e.errno,'passed':expect_denied and e.errno in denied})
def open_only(path):
    fd=os.open(path,os.O_RDONLY)
    os.close(fd)
def read_own():
    with open(spec['own_read']) as f:
        if f.read()!=spec['sentinel']: raise OSError(errno.EIO,'sentinel mismatch')
def write_own():
    with open(spec['own_write']+('.child' if child_mode else '.parent'),'w') as f: f.write('local-probe')
def write_external():
    fd=os.open(spec['external_write'],os.O_WRONLY|os.O_APPEND)
    os.close(fd)
def network_loopback():
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        s.connect(('127.0.0.1',9))
test('copy_read',read_own,False)
test('copy_write',write_own,False)
for item in spec['protected']:
    test('deny_read:'+item['role'],lambda p=item['path']:open_only(p),True)
test('deny_symlink_escape',lambda:open_only(spec['escape_link']),True)
test('deny_external_write',write_external,True)
test('deny_loopback_network',network_loopback,True)
result={'checks':checks}
if not child_mode:
    proc=subprocess.run([sys.executable,'-B','-c',spec['program'],json.dumps(spec),'child'],
                        capture_output=True,text=True,timeout=15)
    result['child_returncode']=proc.returncode
    try: result['child']=json.loads(proc.stdout)
    except (ValueError,TypeError): result['child_error']=proc.stderr[:500]
print(json.dumps(result))
'''


def probe(run_root=None, protected_paths=None, *, backend="sandbox-exec", timeout=30):
    """Prove observable denials, not merely profile creation or a missing file."""
    result = {"schema_version": "isolation-preflight.v1", "status": "blocked",
              "backend": backend, "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
              "observed_at": datetime.now(timezone.utc).isoformat(),
              "boundary_verified": False, "ready_to_run_model": False, "model_started": False,
              "evidence_scope": "one_controlled_process_tree_at_probe_time",
              "probe_files_retained": False, "supplied_run_root_retained": run_root is not None,
              "probe_program_sha256": digest_bytes(PROBE.encode()),
              "coordinator_source_sha256": digest_bytes(Path(__file__).read_bytes()),
              "snapshot_helper_sha256": digest_bytes(Path(__file__).with_name("isolation_support.py").read_bytes()),
              "python_executable": str(Path(sys.executable).resolve()), "python_version": sys.version,
              "network_used": False, "checks": [], "limitations": [
                  "Only a controlled local Python process and its child were probed; DSH startup and its tools remain unverified.",
                  "No model call, provider credential, or real user memory content is used.",
                  "A loopback connection attempt is expected to be denied by the OS; no external host is contacted."]}
    temp = tempfile.TemporaryDirectory(prefix="harness-isolation-probe-")
    audit_root = Path(temp.name).resolve()
    active = Path(run_root).resolve() if run_root else audit_root / "active"
    active.mkdir(parents=True, exist_ok=True, mode=0o700)
    ensure_run_dirs(active)
    probe_dir = Path(tempfile.mkdtemp(prefix=".boundary-probe-", dir=active))
    try:
        if backend != "sandbox-exec" or platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").is_file():
            raise IsolationError("supported OS sandbox backend unavailable; no unsandboxed fallback")
        protected = []
        for role in ("original_repo_sentinel", "original_memory_sentinel", "hidden_grader_sentinel"):
            folder = audit_root / role
            folder.mkdir()
            marker = folder / "sentinel.txt"
            marker.write_text("controlled non-secret sentinel\n", encoding='utf-8')
            protected.append({"role": role, "path": str(marker), "root": str(folder),
                              "kind": "controlled_sentinel"})
        for i, path in enumerate(protected_paths or []):
            path = Path(path).resolve()
            if not path.exists():
                raise IsolationError(f"protected source missing; denial cannot be verified: {path}")
            # Open/close outside the sandbox verifies that ENOENT or pre-existing permissions
            # cannot masquerade as an OS-policy denial. Never read original bytes.
            fd = os.open(path, os.O_RDONLY)
            os.close(fd)
            protected.append({"role": f"original_path_{i}", "path": str(path), "root": str(path),
                              "kind": "original_open_without_content_read"})
        sentinel = "copy-readable-local-probe"
        own_read = probe_dir / "allowed.txt"
        own_read.write_text(sentinel, encoding='utf-8')
        escape = probe_dir / "escape-link"
        escape.symlink_to(protected[0]["path"])
        policy = sandbox_policy(active, [p["root"] for p in protected])
        policy_path = audit_root / "policy.sb"
        policy_path.write_text(policy, encoding='utf-8')
        result.update(policy_sha256=digest_bytes(policy.encode()), policy=policy,
                      run_root=str(active), protected_paths=protected,
                      environment_keys=sorted(clean_env(active)))
        spec = {"own_read": str(own_read), "own_write": str(probe_dir / "write.txt"),
                "sentinel": sentinel, "protected": protected, "escape_link": str(escape),
                "external_write": protected[0]["path"], "program": PROBE}
        proc = subprocess.run(["/usr/bin/sandbox-exec", "-f", str(policy_path),
                               str(Path(sys.executable).resolve()), "-B", "-c", PROBE, json.dumps(spec)],
                              cwd=active, env=clean_env(active), capture_output=True,
                              text=True, timeout=timeout, encoding='utf-8')
        result.update(returncode=proc.returncode, stderr=proc.stderr[:2000])
        if proc.returncode != 0:
            raise IsolationError(f"sandbox probe did not execute successfully (exit {proc.returncode})")
        try:
            observed = json.loads(proc.stdout)
        except ValueError as exc:
            raise IsolationError("sandbox probe did not return structured evidence") from exc
        if (not isinstance(observed, dict) or not isinstance(observed.get("checks"), list)
                or not isinstance(observed.get("child", {}), dict)
                or not isinstance(observed.get("child", {}).get("checks", []), list)):
            raise IsolationError("invalid structured probe evidence")
        direct = observed.get("checks", [])
        child = observed.get("child", {}).get("checks", [])
        if not all(isinstance(c, dict) for c in direct + child):
            raise IsolationError("invalid structured probe checks")
        expected_names = {"copy_read", "copy_write", "deny_symlink_escape", "deny_external_write",
                          "deny_loopback_network"} | {"deny_read:" + p["role"] for p in protected}
        expected_count = len(expected_names)
        result["checks"] = [dict(c, process="parent") for c in direct] + [dict(c, process="child") for c in child]
        result["child_returncode"] = observed.get("child_returncode")
        if (len(direct) != expected_count or len(child) != expected_count
                or {c.get("name") for c in direct} != expected_names
                or {c.get("name") for c in child} != expected_names
                or observed.get("child_returncode") != 0):
            result["child_error"] = observed.get("child_error")
            raise IsolationError("incomplete probe or subprocess-inheritance evidence")
        if not all(c.get("passed") is True and c.get("expected") == c.get("observed")
                   and c.get("expected") == ("denied" if c["name"].startswith("deny_") else "allowed")
                   and (c.get("expected") != "denied" or c.get("errno") in (errno.EPERM, errno.EACCES))
                   for c in result["checks"]):
            raise IsolationError("one or more required OS boundary checks failed")
        result.update(status="passed", boundary_verified=True)
    except (IsolationError, OSError, subprocess.TimeoutExpired) as exc:
        result["blocked_reason"] = str(exc)
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
        temp.cleanup()
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", type=Path, help="save structured evidence; stdout always contains JSON")
    ap.add_argument("--protect-root", action="append", default=[], type=Path,
                    help="existing original file/directory to prove unreadable, without reading its contents")
    args = ap.parse_args(argv)
    protected = [Path(__file__).resolve().parents[1]] + args.protect_root
    original_home = Path.home() / ".dsh"
    if original_home.exists():
        protected.append(original_home)
        if (original_home / "memorix-data").exists():
            protected.append(original_home / "memorix-data")
    result = probe(protected_paths=protected)
    print(write_evidence(result, args.json), end="")
    return 0 if result["boundary_verified"] else 2


if __name__ == "__main__":
    sys.exit(main())
