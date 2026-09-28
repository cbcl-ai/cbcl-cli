#!/usr/bin/env python3
"""Compute raw invoice-vs-ledger differences and a receipt of the inputs.

No tolerance is applied: every difference is reported exactly. Exit code 2 on
a row that cannot be parsed (the message names the file and line).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

VALIDATOR_VERSION = "finance-validate/1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path, id_field: str) -> dict[str, Decimal]:
    rows: dict[str, Decimal] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for line_number, row in enumerate(reader, start=2):
            identifier = (row.get(id_field) or "").strip()
            raw_amount = (row.get("amount_eur") or "").strip()
            try:
                amount = Decimal(raw_amount)
            except InvalidOperation:
                amount = None
            if not identifier or amount is None:
                print(
                    f"{path.name}: line {line_number}: cannot parse row "
                    f"({id_field}={identifier!r}, amount_eur={raw_amount!r})",
                    file=sys.stderr,
                )
                sys.exit(2)
            rows[identifier] = amount
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--invoices", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    invoices = _load(args.invoices, "invoice_id")
    ledger = _load(args.ledger, "invoice_ref")
    rows = []
    for invoice_id, invoice_amount in invoices.items():
        ledger_amount = ledger.get(invoice_id)
        rows.append({
            "invoice_id": invoice_id,
            "invoice_amount": str(invoice_amount),
            "ledger_amount": None if ledger_amount is None else str(ledger_amount),
            "raw_difference": (
                None if ledger_amount is None else str(invoice_amount - ledger_amount)
            ),
        })
    output = {
        "validator_version": VALIDATOR_VERSION,
        "receipt": {
            "invoices": {"path": str(args.invoices), "sha256": _sha256(args.invoices)},
            "ledger": {"path": str(args.ledger), "sha256": _sha256(args.ledger)},
        },
        "rows": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(f"validated {len(rows)} invoices -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
