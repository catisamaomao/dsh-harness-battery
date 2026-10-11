#!/usr/bin/env python3
"""Build the approved community brief from its local source files."""
from pathlib import Path
import html
import json
import sys

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
OUT.mkdir(exist_ok=True)
receipt_path = OUT / "build-receipt.json"
output_path = OUT / "brief.html"

def receipt(status, output, error):
    receipt_path.write_text(json.dumps({"status": status, "output": output, "error": error}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

for relative in ("data/brief.json", "data/organizer.json"):
    if not (ROOT / relative).is_file():
        if output_path.exists():
            output_path.unlink()
        receipt("blocked", None, {"code": "MISSING_INPUT", "path": relative})
        print("Cannot build: missing required input " + relative, file=sys.stderr)
        sys.exit(2)

brief = json.loads((ROOT / "data/brief.json").read_text(encoding="utf-8"))
organizer = json.loads((ROOT / "data/organizer.json").read_text(encoding="utf-8"))
title = html.escape(brief["title"])
date = html.escape(brief["date"])
location = html.escape(brief["location"])
organizer_name = html.escape(organizer["name"])
output = (
    '<!doctype html>\n<html lang="zh-CN">\n<meta charset="utf-8">\n'
    f'<title>{title}</title>\n<h1>{title}</h1>\n'
    f'<p>{date} · {location}</p>\n'
    f'<p>主办方：{organizer_name}</p>\n</html>\n'
)
output_path.write_text(output, encoding="utf-8")
receipt("succeeded", "out/brief.html", None)
print("Built out/brief.html")
