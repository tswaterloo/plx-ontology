# 8. Amazon FBA

Replenishing Amazon: shipment, labels, customs, freight and the stock move.

## Questions it must answer

### Understanding the process

- **What does the Amazon FBA Replenishment Map cover, and what kind of evidence is it?**
  - A good answer: Attribute to the map (v5 of 16 Oct 2026, provider version 1.0): built from Greg's walkthrough and one real shipment, OR1000154027 for For & Against to Amazon HIA-1; nine stages from the low-stock trigger to tracking received; twelve balance checks, ten broken, one holding and one not checked; seven ranked gaps plus a stock-accuracy dependency declared out of scope. Say that its author read no Portal or BC code, so its 'not built' labels and proposed tables are expectations, and that the underlying emails, customs and BOL documents were not read. Call it a stakeholder process map, not a verified report of deployed systems.
- **When should a SKU be replenished at Amazon and how much should be shipped, according to the map?**
  - A good answer: State the rule as Greg's (16 Oct 2026, status stated, not approved): alert when Amazon stock of a SKU falls below 45 days of supply and ship enough to bring it up to 90 days of supply, both computed on last-30-day sales; the same process for every brand. Keep it apart from practice (a manual Seller Central check with no threshold) and from implementation (no alert exists; the map says nothing in the Portal watches Amazon inventory, a claim not checked against code). Days of supply is on-hand divided by the daily rate of last-30-day sales; whether inbound units count is not stated and stays an open question. A computed quantity needs a read of FBA on-hand and last-30-day sales per SKU with its as-of date; without that reader the answer is a gap, never a number.

### Tracing real records

- **Which identifier ties a replenishment shipment together across Amazon, the Sage OR, the Portal, Freightcom and Business Central?**
  - A good answer: Per the map, the only key that joins them is the Amazon shipment ID (issued by Amazon, unique within the seller account), and in the real case it is on none of the OR, the packing slip (PO Number blank) or the BOL. The OR number is issued by Sage, unique within the company, retires with Sage and is reused as the TSCA reference across shipments, so it is not a shipment identity; matching on it outside its scope crosses records. The map's target (a Portal shipment order with amazon_shipment_id, one per Amazon shipment, and a BC transfer order) is proposed, not built, and the 'one order per Amazon shipment' check could not be tested. An answer names the missing join as a gap and never matches OR, BOL and Amazon shipment by date, SKU or quantity.
- **How are cases on a shipment order reconciled to units on the customs invoice?**
  - A good answer: Units are cases times the item's case pack, and the case pack must come from one cited source: the ontology's ItemUnitOfMeasure (Sage unit conversion) is the only source a Workbench conversion may cite. The map reports that case pack 'lives in people's heads': for FNA305-2LP Greg states 8 units per case (16 Oct 2026) while the customs invoice implies 12. An answer computes units from cases with the cited case pack, reports a disagreement between sources as a conflict with both sides attributed, and never picks one silently. The map's balance check 'cases ordered = boxes shipped' holds (69 cases, 69 boxes); 'units on customs = units ordered' is broken.
- **Is a PLX item the same thing as an Amazon seller SKU, FNSKU or ASIN?**
  - A good answer: Not established: the map lists the item to Amazon SKU mapping as unconfirmed ('unknown whether the Sage item number is the Amazon seller SKU') and proposes storing seller SKU and FNSKU on the item. The ontology has Product over sage.ic_item and no Amazon listing identity. An answer joins Amazon inventory or sales to a PLX item only through a cited mapping and otherwise reports the join as a gap; equal-looking codes are not a match.
- **Was the freight booked on the final pallet dimensions and weight?**
  - A good answer: The map's rule is that the booked quote uses the final pallet dims and weight, re-quoted before booking, with final freight approved by the warehouse manager and Greg copied (Greg, 16 Oct 2026; a stated requirement, not an approval or permission). It reports the check broken in the real case: a quote was put forward on 8 Oct, before the dims email of 9 Oct. The Freightcom quote and booking originals are still to come, so whether the booked freight was re-quoted on the final dims cannot be read today; an answer says so, keeps the map's finding attributed and states no freight amount.
- **When does stock move from the physical warehouse to the Amazon location, and do the records agree?**
  - A good answer: Keep the views apart, each attributed: as decided, the move happens when the BOL is generated (Greg, confirmed 16 Oct 2026); as practised, the 24 Sep email asks for the Sage VMI transfer once the OR exists; as it happened, the packing slip already shows warehouse VMI on every line before the BOL. The target, a BC transfer order to an Amazon location posted at BOL, is the map's expectation of standard BC behaviour, not checked in the repo; the map says BC has no Amazon location yet. An answer reports the timing conflict, reads posted movements only from item ledger entries by location and entry type (never summed across types), and treats an unread movement as a gap.
- **Can the stock on record be trusted when a replenishment shipment is planned?**
  - A good answer: The map reports 'stock on record = stock on shelf' broken: for FNA305-500 the record showed 30 cases and 17 existed (Greg, 16 Oct 2026), found after the OR and the Amazon build, so both had to change. It declares warehouse stock accuracy out of scope and assumed fixed by a separate process. An answer cites on-hand per location with its source and as-of time, says a ledger balance is not a shelf count, and never states availability the reads do not support.

### Answering the real case

- **What is the chain of records for shipment OR1000154027, from the first OR to the Amazon shipment and the BOL?**
  - A good answer: As the map reports it: the OR was issued on 28 Sep before Amazon had assigned a destination, with a guessed ship-to FC; Amazon built the shipment on 7 Oct and assigned HIA-1, so the OR was revised on 8 Oct for the new FC and a lower quantity (the revised packing slip is still dated 28 Sep); the BOL is dated 13 Oct and carries the OR number only; the pallet left on 15 Oct. The Amazon shipment ID is on no document the map saw, so the Amazon side of the chain cannot be joined. Unscored until the OR, packing slip and BOL originals are read; an answer lists the missing links instead of inferring them.
- **Do the units declared on the customs invoice of OR1000154027 match the cases shipped?**
  - A good answer: Per the map, three lines agree and the FNA305-2LP line does not: 12 cases at Greg's stated case pack of 8 are 96 units, and the customs invoice declares 144. The map also reports the TSCA and USMCA certificates reused from an earlier shipment, listing other SKUs, and net weight equal to gross weight. Unscored until the customs packet original is read; an answer attributes each figure to the map or to a read document, recomputes units from cases with a cited case pack, and states no corrected invoice value.
- **Did the identity fields on the OR1000154027 paperwork (sold-to, ship-from, ship-to FC, Amazon shipment ID) agree with each other?**
  - A good answer: Per the map's balance checks, all four broke: the OR is sold to another brand's customer account, the BOL and customs invoice name a different ship-from address than the warehouse the pallet left from, the first OR's ship-to FC differed from the one Amazon assigned, and no document carries the Amazon shipment ID. An answer reports each as a conflict between named documents, attributes it to the map until the originals are read, and copies no address or account detail.
- **Did OR1000154027 bring each SKU up to 90 days of supply at Amazon under the 45/90-day rule?**
  - A good answer: Not answerable today: the rule needs FBA on-hand and last-30-day sales per seller SKU as of the build date, and no Workbench reader reaches Seller Central or another sales-velocity source; the item to seller SKU mapping is also unconfirmed. The map gives the shipped cases, not the stock or sales behind them, and the rule was stated on 16 Oct, after this shipment was planned. An answer says what is missing and does not estimate days of supply.
- **When did the OR1000154027 stock move to the Amazon VMI warehouse, and was that at the BOL?**
  - A good answer: Per the map, the packing slip of 28 Sep already shows VMI on every line, before the BOL of 13 Oct, against the rule 'move at BOL'. The Sage transfer itself, any BC posting and the BOL are not read, and BC has no Amazon location per the map; an answer reports the move time only from a read movement record and otherwise says it is unknown.
- **Which originals of shipment OR1000154027 exist, and which have been read?**
  - A good answer: Only the map has been read (pinned by item id and SHA-256). It names six source-file folders for the example (kick-off email, first OR and packing slip, Amazon build and OR revision, quote and dims, customs packet, BOL) whose SharePoint links were 'added after upload', and lists the Amazon shipment and labels, the Freightcom quote and booking, and tracking as still to come; Teams messages were not provided. An answer lists each as unread with its state and infers nothing about its content.

## Open questions for the business

- whether days of supply counts FBA inbound units as well as on-hand
- whether the Sage item number is the Amazon seller SKU, and where FNSKU and ASIN are kept
- which system holds last-30-day Amazon sales per SKU for the 45/90-day rule
- whether the dashboard's Seller Central app holds the inbound/fulfilment role
- whether the Portal's Freightcom client is used for Amazon LTL quotes and bookings

## What the model still needs

| # | Need | Today |
|---|---|---|
| 1 | Create a shipment order for Amazon replenishment (ReplenishmentShipment (one Amazon inbound shipment of PLX stock: Amazon shipment ID, FC, status, OR, BOL, freight booking, lines in cases)) | SalesOrder (sage.oe_order_header) and Shipment (Sage OE, Portal and BC bindings) exist; neither carries an Amazon shipment ID or an Amazon FC, and Shipment.same_record_as needs a stable shared key the map says does not exist. |
| 2 | Connect Seller Central inbound (Amazon shipment ID, FC, labels) (Shipment.amazon_inbound_shipment (an OR or shipment to the Amazon shipment ID it fulfils; one order per Amazon shipment)) | No Amazon or SP-API object, binding or source registry entry exists. |
| 3 | Amazon inventory feed and low-stock alert (45/90 days on last-30-day sales) (Amazon FBA on-hand, inbound and last-30-day sales per seller SKU, with as-of date) | No object or reader for Amazon inventory or sales; no days-of-supply measure. |
| 4 | Warehouse screen for final dims, pallets and customs fields (ShipmentPallet (final pallet count, dimensions and weight, gross and net, captured before the freight quote)) | Shipment has no pallet, dimension or weight members the cards can cite. |
| 5 | Generate customs documents from item data (CustomsDocument (commercial invoice and TSCA/USMCA certificates of one shipment: lines in units, certificate coverage per SKU, validity)) | No customs document or certificate object; Product has no certificate link. |
| 6 | Connect Freightcom (quote on final dims, booking, BOL, tracking) (Freightcom quote, booking, BOL, PRO/SCAC and tracking per shipment) | The source registry lists the Freightcom hosts as a Portal integration; no ontology object or Workbench reader covers a booking, BOL or tracking event. |
| 7 | Inventory move to an Amazon location in BC at BOL (Shipment.moved_by_transfer (the BOL of a replenishment shipment to the BC transfer posting that moves its stock to the Amazon location)) | ItemLedgerEntry is partial (entry types include transfer) and Warehouse is not served (it pends bc_warehouse.json); nothing links a shipment or BOL to a ledger entry by location. |
| weak link 2 | Case pack lives in people's heads (Product.case_pack (an item's units per case, cited to one ItemUnitOfMeasure row)) | ItemUnitOfMeasure (sage.ic_item_uom) is the only source a unit conversion may cite; whether a case-pack row exists for each FBA SKU is unread. |
| weak link 4 | Item to Amazon SKU mapping unconfirmed (Product.amazon_listing (a PLX item to its Amazon seller SKU, FNSKU and ASIN)) | Product binds sage.ic_item; no Amazon listing identity exists. |
| dependency | Warehouse stock accuracy (out of scope, assumed fixed separately) (On-hand per item and location with as-of time, and a shelf count to compare) | ItemLedgerEntry is partial; no cycle-count or shelf-count object. |
| source files | Source files of example A (links 'added after upload') (The originals of OR1000154027 (missing_originals MO-01 to MO-10 in proposed_items.json)) | Email and SharePointFile exist; only the map itself is a watched original, and none of these folders is a registry watch folder. |

## Systems it reads

amazon, bc, freightcom, portal, portal-reporting-api, sharepoint

## Out of scope

- Portal, Sage, BC, Amazon or Freightcom writes
- building an Amazon shipment, generating labels, booking freight or posting an inventory move
- customs or regulatory conclusions
- choosing between the map's as-practised, as-it-happened and target statements
