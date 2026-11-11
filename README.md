# PLX Workbench

One place to see your work, ask questions about PLX's business (customers, orders,
suppliers, lots, documents, meetings) and get an answer with its source. It can also
draft the email or the change for you. Nothing is sent or changed until a person
approves it.

[![Watch the 3-minute demo](docs/media/poster.png)](docs/media/plx-workbench-demo.mp4)

**[Watch the demo (3 min, with captions)](docs/media/plx-workbench-demo.mp4).** It uses
made-up demo data, not real company records.

## What it does

- My work shows what needs you today, from every system, in one list.
- You can ask anything. Questions about the business get an answer with the record
  behind every fact. General questions ("what is FEFO?") get a plain answer, marked as
  general.
- You can ask it to do something, like a reply, a chase or an update, and it drafts it
  from what you have open.
- Every draft waits for a person. You see exactly what will be sent
  or changed, then you confirm. One at a time.
- Autopilots prepare routine work (supplier dates, PO chasers, new customer orders)
  for you. An autopilot only acts after it has proved itself.
- Meetings shows your calendar by day or week, with a prep sheet for each meeting.
- With view-as, the owner can see a colleague's work list the way they see it, read-only.

## Where the information comes from

![Nine systems, read into one place: Business Central, the Portal, Sage and FileMaker, Mission Control, email, Teams, calendar, Planner and SharePoint](docs/media/where-it-comes-from.svg)

It reads these systems. It never changes one by itself.

## A day with the Workbench

### Your morning

![Open My work, see what needs you, decide or approve, and chase what's blocked](docs/media/flow-morning.svg)

### Asking a question

![Ask, it reads every system, the answer has sources, check one, then ask it to do something and approve the draft](docs/media/flow-ask.svg)

### How an action gets approved

![A draft is made, you see exactly what will happen, safety checks run, you confirm, and it is done and written down](docs/media/flow-approve.svg)

### A supplier order goes late

![The supplier emails a new date, an alert appears on My work, a chase is drafted, you approve it, and a follow-up comes back if there is no reply](docs/media/flow-late-po.svg)

### A customer emails a purchase order

![The customer PO arrives, it is read and matched to items, the order is drafted, you approve, and the reply is sent](docs/media/flow-customer-po.svg)

### A meeting

![The meeting is on your calendar, a prep sheet is ready, you meet, and action items become follow-ups](docs/media/flow-meeting.svg)

## What works today, and what is waiting

**Working now:**
- Reading every system listed above.
- My work, Ask with sources, record pages, source pages, meetings and prep sheets,
  alerts, the work log and view-as.
- The approvals desk. Email sending is switched on for one approved mailbox, and
  that person still confirms every send.

**Built, but switched off until it has earned it:**
- Autopilots run in Draft mode: they prepare work and a person decides. None acts on
  its own yet, because none has passed its test on fresh days and been switched on.
- Changes in Business Central, Mission Control, the Portal, FileMaker, Teams, Planner
  and the calendar are prepared as practice runs: the exact change is written down,
  nothing is sent.
- Saving a file to SharePoint needs your own access to that folder, your confirm and
  the system's switch.
- Sage is never changed.

**Still waiting:**
- Permission to make changes in each system.
- Meeting recordings and transcripts (not connected yet).
- Amazon, Freightcom and Fiix.
- Some older Portal, Sage and FileMaker tables still need to be turned into proper
  records.
- More autopilots for every team, and logins for more people.
- Every page opening in under a second (most are quick; some are still slow).

## How to use

1. Open a terminal in this folder.
2. Type `pip install fastapi uvicorn pydantic` and press Enter.
3. Type `python3 -m operator_api serve --mock --port 8780 --insecure-cookie` and press Enter. This starts the demo with made-up data.
4. In a second terminal, type `cd app && npm ci && npm run dev` and press Enter.
5. Open `http://localhost:3000` and click the demo sign-in.
6. Try My work, ask a question, open a purchase order, or look at Autopilot and Meetings.
7. If it says **missing** or **hidden**, the data isn't there or isn't yours to see. It never guesses.
8. Something wrong or missing? Add a line to [`context/FEEDBACK.md`](context/FEEDBACK.md).

Full guide: [`context/HOW-TO-USE.md`](context/HOW-TO-USE.md).

## What's new

![What changed this week](docs/media/whats-new.svg)

- **v0.17:** one Workbench app; Ask can draft actions; the approvals desk; the Autopilot
  studio with its test on fresh days; meetings as a calendar with prep sheets; source
  pages that list every record citing them; view-as; data kept up to date through the
  day.
- **v0.16:** a correct late-order list, freshness per source, profitability labelled on item cost with careful verdicts, ambiguous order numbers resolved by asking.
- **v0.15:** answers show how we know (late-PO trails with vendor confirmation, why-trails for every record type); customer profitability; the email behind a vendor price; an accuracy report.
- **v0.14:** faster refreshes that keep already-read documents; large Portal tables load in resumable steps.
- **v0.13:** questions keep working during data refreshes; R&D agenda in seconds; search by meaning measured and ready.
- **v0.12:** answers in the app checked like the scoreboard and written plainly; late POs show how late; cited maps open; disagreeing systems and duplicate files handled.

All releases: [`context/RELEASE-NOTES.md`](context/RELEASE-NOTES.md).

## How it works

This part goes a little deeper, still in plain words.

### How the systems are linked together

![One purchase order linked to its copies in Business Central, the Portal, FileMaker and Sage, to the emails, Teams messages and files that mention it, and to its supplier, customer order and lot](docs/media/how-linking-works.svg)

The same purchase order often lives in four systems. The Workbench keeps each copy as
its own record and links them, so you can see what each system says. When two systems
disagree, you see both.

There are two kinds of link. A **firm** link is made when the systems' own numbers
agree. A **lead** is a number or name found in the text of an email, a Teams message
or a file; it is checked before it is used. In total about 70 million links tie each
order, lot, invoice and person to everything about it. Every value remembers where it
came from and when it was read.

### How Ask finds and checks an answer

![Your question, who is asking, find the records, write a draft answer, check every fact, show disagreements, say what is missing or hidden, and general knowledge labelled](docs/media/how-ask-works.svg)

1. Who is asking comes first. You only ever get answers from what you are allowed to
   open. A short list of people is private: their mail and records are never read. If
   that list can't be loaded, Ask stops instead of guessing.
2. It follows the links from the order or customer you asked about to its emails,
   files and the other systems.
3. Every fact is checked against the record or email it cites. A fact that doesn't
   match is removed before you see it.
4. Missing data is named, with where it would normally be.
   Things you may not open are counted, never described.
5. General questions are answered separately, under their own heading, without
   access to any company record.

When you ask it to *do* something, it drafts from what you have open and sends the
draft to the approvals desk. Ask never sends anything itself.

### How an autopilot works

![What it watches, how it prepares, the review queue, a person decides, the five modes, and the safety stops](docs/media/how-autopilot-works.svg)

An autopilot is a workflow that does one routine job, like "a supplier states a ship
date: update the expected date and reply".

- It watches for a new email or Teams message, a record coming due, an approval
  request in Mission Control, a meeting ending, a QC result.
- To prepare an item, it reads the records the item touches, the same way Ask does,
  and drafts the reply, the update or the approval note. Anything it can't check is
  left blank for you to fill in.
- Each prepared item waits in the review queue with its evidence.
  You accept, edit, reject or snooze it.
- Each workflow is set to one of five modes:

  | Mode | What it does |
  |---|---|
  | Off | Nothing is prepared. |
  | Suggest | Says what is open and what to do next. |
  | Draft | Prepares the work; you edit and approve each one. |
  | Auto | Acts when you click, after showing exactly what it will do. Only after its test. |
  | Watch | Acts by itself and shows what it did, with undo. Only after its test. |

  Today every workflow is in Draft. Off, Draft and Auto work on the live server;
  Suggest and Watch are in the studio and come with the workflow builder.
- These safety stops apply in every mode. A person confirms each live action, and there are no
  bulk live changes. Each system has its own switch, and all start off. One kill switch
  stops every autopilot at once, and each workflow has its own. Money, prices, credit,
  new customers or suppliers, people matters and judgment calls never run on their own.
  Every action, refusal and practice run is written down with the exact request and how
  to undo it.

### How an autopilot earns the right to act

![Starts in Draft, tested on past days, must clear the bar of 90% on at least 15 fresh items, the owner is told once, switched to Auto, later Watch](docs/media/autopilot-trust.svg)

Each workflow is replayed over real past days, and its drafts are compared with what
people actually did. Only days it never saw while it was being built count. It must get
**at least 90% right on at least 15 judged items** from those fresh days. Fresh days
show how it does on work it hasn't seen; the days it was built on would flatter it. When a workflow
passes, the owner is told once. It still does nothing more until someone switches it
on.

### Keeping it up to date and fast

The Workbench reads each system's changes through the day and updates only what
changed, so there is no nightly rebuild. When the shape of the data changes (a new kind
of link, say), only the affected part is redone, inside the database, with an undo log
so it can be rolled back. Big changes are tried on a small sample first and must finish
within two hours.

### How we know it works

We keep two sets of test questions. One is used while building. The other is kept
aside and only used to decide whether a version is good enough, so the score can't be
tuned to it. The bar: at least 90% correct with evidence, at most 1% confidently wrong,
every "I don't know" backed by truly missing data, and the same result twice in a row.
The working goal for answers is 97% right with none wrong.

## Use cases

Each use case is a real PLX process. For each one: the questions still open with the
business, and what the Workbench still needs, in priority order where the process owner
ranked it. The Workbench reads these systems. When it helps with a step, it drafts the
email or the change, and a person approves it first.

### 1. Quote to order

Quote → customer PO → sales order. Based on the company's Quote-to-Order map, the quote
workbook examples and the cost-to-assembly user journey.

Open questions:
- Which workbook revision is the quote of record for each example?
- Which BC sales order, if any, carries two recent quote wins?
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
- Which of rules P1 to P6 are accepted, and whether that means five rules or six
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
- A decision on each rule P1 to P6

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
- Do one brand's requirement dates and documents exist in a source?

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

### 11. Operations maps

Receiving, QC, release and complaints, traced through real examples.

What it needs:
- The QC release decision for a risk assessment (QRM-26037): who signed, when, released or not
- Purchase orders matched to actual receipts
- Two supplier names for one raw material recognised as the same material
- A customer complaint traced to resolution and the stock posting
- Receiving, QC and release linked: inspection result, accept/reject/rework, signer

## Full use cases

Every use case has its own page in [`use-cases/`](use-cases/): the questions it must
answer and what a good answer looks like, open questions, what it still needs, and
what is out of scope.
