"""本地结算文件转换，不调用网络。契约见 docs/spec.md。"""
import argparse
import csv
import json
from pathlib import Path
import sys

class RatesPending(RuntimeError):
    pass

def normalize(records):
    return [{"id": r["id"], "currency": r["currency"], "net_cents": r["amount_cents"]}
            for r in records]

def convert(rows, rate_document):
    if rate_document.get("status") != "approved" or not rate_document.get("approval_id"):
        raise RatesPending("approved_rates.json 尚未获得审批；需要已批准的汇率及 approval_id")
    rates = rate_document["eur_per_unit"]
    return [{**r, "eur_cents": int(r["net_cents"] * float(rates[r["currency"]]))} for r in rows]

def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--rates", required=True)
    p.add_argument("--preview", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)
    records = json.loads(Path(args.input).read_text())
    rows = normalize(records)
    preview = Path(args.preview)
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    try:
        converted = convert(rows, json.loads(Path(args.rates).read_text()))
    except RatesPending as exc:
        print(str(exc), file=sys.stderr)
        return 3
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "currency", "net_cents", "eur_cents"])
        writer.writeheader()
        writer.writerows(converted)
    print(f"generated {out}: {len(converted)} rows")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
