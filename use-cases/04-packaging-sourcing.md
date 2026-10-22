# 4. Packaging sourcing

From a packaging quote to an approved part, PO, proforma invoice and deposit.

## Questions it must answer

### Understanding the process

- **What is the New Packaging Sourcing proposal, and what kind of evidence is it?**
  - A good answer: Attribute to the proposal (a Markdown file for review in /Ontology UseCases, pinned by item id and SHA-256, captured 16 Oct 2026): a Ross-attributed walkthrough of how a new packaging item should be sourced, with eleven target stages confirmed on 16 Oct 2026 and six proposed business rules P1 to P6 put to Vince (proposed, pending Vince). Say that approval of the rules is not evidenced, that its introduction counts five rules while its table lists six, that its six build tasks are proposals, and that its Portal and BC references come from a system map last checked on 30 Sep 2026 and need code verification, so every Portal or BC statement in it is unverified. Call it a stakeholder proposal, not a decision, an implementation instruction or authority to act.
- **Which business rules does the proposal put to Vince, and what is their status?**
  - A good answer: Name the six as the proposal's (proposed_rules): P1 three sign-offs before a packaging quote is approved; P2 part creation at approval with the order prerequisites; P3 linking the part to its project; P4 allocating cost to the component that carries it; P5 who requests and records a vendor deposit; P6 a PO before the vendor PI and the PI matched before payment. Each is proposed, pending Vince: an answer never describes any of them as decided, as current practice or as built, and notes the five-versus-six count mismatch in the proposal.
- **What quote evidence does the proposal require from a packaging vendor?**
  - A good answer: Per the proposal: currency, MOQ, lead time, INCO term with its named place, country of origin, the vendor's item number and the case-pack, with the drawing and sample evidence behind the quote, for each component. Per the Sept 30 map (unverified): a supplier item holds cost, price tiers, MOQ, lead time, INCO terms, origin and one delivery cost; a named place and a case-pack are not named there. An answer keeps the proposal's requirement apart from what the Portal holds and gives no figures.
- **Which sign-offs does the proposal require before a packaging quote is approved, and where are they recorded?**
  - A good answer: The proposal requires customer, QC and costing sign-offs (P1, proposed, pending Vince). It does not evidence an approval, and where each sign-off is recorded today is unknown (PS-GAP-01). An answer names the three roles, not people, and reports the record as unknown rather than inferring it from a sample status.

### Tracing real records

- **When does the proposal have the packaging part created, and what can the Portal do today?**
  - A good answer: The proposal: the part is created at approval from the approved quote and linked to its project (P2 and P3, proposed, pending Vince). The Sept 30 map (unverified): linking a sourcing request needs an accepted sample, a drawing and an existing packaging item; the Portal has no way to create a new packaging item and no P-#### number series. An answer keeps the target, the map's claim and the unchecked code apart.
- **What must exist before a packaging part may be ordered, and does the purchase order carry the HS code?**
  - A good answer: The proposal: a drawing and an HS code before the part is ordered (P2, proposed, pending Vince). The Sept 30 map (unverified): the HS code is set on the product when the request is linked, purchase order lines carry no HS code and none is sent to BC. An answer says whether a given part has an HS code only from public.app_product and never claims the PO line or BC carries it.
- **How should prices be recorded when the components of one packaging item ship separately?**
  - A good answer: The proposal: a price per component when the parts ship separately, and the cost allocated to the component that carries it (P4, proposed, pending Vince). The Sept 30 map (unverified): a supplier item carries one cost with price tiers per supplier and product. An answer describes the price structure (per component, currency, unit, tier) and states no price figures.
- **What must exist before a vendor proforma invoice, and what is checked on it before payment?**
  - A good answer: The proposal: a PO is issued before the vendor's PI, and the PI's price, quantity and terms are checked against the PO before anything is paid (P6, proposed, pending Vince). The Sept 30 map (unverified): the PO has no PI number field; BC purchaseInvoices are posted invoices, not proforma invoices. Where a PI is kept and who checks it is unknown (PS-GAP-06, PS-GAP-07). An answer never treats a posted BC invoice as the PI.
- **How does the proposal want vendor deposits handled, and what exists for them today?**
  - A good answer: The proposal: purchasing requests the deposit, with the account manager stating urgency, and it is recorded in BC as a prepayment on the purchase order (P5, proposed, pending Vince). The Sept 30 map (unverified): vendor prepayment is not built; BC Prepayment % on the PO is never set; AP handles deposits in BC by hand; OrderPrepayment is customer (AR) only. The Workbench's purchaseOrders allowlist has no prepayment field and vendorPayments apply to invoices, so an answer reports a PO's deposit as unknown.
- **How does an approved packaging part reach a purchase order in the proposal's workflow?**
  - A good answer: Per the proposal: brief, component BOM, vendor RFQs, quote, drawing and sample evidence, approval, handoff to purchasing, part creation, assembly demand, requisition and PO, then the deposit. Per the Sept 30 map (unverified): only the requisition is approved, a PO has no approval of its own and sending it records no send time. An answer traces a part from public.app_product to public.app_purchase_requisition and public.app_purchase_order and reports a step it cannot see as a gap, never a match.

### Understanding the process

- **What does the proposal leave open, and what must a cutover handle?**
  - A good answer: Open per the proposal: whether a label states 100mL or 3.5 fl.oz; which entity purchases and which pays; whether the DDP multiplier counts freight and duty twice; how far the sourcing-request workflow has been used historically. Cutover: packaging item identifiers, open assembly orders and POs, and vendor deposits. An answer lists these as open and does not answer them from the proposal.

### Tracing real records

- **Can a DDP vendor quote be double counted by the landed-cost formula?**
  - A good answer: The proposal raises it as an open question. The Sept 30 map (unverified): the quote-time landed cost is (tier price + duty) times a multiplier plus freight, with freight added per unit, so a DDP price that already includes freight and duty could be counted twice. An answer reports the risk as unconfirmed, computes no landed cost and names the question for Vince (PS-GAP-09).

### Answering the real case

- **How was the P-02486 bottle and cap cost allocated, and what does the proposal correct?**
  - A good answer: The proposal says the cost of the bottle and its cap was assigned to the bottle alone, and proposes allocating it to each component (P4, proposed, pending Vince). The P-02486 item records are not supplied (MO-02), so the allocation is the proposal's claim, unverified. An answer quotes no price and does not reconstruct the allocation from other records.
- **What mismatches does the proposal report on PI-SWC025623?**
  - A good answer: The proposal cites mismatches between PI-SWC025623 and what was ordered; the PI is not supplied (MO-03), so the mismatches and the PO they belong to are unverified. An answer attributes them to the proposal, lists no amounts and treats a PI-to-PO join as unseen.
- **Which vendor deposit does the proposal cite, and was it recorded as a prepayment on the purchase order?**
  - A good answer: The proposal cites a vendor deposit; the deposit record is not supplied (MO-04), and the Sept 30 map says deposits are booked in BC by hand without a PO prepayment (unverified). An answer reports whether it was recorded as a prepayment as unknown and states no amount.
- **Which originals of this case exist, and which have been read?**
  - A good answer: Pinned: the proposal file (item id, SHA-256 and size from the advisory record), watched in /Ontology UseCases; its body was read by the intake, which exported minimised facts only, and the discovered receipt of 17 Oct 2026 lists the item with its body unavailable, so nothing here was read from it. Not supplied (missing_originals): the fuller map the proposal links (not fetched), the P-02486 item records, PI-SWC025623, the deposit record and the three sign-offs. An answer lists each unread original with its state and infers nothing about its content.

## Open questions for the business

- which of P1 to P6 Vince accepts, and whether the proposal means five rules or six
- where the customer, QC and costing sign-offs are recorded today
- where a vendor PI is kept and who checks it against the PO before payment
- whether a DDP quote is counted twice by the landed-cost multiplier
- which entity purchases and which pays for packaging
- whether the Sept 30 map's Portal and BC statements still hold in the code

## What the model still needs

| # | Need | Today |
|---|---|---|
|  | SourcingApproval (one approval of a packaging quote with its customer, QC and costing sign-offs: who, when, on which quote) | No approval or sign-off object for a packaging quote; the sourcing request is a Portal model outside the reporting API. |
|  | Product.created_from_quote and Product.project (the packaging part created at approval from the approved quote and linked to its project) | Product is served; no member names the approved quote or sourcing request it came from, or its project. |
|  | OrderPrerequisite (a drawing and an HS code present on the part before it may be ordered, and the HS code carried on the PO line) | Product.hs_code is served; no drawing member and no HS code on a purchase order line; Attachment is outside the reporting API. |
|  | ComponentPrice and CostAllocation (a quoted price per component when parts ship separately, and the cost assigned to the component that carries it) | SupplierProduct is served with one cost and tiers per supplier and product; no component split or allocation member. |
|  | QuoteEvidence (currency, MOQ, lead time, INCO term and named place, origin, vendor item number, case-pack, with the drawing and sample behind one vendor quote) | SupplierProduct is served; the vendor responses and samples sit on Portal sourcing models outside the reporting API; no named place or case-pack member is known. |
|  | ProformaInvoice.purchase_order (a vendor PI received only after, and tied to, a PO) | PurchaseOrder is served with no PI member; BC purchaseInvoices are posted invoices, not proforma invoices. |
|  | PIMatch (the PI's price, quantity and terms checked against the PO: by whom, when, result) before payment | No check or match record between a PI and a PO. |
|  | VendorPrepayment (a deposit against a PO: requested by, urgency, amount, paid date, BC prepayment) | OrderPrepayment is a Portal model outside the reporting API; BC Payment is partial and applies to invoices; the purchaseOrders allowlist has no prepayment field. |
|  | LandedCost.basis (whether a quote's price already includes freight and duty, as a DDP quote does, before the multiplier is applied) | SupplierProduct is served with one delivery cost; no member states a quote's INCO basis for costing. |
|  | A read of the originals the proposal names: the fuller map it links, the P-02486 item records, PI-SWC025623 and the deposit record | None is in the case-originals registry or the local store; the fuller map was not fetched. |
|  | A read receipt of the proposal file itself (body read and hashed equal to the pin) | The file is pinned by item id and SHA-256 from the source record; the live check's discovered receipt has no body, so no receipt matches the pin. |
|  | RuleDecision (Vince's decision on each of P1 to P6: accepted, changed or refused, with date and scope) | No decision object; the proposal's rules exist only as its text, pinned through the source record. |

## Systems it reads

bc, portal, portal-reporting-api, sharepoint

## Out of scope

- Portal or BC writes, creating parts, raising POs or recording deposits
- deciding P1 to P6
- computing landed costs or reconciling a PI or a deposit
- fetching the fuller map or any original
