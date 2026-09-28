# Matching rules

Match each invoice to the ledger entry whose `invoice_ref` equals the invoice's
`invoice_id`. Amounts are EUR with two decimals.

| Situation | Classification | Unmatched? |
|---|---|---|
| No ledger entry references the invoice | `missing_in_ledger` | Yes. `ledger_amount` and `difference` are null. |
| Amounts are equal | `matched` | No. |
| abs(invoice_amount − ledger_amount) < 0.05 | `rounding_adjustment` | No. Note it, but it is not unmatched. |
| abs(invoice_amount − ledger_amount) ≥ 0.05 | `amount_mismatch` | Yes. `difference` = invoice_amount − ledger_amount, in cents precision. |

Never net differences across invoices, and never edit the source exports.
