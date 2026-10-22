# Decision 2026-10-04: scope-freshness (how plx.find states a source served from a persisted capture)

- decided: 2026-10-04
- context: Portal Workbench API `GET /v1/health` answers HTTP 500
  `internal` since 2026-10-03 03:33Z; every business question failed with `MirrorError`
- status: decided

## Decision

1. **Scope freshness for every source of the planned scope.** `ontology/resolve.py`:
   every probe of a unit the reader may read states how it was served
   (`Probe.freshness`: `live`; `cached_mirror` for a relation the mirror holds whose
   refresh failed, as of its real `last_refresh_at`, with the HTTP status, error code
   and label; `persisted_capture` with its capture
   time and label; or `none` with the HTTP status and error code); `_take` records it for every probe
   into `Resolution.scope_freshness`, keyed by every source family of the plan. One
   helper, `OntologyTools._with_scope_freshness`, serves both answers of
   `_find_sources` (records found, or no record found).
2. **The contract carries it.** `$defs.freshness` in
   `operating/ontology_tool_contracts.json` adds `label`, `served_from`
   (`live` | `cached_mirror` | `persisted_capture`) and `relations[]` (the units not
   served live, each with `served_from` `cached_mirror` | `persisted_capture` | `none`, `captured_at`, `label`,
   `http_status`, `error_code`). Freshness gaps come first in `gaps` and are never cut
   (at most two per source family plus schema drift, within the 20-gap bound).
3. **No absence over a copy.** A no-match over a copy reads `unknown`
   (`no_visible_match`, the reason from `operating/absence_claims.traversal_problem`),
   never `absent_in_source`. No copy available: the source has one
   `inaccessible` / `source_unavailable` gap naming the HTTP status and error code, and
   its units are listed with them in `meta.freshness[].relations`.
4. **A copy is never action authority.** `operating/capability_guards.current_reader`
   refuses every row whose citation resolves with `source_freshness` (a row filled from
   a persisted capture), however recent the copy; `plx.case_get` reports such a
   citation `stale`, never `current`; `plx.case_propose` verify mode
   (`operating/case_verification._read_back`) answers `unknown`
   (`relation_served_from_persisted_capture`) over such a row, never an observed or
   not-observed effect.
5. **Identity is unchanged.** Exact and alias matches decide `plx.find`'s status; leads
   never count toward ambiguity.

## Reasons

- Honesty about staleness: a copy is a past capture. Every answer that rests on it says
  so in machine-readable form (`served_from`, `stale`, `captured_at`) and in the one
  human label `source-mirror captures as of <captured_at>, live source unavailable
  (<http_status>, <error_code>)`, on found candidates and on a no-match alike. It is never presented as live, and a no-match over it never claims
  absence.
- A copy is advisory, never action authority: an action precondition must rest on a
  current read of the source. The
  capture store keeps captures outside Git, logs and advisory records; the case runtime injects it only when `PLX_WORKBENCH_STATE_DIR` is set
  explicitly, and tests always inject a temporary store.

## Evidence

`injected-transport`: `integrations/tests/test_source_mirror_persisted.py` (an HTTP 500
`internal` injected on `/v1/health`, FakeWorkbench rows, every `plx.find` output checked
against the contract) and `operating/tests/test_capability_guards.py`
(`CopiedRowPreconditionTests`). Live behaviour is `live-unverified` until
`verify/live-checks/mirror.persisted_fallback.json` records a result; until the API
answers once and a complete sync is persisted on the verifier host, no copy exists and
that check fails with `missing_mirror`.
