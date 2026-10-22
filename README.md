# PLX Ontology

One place to ask questions about PLX's business (customers, orders, suppliers, lots,
documents) and get an answer with its source. It only reads; it never changes anything.

## How to use

1. Open a terminal in this folder.
2. Type `python3 -m operator_ui.case_console` and press Enter.
3. Open the web address it prints.
4. Pick a case or a customer, order or lot, and read the answer. Click a source to check it.
5. If it says **missing** or **blocked**, the data isn't there yet. It never guesses.
6. Something wrong or missing? Add a line to [`context/FEEDBACK.md`](context/FEEDBACK.md).

Full guide: [`context/HOW-TO-USE.md`](context/HOW-TO-USE.md).

## What's new

- **v0.2:** this page, a simple how-to guide, release notes and one feedback log.
- **v0.1:** first version: the business model, the console and the use cases below.

All releases: [`context/RELEASE-NOTES.md`](context/RELEASE-NOTES.md).

## Use cases

Each use case is a real PLX process. For each one: the questions still open with the
business, and what the model still needs, in priority order where the process owner
ranked it. In every case it only reads: it never writes to Portal, BC, Sage or
SharePoint, and never approves, sends or releases anything.

### 1. Quote to order

Quote → customer PO → sales order. Based on Vince's Quote-to-Order map, the quote
workbook examples and the cost-to-assembly user journey.

Open questions:
- Which workbook revision is the quote of record for each example?
- Which BC sales order, if any, carries the Sahajan full-size and Soapbox wins?
- Which overhead standard applies to quotes issued now (ERP-D-022 or OD-14)?

What it needs (ranked):
1. Bring every cost input into the portal
2. Stop sending quotes to BC; send the order with its quote reference
3. Keep quoted tier prices as the customer's price agreement
4. Hold supply agreements and carry terms to the order
5. Never quote a PLX-supplied component without a vendor price
6. Build workbacks in the portal
7. Excel round-trip covers the whole quote with evidence
8. Turn emails into proposed portal updates
9. Petra standard labour and overhead (ERP-D-022 vs OD-14)
10. Convert only the won tier, in the quote's currency
11. New customers and finished-good items reach BC
12. Record lost quotes properly
13. Payment terms, deposit % and incoterms as fields
14. Sending, versions and an approval gate
15. Quote on recorded assumptions and fix costing inputs
16. Take quoting off FileMaker and Sage

### 2. Procure to G/L (packaging and screening)

Supplier PO → receipt → invoice → G/L, using the Packaging Procure-to-GL map and the
Screening Process map.

Open questions:
- Are the ERP-D-015 exception for screening blanks and the screening clearing account approved?
- Which BC document converts a blank into a screened item?
- Which proforma invoice sets the packaging order price and deposit, in which currency and unit?
- Can BC show the link between G/L entries and item ledger entries?

What it needs:
- A screening job record: blank and screened SKUs, quantities, screener, artwork, proof, counts, transfers, conversion, invoice, G/L
- The screen-proof approval and the accepted and rejected counts
- The blank-to-screened conversion step and how its cost rolls up
- The approved cost policy for screening blanks and the clearing account
- Each screened SKU linked to the blank it is made from
- G/L postings traced back to the receipt or conversion that caused them
- Portal POs and receipts matched to their BC documents
- Supplier deposits linked to their PO or proforma invoice
- Transfers of blanks to and from the screener
- Incoming QC records for received packaging

### 3. Screening

Cost and accounting for screened (printed) blanks.

Open questions:
- The actual screening job numbers, the good units per job and the final unit cost per job
- The approved ERP-D-015 treatment for screening blanks and the approved clearing account
- Whether the map's Portal and BC statements hold in the live systems

What it needs:
- A screening job record (as in Procure to G/L)
- The link from each blank SKU to its screened SKU
- Full cost per good unit: blank, landed, screening and two-way freight, over good units
- The clearing account and approved treatment
- PO, receipt and invoice data that BC sync leaves out today, for quantity and invoice matching

### 4. Packaging sourcing

From a packaging quote to an approved part, PO, proforma invoice and deposit.

Open questions:
- Which of rules P1 to P6 Vince accepts, and whether that means five rules or six
- Where the customer, QC and costing sign-offs are recorded today
- Where a vendor proforma invoice is kept, and who checks it against the PO before payment
- Whether a DDP quote is counted twice by the landed-cost multiplier
- Which entity buys and which pays for packaging
- Whether the Sept 30 map's Portal and BC statements still hold

What it needs:
- One sourcing approval per quote with customer, QC and costing sign-offs
- The packaging part created from the approved quote and linked to its project
- A drawing and HS code required before a part can be ordered, with the HS code on the PO line
- A price per component when parts ship separately
- Full quote details: currency, MOQ, lead time, incoterm, origin, vendor item, case pack, drawing and sample
- Proforma invoices tied to their PO and checked against it before payment
- Deposits against a PO: who asked, amount, paid date, BC prepayment
- Whether a quote already includes freight and duty before landed cost is applied
- Vince's decision on each rule P1 to P6

### 5. Artwork and labels

Artwork and label approval from proof to printed component to finished good.

Open questions:
- How strong is the artwork QA gate: a warning that can be waived, or a block?
- Does accepting artwork for an unreleased formula warn or block?
- Who owns the artwork department and die-lines?
- Where do legacy proofs and print-ready files live?

What it needs:
- Each proof version with its approval status and approver
- Customer sign-off on one exact proof version, with channel and reference
- QA artwork acceptance and Regulatory review
- The chain from proof to printed component to finished-good revision
- The artwork version on the PO line, receipt and incoming QC
- Label reconciliation at assembly (Form #013B, QA sign-off, inspection #010)
- Artwork linked to the formula and ingredient list it was based on
- Portal, FileMaker and BC item numbers matched for printed components
- BC consumption of printed labels on assembly orders
- Digital capture of Form #008

### 6. Brand requirements

Customer brand requirements and the compliance documents that back each claim.

Open questions:
- Do the Portal's customer requirement rows carry scope, category, source document and contact?
- What does "reviewed" mean for a document before batching, and who reviews it?
- Do the Three Ships dates and documents exist in a source?

What it needs:
- Customer requirements scoped to brand, product line and product exceptions
- Compliance documents per supplier raw material: type, claims supported, issue and expiry dates, reviewer
- Which documents support which claim, per product and material
- A Regulatory review opened when a supplier or material changes, with Quality told
- A record of each product's supplier or material switch, with date
- A batching check on document review status and expiry
- Formula versions (pending or final) with ingredients and allergen notes
- Formula and artwork approvals tied to one exact version
- A claim-affecting change reopens formula approval

### 7. Expired material

What happens to expired or short-dated lots: hold, extend, waive, write off or replace.

Open questions:
- How is a batch-limited waiver without a new expiry posted when BC blocks expired lots?
- Where is a lot's extension history kept today?
- Does each customer's quality agreement state an expired-material policy?
- Does the Portal pick step filter expired or restricted lots today?
- Which exact lot and batch does the R-0030 waiver name?

Problems it needs to fix:
- Approved lots cannot be restricted
- No disposition case; requests and priorities run by email
- Waivers are approved without a new date
- Customer acceptance is implied, not recorded
- The label hides the extension
- Justification text is free-form
- Short-dated receipts feed the problem
- Date type and extension history are not captured
- Cases open at scheduling time, too late
- Lot dates live in FileMaker
- Write-offs need a reason code and case number
- Replacement purchases need a link to the expired lot they replace

### 8. Amazon FBA

Replenishing Amazon: shipment, labels, customs, freight and the stock move.

Open questions:
- Does days-of-supply count FBA inbound units as well as on-hand?
- Is the Sage item number the Amazon seller SKU, and where are FNSKU and ASIN kept?
- Which system holds last-30-day Amazon sales per SKU for the 45/90-day rule?
- Does the Seller Central app have the inbound role?
- Is the Portal's Freightcom link used for Amazon LTL quotes and bookings?

What it needs (ranked):
1. A shipment order for Amazon replenishment
2. Seller Central inbound (shipment ID, fulfilment centre, labels)
3. An Amazon stock feed and low-stock alert (45/90 days on last-30-day sales)
4. A warehouse screen for final dimensions, pallets and customs fields
5. Customs documents generated from item data
6. Freightcom: quote on final dimensions, booking, BOL, tracking
7. A BC stock move to an Amazon location at BOL

Weak links: case pack lives in people's heads; item-to-Amazon-SKU mapping is unconfirmed.

### 9. Equipment and assets

Tracking machines from purchase to production line, and whether each is capitalized.

Open questions:
- Who is the CFO contact and what is the capitalization threshold?
- Does the tracker or Fiix own the asset register?
- How are Sage vendor invoices coded?

What it needs:
- Each asset with tag, line, Fiix ID and purchase history
- The Fiix asset register and the tracker workbook
- The capitalization decision and who approved it (CFO)
- New, replacement or repair classification, and who decided
- Asset to PO, receipt and purchase invoice
- Asset to production line
- Purchases that are also grant costs
- Sage invoice and G/L coding, and BC fixed assets

### 10. Grants

Grant programs (NGen 14091, FedDev, AMIC, ISED AI-CAF) from eligible cost to claim to
reimbursement.

Open questions:
- Funder portal, claim schedule and contact
- How payroll is coded to projects
- The tracker format

What it needs:
- Each grant with its agreement, periods and status
- Eligible cost rows from the claim workbook
- An eligibility ruling per invoice line: ruling, who decided, date, source
- Funder claims: period, claimed and reimbursed amounts per cost
- Hours per person, period and project
- Each cost linked to its invoice, payment proof and claims
- Costs and hours linked to deliverables and partners
- Sage invoice and G/L coding, and the funder portal records

### 11. Operations maps (Ricardo)

Receiving, QC, release and complaints, traced through real examples.

What it needs:
- The QC release decision for a risk assessment (QRM-26037): who signed, when, released or not
- Purchase orders matched to actual receipts (PO5455, PO-00349/MIDAS2)
- ES-2 and Thamesurf SLES-30 recognised as the same material
- A customer complaint traced to resolution and the stock posting (Dandylion P-02289 / PO-00269)
- Receiving, QC and release linked: inspection result, accept/reject/rework, signer

## Where the use cases live

Each use case has its own folder in [`evals/dev/`](evals/dev/) with its full detail
and sources.
