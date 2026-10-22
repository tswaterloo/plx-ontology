# Decision 2026-10-02: bc-capture-incremental (typed modified-since filter, weekly full pass)

- decided: 2026-10-02T00:40Z
- status: decided

## Context

`bc_read.BcCapture` re-reads every entity set in full each pass, plus one lines GET per
header. The closed request grammar (`integrations/live_source_grammar.bc_query`) allows
one `eq` on an allowlisted number/GUID field, so any incremental read needs a reviewed
grammar change.

## Evidence

Microsoft's API v2.0 reference (MicrosoftDocs/dynamics365smb-devitpro-pb, main,
`dev-itpro/api-reference/v2.0/resources/dynamics_*.md`; learn.microsoft.com is blocked
from this runtime, the GitHub source is the same text):

- Read-only `lastModifiedDateTime` exists on purchaseInvoice, salesInvoice,
  purchaseReceipt, salesShipment, vendor, **itemLedgerEntry** and **generalLedgerEntry**.
  The reference says itemLedgerEntries have the field.
  The live profile `profile.bc_case_entities` sees the field on the sets it reads.
- Line resources (purchaseInvoiceLine, purchaseReceiptLine) have no
  `lastModifiedDateTime`.
- Deletions: only webhooks report `changeType: "deleted"` (dynamics_subscriptions.md),
  and a subscription is a POST. The Workbench is GET-only, so webhooks are excluded.
  Practitioners report the same gap: a since-filter never sees deletions
  (https://community.fabric.microsoft.com/t5/Pipelines/Get-Business-Central-data-thru-API-incrementally-when-Business/td-p/3999459).
- Filter form used by integrators: `lastModifiedDateTime ge <ISO-8601 UTC>`
  (https://www.eonesolutions.com/help-article/how-to-set-up-a-business-central-data-source-using-dynamic-date-filters/).

## Decision

Option (c), narrowed to posted invoices for the line skip:

1. **Grammar.** `live_source_grammar.bc_query` gains one typed form:
   `$filter=lastModifiedDateTime ge <UTC instant>` (fixed-width `YYYY-MM-DDTHH:MM:SS.fffZ`,
   parsed, never pasted), allowed only on sets the grammar lists as having the field
   (headers, parties, items, itemLedgerEntries, generalLedgerEntries). Not on line sets.
   The guard test proves no other operator or field passes.
2. **Watermark.** Per unit (company x set): next `since` = max seen
   `lastModifiedDateTime` minus 10 minutes overlap. Re-read rows dedupe by etag/content
   digest in capture_store, so overlap costs requests, not versions.
3. **Lines.** Re-read the lines of a header when the header is new, its etag or
   `lastModifiedDateTime` changed, or it is an open (unposted) document. Skip the line GET
   only for unchanged **posted invoices and credit memos**: BC corrects those with a
   separate document. Posted receipts and shipments keep a line re-read every pass,
   because BC's Undo Receipt/Undo Shipment adds correction lines to the posted document
   itself, and nothing shows that the header timestamp moves when it does.
4. **Weekly full pass** (key-only `$select=id,lastModifiedDateTime` where allowed) sets
   `missing_from_source_since` and catches renumbering and deletions. Daily if a
   week shows any missing key.
5. **Report** per unit: `mode` (incremental|full), `since`, requests, rows changed.

## Consequences

- The grammar change needs its own review (it widens what the guard accepts).
- Between full passes, a deleted BC record still looks present for up to 7 days; tools
  must show `captured_at` of the last full pass (freshness already carries it).

## How we will know this was wrong

- A full pass finds changed content the incremental passes missed (same key, different
  digest, `lastModifiedDateTime` older than the watermark): then the field is not
  reliable for that set; that set goes back to full reads.
- Line digests of an unchanged posted invoice or credit memo ever differ on a full pass:
  drop the skip in rule 3.
