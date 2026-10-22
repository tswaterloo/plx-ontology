# Decided 2026-10-07: where BC payment state comes from

Replaces `open-bc-finance-2-payment-ledger.md`.

## Decision
Options 1 and 2 below, as built. Option 3 stays out until a source serves posted ledger
entries.

- **Paid state and the amount owed come from the posted invoices.** `status` (Open / Paid /
  Canceled / Corrective) decides the `vendor_payment` stage of `purchase_to_invoice`.
  `remainingAmount` is `PurchaseInvoice.remaining_amount` / the sales equivalent, summed by
  `plx.aggregate` per currency only. A record without it is a gap
  (`measure_value_missing`), never zero. Applied credit memos (`purchaseCreditMemos`,
  `salesCreditMemos`, by `invoiceId`, else a number naming exactly one invoice of the
  company) are the optional `vendor_credit` stage.
- **Payment journals are captured as `Payment`: a pending payment, never proof of one.**
  `customerPaymentJournals` / `vendorPaymentJournals` with their lines (`customerPayments` /
  `vendorPayments`, read by nested navigation, parent field `journalId`). BC removes a line
  from the journal when it posts it, so a line that disappears was posted or deleted;
  neither is a proven payment. The line amount has no currency in API v2.0: a typed
  `currency_unknown` gap.
- **Aged balances per party come from `agedAccountsReceivables` / `agedAccountsPayables`.**
  These are report pages keyed by `customerId` / `vendorId` (`BC_RECORD_KEY_FIELD`). A row
  without an etag is versioned by its content digest. Their amounts carry no asserted
  currency, so they are never summed. Their `$select` allowlist has no
  `lastModifiedDateTime`, which a report row does not serve.
- **A set that a company does not expose is a coded gap.** A 404 on a unit's first page
  fails the unit with reason `not_exposed` (`units_not_exposed`). The capture stays
  incomplete and the projection binding stays `source_capture_incomplete`, never an empty
  set. A 404 on a later page or a line read stays `source_refused`.

## Options considered
1. Posted invoices (`status`, `remainingAmount`) plus applied credit memos. Chosen.
2. Payment journals (unposted lines with `appliesToInvoiceId` / `appliesToInvoiceNumber`).
   Chosen, as pending payments only.
3. Customer / vendor ledger entries (posted payments with applied and remaining amounts,
   the real history). Rejected for now: not a standard API v2.0 entity set for these
   companies.

## Evidence
- plx-customer-portal@790256c `portal/src/lib/business-central/inbound.ts:248-268`
  (`shouldSkipMissingPostedCollection`): customerLedgerEntries / vendorLedgerEntries are
  not published on api/v2.0 for this BC company, so the Portal skips their 404s. In the
  same file, `projectPurchaseInvoice` says "purchaseInvoices may omit remainingAmount;
  status=Paid is authoritative". `document-finance.ts:154`: posted vendor payments leave
  the vendorPayments journal API.
- BC API v2.0 resource reference (customerPayment, vendorPayment, agedAccountsReceivable,
  agedAccountsPayable, purchaseCreditMemo): the field names in
  `integrations/live_source_grammar.py` `BC_QUERY_SPECS`.
- Injected-transport and isolated-fixture tests: `integrations/tests/test_bc_capture.py`
  (31 units per company, `not_exposed`, the filtered line fallback),
  `integrations/tests/test_bc_read_entities.py` (`probe --finance-entities` against the
  `bc.ledgers` expectations), `ontology/tests/test_project_bc.py` (paid / partial /
  unpaid / credited per currency, Payment and aged rows on both sides with `bc://`
  citations, none visible to a reader without the finance marking).

## Not verified live (what would change this)
- `verify/live-checks/bc.ledgers.json` and `bc.capture_full.json` are live-unverified.
  They will show:
  - whether the PLX tenant serves `remainingAmount` on purchase invoices;
  - whether each `$select` allowlist is accepted;
  - which sets each company answers 404 for (`finance_not_exposed`, `units_not_exposed`).
- Whether an aged report page includes a totals row (for example a nil `customerId`). Such
  a row would project as an aged row linked to no party. It is never summed, but it would
  be a finding: drop it in the capture if the live probe shows it.
- A custom API page, or a newer API version, that serves posted customer / vendor ledger
  entries reopens option 3. Until then, posted payment history is a gap.
