# 2. Procure to G/L

Supplier PO → receipt → invoice → G/L, for packaging and screening.

## Based on

- Screening Process Map (Draft 6)
- Packaging Procure-to-GL Map

## Questions it must answer

### Understanding the process

- **What does the Screening Process Map cover, and what kind of evidence is it?**
  - A good answer: Attribute to the map (Draft 6, version 1.0 of the ZIP in AGENTS/Ontology UseCases): outside bottle and towel screening from the blank PO through conversion and G/L, with source-reported current gaps, targets and open accounting decisions kept apart. Its Portal analysis reads staging commit 1f2d42d code and does not run it. Call it a stakeholder use-case capture, not a verified report of deployed systems or approved accounting.
- **How does the screening map propose to cost a screened unit that can be sold?**
  - A good answer: Attribute the target to the map: total blank, landed, screening and two-way freight cost divided by good units. It is a target, not current practice. Any figure is Decimal with its currency and a per-good-unit unit, computed only from cost lines read; an unread part makes the total unknown, never zero.
- **Is the conversion approach for screened goods and the screening clearing account approved?**
  - A good answer: The map reports conversion option (a) chosen in owner answers dated Oct 14, while an ERP-D-015 exception for screening blanks and a screening clearing account still need approval. These are source-reported statuses; never present either as approved. The schema pin records ERP-D-015 as receipt freight to expense G/L, not item cost, which is the rule the exception would change.

### Tracing real records

- **Did this drop-ship (DPO) blank purchase order and its receipt reach Business Central?**
  - A good answer: The map reports DPO and receipt exclusions from the BC sync; the pinned drain code skips fulfillment mode DISTRIBUTE for purchase orders and their receipts. That is a code reading, not deployed behaviour. For one PO, name the keys tried in BC purchase orders and receipts; the Portal-to-BC entity map is not served, so a PO absent in BC is 'not found under the keys checked', never 'never posted'.
- **Which screened SKU was made from which blank SKU in this screening job?**
  - A good answer: Needs a recorded blank-to-screened link or a screening job naming both. The map reports that link missing, and no Portal model at 1f2d42d records a screening job. Report the gap; never match a blank to a screened SKU by description, name, artwork or date.
- **Do the blanks sent to the screener reconcile with the screened units returned and the screener's counts?**
  - A good answer: Use transfer lines out and back and the screener's proof and counts, each with its source; quantities are Decimal with their unit, and the balance is computed only from lines read. Transfers are not served and the counts are documents, so while they are unreadable report the gap; never derive a balance from the PO quantity alone.
- **Does the screener's invoice match the rate-card PO and the good units screened?**
  - A good answer: Compare the posted purchase invoice lines with the rate-card PO lines and the good-unit count, Decimal money with its currency. An invoice is tied to the PO only by a recorded link (BC order reference or the entity map); a number in invoice text is a lead. The map reports quantity and invoice reconciliation missing; answer with the keys tried and the gap.
- **Which G/L entries did the blank purchase, the screening charge and the conversion post, and to which accounts?**
  - A good answer: G/L entries tie to a posted purchase invoice only by document No. (corroborated, not typed). No served source ties item ledger entries, a receipt or a conversion to G/L entries, and the clearing account is not approved. Name the entries found with their keys and report the G/L lineage gap; never assign an entry to the job by amount, date or account.

### Understanding the process

- **What does Vince's Packaging Procure-to-GL Map cover, and what kind of evidence is it?**
  - A good answer: Attribute to the assessment: 19 stages from brief, procurement, PO, deposit, freight, receipt and QC to landed cost and BC/GL; the map says no database or BC execution was done. It mixes current-code and process descriptions, proposed fixes and sanitized real SoapBox excerpts; keep these types apart. Vince described it as brief to GL with one real example.
- **Is the packaging map's real example one completed transaction from brief to G/L?**
  - A good answer: No. The retail/gallon examples and the Lifetime bottles are partial anchors, not a completed chain. Quote, PO, P-number and PI references are document anchors; no case-native Portal or FileMaker row id, BC GUID, receipt lot or G/L entry number links them. Say so; never present the chain as complete.

### Tracing real records

- **Which Portal purchase order and lines is PO5447, and which Business Central purchase order is it?**
  - A good answer: Root the case on the served Portal purchase order found by the normalised number (PO#5447 is PO5447), with its lines cited. The Portal-to-BC link needs the entity map (not served) or a recorded renumbering; a BC order with a similar number is a lead. The supplied PO PDF and emails are documents that name the PO, not links.
- **Which proforma invoice governs the packaging price and the deposit, and how was the deposit worked out?**
  - A good answer: The assessment finds a plausible deposit basis but leaves document version, currency, basis and the governing PI unresolved, and calls none of it an approved payment. Name the candidate PIs and the deposit email as unresolved; a payment ties to the order only by a recorded applies-to or document reference. Money is Decimal with currency; never pick a PI or a basis.
- **What is the landed cost per unit of this packaging, freight and duties included?**
  - A good answer: Per unit landed cost is (goods + freight + duty + other) / quantity in one currency, Decimal, with explicit FX (operating.landed_cost). Forwarder quotes are options, not actuals; receipt freight and duties are actuals, and ERP-D-015 sends receipt freight to expense G/L, so the BC item cost can differ from the landed cost. Any unknown part makes the total unknown.
- **Was this packaging received and checked by incoming QC, and against which PO line?**
  - A good answer: Read the served Portal receipt and receipt lines tied to the PO, and the BC posted receipt only through a recorded link. Incoming QC records are not served: report the gap, never 'passed'. The assessment found no receipt lot id for the example, so an answer without a read receipt is 'not found under the keys checked'.
- **Which G/L entries did this packaging purchase post, and to which accounts?**
  - A good answer: Only G/L entries carrying a posted purchase invoice No. are tied, and only as corroborated. Nothing served ties the receipt or item ledger entries to G/L entries. The assessment found no G/L entry numbers for the example. Name the keys tried and report the G/L lineage gap; never match entries by amount or date.

### Understanding the process

- **What did Vince ask about the map's structure, and what must people clarify before more cases?**
  - A good answer: Vince asked whether the map's structure is what is needed before he, Ricardo and others submit more use cases. The assessment asks people to clarify document precedence and roles (specifier, procurer, payer, owner, approver), currency, UOM, cost allocation and authority, not to write the ontology. Its read answers make it dev-exposed, never a held-out case.

## Open questions for the business

- whether the ERP-D-015 exception for screening blanks and the screening clearing account are approved
- which BC document performs the blank-to-screened conversion under option (a)
- which proforma invoice governs the packaging order price and deposit, in which currency and unit
- whether any BC API page exposes value entries or the G/L to item ledger relation

## What the model still needs

| # | Need | Today |
|---|---|---|
|  | ScreeningJob (blank and screened SKUs, quantities, screener, artwork version, rate-card PO, proof and counts, transfers, conversion, invoice, G/L) | No screening job type, and no Portal model at 1f2d42d records one; the map proposes it as a target. |
|  | Screening decision (approval of the screen proof and the accepted and rejected counts that make units good) | No screening decision; the nearest records are Portal artwork proof-version decisions (not served), which carry no screening counts. |
|  | ConversionStep (blank SKU consumed, screened SKU produced, cost rolled under conversion option (a)) | No conversion event; ItemLedgerEntry links only to its item and to receipts, shipments and credit memos by document No. |
|  | Cost policy decision (the ERP-D-015 exception for screening blanks and the screening clearing account, with approver and date) | No decision or policy object; ERP-D-015 appears only as schema comments and as a GeneralLedgerEntry description. |
|  | Product.screened_from (screened SKU -> the blank SKU it is made from) | No link between two items; the map reports the blank-to-screened SKU link missing. |
|  | G/L posting lineage (receipt, item ledger entry or conversion -> GeneralLedgerEntry) | GeneralLedgerEntry links only to its account and, corroborated by document No., to posted purchase and sales invoices; BC value entries are not in the read grammar, so nothing ties item movements to G/L entries. |
|  | Portal purchase order and receipt -> Business Central document (entity map) | BcEntityMapping is declared but unserved; Portal-to-BC links fall back to renumbering and number matches, which are leads. |
|  | Supplier deposit -> purchase order or proforma invoice | Payment links only to a posted purchase invoice (applies_to_purchase_invoice); a deposit paid before any invoice has no link to its PO or PI. |
|  | Warehouse transfers of blanks to and from the screener | RelPublicAppWarehouseTransfer and its lines are generated not_served types; no reader. |
|  | Incoming QC records of received packaging | RelPublicAppIncomingQcRecord is a generated not_served type; no reader. |
|  | Screening Process Map originals (the ZIP's HTML, spec JSON and ontology CSV/JSON members) | Not a row of examples/case_originals.json; only the source descriptor with member hashes is pinned, and the Workbench has no verified read of the members. |
|  | Packaging map examples archive members (quotes, PO, PI, deposit, freight) | The archive row is pinned with extraction incomplete and its members' bodies are not verified, so documents the cards need cannot be cited by content. |

## Systems it reads

business-central, portal, portal-reporting-api

## Out of scope

- Portal or BC writes
- approving an accounting exception, clearing account or payment
- choosing between a map's inconsistent or unresolved statements
