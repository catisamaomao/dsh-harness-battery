#!/usr/bin/env python3
"""Prepare a frozen validation item and probe OS isolation; never launch a model.

This deliberately has no --execute flag. DSH/provider/tool adaptation is pending.
"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

from isolation_preflight import probe
from isolation_support import (IsolationError, copy_snapshot, digest_bytes, ensure_run_dirs,
                               git, inventory, is_within, json_digest, write_evidence)

PROJ = Path(__file__).resolve().parents[1]


def bundle_loader():
    # Always use the repository's reviewed validator, never execute code from an
    # arbitrary --bundle path. The supplied bundle is only read as data.
    path = PROJ / "validation/validate_bundle.py"
    spec = importlib.util.spec_from_file_location("harness_frozen_bundle_validator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_and_validate


def private_directory(path):
    path = Path(path)
    if path.is_symlink():
        raise IsolationError("private directory cannot be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    mode = path.stat()
    if mode.st_uid != os.getuid() or mode.st_mode & 0o077:
        raise IsolationError("private directory must be accessible only by this user (mode 0700)")
    return path.resolve()


def frozen_baseline(fixture, bundle_sha, item_id, cache_root):
    """Both conditions copy the same actual .git/index bytes, not two git inits."""
    fixture = Path(fixture)
    source = inventory(fixture)
    if any(e["path"].split("/")[0] in (".git", "AGENTS.md", ".hidden") for e in source["entries"]):
        raise IsolationError("fixture contains a condition, Git state, or hidden evaluation directory")
    cache_root = private_directory(cache_root)
    key = json_digest({"bundle": bundle_sha, "item": item_id, "fixture": source["sha256"]})
    target = cache_root / key
    if target.is_symlink():
        raise IsolationError("baseline cache cannot be a symlink")
    if not target.exists():
        stage = Path(tempfile.mkdtemp(prefix=".building-", dir=cache_root))
        try:
            workspace = stage / "workspace"
            copy_snapshot(fixture, workspace)
            git(workspace, "-c", "init.defaultBranch=main", "init", "--template=", "-q")
            (workspace / ".git/info").mkdir(exist_ok=True)
            (workspace / ".git/info/exclude").write_text("/AGENTS.md\n", encoding="utf-8")
            git(workspace, "add", "-A")
            git(workspace, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-q", "-m", "Frozen validation baseline")
            if git(workspace, "status", "--porcelain=v1", "--untracked-files=all"):
                raise IsolationError("baseline is not clean")
            record = {"bundle_sha256": bundle_sha, "item_id": item_id, "fixture_sha256": source["sha256"],
                      "baseline_workspace_sha256": inventory(workspace)["sha256"],
                      "git_content_sha256": inventory(workspace / ".git")["sha256"],
                      "git_revision": git(workspace, "rev-parse", "HEAD")}
            (stage / "baseline.json").write_text(json.dumps(record, sort_keys=True), encoding='utf-8')
            try:
                stage.rename(target)
            except OSError:
                if not target.is_dir():
                    raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    record = json.loads((target / "baseline.json").read_text(encoding='utf-8'))
    workspace = target / "workspace"
    if (record.get("fixture_sha256") != source["sha256"] or record.get("bundle_sha256") != bundle_sha
            or record.get("item_id") != item_id
            or inventory(workspace)["sha256"] != record.get("baseline_workspace_sha256")
            or inventory(workspace / ".git")["sha256"] != record.get("git_content_sha256")):
        raise IsolationError("frozen Git baseline changed; refusing to regenerate it silently")
    return workspace, record


def prepare(bundle_dir, item_id, condition_id, *, config_snapshot=None, memory_snapshot=None,
            run_parent=None, baseline_cache=None, expected_bundle_sha256=None, probe_fn=None):
    result = {"schema_version": "validation-prepare.v1", "mode": "prepare_only", "status": "blocked",
              "prepared_at": datetime.now(timezone.utc).isoformat(), "run_id": uuid.uuid4().hex,
              "item_id": item_id, "condition_id": condition_id, "preparation_complete": False,
              "runner_source_sha256": digest_bytes(Path(__file__).read_bytes()),
              "boundary_verified": False, "ready_to_run_model": False, "model_started": False,
              "execution_enabled": False, "actual_injection": {"status": "unavailable", "complete": False,
                  "reason": "prepare_only_no_dsh_session"}, "injected_instruction_sha256": None,
              "model_visible_path_verified": False, "causal_effect_established": False,
              "remaining_gates": ["DSH startup/tool/provider adapter has not been verified under this OS policy.",
                  "Provider networking is denied; no credential delivery adapter is implemented.",
                  "Actual model settings, retrieval, instruction injection and model-visible paths are unobserved."],
              "confounds": ["Each preparation has a different physical path; identical model-visible paths are unverified."]}
    run_dir = None
    try:
        bundle = Path(bundle_dir).resolve()
        validate = bundle_loader()
        manifest = validate(bundle, expected_bundle_sha256)
        matches = [i for i in manifest["items"] if i["id"] == item_id]
        conditions = [c for c in manifest["conditions"] if c["id"] == condition_id]
        if len(matches) != 1 or len(conditions) != 1:
            raise IsolationError("unknown or ambiguous item/condition")
        item, condition = matches[0], conditions[0]
        fixture = bundle / item["fixture_path"]
        prompt = (bundle / item["prompt_path"]).read_bytes()
        rules = (bundle / condition["agents_path"]).read_bytes() if condition["agents_path"] else None
        sources = {}
        for role, source in (("config", config_snapshot), ("memory", memory_snapshot)):
            if source is not None:
                if Path(source).is_symlink():
                    raise IsolationError(f"{role} snapshot root cannot be a symlink")
                source = Path(source).resolve()
                if role == "config" and is_within(source, Path.home() / ".dsh"):
                    raise IsolationError("use an explicitly exported credential-free config snapshot outside real ~/.dsh")
                if role == "config" and (source / "memorix-data").exists():
                    raise IsolationError("config snapshot must not include memorix-data; pass --memory-snapshot separately")
                sources[role] = {"path": source, "before": inventory(source, credentials_forbidden=role == "config")}
        cache = baseline_cache or Path(tempfile.gettempdir()) / f"harness-validation-baselines-{os.getuid()}"
        # Check destinations before mkdir/copy so a bad option never writes into
        # the real DSH home or a source snapshot, even on a later blocked result.
        forbidden = [PROJ, bundle, Path.home() / ".dsh"] + [s["path"] for s in sources.values()]
        for destination in (run_parent, cache):
            if destination and any(is_within(destination, p) for p in forbidden):
                raise IsolationError("preparation destinations must be outside real ~/.dsh, source repo, bundle and snapshots")
        parent = private_directory(run_parent) if run_parent else None
        baseline, base_record = frozen_baseline(fixture, manifest["bundle_sha256"], item_id, cache)
        run_dir = Path(tempfile.mkdtemp(prefix="harness-validation-", dir=parent)).resolve()
        active, control = run_dir / "active", run_dir / "control"
        active.mkdir(mode=0o700)
        control.mkdir(mode=0o700)
        workspace = active / "workspace"
        copy_snapshot(baseline, workspace)
        if rules is not None:
            (workspace / "AGENTS.md").write_bytes(rules)
        if git(workspace, "status", "--porcelain=v1", "--untracked-files=all"):
            raise IsolationError("prepared workspace must remain clean; AGENTS treatment must be ignored")
        dsh_home = active / "dsh-home"
        if "config" in sources:
            copy_snapshot(sources["config"]["path"], dsh_home, credentials_forbidden=True)
        else:
            dsh_home.mkdir()
        config_hash = inventory(dsh_home)["sha256"]
        memory = dsh_home / "memorix-data"
        if "memory" in sources:
            copy_snapshot(sources["memory"]["path"], memory)
        else:
            memory.mkdir()
        ensure_run_dirs(active)
        (control / "prompt.txt").write_bytes(prompt)
        before = {"workspace": inventory(workspace)["sha256"],
                  "dsh_home": inventory(dsh_home)["sha256"], "memory": inventory(memory)["sha256"]}
        protected = [PROJ, bundle, bundle / item["metadata_path"], baseline.parent, control] + [s["path"] for s in sources.values()]
        if (bundle / "graders").exists():
            protected.append(bundle / "graders")
        original_dsh = Path.home() / ".dsh"
        if original_dsh.exists():
            protected.append(original_dsh)
            if (original_dsh / "memorix-data").exists():
                protected.append(original_dsh / "memorix-data")
        isolation = (probe_fn or probe)(active, protected_paths=list(dict.fromkeys(protected)))
        after = {"workspace": inventory(workspace)["sha256"],
                 "dsh_home": inventory(dsh_home)["sha256"], "memory": inventory(memory)["sha256"]}
        if before != after:
            raise IsolationError("OS preflight changed prepared inputs")
        for role, source in sources.items():
            if inventory(source["path"], credentials_forbidden=role == "config") != source["before"]:
                raise IsolationError(f"original {role} snapshot changed during preparation")
        validate(bundle, manifest["bundle_sha256"])
        result.update(bundle_id=manifest["bundle_id"], bundle_sha256=manifest["bundle_sha256"],
                      protocol=item["protocol"], dimensions=item["dimensions"],
                      run_dir=str(run_dir), active_root=str(active), workspace_path=str(workspace),
                      model_visible_workspace_path=str(workspace), prompt_path=str(control / "prompt.txt"),
                      dsh_home=str(dsh_home), memory_path=str(memory),
                      prompt_sha256=digest_bytes(prompt), condition_file_sha256=digest_bytes(rules) if rules is not None else None,
                      fixture_sha256=base_record["fixture_sha256"], baseline_workspace_sha256=base_record["baseline_workspace_sha256"],
                      git_content_sha256=inventory(workspace / ".git")["sha256"], git_revision=base_record["git_revision"],
                      workspace_content_sha256=before["workspace"], config_snapshot_sha256=config_hash,
                      memory_snapshot_sha256=before["memory"], dsh_home_initial_sha256=before["dsh_home"],
                      config_source="explicit_snapshot" if "config" in sources else "synthetic_empty",
                      memory_source="explicit_snapshot" if "memory" in sources else "synthetic_empty",
                      source_snapshots_unchanged=True, inputs_unchanged_by_preflight=True,
                      isolation=isolation, preparation_complete=True,
                      boundary_verified=isolation.get("boundary_verified") is True)
        if result["boundary_verified"]:
            result["status"] = "prepared"
        else:
            result["blocked_reason"] = isolation.get("blocked_reason", "OS boundary has not been verified")
        write_evidence(result, control / "preparation.json")
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        result["blocked_reason"] = str(exc)
        if run_dir is not None:
            shutil.rmtree(run_dir, ignore_errors=True)
        result["failed_preparation_cleaned"] = True
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bundle", type=Path, default=PROJ / "validation")
    ap.add_argument("--item", required=True)
    ap.add_argument("--condition", choices=("baseline", "project_rules"), default="baseline")
    ap.add_argument("--expected-bundle-sha256")
    ap.add_argument("--config-snapshot", type=Path, help="explicitly exported credential-free directory, never real ~/.dsh")
    ap.add_argument("--memory-snapshot", type=Path, help="optional memory directory; copied separately for every preparation")
    ap.add_argument("--run-parent", type=Path, help="private directory outside the source repo; defaults to temporary storage")
    ap.add_argument("--baseline-cache", type=Path, help="private immutable Git baseline cache")
    ap.add_argument("--json", type=Path, help="save structured evidence in addition to run_dir/control/preparation.json")
    args = ap.parse_args(argv)
    result = prepare(args.bundle, args.item, args.condition, config_snapshot=args.config_snapshot,
                     memory_snapshot=args.memory_snapshot, run_parent=args.run_parent,
                     baseline_cache=args.baseline_cache, expected_bundle_sha256=args.expected_bundle_sha256)
    print(write_evidence(result, args.json), end="")
    return 0 if result["status"] == "prepared" else 2


if __name__ == "__main__":
    sys.exit(main())
