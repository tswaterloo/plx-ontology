# 10. Grants

Grant programs from eligible cost to claim to reimbursement.

## Questions it must answer

### Understanding the process

- **What does Stephen's Grants Program brief ask for first, and what does it rule out?**
  - A good answer: Attribute to the brief: trace NGen project 14091 (LightsOut) first, producing a stages/status map, keys/crosswalk, weak links, a tracer and minimal entities, with unseen joins marked as gaps. State the boundaries (no writes to Sage, BC, funder portals or SharePoint; no duplicate PDFs; no inferred or hard-coded eligibility rules) and the questions Stephen wants answered before starting. Call it a stakeholder proposal, not implemented capability or grant-submission authority.
- **How many cost rows are in the NGen Q10 claim, and how many of them need an invoice PDF over $500?**
  - A good answer: Report the brief's two counts as source-reported, not rechecked. A recount needs the Q10 claim workbook read at a named revision; it is not a registered original, so say where it would be read from is unknown rather than presenting the brief's figures as verified.

### Tracing real records

- **Which invoice and which payment proof support this NGen cost row?**
  - A good answer: Link the cost row to its invoice and payment proof only on a named key (BC purchase invoice, BC vendor payment, Sage PO or receipt, Portal PO), citing each record and the original PDF in place, never a copy. A vendor, amount or date coincidence is a candidate, never a match; a join not seen is reported as a gap with the keys tried. Amounts are Decimal with currency stated.
- **Is this invoice line eligible under NGen, and which ruling says so?**
  - A good answer: Give eligibility only with a cited ruling: who ruled, when and in which source. Without a recorded ruling the answer is open. Never infer eligibility from an expense category, amount, vendor or a rule (inferred or hard-coded eligibility rules are a boundary of the brief).

### Understanding the process

- **What did the NGen Q9 review reject or question, and which cost rows did it affect?**
  - A good answer: Attribute to the brief: the Q9 review rejected statement references and questioned project dedication. That is source-reported; the review itself was not read, so the rows it affected are unknown until the ruling is a cited original. Never mark rows rejected from the brief's summary.

### Tracing real records

- **How many hours did each person charge to NGen project 14091 in a claim period, and where are they recorded?**
  - A good answer: Hours come only from a cited source naming person, period and project. Payroll project coding is an open question in the brief and no served reader carries hours (BC payroll and employees are outside the GET-only grammar; the reporting API excludes wages and person names), so the answer today is a named gap. Never estimate hours from rates or cost, and never expose wages.
- **Under which funder claims was this cost claimed, and how much was claimed and reimbursed in each?**
  - A good answer: Per the brief one cost can carry several funder claims, each with its own claimed and reimbursed amount. List each claim separately with its source, Decimal amounts and currency; never merge claims or sum across funders. Without a claim record the answer is open.

### Understanding the process

- **Is a grant cost row a separate record from the purchase it pays for?**
  - A good answer: No: the brief reuses the shared Purchase / Cost Line entity from the asset brief, one record with funder-claim edges (and an asset edge where it is equipment), never two records reconciled after the fact.

### Tracing real records

- **Which deliverable or partner delivery does this cost or these hours support?**
  - A good answer: The brief asks for stable links from costs and hours to deliverables. Give the link only with its source; a link not recorded anywhere is a gap, never inferred from dates, vendor or description.

### Understanding the process

- **Can the Workbench trace FedDev and AMIC claims yet, and where does ISED AI-CAF stand?**
  - A good answer: Attribute to the brief: FedDev and AMIC stay untraced until NGen closes, and ISED AI-CAF is pending. Answer that no trace exists for them, without attempting one or inventing a status.
- **Which funder portal, claim cadence and contact apply to NGen, and how is grant work coded in payroll?**
  - A good answer: These are open questions in the brief. Answer that they are unknown and name what would settle each (the funder portal record, the payroll project coding source); never guess from mail or past claims.

## Open questions for the business

- funder portal, claim cadence and contact
- payroll project coding
- tracker format

## What the model still needs

| # | Need | Today |
|---|---|---|
|  | Grant (funding program and project: NGen 14091, FedDev, AMIC, ISED AI-CAF) with its agreement, periods and status | No grant or funding-program type; nothing in Portal, Sage relations or the BC grammar names a grant. |
|  | Eligible cost row (the shared Purchase / Cost Line, from the claim workbook) | Purchase objects exist (Sage PO, BC purchase invoices, Portal PO) but no Cost Line; the asset case records the same missing edge (evals/dev/asset-tracker/ontology_gaps.json AT-GAP-10). |
|  | Eligibility ruling (per invoice line: ruling, decider, date, cited source) | No ruling or decision object for funder eligibility; mail is readable but no mailbox is known to hold rulings. |
|  | Funder claim (claim period, claimed and reimbursed amounts per cost) | No claim object; no reader for any funder portal. |
|  | Hours (person, period, project, hours) charged to a grant project | No time-entry type. Portal holds employee rates, not hours, and they are not served; the BC grammar excludes employees and payroll; the reporting API excludes wages and person names. |
|  | Cost row to invoice and payment proof | Invoice and payment objects are readable in BC and Sage PO relations are served, but no key from a cost row to them is known. |
|  | Cost row to funder claims (one cost, many claims) | No claim object, so no edge; the asset case records the same funder-claim edge as missing (AT-GAP-10). |
|  | Cost or hours to deliverable and partner delivery | No deliverable object or link. |
|  | Purchase or invoice line to grant project coding | BC dimensions are readable, but whether any dimension codes a grant project is unverified; Sage AP/G-L relations are not served. |
|  | Claim workbooks and invoice PDFs as originals | The brief is the only registered grants original; the Q10 workbook, the over-$500 PDFs and the Q9 review are not captured, so nothing can be recounted or cited. |
|  | Sage AP and G/L (invoice and payment coding) | The reporting API serves Sage PO, order and invoice-to-customer relations but no AP or G/L relation. |
|  | Funder portal (submitted claims, rulings, reimbursements) | No reader; the brief forbids writes to funder portals, so any reader would be read-only exports. |

## Systems it reads

business-central, microsoft-graph, portal-reporting-api, sharepoint

## Out of scope

- writes to Sage, BC, funder portals or SharePoint
- duplicate PDFs
- inferred or hard-coded eligibility rules
- grant submission or outreach
