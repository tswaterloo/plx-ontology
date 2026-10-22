# Decision 2026-10-04: BC three-way sides need per-root completeness (task t1-bc-threeway)

- decided: 2026-10-04
- status: decided; option 3, built as a separate follow-up task, not in this branch

## Question

`eval.bc_three_way` projects only the BC documents `integrations.bc_purchase` read for
each sampled PO (plus the item masters of their lines). `plx.process` decides a quantity
side only when the `bc` source family is known complete and fresh
(`operating/ontology_tools_process.py` `_carrier_problem`, `store_meta.freshness`), so on
this capture it leaves both sides undecided (`evidence_not_fully_visible`). How should the
eval let the product answer sides?

## Options

1. Keep it: the capture never claims a full traversal; sides abstain honestly.
2. Build the product store from a full `BcCapture` of the purchase sets (and items).
3. A per-root completeness notion in `plx.process`: a side over records read completely
   for this PO is decidable.

## Decision

Option 3. A three-way side for one PO is decidable when every BC collection filtered to
that PO (receipts, invoices, their lines) was paged to the end, with no remaining
`nextLink` and no error. This is recorded as a scoped completeness claim (source `bc`,
scope `po=<number>`, complete true), never as a source-wide completeness claim.

It is built as a separate follow-up task after p0-d-tools lands, because that task owns
`operating/ontology_tools_process.py`. Option 2 stays available once p1-bc-ontology lands.

## Interim behaviour

Option 1 stays until the follow-up lands. Declaring the per-PO capture source-complete
would be a false completeness claim, so it is not done. The live spec fails
`no_correct_answer` whenever no sampled PO can be decided on its stages alone. That red result
is honest and stays as it is.
