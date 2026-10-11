#!/usr/bin/env python3
"""Audit source or an npm tarball without exposing matched private values.

This is a bounded release check, not a proof that arbitrary personal data is absent.
It never extracts archives, executes packaged code, or reads the user's DSH home.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tarfile

BUNDLE_PINS = {
    'runtime/validation': '4cf2c3d7ea272ddec2e4423be3cb4dd3894c61b9f8f70dab09ec25e34b782f61',
    'runtime/batteries/extended-v2': '7dcf6d3d842020820d69912357feeab1e3fdbe82fcf2f1cf60f8f608f548bfdc',
}
SKIP_SOURCE_DIRS = {'.git', 'node_modules', '__pycache__', '.pytest_cache'}
PRIVATE_DIRS = {'.dsh', '.codex', '.aws', 'sessions', 'results', 'reports', 'memory-snapshot', 'memory-snapshots'}
PRIVATE_NAMES = {'.DS_Store', '.npmrc', '.pypirc', 'credentials.json', 'auth.json', 'tokens.json', 'profile.report.json'}
PRIVATE_SUFFIXES = {'.sqlite', '.sqlite3', '.db', '.log', '.jsonl', '.pem', '.key'}
PATTERNS = {
    'user_absolute_path': re.compile(r'/(?:Users|home)/(?!example(?:/|\b)|test(?:/|\b)|user(?:/|\b))[A-Za-z0-9._-]+/'),
    'credential_token': re.compile(r'(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{25,}|AKIA[0-9A-Z]{16})'),
    'private_key': re.compile(r'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----'),
}
MAX_FILE_BYTES = 64 * 1024 * 1024


def _finding(path, category, line=None):
    value = {'path': path, 'category': category}
    if line is not None:
        value['line'] = line
    return value


def source_entries(root):
    entries, findings = {}, []
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if set(relative.parts) & SKIP_SOURCE_DIRS:
            continue
        name = relative.as_posix()
        if path.is_symlink():
            findings.append(_finding(name, 'symlink'))
            continue
        if path.is_file():
            if path.stat().st_size > MAX_FILE_BYTES:
                findings.append(_finding(name, 'oversize_uninspected_file'))
                continue
            entries[name] = path.read_bytes()
    return entries, findings


def archive_entries(path):
    entries, findings = {}, []
    with tarfile.open(path, 'r:*') as archive:
        for member in archive.getmembers():
            name = member.name
            if name.startswith('package/'):
                name = name[len('package/'):]
            parts = PurePosixPath(name).parts
            if PurePosixPath(name).is_absolute() or '..' in parts:
                findings.append(_finding(name, 'unsafe_archive_path'))
                continue
            if member.isdir():
                continue
            if not member.isfile():
                findings.append(_finding(name, 'archive_link_or_special_file'))
                continue
            if name in entries:
                findings.append(_finding(name, 'duplicate_archive_member'))
                continue
            if member.size > MAX_FILE_BYTES:
                findings.append(_finding(name, 'oversize_uninspected_file'))
                continue
            stream = archive.extractfile(member)
            if stream is None:
                findings.append(_finding(name, 'unreadable_archive_member'))
            else:
                entries[name] = stream.read()
    return entries, findings


def audit_entries(entries, *, check_bundles=True):
    findings, bundle_checks = [], []
    text_count = 0
    for name, data in sorted(entries.items()):
        p = PurePosixPath(name)
        if (set(p.parts) & PRIVATE_DIRS or p.name in PRIVATE_NAMES
                or p.suffix.lower() in PRIVATE_SUFFIXES
                or (p.name == '.env' or p.name.startswith('.env.') and not p.name.endswith(('.example', '.sample')))):
            findings.append(_finding(name, 'private_runtime_or_credential_file'))
        if '.git' in p.parts or '__pycache__' in p.parts or p.suffix == '.pyc':
            findings.append(_finding(name, 'repository_or_cache_artifact'))
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            continue
        if '\x00' in text:
            continue
        text_count += 1
        for category, pattern in PATTERNS.items():
            for line_number, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    findings.append(_finding(name, category, line_number))
    if check_bundles:
        for prefix, pinned in BUNDLE_PINS.items():
            lock_name = prefix + '/bundle.lock.json'
            try:
                lock = json.loads(entries[lock_name])
                actual = {name[len(prefix) + 1:]: hashlib.sha256(data).hexdigest()
                          for name, data in entries.items()
                          if name.startswith(prefix + '/') and name != lock_name
                          and '__pycache__' not in PurePosixPath(name).parts}
                canonical = json.dumps(actual, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
                digest = hashlib.sha256(canonical).hexdigest()
                ok = (lock.get('files') == actual and lock.get('bundle_sha256') == digest == pinned)
            except (KeyError, TypeError, ValueError):
                ok, digest = False, None
            bundle_checks.append({'path': prefix, 'ok': ok, 'expected_sha256': pinned, 'actual_sha256': digest})
            if not ok:
                findings.append(_finding(prefix, 'frozen_bundle_integrity'))
    return {'ok': not findings, 'file_count': len(entries), 'text_files_scanned': text_count,
            'findings': findings, 'frozen_bundles': bundle_checks,
            'limitations': ['Pattern scanning does not prove absence of all personal data.',
                            'Historical identifiers inside frozen evaluator metadata are provenance references, not bundled raw runs.',
                            'No network calls, model calls, source execution, or archive extraction are performed.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    source.add_argument('--tarball', type=Path)
    args = parser.parse_args(argv)
    try:
        entries, initial = archive_entries(args.tarball) if args.tarball else source_entries(args.root)
        result = audit_entries(entries)
        result['findings'] = initial + result['findings']
        result['ok'] = not result['findings']
    except (OSError, tarfile.TarError) as exc:
        result = {'ok': False, 'error_type': type(exc).__name__, 'findings': []}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
