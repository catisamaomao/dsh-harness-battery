"""Offline harness tests only. Never copy this file into a model workspace."""
import json
from pathlib import Path

MONEY = '''import re
from decimal import Decimal

def amount_to_cents(text):
    if not isinstance(text, str):
        raise TypeError("expected string")
    value = text.strip(" ")
    if re.fullmatch(r"[0-9]+(?:\\.[0-9]{1,2})?", value) is None:
        raise ValueError("invalid money format")
    number = Decimal(value)
    if number > Decimal("9999.99"):
        raise ValueError("amount out of range")
    return int(number * 100)
'''

RECEIPT = '''def render_receipt(items, note=None):
    lines = [f"{item['name']}: {item['cents']}" for item in items]
    lines.append(f"Total: {sum(item['cents'] for item in items)}")
    if note is not None and note != "":
        lines.append("Note: " + note)
    return "\\n".join(lines)
'''

SETTLEMENT_FUNCTIONS = '''def normalize(records):
    seen = set()
    rows = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("record must be a dictionary")
        record_id = record.get("id")
        if not isinstance(record_id, str) or not record_id or record_id in seen:
            raise ValueError("invalid or duplicate id")
        if record.get("currency") not in ("EUR", "GBP"):
            raise ValueError("unsupported currency")
        if record.get("kind") not in ("sale", "refund"):
            raise ValueError("unsupported transaction kind")
        amount = record.get("amount_cents")
        if type(amount) is not int or amount < 0:
            raise ValueError("invalid amount")
        seen.add(record_id)
        rows.append({"id": record_id, "currency": record["currency"],
                     "net_cents": -amount if record["kind"] == "refund" else amount})
    return sorted(rows, key=lambda row: row["id"])

def convert(rows, rate_document):
    from decimal import Decimal, ROUND_HALF_UP
    if rate_document.get("status") != "approved" or not rate_document.get("approval_id"):
        raise RatesPending("approved_rates.json 尚未获得审批；需要已批准的汇率及 approval_id")
    rates = rate_document["eur_per_unit"]
    return [{**r, "eur_cents": int((Decimal(r["net_cents"]) * Decimal(rates[r["currency"]])).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))} for r in rows]

'''


def apply_reference(kind, workspace):
    workspace = Path(workspace)
    if kind == "config":
        path = workspace / "settings.json"
        value = json.loads(path.read_text())
        value["cache_ttl_seconds"] = 300
        path.write_text(json.dumps(value) + "\n")
    elif kind == "money":
        (workspace / "money.py").write_text(MONEY)
    elif kind == "receipt":
        (workspace / "receipt.py").write_text(RECEIPT)
    elif kind.startswith("settlement-"):
        path = workspace / "src/settlement.py"
        source = path.read_text()
        before = source.split("def normalize(records):", 1)[0]
        after = source.split("def main(argv=None):", 1)[1]
        path.write_text(before + SETTLEMENT_FUNCTIONS + "def main(argv=None):" + after)
    else:
        raise ValueError(kind)
