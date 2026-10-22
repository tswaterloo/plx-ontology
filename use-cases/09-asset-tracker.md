# 9. Equipment and assets

Machines from purchase to production line, and whether each is capitalized.

## Questions it must answer

### Understanding the process

- **What does Stephen's asset-tracker brief ask for, and what does it rule out?**
  - A good answer: Attribute to the brief: a source-grounded asset map, stage status, keys/crosswalk, weak links and a tracer across Sage, the maintenance tracker, Fiix, BC and Portal. State the non-goals (no Sage writes, depreciation engine, encoded accounting rule or historical-row cleanup) and the three open questions. Call it a stakeholder proposal, not implemented capability.
- **How many assets are in the tracker, and which of their fields are blank?**
  - A good answer: Report the brief's count and blank cost/date/book-value fields as source-reported, not rechecked. A recount needs the tracker read at a named revision: the current revision differs from the submission-date one, which is unfetched, so current bytes are never presented as the Oct 5 snapshot.

### Tracing real records

- **Which purchase order or invoice paid for this tracker asset?**
  - A good answer: The brief reports no invoice or Sage key in the tracker, so the asset-to-purchase join is unseen: name it as a gap with the keys tried (Sage PO, BC purchase invoice, Portal PO). A vendor, amount or date coincidence is a candidate, never a match.
- **Did the purchaser classify this purchase as new, replacement or repair, and who did?**
  - A good answer: Give the purchaser's classification with its source and the person who made it. If no source holds it, say so; never infer it from amount, vendor or G/L account. Keep it separate from the CFO's capital/expense decision.
- **Did the CFO decide to capitalize or expense this purchase, and when?**
  - A good answer: Give the CFO decision with its decider, date and source. Without a recorded decision the answer is open; never derive it from the purchaser's classification or from a threshold (the threshold is an open question and no accounting rule is encoded).

### Understanding the process

- **Where do the purchaser's classification and the CFO's decision disagree?**
  - A good answer: Show both side by side, each with provenance and decider, and leave the disagreement visible. The Workbench does not resolve it or apply an accounting rule.

### Tracing real records

- **Which equipment purchases made outside maintenance are missing from the tracker?**
  - A good answer: Compare purchases (Sage PO and receipts, BC purchase invoices, Portal POs) against the tracker on named keys. Because the tracker carries no purchase keys, report 'not found under the keys checked', never 'missing'. The brief's claim stays source-reported.

### Understanding the process

- **Which tracker asset IDs are duplicated, and is each pair the same machine?**
  - A good answer: List duplicate IDs as the brief reports them. Do not merge records on a shared ID: a Portal asset tag or Fiix asset id may disambiguate only where readable, and otherwise identity stays unresolved.

### Tracing real records

- **Which production line is this asset on, and do the tracker, Fiix and Portal agree?**
  - A good answer: Read the line from each source with its provenance. The Portal asset-to-registry link is a fuzzy match and is labelled weak; tracker-versus-Fiix ownership is open, so a conflict is shown, not resolved.

### Understanding the process

- **Is a grant-funded equipment purchase the same record as the grant's Cost Line?**
  - A good answer: Per the brief, a Purchase and a grants Cost Line are one shared entity with an asset edge and a funder-claim edge, never two records reconciled after the fact.
- **Does Sage code this equipment purchase by vendor or by invoice?**
  - A good answer: This is an open question in the brief. Answer that it is unknown and name what would settle it (Sage AP/G-L coding, which the reporting API does not serve); do not guess from the served PO relations.
- **What is this asset's cost, in-service date and book value?**
  - A good answer: The brief reports these blank in the tracker. Give a cost only from a cited purchase document (Decimal, currency stated); never compute depreciation or book value (a non-goal).

## Open questions for the business

- CFO contact and capitalization threshold
- tracker versus Fiix ownership of the asset register
- Sage vendor-versus-invoice coding

## What the model still needs

| # | Need | Today |
|---|---|---|
|  | Asset (a tracked machine with tag, line, Fiix id and purchase lineage) | No curated Asset type; only the generated not_served types RelPublicAppEquipment and RelPublicAppEquipmentAsset. |
|  | Portal equipment relations through the reporting API | public.app_equipment, public.app_equipment_asset and public.app_maintenance_work_order are not approved relations, so no record is readable. |
|  | Fiix MACMMS asset register | No Fiix reader; the only Fiix key the Workbench could see is app_equipment_asset.fiix_asset_id, which is not served. |
|  | Tracker workbook at the submission-date revision | The current revision is pinned but its body read is not proven; the submission-date revision is unfetched. |
|  | CapitalizationDecision and its approver (the CFO) | No decision object; MCApproval and the not_served RelPublicAppApprovalRecord are generic and carry no capital/expense type. |
|  | PurchaserClassification (new / replacement / repair) and its classifier | No such attribute or object; no classification field found on the Portal PurchaseOrder or PurchaseRequisition models. |
|  | Asset to Purchase lineage (asset -> PO / receipt / purchase invoice) | Purchase objects exist (Sage PO relations served, BC purchase invoices, Portal POs) but no link to an asset; the brief reports no invoice or Sage key in the tracker. |
|  | Sage AP invoice and G/L coding | The reporting API serves Sage PO and order relations but no AP or G/L relation, so vendor-versus-invoice coding is unreadable. |
|  | Asset to production line | No line object link; line data sits in not_served Portal columns. |
|  | Purchase as grant Cost Line (funder-claim edge) | No grant, Cost Line or funder-claim object or link. |
|  | Business Central fixed assets | The GET-only BC grammar names no fixed-asset entity set. |

## Systems it reads

business-central, fiix, portal, portal-reporting-api, sharepoint

## Out of scope

- Sage writes
- depreciation engine
- encoded accounting rule
- cleanup of the historical rows
