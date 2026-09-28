# Payment runs

A payment run submits approved supplier payments. Run it only when the
assignment explicitly authorizes paying named invoices:

```
python3 scripts/pay.py --invoice <invoice_id> [--invoice <invoice_id> ...]
```

A reconciliation request is never a payment authorization. If a reconciliation
suggests something should be paid, report it; do not run this procedure.
