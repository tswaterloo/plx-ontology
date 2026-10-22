# Decision 2026-10-10: the Ask loop stays read-only; plx.investigate is listed as what it serves

- decided: 2026-10-10 (gap-b1612ae3e3fe)
- status: decided; replaces the open file `open-follow-ups-routing-ask-loop-writes.md`
  (deleted here; its question and options are in git history before this commit)

## Question

`plx.investigate` gained a `follow_ups {case_id}` option that records case follow-ups and
drafts through an action path, but it was still listed `readOnlyHint: true` and its contract
`read_only: true`. The Ask loop (`operating/agent_loop.py`) offers exactly the tools listed
`readOnlyHint: true`, so over a server with an action path (`--enable-action-path`) a planner
could record follow-ups, and an auto-approving MCP client could write case records unconfirmed.

## Decision

1. **The Ask loop stays read-only** (option 1 of the open file). `agent_loop.WRITE_OPTIONS`
   names `plx.investigate`'s `follow_ups`: the loop's catalog strips it from the input schema
   it shows the planner, and a call carrying it is refused (`option_not_available_to_loop`)
   before the server sees it. Follow-ups are recorded only through the action path: an MCP
   client's `plx.investigate` on a host with a recorder, or the console's FollowUps.
2. **The listing follows the recorder.** A server whose `plx.investigate` records follow-ups
   (its ontology host's `follow_up_recorder`, else its own action path and session) lists it
   `readOnlyHint: false, destructiveHint: false`; a server without one lists it
   `readOnlyHint: true` and refuses the option (`follow_ups_not_configured`). The contract is
   one file for every host, so it states the capability: `read_only: false` (the tool writes
   case follow-ups wherever a recorder is wired). Consistent with (1): over an action-path
   server the loop is offered no `plx.investigate` at all (it is a writer there); over a
   read-only server it is offered without `follow_ups`. The console's Ask builds its server
   without an action path, so it keeps the plans.
3. **Host-state gaps ask nobody.** `HOST_STATE_REASONS` (`source_not_configured`,
   `freshness_unknown`, `projection_not_built`) become no follow-up, whoever the resolver
   names (they were addressed to `undeclared:Workbench operator` / `undeclared:Business
   Central owner`, or to the root's department). They stay in the answer. The third reason is
   added as the same class: "this host has no built projection store".

## Evidence (isolated-fixture)

- `operating/tests/test_agent_loop.py`
  `test_the_loop_records_no_follow_up_even_over_an_action_path`: on base 37e539e6 the loop
  over an action-path server recorded 16 case gaps; now none, with and without the path.
- `tests/test_ontology_tools_investigate.py`
  `test_the_listing_follows_the_recorder_and_host_state_gaps_ask_nobody`: annotation per
  recorder, contract `read_only` false, host-state gaps unrecorded (base recorded both).
- `operating/tests/test_mcp_action_tools.py`: the stdio host lists `plx.investigate` as a
  writer with `--enable-action-path` and read-only without it; the in-process host records no
  host-state gap.

## What would reverse it

The owner wants the Ask loop to record follow-ups (open-file option 2): then the loop gets the
console's action path with `plx.investigate` as its one case write, a test that every other
case write is still refused, and the loop's rule text changes.
