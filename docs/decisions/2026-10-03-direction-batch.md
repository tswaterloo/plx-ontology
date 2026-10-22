# 2026-10-03 direction batch: projection budget, injection boundary, identity, eval semantics

Main d92c092; Portal 022e51d; MC f4ed0b1. Key claims were re-checked against the cited lines.

## Context
Live `eval.unfamiliar_sample` scored 0/28 on bc5ab8e (`product.status budget_exhausted`,
`build_seconds 0.0`); `ontology.projection` timed out at 1800 s. Twenty-two decisions
were open, several on permission, identity and evidence paths.

## Decisions

### A. Capability measurement (projection budget)
- Cause of 0/28 is ordering, not build speed: `scripts/eval_unfamiliar_sample.py:673-682`
  runs the oracle reads (`dev.build_live`, `sample`) before `product_from_build`, so the
  shared deadline is gone (`:393-394` returns `budget_exhausted`, 0.0 s). 8391fc4, which
  got leftover time, scored 14/14 and built 21,091 objects in 35.7 s.
- `ontology.projection` stages every non-unstable catalog relation
  (`scripts/mirror_measure.py:669-672`), including ~1M-row unbound relations, then runs
  two unbounded builds and 2,000 claim checks.
- Decision: the eval builds the product first with a fixed share; the projection probe
  and the eval build sync bound relations only; the projection pass condition is bound
  completeness (as in 2026-10-02-capture-persistence.md), not `mirror_complete`.
  627f83c (build share 0.5, probe SYNC_SHARE 0.6) lands first.
- projection-memory: resolved (a), done (2,769 -> 167 MB peak RSS).
- projection-memory-streamed: (b) bound relations only, full streamed rebuild stays; no
  incremental build until build time is shown to be the limit.
- mirror-versions-export: (a) `source_mirror.iter_versions` as the one owner (low).
- ontology-tools-store-conventions (+ -l7): (a), with `store_meta.audience_keys`
  (the ontology tools stop `SELECT DISTINCT audience_key` per call) and naive timestamps stay naive.
- bc-incremental-store-state: (a) now; (b) when a BC capture source joins the projector.
- Wrong if: a bound-only build still exceeds ~600 s live, or the eval scores < 20 items.

### B. Injection boundary and action provenance (safety)
- injection-firewall-sites: (a). Gap on main: the MCP result path
  (`operating/mcp_server.py:743`) never calls `injection_guard.firewall_tool_output`
  (no non-test caller); original-page `text` and mail `body_text` reach the model raw.
  Wrap once at the output boundary; test `bare_text_leaks` on every tool result.
  Source: OWASP LLM01 (https://genai.owasp.org/llmrisk/llm01-prompt-injection/).
  Raw text does not reach an action value today (`action_path.py:808-816`).
- injection-provenance-scope: (a) plus a role rule: refuse a traced value when both
  columns have a role in `source_unit_contracts.json` and the roles differ; move to (b)
  once verified links exist. Add eval items for cross-role and free-text traces (the
  51/51 injection eval covers neither). Source: CaMeL provenance-tagged values.
- action-catalog-format: (b), as built in 53c0120. Identity-review records come
  only from human sessions; draft author kind follows `CHANNEL_AUTHOR`.
- projection-hop-audience: (a) now; then (d) the read gate also requires the hop
  target's relation from schema `via_link`; (b) if the target audience varies per row.

### C. Session identity
- session-identity-ticket: Q1 (c) now, target (b) Entra auth code + PKCE keyed on the
  Entra oid as Portal and MC do (needs an app registration: later_ask, not a blocker);
  Q2 snapshot default; Q3 (a) probes never stamp `human`.
- Delegation gap: MC `approval-actions.ts:37` records only the Workbench operator as
  `requestedBy`, so MC's separation of duties cannot stop the proposing employee from
  deciding their own gate. Workbench side now: record subject and actor on the intent
  (RFC 8693 `act` shape) and refuse dispatch when the decider equals the session
  employee. MC side is an upstream ask (never pushed from here).

### D. Eval semantics (mirror the Portal, cited)
- oracle-closed-vocabulary: (a) using the Portal's derivations: PO closed =
  `derivePoFulfillmentCode(lines)` is RECEIVED or `closed_at` set
  (P:src/lib/mrp/po-fulfillment-status.ts); today `evals/oracle/keys.py:246` checks the
  header only. Report out-of-vocabulary counts per relation.
- oracle-f3-definitions: shipped = status in {SHIPPED, DELIVERED} (confirm route, and
  pack-out-history.ts:274); PICKING/STAGED are not shipped. `no_linked_assembly_order` is
  `not_ingested` (FM ETL never sets `sales_order_line_id`), not `absent_in_source`.
- eval-produced-role: (b) produced follows the Portal derivation chain
  (SUM status qty_manufactured, else yield_actual; manufacturing.json:4771) with a
  `derived` label; never copy the `|| quantity` fallback.
- eval-unfamiliar-sample-scoring: (a) plus tier: `found_exact` uses find_root's reason
  gate; add `found_lead`, success@1 and MRR; report rate over sampled and over scored.
- find-root-exact-compare: (b) one `exact_compare` per identifier kind read by both
  plx.find and find_root (SO case-sensitive trim; PO trim + case-insensitive, no `%`).
- find-secondary-why-not: (a); the gold_native_staging lead cap also emits why_not.
- traverse-semantics: keep (a)'s tier floor; `source_recorded` times of `gold_etl_*`
  lineage get their own order class (ETL time is not a business date).
- mention-link-polymorphism: (a) concrete links for storage, generated by the loader
  from one rule, plus (b) an interface link for navigation (Foundry interface links).

### E. Defaulted (settled only by live data not yet available)
mirror-lineage-unclassified, schema-generated-meta-location, bc-company-lineage-slugs,
bc-link-company-scope, supplier-code-bc-vendor, sage-multi-po-receipts, identifiers-2,
identifiers-3, identifiers-4: interim defaults kept; see each entry.

## Consequences
The firewall wrap and the decider != employee check are safety work and go before feature work.

## How we will know if it was wrong
A: next live unfamiliar eval still scores < 20 or the bound build exceeds 600 s.
B: a tool-result leak test or a new injection item fails after the wrap.
D: live profiles show FM status words the Portal classifies elsewhere, or yield_actual
differs from SUM(qtymanuf) where both exist.
