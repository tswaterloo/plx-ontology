# Decision 2026-10-05: a BC-only store may decide a BC three-way side past uncaptured Portal hops

- decided: 2026-10-05
- status: decided; option 2 with an honest label

## Question

A BC side is decidable over a per-PO read (scoped completeness
claims, `integrations.bc_purchase.scope_claims`). The `purchase_to_invoice` sides also read
`fallback_paths` through `PurchaseOrder.same_record_as` (the Portal twin) and group items
through `Product.same_record_as` (Portal products). In a store projected from BC alone,
those Portal bindings are `source_not_captured`, so every side stayed undecided, even when
every BC read for the PO was complete.

## Options

1. Keep it: decide only when Portal is captured too.
2. Let a side whose own system is BC skip the twin and identity hops when the twin source
   is not captured, and say so in the answer.
3. Capture the Portal PO, receipts and products scoped to the same PO.

## Decision

Option 2, with an honest label. When the Portal twin or product bindings are not captured
(build report `source_not_captured`), a side of a BC-rooted PO that is read from its own
BC records may be decided from BC alone, if the BC claims (per-root or per-company) prove
those records complete. The side then carries a typed gap `cross_system_unchecked`
(state `not_ingested`: Portal was not captured, so the twin conflict check did not run).
The process result's `completeness` is partial (`complete: false`, reason
`cross_system_unchecked`), never complete.

A Portal that is withheld from the reader, or captured but incomplete, keeps the side
undecided as before.

## Reason

P1 acceptance asks for Portal hops to be typed gaps on BC-rooted questions. Within one BC
company an item No. is unique, and BC links never cross companies, so a Portal product
cannot merge two BC items of one PO. Only the twin-receipt conflict check is lost, and the
label says so.

## Consequences

- `ontology/tests/test_project_bc.py`: Portal hops stay typed gaps and never an absence.
- `scripts/eval_bc_three_way.py` reads each sampled PO with `scoped_completeness=True` and
  lists `bc_purchase.scope_claims(evidence)` as its relations' `scopes`, so its sides can
  be decided.
