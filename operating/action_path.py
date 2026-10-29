"""Goal-2 action path: case proposal -> human approvals -> dispatch -> read-back.

Design: ``devloop/GOAL2-ACTIONS.md``. This module is the state machine and its
records; it enables **no** production write. Every step is one append-only,
hash-chained event (own SQLite table, same pattern as ``operating.cases``), and
every call re-reads the case for the calling actor, so an actor who cannot see
the case cannot see or move its actions.

Lifecycle (state after each step)::

    register(case proposal)            proposed             (structured preconditions hold
                                                             on the cited evidence)
    prepare_gate(task id, MC reader)   gate_prepared        (MC read first: live, task open,
                                                             no untracked gate for this action;
                                                             then the MC approval request record)
    approve_call(per-call record)      call_approved        (a human names the exact MC call)
    request_gate(requester)            gate_requested | gate_request_refused | gate_request_unknown
    reconcile_gate(MC reader)          any state; from gate_request_unknown: gate_requested (one
                                       gate with our reason) | gate_request_lost (none, after
                                       the send settled: a human may approve a new request)
    record_decision(MC reader)         gate_authorized | gate_rejected | gate_not_authorizing
    dispatch(registry)                 dispatched | dispatch_refused | dispatched_unknown
                                       | approval_expired (MC decision older than max age)
                                       | failed (the owner refused it for certain)
    read_back(read_back)               verified (effect observed) | failed (after an unknown
                                       dispatch the owner reports our key not applied and
                                       fenced) | unchanged

EXE-04 outcome of every state (``outcome_of``): not_dispatched | unknown | verified |
failed; the trail is rebuilt from the durable events (``outcome_trail``). The owning
system contract (``dispatch(payload, idempotency_key)`` / ``read_back(ref)``) is
``operating.goal2_exec_owner``.
    escalate(note)                     escalated (a human reconciles in the owning system)
    escalate_to_mc()                   escalated, plus one MC follow-up task proposed for the
                                       outcome (EXE-05; sent only after a per-call approval)

Safety properties this module holds by construction:

* The gate request is ``mc_request_approval``, an MC write. It is sent only
  through ``MissionControlMcpClient.call_write_tool`` (opted-in client, an
  ``McWriteAuthorization`` built from this intent's per-call human approval,
  the shared durable ``WriteAuthorizationLedger``, audit), once. A
  ``gate.request_started`` record is committed before the send and no SQLite
  lock is held across it: a crash or lost answer reads as
  ``gate_request_unknown``, left only by reading the task, never by resending.
* A per-call approval is single use (its digest is consumed, on any intent) and
  its validity window is re-checked immediately before the MC call.
* The MC decision is re-checked by ``MissionControlMcpClient.approval_decision``
  semantics: approved, requested by our operator, exact reason (SHA-256), decided
  by a human other than the operator/requester, after the request; and the
  decider must be an ``admin``/``owner`` on the supplied MC ``ApproverRoster``.
  Agents, service principals, members and the operator can never authorize.
* Separation of duties (decision g2-1; ``plx-intent-identity/v2``,
  devloop/DECISIONS/2026-10-04-goal2-identity.md): every intent records ``sub`` (the
  employee it is for, with their Entra ``oid``) and ``act`` (who acted: agent or
  human, channel, client host, session digest), taken only from the registering
  session (``session_identity.intent_identity``). Every later step takes the host's
  ``session=`` too and stores its ``act`` in the hashed event; a session of another
  actor is refused, and a step without a session is refused (``session_required``)
  before anything is read or written: no step records an unknown ``act`` (events
  written before v2 still fold, verify and read as ``act`` unknown). ``act`` is
  attribution only, never a principal: every read and ACL check uses ``actor``. The
  case's opener and the proposal's author are subjects too: ``open_case`` /
  ``record_proposal`` open and record through the case store and record the session's
  ``sub``/``act`` (``case_author``); a case or proposal recorded any other way has an
  unknown author. One check (``_sod_refusal``), inside the committing write of
  ``approve_call``, ``request_gate``, ``record_decision`` and ``dispatch``, refuses a
  subject as call approver or decider, matched by canonical email and by Entra oid (the
  call approver's from ``call_approvers`` or the ``ApproverRoster``, the decider's from
  the roster), and an intent with any unknown subject (``subject_unknown``); the intent
  stays parked. A roster approver, per call or as a decider, counts only for the action
  types the roster lists for them (``ApproverRoster.approves``). A call approver or
  decider without a known oid never counts
  (``call_approver_oid_unknown`` / ``decider_oid_unknown``; a host-resolved Graph oid past
  its age limit at that check: ``call_approver_oid_unverified`` / ``decider_oid_unverified``),
  and the approval or decision records the oid's provenance; re-checked at
  ``request_gate``, ``send_call`` and ``dispatch`` for an approval or decision recorded
  before the oid was required. One age rule (``_standing_oid_refusal``): there, an oid
  recorded from Graph (``graph_users``) stands only while the roster's copy, re-read when
  it is past its age limit, is fresh and names the same oid (else ``<role>_oid_unverified``
  / ``<role>_oid_changed``), so no Graph oid older than 24 h authorizes anything. An oid
  recorded with no provenance that is not the committed one was read from Graph too (a build
  before provenance was recorded): it stands 24 h from its record's own time
  (``<role>_oid_unverified:older_than_24h``).
  Whoever requests approval is a subject too (a co-requester): the session
  of ``prepare_gate``, ``request_gate`` and ``prepare_call`` is recorded as
  ``intent.co_requested`` in the same write (``_co_request``; a requester with no known
  identity is refused, ``subject_unknown``), so a person who asks for an approval can
  never approve the call or decide the gate, and dispatch refuses such a decision. A
  subject co-requesting an authorized intent before dispatch withdraws
  the decision
  (``approval_withdrawn:co_requested_after_decision``), and ``dispatch`` re-checks the
  standing decision's decider against the subjects on its own write: nothing is
  dispatched.
* ``production_target_refusal`` is a hard guard ahead of the dispatcher: an intent
  whose owner, capability and target relation are not all the isolated sandbox is
  refused, however it was approved, unless it is a real-system intent for which every
  gate holds (``target_gates``, ``operating.action_adapters.GATES``): a cataloged real
  capability (``executor.dispatch: gated_adapter``) for this owner and relation, its
  reviewed adapter registered, the MC decision read back from MC at dispatch, and the
  allow switch ``PLX_WORKBENCH_REAL_DISPATCH`` naming the capability (off by default,
  never set by the development loop). ``prepare_gate`` checks the first and the last
  before any MC read; the refusal names the unmet gates (``unmet_gates``).
* ``DispatcherRegistry`` accepts exactly three dispatcher types: ``FixtureDispatcher``
  (``fixture.*`` capabilities over ``fixture.*`` relations, in-memory),
  ``RealSystemDispatcher`` (one cataloged capability over its reviewed adapter and a
  reviewed transport: ``injected-transport``, or ``twin-transport`` in test mode only,
  ``twin.mode``) and ``RefusingDispatcher``.
  A refusal is decided before a dispatch intent is written, so it is certain nothing
  was sent.
* A dispatch intent is committed before the call. A call that raises, or a
  process that dies before the outcome is written, leaves ``dispatched_unknown``.
  It is never re-sent: only a read-back that observes the expected effect moves
  it on (``verified``, ``reconciled_by: read_back``); otherwise a human is asked.
  ``escalated`` holds against late answers: a dispatch answering after the intent
  was escalated is recorded on its attempt (``late_dispatch_answer``) and never
  moves the state, so nothing is re-sent.
  Every failed or uncertain outcome appends a ``recovery.recorded`` record (F7)
  naming the trigger and the one safe next step.
* Preconditions are re-checked at prepare_gate, before the gate request is sent
  and at dispatch: the case must be open, the proposal still readable and
  unchanged (payload digest), every citation must resolve to the content that
  was cited, and every structured precondition must still hold on it. ``register``
  refuses a closed case; ``next_safe_step`` names no step these would refuse.
* Every record after registration carries ``binding_sha256`` (case, proposal,
  proposal digest, cited evidence digests, idempotency key); a record bound to
  anything else breaks the chain check.

Mission Control as the owning system (``MC_CALL_CAPABILITIES``: the catalog's
``mc_write_door`` types, today ``mc.task.create_follow_up``; P6, 2026-10-04)::

    register(case proposal)            proposed             (parameters traced to a case gap
                                                             or a declared set; no row effect)
    prepare_call()                     call_approval_required (the exact MC call a person sees;
                                                             nothing read from or sent to MC)
    approve_call(per-call record)      call_approved        (a human names that exact call;
                                                             never a subject of the intent)
    send_call(MC client)               dispatched | dispatch_refused | dispatched_unknown | failed
                                       (sent once through the write door; the intent's
                                       idempotency key is MC's idempotencyKey)
    read_back_call(MC reader)          verified (mc_get_task holds every sent field) | unchanged

The per-call human approval is the approval of an MC write (pack 04 section 8 R1/R3: MC
writes are W-A). No MC gate is raised: MC's gate needs a task, and the write here is the
task. ``mc_call_refusal`` is this path's hard guard; ``production_target_refusal`` still
refuses every other real system.

Evidence label of everything executable here: ``isolated-fixture``; a real-system
adapter's dispatch and read-back: its transport's label (``injected-transport``, or
``twin-transport`` on the write twin in test mode, ``twin.world``).
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

from integrations.graph_directory import approver_oid_fresh
from integrations.mc import MissionControlMcpClient
from integrations.plx_hub_guard import APPROVAL_REQUEST_TOOL
from ontology.core import Actor, ValidationError

from . import action_adapters, answerers, capability_guards, goal2_preconditions, injection_guard
from .action_catalog import ActionCatalog, ActionRefused
from .goal2_exec_owner import (CONTRADICTING_READBACK_REASONS, IsolatedOwnerSystem,
                               OwnerRejected, PayloadUnmappable, dispatch_payload)
from .case_verification import field_status
from .cases import (WITHHELD, CaseNotFound, CaseStore, CitationError, actor_label,
                    effect_outcome, effect_values)
from .goal2_mc_approval import (DECIDING_ROLES, MC_TASK_DESCRIPTION_MAX, OID_COMMITTED,
                                ApproverRoster, McBucketAudience, audience_withholding,
                                authorization_for, authorization_still_current,
                                canonical_email, canonical_oid, cited_sources, ground_task,
                                mc_length, prepare_request, read_created_task,
                                send_create_task, send_request)
from .employee_permissions import EmployeeRightsUnresolved
from .session_identity import SessionIdentityRefused, intent_identity

_LOG = logging.getLogger(__name__)

SCHEMA = "plx-action-path/v1"
CALL_APPROVAL_SCHEMA = "plx-action-call-approval/v1"
EVIDENCE_LABEL = "isolated-fixture"

STATES = ("proposed", "call_approval_required", "gate_prepared", "call_approved", "approval_expired", "gate_requested",
          "gate_request_refused",
          "gate_request_unknown", "gate_request_lost", "gate_authorized", "gate_rejected",
          "gate_not_authorizing",
          "dispatch_refused", "dispatched", "dispatched_unknown", "verified", "escalated",
          "failed")
# EXE-04 outcome of an intent (``outcome_of``): nothing sent, sent but not (yet)
# verified, effect observed on independent read-back, or certainly not applied.
OUTCOMES = ("not_dispatched", "unknown", "verified", "failed")
_CALL_APPROVABLE = {"proposed", "gate_request_refused", "gate_rejected", "gate_not_authorizing",
                    "approval_expired", "gate_request_lost"}
DEFAULT_DECISION_MAX_AGE = timedelta(hours=24)
# A person's per-call approval taken at the console (``approve_in_session``): what was
# shown (``call_approval_subject``) and how long the approval stands for the one send.
CALL_APPROVAL_SUBJECT_SCHEMA = "plx-call-approval-subject/v1"
CONSOLE_APPROVAL_VALIDITY = timedelta(hours=1)
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
# How long an unanswered mc_request_approval may still be running somewhere. The
# client sends it once (``retry=False``) with a transport timeout of at most 60 s
# (``plx_hub_guard.HubTransport``), but MC's route (PLX_MC
# ``src/app/api/cursor/mcp/route.ts`` @ 1fba083) declares no ``maxDuration``, so the
# server-side function may outlive our timeout up to the hosting platform's cap.
# 15 min is chosen to exceed that cap (Vercel's paid maximum is reported as about
# 800 s; not verified here). Until it has passed since ``gate.request_started`` a
# task without our gate proves nothing: reconcile records no absence and no human
# next step. A gate landing after the intent's own gate authorized and dispatched
# is not recorded (nothing reads the task then); see GOAL2-ACTIONS.md limitations.
DEFAULT_GATE_SEND_SETTLE = timedelta(minutes=15)
_RECONCILABLE = {"gate_request_unknown", "gate_request_lost"}
_CLOCK_SKEW = timedelta(minutes=5)
_DISPATCHABLE = {"gate_authorized", "dispatch_refused"}
# A subject co-requested an authorized intent before its dispatch (``register``).
CO_REQUESTED_AFTER_DECISION = "approval_withdrawn:co_requested_after_decision"
_DISPATCH_STATE = {"dispatched": "dispatched", "not_dispatched": "dispatch_refused",
                   "rejected": "failed"}
# An MC call is (re-)prepared before its first approval and after a certain refusal (a
# new per-call approval of the same exact call, same idempotency key).
_CALL_PREPARABLE = {"proposed", "dispatch_refused"}
MC_CALL_SCHEMA = "plx-mc-call/v1"
_BOUND_KINDS = frozenset({"intent.co_requested", "gate.prepared", "call.prepared", "call.approved", "gate.request_started",
                          "gate.requested", "gate.request_lost",
                          "gate.request_refused", "gate.request_unknown", "gate.decision",
                          "decision.expired", "dispatch.refused", "dispatch.started",
                          "dispatch.finished", "readback.recorded", "recovery.recorded",
                          "recovery.escalated"})
_READABLE_BACK = {"dispatched", "dispatched_unknown", "escalated"}
# Events whose payload ``act`` is the registering session's (the intent's own ``act``).
_IDENTITY_KINDS = frozenset({"intent.registered", "intent.co_requested"})
MAX_LISTED_INTENTS = 200


@dataclass(frozen=True)
class Capability:
    """A real owning-system capability a proposal may name. None is enabled."""

    name: str
    owner_system: str
    owner_interface: str
    source_ref: str
    status: str = "not_authorized"


# The only capability a case tool proposes today (operating.customer_po_coverage).
REAL_CAPABILITIES: Mapping[str, Capability] = {
    "portal.sales_order.set_customer_po": Capability(
        "portal.sales_order.set_customer_po", "portal",
        "PATCH sales order header -> updateSalesOrderHeader (po_reference)",
        "plx-customer-portal service.ts:651 @ 6c9415973c19 (pack 03 section 3.3)"),
}
REAL_SYSTEMS = frozenset({"portal", "bc", "mc", "sharepoint", "mail"})
FIXTURE_CAPABILITY = "fixture.record.set_field"
# Every capability the hard guard admits: the isolated owner's test-host types of the
# action catalog (owner ``fixture``, ``target_relations: ["fixture.*"]``, ``hosts:
# ["test"]``, enforced by the catalog loader). The same ``hosts`` declaration keeps
# them off the production case host (operating.capability_guards.available), so the
# test-only flag and this set are one mechanism. The hard guard below still checks
# owner and relation in code. ``set_cited_value`` writes a value read from cited
# evidence.
FIXTURE_CAPABILITIES = ActionCatalog.builtin().test_host_isolated_owner_ids()
if FIXTURE_CAPABILITY not in FIXTURE_CAPABILITIES:
    raise RuntimeError("action_catalog_lacks_the_isolated_owner_capability")

# The MC-owned types (the catalog's ``mc_write_door`` types): sent only through the MC
# client's write door after a per-call human approval (``send_call``), never dispatched.
MC_CALL_CAPABILITIES = ActionCatalog.builtin().mc_call_ids()
MC_CALL_RELATION = "mc.task"
# EXE-05: an unresolved owner outcome becomes one MC follow-up task (``escalate_to_mc``).
ESCALATION_CAPABILITY = "mc.task.create_follow_up"
if ESCALATION_CAPABILITY not in MC_CALL_CAPABILITIES:
    raise RuntimeError("action_catalog_lacks_the_mc_follow_up_type")
_ESCALATED_TO_MC = frozenset({"dispatched_unknown", "escalated", "failed"})

_GENESIS = "0" * 64
_GATE_ID = re.compile(r"^apg_[A-Za-z0-9_-]{1,80}$")
_TASK_ID = re.compile(r"^TASK-[A-Za-z0-9-]{1,64}$")
_HUMAN_EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+$")
_SERVICE_LABEL = re.compile(r"^(?:sp_|agent:|svc[_:-]|service[_:-])", re.IGNORECASE)
_DDL = """
CREATE TABLE IF NOT EXISTS action_event (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  intent_id TEXT NOT NULL,
  intent_seq INTEGER NOT NULL CHECK(intent_seq >= 1),
  kind TEXT NOT NULL,
  actor TEXT NOT NULL,
  at TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  event_hash TEXT NOT NULL UNIQUE,
  UNIQUE(intent_id, intent_seq)
);
CREATE TRIGGER IF NOT EXISTS action_event_no_update BEFORE UPDATE ON action_event
BEGIN SELECT RAISE(ABORT, 'action_event_is_append_only'); END;
CREATE TRIGGER IF NOT EXISTS action_event_no_delete BEFORE DELETE ON action_event
BEGIN SELECT RAISE(ABORT, 'action_event_is_append_only'); END;
CREATE TABLE IF NOT EXISTS consumed_call_approval (
  approval_sha256 TEXT PRIMARY KEY,
  intent_id TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS consumed_call_approval_no_update BEFORE UPDATE
ON consumed_call_approval BEGIN SELECT RAISE(ABORT, 'consumed_is_append_only'); END;
CREATE TRIGGER IF NOT EXISTS consumed_call_approval_no_delete BEFORE DELETE
ON consumed_call_approval BEGIN SELECT RAISE(ABORT, 'consumed_is_append_only'); END;
CREATE TABLE IF NOT EXISTS internal_action (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('started', 'executed', 'refused')),
  case_id TEXT NOT NULL,
  action_id TEXT NOT NULL,
  actor TEXT NOT NULL,
  at TEXT NOT NULL,
  identity_json TEXT NOT NULL,
  record_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS internal_action_no_update BEFORE UPDATE ON internal_action
BEGIN SELECT RAISE(ABORT, 'internal_action_is_append_only'); END;
CREATE TRIGGER IF NOT EXISTS internal_action_no_delete BEFORE DELETE ON internal_action
BEGIN SELECT RAISE(ABORT, 'internal_action_is_append_only'); END;
CREATE TABLE IF NOT EXISTS case_author (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('case.opened', 'proposal.recorded')),
  ref TEXT NOT NULL,
  original INTEGER NOT NULL CHECK(original IN (0, 1)),
  case_seq INTEGER,
  actor TEXT NOT NULL,
  at TEXT NOT NULL,
  identity_json TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_case_author_original ON case_author(case_id, kind, ref)
WHERE original = 1;
CREATE INDEX IF NOT EXISTS idx_case_author ON case_author(case_id, kind, ref);
CREATE TRIGGER IF NOT EXISTS case_author_no_update BEFORE UPDATE ON case_author
BEGIN SELECT RAISE(ABORT, 'case_author_is_append_only'); END;
CREATE TRIGGER IF NOT EXISTS case_author_no_delete BEFORE DELETE ON case_author
BEGIN SELECT RAISE(ABORT, 'case_author_is_append_only'); END;
CREATE TABLE IF NOT EXISTS case_request (
  actor TEXT NOT NULL,
  request_key TEXT NOT NULL,
  case_id TEXT NOT NULL,
  at TEXT NOT NULL,
  PRIMARY KEY(actor, request_key)
);
CREATE TRIGGER IF NOT EXISTS case_request_no_update BEFORE UPDATE ON case_request
BEGIN SELECT RAISE(ABORT, 'case_request_is_append_only'); END;
CREATE TRIGGER IF NOT EXISTS case_request_no_delete BEFORE DELETE ON case_request
BEGIN SELECT RAISE(ABORT, 'case_request_is_append_only'); END;
"""


class ActionPathError(ValidationError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class IntentNotFound(LookupError):
    """Unknown intent, or one whose case this actor may not see (identical)."""


class DispatchRefused(Exception):
    """Raised by a dispatcher only when it is certain nothing was sent."""


class ProductionTargetRefused(ActionPathError):
    """``production_target_refused``, naming the gates that are not met
    (``unmet_gates``: ``action_adapters.GATES`` or ``isolated_owner_binding``)."""

    def __init__(self, gates: list[str]) -> None:
        super().__init__("production_target_refused")
        self.unmet_gates = tuple(gates)


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canon(value).encode("utf-8")).hexdigest()


# --------------------------------------------------------------- records

@dataclass(frozen=True)
class ActionIntent:
    """What will be asked for, pinned to one case proposal."""

    intent_id: str
    case_id: str
    proposal_id: str
    capability: str
    owner_system: str
    proposal_sha256: str
    expected_effect: Mapping[str, Any]
    idempotency_key: str
    gate_reason: str
    # The proposal's citations with the digests they were cited at.
    evidence: tuple[Mapping[str, str], ...] = ()
    checked_preconditions: tuple[Mapping[str, Any], ...] = ()
    human_preconditions: tuple[str, ...] = ()
    # Per dispatched parameter: where its value came from (operating.injection_guard).
    parameter_trace: Mapping[str, Any] | None = None
    # RFC 8693-style subject (the employee the action is for) and actor chain, from the
    # registering session only (``session_identity.intent_identity``); None: unknown.
    sub: Mapping[str, Any] | None = None
    act: tuple[Mapping[str, Any], ...] | None = None
    # The owner's own warnings on this write, checked at registration (the catalog's
    # ``owner_warnings``): ``{id, status: flagged|clear|not_checked|check_failed, count}``.
    owner_warnings: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class CallApproval:
    """A human's approval of one exact MC call (pack 04 section 8 R1)."""

    tool: str
    arguments: Mapping[str, Any]
    arguments_sha256: str
    approved_by: str
    approved_at: str
    expires_at: str
    evidence_ref: str
    approval_sha256: str    # identity of the human approval: approver, time, evidence
    approver_oid: str | None = None   # the approver's Entra oid, as configured
    # Where that oid came from: {"source": "committed"} or {"source": "graph_users", "read_at"}.
    approver_oid_provenance: Mapping[str, str] | None = None


@dataclass(frozen=True)
class ApprovalRecord:
    """MC's decision on our gate, as re-checked by ``approval_decision``."""

    task_id: str
    gate_id: str
    status: str | None
    decided_by: str | None
    decided_at: str | None
    reason_sha256: str
    authorizes: bool
    reason_code: str
    decider_role: str | None = None       # from the MC admin/owner roster
    roster_sha256: str | None = None
    roster_source: str | None = None
    decider_oid: str | None = None        # the decider's Entra object id, from the roster
    subjects_sha256: str | None = None    # the subject identities the decider was checked against
    decider_oid_provenance: Mapping[str, str] | None = None   # as ``CallApproval``'s


@dataclass(frozen=True)
class DispatchRecord:
    attempt_id: str
    idempotency_key: str
    system: str
    outcome: str            # dispatched | not_dispatched | dispatched_unknown
    receipt: str | None
    reason: str | None


@dataclass(frozen=True)
class VerificationRecord:
    outcome: str            # operating.cases.VERIFICATION_OUTCOMES
    reason: str | None
    fields: Mapping[str, Any]
    case_verification_id: str | None
    reconciled_by: str | None
    attribution: str = "not_established"
    # The owner's operation status for our idempotency key, when the owner reports
    # it (``goal2_exec_owner`` contract): applied | not_applied | pending | unknown,
    # with the owner's own reason for it.
    operation: Mapping[str, Any] | None = None
    concludes: str | None = None    # "failed": certainly not applied (fenced key)


# ------------------------------------------------------------- dispatchers

class Dispatcher(Protocol):
    system: str

    def refusal(self, intent: ActionIntent) -> str | None: ...
    def dispatch(self, intent: ActionIntent) -> str: ...


class _Sealed:
    """Attributes are set once in ``__init__``; ``__slots__`` leaves no instance
    dict, so a method cannot be shadowed on an instance."""

    __slots__ = ()

    def __setattr__(self, name: str, value: Any) -> None:
        if hasattr(self, name):
            raise AttributeError(f"{type(self).__name__} is sealed")
        object.__setattr__(self, name, value)


class RefusingDispatcher(_Sealed):
    """The dispatcher for every real system: refuses before any I/O."""

    __slots__ = ("system", "reason")

    def __init__(self, system: str, reason: str = "no_authorized_write_capability") -> None:
        self.system = system
        self.reason = reason

    def refusal(self, intent: ActionIntent) -> str | None:
        return self.reason

    def dispatch(self, intent: ActionIntent) -> str:  # pragma: no cover - never reached
        raise DispatchRefused(self.reason)


class FixtureSystem:
    """An in-memory owning system for ``fixture.*`` relations (isolated-fixture).

    It is also the case store's citation resolver for ``fixture://`` handles, so
    a dispatched change shows up as changed cited content, as a real source would.
    ``lose_next_response`` applies the write and then raises (a lost answer);
    ``fail_next_before_send`` raises without writing.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.roles: dict[str, dict[str, str]] = {}
        self.receipts: dict[str, str] = {}
        self.calls = 0
        self.lose_next_response = False
        self.fail_next_before_send = False

    @staticmethod
    def handle(relation: str, key: Mapping[str, Any]) -> str:
        return f"fixture://{relation}/{_canon(dict(key))}"

    def seed(self, relation: str, key: Mapping[str, Any], row: Mapping[str, Any],
             roles: Mapping[str, str] | None = None) -> str:
        """Add one row. ``roles``: the relation's declared field roles
        (``injection_guard.cited_role``: ``quantity:received``, ``money:amount``,
        ``identifier:customer_po`` ...), served on its citations as ``field_roles``."""
        if not relation.startswith("fixture."):
            raise ValueError("fixture rows live only in fixture.* relations")
        if roles is not None:
            if any(not isinstance(role, str) or not injection_guard.ROLE.fullmatch(role)
                   for role in roles.values()):
                raise ValueError("fixture field roles must be role names")
            self.roles.setdefault(relation, {}).update(roles)
        self.rows[(relation, _canon(dict(key)))] = dict(row)
        return self.handle(relation, key)

    def resolve(self, handle: str, actor: Actor) -> dict[str, Any]:
        if not handle.startswith("fixture://"):
            raise CitationError("not_a_fixture_handle")
        relation, _, key = handle[len("fixture://"):].partition("/")
        row = self.rows.get((relation, key))
        if row is None:
            raise CitationError("fixture_row_absent")
        out = {"relation": relation, "key": json.loads(key), "row": dict(row),
               "evidence": EVIDENCE_LABEL}
        if relation in self.roles:
            out[injection_guard.FIELD_ROLES_KEY] = dict(self.roles[relation])
        return out

    def apply(self, idempotency_key: str, relation: str, key: Mapping[str, Any],
              field: str | Mapping[str, Any], value: Any = None) -> str:
        """Write one field (``field``, ``value``) or every field of a mapping, once per
        idempotency key."""
        self.calls += 1
        if self.fail_next_before_send:
            self.fail_next_before_send = False
            raise ConnectionError("fixture: connection refused before send")
        if idempotency_key in self.receipts:
            return self.receipts[idempotency_key]
        row = self.rows.get((relation, _canon(dict(key))))
        if row is None:
            raise DispatchRefused("fixture_target_absent")
        values = dict(field) if isinstance(field, Mapping) else {field: value}
        row.update(values)
        receipt = "fixture-receipt:" + _sha([idempotency_key, relation, key,
                                             field if not isinstance(field, Mapping)
                                             else sorted(values)])[:16]
        self.receipts[idempotency_key] = receipt
        if self.lose_next_response:
            self.lose_next_response = False
            raise TimeoutError("fixture: response lost after the write")
        return receipt

    def read_back(self, intent: ActionIntent) -> dict[str, Any]:
        effect = intent.expected_effect
        row = self.rows.get((effect["relation"], _canon(effect["key"])))
        if row is None:
            return {"outcome": "unknown", "reason": "row_not_returned", "fields": {}}
        fields = {path: field_status(row, path, value)
                  for path, value in effect_values(effect).items()}
        outcome, reason = effect_outcome(effect, {p: f["status"] for p, f in fields.items()})
        return {"outcome": outcome, "reason": reason, "fields": fields}


_ISOLATED_OWNERS = (FixtureSystem, IsolatedOwnerSystem)


class FixtureDispatcher(_Sealed):
    """Dispatches ``fixture.record.set_field`` into an isolated owner only: this
    module's in-memory ``FixtureSystem`` or ``goal2_exec_owner.IsolatedOwnerSystem``
    (exact types; the latter through the owner contract
    ``dispatch(payload, idempotency_key)``)."""

    __slots__ = ("system", "fixture")

    def __init__(self, fixture: FixtureSystem | IsolatedOwnerSystem) -> None:
        if type(fixture) not in _ISOLATED_OWNERS:
            raise ActionPathError("fixture_dispatcher_needs_fixture_system")
        self.system = "fixture"
        self.fixture = fixture

    def refusal(self, intent: ActionIntent) -> str | None:
        if intent.capability not in FIXTURE_CAPABILITIES:
            return "fixture_dispatcher_capability_mismatch"
        if not str(intent.expected_effect.get("relation", "")).startswith("fixture."):
            return "fixture_dispatcher_refuses_non_fixture_relation"
        if type(self.fixture) is IsolatedOwnerSystem:
            try:  # every precondition on the target must become an owner-side condition
                dispatch_payload(intent)
            except PayloadUnmappable as exc:
                return exc.code
        return None

    def dispatch(self, intent: ActionIntent) -> str:
        effect = intent.expected_effect
        if type(self.fixture) is IsolatedOwnerSystem:
            try:
                payload = dispatch_payload(intent)
            except PayloadUnmappable as exc:  # certain: nothing was sent
                raise DispatchRefused(exc.code) from None
            return IsolatedOwnerSystem.dispatch(self.fixture, payload, intent.idempotency_key)
        values = effect_values(effect)  # every field of the effect, in one write
        if len(values) == 1:
            [(field, value)] = values.items()
            return FixtureSystem.apply(self.fixture, intent.idempotency_key, effect["relation"],
                                       effect["key"], field, value)
        return FixtureSystem.apply(self.fixture, intent.idempotency_key, effect["relation"],
                                   effect["key"], values)


class RealSystemDispatcher(_Sealed):
    """Dispatches one cataloged real capability through its reviewed adapter
    (``action_adapters.ADAPTER_TYPES``, exact types, over a reviewed transport). The
    action path calls it only when every gate holds (``target_gates``)."""

    __slots__ = ("system", "capability", "adapter")

    def __init__(self, adapter: Any) -> None:
        if type(adapter) not in action_adapters.ADAPTER_TYPES:
            raise ActionPathError("real_dispatcher_needs_reviewed_adapter")
        self.system = adapter.SYSTEM
        self.capability = adapter.capability
        self.adapter = adapter

    def refusal(self, intent: ActionIntent) -> str | None:
        if intent.capability != self.capability or intent.owner_system != self.system:
            return "real_dispatcher_capability_mismatch"
        return type(self.adapter).refusal(self.adapter, intent)

    def dispatch(self, intent: ActionIntent) -> str:
        try:
            return type(self.adapter).dispatch(self.adapter, intent)
        except action_adapters.NotSent as exc:   # certain: nothing written
            raise DispatchRefused(exc.code) from None

    def read_back(self, intent: ActionIntent) -> dict[str, Any]:
        """The ``ActionPath.read_back`` callable: a GET of the owner's record."""
        return type(self.adapter).read_back(self.adapter, intent)


class DispatcherRegistry:
    """Capability -> dispatcher. Only the three dispatcher types of this module.

    A real-system capability (``REAL_CAPABILITIES``, or any capability whose
    prefix names a real system) maps to ``RefusingDispatcher`` unless its reviewed
    adapter is registered (``RealSystemDispatcher``, for exactly its capability); an
    unregistered capability is refused.
    """

    def __init__(self) -> None:
        self._dispatchers: dict[str, Any] = {
            name: RefusingDispatcher(cap.owner_system) for name, cap in REAL_CAPABILITIES.items()}

    def register(self, capability: str, dispatcher: Any) -> None:
        if type(dispatcher) is FixtureDispatcher:
            if (capability not in FIXTURE_CAPABILITIES
                    or type(dispatcher.fixture) not in _ISOLATED_OWNERS):
                raise ActionPathError("real_system_dispatch_not_enabled")
        elif type(dispatcher) is RealSystemDispatcher:
            if dispatcher.capability != capability:
                raise ActionPathError("real_dispatcher_capability_mismatch")
        elif type(dispatcher) is not RefusingDispatcher:
            raise ActionPathError("real_system_dispatch_not_enabled")
        self._dispatchers[capability] = dispatcher

    def for_capability(self, capability: str) -> Any:
        found = self._dispatchers.get(capability)
        if found is not None:
            return found
        prefix = capability.partition(".")[0]
        return RefusingDispatcher(prefix if prefix in REAL_SYSTEMS else "unknown",
                                  "capability_not_registered")


# -------------------------------------------------------- gate requester

# ------------------------------------------------------------------ path

def _proposal_body(proposal: Mapping[str, Any]) -> dict[str, Any]:
    return {key: proposal.get(key) for key in
            ("capability", "payload", "preconditions", "expected_effect")}


def _document_rows(evidence: Sequence[Mapping[str, Any]],
                   read: Callable[[str], Mapping[str, Any] | None] | None,
                   capability: Any) -> list[Mapping[str, Any]]:
    """``evidence`` (``CaseStore.proposal_evidence``) with the content of each unchanged
    byte-pinned document citation (``capability_guards.DOCUMENT_SCHEMES``) replaced by the
    ``documentFields`` row ``read`` (``capability_guards.current_reader`` for the actor)
    gives for it now, holding only the fields its profile's ``attests`` rule names
    ``capability`` for (``document_fields.proposed_fields``): no other field of the
    document can supply a value of the proposal. Any other item, or one ``read`` gives
    nothing for (other bytes, not readable, not a whole pinned document), is kept as the
    case resolved it."""
    if read is None:
        return list(evidence)
    from ontology.document_fields import proposed_fields
    out: list[Mapping[str, Any]] = []
    for item in evidence:
        if (str(item.get("handle")).startswith(capability_guards.DOCUMENT_SCHEMES)
                and item.get("content") is not None
                and item.get("current_digest") == item.get("cited_digest")):
            found = read(item["handle"])
            if found is not None:
                row = found["row"]
                named = proposed_fields(row.get("profile"), capability)
                item = {**item, "content": {
                    "relation": found.get("relation"), "key": found.get("key"),
                    "name": found.get("name"),
                    "row": {**row, "fields": {name: field for name, field in
                                              (row.get("fields") or {}).items()
                                              if name in named}}}}
        out.append(item)
    return out


def _cited_files(traces: Mapping[str, Any], evidence: Sequence[Mapping[str, Any]]) -> str:
    """Which file supplied a traced value, for the approver: each byte-pinned document a
    trace cites (as ``_document_rows`` read it), by the file name its capture records
    (the document's author chose it: printable characters only, cut to 40, quoted) and the
    first 12 hex digits of its pinned SHA-256; "" when no trace cites one."""
    contents = {item.get("handle"): item.get("content") for item in evidence}
    out: list[str] = []
    for trace in traces.values():
        handle = trace.get("citation") if isinstance(trace, Mapping) else None
        content = contents.get(handle)
        if (not str(handle).startswith(capability_guards.DOCUMENT_SCHEMES)
                or not isinstance(content, Mapping)
                or not isinstance(content.get("row"), Mapping)):
            continue
        name = content.get("name")
        name = "".join(filter(str.isprintable, name))[:40] if isinstance(name, str) else ""
        named = json.dumps(name, ensure_ascii=False) + " " if name.strip() else ""
        cited = f"file {named}sha256 {str(content['row'].get('content_sha256'))[:12]}"
        if cited not in out:
            out.append(cited)
    return "; ".join(out)


def _binding(intent: Mapping[str, Any]) -> str:
    """What every later record of an intent is bound to: its case, proposal,
    proposal digest, cited evidence digests and idempotency key."""
    return _sha({key: intent.get(key) for key in ("intent_id", "case_id", "proposal_id",
                                                  "proposal_sha256", "idempotency_key")}
                | {"evidence": [{"handle": item["handle"], "digest": item["digest"]}
                                for item in intent.get("evidence") or ()]})


def target_gates(intent: ActionIntent, *, adapter_registered: bool | None = None,
                 mc_read_back: bool | None = None, transport: Any = None,
                 gate_request: bool = False) -> list[str]:
    """The gates an intent's target has not met (``[]``: it may be dispatched to).

    The isolated sandbox: owner ``fixture``, a ``FIXTURE_CAPABILITIES`` type and a
    ``fixture.*`` relation, all three, or ``isolated_owner_binding``. Every other
    target is a real system, reached only when every ``action_adapters.GATES`` holds:
    a cataloged real capability for this owner and relation, its adapter registered,
    the MC approval read back at dispatch and the allow switch on. ``adapter_registered``
    and ``mc_read_back`` are ``None`` before dispatch (not reported); ``transport``, the
    dispatcher's at dispatch, or ``gate_request`` (the MC gate request) decides whether a
    test-mode allow counts (``action_adapters.allow_switch_on``; a view counts none)."""
    relation = str((intent.expected_effect or {}).get("relation", ""))
    if (intent.owner_system == "fixture" or intent.capability in FIXTURE_CAPABILITIES
            or relation.startswith("fixture.")):
        isolated = (intent.owner_system == "fixture"
                    and intent.capability in FIXTURE_CAPABILITIES
                    and intent.capability not in REAL_CAPABILITIES
                    and relation.startswith("fixture."))
        return [] if isolated else ["isolated_owner_binding"]
    return action_adapters.unmet_gates(intent, adapter_registered=adapter_registered,
                                       mc_read_back=mc_read_back, transport=transport,
                                       gate_request=gate_request)


def production_target_refusal(intent: ActionIntent, *, adapter_registered: bool | None = None,
                              mc_read_back: bool | None = None,
                              gate_request: bool = False) -> str | None:
    """The hard guard: ``production_target_refused`` while any gate is unmet
    (``target_gates`` names them). The default outcome for every real system: its
    gates include the allow switch, which is off unless a person turns it on."""
    return ("production_target_refused" if target_gates(
        intent, adapter_registered=adapter_registered, mc_read_back=mc_read_back,
        gate_request=gate_request) else None)


def mc_call_refusal(intent: ActionIntent, tool: Any = None) -> str | None:
    """The hard guard of the MC write door: only an MC-owned catalog type
    (``MC_CALL_CAPABILITIES``) creating an MC task, through one of the catalog's MC write
    tools (``tool``: the approved call's, when there is one). Everything else is refused
    before any I/O, whatever was approved."""
    from .action_catalog import MC_OWNER, MC_WRITE_TOOLS
    if (intent.capability not in MC_CALL_CAPABILITIES or intent.owner_system != MC_OWNER
            or (intent.expected_effect or {}).get("relation") != MC_CALL_RELATION
            or (tool is not None and tool not in MC_WRITE_TOOLS)):
        return "mc_call_refused"
    return None


def _gate_reason(intent_id: str, capability: str, effect: Mapping[str, Any],
                 case_id: str, proposal_id: str, provenance: str = "",
                 warnings: tuple[Mapping[str, Any], ...] = ()) -> str:
    """The MC gate reason; an owner warning that is not ``clear`` is named in it (a flagged
    one with its count of other records) so the approver decides knowing it."""
    noted = "".join(f"; WARNING {item['id']}: {item['count']} other record(s)"
                    if item["status"] == "flagged" else f"; {item['id']} {item['status']}"
                    for item in warnings if item["status"] != "clear")
    reason = (f"PLX Workbench action {intent_id}: {capability} on {effect['relation']} "
              f"{_canon(effect['key'])}: set "
              + ", ".join(f"{field} = {json.dumps(value, ensure_ascii=False)}"
                          for field, value in effect_values(effect).items())
              + f" (case {case_id} {proposal_id}; read-back verified"
              f"{'; ' + provenance if provenance else ''}{noted})")
    if len(reason) > 500:
        raise ActionPathError("gate_reason_too_long")
    return reason


class ActionPath:
    """The goal-2 action state machine over a ``CaseStore``.

    ``call_approvers``: human emails configured to approve every action type per call
    (pack 04 section 8 R1: Tanush or Vince), as a set or as ``{email: Entra oid}``; a
    host passes none and takes its approvers from ``approver_roster``, where each counts
    only for its own action types. Separation of duties matches an approver by oid too
    (the configured one, else the ``approver_roster``'s); an approver with no known oid is
    refused (``call_approver_oid_unknown``). With neither, every per-call approval is
    refused (``call_approvers_not_configured``).
    ``mc_operator_email``: our ``X-MC-Operator-Email`` (the gate requester); it
    may never approve a call or decide a gate, and the MC client must use it.
    ``decision_max_age``: an MC decision older than this (by ``decidedAt``)
    no longer authorizes a dispatch; a new call approval and gate are needed.
    ``approver_roster``: who approves which action type (``ApproverRoster``), one rule
    for every approver: a per-call approver counts for an intent only when the roster
    lists the intent's action type for them, and a gate decided by anyone who is not an
    ``admin``/``owner`` on it approving that type never authorizes
    (``decider_not_approver_of_action_type``; an empty list approves nothing). Empty by
    default: no decision authorizes until a person supplies it.
    ``approver_roster_reload``: the host's load of that roster
    (``ActionApprovers.reload``), called when the roster's Graph oid for a person is past
    its age limit at a check; a load that fails keeps the stale copy (refused unverified).
    ``gate_send_settle``: how long after ``gate.request_started`` an unanswered
    request may still land (``DEFAULT_GATE_SEND_SETTLE``).
    ``mc_follow_up_bucket``: the MC bucket (``BKT-*``) a person chose for the Workbench's
    follow-up tasks (GOAL2-ACTIONS.md section 8 item 1); without it, and without
    ``mc_operator_email``, no MC call is prepared.
    ``mc_follow_up_audience``: ``bucket -> McBucketAudience | None``, who reads that
    bucket's tasks in MC. A cited source's text goes into the call only when every member
    reads the exact cited content through this store's own citation read (the check every
    ``plx.*`` read passes); without it, or when it cannot say, every source goes as its
    citation alone (``audience_withholding``).
    ``owner_peers``: ``(actor, relation) -> [{"key", "row"}]``, the owner's records of a
    relation as ``actor`` may read them (``None``: the actor may not read them);
    ``register`` runs the type's ``owner_warnings`` on them (e.g. the Portal's warning that
    the customer already used this PO). Without it, without a readable relation, or when it
    fails, the warning is recorded as ``not_checked`` / ``check_failed``, never ``clear``.
    """

    def __init__(self, path: str | Path, cases: CaseStore, *,
                 call_approvers: frozenset[str] | Mapping[str, str | None] = frozenset(),
                 mc_operator_email: str | None = None,
                 approver_roster: ApproverRoster | None = None,
                 approver_roster_reload: Callable[[], ApproverRoster] | None = None,
                 decision_max_age: timedelta = DEFAULT_DECISION_MAX_AGE,
                 gate_send_settle: timedelta = DEFAULT_GATE_SEND_SETTLE,
                 clock: Callable[[], datetime] | None = None,
                 catalog: ActionCatalog | None = None,
                 mc_follow_up_bucket: str | None = None,
                 mc_follow_up_audience: Callable[[str], McBucketAudience | None] | None = None,
                 owner_peers: Callable[[Actor, str], Any] | None = None
                 ) -> None:
        if not isinstance(cases, CaseStore):
            raise TypeError("cases must be a CaseStore")
        if catalog is not None and type(catalog) is not ActionCatalog:
            raise TypeError("catalog must be an ActionCatalog")
        # The typed action catalog (operating/action_catalog.json): registration hook.
        self.catalog = catalog or ActionCatalog.builtin()
        if approver_roster is not None and type(approver_roster) is not ApproverRoster:
            raise TypeError("approver_roster must be an ApproverRoster")
        self.approver_roster = approver_roster or ApproverRoster()
        if approver_roster_reload is not None and not callable(approver_roster_reload):
            raise TypeError("approver_roster_reload must be callable")
        self._roster_reload = approver_roster_reload
        if not isinstance(decision_max_age, timedelta) or decision_max_age <= timedelta(0):
            raise ValueError("decision_max_age must be a positive timedelta")
        if not isinstance(gate_send_settle, timedelta) or gate_send_settle <= timedelta(0):
            raise ValueError("gate_send_settle must be a positive timedelta")
        self.gate_send_settle = gate_send_settle
        self.path = str(path)
        self.cases = cases
        configured = (dict(call_approvers) if isinstance(call_approvers, Mapping)
                      else dict.fromkeys(call_approvers))
        try:
            self._call_approver_oids = {canonical_email(email): canonical_oid(oid)
                                        for email, oid in configured.items()}
        except ValueError:
            raise ValueError("call approver oid must be an Entra object id") from None
        self.call_approvers = frozenset(self._call_approver_oids)
        self.mc_operator = (canonical_email(mc_operator_email)
                            if isinstance(mc_operator_email, str) and mc_operator_email.strip()
                            else None)
        self.decision_max_age = decision_max_age
        if mc_follow_up_bucket is not None and (
                not isinstance(mc_follow_up_bucket, str)
                or not re.fullmatch(r"BKT-[A-Za-z0-9_-]{1,80}", mc_follow_up_bucket)):
            raise ValueError("mc_follow_up_bucket must be an MC bucket id (BKT-...)")
        self.mc_follow_up_bucket = mc_follow_up_bucket
        if mc_follow_up_audience is not None and not callable(mc_follow_up_audience):
            raise TypeError("mc_follow_up_audience must be callable")
        self.mc_follow_up_audience = mc_follow_up_audience
        if owner_peers is not None and not callable(owner_peers):
            raise TypeError("owner_peers must be callable")
        self.owner_peers = owner_peers
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(_DDL)

    # ---- plumbing -----------------------------------------------------

    @contextmanager
    def _connect(self, write: bool = False, hold: Any = None) -> Iterator[sqlite3.Connection]:
        """``hold``: a context (``CaseStore.outside_write``) entered after this store's
        write lock and left after its commit, so the two commit as one step."""
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            if not write:
                yield db
                return
            db.execute("BEGIN IMMEDIATE")
            try:
                with hold if hold is not None else nullcontext():
                    yield db
                    db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        finally:
            db.close()

    def _now(self) -> datetime:
        moment = self._clock()
        if moment.tzinfo is None:
            raise ActionPathError("clock_must_be_timezone_aware")
        return moment.astimezone(timezone.utc)

    def _append(self, db: sqlite3.Connection, intent_id: str, kind: str, actor: Actor,
                payload: Mapping[str, Any], act: list[Any]) -> dict[str, Any]:
        """Append one hash-chained event; ``act`` (the step's session, ``_session_act``)
        is stored in the hashed body of every event. No event is written without it."""
        if not act:
            raise ActionPathError("session_required")
        last = db.execute("SELECT intent_seq, event_hash FROM action_event WHERE intent_id=? "
                          "ORDER BY intent_seq DESC LIMIT 1", (intent_id,)).fetchone()
        seq = last["intent_seq"] + 1 if last else 1
        prev = last["event_hash"] if last else _GENESIS
        at = self._now().isoformat(timespec="microseconds")
        if kind in _BOUND_KINDS:
            first = db.execute("SELECT payload_json FROM action_event WHERE intent_id=? AND "
                               "intent_seq=1", (intent_id,)).fetchone()
            if first is None:
                raise ActionPathError("intent_not_registered")
            payload = {**payload, "binding_sha256": _binding(json.loads(first["payload_json"]))}
        body = _canon({**payload, "act": act})
        label = actor_label(actor)
        event_hash = _sha([intent_id, seq, kind, label, at, body, prev])
        db.execute("INSERT INTO action_event(intent_id,intent_seq,kind,actor,at,payload_json,"
                   "prev_hash,event_hash) VALUES(?,?,?,?,?,?,?,?)",
                   (intent_id, seq, kind, label, at, body, prev, event_hash))
        return {"intent_id": intent_id, "seq": seq, "kind": kind, "at": at}

    def _fold(self, rows: list[sqlite3.Row]) -> dict[str, Any] | None:
        if not rows:
            return None
        prev, chain_ok = _GENESIS, True
        state: dict[str, Any] = {}
        for index, row in enumerate(rows, 1):
            payload = json.loads(row["payload_json"])
            # The step's act (None: unknown, as on every event before v2). On a
            # registration it is the intent's own ``act`` and stays in the payload.
            step_act = (payload.get("act") if row["kind"] in _IDENTITY_KINDS
                        else payload.pop("act", None))
            if (row["intent_seq"] != index or row["prev_hash"] != prev or row["event_hash"] !=
                    _sha([row["intent_id"], index, row["kind"], row["actor"], row["at"],
                          row["payload_json"], row["prev_hash"]])):
                chain_ok = False
            prev = row["event_hash"]
            kind = row["kind"]
            if kind in _BOUND_KINDS and (not state or payload.get("binding_sha256")
                                         != _binding(state["intent"])):
                chain_ok = False  # a record not bound to this intent's case and evidence
            if kind == "intent.registered":
                state = {"intent": payload, "state": "proposed", "call_approval": None,
                         "prepared": None, "gate": None, "gate_attempts": 0, "gate_ids": [],
                         "decisions": [], "dispatches": [], "late_gate": None,
                         "verifications": [], "recovery": [], "history": [],
                         "readback_conclusion": None,
                         "subjects": [{"sub": payload.get("sub"), "act": payload.get("act")}]}
            elif kind == "intent.co_requested":
                # Another registering session's subject: a subject of this intent too.
                state["subjects"].append({"sub": payload.get("sub"), "act": payload.get("act")})
            elif kind == "gate.prepared":
                state.update(state="gate_prepared", prepared=payload, call_approval=None,
                             late_gate=None)
            elif kind == "call.prepared":
                # The exact MC call a person is asked to approve (an MC-owned intent).
                state.update(state="call_approval_required", prepared=payload,
                             call_approval=None)
            elif kind == "call.approved":
                state.update(state="call_approved", call_approval=payload)
            elif kind == "gate.request_started":
                # Outcome not (yet) recorded: unknown until a result or a task read.
                state["gate_attempts"] += 1
                state.update(state="gate_request_unknown", call_approval=None, prepared=None,
                             gate={"task_id": payload["task_id"], "gate_id": None,
                                   "attempt_id": payload["attempt_id"],
                                   "started_at": row["at"], "send_finished": False})
            elif kind == "gate.requested":
                state["gate_ids"].append(payload["gate_id"])
                state.update(state="gate_requested", gate=payload, call_approval=None,
                             prepared=None)
            elif kind == "gate.request_refused":
                state.update(state="gate_request_refused", call_approval=None, prepared=None)
            elif kind == "gate.request_unknown":
                started = (state.get("gate") or {}).get("started_at")
                state.update(state="gate_request_unknown", call_approval=None, prepared=None,
                             gate={**payload, "started_at": started, "send_finished": True})
            elif kind == "gate.request_lost":
                state["state"] = "gate_request_lost"
                state["gate"] = {**(state.get("gate") or {}), "lost": True}
            elif kind == "gate.decision":
                state["decisions"].append(payload)
                if payload["authorizes"]:
                    state["state"] = "gate_authorized"
                elif payload["status"] == "rejected":
                    state["state"] = "gate_rejected"
                elif payload["reason_code"] != "not_approved":
                    state["state"] = "gate_not_authorizing"
            elif kind == "decision.expired":
                state["state"] = "approval_expired"
            elif kind == "dispatch.refused":
                state["dispatches"].append({**payload, "outcome": "not_dispatched"})
                state["state"] = "dispatch_refused"
            elif kind == "dispatch.started":
                state["dispatches"].append({**payload, "outcome": "dispatched_unknown",
                                            "receipt": None, "reason": "no_outcome_recorded"})
                state["state"] = "dispatched_unknown"
            elif kind == "dispatch.finished":
                state["dispatches"][-1].update(payload)
                concluded = state.get("readback_conclusion") or {}
                if state["state"] == "escalated":
                    # Escalated while this dispatch was in flight: a human owns the
                    # intent. The late answer adds its receipt and outcome to the attempt
                    # and never regresses the state (a refusal would read dispatchable
                    # again and be re-sent; an acknowledgement would hide the hand-off).
                    state["dispatches"][-1]["after_escalation"] = True
                elif not (concluded.get("attempt_id") == payload["attempt_id"]
                          and concluded.get("state") == state["state"]):
                    state["state"] = _DISPATCH_STATE.get(payload["outcome"],
                                                         "dispatched_unknown")
                # else: a read-back already concluded this attempt (verified or failed)
                # while its dispatch was in flight; the late answer adds the receipt
                # and outcome to the attempt but never regresses that conclusion.
            elif kind == "readback.recorded":
                state["verifications"].append(payload)
                if payload["outcome"] == "effect_observed":
                    state["state"] = "verified"
                elif payload.get("concludes") == "failed":
                    state["state"] = "failed"
                if state["state"] in ("verified", "failed") and state["dispatches"]:
                    state["readback_conclusion"] = {
                        "attempt_id": state["dispatches"][-1].get("attempt_id"),
                        "state": state["state"]}
            elif kind == "recovery.recorded":
                state["recovery"].append(payload)
                if payload.get("trigger") == "late_gate_answer" and payload.get("gate_id"):
                    state["late_gate"] = payload["gate_id"]
            elif kind == "recovery.escalated":
                state["recovery"].append({**payload, "trigger": "escalated_to_human"})
                state["state"] = "escalated"
            state["history"].append({"seq": index, "kind": kind, "actor": row["actor"],
                                     "at": row["at"], "act": step_act})
            if not state.get("intent"):
                continue
            # EXE-04 trail, rebuilt from the durable events on every read.
            states = state.setdefault("state_trail", [])
            if not states or states[-1]["state"] != state["state"]:
                states.append({"state": state["state"], "seq": index, "kind": kind})
            outcome = outcome_of(state)
            trail = state.setdefault("outcome_trail", [])
            if not trail or trail[-1]["outcome"] != outcome:
                trail.append({"outcome": outcome, "state": state["state"], "seq": index,
                              "kind": kind, "at": row["at"]})
        state["chain_ok"] = chain_ok
        return state

    def _fold_authored(self, db: sqlite3.Connection,
                       rows: list[sqlite3.Row]) -> dict[str, Any] | None:
        """``_fold`` plus ``authors``: the recorded subjects of the intent's case opening
        and proposal (``open_case`` / ``record_proposal``), read on the same connection,
        so a check inside a committing write sees every one recorded before it."""
        state = self._fold(rows)
        if state is None or not state.get("intent"):
            return state
        intent = state["intent"]
        found = db.execute(
            "SELECT kind, original, identity_json FROM case_author WHERE case_id=? AND "
            "(kind='case.opened' OR (kind='proposal.recorded' AND ref=?)) ORDER BY seq",
            (intent.get("case_id"), intent.get("proposal_id"))).fetchall()
        authors = []
        for row in found:
            identity = json.loads(row["identity_json"])
            authors.append({"kind": row["kind"], "original": bool(row["original"]),
                            "sub": identity.get("sub"), "act": identity.get("act")})
        state["authors"] = authors
        return state

    def _load(self, db: sqlite3.Connection, actor: Actor, intent_id: str) -> dict[str, Any]:
        return self._load_viewed(db, actor, intent_id)[0]

    def _load_viewed(self, db: sqlite3.Connection, actor: Actor,
                     intent_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """The intent's folded state and its case as ``actor`` reads it now."""
        if not isinstance(actor, Actor):
            raise ValidationError("actor_required")
        rows = db.execute("SELECT * FROM action_event WHERE intent_id=? ORDER BY intent_seq",
                          (intent_id if isinstance(intent_id, str) else "",)).fetchall()
        state = self._fold_authored(db, rows)
        if state is None:
            raise IntentNotFound("intent_not_found")
        try:
            case_view = self.cases.get(actor, state["intent"]["case_id"])
        except CaseNotFound:
            raise IntentNotFound("intent_not_found") from None
        if not state["chain_ok"]:
            raise ActionPathError("action_event_chain_broken")
        return state, case_view

    def _step(self, actor: Actor, intent_id: str, allowed: set[str],
              make: Callable[[dict[str, Any]], tuple[str, Mapping[str, Any]]],
              act: list[Any] | None,
              requester: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """One committing step; ``requester`` (an approval request's session identity)
        is recorded as a co-requester first, in the same write (``_co_request``)."""
        with self._connect(write=True) as db:
            state = self._load(db, actor, intent_id)
            if state["state"] not in allowed:
                raise ActionPathError(f"invalid_state:{state['state']}")
            if requester is not None:
                state = self._co_request(db, actor, intent_id, state, requester)
            kind, payload = make(state)
            return self._append(db, intent_id, kind, actor, payload, act)

    def _co_request(self, db: sqlite3.Connection, actor: Actor, intent_id: str,
                    state: dict[str, Any], requester: Mapping[str, Any]) -> dict[str, Any]:
        """Record whoever requests an approval as a subject of the intent (the
        co-requester, decision 2026-10-05 on WS-11): like any subject they may never
        approve the call or decide the gate. A requester with no known identity is
        refused (``subject_unknown``) before anything is written. Returns the state as
        it is now."""
        if not _identities_of(requester["sub"]):
            raise ActionPathError("subject_unknown")
        if not _adds_subject(state, requester["sub"]):
            return state
        self._append(db, intent_id, "intent.co_requested", actor,
                     {"sub": requester["sub"], "act": requester["act"]}, requester["act"])
        return self._load(db, actor, intent_id)

    @staticmethod
    def _intent(state: Mapping[str, Any]) -> ActionIntent:
        return ActionIntent(**state["intent"])

    def _proposal(self, actor: Actor, case_id: str, proposal_id: str) -> dict[str, Any]:
        view = self.cases.get(actor, case_id)
        proposal = next((p for p in view["proposals"] if p["id"] == proposal_id), None)
        if proposal is None:
            raise ActionPathError("proposal_not_found")
        proposal["_case_status"] = view["status"]
        # The case context the catalog's traces read: the objective, and the case's status
        # and gaps as this actor reads them now.
        proposal["_objective"] = {"question": view.get("question"), "roots": view.get("roots"),
                                  "objective_author_kind": view.get("objective_author_kind"),
                                  "status": view["status"], "gaps": view.get("gaps")}
        return proposal

    # ---- authorship (subjects before an intent exists) -----------------

    def open_case(self, actor: Actor, question: str, *, session: Any = None,
                  request_key: str | None = None, **options: Any) -> str:
        """Open a case on this path's case store (``CaseStore.open``, ``options`` as
        there) and record the opening session's ``sub``/``act`` (``case.opened``).

        The opener is a subject of every action on the case: they may never approve
        one (``_sod_refusal``). A case opened any other way, or by a session with no
        subject, has an unknown opener, and no decision authorizes an action on it.
        Another actor's session is refused before anything is written.

        ``request_key`` (the caller's idempotency key for one request): the same actor's
        repeat returns the case the first call opened, which it can still read, and
        opens nothing; concurrent repeats wait on this store's write lock.
        """
        identity = self._author_identity(session, actor)
        if request_key is not None and (not isinstance(request_key, str)
                                        or not 1 <= len(request_key) <= 200):
            raise ActionPathError("invalid_request_key")
        with self._connect(write=True) as db:
            if request_key is not None:
                found = db.execute("SELECT case_id FROM case_request WHERE actor=? AND "
                                   "request_key=?", (actor_label(actor), request_key)).fetchone()
                if found is not None:
                    self.cases.get(actor, found["case_id"])   # CaseNotFound when not readable
                    return found["case_id"]
            case_id = self.cases.open(actor, question, **options)
            self._insert_author(db, actor, case_id, "case.opened", "", True, 1, identity)
            if request_key is not None:
                db.execute("INSERT INTO case_request(actor,request_key,case_id,at) "
                           "VALUES(?,?,?,?)", (actor_label(actor), request_key, case_id,
                                               self._now().isoformat(timespec="microseconds")))
        return case_id

    def break_claim(self, actor: Actor, case_id: str, reason: str, *,
                    session: Any = None) -> dict[str, Any]:
        """End another session's active claim on a case (``CaseStore.break_claim``) with
        a recorded ``reason``. Only an approver on this path's roster may: a session whose
        subject is a configured call approver or approves an action type on the
        ``approver_roster`` (an empty list approves nothing), matched by
        email and by the Entra oid the roster knows; an approver with no known oid is
        refused (``claim_breaker_not_an_approver``). Its console (human) or MCP (an agent
        acting for the approver, ``plx.case_open`` break mode) session is the break's
        journalled author kind: a break changes case state only, never a business system."""
        identity = self._author_identity(session, actor)
        sub = identity["sub"] or {}
        for email in sub.get("identities") or ():
            if not _HUMAN_EMAIL.fullmatch(email) or canonical_email(email) == self.mc_operator:
                continue
            if canonical_email(email) not in self.call_approvers \
                    and not self.approver_roster.approves_any(email):
                continue
            oid = self._call_approver_oid(email)
            if oid is not None and oid == sub.get("oid"):
                return self.cases.break_claim(actor, case_id, reason, {
                    "email": canonical_email(email), "oid": oid,
                    "session": identity["act"][0]["session"]},
                    author_kind=session.author_kind)
        raise ActionPathError("claim_breaker_not_an_approver")

    def record_proposal(self, actor: Actor, case_id: str, capability: str,
                        payload: Mapping[str, Any], preconditions: Any = (),
                        citations: Any = (), *, session: Any = None,
                        **options: Any) -> dict[str, Any]:
        """Record a proposal on this path's case store (``CaseStore.propose``, same
        arguments) and the recording session's ``sub``/``act`` as its author
        (``proposal.recorded``).

        The author is a subject of any intent on the proposal: they may never approve
        it. Only the call that creates a proposal records its original author; a
        repeat (``duplicate``) adds the repeating session as a further subject and can
        never stand in for an author never recorded. A proposal recorded any other way
        has an unknown author, and no decision authorizes it.
        """
        identity = self._author_identity(session, actor)
        receipt = self.cases.propose(actor, case_id, capability, payload, preconditions,
                                     citations, **options)
        self._record_author(actor, case_id, "proposal.recorded", receipt["proposal_id"],
                            receipt.get("duplicate") is False, receipt.get("seq"), identity)
        return receipt

    @staticmethod
    def _author_identity(session: Any, actor: Actor) -> dict[str, Any]:
        if not isinstance(actor, Actor):
            raise ValidationError("actor_required")
        try:
            return intent_identity(session, actor)
        except SessionIdentityRefused as exc:
            raise ActionPathError(exc.reason) from None

    def _record_author(self, actor: Actor, case_id: str, kind: str, ref: str,
                       original: bool, case_seq: Any, identity: Mapping[str, Any]) -> None:
        with self._connect(write=True) as db:
            self._insert_author(db, actor, case_id, kind, ref, original, case_seq, identity)

    def _insert_author(self, db: sqlite3.Connection, actor: Actor, case_id: str, kind: str,
                       ref: str, original: bool, case_seq: Any,
                       identity: Mapping[str, Any]) -> None:
        db.execute("INSERT INTO case_author(case_id,kind,ref,original,case_seq,actor,at,"
                   "identity_json) VALUES(?,?,?,?,?,?,?,?)",
                   (case_id, kind, ref, int(original),
                    case_seq if isinstance(case_seq, int) else None, actor_label(actor),
                    self._now().isoformat(timespec="microseconds"),
                    _canon({"sub": identity["sub"], "act": identity["act"]})))

    # ---- lifecycle ----------------------------------------------------

    def register(self, actor: Actor, case_id: str, proposal_id: str, *,
                 read: Callable[[str], Mapping[str, Any] | None] | None = None,
                 session: Any = None) -> dict[str, Any]:
        """Pin one readable, unchanged case proposal with an expected effect as an intent.

        ``session``: the host's ``SessionIdentity`` whose actor is ``actor``, required
        (``session_required``); the intent's ``sub``/``act`` come from it alone (a
        session naming no subject: unknown subject, so the intent never gets an MC
        gate). A repeat from a session whose subject is not yet
        recorded appends ``intent.co_requested``: every registering subject is a
        subject of the intent (and an unknown one makes the subject unknown). On an
        authorized intent not yet dispatched it also records the decision as withdrawn
        (``CO_REQUESTED_AFTER_DECISION``, F7 ``approval_withdrawn``): the decider was
        never checked against the new subject, so nothing is dispatched.

        Deterministic intent id and idempotency key: a repeat returns the same
        intent (``duplicate: true``) and appends nothing.

        The one place a registration enforces the capability guards
        (``operating.capability_guards.check``, declared in the action catalog): they
        must hold on the cited rows, read through ``read`` (a host passes its
        current-content reader, ``capability_guards.current_reader``) or, without one,
        on the cited content exactly as cited (``CaseStore.proposal_evidence``: a
        citation whose content changed reads as unreadable, so its guards fail).
        """
        try:
            identity = intent_identity(session, actor)
        except SessionIdentityRefused as exc:
            raise ActionPathError(exc.reason) from None
        proposal = self._proposal(actor, case_id, proposal_id)
        if proposal["_case_status"] != "open":
            # Nothing on a closed case may start an action (a repeat included).
            raise ActionPathError("case_closed")
        if proposal.get("content"):
            raise ActionPathError("proposal_withheld")
        if proposal["capability"] in MC_CALL_CAPABILITIES:
            return self._record_intent(actor, self._mc_call_intent(actor, case_id, proposal_id,
                                                                   proposal, identity),
                                       identity)
        if proposal.get("citations_status") != "resolved":
            raise ActionPathError("proposal_citations_not_current")
        effect = proposal.get("expected_effect")
        if not effect:
            raise ActionPathError("proposal_has_no_expected_effect")
        try:
            checks, prose = goal2_preconditions.split(proposal.get("preconditions") or [])
        except ValueError as exc:
            raise ActionPathError(str(exc)) from None
        if not checks:
            raise ActionPathError("proposal_has_no_checkable_precondition")
        evidence = self.cases.proposal_evidence(actor, case_id, proposal_id)
        evaluated = goal2_preconditions.evaluate(checks, evidence)
        if not evaluated["holds"]:
            raise ActionPathError(evaluated["reason"] or "precondition_not_met")
        # Catalog hook: a known owner-system action type, typed parameters each
        # traced to cited evidence or the human-written case objective, an effect
        # that sets the traced value. Unknown and workbench-internal types never
        # become intents. Then the injection hook (argument provenance).
        # A byte-pinned document's case view is its capture's, which states no field: the
        # catalog and injection hooks read the documentFields row its pinned bytes state
        # for this reader now, through ``read`` (the row the capability guards check),
        # cut to the fields its profile proposes through this type.
        evidence = _document_rows(evidence, read, proposal["capability"])
        refusal = self.catalog.registration_refusal(proposal, evidence,
                                                    proposal["_objective"])
        if refusal is not None:
            raise ActionPathError(refusal)
        if read is None:
            cited = {item["handle"]: item["content"] for item in evidence}

            def read(handle: str) -> Mapping[str, Any] | None:
                content = cited.get(handle)
                if not isinstance(content, Mapping) or not isinstance(content.get("row"),
                                                                       Mapping):
                    return None
                return {"relation": content.get("relation"), "key": content.get("key"),
                        "row": content["row"]}
        verdict = capability_guards.check(
            proposal["capability"], proposal.get("payload") or {}, effect,
            [cite["handle"] for cite in proposal.get("citations") or ()], read)
        if not verdict["passed"]:
            raise ActionPathError(("capability_guard_failed:" + ",".join(
                item["id"] for item in verdict["failures"]) + ": "
                + verdict["failures"][0]["reason"])[:400])
        traced = self._injection_hook(actor, case_id, proposal_id, proposal, evidence)
        body = _proposal_body(proposal)
        digest = _sha({"case_id": case_id, "proposal_id": proposal_id, **body})
        intent_id = "act_" + digest[:24]
        capability = proposal["capability"]
        warnings = self._owner_warnings(actor, capability, effect, [
            cite["handle"] for cite in proposal.get("citations") or ()], read)
        owner = (REAL_CAPABILITIES[capability].owner_system if capability in REAL_CAPABILITIES
                 else "fixture" if capability.startswith("fixture.") else
                 capability.partition(".")[0])
        intent = ActionIntent(
            intent_id=intent_id, case_id=case_id, proposal_id=proposal_id,
            capability=capability, owner_system=owner, proposal_sha256=_sha(body),
            expected_effect={"relation": effect["relation"], "key": effect["key"],
                             "fields": list(effect["fields"]),
                             "values": effect_values(effect)},
            idempotency_key="wb-" + digest[:40],
            gate_reason=_gate_reason(intent_id, capability, effect, case_id, proposal_id,
                                     "; ".join(filter(None, (
                                         injection_guard.provenance_summary(traced["traces"]),
                                         _cited_files(traced["traces"], evidence)))),
                                     warnings),
            evidence=tuple({"handle": c["handle"], "digest": c["digest"]}
                           for c in proposal["citations"]),
            checked_preconditions=tuple(checks), human_preconditions=tuple(prose),
            parameter_trace={"objective_author_kind": traced.get("objective_author_kind"),
                             "traces": traced["traces"]},
            sub=identity["sub"],
            act=tuple(identity["act"]), owner_warnings=warnings)
        return self._record_intent(actor, intent, identity)

    def _owner_warnings(self, actor: Actor, capability: str, effect: Mapping[str, Any],
                        citations: list[str], read: Callable[[str], Any]
                        ) -> tuple[dict[str, Any], ...]:
        """The type's ``owner_warnings`` on the effect's cited target row and the owner's
        other records the actor may read (``owner_peers``). A warning never refuses: it
        is recorded on the intent and named in the gate reason the MC approver reads."""
        spec = self.catalog.real_capability(capability) or {}
        if not spec.get("owner_warnings"):
            return ()
        target = next((found for found in map(read, citations)
                       if isinstance(found, Mapping)
                       and found.get("relation") == effect["relation"]
                       and _canon(found.get("key")) == _canon(effect["key"])
                       and isinstance(found.get("row"), Mapping)), None)
        values = effect_values(effect)
        out = []
        for warning in spec["owner_warnings"]:
            status, count = "not_checked", None
            if self.owner_peers is not None and target is not None:
                try:
                    peers = self.owner_peers(actor, effect["relation"])
                    if peers is not None:   # None: the actor may not read the owner's records
                        count = action_adapters.reused_by(
                            warning, effect["key"], target["row"],
                            values.get(warning["field"]), list(peers))
                        status = "flagged" if count else "clear"
                except Exception:  # noqa: BLE001 - shown as a failed check, never hidden
                    _LOG.warning("owner warning check failed", exc_info=True)
                    status, count = "check_failed", None
            out.append({"id": warning["id"], "status": status, "count": count})
        return tuple(out)

    def _mc_call_intent(self, actor: Actor, case_id: str, proposal_id: str,
                        proposal: Mapping[str, Any],
                        identity: Mapping[str, Any]) -> ActionIntent:
        """The intent of an MC-owned proposal: the catalog hook (parameters typed and
        traced to the case's gaps or a declared set, its preconditions on the case) and
        any structured precondition on its citations. Its effect is the MC task the call
        creates under the intent's idempotency key; no row of a cited record is written."""
        if proposal.get("citations_status") not in ("resolved", "uncited"):
            raise ActionPathError("proposal_citations_not_current")
        refusal = self.catalog.registration_refusal(proposal, (), proposal["_objective"])
        if refusal is not None:
            raise ActionPathError(refusal)
        try:
            checks, prose = goal2_preconditions.split(proposal.get("preconditions") or [])
        except ValueError as exc:
            raise ActionPathError(str(exc)) from None
        if checks:
            evaluated = goal2_preconditions.evaluate(
                checks, self.cases.proposal_evidence(actor, case_id, proposal_id))
            if not evaluated["holds"]:
                raise ActionPathError(evaluated["reason"] or "precondition_not_met")
        entry = self.catalog.get(proposal["capability"])
        payload = proposal["payload"]
        body = _proposal_body(proposal)
        digest = _sha({"case_id": case_id, "proposal_id": proposal_id, **body})
        intent_id, key = "act_" + digest[:24], "wb-" + digest[:40]
        fields = list(entry["effect"]["field_parameters"])
        traces = {}
        for name, spec in entry["parameters"].items():
            trace = spec["trace"]
            traces[name] = ({"kind": "case_gap", "record": payload.get(trace["record"]),
                             "field": trace["field"]} if trace["kind"] == "case_gap"
                            else {"kind": trace["kind"]})
        return ActionIntent(
            intent_id=intent_id, case_id=case_id, proposal_id=proposal_id,
            capability=proposal["capability"], owner_system=entry["owner_system"],
            proposal_sha256=_sha(body),
            expected_effect={"relation": MC_CALL_RELATION, "key": {"idempotencyKey": key},
                             "fields": fields,
                             "values": {field: payload[entry["effect"]["field_parameters"][field]]
                                        for field in fields}},
            idempotency_key=key,
            gate_reason=(f"PLX Workbench action {intent_id}: {proposal['capability']} (case "
                         f"{case_id} {proposal_id}; one {entry['mc_call']['tool']} call after "
                         f"a per-call human approval of its exact arguments; read-back "
                         f"verified)"),
            evidence=tuple({"handle": c["handle"], "digest": c["digest"]}
                           for c in proposal.get("citations") or ()),
            checked_preconditions=tuple(checks), human_preconditions=tuple(prose),
            parameter_trace={"objective_author_kind":
                             proposal["_objective"].get("objective_author_kind"),
                             "traces": traces},
            sub=identity["sub"], act=tuple(identity["act"]))

    def _record_intent(self, actor: Actor, intent: ActionIntent,
                       identity: Mapping[str, Any]) -> dict[str, Any]:
        """Append ``intent.registered`` once; a repeat returns it (``duplicate: true``) and
        records a new subject as a co-requester (see ``register``). The case's claim is
        checked under its write lock, held until the intent commits: a claim taken by
        another session meanwhile waits and sees the intent, never the reverse."""
        intent_id = intent.intent_id
        with self._connect(write=True,
                           hold=self.cases.outside_write(actor, intent.case_id)) as db:
            exists = db.execute("SELECT 1 FROM action_event WHERE intent_id=? LIMIT 1",
                                (intent_id,)).fetchone()
            if exists:
                current = self._load(db, actor, intent_id)
                if _adds_subject(current, identity["sub"]):
                    self._append(db, intent_id, "intent.co_requested", actor,
                                 {"sub": identity["sub"], "act": identity["act"]},
                                 identity["act"])
                    decision = next((d for d in reversed(current["decisions"])
                                     if d["authorizes"]), None)
                    if current["state"] in _DISPATCHABLE and decision is not None:
                        # The decider was checked against the subjects as they were when
                        # MC decided, never against this one: the decision no longer
                        # authorizes and nothing is dispatched (decision g2-1). (An MC call
                        # has no decision: send_call re-checks its approver on the send.)
                        self._withdrawal(db, actor, intent_id, current, decision,
                                         (decision["gate_id"], CO_REQUESTED_AFTER_DECISION),
                                         identity["act"])
                return {**current["intent"], "duplicate": True}
            self._append(db, intent_id, "intent.registered", actor, asdict(intent),
                         identity["act"])
        return {**asdict(intent), "duplicate": False}

    def _injection_hook(self, actor: Actor, case_id: str, proposal_id: str,
                        proposal: Mapping[str, Any],
                        evidence: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Argument provenance (10-DECISION 7): every dispatched parameter traces to a
        typed cited value or a human-written case objective, else a refusal
        (``injection_guard.REFUSAL*``). ``evidence``: ``register``'s (document rows
        read for ``actor``). Returns the per-parameter trace."""
        checked = injection_guard.check_registration(self.cases, actor, case_id,
                                                     proposal_id, proposal, evidence=evidence)
        refusal = injection_guard.registration_refusal(self.cases, actor, case_id,
                                                       proposal_id, proposal, checked)
        if refusal:
            raise ActionPathError(refusal)
        return checked

    def prepare_gate(self, actor: Actor, intent_id: str, task_id: str,
                     mc: Any, *, session: Any = None) -> dict[str, Any]:
        """Record the MC approval request (``mc_request_approval{taskId, reason}``)
        that a human is asked to approve. Nothing is sent; see ``request_gate``.

        First MC is read through ``mc`` (``goal2_mc_approval.ground_task``, pack
        04 section 8 R3 step 1): MC must be live with its sync gate met, the task
        visible and open, and carry no gate with this action's reason that the
        intent does not track (a lost request that landed). A refusal raises and
        records nothing. The record names the exact call and carries the case,
        proposal digest, cited evidence digests, the MC task as read, the
        machine-checked preconditions (re-evaluated now) and the prose
        preconditions a person must confirm. After ``gate_request_lost`` it also
        names the lost attempt it supersedes.

        ``session`` (every step, required): the host's ``SessionIdentity`` for ``actor``;
        its ``act`` is stored on the event. No session (``session_required``) or another
        actor's session is refused before anything is read or recorded. Its subject is
        recorded as a co-requester (``_co_request``).
        """
        requester = _session_identity(session, actor)
        act = requester["act"]
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise ActionPathError("invalid_task_id")
        self._check_mc(mc)
        with self._connect() as db:
            state = self._load(db, actor, intent_id)
        if state["state"] not in _CALL_APPROVABLE:
            raise ActionPathError(f"invalid_state:{state['state']}")
        # No MC gate, and no MC read, for something that cannot run (hard rule): not a
        # cataloged real capability, or its allow switch is off.
        if production_target_refusal(self._intent(state), gate_request=True):
            raise ProductionTargetRefused(target_gates(self._intent(state), gate_request=True))
        # Nor for an intent no decision could authorize (decision g2-1).
        if not subject_identities(state):
            raise ActionPathError("subject_unknown")
        # No MC gate, and no MC read, for an intent that dispatch would refuse: a
        # closed case, a withheld or changed proposal, or cited evidence that is
        # no longer the content that was cited.
        stale = self._precondition_refusal(actor, self._intent(state))
        if stale:
            raise ActionPathError(stale)
        grounding = ground_task(mc, task_id, state["intent"]["gate_reason"], state["gate_ids"])
        if grounding["refusal"]:
            raise ActionPathError(grounding["refusal"])
        grounding["record"]["read_at"] = self._now().isoformat(timespec="microseconds")

        def make(current: dict[str, Any]) -> tuple[str, Mapping[str, Any]]:
            intent = current["intent"]
            if current["gate_ids"] != state["gate_ids"]:
                raise ActionPathError("intent_changed_during_mc_read")
            stale = self._precondition_refusal(actor, self._intent(current))
            if stale:
                raise ActionPathError(stale)
            evaluated = goal2_preconditions.evaluate(
                intent["checked_preconditions"],
                self.cases.proposal_evidence(actor, intent["case_id"], intent["proposal_id"]))
            if not evaluated["holds"]:
                raise ActionPathError(evaluated["reason"] or "precondition_not_met")
            human = list(intent["human_preconditions"])
            lost = (current.get("gate") or {}).get("attempt_id") \
                if current["state"] == "gate_request_lost" else None
            if lost:
                human.append(f"An earlier request for this action (attempt {lost}) got no "
                             f"answer and {task_id} showed no gate for it after it settled. "
                             f"If a gate with this reason appears on {task_id} later, reject "
                             f"it in Mission Control.")
            request = prepare_request(intent, task_id,
                                      checked_preconditions=evaluated["results"],
                                      human_preconditions=human)
            return "gate.prepared", {**request, "mc_task": grounding["record"],
                                     "supersedes_attempt": lost}

        event = self._step(actor, intent_id, _CALL_APPROVABLE, make, act, requester)
        return {**event, "request": self.get(actor, intent_id)["prepared"]}

    def prepare_call(self, actor: Actor, intent_id: str, *,
                     session: Any = None) -> dict[str, Any]:
        """Record the exact MC call of an MC-owned intent for a person to approve.

        State ``call_approval_required``: the call (tool and every argument, built by the
        catalog's ``mc_call`` from the traced parameters, this path's
        ``mc_follow_up_bucket`` and operator, and the intent's idempotency key, reference
        and cited sources: ``cited_sources``, each record the proposal cites with its
        citation, and its text as the requester resolves it only where every member of the
        bucket's audience reads that exact content (``_audience_reads``; else the reason it
        is withheld), the description within MC's ``MC_TASK_DESCRIPTION_MAX``) is on the
        intent for the approver to read; a reader who cannot resolve a citation sees the
        intent withheld (``public_view``). Nothing is read from or sent to MC. Refused,
        recording nothing: another type (``not_an_mc_call``), an unknown subject
        (``subject_unknown``: no approval could ever count), a closed case or changed
        evidence (``cited_source_unavailable`` included), a description over MC's limit
        (``mc_description_too_long``) or missing configuration
        (``mc_operator_not_configured``, ``mc_follow_up_bucket_not_configured``). The
        session's subject is recorded as a co-requester (``_co_request``).
        """
        requester = _session_identity(session, actor)
        act = requester["act"]

        def make(current: dict[str, Any]) -> tuple[str, Mapping[str, Any]]:
            intent = self._intent(current)
            if intent.capability not in MC_CALL_CAPABILITIES:
                raise ActionPathError("not_an_mc_call")
            if not subject_identities(current):
                raise ActionPathError("subject_unknown")
            stale = self._precondition_refusal(actor, intent)
            if stale:
                raise ActionPathError(stale)
            if self.mc_operator is None:
                raise ActionPathError("mc_operator_not_configured")
            if self.mc_follow_up_bucket is None:
                raise ActionPathError("mc_follow_up_bucket_not_configured")
            proposal = self._proposal(actor, intent.case_id, intent.proposal_id)

            def build(sources: str) -> dict[str, Any]:
                return self.catalog.mc_call(
                    intent.capability, proposal["payload"],
                    host={"mc_follow_up_bucket": self.mc_follow_up_bucket,
                          "mc_operator": self.mc_operator},
                    intent={"idempotency_key": intent.idempotency_key,
                            "cited_sources": sources,
                            "reference": (f"PLX Workbench case {intent.case_id}, action "
                                          f"{intent.intent_id}, key {intent.idempotency_key}.")})

            try:
                # The cited sources as this requester resolves them unchanged; a source's
                # text only where every audience member reads that same content.
                evidence = self.cases.proposal_evidence(actor, intent.case_id,
                                                        intent.proposal_id)
                withheld = audience_withholding(evidence, self._audience_reads(intent))
                # A one-character stand-in measures the rest of the description.
                rest = mc_length(build("-")["arguments"].get("description", "")) - 1
                call = build(cited_sources(evidence, withheld,
                                           limit=MC_TASK_DESCRIPTION_MAX - rest))
            except ActionRefused as exc:
                raise ActionPathError(exc.code) from None
            except ValueError as exc:
                raise ActionPathError(str(exc)) from None
            if mc_length(call["arguments"].get("description", "")) > MC_TASK_DESCRIPTION_MAX:
                raise ActionPathError("mc_description_too_long")
            return "call.prepared", {
                "schema": MC_CALL_SCHEMA, "tool": call["tool"], "arguments": call["arguments"],
                "arguments_sha256": _sha(call["arguments"]), "read_back": call["read_back"],
                "human_preconditions": list(intent.human_preconditions),
                "quoted_sources": [item["handle"] for item, reason in zip(evidence, withheld)
                                   if reason is None]}

        event = self._step(actor, intent_id, _CALL_PREPARABLE, make, act, requester)
        return {**event, "state": "call_approval_required",
                "request": self.get(actor, intent_id)["prepared"]}

    def approve_call(self, actor: Actor, intent_id: str, task_id: str | None,
                     record: Mapping[str, Any], *, session: Any = None,
                     shown_sha256: str | None = None,
                     approver_session: Any = None) -> dict[str, Any]:
        """Record a human's per-call approval of exactly the prepared MC call:
        ``mc_request_approval{taskId, reason}`` of a gate (``task_id``), or the prepared
        call of an MC-owned intent (``task_id`` None; ``prepare_call``).

        Record format (proposed, see GOAL2-ACTIONS.md): ``schema``, ``tool``,
        ``arguments`` (must equal the prepared call's: for a gate ``{"taskId": task_id,
        "reason": gate_reason}``), ``approved_by`` (a configured human approver),
        ``approved_at``, ``expires_at`` (ISO 8601 with offset), ``evidence_ref`` (where the
        human approval itself is kept). One record authorizes one call. The approver may
        not be a subject by canonical email or by their oid (``_call_approver_oid``;
        ``_sod_refusal``, on the committing write); an approver with no known oid is
        refused.

        ``shown_sha256`` / ``approver_session`` (``approve_in_session``, the console's
        door) only add checks, inside the same write: the call as shown to the person
        must still be the call approved (``call_approval_stale``), the session's own
        identities count for separation of duties, and its Entra oid must be the one the
        roster or configuration names for the approver
        (``call_approver_session_oid_mismatch``).
        """
        act = _session_act(session, actor)
        held = _approver_session_identities(approver_session)
        if task_id is not None and (not isinstance(task_id, str)
                                    or not _TASK_ID.fullmatch(task_id)):
            raise ActionPathError("invalid_task_id")
        if not isinstance(record, Mapping):
            raise ActionPathError("invalid_call_approval")

        with self._connect(write=True) as db:
            state = self._load(db, actor, intent_id)
            intent = self._intent(state)
            if intent.capability in MC_CALL_CAPABILITIES:
                if state["state"] != "call_approval_required":
                    raise ActionPathError("call_not_prepared" if state["state"] in
                                          _CALL_PREPARABLE else f"invalid_state:{state['state']}")
                if task_id is not None:
                    raise ActionPathError("mc_call_has_no_gate_task")
                tool, expected = state["prepared"]["tool"], state["prepared"]["arguments"]
            else:
                if state["state"] in _CALL_APPROVABLE:
                    raise ActionPathError("gate_request_not_prepared")
                if state["state"] != "gate_prepared":
                    raise ActionPathError(f"invalid_state:{state['state']}")
                if task_id is None:
                    raise ActionPathError("invalid_task_id")
                tool, expected = APPROVAL_REQUEST_TOOL, {"taskId": task_id,
                                                         "reason": intent.gate_reason}
                if _canon(state["prepared"]["arguments"]) != _canon(expected):
                    raise ActionPathError("call_approval_arguments_mismatch")
            if shown_sha256 is not None and shown_sha256 != call_approval_sha256(state):
                raise ActionPathError("call_approval_stale")
            if (record.get("schema") != CALL_APPROVAL_SCHEMA
                    or record.get("tool") != tool):
                raise ActionPathError("call_approval_names_another_call")
            if _canon(record.get("arguments")) != _canon(expected):
                raise ActionPathError("call_approval_arguments_mismatch")
            approver = str(record.get("approved_by") or "").strip()
            approver_oid = self._call_approver_oid(approver)
            refusal = _sod_refusal(state, "call_approver", approver, (approver_oid, *held))
            if refusal:
                raise ActionPathError(refusal)
            self._check_call_approval(approver, record.get("approved_at"),
                                      record.get("expires_at"), intent.capability)
            if approver_oid is None:   # SoD cannot match an alias without it
                raise ActionPathError(self._oid_refusal("call_approver", approver))
            if approver_session is not None and (
                    approver_session.subject_oid or "").lower() != approver_oid.lower():
                raise ActionPathError("call_approver_session_oid_mismatch")
            evidence = record.get("evidence_ref")
            if not isinstance(evidence, str) or not 1 <= len(evidence.strip()) <= 300:
                raise ActionPathError("call_approval_evidence_missing")
            try:  # the MC client's write authorization must accept it too (24 h max)
                authorization_for({"tool": tool, "arguments": expected,
                                   "approved_by": approver,
                                   "approved_at": record["approved_at"],
                                   "expires_at": record["expires_at"],
                                   "evidence_ref": evidence.strip()})
            except ValueError:
                raise ActionPathError("call_approval_not_a_valid_mc_write_authorization") from None
            # One human approval authorizes one call, on any intent: its identity
            # (approver, approval time, evidence) is consumed here, in the same
            # transaction as the event, and can never be presented again.
            identity = _sha({"approved_by": canonical_email(approver),
                             "approved_at": _time(record["approved_at"]).isoformat(),
                             "evidence_ref": evidence.strip()})
            if db.execute("SELECT 1 FROM consumed_call_approval WHERE approval_sha256=?",
                          (identity,)).fetchone():
                raise ActionPathError("call_approval_already_used")
            event = self._append(db, intent_id, "call.approved", actor, asdict(CallApproval(
                tool, expected, _sha(expected), approver,
                record["approved_at"], record["expires_at"], evidence.strip(), identity,
                approver_oid, self._call_approver_oid_provenance(approver))), act)
            db.execute("INSERT INTO consumed_call_approval VALUES(?,?,?)",
                       (identity, intent_id, event["at"]))
            return event

    def approve_in_session(self, actor: Actor, intent_id: str, *, session: Any,
                           shown_sha256: Any,
                           valid_for: timedelta = CONSOLE_APPROVAL_VALIDITY) -> dict[str, Any]:
        """The signed-in person's per-call approval of exactly the call they were shown
        (``call_approval_subject``; ``shown_sha256`` its digest), as ``approve_call``
        records any: the approver is the session's own person (never a record's text),
        approved now, for ``valid_for`` (at most 24 h), with the session and the shown
        digest as ``evidence_ref``. Only a person's session (no agent, no eval principal)
        whose email and Entra oid are known; every ``approve_call`` rule applies (an
        allowed approver of the type with that oid, never a subject, single use). It
        records the approval only: nothing is sent (``request_gate``/``send_call`` stay
        separate steps)."""
        approver = session_approver(session, actor)
        if not isinstance(shown_sha256, str) or not _SHA256_HEX.fullmatch(shown_sha256):
            raise ActionPathError("call_approval_subject_required")
        if not isinstance(valid_for, timedelta) or not (
                timedelta(0) < valid_for <= DEFAULT_DECISION_MAX_AGE):
            raise ActionPathError("call_approval_window_invalid")
        shown = self.view(actor, intent_id)   # the reader's view: a withheld one is unknown
        if shown.get("withheld"):
            raise IntentNotFound(intent_id)
        subject = shown.get("call_approval_subject")
        if subject is None:
            raise ActionPathError(f"invalid_state:{shown['state']}")
        arguments = subject["call"]["arguments"]
        task_id = (None if subject["action_type"] in MC_CALL_CAPABILITIES
                   else arguments.get("taskId") if isinstance(arguments, Mapping) else None)
        now = self._now().astimezone(timezone.utc)
        record = {"schema": CALL_APPROVAL_SCHEMA, "tool": subject["call"]["tool"],
                  "arguments": arguments, "approved_by": approver,
                  "approved_at": now.isoformat(), "expires_at": (now + valid_for).isoformat(),
                  "evidence_ref": (f"workbench-console:{session.channel}:session:"
                                   f"{session.session_ref}:shown:{shown_sha256}")}
        return self.approve_call(actor, intent_id, task_id, record, session=session,
                                 shown_sha256=shown_sha256, approver_session=session)

    def _call_approver_oid(self, approver: str) -> str | None:
        """The call approver's Entra oid: as configured, else the roster's usable now
        (None: unknown, or a host-resolved oid past its age limit)."""
        return (self._call_approver_oids.get(canonical_email(approver))
                or self._roster_oid(approver))

    def _roster_oid(self, email: str) -> str | None:
        """``email``'s roster oid usable now (``ApproverRoster.oid_of``). A Graph oid past
        its age limit re-reads the roster first (``approver_roster_reload``), and the
        re-read copy replaces the stale one; one that fails to load changes nothing."""
        now = self._now()
        if self._roster_reload is not None and self.approver_roster.oid_unverified(email, now):
            try:
                reread = self._roster_reload()
            except (ValueError, OSError):
                reread = None
            if type(reread) is ApproverRoster:
                self.approver_roster = reread
        return self.approver_roster.oid_of(email, now)

    def _standing_oid_refusal(self, state: Mapping[str, Any], role: str, email: str,
                              recorded: Any, provenance: Any,
                              recorded_at: Any) -> str | None:
        """Separation of duties and the oid of an approval or decision recorded earlier,
        re-checked on the committing write that acts on it (``request_gate``,
        ``send_call``, ``dispatch``), by the age rule ``approve_call``/``record_decision``
        apply: the oid usable now (``_call_approver_oid`` / ``_roster_oid``). An oid
        recorded from Graph (``graph_users``), or with a provenance this build does not
        know, stands only while that one is the same (``<role>_oid_unverified`` past the age
        limit after a re-read, ``<role>_oid_changed`` when the re-read names another); a
        committed oid, or none recorded before v2, stands as before: either one known
        counts. An oid recorded before provenance (no provenance) that is not the committed
        one (configured, or the roster's committed copy) was read from Graph: the same age
        limit, from ``recorded_at`` (the approval's or decision's own time: such a record
        holds no roster read time), past it ``<role>_oid_unverified:older_than_24h``."""
        current = (self._call_approver_oid(email) if role == "call_approver"
                   else self._roster_oid(email))
        oids = (recorded, current)
        refusal = _sod_refusal(state, role, email, oids)
        if refusal:
            return refusal
        if provenance is None and recorded and recorded != self._committed_oid(role, email):
            at = _time(recorded_at)
            if at is None or not approver_oid_fresh(
                    at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), self._now()):
                return f"{role}_oid_unverified:older_than_24h"
        if provenance is None or provenance == {"source": OID_COMMITTED}:
            return None if any(oids) else f"{role}_oid_unknown"
        if current is None:
            return self._oid_refusal(role, email)
        return None if current == recorded else f"{role}_oid_changed"

    def _committed_oid(self, role: str, email: str) -> str | None:
        """``email``'s committed oid: a configured call approver's, else the roster's
        committed copy (None: none, or the roster's is from Graph)."""
        configured = (self._call_approver_oids.get(canonical_email(email))
                      if role == "call_approver" else None)
        if configured:
            return configured
        if self.approver_roster.oid_provenance(email) == {"source": OID_COMMITTED}:
            return self.approver_roster.oid_of(email)
        return None

    def _call_approver_oid_provenance(self, approver: str) -> Mapping[str, str] | None:
        if self._call_approver_oids.get(canonical_email(approver)):
            return {"source": OID_COMMITTED}   # configured by the host from a person's list
        return self.approver_roster.oid_provenance(approver)

    def _oid_refusal(self, role: str, email: str) -> str:
        """Why ``email`` has no usable oid now: a roster oid gone stale is unverified."""
        stale = self.approver_roster.oid_unverified(email, self._now())
        return f"{role}_oid_unverified" if stale else f"{role}_oid_unknown"

    def _check_call_approval(self, approver: str, approved_at: Any, expires_at: Any,
                             action_type: str) -> None:
        """Approver of ``action_type`` (a configured call approver, else the roster's
        approver of that type) and validity window; checked on record and again before
        the call."""
        if not self.call_approvers and not self.approver_roster.members:
            raise ActionPathError("call_approvers_not_configured")
        if self.mc_operator is None:
            raise ActionPathError("mc_operator_not_configured")
        if (_SERVICE_LABEL.match(approver) or not _HUMAN_EMAIL.fullmatch(approver)
                or (canonical_email(approver) not in self.call_approvers
                    and not self.approver_roster.approves(approver, action_type))
                or canonical_email(approver) == self.mc_operator):
            raise ActionPathError("call_approver_not_allowed")
        start, end, now = _time(approved_at), _time(expires_at), self._now()
        if start is None or end is None or not start <= now < end:
            raise ActionPathError("call_approval_not_current")

    def request_gate(self, actor: Actor, intent_id: str, mc: Any, *,
                     session: Any = None) -> dict[str, Any]:
        """Send the one approved ``mc_request_approval`` call through ``mc``.

        ``mc`` must be a ``MissionControlMcpClient`` configured with our
        operator; it refuses the write unless opted in with this approver and
        an unused ledger entry. ``gate.request_started`` is committed first.
        Refusal -> ``gate_request_refused`` (certain: no gate). Any other
        failure, or a process that dies mid-send -> ``gate_request_unknown``
        (MC has no idempotency key; reconcile with ``reconcile_gate``). The session's
        subject is recorded as a co-requester first (``_co_request``), so the separation
        check below covers the person sending the request.
        """
        requester = _session_identity(session, actor)
        act = requester["act"]
        self._check_mc(mc)
        with self._connect(write=True) as db:
            state = self._load(db, actor, intent_id)
            if state["state"] != "call_approved":
                raise ActionPathError(f"invalid_state:{state['state']}")
            state = self._co_request(db, actor, intent_id, state, requester)
            approval = state["call_approval"]
            if approval["tool"] != APPROVAL_REQUEST_TOOL:
                raise ActionPathError("not_a_gate_request")   # an MC call: send_call
            call = approval["arguments"]
            if state.get("late_gate"):
                # An earlier request's gate landed after this round was prepared: a second
                # request would duplicate it. Certain refusal, nothing sent.
                return self._append(db, intent_id, "gate.request_refused", actor, {
                    "task_id": call["taskId"],
                    "reason": f"earlier_request_landed:{state['late_gate']}"}, act)
            stale = self._precondition_refusal(actor, self._intent(state))
            if stale:
                # The case closed or the cited evidence changed since the request was
                # prepared: no MC gate for an action dispatch would refuse. Certain
                # refusal, nothing sent; a new round needs a new proposal.
                return self._append(db, intent_id, "gate.request_refused", actor, {
                    "task_id": call["taskId"], "reason": stale}, act)
            # By email and oid: the recorded one and the one usable now (a call approval
            # recorded before the oid was required has none: refused).
            refusal = self._standing_oid_refusal(
                state, "call_approver", approval["approved_by"], approval.get("approver_oid"),
                approval.get("approver_oid_provenance"), approval.get("approved_at"))
            if refusal:
                # A subject recorded since the approval (a co-request) is the approver, or
                # is unknown: certain refusal, nothing sent.
                return self._append(db, intent_id, "gate.request_refused", actor, {
                    "task_id": call["taskId"], "reason": refusal}, act)
            try:  # the approval must still hold at the moment the call is sent
                self._check_call_approval(approval["approved_by"], approval["approved_at"],
                                          approval["expires_at"],
                                          state["intent"]["capability"])
                authorization = authorization_for(approval)
            except (ActionPathError, ValueError) as exc:
                return self._append(db, intent_id, "gate.request_refused", actor, {
                    "task_id": call["taskId"],
                    "reason": exc.code if isinstance(exc, ActionPathError) else str(exc)[:200]},
                    act)
            # Committed before any I/O (as dispatch.started): from here on a crash,
            # kill or lost answer reads as gate_request_unknown.
            attempt = f"{intent_id}-g{state['gate_attempts'] + 1}"
            self._append(db, intent_id, "gate.request_started", actor, {
                "task_id": call["taskId"], "attempt_id": attempt,
                "approval_sha256": approval["approval_sha256"],
                "request_sha256": approval["arguments_sha256"]}, act)
        # The SQLite lock is released here: no network I/O inside a transaction.
        outcome, detail = send_request(mc, call["taskId"], call["reason"], authorization)
        if outcome == "requested":
            kind, payload = "gate.requested", {"task_id": call["taskId"], "gate_id": detail,
                                               "reconciled": False,
                                               "request_sha256": approval["arguments_sha256"]}
        elif outcome == "refused":
            kind, payload = "gate.request_refused", {"task_id": call["taskId"], "reason": detail}
        else:
            kind, payload = "gate.request_unknown", {"task_id": call["taskId"], "gate_id": None,
                                                     "error": detail}
        payload["attempt_id"] = attempt

        with self._connect(write=True) as db:
            current = self._load(db, actor, intent_id)
            gate = current.get("gate") or {}
            same_attempt = gate.get("attempt_id") == attempt
            if current["state"] == "gate_request_unknown" and same_attempt:
                event = self._append(db, intent_id, kind, actor, payload, act)
                if kind == "gate.request_unknown":
                    # F7: the outcome is uncertain (a gate may exist); the safe step is a read.
                    self._append(db, intent_id, "recovery.recorded", actor, _gate_recovery(
                        "gate_request_unknown", call["taskId"], attempt, str(detail)[:200]),
                        act)
                return event
            if current["state"] == "gate_request_lost" and same_attempt \
                    and kind != "gate.request_unknown":
                # The send outlived the settle window and reconcile recorded it lost. A
                # definite answer is better evidence and is recorded.
                return self._append(db, intent_id, kind, actor, payload, act)
            if (current["state"] == "gate_requested" and same_attempt
                    and gate.get("gate_id") and (kind != "gate.requested"
                                                 or payload["gate_id"] == gate["gate_id"])):
                # reconcile_gate adopted this attempt's gate while we were sending: the
                # stored state is already right, so report it instead of an error.
                return {"intent_id": intent_id, "kind": "gate.requested",
                        "state": "gate_requested", "gate_id": gate["gate_id"],
                        "reconciled": True}
            if kind == "gate.request_unknown":
                return {"intent_id": intent_id, "kind": kind, "state": current["state"],
                        "recorded": False, "reason": "gate_request_attempt_superseded"}
            # A definite answer that arrives after the intent moved on (lost, then
            # re-prepared or re-approved; or adopted another gate) is never dropped: the
            # gate it names may be a duplicate, so it is recorded and blocks a new send.
            self._append(db, intent_id, "recovery.recorded", actor, _gate_recovery(
                "late_gate_answer", call["taskId"], attempt,
                "late_gate_written" if kind == "gate.requested" else payload.get("reason"),
                gate_id=payload.get("gate_id") if kind == "gate.requested" else None,
                state_when_answered=current["state"]), act)
            return {"intent_id": intent_id, "kind": kind, "state": current["state"],
                    "recorded": True, "reason": "late_answer_recorded",
                    "gate_id": payload.get("gate_id")}

    def reconcile_gate(self, actor: Actor, intent_id: str, mc: Any, *,
                       session: Any = None) -> dict[str, Any]:
        """Resolve an unknown gate request by reading the task (``mc_get_task``).

        Safe at any point; it never resends. In ``gate_requested`` it reads the task
        and records any untracked, not rejected gate carrying this action's reason
        (a duplicate, ``gate_duplicate``). In other states without an open
        request it reads and writes nothing (``nothing_to_reconcile``). From
        ``gate_request_unknown`` or ``gate_request_lost``, among the untracked gates
        with our exact reason: exactly one not rejected -> adopt it; only rejected
        ones -> adopt the newest this attempt could have written (requested at or
        after its start, or any once the send settled; ``record_decision`` records
        the rejection);
        several not rejected -> ``gate_ambiguous`` (a human rejects all but one in
        MC, then this adopts it). No gate is evidence of a lost request only once
        ``gate_send_settle`` has passed since ``gate.request_started``, whatever
        answer was recorded: a timeout or 5xx does not prove MC stopped working.
        Before that it writes nothing (``send_in_flight``); after it,
        ``gate.request_lost`` (a human may approve a new request). Each F7 record is
        written once per attempt (per gate id for duplicates).
        """
        act = _session_act(session, actor)
        self._check_mc(mc)
        with self._connect() as db:
            state = self._load(db, actor, intent_id)
        if state["state"] == "gate_requested":
            gate = state["gate"]
            untracked = self._note_untracked_gates(actor, intent_id, mc, gate["task_id"], act)
            return {"state": "gate_requested", "reconciled": False,
                    "reason": "gate_already_known", "gate_id": gate["gate_id"],
                    "matching_gates": None, "untracked_gates": untracked}
        if state["state"] not in _RECONCILABLE:
            return {"state": state["state"], "reconciled": False,
                    "reason": "nothing_to_reconcile", "matching_gates": None}
        task_id, reason = state["gate"]["task_id"], state["intent"]["gate_reason"]
        gates = _matching_gates(mc.get_task(task_id)["data"]["task"], reason,
                                state["gate_ids"])  # an earlier round's gate is not ours
        standing = [g for g in gates if g.get("status") != "rejected"]
        # A rejected gate is adopted (so record_decision records the rejection) only if
        # it can be this attempt's own: MC stamps requestedAt when it writes the gate
        # (PLX_MC src/lib/compliance/approvals.ts requestApprovalGate @ 1fba083), so a
        # gate requested before this send started is an older one. Until the send has
        # settled, such a gate is not adopted: this attempt's gate may still land, and
        # adopting the old one would leave it untracked.
        own_rejected = [g for g in gates if g.get("status") == "rejected"
                        and self._may_be_this_attempts(g, state["gate"])]
        pick = (standing[0] if len(standing) == 1 else
                max(own_rejected, key=lambda g: str(g.get("requestedAt") or ""))
                if own_rejected and not standing else None)
        if pick is not None:
            def make(current: dict[str, Any]) -> tuple[str, Mapping[str, Any]]:
                return "gate.requested", {"task_id": task_id, "gate_id": pick["id"],
                                          "reconciled": True,
                                          "attempt_id": current["gate"].get("attempt_id"),
                                          "matching_gates": len(gates),
                                          "gate_status": pick.get("status")}

            try:
                event = self._step(actor, intent_id, _RECONCILABLE, make, act)
            except ActionPathError as exc:
                if not exc.code.startswith("invalid_state:"):
                    raise
                # The send's own answer got there first: report the stored state.
                current = self.get(actor, intent_id)
                return {"state": current["state"], "reconciled": False,
                        "reason": "outcome_recorded_meanwhile", "matching_gates": len(gates)}
            return {**event, "state": "gate_requested", "reconciled": True,
                    "gate_id": pick["id"], "matching_gates": len(gates)}

        with self._connect(write=True) as db:
            current = self._load(db, actor, intent_id)
            gate = current.get("gate") or {}
            attempt = gate.get("attempt_id")
            result = {"state": current["state"], "reconciled": False,
                      "matching_gates": len(gates)}
            if current["state"] not in _RECONCILABLE or attempt != state["gate"].get(
                    "attempt_id"):
                return {**result, "reason": "outcome_recorded_meanwhile"}
            if standing:  # several gates still stand: a human picks one in MC
                if not any(entry.get("trigger") == "gate_ambiguous"
                           and entry.get("attempt_id") == attempt
                           for entry in current["recovery"]):
                    self._append(db, intent_id, "recovery.recorded", actor, _gate_recovery(
                        "gate_ambiguous", task_id, attempt, "several_gates_not_rejected",
                        matching_gates=len(gates),
                        gate_ids=sorted(g["id"] for g in standing)), act)
                return {**result, "reason": "ambiguous_gates"}
            started = _time(gate.get("started_at"))
            if started is None or self._now() - started < self.gate_send_settle:
                # The send may still be running at MC, whatever answer we got: an
                # absent gate is not yet evidence.
                return {**result, "reason": "send_in_flight", "send_in_flight": True}
            if not any(entry.get("trigger") == "gate_absent_after_request"
                       and entry.get("attempt_id") == attempt
                       for entry in current["recovery"]):
                detail = ("send_answered_unknown_past_settle_window"
                          if gate.get("send_finished")
                          else "send_unanswered_past_settle_window")
                self._append(db, intent_id, "recovery.recorded", actor, _gate_recovery(
                    "gate_absent_after_request", task_id, attempt, detail, matching_gates=0),
                    act)
                if current["state"] == "gate_request_unknown":
                    self._append(db, intent_id, "gate.request_lost", actor, {
                        "task_id": task_id, "attempt_id": attempt, "matching_gates": 0,
                        "reason": detail}, act)
                    result["state"] = "gate_request_lost"
            return {**result, "reason": "no_matching_gate"}

    def _may_be_this_attempts(self, mc_gate: Mapping[str, Any],
                              gate: Mapping[str, Any]) -> bool:
        """Could ``mc_gate`` have been written by the attempt ``gate`` records?"""
        started = _time(gate.get("started_at"))
        if started is None or self._now() - started >= self.gate_send_settle:
            return True  # settled: nothing of this attempt can still land
        requested = _time(mc_gate.get("requestedAt"))
        return requested is not None and requested >= started - _CLOCK_SKEW

    def _note_untracked_gates(self, actor: Actor, intent_id: str, mc: Any,
                              task_id: str, act: list[Any] | None) -> list[str]:
        """Read the task; record each untracked, not rejected gate with this action's
        reason once (``gate_duplicate``). A rejected one can never authorize."""
        with self._connect() as db:
            state = self._load(db, actor, intent_id)
        gates = _matching_gates(mc.get_task(task_id)["data"]["task"],
                                state["intent"]["gate_reason"], state["gate_ids"])
        stray = sorted(g["id"] for g in gates if g.get("status") != "rejected")
        if not stray:
            return []
        with self._connect(write=True) as db:
            current = self._load(db, actor, intent_id)
            noted = {entry.get("gate_id") for entry in current["recovery"]
                     if entry.get("trigger") == "gate_duplicate"}
            tracked = (current.get("gate") or {}).get("gate_id")
            for gate_id in stray:
                if gate_id not in noted and gate_id not in current["gate_ids"]:
                    self._append(db, intent_id, "recovery.recorded", actor, _gate_recovery(
                        "gate_duplicate", task_id, (current.get("gate") or {}).get(
                            "attempt_id"), f"untracked_gate_beside:{tracked}",
                        gate_id=gate_id), act)
        return stray

    def _check_mc(self, mc: Any) -> None:
        if not isinstance(mc, MissionControlMcpClient):
            raise TypeError("mc must be a MissionControlMcpClient")
        if self.mc_operator is None or \
                canonical_email(mc._config.operator_email) != self.mc_operator:
            raise ActionPathError("mc_operator_mismatch")

    def record_decision(self, actor: Actor, intent_id: str, mc: Any, *,
                        session: Any = None) -> dict[str, Any]:
        """Read MC's decision (``approval_decision``: a read) and record it.

        Separation of duties runs on the committing write (``_sod_refusal``): a decider
        who is a subject by email or by roster oid is ``decider_is_subject``, an intent
        with no known subject ``subject_unknown``; a roster member without an oid never
        authorizes (``decider_oid_unknown``), nor does an admin/owner the roster does not
        list for the intent's action type (``decider_not_approver_of_action_type``).
        """
        act = _session_act(session, actor)
        with self._connect() as db:
            state = self._load(db, actor, intent_id)
        if state["state"] != "gate_requested":
            raise ActionPathError(f"invalid_state:{state['state']}")
        self._check_mc(mc)
        gate, reason = state["gate"], state["intent"]["gate_reason"]
        action_type = state["intent"]["capability"]
        decision = mc.approval_decision(gate["task_id"], gate["gate_id"], expected_reason=reason)
        if (decision.get("task_id"), decision.get("gate_id")) != (gate["task_id"],
                                                                  gate["gate_id"]):
            raise ActionPathError("decision_for_another_gate")
        expected_sha = hashlib.sha256(reason.encode()).hexdigest()
        code = str(decision.get("reason_code") or "decision_unreadable")
        if code == "authorized" and (decision.get("authorizes") is not True
                                     or decision.get("reason_sha256") != expected_sha):
            code = "reason_mismatch"  # never trust an "authorized" for another reason
        decider = str(decision.get("decided_by") or "").strip()
        if code == "authorized" and (_SERVICE_LABEL.match(decider)
                                     or not _HUMAN_EMAIL.fullmatch(decider)):
            code = "decider_not_human"
        elif code == "authorized" and canonical_email(decider) == self.mc_operator:
            code = "self_decided"
        elif code == "authorized" and _time(decision.get("decided_at")) is None:
            code = "decided_at_missing"
        oid = self._roster_oid(decider) if decider else None
        roster = self.approver_roster   # the copy that oid came from (re-read if stale)
        role = roster.role_of(decider) if decider else None
        oid_refusal = self._oid_refusal("decider", decider) if decider else None
        # The same poll notes any duplicate gate for this action (a late landing).
        self._note_untracked_gates(actor, intent_id, mc, gate["task_id"], act)

        def decide(current: dict[str, Any]) -> tuple[str, Mapping[str, Any]]:
            # Everything the state guards is read from ``current``, inside the committing
            # write: a co-request recorded during the MC reads above is never missed.
            if (current["gate"]["task_id"], current["gate"]["gate_id"]) != (
                    gate["task_id"], gate["gate_id"]):
                raise ActionPathError("decision_for_another_gate")
            final = code
            if final == "authorized":   # fail closed: parked, never defaulted
                final = _sod_refusal(current, "decider", decider, (oid,)) or final
            if final == "authorized" and role is None:
                final = "decider_not_in_mc_roster"
            elif final == "authorized" and role not in DECIDING_ROLES:
                final = "decider_role_not_admin_or_owner"
            elif final == "authorized" and not roster.may_decide(decider, action_type):
                final = "decider_not_approver_of_action_type"
            elif final == "authorized" and oid is None:
                final = oid_refusal    # SoD cannot match an alias without it
            record = ApprovalRecord(
                gate["task_id"], gate["gate_id"], decision.get("status"),
                decision.get("decided_by"), decision.get("decided_at"),
                str(decision.get("reason_sha256")), final == "authorized", final,
                role, roster.sha256, roster.source_ref.strip() or None, oid,
                _subjects_sha256(current),
                roster.oid_provenance(decider) if oid is not None else None)
            return "gate.decision", asdict(record)

        return self._step(actor, intent_id, {"gate_requested"}, decide, act)

    def dispatch(self, actor: Actor, intent_id: str,
                 registry: DispatcherRegistry, mc: Any = None, *,
                 session: Any = None) -> dict[str, Any]:
        """Dispatch once, after an authorizing MC decision and current preconditions.

        Replay: ``dispatched``/``verified`` return the recorded dispatch;
        ``dispatched_unknown``/``escalated`` return ``reconcile_required``.
        Neither calls the dispatcher again.

        ``mc`` (the operator's ``MissionControlMcpClient``; required, else
        ``mc_recheck_required`` before anything is recorded, whenever the
        dispatcher wraps ``IsolatedOwnerSystem``): re-read the authorizing gate first (``authorization_still_current``, reads only).
        An approval that no longer stands is recorded as a non-authorizing
        decision (``approval_withdrawn:<why>``, state ``gate_not_authorizing``,
        F7 ``approval_withdrawn``) and nothing is dispatched; an unreadable MC is
        a certain ``dispatch_refused`` (``mc_unreadable``).

        Separation of duties is re-checked on the dispatch write itself, on the state
        as committed (``_sod_refusal``: the standing decision's decider against every
        subject, by email and by oid): a decider who is a subject, a decider with no
        known oid (a decision recorded before v2), or an intent with no known subject
        withdraws the decision (``approval_withdrawn:<code>``) and nothing is
        dispatched, however the decision came to be recorded.
        """
        act = _session_act(session, actor)
        if not isinstance(registry, DispatcherRegistry):
            raise TypeError("registry must be a DispatcherRegistry")
        recheck: tuple[str, str | None] | None = None
        if mc is not None:
            self._check_mc(mc)
            with self._connect() as db:
                before = self._load(db, actor, intent_id)
            if before["state"] in _DISPATCHABLE:
                standing = next(d for d in reversed(before["decisions"]) if d["authorizes"])
                recheck = (standing["gate_id"], authorization_still_current(
                    mc, standing, before["intent"]["gate_reason"], self.approver_roster,
                    action_type=before["intent"]["capability"], now=self._now()))
        with self._connect(write=True) as db:
            state = self._load(db, actor, intent_id)
            if state["intent"]["capability"] in MC_CALL_CAPABILITIES:
                raise ActionPathError("mc_call_is_sent_by_send_call")
            current = state["state"]
            if current in {"dispatched", "verified"}:
                return {"state": current, "replayed": True, "dispatch": state["dispatches"][-1]}
            if current == "failed":
                # Certainly not applied and the key is fenced: this intent is over. A new
                # attempt is a new proposal on re-read evidence, never a resend.
                return {"state": current, "replayed": True, "reconcile_required": False,
                        "dispatch": state["dispatches"][-1]}
            if current == "escalated" and not state["dispatches"]:
                # Escalated before anything was dispatched (an unknown gate request):
                # a human owns it now; nothing here can dispatch it.
                raise ActionPathError("escalated_before_dispatch")
            if current in {"dispatched_unknown", "escalated"}:
                last = state["dispatches"][-1]
                if not any(r.get("attempt_id") == last["attempt_id"] for r in state["recovery"]):
                    # e.g. the process died before the outcome was written: record it once.
                    self._append(db, intent_id, "recovery.recorded", actor, _recovery(
                        "dispatch_outcome_unknown", last, last.get("reason")), act)
                return {"state": current, "replayed": True, "reconcile_required": True,
                        "dispatch": last}
            if current not in _DISPATCHABLE:
                raise ActionPathError("gate_not_authorized")
            intent = self._intent(state)
            decision = next(d for d in reversed(state["decisions"]) if d["authorizes"])
            decider = str(decision.get("decided_by") or "")
            # A decision recorded before oids were required (pre-v2, the upgrade window)
            # names no decider oid: with none on the roster either it never authorizes.
            sod = self._standing_oid_refusal(state, "decider", decider,
                                             decision.get("decider_oid"),
                                             decision.get("decider_oid_provenance"),
                                             decision.get("decided_at"))
            if sod:
                return self._withdrawal(db, actor, intent_id, state, decision,
                                        (decision["gate_id"], "approval_withdrawn:" + sod), act)
            dispatcher = registry.for_capability(intent.capability)
            if (recheck is None and type(dispatcher) is FixtureDispatcher
                    and type(dispatcher.fixture) is IsolatedOwnerSystem):
                # The owner contract is dispatched only after a fresh MC re-read of the
                # approval (10-DECISION #6): a property of the path, not of the caller.
                raise ActionPathError("mc_recheck_required")
            withdrawn = self._withdrawal(db, actor, intent_id, state, decision, recheck, act)
            if withdrawn is not None:
                return withdrawn
            decided_at = _time(decision.get("decided_at"))
            now = self._now()
            if (decided_at is None  # a few minutes of clock skew are tolerated, no more
                    or not -_CLOCK_SKEW <= now - decided_at <= self.decision_max_age):
                self._append(db, intent_id, "decision.expired", actor,
                             {"gate_id": decision["gate_id"], "decided_at":
                              decision.get("decided_at"), "max_age_seconds":
                              int(self.decision_max_age.total_seconds())}, act)
                return {"state": "approval_expired", "replayed": False,
                        "reason": "decision_too_old", "business_system_writes": 0}
            attempt = f"{intent_id}-d{len(state['dispatches']) + 1}"
            # The hard rule, whatever was approved: a real system only when every gate
            # holds; the refusal names the unmet ones.
            real = type(dispatcher) is RealSystemDispatcher
            gates = target_gates(intent, adapter_registered=(
                real and dispatcher.capability == intent.capability),
                mc_read_back=recheck is not None,
                transport=dispatcher.adapter.transport if real else None)
            if gates:
                self._append(db, intent_id, "dispatch.refused", actor, {
                    "attempt_id": attempt, "idempotency_key": intent.idempotency_key,
                    "system": intent.owner_system, "receipt": None,
                    "reason": "production_target_refused", "unmet_gates": gates}, act)
                return {"state": "dispatch_refused", "replayed": False,
                        "reason": "production_target_refused", "unmet_gates": gates,
                        "business_system_writes": 0}
            kind = type(dispatcher)
            if kind not in (FixtureDispatcher, RefusingDispatcher, RealSystemDispatcher):
                raise ActionPathError("real_system_dispatch_not_enabled")
            # Class methods, never instance attributes (the dispatchers are sealed too).
            refusal = kind.refusal(dispatcher, intent) or self._precondition_refusal(actor, intent)
            if refusal:
                self._append(db, intent_id, "dispatch.refused", actor, {
                    "attempt_id": attempt, "idempotency_key": intent.idempotency_key,
                    "system": dispatcher.system, "receipt": None, "reason": refusal}, act)
                return {"state": "dispatch_refused", "replayed": False, "reason": refusal,
                        "business_system_writes": 0}
            self._append(db, intent_id, "dispatch.started", actor, {
                "attempt_id": attempt, "idempotency_key": intent.idempotency_key,
                "system": dispatcher.system}, act)
        # Committed before the call: a crash from here on reads as dispatched_unknown.
        try:
            receipt, outcome, reason = kind.dispatch(dispatcher, intent), "dispatched", None
        except DispatchRefused as exc:
            receipt, outcome, reason = None, "not_dispatched", str(exc)[:200]
        except OwnerRejected as exc:
            # The owner is certain it did not, and never will, apply this key.
            receipt, outcome, reason = None, "rejected", exc.code[:200]
        except Exception as exc:  # noqa: BLE001 - lost answer: outcome unknown
            receipt, outcome, reason = None, "dispatched_unknown", type(exc).__name__
        record = DispatchRecord(attempt, intent.idempotency_key, dispatcher.system, outcome,
                                receipt if isinstance(receipt, str) else None, reason)
        with self._connect(write=True) as db:
            escalated = self._load(db, actor, intent_id)["state"] == "escalated"
            self._append(db, intent_id, "dispatch.finished", actor, asdict(record), act)
            if escalated:
                # Escalated while the call was in flight: the late answer is recorded and
                # the intent stays escalated (a human owns it; never re-sent).
                self._append(db, intent_id, "recovery.recorded", actor, _recovery(
                    "late_dispatch_answer", asdict(record), reason or outcome), act)
            elif outcome != "dispatched":
                self._append(db, intent_id, "recovery.recorded", actor, _recovery(
                    {"not_dispatched": "dispatch_failed",
                     "rejected": "dispatch_rejected_by_owner"}.get(
                        outcome, "dispatch_outcome_unknown"), asdict(record), reason), act)
            # The reconciled state: a read-back may have concluded while we waited.
            state_name = self._load(db, actor, intent_id)["state"]
        return {"state": state_name, "replayed": False, "receipt": receipt, "reason": reason}

    def send_call(self, actor: Actor, intent_id: str, mc: Any, *,
                  session: Any = None) -> dict[str, Any]:
        """Send an MC-owned intent's approved call once through ``mc``'s write door.

        ``mc``: a ``MissionControlMcpClient`` with our operator that opted in to the
        call's tool; it refuses before I/O unless the approval names this exact call, its
        approver is one of its write approvers and its ledger records the single use.
        Before anything is sent this re-checks, on the committing write: the MC write
        door's hard guard (``mc_call_refusal``), the case and the traced parameters
        (``_precondition_refusal``), that every source the call quotes is still read by
        every member of the bucket's audience (``_audience_refusal``:
        ``cited_source_audience_changed``), separation of duties against every subject
        recorded now (``_sod_refusal``: an approver who is a subject, or with no known
        oid) and the approval window; a refusal is a certain ``dispatch_refused`` (nothing sent; a new
        call needs ``prepare_call`` and a new approval). ``dispatch.started`` is committed
        before the send: a lost answer reads as ``dispatched_unknown`` and is reconciled
        only by ``read_back_call``. Replay: ``dispatched``/``verified`` return the
        recorded send, ``dispatched_unknown``/``escalated`` ``reconcile_required``; MC is
        not called again (and MC itself returns the original task for our key).
        """
        act = _session_act(session, actor)
        self._check_mc(mc)
        with self._connect(write=True) as db:
            state = self._load(db, actor, intent_id)
            intent = self._intent(state)
            if intent.capability not in MC_CALL_CAPABILITIES:
                raise ActionPathError("not_an_mc_call")
            current = state["state"]
            if current in {"dispatched", "verified", "failed"}:
                return {"state": current, "replayed": True, "dispatch": state["dispatches"][-1]}
            if current in {"dispatched_unknown", "escalated"} and state["dispatches"]:
                return {"state": current, "replayed": True, "reconcile_required": True,
                        "dispatch": state["dispatches"][-1]}
            if current != "call_approved":
                raise ActionPathError(f"invalid_state:{current}")
            approval = state["call_approval"]
            attempt = f"{intent_id}-d{len(state['dispatches']) + 1}"
            refusal = (mc_call_refusal(intent, approval["tool"])
                       or self._precondition_refusal(actor, intent)
                       or self._audience_refusal(intent, state["prepared"])
                       or self._standing_oid_refusal(
                           state, "call_approver", approval["approved_by"],
                           approval.get("approver_oid"),
                           approval.get("approver_oid_provenance"),
                           approval.get("approved_at")))
            authorization = None
            if refusal is None:
                try:   # the approval must still hold at the moment the call is sent
                    self._check_call_approval(approval["approved_by"], approval["approved_at"],
                                              approval["expires_at"], intent.capability)
                    authorization = authorization_for(approval)
                except (ActionPathError, ValueError) as exc:
                    refusal = (exc.code if isinstance(exc, ActionPathError)
                               else str(exc)[:200])
            if refusal is not None:
                self._append(db, intent_id, "dispatch.refused", actor, {
                    "attempt_id": attempt, "idempotency_key": intent.idempotency_key,
                    "system": intent.owner_system, "receipt": None, "reason": refusal}, act)
                return {"state": "dispatch_refused", "replayed": False, "reason": refusal,
                        "mc_writes": 0}
            self._append(db, intent_id, "dispatch.started", actor, {
                "attempt_id": attempt, "idempotency_key": intent.idempotency_key,
                "system": intent.owner_system, "tool": approval["tool"],
                "approval_sha256": approval["approval_sha256"],
                "request_sha256": approval["arguments_sha256"]}, act)
        # Committed before the call; no SQLite lock is held across it.
        outcome, detail = send_create_task(mc, approval["arguments"], authorization)
        record = DispatchRecord(
            attempt, intent.idempotency_key, intent.owner_system,
            {"created": "dispatched", "refused": "not_dispatched",
             "rejected": "rejected"}.get(outcome, "dispatched_unknown"),
            detail if outcome == "created" else None,
            None if outcome == "created" else str(detail)[:200])
        with self._connect(write=True) as db:
            escalated = self._load(db, actor, intent_id)["state"] == "escalated"
            self._append(db, intent_id, "dispatch.finished", actor, asdict(record), act)
            if escalated:
                self._append(db, intent_id, "recovery.recorded", actor, _recovery(
                    "late_dispatch_answer", asdict(record), record.reason or record.outcome),
                    act)
            elif record.outcome != "dispatched":
                self._append(db, intent_id, "recovery.recorded", actor, _recovery(
                    {"not_dispatched": "mc_call_refused",
                     "rejected": "dispatch_rejected_by_owner"}.get(
                        record.outcome, "dispatch_outcome_unknown"), asdict(record),
                    record.reason), act)
            state_name = self._load(db, actor, intent_id)["state"]
        return {"state": state_name, "replayed": False, "task_id": record.receipt,
                "reason": record.reason}

    def read_back_call(self, actor: Actor, intent_id: str, mc: Any, *,
                       session: Any = None) -> dict[str, Any]:
        """Read the task an MC-owned intent's call created back (reads only) and record it.

        The task is the one MC's answer named or, after an unknown outcome, the one
        ``mc_search_tasks`` finds for the intent's key (the description carries it).
        ``verified`` only when ``mc_get_task`` shows every ``read_back`` argument of the
        sent call exactly; otherwise the state stays and an F7 record names the next step
        (read again, then a person; a task holding other values is flagged). Never sends.
        """
        act = _session_act(session, actor)
        self._check_mc(mc)
        with self._connect() as db:
            state = self._load(db, actor, intent_id)
        intent = self._intent(state)
        if intent.capability not in MC_CALL_CAPABILITIES:
            raise ActionPathError("not_an_mc_call")
        if state["state"] not in _READABLE_BACK:
            raise ActionPathError(f"invalid_state:{state['state']}")
        if not state["dispatches"]:
            raise ActionPathError("escalated_before_dispatch")
        sent = state["call_approval"]["arguments"]
        observed = read_created_task(mc, state["dispatches"][-1].get("receipt"),
                                     intent.idempotency_key,
                                     {name: sent[name] for name in state["prepared"]["read_back"]})
        if observed["outcome"] == "effect_observed" or observed["reason"] == "value_differs":
            observed["operation"] = {"status": "applied", "fenced": False,
                                     "receipt": observed["task_id"]}
        return {**self._record_read_back(actor, intent_id, state, observed, act),
                "task_id": observed["task_id"]}

    def _withdrawal(self, db: sqlite3.Connection, actor: Actor, intent_id: str,
                    state: Mapping[str, Any], decision: Mapping[str, Any],
                    recheck: tuple[str, str | None] | None,
                    act: list[Any] | None) -> dict[str, Any] | None:
        """Record what the pre-dispatch MC re-read found, if it stops the dispatch."""
        if recheck is None:
            return None
        attempt = f"{intent_id}-d{len(state['dispatches']) + 1}"
        gate_id, found = recheck
        if gate_id != decision["gate_id"]:
            found = "mc_recheck_raced"   # the state moved while MC was read: read again
        if found is None:
            return None
        if not found.startswith("approval_withdrawn:"):
            self._append(db, intent_id, "dispatch.refused", actor, {
                "attempt_id": attempt, "idempotency_key": state["intent"]["idempotency_key"],
                "system": state["intent"]["owner_system"], "receipt": None, "reason": found},
                act)
            return {"state": "dispatch_refused", "replayed": False, "reason": found,
                    "business_system_writes": 0}
        record = ApprovalRecord(
            decision["task_id"], decision["gate_id"], None, decision.get("decided_by"),
            decision.get("decided_at"), str(decision.get("reason_sha256")), False, found,
            decision.get("decider_role"), self.approver_roster.sha256,
            self.approver_roster.source_ref.strip() or None, decision.get("decider_oid"),
            _subjects_sha256(state), decision.get("decider_oid_provenance"))
        self._append(db, intent_id, "gate.decision", actor, asdict(record), act)
        self._append(db, intent_id, "recovery.recorded", actor, _recovery(
            "approval_withdrawn", {"attempt_id": None,
                                   "idempotency_key": state["intent"]["idempotency_key"],
                                   "outcome": "not_dispatched"}, found), act)
        return {"state": "gate_not_authorizing", "replayed": False, "reason": found,
                "business_system_writes": 0}

    def _audience_reads(self, intent: ActionIntent
                        ) -> list[list[dict[str, Any]] | None] | None:
        """Each member's ``proposal_evidence`` for the configured bucket's audience: the
        same citation read (and, on a live host, the same employee gate) every ``plx.*``
        read passes, so no second permission path. ``None`` per member the read refuses
        (case or source not theirs); ``None`` for the whole when there is no audience, it
        names another bucket, a member is unknown, the employee gate cannot resolve a
        member's rights (``EmployeeRightsUnresolved``: a provider outage, not a refusal) or
        a read fails otherwise."""
        if self.mc_follow_up_audience is None or self.mc_follow_up_bucket is None:
            return None
        try:
            audience = self.mc_follow_up_audience(self.mc_follow_up_bucket)
        except Exception:  # noqa: BLE001 - an audience that cannot be read withholds text
            _LOG.warning("mc follow-up audience unreadable", exc_info=True)
            return None
        if (not isinstance(audience, McBucketAudience)
                or audience.bucket != self.mc_follow_up_bucket
                or any(member is None for member in audience.members)):
            return None
        reads: list[list[dict[str, Any]] | None] = []
        for member in audience.members:
            try:
                reads.append(self.cases.proposal_evidence(member, intent.case_id,
                                                          intent.proposal_id))
            except EmployeeRightsUnresolved:
                _LOG.warning("mc follow-up audience rights unresolved", exc_info=True)
                return None
            except (CaseNotFound, PermissionError):
                reads.append(None)   # the case, or the employee gate, refuses this member
            except Exception:  # noqa: BLE001 - rights not known: withhold, never quote
                _LOG.warning("mc follow-up audience read failed", exc_info=True)
                return None
        return reads

    def _audience_refusal(self, intent: ActionIntent,
                          prepared: Mapping[str, Any] | None) -> str | None:
        """At the send: every source the prepared call quotes is still read, unchanged, by
        every member of the bucket's audience as it is now; else nothing is sent."""
        quoted = set((prepared or {}).get("quoted_sources") or ())
        if not quoted:
            return None
        evidence = [{"handle": item["handle"], "cited_digest": item["digest"]}
                    for item in intent.evidence if item["handle"] in quoted]
        withheld = audience_withholding(evidence, self._audience_reads(intent))
        if len(evidence) != len(quoted) or any(withheld):
            return "cited_source_audience_changed"
        return None

    def _precondition_refusal(self, actor: Actor, intent: ActionIntent) -> str | None:
        try:
            proposal = self._proposal(actor, intent.case_id, intent.proposal_id)
        except (CaseNotFound, ActionPathError):
            return "proposal_not_readable"
        stale = _currency_refusal({"proposal_sha256": intent.proposal_sha256,
                                   "evidence": intent.evidence}, proposal,
                                  proposal["_case_status"])
        if stale:
            return stale
        if intent.capability in MC_CALL_CAPABILITIES:
            # Its parameters still trace to the case's gaps as the actor reads them now.
            refusal = self.catalog.registration_refusal(proposal, (), proposal["_objective"])
            if refusal is not None:
                return refusal
            if not intent.checked_preconditions:
                return None
        evaluated = goal2_preconditions.evaluate(
            intent.checked_preconditions,
            self.cases.proposal_evidence(actor, intent.case_id, intent.proposal_id))
        if not evaluated["holds"]:
            return evaluated["reason"] or "precondition_not_met"
        return None

    def read_back(self, actor: Actor, intent_id: str,
                  read_back: Callable[[ActionIntent], Mapping[str, Any]], *,
                  session: Any = None) -> dict[str, Any]:
        """Read the owning system back and record it on the case and the intent.

        ``effect_observed`` -> ``verified`` (from ``dispatched_unknown`` it is
        ``reconciled_by: read_back``); anything else leaves the state as it is.
        """
        act = _session_act(session, actor)
        with self._connect() as db:
            state = self._load(db, actor, intent_id)
        if state["intent"]["capability"] in MC_CALL_CAPABILITIES:
            raise ActionPathError("mc_call_is_read_back_by_read_back_call")
        if state["state"] not in _READABLE_BACK:
            raise ActionPathError(f"invalid_state:{state['state']}")
        if not state["dispatches"]:
            raise ActionPathError("escalated_before_dispatch")  # nothing was sent to read
        return self._record_read_back(actor, intent_id, state, read_back(self._intent(state)),
                                      act)

    def _record_read_back(self, actor: Actor, intent_id: str, state: Mapping[str, Any],
                          observed: Mapping[str, Any], act: list[Any]) -> dict[str, Any]:
        """Record one read-back on the intent (and, for an owner record, on the case)."""
        intent = self._intent(state)
        outcome, reason = observed.get("outcome"), observed.get("reason")
        if outcome not in ("effect_observed", "effect_not_observed", "unknown"):
            raise ActionPathError("invalid_read_back_outcome")
        last = state["dispatches"][-1]
        operation = observed.get("operation")
        if operation is not None:
            if not isinstance(operation, Mapping) or operation.get("status") not in (
                    "applied", "not_applied", "pending", "unknown"):
                raise ActionPathError("invalid_read_back_operation")
            # ``reason``: the owner's own word on the operation (why it waits, or why it
            # was not applied), so the action record shows what the owner's log says.
            operation = {"status": operation["status"], "fenced": operation.get("fenced") is True,
                         "receipt": operation.get("receipt")
                         if isinstance(operation.get("receipt"), str) else None,
                         "reason": operation["reason"][:200]
                         if isinstance(operation.get("reason"), str) else None}
        # Failed only on certainty: the effect is not on the record, the owner says our
        # key was not applied *and* is fenced (it can never apply later), and our own
        # dispatch outcome was unknown (an acknowledged dispatch it contradicts stays
        # a question for a human).
        concludes = ("failed" if outcome != "effect_observed" and operation is not None
                     and operation["status"] == "not_applied" and operation["fenced"]
                     and last["outcome"] == "dispatched_unknown" else None)
        if concludes:
            reason = "owner_reports_not_applied"
        attribution = ("idempotency_key" if outcome == "effect_observed" and operation
                       and operation["status"] == "applied" else "not_established")
        reconciled = ("read_back" if (outcome == "effect_observed" or concludes)
                      and state["state"] != "dispatched" else None)
        detail = {"schema": SCHEMA, "intent_id": intent_id, "fields": observed.get("fields", {}),
                  "dispatch_attempt": last["attempt_id"], "dispatch_outcome": last["outcome"],
                  "receipt": last.get("receipt"), "reconciled_by": reconciled,
                  "evidence": (EVIDENCE_LABEL if intent.owner_system == "fixture"
                               else observed.get("evidence")),
                  "attribution": attribution, "operation": operation, "concludes": concludes}
        receipt: Mapping[str, Any] = {}
        if intent.capability not in MC_CALL_CAPABILITIES:
            # An owner record is a cited row: the read-back is recorded on the case too.
            # An MC call created a record no case cites; it is recorded on the intent.
            target = self.cases.get(actor, intent.case_id)
            proposal = next((p for p in target["proposals"]
                             if p["id"] == intent.proposal_id), {})
            effect = proposal.get("expected_effect") or {}
            if not effect.get("target_citation"):
                raise ActionPathError("proposal_not_readable")
            # Never refused by another session's claim on the case: the write is sent.
            receipt = self.cases.verification(actor, intent.case_id, intent.proposal_id,
                                              outcome, reason, detail,
                                              [effect["target_citation"]],
                                              dispatched_read_back=True)
        record = VerificationRecord(outcome, reason, dict(observed.get("fields", {})),
                                    receipt.get("verification_id"), reconciled, attribution,
                                    operation, concludes)
        with self._connect(write=True) as db:
            current = self._load(db, actor, intent_id)
            if current["state"] not in _READABLE_BACK:
                raise ActionPathError(f"invalid_state:{current['state']}")
            self._append(db, intent_id, "readback.recorded", actor, asdict(record), act)
            if outcome != "effect_observed":
                trigger = ("owner_reports_not_applied" if concludes else
                           "effect_contradicted" if outcome == "effect_not_observed"
                           and reason in CONTRADICTING_READBACK_REASONS else
                           "effect_not_observed" if outcome == "effect_not_observed"
                           else "read_back_unknown")
                self._append(db, intent_id, "recovery.recorded", actor, _recovery(
                    trigger, last, reason, verification_id=receipt.get("verification_id")),
                    act)
        return {"state": "verified" if outcome == "effect_observed" else
                "failed" if concludes else state["state"], **asdict(record)}

    def escalate(self, actor: Actor, intent_id: str, note: str, *,
                 session: Any = None) -> dict[str, Any]:
        """Hand an unknown outcome to a human; it is never re-sent from here."""
        act = _session_act(session, actor)
        if not isinstance(note, str) or not 1 <= len(note.strip()) <= 1000:
            raise ActionPathError("invalid_note")
        return self._step(actor, intent_id,
                          {"dispatched_unknown", "dispatched", "gate_request_unknown",
                           "gate_request_lost"},
                          lambda current: ("recovery.escalated",
                                           {"note": note.strip(), "resend": False}), act)

    def escalate_to_mc(self, actor: Actor, intent_id: str, *, priority: str = "high",
                       session: Any = None,
                       read: Callable[[str], Mapping[str, Any] | None] | None = None
                       ) -> dict[str, Any]:
        """Hand an unknown or owner-refused outcome to a person as one named MC task (EXE-05).

        For an intent ``dispatched_unknown`` (escalated first, ``escalate``), ``escalated``
        or ``failed``: the case records one follow-up (the catalog's
        ``workbench.follow_up.record``; its ``missing`` names the action, its
        ``who_can_answer`` the target record's Portal Department, ``operating.answerers``) and one
        ``mc.task.create_follow_up`` proposal traced to it, registered and prepared as the
        exact ``mc_create_task`` call. That call then waits for a person's per-call
        approval (``approve_call``) and is sent once (``send_call``); nothing here approves
        or sends. A repeat finds the same follow-up, proposal and intent, so one outcome
        is never more than one MC task. An MC call's own outcome is not escalated to MC.
        ``read``: the host's current-content reader for ``actor``
        (``capability_guards.current_reader``), the one the follow-up's record and
        registration read its citations through, as ``plx.act`` does (``register``).
        Returns ``{"intent_id", "state", "gap_id", "escalated"}`` of the follow-up intent.
        """
        state = self.get(actor, intent_id)
        intent = state["intent"]
        if intent["capability"] in MC_CALL_CAPABILITIES:
            raise ActionPathError("mc_call_outcome_goes_to_a_person")
        if state["state"] not in _ESCALATED_TO_MC:
            raise ActionPathError(f"nothing_to_escalate:{state['state']}")
        if state["state"] == "dispatched_unknown":
            self.escalate(actor, intent_id, "outcome not observed by read-back; escalated as "
                          "an MC follow-up task", session=session)
        case_id = intent["case_id"]
        missing = (f"Unverified outcome of Workbench action {intent_id} "
                   f"({intent['capability']} on {intent['owner_system']})")
        case = self.cases.get(actor, case_id)
        gap = next((item for item in case["gaps"] if item.get("missing") == missing), None)
        # The follow-up cites the action's target record: the gap and the MC proposal
        # (which must cite every citation of its gap) both carry it.
        cited = [target for target in (
            (item.get("expected_effect") or {}).get("target_citation")
            for item in case["proposals"] if item["id"] == intent["proposal_id"]) if target]
        if gap is None:
            view = self.view(actor, intent_id)
            step = view["next_safe_step"]
            reasons = [str(item["reason"]) for item in view["recovery"] if item.get("reason")]
            why = (f"State {view['state']}, outcome {view['outcome']}: "
                   f"{'; '.join(reasons) or 'no reason recorded'}. Next: {step['step']} by "
                   f"{step['by']}: {step['detail']}")[:1900]
            recorded = self.execute_internal(actor, "workbench.follow_up.record", case_id, {
                "missing": missing, "why": why,
                "who_can_answer": answerers.for_citation(cited[0] if cited else None,
                                                         intent["owner_system"]),
                "state": "unknown", **({"citations": cited} if cited else {})},
                session=session, read=read)
            gap_id = recorded["receipt"]["gap_id"]
        else:
            gap_id, why = gap["id"], gap["why"]
        proposal = next((item for item in self.cases.get(actor, case_id)["proposals"]
                         if item.get("capability") == ESCALATION_CAPABILITY
                         and (item.get("payload") or {}).get("gap_id") == gap_id), None)
        proposal_id = proposal["id"] if proposal is not None else self.record_proposal(
            actor, case_id, ESCALATION_CAPABILITY, {"gap_id": gap_id, "title": missing,
                                                    "description": why,
                                                    "priority": priority},
            citations=cited, session=session)["proposal_id"]
        follow_up = self.register(actor, case_id, proposal_id, session=session,
                                  read=read)["intent_id"]
        if self.get(actor, follow_up)["state"] == "proposed":
            self.prepare_call(actor, follow_up, session=session)
        return {"intent_id": follow_up, "state": self.get(actor, follow_up)["state"],
                "gap_id": gap_id, "escalated": self.get(actor, intent_id)["state"]}

    def execute_internal(self, actor: Actor, action_id: Any, case_id: str,
                         parameters: Mapping[str, Any], *, session: Any = None,
                         read: Any = None, schema: Any = None) -> dict[str, Any]:
        """One workbench-internal action (``ActionCatalog.execute`` over this path's case
        store), recorded with its ``sub``/``act`` from ``session`` (decision g2-1).

        A session that is not ``actor``'s is refused before anything is written. The
        audit row (``started``, with ``sub``/``act``) is committed before the case-store
        write, so a crash never loses it (the attempt then reads ``unknown``); the
        outcome row (``executed`` / ``refused``) follows. The returned record names
        whether a subject is known and who acted, never the subject's identities.
        """
        try:
            identity = intent_identity(session, actor)
        except SessionIdentityRefused as exc:
            raise ActionRefused(exc.reason) from None
        attempt = _sha([case_id, str(action_id), _canon(parameters), actor_label(actor),
                        self._now().isoformat(timespec="microseconds")])[:24]
        self._internal_row(attempt, "started", case_id, action_id, actor, identity, None)
        try:
            record = self.catalog.execute(actor, action_id, case_id, parameters,
                                          stores={"cases": self.cases}, read=read,
                                          schema=schema, author_kind=session.author_kind)
        except ActionRefused as exc:
            self._internal_row(attempt, "refused", case_id, action_id, actor, identity,
                               {"code": exc.code})
            raise
        self._internal_row(attempt, "executed", case_id, action_id, actor, identity, record)
        return {**record, "subject_known": bool(_identities_of(identity["sub"])),
                "act": [{key: item.get(key) for key in ("kind", "channel")}
                        for item in identity["act"] or ()]}

    def _internal_row(self, attempt: str, kind: str, case_id: str, action_id: Any,
                      actor: Actor, identity: Mapping[str, Any], record: Any) -> None:
        with self._connect(write=True) as db:
            db.execute("INSERT INTO internal_action(attempt_id,kind,case_id,action_id,actor,"
                       "at,identity_json,record_json) VALUES(?,?,?,?,?,?,?,?)",
                       (attempt, kind, case_id, str(action_id), actor_label(actor),
                        self._now().isoformat(timespec="microseconds"), _canon(identity),
                        _canon(record)))

    def internal_actions(self, actor: Actor, case_id: str) -> list[dict[str, Any]]:
        """The internal-action attempts of a case ``actor`` may read (host and tests):
        one per ``started`` row, its outcome ``executed`` / ``refused`` or ``unknown``
        (no outcome row: the process died during the case-store write)."""
        self.cases.get(actor, case_id)   # CaseNotFound when not visible
        with self._connect() as db:
            rows = db.execute("SELECT * FROM internal_action WHERE case_id=? ORDER BY seq",
                              (case_id,)).fetchall()
        attempts: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row["kind"] == "started":
                attempts[row["attempt_id"]] = {
                    "attempt_id": row["attempt_id"], "action_id": row["action_id"],
                    "actor": row["actor"], "at": row["at"],
                    **json.loads(row["identity_json"]), "outcome": "unknown", "record": None}
            elif row["attempt_id"] in attempts:
                attempts[row["attempt_id"]].update(outcome=row["kind"],
                                                   record=json.loads(row["record_json"]))
        return list(attempts.values())

    def get(self, actor: Actor, intent_id: str) -> dict[str, Any]:
        """The full folded state (internal: host code and tests). Readers outside
        the host use ``view`` / ``list_for_case``, which withhold what the actor's
        view of the proposal withholds."""
        with self._connect() as db:
            state = self._load(db, actor, intent_id)
        return state

    # ---- permission-aware reads (MCP tools, console) --------------------

    def view(self, actor: Actor, intent_id: str) -> dict[str, Any]:
        """One intent as ``actor`` may see it (see ``public_view``).

        An unknown intent and one whose case the actor cannot see are the same
        ``IntentNotFound``. Reads only this store and the case; never MC, a
        dispatcher or any owning system.
        """
        with self._connect() as db:
            state, case_view = self._load_viewed(db, actor, intent_id)
        return public_view(state, case_view)

    def list_for_case(self, actor: Actor, case_id: str) -> dict[str, Any]:
        """Every intent registered for one case, as ``actor`` may see each.

        The case is read for ``actor`` first (``CaseNotFound`` when not visible,
        before this store is read). An intent whose proposal is withheld from the
        actor shows its state only: no intent id, capability, effect,
        preconditions or records (the intent id is a digest of the proposal
        content). An intent whose event chain does not verify is listed as
        ``action_event_chain_broken`` with nothing read from it. Ordered by
        registration; at most ``MAX_LISTED_INTENTS`` (``truncated`` says so).
        Reads only this store and the case; never MC or any owning system.
        """
        if not isinstance(actor, Actor):
            raise ValidationError("actor_required")
        if not isinstance(case_id, str) or not case_id:
            raise ActionPathError("invalid_case_id")
        case_view = self.cases.get(actor, case_id)
        with self._connect() as db:
            ids = [row["intent_id"] for row in db.execute(
                "SELECT intent_id FROM action_event WHERE kind='intent.registered' AND "
                "intent_seq=1 AND json_extract(payload_json, '$.case_id')=? ORDER BY seq",
                (case_id,))]
            total = len(ids)
            intents = []
            for intent_id in ids[:MAX_LISTED_INTENTS]:
                rows = db.execute("SELECT * FROM action_event WHERE intent_id=? "
                                  "ORDER BY intent_seq", (intent_id,)).fetchall()
                state = self._fold_authored(db, rows)
                if state is None or not state.get("chain_ok"):
                    intents.append({"case_id": case_id, "state": None,
                                    "integrity": "action_event_chain_broken"})
                    continue
                intents.append(public_view(state, case_view))
        return {"case_id": case_id, "intents": intents, "total": total,
                "truncated": total > MAX_LISTED_INTENTS, "evidence_label": EVIDENCE_LABEL}


_NEXT_STEP = {
    "gate_request_unknown": "the request's outcome is unknown (a gate may exist): read the "
                            "task with reconcile_gate; never re-send this request",
    "gate_absent_after_request": "the task shows no gate for this request after the send "
                                 "settled: a human may approve a new request (prepare_gate "
                                 "reads the task again first); this one is never re-sent; if "
                                 "its gate appears later, reconcile_gate adopts it or a human "
                                 "rejects it in Mission Control",
    "gate_ambiguous": "several gates on the task carry this action's reason and none is "
                      "rejected: a human rejects all but one in Mission Control, then "
                      "reconcile_gate adopts the one that stands (record_decision re-checks "
                      "it); or escalate, after which the action needs a new proposal; never "
                      "re-send",
    "gate_duplicate": "a second gate with this action's reason is on the task (a late landing "
                      "of an earlier request): a human rejects it in Mission Control; only "
                      "the tracked gate can authorize this intent",
    "late_gate_answer": "an earlier request answered after this intent moved on: if it names "
                        "a gate, a human rejects that gate in Mission Control (a new request "
                        "is refused until then; prepare_gate ignores rejected gates)",
    "dispatch_failed": "certain_nothing_written: a retry is allowed only on a current "
                       "authorizing decision, with the same idempotency key",
    "mc_call_refused": "certain_nothing_written: Mission Control or the write door refused the "
                       "call before creating anything (see the reason); a new call needs "
                       "prepare_call and a new per-call human approval of the exact call (same "
                       "idempotency key, so Mission Control never creates a second task)",
    "dispatch_outcome_unknown": "read back the owning system; if the effect is not observed, "
                                "escalate to a human; never re-send",
    "late_dispatch_answer": "the dispatch answered after the intent was escalated: its answer "
                            "is recorded on the attempt and the intent stays with the human "
                            "(the write may have landed); a person reconciles in the owning "
                            "system; never re-send",
    "effect_not_observed": "read back again later (the source may lag), then escalate to a "
                           "human; never re-send",
    "read_back_unknown": "the owning record could not be inspected: escalate to a human",
    "effect_contradicted": "the owning record holds another value than the one dispatched, "
                           "or only part of the write: flagged. A person reconciles in the "
                           "owning system and records the hand-off (escalate); never re-send "
                           "and never re-propose without re-reading the record",
    "approval_withdrawn": "the approval no longer stands in Mission Control (see the "
                          "reason); nothing was dispatched. Only a new human-approved "
                          "request on a fresh Mission Control read can raise another gate",
    "dispatch_rejected_by_owner": "certain_nothing_written: the owning system refused this "
                                  "request and fenced its key; this intent never proceeds. "
                                  "Re-read the cited record and, if the action is still "
                                  "right, record a new proposal (a new MC gate follows)",
    "owner_reports_not_applied": "certain_nothing_written: after the unknown outcome the "
                                 "owning system reports the key not applied and fenced (it "
                                 "can never apply later); this intent never proceeds. Re-read "
                                 "the cited record and, if the action is still right, record a "
                                 "new proposal (a new MC gate follows); never re-send",
}


def _recovery(trigger: str, dispatch: Mapping[str, Any], reason: Any,
              **extra: Any) -> dict[str, Any]:
    """F7 recovery record: what is uncertain or failed and the one safe next step."""
    return {"trigger": trigger, "attempt_id": dispatch.get("attempt_id"),
            "idempotency_key": dispatch.get("idempotency_key"),
            "dispatch_outcome": dispatch.get("outcome"),
            "reason": str(reason)[:200] if reason is not None else None,
            "resend_allowed": trigger == "dispatch_failed",
            "next_step": _NEXT_STEP[trigger], **extra}


# The one safe next step per state, for readers (``public_view``). ``by`` names
# who may take it: ``host`` (Workbench host code configured by a person: MC reads,
# the one approved MC write, the sandbox dispatch), ``human`` (a configured
# approver, or a person reconciling in the owning system), ``mc_decider`` (an MC
# admin/owner in Mission Control) or ``none``. None of them is an agent tool.
_SAFE_STEP: Mapping[str, tuple[str, str, str]] = {
    "proposed": ("prepare_gate", "host",
                 "the host reads the Mission Control task and records the approval request "
                 "for a human to approve; nothing is sent"),
    "call_approval_required": ("approve_call", "human",
                               "a configured human call approver who is none of this action's "
                               "subjects approves the exact Mission Control call shown "
                               "(per-call approval record); nothing is sent until then"),
    "gate_prepared": ("approve_call", "human",
                      "a configured human approver approves the exact prepared "
                      "mc_request_approval call (per-call approval record)"),
    "call_approved": ("request_gate", "host",
                      "the host sends the one approved mc_request_approval, once"),
    "gate_requested": ("record_decision", "mc_decider",
                       "a Mission Control admin/owner decides the gate in Mission Control; "
                       "the host then reads and re-checks the decision"),
    "gate_request_refused": ("prepare_gate", "host",
                             "nothing was raised: a new request needs a fresh Mission Control "
                             "read and a new human per-call approval"),
    "gate_request_unknown": ("reconcile_gate", "host", _NEXT_STEP["gate_request_unknown"]),
    "gate_request_lost": ("prepare_gate", "host", _NEXT_STEP["gate_absent_after_request"]),
    "gate_authorized": ("dispatch", "host",
                        "the host dispatches to the isolated fixture sandbox only; every "
                        "production target is refused whatever was approved"),
    "gate_rejected": ("prepare_gate", "host",
                      "rejected in Mission Control: only a new human-approved request can "
                      "raise another gate"),
    "gate_not_authorizing": ("prepare_gate", "host",
                             "the decision does not authorize (see the decision reason): only "
                             "a new human-approved request can raise another gate"),
    "approval_expired": ("prepare_gate", "host",
                         "the Mission Control decision is older than the maximum age: a new "
                         "per-call approval and gate are required"),
    "dispatch_refused": ("dispatch", "host", _NEXT_STEP["dispatch_failed"]),
    "dispatched": ("read_back", "host", "read back the owning record"),
    "dispatched_unknown": ("read_back", "host", _NEXT_STEP["dispatch_outcome_unknown"]),
    "verified": ("none", "none", "the expected effect was observed on read-back "
                                 "(attribution not established)"),
    "escalated": ("reconcile_in_owning_system", "human",
                  "a person reconciles in the owning system; nothing is re-sent"),
    "failed": ("reverify_and_repropose", "human",
               "certainly not applied: re-read the cited record and, if the action is still "
               "right, record a new proposal; nothing is re-sent"),
}
# An MC-owned intent (``MC_CALL_CAPABILITIES``) takes no MC gate: its steps.
_MC_CALL_STEP: Mapping[str, tuple[str, str, str]] = {
    "proposed": ("prepare_call", "host",
                 "the host records the exact Mission Control call for a person to approve; "
                 "nothing is read from or sent to Mission Control"),
    "call_approved": ("send_call", "host",
                      "the host sends the one approved call once through the Mission Control "
                      "write door"),
    "dispatch_refused": ("prepare_call", "host",
                         "nothing was written: a new call needs prepare_call and a new per-call "
                         "human approval of the exact call (same idempotency key)"),
    "dispatched": ("read_back_call", "host",
                   "read the created task back with mc_get_task"),
    "dispatched_unknown": ("read_back_call", "host",
                           "find the task by this action's key (mc_search_tasks, mc_get_task); "
                           "if it is not observed, escalate to a human; never re-send"),
}
# A decision refused for separation of duties (decision g2-1): the intent stays parked.
_DECIDED_BY_SUBJECT = ("prepare_gate", "host",
                       "decided by the employee this action is for: only a new "
                       "human-approved request, decided by a different Mission Control "
                       "admin/owner, can authorize it")
_SUBJECT_STEP = {
    "decider_is_subject": _DECIDED_BY_SUBJECT,
    "approval_withdrawn:decider_is_subject": _DECIDED_BY_SUBJECT,
    CO_REQUESTED_AFTER_DECISION: ("prepare_gate", "host",
                                  "another employee co-requested this action after the "
                                  "decision, so the decider was never checked against them: "
                                  "only a new human-approved request, decided by a Mission "
                                  "Control admin/owner who is none of its subjects, can "
                                  "authorize it"),
}
# An intent with an unknown subject never gets an MC gate: parked before any request.
_NEEDS_SUBJECT = ("needs_session_subject", "human",
                  "needs a session subject: a registering session named no authenticated "
                  "subject employee, or the case's opener or the proposal's author was not "
                  "recorded with one, so no MC gate is requested and no decision can "
                  "authorize it; if the action still matters, employee sessions record "
                  "and register a new proposal (on a case opened through the action path "
                  "when the opener is the unknown one)")
_NEEDS_SUBJECT_STATES = frozenset({"proposed", "call_approval_required", "gate_prepared",
                                   "call_approved",
                                   "gate_request_refused", "gate_request_lost", "gate_rejected",
                                   "gate_not_authorizing", "approval_expired"})
# Past the gate request with an unknown subject (an intent recorded before its opener and
# author were, or an unknown subject recorded since): no decision on it can authorize a
# dispatch (``record_decision`` and ``dispatch`` refuse ``subject_unknown``).
_REREGISTER = ("reregister", "human",
               "needs re-registration: a subject of this intent is unknown (a registering "
               "session named no subject employee, or the case's opener or the proposal's "
               "author was not recorded with one), so no decision on it can authorize a "
               "dispatch and it never dispatches; if the action still matters, employee "
               "sessions record and register a new proposal through the action path")
_REREGISTER_STATES = frozenset({"gate_requested", "gate_authorized", "dispatch_refused"})
# After this many ``effect_not_observed`` read-backs on one intent the next safe step
# is a human escalation instead of another read (controller decision, review 4ed9adc).
READ_BACKS_BEFORE_ESCALATION = 3
_RECOVERY_LED = {"gate_request_unknown", "gate_request_lost", "dispatch_refused",
                 "dispatched_unknown", "failed"}
# Why an intent can no longer proceed, whatever its state says (``public_view``
# reads it from the case as the reader sees it now). Every one of them is also a
# refusal at prepare_gate, request_gate and dispatch (``_precondition_refusal``).
BLOCKERS = ("case_closed", "precondition_citation_changed", "proposal_changed_since_approval")
# States in which nothing has been sent to the owning system: a blocker ends them.
_BEFORE_DISPATCH = frozenset({"proposed", "call_approval_required", "gate_prepared",
                              "call_approved",
                              "gate_request_refused", "gate_request_lost", "gate_rejected",
                              "gate_not_authorizing", "approval_expired", "gate_authorized",
                              "dispatch_refused", "failed"})
_AFTER_DISPATCH = frozenset({"dispatched", "dispatched_unknown", "verified", "escalated"})
_STALE = ("the cited evidence is no longer the content this intent was registered on "
          "(a citation changed, or the proposal did): this intent never proceeds; re-read "
          "the cited source and, if the action is still right, record a new proposal and "
          "register it as a new intent")
_CLOSED = ("the case is closed: nothing on this intent proceeds; if the action still "
           "matters it needs a new case and proposal")
_PENDING_GATE = ("a Mission Control admin/owner rejects the pending gate in Mission Control "
                 "(a pending gate freezes the task's stage; this intent can never be "
                 "dispatched)")


def next_safe_step(state: Mapping[str, Any], blocked_by: str | None = None) -> dict[str, Any]:
    """The one safe next step for a folded intent state (never an agent tool).

    ``blocked_by`` (one of ``BLOCKERS``, from the case as read now) overrides a step
    that would only be refused: before dispatch the step is to re-read and propose
    again (stale evidence) or nothing (closed case); a pending gate is rejected in
    MC; an unknown gate request is still reconciled first (a read, never a send);
    after a dispatch on a closed case (no read-back can be recorded on it) a person
    reconciles in the owning system. A dispatched, verified or escalated intent on
    changed evidence is unaffected: its own write changes the cited content.
    """
    current = state["state"]
    step, by, detail = _SAFE_STEP[current]
    if (state.get("intent") or {}).get("capability") in MC_CALL_CAPABILITIES:
        step, by, detail = _MC_CALL_STEP.get(current, (step, by, detail))
    decisions = state.get("decisions") or []
    if current == "gate_not_authorizing" and decisions \
            and decisions[-1].get("reason_code") in _SUBJECT_STEP:
        step, by, detail = _SUBJECT_STEP[decisions[-1]["reason_code"]]
    if current in _NEEDS_SUBJECT_STATES and not subject_identities(state):
        step, by, detail = _NEEDS_SUBJECT
    recovery = state.get("recovery") or []
    if current in _RECOVERY_LED and recovery and recovery[-1].get("next_step"):
        detail = recovery[-1]["next_step"]
    resend = bool(recovery and recovery[-1].get("resend_allowed")
                  and current == "dispatch_refused")
    if current in _REREGISTER_STATES and not subject_identities(state):
        step, by, detail = _REREGISTER
        resend = False
        if current == "gate_requested":   # its pending gate freezes the MC task first
            step, by, detail = ("reject_gate_in_mc", "mc_decider",
                                f"{_PENDING_GATE}. Then: {_REREGISTER[2]}")
    if blocked_by is not None and blocked_by not in BLOCKERS:
        raise ValueError("unknown_blocker")
    if blocked_by != "case_closed" and current in _AFTER_DISPATCH:
        blocked_by = None  # the intent's own write is what changed the cited content
    closed = blocked_by == "case_closed"
    if blocked_by and current in _BEFORE_DISPATCH:
        step, by, detail = (("none", "none", _CLOSED) if closed else
                            ("reverify_and_repropose", "human", _STALE))
        resend = False
    elif blocked_by and current == "gate_requested":
        step, by, detail = ("reject_gate_in_mc", "mc_decider",
                            f"{_PENDING_GATE}. Then: {_CLOSED if closed else _STALE}")
    elif blocked_by and current == "gate_request_unknown":
        detail = (f"{detail}. Any gate it finds can never authorize this intent "
                  f"({blocked_by}): a Mission Control admin/owner rejects it")
    elif (current in {"dispatched", "dispatched_unknown"} and recovery
          and recovery[-1].get("trigger") == "effect_contradicted"):
        step, by, detail = ("escalate", "human", recovery[-1]["next_step"])
        resend = False
    elif (current in {"dispatched", "dispatched_unknown"}
          and sum(1 for item in recovery if item.get("trigger") == "effect_not_observed")
          >= READ_BACKS_BEFORE_ESCALATION):
        step, by, detail = ("escalate", "human",
                            f"{READ_BACKS_BEFORE_ESCALATION} read-backs have not observed the "
                            "effect: a person reconciles in the owning system and records "
                            "the hand-off (escalate); never re-send")
        resend = False
    elif closed and current in {"dispatched", "dispatched_unknown"}:
        step, by, detail = ("reconcile_in_owning_system", "human",
                            "the case is closed, so no read-back can be recorded on it: a "
                            "person checks the owning record and records the hand-off "
                            "(escalate); never re-send")
    return {"step": step, "by": by, "detail": detail, "agent_tool": None,
            "resend_allowed": resend, "blocked_by": blocked_by}


def outcome_of(state: Mapping[str, Any]) -> str:
    """The EXE-04 outcome of a folded intent (one of ``OUTCOMES``).

    ``not_dispatched``: nothing was sent to the owning system (before any dispatch,
    a certain refusal, an expired decision, an escalation before dispatch).
    ``unknown``: a dispatch was sent and its effect is not verified yet (an
    acknowledged dispatch awaiting read-back, a lost answer, an escalation after
    dispatch). ``verified``: the expected effect was observed on independent
    read-back. ``failed``: certainly not applied (the owner refused it, or reported
    the key not applied and fenced after an unknown outcome).
    """
    current = state.get("state")
    if current in ("verified", "failed"):
        return current
    if current in ("dispatched", "dispatched_unknown"):
        return "unknown"
    dispatches = state.get("dispatches") or ()
    if current == "escalated" and dispatches and dispatches[-1].get("outcome") in (
            "dispatched", "dispatched_unknown"):
        return "unknown"
    return "not_dispatched"


def blocker_for(intent: Mapping[str, Any], case_view: Mapping[str, Any]) -> str | None:
    """Why ``intent`` cannot proceed on ``case_view`` (the case as its reader sees it now)."""
    proposal = next((item for item in case_view.get("proposals") or ()
                     if item.get("id") == intent["proposal_id"]), None)
    found = _currency_refusal(intent, proposal, case_view.get("status"))
    return found if found in BLOCKERS else None


def _currency_refusal(intent: Mapping[str, Any], proposal: Mapping[str, Any] | None,
                      case_status: Any) -> str | None:
    """Is ``proposal`` (as read now) still what ``intent`` was registered on, on an open case?"""
    if case_status != "open":
        return "case_closed"
    if proposal is None:
        return "proposal_not_readable"
    if proposal.get("content"):
        return "proposal_withheld"
    if proposal.get("citations_status") != "resolved" and not (
            proposal.get("citations_status") == "uncited" and not intent.get("evidence")):
        return "precondition_citation_changed"
    if _sha(_proposal_body(proposal)) != intent["proposal_sha256"]:
        return "proposal_changed_since_approval"
    if [{"handle": c.get("handle"), "digest": c.get("digest")}
            for c in proposal.get("citations") or ()] != [
            {"handle": e["handle"], "digest": e["digest"]} for e in intent.get("evidence") or ()]:
        return "precondition_citation_changed"
    return None


def public_view(state: Mapping[str, Any], case_view: Mapping[str, Any]) -> dict[str, Any]:
    """An intent as the reader of ``case_view`` (the case read for them) may see it.

    Withheld proposal (any citation does not resolve for the reader): state
    only, never the intent id, capability, effect, gate reason, preconditions,
    MC ids or records, which all derive from the proposal content. Otherwise the
    full reader detail: intent, F7 recovery records, decisions (no decider
    identity), dispatches, read-backs, history and the next safe step.
    """
    intent = state["intent"]
    proposal = next((item for item in case_view.get("proposals") or ()
                     if item.get("id") == intent["proposal_id"]), None)
    if proposal is None or proposal.get("content") == WITHHELD:
        return {"case_id": intent["case_id"], "proposal_id": intent["proposal_id"],
                "state": state["state"], "withheld": True, "content": WITHHELD}
    gate = state.get("gate") or {}
    prepared = state.get("prepared")
    blocked_by = blocker_for(intent, case_view)
    return {
        "case_id": intent["case_id"], "proposal_id": intent["proposal_id"],
        "withheld": False, "intent_id": intent["intent_id"], "state": state["state"],
        "case_status": case_view.get("status"),
        "citations_status": proposal.get("citations_status"),
        "capability": intent["capability"], "owner_system": intent["owner_system"],
        "expected_effect": intent["expected_effect"], "gate_reason": intent["gate_reason"],
        "idempotency_key": intent["idempotency_key"],
        "evidence": [dict(item) for item in intent.get("evidence") or ()],
        "checked_preconditions": [dict(item) for item in
                                  intent.get("checked_preconditions") or ()],
        "human_preconditions": list(intent.get("human_preconditions") or ()),
        "owner_warnings": [dict(item) for item in intent.get("owner_warnings") or ()],
        # Whether a subject is recorded and who acted; never the subject's identities.
        "subject_known": bool(subject_identities(state)),
        "act": [{key: item.get(key) for key in ("kind", "channel")}
                for item in intent.get("act") or ()],
        # v2 act names its client and session; an earlier intent's act reads unknown.
        "act_known": bool(intent.get("act")) and all(
            item.get("client") and item.get("session") for item in intent.get("act") or ()),
        "production_guard": (mc_call_refusal(ActionIntent(**intent))
                             if intent["capability"] in MC_CALL_CAPABILITIES
                             else production_target_refusal(ActionIntent(**intent))),
        "unmet_gates": ([] if intent["capability"] in MC_CALL_CAPABILITIES
                        else target_gates(ActionIntent(**intent))),
        # The exact call a person approves (an MC call stays shown after it is sent).
        "prepared_request": ({"tool": prepared.get("tool"),
                              "arguments": prepared.get("arguments"),
                              "sent": bool(state.get("dispatches")) and state["dispatches"][-1]
                              .get("outcome") != "not_dispatched"}
                             if prepared else None),
        "call_approved": state.get("call_approval") is not None,
        # Exactly what a person approves per call, and its digest the approval names.
        "call_approval_subject": call_approval_subject(state),
        "call_approval_sha256": call_approval_sha256(state),
        "gate": ({key: gate.get(key) for key in ("task_id", "gate_id", "attempt_id")}
                 if gate else None),
        "gate_request_attempts": state.get("gate_attempts", 0),
        "decisions": [{key: item.get(key) for key in (
            "task_id", "gate_id", "status", "authorizes", "reason_code", "decided_at",
            "decider_role")} for item in state.get("decisions") or ()],
        "dispatches": [{key: item.get(key) for key in (
            "attempt_id", "outcome", "receipt", "reason")}
            for item in state.get("dispatches") or ()],
        "read_backs": [{key: item.get(key) for key in (
            "outcome", "reason", "reconciled_by", "attribution", "operation", "concludes")}
            for item in state.get("verifications") or ()],
        "outcome": outcome_of(state),
        "outcome_trail": [{key: item[key] for key in ("outcome", "state", "seq", "kind", "at")}
                          for item in state.get("outcome_trail") or ()],
        "state_trail": [dict(item) for item in state.get("state_trail") or ()],
        "recovery": [{key: value for key, value in item.items() if key != "binding_sha256"}
                     for item in state.get("recovery") or ()],
        "next_safe_step": next_safe_step(state, blocked_by),
        "history": [{"seq": item["seq"], "kind": item["kind"], "at": item["at"]}
                    for item in state.get("history") or ()],
        "chain_ok": state["chain_ok"], "evidence_label": EVIDENCE_LABEL}


def _identities_of(sub: Any) -> frozenset[str]:
    """One recorded ``sub``'s identities (canonical), empty when unknown."""
    found = sub.get("identities") if isinstance(sub, Mapping) else None
    if not isinstance(found, (list, tuple)):
        return frozenset()
    return frozenset(canonical_email(item) for item in found
                     if isinstance(item, str) and item.strip())


_AUTHORED = frozenset({"case.opened", "proposal.recorded"})


def subject_identities(state: Mapping[str, Any]) -> frozenset[str]:
    """Every subject of a folded intent, canonical: the case's opener and the
    proposal's author (``authors``, recorded by ``ActionPath.open_case`` /
    ``record_proposal``), the registering session and each co-requesting one. Empty
    (fail closed) when any of them is unknown, or when the case opening or the
    proposal has no original authoring record (its author was never recorded)."""
    authors = state.get("authors") or ()
    if not _AUTHORED <= {item.get("kind") for item in authors if item.get("original")}:
        return frozenset()
    subjects = state.get("subjects") or [{"sub": (state.get("intent") or {}).get("sub")}]
    found = [_identities_of(item.get("sub")) for item in (*subjects, *authors)]
    if not all(found):
        return frozenset()
    return frozenset().union(*found)


def _adds_subject(state: Mapping[str, Any], sub: Any) -> bool:
    """Whether a repeat registration's ``sub`` is not yet among the intent's requesting
    subjects (an author who registers too is recorded as a co-requester)."""
    new = _identities_of(sub)
    if not new:
        return all(_identities_of(item.get("sub")) for item in state["subjects"])
    known = frozenset().union(*(_identities_of(item.get("sub")) for item in state["subjects"]))
    return not new <= known


def call_approval_subject(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """What a person approves per call, exactly as shown before they click (None: the
    intent is not awaiting a per-call approval): the action type and owning system, its
    target, the parameters with the source each value was traced to, the expected
    effect, the cited evidence, the prose preconditions and the exact MC call. It is
    bound to the intent (``binding_sha256``) and to this prepared request
    (``prepared_sha256``): a re-prepared request or other intent is another subject."""
    prepared = state.get("prepared")
    if state.get("state") not in {"gate_prepared", "call_approval_required"} \
            or not isinstance(prepared, Mapping):
        return None
    intent = state["intent"]
    effect = intent.get("expected_effect") or {}
    traces = ((intent.get("parameter_trace") or {}).get("traces") or {})
    return {"schema": CALL_APPROVAL_SUBJECT_SCHEMA, "intent_id": intent["intent_id"],
            "binding_sha256": _binding(intent), "prepared_sha256": _sha(prepared),
            "action_type": intent["capability"], "owner_system": intent["owner_system"],
            "target": {"relation": effect.get("relation"), "key": effect.get("key")},
            "parameters": [{"name": name, "source": traces[name]} for name in sorted(traces)],
            "expected_effect": effect,
            "evidence": [{"handle": item.get("handle"), "digest": item.get("digest")}
                         for item in intent.get("evidence") or ()],
            "human_preconditions": list(intent.get("human_preconditions") or ()),
            "call": {"tool": prepared.get("tool"), "arguments": prepared.get("arguments")}}


def call_approval_sha256(state: Mapping[str, Any]) -> str | None:
    """Digest of ``call_approval_subject`` (None when there is none)."""
    subject = call_approval_subject(state)
    return None if subject is None else _sha(subject)


def session_approver(session: Any, actor: Actor) -> str:
    """The email of the person whose session approves: a person's own session (never an
    agent, never the eval principal) for ``actor``, with a known email and Entra oid."""
    from .session_identity import AUTHOR_HUMAN, SessionIdentity

    _session_act(session, actor)   # a session, this actor's, current
    if type(session) is not SessionIdentity:
        raise ActionPathError("session_identity_required")
    if session.author_kind != AUTHOR_HUMAN:
        raise ActionPathError("only_a_person_approves")
    if session.eval_principal:
        raise ActionPathError("eval_principal_never_approves")
    if not session.subject_oid:
        raise ActionPathError("call_approver_oid_unknown")
    if isinstance(session.email, str) and session.email.strip():
        return session.email.strip().lower()
    emails = [item for item in session.identities if "@" in item]
    if len(emails) != 1:
        raise ActionPathError("call_approver_email_unknown")
    return emails[0]


def _approver_session_identities(session: Any) -> tuple[str, ...]:
    """The approving session's own identities (email(s), Entra oid), for separation of
    duties; none without a session."""
    if session is None:
        return ()
    return tuple(sorted(getattr(session, "identities", ()) or ()))


def _sod_refusal(state: Mapping[str, Any], role: str, email: Any,
                 oids: Any = ()) -> str | None:
    """Separation of duties, the one check (decisions g2-1, goal2-identity): a call
    approver or decider (``role``) for an intent with an unknown subject
    (``subject_unknown``), or who is one of its subjects by canonical email or by any
    of their Entra ``oids`` (``<role>_is_subject``). Callers run it on the state read
    inside their committing write, so a co-request recorded meanwhile is never missed."""
    subjects = subject_identities(state)
    if not subjects:
        return "subject_unknown"
    held = {canonical_email(email)} | {oid.strip().lower() for oid in oids
                                       if isinstance(oid, str) and oid.strip()}
    if held & subjects:
        return f"{role}_is_subject"
    return None


def _subjects_sha256(state: Mapping[str, Any]) -> str:
    """Digest of the subject identities a decision was checked against."""
    return _sha(sorted(subject_identities(state)))


def _session_identity(session: Any, actor: Actor) -> dict[str, Any]:
    """The step's ``sub``/``act`` (``session_identity.intent_identity``), refused as
    ``_session_act`` refuses."""
    try:
        return intent_identity(session, actor)
    except SessionIdentityRefused as exc:
        raise ActionPathError(exc.reason) from None


def _session_act(session: Any, actor: Actor) -> list[Any]:
    """The ``act`` a step records: the host's ``SessionIdentity`` for ``actor``; no
    session (``session_required``), another actor's session, an expired one or anything
    else is refused."""
    try:
        return intent_identity(session, actor)["act"]
    except SessionIdentityRefused as exc:
        raise ActionPathError(exc.reason) from None


def _matching_gates(task: Any, reason: str, known: Any) -> list[dict[str, Any]]:
    """Gates on an MC task with exactly this reason that the intent does not track."""
    if not isinstance(task, Mapping):
        raise ActionPathError("mc_unreadable")
    return [g for g in task.get("approvalGates") or []
            if isinstance(g, dict) and g.get("reason") == reason
            and isinstance(g.get("id"), str) and _GATE_ID.fullmatch(g["id"])
            and g["id"] not in known]


def _gate_recovery(trigger: str, task_id: str, attempt_id: Any, reason: Any,
                   **extra: Any) -> dict[str, Any]:
    """F7 recovery record for the MC gate request (no dispatch exists yet)."""
    return {"trigger": trigger, "attempt_id": attempt_id, "task_id": task_id,
            "dispatch_outcome": None,
            "reason": str(reason)[:200] if reason is not None else None,
            "resend_allowed": False,
            "new_request_allowed": trigger == "gate_absent_after_request",
            "next_step": _NEXT_STEP[trigger], **extra}


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


__all__ = ["ActionIntent", "ActionPath", "ActionPathError", "ApprovalRecord", "CallApproval",
           "Capability", "DispatchRecord", "DispatchRefused", "Dispatcher",
           "DispatcherRegistry", "FIXTURE_CAPABILITIES", "ProductionTargetRefused",
           "RealSystemDispatcher", "target_gates", "FIXTURE_CAPABILITY", "FixtureDispatcher", "FixtureSystem",
           "IntentNotFound", "REAL_CAPABILITIES", "RefusingDispatcher",
           "BLOCKERS", "CALL_APPROVAL_SUBJECT_SCHEMA", "CONSOLE_APPROVAL_VALIDITY",
           "call_approval_sha256", "call_approval_subject", "session_approver", "MAX_LISTED_INTENTS", "READ_BACKS_BEFORE_ESCALATION", "STATES", "VerificationRecord", "blocker_for",
           "next_safe_step", "OUTCOMES", "outcome_of", "MC_CALL_CAPABILITIES", "mc_call_refusal",
           "production_target_refusal", "public_view"]
