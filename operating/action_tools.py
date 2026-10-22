"""``plx.act``: the goal-2 action path as one case-host MCP tool.

One tool, three modes, for the case host's 22-tool budget: it replaces the retired
``plx.action_get`` (mode status) and ``plx.action_propose`` (mode propose) and adds internal
execution and verification. Wiring (2026-10-04):
``operating/mcp_server.py`` lists it and calls one ``ActTool`` per session through
``MCPServer.handle`` (output boundary, session actor, per-call host policy);
``register`` serves the same tool through ``ontology_host.CaseHostTools.call``. Modes:

* ``propose``. With ``{case_id, proposal_id}``: register a recorded owner-system case
  proposal (``plx.case_propose``) as an intent on ``operating.action_path``: the
  capability guards run again on the cited rows as they are now, then
  ``ActionPath.register`` (catalog hook: typed parameters, each traced to cited
  evidence or the human-written objective; injection hook). Nothing is requested in
  Mission Control and nothing is dispatched: owner-system writes stay propose -> MC
  approval (a human) -> the owner executes -> read-back. With ``{case_id, action,
  parameters}``: a ``workbench_internal`` action type of ``operating/action_catalog.json``
  (identity link review, correction, follow-up, draft) runs now, under the catalog's
  approval rule (``approval.required``; only ``none`` executes), through
  ``ActionCatalog.execute``: typed, traced parameters, declared preconditions, one write
  to the Workbench case store, then a re-read of that store as the acting reader. An
  owner-system type named this way is refused (``owner_system_action_needs_mc_gate``).
* ``status``. With ``intent_id``: one intent as the session actor may see it
  (``ActionPath.view``), plus the catalog's approval rule for its capability. With
  ``case_id``: the case's intents, paged. Reads the local action store and the case
  only: no Mission Control call.
* ``verify``. With ``intent_id``: the independent read step for the intent's state,
  through host-configured readers (``ActionReads``) only:
  - the MC journal read (``goal2_mc_approval.read_journal``: one ``mc_get_task``
    through the operator's ``MissionControlMcpClient``, which must hold no write
    tool) whenever the intent has a gate request on an MC task;
  - ``gate_requested`` and the gate decided in MC: ``ActionPath.record_decision``
    (reads MC and re-checks the decision; a pending gate records nothing);
  - ``gate_request_unknown`` / ``gate_request_lost``: ``ActionPath.reconcile_gate``
    (reads the task; never re-sends);
  - ``dispatched`` / ``dispatched_unknown`` / an escalation after a dispatch:
    ``ActionPath.read_back`` against the owning record. This is the only way an
    intent becomes ``verified``; an unknown dispatch is reconciled only by it.

Never reachable from here: the MC gate request (``mc_request_approval``, an MC write),
the per-call human approval, the dispatch, escalation, and every other MC write.
Agents are never principals: MC decides gates (a human admin/owner), Portal owns
business approvals. Evidence: ``isolated-fixture`` (dispatch/read-back),
``workbench-store`` (internal actions), ``mc-read`` (the journal; exercised only over
injected transports here: live-unverified, ``verify/live-checks/mc.action_journal.json``).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from functools import partial
from types import MappingProxyType, SimpleNamespace
from typing import Any, Callable, Mapping

from integrations.http import SourceUnavailable
from integrations.live_source_guard import ReadOnlyGuardError
from integrations.mc import MissionControlMcpClient

from . import capability_guards
from .action_catalog import ActionRefused
from .action_path import ActionPath, ActionPathError, FixtureSystem, IntentNotFound
from .goal2_exec_owner import IsolatedOwnerSystem, read_back_for
from .goal2_mc_approval import canonical_email, read_journal
from .cases import CaseNotFound, CitationUnresolvable
from .ontology_tools import shared_schema

TOOL_NAME = "plx.act"
MODES = ("propose", "status", "verify")
_INTENT_ID = re.compile(r"^act_[0-9a-f]{24}$")
_ACTION_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63}){1,5}$")
# The owner the Workbench runs itself (catalog ``owner_system: fixture``); the only owner
# whose record a host may read back today. Real systems are refused at dispatch.
ISOLATED_OWNER = "fixture"
_READ_BACK_STATES = frozenset({"dispatched", "dispatched_unknown"})
_RECONCILE_STATES = frozenset({"gate_request_unknown", "gate_request_lost"})

TOOL: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Goal-2 actions on a case. mode propose {case_id, proposal_id}: register a "
        "recorded owner-system proposal (plx.case_propose) as an intent; guards re-run, "
        "parameters must trace to cited values or the human-written objective. It never "
        "requests approval or dispatches: a human approves in Mission Control, the owner "
        "executes. propose {case_id, action, parameters}: run a workbench_internal catalog "
        "action under its approval rule and read it back. status {intent_id} or {case_id, offset, limit}: local reads. verify "
        "{intent_id}: read Mission Control to record a decided gate or reconcile an unknown "
        "request, or read the owning record back (the only way to verified). No Mission "
        "Control write, no dispatch."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": list(MODES)},
            "case_id": {"type": "string", "minLength": 1, "maxLength": 64},
            "proposal_id": {"type": "string", "minLength": 1, "maxLength": 32},
            "action": {"type": "string", "minLength": 3, "maxLength": 200},
            "parameters": {"type": "object"},
            "intent_id": {"type": "string", "pattern": "^act_[0-9a-f]{24}$",
                          "minLength": 28, "maxLength": 28},
            "offset": {"type": "integer", "minimum": 0, "maximum": 1000000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
        "required": ["mode"], "additionalProperties": False},
    "annotations": {"readOnlyHint": False, "destructiveHint": False,
                    "idempotentHint": False, "openWorldHint": True},
}

# Arguments each mode accepts besides ``mode`` (exactly one form per call).
_FORMS: Mapping[str, tuple[frozenset[str], ...]] = {
    "propose": (frozenset({"case_id", "proposal_id"}),
                frozenset({"case_id", "action", "parameters"})),
    "status": (frozenset({"intent_id"}), frozenset({"case_id", "offset", "limit"})),
    "verify": (frozenset({"intent_id"}),),
}
_REQUIRED: Mapping[frozenset[str], frozenset[str]] = {
    frozenset({"case_id", "proposal_id"}): frozenset({"case_id", "proposal_id"}),
    frozenset({"case_id", "action", "parameters"}): frozenset({"case_id", "action",
                                                              "parameters"}),
    frozenset({"intent_id"}): frozenset({"intent_id"}),
    frozenset({"case_id", "offset", "limit"}): frozenset({"case_id"}),
}


class ActionReads:
    """The host's read-only access for ``plx.act verify`` (configured by a person).

    ``mc``: the operator's ``MissionControlMcpClient`` holding **no** write tool (a
    client opted in to any MC write is refused here: this host never holds a write
    door). ``owners``: the owning systems whose records may be read back, by
    ``owner_system``; only the isolated owner the Workbench runs itself
    (``fixture``: a ``FixtureSystem`` or ``goal2_exec_owner.IsolatedOwnerSystem``).
    Empty by default: verify then reads nothing and says what is not configured.
    """

    __slots__ = ("mc", "owners")

    def __init__(self, *, mc: MissionControlMcpClient | None = None,
                 owners: Mapping[str, Any] | None = None) -> None:
        if mc is not None:
            if type(mc) is not MissionControlMcpClient:
                raise TypeError("mc must be a MissionControlMcpClient")
            if mc.write_tools:
                raise ValueError("action_reads_mc_must_hold_no_write_tool")
        clean: dict[str, Any] = {}
        for system, owner in dict(owners or {}).items():
            if system != ISOLATED_OWNER or type(owner) not in (FixtureSystem,
                                                               IsolatedOwnerSystem):
                raise ValueError("action_reads_owner_must_be_the_isolated_owner")
            clean[system] = owner
        object.__setattr__(self, "mc", mc)
        object.__setattr__(self, "owners", MappingProxyType(clean))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("ActionReads is sealed")

    def read_back(self, owner_system: str) -> Any:
        owner = self.owners.get(owner_system)
        if owner is None:
            return None
        if type(owner) is FixtureSystem:
            return lambda intent: FixtureSystem.read_back(owner, intent)
        return read_back_for(owner)


def _mc_counts(mc: MissionControlMcpClient | None, since: int) -> tuple[int, int]:
    """(reads, writes) the client audited after audit seq ``since``."""
    if mc is None:
        return 0, 0
    records = [r for r in mc.audit.records if r["seq"] > since]
    return (sum(1 for r in records if r["kind"] == "read"),
            sum(1 for r in records if r["kind"] != "read"))


def _audit_seq(mc: MissionControlMcpClient | None) -> int:
    if mc is None:
        return 0
    records = mc.audit.records
    return records[-1]["seq"] if records else 0


class ActTool:
    """``plx.act`` for one case runtime: ``tool(arguments, session_actor)``.

    ``path``: the runtime's ``ActionPath`` (over the runtime's own case store);
    ``reads``: ``ActionReads`` for verify (default: none configured);
    ``test_capabilities``: a test host (catalog ``hosts: ["test"]`` capabilities pass the
    guards), never set by a production host;
    ``schema``: the host's compiled schema, read per call (``schema()``), that the gap guard
    checks a follow-up's property against, as ``plx.case_record_gap`` on the same host
    does (default: ``ontology_tools.shared_schema``);
    ``session``: the host's ``SessionIdentity`` (the intent's ``sub``/``act`` come from it
    alone, never from the tool's arguments); without one ``ActionPath`` refuses every
    step ``propose`` and ``verify`` would record (``session_required``), and ``verify``
    reads nothing first.
    """

    __slots__ = ("runtime", "path", "reads", "test_capabilities", "schema", "session")

    def __init__(self, runtime: Any, path: ActionPath | None, *,
                 reads: ActionReads | None = None, test_capabilities: bool = False,
                 schema: Callable[[], Any] = shared_schema, session: Any = None) -> None:
        if path is not None:
            if type(path) is not ActionPath:
                raise TypeError("path must be an ActionPath")
            if path.cases is not runtime.cases:
                raise ValueError("action_path_must_use_the_case_runtime_case_store")
        if reads is not None and type(reads) is not ActionReads:
            raise TypeError("reads must be an ActionReads")
        if type(test_capabilities) is not bool:
            raise TypeError("test_capabilities must be a bool")
        if not callable(schema):
            raise TypeError("schema must be a callable returning the host's schema")
        object.__setattr__(self, "runtime", runtime)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "reads", reads)
        object.__setattr__(self, "test_capabilities", test_capabilities)
        object.__setattr__(self, "schema", schema)
        object.__setattr__(self, "session", session)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("ActTool is sealed")

    def __call__(self, arguments: Any, actor: Any) -> dict[str, Any]:
        from . import mcp_server as host  # the host's argument errors and paging helpers

        if not isinstance(arguments, dict):
            raise host.InputError("invalid_arguments")
        try:
            return _call(self, host, actor, arguments)
        except CaseNotFound:
            raise host.InputError("case_not_found") from None
        except CitationUnresolvable:
            raise host.InputError("citation_unresolvable_for_actor") from None


def register(host_tools: Any, runtime: Any, path: ActionPath | None, *,
             reads: ActionReads | None = None, test_capabilities: bool = False,
             session: Any = None) -> ActTool:
    """Serve ``plx.act`` through ``ontology_host.CaseHostTools.call`` (the gap guard
    reads that host's schema)."""
    tool = ActTool(runtime, path, reads=reads, test_capabilities=test_capabilities,
                   schema=lambda: host_tools.schema, session=session)
    host_tools.register_tool(TOOL_NAME, tool)
    return tool


def _call(tool: ActTool, host: Any, actor: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    path = tool.path
    if path is None:
        raise host.InputError("action_path_not_configured: the host did not pass "
                              "--enable-action-path (default deny)")
    unknown = set(arguments) - set(TOOL["inputSchema"]["properties"])
    if unknown:
        raise host.InputError("unknown_arguments:" + ",".join(sorted(unknown))[:128])
    mode = host._arg(arguments, "mode", str, required=True, lower=1, upper=16)
    if mode not in MODES:
        raise host.InputError("invalid_argument_value:mode")
    given = frozenset(arguments) - {"mode"}
    form = next((f for f in _FORMS[mode] if given <= f and _REQUIRED[f] <= given), None)
    if form is None:
        raise host.InputError(f"mode_{mode}_takes:" + " | ".join(
            ",".join(sorted(f)) for f in _FORMS[mode]))
    if mode == "verify" and tool.session is None:
        # Its step records who acted (decision goal2-identity): no MC or owner read for
        # a step that would be refused.
        raise host.InputError("action_refused:session_required")
    try:
        if mode == "propose" and "proposal_id" in form:
            return _register(tool, host, path, actor, arguments)
        if mode == "propose":
            return _execute_internal(host, path, tool.runtime, actor, arguments, tool.schema,
                                     tool.session)
        if mode == "status" and "case_id" in form:
            return _list(host, path, actor, arguments)
        intent_id = host._arg(arguments, "intent_id", str, required=True, lower=28, upper=28)
        if not _INTENT_ID.fullmatch(intent_id):
            raise host.InputError("invalid_argument_value:intent_id")
        if mode == "status":
            intent = path.view(actor, intent_id)
            return {"mode": "status", "intent": intent,
                    "approval_rule": _approval_rule(path, intent), "mc_calls": 0,
                    "business_system_writes": 0}
        return _verify(host, path, tool.reads, actor, intent_id, tool.session)
    except IntentNotFound:
        raise host.InputError("intent_not_found") from None
    except ActionPathError as exc:
        raise host.InputError(f"action_refused:{exc.code}"[:400]) from None


def _approval_rule(path: ActionPath, intent: Mapping[str, Any]) -> dict[str, Any] | None:
    """The catalog's approval rule for an intent's capability (none when withheld)."""
    if intent.get("withheld"):
        return None
    entry = path.catalog.get(intent.get("capability"))
    if entry is None:
        return None
    rule = entry["approval"]
    return {key: rule[key] for key in ("required", "tool", "per_call_human_approval",
                                       "decider") if key in rule}


def _register(tool: ActTool, host: Any, path: ActionPath, actor: Any,
              arguments: dict[str, Any]) -> dict[str, Any]:
    case_id = host._arg(arguments, "case_id", str, required=True, lower=1, upper=64)
    proposal_id = host._arg(arguments, "proposal_id", str, required=True, lower=1, upper=32)
    # The guards are checked again on the cited rows as they are now.
    proposal = next((item for item in tool.runtime.cases.get(
        actor, case_id).get("proposals", ()) if item.get("id") == proposal_id), None)
    # A proposal withheld from this reader shows no capability; registration refuses
    # it on its own (its citations must resolve).
    if proposal is not None and proposal.get("capability") is not None:
        # The case host's own guard check (plx.case_propose), reused
        # as is: it reads only ``test_capabilities`` from the host.
        guard_host = SimpleNamespace(test_capabilities=tool.test_capabilities)
        guard_host._capability_offered = partial(host.MCPServer._capability_offered, guard_host)
        host.MCPServer._guard_verdict(
            guard_host, tool.runtime, actor,
            proposal["capability"], proposal.get("payload"), proposal.get("expected_effect"),
            [cite.get("handle") for cite in proposal.get("citations") or ()])
    # The intent journal is not the case journal: registration checks the case's claim
    # and commits the intent under the case's write lock (``CaseStore.outside_write``).
    # The cited rows are read as this reader reads them now: the same reader the guard
    # check above used, so a byte-pinned document citation's fields are readable.
    registered = path.register(actor, case_id, proposal_id, session=tool.session,
                               read=capability_guards.current_reader(tool.runtime, actor))
    intent = path.view(actor, registered["intent_id"])
    return {"mode": "propose", "kind": "owner_system_proposal", "intent": intent,
            "duplicate": registered["duplicate"], "approval_rule": _approval_rule(path, intent),
            "approval_requested": False, "dispatched": False, "mc_calls": 0,
            "business_system_writes": 0,
            "note": "Registered on the local action path only. The Mission Control approval "
                    "request, its human per-call approval, the decision (a human MC "
                    "admin/owner) and the owner's execution are not available through this "
                    "tool; plx.act verify reads their outcome.",
            "next_tools": ["plx.act (mode status)", "plx.act (mode verify)"]}


def _execute_internal(host: Any, path: ActionPath, runtime: Any, actor: Any,
                      arguments: dict[str, Any], schema: Callable[[], Any],
                      session: Any = None) -> dict[str, Any]:
    case_id = host._arg(arguments, "case_id", str, required=True, lower=1, upper=64)
    action = host._arg(arguments, "action", str, required=True, lower=3, upper=200)
    parameters = host._arg(arguments, "parameters", dict, required=True)
    if not _ACTION_ID.fullmatch(action):
        raise host.InputError("invalid_argument_value:action")
    entry = path.catalog.get(action)
    if entry is None:
        raise host.InputError("action_refused:action_type_not_in_catalog")
    if entry["kind"] != "workbench_internal":
        raise host.InputError(
            "action_refused:owner_system_action_needs_mc_gate: record it with "
            "plx.case_propose, then plx.act {mode: propose, case_id, proposal_id}")
    rule = entry["approval"]
    if rule.get("required") != "none":   # the catalog's approval rule decides
        raise host.InputError("action_refused:approval_required:"
                              + str(rule.get("required"))[:40])
    try:
        # The gap guard reads the cited rows as this reader reads them now, against the
        # host's schema plx.case_record_gap checks (the same prover on both routes).
        record = path.execute_internal(actor, action, case_id, parameters, session=session,
                                       read=capability_guards.current_reader(runtime, actor),
                                       schema=schema())
    except ActionRefused as exc:
        if exc.code == "case_not_found":
            raise CaseNotFound(case_id) from None
        raise host.InputError(f"action_refused:{exc.code}"[:400]) from None
    return {"mode": "propose", "kind": "workbench_internal", "execution": record,
            "approval_rule": {"required": rule["required"], "why": rule.get("why")},
            "mc_calls": 0, "business_system_writes": 0,
            "evidence_label": record.get("evidence_label")}


def _list(host: Any, path: ActionPath, actor: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    case_id = host._arg(arguments, "case_id", str, required=True, lower=1, upper=64)
    offset = host._arg(arguments, "offset", int, default=0, lower=0, upper=1_000_000)
    limit = host._arg(arguments, "limit", int, default=20, lower=1, upper=50)
    listed = path.list_for_case(actor, case_id)
    intents, total = listed["intents"], listed["total"]
    capped = bool(listed["truncated"])
    base = {key: value for key, value in listed.items()
            if key not in {"intents", "total", "truncated"}}

    def build(page: list[Any]) -> dict[str, Any]:
        # total is every intent registered for the case (never the page or the
        # listable cap); truncated says more exist beyond this page.
        end = offset + len(page)
        next_offset = end if end < len(intents) else None
        return {"mode": "status", **base, "offset": offset, "limit": limit, "total": total,
                "listed": len(intents), "returned": len(page), "next_offset": next_offset,
                "store_capped": capped, "truncated": capped or next_offset is not None,
                "complete": not capped and offset == 0 and next_offset is None,
                "mc_calls": 0, "business_system_writes": 0, "intents": page}

    return host._fit_page(intents, offset, limit, build,
                          lambda item: host._without_key(item, "history"),
                          ("proposal_id", "intent_id", "state", "withheld"))


def _verify(host: Any, path: ActionPath, reads: ActionReads | None, actor: Any,
            intent_id: str, session: Any = None) -> dict[str, Any]:
    """The read step the intent's state calls for; never a send, never a dispatch. The
    step records the host session's ``act`` (``ActionPath``, decision goal2-identity)."""
    reads = reads or ActionReads()
    before = path.view(actor, intent_id)   # the case is read for the actor first
    if before.get("withheld"):
        # The intent derives from rows this reader cannot see: no reads on its behalf.
        raise host.InputError("action_refused:proposal_withheld")
    state, mc = before["state"], reads.mc
    if mc is not None and (path.mc_operator is None or canonical_email(
            mc._config.operator_email) != path.mc_operator):
        # The journal is read as the operator that requested the gate, or not at all.
        raise ActionPathError("mc_operator_mismatch")
    gate = before.get("gate") or {}
    since = _audit_seq(mc)
    journal: dict[str, Any] | None = None
    if gate.get("task_id") and mc is not None:
        journal = read_journal(mc, gate["task_id"], gate_id=gate.get("gate_id"),
                               reason=before["gate_reason"])
    step: str | None = None
    result: dict[str, Any] | None = None
    reason: str | None = None
    if state == "gate_requested":
        if mc is None:
            reason = "mc_reader_not_configured"
        elif not (journal or {}).get("readable"):
            reason = (journal or {}).get("reason") or "mc_unreadable"
        elif ((journal.get("gate") or {}).get("status")) in ("approved", "rejected"):
            try:
                path.record_decision(actor, intent_id, mc, session=session)
                step = "record_decision"
            except (SourceUnavailable, ReadOnlyGuardError):
                # The re-read failed before anything was recorded: read again later.
                reason = "mc_unreadable"
        else:
            reason = "gate_not_decided_in_mc"   # pending: nothing to record
    elif state in _RECONCILE_STATES:
        if mc is None:
            reason = "mc_reader_not_configured"
        else:
            try:
                found = path.reconcile_gate(actor, intent_id, mc, session=session)
                step = "reconcile_gate"
                result = {key: found.get(key) for key in (
                    "state", "reconciled", "reason", "matching_gates", "gate_id",
                    "send_in_flight") if key in found}
            except (SourceUnavailable, ReadOnlyGuardError):
                reason = "mc_unreadable"   # nothing recorded; never a send
    elif state in _READ_BACK_STATES or (state == "escalated" and before.get("dispatches")):
        read_back = reads.read_back(before["owner_system"])
        if read_back is None:
            reason = f"read_back_not_available:{before['owner_system']}"
        else:
            step = "read_back"
            found = path.read_back(actor, intent_id, read_back, session=session)
            result = {key: found.get(key) for key in (
                "state", "outcome", "reason", "reconciled_by", "attribution", "concludes")}
    else:
        reason = "nothing_to_verify_in_state:" + str(state)
    after = path.view(actor, intent_id)
    if step == "record_decision" and after.get("decisions"):
        result = {"state": after["state"], **{key: after["decisions"][-1].get(key) for key in (
            "gate_id", "status", "authorizes", "reason_code", "decider_role")}}
    reads_made, writes_made = _mc_counts(mc, since)
    return {"mode": "verify", "intent_id": intent_id, "state_before": state,
            "state": after["state"], "step": step, "step_result": result, "reason": reason,
            "mc_journal": journal, "intent": after, "approval_rule": _approval_rule(path, after),
            "mc_calls": reads_made, "mc_writes": writes_made, "dispatched": False,
            "business_system_writes": 0}


def _journal_probe(argv: list[str] | None = None) -> int:
    """``python3 -m operating.action_tools mc-journal --task-id TASK-N``: one MC journal
    read through a read-only client from the environment (live check
    ``mc.action_journal``). Prints the shape only: stage, gate counts, event kinds."""
    parser = argparse.ArgumentParser(prog="python3 -m operating.action_tools")
    parser.add_argument("probe", choices=["mc-journal"])
    parser.add_argument("--task-id", help="default: env MC_ACTION_JOURNAL_TASK_ID")
    parser.add_argument("--operator-email-default")
    args = parser.parse_args(argv)
    from integrations.plx_hub_guard import McConfig
    import os

    task_id = args.task_id or os.environ.get("MC_ACTION_JOURNAL_TASK_ID", "").strip()
    if not task_id:
        parser.error("--task-id or MC_ACTION_JOURNAL_TASK_ID is required")
    email = os.environ.get("MC_OPERATOR_EMAIL") or args.operator_email_default
    client = MissionControlMcpClient(McConfig.from_env(operator_email=email))
    journal = read_journal(client, task_id, gate_id=None, reason="")
    task = journal.get("task") or {}
    print(json.dumps({"probe": "mc-journal/v1", "readable": journal["readable"],
                      "reason": journal.get("reason"), "stage_known": task.get("stage_known"),
                      "stage": task.get("stage"), "pending_gates": task.get("pending_gates"),
                      "write_tools": sorted(client.write_tools),
                      "mc_writes": _mc_counts(client, 0)[1]}, sort_keys=True))
    return 0 if journal["readable"] else 1


__all__ = ["ActTool", "ActionReads", "MODES", "TOOL", "TOOL_NAME", "register"]


if __name__ == "__main__":
    sys.exit(_journal_probe())
