#!/usr/bin/env python3
"""Archive stopped assessment workspaces without executing candidate code.

Candidate identity exactly follows the frozen grader's file-byte mapping:
all regular files except components named .git or __pycache__, JSON encoded
with sort_keys=True, separators=(',', ':'), and the default ensure_ascii=True.
The archive receipt is additive; original batch, metadata and hashes are never
rewritten. Source absence or changed bytes cannot be repaired from a fixture.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile

EXCLUDED = {'.git', '__pycache__'}
HEX = re.compile(r'^[a-f0-9]{64}$')
SCHEMA = 'harness-candidate-archive.v1'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def candidate_hash(files):
    return sha(json.dumps(files, sort_keys=True, separators=(',', ':')).encode())


def _json(raw, label):
    def pairs(entries):
        result = {}
        for key, value in entries:
            require(key not in result, f'{label}: duplicate JSON key {key}')
            result[key] = value
        return result
    try:
        result = json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as error:
        raise ValueError(f'{label}: invalid JSON: {error}') from error
    require(isinstance(result, dict), f'{label}: expected JSON object')
    return result


def _path(path):
    """Reject user-created ancestor links; allow standard macOS /tmp,/var aliases."""
    requested = Path(path).absolute()
    require('..' not in requested.parts, 'path traversal is not permitted')
    aliases = {Path('/tmp'): Path('/private/tmp'), Path('/var'): Path('/private/var')}
    for entry in (requested, *requested.parents):
        if entry.is_symlink():
            require(entry != requested and aliases.get(entry) == entry.resolve(),
                    f'symlink path rejected: {entry}')
    return requested.resolve()


def _same_file(before, after):
    return (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)


def _snapshot(workspace, destination=None):
    """Read through directory descriptors, never following candidate links.

    A second source snapshot is still required by archive_candidate after the
    copy. Descriptor checks protect reads from ordinary replacement races.
    """
    root = _path(workspace)
    require(root.is_dir(), f'candidate workspace is missing: {root}')
    target = Path(destination) if destination is not None else None
    if target is not None:
        target.mkdir(mode=0o700)
    files, sizes, modes, directories = {}, {}, {}, []
    flags = os.O_RDONLY | os.O_NOFOLLOW
    root_fd = os.open(root, flags | os.O_DIRECTORY)
    root_stat = os.fstat(root_fd)

    def walk(directory_fd, prefix):
        names = sorted(os.listdir(directory_fd))
        selected = [name for name in names if name not in EXCLUDED]
        for name in names:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            relative = (prefix / name).as_posix()
            require(not stat.S_ISLNK(before.st_mode), f'candidate symlink rejected: {relative}')
            require(stat.S_ISREG(before.st_mode) or stat.S_ISDIR(before.st_mode),
                    f'candidate special file rejected: {relative}')
            if name in EXCLUDED:
                continue
            if stat.S_ISDIR(before.st_mode):
                child_fd = os.open(name, flags | os.O_DIRECTORY, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    require((opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino),
                            f'candidate directory changed while opening: {relative}')
                    directories.append(relative)
                    if target is not None:
                        (target / relative).mkdir(mode=0o700)
                    walk(child_fd, prefix / name)
                    final = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    require(stat.S_ISDIR(final.st_mode) and (final.st_dev, final.st_ino) ==
                            (opened.st_dev, opened.st_ino), f'candidate directory replaced: {relative}')
                finally:
                    os.close(child_fd)
                continue
            descriptor = os.open(name, flags | os.O_NONBLOCK, dir_fd=directory_fd)
            output = None
            try:
                opened = os.fstat(descriptor)
                require(stat.S_ISREG(opened.st_mode) and _same_file(before, opened),
                        f'candidate file changed while opening: {relative}')
                if target is not None:
                    output = (target / relative).open('xb')
                hashed, size = hashlib.sha256(), 0
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    hashed.update(chunk)
                    size += len(chunk)
                    if output is not None:
                        output.write(chunk)
                final = os.fstat(descriptor)
                linked = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                require(_same_file(opened, final) and _same_file(opened, linked)
                        and stat.S_ISREG(linked.st_mode), f'candidate file changed during read: {relative}')
                files[relative], sizes[relative], modes[relative] = hashed.hexdigest(), size, stat.S_IMODE(opened.st_mode) & 0o777
            finally:
                os.close(descriptor)
                if output is not None:
                    output.close()
            if target is not None:
                os.chmod(target / relative, modes[relative])
        require(selected == sorted(name for name in os.listdir(directory_fd) if name not in EXCLUDED),
                f'candidate directory entries changed during read: {prefix}')

    try:
        walk(root_fd, Path())
        final_root = root.stat(follow_symlinks=False)
        require(stat.S_ISDIR(final_root.st_mode) and (final_root.st_dev, final_root.st_ino) ==
                (root_stat.st_dev, root_stat.st_ino), 'candidate root was replaced during read')
    finally:
        os.close(root_fd)
    return {'candidate_sha256': candidate_hash(files), 'files': files, 'sizes': sizes,
            'modes': modes, 'directories': sorted(directories), 'total_bytes': sum(sizes.values())}


def verify_archive(output_dir, expected_candidate_sha256):
    """Verify an existing archive independently of the temporary source path."""
    require(isinstance(expected_candidate_sha256, str) and HEX.fullmatch(expected_candidate_sha256),
            'expected candidate SHA256 is required')
    output = _path(output_dir)
    receipt_path, candidate = output / 'candidate-archive.json', output / 'candidate'
    require(receipt_path.is_file() and not receipt_path.is_symlink(), 'archive receipt is missing or a symlink')
    receipt_raw = receipt_path.read_bytes()
    receipt = _json(receipt_raw, 'archive receipt')
    require(receipt.get('schema_version') == SCHEMA and receipt.get('status') == 'archived',
            'unsupported archive receipt')
    require(receipt.get('candidate_path') == 'candidate', 'unexpected archive candidate path')
    snapshot = _snapshot(candidate)
    require(snapshot['candidate_sha256'] == expected_candidate_sha256 == receipt.get('candidate_sha256')
            == receipt.get('expected_candidate_sha256') == receipt.get('source_sha256_before')
            == receipt.get('source_sha256_after'), 'archive candidate digest mismatch')
    for key in ('files', 'sizes', 'modes', 'directories', 'total_bytes'):
        require(receipt.get(key) == snapshot[key], f'archive receipt inventory mismatch: {key}')
    require(receipt.get('file_count') == len(snapshot['files']), 'archive file count mismatch')
    return {**receipt, 'receipt_path': str(receipt_path), 'receipt_sha256': sha(receipt_raw),
            'archive_path': str(candidate)}


def archive_candidate(workspace, output_dir, expected_candidate_sha256):
    """Create output_dir/candidate and candidate-archive.json, without overwriting.

    The source must still exist and match the original candidate hash. Existing
    archives are verified and returned unchanged. Use verify_archive after the
    temporary source has been removed; it never recreates missing content.
    """
    require(isinstance(expected_candidate_sha256, str) and HEX.fullmatch(expected_candidate_sha256),
            'expected candidate SHA256 is required')
    source, output = _path(workspace), _path(output_dir)
    require(source.is_dir(), f'candidate workspace is missing: {source}')
    require(not output.is_relative_to(source) and not source.is_relative_to(output),
            'candidate source and archive output must be disjoint')
    before = _snapshot(source)
    require(before['candidate_sha256'] == expected_candidate_sha256,
            'candidate source differs from recorded digest; refusing replacement data')
    output.mkdir(parents=True, exist_ok=True)
    require(output.is_dir(), 'archive output must be a directory')
    lock = output / '.candidate-archive.lock'
    descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    staging = None
    try:
        candidate, receipt_path = output / 'candidate', output / 'candidate-archive.json'
        require(not candidate.is_symlink() and not receipt_path.is_symlink(), 'archive destination symlink rejected')
        if candidate.exists() or receipt_path.exists():
            result = verify_archive(output, expected_candidate_sha256)
            require(_snapshot(source) == before, 'candidate source changed during archive verification')
            return {**result, 'operation': 'already_archived'}
        staging = Path(tempfile.mkdtemp(prefix='.candidate-staging-', dir=output))
        copied = _snapshot(source, staging / 'candidate')
        after = _snapshot(source)
        archived = _snapshot(staging / 'candidate')
        require(before == copied == after == archived,
                'candidate source or archive changed during copy; no archive published')
        receipt = {
            'schema_version': SCHEMA, 'status': 'archived', 'created_at': datetime.now(timezone.utc).isoformat(),
            'source_workspace': str(source), 'candidate_path': 'candidate',
            'expected_candidate_sha256': expected_candidate_sha256,
            'source_sha256_before': before['candidate_sha256'], 'source_sha256_after': after['candidate_sha256'],
            'excluded_components': sorted(EXCLUDED), 'file_count': len(before['files']),
            **archived,
            'limitations': ['Files were copied and verified; candidate code was not executed.',
                           'Identity follows the frozen grader file-byte mapping; git and Python bytecode are excluded.',
                           'This additive receipt does not change the original batch, metadata, or grading result.'],
        }
        receipt_bytes = (json.dumps(receipt, ensure_ascii=False, indent=2) + '\n').encode()
        staged_receipt = staging / 'candidate-archive.json'
        staged_receipt.write_bytes(receipt_bytes)
        # Reserve the destination exclusively. POSIX rename can overwrite an
        # empty directory which appears after an existence check, so publish
        # files with exclusive links instead. The receipt is the commit marker.
        candidate.mkdir(mode=0o700)
        for relative in archived['directories']:
            (candidate / relative).mkdir(mode=0o700)
        for relative in archived['files']:
            os.link(staging / 'candidate' / relative, candidate / relative, follow_symlinks=False)
        require(_snapshot(candidate) == archived, 'published archive differs from verified staging')
        # Hard-link publication is exclusive: an unexpected receipt is never
        # replaced. Staging and archive are on the same filesystem.
        os.link(staged_receipt, receipt_path)
        result = verify_archive(output, expected_candidate_sha256)
        return {**result, 'operation': 'archived'}
    finally:
        # A crash or publication failure can leave candidate/ without a receipt.
        # Preserve those bytes for inspection; the next call refuses to overwrite.
        if staging is not None:
            shutil.rmtree(staging)
        lock.unlink()


def _relative_file(root, relative):
    require(isinstance(relative, str) and relative and not Path(relative).is_absolute()
            and all(part not in ('', '.', '..') for part in relative.split('/')) and '\\' not in relative,
            'invalid relative metadata path')
    path = _path(root / relative)
    require(path.is_relative_to(root) and path.is_file(), 'metadata path is missing or escapes batch')
    return path


def archive_batch(batch_path, expected_batch_sha256=None):
    """Archive each terminal batch run with a bound candidate hash; preserve all originals."""
    path = _path(batch_path)
    require(path.is_file(), 'batch manifest is missing')
    original = path.read_bytes()
    if expected_batch_sha256 is not None:
        require(sha(original) == expected_batch_sha256, 'batch differs from expected SHA256')
    batch = _json(original, 'batch manifest')
    require(batch.get('schema_version') == 'harness-assessment-batch.v1', 'unsupported batch schema')
    require(batch.get('status') in ('completed', 'stopped_on_failure'), 'only terminal batches may be archived')
    require(isinstance(batch.get('runs'), list), 'batch runs must be a list')
    seen, results = set(), []
    for record in batch['runs']:
        require(isinstance(record, dict) and isinstance(record.get('run_id'), str)
                and record['run_id'] and record['run_id'] not in seen, 'invalid or duplicate run record')
        rid = record['run_id']
        seen.add(rid)
        try:
            require(isinstance(record.get('paths'), dict) and isinstance(record.get('sha256'), dict),
                    f'{rid}: malformed metadata provenance')
            meta_path = _relative_file(path.parent, record['paths'].get('meta'))
            meta_raw = meta_path.read_bytes()
            require(sha(meta_raw) == record['sha256'].get('meta'), f'{rid}: metadata hash mismatch')
            meta = _json(meta_raw, f'{rid} metadata')
            for field in ('run_id', 'item_id', 'condition_id'):
                require(meta.get(field) == record.get(field), f'{rid}: metadata identity mismatch: {field}')
            expected = meta.get('candidate_sha256')
            if expected is None:
                require(record.get('candidate_sha256') is None, f'{rid}: candidate hash missing from bound metadata')
                require(record.get('status') != 'completed', f'{rid}: completed run has no candidate digest')
                results.append({'run_id': rid, 'status': 'skipped', 'reason': 'unfinished_run_without_candidate_hash'})
                continue
            require(record.get('candidate_sha256', expected) == expected, f'{rid}: manifest/metadata candidate mismatch')
            workspace = meta.get('workspace_path')
            require(isinstance(workspace, str) and Path(workspace).is_absolute(), f'{rid}: missing absolute workspace path')
            result = archive_candidate(workspace, meta_path.parent, expected)
            require(meta_path.read_bytes() == meta_raw, f'{rid}: metadata changed during archiving')
            results.append({'run_id': rid, 'status': result['operation'],
                            'candidate_sha256': result['candidate_sha256'],
                            'archive_path': result['archive_path'], 'receipt_path': result['receipt_path'],
                            'receipt_sha256': result['receipt_sha256'], 'meta_sha256': sha(meta_raw)})
        except (ValueError, OSError) as error:
            results.append({'run_id': rid, 'status': 'error', 'reason': str(error)})
    require(path.read_bytes() == original, 'batch manifest changed during archiving')
    return {'schema_version': 'harness-assessment-archives.v1', 'batch': str(path),
            'batch_sha256': sha(original), 'status': 'failed' if any(r['status'] == 'error' for r in results) else 'completed',
            'runs': results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--batch', type=Path)
    mode.add_argument('--workspace', type=Path)
    parser.add_argument('--out', type=Path, help='single-run output directory, containing candidate/')
    parser.add_argument('--expected-candidate-sha256')
    parser.add_argument('--expected-batch-sha256')
    args = parser.parse_args(argv)
    try:
        if args.batch:
            result = archive_batch(args.batch, args.expected_batch_sha256)
        else:
            require(args.out is not None, '--workspace requires --out')
            result = archive_candidate(args.workspace, args.out, args.expected_candidate_sha256)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get('status') == 'failed' else 0
    except (ValueError, OSError) as error:
        print('candidate archive: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
