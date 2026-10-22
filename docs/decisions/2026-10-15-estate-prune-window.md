# Decision 2026-10-15: a superseded SharePoint version keeps its bytes for one complete walk

- decided: 2026-10-15
- status: decided
- source: task estate-refresh-priority ("the store keeps only the last complete capture of each root"), candidate e0bd86d

## Question

When a root's full walk captures everything, which superseded versions lose their bytes and text?

## Options

1. Every version no longer current (strictly "only the last complete capture").
2. Every version superseded before that walk started. A version the walk itself superseded keeps its bytes until the next complete walk (one capture of grace).
3. No pruning. Rely on the default store bound alone.

## Decision

Option 2, the shipped default. A version superseded during a walk keeps its bytes and text until the next complete walk of its root. After that its `#sha256=` citation no longer resolves, but its metadata, permissions, digest and interval stay.

## Reasons

- Option 1 breaks two citation-provenance invariants on main, and weakening them is not allowed:
  - `integrations.tests.test_capture_sharepoint` `test_recapture_on_etag_change_keeps_both_versions`: the superseded version still resolves to exactly what it cited.
  - `tests.test_value_review` `test_a_cited_page_is_taken_exactly_when_plx_read_resolves_it`: a reviewer's evidence on the superseded version's page still reads after a recapture.
- Option 2 keeps both invariants and still bounds the store: at most the last complete capture plus the versions superseded during it. The store tests prove the older capture is dropped.
- Option 3 lets the store grow without limit until `store_full`.

## Not planned

The other notes are not planned: no capability or live defect depends on them.

- Root full-walk age is on the store resolve answer and on completeness, but not yet on case-tool or `plx.read` answers that cite a refreshed root.
- A lost change feed shows its age from the placeholder 0001-01-01.
- `capture_breadth` still sums all stored files, with no store limit.

No code change.
