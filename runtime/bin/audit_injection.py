#!/usr/bin/env python3
"""audit_injection.py —— 审计每条 run 到底有没有收到「指令注入」。

## 为什么必须做这个

F-017 的教训：声明了某个测量条件，实际条件却不是那个，而 meta 里看不出异常。
F-024（本条）是同一错误模式的第二次：`fixtures/task-a-billing/` 里没有 AGENTS.md，
题面却写着「改动请遵守项目里的规范」⇒ 三条 run 全部跑在「无指令层」的条件下，
而 meta 与事件流都完全正常，只有去读**会话日志**才发现。

## 注入长什么样（实测，session-20969a62）

`AGENTS.md` 不是拼进 system prompt 的，而是作为**一条独立的 user/message**，
内容是 `<system-reminder> ... Instructions from: AGENTS.md ... </system-reminder>`。
所以查事件流的 `--json` 输出**看不到它**（那里只有 status/thinking/text/final），
必须读 `$DSH_HOME/sessions/<cwd-slug>/<session-id>/session.v*.jsonl.zstd`。

## 判定
- `has_reminder`: 存在含 `<system-reminder>` 且提到 `Instructions from` 的 user/message
- `sources`: 从 `Instructions from: X` 抽出的文件名（AGENTS.md / CLAUDE.md …）
- `digest`: 注入正文（去掉 system-reminder 外壳）的 sha256 前 12 位 —— 用于确认
  「同一工作区两次跑分是否真的注入了同一份内容」
- `chars`: 注入正文字数

用法：
  audit_injection.py <run_id>              # 单条
  audit_injection.py --all                 # 全部 results/runs + results/tasks
  audit_injection.py --all --json out.json
"""
import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

BAT_HOME = os.environ.get("BAT_HOME", str(Path.home() / ".dsh"))
PROJ = os.environ.get("PROJ", str(Path(__file__).resolve().parents[1]))

FROM_RE = re.compile(r"Instructions from:[ \t]*([^\r\n<>]*[^\s<>])[ \t]*(?=\r?$|<)", re.M)
REMINDER_RE = re.compile(r"<system-reminder>(.*?)</system-reminder>", re.S)


def session_dir(session_id):
    if not session_id:
        return None
    hits = glob.glob(os.path.join(BAT_HOME, "sessions", "*", session_id))
    return hits[0] if hits else None


def read_session_log(sdir):
    logs = sorted(glob.glob(os.path.join(sdir, "session.v*.jsonl.zstd")),
                  key=lambda p: int(re.search(r'session\.v(\d+)', p).group(1)))
    if not logs:
        return ""
    try:
        try:
            from compression import zstd
        except ImportError:
            pass
        else:
            return zstd.decompress(Path(logs[-1]).read_bytes()).decode("utf-8", "strict")
        r = subprocess.run(["zstd", "-d", "-c", logs[-1]],
                           capture_output=True, timeout=120)
        if r.returncode != 0:
            return ""
        return r.stdout.decode("utf-8", "strict")
    except Exception:
        return ""


def instruction_blocks(event):
    """Workspace instruction wrappers, excluding the original user's text."""
    if not isinstance(event, dict) or event.get('type') != 'user/message':
        return []
    data = event.get('data') or {}
    if not isinstance(data, dict):
        return []
    source = data.get('source') or {}
    if isinstance(source, dict) and source.get('kind') == 'user':
        return []
    content = data.get('content', [])
    if not isinstance(content, list):
        return []
    text = ''.join(c['text'] for c in content if isinstance(c, dict)
                   and c.get('type', 'text') == 'text' and isinstance(c.get('text'), str))
    return [{'sources': FROM_RE.findall(m.group(1)),
             'body': FROM_RE.sub('', m.group(1)).strip()}
            for m in REMINDER_RE.finditer(text) if FROM_RE.findall(m.group(1))]


def analyze(session_id):
    sdir = session_dir(session_id)
    if not sdir:
        return {"status": "no_session_dir", "has_reminder": None, "sources": []}
    log = read_session_log(sdir)
    return analyze_log(log)


def analyze_log(log):
    """Shared parser used by the audit and new-run condition extractor."""
    if not log.strip():
        return {"status": "no_log", "has_reminder": None, "sources": []}

    sources, bodies, user_inputs = [], [], []
    errors = 0
    for line in log.splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except Exception:
            errors += 1
            continue
        if not isinstance(ev, dict):
            errors += 1
            continue
        if ev.get("type") != "user/message":
            continue
        data = ev.get("data") or {}
        if not isinstance(data, dict) or not isinstance(data.get("content", []), list):
            errors += 1
            continue
        content = data.get("content", [])
        text = "".join(c.get("text", "") for c in content
                       if isinstance(c, dict) and isinstance(c.get("text"), str)
                       and c.get("type", "text") == "text")
        source = data.get("source") or {}
        if isinstance(source, dict) and source.get("kind") == "user":
            user_inputs.append(text)
            # A user question mentioning an instruction wrapper is not injected policy.
            continue
        for block in instruction_blocks(ev):
            for f in block['sources']:
                if f not in sources:
                    sources.append(f)
            # Preserve the existing normalized digest for historical comparison.
            bodies.append(block['body'])

    prompt = user_inputs[0] if len(user_inputs) == 1 else None
    provenance = {
        "user_input_count": len(user_inputs),
        "observed_prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest() if prompt is not None else None,
        "observed_prompt_sha1": hashlib.sha1(prompt.encode()).hexdigest() if prompt is not None else None,
        "invalid_log_lines": errors,
    }
    if errors:
        return {"status": "invalid_log", "has_reminder": None, "sources": sources, **provenance}

    if not bodies:
        return {"status": "ok", "has_reminder": False, "sources": [], **provenance}

    joined = "\n---\n".join(bodies)
    return {
        "status": "ok",
        "has_reminder": True,
        "sources": sources,
        "chars": len(joined),
        "digest": hashlib.sha256(joined.encode()).hexdigest()[:12],
        "blocks": len(bodies),
        **provenance,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id", nargs="?")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    metas = []
    for d in ("results/runs", "results/tasks"):
        metas.extend(sorted(glob.glob(os.path.join(PROJ, d, "*.meta.json"))))
    if args.run_id:
        metas = [m for m in metas if args.run_id in os.path.basename(m)]
    if not metas:
        print("没有 meta.json")
        return 1

    rows = []
    meta_errors = 0
    for m in metas:
        try:
            meta_bytes = Path(m).read_bytes()
            meta = json.loads(meta_bytes)
            if not isinstance(meta, dict):
                raise ValueError("metadata must be an object")
        except (OSError, ValueError) as error:
            meta_errors += 1
            rows.append({"run_id": Path(m).name.removesuffix('.meta.json'),
                         "item": "(unknown)", "profile": None, "status": "invalid_meta",
                         "has_reminder": None, "sources": [], "prompt_integrity": "unknown",
                         "prompt_matches_meta": None, "error": str(error)})
            continue
        r = analyze(meta.get("session_id"))
        r["run_id"] = meta.get("run_id") or Path(m).name.removesuffix('.meta.json')
        r["audit_version"] = "injection-audit.v2"
        r["session_id"] = meta.get("session_id")
        r["meta_sha256"] = hashlib.sha256(meta_bytes).hexdigest()
        r["item"] = meta.get("item_id") or meta.get("task_id")
        r["profile"] = meta.get("profile")
        declared = meta.get("item_sha256") or meta.get("prompt_sha256")
        observed = r.get("observed_prompt_sha256")
        if not declared or not observed:
            r["prompt_integrity"] = "unknown"
        elif declared == observed:
            r["prompt_integrity"] = "match"
        elif isinstance(declared, str) and len(declared) == 40 and declared == r.get("observed_prompt_sha1"):
            r["prompt_integrity"] = "legacy_sha1_match"
        else:
            r["prompt_integrity"] = "mismatch"
        r["prompt_matches_meta"] = (None if r["prompt_integrity"] == "unknown"
                                    else r["prompt_integrity"] != "mismatch")
        rows.append(r)

    n_inj = sum(r.get("has_reminder") is True for r in rows)
    n_none = sum(r.get("has_reminder") is False for r in rows)
    print(f"扫描 {len(rows)} 条 run：有注入 {n_inj}，本日志未见注入 {n_none}，未知 {len(rows)-n_inj-n_none}")
    print()

    # 注入正文只是条件之一；不能忽略题面、模型、工具与工作区差异。
    groups = {}
    for r in rows:
        key = ("(未知)" if r.get("has_reminder") is None
               else r.get("digest") or "(本日志未见注入)")
        groups.setdefault(key, []).append(r)

    if len(groups) > 1:
        print("⚠️  存在多个注入条件：")
    for k, v in sorted(groups.items(), key=lambda x: -len(x[1])):
        srcs = sorted({s for r in v for s in (r.get("sources") or [])})
        chars = v[0].get("chars", 0)
        items = sorted({str(r["item"] or "(unknown)") for r in v})
        print(f"  [{k}] n={len(v)}  {chars} 字  来源={srcs or '(无)'}")
        print(f"       覆盖 {len(items)} 个 item: {', '.join(items[:12])}"
              + (" …" if len(items) > 12 else ""))

    print()
    # 任务型要单独看：它的题面自称"遵守项目规范"
    task = [r for r in rows if str(r["item"]).startswith("T-")]
    if task:
        inj = sum(1 for r in task if r.get("has_reminder"))
        print(f"任务型 run: {len(task)} 条，其中有指令注入 {inj} 条")
        for r in task:
            flag = ("✅" if r.get("has_reminder") is True else
                    "未见注入" if r.get("has_reminder") is False else "未知")
            print(f"  {r['run_id']:52} {flag}  {r.get('sources') or ''}")

    if args.json:
        json.dump(rows, open(args.json, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"\n明细 → {args.json}")
    if meta_errors:
        print(f"元数据损坏 {meta_errors} 条，已列为未知，不能视为完整审计。", file=sys.stderr)
    return 1 if meta_errors else 0


if __name__ == "__main__":
    sys.exit(main())
