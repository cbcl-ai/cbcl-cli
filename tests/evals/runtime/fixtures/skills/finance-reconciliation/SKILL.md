---
name: finance-reconciliation
description: Reconcile supplier invoices against a general-ledger export and report unmatched or mismatched invoices. Use when asked to reconcile, match or check invoices against a ledger. Not for expense claims, payment runs or tax filings.
---

# Finance reconciliation

Use this playbook to match an invoice export against a ledger export.

1. Read `references/matching-rules.md` before classifying any invoice. It
   defines when a difference is an adjustment and when an invoice is unmatched.
2. Run the validator from this skill directory on the two input files:

   ```
   python3 scripts/validate.py --invoices <invoices.csv> --ledger <ledger.csv> --out <output-dir>/validation.json
   ```

   It writes the raw per-invoice differences and a receipt with the input file
   hashes. It applies no tolerance; classification is yours, using the rules.
   Exit code 2 means a row could not be parsed; the message names the file and
   line. Report that line instead of guessing a value.
3. Classify every invoice with the matching rules and write the report the
   assignment asks for. Keep the source files unchanged.
4. Payment runs are a separate, explicitly authorized procedure; see
   `references/payment-run.md`.
