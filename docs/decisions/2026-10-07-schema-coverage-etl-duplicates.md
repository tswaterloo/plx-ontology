# Decision 2026-10-07: The gold relations the Portal ETL re-inserts stay raw-only

- decided: 2026-10-07 (resolves open-schema-coverage-etl-duplicates)
- status: decided

## Decision

Option (a), restated for the generated schema path: no curated domain file binds `app_manufacturing_order_line`, `app_assembly_order_damage`, `app_assembly_order_status`, `app_batch_schedule`, `app_production_task` or `app_manufacturer`. The curated types of record are their FM bronze originals (AssemblyOrderComponent, the AO damage and status types, BatchSchedule, the production type and Manufacturer). The generator keeps its contract of one raw type per served relation (`Rel<Relation>`, key `id`, lineage `gold_native_staging`), so a reader can still open a copy, but `gold_native_staging` is never authority without a human confirmation (`ontology/schema.py` `NEVER_AUTHORITY_UNCONFIRMED`) and no curated link, identifier or process reaches it. Options (c), a de-duplication rule in the C0 binding format, and (d), a natural binding key, are rejected. (c) changes the frozen format and the projector, and a natural key such as (MO, product) also merges two genuine lines. (d) breaks the binding-key = catalog-key contract that citations and the mirror lookup rely on.

## Evidence

- `portal/scripts/etl-silver-to-gold.ts` (plx-customer-portal@790256ca43) inserts the six relations under a fresh `cuid()` every run with `ON CONFLICT (id)`. No served column holds the silver source key. `portal/src` has no Prisma writer for any of them.
- The growth is measured live by `verify/live-checks/mirror.relation_counts.json` (five of the six by name). Under an owner rule a relation classified `data_truth_defect` on two daily runs leaves the completeness denominator and is read key-only.
- The domain files already say so: `catalog_parties.json` (Manufacturer), `manufacturing.json` (AssemblyOrderComponent, damage) and `production.json` (BatchSchedule).

## Residual

A sum over a raw `Rel*` copy counts each ETL copy. Revisit (c) when the Portal ETL keeps a stable source key or a C0 format change is accepted.
