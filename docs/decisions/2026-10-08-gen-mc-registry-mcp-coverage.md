# gen-mc-registry-mcp-coverage: how action-typed registry entries leave the coverage gaps

- status: decided
- decided: 2026-10-08, replacing `open-gen-mc-registry-mcp-coverage.md` (2026-10-07)
- gaps: gap-109c7c494f8a (`registry:plx-mc.mcp_tool:action_type`, 5 entries),
  gap-4e9b0fd4c236 (`registry:business-central.web_service_action:action_type`, 2 entries);
  gap-799cbc8aaa44 (`registry:business-central.web_service_action:no_source_family`) closed
  earlier when `ontology/schema/domains/bc_bridge.json` declared both BC actions.
- question: an entry an action type declares (`registry_entries`) stopped at `bound` with
  reason `action_type`, so the 5 MC writes (`mc_checkout_task`, `mc_complete_task`,
  `mc_create_task`, `mc_report_progress`, `mc_request_approval`) and the 2 BC web-service
  actions (`PLXPurchaseOrderRef_EnsureOrderManualNos`,
  `PLXSalesBridge_EnsureSalesOrderManualNos`) were permanent gaps.
- options: A. align the MC and brain families with their live sources (reads only);
  B. treat an action as covered: by a passing read-back live check, or as terminal because an
  action is verified per call, never captured; C. leave them as gaps.
- decision: B, terminal variant. `scripts/source_registry.py` gives an entry whose only data
  keys are action types on served targets the terminal status `bound_action` (schema-only:
  not in the coverage denominator, not a gap). Its proof is per call: each action type's
  `verification: read_back` and its approval route, never a capture or a live check of the
  write. An entry with data keys as well goes through the data stages on those keys alone.
  An action type on a not_served target stays `declared`/`not_served`.
- not decided here: option A for the 8 bound MC and brain read tools (they stay
  `no_live_check` / `no_live_results` gaps until a live check aligned with their binding
  family names them). The BC action lifecycle (executing the write after an MC approval)
  belongs to twin-bc behind the approval door: no `operating/action_catalog.json` entry here.
- evidence: `tests/test_gen_bc.py` (BC actions `bound_action`, absent from `uncovered`),
  `ontology/tests/test_generator_mcp.py` (the 5 MC tools reach `bound_action`),
  `tests/test_source_registry.py` (a declared action entry is terminal); isolated-fixture.
- reversal: an action executed by the Workbench itself (not a human in the owner system)
  gets a live read-back check; then B's read-back variant replaces the terminal status.
