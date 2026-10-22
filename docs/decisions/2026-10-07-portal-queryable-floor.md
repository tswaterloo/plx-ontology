# schema.portal_queryable: a queryable floor, and failing ids that fit the cap

2026-10-07. Evidence label: injected-transport (fixture rows,
`tests/test_schema_queryable_probe.py`).

## Problem

1. `scripts/live_read_probes.schema_queryable` set `ok` = at least one relation queryable and
   no leak, and the spec's `complete_when` no longer required `not_queryable_count == 0`. A run
   with 92 of 93 relations `not_queryable` recorded **pass**.
2. `not_queryable` was `{group: {relation: code}}`. With 92 failing relations the recorded field
   passed `verify/live_verify.MAX_DETAIL_CHARS` (4000) by itself, so `_cap` dropped it and every
   field after it in record order (`read_empty`, `queryable`). The registry then saw no names at
   all: every relation read `no_live_check` instead of `live_not_passing`. Reproduced on the base
   (1 of 93 queryable: detail 4944 chars, `fields_truncated`, no `failures`).

## Decision

- **Share and floor.** The probe reports `queryable_share` = queryable / evaluated, where
  evaluated = queryable + not_queryable, rounded down to 4 places (null when nothing was
  evaluated), and `queryable_floor` = `QUERYABLE_FLOOR` = 0.9 (a `Decimal`). `ok` is true only
  when `queryable >= floor x evaluated` (exact Decimal comparison, so 81 of 90 passes) and no
  candidate leaked. The spec keeps `json_equals ok: true` and replaces `json_min
  queryable_count: 1` with `json_min queryable_share: 0.9`, so a failing run's `failures` names
  the share (`queryable_share < 0.9`). The test pins the spec's minimum to the probe's floor.
- **Why 0.9, not 1.0.** A floor of 1.0 would make one failing relation withhold credit from
  every other relation (the registry credits `queryable` names only when the check passed),
  which is the coupling that was removed. Each failing relation stays named
  `live_not_passing` in the registry whatever the floor; the floor decides only whether the run
  as a whole is a pass. 0.9 allows at most 9 of 93 failing relations. Today's live state (92 of
  93 failing) fails, which is the honest result.
- **Superseded by the Amendment below (they now count against the share).** **Not evaluated,
  never in the floor:** `read_empty` (page had no row), `unsampled` (no
  binding's `where` matched a row), `unavailable` (catalog does not serve it; makes the run
  incomplete) and `unreached` (deadline; makes the run incomplete). They neither pass nor fail.
- **Ids only within the cap.** `not_queryable` is now `{group: [relation id]}`, the same shape
  as `queryable` and `read_empty`; the failure codes move to `not_queryable_codes`
  `{code: count}` (a handful of keys, recorded as enums). Every bound relation is in exactly one
  of the three name lists, so all 93 ids together fit the cap with the counts and `failures`
  (1 of 93 queryable: no `fields_truncated`, all 92 failing ids recorded). The per-relation code
  is no longer published; the code histogram tells which failure class dominates, and the
  relation's own code is one local probe run away. Older results with the mapping form still
  read correctly (`scripts/source_registry._strings` takes mapping keys).

## Empty relations (gaps gap-1cda0e1f879f, gap-494946938ce8, gap-984fde40c1e2)

The 7 relations read empty at 2026-10-07T07:51Z (`app_manufacturing_job`, `pr_line_item`,
`purchase_requisition`, `receipt`, `receipt_line`, `ship_release`, `shipment_line`, the Portal
`public.app_*` tables) stay `live_unexercised` by honest status: they are named under
`read_empty` (registry_names `unexercised`) and never credited as queryable. Under the
amendment below they count against `queryable_share` (the denominator is every bound relation).
The daily `schema.portal_queryable` run re-reads each one; when a page returns rows the
relation is projected and probed and flips to `queryable` (credited) or `not_queryable`
(`live_not_passing`) on that run. No manual step closes these three gaps.

## Options not taken

- Keep codes per relation and raise the verifier cap: `verify/live_verify.py` is shared by
  every check; a larger cap for one check is a wider change than the record needs.
- A `--floor` CLI argument: one more declaration to drift; the constant plus the spec's
  `json_min`, pinned together by the test, is enough.

## Amendment: the floor measures the bound catalog

It replaces the "evaluated" denominator and the
"not evaluated, never in the floor" rule above.

- `queryable_share` = queryable / bound relations (`bound_relation_count`). Read-empty,
  unsampled, unavailable, unreached and failing relations all count against it. The share
  measures the catalog the schema binds, so 1 queryable and 92 unreached reads 0.0107, not 1.0.
  With the 7 relations read empty live, the ceiling is 86 / 93 = 0.9247: still above the floor.
- `ok` also requires a complete run (the spec's `complete_when`: nothing unreached, unavailable
  or unsampled). An incomplete run fails (`ok != expected`) with its counts recorded, so the
  projected tier never reads green on a partial run.
- The floor is declared once, as `expect.json_min.queryable_share` in
  `verify/live-checks/schema.portal_queryable.json`. The probe reads it (and `complete_when`)
  from the spec as an exact `Decimal`; a missing or malformed floor fails the run closed.
  `queryable_floor` in the output echoes what the probe read.
