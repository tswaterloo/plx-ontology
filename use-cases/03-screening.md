# 3. Screening

Cost and accounting for screened (printed) blanks.

## Questions it must answer

### Understanding the process

- **What does Vince's Screening Process Map (Draft 6) propose for outside bottle and towel screening, from blank purchase order through conversion and GL?**
  - A good answer: Attribute to the map: a proposed Screening Job tying blank and screened SKUs, quantities, screener, artwork version, rate-card PO, proof and counts, transfers, conversion, invoice and GL. Call it a stakeholder use-case map whose portal analysis traces staging commit 1f2d42d by reading code, not running it; implementation and business assertions are not independently verified.
- **How does the map say the value of a sellable screened unit is set?**
  - A good answer: Attribute to the map's stated target only: good units valued at total blank, landed, screening and two-way freight cost divided by good units. Label it a document target, not a verified implementation, and give no figure.
- **Which conversion option did the owner choose for turning blank items into screened items, and what does the map still leave open?**
  - A good answer: Attribute to the map: option (a) reported chosen in owner answers dated 14 Oct 2026; an ERP-D-015 exception for screening blanks and a screening clearing account still need approval. These are source-reported decision statuses, not approvals and not independently verified. Never treat either as granted.
- **Which current gaps does the map report between the purchase order, the Business Central sync, and the screened item?**
  - A good answer: Attribute to the map as reported, unverified: DPO and receipt exclusions from the BC sync, missing blank-to-screened SKU links, missing job conversion, and missing quantity-to-invoice reconciliation.

### Tracing real records

- **For one actual screening job, which blank item became which screened item, with what quantities and which screener?**
  - A good answer: Answer only from a joined record linking blank SKU, screened SKU, quantities and screener. No Screening Job object or blank-to-screened link exists today (ontology_gaps.json); an unseen join is a gap, never a match. Name the actual job number as the missing original.
- **For one actual screening job, how many units were good, and what is the final unit cost?**
  - A good answer: Compute good units and cost per good unit only from sourced counts and sourced blank, landed, screening and freight costs, with Decimal arithmetic and cited inputs. The actual job numbers, good-unit counts and final unit cost are not in the checked sources: say so and give no value.
- **Does the screening invoice quantity reconcile to the proof counts and the good units for a job?**
  - A good answer: Report a reconciliation only when invoice, proof count and good-unit sources are all read for one job. The map reports quantity and invoice reconciliation as missing; without those sources the answer is unresolved, not matched.

### Answering the real case

- **Which accounting treatment applies to the screening blanks under ERP-D-015, and where does the cost sit before conversion?**
  - A good answer: Do not choose a treatment. The approved ERP-D-015 treatment and the screening clearing account are open decisions in the map; report them as unapproved and name the approver as the missing original.

## Open questions for the business

- the actual screening job numbers
- the good units per job
- the final unit cost per job
- the approved ERP-D-015 treatment for screening blanks
- the approved screening clearing account
- whether the map's portal and BC assertions (staging commit 1f2d42d) hold in the deployed systems

## What the model still needs

| # | Need | Today |
|---|---|---|
|  | Screening Job (blank and screened SKUs, quantities, screener, artwork version, rate-card PO, proof and counts, transfers, conversion, invoice, GL) | No curated screening-job object is known in the checked sources. |
|  | Blank-to-screened item conversion, option (a): the link from a blank SKU to its screened SKU | The map reports missing blank-to-screened SKU links and missing job conversion; no such link exists in the checked sources. |
|  | Full cost per good unit: blank, landed, screening and two-way freight cost over good units | No cost-per-good-unit object or reader is known; good units and final unit cost are missing originals. |
|  | Screening clearing account and the approved ERP-D-015 treatment for screening blanks | Both are open accounting decisions in the map; the ontology holds no approved treatment or clearing account. |
|  | Purchase order, receipt and invoice reads excluded from the BC sync (DPO and receipt exclusions) for quantity and invoice reconciliation | The map reports the exclusions and the missing reconciliation; the reader coverage is not verified. |

## Out of scope

- Portal or BC writes
- approving or granting the ERP-D-015 exception or the clearing account
- choosing a unit cost or a conversion treatment
- reading the zip members from this repository
