# 2026-10-10 dev-baseline projection budget: one session projection (option 3)

Landed `187403b9d258250466e8c50a3c581aa23c2d1185`.

## Question
Where does `eval.dev_baseline`'s projection come from once it reads the verifier session's
full Portal mirror (`{session_state}/mirror.sqlite`, filled by `mirror.full_coverage`)? The run
keeps 600 s for the projection and 30 items. The mirror held 1,489,088 current rows over 93
relations on 2026-10-10, and `ontology.project.build_store` projects about 1,450 rows/s on
a 4-core container. A full projection takes far longer than the reserve. The one run on the
session mirror timed out at 2,400 s with no record, and a cold `ontology.projection` of the
same mirror failed after 2,857 s on 2026-10-07.

## Options
1. Raise the check's budget (for example 2,700 s, timeout 3,300 s). Simple, but the daily
   session grows with the mirror.
2. Project only what the items' answers can read (plan relations, identifier carriers and
   their lineage and value dependencies via `ontology.project_incremental.plan`).
3. One session projection: a producer keeps an incremental projection
   (`ontology.project_incremental.build_generation`) in `{session_state}/projection`, and the
   eval checks read it read-only, as they read the mirror.

## Decision
Option 3. Check `ontology.session_projection` produces the generation once per session. Its
consumers (`eval.dev_baseline`, `ontology.projection`, `ontology.traverse`,
`workspace.object_view`, `ontology.mentions`, `process.dependencies`) open it read-only when
the session committed it and every unit key matches, and project only what differs.

## Reason
A full projection of about 1.49M rows at about 1,450 rows/s never fits a consumer's budget,
and every consumer projected the same mirror cold. Paying the cost once per session removes
that repeat work. It is also the product's own shape: the persistent host keeps one
projection.
