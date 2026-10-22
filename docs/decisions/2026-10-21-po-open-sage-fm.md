# Decision 2026-10-21: one purchase-order status definition; Sage and FileMaker headers alone never open a PO

- decided: 2026-10-21
- gaps: open-po-lateness-model-sage-fm-open
- paths: operating/po_status.py, operating/process_lateness.py, operating/proactive_engine.py, operating/ontology_tools_process.py, ontology/schema/process/po_delivery_window.json, ontology/schema/process/purchase_to_invoice.json, ontology/schema/process/_process.schema.json, scripts/eval_packaging_oracle_check.py
- supersedes: `open-po-lateness-model-sage-fm-open.md` (deleted with this record)
- status: decided

## Question
When is a purchase order open, closed, not issued or unknown, for every reader (the
proactive engine, `po_delivery_window`'s `open_when` and PO stage, PO lateness, the
packaging eval check)? Each reader had its own word list, and those lists named the
Portal's derived code (SUBMITTED, PARTIAL, RECEIVED, CANCELLED). The column holds
FileMaker's words instead (live capture 2026-10-20, `app_purchase_order.status`, 5,880
rows): Cancel 4221, Complete 1139, DRAFT 362, Pending 96, B.Order 42, Revised 17,
Generated 3. So no list matched a single real PO.

## Decision
1. **One definition.** `operating/po_status.py` is the only PO status word list. A
   process graph names it: `open_when.definition: "po_status"` and the PO stage
   predicate `{"kind": "po_status"}`. The process schema refuses a word list there.
   `OneDefinitionTest` refuses a quoted PO word in any other `operating/` reader or in
   `scripts/eval_packaging_oracle_check.py`. The oracle (`evals/oracle/keys`) is
   excepted, and `OracleCrossCheckTest` checks against it.
2. **Openness is by quantity**, as the Portal decides it. The evidence is
   plx-customer-portal@d39d91b:
   - `open-po-lines/route.ts:38-46,302-312`;
   - `po-queries.ts:196-206`;
   - `derivePoFulfillmentCode`.

   The classes:
   - Cancel is closed. DRAFT is not issued.
   - Pending, B.Order, Complete and ORDERED are issued. ORDERED is what the
     purchase-order-sent hook writes (purchase-order-sent.ts@4af6957:89-92).
   - An issued word is open while the summed ordered quantity exceeds the summed
     received quantity (EPS 0.0001), and closed once it does not.
   - **FileMaker Complete alone never closes a PO.**
   - A null or unmapped word is unknown, and every reader counts it with its reason.
     So are unreadable quantities or quantities in mixed units, and a PO with nothing
     ordered. Today the unmapped words are Revised, Generated and anything new.
3. **Sage and FileMaker.**
   - **Sage-only:** a Sage-only PO is unknown (`sage_unmapped`). The SOP's 1-5
     `iscomplete` enumeration was replaced by a quantity check
     (plx-customer-portal@48b6cfc docs/Internal-SOP.md v8.598). The vocabulary live
     check records `iscomplete` against `linescmpl >= lines` as a cross-tab, counts only.
   - **FileMaker-only:** an FM-only header carries no received quantity, so it is
     unknown too and never open.
   - **Unnamed calls:** an unnamed `plx.process` call on either keeps
     `purchase_to_invoice`.
   - **Twins:** one PO is read across its settled `same_record_as` twins (verified or
     reviewed), each record by `po_status.record` and the lot by `combine`, by the
     proactive engine and `plx.process` alike. So an FM root whose Portal twin is open
     is open, and twins whose classes disagree are `status_disputed`. An identity link
     hidden from the reader leaves an unknown reader-dependent (`ambiguous_process`).
4. **Lateness** follows Vince.
   - An open PO is **late** once its requested-by date has passed.
   - It is **at risk** while that date has not passed but the supplier's confirmed date
     is later.
   - A received, cancelled or draft PO is neither.
   - The requested-by date is the reference, or it is reported missing (`no_due_date`).
     A supplier date is never put in its place.
5. **Routing fails closed.** Say a reader may not read the status, or the PO lines (by
   grant or marking), and so cannot decide openness. An unnamed `plx.process` call then
   answers `ambiguous_process` ("name the process"). It never routes that reader to a
   graph that a full-grant reader would not get. An unknown that comes from the data
   itself still keeps the implicit graph.

## Before and after (the 2026-10-20 capture, counts only)
- **Before:** 0 open and 0 closed. The engine's `OPEN_PO`/`CLOSED_PO` and `open_when`
  matched no captured word, so the lateness detector evaluated 0 POs.
- **After, by word:**
  - Cancel 4221 are closed.
  - DRAFT 362 are not issued.
  - Revised 17 and Generated 3 are unknown (20).
  - Complete 1139, Pending 96 and B.Order 42 (1,277) are open or closed by their line
    quantities.
- **Pending the live run:** the split of those 1,277 into open and closed, and the
  lateness detector's evaluated count. Run
  `python3 verify/live_verify.py --only po_status.vocabulary`, then `process.po_lateness`.
- **Fixture:** the proactive engine evaluates 5 POs (3 late, 1 at risk, 1 unconfirmed).
  The estimate check lists 2 open POs where the header list gave 3.

## Reverse triggers
- Ross Pennino's PO-status clarification maps a word
  differently. Then change `po_status.py` only; every reader follows.
- The `iscomplete` x `linescmpl` cross-tab of the vocabulary check disagrees with
  `linescmpl >= lines` for a value. The cross-tab counts are pending the live run. Then
  Sage `iscomplete` gets a mapping in `po_status.py`, instead of every Sage PO being
  unknown.

## F14
The F14 overlap noted in the superseded open record is settled: `delivery_delay.json`
is gone, and its PO chain is folded into `late_po` (e86a369bf4). `late_po` is F14's one
plan.
