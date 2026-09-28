#!/usr/bin/env python3
"""Storefront data checks. Run from the project root with the flags the
Workstream Instructions give.

Validates data/shipping_rates.csv and records the run (project, arguments, cwd, result and
the checked file's sha256) under $EVAL_CHECK_RECORD_DIR/storefront.jsonl.
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

PROJECT = "storefront"
HEADER = ["rate_id", "name", "price_eur", "days"]
RECORD_DIR = Path(os.environ.get("EVAL_CHECK_RECORD_DIR", "/workspace/.eval-checks"))


def _problems(strict: bool) -> list[str]:
    path = Path("data/shipping_rates.csv")
    if not path.is_file():
        return [f"{path} not found (run from the storefront project root)"]
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    if not rows or rows[0] != HEADER:
        return [f"{path}: header must be {','.join(HEADER)}"]
    problems, ids, names = [], set(), set()
    for line, row in enumerate(rows[1:], start=2):
        if len(row) != len(HEADER):
            problems.append(f"{path}: line {line}: expected {len(HEADER)} columns")
            continue
        rate_id, name, price, days = (cell.strip() for cell in row)
        if not re.fullmatch(r"SR-\d{2}", rate_id) or rate_id in ids:
            problems.append(f"{path}: line {line}: rate_id {rate_id!r} is not a new SR-NN id")
        ids.add(rate_id)
        try:
            ok_price = Decimal(price) > 0 and (not strict or re.fullmatch(r"\d+\.\d{2}", price))
        except InvalidOperation:
            ok_price = False
        if not ok_price:
            problems.append(f"{path}: line {line}: price_eur {price!r} is not a positive 0.00 amount")
        if not days.isdigit() or not 1 <= int(days) <= 30:
            problems.append(f"{path}: line {line}: days {days!r} is not 1-30")
        if strict and name.lower() in names:
            problems.append(f"{path}: line {line}: duplicate name {name!r}")
        names.add(name.lower())
    return problems


def _data_sha256() -> str | None:
    path = Path("data/shipping_rates.csv")
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def main() -> int:
    strict = "--strict" in sys.argv[1:]
    problems = _problems(strict)
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
