#!/usr/bin/env python3
"""Compare prepared condition copies; file presence is not proof of injection."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

BUNDLE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUNDLE))
from validate_bundle import load_and_validate


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _snapshot(workspace):
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise ValueError(f"missing workspace: {root}")
    code, git = {}, {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError(f"symlink in condition workspace: {relative}")
        if path.is_file() and relative != "AGENTS.md":
            target = git if relative.startswith(".git/") else code
            target[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return root, code, git


def _git_state(workspace):
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_SYSTEM": os.devnull}
    command = ["git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
               "-C", str(workspace)]
    inside = subprocess.run(command + ["rev-parse", "--is-inside-work-tree"], env=env,
                            capture_output=True, text=True, timeout=10)
    if inside.returncode or inside.stdout.strip() != "true":
        raise ValueError("condition workspace is not a valid git checkout")
    status = subprocess.run(command + ["status", "--porcelain=v1", "--untracked-files=all"],
                            env=env, capture_output=True, text=True, timeout=10)
    ignored = subprocess.run(command + ["check-ignore", "AGENTS.md"], env=env,
                             capture_output=True, text=True, timeout=10)
    if status.returncode:
        raise ValueError("cannot read git status")
    return status.stdout, ignored.returncode == 0


def validate_condition_pair(baseline_workspace, rules_workspace,
                            baseline_evidence=None, rules_evidence=None):
    manifest = load_and_validate(BUNDLE)
    baseline_evidence = baseline_evidence or {}
    rules_evidence = rules_evidence or {}
    baseline, code_a, git_a = _snapshot(baseline_workspace)
    rules, code_b, git_b = _snapshot(rules_workspace)
    prescribed = BUNDLE / next(c["agents_path"] for c in manifest["conditions"]
                              if c["id"] == "project_rules")
    rule_sha = hashlib.sha256(prescribed.read_bytes()).hexdigest()
    errors, confounds, unknown = [], [], []
    if not git_a or not git_b:
        errors.append("both conditions require a real .git snapshot; removal is not an allowed treatment")
    if code_a != code_b:
        errors.append("non-AGENTS workspace contents differ")
    if git_a != git_b:
        errors.append(".git contents differ")
    git_status = [None, None]
    if git_a and git_b:
        try:
            status_a, ignored_a = _git_state(baseline)
            status_b, ignored_b = _git_state(rules)
            git_status = [status_a, status_b]
            if status_a != status_b:
                errors.append("git status differs")
            if not (ignored_a and ignored_b):
                errors.append("root AGENTS.md must be ignored in both conditions")
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            errors.append(str(exc))
    if (baseline / "AGENTS.md").exists():
        errors.append("baseline unexpectedly contains root AGENTS.md")
    target = rules / "AGENTS.md"
    if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != rule_sha:
        errors.append("rules condition does not match prescribed AGENTS.md")

    visible_a = baseline_evidence.get("model_visible_workspace_path")
    visible_b = rules_evidence.get("model_visible_workspace_path")
    verified_visible = (baseline_evidence.get("model_visible_path_verified") is True
                        and rules_evidence.get("model_visible_path_verified") is True
                        and isinstance(visible_a, str) and isinstance(visible_b, str))
    if verified_visible:
        if visible_a != visible_b:
            confounds.append("model-visible workspace paths differ")
    elif baseline != rules:
        confounds.append("physical workspace paths differ and identical model-visible paths are unverified")
    else:
        unknown.append("model-visible workspace path not verified")

    # These fingerprints describe exposure beyond files. Missing is not equal.
    fields = ("model_settings_sha256", "tools_sha256", "memory_snapshot_sha256",
              "retrieval_settings_sha256", "other_instructions_sha256")
    for field in fields:
        a, b = baseline_evidence.get(field), rules_evidence.get(field)
        if not isinstance(a, str) or not a or not isinstance(b, str) or not b:
            unknown.append(field)
        elif a != b:
            confounds.append(field + " differs")
    observations = [baseline_evidence.get("actual_injection"), rules_evidence.get("actual_injection")]
    complete = all(isinstance(o, dict) and o.get("status") == "parsed"
                   and o.get("complete") is True and o.get("source") == "session"
                   and "agents_content_sha256" in o for o in observations)
    manipulation = "unknown"
    if complete:
        if observations[0]["agents_content_sha256"] is None and observations[1]["agents_content_sha256"] == rule_sha:
            manipulation = "observed_as_planned"
        else:
            manipulation = "failed"
            errors.append("actual AGENTS exposure disagrees with planned conditions")
    else:
        unknown.append("actual session injection unavailable or incomplete")
    status = "invalid" if errors else "confounded" if confounds else "incomplete" if unknown else "inputs_verified"
    return {"schema_version": 1, "bundle_sha256": manifest["bundle_sha256"],
            "status": status, "manipulation_status": manipulation,
            "code_equal": code_a == code_b, "git_equal": git_a == git_b and bool(git_a),
            "baseline_code_sha256": _digest(code_a), "rules_code_sha256": _digest(code_b),
            "baseline_git_sha256": _digest(git_a), "rules_git_sha256": _digest(git_b),
            "git_status": git_status,
            "expected_rules_sha256": rule_sha,
            "physical_workspace_paths": [str(baseline), str(rules)],
            "errors": errors, "confounds": confounds, "unknown": unknown,
            "causal_effect_established": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-workspace", required=True)
    parser.add_argument("--rules-workspace", required=True)
    parser.add_argument("--baseline-evidence", type=Path)
    parser.add_argument("--rules-evidence", type=Path)
    args = parser.parse_args(argv)
    try:
        report = validate_condition_pair(args.baseline_workspace, args.rules_workspace,
            json.loads(args.baseline_evidence.read_text()) if args.baseline_evidence else None,
            json.loads(args.rules_evidence.read_text()) if args.rules_evidence else None)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "inputs_verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
