"""Local, credential-free preparation helpers. No model or network calls."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys


class IsolationError(ValueError):
    pass


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def json_digest(value):
    return digest_bytes(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False).encode())


def is_within(path, root):
    return Path(path).resolve().is_relative_to(Path(root).resolve())


def inventory(root, *, credentials_forbidden=False):
    """Hash regular files only; reject links/devices before opening their bytes."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise IsolationError(f"snapshot must be a real directory: {root}")
    entries = []
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        for name in sorted(dirs + files):
            path = Path(parent) / name
            mode = path.lstat().st_mode
            rel = path.relative_to(root).as_posix()
            if stat.S_ISLNK(mode):
                raise IsolationError(f"snapshot symlink rejected: {rel}")
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise IsolationError(f"snapshot special file rejected: {rel}")
            lower = name.lower()
            if credentials_forbidden and (any(s in lower for s in ("credential", "secret", "token"))
                    or lower in (".env", "id_rsa", "id_ed25519")
                    or lower.endswith((".pem", ".key"))):
                raise IsolationError(f"credential-like file must not enter config snapshot: {rel}")
            entry = {"path": rel, "kind": "directory" if path.is_dir() else "file"}
            if path.is_file():
                entry.update(sha256=digest_bytes(path.read_bytes()), executable=bool(mode & 0o111))
            entries.append(entry)
    return {"sha256": json_digest(entries), "entries": entries}


def copy_snapshot(source, target, *, credentials_forbidden=False):
    source, target = Path(source), Path(target)
    before = inventory(source, credentials_forbidden=credentials_forbidden)
    if target.exists():
        raise IsolationError(f"copy target already exists: {target}")
    if is_within(target, source) or is_within(source, target):
        raise IsolationError("snapshot source and destination must be disjoint")
    shutil.copytree(source, target, symlinks=True)
    copied = inventory(target, credentials_forbidden=credentials_forbidden)
    after = inventory(source, credentials_forbidden=credentials_forbidden)
    if before != copied or before != after:
        raise IsolationError("source changed during snapshot copy")
    return before


def clean_env(run_root):
    root = Path(run_root).resolve()
    return {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
            "HOME": str(root / "home"), "DSH_HOME": str(root / "dsh-home"),
            "TMPDIR": str(root / "tmp"), "XDG_CONFIG_HOME": str(root / "home/.config"),
            "XDG_CACHE_HOME": str(root / "home/.cache"), "XDG_DATA_HOME": str(root / "home/.local/share"),
            "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_OPTIONAL_LOCKS": "0",
            "GIT_AUTHOR_NAME": "Harness Validation", "GIT_AUTHOR_EMAIL": "validation@local.invalid",
            "GIT_COMMITTER_NAME": "Harness Validation", "GIT_COMMITTER_EMAIL": "validation@local.invalid",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000"}


def ensure_run_dirs(root):
    root = Path(root).resolve()
    for rel in ("home", "home/.config", "home/.cache", "home/.local/share", "tmp", "dsh-home"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    return root


def git(root, *args):
    proc = subprocess.run(["/usr/bin/git", *args], cwd=root, env=clean_env(Path(root).parent),
                          capture_output=True, text=True, timeout=30, encoding='utf-8')
    if proc.returncode:
        raise IsolationError(f"git {args[0]} failed ({proc.returncode}): {proc.stderr.strip()[:500]}")
    return proc.stdout.strip()


def runtime_read_roots():
    # Fixed OS/runtime roots only; neither the user's home nor arbitrary cwd is allowed.
    roots = [Path("/System"), Path("/usr"), Path("/bin"), Path("/sbin"),
             Path("/Library/Apple"), Path("/private/etc"),
             Path(sys.base_prefix), Path(sys.executable).resolve().parent]
    return sorted({str(p.resolve()) for p in roots if p.exists()})


def sandbox_policy(run_root, protected_roots):
    root = Path(run_root).resolve()
    protected = sorted({str(Path(p).resolve()) for p in protected_roots})
    reads = runtime_read_roots()
    for p in protected:
        if is_within(root, p) or is_within(p, root):
            raise IsolationError("active run and protected source roots overlap")
        if any(is_within(p, runtime) or is_within(runtime, p) for runtime in reads):
            raise IsolationError("protected root overlaps a required runtime root")
    q = lambda value: json.dumps(str(value), ensure_ascii=False)
    lines = ["(version 1)", "(deny default)", "(allow process-exec)", "(allow process-fork)",
             "(allow sysctl-read)", "(deny network*)", "(allow file-read-metadata)",
             # macOS 26 libignition opens / as its openat root before Python starts.
             '(allow file-read* (literal "/"))']
    for path in reads + [str(root)]:
        lines.append(f"(allow file-read* file-map-executable (subpath {q(path)}))")
    lines += [f"(allow file-write* (subpath {q(root)}))",
              '(allow file-read* file-write* (literal "/dev/null"))',
              '(allow file-read* (literal "/dev/urandom") (literal "/dev/random"))']
    for path in protected:
        lines.append(f"(deny file-read* file-write* (subpath {q(path)}))")
    return "\n".join(lines) + "\n"


def write_evidence(result, target=None):
    data = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if target:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")
    return data
