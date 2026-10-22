# Decision 2026-10-14: a BC three-way stage is proven absent by a complete zero read

- decided: 2026-10-14, owner order
- reverses: docs/decisions/2026-10-10-followup-decisions.md #2 ("absent side in three-way match"), under its own reversal clause: "a source proves absence by a complete, dated read"
- rows: G-04, G-11
- gaps: gap-5f5ea77b6511
- paths: scripts/eval_bc_three_way.py (oracle), operating/ontology_tools_process.py (product), ontology/schema/process/purchase_to_invoice.json and _process.schema.json (`absent_by`), verify/live-checks/eval.bc_three_way.json
- status: decided; live-unverified until the hourly eval.bc_three_way and the daily projection_bc run after landing

## Decision

A BC purchase order with no posted receipt (or no counted invoice) has its stage
`stage:receipt` (or `stage:vendor_invoice`) decided `absent_in_bc`, with the read time,
only when all of these hold:

1. The posted-document query for the order was complete: every purchaseReceipts (or
   purchaseInvoices) page read, nothing held, and none found.
2. The order's lines were read completely (every page of purchaseOrderLines).
3. Every counted line carries BC's own figure (receivedQuantity, or invoicedQuantity)
   and every figure is 0. BC raises the figure when a receipt (or invoice) of the line
   posts. The oracle counts the Item lines. The product counts every line of the side
   that it does not ignore (`ignore_through`: Comment, Account and Charge lines), so it
   needs at least as much as the oracle and can only be less decided.

Anything less stays undecided:

- an incomplete or failed read gives `bc_read_incomplete` (oracle) or
  `recorded_measure_unproven` / `absence_unproven` (product);
- a non-zero figure (a partly received or invoiced line) gives
  `{stage}_quantity_recorded` (oracle) or `recorded_measure_conflict` (product);
- a missing or unreadable figure is never read as 0.

The oracle and the product share no code. The oracle (`_line_absence` in
scripts/eval_bc_three_way.py) reads its own whole records through `BcReads` and counts
Item lines. The product (`_absent_in_bc` in operating/ontology_tools_process.py) reads
the projected graph. It names the proving side in the schema (`absent_by`: role,
quantity, evidence) and needs complete scoped claims for each hop plus a read time
(`_bc_read_time`). When the Portal twin was not captured, the product also states a
`cross_system_unchecked` gap. An absent side counts as 0 in its quantity comparisons.

## Why #2 is reversed

#2 left an absent side `unknown`, because no source had proven the absence. BC's per-line
receivedQuantity / invoicedQuantity, read completely beside a complete posted-document
query, is that complete, dated read. Live samples (120acb30, 10:33Z 2026-10-08) had 5 to 7
of 15 roots with no receipt that the oracle excluded. That left coverage below 12/15.

## Cost

Each order now reads its lines, the excluded ones included. On the injected-transport
replay of the slowest live hour (10:33Z: 0.77 s per answered GET, 1 attempt in 6 failing
after 16 s), 15 items take 549 s, against 468 s before. So:

- the live-check budget goes from 540 s to 600 s (timeout 660 s to 720 s), which leaves
  about one failed GET of margin;
- the `RunCost` prior `ITEM_GETS` goes from 8.5 to 9.5. The replay spends 525 s on 15
  items, 35 s an item, about 9.3 GETs at 483/128 s.

Decision 15 (Retry-After wait) is unchanged: across the throttle profiles no run ends
incomplete or over budget.

## Acceptance

- Tests (injected-transport / isolated-fixture):
  - tests/test_eval_bc_three_way.py `test_absent_stages_need_a_complete_zero_read_on_both_sides`;
  - tests/test_process_three_way.py `test_absent_in_bc_needs_a_complete_zero_read_of_every_line`.
- Live, after landing: hourly eval.bc_three_way coverage >= 12/15 with wrong 0, and daily
  projection_bc three_way decided > 0.

## Reverses if

- Live BC shows a posted receipt or invoice for a line whose figure reads 0. That would
  mean the figure lags posting.
- Draft invoices or credit memos turn out to move invoicedQuantity in a way that makes 0
  not mean "no counted invoice".

## Limitations

- A prepayment invoice posts against the order without raising any line's
  invoicedQuantity. An order with only prepayment invoices can then read as
  vendor_invoice absent_in_bc. Prepayment documents are not read.
