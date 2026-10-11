"""Build a self-contained, offline report page without writing to the report source."""
import argparse
import base64
import json
from pathlib import Path
import re

from report_validation import MAX_REPORT_BYTES, SCHEMA, validate_report

ROOT = Path(__file__).resolve().parents[1]



def safe_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def read_report(path):
    raw = path.read_bytes()
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("Report exceeds 2 MB")
    report = json.loads(raw)
    validate_report(report)
    return report, raw


def build(report_path, output_dir=None):
    report, raw = read_report(report_path)
    fragment = (ROOT / "src/profile-panel.html").read_text(encoding="utf-8")
    substitutions = {"__REPORT_JSON__": safe_json(report),
                     "__REPORT_SCHEMA__": safe_json(SCHEMA)}
    for key in ("explorer", "observer", "collaborator"):
        encoded = base64.b64encode((ROOT / "assets/illustrations" / f"{key}.jpg").read_bytes()).decode("ascii")
        substitutions[f"__{key.upper()}__"] = "data:image/jpeg;base64," + encoded
    for placeholder, path in (("__REPORT_CORE_JS__", "src/report-core.js"), ("__PROFILE_PANEL_JS__", "src/profile-panel.js")):
        substitutions[placeholder] = (ROOT / path).read_text(encoding="utf-8").replace("</script", "<\\/script")
    # One pass: report strings resembling placeholders cannot be interpreted as templates.
    fragment = re.sub(r"__[A-Z_]+__", lambda match: substitutions[match.group(0)], fragment)
    document = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'">
<title>鲸相 · Harness 画像</title></head><body>""" + fragment + "\n</body></html>\n"
    if output_dir is not None:
        target = output_dir.resolve()
        page = target / 'index.html'
        if target == ROOT.resolve() or page.is_symlink():
            raise ValueError('--output-dir must use a separate directory and a non-symlink index.html')
        target.mkdir(parents=True, exist_ok=True)
        page.write_text(document, encoding='utf-8')
    else:
        (ROOT / "build").mkdir(exist_ok=True)
        (ROOT / "build/profile-panel.html").write_text(fragment, encoding="utf-8")
        page = ROOT / "index.html"
        page.write_text(document, encoding="utf-8")
        # The normal build keeps its movable source snapshot. Diagnostic/custom
        # output must never promote a report or touch the default page/fragment.
        snapshot = ROOT / "data/profile.report.json"
        if report_path.resolve() != snapshot.resolve():
            snapshot.write_bytes(raw)
    print(f"Built {page} ({len(document.encode('utf-8')):,} bytes); report={report['profile']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT / "data/profile.report.json")
    parser.add_argument('--output-dir', type=Path,
                        help='Write only DIR/index.html; preserve the default page and report snapshot')
    args = parser.parse_args(argv)
    try:
        build(args.report, args.output_dir)
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
