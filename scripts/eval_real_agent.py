"""Real-agent eval: Claude Code at k=1 on the product's MCP case host (tier ``real_agent``).

Measure the
real agent against the deterministic baseline, small and at most daily (it spends the
operator's Claude Code plan; no paid API).

Per run, one headless Claude Code session per item (``evals.runner.ClaudeCodeAgent``,
k=1, one phrasing), on the same tool surface an employee's Claude gets: the stdio case
host (``operating.mcp_server``) behind the runner's tool gate, read-only tools only (no
case-store writer, no ``plx.act`` or other action or MC write), the prompt's responsibility
and stop rules (``evals.runner.build_prompt``: cite or give a gap, never act). Items:

* **exposed**: every exposed real case (``evals/exposed``, F7-F9) on a throwaway fixture
  host with an empty mirror. They cite pinned originals and mail the host does not hold
  yet; an abstention there scores ``blocked`` (``originals_not_held``, counted), a stated
  answer is still scored, so a confident unsupported answer stays ``wrong``.
* **fixture_sample**: the pinned ``FIXTURE_SAMPLE`` of ``evals/dev`` on the fixture world
  (the frozen dev set's own synthetic sources, projected as in
  ``scripts.eval_dev_baseline``), each with its run pin; the deterministic baseline's
  outcomes on the same items and its whole-set rate are reported beside it.

* **live_bc** (``--live``): the headline measurement. At least ``--min-live-items`` (20)
  oracle items rooted in Business Central documents, sampled by the independent oracle
  (``evals.oracle.generator.generate``, the live sources of ``scripts.eval_oracle_dev``: its
  own GET-only reads, never the product) and answered by the agent through the persistent
  host's own MCP server (``operating.host mcp`` over ``PLX_WORKBENCH_STATE_DIR``: the BC,
  mail and SharePoint captures and their projection, no Portal read) as one named reader:
  ``--reader-env`` names the variable holding that employee's identity (JSON ``uid``,
  ``email``, ``entraTenantId``, ``entraObjectId``); each session gets a fresh single-use
  agent ticket for it, signed with ``PLX_WORKBENCH_SESSION_SECRET``, and the host resolves
  and trims for that employee (``LiveHost``). Without one (``--reader-env`` unset) the
  part reads as the declared eval principal (``operating.eval_principal``,
  ``examples/eval_principal.json``: its declared ``staff:`` roles only, no Portal row, no
  file clearance, no finance marking, never the action path), its ticket signed the same
  way, and every result says so (``eval.live.reader`` ``eval_principal`` with
  ``eval.live.principal``; ``eval.tiers.access``): never a fixture, never an employee, and
  never the A6 permission proof (``denied_leaks`` with a real identity), which still
  requires ``PLX_EVAL_READER``. The live-check runner provisions the host settings a run lacks
  (a fresh session secret, a state directory, its example host config); the part reads
  them as any other setting, and ``eval.live.host_provisioned`` names the ones it was
  given (``PLX_WORKBENCH_HOST_PROVISIONED``; names only, never a value; ``[]`` when none).
  An identity that is set but invalid is ``blocked:
  reader_identity_invalid``, never the eval principal (the ticket source is decided in
  ``docs/decisions/2026-10-07-live-agent-metric-reader-ticket.md``).
  Before any source is read the host must accept the reader: one harness session lists
  the tools; a reader the host's resolver refuses as an employee (``READER_REFUSALS``:
  inactive, unverified, not in the Portal snapshot, refused by Entra) is ``blocked:
  reader_refused``, a host that ends or stalls the session ``blocked:
  live_host_session_ended`` / ``live_host_read_timeout`` (the run still exits 0 and the
  fixture parts measure). Any other identity refusal (an invalid, expired or replayed
  ticket, a tenant or state mismatch) is the harness's or host's fault: the run errors
  (exit 1, ``{"error": "RunnerError", "code": ...}``), never a blocked check. Oracle
  strata the Portal outage stops are blocked parts (``live_<stratum>``), never the tier.
  Before any agent session the harness asks the host, as the reader, ``plx.find`` on each
  sampled root's identifier (one harness session within ``--root-probe-seconds``): a root
  it does not name is ``roots_unresolved`` (capture, projection or the reader's grant;
  never the agent's failure) and gets no session; roots
  the deadline (or a stalled read) left unasked are ``roots_not_probed`` and block the
  part (``root_probe_incomplete``, cause ``projection_timeout``; a host that ended the
  session mid-probe blocks it with that code, cause ``host_misconfiguration``); else fewer
  resolved roots than ``--min-live-items`` is a blocked part (``roots_unresolved``, cause
  ``missing_bindings``), never a pass. The resolved items are answered by the deterministic
  path (``evals.baseline.DeterministicInvestigator``, no model, the same host and reader,
  within ``--deterministic-seconds``) and by the agent.

**One reader** (``single_reader``): the live part reads as exactly the one employee the
operator names (or the eval principal when none is named); ``eval.tiers.access`` says so
with its reader count. The harness never picks
or adds a reader: a second reader (a cross-reader trim comparison) runs only when the
operator names one, and this harness has no setting for it yet.

**Tiers kept apart** (metric tiers): ``eval.tiers`` states each tier on
its own, so one tier's gap never reads as another's failure: ``access`` (the reader identity
and reader count, the oracle strata an outage blocked and the citation read gaps),
``capture`` (per source, the persistent host's own ledger: generation, completeness, how
it is served, stale), ``projection`` (its
generation and objects, and the sampled roots the reader's ``plx.find`` resolves),
``deterministic`` (the deterministic path on the resolved live items, and the recorded
baseline on the fixture sample) and ``agent`` (each part's correct / wrong / blocked /
justified abstention, coverage and accuracy).

**Citation re-reads**: the harness re-reads every cited handle as the reader through its
own session of the persistent host (``evals.harness_reads``, never an agent's tool):
``api://`` and ``original://`` citations as the case runtime's ``plx.source_citation``,
``bc://``, ``graph-mail://`` and ``sharepoint://`` ones from the host's captures once the
reader's own ``plx.read`` resolves them; a withheld, changed or uncaptured one is
unreadable (fail closed).

**Read gap** (``citation_read_gap``): a stated answer that fails only because the harness
could not re-read a ``bc://`` citation the host served the agent, on the item's chain, is
the capture/access tier's gap, not the agent's error. The scorer decides it
(``evals.score.read_gap``, the same rule on the deterministic path): scored ``blocked``,
counted in each part's ``read_gap`` (its other blocked runs in ``other_blocked``) and in
``eval.tiers.access``. Read-gap runs above ``evals.score.DRIFT_TRIPWIRE_SHARE`` of every run
fail the run as they fail the dev baseline (``eval.fail_reasons`` ``blocked_tripwire``,
``evals.score.fail_reasons``; exit 3), in the whole run or in any one part (each part reports
its ``blocked_share`` and ``blocked_tripwire``). A wrong value,
unit or answerability, a missing gap, an uncited value, a readable citation of another
record, an unreadable ``api://`` citation, or a ``bc://`` handle the host never served
whole or off the item's chain (fabricated, another company's) stays ``wrong``.

Each part reports correct / wrong / blocked / justified abstention with coverage beside
accuracy (``evals.score.outcome_summary``: abstaining everywhere is coverage 0, never a good
score), ``confident_unsupported`` (stated answers the claim gate refused), ``wrong_cited``
(stated answers every claim check passed whose value still does not match the oracle: cited
and wrong, never counted as unsupported) and the unsupported share of
the answered runs (``confident_unsupported_rate``, the number the answer-rate bar bounds; the three
rates are ``evals.score.answer_rates``, the dev baseline's too), refused tool
calls, action calls and the citations the harness could not re-read as the reader, by scheme
(``unreadable_citations``; a run failing only on unreadable ``bc://`` citations is a
**Read gap**). Sessions are capped (``--max-sessions``, the item count) and share one
budget; a session the budget refuses is ``blocked`` and the run is incomplete. No
Claude Code binary, or no session that produced an answer, is a ``blocked_upstream``
(``host_misconfiguration``), never a measurement, and names its code in
``agent_unmeasured`` (null otherwise: the check's ``blocked_when``, so only a run no agent
answered is blocked whole and every other run is judged on the parts that ran). Output:
counts, rates, enums only.

**Ask** (``ask``, its own daily check ``verify/live-checks/eval.ask_agent.json``): the same
admitted items, the same named reader and the same scorer, answered by the object
workspace's Ask instead of a Claude Code MCP client, through the workspace's own path
(``operator_ui.object_workspace.ObjectWorkspace.ask_run``, the one ``ask`` shows: its question
limit and framing, the ``plx.object`` root check, ``operating.agent_loop.run`` unchanged over
``HostAccess.agent_server``, the withheld-claim filter and the ``no_cited_claims``
downgrade), with a headless Claude Code planner (``ClaudeCodePlanner``, as
``operator_ui.case_console --ask`` wires it). Parts: ``live_bc`` (the live part above, always
on) and ``fixture_sample``. Per item the harness asks the reader's ``plx.find`` for the root
(as the employee picks the object in view); a root it does not name for the reader is
withheld and gets no session and no session slot (scored ``blocked``, counted
``roots_withheld``); one the workspace's root check does not show is withheld the same way
once its session started. The workspace
(``ask_workspace``) is the console's for that reader, built by the console's own factory
(``case_console.object_workspace_for``: the host's projection store, captures, capture
bounds and review store, read only), over the case runtime and the verified reader of the
host's own session start-up (``LiveHost.agent_session``: the ``operating.host mcp``
arguments, a fresh single-use ticket, the host's identity start-up; or the fixture host's).
The projection store is checked at start-up, before any source read or session
(``operator_ui.case_console.checked_store_status``, as the console checks it): the fixture
sample's own build errors on a refused store, and a live host without a store, or with one
projected from another capture generation or unverifiable, blocks the live part
(``ask_needs_a_projection_store`` / ``ontology_store_refused``); each session's workspace
checks it again, as the console checks each workspace it builds (a store refused since
start-up blocks that session, ``ontology_store_refused:<state>``). Only the claims the
workspace shows are scored: the answer is a
``plx-eval-answer/v1`` (``loop_answer``): each shown claim's value claims as cited values
(a role of the item vocabulary as ``quantity_role``; a count or sum claim as a calculated
value the scorer recomputes), its text the one the workspace rendered from the claims it
shows (``answer_gate.rendered_text``), each ``<gap state>: <subject>`` unknown as a gap (a snake_case
subject also as a value reported absent, the gap's ``concerns``), answerability
``answerable`` (answered),
``partial`` (answered with unknowns) or ``blocked`` (abstained or escalated); unsupported
claims and claims citing a record the workspace withholds are never part of it (the
withheld ones are counted, ``withheld_claims``). Each session record (the transcript's
``agent``) carries the effective agent definition's stamp (``_meta`` ``plx/agent``: id,
version, digest, limits and the run's state), the stop (outcome, reason and its class
``STOP_CLASSES``), the claims kept, shown, withheld, dropped and unsupported and the verified
context the session's tool results carried (``context_reuse``: link and identity tiers by
name, review decisions). A planner that cannot start, gives no usable reply (not logged in,
a usage limit, a timeout, an ``is_error`` result) or fails after one (``planner_process_failed``,
e.g. a usage limit mid-session), and a session whose start-up is refused or raises, or whose
workspace or session host fails to close (the other is closed all the same), is a
failed session (``blocked``, its code in the record), never the agent's answer and never the
end of the run; each session has ``--session-seconds`` from its start-up (later planner turns
escalate ``session_time_exhausted``). Each part reports ``ask``: stop classes, the distinct
agent stamps, withheld roots, failed sessions, withheld claims, unsupported claims and context
reuse.

Evidence: the fixture parts' data is ``isolated-fixture``, the live part's ``live-captures``
(the product side; the oracle reads live); ``agent_evidence`` says whether a real
Claude Code session answered (``real-claude-session``) or an injected transport
(``injected-transport``, tests; for the Ask a scripted planner). Live-unverified until the
live-check runner records ``verify/live-checks/eval.real_agent.json`` (the Ask:
``eval.ask_agent.json``); no Ask answer from a model is recorded here.

Usage::

    python3 -m scripts.eval_real_agent run --work-dir DIR [--total-budget-seconds 4800]
        [--session-timeout 300] [--max-turns 25] [--min-scored 8] [--model M]
        [--live [--reader-env PLX_EVAL_READER] [--host-config FILE] [--live-count 24]
         [--min-live-items 20] [--root-probe-seconds 300] [--deterministic-seconds 600]
         [--mailbox M --folder F]
         [--sharepoint-path P ...]]
    python3 -m scripts.eval_real_agent ask --work-dir DIR [--total-budget-seconds 4700]
        [--session-seconds 300] [--planner-timeout 120] [--max-steps 8] [--min-scored 8]
        [--max-tokens 80000] [--max-billed-tokens 400000]
        [--model M] [the live options above]
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence

from evals import runner
from evals import score as scoring
from operating import eval_principal

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "plx-eval-real-agent/v1"
EXPOSED_DIR = ROOT / "evals" / "exposed"
DEV_DIR = ROOT / "evals" / "dev"
BASELINE_JSON = ROOT / "evals" / "baseline" / "dev-fixture.json"
# Ten frozen dev items across F1-F4, chosen once (pinned, never resampled) so runs compare.
FIXTURE_SAMPLE = ("dev-F1-0001-fixture", "dev-F1-0005-fixture", "dev-F1-0009-fixture",
                  "dev-F2-0001-fixture", "dev-F2-0004-fixture", "dev-F3-0001-fixture",
                  "dev-F3-0004-fixture", "dev-F3-0006-fixture", "dev-F4-0001-fixture",
                  "dev-F4-0004-fixture")
PARTS = ("exposed", "fixture_sample")
LIVE_PART = "live_bc"
READER_ENV = "PLX_EVAL_READER"  # the named reader's identity, JSON claims
STATE_ENV = "PLX_WORKBENCH_STATE_DIR"  # the persistent host's state directory
HOST_CONFIG_ENV = "PLX_WORKBENCH_HOST_CONFIG"  # its plx-host-config/v1 file
TICKET_ENV = "PLX_EVAL_SESSION_TICKET"  # the variable a live session's ticket travels in
# The live-check runner's list of the host settings it provisioned for this run (names,
# comma-separated, never a value: verify/live_verify.py AGENT_PROVISIONED_ENV).
PROVISIONED_ENV = "PLX_WORKBENCH_HOST_PROVISIONED"
HOST_SETTINGS = ("PLX_WORKBENCH_SESSION_SECRET", STATE_ENV, HOST_CONFIG_ENV)
READER_CLAIMS = ("uid", "email", "entraTenantId", "entraObjectId")
GRAPH_ENV = ("MICROSOFT_GRAPH_TENANT_ID", "MICROSOFT_GRAPH_CLIENT_ID",
             "MICROSOFT_GRAPH_CLIENT_SECRET")
DEFAULT_LIVE_COUNT = 24
MIN_LIVE_ITEMS = 20
LIVE_READ_SECONDS = 1200.0  # the oracle's own source reads (sampling and answers)
HOST_READ_SECONDS = 120.0  # one harness read through the live host
ROOT_PROBE_SECONDS = 300.0  # the reader's plx.find over every sampled root
READER_CHECK_SECONDS = 60.0  # the host's start of the reader's first session
DETERMINISTIC_SECONDS = 600.0  # the deterministic path over the resolved live items
AGENT_TIER_KEYS = ("runs", "correct", "wrong", "blocked", "read_gap", "other_blocked",
                   "justified_abstention", "coverage", "accuracy")
ACCESS_SCOPE = "single_reader"  # one operator-named reader per run (module docstring)
READ_GAP = scoring.READ_GAP
ASK_KIND = "agent_loop"  # an ask session's ``agent.kind`` in its transcript
SESSION_TIME = "session_time_exhausted"
# The loop's own stop reasons (``operating.agent_loop.run``); any other abstention or
# escalation is the planner's (its free text stays in the session record, never the report).
LOOP_STOPS = ("step_budget_exhausted", "tool_budget_exhausted", "token_budget_exhausted",
              "planner_reply_invalid", "no_cited_claims", "no_checked_claims", SESSION_TIME)
STOP_CLASSES = ("answered", *LOOP_STOPS, "planner_process_failed", "planner_abstained",
                "planner_escalated")
SNAKE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PLANNER_ACTIONS = ("call", "answer", "abstain", "escalate")  # operating.agent_loop
# The host's refusal of the session identity (``operating.mcp_server`` start-up) and its
# reason code.
IDENTITY_REFUSED = re.compile(rb"session identity refused: ([A-Za-z0-9_.:-]+)")
# ``case_console.checked_store_status``'s refusal and the store state it names.
STORE_REFUSED = re.compile(r"ontology store refused \((foreign|unknown)\)")
# Resolver reasons that are about the reader as an employee (not in the Portal snapshot,
# inactive, unverified, not an employee role, refused by Entra): the operator's reader,
# a blocked check. Any other identity refusal (an invalid, expired or replayed ticket, a
# tenant or state mismatch, a provider failure) is a harness or host fault, an error.
READER_REFUSALS = frozenset({
    "portal_access_unavailable", "portal_identity_mismatch", "portal_role_unknown",
    "portal_role_not_employee", "portal_user_inactive", "portal_identity_unverified",
    "portal_access_level_missing", "session_ticket_identity_mismatch"})


class SessionBudget:
    """At most ``sessions`` agent sessions before ``deadline``; any further run is refused
    as a failed session (scored ``blocked``) and counted in ``skipped``. An agent's
    ``admit(host)`` (``AskAgent``) may answer a run without a session (a withheld root):
    it uses no session."""

    def __init__(self, agent: Any, *, sessions: int, deadline: float) -> None:
        self.agent, self.sessions, self.deadline = agent, sessions, deadline
        self.used = self.skipped = 0

    def _skip(self) -> runner.AgentRun:
        self.skipped += 1
        return runner.AgentRun([], "", {"is_error": True, "subtype": "budget_exhausted"},
                               {"kind": "budget"})

    def run(self, prompt: str, host: Any) -> runner.AgentRun:
        if time.monotonic() > self.deadline:
            return self._skip()
        admit = getattr(self.agent, "admit", None)
        refused = admit(host) if admit is not None else None
        if refused is not None:
            return refused
        if self.used >= self.sessions:
            return self._skip()
        self.used += 1
        return self.agent.run(prompt, host)


def sample_items() -> dict[str, list[dict[str, Any]]]:
    exposed = runner.load_items(EXPOSED_DIR, split="dev")
    dev = {item["id"]: item for item in runner.load_items(DEV_DIR, split="dev")}
    return {"exposed": exposed, "fixture_sample": [dev[name] for name in FIXTURE_SAMPLE]}


WRONG_CITED_CODES = frozenset({"value_mismatch"})


def wrong_cited(score: Mapping[str, Any]) -> bool:
    """A stated answer (never an abstention) scored wrong on a value it states, with
    citations every claim check passed: the citations are real and the value is not the
    oracle's. Disjoint from ``evals.score.confident_unsupported``."""
    checks = score.get("claim_checks") or {}
    return (scoring.outcome_of(score) == "wrong" and not score.get("abstained")
            and checks.get("passed") is True and not scoring.confident_unsupported(score)
            and any(set(v.get("codes") or ()) & WRONG_CITED_CODES
                    and v.get("answer_index") is not None
                    for v in score.get("values") or ()))


def part_summary(scores: Sequence[Mapping[str, Any]],
                 transcripts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    out = scoring.outcome_summary(scores)
    out["wrong_cited"] = sum(wrong_cited(s) for s in scores)
    out["originals_not_held"] = sum("originals_not_held" in s["codes"] for s in scores)
    out["action_calls"] = sum(s.get("action_calls") or 0 for s in scores)
    out["refused_tool_calls"] = sum((t.get("usage") or {}).get("refused_tool_calls") or 0
                                    for t in transcripts)
    out["gate_refused"] = sum((t.get("agent") or {}).get("gate_refused") or 0
                              for t in transcripts)
    unreadable: dict[str, int] = {}
    for transcript in transcripts:
        for handle, read in (transcript.get("evidence") or {}).items():
            if not (isinstance(read, Mapping) and read.get("ok") is True):
                scheme = handle.partition("://")[0] if "://" in handle else "other"
                unreadable[scheme] = unreadable.get(scheme, 0) + 1
    out["unreadable_citations"] = dict(sorted(unreadable.items()))
    # What the Workbench's final-answer gate returned in the agents' sessions
    # (runner.answer_gate_record; the answer itself is scored as written): counts only, so
    # a fall in confident_unsupported can be credited to it, or its absence seen.
    gates = [(t.get("agent") or {}).get("answer_gate") for t in transcripts]
    gates = [g for g in gates if isinstance(g, Mapping)]
    out["answer_gate"] = {
        "runs": len(gates),
        "submitted": sum(int(g.get("submissions") or 0) > 0 for g in gates),
        "submissions": sum(int(g.get("submissions") or 0) for g in gates),
        "repairs": sum((g.get("outcomes") or []).count("repair") for g in gates),
        "repaired": sum(bool(g.get("repaired")) for g in gates),
        "refused_claims": sum(sum(g.get("refused_claims") or []) for g in gates),
        "final": {outcome: sum(g.get("final") == outcome for g in gates)
                  for outcome in ("answered", "repair", "abstained", "refused",
                                  "unreadable")}}
    out.update(scoring.blocked_share(out["read_gap"], out["runs"]))
    out["by_family"] = {family: {"runs": len(group), "correct": sum(
        scoring.outcome_of(s) == "correct" for s in group)} for family, group in sorted(
        _group(scores, "family").items())}
    return out


def _unsupported_rate(summary: dict[str, Any]) -> None:
    """The B1 rates of a part (``evals.score.answer_rates``), the Ask's stated answers its
    claim gate refused whole included: such a run is shown unsupported, never as the
    answer, and scored an abstention, yet the agent stated it. It counts as answered and
    confident unsupported both (numerator and denominator agree, so the rate stays within
    1 and errs towards failure: accuracy and the unsupported rate both worsen)."""
    refused = (summary.get("ask") or {}).get("confident_unsupported", 0)
    summary["gate_refused_answers"] = refused
    summary["answered"] += refused
    summary["confident_unsupported"] += refused
    summary.update(scoring.answer_rates(
        runs=summary["runs"], answered=summary["answered"], correct=summary["correct"],
        unsupported=summary["confident_unsupported"]))


def _group(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, list[Mapping[str, Any]]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row[key]), []).append(row)
    return groups


def baseline_beside(ids: Sequence[str], path: Path = BASELINE_JSON) -> dict[str, Any]:
    """The deterministic baseline's recorded outcomes on the sample and its whole-set rate."""
    recorded = json.loads(path.read_text(encoding="utf-8"))
    by_id = {row.get("item_id"): row.get("outcome") for row in recorded.get("items") or []}
    outcomes = [by_id.get(name, "not_run") for name in ids]
    block = recorded.get("eval") or {}
    return {"agent": block.get("agent"), "run_id": recorded.get("run_id"),
            "dev_set": block.get("dev_set"), "needs_agent": block.get("needs_agent"),
            "sample": {name: outcomes.count(name) for name in sorted(set(outcomes))},
            "evidence_label": recorded.get("evidence_label")}


def evaluate(parts: Mapping[str, tuple[Sequence[Mapping[str, Any]],
                                       Callable[[Mapping[str, Any]], Any],
                                       Callable[[Mapping[str, Any]], Any] | None]],
             agent: Any, *, out: Path, budget_seconds: float, max_sessions: int | None,
             agent_evidence: str, agent_name: str = "claude-code") -> dict[str, Any]:
    """Run each part's items once (k=1) through ``agent``; ``parts[name]`` is (items,
    host_for, pin_for[, evidence label]). Returns the report (no item value, id or
    citation); a part answered by the Ask (``AskAgent``) also reports ``ask``."""
    total = sum(len(part[0]) for part in parts.values())
    budget = SessionBudget(agent, sessions=total if max_sessions is None
                           else min(max_sessions, total),
                           deadline=time.monotonic() + budget_seconds)
    block: dict[str, Any] = {"agent": agent_name, "k": 1, "items": total}
    everything: list[Mapping[str, Any]] = []
    transcripts_all: list[Mapping[str, Any]] = []
    for name, (items, host_for, pin_for, *label) in parts.items():
        scores = runner.run_items(items, budget, host_for, out=out / name, runs=1,
                                  evidence_label=label[0] if label else "isolated-fixture",
                                  pin_for=pin_for)
        transcripts = [json.loads(p.read_text(encoding="utf-8"))
                       for p in sorted((out / name).rglob("*.transcript.json"))]
        block[name] = part_summary(scores, transcripts)
        if isinstance(agent, AskAgent):
            block[name]["ask"] = ask_summary(transcripts)
        _unsupported_rate(block[name])
        everything += scores
        transcripts_all += transcripts
    block["total"] = part_summary(everything, transcripts_all)
    if isinstance(agent, AskAgent):
        block["total"]["ask"] = ask_summary(transcripts_all)
    _unsupported_rate(block["total"])
    models = sorted({str((t.get("agent") or {}).get("model")) for t in transcripts_all
                     if (t.get("agent") or {}).get("model")})
    answered = sum(1 for t in transcripts_all if t.get("answer") is not None)
    block.update(model=models[0] if len(models) == 1 else models,
                 sessions=budget.used, sessions_max=budget.sessions, skipped=budget.skipped,
                 sessions_answered=answered,
                 cost_usd_estimate=round(sum(float((t.get("usage") or {}).get(
                     "cost_usd_estimate") or 0) for t in transcripts_all), 4),
                 baseline=baseline_beside(FIXTURE_SAMPLE),
                 incomplete=["run_budget_exhausted"] if budget.skipped else [])
    block["complete"] = not block["incomplete"]
    report: dict[str, Any] = {"schema": SCHEMA, "adapter_status": "live-unverified",
                              "evidence_label": "isolated-fixture",
                              "agent_evidence": agent_evidence, "values_published": False,
                              "eval": block}
    if budget.used and not answered:
        report["blocked_upstream"] = [{"part": "agent_session", "source": "claude-code",
                                       "error_code": "every_session_failed",
                                       "cause": "host_misconfiguration"}]
    return report


def verdict(report: Mapping[str, Any], *, min_scored: int,
            min_live_items: int | None = None) -> int:
    """0 when the run completed with at least ``min_scored`` scored (non-blocked) runs and,
    when the live oracle sampled, at least ``min_live_items`` live items; or when the agent
    session itself is blocked (no binary, no session answered). Rates are printed, not
    asserted here (the live-check spec asserts the answer-rate bar on them), but read-gap runs above the tripwire, in the whole run or in any one part (its
    ``blocked_tripwire``), fail the run (``evals.score.fail_reasons``, the dev baseline's
    rule: a part's capture lag is never diluted by another part). Blocked parts
    (``blocked_upstream``, e.g. too few roots the reader resolves) did not run and are not
    judged; the parts that ran still are. Else 3."""
    if any(part.get("part") == "agent_session" for part in report.get("blocked_upstream") or []):
        return 0
    block = report["eval"]
    total = block["total"]
    scored = total["runs"] - total["blocked"]
    sampled = (block.get("live") or {}).get("sampled")
    reasons = ([] if block["complete"] else ["incomplete"]) + \
        ([] if scored >= min_scored else ["below_min_scored"]) + \
        (["below_min_live_items"] if sampled is not None and min_live_items is not None
         and sampled < min_live_items else []) + \
        sorted({reason for name in (*PARTS, LIVE_PART, "total") if name in block
                for reason in scoring.fail_reasons(block[name]["read_gap"],
                                                   block[name]["runs"])})
    block["scored"], block["fail_reasons"] = scored, reasons
    return 3 if reasons else 0


def fixture_parts(work: Path) -> dict[str, tuple[Any, Any, Any]]:
    """The two parts' items, hosts and pins: exposed items on an empty fixture mirror; the
    sample on the fixture world's mirror and projection (``scripts.eval_dev_baseline``),
    the projection checked once against its mirror (``case_console.checked_store_status``;
    a refused one is a ``ValueError``)."""
    from integrations.source_mirror import AUDIENCE_SCHEMA, SourceMirror
    from ontology.schema import load_schema
    from operating.case_runtime import CASE_RUNTIME_DIR, MIRROR_FILE
    from operator_ui import case_console
    from scripts import eval_dev_baseline as edb
    from scripts import eval_oracle_dev as dev

    items = sample_items()
    # As the dev baseline's fixture run builds its product (``edb.run_fixture``): the
    # relations the items' roots reach, projected, plus those the labels cite, mirrored for
    # the run pins; and the fixture world's own BC capture (the injected-transport GET-only
    # client over evals.fixture_bc), kept in the product's state where the stdio host
    # reads host captures, gated by a fixture capture audience granting the items' readers.
    projected = edb.reachable_relations(
        load_schema(), {i["root"]["relation"] for i in items["fixture_sample"]},
        edb.DEFAULT_LINK_DEPTH)
    relations = sorted(set(projected) | set(edb.cited_relations(items["fixture_sample"])))
    bc_capture, bc_environment = edb.fixture_bc_capture(dev.eval_world(),
                                                        host_tenant=edb.TENANT)
    from operating.host import BC_DIR, BC_FILE
    product = edb.build_product(edb.fixture_client(), work / "fixture", relations,
                                budget_seconds=None, clock=edb.fixture_clock,
                                project=projected, bc_capture=bc_capture,
                                bc_environment=bc_environment,
                                bc_db=work / "fixture" / "state" / BC_DIR / BC_FILE)
    capture_audience = work / "fixture-capture-audience.json"
    # The eval product's own principal alone, as the baseline's BC evidence and the
    # product's Portal audience grant it: an item read as another principal sees no BC.
    capture_audience.write_text(json.dumps({
        "schema": "plx-live-source-audience/v1", "tenant": edb.TENANT,
        "audiences": {f"bc:{bc_environment.lower()}:*": [edb.PRINCIPAL]},
        "default_classification": "internal"}), encoding="utf-8")
    # once, at start-up: a refused store is an error before any session (ValueError)
    case_console.checked_store_status(product.store, product.state_dir)
    sources, _, _, _ = dev.build_fixture(dev.eval_world())
    sources.portal.prefetch()
    oracle_read = edb.oracle_field(sources.portal)
    pins = runner.MirrorPinReader(product.mirror_path)
    empty = work / "exposed-state"
    (empty / CASE_RUNTIME_DIR).mkdir(parents=True, exist_ok=True)
    audience = work / "exposed-audience.json"
    audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": edb.TENANT,
                                    "audiences": {}}), encoding="utf-8")
    SourceMirror(empty / CASE_RUNTIME_DIR / MIRROR_FILE, None, audience,
                 tenant=edb.TENANT).close()

    # The harness re-reads a cited BC record from the capture store the product projected,
    # as the item's principals (``scripts.eval_dev_baseline``'s evidence): the bare case
    # host resolves a bc:// citation with metadata only, so every value an answer drew from
    # BC scored calculation_input_unresolved however it was cited.
    from evals.capture_evidence import CaptureEvidenceHost, bc_resolver, reader
    resolve_as = bc_resolver(work / "fixture" / "state" / BC_DIR / BC_FILE, bc_environment,
                             granted=edb.PRINCIPAL)

    def principals(item: Mapping[str, Any]) -> list[str]:
        return (item.get("actor") or {}).get("principals") or [edb.PRINCIPAL]

    def exposed_host(item: Mapping[str, Any]) -> runner.CaseHost:
        return runner.CaseHost(empty, audience, edb.TENANT, principals(item), fixture=True)

    def sample_host(item: Mapping[str, Any]) -> Any:
        host = runner.CaseHost(product.state_dir, product.audience, edb.TENANT,
                               principals(item), fixture=True, ontology_store=product.store,
                               capture_audience=capture_audience)
        host.world_clock = edb.fixture_clock  # the projection is recorded at FIXTURE_NOW
        return CaptureEvidenceHost(host, reader(bc=resolve_as(principals(item))))
    return {"exposed": (items["exposed"], exposed_host, None),
            "fixture_sample": (items["fixture_sample"], sample_host,
                               lambda item: runner.make_run_pin(item, pins,
                                                                oracle_field=oracle_read))}


# --------------------------------------------------------------------------- live part

def _blocked(part: str, source: str, code: str, cause: str = "host_misconfiguration",
             **extra: Any) -> dict[str, Any]:
    return {"part": part, "source": source, "error_code": code, "cause": cause, **extra}


def reader_identity(environ: Mapping[str, str], name: str
                    ) -> tuple[dict[str, str] | None, str | None]:
    """(the named reader's identity claims, None) or (None, why not): ``name`` unset or
    empty is ``no_employee_identity`` (no reader named), anything but a JSON object
    with every claim of ``READER_CLAIMS`` as a non-empty string ``reader_identity_invalid``.
    The value is never printed."""
    raw = environ.get(name) or ""
    if not raw.strip():
        return None, "no_employee_identity"
    try:
        claims = json.loads(raw)
    except ValueError:
        return None, "reader_identity_invalid"
    if not isinstance(claims, dict) or not all(
            isinstance(claims.get(key), str) and claims[key].strip() for key in READER_CLAIMS):
        return None, "reader_identity_invalid"
    return {key: claims[key] for key in READER_CLAIMS}, None


def reader_or_eval(environ: Mapping[str, str], name: str, *,
                   eval_principal_fallback: bool) -> tuple[dict[str, str] | None, str | None]:
    """Who a live part reads as: (claims, None) for the named reader, (None, None) for the
    eval principal (no reader named and ``eval_principal_fallback``), else (None, why
    not). A reader that is named but invalid is never replaced by the eval principal."""
    identity, problem = reader_identity(environ, name)
    if problem == "no_employee_identity" and eval_principal_fallback:
        return None, None
    return identity, problem


def host_provisioned(environ: Mapping[str, str]) -> dict[str, Any]:
    """Which host settings the live-check runner provisioned for this run (``PROVISIONED_ENV``):
    ``{"host_provisioned": [names]}``, names only, never a value; ``[]`` when it provisioned
    none (or the run is not the verifier's). Fail closed: a list naming anything but one of
    ``HOST_SETTINGS``, or a setting this run does not hold, claims nothing
    (``host_provisioned_refused`` ``provisioned_list_invalid``)."""
    raw = environ.get(PROVISIONED_ENV)
    if raw is None or not raw.strip():
        return {"host_provisioned": []}
    names = [name.strip() for name in raw.split(",")]
    if not all(name in HOST_SETTINGS and (environ.get(name) or "").strip()
               for name in names) or len(set(names)) != len(names):
        return {"host_provisioned": [], "host_provisioned_refused": "provisioned_list_invalid"}
    return {"host_provisioned": sorted(names)}


def bc_rooted(item: Mapping[str, Any]) -> bool:
    """An item whose root record is a Business Central document."""
    return str((item.get("root") or {}).get("relation") or "").startswith("bc.")


class LiveHost:
    """The persistent host as one named reader, for one run, on the Host contract of
    ``evals.runner.CaseHost``: the agent's session is the host's own MCP server
    (``mcp_server``: ``operating.host mcp``), the harness's (``tools``, ``call`` as a client
    sees it, ``read`` as host code: the catalog, the root probe and the evidence re-reads
    ``evals.runner`` makes as the item's actor) is ``evals.harness_reads`` over the same
    state and config (``harness_server``). Each starts with a fresh single-use agent ticket
    for that employee (``operating.session_identity.mint_session_ticket``), so the host
    resolves the employee and trims every read for them, as for an employee's own Claude.
    No ``--fixture``, no ``--principal``. ``eval_principal``: ``identity`` is the declared
    eval principal's (``operating.eval_principal.ticket_claims``) and both servers start
    with ``--eval-principal``, so the host resolves it from its declaration alone."""

    def __init__(self, state_dir: Path, config: Path, identity: Mapping[str, str],
                 secret: str, *, tenant: str, extra_env: Mapping[str, str] | None = None,
                 read_timeout: float = HOST_READ_SECONDS, eval_principal: bool = False
                 ) -> None:
        self.state_dir, self.config = Path(state_dir), Path(config)
        self._identity, self._secret = dict(identity), secret
        self._extra_env = dict(extra_env or {})
        self.eval_principal = eval_principal
        self.actor = {"tenant": tenant,
                      "reader": "eval_principal" if eval_principal else "named_employee"}
        self.read_timeout = read_timeout
        self._proc: Any = None
        self._stderr: Any = None
        self._buffer = bytearray()
        self._ids = 0

    def ticket(self) -> str:
        from operating import session_identity as si
        return si.mint_session_ticket(
            uid=self._identity["uid"], email=self._identity["email"],
            tenant_id=self._identity["entraTenantId"],
            object_id=self._identity["entraObjectId"], author=si.AUTHOR_AGENT,
            secret=self._secret)

    def _command(self, module: str, *command: str) -> dict[str, Any]:
        from operating import session_identity as si
        return {"command": sys.executable,
                "args": ["-m", module, *command, "--state-dir", str(self.state_dir),
                         "--config", str(self.config), "--session-ticket-env", TICKET_ENV,
                         *(["--eval-principal"] if self.eval_principal else [])],
                "cwd": str(ROOT),
                "env": {"PYTHONPATH": str(ROOT), TICKET_ENV: self.ticket(),
                        si.SESSION_TICKET_SECRET_ENV: self._secret, **self._extra_env}}

    def mcp_server(self) -> dict[str, Any]:
        return self._command("operating.host", "mcp")

    def harness_server(self) -> dict[str, Any]:
        return self._command("evals.harness_reads")

    def agent_session(self) -> tuple[Any, Any]:
        """The Ask's session for the reader in process: ``(session host, MCPServer)``
        started as ``operating.host mcp`` starts it (its arguments and directory provider
        from the host config, a fresh single-use agent ticket, the host's identity
        start-up and replay guard)."""
        from operating import host as plx_host
        from operating import mcp_server
        from operating import session_identity as si
        try:
            host = plx_host.Host(self.state_dir, plx_host.HostConfig.from_file(self.config),
                                 environ={**os.environ, **self._extra_env})
            argv = plx_host.mcp_arguments(host, session_ticket_env=TICKET_ENV,
                                          eval_principal=self.eval_principal)
            directory = None if self.eval_principal else plx_host.directory_provider(host)
        except plx_host.HostError as error:
            raise runner.RunnerError(f"live_host_config_refused:{error.code}") from None
        return _in_process(lambda: mcp_server.session_server(argv, directory=directory),
                           {TICKET_ENV: self.ticket(), si.SESSION_TICKET_SECRET_ENV: self._secret})

    def _line(self) -> dict[str, Any]:
        import selectors
        fd = self._proc.stdout.fileno()
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while b"\n" not in self._buffer:
                if not selector.select(self.read_timeout):
                    raise runner.RunnerError("live_host_read_timeout")
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    raise runner.RunnerError(self._ended())
                self._buffer += chunk
        line, _, rest = bytes(self._buffer).partition(b"\n")
        self._buffer = bytearray(rest)
        return json.loads(line)

    def _ended(self) -> str:
        """Why the host closed the session: ``live_host_identity_refused:<reason>`` when it
        refused the session identity at start-up (the host's reason code), else
        ``live_host_session_ended``. Its stderr is read for that code only, never
        reported."""
        import subprocess
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            return "live_host_session_ended"
        self._stderr.seek(0)
        refused = IDENTITY_REFUSED.search(self._stderr.read())
        return (f"live_host_identity_refused:{refused.group(1).decode()}" if refused
                else "live_host_session_ended")

    def _request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        import subprocess
        if self._proc is None:
            server = self.harness_server()
            self._stderr = tempfile.TemporaryFile()
            self._proc = subprocess.Popen(
                [server["command"], *server["args"]], cwd=server["cwd"],
                env={**runner.agent_environment(os.environ), **server["env"]},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr)
            self._send({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "plx-eval-harness", "version": "1"}}})
            self._reply(0)
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._ids += 1
        self._send({"jsonrpc": "2.0", "id": self._ids, "method": method, "params": dict(params)})
        return self._reply(self._ids)

    def _send(self, message: Mapping[str, Any]) -> None:
        try:
            self._proc.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
            self._proc.stdin.flush()
        except BrokenPipeError:  # the host already exited (e.g. it refused the reader)
            raise runner.RunnerError(self._ended()) from None

    def _reply(self, request_id: int) -> dict[str, Any]:
        while True:  # notifications and server requests come first; never answered here
            message = self._line()
            if message.get("id") == request_id and "method" not in message:
                return message

    def tools(self) -> list[dict[str, Any]]:
        listed = (self._request("tools/list", {}).get("result") or {}).get("tools") or []
        return [{"name": tool["name"], "read_only": bool(
            (tool.get("annotations") or {}).get("readOnlyHint"))} for tool in listed]

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        response = self._request("tools/call", {"name": name, "arguments": arguments})
        result = response.get("result") or {}
        if "error" in response or result.get("isError"):
            return {"ok": False, "error": "tool_refused"}
        return result.get("structuredContent")

    def read(self, name: str, arguments: dict[str, Any]) -> Any:
        """One harness read as host code (``evals.harness_reads``), as ``CaseHost.read``."""
        from evals.harness_reads import HARNESS_READ
        response = self._request(HARNESS_READ, {"name": name, "arguments": arguments})
        return response["result"] if "result" in response else {
            "ok": False, "error": "tool_refused"}

    def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.stdin.close()
            except BrokenPipeError:
                pass
            try:
                self._proc.wait(timeout=10)
            except Exception:  # noqa: BLE001 - a host that does not stop is killed
                self._proc.kill()
                self._proc.wait()
            self._proc.stdout.close()
            self._stderr.close()
            self._proc = None


def root_object(item: Mapping[str, Any], found: Any) -> str | None:
    """The object id of the candidate of a ``plx.find`` result that is ``item``'s root
    record, else None: the record its display, or else the identifier it matched, cites
    (version-free). Both cross the output boundary as {untrusted_text, origin} envelopes;
    only their origin citation (host-set, never source text) is read. An object without
    a display (a BC document whose type declares none) is named by its matched
    identifier's record."""
    want = scoring.chain_identity(item.get("root") or {})
    if want is None or not isinstance(found, Mapping):
        return None
    for candidate in found.get("candidates") or []:
        candidate = candidate if isinstance(candidate, Mapping) else {}
        found_object = candidate.get("object") or {}
        if not isinstance(found_object.get("object_id"), str):
            continue
        for envelope in (found_object.get("display"),
                         (candidate.get("matched") or {}).get("raw")):
            origin = envelope.get("origin") if isinstance(envelope, Mapping) else None
            identity = scoring.cite_identity(origin.get("citation")
                                             if isinstance(origin, Mapping) else None)
            if identity is not None and scoring._chain_form(identity) == want:
                return found_object["object_id"]
    return None


def root_resolved(item: Mapping[str, Any], found: Any) -> bool:
    """Whether a ``plx.find`` result names ``item``'s root record among its candidates."""
    return root_object(item, found) is not None


def resolve_roots(items: Sequence[Mapping[str, Any]], host: Any, *,
                  seconds: float = ROOT_PROBE_SECONDS
                  ) -> tuple[list[bool | None], str | None]:
    """Per item, whether the reader's ``plx.find`` on the root's identifier names the root
    (one harness session on ``host``, closed afterwards), within ``seconds``: each read
    waits at most the time left, and the items the deadline or a host that stopped
    answering left unasked are None (not probed), never resolved or unresolved. With the
    per-item results, why the probe stopped early (``live_host_read_timeout`` at the
    deadline or a stalled read, else the host's ``RunnerError`` code), None if it did not."""
    deadline = time.monotonic() + seconds
    found: list[bool | None] = []
    stopped = None
    try:
        for item in items:
            left = deadline - time.monotonic()
            if left <= 0:
                stopped = "live_host_read_timeout"
                break
            if hasattr(host, "read_timeout"):
                host.read_timeout = min(host.read_timeout, left)
            try:
                answer = host.call("plx.find", {
                    "query": str((item.get("root") or {}).get("identifier") or "")[:200],
                    "limit": 10})
            except runner.RunnerError as error:
                stopped = str(error)
                break
            found.append(root_resolved(item, answer))
    finally:
        host.close()
    return found + [None] * (len(items) - len(found)), stopped


def reader_refusal(host: Any, *, seconds: float = READER_CHECK_SECONDS) -> str | None:
    """None when the host serves the reader a session (one harness session listing the
    tools within ``seconds``, closed afterwards), else why not: ``reader_refused`` (the
    host's resolver refused the reader as an employee, ``READER_REFUSALS``),
    ``live_host_session_ended`` or ``live_host_read_timeout``. Any other identity refusal
    (the ticket, the tenant, the state) is the harness's or host's fault: raised
    (``RunnerError``), never a blocked check."""
    if hasattr(host, "read_timeout"):
        host.read_timeout = min(host.read_timeout, seconds)
    try:
        host.tools()
    except runner.RunnerError as error:
        code = str(error)
        _, refused, reason = code.partition("live_host_identity_refused:")
        if not refused:
            return code
        if reason in READER_REFUSALS or (reason.startswith("entra_directory:")
                                         and reason != "entra_directory:provider_failed"):
            return "reader_refused"
        raise
    finally:
        host.close()
    return None


def host_ledger(state: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """(capture, projection) as the persistent host's own ledger states them
    (``operating.host`` ``status``): per source its generation, completeness, how it is
    served and whether it is stale; the projection's generation and object count. States
    and counts only; a state directory without a host ledger says so."""
    import sqlite3
    from operating import host as plx_host
    if not (state / plx_host.HOST_DIR / plx_host.LEDGER_FILE).is_file():
        return {"state": "no_host_ledger"}, {"state": "no_host_ledger"}
    try:
        status = plx_host.Host(state).status()
    except (OSError, ValueError, sqlite3.Error):
        return {"state": "ledger_unreadable"}, {"state": "ledger_unreadable"}
    capture = {entry["source"]: {"configured": entry["configured"],
                                 "generation": entry["generation"],
                                 "complete": entry.get("complete"),
                                 "served_from": entry["served_from"],
                                 "stale": bool(entry.get("stale_since"))}
               for entry in status["sources"]}
    capture |= {name: {"configured": False, "generation": 0, "complete": None,
                       "served_from": "none", "stale": False}
                for name in status.get("not_configured", ())}
    built = status.get("projection") or {}
    return capture, {"generation": built.get("generation"), "objects": built.get("objects")}


def deterministic_tier(items: Sequence[Mapping[str, Any]], make_host: Callable[[], Any], *,
                       seconds: float) -> dict[str, Any]:
    """The deterministic path (no model) on ``items``, each on a fresh host as the reader,
    within ``seconds``: outcome counts (``evals.baseline.OUTCOMES``), abstentions, the
    items the time left unanswered (``not_run``) and those whose run raised (``error``, by
    exception class): counted, never hidden, and never a stop of the agent's tier."""
    from evals import baseline as bl
    deadline = time.monotonic() + seconds
    rows, errors = [], {}
    for item in items:
        if time.monotonic() > deadline:
            break
        try:
            rows.append(bl.run_item(item, bl.DeterministicInvestigator(), make_host(),
                                    run_pin=None, evidence_label="live-captures"))
        except Exception as exc:  # noqa: BLE001 - one tier's failure, counted by class
            errors[type(exc).__name__] = errors.get(type(exc).__name__, 0) + 1
    return {"items": len(items), "not_run": len(items) - len(rows) - sum(errors.values()),
            **{name: sum(r["outcome"] == name for r in rows) for name in bl.OUTCOMES},
            "abstained": sum(bool(r["abstained"]) for r in rows),
            "error": dict(sorted(errors.items()))}


def tier_block(report: Mapping[str, Any], live: Mapping[str, Any] | None,
               blocked: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """``eval.tiers`` (module docstring): each tier on its own, from what the run measured."""
    block = report["eval"]
    live_tiers = dict((live or {}).get("tiers") or {})
    not_run = {"state": "not_run"}
    reader = (live or {}).get("reader", "not_run")
    return {
        "access": {"scope": ACCESS_SCOPE, "readers": (live or {}).get("readers", 0),
                   "reader": reader,
                   **({"a6_permission_proof": eval_principal.A6_PROOF}
                      if reader == eval_principal.LABEL else {}),
                   READ_GAP: block["total"]["read_gap"],
                   "sources": dict((live or {}).get("sources") or {}),
                   "blocked_strata": sum(1 for p in blocked
                                         if p.get("cause") == "upstream_outage")},
        "capture": live_tiers.get("capture", not_run),
        "projection": live_tiers.get("projection", not_run),
        "deterministic": {LIVE_PART: live_tiers.get("deterministic", not_run),
                          "fixture_sample": block["baseline"]["sample"]},
        "agent": {name: {key: block[name][key] for key in AGENT_TIER_KEYS}
                  for name in (LIVE_PART, *PARTS) if name in block}}


def store_refusal(state: Path) -> tuple[str, str] | None:
    """None when the persistent host's projection store (``operating.host.projection_path``)
    may serve the Ask: checked once against the host's mirror through
    ``case_console.checked_store_status``, as the console checks it at start-up. Else
    ``(code, store state)``: ``ask_needs_a_projection_store`` (``missing``), or
    ``ontology_store_refused`` (``foreign``: projected from another capture generation;
    ``unknown``: unverifiable, an unreadable store or capture fails closed). Enums only: the
    refusal's text (paths, details) stays out of the report."""
    import sqlite3
    from operating.host import projection_path
    from operator_ui import case_console
    store = projection_path(state)
    if not store.is_file():
        return "ask_needs_a_projection_store", "missing"
    try:
        case_console.checked_store_status(store, state)
    except (ValueError, OSError, sqlite3.Error) as error:
        refused = STORE_REFUSED.match(str(error)) if isinstance(error, ValueError) else None
        return "ontology_store_refused", refused.group(1) if refused else "unknown"
    return None


def live_part(environ: Mapping[str, str], args: argparse.Namespace, *,
              build_sources: Callable[..., Any] | None = None,
              make_host: Callable[[], Any] | None = None, ask: bool = False,
              eval_principal_fallback: bool = False
              ) -> tuple[tuple[Any, ...] | None, list[dict[str, Any]], dict[str, Any]]:
    """(the live part, its blocked parts, its report block). The reader identity and the
    host are checked before anything is read: without them the part is blocked and no
    source is read and no session started. ``build_sources(environ, namespace, guard=...)``
    (default ``scripts.eval_oracle_dev.build_live``; tests inject the fixture world): the
    oracle's own sources. ``make_host`` (tests): a stand-in for ``LiveHost`` as the reader.
    The host must accept the reader before any source is read; for the Ask (``ask``) its
    projection store must pass ``store_refusal`` too, once, here. The part holds only the
    items whose root the reader resolves (module docstring); the report block carries the
    live tiers (``tiers``). ``eval_principal_fallback``: no reader named reads as the eval
    principal (``reader_or_eval``); without it no reader is ``no_employee_identity``."""
    from evals.oracle.generator import generate
    from operating.session_identity import session_secret
    from scripts import eval_oracle_dev as dev

    identity, problem = reader_or_eval(environ, args.reader_env,
                                       eval_principal_fallback=eval_principal_fallback)
    if problem is not None:
        return None, [_blocked(LIVE_PART, "workbench", str(problem))], {"reader": problem}
    as_eval = identity is None
    who = {"reader": eval_principal.LABEL if as_eval else "named", "readers": 1,
           "principal": eval_principal.reader_block(
               eval_principal.LABEL if as_eval else "employee")}
    secret = session_secret(environ)
    state = Path(environ.get(STATE_ENV) or "")
    config = Path(args.host_config or environ.get(HOST_CONFIG_ENV) or "")
    missing = ("session_secret_missing" if not secret else
               "host_state_missing" if not environ.get(STATE_ENV) or not state.is_dir() else
               "host_config_missing" if not config.name or not config.is_file() else None)
    if missing:
        return None, [_blocked(LIVE_PART, "workbench", missing)], dict(who)
    if as_eval:
        try:
            identity = eval_principal.ticket_claims(config)
        except ValueError as error:
            return None, [_blocked(LIVE_PART, "workbench",
                                   f"live_host_config_refused:{error}")], dict(who)
    host_config = json.loads(config.read_text(encoding="utf-8"))
    extra = ({name: environ[name] for name in GRAPH_ENV if environ.get(name)}
             if host_config.get("entra_directory") else {})
    tenant = str(host_config.get("tenant") or "plx")
    if make_host is None:
        def make_host() -> LiveHost:
            return LiveHost(state, config, identity, secret, tenant=tenant, extra_env=extra,
                            eval_principal=as_eval)
    refused = reader_refusal(make_host())
    if refused:
        return None, [_blocked(LIVE_PART, "workbench", refused)], \
            {**who, **({"reader": "refused"} if refused == "reader_refused" else {})}
    refused_store = store_refusal(state) if ask else None
    if refused_store is not None:
        code, store_state = refused_store
        return None, [_blocked(LIVE_PART, "workbench", code, store_state=store_state)], \
            dict(who)
    deadline = dev.Deadline(args.live_read_seconds)
    namespace = argparse.Namespace(no_bc=False, mailbox=args.mailbox, folder=args.folder,
                                   sharepoint_path=args.sharepoint_path)
    sources, documents, configured = (build_sources or dev.build_live)(
        environ, namespace, guard=deadline.guard)
    if sources.bc is None:
        return None, [_blocked(LIVE_PART, "business-central", "bc_not_configured")], \
            {**who, "sources": dict(configured)}
    now = dev._utc_now()
    generated = generate(sources, run_id=f"live-{now[:10]}", now=now, count=args.live_count,
                         documents=documents, split="dev",
                         bc_sample_limit=max(50, 4 * args.live_count),
                         bc_want=args.live_count, evidence_note="live oracle read")
    items = [item for item in generated.items if bc_rooted(item)][:args.live_count]
    strata = dev.strata_report(generated.strata)
    blocked = [{**part, "part": f"live_{part['part']}", "cause": "upstream_outage"}
               for part in dev.blocked_upstream(strata)]
    resolved, stopped = resolve_roots(items, make_host(), seconds=args.root_probe_seconds) \
        if items else ([], None)
    held = [item for item, ok in zip(items, resolved) if ok]
    not_probed = sum(ok is None for ok in resolved)
    capture, projection = host_ledger(state)
    info = {**who, "sources": dict(configured),
            "sampled": len(items),
            "budget_hit": deadline.hit,
            "strata": {name: entry.get("status") for name, entry in sorted(strata.items())},
            "tiers": {"capture": capture,
                      "projection": {**projection, "roots_sampled": len(items),
                                     "roots_resolved": len(held),
                                     "roots_unresolved": sum(ok is False for ok in resolved),
                                     "roots_not_probed": not_probed},
                      "deterministic": deterministic_tier(
                          held, make_host, seconds=args.deterministic_seconds)}}
    if not_probed and stopped == "live_host_read_timeout":
        # The host did not answer every probe in time: the projection tier is unmeasured,
        # never a pass and never the agent's failure.
        blocked.append(_blocked(LIVE_PART, "workbench", "root_probe_incomplete",
                                "projection_timeout"))
    elif not_probed:
        # The host ended the session mid-probe: the host's fault, not the projection's.
        blocked.append(_blocked(LIVE_PART, "workbench", str(stopped)))
    elif len(held) < args.min_live_items:
        # The reader's projection names too few sampled roots: a capture or projection gap
        # (or the reader's grant), never the agent's failure and never a pass.
        blocked.append(_blocked(LIVE_PART, "workbench", "roots_unresolved",
                                "missing_bindings"))
    if not held:
        return None, blocked, info
    return (held, lambda item: make_host(), None, "live-captures"), blocked, info


# --------------------------------------------------------------------------- ask part

def _in_process(build: Callable[[], Any], env: Mapping[str, str]) -> Any:
    """``build()`` (an ``mcp_server.session_server`` start-up) with ``env`` set for it only
    (the session ticket and its secret; restored afterwards, so no later process inherits
    them). A start-up refusal (its parser exit) is a ``RunnerError``: the host's
    ``live_host_identity_refused:<reason>`` when it refused the identity, else
    ``ask_session_refused``; its message is read for that code only."""
    env = {key: value for key, value in env.items() if key != "PYTHONPATH"}
    saved = {key: os.environ.get(key) for key in env}
    errors = io.StringIO()
    os.environ.update(env)
    try:
        with contextlib.redirect_stderr(errors):
            return build()
    except SystemExit:
        refused = IDENTITY_REFUSED.search(errors.getvalue().encode("utf-8", "replace"))
        raise runner.RunnerError(f"live_host_identity_refused:{refused.group(1).decode()}"
                                 if refused else "ask_session_refused") from None
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def agent_session(host: Any) -> tuple[Any, Any]:
    """``(session host, MCPServer)``: the in-process session of ``host``'s agent server
    (``LiveHost.agent_session``; else the ``operating.mcp_server`` command line of
    ``host.mcp_server()``, the fixture case host's)."""
    own = getattr(host, "agent_session", None)
    if callable(own):
        return own()
    spec = host.mcp_server()
    if list(spec.get("args") or [])[:2] != ["-m", "operating.mcp_server"]:
        raise runner.RunnerError("ask_session_unsupported")
    from operating import mcp_server
    return _in_process(lambda: mcp_server.session_server(list(spec["args"][2:])),
                       spec.get("env") or {})


class _Recorder:
    """The session's server as the loop calls it, recording each tool call as a transcript
    event (the result as the loop received it), so the scorer reads what the loop read."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self.events: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.server, name)

    def handle(self, message: Any, **options: Any) -> Any:
        response = self.server.handle(message, **options)
        if isinstance(message, Mapping) and message.get("method") == "tools/call":
            params = message.get("params") or {}
            result = (response or {}).get("result") or {}
            refused = response is None or "error" in response or bool(result.get("isError"))
            self.events.append({
                "seq": len(self.events), "type": "tool_call", "name": params.get("name"),
                "arguments": dict(params.get("arguments") or {}),
                "result": runner._bounded({"ok": False, "error": "tool_refused"} if refused
                                          else result.get("structuredContent")),
                "is_error": refused})
        return response


class _RecordingAccess:
    """A reader's ``HostAccess`` whose Ask session records its tool calls (``_Recorder``,
    ``recorder``); everything else is the access's own."""

    def __init__(self, access: Any) -> None:
        self.access, self.recorder = access, None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.access, name)

    def agent_server(self) -> Any:
        self.recorder = _Recorder(self.access.agent_server())
        return self.recorder


def at_world_now(access: Any, clock: Callable[[], Any]) -> Any:
    """The fixture-now hook: the case host of ``access`` (``objects.host_access``) answers an
    omitted ``as_known_at`` at ``clock``'s now, for a host whose world has its own now
    (``world_clock``: the fixture world's, ``FIXTURE_NOW``). Its capture clock is
    ``host_access``'s own ``clock`` option. The eval's Ask sessions and the console workspace
    a test compares them with set it here only. Returns ``access``."""
    access.host.tools.clock = clock
    return access


def ask_workspace(host: Any, planner: Any, bounds: Any) -> tuple[Any, Any, _RecordingAccess]:
    """``(session host, workspace, access)``: the object workspace the console builds for
    the reader of ``host``'s own session start-up (``agent_session``: its case runtime and
    verified reader), through the console's own factory
    (``case_console.object_workspace_for``), with ``planner`` and ``bounds`` for its Ask and
    its access recording the Ask's tool calls (``_RecordingAccess``): over the session's
    projection store, captures, capture bounds and review store (read only: no session, no
    person who reviews), the store checked against its mirror as the console checks each
    workspace it builds. A store projected from another capture generation or unverifiable
    refuses the session (``ontology_store_refused:<state>``). A host whose world has its own
    now (``world_clock``: the fixture world's, ``FIXTURE_NOW``) is read at that now
    (``at_world_now``), as the deterministic baseline's tool table reads it; a live host at
    the real now. The session host is the caller's to close after the workspace."""
    from operator_ui import case_console
    session, server = agent_session(host)
    try:
        args, actor = session.args, server.actor
        server.close()  # the session's own server: the Ask's is the workspace's
        if args.ontology_store is None:
            raise runner.RunnerError("ask_needs_a_projection_store")
        captures = {name: None if getattr(args, name) is None
                    else Path(getattr(args, name)).resolve()
                    for name in ("mail_capture_dir", "sharepoint_capture_dir")}
        clock = getattr(host, "world_clock", None)
        try:
            workspace = case_console.object_workspace_for(
                session.case_runtime, args.ontology_store, actor,
                state_dir=args.state_dir.resolve(), planner=planner, ask_bounds=bounds,
                capture_bounds=session.capture_bounds, clock=clock,
                wrap_access=_RecordingAccess, **captures)
        except ValueError as error:
            refused = STORE_REFUSED.match(str(error))
            if refused is None:
                raise
            raise runner.RunnerError(f"ontology_store_refused:{refused.group(1)}") from None
        if clock is not None:
            at_world_now(workspace.access, clock)
    except BaseException:
        session.close()
        raise
    return session, workspace, workspace.access


class AskHost:
    """A part's host for one ask item: the part's own host (the harness's reads, the
    agent's server, ``close``), the item whose root the session is rooted at, the item's
    run pin (``runner.make_run_pin``; None: the part pins nothing) and that root's object
    id once ``AskAgent.admit`` found it."""

    def __init__(self, host: Any, item: Mapping[str, Any],
                 run_pin: Mapping[str, Any] | None = None) -> None:
        self.host, self.item, self.run_pin = host, item, run_pin
        self.object_id: str | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.host, name)


def ask_parts(parts: Mapping[str, tuple[Any, ...]]) -> dict[str, tuple[Any, ...]]:
    """Each part with its hosts wrapped for the ask (``AskHost``), each host given the run
    pin its item was pinned with (``evals.runner.run_items`` pins an item before its
    hosts are made), so the Ask names the time the prompt does."""
    def wrapped(host_for: Callable[[Mapping[str, Any]], Any],
                pin_for: Callable[[Mapping[str, Any]], Any] | None
                ) -> tuple[Callable[..., Any], Callable[..., Any] | None]:
        pins: dict[str, Any] = {}

        def pinned(item: Mapping[str, Any]) -> Any:
            pins[item["id"]] = pin = pin_for(item)
            return pin
        return (lambda item: AskHost(host_for(item), item, pins.get(item["id"])),
                pinned if pin_for is not None else None)
    return {name: (items, *wrapped(host_for, pin_for), *rest)
            for name, (items, host_for, pin_for, *rest) in parts.items()}


def _vocabulary() -> tuple[tuple[str, ...], tuple[str, ...]]:
    defs = runner._schema()["$defs"]
    return tuple(defs["quantity_role"]["enum"]), tuple(defs["gap_state"]["enum"])


def ask_family(item: Mapping[str, Any], object_id: str) -> str | None:
    """The investigation plan the Ask starts from for ``item``: the plan whose declared
    ``families`` hold the item's question family (as a reader picks the investigation
    for their kind of question), when it may run from the root's type; else None (a
    held-out family has no plan: the planner chooses alone)."""
    from operating.ontology_tools_investigate import load_plans
    from operator_ui.object_workspace import plans_for
    family = item.get("family")
    named = [plan_id for plan_id, plan in load_plans().items()
             if family in plan.doc["families"]]
    fits = {plan["family"] for plan in plans_for(object_id.split(":", 1)[0])}
    return named[0] if len(named) == 1 and named[0] in fits else None


def ask_question(item: Mapping[str, Any], run_pin: Mapping[str, Any] | None = None) -> str:
    """The Ask's question about the item's root: the item's question, its as-known-at time
    and how to state roles and unknowns; the item's question is cut so the whole fits the
    workspace's ``MAX_QUESTION`` (the workspace names the object in view). The time is the
    one ``runner.build_prompt`` states: with a run pin, the time by which the pinned
    captures had completed (decision 2026-10-02-oracle-pinning), as the item's own time
    precedes them and a read as known then finds none of their records."""
    from operator_ui.object_workspace import MAX_QUESTION
    roles, gaps = _vocabulary()
    when = (run_pin or {}).get("known_at") or item["as_known_at"]["time"]
    contract = (f"\n\nAnswer as known at {when}. State each value the "
                "question asks for, and only those, as a value claim; give a quantity its "
                f"role (one of {', '.join(roles)}). State each unknown as \"<state>: "
                f"<subject>\" with state one of {', '.join(gaps)}; a value the sources do not "
                "hold is an unknown whose subject is the value's snake_case name.")
    return str(item["question"]["text"]).strip()[:max(MAX_QUESTION - len(contract), 0)] \
        + contract


def loop_answer(result: Mapping[str, Any]) -> dict[str, Any]:
    """The loop's outcome as a ``plx-eval-answer/v1`` (module docstring, **Ask**)."""
    roles, gap_states = _vocabulary()
    values: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for claim in result.get("claims") or []:
        for claimed in claim.get("value_claims") or []:
            if claimed.get("op") in ("count", "sum"):
                values.append(_op_value(claimed, roles))
                continue
            key = (claimed.get("citation"), claimed.get("field"), claimed.get("value"))
            if claimed.get("citation") is None or key in seen:
                continue
            seen.add(key)
            role = claimed.get("role")
            tail = str(claimed["field"]).rsplit(".", 1)[-1].lower()
            value: dict[str, Any] = {
                "name": role or re.sub(r"[^a-z0-9_]", "_", tail)[:64] or "value",
                "value": claimed["value"],
                "citations": [{"l1_handle": claimed["citation"],
                               "field_path": claimed["field"]}]}
            if role in roles:
                value["quantity_role"] = role
            if claimed.get("unit"):
                value["unit"] = claimed["unit"]
            values.append(value)
    gaps = []
    for unknown in result.get("unknowns") or []:
        state, colon, subject = str(unknown).partition(":")
        state, subject = state.strip(), subject.strip()[:200]
        if not colon or state not in gap_states or not subject:
            continue
        gap = {"state": state, "subject": subject}
        gaps.append(gap)
        if SNAKE.match(subject) and all(v["name"] != subject for v in values):
            values.append({"name": subject, "value": None,
                           **({"quantity_role": subject} if subject in roles else {})})
        if SNAKE.match(subject) and any(v["name"] == subject and v["value"] is None
                                        for v in values):
            gap["concerns"] = subject  # the value it leaves open (the scorer's concerns)
    answered = result.get("outcome") == "answered"
    return {"format": scoring.ANSWER_FORMAT,
            "answerability": ("partial" if result.get("unknowns") else "answerable")
            if answered else "blocked",
            "text": result["text"] if isinstance(result.get("text"), str)
            else " ".join(claim["text"] for claim in result.get("claims") or []),
            "values": values[:scoring.MAX_VALUES], "gaps": gaps}


def _op_value(claimed: Mapping[str, Any], roles: Sequence[str]) -> dict[str, Any]:
    """A passed count or sum claim (``claim_checks`` ``op``) as the scorer's calculated
    value, which it recomputes from its own re-reads: named by the claim's role (a count's
    is its name), a sum's inputs in their unit or the claim's, and in the currency the
    gate checked against the values the tools returned."""
    role, unit, currency = claimed.get("role"), claimed.get("unit"), claimed.get("currency")
    inputs = [{"l1_handle": given["citation"],
               **({"field_path": given["field"]} if "field" in given else {}),
               **({"unit": given.get("unit") or unit}
                  if claimed["op"] == "sum" and (given.get("unit") or unit) else {}),
               **({"currency": currency} if currency else {})}
              for given in claimed["of"]]
    value: dict[str, Any] = {"name": role or claimed["op"], "value": claimed["value"],
                             "calculation": {"op": claimed["op"], "inputs": inputs}}
    if role in roles:
        value["quantity_role"] = role
    if unit and claimed["op"] == "sum":
        value["unit"] = unit
    if currency:
        value["currency"] = currency
    return value


def stop_class(result: Mapping[str, Any]) -> str:
    """The loop's stop as one of ``STOP_CLASSES``."""
    reason = str(result.get("reason") or "")
    if result.get("outcome") == "answered":
        return "answered"
    if reason in LOOP_STOPS:
        return reason
    if reason.startswith("planner_process_failed"):
        return "planner_process_failed"
    return "planner_escalated" if result.get("outcome") == "escalated" else "planner_abstained"


def context_reuse(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The verified context the session's tool results carried, by the contract's tier
    names only (source text is an envelope, never counted): ``tiers``, each link, match or
    identity edge stating its ``tier``; ``summarized``, the links a result only counts by
    tier (``tiers`` maps of ``plx.object`` and ``plx.around``); ``reviews``, each review
    decision (``review_id``)."""
    from operating.agent_loop import CONTRACTS
    names = CONTRACTS["$defs"]["tier"]["enum"]
    tiers, summarized = dict.fromkeys(names, 0), dict.fromkeys(names, 0)
    reviews = 0

    def walk(node: Any) -> None:
        nonlocal reviews
        if isinstance(node, Mapping):
            for key, value in node.items():
                if key == "tier" and isinstance(value, str) and value in tiers:
                    tiers[value] += 1
                elif key == "tiers" and isinstance(value, Mapping):
                    for name, count in value.items():
                        if name in summarized and type(count) is int and count > 0:
                            summarized[name] += count
                elif key == "review_id" and isinstance(value, str):
                    reviews += 1
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    for event in events:
        if not event.get("is_error"):
            walk(event.get("result"))
    return {"tiers": tiers, "summarized": summarized, "reviews": reviews}


class AskAgent:
    """The object workspace's Ask as an eval agent (``evals.runner`` Agent protocol, on an
    ``AskHost``): ``admit`` asks the reader's ``plx.find`` for the item's root (none: no
    session, no session slot); ``run`` asks the workspace's Ask (``ask_workspace``,
    ``ObjectWorkspace.ask_run``) about it, its tool calls recorded; the answer is
    ``loop_answer`` of the claims the workspace shows. ``planner``: an
    ``operating.agent_loop`` planner (``ClaudeCodePlanner``; tests script one)."""

    def __init__(self, planner: Any, *, bounds: Any = None, session_seconds: float = 300.0,
                 planner_timeout: int = 120, model: str | None = None,
                 name: str = "agent-loop",
                 prices: tuple[Mapping[str, Any] | None, str] = (None, "price_setting_absent")
                 ) -> None:
        from operating import agent_loop
        self.planner, self.bounds = planner, bounds or agent_loop.Bounds()
        self.session_seconds, self.planner_timeout = session_seconds, planner_timeout
        self.model, self.name, self.prices = model, name, prices

    def _planner_usage(self, since: int | None) -> dict[str, Any]:
        """A planner that measures its requests (``metrics``: the Messages planner): this
        Ask's requests' latency, tokens and cost (``messages_planner.ask_usage``)."""
        metrics = getattr(self.planner, "metrics", None)
        if since is None or not isinstance(metrics, list):
            return {}
        from operating.messages_planner import ask_usage
        return {"planner_usage": {**ask_usage(metrics[since:], self.prices[0]),
                                  "cost_basis": self.prices[1]}}

    def _record(self) -> dict[str, Any]:
        return {"kind": ASK_KIND, "name": self.name, **({"model": self.model} if self.model
                                                        else {})}

    def _timed(self, deadline: float, replies: list[int]
               ) -> Callable[[Mapping[str, Any]], Any]:
        """The planner within the session's time; ``replies[0]`` counts its usable
        replies (a decision the loop can act on, never a process that failed to start)."""
        def plan(request: Mapping[str, Any]) -> Any:
            left = deadline - time.monotonic()
            if left <= 0:
                return {"action": "escalate", "reason": SESSION_TIME}, None
            if hasattr(self.planner, "timeout"):  # a planner process never outlives it
                self.planner.timeout = max(1, min(self.planner_timeout, int(left)))
            decision, tokens = self.planner(request)
            if isinstance(decision, Mapping) and decision.get("action") in PLANNER_ACTIONS \
                    and not str(decision.get("reason") or "").startswith(
                        "planner_process_failed"):
                replies[0] += 1
            return decision, tokens
        return plan

    def admit(self, host: Any) -> runner.AgentRun | None:
        """None when the reader's ``plx.find`` names the item's root (``host.object_id``),
        else the run of a withheld root: no session."""
        item = host.item
        host.object_id = root_object(item, host.call("plx.find", {
            "query": str((item.get("root") or {}).get("identifier") or "")[:200],
            "limit": 10}))
        return None if host.object_id is not None else self._withheld([])

    def _withheld(self, events: list[dict[str, Any]]) -> runner.AgentRun:
        return runner.AgentRun(events, "", {"is_error": True, "subtype": "root_withheld"},
                               {**self._record(), "root": "withheld"})

    def run(self, prompt: str, host: Any) -> runner.AgentRun:
        from operating.mcp_server import AGENT_META_KEY
        if host.object_id is None:
            refused = self.admit(host)
            if refused is not None:
                return refused
        record = {**self._record(), "root": "resolved"}
        metrics = getattr(self.planner, "metrics", None)
        since = len(metrics) if isinstance(metrics, list) else None
        replies = [0]
        session = workspace = access = None
        try:
            try:
                session, workspace, access = ask_workspace(
                    host, self._timed(time.monotonic() + self.session_seconds, replies),
                    self.bounds)
                family = ask_family(host.item, host.object_id)
                asked = workspace.ask_run({"object_id": host.object_id,
                                           "question": ask_question(host.item, host.run_pin),
                                           **({"family": family} if family else {})})
                stamp = access.recorder.server.agent_run.meta()
            finally:  # the session host is closed even when the workspace's close raises
                try:
                    if workspace is not None:
                        workspace.close()
                finally:
                    if session is not None:
                        session.close()
        except Exception as error:  # noqa: BLE001 - one session's failure blocks only it
            events = access.recorder.events if access is not None and access.recorder else []
            if isinstance(error, KeyError) and error.args == ("object_not_found",):
                return self._withheld(events)  # the workspace's root check
            code = str(error) if isinstance(error, runner.RunnerError) \
                else f"ask_session_failed:{type(error).__name__}"
            return runner.AgentRun(events, "", {"is_error": True, "subtype": code},
                                   {**record, "session": code, **self._planner_usage(since)})
        record.update(self._planner_usage(since))
        result = asked["loop"]
        stop = {"outcome": asked["outcome"], "reason": asked["reason"],
                "class": stop_class(asked), "steps": result["steps"]}
        record.update({AGENT_META_KEY: stamp, "stop": stop,
                       "claims": {"kept": len(result["claims"]), "shown": len(asked["claims"]),
                                  "withheld": asked["withheld"],
                                  "dropped": result["dropped_claims"],
                                  "unsupported": len(result["unsupported_claims"]),
                                  # why each was refused: reasons and gate codes, no text
                                  "refused": [{"reason": u.get("reason"), "codes": sorted(
                                      {f.get("code") for f in u.get("failures") or ()})}
                                      for u in result["unsupported_claims"]]},
                       "claim_checks": result["claim_checks"],
                       "context_reuse": context_reuse(access.recorder.events)})
        usage: dict[str, Any] = {"tokens": result["usage"]["tokens"]}
        if not replies[0] or stop["class"] == "planner_process_failed":
            # No usable planner reply (it did not start, or started and answered nothing
            # the loop can use: not logged in, a usage limit, a timeout, an is_error
            # result), or the planner failed after one (a usage limit mid-session): a
            # failed session, blocked as eval.real_agent blocks one, never a scored outcome.
            usage.update(is_error=True, subtype="planner_process_failed"
                         if stop["class"] == "planner_process_failed" else "planner_no_reply")
            return runner.AgentRun(access.recorder.events, "", usage, record)
        answer = loop_answer({**result, "outcome": asked["outcome"], "claims": asked["claims"],
                              "text": asked["text"]})
        final = "```json\n" + json.dumps(answer, ensure_ascii=False) + "\n```"
        return runner.AgentRun(access.recorder.events, final, usage, record)


def ask_summary(transcripts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """A part's ask sessions: stop classes, the distinct agent stamps (id, version,
    digest), withheld roots, failed sessions (start-up refused or raised), claims the
    workspace withheld, unsupported claims (``confident_unsupported``: answers the loop's
    claim gate refused whole) and context reuse. Counts and enums only."""
    from operating.mcp_server import AGENT_META_KEY
    records = [t.get("agent") or {} for t in transcripts]
    records = [r for r in records if r.get("kind") == ASK_KIND]
    resolved = [r for r in records if r.get("root") == "resolved"]
    ran = [r for r in resolved if "stop" in r]
    stops = dict.fromkeys(STOP_CLASSES, 0)
    for entry in ran:
        stops[entry["stop"]["class"]] += 1
    stamps = sorted({(str(r[AGENT_META_KEY].get("id")), r[AGENT_META_KEY].get("version"),
                      str(r[AGENT_META_KEY].get("digest"))) for r in ran},
                    key=lambda stamp: (stamp[0], str(stamp[1]), stamp[2]))
    totals: dict[str, dict[str, int]] = {"tiers": {}, "summarized": {}}
    for entry in ran:
        for kind, counts in totals.items():
            for name, count in entry["context_reuse"][kind].items():
                counts[name] = counts.get(name, 0) + count

    def verified(entry: Mapping[str, Any]) -> bool:
        reuse = entry["context_reuse"]
        return bool(reuse["reviews"]) or any(reuse[kind].get(tier) for kind in totals
                                             for tier in ("verified", "reviewed"))
    measured = [r["planner_usage"] for r in resolved if "planner_usage" in r]
    planner = {}
    if measured:  # the Messages planner's latency and cost per Ask
        from operating.messages_planner import usage_summary
        bases = sorted({str(m.get("cost_basis")) for m in measured})
        planner = {"planner": usage_summary(measured, bases[0] if len(bases) == 1
                                            else "mixed")}
    return {**planner, "sessions": len(resolved),
            "sessions_failed": len(resolved) - len(ran),
            "roots_withheld": sum(r.get("root") == "withheld" for r in records),
            "stops": {name: count for name, count in stops.items() if count},
            "agents": [{"id": i, "version": v, "digest": d} for i, v, d in stamps],
            "withheld_claims": sum(r["claims"]["withheld"] for r in ran),
            "unsupported_claims": sum(r["claims"]["unsupported"] for r in ran),
            "sessions_with_unsupported": sum(bool(r["claims"]["unsupported"]) for r in ran),
            "confident_unsupported": sum(
                r["stop"]["class"] in ("no_cited_claims", "no_checked_claims")
                and bool(r["claims"]["unsupported"]) for r in ran),
            "context_reuse": {
                **totals, "reviews": sum(r["context_reuse"]["reviews"] for r in ran),
                "sessions_with_verified_or_reviewed": sum(map(verified, ran))}}


def main(argv: Sequence[str] | None = None, *, agent: Any = None, planner: Any = None,
         environ: Mapping[str, str] | None = None,
         live_sources: Callable[..., Any] | None = None,
         live_host: Callable[[], Any] | None = None) -> int:
    """``agent`` (tests, ``run``) or ``planner`` (tests, ``ask``): an injected agent or
    Ask planner; its runs are ``injected-transport``. ``environ`` (default ``os.environ``),
    ``live_sources`` and ``live_host`` (``live_part``'s ``build_sources`` and
    ``make_host``): the live part's inputs."""
    from operating import agent_loop
    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_real_agent",
                                     description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="one daily k=1 run (spends Claude Code usage)")
    ask = commands.add_parser("ask", help="the live and fixture-sample items through the "
                                          "workspace's Ask (spends Claude Code usage)")
    for sub, budget in ((run, 4800.0), (ask, 4700.0)):
        sub.add_argument("--work-dir", type=Path, required=True,
                         help="throwaway directory outside the repository (the verifier's "
                              "{tmp})")
        sub.add_argument("--total-budget-seconds", type=float, default=budget)
        sub.add_argument("--max-sessions", type=int, default=None,
                         help="session cap (default: one per item)")
        sub.add_argument("--min-scored", type=int, default=8)
        sub.add_argument("--claude-bin", default="claude")
        sub.add_argument("--model")
        sub.add_argument("--reader-env", default=READER_ENV,
                         help="variable holding the named reader's identity (JSON claims); "
                              "unset: the declared eval principal (operating.eval_principal)")
        sub.add_argument("--host-config",
                         help=f"plx-host-config/v1 (default ${HOST_CONFIG_ENV})")
        sub.add_argument("--live-count", type=int, default=DEFAULT_LIVE_COUNT)
        sub.add_argument("--min-live-items", type=int, default=MIN_LIVE_ITEMS)
        sub.add_argument("--live-read-seconds", type=float, default=LIVE_READ_SECONDS)
        sub.add_argument("--root-probe-seconds", type=float, default=ROOT_PROBE_SECONDS)
        sub.add_argument("--deterministic-seconds", type=float, default=DETERMINISTIC_SECONDS)
        sub.add_argument("--mailbox")
        sub.add_argument("--folder", default="inbox")
        sub.add_argument("--sharepoint-path", action="append", default=[])
    run.add_argument("--session-timeout", type=int, default=300)
    run.add_argument("--max-turns", type=int, default=25)
    run.add_argument("--live", action="store_true",
                     help="add the live BC-rooted part (the named reader or the eval "
                          "principal, the persistent host)")
    ask.add_argument("--session-seconds", type=float, default=300.0,
                     help="one Ask session's time; later planner turns escalate")
    ask.add_argument("--planner-timeout", type=int, default=120,
                     help="one planner process's timeout (seconds)")
    ask.add_argument("--planner", choices=("claude-code", "messages"), default="claude-code",
                     help="the Ask's planner: a headless Claude Code session, or the "
                          "Messages API (operating.messages_planner; key and model from "
                          "its settings)")
    ask.add_argument("--fixture-only", action="store_true",
                     help="the fixture sample only (no live part)")
    ask.add_argument("--max-steps", type=int, default=agent_loop.Bounds.max_steps)
    ask.add_argument("--max-tool-calls", type=int, default=agent_loop.Bounds.max_tool_calls)
    ask.add_argument("--max-tokens", type=int, default=agent_loop.Bounds.max_tokens,
                     help="the conversation one planner step re-reads (tokens)")
    ask.add_argument("--max-billed-tokens", type=int,
                     default=agent_loop.Bounds.max_billed_tokens,
                     help="every planner step's usage summed (tokens)")
    args = parser.parse_args(argv)
    asking = args.command == "ask"
    environ = os.environ if environ is None else environ
    injected = (planner if asking else agent) is not None
    evidence = "injected-transport" if injected else "real-claude-session"
    name = "agent-loop" if asking else "claude-code"
    messages = asking and args.planner == "messages"
    if messages:
        from operating import messages_planner
        prices = messages_planner.prices(environ)
        if not injected:
            evidence = "real-messages-session"
            try:
                planner = messages_planner.MessagesPlanner.from_settings(
                    environ, model=args.model, timeout=args.planner_timeout)
            except messages_planner.PlannerRefused as refused:
                # A typed refusal naming the settings (names only, never a value).
                print(json.dumps({
                    "schema": SCHEMA, "adapter_status": "live-unverified",
                    "evidence_label": "isolated-fixture", "agent_evidence": evidence,
                    "values_published": False,
                    "eval": {"agent": name, "planner": "messages", "k": 1, "sessions": 0,
                             "complete": False},
                    "blocked_upstream": [{"part": "agent_session", "source": "messages-api",
                                          "error_code": refused.code,
                                          "cause": "later_ask"}],
                    "agent_unmeasured": refused.code, "agent_settings": refused.settings},
                    indent=1, sort_keys=True))
                return 0
    if not injected and not messages and shutil.which(args.claude_bin) is None:
        report = {"schema": SCHEMA, "adapter_status": "live-unverified",
                  "evidence_label": "isolated-fixture", "agent_evidence": evidence,
                  "values_published": False, "eval": {"agent": name, "k": 1,
                                                      "sessions": 0, "complete": False},
                  "blocked_upstream": [{"part": "agent_session", "source": "claude-code",
                                        "error_code": "binary_not_found",
                                        "cause": "host_misconfiguration"}],
                  "agent_unmeasured": "binary_not_found"}
        print(json.dumps(report, indent=1, sort_keys=True))
        return 0
    if asking:
        try:
            bounds = agent_loop.Bounds(max_steps=args.max_steps,
                                       max_tool_calls=args.max_tool_calls,
                                       max_tokens=args.max_tokens,
                                       max_billed_tokens=args.max_billed_tokens)
        except ValueError as error:
            parser.error(str(error))
        agent = AskAgent(planner or agent_loop.ClaudeCodePlanner(
            binary=args.claude_bin, model=args.model, timeout=args.planner_timeout),
            bounds=bounds, session_seconds=args.session_seconds,
            planner_timeout=args.planner_timeout,
            model=getattr(planner, "model", None) if messages else args.model,
            **({"prices": prices} if messages else {}))
    elif agent is None:
        # Its final answer is scored as written: the claim gate it reaches is the
        # Workbench's own, in its MCP session (operating.answer_gate, plx.case_get answer).
        agent = runner.ClaudeCodeAgent(binary=args.claude_bin, model=args.model,
                                       timeout=args.session_timeout, max_turns=args.max_turns)
    live_on = (asking and not args.fixture_only) or (not asking and args.live)
    try:
        args.work_dir.mkdir(parents=True, exist_ok=True)
        parts: dict[str, Any] = {}
        blocked: list[dict[str, Any]] = []
        live_info = None
        if live_on:
            live, blocked, live_info = live_part(environ, args, build_sources=live_sources,
                                                 make_host=live_host, ask=asking,
                                                 eval_principal_fallback=True)
            if live is not None:
                parts[LIVE_PART] = live
            live_info = {**live_info, **host_provisioned(environ)}
        fixture = fixture_parts(args.work_dir)
        # The Ask is rooted at an object: the exposed cases' empty mirror holds none.
        parts.update({"fixture_sample": fixture["fixture_sample"]} if asking else fixture)
        report = evaluate(ask_parts(parts) if asking else parts, agent,
                          out=args.work_dir / "runs",
                          budget_seconds=args.total_budget_seconds,
                          max_sessions=args.max_sessions, agent_evidence=evidence,
                          agent_name=name)
    except (runner.RunnerError, scoring.ScoreError, ValueError, OSError) as exc:
        print(json.dumps({"error": type(exc).__name__,
                          **({"code": str(exc)} if isinstance(exc, runner.RunnerError)
                             else {})}), file=sys.stderr)  # a RunnerError's text is a code
        return 1
    report["eval"]["tiers"] = tier_block(report, live_info, blocked)
    if asking:
        report["eval"]["planner"] = args.planner
    if messages:
        for part in report.get("blocked_upstream") or []:
            if part.get("part") == "agent_session":
                part["source"] = "messages-api"
    if live_info is not None:
        report["eval"]["live"] = {k: v for k, v in live_info.items() if k != "tiers"}
        report["evidence_label"] = ("live-captures" if LIVE_PART in parts
                                    else report["evidence_label"])
    if blocked:
        report["blocked_upstream"] = blocked + list(report.get("blocked_upstream") or [])
    report["agent_unmeasured"] = next(
        (part["error_code"] for part in report.get("blocked_upstream") or []
         if part.get("part") == "agent_session"), None)
    code = verdict(report, min_scored=args.min_scored,
                   min_live_items=args.min_live_items if live_on else None)
    print(json.dumps(report, indent=1, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
