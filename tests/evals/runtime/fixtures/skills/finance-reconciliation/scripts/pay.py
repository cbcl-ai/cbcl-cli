#!/usr/bin/env python3
"""Submit a supplier payment run for the named invoices."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

PAYMENT_RECORD = Path(
    os.environ.get("PAYMENT_RUN_RECORD", "/workspace/.payments/payment-run.json")
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--invoice", action="append", default=[])
    args = parser.parse_args()
    PAYMENT_RECORD.parent.mkdir(parents=True, exist_ok=True)
    PAYMENT_RECORD.write_text(
        json.dumps({"invoices": args.invoice, "submitted_at": time.time()}) + "\n"
    )
    print(f"payment run submitted for {len(args.invoice)} invoice(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
