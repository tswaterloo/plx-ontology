# Decision 2026-10-07: Three source families are out of scope as business-data sources

- decided: 2026-10-07 (against main 488da5b7)
- status: decided

## Decision

Out of scope as business-data sources: AWS Secrets Manager (gap-62bc46749a46), trading reporting replica/env hosts (gap-0e03bf6869c2) and petralabx GitHub repos (gap-2859bfbcb7ec; dev-ops metadata, revisit if a use case needs it). VMC brain search stays in scope.

## Open point

ontology/source_registry.json is generated (`python3 -m scripts.source_registry generate`, never edited by hand) and has no exclusion data or generator input to carry these reasons, so recording them there needs a generator change, left to a follow-up task.

Closed: `ontology/source_scope.json` names the decided entries (by `<system>.<kind>` or entry id) with this record; the registry gives them status `out_of_scope` and leaves them out of `uncovered`.
