# Decision 2026-10-17: decide a Portal comparison on relation-level completeness

- decided: 2026-10-17, option 1
- planned against: docs/decisions/2026-10-14-three-way-absence-proven.md (BC absence
  stays decided by its complete zero read; this changes only when a Portal relation
  counts as complete)
- paths: ontology/project.py (`Projector.relation_completeness`,
  `store_meta.relation_completeness`), operating/ontology_tools_process.py
  (`_relation_claims`, `ProcessTools._relation_complete`, `_carrier_problem`)
- status: decided; isolated-fixture tested, live-unverified

## Question

Should `plx.process` decide a Portal-side comparison (three-way match, ordered vs
received) when the relations that side reads are fully captured, although the
`portal_api` source as a whole is not?

## Evidence

- `ontology/project.py` `freshness` marks a source `complete` only when every bound
  relation of that source has a completed full traversal; one unserved relation makes the
  whole source `partial`.
- `ProcessTool._carrier_problem` (`operating/ontology_tools_process.py`) then needs a
  scoped claim, and scoped claims exist for BC only (`_claim_cause`: `claims ... if source
  == "bc"`), so every Portal side is `scope_not_read` and every verdict null.
- Fixture dev baseline (isolated-fixture, items_digest f03852a80198817b): 7 of 14 fails
  are this class; all 6 F4 items have null verdicts. The fixture serves 21 of 63
  relations. The live Portal serves 89 of 93 relations (2026-10-06), so the live source is
  partial too and the live F4 verdicts are null for the same reason (not measured:
  keyless).

## Options

1. Per-relation completeness: the build records each relation's full traversal;
   `_carrier_problem` accepts a Portal binding whose own relation (and `via` relation) is
   fully captured, not stale and granted. Keeps permission monotonicity: a full-grant
   reader reads the same relation.
2. Scoped Portal claims: record a complete read scoped to the root's key per relation, as
   for BC. More reads, same effect for one record.
3. Keep source-level completeness: F4 stays undecided on any partial Portal capture.

## Decision

Option 1. It is the smallest change that keeps "decided only when every source that
could change it is proven complete": completeness of the relations the side reads is
that proof.

- The projection writes `store_meta.relation_completeness`: source family -> bound
  relation -> `{completeness, full_traversal_at, stale}`. A relation is `complete` only
  when its capture source records a completed full traversal of it
  (`relation_state.last_full_traversal_at` for the mirror); `stale` when it is served
  from a persisted capture or a held mirror copy. A family with no capture source has no
  entry. The family's `freshness.completeness` is derived from the same per-relation
  facts, so the two never disagree.
- `_carrier_problem` accepts a Portal carrier binding (the far binding and its `via`
  binding, each checked) when the family is not complete only if its relation's entry is
  `complete`, `stale` is exactly false, `full_traversal_at` is a timestamp at or before
  the answer's `as_known_at`, and the build report names no `source_capture_incomplete`
  for that binding. Every other check stays in force before it: the answer is about now,
  the reader holds the markings and the relation's grant, no binding gap, and a stale
  family stays unproven whatever its relations say.
- Never inferred from row counts. A partial, stale, unknown, late, date-only or malformed
  entry, or no entry, leaves the carrier problem as before (`scope_not_read`).
- BC is unchanged: a BC hop still needs a complete family or a scoped claim; a BC
  relation entry proves nothing.

## Acceptance

- Tests (isolated-fixture): tests/test_ontology_tools_process.py
  `PortalRelationCompletenessTests` (decided with the read relations complete and the
  source partial; undecided for each read relation partial, stale, unclaimed, late,
  date-only or unknown, for a stale family, a report-incomplete binding, malformed meta,
  a withheld grant; a BC relation entry proves no BC hop) and
  `PermissionMonotonicityTests.test_holds_when_portal_relations_are_complete_and_the_family_partial`;
  ontology/tests/test_projection.py
  `RebuildTests.test_relation_completeness_is_each_relations_own_capture_claim` (the
  build records each relation's own claim from a mirror with one relation never fully
  traversed and one persisted).
- Live, after landing: the F4 items decide on the daily projection where their relations
  are fully traversed.

## Reverses if

- A mirror relation's `last_full_traversal_at` turns out to survive a later capture that
  lost rows without marking them missing (the full traversal would then not bound the
  relation's current rows).
- A Portal relation is found whose rows a reader's grant can withhold item by item
  (`api:` grants are per relation today).

## Limitations

- An incremental refresh after the last full traversal keeps the relation `complete`, as
  the family-level rule already did; deletions between full traversals are only seen at
  the next full traversal.
- A relation entry carries no age limit at read time: one fully traversed long ago stays
  `complete` until it is marked `stale`, no weaker than the source-level rule.
- No end-to-end test runs plx.process over a store built from a real mirror with one
  relation missing; the evidence is isolated-fixture and live-unverified.
