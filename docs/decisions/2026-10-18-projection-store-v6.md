# 2026-10-18 projection store format v6 (option 2')

- decided: 2026-10-18
- paths: ontology/store_ddl.py, ontology/store.py, ontology/project.py, ontology/project_incremental.py
- retires: open-projection-disk-need-store-format
- status: decided

Evidence base: main dabe69ce and three passing ontology.session_projection runs on layout 2 (2026-10-18 00:46Z, 04:38Z, 09:00Z): store 6.30 GB against 6.8 GB projected; build peak 11.18 GB of 23.5 GB free (47.6%); 1.60M objects; etl_duplicates_collapsed 22,303. Planned against 2026-10-17-answer-correctness-relation-completeness.md, which this record leaves unchanged.

## Decision

CONFIRMED: option 2' (`plx-ontology-store/v6`).
- `object_values` and `links` stay v5-shaped views with INSTEAD OF triggers, so every reader and writer keeps its SQL.
- Provenance is shared per record version (`value_sources`); link reason and evidence texts are interned (`link_skeletons`, `link_texts`).
- A write through a view reports rowcount 0 (`ontology/store_ddl.py:51-54`); no product reader uses rowcount, and a writer reads its rows back.
- A duplicate logical key is refused at commit, not at insert.

## Residual risks

- About 0.6 GB of headroom under the 50% line (peak 11.18 GB of 23.5 GB free is 47.6%).
- Work file 5.07 GB live against 2.8 GB projected.
- Link reads are about 3x slower than v5.
- `ontology.projection` has no layout-2 result yet.

## What would show it wrong

- A layout-2 daily peak above half of free space, or `ontology.projection` SQLITE_FULL.
- A reviewed pair of real rows merged by the collapse that `gen.silver` holds as two.
- `ontology.traverse` or `ontology.mentions` over budget from link reads.
