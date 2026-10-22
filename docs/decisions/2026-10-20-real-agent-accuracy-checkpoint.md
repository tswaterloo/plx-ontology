# Decision 2026-10-20: real-agent-accuracy lands as a checkpoint; the 6/10 bar moves to a plan change

- decided: 2026-10-20
- cites: docs/decisions/2026-10-19-b1-one-answer-rate.md (unchanged)
- folds: open-real-agent-accuracy-fixture-bc (option 2, below; its open- file is removed)
- status: decided

## Question
`fixture_sample` (10 items, k=1, real Claude Code sessions over the stdio MCP host) holds at
4/10 correct, confident_unsupported 2, after fix sets a-i (baseline 0/10, cu 5). Keep the
bar (>= 6/10, cu 0), or land and split?

## Evidence
- n=10 at k=1: the error bar on a share is about +/-30 points, so 4/10 and 6/10 are not
  distinguishable on one run; the a-g, a-h, a-i runs went 2, 4, 4 (cu 3, 4, 2).
- On the same 10 items the deterministic path over the same plans scores 5/10
  (`evals/baseline/dev-fixture.json`). It fails F1-0001 gap_missing, F2-0004 value_missing,
  F3-0006 cited_other_row and F4-0001/0004 answerability_mismatch. The agent's ceiling is the
  plans' ceiling: the plateau is in the plans, not the agent.
- F2-0004 hits the plans' never-zero count rule (`operating/ontology_tools_investigate.py`,
  module doc and `_count`: nothing reached is a gap, "not a count of zero"). F4 hits the
  lead-tier evidence rule. Both rules stay. Per-item classes:
  `evals/bench/real_agent_failures.json` (`after.remaining`).

## Options
- (a) keep the bar here: needs plan changes and gives no slack at k=1 (+/-30 pt).
- (b) land as a checkpoint; move the plan changes to a follow-on change.
- (c) lower the B1 bar. Rejected: B1 is unchanged.

## Decision
(b). Checkpoint acceptance: rebased on main; a unique scorer version (`plx-eval-scorer/14`:
main took /13 for the prose screen); `answer_gate.check_text` runs `claim_checks.screen_prose`
(the scorer's prose screen); one post-rebase real run shows correct >= 4/10 and cu <= 2,
stated with its +/-30 pt error bar; every failing item classified, F2-0004 as
`tool_returned_nothing_useful`. B1 stays one rate pair; the claim gate and the evidence rule
do not change.

Follow-on `plan-party-twins-proven-zero`: F1 same-number records of another party become
a declared gap via `resolve.corroborations`; a count is 0 only on a proven-complete search.
Its bar is `fixture_sample` >= 6/10 with cu 0.

Reversed if the post-rebase run gives correct < 4 or cu > 2, or the deterministic path passes
an item the agent fails twice.

## Live part
`verify/live-checks/eval.real_agent.json` is live-unverified: the keyless
container has no Claude Code login. Its known blockers are `PLX_EVAL_READER` unset (the live
part reads as the declared eval principal and makes no A6 permission proof) and the held-out
dataset absent. No credential or access change is made to close them; a credentialed
run records the result or the exact blocker in `real_agent_failures.json` (`post_rebase_run`).

Recorded at d1770f83a (scorer /14, k=1, +/-30 points): `fixture_sample` 5/10 correct, cu 0,
so the checkpoint bar holds. The live part ran and blocked upstream: `live_bc`
`session_secret_missing` (`PLX_WORKBENCH_SESSION_SECRET` unset in the run shell), with
`PLX_EVAL_READER` unset and the held-out dataset absent; left as named blockers.

## Folded open questions
- `open-real-agent-accuracy-fixture-bc.md`: decided, option 2. A `--fixture` MCP session may
  gate its fixture state's own BC capture with `--fixture-capture-audience` (refused without
  `--fixture`, with `--live-source-audience` or another tenant; no reader built, no credential
  read). Guarded by `operating/tests/test_real_agent_accuracy.py::FixtureCaptureHostTests`.
- `open-real-agent-accuracy-mcp-double-send.md`: stays open, default kept (both MCP copies on
  the wire); it needs a recorded Claude Code session showing which copy reaches the model.
