"""Mission Control approval gate for goal-2 actions: request record, the one
send, its outcome, and the MC admin/owner roster that re-checks deciders.

The send itself is not here. ``mc_request_approval`` is an MC write and goes
only through ``integrations.mc.MissionControlMcpClient.call_write_tool``: the
client must have opted in to that tool (``write_tools``, empty by default),
the ``McWriteAuthorization`` must name exactly ``{taskId, reason}`` with a
configured human ``write_approvers`` approver who is not our operator, inside
its window, and the shared durable ``WriteAuthorizationLedger`` records the
single use before any I/O; the call is audited and sent once. This module
only builds that authorization from the action path's per-call record and
maps the result to a gate id, a certain refusal or an unknown outcome.

It is a production MC write: ``requestApprovalGate`` patches the task
(``approvalGates`` + an ``activity`` line, stage frozen until decided) and
appends ``approval.requested`` to ``mc_events``. Neither field is in
``PUSHED_FIELDS``, so it is not mirrored to the SharePoint ToDos list (PLX_MC
``src/lib/compliance/approvals.ts`` ``requestApprovalGate``, ``src/lib/mcp/approval-actions.ts``,
``src/lib/sync/state.ts:674-689`` ``PUSHED_FIELDS``/``patchTask`` @ ``2f653cf``). MC has no idempotency key for
it: a lost answer is reconciled by reading the task, never by sending again.

``send_create_task`` / ``read_created_task``: the one MC-owned action type
(``mc.task.create_follow_up``) sends ``mc_create_task`` through the same write door, once,
with the intent's idempotency key as MC's ``idempotencyKey`` (a repeat from our principal
returns the original task, ``replayed: true``; the same key with another payload is 409
``idempotency_key_reused``; a key whose first create was abandoned is 409
``idempotency_key_failed``: PLX_MC ``src/lib/mcp/task-create-idempotency.ts`` @ 073f192),
and reads the task back (``mc_get_task``; after a lost answer ``mc_search_tasks`` for the
key the description carries). Reads and the one send only; this module decides nothing.

``ApproverRoster``: MC decides gates only for a signed-in Entra session holding
``approval.decide`` (role ``admin`` or ``owner`` in ``mc_users``; services are
always denied: PLX_MC ``src/lib/permissions/grants.ts`` ROLE_GRANTS,
``src/lib/permissions/predicates.ts:30-38``; decider label = session email,
``src/lib/routing/mutations/actors.ts:60``). The gate records only the label,
not the role, and MC exposes ``mc_users`` to no MCP key. So the Workbench
re-checks the decider against a roster of MC admin/owner emails that a person
supplies with its provenance; an empty roster authorizes no one. Each member names the
action types it approves (``action_types``), one rule for every approver: a per-call
approver and an MC gate decider alike count only for an intent whose action type is
theirs (``ApproverRoster.approves``; a decider must also be an admin/owner,
``may_decide``), and an empty list approves nothing. Each member also
carries the person's Entra object id (``oid``): MC knows the decider's oid (``actorId``,
``actors.ts:67`` @ 073f192) but stores only the email label, and a subject's Portal
email may differ from the Entra alias MC shows, so separation of duties matches the
decider to the intent's subjects by oid as well as by email
(docs/decisions/2026-10-04-goal2-identity.md). A member without an oid cannot
authorize (``decider_oid_unknown``).

``load_approver_roster``: the committed default roster (``operating/approver_roster.json``):
who approves each action type of the catalog
(``operating/action_catalog.json``; ``"all"`` is every type it declares) and their MC role
(null while no source states it: a per-call approver who decides no gate) and Entra oid,
each with where it was read. An oid is recorded only with its source and is
never invented: until one is read, the approver stays refused (``call_approver_oid_unknown``,
``decider_oid_unknown``). A host may instead resolve a null oid from Graph ``/users`` by the
approver's UPN at its start (``integrations.graph_directory.resolve_roster``):
such an oid keeps its provenance (``graph_users`` and its read
time, ``oid_provenance``) and counts only while it is at most ``APPROVER_OID_MAX_AGE`` old at
the moment of each check (``oid_of(email, now)``), else that approver is refused as
unverified (``call_approver_oid_unverified``, ``decider_oid_unverified``). The roster loads
the host's resolution once; a host whose copy aged out re-reads it through
``ActionApprovers.reload`` (the same load, the same one reader of the host state) at the
check, and nothing re-reads Graph before the next host start (load-only, decision record
docs/decisions/2026-10-04-goal2-identity.md).

``cited_sources``: the cited sources an MC call carries so its approvers see them.
They give approvers context; it authorizes no
disclosure beyond a source's read rights: a source's text is
quoted only when every member of the target bucket's audience (``McBucketAudience``) reads
exactly the cited content through the Workbench's own read check, else it goes as its
citation alone with the reason (``audience_withholding``).

``load_bucket_audiences``: who reads each MC follow-up bucket, from the committed file
``operating/mc_bucket_audiences.json`` that the owner states, because MC exposes no read of
project members (docs/decisions/2026-10-08-mc-follow-up-audience.md). A host passes it as
``mc_follow_up_audience``; each member reads as ``employee:<oid>`` through the host's own
employee gate. Only a restricted bucket whose every MC member token is an Entra oid has an
audience: a shared or unparented bucket, one listing an email, ``sp_*`` or directory-id
token, a null oid, a bucket not in the file or an entry past ``BUCKET_AUDIENCE_MAX_AGE``
gives none (citations only).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence

from integrations.graph_directory import (APPROVER_ROSTER_SCHEMA, DEFAULT_APPROVER_ROSTER,
                                          approver_oid_fresh)
from integrations.live_source_guard import ReadOnlyGuardError, text_bytes
from integrations.http import SourceUnavailable
from integrations.mc import MissionControlMcpClient
from integrations.plx_hub_guard import (APPROVAL_REQUEST_TOOL, McWriteAuthorization,
                                        UpstreamError, normalize_email)
from ontology.core import Actor

from .employee_permissions import EMPLOYEE_PREFIX

REQUEST_SCHEMA = "plx-mc-approval-request/v1"
# Where a roster oid came from: a person (the committed roster, or a host's configuration)
# or the host's GET-only Graph ``/users`` read by UPN (with its read time).
OID_COMMITTED, OID_GRAPH_USERS = "committed", "graph_users"
DECIDING_ROLES = frozenset({"admin", "owner"})
MC_ROLES = frozenset({"member", "admin", "owner"})

_TASK_ID = re.compile(r"^TASK-[A-Za-z0-9-]{1,64}$")
_GATE_ID = re.compile(r"^apg_[A-Za-z0-9_-]{1,80}$")
_HUMAN_EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+$")
_SERVICE_LABEL = re.compile(r"^(?:sp_|agent:|svc[_:-]|service[_:-])", re.IGNORECASE)
_OID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Error codes MC throws before ``patchTask`` (requireMcpActor, the project ACL
# guard, unknown task) and HTTP auth refusals before JSON-RPC runs: certain
# that no gate was written. Everything else (``internal``, 5xx, lost answer)
# may have written one.
_REFUSED_BEFORE_WRITE = frozenset({"forbidden", "not_found", "unauthorized", "auth_refused"})
CREATE_TASK_TOOL = "mc_create_task"
_CREATED_TASK = re.compile(r"^TASK-[0-9]{1,9}$")
# mc_create_task: refused before createTask commits (requireMcpActor, the bucket's project
# ACL, the agent-assignee guard, an unknown bucket: PLX_MC actions.ts:231-243,
# state.ts:500-505). The key can never create this payload: the same key with another
# payload, or a key whose first create was abandoned (task-create-idempotency.ts).
_CREATE_REFUSED_BEFORE_WRITE = _REFUSED_BEFORE_WRITE | {"invalid_bucket"}
_CREATE_KEY_SPENT = frozenset({"idempotency_key_reused", "idempotency_key_failed"})


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canon(value).encode("utf-8")).hexdigest()


def _parse_stamp(stamp: Any) -> datetime:
    """A ``YYYY-MM-DDTHH:MM:SSZ`` read time; ``ValueError`` for anything else."""
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        raise ValueError("roster oid provenance must be committed or graph_users") from None


def canonical_email(email: Any) -> str:
    """One identity per mailbox (``plx_hub_guard.normalize_email``: trimmed, lower
    case, ``local+tag@domain`` -> ``local@domain``). MC compares labels only
    case-insensitively (``approvals.ts`` ``separationOfDutiesViolation``), so a
    plus-alias of the operator would pass MC's check; it never passes ours."""
    if not isinstance(email, str) or "@" not in email:
        return email.strip().lower() if isinstance(email, str) else ""
    return normalize_email(email)


def canonical_oid(oid: Any) -> str | None:
    """An Entra object id as configured (trimmed, lower case); None stays None
    (unknown). Anything else is a ``ValueError``: a person-supplied oid that is not
    a GUID must never silently read as unknown or as a match."""
    if oid is None:
        return None
    clean = oid.strip().lower() if isinstance(oid, str) else ""
    if not _OID.fullmatch(clean):
        raise ValueError("oid must be an Entra object id")
    return clean


def prepare_request(intent: Mapping[str, Any], task_id: str, *,
                    checked_preconditions: list[Mapping[str, Any]],
                    human_preconditions: list[str]) -> dict[str, Any]:
    """The MC approval request record a human approves before it is sent.

    It names the exact call and binds it to the case, the proposal digest and
    the cited evidence digests. Pure: no I/O.
    """
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise ValueError("invalid_task_id")
    reason = intent["gate_reason"]
    if (not isinstance(reason, str) or not reason.strip() or len(reason) > 500
            or any(ord(c) < 32 or ord(c) == 127 for c in reason)):
        raise ValueError("invalid_gate_reason")  # PLX_MC requestApprovalSchema: 1..500
    arguments = {"taskId": task_id, "reason": reason}
    return {"schema": REQUEST_SCHEMA, "tool": APPROVAL_REQUEST_TOOL, "arguments": arguments,
            "arguments_sha256": _sha(arguments), "intent_id": intent["intent_id"],
            "case_id": intent["case_id"], "proposal_id": intent["proposal_id"],
            "proposal_sha256": intent["proposal_sha256"],
            "evidence": [dict(item) for item in intent.get("evidence") or ()],
            "idempotency_key": intent["idempotency_key"],
            "owner_system": intent["owner_system"], "capability": intent["capability"],
            "checked_preconditions": [dict(item) for item in checked_preconditions],
            "human_preconditions": list(human_preconditions),
            "sent": False}



@dataclass(frozen=True)
class ApproverRoster:
    """MC ``mc_users`` admins/owners able to decide gates, as supplied by a person.

    ``members``: email -> ``{"role": member|admin|owner or None, "oid": Entra object id
    or None, "action_types": [action type, ...]}`` of *active* MC users; a None role: no
    source states the person's MC role, so they approve per call but decide no gate
    (``decider_not_in_mc_roster``). ``action_types``
    (required, no default) are the catalog action types the member approves, per call
    and as a gate decider; an empty list approves nothing. ``source_ref``: where this
    list was read (who, when, from what); required, because the roster is only as good
    as its source. A host-resolved oid also carries ``"oid_source": "graph_users"`` and
    its ``"oid_read_at"`` (``YYYY-MM-DDTHH:MM:SSZ``); any other oid is ``committed``.
    """

    members: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    source_ref: str = ""

    def __post_init__(self) -> None:
        clean: dict[str, dict[str, Any]] = {}
        for email, entry in dict(self.members).items():
            if not isinstance(email, str) or not _HUMAN_EMAIL.fullmatch(email.strip()) \
                    or _SERVICE_LABEL.match(email.strip()):
                raise ValueError("roster members must be human emails")
            if not isinstance(entry, Mapping) or not {"role", "action_types"} <= set(entry) \
                    or not set(entry) <= _MEMBER_KEYS:
                raise ValueError("roster entries must be {role, oid, action_types}")
            if entry["role"] is not None and (not isinstance(entry["role"], str)
                                                  or entry["role"] not in MC_ROLES):
                raise ValueError("roster role must be member, admin or owner")
            types = entry["action_types"]
            if not isinstance(types, (list, tuple)) or not all(
                    isinstance(name, str) and name and name == name.strip() for name in types):
                raise ValueError("roster action_types must be a list of action types")
            try:
                oid = canonical_oid(entry.get("oid"))
            except ValueError:
                raise ValueError("roster oid must be an Entra object id") from None
            member = {"role": entry["role"], "oid": oid,
                      "action_types": tuple(sorted(set(types)))}
            source = entry.get("oid_source", OID_COMMITTED)
            if source == OID_GRAPH_USERS and oid is not None:
                _parse_stamp(entry.get("oid_read_at"))   # its form; its age: at each use
                member.update(oid_source=OID_GRAPH_USERS, oid_read_at=entry["oid_read_at"])
            elif source != OID_COMMITTED or "oid_read_at" in entry:
                raise ValueError("roster oid provenance must be committed or graph_users")
            clean[canonical_email(email)] = member
        object.__setattr__(self, "members", clean)
        if self.members and (not isinstance(self.source_ref, str)
                             or not 1 <= len(self.source_ref.strip()) <= 300):
            raise ValueError("a non-empty roster needs its source_ref")

    @property
    def sha256(self) -> str:
        return _sha({"members": self.members, "source_ref": self.source_ref.strip()})

    def role_of(self, email: str) -> str | None:
        return (self.members.get(canonical_email(email)) or {}).get("role") \
            if isinstance(email, str) else None

    def oid_of(self, email: str, now: datetime | None = None) -> str | None:
        """The member's Entra object id usable at ``now`` (default: the current time):
        a committed one, or a ``graph_users`` one read at most ``APPROVER_OID_MAX_AGE``
        before ``now``; None: unknown or unverified (``oid_unverified``)."""
        member = self.members.get(canonical_email(email)) if isinstance(email, str) else None
        if not member or member["oid"] is None:
            return None
        if member.get("oid_source") == OID_GRAPH_USERS and not approver_oid_fresh(
                member["oid_read_at"], now or datetime.now(timezone.utc)):
            return None
        return member["oid"]

    def oid_unverified(self, email: str, now: datetime | None = None) -> bool:
        """Whether the member has a ``graph_users`` oid that is not fresh at ``now``."""
        member = self.members.get(canonical_email(email)) if isinstance(email, str) else None
        return bool(member and member["oid"] is not None
                    and self.oid_of(email, now) is None)

    def oid_provenance(self, email: str) -> dict[str, str] | None:
        """``{"source": "committed"}`` or ``{"source": "graph_users", "read_at": ...}`` of
        the member's oid (None: no oid); what an approval record states."""
        member = self.members.get(canonical_email(email)) if isinstance(email, str) else None
        if not member or member["oid"] is None:
            return None
        if member.get("oid_source") == OID_GRAPH_USERS:
            return {"source": OID_GRAPH_USERS, "read_at": member["oid_read_at"]}
        return {"source": OID_COMMITTED}

    def approves(self, email: str, action_type: str) -> bool:
        """Whether the member approves ``action_type``: listed in its ``action_types``
        (an empty list approves nothing). The one type rule for a per-call approver and
        a gate decider."""
        member = self.members.get(canonical_email(email)) if isinstance(email, str) else None
        return bool(member) and action_type in member["action_types"]

    def approves_any(self, email: str) -> bool:
        """Whether the member approves at least one action type."""
        member = self.members.get(canonical_email(email)) if isinstance(email, str) else None
        return bool(member and member["action_types"])

    def may_decide(self, email: str, action_type: str) -> bool:
        """Whether the member may decide an MC gate of ``action_type``: an admin/owner
        (MC's ``approval.decide``) who approves that type."""
        return self.role_of(email) in DECIDING_ROLES and self.approves(email, action_type)


_MEMBER_KEYS = frozenset({"role", "oid", "action_types", "oid_source", "oid_read_at"})



ALL_ACTION_TYPES = "all"
_ROSTER_KEYS = frozenset({"schema", "note", "source_ref", "approvers"})
_APPROVER_KEYS = frozenset({"name", "mc_role", "mc_role_source", "oid", "oid_source",
                            "action_types"})


@dataclass(frozen=True)
class ActionApprovers:
    """The committed roster: ``by_action`` maps every catalog action type to the
    ``{email: committed oid}`` of its approvers (empty: nobody approves it); ``mc_roster`` is
    the same people as the ``ApproverRoster`` (role, action types, oid with its
    provenance) an ``ActionPath`` checks every per-call approver and gate decider against,
    by the intent's action type, and that supplies a host-resolved oid, checked for age at
    each use.
    ``reload``: the same load again (``load_approver_roster`` with the same roster, catalog
    and host state; None without a host state), the ``mc_roster`` as the host state holds
    it now."""

    by_action: Mapping[str, Mapping[str, str | None]]
    mc_roster: ApproverRoster
    reload: Callable[[], ApproverRoster] | None = field(default=None, compare=False)

    def for_action(self, action_type: str) -> dict[str, str | None]:
        if action_type not in self.by_action:
            raise ValueError("roster_action_type_unknown")
        return dict(self.by_action[action_type])

    def path_options(self) -> dict[str, Any]:
        """The ``ActionPath`` keywords every dispatching host takes from this one roster:
        ``approver_roster`` (who approves which action type, per call and as a gate
        decider; an oid is read from it at each check, so a host-resolved oid is never
        copied past its age limit) and ``approver_roster_reload`` (``reload``: the roster
        re-read when its copy of that oid is past the limit). A type-scoped approver
        approves its types at any host."""
        return {"approver_roster": self.mc_roster, "approver_roster_reload": self.reload}


def load_approver_roster(path: str | Path | None = None, *, catalog: Any = None,
                         state_dir: str | Path | None = None) -> ActionApprovers:
    """Load and check the approver roster (default ``DEFAULT_APPROVER_ROSTER``) against
    the action catalog (default ``ActionCatalog.builtin()``). Fails closed
    (``ValueError``): another schema, an unknown key, an action type the catalog does not
    declare, two entries for one mailbox, a person without a name, an MC role without its
    source or a source without a role (a null ``mc_role`` has a null ``mc_role_source``:
    never an invented one), or an oid without the source it was read from
    (``roster_oid_without_source``).

    ``state_dir``: a host's state. An approver whose committed oid is null takes the oid
    the host read from Graph by the approver's UPN at its start, with its provenance
    (``integrations.graph_directory.resolved_approver_oids``, the one reader: there, not
    here, because an oid is never committed); its age is checked at each use, not here. A
    read that differs from a committed oid leaves none (``call_approver_oid_unknown``,
    ``decider_oid_unknown``)."""
    resolved: Mapping[str, Mapping[str, str]] = {}
    if state_dir is not None:
        from integrations.graph_directory import resolved_approver_oids
        resolved = resolved_approver_oids(state_dir)
    if catalog is None:
        from .action_catalog import ActionCatalog  # the catalog imports the action path's deps
        catalog = ActionCatalog.builtin()
    known = list(catalog.ids())
    document = json.loads(Path(path or DEFAULT_APPROVER_ROSTER).read_text(encoding="utf-8"))

    def require(condition: bool, code: str) -> None:
        if not condition:
            raise ValueError(code)

    def text(value: Any) -> bool:
        return isinstance(value, str) and 1 <= len(value.strip()) <= 300

    require(isinstance(document, Mapping) and set(document) == _ROSTER_KEYS
            and document["schema"] == APPROVER_ROSTER_SCHEMA, "roster_document_invalid")
    approvers = document["approvers"]
    require(isinstance(approvers, Mapping) and bool(approvers), "roster_approvers_missing")
    members = {}
    for email, entry in approvers.items():
        require(isinstance(entry, Mapping) and set(entry) == _APPROVER_KEYS,
                "roster_approver_keys")
        require(text(entry["name"]) and (text(entry["mc_role_source"]) if entry["mc_role"]
                                          is not None else entry["mc_role_source"] is None),
                "roster_approver_source")
        # Never an invented oid: one is recorded only with where it was read.
        require((entry["oid"] is None and entry["oid_source"] is None)
                or (entry["oid"] is not None and text(entry["oid_source"])),
                "roster_oid_without_source")
        types = known if entry["action_types"] == ALL_ACTION_TYPES else entry["action_types"]
        # An empty list: an MC user the roster names for its role only: it approves no
        # call and decides no gate.
        require(isinstance(types, list) and all(isinstance(name, str) for name in types)
                and set(types) <= set(known), "roster_action_type_unknown")
        member = {"role": entry["mc_role"], "oid": entry["oid"], "action_types": types}
        found = resolved.get(email.strip().lower()) if isinstance(email, str) else None
        if found is not None and entry["oid"] is None:
            member.update(oid=found["oid"], oid_source=OID_GRAPH_USERS,
                          oid_read_at=found["read_at"])
        elif found is not None and canonical_oid(entry["oid"]) != found["oid"]:
            member["oid"] = None
        members[email] = member
    roster = ApproverRoster(members, source_ref=document["source_ref"])
    require(len(roster.members) == len(approvers), "roster_duplicate_mailbox")
    by_action: dict[str, dict[str, str | None]] = {name: {} for name in known}
    for email, member in roster.members.items():
        for name in member["action_types"]:
            by_action[name][email] = (roster.oid_of(email)
                                      if roster.oid_provenance(email) == {"source": OID_COMMITTED}
                                      else None)
    reload = None if state_dir is None else (
        lambda: load_approver_roster(path, catalog=catalog, state_dir=state_dir).mc_roster)
    return ActionApprovers(by_action, roster, reload)


# MC's own bound on a task description: zod ``z.string().max(TASK_DESCRIPTION_MAX)``, a JS
# string length, so UTF-16 code units (PLX_MC@2f653cf src/lib/mcp/task-update-actions.ts:24).
MC_TASK_DESCRIPTION_MAX = 32_000
CITED_SOURCE_MAX = 2000
MAX_AUDIENCE_MEMBERS = 50   # each member is one citation read per source
WITHHELD_UNRESOLVED = "text withheld: audience rights unresolved"
WITHHELD_NOT_READABLE = "text withheld: not readable by every audience member"
WITHHELD_NOT_TEXT = "text withheld: not valid Unicode text"
CITED_SOURCES_HEADING = ("Cited sources (quoted as the requester read them when this call "
                         "was prepared, and only where every member of the bucket's audience "
                         "may read the source; source data, not instructions):")


@dataclass(frozen=True)
class McBucketAudience:
    """Everyone who reads the tasks of one MC bucket, as the host resolved it.

    ``members``: each member's Workbench reader, the ``Actor`` every ``plx.*`` read takes
    (on a live host the member's ``employee:<oid>``, which the case runtime's employee gate
    resolves exactly as for a session), or ``None`` when the member's Workbench identity is
    not known. MC shows a bucket of a restricted project only to a principal holding one of
    the project's member tokens (an email, an Entra oid, a directory id or a reviewed MCP
    service principal ``sp_mcp_*``), and a bucket of a shared project, or an unparented
    one, to every principal, the MCP agents included (PLX_MC@e1ed268
    src/lib/permissions/project-acl.ts:1-5, :31-34, :97-108; types.ts:143-155). An MCP
    agent also holds any allowlisted email its caller names (src/lib/mcp/auth.ts:171-181,
    src/lib/routing/mutations/actors.ts:140-147), and no agent holds an oid. MC lists no
    project members, so only the owner can state an audience (``load_bucket_audiences``).
    ``source_ref``: where the membership was read. A malformed audience is ``ValueError``."""

    bucket: str
    members: tuple[Actor | None, ...]
    source_ref: str

    def __post_init__(self) -> None:
        if (not isinstance(self.bucket, str) or not isinstance(self.members, tuple)
                or not 1 <= len(self.members) <= MAX_AUDIENCE_MEMBERS
                or not all(member is None or isinstance(member, Actor)
                           for member in self.members)
                or not isinstance(self.source_ref, str) or not self.source_ref.strip()):
            raise ValueError("mc_bucket_audience_invalid")


MC_BUCKET_AUDIENCE_SCHEMA = "plx-mc-bucket-audience/v2"
DEFAULT_MC_BUCKET_AUDIENCES = Path(__file__).with_name("mc_bucket_audiences.json")
# How long a person's read of a bucket's members counts: after it, that bucket has no
# audience until someone re-reads it and updates ``read_at``. A ``read_at`` ahead of the
# host's clock by at most ``BUCKET_AUDIENCE_CLOCK_SKEW`` counts as read now.
BUCKET_AUDIENCE_MAX_AGE = timedelta(days=30)
BUCKET_AUDIENCE_CLOCK_SKEW = timedelta(minutes=5)
# MC's project visibility (project-acl.ts:11-12); "unparented" is a bucket with no project.
BUCKET_RESTRICTED = "restricted"
BUCKET_VISIBILITIES = frozenset({BUCKET_RESTRICTED, "shared", "unparented"})
_BUCKET_ID = re.compile(r"^BKT-[A-Za-z0-9_-]{1,80}$")
# MC's member token (project-acl.ts ``normalizeMemberToken``): trimmed, lower case; a
# plus-tagged email stays its own token.
# A directory id: a key of MC's directory (``HUMANS``, src/lib/mc-data/data.ts:88), which
# every MCP agent operated by that person also holds (routing/mutations/actors.ts:132-147).
_DIRECTORY_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_AUDIENCES_KEYS = frozenset({"schema", "note", "buckets"})
_BUCKET_KEYS = frozenset({"source_ref", "visibility", "members"})
_AUDIENCE_MEMBER_KEYS = frozenset({"oid", "oid_source", "read_from", "read_at"})


@dataclass(frozen=True)
class BucketAudiences:
    """The committed audiences (``load_bucket_audiences``): called with a bucket id, the
    ``McBucketAudience`` of its members, each read as ``employee:<oid>`` (the lowest
    clearance it presents; the host's employee gate gives the employee's own rights), or
    None (citations only) for a bucket not in the file, one that does not quote (shared,
    unparented, or a token that is not an Entra oid), a member without an oid, or a member
    read longer than ``BUCKET_AUDIENCE_MAX_AGE`` ago (or more than
    ``BUCKET_AUDIENCE_CLOCK_SKEW`` in the future) at the call. ``buckets``: id ->
    ``(source_ref, quotes, ((oid or None, read_at), ...))``, one member per person (the
    tokens naming one oid merge, read at the oldest of their reads) and per other token."""

    buckets: Mapping[str, tuple[str, bool, tuple[tuple[str | None, datetime], ...]]]
    tenant: str
    clock: Callable[[], datetime] | None = field(default=None, compare=False)

    def __call__(self, bucket: str) -> McBucketAudience | None:
        entry = self.buckets.get(bucket) if isinstance(bucket, str) else None
        if entry is None or not entry[1]:
            return None
        source_ref, _quotes, members = entry
        now = (self.clock or (lambda: datetime.now(timezone.utc)))()
        if any(oid is None
               or not -BUCKET_AUDIENCE_CLOCK_SKEW <= now - read_at <= BUCKET_AUDIENCE_MAX_AGE
               for oid, read_at in members):
            return None
        return McBucketAudience(bucket, tuple(
            Actor(self.tenant, frozenset({EMPLOYEE_PREFIX + oid}), "public")
            for oid, _read_at in members), source_ref)


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _value in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("mc_bucket_audience_duplicate_key")
    return dict(pairs)


def load_bucket_audiences(path: str | Path | None = None, *, tenant: str,
                          clock: Callable[[], datetime] | None = None) -> BucketAudiences:
    """Load and check the bucket audiences (default ``DEFAULT_MC_BUCKET_AUDIENCES``) for a
    host of ``tenant``. Each bucket names its MC ``visibility`` and, keyed by MC member
    token, every principal that reads it. Only a ``restricted`` bucket whose every token is
    an Entra oid has an audience: no MCP agent holds an oid. A shared or unparented bucket,
    or one listing an email (any agent may name it), an ``sp_*`` (or other service) token
    or a directory id, loads with none. An email and an oid token naming one oid are one
    person.

    Fails closed at host start: a missing file is ``OSError``; a key twice in one object,
    another schema, an unknown key, a bucket id that is not ``BKT-*``, an unknown
    visibility, a restricted bucket with no members, more than ``MAX_AUDIENCE_MEMBERS``,
    a token of no known kind (or the same token twice), an oid that is not an Entra object
    id, has no source, does not match its oid token or belongs to a non-person token, a
    member without where and when it was read, or a ``read_at``
    more than ``BUCKET_AUDIENCE_CLOCK_SKEW`` ahead of ``clock`` is ``ValueError``. An oid
    is never invented: one not yet read stays null and leaves its bucket without an
    audience."""
    document = json.loads(Path(path or DEFAULT_MC_BUCKET_AUDIENCES).read_text(encoding="utf-8"),
                          object_pairs_hook=_unique_keys)
    now = (clock or (lambda: datetime.now(timezone.utc)))()

    def require(condition: bool, code: str) -> None:
        if not condition:
            raise ValueError(code)

    def text(value: Any) -> bool:
        return isinstance(value, str) and 1 <= len(value.strip()) <= 300

    require(isinstance(tenant, str) and bool(tenant.strip()), "mc_bucket_audience_tenant")
    require(isinstance(document, Mapping) and set(document) == _AUDIENCES_KEYS
            and document["schema"] == MC_BUCKET_AUDIENCE_SCHEMA
            and isinstance(document["buckets"], Mapping), "mc_bucket_audience_document_invalid")
    buckets: dict[str, tuple[str, bool, tuple[tuple[str | None, datetime], ...]]] = {}
    for bucket, entry in document["buckets"].items():
        require(isinstance(bucket, str) and bool(_BUCKET_ID.fullmatch(bucket))
                and isinstance(entry, Mapping) and set(entry) == _BUCKET_KEYS
                and text(entry["source_ref"]) and isinstance(entry["visibility"], str)
                and entry["visibility"] in BUCKET_VISIBILITIES
                and isinstance(entry["members"], Mapping)
                and (entry["visibility"] != BUCKET_RESTRICTED or entry["members"])
                and len(entry["members"]) <= MAX_AUDIENCE_MEMBERS,
                "mc_bucket_audience_bucket_invalid")
        people: dict[str | int, datetime] = {}
        seen: set[str] = set()
        quotes = entry["visibility"] == BUCKET_RESTRICTED
        for raw, member in entry["members"].items():
            token = raw.strip().lower() if isinstance(raw, str) else ""
            require(isinstance(member, Mapping) and set(member) == _AUDIENCE_MEMBER_KEYS
                    and text(member["read_from"]), "mc_bucket_audience_member_invalid")
            if _SERVICE_LABEL.match(token):
                person = False
            elif "@" in token:
                require(bool(_HUMAN_EMAIL.fullmatch(token)), "mc_bucket_audience_member_invalid")
                person = True
            else:
                person = bool(_OID.fullmatch(token))
                require(person or bool(_DIRECTORY_ID.fullmatch(token)),
                        "mc_bucket_audience_member_invalid")
            require(token not in seen, "mc_bucket_audience_duplicate_member")
            seen.add(token)
            try:
                oid = canonical_oid(member["oid"])
            except ValueError:
                raise ValueError("mc_bucket_audience_oid_invalid") from None
            require((oid is None and member["oid_source"] is None)
                    or (oid is not None and text(member["oid_source"])),
                    "mc_bucket_audience_oid_without_source")
            require(person or oid is None, "mc_bucket_audience_oid_not_a_person")
            require(oid is None or not _OID.fullmatch(token) or oid == token,
                    "mc_bucket_audience_oid_mismatch")
            try:
                read_at = _parse_stamp(member["read_at"])
            except ValueError:
                raise ValueError("mc_bucket_audience_read_at_invalid") from None
            require(read_at - now <= BUCKET_AUDIENCE_CLOCK_SKEW,
                    "mc_bucket_audience_read_at_future")
            quotes = quotes and bool(_OID.fullmatch(token))
            key = oid if oid is not None else len(seen)   # an unknown member stays its own
            people[key] = min(read_at, people.get(key, read_at))
        buckets[bucket] = (entry["source_ref"].strip(), quotes, tuple(
            (key if isinstance(key, str) else None, read_at) for key, read_at in people.items()))
    return BucketAudiences(buckets, tenant, clock)


def audience_withholding(evidence: Sequence[Mapping[str, Any]],
                         members: Sequence[Sequence[Mapping[str, Any]] | None] | None
                         ) -> list[str | None]:
    """Per citation of ``evidence`` (the requester's ``CaseStore.proposal_evidence``): None
    when its text may be quoted, else why it is withheld. ``members``: one
    ``proposal_evidence`` per audience member, ``None`` for a member the read refused
    (case or source not readable for them); ``None`` for the whole when the audience or a
    member's rights could not be resolved. Text is quoted only when every member's read
    resolves the exact cited content (fail closed) and that content is Unicode text MC can
    measure: a source that is not (a lone surrogate) is withheld alone, the others are
    still quoted."""
    if not members:
        return [WITHHELD_UNRESOLVED] * len(evidence)
    reads = [None if items is None else {item.get("handle"): item.get("current_digest")
                                         for item in items} for items in members]
    return [WITHHELD_NOT_READABLE
            if not all(read is not None and read.get(item["handle"]) is not None
                       and read.get(item["handle"]) == item.get("cited_digest")
                       for read in reads)
            else None if _is_text(_source_text(item.get("content"))) else WITHHELD_NOT_TEXT
            for item in evidence]


def _is_text(text: str) -> bool:
    try:
        text_bytes(text, "utf-16-le")
    except ValueError:
        return False
    return True


def mc_length(text: str) -> int:
    """A string's length as MC measures it: UTF-16 code units. A lone surrogate is
    ``ValueError("text_not_unicode")``."""
    return len(text_bytes(text, "utf-16-le")) // 2


def mc_prefix(text: str, limit: int) -> str:
    """The longest prefix of ``text`` within ``limit`` UTF-16 code units; a surrogate pair
    the limit would split is left out whole."""
    return text_bytes(text, "utf-16-le")[:2 * limit].decode("utf-16-le", "ignore")


def _source_text(content: Any) -> str:
    body = (content.get("row", content.get("rows", content))
            if isinstance(content, Mapping) else content)

    def show(value: Any) -> str:
        return value if isinstance(value, str) else json.dumps(
            value, sort_keys=True, ensure_ascii=False, default=str)

    if isinstance(body, Mapping):
        return "\n".join(f"{name}: {show(value)}" for name, value in body.items())
    return show(body)


def cited_sources(evidence: Sequence[Mapping[str, Any]], withheld: Sequence[str | None], *,
                  limit: int = MC_TASK_DESCRIPTION_MAX) -> str:
    """The cited sources an MC call carries for its approvers: each citation of the
    proposal (``CaseStore.proposal_evidence`` for the requester), numbered, with its handle
    and either what it resolves to or, where ``withheld`` (``audience_withholding``) gives
    a reason, that reason alone. A citation that no longer resolves to the content that
    was cited is ``ValueError("cited_source_unavailable")``: no call is prepared without
    its sources. A quoted source shows at most ``CITED_SOURCE_MAX`` UTF-16 units and, when
    it is cut, says how many of its characters it shows. The whole text stays within
    ``limit`` UTF-16 units: a source whose text does not fit is listed without it, one that
    cannot be listed at all ends the list, and a closing line states both
    (``ValueError("mc_description_too_long")`` when not even that fits)."""
    if len(withheld) != len(evidence):
        raise ValueError("cited_sources_withholding_mismatch")
    for item in evidence:
        if (item.get("content") is None or item.get("current_digest") is None
                or item.get("current_digest") != item.get("cited_digest")):
            raise ValueError("cited_source_unavailable")
    if not evidence:
        text = "Cited sources: none (the proposal cites no record)."
        if mc_length(text) > limit:
            raise ValueError("mc_description_too_long")
        return text
    total = len(evidence)

    def notice(omitted: int, unlisted: int) -> str:
        return (f"[cut to stay within Mission Control's {MC_TASK_DESCRIPTION_MAX:,}-character "
                f"description limit: the text of {omitted} of {total} sources omitted, "
                f"{unlisted} of {total} sources not listed; the case cites every one]")

    # Every source listed first (its citation, and its reason or a text-omitted mark),
    # as many as fit; then each listed source's text, in order, while it still fits.
    room = limit - mc_length(CITED_SOURCES_HEADING) - 2 - mc_length(notice(total, total))
    listed: list[tuple[str, str | None]] = []
    for index, (item, reason) in enumerate(zip(evidence, withheld), 1):
        label = f"[{index}] {item['handle']}"
        short = f"{label}\n{reason or '[text omitted: description limit]'}"
        if 2 + mc_length(short) > room:
            break
        room -= 2 + mc_length(short)
        if reason is None:
            shown = _source_text(item["content"])
            if mc_length(shown) > CITED_SOURCE_MAX:
                head = mc_prefix(shown, CITED_SOURCE_MAX)
                # The note counts characters (code points), what a reader counts; the
                # cut itself is in MC's UTF-16 units.
                shown = (head + f"\n[cut: {len(head)} of {len(shown)} "
                         "characters shown; the citation names the whole record]")
            listed.append((short, f"{label}\n{shown}"))
        else:
            listed.append((short, None))
    parts, omitted, unlisted = [CITED_SOURCES_HEADING], 0, total - len(listed)
    for short, full in listed:
        extra = None if full is None else mc_length(full) - mc_length(short)
        if extra is not None and extra <= room:
            room -= extra
            parts.append(full)
        else:
            parts.append(short)
            omitted += full is not None
    if omitted or unlisted:
        parts.append(notice(omitted, unlisted))
    text = "\n\n".join(parts)
    if mc_length(text) > limit:
        raise ValueError("mc_description_too_long")
    return text


def authorization_for(call_approval: Mapping[str, Any]) -> McWriteAuthorization:
    """The client's ``McWriteAuthorization`` for the action path's recorded
    per-call approval (same tool, arguments, approver, window, evidence)."""
    return McWriteAuthorization.for_call(
        call_approval["tool"], call_approval["arguments"],
        approved_by=call_approval["approved_by"], approved_at=call_approval["approved_at"],
        expires_at=call_approval["expires_at"], evidence_ref=call_approval["evidence_ref"])


def send_request(mc: MissionControlMcpClient, task_id: str, reason: str,
                 authorization: McWriteAuthorization) -> tuple[str, str]:
    """Send ``mc_request_approval`` once through the MC client's write door.

    Returns ``("requested", gate_id)``, ``("refused", reason)`` (certain no gate
    was written: refused before I/O by the client/guard/ledger, or MC refused
    before writing) or ``("unknown", error)`` (a gate may exist; reconcile by
    reading the task, never resend).
    """
    if type(mc) is not MissionControlMcpClient:
        raise TypeError("mc must be a MissionControlMcpClient")
    try:
        payload = mc.call_write_tool(APPROVAL_REQUEST_TOOL, {"taskId": task_id, "reason": reason},
                                     authorization=authorization)
    except ReadOnlyGuardError as exc:
        return "refused", str(exc)[:200]
    except UpstreamError as exc:
        if exc.code in _REFUSED_BEFORE_WRITE:
            return "refused", f"mc_refused:{exc.code}"
        return "unknown", exc.code
    except Exception as exc:  # noqa: BLE001 - malformed answer etc.: outcome unknown
        return "unknown", type(exc).__name__
    data = payload.get("data") if isinstance(payload, dict) else None
    if (not isinstance(data, dict) or data.get("taskId") != task_id
            or data.get("status") != "pending" or not isinstance(data.get("gateId"), str)
            or not _GATE_ID.fullmatch(data["gateId"])):
        return "unknown", "invalid_gate_answer"
    return "requested", data["gateId"]


def send_create_task(mc: MissionControlMcpClient, arguments: Mapping[str, Any],
                     authorization: McWriteAuthorization) -> tuple[str, str]:
    """Send ``mc_create_task`` once through the MC client's write door.

    Returns ``("created", task_id)`` (MC's answer names the task, a replay of our key
    included), ``("refused", reason)`` (certain nothing was created: refused before I/O
    by the client, guard or ledger, or by MC before its create committed),
    ``("rejected", reason)`` (MC will never create this payload under this key) or
    ``("unknown", error)`` (a task may exist: read back by the key, never resend).
    """
    if type(mc) is not MissionControlMcpClient:
        raise TypeError("mc must be a MissionControlMcpClient")
    try:
        payload = mc.call_write_tool(CREATE_TASK_TOOL, dict(arguments),
                                     authorization=authorization)
    except ReadOnlyGuardError as exc:
        return "refused", str(exc)[:200]
    except UpstreamError as exc:
        if exc.code in _CREATE_KEY_SPENT:
            return "rejected", f"mc_refused:{exc.code}"
        if exc.code in _CREATE_REFUSED_BEFORE_WRITE:
            return "refused", f"mc_refused:{exc.code}"
        return "unknown", exc.code
    except Exception as exc:  # noqa: BLE001 - malformed answer etc.: outcome unknown
        return "unknown", type(exc).__name__
    task = payload.get("task") if isinstance(payload, dict) else None
    task_id = payload.get("taskId") if isinstance(payload, dict) else None
    if (not isinstance(task_id, str) or not _CREATED_TASK.fullmatch(task_id)
            or not isinstance(task, dict) or task.get("id") != task_id):
        return "unknown", "invalid_create_answer"
    return "created", task_id


def read_created_task(mc: MissionControlMcpClient, task_id: str | None, key: str,
                      expected: Mapping[str, Any]) -> dict[str, Any]:
    """Read back the task one ``mc_create_task`` call created (reads only).

    ``task_id``: MC's answer to the call; None after an unknown outcome, when the task is
    the one ``mc_search_tasks`` finds for ``key`` (its description carries it). Returns
    ``{outcome, reason, fields, task_id}``: ``effect_observed`` only when the task holds
    every ``expected`` field exactly; no task is ``effect_not_observed`` (MC may still be
    creating it); an unreadable MC or several tasks for the key is ``unknown``."""
    if type(mc) is not MissionControlMcpClient:
        raise TypeError("mc must be a MissionControlMcpClient")
    found = {"fields": {}, "task_id": task_id}
    try:
        if task_id is None:
            tasks = mc.search_tasks(key, limit=5)["data"]["tasks"]
            ids = sorted({item["id"] for item in tasks if isinstance(item, dict)
                          and isinstance(item.get("id"), str)
                          and _CREATED_TASK.fullmatch(item["id"])})
            if not ids:
                return {**found, "outcome": "effect_not_observed",
                        "reason": "no_task_carries_this_key"}
            if len(ids) > 1:
                return {**found, "outcome": "unknown", "reason": "several_tasks_carry_this_key"}
            task_id = ids[0]
        task = mc.get_task(task_id)["data"]["task"]
    except UpstreamError as exc:
        if exc.code == "not_found":
            return {**found, "outcome": "effect_not_observed", "reason": "mc_task_not_found"}
        return {**found, "outcome": "unknown", "reason": f"mc_unreadable:{exc.code}"}
    except SourceUnavailable:
        return {**found, "outcome": "unknown", "reason": "mc_unreadable"}
    fields = {name: {"status": "equals_expected" if task.get(name) == value
                     else "empty" if task.get(name) in (None, "") else "differs"}
              for name, value in expected.items()}
    held = all(item["status"] == "equals_expected" for item in fields.values())
    return {"outcome": "effect_observed" if held else "effect_not_observed",
            "reason": None if held else "value_differs", "fields": fields,
            "task_id": task_id}


# PLX_MC @ 2f653cf ``src/lib/mc-data/types.ts:11-21`` StageKey (``cancelled`` since 77a5af6):
# every stage MC holds. Grounding and the dispatch re-check refuse any other value, so a stage
# MC adds later fails closed until it is mirrored here.
MC_STAGES = ("backlog", "specced", "approved", "planned", "progress", "qa", "review",
             "merged", "verified", "cancelled")
# PLX_MC ``src/lib/mc-data/policy.ts`` ``TERMINAL_STAGES`` / ``isClosedStage`` @ 77a5af6:
# ``merged``, ``verified`` and ``cancelled`` (TASK-2529) are closed; no work is gated on them.
CLOSED_STAGES = frozenset({"merged", "verified", "cancelled"})
# ``src/lib/sync/cancel.ts`` @ 2f653cf: a cancel and a reopen each append one event, and
# ``mc_get_task`` returns the task's newest 25 events (``read-actions.ts``
# ``GET_TASK_EVENT_LIMIT``, ``compliance/repo.ts`` ``eventsForTask``: ``ts`` ISO-8601).
CANCEL_CYCLE_EVENT_KINDS = frozenset({"task.cancelled", "task.reopened"})
TASK_EVENT_WINDOW = 25
GROUNDING_SCHEMA = "plx-mc-task-grounding/v1"


def ground_task(mc: MissionControlMcpClient, task_id: str, reason: str,
                known_gate_ids: Any = ()) -> dict[str, Any]:
    """Read MC before a gate is prepared. Reads only.

    ``mc_self_check`` must answer ``ok``, ``dataSource: "live"`` and
    ``boringGateMet: true`` (PLX_MC ``src/lib/mcp/actions.ts:65-76``,
    ``src/lib/mcp/honesty.ts:155-161``: "seed" means MC is not serving the
    SharePoint-synced records). ``mc_get_task`` must return the task (a
    restricted task is ``not_found``, ``src/lib/mcp/read-actions.ts:91-112``),
    in a stage of ``MC_STAGES`` that is not closed (an unknown stage is
    ``mc_task_stage_unknown``), and with no pending or approved gate carrying this
    exact reason that the intent does not already track: such a gate is a request that landed
    unrecorded (MC has no idempotency key for ``mc_request_approval`` and no way
    to withdraw a gate, ``src/lib/compliance/approvals.ts``), so a second one
    would be a duplicate. Returns ``{"refusal": code | None, "record": {...}}``;
    the record is what the human approving the call sees.
    """
    if type(mc) is not MissionControlMcpClient:
        raise TypeError("mc must be a MissionControlMcpClient")
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise ValueError("invalid_task_id")
    known = set(known_gate_ids or ())
    record: dict[str, Any] = {"schema": GROUNDING_SCHEMA, "task_id": task_id,
                              "evidence": "mc-read"}
    try:
        return _ground(mc, task_id, reason, known, record)
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        # A parseable but malformed MC answer: refuse cleanly, never raise past here.
        return {"refusal": "mc_unreadable", "record": {**record, "error": type(exc).__name__}}


def _ground(mc: MissionControlMcpClient, task_id: str, reason: str, known: set[str],
            record: dict[str, Any]) -> dict[str, Any]:
    try:
        check = mc.self_check()
    except UpstreamError as exc:
        return {"refusal": f"mc_unreadable:{exc.code}", "record": record}
    except (SourceUnavailable, ReadOnlyGuardError) as exc:
        return {"refusal": "mc_unreadable", "record": {**record, "error": type(exc).__name__}}
    if not isinstance(check, Mapping):
        return {"refusal": "mc_unreadable", "record": {**record, "error": "self_check_shape"}}
    record.update(data_source=check.get("dataSource"),
                  boring_gate_met=check.get("boringGateMet"),
                  last_sweep=check.get("lastSweep") if isinstance(check.get("lastSweep"), str)
                  else None)
    if check.get("ok") is not True:
        return {"refusal": "mc_self_check_failed", "record": record}
    if check.get("dataSource") != "live":
        return {"refusal": "mc_data_not_live", "record": record}
    if check.get("boringGateMet") is not True:
        return {"refusal": "mc_sync_gate_not_met", "record": record}
    try:
        task = mc.get_task(task_id)["data"]["task"]
    except UpstreamError as exc:
        return {"refusal": "mc_task_not_found" if exc.code == "not_found"
                else f"mc_unreadable:{exc.code}", "record": record}
    except (SourceUnavailable, ReadOnlyGuardError) as exc:
        return {"refusal": "mc_unreadable", "record": {**record, "error": type(exc).__name__}}
    if not isinstance(task, Mapping) or not isinstance(task.get("approvalGates") or [], list):
        return {"refusal": "mc_unreadable", "record": {**record, "error": "task_shape"}}
    gates = [g for g in task.get("approvalGates") or [] if isinstance(g, dict)]
    stage = task.get("stage") if isinstance(task.get("stage"), str) else None
    # A rejected gate can never authorize, so a rejected stray (as the lost-request
    # precondition asks the human to do) does not block; pending/approved ones do.
    untracked = sorted(str(g.get("id")) for g in gates
                       if g.get("reason") == reason and g.get("id") not in known
                       and g.get("status") != "rejected")
    owner = task.get("accountableOwner")
    record.update(stage=stage, title=str(task.get("title") or "")[:200] or None,
                  accountable_owner=owner if isinstance(owner, str) else None,
                  pending_gates=sum(1 for g in gates if g.get("status") == "pending"),
                  untracked_gates_with_this_reason=untracked)
    if stage is None:
        return {"refusal": "mc_task_stage_unreadable", "record": record}
    if stage in CLOSED_STAGES:
        return {"refusal": "mc_task_closed", "record": record}
    if stage not in MC_STAGES:
        return {"refusal": "mc_task_stage_unknown", "record": record}
    if untracked:
        return {"refusal": "mc_untracked_gate_for_this_action", "record": record}
    return {"refusal": None, "record": record}


def authorization_still_current(mc: MissionControlMcpClient, decision: Mapping[str, Any],
                                expected_reason: str, roster: ApproverRoster, *,
                                action_type: str, now: datetime) -> str | None:
    """Re-read the authorizing gate just before a dispatch. Reads only.

    MC has no route that withdraws a decided gate (``approvals.ts``
    ``decideApprovalGate``: a decided gate is final, 409 ``gate_already_decided``)
    and no way to edit one (``patchTask`` callers append or decide gates only).
    What *can* happen between the decision and the dispatch, in MC, is that the
    approval no longer stands: the task is no longer visible to our operator key
    (removed or restricted: ``not_found``, ``read-actions.ts:91-112``), the task
    was closed (``merged``/``verified`` once no gate is pending; ``cancelled`` even with
    one pending, ``policy.ts`` ``stageAdvanceViolation`` @ 77a5af6, whose gate
    ``decideApprovalGate`` still decides afterwards), the task is in a stage
    ``MC_STAGES`` does not know (``mc_task_stage_unknown``), the task was cancelled and
    reopened since the decision (``_cancel_cycle``), the gate no longer reads as an
    authorizing decision of this exact action by the decider recorded, or the
    decider is no longer an admin/owner on the roster or no longer approves the intent's
    ``action_type`` there, or MC's own expiry of the approval has passed. Each of those is
    ``approval_withdrawn:<why>``; ``None`` means the approval stands. A read that
    fails is ``mc_unreadable[:code]`` (nothing is dispatched; a later attempt may
    read again). Source: PLX_MC ``src/lib/compliance/approvals.ts``,
    ``src/lib/mc-data/policy.ts``, ``src/lib/mcp/read-actions.ts``,
    ``src/lib/sync/cancel.ts`` @ ``2f653cf``.

    Expiry (PLX_MC TASK-3408, ``approvals.ts`` ``decideApprovalGate`` and
    ``approval-signature.ts`` ``approvalExpiry``/``checkApproval`` @ ``2d2cac7``): MC stamps
    an approved gate with ``expiresAt`` (an unscoped gate, which is every gate the Workbench
    requests: ``decidedAt`` + 72h) and the approval stops covering at that time
    (``expiresAt <= now`` is expired, as ``checkApproval`` reads it). ``now`` is the
    caller's clock (the action path's). An ``expiresAt`` at or before ``now`` is
    ``approval_withdrawn:mc_approval_expired``; one that is not an ISO-8601 instant with an
    offset, or is not after ``decidedAt``, is ``approval_withdrawn:mc_approval_expiry_unreadable``.
    A gate without one (decided before MC stamped expiries; ``null`` is MC's own "not set",
    ``ApprovalGateState.expiresAt``) carries no MC bound and stays under the Workbench's own
    decision age (``ActionPath.decision_max_age``, checked on the dispatch). The gate's HMAC
    ``signature`` is not checked: its key is MC's server secret
    (``PLX_MC_APPROVAL_SIGNING_KEY``), which the Workbench never holds, and MC's own check
    (``mc_verify_approval``) never passes an unscoped gate (``scope_missing``). The read
    itself (``mc_get_task`` under our operator key) is what the Workbench trusts.
    """
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise TypeError("now must be a timezone-aware datetime")
    if type(mc) is not MissionControlMcpClient:
        raise TypeError("mc must be a MissionControlMcpClient")
    task_id, gate_id = decision.get("task_id"), decision.get("gate_id")
    try:
        data = mc.get_task(task_id)["data"]
        task, events = data["task"], data.get("events") or []
        stage, gates = task.get("stage"), task.get("approvalGates") or []
    except UpstreamError as exc:
        return ("approval_withdrawn:mc_task_not_found" if exc.code == "not_found"
                else f"mc_unreadable:{exc.code}")
    except (SourceUnavailable, ReadOnlyGuardError, KeyError, TypeError, AttributeError,
            ValueError):
        return "mc_unreadable"
    if not isinstance(stage, str) or not isinstance(gates, list) or not isinstance(events, list):
        return "mc_unreadable"
    if stage in CLOSED_STAGES:
        return "approval_withdrawn:mc_task_closed"
    if stage not in MC_STAGES:
        return "approval_withdrawn:mc_task_stage_unknown"
    tracked = [g for g in gates if isinstance(g, Mapping) and g.get("id") == gate_id]
    if len(tracked) != 1:
        return "approval_withdrawn:gate_absent"
    expires_raw = tracked[0].get("expiresAt")
    try:
        reread = mc.approval_decision(task_id, gate_id, expected_reason=expected_reason)
    except UpstreamError as exc:
        return f"mc_unreadable:{exc.code}"
    except (SourceUnavailable, ReadOnlyGuardError, KeyError, TypeError, AttributeError,
            ValueError):
        return "mc_unreadable"
    if reread.get("authorizes") is not True:
        return "approval_withdrawn:" + str(reread.get("reason_code") or "not_authorizing")
    if (canonical_email(str(reread.get("decided_by") or ""))
            != canonical_email(str(decision.get("decided_by") or ""))
            or reread.get("decided_at") != decision.get("decided_at")):
        return "approval_withdrawn:decision_changed"
    if expires_raw is not None:
        expires, decided = _instant(expires_raw), _instant(reread.get("decided_at"))
        if expires is None or decided is None or expires <= decided:
            return "approval_withdrawn:mc_approval_expiry_unreadable"
        if expires <= now:
            return "approval_withdrawn:mc_approval_expired"
    cycle = _cancel_cycle(events, reread.get("decided_at"))
    if cycle is not None:
        return "approval_withdrawn:" + cycle
    decider = str(reread.get("decided_by") or "")
    if roster.role_of(decider) not in DECIDING_ROLES:
        return "approval_withdrawn:decider_no_longer_admin_or_owner"
    if not roster.may_decide(decider, action_type):
        return "approval_withdrawn:decider_no_longer_approver_of_action_type"
    return None


def _instant(value: Any) -> datetime | None:
    """An ISO-8601 instant with its offset (MC's ``decidedAt`` and event ``ts``), else None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _cancel_cycle(events: Sequence[Any], decided_at: Any) -> str | None:
    """Whether the (open) task was cancelled or reopened at or after the decision.

    Decided: an approval recorded while its task was
    cancelled does not stand once the task is reopened, and neither does one whose task was
    cancelled and reopened after it: a cancel then a reopen changes the conditions it was
    granted on, so a new approval is needed. The task is open when this runs (a closed one
    is withdrawn before), so a ``task.cancelled`` or ``task.reopened`` event at or after
    ``decided_at`` means one of the two: ``task_reopened_after_cancel``. ``mc_get_task``
    returns only the newest 25 events: when all 25 are newer than the decision, or a time
    cannot be read, whether one came after it cannot be told (``task_history_unreadable``,
    fail closed). None: no cancel or reopen followed the decision.
    """
    decided = _instant(decided_at)
    if decided is None:
        return "task_history_unreadable"
    stamps = []
    for event in events:
        at = _instant(event.get("ts")) if isinstance(event, Mapping) else None
        if at is None:
            return "task_history_unreadable"
        if event.get("kind") in CANCEL_CYCLE_EVENT_KINDS and at >= decided:
            return "task_reopened_after_cancel"
        stamps.append(at)
    if len(stamps) >= TASK_EVENT_WINDOW and min(stamps) >= decided:
        return "task_history_unreadable"
    return None


# The MC journal read (plx.act verify): MC's own names, mirrored exactly.
# PLX_MC @ 2f653cf: ``src/lib/mc-data/types.ts:11-21`` StageKey (``MC_STAGES``),
# ``:316`` ApprovalGateStatus, ``:327-351`` ApprovalGate fields;
# ``src/lib/compliance/approvals.ts`` event kinds ``approval.requested`` (payload gateId,
# reason, runtime; checkoutRef and hasProposal since 77a5af6) and
# ``approval.decided`` (payload gateId, decision, reason, requestedBy, note);
# ``src/lib/compliance/repo.ts`` ``eventsForTask`` rows (seq, ts, kind, actor, repo,
# taskId, pr, payload), newest first, returned by ``mc_get_task``
# (``src/lib/mcp/read-actions.ts`` ``actionGetTask``).
JOURNAL_SCHEMA = "plx-mc-journal-read/v1"
GATE_STATUSES = ("pending", "approved", "rejected")
APPROVAL_EVENT_KINDS = ("approval.requested", "approval.decided")
_MAX_JOURNAL_GATES = 20


def read_journal(mc: MissionControlMcpClient, task_id: str, *, gate_id: str | None,
                 reason: str) -> dict[str, Any]:
    """Read one MC task's approval state for an action (one ``mc_get_task``; reads only).

    Returns MC's task ``stage``, the tracked gate (``gate_id``) and every gate whose
    ``reason`` is exactly this action's gate reason (``status``, ``requestedAt``,
    ``decidedAt``: MC's field names and values; an unknown value reads as None with
    ``*_known: false``), and the task's ``approval.requested`` / ``approval.decided``
    events for those gates (``seq``, ``ts``, ``kind``, ``gateId``, ``decision``). No
    identity (``requestedBy``, ``decidedBy``, event ``actor``) and no free text (gate
    ``reason``, decision ``note``) is projected: whether a decision authorizes is
    ``ActionPath.record_decision``'s re-check, never this read. A failed read is
    ``readable: false`` with ``reason`` (``mc_task_not_found`` for an unknown or
    restricted task, ``mc_unreadable[:code]`` otherwise); it never raises past here.
    Evidence: ``mc-read`` (whatever MC serves to our operator key at read time).
    """
    if type(mc) is not MissionControlMcpClient:
        raise TypeError("mc must be a MissionControlMcpClient")
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise ValueError("invalid_task_id")
    if gate_id is not None and (not isinstance(gate_id, str) or not _GATE_ID.fullmatch(gate_id)):
        raise ValueError("invalid_gate_id")
    record: dict[str, Any] = {"schema": JOURNAL_SCHEMA, "task_id": task_id,
                              "evidence": "mc-read", "readable": False}
    try:
        payload = mc.get_task(task_id)
        data = payload["data"]
        task = data["task"]
        gates = task.get("approvalGates") or []
        events = data.get("events") or []
        if not isinstance(gates, list) or not isinstance(events, list):
            raise TypeError("journal_shape")
    except UpstreamError as exc:
        return {**record, "reason": "mc_task_not_found" if exc.code == "not_found"
                else f"mc_unreadable:{exc.code}"}
    except (SourceUnavailable, ReadOnlyGuardError, KeyError, TypeError, AttributeError,
            ValueError) as exc:
        return {**record, "reason": "mc_unreadable", "error": type(exc).__name__}
    stage = task.get("stage")

    def gate_view(gate: Mapping[str, Any]) -> dict[str, Any]:
        status = gate.get("status")
        return {"id": gate.get("id"), "status": status if status in GATE_STATUSES else None,
                "status_known": status in GATE_STATUSES,
                "requestedAt": gate.get("requestedAt")
                if isinstance(gate.get("requestedAt"), str) else None,
                "decidedAt": gate.get("decidedAt")
                if isinstance(gate.get("decidedAt"), str) else None}

    ours = [g for g in gates if isinstance(g, Mapping) and isinstance(g.get("id"), str)
            and _GATE_ID.fullmatch(g["id"]) and g.get("reason") == reason]
    tracked = [g for g in gates if isinstance(g, Mapping) and gate_id is not None
               and g.get("id") == gate_id]
    ids = {g["id"] for g in ours} | ({gate_id} if gate_id else set())
    trail = []
    for event in events:
        body = event.get("payload") if isinstance(event, Mapping) else None
        if (isinstance(body, Mapping) and event.get("kind") in APPROVAL_EVENT_KINDS
                and body.get("gateId") in ids):
            decision = body.get("decision")
            trail.append({"seq": str(event.get("seq")) if event.get("seq") is not None
                          else None,
                          "ts": event.get("ts") if isinstance(event.get("ts"), str) else None,
                          "kind": event["kind"], "gateId": body["gateId"],
                          "decision": decision if decision in GATE_STATUSES[1:] else None})
    pending = sum(1 for g in gates if isinstance(g, Mapping) and g.get("status") == "pending")
    return {**record, "readable": True,
            "task": {"stage": stage if stage in MC_STAGES else None,
                     "stage_known": stage in MC_STAGES,
                     "closed": stage in CLOSED_STAGES,
                     # policy.ts stageAdvanceViolation: a pending gate freezes the stage
                     # (a cancel alone passes it, @ 77a5af6).
                     "pending_gates": pending, "stage_frozen": pending > 0},
            "gate": (gate_view(tracked[0]) if len(tracked) == 1 else None),
            "gate_found": (len(tracked) == 1) if gate_id is not None else None,
            "gates_with_this_reason": [gate_view(g) for g in ours[:_MAX_JOURNAL_GATES]],
            "gates_with_this_reason_total": len(ours),
            "events": trail[:_MAX_JOURNAL_GATES * 2]}


__all__ = ["ALL_ACTION_TYPES", "APPROVAL_EVENT_KINDS", "APPROVER_ROSTER_SCHEMA",
           "ActionApprovers", "ApproverRoster", "CITED_SOURCE_MAX", "CLOSED_STAGES",
           "DECIDING_ROLES", "DEFAULT_APPROVER_ROSTER", "GATE_STATUSES", "GROUNDING_SCHEMA",
           "JOURNAL_SCHEMA", "MC_STAGES", "MC_TASK_DESCRIPTION_MAX", "McBucketAudience",
           "OID_COMMITTED", "OID_GRAPH_USERS", "REQUEST_SCHEMA", "WITHHELD_NOT_READABLE",
           "WITHHELD_NOT_TEXT", "WITHHELD_UNRESOLVED",
           "audience_withholding", "authorization_for", "authorization_still_current",
           "canonical_email", "canonical_oid", "cited_sources", "ground_task",
           "load_approver_roster", "mc_length", "prepare_request", "read_journal",
           "read_created_task", "send_create_task", "send_request"]
