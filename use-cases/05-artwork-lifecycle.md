# 5. Artwork and labels

Artwork and label approval, from proof to printed component to finished good.

## Questions it must answer

### Understanding the process

- **What does Vince's Artwork Lifecycle Map propose for artwork, printed components and AO label control, and what does it call current practice?**
  - A good answer: Attribute to the map: a Portal + Business Central target where artwork files stay in Portal and BC represents printed items, receipts and AO component consumption; FileMaker and Sage are current practice or a one-time cutover. Call it a process synthesis posted by Vince on 12 Oct 2026, not a verified report of deployed systems.
- **When a customer approves new artwork, does the printed component keep its P-number or get a new one, and what happens to the old stock?**
  - A good answer: Per the map's target: a new P-item per approved artwork version, old stock held, open POs, receipts and AOs flagged, and the FG number unchanged with revision N+1. Keep the map's own inconsistency visible: its recorded API implementation mints a new part and holds lots while its As-built paragraph says no new part number and no stock flag. Never pick one; screens, backfill and supplier/reorder links are reported absent.
- **Is QA approval of artwork a blocking gate or a soft warning?**
  - A good answer: Attribute to the map: QA owns approval, Regulatory records its review within the same version, and approval is a soft warning with a waiver per the Oct 12 owner answer, which supersedes the Oct 1 blocking decision. The rules table still carries the blocking row. The soft-gate code paths are a source pin, not deployed configuration; the live setting is open.
- **Does accepting artwork for a formula that is not released warn or block?**
  - A good answer: The map is internally inconsistent: its stage target says it warns, its weak-link list and gap list say it blocks or allows no QA acceptance. Report both statements, attributed; do not infer either gate.
- **Is batch release blocked until both label forms are signed?**
  - A good answer: The map's stage target and final business rule propose a missing-evidence soft warning with acknowledgement and the releasing actor recorded; its gap list still says release is gated on both forms. The SOP requirement for prior QA sign-off is a separate rule. Do not infer a blocking gate.

### Tracing real records

- **Which proof version did the customer approve for this pack component, through which channel, and who recorded it?**
  - A good answer: Read the customer sign-off bound to the exact proof attachment and version, with the customer as approver, the channel (email or DocuSign) and the recording staff member kept separate. A sign-off never carries over to a newer proof version, and customer approval is distinct from QA acceptance and Regulatory review. While the sign-off records are unreadable, report a gap, never 'approved'.
- **Which printed component P-number was minted for this artwork version, and which item did it replace?**
  - A good answer: Read the proof-version-to-item record (minted item, previous item) and name both items with provenance. Without that record the lineage is a gap: never match a P-item to an artwork version by description, name or date coincidence.
- **Which artwork version did this purchase order line order, and which version did the receipt and its incoming QC check against?**
  - A good answer: The Portal PO line, receipt line and incoming QC record carry artwork version columns, but the reporting API serves the PO and receipt lines without them and does not serve incoming QC. Answer with the keys tried and name the gap. A version named in the PO text or the supplier email is a candidate, never a link.
- **For this AO, which label lot and artwork version were issued, and does received equal used plus rejected plus returned?**
  - A good answer: Use the AO's label reconciliation lines (Form #013B), excluding voided lines; quantities are Decimal with their unit, and the balance is computed only from lines read. FileMaker AO items give packaging and lot numbers, not a reconciliation. While the reconciliation lines are unreadable, report the gap; never present a balance from the AO packet alone as reconciled.
- **Did QA sign off the label verification before this AO started, and who released the batch?**
  - A good answer: Compare the QA sign-off time with the AO start time and name the releasing actor, each with its source. A missing timestamp leaves the order of events open; never infer it. The map's own example of a late QA sign-off is a document claim, not inspected evidence.
- **Which formula and ingredient list was this artwork version checked against, and is that basis stale?**
  - A good answer: Read the proof version's pinned formula and ingredient-list basis (source, version, checked-by) and its stale or superseded state. Without that record, say the basis is unknown; never match an ingredient-list document to an artwork version by product name.

### Understanding the process

- **Which label problems does the map report from QA, and are they verified?**
  - A good answer: List the map's examples (obsolete labels, mismatched stock, a late QA sign-off, a missing printed-component verification) as document claims, not independently inspected evidence. Point to the supplied originals that would support each only as unread sources.

### Tracing real records

- **Can a newly minted P-item flow through receiving, reorder and AO today?**
  - A good answer: Attribute to the map: Portal-only new P-items cannot flow end to end because receiving, reorder and AO paths still resolve through FileMaker packaging, supplier and AO items; the target is Portal-owned identities and foreign keys. A check names the keys tried in Portal products and FileMaker packaging; an item absent from FileMaker is 'not found under the keys checked'.

### Understanding the process

- **Is the customer-approved artwork version carried on PO, receipt and QC (TASK-2627) done?**
  - A good answer: The sources conflict: the map marks TASK-2627 in progress, and a COS message the same day reports it complete. Neither status nor the deployed code was checked, and the map's code citations name a staging revision from Oct 11, not a current one. Report the conflict; never present the task as done.

## Open questions for the business

- the live strength of the artwork QA gate (soft warning with waiver versus blocking)
- whether accepting artwork for an unreleased formula warns or blocks
- whether TASK-2627 is complete and deployed
- the current code revision behind the map's citations

## What the model still needs

| # | Need | Today |
|---|---|---|
|  | ArtworkVersion / Proof (an exact proof attachment version with its approval status and approver) | No curated artwork or proof type; only the generated not_served types RelPublicAppAttachment and RelPublicAppArtworkVersionDecision. |
|  | CustomerApproval (customer sign-off on one exact proof version, channel and reference, recorder kept apart) | No customer-approval object; MCApproval and the not_served RelPublicAppApprovalRecord are generic and carry no proof-version binding. |
|  | QA artwork acceptance and Regulatory review, with the gate strength in force | No QA-acceptance or regulatory-review object; the soft-gate rule exists only as Portal code paths, and no source of the deployed gate setting is known. |
|  | Printed-component lineage (proof version -> minted P-item -> previous item with held lots -> FG revision) | FgItem and the served app_product exist, but no link from a proof version to its P-item, to the item it replaced, or to the FG revision that froze it. |
|  | Artwork version on PO line, receipt line and incoming QC | PurchaseOrderLine and ReceiptLine exist, but the reporting API serves them without artwork_attachment_id / artwork_version, and app_incoming_qc_record is not served. |
|  | AO label reconciliation (Form #013B lines, QA sign-off, in-process inspection #010, release acknowledgement) | AssemblyOrder exists, but no label-reconciliation, sign-off or release-acknowledgement object; only the generated not_served types RelPublicAppAoLabelReconciliationLine, RelPublicAppAoLabelVerificationSignoff and RelPublicAppAoQcReleaseAck. |
|  | Portal artwork and label relations through the reporting API | None of app_attachment, app_artwork_version_*, app_approval_record, app_ao_label_* or app_ao_qc_release_ack is an approved relation, so no record is readable. |
|  | Artwork version to formula / ingredient-list basis | No link from an artwork version to the formula version and ingredient list it was checked against; only the generated not_served RelPublicAppArtworkVersionBasis. |
|  | Portal P-item to FileMaker packaging and BC item identity | No crosswalk from a Portal-minted P-item to a FileMaker packaging id or a BC item; the map reports new P-items cannot flow through FileMaker-resolved receiving, reorder and AO paths. |
|  | BC AO component consumption of printed labels | The GET-only BC grammar serves items and itemLedgerEntries but names no assembly-order entity set, so AO consumption of a label lot is unreadable as such. |
|  | Digital Form #008 capture | The map's target captures Forms #013B, #010 and #008; Portal has tables for #013B and #010, and no #008 table was found. |
|  | Open question from the Artwork Handoff cases: artwork-department owner | Not in the ontology; named by the handoff request, whose originals are unread here (read receipt pending). |
|  | Open question from the Artwork Handoff cases: die-line owner | Not in the ontology; named by the handoff request, whose originals are unread here (read receipt pending). |
|  | Open question from the Artwork Handoff cases: legacy proof attachment | Not in the ontology; named by the handoff request, whose originals are unread here (read receipt pending). |
|  | Open question from the Artwork Handoff cases: print-ready artifacts | Not in the ontology; named by the handoff request, whose originals are unread here (read receipt pending). |

## Systems it reads

business-central, portal, portal-reporting-api

## Out of scope

- Portal or BC writes
- approving, waiving or releasing anything
- choosing between the map's inconsistent statements
