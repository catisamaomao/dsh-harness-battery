#!/usr/bin/env python3
"""Verify the frozen, offline validation bundle without invoking a model."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

LOCK_NAME = "bundle.lock.json"


class BundleValidationError(ValueError):
    pass


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _inventory(root):
    files = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts:
            continue
        if path.is_symlink():
            raise BundleValidationError(f"symlink in bundle: {relative}")
        if path.is_file() and relative.as_posix() != LOCK_NAME:
            files[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def _safe_path(root, value, *, directory=False):
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise BundleValidationError(f"invalid relative path: {value!r}")
    if any(part in (".", "..") for part in value.split("/")):
        raise BundleValidationError(f"unsafe relative path: {value}")
    path = root / value
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise BundleValidationError(f"path escapes bundle: {value}") from exc
    if not (path.is_dir() if directory else path.is_file()):
        raise BundleValidationError(f"missing {'directory' if directory else 'file'}: {value}")
    return path


def _manifest(root):
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleValidationError(f"cannot read manifest: {exc}") from exc
    if manifest.get("schema_version") != 1 or manifest.get("status") != "unvalidated":
        raise BundleValidationError("unsupported manifest version/status")
    if not isinstance(manifest.get("bundle_id"), str) or not manifest["bundle_id"]:
        raise BundleValidationError("missing bundle_id")
    conditions = manifest.get("conditions")
    if not isinstance(conditions, list) or {c.get("id") for c in conditions} != {"baseline", "project_rules"} or len(conditions) != 2:
        raise BundleValidationError("expected baseline and project_rules conditions")
    for condition in conditions:
        if condition["id"] == "baseline":
            if condition.get("agents_path") is not None:
                raise BundleValidationError("baseline cannot prescribe an AGENTS file")
        else:
            _safe_path(root, condition.get("agents_path"))
    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        raise BundleValidationError("items must be a nonempty list")
    seen = set()
    for item in items:
        item_id = item.get("id")
        if not isinstance(item_id, str) or item_id in seen:
            raise BundleValidationError(f"invalid or duplicate item id: {item_id}")
        seen.add(item_id)
        if item.get("protocol") not in ("open_response", "open_question_with_tools", "task"):
            raise BundleValidationError(f"unsupported protocol: {item_id}")
        if not item.get("dimensions") or not set(item["dimensions"]) <= {f"D{i}" for i in range(1, 8)}:
            raise BundleValidationError(f"invalid dimensions: {item_id}")
        _safe_path(root, item.get("prompt_path"))
        fixture = _safe_path(root, item.get("fixture_path"), directory=True)
        if (fixture / ".git").exists() or (fixture / "AGENTS.md").exists():
            raise BundleValidationError(f"fixture must not prescribe a git/AGENTS condition: {item_id}")
        metadata_path = _safe_path(root, item.get("metadata_path"))
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BundleValidationError(f"invalid metadata: {item_id}") from exc
        for key in ("id", "dimensions", "protocol", "prompt_path", "fixture_path"):
            if metadata.get(key) != item.get(key):
                raise BundleValidationError(f"metadata disagrees on {key}: {item_id}")
        if metadata.get("status") != "unvalidated":
            raise BundleValidationError(f"item must remain unvalidated: {item_id}")
        if not metadata.get("observable_criteria") or not metadata.get("limitations"):
            raise BundleValidationError(f"missing evidence criteria/limitations: {item_id}")
        if not isinstance(metadata.get("acceptance"), dict):
            raise BundleValidationError(f"missing acceptance rules: {item_id}")
    return manifest


def load_and_validate(bundle_dir, expected_bundle_sha256=None):
    root = Path(bundle_dir).resolve()
    try:
        lock = json.loads((root / LOCK_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleValidationError(f"missing or invalid lock: {exc}") from exc
    if lock.get("schema_version") != 1 or lock.get("algorithm") != "sha256":
        raise BundleValidationError("unsupported lock format")
    actual = _inventory(root)
    expected = lock.get("files")
    if not isinstance(expected, dict) or actual != expected:
        expected = expected if isinstance(expected, dict) else {}
        changed = sorted(k for k in actual.keys() | expected.keys()
                         if actual.get(k) != expected.get(k))
        raise BundleValidationError("bundle modified: " + ", ".join(changed[:12]))
    digest = hashlib.sha256(_canonical(actual)).hexdigest()
    if lock.get("bundle_sha256") != digest:
        raise BundleValidationError("invalid bundle digest")
    if expected_bundle_sha256 is not None and digest != expected_bundle_sha256:
        raise BundleValidationError("bundle differs from externally pinned digest")
    manifest = _manifest(root)
    if lock.get("bundle_id") != manifest["bundle_id"]:
        raise BundleValidationError("lock and manifest bundle ids differ")
    return {**manifest, "bundle_sha256": digest}


def freeze(bundle_dir):
    root = Path(bundle_dir).resolve()
    if (root / LOCK_NAME).exists():
        raise BundleValidationError("lock already exists; changes require a new version and reviewed lock")
    manifest = _manifest(root)
    files = _inventory(root)
    lock = {"schema_version": 1, "algorithm": "sha256", "bundle_id": manifest["bundle_id"],
            "bundle_sha256": hashlib.sha256(_canonical(files)).hexdigest(), "files": files}
    (root / LOCK_NAME).write_text(json.dumps(lock, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                                 encoding="utf-8")
    return lock


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parent)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="default: verify lock and metadata")
    action.add_argument("--freeze", action="store_true", help="create first lock; never silently update it")
    parser.add_argument("--expected-bundle-sha256", help="digest pinned outside the bundle")
    args = parser.parse_args(argv)
    try:
        result = freeze(args.bundle) if args.freeze else load_and_validate(args.bundle, args.expected_bundle_sha256)
    except (BundleValidationError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "bundle_id": result["bundle_id"],
                      "bundle_sha256": result["bundle_sha256"], "status": "unvalidated"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
