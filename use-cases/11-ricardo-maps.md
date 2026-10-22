# 11. Operations maps (Ricardo)

Receiving, QC, release and complaints, traced through real examples.

## Questions it must answer

### Understanding the process

- **What do Ricardo's three October 2 process-map packages cover, and what kind of evidence are they?**
  - A good answer: Attribute to Ricardo Savelli Fuzito: ontology-file-intake, order-lifecycle-cost and po-delivery-window under Operations/Ricardo/process-maps, each as HTML, CSV, ontology JSON and spec.json (12 files), with 21, 18 and 15 stage records agreeing across formats. They are process documents: the originals they describe were retrieved separately, not embedded, and Claude-host retrieval of each exact original is unverified. Packet replay is not live original retrieval.

### Answering the real case

- **Is QRM-26037 approved and released?**
  - A good answer: Missing outcome: the sources do not establish signed approval or release. Report only that the populated DOCX XML and three PDFs distinguish liquid RMP-0425 from granulate, the recorded risk is 4/25 (likelihood 2 x severity 2), the signature and date fields are blank and the SDS is only page 3/7. Name the approval or release record as absent; do not infer it.
- **Was Dandylion's October 2 missing-inventory complaint on P-02289 (customer reference PO-00269) resolved, and was the inventory posted?**
  - A good answer: Missing outcome: resolution and inventory posting are unverified. Report that a 31-row/29-box workbook and 2485.pdf support the receipt-confirmation communication, that September 30's sent attachment does not resolve the October 2 complaint, and that the component join and handwriting remain unverified. Keep the customer reference PO-00269 distinct from P-02289.
- **Was PO5455 actually received, and is the ingredient ES-2 or Thamesurf SLES-30?**
  - A good answer: Missing outcomes: actual receipt is unverified and ES-2 versus Thamesurf SLES-30 is an unresolved alias; do not merge the two names. Report that mail and PDF support a September 18 expected arrival and 10 totes / 10,000 kg.
- **When did PO-00349 (MIDAS2) arrive?**
  - A good answer: Missing outcome: real arrival is not established. Report that mail and slip 228524 support a September 15 shipment and 375 kg while the slip's August 26 expected delivery conflicts; present both dates and do not repair the ETA.
- **For received material, what was the inspection result, was it accepted, rejected or sent to rework, who signed, and what was the stock disposition?**
  - A good answer: Missing outcome: inspection result, accept/reject/rework decision, signer and stock disposition are missing from the ontology and the sources (stakeholder_description_claim, not independently verified; Ricardo's outcome review, evidence cutoff Oct 1, no fresh source queries). Name each as absent; do not infer a result, decision, signer or disposition.

### Tracing real records

- **What does the F-18-0025 helper establish about cost?**
  - A good answer: Report that the helper at 3d15ff87 records SLES 8% to 9% changing the contribution from 0.1184 to 0.1332, and that the three rows cover 58.79% of mass. State that this is not full cost and not a quote-to-cash outcome.

### Understanding the process

- **Which identities must stay distinct when answering the Ricardo anchors, and what is not yet observed?**
  - A good answer: Keep customer reference, ownership, receipt authority, QC release and accounting distinct. TASK-2299, TASK-2275 and TASK-2276 are documented prerequisites, not observed completion. The interim at-item-cost option is not an approved off-book rule. The communications-hub design (PR1791 documents at Portal staging 59b05489) is not implementation proof.

## What the model still needs

| # | Need | Today |
|---|---|---|
|  | QC release decision for a risk assessment (QRM-26037: signer, date, released or not) | No record of a signed approval or release; the read QRM-26037 fields are blank. |
|  | Purchase order to actual receipt (PO5455, PO-00349/MIDAS2) | Mail, PDF and slip support expected or shipped dates only; no receipt is linked. |
|  | Alias between ES-2 and Thamesurf SLES-30 | The alias is unresolved in the sources; no equivalence record exists. |
|  | Customer complaint to resolution and inventory posting (Dandylion P-02289 / PO-00269) | The complaint and receipt-confirmation mail are read; no resolution or posting is linked, and the component join is unverified. |
|  | Retrieval of the exact anchor originals (QRM files, Receiving messages, attachments) | Source inspection read four QRM files, eight messages and four attachments; Claude-host retrieval of each exact original is unverified. |
|  | Receiving, QC and release crosswalk (inspection result, accept/reject/rework disposition, signer) | No record links a receipt to an inspection result, a disposition or a signer. Evidence label: stakeholder_description_claim, not independently verified,. |

## Systems it reads

unverified

## Out of scope

- Portal or BC writes
- repairing a contested date or resolving an alias
- treating packet replay as live original retrieval
