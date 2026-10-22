# 1. Quote to order

Quote → customer PO → sales order in BC, or a lost quote.

## Based on

- Quote-to-Order Map
- Quote Examples: _Quote Workbooks originals
- User journey for cost workflows (cost to assembly)

## Worked examples

- **A. Sahajan 30 mL minis quote (Sep 2026)**: open: Sahajan had not decided as of 15 Oct 2026
  - line, speeds and crewing: assumed (seeded from another item, not actuals)
  - oil specific gravity: assumed (left at the template default)
  - primary packaging owner: assumed (assumed customer-supplied)
  - fragrance cost: assumed (assumed customer-supplied, priced at zero)
  - formula stability: missing (two formulas still in stability testing; prices indicative)
  - target price: missing (not in the brief)
  - annual forecast: missing (not in the brief)
  - deposit percentage: missing (not stated)
- **B. Sahajan full-size products (Feb to Oct 2026)**: won: customer POs, then the Manufacture and Supply Agreement, then Sage orders and pilot invoices
  - won tier per SKU: missing (not recorded on the order)
  - payment terms: unclear (changed between quote, agreement and orders)
  - quote validity: unclear (printed window disagreed with the actual one)
- **C. G2 Beauty 12-piece body wash display (Sep to Oct 2026)**: open: price given by email, no formal quote; G2's decision still to come
  - bottle vendor quote: missing
  - formula cost per kg: missing
  - display (PDQ) cost: missing
  - incoterms: missing
  - payment and deposit terms: missing
  - quote validity: missing
  - minimum order quantity: missing
  - price at the largest asked volume: missing
- **D. Soapbox gallons, refill bottles and pumps (Feb to Oct 2026)**: won: Supply Agreement 14 Sep 2026, customer POs 18 Sep 2026; sales invoices still to come
  - refill bottle vendor price at the top tier: missing (the component sold below its later landed cost)
  - pump and bracket vendor prices: missing
  - mould costs: missing
- **E. Vince's quoting template as used for G2 (Oct 2026)**: issue date entered, no quote sent
  - formula cost: assumed (typed as a placeholder while the formula sheet reads MISSING)
  - won-tier field: missing (the template has none)

## Questions it must answer

### Understanding the process

- **What does the Quote-to-Order Map cover, and what kind of evidence is it?**
  - A good answer: Attribute to the map (Vince's artifact, exported 15 Oct 2026): 22 stages from the brief to a won order in Business Central or a lost quote, five worked examples A to E and 16 ranked gaps. Its Portal code readings are of staging at an unstated commit and were not rechecked by it. Call it a stakeholder use-case synthesis, not a verified report of deployed systems.

### Tracing real records

- **How is a lost quote recorded today, and when does a quote expire?**
  - A good answer: Keep three views apart, each attributed: as practised, lost quotes are not recorded (Vince, 15 Oct 2026); as built, a Portal quote can be REJECTED with an optional free-text reason or marked EXPIRED by hand, with a default validity window set at create; as targeted by the map, a reason from a set list and automatic expiry 30 days after issue. A count of lost quotes comes only from read quote rows.
- **When an accepted quote converts to an order, which volume tier and which currency does the order carry?**
  - A good answer: As built, conversion accepts only ACCEPTED quotes and copies every quote line, so every asked-volume tier, onto the order; the map also reports that the quote's currency is dropped. The target is the won tier only, in the quote's currency, with the quote reference on the order. A tier match is shown only from read quote-volume and order lines; an unseen join is a gap, never a match.

### Answering the real case

- **For the Sahajan 30 mL minis quote (example A), is there a Business Central sales order or a recorded lost reason?**
  - A good answer: As of the map (15 Oct 2026) Sahajan had not decided: the map names neither a Business Central sales order nor a lost reason. The expected answer is still open, with no BC order and no lost reason found, after reading the Portal quote and order rows for the customer and BC salesQuotes and salesOrders. A record not found is reported as not found under the keys checked (customer, quote number, customer PO), never as lost; a quote past its validity with no reason is expired without a recorded reason, never a lost reason.

### Understanding the process

- **Which quote inputs were missing or assumed when the Sahajan minis quote was priced?**
  - A good answer: Name each input of examples.A.missing_at_quote as missing or assumed, attributed to the map's reading of the workbook notes; never fill one with a value. The workbook's READY TO QUOTE result does not make an assumed input known.

### Answering the real case

- **For the Sahajan full-size products (example B), which Business Central sales order carries the won quote?**
  - A good answer: The map traces the win through customer POs (12 and 17 Jun 2026), the Manufacture and Supply Agreement (effective 18 Jun 2026) and orders keyed by hand into Sage; it names no Business Central sales order, and the invoice for the first production run is still to come. The expected answer is won, with the BC order unknown until read from BC salesOrders for the customer; the won tier per SKU is recorded nowhere and is reported missing. A record not found is reported as not found under the keys checked (customer, quote number, customer PO), never as lost; a quote past its validity with no reason is expired without a recorded reason, never a lost reason.

### Understanding the process

- **Which terms of the Sahajan full-size quote changed between the quote, the contract and the orders?**
  - A good answer: Attribute to the map: payment terms moved from the quote and the agreement to COD on the Sage orders and changed again later; the printed validity disagreed with the actual window; the won tier per SKU was not recorded. Report each as a difference between named documents and never choose one as correct.

### Answering the real case

- **For the G2 Beauty body wash display (example C), is there a Business Central sales order or a recorded lost reason?**
  - A good answer: The map reports a price given by email before any cost existed, no formal quote and no terms; G2's decision (stages 13 to 18) is still to come. The expected answer is open: no formal quote, no BC sales order and no lost reason. A price in an email is not a quote. A record not found is reported as not found under the keys checked (customer, quote number, customer PO), never as lost; a quote past its validity with no reason is expired without a recorded reason, never a lost reason.

### Understanding the process

- **Which quote inputs were missing when G2 Beauty was given a price?**
  - A good answer: Name each input of examples.C.missing_at_quote as missing, attributed to the map; a later vendor quote or formula cost does not make it known at quote time.

### Answering the real case

- **For Soapbox (example D), which Business Central sales order fulfils the won supply agreement, and has it been invoiced?**
  - A good answer: The map reports a Supply Agreement dated 14 Sep 2026 and customer POs dated 18 Sep 2026; it names no Business Central sales order, and sales invoices to Soapbox are still to come. The expected answer is won, with the BC order unknown until read and no invoice yet. A customer PO is not a BC order. A record not found is reported as not found under the keys checked (customer, quote number, customer PO), never as lost; a quote past its validity with no reason is expired without a recorded reason, never a lost reason.

### Understanding the process

- **Which component prices were missing when Soapbox was quoted?**
  - A good answer: Name each input of examples.D.missing_at_quote as missing, attributed to the map. A component PLX supplies with no vendor price is a missing input, never a zero cost.

### Answering the real case

- **Was the G2 quote built in Vince's quoting template (example E) sent, and did it end in a Business Central sales order or a recorded lost reason?**
  - A good answer: The map reads an issue date in the template but finds no quote sent, so there is no BC sales order and no lost reason. The expected answer is not sent, with neither outcome. A record not found is reported as not found under the keys checked (customer, quote number, customer PO), never as lost; a quote past its validity with no reason is expired without a recorded reason, never a lost reason.

### Understanding the process

- **Which inputs were missing in the G2 quoting template while it reported ready to quote?**
  - A good answer: Name each input of examples.E.missing_at_quote, attributed to the map's read of the template: a readiness result of READY TO QUOTE next to a formula sheet that reads MISSING is a contradiction to report, not a ready quote.
- **Which pinned workbook revisions belong to each quote example, and which one is the quote of record?**
  - A good answer: Identify a workbook only by name, item id and SHA-256 from a read receipt that matches source.watched_originals; two files with one name are distinct revisions. Which revision is the quote of record is not stated anywhere read and stays unknown until a person answers.

### Tracing real records

- **When was each example's quote or price sent, and by whom (by role)?**
  - A good answer: Answer from the pinned emails of the three folders only after they are read through scripts.case_originals read; record the date and the sender's role, never a body. The three _summary.md files are folder summaries, not quotes.

### Understanding the process

- **What does the cost-to-assembly user journey describe, and is it a record of transactions?**
  - A good answer: Attribute to the journey (ZIP version 1.0 in AGENTS/Ontology UseCases): 29 steps across six roles, from standard costs and line setup through the quote and finance review to assembly and batch cards and the BC posting. It is a design artifact, not evidence of any transaction.

### Tracing real records

- **Which quote cost inputs does the journey take from standard costs, and which of them were missing in examples A to E?**
  - A good answer: Join each journey cost step to examples.*.missing_at_quote by input kind; an input absent from both is unknown. Planned conversion per unit is not a served column, so a value for it needs a reader first.

## Open questions for the business

- which workbook revision is the quote of record for each example
- which BC sales order, if any, carries the Sahajan full-size and Soapbox wins
- which overhead standard applies to quotes issued now (ERP-D-022 or OD-14)

## What the model still needs

| # | Need | Today |
|---|---|---|
| 1 | Bring every cost input into the portal (QuoteCostInput (one cost input of a quote: kind, source record, as-of date, owner)) | Quote and QuoteLine exist; a line's standard cost is a soft custom_fields value with unknown currency and denominator, and no cost input carries its source. |
| 2 | Stop sending quotes to BC; send the order with its quote reference (ERP-D-014) (BC salesOrder -> Portal quote (the quote reference carried on the BC order)) | Quote.converted_to links a Portal quote to a Portal sales order through custom_fields; SalesOrder.same_record_as reaches BC, but nothing links a BC order back to a quote, and BC salesQuotes are unbound. |
| 3 | Keep quoted tier prices as the customer's price agreement (CustomerPriceAgreement (won tier per SKU, currency, validity, source quote)) | No price agreement type; asked volumes (app_quote_volume) are not served and nothing keeps a won tier after the first order. |
| 4 | Hold supply agreements and carry terms to the order (SupplyAgreement (MSA or supply agreement: price period, deposit rule, payment terms, notice period)) | No agreement type; customer and order carry no agreement reference. |
| 5 | Never quote a PLX-supplied component without a vendor price (Reader of vendor tier prices (app_supplier_product_tier) at quote time) | RelPublicAppSupplierProductTier is generated as not served; no reader returns the vendor price a quote used. |
| 6 | Build workbacks in the portal (WorkbackSchedule (dated steps back from the ship date for one quote or project)) | No workback type; ProjectTask is generated as not served and carries no quote link. |
| 7 | Excel round-trip covers the whole quote with evidence (Quote workbook revision (name, item id, SHA-256) -> Portal quote revision) | No link from a quote to the workbook it was priced in; the Portal round-trip covers the volume matrix only. |
| 8 | Agent turns emails into proposed portal updates (Reader of quote emails (sent date, sender role, attachment SHA-256) into proposed quote updates) | No quote email reader; the pinned emails are hashed but their bodies are not assessed. |
| 9 | Petra standard labour and overhead (ERP-D-022 vs OD-14) (Standard labour and overhead rate version used by one quote line) | QuoteLine planned conversion per unit exists in the schema but is not a served column; no rate version is linked. |
| 10 | Convert only the won tier, in the quote's currency (Won QuoteVolume -> SalesOrderLine) | Quote.converted_to links the whole quote to one order; conversion copies every quote line. |
| 11 | New customers and FG items reach BC (Sage code) (Customer and finished-good item -> BC customer and item (same record) at conversion) | SalesOrder.same_record_as reaches BC; app_bc_entity_map is not served, so a customer or item never pushed to BC is not visible as missing. |
| 12 | Record lost quotes properly (LostQuote outcome (reason from a set list, competitor, note, date; or automatic expiry)) | Quote has status REJECTED or EXPIRED and a free-text rejection_reason; no reason list, no expiry event. |
| 13 | Payment terms, deposit % and incoterms as fields (OD-3) (Quote commercial terms as fields (payment term, deposit percentage, incoterm)) | Quote.terms is free text; deposits received are app_order_prepayment rows, not served and tied to orders, not quotes. |
| 14 | Sending, versions and approval gate (QuoteRevision with its send event and approval (who, when, at what margin)) | Quote has one status and no revision, sent-at or approver. |
| 15 | Quote on recorded assumptions and fix costing inputs (QuoteAssumption (input missing or assumed at quote time, its kind and owner): the slot for the quoting-with-incomplete-information case) | The ready-to-quote census derives blockers and warnings at read time; no assumption is stored with the quote. |
| 16 | Take quoting off FileMaker and Sage (Reader of FileMaker quote layouts and Sage order entry for these customers) | The ontology states quotes exist only in the Portal (no FM, Sage or BC writer); Sage orders keyed by hand are not reachable. |

## Systems it reads

business-central, portal, portal-reporting-api, sharepoint

## Out of scope

- Portal, Sage or BC writes
- sending, approving or expiring a quote
- choosing between the map's as-practised, as-built and target statements
