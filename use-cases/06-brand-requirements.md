# 6. Brand requirements

Customer brand requirements and the documents behind each claim.

## Questions it must answer

### Understanding the process

- **What does the Customer Brand Requirements map cover, and what is its status?**
  - A good answer: Attribute to Draft 6 (16 Oct 2026), Laura Abate's Customer Brand Requirements map: regulatory requirements, material-change review and claim substantiation, 12 stages and 15 stated rules. State that the stages and rules are stakeholder proposals, not approved decisions or verified current implementation, and that the draft was made without a portal code audit. A card answer that treats a rule as decided or implemented fails.
- **Who enters a customer's brand requirements and who reviews them, and how is a requirement scoped?**
  - A good answer: State the map's proposal (not decided): requirements are scoped to customer, brand/product line and product exceptions, with category, source document, date, contact and completeness/currentness; account managers enter them and Regulatory reviews or is notified. Attribute to the map; say whether the Portal holds this today is unverified.
- **What does the map say a compliance document is attached to, and what must it record?**
  - A good answer: State the proposal: compliance documents attach to supplier-specific raw-material R numbers, with type, supported claims, issue/expiry dates and reviewer. Attribute to the map; do not state that the CoA fields already hold the claims or reviewer.
- **What does the map say happens when a supplier or material is switched on a product?**
  - A good answer: State the proposal: a supplier/material switch opens a Regulatory review and notifies Quality, including customer-supplied materials, and does not block the PO. Attribute to the map; the answer must not say the PO is blocked.
- **Which document conditions does the map say gate a batch, and which do not?**
  - A good answer: State the proposal: documents must be reviewed before batching; expiry alerts alone do not block batching. Keep the difference between a reviewed document and an expired one and attribute to the map; not an approved rule.
- **What does the map say about formula and artwork approval versions and about claim-affecting changes?**
  - A good answer: State the proposal: pending and final formulas need ingredient lines, allergen notes and supersedes links; derived substantiation is claim by product by material; formula and artwork approvals bind exact versions; claim-affecting material changes reopen formula approval; a pre-production Regulatory review is proposed; CNF tracking is informational and case pack and best-before format are out of scope. Attribute to the map.
- **What is the Three Ships example in the map, and how reliable is it?**
  - A good answer: Report it as an attributed Regulatory account: a supplier switch is said to have introduced animal-derived content into a vegan product, found through a later RSPO-document request, with approximate dates and no supporting source bundle. It is not an independently verified incident; the answer must not state it as fact or name a date as exact.
- **What do the map's own consistency checks show, and are they test results?**
  - A good answer: State that the companion data model has 12 states, 15 rules, 17 records, 11 relations, 8 identifiers and 5 checks, four marked fail and one open by the draft. These are source assertions by the draft, not fresh runtime evaluations; the answer must not report them as a run of this product.

### Tracing real records

- **Which brand requirements apply to customer X, brand Y and product Z, and are they current?**
  - A good answer: Join Customer, brand/product line, product exceptions and the requirement rows, with date, source document and completeness. Give each requirement with its source and date, or a gap naming the missing link; never a requirement from a different customer or brand, and no currentness claim without a date.
- **Which compliance documents on raw-material R numbers support claim C, and when do they expire?**
  - A good answer: Join claim C to the products and materials that make it, the R numbers and supplier-specific documents with type, supported claims, issue and expiry dates. Name each document with its dates and source, or a gap; a CoA is not a claim-support document unless a record links it.
- **Did a supplier or material switch on product P open a Regulatory review and notify Quality?**
  - A good answer: Trace the product's supplier-product change, the Regulatory review it should open and the Quality notification. Report the review and notification if they exist with their records, else the missing link as a gap; no inference that one occurred from the switch alone. The PO is not blocked by the switch.
- **May batch B run, given the review state and expiry of its materials' compliance documents?**
  - A good answer: Trace batch B to its materials, their compliance documents and review status. State per material whether the document is reviewed; an expiry alert is shown but does not by itself block. Where a document, review or link is unseen, report the gap and make no release decision; the product does not release a batch.
- **Which formula and artwork versions are approved for product P, and which versions did they supersede?**
  - A good answer: Trace the approved formula and artwork versions with approval record and supersedes chain. Name each by exact version and approver, or the gap; an approval binds one exact version, never 'latest'.

### Answering the real case

- **Which claims does a material change on product P reopen for formula approval?**
  - A good answer: Can the product answer today: list the claims the changed material supports (claim by product by material) and whether formula approval is reopened. Expected: abstain with the named gaps (claim substantiation, material-change link, reopen rule) unless they exist; never present the map's proposed rule as a fact.
- **Was the Three Ships supplier switch reviewed, and was Quality told, before the product shipped?**
  - A good answer: Can the product answer from the originals of the Three Ships case today: no, the account has no supporting source bundle. Expected: abstain, attribute the account to Regulatory, and list the missing originals; do not confirm or date the incident.

## Open questions for the business

- whether the Portal's customer requirement rows carry the map's scope, category, source document and contact
- what 'reviewed' means for a document before batching, and who reviews
- whether the Three Ships dates and documents exist in a source

## What the model still needs

| # | Need | Today |
|---|---|---|
|  | CustomerBrandRequirement (a requirement scoped to customer, brand/product line and product exceptions, with category, source document, date, contact, completeness/currentness) | Customer and Product exist; the Portal's customer and project requirement tables are not_served and unread for the map's fields. |
|  | Requirement.applies_to (brand/product line and product exceptions resolved to one product) | Product carries no brand-line scope link and no requirement link. |
|  | ComplianceDocument on a supplier-specific raw-material R number (type, supported claims, issue and expiry dates, reviewer) | CertificateOfAnalysis holds product, lot, dates, supplier and test table as a supplier statement; it has no document type for a claim, no supported-claim list and no reviewer. FM raw material (R number) is served. |
|  | ComplianceDocument.supports_claim and the derived claim by product by material substantiation | No claim object or claim link exists; the Portal formula and ingredient tables are not_served. |
|  | RegulatoryReview (opened by a supplier or material switch, with Quality notified) | Portal ApprovalRecord exists but is not_served; no review or notification object is modelled. |
|  | Product.supplier_material_switch (the change event of a product's supplier or material, with date) | SupplierProduct is served as the current link; no change history or event exists. |
|  | Batching gate: document review status and document expiry as separate conditions, reusing the lot restriction and expired-disposition findings | Lot and its expiry exist (not_served in the Portal); expired-disposition gaps EMD-GAP-01 (restricted class, LotRestriction) and EMD-GAP-09 (lot allocated to a batch) already name the lot-side links. No document review status exists. |
|  | FormulaVersion (pending or final, ingredient lines, allergen notes, supersedes link) | The Portal Formulation and FormulationIngredient are not_served; no supersedes link or allergen note is confirmed. |
|  | Approval.binds_version (formula and artwork approval to one exact version), reusing the artwork lifecycle ArtworkVersion and CustomerApproval gaps AL-GAP-01 and AL-GAP-02 | ArtworkVersionDecision and ApprovalRecord exist in the Portal (not_served); no formula approval binds a version. |
|  | MaterialChange.reopens_formula_approval (a claim-affecting change reopens approval; a pre-production Regulatory review is proposed) | No change object, claim link or reopen rule exists. |
|  | The map's own body: 12 stages, 15 rules, 17 records, 11 relations, 8 identifiers and 5 checks (four marked fail, one open) | Only the source record's summary is read here; no read receipt of the four members exists, so no numbered stage or rule is quoted. |
|  | The Three Ships source bundle (switch, product, RSPO-document request, dates) | No source is held; the account has approximate dates and no supporting bundle. |

## Systems it reads

graph_mail, portal, portal-reporting-api

## Out of scope

- Portal, BC, Sage or SharePoint writes
- opening a Regulatory review, notifying Quality or releasing a batch
- deciding that a claim is substantiated
