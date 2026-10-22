# Decision 2026-10-07: roles of the Mission Control, brain and VMC context MCP tools

- status: decided
- gaps: gap-c71a7ca99e19 (plx-mc.mcp_tool, 37), gap-58979a9f79d6 (plx-brain.mcp_tool, 11),
  gap-b3672692eb3a (vmc-context.mcp_tool, 11)

## Question

Which of the 59 `plx-mc`, `plx-brain` and `vmc-context` MCP tools of the source registry
are ontology sources, which are action types and which are not sources at all?

## Decision

Every tool has exactly one role; `python3 -m ontology.generator.mcp generate --check` fails
otherwise.

- **Bound** (a curated binding types the records the tool returns; its `registry_entries`
  names it): `mc_get_context` and `mc_search_tasks` (MCTask), `mc_get_task` (MCApproval,
  MCTaskEvent), `mc_list_buckets` (MCBucket), `mc_search_knowledge` and `brain_search`
  (KnowledgeEntry: the same brain searchKnowledge core), `brain_get_node` and
  `brain_timeline` (KnowledgeItem: memory items; other graph node kinds stay untyped).
- **Action types** (the five approvable MC writes of `integrations/mc_tool_policy.json`,
  generated into `ontology/schema/domains/mission_control.json` from the pinned input
  shapes): `mc_checkout_task`, `mc_complete_task`, `mc_create_task`, `mc_report_progress`,
  `mc_request_approval`. They describe the call; the only send path stays the MC write door
  (`integrations/mc.py` `call_write_tool`, per-call human approval).
- **Excluded** (`ontology/source_scope.json` names each entry id with this record), with
  the reason per tool below.

Evidence: the pinned registrations (`ontology/schema/generated/meta/mcp_tools.json`, PLX_MC
`77a5af6`, agentic-swarm `e228067`), `integrations/mc_tool_policy.json`,
`integrations/plx_hub_guard.py` (MC never-sent tools, the brain's GET-only grammar).

### plx-mc reads that are not business or work records

| Tool | Reason |
|---|---|
| `mc_self_check` | Auth and reachability probe; returns no records (used by `mc.brain_probe`). |
| `mc_list_skills` | The PLX skills catalog: agent instructions, not records the Workbench types. |
| `mc_install_skills` | Returns skill packages to install into a local runtime; agent tooling. |
| `mc_sync_skills` | Skill package sync for local runtimes; agent tooling. |
| `mc_list_checkouts` | MC checkout leases; MC is the checkout authority (GOV-12, AGT-04): no second view. |
| `mc_list_conflicts` | MC <-> SharePoint mirror sync-conflict queue; MC's own operations. |
| `mc_verify_pr` | GitHub PR compliance verdicts; petralabx GitHub is out of scope (2026-10-07-source-scope-out-of-scope.md). |

### plx-mc tools the Workbench never sends

The policy's `never_sent` class: `plx_hub_guard` refuses them before any request. Each is
an MC write outside the approvable set, a routing mutation, a swarm call or a read the
Workbench does not need (PLX_MC `77a5af6`); none returns records the
Workbench types.

| Tool | Reason |
|---|---|
| `mc_attach_checkout` | Routing mutation (attach a checkout to a proposal). |
| `mc_confirm_existing` | Routing mutation (confirm a task for a proposal). |
| `mc_create_routed_task` | Routing mutation (create a task from a proposal). |
| `mc_suggest_work` | Routing session write (creates a routing session); never sent. |
| `mc_create_bucket` | Bucket write; MC owns buckets. |
| `mc_update_bucket` | Bucket write; MC owns buckets. |
| `mc_create_project` | Project write; MC owns projects. |
| `mc_update_task` | Task metadata write outside the approvable set. |
| `mc_update_tasks` | Batch task metadata write outside the approvable set. |
| `mc_release_checkout` | Checkout lease write; MC is the checkout authority. |
| `mc_release_checkouts` | Batch checkout lease write. |
| `mc_dismiss_conflict` | Sync-conflict write. |
| `mc_dismiss_conflicts` | Batch sync-conflict write. |
| `mc_resolve_conflict` | Sync-conflict write. |
| `mc_resolve_conflicts` | Batch sync-conflict write. |
| `mc_submit_skill` | Skills catalog write. |
| `mc_report_session_telemetry` | Agent session cost telemetry write. |
| `mc_update_project` | Project steward write (status, owner, archive; TASK-2530/2532); MC owns projects. |
| `mc_link_merged_pr` | Steward-only merged-PR link (`task.link_merged_pr`, `sp_mcp_ledger` only; TASK-2559). |
| `mc_upload_evidence` | Evidence upload to the MC SharePoint library (diagram, screenshot, web page, video; needs a live checkout and `task.complete`; TASK-3166/3167, PLX_MC `e857f8c`): a write. |
| `mc_list_projects` | A read the Workbench does not need: project names, owners, status and counts, no visibility or members (so it cannot settle 2026-10-07-mc-shared-project-grant.md); tasks and buckets already carry the project id. Kept out of the read door. |
| `mc_get_approval_gate` | A read the Workbench does not need: gate state is read through `mc_get_task` (MCApproval); this one is bound to the raising principal, long-polls up to 25 s and takes a bearer `checkoutId` (TASK-629). Kept out of the read door. |
| `dispatch_to_swarm` | Swarm dispatch; stdio-only, not served by the HTTP MCP server. |
| `list_swarm_teams` | Swarm routes; stdio-only. |
| `swarm_health` | Swarm health; stdio-only. |

### plx-brain

The Workbench reads the brain with GET only (`integrations/vmc.py`, `plx_hub_guard`
`check_brain_request`); it has no brain write door and no brain MCP client.

| Tool | Reason |
|---|---|
| `brain_self_check` | Knowledge-plane health probe; no records. |
| `brain_get_subgraph` | Derived neighbourhood view over nodes (inferred edges are T3 leads, not records). |
| `brain_trail` | Derived path between two nodes; a view, not records. |
| `brain_query` | Search plus a best-effort expansion; a view over hits already typed. |
| `brain_find_gaps` | Lint summary of the brain itself. |
| `brain_run_lint` | Returns an operator cron command; reads nothing. |
| `brain_ingest` | Brain write; the Workbench never writes the brain. |
| `brain_propose_relation` | Brain write (inferred link); never sent. |

### vmc-context

VMC todos are a separate lease system; MC owns tasks and checkouts (GOV-12) and the
Workbench adds no redundant task store (AGT-04). It has no VMC context client.

| Tool | Reason |
|---|---|
| `vmc_self_check` | Setup probe; no records. |
| `vmc_get_context` | VMC projects and todos: a second task store, not adopted (AGT-04). |
| `vmc_get_roadmap` | VMC work queue: as above. |
| `vmc_get_repo_health` | Cached code-health scan of repositories; dev-ops metadata. |
| `vmc_get_unused_candidates` | Cached dead-code candidates; dev-ops metadata. |
| `vmc_get_dependency_risks` | Cached dependency-graph analysis; dev-ops metadata. |
| `vmc_checkout_task` | VMC todo lease write; never sent. |
| `vmc_claim_handoff` | VMC handoff claim write; never sent. |
| `vmc_report_progress` | VMC todo progress write; never sent. |
| `vmc_complete_task` | VMC todo completion write; never sent. |
| `vmc_create_todo` | VMC todo creation write; never sent. |

## Options

- Bind every read tool: rejected for the tools above; they return no records the Workbench
  types, or records MC alone owns.
- Action-type every write: rejected for writes without a Workbench write door (brain, VMC,
  the never-sent MC writes); an action type without a guarded send path would describe a
  capability the Workbench must not have.

## Reversal

A use case that needs one of these records (for example checkout history as evidence of
who worked a task) binds that tool through a capture and a live check, and its source
scope line is removed. A new upstream tool has no role and fails the check until it gets one.
