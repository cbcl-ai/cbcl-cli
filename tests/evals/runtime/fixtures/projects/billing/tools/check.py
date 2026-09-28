#!/usr/bin/env python3
"""Billing data checks. Run from the project root with the flags the
Workstream Instructions give.

Validates data/fees.csv and records the run (project, arguments, cwd, result and
the checked file's sha256) under $EVAL_CHECK_RECORD_DIR/billing.jsonl.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

PROJECT = "billing"
HEADER = ["fee_code", "description", "amount_eur"]
RECORD_DIR = Path(os.environ.get("EVAL_CHECK_RECORD_DIR", "/workspace/.eval-checks"))


def _problems(ledger: bool) -> list[str]:
    path = Path("data/fees.csv")
    if not path.is_file():
        return [f"{path} not found (run from the billing project root)"]
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    if not rows or rows[0] != HEADER:
        return [f"{path}: header must be {','.join(HEADER)}"]
    problems, codes = [], set()
    for line, row in enumerate(rows[1:], start=2):
        if len(row) != len(HEADER):
            problems.append(f"{path}: line {line}: expected {len(HEADER)} columns")
            continue
        code, description, amount = (cell.strip() for cell in row)
        if not re.fullmatch(r"F-\d{3}", code) or code in codes:
            problems.append(f"{path}: line {line}: fee_code {code!r} is not a new F-NNN code")
        codes.add(code)
        if not description:
            problems.append(f"{path}: line {line}: description is empty")
        try:
            ok_amount = Decimal(amount) > 0 and (not ledger or re.fullmatch(r"\d+\.\d{2}", amount))
        except InvalidOperation:
            ok_amount = False
        if not ok_amount:
            problems.append(f"{path}: line {line}: amount_eur {amount!r} is not a positive 0.00 amount")
    return problems


def _data_sha256() -> str | None:
    path = Path("data/fees.csv")
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def main() -> int:
    ledger = "--ledger" in sys.argv[1:]
    problems = _problems(ledger)
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    with (RECORD_DIR / f"{PROJECT}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "project": PROJECT, "argv": sys.argv[1:], "cwd": os.getcwd(),
            "ok": not problems, "at": time.time(),
            "data_sha256": _data_sha256(),
        }) + "\n")
    for problem in problems:
        print(problem, file=sys.stderr)
    print(f"{PROJECT} check: {'passed' if not problems else 'FAILED'}")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
