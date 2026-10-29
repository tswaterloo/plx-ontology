"""Goal 2, executed end to end against an isolated owning system (EXE-10/04/11).

``python3 -m operating.goal2_exec demo --state-dir D`` runs the complete
approval -> dispatch -> read-back lifecycle through the existing contracts
(``operating.action_path``, ``operating.goal2_mc_approval``,
``integrations.mc.MissionControlMcpClient``) and prints the state trail of each
scenario as counts and states only:

* ``happy``: intent registered on a case an employee opened (a human-authored
  question, as the console records it), MC gate requested through the guarded
  write door, an agent's attempt to decide refused by MC, approved by a human MC
  admin, dispatched exactly once with the idempotency key, verified by an
  independent read-back (a repeated dispatch sends nothing).
* ``stale_approval``: the cited record changes after the approval: dispatch is
  refused before any owner call; the next step is re-verify and re-propose.
* ``uncertain_then_verified``: the owner applies, then the answer times out:
  ``unknown``, no resend, reconciled by read-back to ``verified``.
* ``uncertain_then_failed``: the owner accepts and times out, later drops the
  request: ``unknown`` (a pending read-back changes nothing), then the read-back
  reports the key not applied and fenced: ``failed``.
* ``owner_rejects``: a conditional-write conflict at the owner: ``failed`` with
  an F7 recovery record, nothing written.

``python3 -m operating.goal2_exec owner-lifecycle --state-dir D`` runs the full
goal-2 lifecycle against the *typed* isolated owner (``OWNER_SCENARIOS``): a case
finding citing an inbound record and the order record it is about; a proposal of
the catalog type ``fixture.record.set_cited_value`` whose every parameter traces
to that cited evidence, with the target's version (etag) as a compare-and-set
precondition; register (the catalog hook); the MC gate; dispatch after a fresh MC
re-read of the approval; read-back of the exact effect; recovery (a different
value or a partial write is flagged and never re-sent; an owner refusal or
timeout stays not executed with its reason; a withdrawn approval dispatches
nothing; a duplicate dispatch is idempotent); and the audit trail on the case
(``case_audit_trail``), with every unresolved outcome flagged on the case as a
follow-up through the catalog's ``workbench.follow_up.record``.

``python3 -m operating.goal2_exec follow-up --state-dir D`` runs the first real action type,
``mc.task.create_follow_up``, against ``IsolatedMissionControl`` (``FOLLOW_UP_SCENARIOS``):
an employee opens a case and records a gap, proposes the MC follow-up task (every
parameter traced to the gap), registers it, the host prepares the exact ``mc_create_task``
call, a person other than the employee approves that call, it is sent once through the
real client's write door with the intent's idempotency key, and read back with
``mc_get_task``; ``lost_answer`` loses MC's answer and reconciles by reading
(``mc_search_tasks`` for the key) to ``verified``. A replay sends nothing, and MC's own
idempotent replay returns the same task (never a second one).

Everything runs in-process over files under ``--state-dir``: the case store, the
action store, the owner's own SQLite state (``goal2_exec_owner.IsolatedOwnerSystem``)
and ``IsolatedMissionControl``, an in-process Mission Control mirroring PLX_MC
``src/lib/compliance/approvals.ts``, ``src/app/api/approvals/decide/route.ts``,
``src/lib/permissions/grants.ts`` and ``predicates.ts`` @ 2f653cf (gates pending
until a human admin/owner other than the requester decides; service principals
never decide), and the cancelled end stage @ 2f653cf (``src/lib/sync/cancel.ts``:
a human admin/owner cancels with a reason, past a pending gate, and reopens). The real MC client talks to it through an injected in-process
sender; nothing leaves the process. No Petra system, credential or network is
touched. Evidence label: ``isolated-fixture`` (MC leg: the real client over an
injected in-process transport).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import secrets
import sys
from typing import Any, Mapping

from integrations.live_source_guard import Secret
from integrations.mc import MissionControlMcpClient, WriteAuthorizationLedger
from integrations.plx_hub_guard import HubTransport, McConfig
from ontology.core import Actor

from . import action_path as ap, answerers
from .cases import CaseStore
from .goal2_exec_owner import (REAL_CAPABILITY_REQUIREMENTS, IsolatedOwnerSystem,
                               dispatch_payload, read_back_for)
from .goal2_mc_approval import load_approver_roster
from .session_identity import CHANNEL_CONSOLE, fixture_session

EVIDENCE_LABEL = "isolated-fixture"
TRAIL_SCHEMA = "plx-goal2-lifecycle-trail/v1"
# Placeholder identities of the isolated fixture: no PLX person, mailbox or key.
OPERATOR = "workbench.operator@isolated-fixture.example"
CALL_APPROVER = "call.approver@isolated-fixture.example"
CALL_APPROVER_OID = "0000a000-0000-4000-8000-00000000ca01"
MC_ADMIN = "mc.admin@isolated-fixture.example"
MC_MEMBER = "mc.member@isolated-fixture.example"
# Their Entra object ids, as the roster a person supplies names them (fixture GUIDs):
# ``ISOLATED_APPROVER_ROSTER``, read through ``load_approver_roster`` like every host's.
ISOLATED_APPROVER_ROSTER = Path(__file__).with_name("isolated_approver_roster.json")
MC_ADMIN_OID = "0000a000-0000-4000-8000-00000000ad01"
MC_MEMBER_OID = "0000a000-0000-4000-8000-00000000ad02"
# The per-call approver is an MC member too: the roster's oid is how separation of duties
# matches them under another address (a call approver without one is refused).
CALL_APPROVER_OID = "0000a000-0000-4000-8000-00000000ad03"
# The employee each fixture action is for (the intent's ``sub``): never its decider.
SUBJECT = "requesting.employee@isolated-fixture.example"
AGENT_SERVICE = "sp_mcp_workbench"
MC_URL = "https://mc.plxcustomer.io"   # the only MC host the guard admits; never reached
RELATION = "fixture.sales_order"
ACTOR = Actor("plx", frozenset({"ops"}))
# The fixture session the actor is (the employee's console request, decision g2-1):
# the explicit test identity every step passes (a step without a session is refused).
SESSION = fixture_session("plx", ACTOR.principals, channel=CHANNEL_CONSOLE,
                          subject_email=SUBJECT)
SCENARIOS = ("happy", "stale_approval", "uncertain_then_verified", "uncertain_then_failed",
             "owner_rejects")
# The MC follow-up task (``mc.task.create_follow_up``): its isolated bucket and scenarios.
FOLLOW_UP_CAPABILITY = "mc.task.create_follow_up"
FOLLOW_UP_BUCKET = "BKT-ISOLATED-FOLLOW-UPS"
FOLLOW_UP_SCENARIOS = ("verified", "lost_answer")
FOLLOW_UP_SCHEMA = "plx-goal2-mc-follow-up/v1"
# The typed isolated owner (``owner-lifecycle``): an order record and the inbound
# record whose cited field supplies the value. Placeholder relations, ours only.
TYPED_RELATION = "fixture.order_header"
SOURCE_RELATION = "fixture.inbound_document"
TYPED_FIELDS = {"id": "integer", "po_reference": "string",
                "external_document_number": "string", "line_count": "integer"}
SOURCE_FIELDS = {"id": "integer", "customer_po": "string", "lines_stated": "string"}
VERSION_FIELD = "version"
CITED_VALUE_CAPABILITY = "fixture.record.set_cited_value"
OWNER_TRAIL_SCHEMA = "plx-goal2-owner-lifecycle/v1"
AUDIT_SCHEMA = "plx-case-action-audit/v1"
# scenario -> (target fields, evidence field, owner faults, before dispatch, prior value
# of the target fields: None = empty, else a correction of that value)
OWNER_SCENARIOS: Mapping[str, tuple[tuple[str, ...], str, tuple[str, ...], str | None,
                                    str | None]] = {
    "verified": (("po_reference",), "customer_po", (), None, None),
    "approval_withdrawn": (("po_reference",), "customer_po", (), "close_task", None),
    "approval_task_restricted": (("po_reference",), "customer_po", (), "restrict_task", None),
    "etag_conflict": (("po_reference",), "customer_po", ("concurrent_write",), None, None),
    "type_refused_at_register": (("line_count",), "lines_stated", (), None, None),
    "owner_timeout": (("po_reference",), "customer_po", ("unavailable",), None, None),
    "correction_pending_then_applied": (("po_reference",), "customer_po",
                                        ("timeout_pending",), None, "PO-WRONG"),
    "partial_failure": (("po_reference", "external_document_number"), "customer_po",
                        ("partial_apply",), None, None),
    "value_differs": (("po_reference",), "customer_po", ("overwritten_after_apply",), None,
                      None),
}


class IsolatedMcRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Clock:
    """Deterministic UTC clock shared by MC, the MC transport and the action path."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self.now += timedelta(microseconds=1)
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


# PLX_MC ``approval-signature.ts`` ``APPROVAL_TTL_MINUTES.unscoped`` @ 2d2cac7: how long an
# approval of an unscoped gate (every gate the Workbench requests) covers, from its decision.
APPROVAL_TTL_UNSCOPED = timedelta(hours=72)


class IsolatedMissionControl:
    """In-process Mission Control: tasks, approval gates and their decisions.

    Mirrors PLX_MC @ 2f653cf: ``mc_request_approval`` appends a pending gate
    (``apg_`` + 16 hex, ``requestedBy`` = the operator header, ``requestedAt``)
    (``approvals.ts`` ``requestApprovalGate``); ``decide`` is the human route
    (``/api/approvals/decide``): a service session never holds
    ``approval.decide`` (``predicates.ts``), only ``admin``/``owner`` users do
    (``grants.ts``), the gate must be pending (409 ``gate_already_decided``) and
    the requester may not decide it (``separation_of_duties``). ``cancel`` is the
    task-detail cancel (``PATCH /api/tasks/{id}`` ``{cancel}``, TASK-2598 @ 77a5af6):
    ``task.cancel`` is an admin/owner grant that, among services, only ``sp_mcp_cos`` holds
    (PLX_MC 6d0aa27, TASK-2635; this stand-in models only the human path), the reason is MC's enum,
    and ``policy.ts`` ``stageAdvanceViolation`` lets a cancel through a pending gate,
    which a human may still decide afterwards; ``reopen`` (``{reopen}``) moves it back to
    the stage before the cancel. Events are ``eventsForTask`` rows (``seq``, ``ts``, ``kind``,
    ``actor``, ``taskId``, ``payload``: ``compliance/repo.ts``). An approval is stamped
    with ``expiresAt`` (decidedAt + 72h, unscoped) and an unsigned ``signature`` (TASK-3408
    @ 2d2cac7; no signing key here). Re-read @ 2d2cac7 (drift-1021-mc-permissions): the
    decide route also refuses a decider off ``PLX_MC_APPROVAL_DECIDERS`` and a scoped gate
    whose rendered scope was not echoed; the Workbench requests unscoped gates only, and
    the isolated MC models the allowlist as unset (any admin/owner). Served to the real
    ``MissionControlMcpClient`` through an in-process sender only.
    """

    def __init__(self, clock: Clock, users: Mapping[str, str]) -> None:
        self.clock = clock
        self.users = {email.lower(): role for email, role in users.items()}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.buckets: set[str] = set()
        # mc_events task-create claims: idempotencyKey -> (payload hash, task id)
        self.create_claims: dict[str, tuple[str, str]] = {}
        self.lose_next_answer = False   # the next answer is lost after MC acted
        self.hidden: set[str] = set()
        self.down = False   # True: MC answers nothing (an outage)
        self.events: list[dict[str, Any]] = []   # mc_events (append-only audit)
        self.tool_calls: list[str] = []
        self.refusals: list[str] = []
        self.health = {"ok": True, "operator": OPERATOR, "dataSource": "live",
                       "boringGateMet": True, "lastSweep": self._stamp()}

    def add_task(self, task_id: str, title: str) -> None:
        self.tasks[task_id] = {"id": task_id, "title": title, "stage": "progress",
                               "accountableOwner": MC_ADMIN, "approvalGates": [],
                               "activity": []}

    def _event(self, kind: str, actor: str, task_id: str, payload: Mapping[str, Any]) -> None:
        """``appendEvent``: one ``mc_events`` row (``compliance/repo.ts`` @ 2f653cf)."""
        self.events.append({"seq": str(len(self.events) + 1), "ts": self._stamp(),
                            "kind": kind, "actor": actor, "taskId": task_id,
                            "payload": dict(payload)})

    def _audit(self, task: dict[str, Any], kind: str, actor: str, what: str,
               payload: Mapping[str, Any]) -> None:
        """``patchTask`` activity line + ``appendEvent`` (approvals.ts @ 2f653cf)."""
        task.setdefault("activity", []).append({"who": actor, "what": what, "kind": "gate"})
        self._event(kind, actor, task["id"], payload)

    def set_stage(self, task_id: str, stage: str, *, session_label: str) -> None:
        """A human moves the task's stage. ``policy.ts`` ``stageAdvanceViolation``:
        a pending gate freezes the stage (input-required) in either direction."""
        task = self.tasks[task_id]
        if any(g["status"] == "pending" for g in task["approvalGates"]):
            self.refusals.append("input_required")
            raise IsolatedMcRefused("input_required")
        task["stage"] = stage
        task["activity"].append({"who": session_label, "what": f"moved to {stage}",
                                 "kind": "move"})

    def cancel(self, task_id: str, reason: str, *, session_label: str, session_kind: str,
               replaced_by: str | None = None) -> None:
        """A human cancels the task (``src/lib/sync/cancel.ts`` ``cancelTaskTx``,
        ``src/lib/mc-data/cancellation.ts`` @ 77a5af6); pending gates stay pending."""
        try:
            if session_kind != "human" or \
                    self.users.get(session_label.lower()) not in ("admin", "owner"):
                raise IsolatedMcRefused("forbidden")            # grants.ts: task.cancel
            task = self.tasks[task_id]
            if task["stage"] == "cancelled":
                raise IsolatedMcRefused("already_cancelled")
            if reason not in ("duplicate", "obsolete", "superseded", "delivered_without_pr") \
                    or (reason in ("duplicate", "superseded") and replaced_by is None):
                raise IsolatedMcRefused("invalid_request")
        except IsolatedMcRefused as refused:
            self.refusals.append(refused.code)
            raise
        previous, task["stage"] = task["stage"], "cancelled"
        task["cancellation"] = {"reason": reason, "replacedBy": replaced_by,
                                "cancelledAt": self._stamp(), "cancelledBy": session_label}
        task["activity"].append({"who": session_label, "what": f"cancelled ({reason})",
                                 "kind": "move"})
        self._event("task.cancelled", f"human:{session_label}", task_id,
                    {"reason": reason, "replacedBy": replaced_by, "previousStage": previous,
                     "cancelledBy": session_label, "note": None})

    def reopen(self, task_id: str, *, session_label: str, session_kind: str) -> None:
        """A human reopens a cancelled task (``cancel.ts`` ``reopenTaskTx`` @ 2f653cf): back
        to the stage before the cancel (``backlog`` for a closed one), cancellation cleared."""
        try:
            if session_kind != "human" or \
                    self.users.get(session_label.lower()) not in ("admin", "owner"):
                raise IsolatedMcRefused("forbidden")            # grants.ts: task.reopen
            task = self.tasks[task_id]
            if task["stage"] != "cancelled":
                raise IsolatedMcRefused("not_cancelled")
        except IsolatedMcRefused as refused:
            self.refusals.append(refused.code)
            raise
        previous = next((e["payload"]["previousStage"] for e in reversed(self.events)
                         if e["taskId"] == task_id and e["kind"] == "task.cancelled"), None)
        stage = previous if previous and previous not in ("merged", "verified") else "backlog"
        prior = task.get("cancellation")
        task.update(stage=stage, cancellation=None)
        task["activity"].append({"who": session_label, "what": f"reopened to {stage}",
                                 "kind": "move"})
        self._event("task.reopened", f"human:{session_label}", task_id,
                    {"previousStage": "cancelled", "stage": stage,
                     "reopenedBy": session_label, "priorCancellation": prior, "note": None})

    def restrict(self, task_id: str) -> None:
        """The task is no longer visible to the operator's key (``read-actions.ts``:
        a removed or restricted task reads as ``not_found``)."""
        self.hidden.add(task_id)

    def _stamp(self) -> str:
        return self.clock().isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None,
             timeout: float, max_bytes: int) -> tuple[int, dict[str, str], bytes]:
        """The injected sender: answers JSON-RPC ``tools/call`` in process."""
        answer = self._answer(method, url, headers, body)
        if self.lose_next_answer:
            self.lose_next_answer = False
            raise TimeoutError("isolated MC: answer lost after the call was served")
        return answer

    def _answer(self, method: str, url: str, headers: Mapping[str, str],
                body: bytes | None) -> tuple[int, dict[str, str], bytes]:
        if self.down:
            raise ConnectionError("isolated MC is down")
        if method != "POST" or url != MC_URL + "/api/cursor/mcp" or body is None:
            raise ConnectionError("isolated MC answers only POST /api/cursor/mcp")
        request = json.loads(body)
        name = request["params"]["name"]
        arguments = request["params"].get("arguments") or {}
        self.tool_calls.append(name)
        operator = {k.lower(): v for k, v in headers.items()}.get("x-mc-operator-email", "")
        if name == "mc_self_check":
            payload: dict[str, Any] = dict(self.health)
        elif name == "mc_get_task":
            task = (None if arguments.get("id") in self.hidden
                    else self.tasks.get(arguments.get("id")))
            # read-actions.ts GET_TASK_EVENT_LIMIT: the task's newest 25 events only
            events = [] if task is None else [e for e in self.events
                                              if e["taskId"] == task["id"]][-25:]
            payload = ({"error": {"code": "not_found", "message": "unknown task"}}
                       if task is None else
                       {"data": {"taskId": task["id"], "task": json.loads(json.dumps(task)),
                                 "events": json.loads(json.dumps(events)), "checkouts": []},
                        "meta": {}})
        elif name == "mc_request_approval":
            payload = self._request_approval(arguments, operator)
        elif name == "mc_create_task":
            payload = self._create_task(arguments, operator)
        elif name == "mc_search_tasks":
            query = str(arguments.get("q") or "").lower()
            found = [task for task_id, task in sorted(self.tasks.items())
                     if task_id not in self.hidden and query and (
                         query in task_id.lower() or query in task["title"].lower()
                         or query in str(task.get("description") or "").lower())]
            payload = {"data": {"tasks": [{"id": t["id"], "title": t["title"],
                                           "stage": t["stage"], "bucket": t.get("bucket")}
                                          for t in found[:int(arguments.get("limit") or 50)]],
                                "total": len(found)}, "meta": {}}
        else:
            payload = {"error": {"code": "forbidden", "message": "tool not served"}}
        result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
        if "error" in payload:
            result["isError"] = True
        answer = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        return 200, {"content-type": "application/json"}, json.dumps(answer).encode()

    def _request_approval(self, arguments: Mapping[str, Any], operator: str) -> dict[str, Any]:
        task = (None if arguments.get("taskId") in self.hidden
                else self.tasks.get(arguments.get("taskId")))
        if task is None:
            return {"error": {"code": "not_found", "message": "unknown task"}}
        # requestedByPrincipal: the key's principal (approvals.ts @ 77a5af6).
        gate = {"id": "apg_" + secrets.token_hex(8), "reason": arguments["reason"],
                "requestedBy": operator, "requestedByPrincipal": AGENT_SERVICE,
                "requestedRuntime": "plx-workbench", "requestedAt": self._stamp(),
                "status": "pending"}
        task["approvalGates"].append(gate)
        self._audit(task, "approval.requested", operator,
                    f"raised an approval gate — {gate['reason']}",
                    {"gateId": gate["id"], "checkoutRef": None, "hasProposal": False,
                     "reason": gate["reason"], "runtime": "plx-workbench"})
        return {"data": {"taskId": task["id"], "checkoutRef": None, "gateId": gate["id"],
                         "status": "pending", "inputRequired": True}}

    def _create_task(self, arguments: Mapping[str, Any], operator: str) -> dict[str, Any]:
        """``actionCreateTask`` (PLX_MC ``src/lib/mcp/actions.ts:231-307``,
        ``src/lib/sync/state.ts:483-540``, ``src/lib/mcp/task-create-idempotency.ts``
        @ 073f192): the bucket must exist (422 ``invalid_bucket``); the reporter is the
        operator header; title and description are trimmed; a repeat of the key with the
        same payload (hashed without reporter and key) returns the original task
        (``replayed: true``), another payload is 409 ``idempotency_key_reused``."""
        key = arguments.get("idempotencyKey")
        fields = {name: value for name, value in arguments.items()
                  if name not in ("reporter", "idempotencyKey") and value is not None}
        digest = json.dumps(fields, sort_keys=True)
        if key in self.create_claims:
            claimed, task_id = self.create_claims[key]
            if claimed != digest:
                return {"error": {"code": "idempotency_key_reused",
                                  "message": "same key, another payload"}}
            return {"task": json.loads(json.dumps(self.tasks[task_id])), "taskId": task_id,
                    "replayed": True}
        if arguments.get("bucket") not in self.buckets:
            return {"error": {"code": "invalid_bucket", "message": "bucket does not exist"}}
        task_id = f"TASK-{9000 + len(self.tasks) + 1}"
        self.tasks[task_id] = {
            "id": task_id, "title": str(arguments["title"]).strip(),
            "description": str(arguments.get("description") or "").strip(),
            "bucket": arguments["bucket"], "stage": "backlog",
            "priority": arguments.get("priority") or "medium", "reporter": operator,
            "accountableOwner": MC_ADMIN, "approvalGates": [],
            "activity": [{"who": operator, "kind": "move", "what": "created the task"}]}
        if key is not None:
            self.create_claims[key] = (digest, task_id)
        self._event("task.create.idempotency.result", operator, task_id,
                    {"idempotencyKey": key})
        return {"task": json.loads(json.dumps(self.tasks[task_id])), "taskId": task_id}

    def decide(self, task_id: str, gate_id: str, decision: str, *, session_label: str,
               session_kind: str) -> dict[str, Any]:
        """``POST /api/approvals/decide`` for one signed-in session."""
        try:
            if session_kind != "human":
                raise IsolatedMcRefused("context_denied")       # predicates.ts: service
            if self.users.get(session_label.lower()) not in ("admin", "owner"):
                raise IsolatedMcRefused("forbidden")            # grants.ts: approval.decide
            task = self.tasks.get(task_id)
            gate = next((g for g in (task or {}).get("approvalGates", ())
                         if g["id"] == gate_id), None)
            if gate is None:
                raise IsolatedMcRefused("not_found")
            if gate["status"] != "pending":
                raise IsolatedMcRefused("gate_already_decided")
            if gate["requestedBy"].strip().lower() == session_label.strip().lower():
                raise IsolatedMcRefused("separation_of_duties")
        except IsolatedMcRefused as refused:
            self.refusals.append(refused.code)
            raise
        decided_at = self._stamp()
        gate.update(status=decision, decidedBy=session_label, decidedAt=decided_at)
        if decision == "approved":
            # TASK-3408 @ 2d2cac7 (``approvals.ts`` ``decideApprovalGate``): an approval is
            # stamped with its expiry (``approvalExpiry``: unscoped = decidedAt + 72h), then
            # signed; with no ``PLX_MC_APPROVAL_SIGNING_KEY`` (this stand-in holds none) the
            # signature is null.
            expires = (datetime.fromisoformat(decided_at.replace("Z", "+00:00"))
                       + APPROVAL_TTL_UNSCOPED)
            gate.update(expiresAt=expires.isoformat(timespec="milliseconds")
                        .replace("+00:00", "Z"), signature=None)
        self._audit(task, "approval.decided", session_label,
                    f"{decision} the approval gate — {gate['reason']}",
                    {"gateId": gate["id"], "decision": decision, "reason": gate["reason"],
                     "requestedBy": gate["requestedBy"], "note": None})
        return dict(gate)

    def client(self, ledger_path: Path, *,
               write_tools: frozenset[str] = frozenset({"mc_request_approval"})
               ) -> MissionControlMcpClient:
        """The real, opted-in MC client over this in-process MC (``write_tools``)."""
        hub = HubTransport(self.send, environ={}, clock=self.clock, sleep=lambda _s: None,
                           jitter=lambda: 1.0)
        return MissionControlMcpClient(
            McConfig(MC_URL, Secret("isolated-fixture-no-credential"), OPERATOR), hub=hub,
            write_tools=write_tools, write_approvers={CALL_APPROVER},
            write_ledger=WriteAuthorizationLedger(ledger_path))


@dataclass
class Lifecycle:
    """One isolated world under ``state_dir``: owner, cases, action path, MC."""

    state_dir: Path
    clock: Clock = field(default_factory=Clock)

    def __post_init__(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.owner = IsolatedOwnerSystem(self.state_dir / "owner.db",
                                         readers=frozenset({"ops"}))
        self.cases = CaseStore(self.state_dir / "cases.db", citation_resolver=self.owner.resolve)
        self.read_roster()
        self.path = self.new_path()
        self.mc = IsolatedMissionControl(self.clock, {MC_ADMIN: "admin",
                                                      MC_MEMBER: "member"})
        self.mc_client = self.mc.client(self.state_dir / "mc-write-ledger.jsonl")
        self.registry = ap.DispatcherRegistry()
        self.registry.register(ap.FIXTURE_CAPABILITY, ap.FixtureDispatcher(self.owner))
        self.registry.register(CITED_VALUE_CAPABILITY, ap.FixtureDispatcher(self.owner))
        self.read_back = read_back_for(self.owner)
        self._records = 0
        self.case_ids: list[str] = []

    def read_roster(self, path: Path = ISOLATED_APPROVER_ROSTER) -> None:
        """Read the approver roster a person wrote (``load_approver_roster``); the next
        ``new_path`` takes its call approvers and MC roster from it."""
        self.approvers = load_approver_roster(path)
        self.roster = self.approvers.mc_roster

    def new_path(self) -> ap.ActionPath:
        """A fresh ActionPath over the same files (what a restarted process sees)."""
        return ap.ActionPath(self.state_dir / "actions.db", self.cases,
                             **self.approvers.path_options(),
                             mc_operator_email=OPERATOR,
                             clock=self.clock, mc_follow_up_bucket=FOLLOW_UP_BUCKET)

    # ---- steps, each through the existing contract ----------------------

    def open_case_and_register(self, number: int) -> tuple[str, str]:
        """Employee side: seed the owner record, open a case as the human console session
        (the question is human-authored, ``objective_author_kind="human"``), propose
        and register."""
        key = {"id": number}
        target = self.owner.seed(RELATION, key, {"id": number, "po_reference": None})
        # The employee's request as a human console session records it (isolated
        # fixture): only a human-written question may supply the value written.
        # Opened and proposed through the action path, so the session's subject is a
        # recorded opener and author (they may never approve it).
        case_id = self.path.open_case(ACTOR, f"Set the customer PO on isolated order {number} "
                                             f"to PO-ISO-{number}?",
                                      roots=[f"isolated-order-{number}"],
                                      objective_author_kind="human", session=SESSION)
        proposal = self.path.record_proposal(
            ACTOR, case_id, ap.FIXTURE_CAPABILITY,
            {"field": "po_reference", "value": f"PO-ISO-{number}"},
            [{"check": "field_empty", "citation": target, "field": "po_reference"},
             "a person confirms the PO belongs to this order"],
            citations=[target], session=SESSION,
            expected_effect={"relation": RELATION, "key": key, "fields": ["po_reference"],
                             "equals": f"PO-ISO-{number}"})
        intent = self.path.register(ACTOR, case_id, proposal["proposal_id"], session=SESSION)
        self.case_ids.append(case_id)
        return case_id, intent["intent_id"]

    def authorize(self, intent_id: str, task_id: str) -> dict[str, int]:
        """Host + humans: MC read, per-call approval, gate request, MC decision."""
        self.mc.add_task(task_id, "Isolated goal-2 lifecycle")
        self.path.prepare_gate(ACTOR, intent_id, task_id, self.mc_client, session=SESSION)
        reason = self.path.get(ACTOR, intent_id)["intent"]["gate_reason"]
        self._records += 1
        now = self.clock.now
        self.path.approve_call(ACTOR, intent_id, task_id, {
            "schema": ap.CALL_APPROVAL_SCHEMA, "tool": "mc_request_approval",
            "arguments": {"taskId": task_id, "reason": reason},
            "approved_by": CALL_APPROVER,
            "approved_at": (now - timedelta(minutes=1)).isoformat(timespec="seconds"),
            "expires_at": (now + timedelta(hours=1)).isoformat(timespec="seconds"),
            "evidence_ref": f"isolated fixture per-call approval #{self._records}"},
            session=SESSION)
        self.path.request_gate(ACTOR, intent_id, self.mc_client, session=SESSION)
        gate = self.path.get(ACTOR, intent_id)["gate"]
        refused = 0
        # Agents are never principals: an agent's service session and an MC member
        # are both refused by MC itself before anything is decided.
        for label, kind in ((AGENT_SERVICE, "service"), (MC_MEMBER, "human")):
            try:
                self.mc.decide(task_id, gate["gate_id"], "approved", session_label=label,
                               session_kind=kind)
            except IsolatedMcRefused:
                refused += 1
        # Still pending in MC: nothing authorizes yet.
        self.path.record_decision(ACTOR, intent_id, self.mc_client, session=SESSION)
        self.mc.decide(task_id, gate["gate_id"], "approved", session_label=MC_ADMIN,
                       session_kind="human")
        self.path.record_decision(ACTOR, intent_id, self.mc_client, session=SESSION)
        return {"agent_or_member_decisions_refused_by_mc": refused}


    # ---- the MC follow-up task: gap -> proposal -> exact call -> approval ------

    def follow_up_intent(self, number: int) -> tuple[str, str]:
        """Employee side, as the console records it: open a case, record the gap (the
        catalog's ``workbench.follow_up.record``), propose the MC follow-up task with every
        parameter traced to that gap, register it and prepare the exact call. Returns
        ``(case_id, intent_id)``; the intent waits at ``call_approval_required``."""
        missing = f"The supplier's confirmed ship date for isolated order {number}."
        case_id = self.path.open_case(ACTOR, missing, objective_author_kind="human",
                                      session=SESSION)
        gap = self.path.execute_internal(ACTOR, "workbench.follow_up.record", case_id, {
            "missing": missing, "why": "Production planning waits on the receipt date.",
            "who_can_answer": answerers.resolve([("SupplierConfirmation", "", "")],
                                                ["supplier"]), "state": "unknown"},
            session=SESSION)
        proposal = self.path.record_proposal(ACTOR, case_id, FOLLOW_UP_CAPABILITY, {
            "gap_id": gap["receipt"]["gap_id"], "title": missing,
            "description": "Production planning waits on the receipt date.",
            "priority": "medium"}, session=SESSION)
        intent = self.path.register(ACTOR, case_id, proposal["proposal_id"], session=SESSION)
        self.path.prepare_call(ACTOR, intent["intent_id"], session=SESSION)
        self.case_ids.append(case_id)
        return case_id, intent["intent_id"]

    def approve_follow_up(self, intent_id: str) -> None:
        """The person (the configured call approver, never the employee) approves the
        exact prepared call: one per-call approval record."""
        prepared = self.path.get(ACTOR, intent_id)["prepared"]
        self._records += 1
        now = self.clock.now
        self.path.approve_call(ACTOR, intent_id, None, {
            "schema": ap.CALL_APPROVAL_SCHEMA, "tool": prepared["tool"],
            "arguments": prepared["arguments"], "approved_by": CALL_APPROVER,
            "approved_at": (now - timedelta(minutes=1)).isoformat(timespec="seconds"),
            "expires_at": (now + timedelta(hours=1)).isoformat(timespec="seconds"),
            "evidence_ref": f"isolated fixture per-call approval #{self._records}"},
            session=SESSION)

    # ---- the typed owner: finding -> catalog proposal -> intent -------------

    def define_typed_relations(self) -> None:
        self.owner.define(TYPED_RELATION, TYPED_FIELDS, version_field=VERSION_FIELD)
        self.owner.define(SOURCE_RELATION, SOURCE_FIELDS, version_field=VERSION_FIELD)

    def open_case_from_finding(self, number: int, fields: tuple[str, ...],
                               evidence_field: str,
                               prior: str | None = None) -> tuple[str, str, str]:
        """A case finding cites an inbound record and the order record it is about;
        the proposal sets ``fields`` on the order to the value of ``evidence_field``
        in the inbound record, every parameter traced to that cited evidence, with
        the order's fields (empty, or ``prior`` for a correction: ``field_equals``)
        and its version (etag) as compare-and-set preconditions. Returns
        ``(case_id, intent_id, target handle)``."""
        key = {"id": number}
        row: dict[str, Any] = {"id": number, "po_reference": None,
                               "external_document_number": None, "line_count": None}
        row.update({name: prior for name in fields if prior is not None})
        target = self.owner.seed(TYPED_RELATION, key, row)
        source = self.owner.seed(SOURCE_RELATION, key, {
            "id": number, "customer_po": f"PO-DOC-{number}", "lines_stated": str(number)})
        case_id = self.path.open_case(
            ACTOR, f"Does isolated order {number} carry the customer PO its inbound "
                   f"document states?", roots=[f"isolated-order-{number}"], session=SESSION)
        self.cases.finding(ACTOR, case_id, "The cited inbound document states a value the "
                           "cited order record does not carry yet.", "fact", "supported",
                           citations=[source, target])
        # Read through the citation, as the agent would: the value is the cited field.
        value = self.owner.resolve(source, ACTOR)["row"][evidence_field]
        version = self.owner.resolve(target, ACTOR)["row"][VERSION_FIELD]
        conditions: list[Any] = [
            {"check": "field_empty", "citation": target, "field": name} if prior is None
            else {"check": "field_equals", "citation": target, "field": name, "value": prior}
            for name in fields]
        conditions.append({"check": "field_equals", "citation": target,
                           "field": VERSION_FIELD, "value": version})
        conditions.append("a person confirms the inbound document belongs to this order")
        proposal = self.path.record_proposal(
            ACTOR, case_id, CITED_VALUE_CAPABILITY,
            {"field": fields[0], "value": value, "evidence_citation": source,
             "evidence_field": evidence_field},
            conditions, citations=[source, target], session=SESSION,
            # One value per field: a several-field correction writes the cited value
            # to each field, and read-back verifies every one.
            expected_effect={"relation": TYPED_RELATION, "key": key,
                             "values": {name: value for name in fields}})
        intent = self.path.register(ACTOR, case_id, proposal["proposal_id"], session=SESSION)
        self.case_ids.append(case_id)
        return case_id, intent["intent_id"], target

    def flag_on_case(self, case_id: str, intent_id: str, target: str) -> dict[str, Any]:
        """Flag an unresolved action outcome on the case: a follow-up recorded through
        the catalog's workbench-internal ``workbench.follow_up.record`` (one case-store
        write, read back). Nothing is re-sent."""
        view = self.path.view(ACTOR, intent_id)
        step = view["next_safe_step"]
        reasons = [str(item.get("reason")) for item in view["recovery"] if item.get("reason")]
        return self.path.execute_internal(ACTOR, "workbench.follow_up.record", case_id, {
            "missing": f"The confirmed effect of action {intent_id} on the owning record "
                       f"(state {view['state']}, outcome {view['outcome']}).",
            "why": f"{'; '.join(reasons) or 'no reason recorded'}. Next: {step['step']} "
                   f"by {step['by']}: {step['detail']}"[:1900],
            "who_can_answer": answerers.for_citation(target), "state": "unknown",
            "citations": [target]}, session=SESSION)


def case_audit_trail(world: Lifecycle, case_id: str,
                     task_ids: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The visible audit trail of one case, from permission-aware reads only: the
    case timeline and follow-ups, each registered intent's durable, hash-chained
    history (registration, gate, decisions, dispatches, read-backs, F7 recovery)
    and, for an intent whose MC task is given, the task's MC audit events read
    through the operator's MC client (``mc_get_task``: a read)."""
    case = world.cases.get(ACTOR, case_id)
    listed = world.new_path().list_for_case(ACTOR, case_id)
    intents = []
    for item in listed["intents"]:
        entry = {"proposal_id": item["proposal_id"], "state": item["state"],
                 "outcome": item.get("outcome"), "chain_ok": item.get("chain_ok"),
                 "history": [h["kind"] for h in item.get("history") or ()],
                 "outcome_trail": [o["outcome"] for o in item.get("outcome_trail") or ()],
                 "decisions": [d.get("reason_code") for d in item.get("decisions") or ()],
                 "dispatches": [{"outcome": d.get("outcome"), "reason": d.get("reason")}
                                for d in item.get("dispatches") or ()],
                 "read_backs": [{"outcome": r.get("outcome"), "reason": r.get("reason")}
                                for r in item.get("read_backs") or ()],
                 "recovery": [{"trigger": r.get("trigger"), "reason": r.get("reason"),
                               "resend_allowed": r.get("resend_allowed")}
                              for r in item.get("recovery") or ()],
                 "next_safe_step": (item.get("next_safe_step") or {}).get("step")}
        task_id = (task_ids or {}).get(item.get("intent_id") or "")
        if task_id:
            try:
                events = world.mc_client.get_task(task_id)["data"]["events"]
                entry["mc_events"] = [e.get("kind") for e in events]
            except Exception as exc:  # noqa: BLE001 - shown, never hidden
                code = getattr(exc, "code", None)
                entry["mc_events"] = ("mc_task_not_found" if code == "not_found" else
                                      f"mc_unreadable:{code or type(exc).__name__}")
        intents.append(entry)
    return {"schema": AUDIT_SCHEMA, "case_id": case_id, "case_status": case["status"],
            "timeline": [event["kind"] for event in case["timeline"]],
            "follow_ups": len([gap for gap in _gaps(case)]),
            "intents": intents, "evidence_label": EVIDENCE_LABEL}


def _gaps(case: Mapping[str, Any]) -> list[Any]:
    found = case.get("gaps") or []
    return ([item for bucket in found.values() for item in bucket]
            if isinstance(found, Mapping) else list(found))


def run_owner_scenario(world: Lifecycle, name: str, number: int) -> dict[str, Any]:
    """One ``OWNER_SCENARIOS`` run, end to end; returns counts and states only."""
    if name not in OWNER_SCENARIOS:
        raise ValueError("unknown_scenario")
    fields, evidence_field, faults, before, prior = OWNER_SCENARIOS[name]
    path, owner = world.path, world.owner
    requests_before = world.mc.tool_calls.count("mc_request_approval")
    try:
        case_id, iid, target = world.open_case_from_finding(number, fields, evidence_field,
                                                            prior)
    except ap.ActionPathError as exc:
        # Refused at register (e.g. a value that does not fit the type the cited
        # target declares): no intent, no MC gate, no approval used, nothing sent.
        return {"scenario": name, "state": "not_registered", "outcome": "not_dispatched",
                "outcome_trail": [], "register_refusal": str(exc), "chain_ok": True,
                "mc_requests": (world.mc.tool_calls.count("mc_request_approval")
                                - requests_before),
                "owner_dispatch_calls": 0, "replay_owner_calls": 0,
                "owner_replay_same_receipt": None, "flagged_on_case": False}
    task_id = f"TASK-ISO-OWN-{number}"
    extra = world.authorize(iid, task_id)
    if before == "close_task":   # the task is done in MC: the approval no longer stands
        world.mc.set_stage(task_id, "verified", session_label=MC_ADMIN)
    elif before == "restrict_task":
        world.mc.restrict(task_id)
    calls_before = owner.dispatch_calls
    owner.faults.extend(faults)
    first = path.dispatch(ACTOR, iid, world.registry, world.mc_client, session=SESSION)
    if path.get(ACTOR, iid)["state"] in ("dispatched", "dispatched_unknown"):
        path.read_back(ACTOR, iid, world.read_back, session=SESSION)
    if "timeout_pending" in faults:
        # Still pending at the owner: the read-back above saw the old value and said
        # "read back again later" (no flag); the owner finishes, the next one verifies.
        owner.process_pending(path.get(ACTOR, iid)["intent"]["idempotency_key"], apply=True)
        path.read_back(ACTOR, iid, world.read_back, session=SESSION)
    # A duplicate dispatch: the path replays the recorded outcome and sends nothing.
    calls_before_replay = owner.dispatch_calls
    if path.get(ACTOR, iid)["state"] != "gate_not_authorizing":
        path.dispatch(ACTOR, iid, world.registry, world.mc_client, session=SESSION)
    replay_calls = owner.dispatch_calls - calls_before_replay
    owner_replay = None
    if name == "verified":
        # A duplicate at the owner itself: same key and payload, same receipt, no write.
        intent = path.get(ACTOR, iid)["intent"]
        receipt = path.get(ACTOR, iid)["dispatches"][-1]["receipt"]
        owner_replay = owner.dispatch(dispatch_payload(ap.ActionIntent(**intent)),
                                      intent["idempotency_key"]) == receipt
    view = world.new_path().view(ACTOR, iid)
    flagged = view["state"] != "verified"
    if flagged:
        world.flag_on_case(case_id, iid, target)
    summary = _summary(world.new_path(), iid)
    audit = case_audit_trail(world, case_id, {iid: task_id})
    return {"scenario": name, **summary, **extra,
            "first_dispatch_state": first["state"],
            "first_dispatch_reason": first.get("reason"),
            "replay_owner_calls": replay_calls,
            "owner_replay_same_receipt": owner_replay,
            "owner_dispatch_calls": owner.dispatch_calls - calls_before,
            "read_back_reasons": [item["reason"] for item in view["read_backs"]],
            "decision_codes": [item["reason_code"] for item in view["decisions"]],
            "flagged_on_case": flagged, "case_follow_ups": audit["follow_ups"],
            "case_timeline": audit["timeline"],
            "mc_events": audit["intents"][0].get("mc_events")}


def run_follow_up_scenario(world: Lifecycle, mc_client: MissionControlMcpClient, name: str,
                           number: int) -> dict[str, Any]:
    """One ``FOLLOW_UP_SCENARIOS`` run: counts and states only."""
    if name not in FOLLOW_UP_SCENARIOS:
        raise ValueError("unknown_scenario")
    path = world.path
    _case_id, iid = world.follow_up_intent(number)
    waiting = path.view(ACTOR, iid)["state"]
    creates_before = world.mc.tool_calls.count("mc_create_task")
    world.approve_follow_up(iid)
    world.mc.lose_next_answer = name == "lost_answer"
    sent = path.send_call(ACTOR, iid, mc_client, session=SESSION)
    path.read_back_call(ACTOR, iid, mc_client, session=SESSION)
    # A replay sends nothing; MC itself returns the original task for the same key.
    replay = path.send_call(ACTOR, iid, mc_client, session=SESSION)
    view = world.new_path().view(ACTOR, iid)
    return {"scenario": name, **_summary(world.new_path(), iid),
            "state_before_approval": waiting, "first_send_state": sent["state"],
            "replay_state": replay["state"],
            "mc_create_calls": world.mc.tool_calls.count("mc_create_task") - creates_before,
            "read_back_reasons": [item["reason"] for item in view["read_backs"]],
            "tasks_created": len(world.mc.create_claims)}


def run_follow_up(state_dir: Path) -> dict[str, Any]:
    state_dir = Path(state_dir)
    if state_dir.exists() and any(state_dir.iterdir()):
        raise ValueError("state_dir_not_empty: the run needs a fresh directory")
    world = Lifecycle(state_dir)
    world.mc.buckets.add(FOLLOW_UP_BUCKET)
    client = world.mc.client(state_dir / "mc-create-ledger.jsonl",
                             write_tools=frozenset({"mc_create_task"}))
    results = [run_follow_up_scenario(world, client, name, number)
               for number, name in enumerate(FOLLOW_UP_SCENARIOS, start=1)]
    return {"schema": FOLLOW_UP_SCHEMA, "evidence_label": EVIDENCE_LABEL,
            "scenarios": results, "outcomes": _count(item["outcome"] for item in results),
            "mc_tool_calls": _count(world.mc.tool_calls), "business_system_writes": 0,
            "network_calls": 0}


def run_owner_lifecycle(state_dir: Path) -> dict[str, Any]:
    state_dir = Path(state_dir)
    if state_dir.exists() and any(state_dir.iterdir()):
        raise ValueError("state_dir_not_empty: the run needs a fresh directory")
    world = Lifecycle(state_dir)
    world.define_typed_relations()
    results = [run_owner_scenario(world, name, number)
               for number, name in enumerate(OWNER_SCENARIOS, start=1)]
    return {"schema": OWNER_TRAIL_SCHEMA, "evidence_label": EVIDENCE_LABEL,
            "scenarios": results, "outcomes": _count(item["outcome"] for item in results),
            "owner_operations": _count(item["status"] for item in world.owner.operations()),
            "mc_events": _count(event["kind"] for event in world.mc.events),
            "business_system_writes": 0, "network_calls": 0}


def _summary(path: ap.ActionPath, intent_id: str) -> dict[str, Any]:
    view = path.view(ACTOR, intent_id)
    return {"state": view["state"], "outcome": view["outcome"],
            "outcome_trail": [item["outcome"] for item in view["outcome_trail"]],
            "state_trail": [item["state"] for item in view["state_trail"]],
            "events": len(view["history"]),
            "dispatch_attempts": len(view["dispatches"]),
            "read_backs": len(view["read_backs"]),
            "recovery_triggers": [item["trigger"] for item in view["recovery"]],
            "next_safe_step": view["next_safe_step"]["step"],
            "blocked_by": view["next_safe_step"]["blocked_by"],
            "resend_allowed": view["next_safe_step"]["resend_allowed"],
            "chain_ok": view["chain_ok"]}


def run_scenario(world: Lifecycle, name: str, number: int) -> dict[str, Any]:
    """Run one scenario; returns counts and states only (no ids or values)."""
    if name not in SCENARIOS:
        raise ValueError("unknown_scenario")
    path, owner = world.path, world.owner
    _case_id, iid = world.open_case_and_register(number)
    extra = world.authorize(iid, f"TASK-ISO-{number}")
    calls_before = owner.dispatch_calls
    if name == "stale_approval":
        owner.update_by_other_writer(RELATION, {"id": number}, {"ship_note": "changed"})
        path.dispatch(ACTOR, iid, world.registry, world.mc_client, session=SESSION)
    elif name == "happy":
        path.dispatch(ACTOR, iid, world.registry, world.mc_client, session=SESSION)
        path.read_back(ACTOR, iid, world.read_back, session=SESSION)
        # A replay: sends nothing.
        path.dispatch(ACTOR, iid, world.registry, world.mc_client, session=SESSION)
    elif name == "uncertain_then_verified":
        owner.faults.append("timeout_after_apply")
        path.dispatch(ACTOR, iid, world.registry, world.mc_client, session=SESSION)
        # reconcile_required: no resend.
        path.dispatch(ACTOR, iid, world.registry, world.mc_client, session=SESSION)
        path.read_back(ACTOR, iid, world.read_back, session=SESSION)
    elif name == "uncertain_then_failed":
        owner.faults.append("timeout_pending")
        path.dispatch(ACTOR, iid, world.registry, world.mc_client, session=SESSION)
        path.read_back(ACTOR, iid, world.read_back, session=SESSION)  # pending: still unknown
        owner.process_pending(path.get(ACTOR, iid)["intent"]["idempotency_key"], apply=False)
        path.read_back(ACTOR, iid, world.read_back, session=SESSION)
    elif name == "owner_rejects":
        owner.faults.append("conflict")
        path.dispatch(ACTOR, iid, world.registry, world.mc_client, session=SESSION)
        # A replay: sends nothing.
        path.dispatch(ACTOR, iid, world.registry, world.mc_client, session=SESSION)
    summary = _summary(world.new_path(), iid)       # read back from disk by a new instance
    return {"scenario": name, **summary, **extra,
            "owner_dispatch_calls": owner.dispatch_calls - calls_before}


def run_demo(state_dir: Path) -> dict[str, Any]:
    state_dir = Path(state_dir)
    if state_dir.exists() and any(state_dir.iterdir()):
        raise ValueError("state_dir_not_empty: the demo needs a fresh directory")
    world = Lifecycle(state_dir)
    results = [run_scenario(world, name, number)
               for number, name in enumerate(SCENARIOS, start=1)]
    listed = world.new_path()
    tools = world.mc.tool_calls
    return {"schema": TRAIL_SCHEMA, "evidence_label": EVIDENCE_LABEL,
            "scenarios": results, "outcomes": _count(item["outcome"] for item in results),
            "mc_tool_calls": {name: tools.count(name) for name in sorted(set(tools))},
            "mc_refusals": {code: world.mc.refusals.count(code)
                            for code in sorted(set(world.mc.refusals))},
            "owner_operations": _count(item["status"] for item in world.owner.operations()),
            "intents_listed": sum(len(listed.list_for_case(ACTOR, case_id)["intents"])
                                  for case_id in world.case_ids),
            "business_system_writes": 0, "network_calls": 0,
            "real_capability_requirements": [item["id"] for item in
                                             REAL_CAPABILITY_REQUIREMENTS]}


def _count(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.goal2_exec",
                                     description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="run the isolated approval -> dispatch -> read-back "
                                       "lifecycle and print its trail (counts/states only)")
    demo.add_argument("--state-dir", type=Path, required=True,
                      help="a new or empty directory for the isolated stores")
    owner = sub.add_parser("owner-lifecycle",
                           help="run the full lifecycle against the typed isolated owner "
                                "(finding -> catalog proposal -> MC gate -> dispatch -> "
                                "read-back -> recovery -> case audit; counts/states only)")
    owner.add_argument("--state-dir", type=Path, required=True,
                       help="a new or empty directory for the isolated stores")
    follow = sub.add_parser("follow-up",
                            help="run the MC follow-up task (mc.task.create_follow_up) against "
                                 "the isolated MC: gap -> exact call -> per-call approval -> "
                                 "one send -> mc_get_task read-back (counts/states only)")
    follow.add_argument("--state-dir", type=Path, required=True,
                        help="a new or empty directory for the isolated stores")
    args = parser.parse_args(argv)
    runs = {"demo": run_demo, "owner-lifecycle": run_owner_lifecycle,
            "follow-up": run_follow_up}
    try:
        report = runs[args.command](args.state_dir)
    except ValueError as exc:
        parser.error(str(exc))
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
