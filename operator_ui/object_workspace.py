"""Employee workspace over the generic ontology tools, read-only.

An employee finds any served business object with ``plx.find`` and opens it. The
workspace is the answer of the same tools the case host serves, called for the one
host-configured reader and joined around that object:

* the object card: ``plx.object`` (detailed): every value with its L1 citation
  (handle, field path, digest, lineage), authority rank, unit/currency and markings,
  the value conflicts, identity edges and the linked counts per link type;
* the relationship map: ``plx.around`` one hop along each link type the object card
  lists (weakest tier floor, so leads and contradicted links are shown, each with its
  tier, reason and why-not features, never as fact);
* the timeline: ``plx.timeline`` from the object and from each neighbour set, merged
  by event id and ordered by time; time-basis conflicts stay visible;
* the process: ``plx.process`` (stage states with their gaps, the current stage, the
  quantity checks of the three-way match with ``agree: null`` and the reason when a
  comparison is undecided);
* the quantities and money by role: the card's quantity and money values and the
  process's compared values, each with its role, unit or currency (never summed);
* every gap and conflict of every call, labelled by the call it came from;
* the originals: every L1 record any of these answers cites (card values, link
  reasons, events, process stages and comparisons, conflicts), one entry per cited
  capture with its version, SHA-256 row digests, source system (from its scheme) and
  what cites it; a record whose scheme the reader is not granted is listed as withheld
  with the reason, never dropped silently. Past the cap the list says how many it shows
  of how many (per family and for withheld captures), gives withheld captures at most
  half the slots, shares slots across source families and keeps the object's own card
  captures first, then process, link reasons, neighbours and events. With
  ``plx.documents`` in the tool table, the mail and documents around the object (each with
  its citation and the reader's withheld markers) join them;
* the goal-2 action intents whose cited evidence is one of the records this object
  was projected from (``operating.action_path.ActionPath.list_for_case``, for the same
  reader; a withheld intent shows only that it exists, never what it cites);
* the cases the reader may read whose shown citations name one of those records
  (``CaseStore.list`` and ``get`` for the same reader), each with its playbook evaluation
  as the case workspace shows it (``CaseWorkspace``'s evaluation, never a second
  evaluator): ``current`` or ``stale`` against the case now, ``withheld`` (nothing about
  it) unless the reader's view of the case is complete, ``none`` or ``not_configured``;
* with ``plx.investigate`` in the tool table, the question families (``ontology/plans``)
  whose root type is a found candidate's, and ``investigate`` runs one for a root.

Nothing is computed here that the tools did not answer for this reader, so permission
monotonicity is the tools' own: a reader sees what they may read, or a ``withheld`` /
``unknown`` marker, never a different decided value. A withheld count the tools do not
reveal stays ``null``. Source text stays the tools' ``untrusted_text`` envelope.

Citations open through ``source``, which delegates to the case workspace's
``CaseWorkspace.citation`` (the one L1 resolver of the console's case runtime, for the
same reader, with its text and page-citation truncation). Only schemes the reader's
grant covers open; without a configured resolver the citation is shown with its handle
and digest, not opened.

Grants and per-item permissions are the case host's (``HostAccess`` over
``operating.case_tools_host.CaptureCaseHostTools``, the host the MCP clients use): the
reader's ``ReadPolicy`` is decided once at the start of each request from the audiences
the readers enforce (Portal relations from the mirror audience, BC units from the live
audience, mail and SharePoint units from the capture stores), so a revocation takes
effect on the next find, card or source read and one answer never mixes grants; every
mail, SharePoint and attached-original record is re-gated per item by the host's item
gate. A cited record the reader may not open is listed as withheld with the reason
("not granted to you on this console", or its own item permissions), never dropped. A
mail, SharePoint or BC record opens through the host's ``plx.read``; a Portal row or an
attached original through ``CaseWorkspace.citation``. Without a host (a bare tool
table) only Portal rows open and the other families read "no host grant for this source".

The store's build time and source generations are shown on every answer
(``StoreStatus``), with a status per source: a store projected from another capture
generation of a source is refused naming that source, as is one whose provenance cannot
be checked; a capture it did not project is labelled not_projected, one it projected and
that is gone is labelled removed, and one with captures newer than the store's is
labelled stale.

Three controls write, and only to the Workbench's own stores, never to a business
system (``business_system_writes`` stays 0):

* confirm / reject a link (``review``): one ``plx.review`` decision in the review
  store (``ontology.reviews``, append-only), recorded by the host's tool for the bound
  session, so only a person signed in as an employee records one (a fixture or agent
  session is refused by the tool) and it cites the record versions the person was
  shown. The open object lists the reviews of its links (``reviews``); a link
  decision enters the projection at its next build. The same control confirms or
  corrects one value of the card (``confirm_value`` / ``correct_value``, citing the value
  or the page or span of the original the person read): the next ``plx.object`` shows it
  beside the source value, never in its place. It also allocates (``allocate``): of the
  open object's own quantity (the supply), a share in its unit to one record it reaches
  over a many-to-many link (the demand), citing that value. The open object lists, per
  many-to-many link the map shows it on and per quantity its card shows, ``plx.aggregate``'s
  own answer over that one member (``allocations``: ``allocation_review_required`` until
  a person's allocations give each record its share, ``allocation_conflict`` when they do
  not fit the member's value, never a split by inference); ``plx.review`` refuses a share
  that alone does not fit the value (``allocation_conflict``), and a recorded allocation
  enters the projection at its next build.
* propose a follow-up (``follow_up``): a new case, opened through the action path for the
  signed-in session (its subject is the recorded opener and author), holding the gap
  (``workbench.follow_up.record``, one case-store write) and one ``mc.task.create_follow_up``
  proposal traced to it, citing the object's record; the intent is registered and, on a
  host configured with an MC operator and follow-up bucket, its exact ``mc_create_task``
  call is prepared (``call_approval_required``). Nothing is approved or sent to Mission
  Control here: a person other than the author approves that exact call, and the host
  sends it. A double submit (same person, object and text) answers the same case.
* record a question family's answer on a case (``record_on_case``): the plan runs for the
  reader (as ``investigate``) and ``ontology_tools_investigate.record_follow_ups`` (the one
  path) records its gaps that ask a person as ``workbench.follow_up.record`` follow-ups and
  the drafts its engine words as ``workbench.draft.record`` notes, through the action path
  for the session (``ActionPath.execute_internal``: workbench-internal, approval none, each
  read back), each citing only what that answer showed the reader. The case is one the
  reader sees, that is open and that no other session's claim holds, checked before the
  plan runs or anything is written, or, named none, one opened for that person, family and
  root once the answer is checked (a second press answers it: what is recorded already
  reads ``exists``; once that case is closed, a press opens a fresh one). An answer longer
  than this console reads is recorded as typed partial (``answer_pages_unread``,
  ``answer_entries_cut``): a gap no read entry matches, and any draft from it, is not
  recorded (no citation would gate its text, and the root's never stand in) and is listed
  ``not_recorded`` with that reason. No proposal, intent or outbound call is made, so
  nothing is sent. ``case_records`` lists a case's recorded follow-ups and draft requests
  as the case store shows them to the reader now: every citation re-resolved for them, an
  item citing a record they cannot resolve now withheld with no text.

Ask (``ask``, with a planner configured by the host) runs the employee agent loop
(``operating.agent_loop.run``, unchanged: the one loop and the one evidence rule,
docs/decisions/2026-10-07-agent-evidence-contract.md) about the object in view, for
this reader: its session is an MCP server over this reader's case host
(``HostAccess.agent_server``: the employee gate on every call, the grant of the request
in progress), never a wider principal, and the question names the object, which must be
readable (an unreadable one is not found). The planner (a model) sees only what those
calls return. Each claim it keeps cites only records a tool returned resolved to this
reader in this run and passed the loop's claim gate (``operating.answer_gate`` inside
``agent_loop.run``, the one claim gate: each structured claim's value is checked by
reference against the record it cites); the workspace shows that verdict and runs no
check of its own. The answer's text (``text``) is code's: ``answer_gate.rendered_text`` of
the claims shown here and the planner's templates naming them, never its prose as written. A claim whose citations the run did not return, or that
failed the gate, is shown as unsupported (its text, with the gate's codes), except that
one citing a record this reader may not open (``access.decide``, the same filter as kept
claims) shows its reason and codes only, never its text; an answer with none left is an
abstention. Nothing is written, unless the request names a case (``case_id``, one this
reader can read in the case runtime's store): then the session, its tool calls, evidence
digests and cited digests, is kept on that case as one case event
(``operating.session_replay.record``; ``session``: its id, or why the case store refused
it) to be replayed later over the same evidence. ``ask_run`` is the same path as it ran,
unformatted: the Ask eval (``scripts.eval_real_agent ask``) scores exactly the claims shown here.
``case_records`` also lists the case's kept sessions as ``session_replay.sessions`` shows
them to this reader (question, stop, call count, recorded at, each citation's status now),
a session that touched a record the reader cannot read now as its withheld marker only, and
also one citing a record this console withholds from them (``access.decide``, as for a
claim). ``replay`` runs one again through the reader's own agent session (the Ask's
``HostAccess.agent_server``: ``session_replay.replay``, read-only tools only) and answers its
divergence report: each call whose evidence or error state changed, each cited record whose
digest changed. A session listed withheld is not replayed: no call runs. Replay re-checks the
evidence, not the planner's reasoning (the planner is not run again).

A third control takes the next legal step of a registered intent (``act``, with a
``WorkspaceActions`` configured by the host; ``actions_for`` lists the object's intents
with each step, and without one answers ``served`` false, so the page shows no act panel
rather than an error). Every step is one call of the existing ``ActionPath`` for the session:
request approval (the exact MC call of an MC-owned intent is prepared for another
person's approval; for an owner-system intent the MC approval request is prepared on a
named MC task and, once a person approved that exact request, sent through the MC
client's write door), check the decision (reads MC), dispatch (``send_call`` /
``dispatch``: only on a recorded approval of that exact call or an authorizing MC
decision; a repeat replays, never re-sends; owner-system targets stay refused by
``production_target_refusal``) and verify (read-back, the only way to ``verified``; an
uncertain outcome stays ``unknown``). Nothing here records an approval: approvals are a
person's other than every subject, the person who requested the approval included
(``ActionPath`` records them as a co-requester; separation of duties), recorded through
the approver's own door or decided in MC, and an agent never approves. Only a person's
console session holding a host-configured action principal gets a control; an intent the
reader cannot see, or whose proposal is withheld from them, answers like an unknown one.

No module here names an object type, link type, process or case: every name comes
from the tool answers.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import copy
from datetime import datetime, timezone
import functools
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from stat import S_ISREG
import threading
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology.types import Actor, ValidationError
from operating.explanations import explain

MAX_HANDLE = 4096
MAX_QUERY = 200
MAX_HOP_QUERIES = 12
MAX_TIMELINE_ROOTS = 1 + MAX_HOP_QUERIES
MAX_EVENTS = 200
MAX_GAPS = 300
MAX_ORIGINALS = 200
MAX_DOCUMENTS = 20
MAX_QUESTION = 1000
ASK_FIELDS = frozenset({"object_id", "question", "case_id", "family"})
# How the question names the object in view to the loop's planner.
ASK_FRAME = "{question}\n\nThe business object in view (plx object id): {object_id}"
ASK_NOTE = ("Each claim cites only records a tool returned to you, resolved, in this run "
            "(docs/decisions/2026-10-07-agent-evidence-contract.md), and passed the "
            "claim checks, which are fail-closed: a value that is not what its record "
            "holds, a number, ISO date or quantity word no checked value claim covers, or a "
            "name, status or date value the claim's text does not state, makes the claim "
            "unsupported. Unsupported claims are shown as such, never as the answer; one "
            "citing a record you may not open shows no text. Whether free text follows "
            "from its values is not checked.")
REPLAY_FIELDS = frozenset({"case_id", "session_id"})
REPLAY_NOTE = ("Replay runs the session's read-only tool calls again for you and compares the "
               "evidence each returned, and each record its answer cited, with what was "
               "recorded. It re-checks the evidence, not the planner's reasoning: the planner "
               "is not run again.")
# Cases whose action listings one open reads at most (each is one list_for_case call).
MAX_INTENT_CASES = 50
# Cases one open reads for its case panel at most (each is one CaseStore.get).
MAX_OBJECT_CASES = 50
# The case view's items that cite records (``CaseStore.get``).
CASE_ITEM_SECTIONS = ("findings", "calculations", "gaps", "proposals", "verifications", "links")
# A case's evaluation as the object view counts it: the stored report's freshness
# (current, stale, unknown) or why none is shown (``CaseWorkspace``'s evaluation status).
EVALUATION_STATES = ("current", "stale", "unknown", "withheld", "none", "not_configured",
                     "integrity_error")
TOOLS = ("plx.find", "plx.object", "plx.around", "plx.timeline", "plx.process")
DOCUMENTS_TOOL = "plx.documents"
INVESTIGATE_TOOL = "plx.investigate"
REVIEW_TOOL = "plx.review"
REVIEW_DECISIONS = ("accept_link", "reject_link", "confirm_value", "correct_value",
                    "allocate")
REVIEW_FIELDS = frozenset({"decision", "subject", "target", "link_type", "evidence", "value",
                           "allocation"})
AGGREGATE_TOOL = "plx.aggregate"
# (link type, quantity) pairs of one open object whose allocation state is read (one
# plx.aggregate call each); past it the answer says it is truncated.
MAX_ALLOCATION_CHECKS = 8
# plx.aggregate's gaps about one member over a many-to-many hop, the first present is its
# state (else ``not_split``: it reaches at most one record and nobody allocated it).
ALLOCATION_STATES = ("allocation_conflict", "allocation_review_required",
                     "group_link_withheld", "allocation_remainder_unallocated",
                     "reviewed_allocation_used", "n_to_m_single_reach")
FOLLOW_UP_CAPABILITY = "mc.task.create_follow_up"
FOLLOW_UP_GAP_ACTION = "workbench.follow_up.record"
FOLLOW_UP_STATES = ("absent_in_source", "inaccessible", "stale", "not_ingested", "unknown")
FOLLOW_UP_PRIORITIES = ("urgent", "high", "medium", "low")
FOLLOW_UP_FIELDS = frozenset({"object_id", "missing", "why", "who_can_answer", "state",
                              "priority", "case_id", "draft"})
# A follow-up's late-order supplier draft (``operating.proactive_engine.late_order_draft``)
# as a client sends it back: only a request, checked for shape; the console words it again
# from the store and records its own wording on the case, never sent, approved or sent to MC.
FOLLOW_UP_DRAFT_FIELDS = frozenset({"label", "status", "sent", "to", "subject", "text",
                                    "date_basis", "facts", "citations", "lines_late",
                                    "lines_listed", "unresolved", "internal_notes"})
MAX_FOLLOW_UP_DRAFT = 2000
MAX_FOLLOW_UP_DRAFT_CITATIONS = 100
RECORD_FIELDS = frozenset({"family", "root", "case_id"})
MAX_CASE_RECORDS = 200   # per list of a case's follow-ups and of its drafts
MAX_RECORD_PAGES = 20    # answer pages read for one record (the plan re-runs per page)
RECORD_NOTE = ("Recorded on the Workbench case only: each follow-up and draft request cites "
               "what this answer showed you. Nothing is sent, proposed or approved; a draft is "
               "a working note, never a fact.")
# The act control (``WorkspaceActions``): the next safe steps (``action_path.next_safe_step``)
# a person takes at this console. Each is the ``ActionPath`` method of that name, called
# with these readers after (actor, intent_id); any other step (another person's approval,
# a reconciliation, escalate, re-register, none) is shown with its detail, never taken.
ACTION_STEPS: Mapping[str, tuple[str, ...]] = {
    "prepare_call": (), "prepare_gate": ("task_id", "mc"), "request_gate": ("mc",),
    "record_decision": ("mc",), "reconcile_gate": ("mc",), "dispatch": ("registry", "mc"),
    "send_call": ("mc",), "read_back": ("read_back",), "read_back_call": ("mc",)}
ACTION_FIELDS = frozenset({"intent_id", "step", "task_id"})
STEP_LABELS = {"prepare_call": "Request approval of the call",
               "prepare_gate": "Request approval",
               "request_gate": "Send the approved approval request (once)",
               "record_decision": "Check the decision in Mission Control",
               "reconcile_gate": "Find the approval request in Mission Control",
               "dispatch": "Dispatch (once)", "send_call": "Send the approved call (once)",
               "read_back": "Verify by read-back", "read_back_call": "Verify by read-back"}
_NOT_CONFIGURED = {"mc": "mc_not_configured", "registry": "dispatcher_not_configured",
                   "read_back": "read_back_not_configured"}
# The steps that would put a request or a write towards the owning system in motion.
_GUARDED_STEPS = frozenset({"prepare_call", "prepare_gate", "request_gate", "dispatch",
                            "send_call"})
_INTENT_ID = re.compile(r"^act_[0-9a-f]{24}$")
ACTIONS_NOTE = ("Approval is a person's other than whoever the action is for, in Mission "
                "Control or on the exact call; this console never approves. With an action "
                "role you may request approval, dispatch once approved and verify.")
MAX_FOLLOW_UP_TEXT = 1000
# What is missing becomes the MC task title (mc_create_task: at most 500 characters).
MAX_FOLLOW_UP_TITLE = 500
MAX_FOLLOW_UP_CITATIONS = 3
MAX_ANSWERERS = 10
L1_SCHEMES = ("api://", "bc://", "graph-mail://", "sharepoint://", "original://")
# Without a case host (a bare tool table): only Portal rows open; the other families are
# withheld for want of a host grant.
GRANTED_SCHEMES = ("api://",)
GRANTED_FAMILIES = frozenset({"portal_api"})
NO_HOST_GRANT = "no host grant for this source"
# With the case host: a family or unit the reader's policy does not grant, and a record
# whose own item permissions (mail scope and ACL, SharePoint item permissions, an
# original's audience) refuse the reader.
NOT_GRANTED = "not granted to you on this console"
ITEM_REFUSED = "this record's own permissions do not admit you"
# Schemes opened through the case runtime's L1 resolver (CaseWorkspace.citation); the
# others through the host's plx.read.
CASE_RESOLVER_SCHEMES = ("api://", "original://")
_ITEM_ACL_SCHEMES = ("graph-mail://", "sharepoint://", "original://")
_FAMILY = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
# Which originals survive the cap first, by the label of their nearest citer: the
# object's own card, then its process and conflicts, link reasons, then neighbours and
# events.
CITER_PRIORITY = (("value ", "display of this object", "identity "),
                  ("process ", "conflict "), ("link ",),
                  ("display of linked", "event "))
# The source system of a capture, from its handle's scheme (never from a citation).
SOURCE_SYSTEMS = {"api": "Portal API", "bc": "Business Central", "graph-mail": "mail",
                  "sharepoint": "SharePoint", "original": "attached original"}
# The label of each capture source family (``ontology.project.CAPTURE_SOURCES``).
SOURCE_LABELS = {"portal_api": "Portal", "bc": "Business Central", "graph_mail": "mail",
                 "sharepoint": "SharePoint", "teams": "Teams",
                 "mission_control": "Mission Control", "brain": "knowledge"}
# The weakest tier: the map shows every visible link with its tier, never filtered to
# "facts"; tiers below "corroborated" are rendered as leads, contradicted as conflicts.
TIER_FLOOR = "contradicted"
READ_ONLY_NOTE = ("Read-only. Every value is cited; nothing here approves, dispatches or "
                  "changes a business record.")
EVIDENCE_UNSTATED = {
    "label": "projection store",
    "fixture": False,
    "note": ("The host did not state where this projection store was built from. Every "
             "value cites the capture it was projected from.")}


def record_of(handle: str) -> str:
    """The source record an L1 handle names, without its capture or fragment.

    ``api://<relation>/<key>@<generation>-<capture>#...`` -> ``api://<relation>/<key>``;
    an intent pinned to an earlier capture of the same record still concerns it.
    """
    base = handle.split("#", 1)[0]
    at = base.rfind("@")
    if at > base.rfind("/"):
        base = base[:at]
    return base


def version_of(handle: str) -> str | None:
    """The capture an L1 handle pins (``...@<generation>-<capture>`` -> that suffix)."""
    base = handle.split("#", 1)[0]
    at = base.rfind("@")
    return base[at + 1:] if at > base.rfind("/") else None


# The goal-2 action-path state of an intent, as its approval stage (operating/
# action_path.STATES). A dispatch is only possible from an authorized gate, so every
# dispatch state reads "approved". Unknown states stay "unknown", never "approved".
APPROVAL_STAGES = {
    "proposed": ("not_requested", "No approval has been requested yet"),
    "call_approval_required": ("awaiting", ("Awaiting a person's approval of the exact "
                                            "Mission Control call shown; nothing was sent")),
    "gate_prepared": ("not_requested", "An approval request is prepared; nothing was sent"),
    "call_approved": ("not_requested", ("A person approved requesting the gate; Mission "
                                        "Control has not been asked yet")),
    "gate_request_refused": ("not_requested", "Mission Control refused the approval request"),
    "gate_request_unknown": ("unknown", ("Whether Mission Control received the approval "
                                         "request is unknown")),
    "gate_request_lost": ("unknown", "The approval request was lost; it can be requested again"),
    "gate_requested": ("awaiting", "Awaiting a person's decision in Mission Control"),
    "gate_authorized": ("approved", "Approved in Mission Control; not dispatched yet"),
    "gate_rejected": ("rejected", "Rejected in Mission Control"),
    "gate_not_authorizing": ("not_authorizing", ("Mission Control's decision does not "
                                                 "authorize this action")),
    "approval_expired": ("expired", "The approval expired before dispatch"),
    "dispatch_refused": ("approved", "Approved; the dispatch was refused before sending"),
    "dispatched": ("approved", "Approved and dispatched; awaiting read-back"),
    "dispatched_unknown": ("approved", "Approved; whether the dispatch wrote is unknown"),
    "verified": ("approved", "Approved, dispatched and verified by read-back"),
    "failed": ("approved", "Approved; the owner system did not apply it"),
    "escalated": ("unknown", "Escalated to a person after an uncertain outcome"),
}


def approval_of(state: Any) -> dict[str, Any]:
    stage, label = APPROVAL_STAGES.get(state, ("unknown", f"State {state!r} is not known here"))
    return {"stage": stage, "label": label, "where": "Mission Control", "approved_here": False}


def _cited(value: Any, label: str, out: dict[str, dict[str, Any]]) -> None:
    """Every value citation (a mapping with ``l1_handle``) and every untrusted-text
    origin (``{"origin": {"citation": ...}}``) in ``value``, by handle."""
    if isinstance(value, Mapping):
        handle = value.get("l1_handle")
        origin = value.get("origin")
        if handle is None and isinstance(origin, Mapping) \
                and isinstance(origin.get("citation"), str):
            handle = origin["citation"]
            value = {"l1_handle": handle, "field_path": origin.get("field_path")}
        if isinstance(handle, str) and handle.startswith(L1_SCHEMES):
            item = out.setdefault(handle, {"digests": set(), "lineages": set(),
                                           "cited_by": [], "fields": set()})
            if isinstance(value.get("lineage"), str):
                item["lineages"].add(value["lineage"])
            if isinstance(value.get("digest"), str):
                item["digests"].add(value["digest"])
            if isinstance(value.get("field_path"), str):
                item["fields"].add(value["field_path"])
            if label not in item["cited_by"]:
                item["cited_by"].append(label)
            return
        for item in value.values():
            _cited(item, label, out)
    elif isinstance(value, list):
        for item in value:
            _cited(item, label, out)


def _handles(value: Any, out: set[str]) -> None:
    """Every L1 handle cited anywhere in a tool answer (values, origins, events)."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in ("l1_handle", "citation") and isinstance(item, str) \
                    and item.startswith(L1_SCHEMES):
                out.add(item)
            else:
                _handles(item, out)
    elif isinstance(value, list):
        for item in value:
            _handles(item, out)


def _records_of(card: Mapping[str, Any]) -> list[str]:
    """The source records an object card cites (its values and display)."""
    handles: set[str] = set()
    _handles(card.get("values") or [], handles)
    _handles((card.get("object") or {}).get("display"), handles)
    return sorted({record_of(handle) for handle in handles})


def _case_records(view: Mapping[str, Any]) -> set[str]:
    """The source records a case view's items cite with a handle the reader is shown
    (resolved or changed since cited; a withheld item's citations carry no handle)."""
    from operating.cases import CITATION_CONTENT_CHANGED

    out: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            handle = value.get("handle")
            if isinstance(handle, str) and handle.startswith(L1_SCHEMES) \
                    and value.get("status") in ("resolved", CITATION_CONTENT_CHANGED):
                out.add(record_of(handle))
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    for name in CASE_ITEM_SECTIONS:
        walk(view.get(name))
    return out


def _priority(item: Mapping[str, Any]) -> int:
    """How close a capture's nearest citer is to the object (lower is closer)."""
    return min((rank for label in item.get("cited_by") or []
                for rank, prefixes in enumerate(CITER_PRIORITY)
                if label.startswith(prefixes)), default=len(CITER_PRIORITY))


def _take(handles: Sequence[str], cited: Mapping[str, Mapping[str, Any]],
          slots: int) -> list[str]:
    """``slots`` of ``handles`` round-robin across source families (schemes), each
    family in citer priority, then handle order."""
    families: dict[str, list[str]] = {}
    for handle in sorted(handles, key=lambda h: (_priority(cited[h]), h)):
        families.setdefault(handle.split("://", 1)[0], []).append(handle)
    queues = [families[scheme] for scheme in sorted(families)]
    kept: list[str] = []
    turn = 0
    while len(kept) < slots and any(queues):
        queue = queues[turn % len(queues)]
        if queue:
            kept.append(queue.pop(0))
        turn += 1
    return kept


def _cap_originals(cited: Mapping[str, Mapping[str, Any]], cap: int,
                   withheld: Iterable[str] = ()) -> list[str]:
    """At most ``cap`` of the ``cited`` handles, never silently biased.

    Captures the reader may not open (``withheld``) (the "what you cannot see" signal) take at most
    half the slots unless openable ones leave more free; the rest are counted, not
    listed. In each pool the slots go round-robin across source families, so no family
    disappears because its scheme sorts late, and inside a family the object's own card
    captures come first, then process and conflicts, link reasons, then neighbours and
    events."""
    held = set(withheld)
    withheld = [h for h in cited if h in held]
    granted = [h for h in cited if h not in held]
    withheld_slots = min(len(withheld), max(cap // 2, cap - len(granted)))
    kept = _take(withheld, cited, withheld_slots)
    return sorted(kept + _take(granted, cited, cap - len(kept)))


def _sort_time(event: Mapping[str, Any]) -> tuple:
    time = event.get("time") or (event.get("window") or {}).get("start") or ""
    return (time == "", time, event.get("event_id") or "")


def _naming(code: str, sources: Sequence[str] | None) -> str:
    """A store refusal naming the sources it is about (``code:bc``), only those."""
    return f"{code}:{','.join(sources)}" if sources else code


@functools.cache
def grantable_sources() -> tuple[tuple[str, str], ...]:
    """(family, label) of every capture family a host grant can name: the families of
    the tools' audience map (``operating.ontology_tools._AUDIENCE_SOURCE``) that are
    capture sources. The one list the withheld sources ('what was searched', the
    console banner), the store status trim (``_store_for``) and the documents note read."""
    from operating.ontology_tools import _AUDIENCE_SOURCE

    granted = set(_AUDIENCE_SOURCE.values())
    return tuple((source, label) for source, label in SOURCE_LABELS.items()
                 if source in granted)


def _store_for(store: dict[str, Any], withheld: set[str]) -> dict[str, Any]:
    """The store status a reader is shown: a family withheld from them, or one no host
    grant names (``grantable_sources``), keeps its state but not the names of its
    relations with newer captures."""
    if "newer_relations" not in store:
        return store
    shown = {source for source, _ in grantable_sources()} - withheld
    return {**store, "newer_relations": [relation for relation in store["newer_relations"]
                                         if relation.split("/", 1)[0] in shown],
            "sources": {source: (entry if source in shown else
                                 {key: value for key, value in entry.items()
                                  if key != "newer_relations"})
                        for source, entry in store.get("sources", {}).items()}}


def _withheld(reason: str) -> dict[str, str]:
    return {"state": "withheld", "reason": reason}


class ApiOnlyAccess:
    """The access of a workspace without a case host (a bare tool table): Portal rows
    open through the case runtime's resolver; every other family is withheld for want
    of a host grant."""

    read = None

    def request(self):
        return nullcontext()

    def decide(self, handles: Iterable[str]) -> dict[str, dict[str, str] | None]:
        return {handle: None if handle.startswith(GRANTED_SCHEMES) else _withheld(NO_HOST_GRANT)
                for handle in handles}

    def withheld_sources(self) -> list[dict[str, str]]:
        return [{"source": source, "label": label, **_withheld(NO_HOST_GRANT)}
                for source, label in grantable_sources() if source not in GRANTED_FAMILIES]


class ObjectWorkspace:
    """The console's read-only object workspace for one host-configured reader.

    ``tools``: a case-host tool table (name -> spec with ``handler(arguments, actor)``,
    as ``operating.ontology_tools.register`` and its traverse/process siblings build
    it). ``sources``: an optional L1 resolver with ``resolve_citation``,
    ``citation_status`` and ``read_original`` (``operating.case_runtime.CaseRuntime``).
    ``intents``: an optional ``CaseIntents``. ``evidence``: the host's statement of
    what the projection store holds (e.g. fixture); the UI shows it on every view.
    ``access``: who may open which cited record, decided per request (``HostAccess``;
    default ``ApiOnlyAccess``): its ``request()`` scope holds one grant snapshot for a
    whole answer, ``decide(handles)`` answers a withheld marker or None per record,
    ``read(handle, text_offset)`` opens a record the case runtime's resolver does not.
    ``follow_ups``: an optional ``FollowUps`` (the propose-follow-up and record-on-case
    controls and the case list).
    ``actions``: an optional ``WorkspaceActions`` (the act control on registered intents).
    ``planner``: the Ask control's agent-loop planner (``operating.agent_loop.Planner``:
    a model, or a scripted one in tests); None: Ask is not served. ``ask_bounds``: the
    loop's ``Bounds`` (default the loop's).
    """

    def __init__(self, tools: Mapping[str, Mapping[str, Any]], actor: Actor, *,
                 sources: Any = None, intents: "CaseIntents | None" = None,
                 evidence: Mapping[str, Any] | None = None,
                 store_status: "StoreStatus | None" = None, access: Any = None,
                 follow_ups: "FollowUps | None" = None,
                 actions: "WorkspaceActions | None" = None,
                 planner: Callable[[Mapping[str, Any]], Any] | None = None,
                 ask_bounds: Any = None) -> None:
        if not isinstance(actor, Actor):
            raise ValueError("object_workspace_requires_actor")
        missing = [name for name in TOOLS[:4] if name not in tools]
        if missing:
            raise ValueError(f"object_workspace_requires_tools:{','.join(missing)}")
        self.tools = dict(tools)
        self.actor = actor
        self.sources = sources
        self.intents = intents
        self.evidence = dict(evidence) if evidence else dict(EVIDENCE_UNSTATED)
        self.store_status = store_status
        self.access = access if access is not None else ApiOnlyAccess()
        self.follow_ups = follow_ups
        self.actions = actions
        self.planner = planner
        self.ask_bounds = ask_bounds

    # ---- tool calls ------------------------------------------------------

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        spec = self.tools.get(name)
        if spec is None:
            return {"error": {"code": "tool_not_configured",
                              "message": f"{name} is not served by this console"}}
        return spec["handler"](dict(arguments), self.actor)

    def _base(self) -> dict[str, Any]:
        # The status of the generation answering this request (the host's reader).
        meta = getattr(self.access, "store_meta", None)
        store = self.store_status.read(
            meta() if meta is not None else None) if self.store_status is not None else {
            "state": "not_configured", "detail": "the host did not configure a store status"}
        if store.get("state") == "foreign":
            # Projected from another capture generation: its citations are not these.
            raise PermissionError(_naming("ontology_store_from_another_capture_generation",
                                          store.get("foreign_sources")))
        if store.get("state") == "unknown":
            # A recorded capture is unreadable, or none ties the store to this state.
            raise PermissionError(_naming("ontology_store_provenance_unverifiable",
                                          store.get("unverified_sources")))
        withheld = self.access.withheld_sources()
        return {"read_only": True, "note": READ_ONLY_NOTE, "evidence": dict(self.evidence),
                "business_system_writes": 0,
                "actor": {"tenant": self.actor.tenant,
                          "principals": sorted(self.actor.principals)},
                "sources_openable": self.sources is not None,
                "store": _store_for(store, {item["source"] for item in withheld}),
                "withheld_sources": withheld}

    # ---- find ------------------------------------------------------------

    def _request(self):
        return self.access.request()

    def close(self) -> None:
        close = getattr(self.access, "close", None)
        if close is not None:
            close()

    def bind(self, session: Any) -> None:
        """The session of the request in progress (``HostAccess.bind``)."""
        bind = getattr(self.access, "bind", None)
        if bind is not None:
            bind(session)

    def find(self, query: str | None, *, object_type: str | None = None,
             cursor: str | None = None) -> dict[str, Any]:
        with self._request():
            return explain(self._find(query, object_type=object_type, cursor=cursor))

    def workspace(self, object_id: str) -> dict[str, Any]:
        with self._request():
            return explain(self._workspace(object_id))

    def gap_inbox(self, *, inbox: Any = None) -> dict[str, Any]:
        """The gaps the scheduled engine found (``operating.proactive_engine``: set-based
        rules over the whole projection store, persisted in the inbox beside it), trimmed
        for this reader: a gap shows only when its subject and a citation of each of its
        evidence groups sit in audiences the reader's grant admits, at markings it holds,
        and pass the per-item decision. Each carries a proposed follow-up only a person
        sends. No grant (an API-only reader) or no store: ``not_configured``. ``inbox``:
        another inbox file over this store (the flagship script's one-off run); default:
        the one beside the store."""
        from operating import proactive_engine

        with self._request():
            base = self._base()   # a refused store answers nothing
            grant = getattr(self.access, "grant", None)
            path = getattr(self.store_status, "store_path", None)
            if grant is None or path is None:
                return {**base, "status": "not_configured", "gaps": []}
            may_read, held = grant()
            found = proactive_engine.read_inbox(
                proactive_engine.inbox_path(path) if inbox is None else inbox, path,
                reader=proactive_engine.reader_key(self.actor), may_read=may_read,
                held=held, decide=self.access.decide)
            return {**base, **found}

    def status_card(self, object_id: str | None = None, *, object_type: str | None = None,
                    reference: str | None = None, as_of: date | None = None
                    ) -> dict[str, Any]:
        """The status card of one record for this reader (``operating.status_card``): by
        ``object_id``, or by a case root's ``reference`` and ``object_type`` (one match
        or no card). Read with this reader's tools; a citation the access withholds is
        dropped, and a claim left uncited is not shown."""
        from operating import status_card

        with self._request():
            self._base()   # a refused store answers nothing
            day = as_of or datetime.now(timezone.utc).date()

            def readable(handles: Sequence[str]) -> set[str]:
                granted = self.access.decide(handles)
                return {handle for handle in handles if granted.get(handle) is None}

            if reference is not None:
                return status_card.card_for_reference(self._call, object_type, reference,
                                                      as_of=day, readable=readable)
            return status_card.card(self._call, object_id, as_of=day, readable=readable)

    def source(self, handle: str, *, text_offset: int = 0) -> dict[str, Any]:
        with self._request():
            return self._source(handle, text_offset=text_offset)

    def investigate(self, family: str, root: str) -> dict[str, Any]:
        with self._request():
            out = self._investigate(family, root)
            out["verdict"] = self._openable(investigation_verdict(family, out))
            return explain(out, self.access.decide)

    def review(self, decision: Any) -> dict[str, Any]:
        with self._request():
            return self._review(decision)

    def link_confirm(self, request: Any) -> dict[str, Any]:
        """A person confirms (or undoes, or lists) the link a blocked case is missing
        (``operating.human_links``): every decision is a ``plx.review`` record of this
        session, so the tool decides who may record it."""
        from operating import human_links

        with self._request():
            if REVIEW_TOOL not in self.tools:
                raise LookupError("review_not_served")
            base = self._base()
            return {**base, "link_confirm": human_links.request(self._call, request)}

    def follow_up(self, proposal: Any) -> dict[str, Any]:
        with self._request():
            return self._follow_up(proposal)

    def record_on_case(self, request: Any) -> dict[str, Any]:
        """Record a question family's answer on a case for the session (module doc)."""
        with self._request():
            return self._record_on_case(request)

    def case_records(self, case_id: Any) -> dict[str, Any]:
        """A case's recorded follow-ups and draft requests (with ``follow_ups``) and its kept
        Ask sessions (with the case runtime's store), as the reader sees them now."""
        with self._request():
            cases = self._case_store()
            if self.follow_ups is None and cases is None:
                raise LookupError("follow_up_not_served")
            base = self._base()
            return {**base,
                    **({"case": self._case_view(case_id)}
                       if self.follow_ups is not None else {}),
                    **({"sessions": self._sessions(cases, case_id)}
                       if cases is not None else {})}

    def replay(self, request: Any) -> dict[str, Any]:
        """One kept session of a case run again for this reader: its divergence report."""
        from operating import session_replay
        from operating.cases import CaseIntegrityError
        with self._request():
            cases = self._case_store()
            if cases is None or not callable(getattr(self.access, "agent_server", None)):
                raise LookupError("session_replay_not_served")
            if not isinstance(request, dict) or set(request) != REPLAY_FIELDS:
                raise ValueError("replay_fields_invalid")
            case_id, session_id = request["case_id"], request["session_id"]
            if not isinstance(session_id, str) or not session_id or len(session_id) > 200:
                raise ValueError("session_id_invalid")
            base = self._base()
            shown = next((item for item in self._sessions(cases, case_id)
                          if item["session_id"] == session_id), None)
            if shown is None:
                raise KeyError("session_not_found")
            if "withheld" in shown:
                return {**base, "note": REPLAY_NOTE, "case_id": case_id,
                        "replay": {"session_id": session_id, "replayed": False,
                                   "withheld": shown["withheld"]}}
            try:
                replayed = session_replay.replay(cases, self.access.agent_server(), case_id,
                                                 session_id)
            except CaseIntegrityError:
                raise ValueError("case_event_chain_broken") from None
            openable = {cite["handle"]: cite["openable"] for cite in shown["citations"]}
            for divergence in replayed.get("divergences") or ():
                if "handle" in divergence:
                    divergence["openable"] = openable.get(divergence["handle"], False)
            return {**base, "note": REPLAY_NOTE, "case_id": case_id, "replay": replayed}

    def ask(self, request: Any) -> dict[str, Any]:
        """The employee agent loop's answer about one object for this reader (module doc)."""
        with self._request():
            return self._answer(self._ask(request))

    def ask_run(self, request: Any) -> dict[str, Any]:
        """The Ask as it ran, through the one path ``ask`` shows (``_ask``), for a caller
        that measures it (``scripts.eval_real_agent``, ``eval.ask_agent``): ``outcome`` and
        ``reason`` as shown, ``claims`` the shown claims as the loop kept them (with their
        value claims), ``withheld`` the kept claims not shown, ``loop`` the loop's result."""
        with self._request():
            return self._ask(request)

    def _ask_served(self) -> bool:
        return self.planner is not None and callable(getattr(self.access, "agent_server",
                                                             None))

    def actions_for(self, object_id: str) -> dict[str, Any]:
        """The intents citing the object's records, each with the reader's next step. A
        console that takes no steps (no ``actions``) answers ``served`` false and lists
        nothing here: the object view already shows its intents read-only."""
        with self._request():
            if not isinstance(object_id, str) or not object_id or len(object_id) > 700:
                raise ValueError("object_id_required")
            base = self._base()
            card = self._call("plx.object", {"object_id": object_id})
            if (card.get("error") or {}).get("code") in ("object_not_found", "not_found"):
                raise KeyError("object_not_found")
            if card.get("error"):
                raise ValueError("object_id_invalid")
            if self.actions is None:
                return {**base, "read_only": True, "served": False, "object_id": object_id}
            return {**base, "read_only": False, "served": True, "note": ACTIONS_NOTE,
                    "object_id": object_id, "intents": self._intents(_records_of(card))}

    def act(self, request: Any) -> dict[str, Any]:
        """Take one step of a registered intent for the session (``WorkspaceActions``)."""
        with self._request():
            if self.actions is None:
                raise LookupError("actions_not_served")
            return self.actions.take(self.actor, request)

    def _intents(self, records: Sequence[str]) -> dict[str, Any]:
        if self.intents is None:
            return {"status": "not_configured"}
        listing = self.intents(self.actor, records)
        if self.actions is not None and listing.get("status") == "read":
            for intent in listing["intents"]:
                intent["control"] = self.actions.control(self.actor, intent)
            listing["note"] = ACTIONS_NOTE
        return listing

    def _find(self, query: str | None, *, object_type: str | None = None,
              cursor: str | None = None) -> dict[str, Any]:
        """``plx.find`` for the reader: ranked candidates, never a merged winner."""
        if query is None:
            return {"available": True}
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY:
            raise ValueError("object_query_1_to_200_chars_required")
        arguments: dict[str, Any] = {"query": query.strip(), "limit": 20}
        if object_type:
            arguments["object_type"] = object_type
        if cursor:
            arguments["cursor"] = cursor
        base = self._base()  # a refused store answers nothing, not even a search
        result = self._call("plx.find", arguments)
        error = result.get("error")
        if error and error.get("code") == "invalid_input":
            raise ValueError(f"invalid_find:{error.get('message', '')[:200]}")
        out = {**base, "available": True, "query": query.strip(), "find": result}
        if INVESTIGATE_TOOL in self.tools:
            types = {(c.get("object") or {}).get("object_type")
                     for c in result.get("candidates") or []}
            out["plans"] = {kind: plans for kind in sorted(t for t in types if t)
                            if (plans := plans_for(kind))}
        return out

    # ---- one question family ------------------------------------------------

    def _investigate(self, family: str, root: str) -> dict[str, Any]:
        """``plx.investigate``: one question family's plan run from ``root`` for the
        reader (the chain, cited values, gaps, conflicts and answerability)."""
        if not isinstance(family, str) or not _FAMILY.match(family):
            raise ValueError("investigation_family_required")
        if not isinstance(root, str) or not root or len(root) > 700:
            raise ValueError("investigation_root_required")
        if INVESTIGATE_TOOL not in self.tools:
            raise LookupError("investigate_not_served")
        base = self._base()
        result = self._call(INVESTIGATE_TOOL, {"family": family, "root": root})
        error = result.get("error") or {}
        if error.get("code") == "invalid_input":
            raise ValueError(f"invalid_investigation:{error.get('message', '')[:200]}")
        if error.get("code") in ("object_not_found", "not_found"):
            raise KeyError("object_not_found")
        return {**base, "family": family, "root": root, "investigation": result}

    def _record_on_case(self, request: Any) -> dict[str, Any]:
        """Run one question family for the reader and record its answer's follow-ups and
        draft requests on a case (``FollowUps.record``); the case is checked first."""
        if self.follow_ups is None:
            raise LookupError("follow_up_not_served")
        if not isinstance(request, dict) or set(request) - RECORD_FIELDS:
            raise ValueError("record_fields_invalid")
        case_id = self.follow_ups.case_for(self.actor, request.get("case_id"))
        investigated = self._investigate(request.get("family"), request.get("root"))
        family, root = investigated["family"], investigated["root"]
        answer, pages = self._whole_answer(family, root, investigated.pop("investigation"))
        case_id, recorded = self.follow_ups.record(self.actor, case_id, family, root, answer,
                                                   partial=pages.get("partial"))
        return {**investigated, "read_only": False, "note": RECORD_NOTE, "case_id": case_id,
                "answer_pages": pages, "recorded": recorded, "case": self._case_view(case_id)}

    def _case_view(self, case_id: Any) -> dict[str, Any]:
        """``FollowUps.listed`` with each resolved citation marked ``openable`` when this
        console opens it for the reader (the same decision as every citation it shows)."""
        listing = self.follow_ups.listed(self.actor, case_id)
        cites = [cite for item in listing["follow_ups"] + listing["drafts"]
                 for cite in item["citations"] if cite.get("l1_handle")]
        decided = self.access.decide(sorted({cite["l1_handle"] for cite in cites}))
        for cite in cites:
            handle = cite["l1_handle"]
            cite["openable"] = (cite["status"] == "resolved" and decided.get(handle) is None
                                and self._can_open(handle))
        return listing

    def _whole_answer(self, family: str, root: str, first: Mapping[str, Any]
                      ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The answer over its pages, read for the reader before anything is written: each
        page's entries joined once (up to the contract's ``MAX_ANSWER``), the first page's
        gaps (every page carries them all). A refused page refuses the record (the tool
        says ``cursor_stale`` when the answer changed between pages, so two answers are
        never joined). Past ``MAX_RECORD_PAGES`` pages or ``MAX_ANSWER`` entries the rest is
        not read and the pages say so with a typed ``partial`` (``answer_pages_unread``,
        ``answer_entries_cut``), which the recorder is given: a gap no read entry matches is
        not recorded (``not_recorded``), never cited by the root in their place."""
        from operating.ontology_tools_investigate import MAX_ANSWER

        def refused(result: Mapping[str, Any]) -> None:
            if result.get("error"):
                raise ValueError("investigation_not_recordable:"
                                 f"{str(result['error'].get('code'))[:64]}")
        refused(first)
        entries = list(first.get("answer") or ())
        seen = {json.dumps(entry, sort_keys=True) for entry in entries}
        cursor, read = (first.get("completeness") or {}).get("next_cursor"), 1
        while cursor and read < MAX_RECORD_PAGES:
            page = self._call(INVESTIGATE_TOOL, {"family": family, "root": root,
                                                 "cursor": cursor})
            refused(page)
            read += 1
            for entry in page.get("answer") or ():
                key = json.dumps(entry, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    entries.append(entry)
            cursor = (page.get("completeness") or {}).get("next_cursor")
        partial = ("answer_pages_unread" if cursor
                   else "answer_entries_cut" if len(entries) > MAX_ANSWER else None)
        return (dict(first, answer=entries[:MAX_ANSWER]),
                {"read": read, "complete": partial is None,
                 **({"partial": partial} if partial else {})})

    # ---- ask ----------------------------------------------------------------------

    def _ask(self, request: Any) -> dict[str, Any]:
        from operating import agent_loop, answer_gate

        if not self._ask_served():
            raise LookupError("ask_not_served")
        if not isinstance(request, dict) or set(request) - ASK_FIELDS:
            raise ValueError("ask_fields_invalid")
        object_id, question = request.get("object_id"), request.get("question")
        if not isinstance(object_id, str) or not object_id or len(object_id) > 700:
            raise ValueError("object_id_required")
        if not isinstance(question, str) or not question.strip() \
                or len(question) > MAX_QUESTION:
            raise ValueError("question_1_to_1000_chars_required")
        question = question.strip()
        cases, case_id = self._session_case(request.get("case_id"))
        base = self._base()
        card = self._call("plx.object", {"object_id": object_id})
        error = card.get("error") or {}
        if error.get("code") in ("object_not_found", "not_found"):
            raise KeyError("object_not_found")   # invisible and nonexistent read the same
        if error:
            raise ValueError("object_id_invalid")
        # The planner starts from what the reader sees: the card, and the question
        # family's plan run from it when one is named or only one fits (made by the loop
        # as its own calls, so evidence and the gate treat them as any other).
        seed = [{"tool": "plx.object", "arguments": {"object_id": object_id}}]
        family = self._ask_family(request.get("family"), card)
        if family is not None:
            seed.append({"tool": INVESTIGATE_TOOL,
                         "arguments": {"family": family, "root": object_id}})
        result = agent_loop.run(ASK_FRAME.format(question=question, object_id=object_id),
                                self.access.agent_server(), self.planner,
                                self.ask_bounds or agent_loop.Bounds(),
                                withholds=lambda handles: any(
                                    marker is not None
                                    for marker in self.access.decide(handles).values()),
                                seed=seed)
        claims = self._shown_claims(result["claims"])
        outcome, reason = result["outcome"], result["reason"]
        if outcome == "answered" and not claims:
            outcome, reason = "abstained", "no_cited_claims"
        # The answer text is code's, rendered from the claims this reader is shown only.
        asked = {"base": base, "object_id": object_id, "question": question,
                 "outcome": outcome, "reason": reason, "claims": claims,
                 "text": answer_gate.rendered_text(claims, result.get("prose")),
                 "withheld": len(result["claims"]) - len(claims), "loop": result}
        if cases is not None:
            anchors: set[str] = set()
            _handles(card.get("values") or [], anchors)
            _handles((card.get("object") or {}).get("display"), anchors)
            asked["session"] = self._keep_session(cases, case_id, result, sorted(anchors))
        return asked

    def _ask_family(self, family: Any, card: Mapping[str, Any]) -> str | None:
        """The question family whose plan an Ask starts from: the one it names, which
        must be a plan for the card's object type (else ``ValueError``, nothing runs), or
        the only one; None when none is served or several fit and none is named (the
        planner chooses)."""
        kind = (card.get("object") or {}).get("object_type")
        plans = [plan["family"] for plan in plans_for(kind)] \
            if INVESTIGATE_TOOL in self.tools and isinstance(kind, str) else []
        if family is not None:
            if not isinstance(family, str) or family not in plans:
                raise ValueError("ask_family_invalid")
            return family
        return plans[0] if len(plans) == 1 else None

    def _case_store(self) -> Any:
        """The case runtime's store an Ask keeps its sessions in (None: not served)."""
        return getattr(getattr(self.access, "runtime", None), "cases", None)

    def _sessions(self, cases: Any, case_id: Any) -> list[dict[str, Any]]:
        """The case's kept sessions as this reader may see them (module doc): a session
        citing a record ``access.decide`` withholds is shown as its marker too."""
        from operating import session_replay
        from operating.cases import CaseNotFound
        if not isinstance(case_id, str) or not case_id or len(case_id) > 200:
            raise ValueError("case_id_invalid")
        try:
            listed = session_replay.sessions(cases, self.actor, case_id)
        except CaseNotFound:
            raise KeyError("case_not_found") from None
        decided = self.access.decide(sorted({cite["handle"] for item in listed
                                             for cite in item.get("citations") or ()}))
        out: list[dict[str, Any]] = []
        for item in listed:
            head = {"session_id": item["session_id"], "seq": item["seq"]}
            if "withheld" in item or any(decided.get(cite["handle"]) is not None
                                         for cite in item["citations"]):
                out.append({**head, "withheld": session_replay.WITHHELD})
                continue
            out.append({**head, **{key: item[key] for key in (
                "at", "recorded_by", "question", "outcome", "reason")},
                "call_count": len(item["calls"]),
                "citations": [{**cite, "openable": self._can_open(cite["handle"])}
                              for cite in item["citations"]]})
        return out

    def _session_case(self, case_id: Any) -> tuple[Any, str | None]:
        """(the case store, the case) an Ask keeps its session on; (None, None) for none."""
        if case_id is None:
            return None, None
        from operating.cases import CaseNotFound
        cases = self._case_store()
        if cases is None:
            raise LookupError("ask_session_record_not_served")
        if not isinstance(case_id, str) or not case_id or len(case_id) > 200:
            raise ValueError("case_id_invalid")
        try:
            cases.get(self.actor, case_id)
        except CaseNotFound:
            raise KeyError("case_not_found") from None
        return cases, case_id

    def _keep_session(self, cases: Any, case_id: str, result: Mapping[str, Any],
                      anchors: Sequence[str]) -> dict[str, Any]:
        """Keep the run on the case, gated on the object in view's records (``anchors``);
        a refusal (closed, claimed by another session, no longer visible, a citation the
        store does not resolve, a store error) is reported beside the answer, not raised."""
        from operating import session_replay
        from operating.cases import CaseIntegrityError, CaseNotFound, CitationError
        try:
            receipt = session_replay.record(cases, self.actor, case_id, result, anchors)
        except (ValidationError, CitationError, CaseNotFound, CaseIntegrityError,
                sqlite3.Error) as error:
            return {"case_id": case_id, "recorded": False,
                    "refused": str(error)[:200] or type(error).__name__}
        return {"case_id": case_id, "recorded": True, "session_id": receipt["session_id"]}

    def _answer(self, asked: Mapping[str, Any]) -> dict[str, Any]:
        """The Ask's answer as the workspace shows it; its text rendered by code from the
        shown claims and the loop's prose (an objective's answer passes no text of its own)."""
        from operating import answer_gate

        result = asked["loop"]
        return {**asked["base"], "object_id": asked["object_id"],
                "question": asked["question"], "note": ASK_NOTE,
                "answer": {"outcome": asked["outcome"], "reason": asked["reason"],
                           "text": answer_gate.rendered_text(asked["claims"],
                                                             result.get("prose")),
                           "claims": [{"text": claim["text"],
                                       "citations": [{"l1_handle": handle,
                                                      "openable": self._can_open(handle)}
                                                     for handle in claim["citations"]],
                                       "check": claim["check"]} for claim in asked["claims"]],
                           "unsupported": result["unsupported_claims"],
                           "dropped_claims": result["dropped_claims"] + asked["withheld"],
                           "unknowns": result["unknowns"],
                           "claim_checks": result["claim_checks"],
                           "steps": result["steps"], "usage": result["usage"],
                           "tool_calls": [{key: call[key] for key in ("step", "tool",
                                                                      "is_error")}
                                          for call in result["tool_calls"]]},
                **({"session": asked["session"]} if "session" in asked else {})}

    def _shown_claims(self, claims: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """Each claim the loop kept, its claim gate passed, that cites no record the
        workspace withholds from the reader."""
        decided = self.access.decide(sorted({h for c in claims for h in c["citations"]}))
        # The loop and the workspace decide the reader's grant from the same pinned policy;
        # if they ever disagree the claim is not shown at all (its text may hold the record).
        return [claim for claim in claims
                if all(decided.get(handle) is None for handle in claim["citations"])]

    # ---- the person's controls -----------------------------------------------

    def _review(self, decision: Any) -> dict[str, Any]:
        """Confirm or reject one link, confirm or correct one value, or allocate one share
        of a quantity: a ``plx.review`` record decision. The tool decides who may record (the bound session's person)
        and re-checks every cited version for the reader; its refusal is the answer
        (``review.error``)."""
        if (not isinstance(decision, dict) or set(decision) - REVIEW_FIELDS
                or decision.get("decision") not in REVIEW_DECISIONS):
            raise ValueError("review_decision_invalid")
        if REVIEW_TOOL not in self.tools:
            raise LookupError("review_not_served")
        base = self._base()
        result = self._call(REVIEW_TOOL, {"mode": "record", **decision})
        error = result.get("error") or {}
        if error.get("code") == "invalid_input":
            raise ValueError(f"invalid_review:{error.get('message', '')[:200]}")
        return {**base, "review": result}

    def _reviews(self, object_id: str, around: Mapping[str, Any]) -> dict[str, Any]:
        """The reviews of the links shown: ``plx.review`` status for each link's
        subject (the object and its backward neighbours), as this reader may see them."""
        if REVIEW_TOOL not in self.tools:
            return {"status": "not_configured"}
        subjects = [object_id]
        for hop in around.get("hops") or []:
            for edge in (hop.get("result") or {}).get("edges") or []:
                if edge.get("from") not in subjects and isinstance(edge.get("from"), str):
                    subjects.append(edge["from"])
        reviews: list[dict[str, Any]] = []
        withheld, unread = 0, 0
        for subject in subjects[:MAX_HOP_QUERIES]:
            result = self._call(REVIEW_TOOL, {"mode": "status", "subject": subject})
            if result.get("error"):
                unread += 1
                continue
            reviews.extend(result.get("reviews") or [])
            withheld += (result.get("withheld") or {}).get("reviews") or 0
        return {"status": "read", "reviews": reviews, "withheld": withheld,
                "unread_subjects": unread + max(0, len(subjects) - MAX_HOP_QUERIES)}

    def _allocations(self, object_id: str, card: Mapping[str, Any],
                     around: Mapping[str, Any]) -> dict[str, Any]:
        """Per many-to-many link the map shows the object on and per quantity its card
        shows: ``plx.aggregate``'s answer for the object alone as the supply (a sum of that
        quantity over that one hop, grouped by the reached records), its state the first
        of ``ALLOCATION_STATES`` among the answer's gaps, with the card's values of the
        quantity (value, unit, citation) and the records the map shows it reaching."""
        member_set = getattr(self.access, "member_set", None)
        schema = getattr(self.access, "schema", None)
        if AGGREGATE_TOOL not in self.tools or member_set is None or schema is None:
            return {"status": "not_configured"}
        values: dict[str, list[dict[str, Any]]] = {}
        for value in card.get("values") or []:
            if value.get("value_type") == "quantity":
                values.setdefault(value["property"], []).append(
                    {key: value.get(key) for key in ("value", "unit", "citation")})
        pairs = []
        for hop in around.get("hops") or []:
            declared = schema.link_types.get(hop["link_type"]) or {}
            if declared.get("cardinality") != "N:M" or declared.get("from") == declared.get("to"):
                continue
            far = declared["to"] if hop["direction"] == "forward" else declared["from"]
            spec = schema.object_types.get(far) or {}
            group = next(iter(spec.get("display") or ()), None) or next(
                (p["name"] for p in spec.get("properties") or ()), None)
            if group is None:
                continue
            reached = [edge["to"] if edge.get("from") == object_id else edge.get("from")
                       for edge in (hop.get("result") or {}).get("edges") or []
                       if object_id in (edge.get("from"), edge.get("to"))]
            pairs.extend((hop, group, list(dict.fromkeys(reached)), prop) for prop in values)
        items = []
        handle = member_set(object_id) if pairs else None
        for hop, group, reached, prop in pairs[:MAX_ALLOCATION_CHECKS]:
            result = self._call(AGGREGATE_TOOL, {
                "set": handle, "measure": {"property": prop, "op": "sum"},
                "group_by": [group],
                "path": [{"link_type": hop["link_type"], "direction": hop["direction"]}]})
            gaps = [gap for gap in result.get("gaps") or [] if gap.get("reason")
                    in ALLOCATION_STATES]
            refusal = result.get("error") or result.get("refused")
            reasons = {gap["reason"] for gap in gaps}
            item = {"link_type": hop["link_type"], "direction": hop["direction"],
                    "property": prop, "values": values[prop], "reached": reached,
                    "state": "error" if refusal else next(
                        (state for state in ALLOCATION_STATES if state in reasons), "not_split"),
                    "gaps": gaps}
            if refusal:
                item["error"] = refusal
            items.append(item)
        return {"status": "read", "items": items, "truncated": len(pairs) > MAX_ALLOCATION_CHECKS}

    def _follow_up(self, proposal: Any) -> dict[str, Any]:
        """Propose a follow-up on one object the reader can read: a case proposal,
        registered as an intent but never approved or sent here. A ``draft`` in the
        proposal is only a request for the order's supplier draft: its text is never
        stored or shown. The draft is worded again here from the projection store for this
        reader (``operating.proactive_engine.order_draft``: the facts they may open) and
        recorded on the case as ``workbench.draft.record`` (``FollowUps.propose``); the
        answer carries the server's draft, read back from the case store, ``reworded``
        when the client's text was not it. No store or grant: ``follow_up_draft_not_served``;
        no late line this reader may open: ``follow_up_draft_not_derived``."""
        if self.follow_ups is None:
            raise LookupError("follow_up_not_served")
        if not isinstance(proposal, dict) or set(proposal) - FOLLOW_UP_FIELDS:
            raise ValueError("follow_up_fields_invalid")
        object_id = proposal.get("object_id")
        if not isinstance(object_id, str) or not object_id or len(object_id) > 700:
            raise ValueError("object_id_required")
        asked = _follow_up_draft(proposal.get("draft"))
        base = self._base()
        card = self._call("plx.object", {"object_id": object_id})
        if (card.get("error") or {}).get("code") in ("object_not_found", "not_found"):
            raise KeyError("object_not_found")
        if card.get("error"):
            raise ValueError("object_id_invalid")
        draft = None
        if asked is not None:
            from operating import proactive_engine

            grant = getattr(self.access, "grant", None)
            path = getattr(self.store_status, "store_path", None)
            if grant is None or path is None:
                raise LookupError("follow_up_draft_not_served")
            may_read, held = grant()
            draft = proactive_engine.order_draft(
                path, object_id, datetime.now(timezone.utc).date(),
                may_read=may_read, held=held, decide=self.access.decide)
            if draft is None:
                raise ValueError("follow_up_draft_not_derived")
        handles: set[str] = set()
        _handles(card.get("values") or [], handles)
        _handles((card.get("object") or {}).get("display"), handles)
        # Any record of the object the reader may open, whatever its source (a Portal row,
        # a BC document, a mail or SharePoint record, an original): the case store keeps
        # the ones it resolves for them.
        granted = self.access.decide(sorted(handles))
        cited = [handle for handle in sorted(handles) if granted.get(handle) is None]
        recorded = self.follow_ups.propose(
            self.actor, {k: v for k, v in proposal.items() if k != "draft"}, cited,
            draft=draft)
        if draft is not None:
            # The server's wording only; whether the client's differed is all that is said.
            stored = recorded.pop("draft_record")
            recorded = {**recorded, "draft": {**draft, **stored, "reworded": asked != {
                k: v for k, v in draft.items() if k in FOLLOW_UP_DRAFT_FIELDS}}}
        return {**base, "object_id": object_id, "proposal": recorded}

    # ---- one object --------------------------------------------------------

    def _workspace(self, object_id: str) -> dict[str, Any]:
        """Card, relationship map, timeline, process, gaps, conflicts and intents."""
        if not isinstance(object_id, str) or not object_id or len(object_id) > 700:
            raise ValueError("object_id_required")
        base = self._base()
        card = self._call("plx.object", {"object_id": object_id,
                                         "response_format": "detailed"})
        error = card.get("error")
        if error:
            if error.get("code") in ("object_not_found", "not_found"):
                # Invisible and nonexistent read the same (no oracle).
                raise KeyError("object_not_found")
            if error.get("code") == "invalid_input":
                raise ValueError("object_id_invalid")
            return {**base, "object_id": object_id, "card": card, "status": "error"}
        around = self._around(object_id, card)
        timeline = self._timeline(object_id, around)
        process = self._process(object_id)
        documents = self._documents(object_id)
        gaps, conflicts = self._gaps_and_conflicts(card, around, timeline, process,
                                                   documents)
        records = _records_of(card)
        return {**base, "status": "read", "object_id": object_id,
                "card": card, "around": around, "reviews": self._reviews(object_id, around),
                "allocations": self._allocations(object_id, card, around),
                "timeline": timeline, "process": process,
                "gaps": gaps, "conflicts": conflicts,
                "originals": self._originals(card, around, timeline, process, conflicts),
                "documents": documents,
                "intents": self._intents(records), "cases": self._cases(records),
                "ask": {"served": self._ask_served()},
                "answers": self._openable(object_answers(card, around, process))}

    def _cases(self, records: Sequence[str]) -> dict[str, Any]:
        """The cases the reader may read that cite one of the object's ``records``, each
        with the evaluation the case workspace shows this reader for it (``sources``, a
        ``CaseWorkspace``: its stored playbook report, ``withheld`` unless the reader's
        view of the case is complete, ``stale`` once the case changed after it). A case
        matches on the citations its view shows the reader (a withheld item shows none).
        At most ``MAX_OBJECT_CASES`` cases are read, newest first (``cases_listing_full``);
        one that cannot be read is counted, never shown. A case workspace for another
        reader lists and evaluates nothing (its grants are not this reader's)."""
        from operating.cases import CaseIntegrityError
        from operator_ui.case_workspace import CaseWorkspace

        workspace = self.sources
        if not isinstance(workspace, CaseWorkspace) or workspace.actor != self.actor:
            return {"status": "not_configured"}
        wanted = set(records)
        try:
            listed = workspace.list_cases(limit=MAX_OBJECT_CASES)["cases"]
        except (ValueError, sqlite3.Error) as error:
            return {"status": "unreadable", "error": str(error)[:120]}
        cases: list[dict[str, Any]] = []
        counts = dict.fromkeys(EVALUATION_STATES, 0)
        unreadable = 0
        for case in listed:
            try:
                view = workspace.runtime.cases.get(self.actor, case["case_id"])
            except (LookupError, ValidationError, CaseIntegrityError, sqlite3.Error):
                unreadable += 1
                continue
            hits = sorted(wanted & _case_records(view))
            if not hits:
                continue
            evaluation = workspace.evaluation(view["case_id"], view)
            state = (evaluation.get("freshness") if evaluation["status"] == "stored"
                     else evaluation["status"])
            counts[state] = counts.get(state, 0) + 1
            cases.append({"case_id": view["case_id"], "question": view["question"],
                          "status": view["status"], "cited_records": hits,
                          "evaluation_status": state, "evaluation": evaluation})
        return {"status": "read", "cases": cases, "evaluation_counts": counts,
                "cases_read": len(listed), "cases_unreadable": unreadable,
                "max_cases": MAX_OBJECT_CASES,
                "cases_listing_full": len(listed) >= MAX_OBJECT_CASES}

    def _around(self, object_id: str, card: Mapping[str, Any]) -> dict[str, Any]:
        """One ``plx.around`` hop per link type the card lists (its own link types)."""
        hops = []
        listed = [item for item in card.get("linked_counts") or []
                  if item.get("count") != 0 or item.get("tiers")]
        for item in listed[:MAX_HOP_QUERIES]:
            result = self._call("plx.around", {
                "from": object_id, "tier_floor": TIER_FLOOR, "limit": 50,
                "response_format": "detailed",
                "path": [{"link_type": item["link_type"], "direction": item["direction"]}]})
            hops.append({"link_type": item["link_type"], "direction": item["direction"],
                         "listed_count": item.get("count"), "result": result})
        return {"hops": hops, "link_types_listed": len(listed),
                "truncated": len(listed) > MAX_HOP_QUERIES,
                "tier_floor": TIER_FLOOR}

    def _timeline(self, object_id: str, around: Mapping[str, Any]) -> dict[str, Any]:
        """Events of the object and of each neighbour set, merged by event id."""
        roots = [("object", object_id)]
        for hop in around["hops"]:
            handle = (hop["result"] or {}).get("set")
            if handle and (hop["result"].get("objects") or []):
                roots.append((f"{hop['link_type']} {hop['direction']}", handle))
        events: dict[str, dict[str, Any]] = {}
        calls = []
        for label, root in roots[:MAX_TIMELINE_ROOTS]:
            result = self._call("plx.timeline", {"from": root, "limit": 50})
            calls.append({"root": label, "result": {
                key: result.get(key) for key in ("gaps", "conflicts", "withheld",
                                                 "completeness", "error", "meta")
                if result.get(key) is not None}})
            for event in result.get("events") or []:
                kept = events.setdefault(event["event_id"], {**event, "found_from": []})
                kept["found_from"].append(label)
        ordered = sorted(events.values(), key=_sort_time)
        return {"events": ordered[:MAX_EVENTS], "total": len(ordered),
                "truncated": len(ordered) > MAX_EVENTS
                or any((call["result"].get("completeness") or {}).get("truncated")
                       for call in calls), "calls": calls}

    def _process(self, object_id: str) -> dict[str, Any]:
        if "plx.process" not in self.tools:
            return {"status": "not_configured"}
        result = self._call("plx.process", {"from": object_id})
        error = result.get("error")
        if error and error.get("code") == "no_process":
            return {"status": "not_applicable", "detail": error.get("message"),
                    "hint": error.get("hint")}
        if error:
            return {"status": "error", "error": error}
        return {"status": "read", "result": result}

    def _documents(self, object_id: str) -> dict[str, Any]:
        """``plx.documents`` around the object for the reader, when the host serves it.

        Each document keeps its citation; it opens here only when the access decides the
        reader may open it now, otherwise it says why not."""
        if DOCUMENTS_TOOL not in self.tools:
            return {"status": "not_configured",
                    "withheld_sources": [label for source, label in grantable_sources()
                                         if source in ("graph_mail", "sharepoint")],
                    "reason": NO_HOST_GRANT}
        result = self._call(DOCUMENTS_TOOL, {"object_id": object_id,
                                             "limit": MAX_DOCUMENTS})
        if result.get("error"):
            return {"status": "error", "error": result["error"]}
        documents = [d for d in result.get("documents") or []
                     if isinstance(d.get("citation"), str)]
        decided = self.access.decide([d["citation"] for d in documents])
        for document in documents:
            marker = decided.get(document["citation"])
            document["openable"] = marker is None and self._can_open(document["citation"])
            if marker is not None:
                document["not_openable_reason"] = marker["reason"]
        return {"status": "read", "result": result}

    def _openable(self, answers: dict[str, Any]) -> dict[str, Any]:
        """Each verdict fact's citation marked ``openable`` by the same decision as every
        citation this console shows (``investigation_verdict``, ``object_answers``)."""
        facts = [fact for block in [answers, *answers.values()] if isinstance(block, dict)
                 for fact in block.get("facts") or ()]
        decided = self.access.decide(sorted({fact["citation"] for fact in facts
                                             if fact.get("citation")}))
        for fact in facts:
            handle = fact.get("citation")
            fact["openable"] = bool(handle) and decided.get(handle) is None \
                and self._can_open(handle)
        return answers

    def _can_open(self, handle: str) -> bool:
        """Whether a record this reader may see has a reader here to open it."""
        if handle.startswith(CASE_RESOLVER_SCHEMES):
            return self.sources is not None
        return self.access.read is not None

    @staticmethod
    def _gaps_and_conflicts(card: Mapping[str, Any], around: Mapping[str, Any],
                            timeline: Mapping[str, Any], process: Mapping[str, Any],
                            documents: Mapping[str, Any] | None = None
                            ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Every gap and conflict the calls answered, labelled with the call."""
        gaps: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []

        def add(origin: str, result: Mapping[str, Any] | None) -> None:
            for gap in (result or {}).get("gaps") or []:
                gaps.append({"from": origin, **gap})
            for conflict in (result or {}).get("conflicts") or []:
                conflicts.append({"from": origin, **conflict})

        add("plx.object", card)
        for hop in around["hops"]:
            add(f"plx.around {hop['link_type']} {hop['direction']}", hop["result"])
        for call in timeline["calls"]:
            add(f"plx.timeline {call['root']}", call["result"])
        if process.get("status") == "read":
            result = process["result"]
            add("plx.process", result)
            for stage in result.get("stages") or []:
                if stage.get("missing"):
                    gaps.append({"from": f"plx.process stage {stage['stage']}",
                                 **stage["missing"]})
            for condition in result.get("conditions") or []:
                if condition.get("missing"):
                    gaps.append({"from": f"plx.process condition {condition['condition']}",
                                 **condition["missing"]})
        if (documents or {}).get("status") == "read":
            add("plx.documents", documents["result"])
        by_state: dict[str, int] = {}
        for gap in gaps:
            by_state[gap.get("state", "unknown")] = by_state.get(gap.get("state", "unknown"),
                                                                 0) + 1
        return ({"items": gaps[:MAX_GAPS], "total": len(gaps), "by_state": by_state,
                 "truncated": len(gaps) > MAX_GAPS}, conflicts)

    def _originals(self, card: Mapping[str, Any], around: Mapping[str, Any],
                   timeline: Mapping[str, Any], process: Mapping[str, Any],
                   conflicts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Every L1 capture the answers cite, with its version, digests and citers.

        One entry per cited handle (a record cited at two captures has two entries, so
        a changed row is visible as two versions). ``openable`` says whether this reader
        can open it here; a record the access withholds (``decide``) is listed as withheld
        with the reason, never dropped silently. Past ``MAX_ORIGINALS`` the answer is marked
        ``truncated`` with the ``total``, each scheme's shown/total count and the
        withheld shown/total count (``_cap_originals`` decides which are listed)."""
        cited: dict[str, dict[str, Any]] = {}
        for value in card.get("values") or []:
            _cited(value.get("citation"), f"value {value.get('property')}", cited)
        _cited((card.get("object") or {}).get("display"), "display of this object", cited)
        for edge in card.get("identity") or []:
            _cited(edge.get("reason"), f"identity {edge.get('link_type')}", cited)
            _cited(edge.get("why_not"), f"identity {edge.get('link_type')} (against)", cited)
        for hop in around.get("hops") or []:
            for neighbour in (hop.get("result") or {}).get("objects") or []:
                _cited(neighbour.get("display"),
                       f"display of linked {neighbour.get('object_type')}", cited)
            for edge in (hop.get("result") or {}).get("edges") or []:
                _cited(edge.get("reason"), f"link {edge.get('link_type')}", cited)
                _cited(edge.get("why_not"), f"link {edge.get('link_type')} (against)", cited)
        for event in timeline.get("events") or []:
            _cited(event.get("citation"), f"event {event.get('event_type')}", cited)
        if process.get("status") == "read":
            result = process["result"]
            for stage in result.get("stages") or []:
                _cited(stage.get("citations"), f"process stage {stage.get('stage')}", cited)
            for check in result.get("quantity_checks") or []:
                _cited(check.get("values"),
                       "process check " + " vs ".join(check.get("roles") or []), cited)
        for conflict in conflicts:
            _cited(conflict.get("values"), f"conflict {conflict.get('subject')}", cited)
        out = []
        by_scheme: dict[str, dict[str, int]] = {}
        for handle in cited:
            counts = by_scheme.setdefault(handle.split("://", 1)[0], {"total": 0, "shown": 0})
            counts["total"] += 1
        decided = self.access.decide(list(cited))
        held = {handle for handle in cited if decided.get(handle) is not None}
        withheld = {"total": len(held), "shown": 0}
        for handle in _cap_originals(cited, MAX_ORIGINALS, held):
            item = cited[handle]
            marker = decided.get(handle)
            scheme = handle.split("://", 1)[0]
            by_scheme[scheme]["shown"] += 1
            properties = [label[len("value "):] for label in item["cited_by"]
                          if label.startswith("value ")]
            entry = {"l1_handle": handle, "record": record_of(handle),
                     "version": version_of(handle), "scheme": scheme,
                     "source": SOURCE_SYSTEMS.get(scheme, scheme),
                     "lineages": sorted(item["lineages"]),
                     "sha256": sorted(d.split(":", 1)[-1] for d in item["digests"]),
                     "fields": sorted(item["fields"]), "properties": properties,
                     "cited_by": item["cited_by"],
                     "openable": marker is None and self._can_open(handle)}
            if marker is not None:
                entry["withheld"] = dict(marker)
                withheld["shown"] += 1
            out.append(entry)
        return {"items": out, "total": len(cited), "truncated": len(out) < len(cited),
                "by_scheme": by_scheme, "withheld": withheld}

    # ---- one source record -------------------------------------------------

    def _source(self, handle: str, *, text_offset: int = 0) -> dict[str, Any]:
        """One cited L1 record as the reader may see it now.

        The access decides first: a record the reader may not open reads as not found
        (404, the same answer as a record that does not exist, so a refusal is no
        existence oracle); only a family no host serves here is refused with that reason
        (400, ``ApiOnlyAccess``). A Portal row or attached
        original then opens through ``CaseWorkspace.citation`` (the case runtime
        re-checks the reader's audience on this read); a mail, SharePoint or BC record
        through the host's ``plx.read`` (its gates, as-of-capture labels and refusals).
        """
        if (not isinstance(handle, str) or not handle or len(handle) > MAX_HANDLE
                or not handle.startswith(L1_SCHEMES)):
            raise ValueError("l1_citation_handle_required")
        marker = self.access.decide([handle]).get(handle)
        if marker is not None:
            if marker["reason"] == NO_HOST_GRANT:
                raise ValueError("citation_scheme_not_granted: " + NO_HOST_GRANT)
            raise KeyError("citation_unresolvable_for_actor")
        if not self._can_open(handle):
            raise LookupError("source_reader_not_configured")
        self._base()  # a store from another mirror never opens citations either
        if handle.startswith(CASE_RESOLVER_SCHEMES):
            view = self.sources.citation(handle, text_offset=text_offset)
        else:
            view = self.access.read(handle, text_offset=text_offset)
            if view.get("status") not in ("resolved", "changed_since"):
                raise KeyError("citation_unresolvable_for_actor")
            view = {**view, "kind": "source_read"}
        return {**view, "handle": handle}


class CaseIntents:
    """Goal-2 action intents that cite a record an object was projected from.

    Reads, for the reader, the cases they may see (``CaseStore.list``) and each case's
    intents (``ActionPath.list_for_case``, which withholds what the reader's view of
    the proposal withholds). An intent matches when one of its evidence or checked
    precondition citations names one of the object's source records (``record_of``).
    A withheld intent or one whose event chain does not verify cannot be matched to
    any object: it is counted in ``unmatchable`` over every case read (``scope``), never
    shown as unrelated and never presented as this object's. One open reads at most
    ``max_cases`` cases (newest first); ``cases_listing_full`` says when that bound hit.
    """

    def __init__(self, cases: Any, action_path: Any, *,
                 max_cases: int = MAX_INTENT_CASES) -> None:
        self.cases = cases
        self.action_path = action_path
        self.max_cases = max_cases

    def __call__(self, actor: Actor, records: Iterable[str]) -> dict[str, Any]:
        wanted = set(records)
        try:
            cases = self.cases.list(actor, limit=self.max_cases)
        except (ValidationError, sqlite3.Error) as error:
            return {"status": "unreadable", "error": str(error)[:120]}
        matched: list[dict[str, Any]] = []
        unmatchable = {"withheld": 0, "chain_broken": 0, "cases_unreadable": 0,
                       "cases_truncated": 0}
        for case in cases:
            try:
                listing = self.action_path.list_for_case(actor, case["case_id"])
            except (LookupError, ValidationError, sqlite3.Error):
                unmatchable["cases_unreadable"] += 1
                continue
            if listing.get("truncated"):
                unmatchable["cases_truncated"] += 1
            for intent in listing.get("intents") or []:
                if intent.get("integrity") == "action_event_chain_broken":
                    unmatchable["chain_broken"] += 1
                    continue
                if intent.get("withheld"):
                    unmatchable["withheld"] += 1
                    continue
                cited = [item.get("handle") for item in intent.get("evidence") or []]
                cited += [item.get("citation") for item in
                          intent.get("checked_preconditions") or []]
                hits = sorted({h for h in cited if isinstance(h, str)
                               and record_of(h) in wanted})
                if hits:
                    matched.append(_intent_summary(intent, hits))
        return {"status": "read", "intents": matched, "unmatchable": unmatchable,
                "scope": "all cases you can read, not only this object",
                "cases_read": len(cases), "max_cases": self.max_cases,
                "cases_listing_full": len(cases) >= self.max_cases,
                "note": ("Registered intents only. Each needs a person's approval in "
                         "Mission Control; this console never approves or dispatches.")}


def _intent_summary(intent: Mapping[str, Any], hits: Sequence[str]) -> dict[str, Any]:
    keys = ("case_id", "proposal_id", "intent_id", "state", "outcome", "capability",
            "owner_system", "expected_effect", "human_preconditions", "production_guard",
            "call_approved", "gate", "chain_ok", "next_safe_step", "evidence_label",
            "gate_reason")
    out = {key: intent.get(key) for key in keys}
    out["approval"] = approval_of(intent.get("state"))
    out.update(cited_records=list(hits),
               checked_preconditions=len(intent.get("checked_preconditions") or []),
               decisions=[{key: item.get(key) for key in ("status", "authorizes",
                                                          "reason_code", "decided_at",
                                                          "decider_role")}
                          for item in intent.get("decisions") or []],
               dispatches=[item.get("outcome") for item in intent.get("dispatches") or []],
               read_backs=[item.get("outcome") for item in intent.get("read_backs") or []])
    return out


def _follow_up_draft(draft: Any) -> dict[str, Any] | None:
    """A follow-up's draft message checked for shape (``FOLLOW_UP_DRAFT_FIELDS``): never
    sent, a ``Draft`` label on its text, its citations handles and every fact cited by one
    of them. Anything else is refused before anything is read or written."""
    if draft is None:
        return None
    if not isinstance(draft, dict) or set(draft) - FOLLOW_UP_DRAFT_FIELDS:
        raise ValueError("follow_up_draft_invalid")
    text, citations, facts = draft.get("text"), draft.get("citations"), draft.get("facts", [])
    if (draft.get("sent") is not False or draft.get("status") != "draft_not_sent"
            or not isinstance(text, str) or not text.startswith("Draft")
            or len(text) > MAX_FOLLOW_UP_DRAFT
            or not isinstance(citations, list)
            or not 1 <= len(citations) <= MAX_FOLLOW_UP_DRAFT_CITATIONS
            or not all(isinstance(h, str) and h.startswith(L1_SCHEMES) for h in citations)
            or not isinstance(facts, list)
            or not all(isinstance(f, dict) and f.get("citation") in citations for f in facts)):
        raise ValueError("follow_up_draft_invalid")
    return json.loads(json.dumps(draft))   # a copy of plain data


class FollowUps:
    """The propose-follow-up control over the action path (``ActionPath``) of one session.

    ``propose`` opens a case through ``ActionPath.open_case`` with the session (its subject
    is the recorded opener) whose question is the person's statement of what is missing
    (``objective_author_kind``: the session's author kind), records the gap through the
    catalog's ``workbench.follow_up.record`` (``ActionPath.execute_internal``: the absence
    guard runs, so "absent in source" needs a proof this console does not run and is
    refused), records one ``mc.task.create_follow_up`` proposal traced to that gap through
    ``ActionPath.record_proposal`` (the session's subject is its author: they can never
    approve it), registers the intent and prepares its exact MC call when the path is
    configured for it. With ``case_id`` (proposed from inside a case) the gap and proposal
    go on that case instead (``case_for``: seen, open, unclaimed). Nothing is approved or
    sent to MC. ``request_key`` (person, object, text and case) and the gap's own text make
    a double submit answer the same case, gap, proposal and intent.
    ``resolves(handle, actor)``: whether the case store resolves a citation for the
    reader (the runtime's resolver); the first ``MAX_FOLLOW_UP_CITATIONS`` that do are
    cited."""

    def __init__(self, path: Any, session: Any, *,
                 resolves: Callable[[str, Actor], bool] | None = None) -> None:
        self.path, self.session, self.resolves = path, session, resolves

    @staticmethod
    def _text(value: Any, name: str, maximum: int = MAX_FOLLOW_UP_TEXT) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"follow_up_{name}_required")
        return value.strip()

    def propose(self, actor: Actor, proposal: Mapping[str, Any],
                citations: Sequence[str], *, draft: Mapping[str, Any] | None = None
                ) -> dict[str, Any]:
        """``draft``: the server-worded supplier draft (``proactive_engine.order_draft``),
        recorded on the case through ``ontology_tools_investigate.record_draft`` before the
        gap, every citation of it resolving for the reader (else nothing is written) and
        read back from the case store (``listed``): the answer's ``draft_record``."""
        from operating.action_catalog import ActionRefused
        from operating.action_path import ActionPathError
        from operating.cases import PROPOSAL_STATUS
        from operating.ontology_tools_investigate import record_draft
        from operating.proactive_engine import MAX_DRAFT_CITATIONS

        missing = self._text(proposal.get("missing"), "missing", MAX_FOLLOW_UP_TITLE)
        why = self._text(proposal.get("why"), "why")
        who = proposal.get("who_can_answer")
        if (not isinstance(who, list) or not 1 <= len(who) <= MAX_ANSWERERS):
            raise ValueError("follow_up_who_can_answer_required")
        who = [self._text(item, "who_can_answer") for item in who]
        state = proposal.get("state", "unknown")
        if state not in FOLLOW_UP_STATES:
            raise ValueError("follow_up_state_invalid")
        priority = proposal.get("priority", "medium")
        if priority not in FOLLOW_UP_PRIORITIES:
            raise ValueError("follow_up_priority_invalid")
        # Proposed from inside a case: it attaches to that case (one the reader sees, open
        # and unclaimed, checked before anything is written); else it opens its own.
        case_id = self.case_for(actor, proposal.get("case_id"))
        if draft is not None and (
                len(draft["citations"]) > MAX_DRAFT_CITATIONS
                or not all(self.resolves is None or self.resolves(handle, actor)
                           for handle in draft["citations"])):
            raise ValueError("follow_up_draft_citation_unresolved")   # nothing written
        cited = [handle for handle in citations
                 if self.resolves is None or self.resolves(handle, actor)
                 ][:MAX_FOLLOW_UP_CITATIONS]
        request_key = "follow-up-" + hashlib.sha256(json.dumps(
            [sorted(actor.principals), proposal.get("object_id"), missing, why, who, state,
             priority, case_id], ensure_ascii=False).encode("utf-8")).hexdigest()[:40]
        path, cases = self.path, self.path.cases
        try:
            if case_id is None:
                case_id = path.open_case(actor, missing, session=self.session,
                                         objective_author_kind=self.session.author_kind,
                                         request_key=request_key)
            drafted = None if draft is None else record_draft(
                path, actor, case_id, draft["text"], draft["citations"], session=self.session)
            if drafted is not None and drafted["outcome"] == "refused":
                raise ValueError(f"follow_up_refused:{drafted['refused']}")
            view = cases.get(actor, case_id)
            # A repeat of this request finds its own gap and the proposal traced to it.
            gaps = [gap for gap in view.get("gaps") or ()
                    if (gap.get("missing"), gap.get("why"), gap.get("who_can_answer"))
                    == (missing, why, who)]
            gap_id = gaps[0]["id"] if gaps else path.execute_internal(
                actor, FOLLOW_UP_GAP_ACTION, case_id,
                {"missing": missing, "why": why, "who_can_answer": who, "state": state,
                 "citations": cited}, session=self.session)["receipt"]["gap_id"]
            known = [item for item in view.get("proposals") or ()
                     if item.get("capability") == FOLLOW_UP_CAPABILITY
                     and (item.get("payload") or {}).get("gap_id") == gap_id]
            proposal_id = known[0]["id"] if known else path.record_proposal(
                actor, case_id, FOLLOW_UP_CAPABILITY,
                {"gap_id": gap_id, "title": missing, "description": why,
                 "priority": priority},
                citations=cited, session=self.session)["proposal_id"]
            intent = path.register(actor, case_id, proposal_id, session=self.session)
        except (ActionPathError, ActionRefused) as error:
            raise ValueError(f"follow_up_refused:{error.code}") from None
        refusal = None
        if path.get(actor, intent["intent_id"])["state"] == "proposed":
            try:
                path.prepare_call(actor, intent["intent_id"], session=self.session)
            except ActionPathError as error:
                refusal = error.code   # e.g. subject_unknown, mc_follow_up_bucket_not_configured
        view = path.view(actor, intent["intent_id"])
        prepared = view.get("prepared_request")
        extra: dict[str, Any] = {}
        if drafted is not None:   # read back from the case store as the reader sees it
            listed = next((item for item in self.listed(actor, case_id)["drafts"]
                           if item["record_id"] == drafted["record_id"]), None)
            extra["draft_record"] = {
                "record_id": drafted["record_id"], "recorded": drafted["outcome"],
                "read_back": drafted["read_back"],
                "case_record": None if listed is None else {
                    key: listed.get(key) for key in ("status", "superseded", "citations")}}
        return {**extra, "case_id": case_id, "proposal_id": proposal_id,
                "capability": FOLLOW_UP_CAPABILITY, "status": PROPOSAL_STATUS,
                "duplicate": bool(known),
                "intent_id": view.get("intent_id"), "state": view.get("state"),
                "approval": approval_of(view.get("state")),
                "prepared_request": prepared, "not_prepared": refusal,
                "next_safe_step": view.get("next_safe_step"),
                "cited": cited, "executed": False, "approved": False}

    def case_for(self, actor: Actor, case_id: Any) -> str | None:
        """The case an answer is recorded on, checked before anything is written: one the
        reader sees (else not found, as a case that does not exist), that is open and that
        no other session's active claim holds (the case store's own check for this writer,
        ``CaseStore.outside_write``). None: none named, so ``record`` opens one."""
        from operating.cases import CaseClaimed, CaseNotFound
        if case_id is None:
            return None
        if not isinstance(case_id, str) or not case_id or len(case_id) > 200:
            raise ValueError("case_id_invalid")
        try:
            view = self.path.cases.get(actor, case_id)
            if view.get("status") != "open":
                raise PermissionError("case_closed")
            with self.path.cases.outside_write(actor, case_id):
                pass
        except CaseNotFound:
            raise KeyError("case_not_found") from None
        except CaseClaimed:
            raise PermissionError("case_claimed") from None
        return case_id

    def record(self, actor: Actor, case_id: str | None, family: str, root: str,
               answer: Mapping[str, Any], *, partial: str | None = None
               ) -> tuple[str, list[dict[str, Any]]]:
        """``ontology_tools_investigate.record_follow_ups`` of ``answer`` (a
        ``plx.investigate`` answer as the reader got it; ``partial``: why it is not whole) on
        ``case_id`` through this path for the session. Without a case, the answer is checked
        first (``check_answer``) and it is recorded on one opened for the person, family and
        root (``request_key``: a repeat answers the same case; once that case is closed, the
        next key is chained on it, so a press walks the closed ones to the open case, else
        opens a fresh one: each step is a case it opened before, so the walk ends). Returns
        the case and the recorder's items."""
        from operating import ontology_tools_investigate as oi
        from operating.action_path import ActionPathError
        from operating.cases import CaseNotFound

        if case_id is None:
            oi.check_answer(answer)
            request_key = "investigation-" + hashlib.sha256(json.dumps(
                [sorted(actor.principals), family, root], ensure_ascii=False
            ).encode("utf-8")).hexdigest()[:40]
            while True:
                try:
                    # The Workbench's wording (its author unknown), naming no record: a
                    # question is not gated by citations, the records each follow-up names are.
                    case_id = self.path.open_case(
                        actor, f"What a {family} answer leaves open, and who can answer it",
                        session=self.session, request_key=request_key)
                except ActionPathError as error:
                    raise ValueError(f"record_refused:{error.code}") from None
                if self.path.cases.get(actor, case_id).get("status") == "open":
                    break
                request_key = "investigation-" + hashlib.sha256(json.dumps(
                    [request_key, case_id]).encode("utf-8")).hexdigest()[:40]
            case_id = self.case_for(actor, case_id)
        try:
            return case_id, oi.record_follow_ups(self.path, actor, case_id, answer,
                                                 session=self.session, partial=partial)
        except CaseNotFound:
            raise KeyError("case_not_found") from None

    def listed(self, actor: Actor, case_id: Any) -> dict[str, Any]:
        """The follow-ups and draft requests on a case (each ``workbench.follow_up.record``
        and ``workbench.draft.record`` this path executed on it), as the case store shows
        them to the reader now: every citation re-resolved for them, an item citing one they
        cannot resolve withheld (no text, no handle), a changed one labelled."""
        from operating.cases import CaseNotFound
        from operating.ontology_tools_investigate import DRAFT_ACTION, FOLLOW_UP_ACTION
        if not isinstance(case_id, str) or not case_id or len(case_id) > 200:
            raise ValueError("case_id_invalid")
        try:
            view = self.path.cases.get(actor, case_id)
            attempts = self.path.internal_actions(actor, case_id)
        except CaseNotFound:
            raise KeyError("case_not_found") from None
        made: dict[str, str] = {}
        for attempt in attempts:
            if attempt["outcome"] == "executed" and attempt["action_id"] in (FOLLOW_UP_ACTION,
                                                                             DRAFT_ACTION):
                record_id = ((attempt["record"] or {}).get("read_back") or {}).get("record_id")
                if record_id:
                    made[record_id] = attempt["action_id"]
        gaps = {gap["id"]: gap for gap in view.get("gaps") or ()}
        findings = {item["id"]: item
                    for items in (view.get("findings") or {}).values() for item in items}

        def cites(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
            return [{"l1_handle": c["handle"], "status": c["status"]} if c.get("handle")
                    else {"status": c.get("status")} for c in items]

        follow_ups: list[dict[str, Any]] = []
        drafts: list[dict[str, Any]] = []
        for record_id, action in made.items():
            if action == FOLLOW_UP_ACTION and record_id in gaps:
                gap = gaps[record_id]
                follow_ups.append({"record_id": record_id, **{key: gap.get(key) for key in (
                    "missing", "why", "who_can_answer", "state")},
                    "citations": cites(gap.get("citations") or ()),
                    **({"content": gap["content"]} if gap.get("content") else {})})
            elif action == DRAFT_ACTION and record_id in findings:
                item = findings[record_id]
                drafts.append({"record_id": record_id, "statement": item.get("statement"),
                               "status": item.get("status"),
                               "citations": cites(item.get("citations") or ()),
                               # The store's flag, a withheld item's too (its bucket is
                               # "withheld", whether superseded or not).
                               "superseded": bool(item.get("superseded")),
                               **({"content": item["content"]} if item.get("content") else {})})
        return {"case_id": case_id, "status": view.get("status"),
                "follow_ups": follow_ups[:MAX_CASE_RECORDS],
                "drafts": drafts[:MAX_CASE_RECORDS],
                "truncated": max(len(follow_ups), len(drafts)) > MAX_CASE_RECORDS}


class WorkspaceActions:
    """The act control over the action path (``ActionPath``) of one console session.

    ``actors``: the principals that hold the action role on this console (host
    configuration; empty: nobody acts). ``mc``: the operator's ``MissionControlMcpClient``
    (its write door opted in to the calls the host allows: ``mc_request_approval``,
    ``mc_create_task``); ``registry``: the ``DispatcherRegistry``; ``read_back``: the
    isolated owner's read-back (``FixtureSystem.read_back`` /
    ``goal2_exec_owner.read_back_for``). A step whose reader is missing is refused
    (``<reader>_not_configured``) before the action path is called.

    ``take`` maps one step to one ``ActionPath`` call for the session and lets the path
    decide: approvals, separation of duties, preconditions, the production guard,
    idempotent replay and recovery are all the path's. A step the path refuses is
    answered with its code and nothing else changes.
    """

    def __init__(self, path: Any, session: Any, *, actors: Iterable[str] = (),
                 mc: Any = None, registry: Any = None,
                 read_back: Callable[[Any], Mapping[str, Any]] | None = None) -> None:
        self.path, self.session = path, session
        self.actors = frozenset(actors)
        self.mc, self.registry, self.read_back = mc, registry, read_back

    def refusal(self, actor: Actor) -> str | None:
        """Why ``actor`` may take no step here (None: they may)."""
        from operating.session_identity import AUTHOR_HUMAN

        if self.session is None or getattr(self.session, "actor", None) != actor:
            return "session_required"
        if getattr(self.session, "author_kind", None) != AUTHOR_HUMAN:
            return "only_a_person_acts"   # an agent never acts on, nor approves, an intent
        if not set(actor.principals) & self.actors:
            return "no_action_role"
        return None

    def control(self, actor: Actor, intent: Mapping[str, Any]) -> dict[str, Any] | None:
        """The reader's control for one listed intent; None when they get none (a
        withheld intent, no action role). Its step is the path's ``next_safe_step``."""
        if intent.get("withheld") or not intent.get("intent_id") or self.refusal(actor):
            return None
        safe = intent.get("next_safe_step") or {}
        step = safe.get("step")
        if step in ACTION_STEPS:
            guard = intent.get("production_guard")
            why = self._missing(step) or (guard if step in _GUARDED_STEPS else None)
        else:   # not a console step: someone else's, or nothing; the path says why
            why = safe.get("detail") or f"state {intent.get('state')!r} has no step here"
        return {"step": step, "label": STEP_LABELS.get(step), "by": safe.get("by"),
                "available": step in ACTION_STEPS and not why, "why": why,
                "needs_task_id": step == "prepare_gate",
                "outcome": intent.get("outcome"), "done": intent.get("outcome") == "verified"}

    def _missing(self, step: str) -> str | None:
        for reader in ACTION_STEPS[step]:
            if reader in _NOT_CONFIGURED and getattr(self, reader) is None:
                return _NOT_CONFIGURED[reader]
        return None

    def take(self, actor: Actor, request: Any) -> dict[str, Any]:
        from operating.action_path import ActionPathError, IntentNotFound

        if not isinstance(request, dict) or set(request) - ACTION_FIELDS:
            raise ValueError("action_fields_invalid")
        intent_id, step = request.get("intent_id"), request.get("step")
        if not isinstance(intent_id, str) or not _INTENT_ID.fullmatch(intent_id):
            raise ValueError("intent_id_required")
        if step not in ACTION_STEPS:
            raise ValueError("action_step_invalid")
        try:
            view = self.path.view(actor, intent_id)
        except IntentNotFound:
            raise KeyError("intent_not_found") from None
        except ActionPathError as error:
            raise ValueError(error.code) from None
        if view.get("withheld"):
            raise KeyError("intent_not_found")   # a withheld intent reads as an unknown one
        refusal = self.refusal(actor)
        if refusal:
            raise PermissionError(refusal)
        result: Mapping[str, Any] = {}
        try:
            missing = self._missing(step)
            if missing:
                raise ActionPathError(missing)
            readers = {"mc": self.mc, "registry": self.registry, "read_back": self.read_back,
                       "task_id": request.get("task_id")}
            result = getattr(self.path, step)(actor, intent_id,
                                               *(readers[name] for name in ACTION_STEPS[step]),
                                               session=self.session)
            refused = None
        except ActionPathError as error:
            refused = error.code
        except IntentNotFound:
            raise KeyError("intent_not_found") from None
        try:   # access revoked since the step: answered like an unknown intent
            after = self.path.view(actor, intent_id)
        except IntentNotFound:
            raise KeyError("intent_not_found") from None
        return {"intent_id": intent_id, "step": step, "refusal": refused,
                "result": {key: result[key] for key in ("state", "replayed",
                                                        "reconcile_required", "reason")
                           if key in result},
                "state": after["state"], "outcome": after["outcome"],
                "done": after["outcome"] == "verified", "approval": approval_of(after["state"]),
                "control": self.control(actor, after), "next_safe_step": after["next_safe_step"],
                "evidence_label": after["evidence_label"]}


# ---- host wiring -----------------------------------------------------------

SERVED_TOOLS = TOOLS + (DOCUMENTS_TOOL, INVESTIGATE_TOOL, AGGREGATE_TOOL)


def audience_unit(handle: str) -> str | None:
    """The audience unit (C0 audience key) a cited record belongs to: ``api:<relation>``
    for a Portal row, the capture kind's unit (``bc:...``, ``mail:<mailbox>``) for a
    versioned capture, None when the handle names no unit (an attached original, a
    SharePoint version or live mail handle: their item gate decides)."""
    if handle.startswith("api://"):
        relation = handle[len("api://"):].split("/", 1)[0]
        return f"api:{relation}" if relation else None
    from integrations.capture_store import CaptureError, parse_handle
    try:
        kind, path, _, _ = parse_handle(handle.split("#", 1)[0])
    except (ValueError, CaptureError):
        return None
    return kind.unit(path)


class HostAccess:
    """One console reader's tools, grants and per-item gates: the case host's.

    ``host``: the reader's ``CaptureCaseHostTools`` (``host_access`` builds it; its
    ``policy_now`` answers the policy pinned by ``request``). ``request()`` resolves the
    reader through the runtime's employee gate and decides the ``ReadPolicy`` once (the
    audiences as they are now), so every tool call, item decision and read of one answer
    uses the same grant; a refused employee is a ``PermissionError``. ``decide`` withholds
    a record whose unit the policy does not grant (``NOT_GRANTED``) or whose own item
    permissions refuse the reader (``ITEM_REFUSED``: the host's item gate, the one
    ``plx.find``/``plx.documents``/``plx.read`` use for mail, SharePoint and originals).
    One ``decide`` batch is one host call: its live re-reads share one request bound.
    ``read`` is the host's ``plx.read`` (``PagedRead``) for the reader. ``registered``:
    tools the host serves through ``register_tool`` (``plx.review``), called with the
    host's session actor as the host does.
    """

    def __init__(self, host: Any, runtime: Any, actor: Actor, *,
                 registered: Sequence[str] = ()) -> None:
        self.host, self.runtime, self.actor = host, runtime, actor
        self.registered = tuple(registered)
        self.session: Any = None
        self._local = threading.local()
        host.pinned = self._pinned_policy

    def close(self) -> None:
        self.host.close()

    def bind(self, session: Any) -> None:
        """The session of the request in progress (the author of a review); it must
        be this reader's."""
        if session.actor != self.actor:
            raise PermissionError("session_is_not_this_reader")
        self.session = session

    def _pinned_policy(self) -> Any:
        state = getattr(self._local, "state", None)
        return state[1] if state is not None else None

    def _gated(self) -> Any:
        try:
            return self.runtime.employee_gate(self.actor)
        except PermissionError:
            raise
        except Exception as error:  # noqa: BLE001 - an unresolvable reader reads nothing
            raise PermissionError("employee_gate_refused") from error

    def _state(self) -> tuple[Any, Any, dict[str, Any]]:
        state = getattr(self._local, "state", None)
        if state is not None:
            return state
        gated = self._gated()
        return gated, self.host.policy_now(gated), {}

    @contextmanager
    def request(self):
        """One answer: one grant and one projection generation (``host.request``)."""
        previous = getattr(self._local, "state", None)
        self._local.state = None
        try:
            with self.host.request():
                gated = self._gated()
                self._local.state = (gated, self.host.policy_now(gated), {})
                yield self
        finally:
            self._local.state = previous

    def store_meta(self) -> dict[str, str]:
        """``store_meta`` of the projection generation the host answers from."""
        return self.host.store_meta()

    def grant(self) -> tuple[Callable[[str], bool], frozenset[str]]:
        """The reader's grant inside ``request()``: (may it read this audience key, the
        markings it holds), from the same policy every tool call of the answer uses."""
        gated, policy, _memo = self._state()
        return (lambda key: policy.may_read(gated, key)), frozenset(policy.held_markings(gated))

    def tools(self) -> dict[str, dict[str, Any]]:
        """The tool table the workspace calls: each tool through the host for the reader."""
        def handler(name: str) -> Callable[[dict[str, Any], Actor], dict[str, Any]]:
            def call(arguments: dict[str, Any], actor: Actor) -> dict[str, Any]:
                if actor != self.actor:
                    raise PermissionError("tool_call_for_another_reader")
                gated, _, _ = self._state()
                return self.host.call(name, arguments, gated)
            return call
        return {name: {"handler": handler(name)} for name in SERVED_TOOLS + self.registered}

    @property
    def schema(self) -> Any:
        """The schema the host's tools answer with (``OntologyTools.schema``)."""
        return self.host.tools.schema

    def member_set(self, object_id: str) -> str:
        """A set handle of the one object for this reader, for ``plx.aggregate``; the
        tool re-checks the member's visibility on use, as for any set."""
        gated, _, _ = self._state()
        return self.host.tools.sets.new(gated, [object_id])

    def decide(self, handles: Iterable[str]) -> dict[str, dict[str, str] | None]:
        gated, policy, memo = self._state()
        handles = list(dict.fromkeys(handles))
        todo = [handle for handle in handles if handle not in memo]
        if todo:
            with self.host.per_call():
                item_gate = self.host.tools.item_gate
                for handle in todo:
                    memo[handle] = self._decide(handle, gated, policy, item_gate)
        return {handle: memo[handle] for handle in handles}

    @staticmethod
    def _decide(handle: str, gated: Any, policy: Any,
                item_gate: Callable | None) -> dict[str, str] | None:
        unit = audience_unit(handle)
        if unit is not None and not policy.may_read(gated, unit):
            return _withheld(NOT_GRANTED)
        if handle.startswith(_ITEM_ACL_SCHEMES):
            try:
                admitted = item_gate is not None and item_gate(gated, handle) is True
            except Exception:  # noqa: BLE001 - a failing re-check withholds
                admitted = False
            return None if admitted else _withheld(ITEM_REFUSED)
        return None if unit is not None else _withheld(NOT_GRANTED)

    def agent_server(self) -> Any:
        """The agent loop's MCP session for this reader over this host: its tools, the
        runtime's employee gate on every call and the grant the request in progress
        pinned (``policy_now``). Never closed by its user: closing it closes the host."""
        from operating.mcp_server import MCPServer
        return MCPServer(case_runtime=self.runtime, actor=self.actor, ontology=self.host)

    def read(self, handle: str, *, text_offset: int = 0) -> dict[str, Any]:
        from operating.case_tools_host import PagedRead
        gated, policy, _ = self._state()
        tools = self.host.tools
        tools.policy = policy
        arguments: dict[str, Any] = {"citation": handle}
        if text_offset:
            arguments["text_offset"] = text_offset
        return PagedRead(self.host.evidence, self.runtime).call(arguments, gated)

    def withheld_sources(self) -> list[dict[str, str]]:
        gated, policy, _ = self._state()
        granted = set(policy.granted_sources(gated))
        return [{"source": source, "label": label, **_withheld(NOT_GRANTED)}
                for source, label in grantable_sources() if source not in granted]


def host_access(runtime: Any, store_path: Any, actor: Actor, *, markings: Iterable[str] = (),
                mail_capture_dir: Any = None, sharepoint_capture_dir: Any = None,
                session: Any = None, reviews: Any = None,
                **host_options: Any) -> HostAccess:
    """The reader's ``HostAccess`` over the case host the MCP clients use
    (``CaptureCaseHostTools``: plx.find with its live fallback, plx.object, plx.around,
    plx.timeline, plx.process, plx.documents, plx.investigate and plx.read) on
    ``store_path``. ``markings``: markings the host says the reader holds (default none;
    the case host itself grants none). Capture directories add the mail and SharePoint
    units their stores admit, each record re-gated per item. With a review store
    (``ontology.reviews.ReviewStore``) ``plx.find`` reads its reviewed aliases, and with
    the host serves ``plx.review`` for the session bound last (``HostAccess.bind``; the
    reader's ``session``, whose actor is ``actor``): its author. Without one, the
    tool lists reviews and refuses to record. ``host_options`` go
    to the host as they are (its live re-read bounds and clock)."""
    from operating.case_tools_host import CaptureCaseHostTools
    from operating.ontology_tools import ReadPolicy

    held = tuple(markings)

    class ConsoleHost(CaptureCaseHostTools):
        """The case host answering the policy the request in progress pinned."""

        pinned: Callable[[], Any] | None = None

        def policy_now(self, reader: Any) -> Any:
            pinned = self.pinned() if self.pinned is not None else None
            if pinned is not None:
                return pinned
            policy = super().policy_now(reader)
            principals = frozenset(reader.principals)
            return ReadPolicy(policy.tenant, policy.audiences,
                              {marking: principals for marking in held})

    host = ConsoleHost(runtime, actor, store_path=store_path,
                       mail_capture_dir=mail_capture_dir,
                       sharepoint_capture_dir=sharepoint_capture_dir, reviews=reviews,
                       **host_options)
    if reviews is None:
        return HostAccess(host, runtime, actor)
    from operating.ontology_tools_review import ReviewTools, original_pages

    access = HostAccess(host, runtime, actor, registered=(REVIEW_TOOL,))
    if session is not None:
        access.bind(session)
    # ontology_tools_review.register, with the session of the request in progress: a
    # live employee's workspace outlives one session ticket.
    host.register_tool(REVIEW_TOOL, lambda arguments, caller: ReviewTools(
        host.tools, host.reviews, session=access.session, gate=runtime.employee_gate,
        policy_for=host.policy_now, pages=original_pages(host)).call(REVIEW_TOOL, arguments,
                                                                     caller))
    return access


# ---- the answer a question shows first ----------------------------------------
# A question about one record (the console's question box) answers with one line first:
# ``verdict``, worded only from what the plan run or the workspace answered for this reader.
# A value the plan left undecided or empty reads "Not established" with why, never a guess
# (an unknown family, state or reason fails closed the same way). Under it: the cited values
# (``facts``) and the follow-ups the person may propose (``follow_ups``; a supplier draft is
# only a request: ``_follow_up`` words it again or refuses with a typed code).
MAX_VERDICT_FACTS = 8
MAX_VERDICT_PARTS = 4
MAX_VERDICT_FOLLOW_UPS = 4
# The link tiers a supplier answer states without the lead mark; any other tier, an edge
# with no tier, or no edge to the shown supplier at all, reads as a lead (fails closed).
_FIRM_TIERS = frozenset({"verified", "reviewed"})
# The event qualifier that names who supplied a record (``ontology`` event types): the
# links it goes ``via`` are the supplier links, read from the schema, never by name.
_SUPPLIER_QUALIFIER = "supplier"
# Why a stage of the object's process has no record, by the gap's state.
_STAGE_HIDDEN = frozenset({"inaccessible"})
_FOLLOW_UP_ASK = {"late_po": "Confirm the delivery date of {label}",
                  "landed_cost": "Confirm the unit prices, receipt and freight of {label}",
                  "order_margin": "Confirm the invoiced revenue and booked cost of {label}"}
_SOURCE_CODES = re.compile(r"\b(portal_api|graph_mail|sharepoint|bc)\b")


def _text_of(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("untrusted_text")
    if value is None or isinstance(value, (dict, list)) or value == "":
        return None
    return str(value)


def _subject(obj: Mapping[str, Any] | None) -> dict[str, Any]:
    """The record a verdict is about, named by its type and number, never its object id."""
    from operating.explanations import type_words

    obj = obj or {}
    kind = type_words(obj.get("object_type")) or "record"
    shown = _text_of(obj.get("display"))
    return {"object_id": obj.get("object_id"), "object_type": obj.get("object_type"),
            "label": f"{kind[:1].upper()}{kind[1:]} {shown}" if shown
            else f"{kind[:1].upper()}{kind[1:]} (its number is not readable here)"}


def _slots(answer: Sequence[Mapping[str, Any]], name: str) -> list[tuple[str, Mapping[str, Any]]]:
    """The plan's answer entries ``name`` or ``name_<n>``, with their number ('' if none)."""
    pattern = re.compile(rf"^{re.escape(name)}(?:_(\d+))?$")
    return [(found.group(1) or "", entry) for entry in answer
            if isinstance(entry, Mapping) and (found := pattern.match(str(entry.get("name"))))]


def _parts(*groups: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for group in groups:
        for part in group:
            if part and part not in seen:
                seen.append(part)
    return seen[:MAX_VERDICT_PARTS]


def _not_established(parts: Sequence[str]) -> dict[str, Any]:
    return {"state": "not_established",
            "text": "Not established: " + (", ".join(parts) or "no record answers it")}


def _reason_words(codes: Iterable[Any]) -> list[str]:
    """Each reason code in ``operating.explanations``' words (one source of wording)."""
    from operating.explanations import reason_phrase, words

    out = []
    for code in codes:
        code = str(code)
        phrase = reason_phrase(code)
        if phrase is None and code.startswith("missing_"):
            continue   # the conclusion itself ("missing_late"), not a reason for it
        out.append(phrase or words(code))
    return out


def _default_schema():
    from operating.ontology_tools import shared_schema
    return shared_schema()


@functools.lru_cache(maxsize=1)
def _shared_graphs() -> Mapping[str, Mapping[str, Any]]:
    from operating.ontology_tools_process import load_graphs
    return load_graphs()


def _default_graphs() -> Mapping[str, Mapping[str, Any]]:
    try:
        return _shared_graphs()
    except Exception:   # an unloadable graph set: no stage role is derived (fails closed)
        return {}


def _lateness_types(graphs: Mapping[str, Mapping[str, Any]], role: str,
                    root_type: str | None = None) -> set[str]:
    """The object types the process graphs' lateness rules read as ``role`` ("promised",
    "received"), for graphs rooted at ``root_type`` (every graph when None)."""
    found: set[str] = set()
    for graph in graphs.values():
        if not isinstance(graph, Mapping) or (root_type and graph.get("root_type") != root_type):
            continue
        for stage in graph.get("stages") or ():
            rule = ((stage.get("due") or {}).get("lateness") or {}).get(role) \
                if isinstance(stage, Mapping) else None
            if isinstance(rule, Mapping) and isinstance(rule.get("object_type"), str):
                found.add(rule["object_type"])
    return found


def _supplier_links(schema: Any, object_type: str | None) -> set[str]:
    """The links from ``object_type`` that the schema's events name its supplier by."""
    if schema is None or not object_type:
        return set()
    links = getattr(schema, "link_types", {}) or {}
    vias = {q.get("via") for event in (getattr(schema, "event_types", {}) or {}).values()
            for q in (event.get("qualifiers") or ()) if isinstance(q, Mapping)
            and q.get("qualifier") == _SUPPLIER_QUALIFIER}
    return {via for via in vias if isinstance(via, str) and via in links
            and (links[via] or {}).get("from") == object_type}


def _confirmation_stages(graphs: Mapping[str, Mapping[str, Any]], process_id: Any,
                         root_type: str | None) -> set[str]:
    """The stages of the object's process whose evidence is what lateness reads as the
    supplier's promise (its confirmation), from the graphs, never by stage name."""
    promised = _lateness_types(graphs, "promised", root_type)
    graph = graphs.get(process_id) if isinstance(process_id, str) else None
    pool = [graph] if isinstance(graph, Mapping) else [
        g for g in graphs.values() if isinstance(g, Mapping) and g.get("root_type") == root_type]
    return {stage["id"] for g in pool for stage in g.get("stages") or ()
            if isinstance(stage, Mapping) and isinstance(stage.get("id"), str)
            and any(isinstance(e, Mapping) and e.get("object_type") in promised
                    for e in stage.get("evidence") or ())}


def _absent_links(gaps: Sequence[Mapping[str, Any]],
                  graphs: Mapping[str, Mapping[str, Any]] | None = None) -> list[str]:
    """What the plan's links did not reach, said once per kind of record: the records the
    process graphs' lateness reads (the promise, the receipt), from the graphs."""
    from operating.explanations import reason_phrase, type_words

    graphs = _default_graphs() if graphs is None else graphs
    read = _lateness_types(graphs, "promised") | _lateness_types(graphs, "received")
    out = []
    for gap in gaps:
        subject, reason = str(gap.get("subject") or ""), gap.get("reason")
        if reason == "root_lineage_staging":
            out.append(reason_phrase("root_lineage_staging"))
        elif reason in ("no_match", "absence_unproven"):
            kind = subject.split(".", 1)[0]
            if kind in read:
                out.append(f"no {type_words(kind)} linked")
    return out


def _late_verdict(answer: Sequence[Mapping[str, Any]], gaps: Sequence[Mapping[str, Any]],
                  graphs: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """``late_po``: plx.process's own lateness verdict per order (``late_<n>``), its days
    late, governing date and reason chain; the plan never compares dates, nor does this."""
    def at(name: str, number: str) -> str | None:
        return next((_text_of(entry.get("value")) for n, entry in _slots(answer, name)
                     if n == number), None)

    verdicts = _slots(answer, "late")
    late = [n for n, entry in verdicts if entry.get("value") is True]
    on_time = [n for n, entry in verdicts if entry.get("value") is False]

    def why(number: str) -> list[str]:
        chain = next((entry.get("value") for n, entry in _slots(answer, "why")
                      if n == number), None)
        if isinstance(chain, str):
            chain = chain.replace(" ", "").split(">")
        return _reason_words(c for c in chain if isinstance(c, str)) \
            if isinstance(chain, list) else []

    if late:
        days = {n: at("days_late", n) for n in late}
        worst = max(late, key=lambda n: int(days[n]) if str(days[n]).isdigit() else -1)
        lead = (f"Late by {days[worst]} day{'' if days[worst] == '1' else 's'}"
                if str(days[worst]).isdigit() else "Late")
        if len(verdicts) > 1:
            lead = f"{len(late)} of {len(verdicts)} orders late; the latest {lead[:1].lower()}{lead[1:]}"
        parts = _parts([f"planned arrival {at('governing', worst)}"]
                       if at("governing", worst) else [], why(worst))
        return {"state": "late", "text": lead + (": " + ", ".join(parts) if parts else "")}
    if verdicts and len(on_time) == len(verdicts):
        parts = _parts([f"planned arrival {at('governing', n)}" for n in on_time
                        if at("governing", n)])
        return {"state": "not_late", "text": "Not late" + (": " + ", ".join(parts) if parts else "")}
    planned = sorted({text for _, entry in _slots(answer, "need_date")
                      if (text := _text_of(entry.get("value")))})
    dates = ([f"planned arrival {' or '.join(planned)}"] if planned
             else ["no planned arrival date"])
    return _not_established(_parts(dates, *(why(n) for n, _ in verdicts),
                                   _absent_links(gaps, graphs)))


def _computed_verdict(answer: Sequence[Mapping[str, Any]], name: str, words_: str,
                      reasons: Sequence[Any]) -> dict[str, Any]:
    """A plan's one computed answer (``landed_cost``, ``order_margin``), or why not."""
    entry = next((entry for n, entry in _slots(answer, name) if n == ""), None)
    shown = _text_of((entry or {}).get("value"))
    if shown is not None:
        unit = (entry or {}).get("unit")
        open_ = [word for word in _reason_words(reasons)]
        return {"state": "partial" if open_ else "answered",
                "text": f"{words_} {shown}{' ' + str(unit) if unit else ''}"
                        + (f" (still open: {', '.join(open_[:MAX_VERDICT_PARTS])})"
                           if open_ else "")}
    received = _slots(answer, "received")
    empty = (["no received quantity"] if received
             and all(_text_of(e.get("value")) is None for _, e in received) else [])
    words_found = _reason_words(reasons)
    return _not_established(_parts(words_found[:1], empty, words_found[1:]))


def _facts(answer: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The cited values under a verdict, by field in plain words; one line per distinct
    value (the same quantity on four lines is one line, counted)."""
    from operating.explanations import field_words, record_label, words

    found: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in answer:
        if not isinstance(entry, Mapping) or entry.get("kind") not in (
                "property", "computed", "compute", "count"):
            continue
        shown = _text_of(entry.get("value"))
        handle = (entry.get("citation") or {}).get("l1_handle") \
            if isinstance(entry.get("citation"), Mapping) else None
        if shown is None or not isinstance(handle, str) or not handle.startswith(L1_SCHEMES):
            continue
        field = field_words(entry.get("property")) or words(
            re.sub(r"_\d+$", "", str(entry.get("name"))))
        unit = entry.get("unit")
        key = (field, f"{shown}{' ' + str(unit) if unit else ''}")
        item = found.setdefault(key, {"field": field, "value": key[1], "citation": handle,
                                      "records": 0, "unchecked": False})
        item["records"] += 1
        item["unchecked"] = item["unchecked"] or entry.get("check") == "unverified"
    out = []
    for item in list(found.values())[:MAX_VERDICT_FACTS]:
        source, record, _page = record_label(item["citation"])
        where = f"{item['records']} records" if item["records"] > 1 else f"{source} {record}"
        out.append({"text": f"{item['field'][:1].upper()}{item['field'][1:]}: {item['value']}"
                            f" ({where}{', not re-checked' if item['unchecked'] else ''})",
                    "citation": item["citation"]})
    return _bounded(out, len(found))


def _bounded(facts: list[dict[str, Any]], total: int) -> list[dict[str, Any]]:
    """The facts shown, and how many more were left out: a cut list says it was cut."""
    more = total - len(facts)
    return facts + ([{"text": f"{more} more cited value{'' if more == 1 else 's'} not listed "
                              "here: open the record for all of them", "citation": None}]
                    if more > 0 else [])


def _answerers(gaps: Sequence[Mapping[str, Any]]) -> list[str]:
    """The declared roles the gaps name, most named first; an undeclared owner is not one."""
    count: dict[str, int] = {}
    for gap in gaps:
        for role in gap.get("who_can_answer") or ():
            if isinstance(role, str) and role and not role.startswith("undeclared:"):
                count[role] = count.get(role, 0) + 1
    return sorted(count, key=lambda role: -count[role])[:3]


def investigation_verdict(family: str, investigated: Mapping[str, Any]) -> dict[str, Any]:
    """The one-line answer of one plan run (``_investigate``), its cited facts and the
    follow-up the person may propose when it is not established or is late."""
    from operating.explanations import words
    from operating.proactive_engine import DRAFT_LABEL, DRAFT_STATUS

    result = investigated.get("investigation") or {}
    subject = _subject(result.get("root") or {"object_id": investigated.get("root")})
    error = result.get("error") if isinstance(result, Mapping) else None
    if error or not isinstance(result.get("answer"), list):
        verdict = _not_established([f"the plan did not run ({words((error or {}).get('code')) or 'no answer'})"])
        return {**verdict, "subject": subject, "facts": [], "follow_ups": []}
    answer, gaps = result["answer"], [g for g in result.get("gaps") or [] if isinstance(g, Mapping)]
    answerability = result.get("answerability") or {}
    reasons = answerability.get("reasons") or []
    if answerability.get("state") not in ("answerable", "partial"):
        verdict = _not_established(_parts(_reason_words(reasons)))
    elif family == "late_po":
        verdict = _late_verdict(answer, gaps)
    elif family == "landed_cost":
        verdict = _computed_verdict(answer, "landed_cost", "Landed cost", reasons)
    elif family == "order_margin":
        verdict = _computed_verdict(answer, "order_margin", "Margin", reasons)
    else:   # a family with no one-line answer: never worded as answered
        verdict = _not_established(["this question has no one-line answer here"])
    follow_ups = []
    who = _answerers(gaps)
    ask = _FOLLOW_UP_ASK.get(family)
    if ask and who and subject["object_id"] and verdict["state"] in ("not_established", "late"):
        follow_up: dict[str, Any] = {
            "object_id": subject["object_id"], "missing": ask.format(label=subject["label"]),
            "why": verdict["text"][:MAX_FOLLOW_UP_TEXT], "who_can_answer": who,
            "state": "unknown", "priority": "high" if verdict["state"] == "late" else "medium"}
        origin = ((result.get("root") or {}).get("display") or {}).get("origin") or {}
        if verdict["state"] == "late" and str(origin.get("citation", "")).startswith(L1_SCHEMES):
            # Only a request: the console words the supplier draft from the store again.
            follow_up["draft"] = {"label": DRAFT_LABEL, "status": DRAFT_STATUS, "sent": False,
                                  "text": DRAFT_LABEL, "citations": [origin["citation"]]}
        follow_ups.append(follow_up)
    return {**verdict, "subject": subject, "facts": _facts(answer), "follow_ups": follow_ups}


def _stage_words(stage: Mapping[str, Any]) -> str:
    from operating.explanations import FRESHNESS_SOURCES, field_words

    name = field_words(stage.get("stage")) or "a step"
    reading = _text_of((stage.get("reading") or {}).get("text")) or ""
    reading = re.sub(r"\s*\([a-z]+(?:_[a-z]+)+\)", "", reading)
    reading = _SOURCE_CODES.sub(lambda m: FRESHNESS_SOURCES.get(m[1], m[1]), reading)
    return f"{name[:1].upper()}{name[1:]}: {reading or 'recorded'}"


def object_answers(card: Mapping[str, Any], around: Mapping[str, Any],
                   process: Mapping[str, Any], *, schema: Any = None,
                   graphs: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """The question box's answers from one opened object: ``missing`` (the steps of its
    process with no record, the ones hidden from this reader counted apart) and
    ``supplier`` (who the order is from, by its linked supplier, and whether it is
    confirmed). Each a ``verdict`` line, its facts and follow-ups."""
    from operating.explanations import TIER_MEANING, field_words

    subject = _subject(card.get("object"))
    stages = [s for s in ((process.get("result") or {}).get("stages") or [])
              if isinstance(s, Mapping)] if process.get("status") == "read" else []

    def handle_of(stage: Mapping[str, Any]) -> str | None:
        found: set[str] = set()
        _handles(stage.get("citations") or [], found)
        return min(found) if found else None

    def follow_up(stage: Mapping[str, Any]) -> dict[str, Any] | None:
        role, gap = stage.get("owner_role"), stage.get("missing") or {}
        if not isinstance(role, str) or not role or not subject["object_id"]:
            return None
        return {"object_id": subject["object_id"],
                "missing": f"{field_words(stage.get('stage'))[:1].upper()}"
                           f"{field_words(stage.get('stage'))[1:]} of {subject['label']}",
                "why": _stage_words(stage)[:MAX_FOLLOW_UP_TEXT], "who_can_answer": [role],
                "state": gap.get("state") if gap.get("state") in FOLLOW_UP_STATES else "unknown",
                "priority": "medium"}

    if not stages:
        missing = _not_established([{"not_applicable": "this record has no process to check its steps against",
                                     "not_configured": "the process check is not served here"}.get(
                                         process.get("status"), "the process check could not run")])
        missing.update(facts=[], follow_ups=[])
    else:
        gaps = [s for s in stages if s.get("missing")
                and (s.get("reading") or {}).get("kind") != "found"
                and (s["missing"].get("state") not in _STAGE_HIDDEN)]
        # A record held whose status no definition decides (an unmapped PO header word)
        # is not established, never missing: no follow-up asks for a record that exists.
        undecided = [s for s in gaps if (s.get("reading") or {}).get("kind") == "not_decided"]
        absent = [s for s in gaps if s not in undecided]
        hidden = [s for s in stages if s.get("missing")
                  and s["missing"].get("state") in _STAGE_HIDDEN]
        names = [field_words(s.get("stage")) for s in absent]
        unsure = ", ".join(field_words(s.get("stage")) for s in undecided)
        tail = (f"; {len(hidden)} more step{'' if len(hidden) == 1 else 's'} not visible to you"
                if hidden else "")
        missing = ({"state": "missing", "text": f"Missing: {', '.join(names)}"
                    + (f"; not established: {unsure}" if unsure else "") + tail} if absent
                   else {"state": "not_established", "text": f"Not established: {unsure}{tail}"}
                   if undecided
                   else {"state": "not_established" if hidden else "complete",
                         "text": f"Nothing missing that you can see{tail}" if hidden
                         else "Nothing missing: every step has a record"})
        missing["facts"] = _bounded([{"text": _stage_words(s), "citation": handle_of(s)}
                                     for s in stages][:MAX_VERDICT_FACTS + 4], len(stages))
        missing["follow_ups"] = [f for s in absent if (f := follow_up(s))][:MAX_VERDICT_FOLLOW_UPS]
    schema = _default_schema() if schema is None else schema
    graphs = _default_graphs() if graphs is None else graphs
    root_type = (card.get("object") or {}).get("object_type")
    supplier_links = _supplier_links(schema, root_type)
    hop = next((h for h in around.get("hops") or [] if isinstance(h, Mapping)
                and h.get("link_type") in supplier_links
                and h.get("direction") == "forward"), None)
    confirming = _confirmation_stages(graphs, (process.get("result") or {}).get("process"),
                                      root_type) if stages else set()
    confirmation = next((s for s in stages if s.get("stage") in confirming), None)
    if hop is None:
        supplier = _not_established(["this record is not an order linked to a supplier"])
        supplier.update(facts=[], follow_ups=[])
    else:
        result = hop.get("result") or {}
        found = [o for o in result.get("objects") or [] if isinstance(o, Mapping)]
        tiers = {e.get("to") or e.get("target"): e.get("tier") for e in result.get("edges") or []
                 if isinstance(e, Mapping)}
        names = [_text_of(o.get("display")) or "a supplier (its name is not readable here)"
                 for o in found]
        confirmed = confirmation is not None and not confirmation.get("missing")
        said = ("confirmed" if confirmed else "no supplier confirmation found yet"
                if confirmation is not None else "confirmation not checked")
        if len(found) == 1:
            tier = tiers.get(found[0].get("object_id"))
            lead = "" if tier in _FIRM_TIERS else " (linked only as a lead)"
            supplier = {"state": "answered", "text": f"{names[0]}{lead}; {said}"}
        else:
            supplier = _not_established(
                [f"{len(found)} suppliers are linked ({', '.join(names[:3])})"] if found
                else ["no supplier is linked to this order"])
        supplier["facts"] = _bounded([
            {"text": f"Supplier: {name}"
                     + (f" (link {TIER_MEANING[tiers.get(o.get('object_id'))]})"
                        if tiers.get(o.get("object_id")) in TIER_MEANING else ""),
             "citation": ((o.get("display") or {}).get("origin") or {}).get("citation")
             if isinstance(o.get("display"), Mapping) else None}
            for o, name in zip(found, names)][:MAX_VERDICT_FACTS], len(found))
        if confirmation is not None:
            supplier["facts"].append({"text": _stage_words(confirmation),
                                      "citation": handle_of(confirmation)})
        supplier["follow_ups"] = [f for f in [follow_up(confirmation)]
                                  if f] if confirmation is not None and not confirmed else []
    return {"subject": subject, "missing": missing, "supplier": supplier}


def plans_for(object_type: str) -> list[dict[str, str]]:
    """The question families (``ontology/plans``) whose root may be ``object_type``."""
    return [plan for plan, roots in _plan_index() if object_type in roots]


def page_answers_for(object_type: str | None, *, schema: Any = None,
                     graphs: Mapping[str, Mapping[str, Any]] | None = None) -> list[str]:
    """The ``object_answers`` that can answer for a record of ``object_type``: ``missing``
    when a process graph is rooted at the type, ``supplier`` when the schema's events name
    a supplier link from it. Derived from the schema and process graphs, never a type name."""
    schema = _default_schema() if schema is None else schema
    graphs = _default_graphs() if graphs is None else graphs
    out = []
    if object_type and any(isinstance(g, Mapping) and g.get("root_type") == object_type
                           for g in graphs.values()):
        out.append("missing")
    if _supplier_links(schema, object_type):
        out.append("supplier")
    return out


_PLANS: list[tuple[dict[str, str], frozenset[str]]] | None = None


def _plan_index() -> list[tuple[dict[str, str], frozenset[str]]]:
    global _PLANS
    if _PLANS is None:
        from operating.ontology_tools import default_schema
        from operating.ontology_tools_investigate import PLAN_DIR, plan_root_types
        schema = default_schema()
        index = []
        for path in sorted(Path(PLAN_DIR).glob("*.json")):
            if path.name.startswith("_"):
                continue
            doc = json.loads(path.read_text(encoding="utf-8"))
            index.append(({"family": doc["id"], "title": doc["title"],
                           "description": doc["description"]},
                          plan_root_types(doc, schema)))
        _PLANS = index
    return _PLANS


def _open_store(store_path: Any):
    from operating import ontology_tools as ot
    return ot.StoreReader.open(store_path)


# The source families whose capture generation is random per capture file (the Portal
# mirror's, a BC capture store's): only they tie a store to one state directory. Mail's
# and SharePoint's generation is their capture schema version, the same in every copy.
IDENTITY_SOURCES = ("bc", "portal_api")


class StoreStatus:
    """The projection store's provenance and freshness against a state directory's
    captures, per source (decision architect-20261011T0721Z-MEuPpgWV).

    The store is this state directory's only when its ``store_meta.l1_generations``
    records the generation of an identity-bearing source (``IDENTITY_SOURCES``) whose
    capture here has that generation now. Each source the store recorded is opened read
    only as the build opens it (``opener``: ``ontology.project.MirrorCaptures`` over the
    mirror, ``operating.host.open_state_capture`` for the rest) and gets a state in
    ``sources``: ``current``; ``stale`` (a relation's newest capture differs from the
    projected one, ``newer_relations``); ``removed`` (its capture is gone here: the store
    answers, its records labelled as projected); ``foreign`` (another generation: the
    store cites another capture) or ``unreadable``. A capture held here that the store
    did not project is ``not_projected`` and never opened. The store's ``state``:
    ``foreign`` naming only the foreign sources, else ``unknown`` (unreadable recorded
    generations or captures, a family no state directory holds, or no identity source
    verified, ``unverified_sources``), both refused; else ``stale`` when a source is stale
    or removed; else ``current``. A status is kept for the store generation and capture
    files it was read for (inode, size and modification time of each file and its WAL),
    so an unchanged store over unchanged captures opens none of them again.
    """

    def __init__(self, store_path: Any, state_dir: Any, *,
                 opener: Callable[[str], Any] | None = None) -> None:
        self.store_path = Path(store_path)
        self.state_dir = Path(state_dir)
        self.mirror_path = mirror_path(state_dir)
        self.opener = opener or self.open_capture
        self._kept: tuple[Any, dict[str, Any]] | None = None

    def open_capture(self, source: str) -> Any:
        """The capture of ``source`` here, opened read only (a context manager)."""
        from ontology import project
        from operating.host import open_state_capture

        if source == "portal_api":
            return project.MirrorCaptures(self.mirror_path)
        return open_state_capture(self.state_dir, source)

    def read(self, meta: Mapping[str, str] | None = None) -> dict[str, Any]:
        """The status of the store whose ``store_meta`` is ``meta`` (the generation the
        request reads); None reads the file at ``store_path`` now."""
        from operating.host import state_capture_paths

        if meta is None:
            reader = _open_store(self.store_path)
            try:
                meta = reader.meta()
            finally:
                reader.conn.close()
        paths = {"portal_api": self.mirror_path, **state_capture_paths(self.state_dir)}
        files = {source: _file_signature(path) for source, path in paths.items()}
        key = (tuple(meta.get(name) for name in (
            "built_at", "content_digest", "l1_generations", "source_freshness")),
            tuple(sorted(files.items())))
        kept = self._kept
        if kept is not None and kept[0] == key:
            return copy.deepcopy(kept[1])
        status = self._status(meta, {source for source, held in files.items() if held[0]},
                              set(paths))
        if not status.get("unreadable_sources"):
            self._kept = (key, status)
        return copy.deepcopy(status)

    def _status(self, meta: Mapping[str, str], held: set[str],
                families: set[str]) -> dict[str, Any]:
        from ontology import project

        try:
            generations = json.loads(meta.get("l1_generations") or "{}")
        except ValueError:
            generations = None
        out: dict[str, Any] = {"built_at": meta.get("built_at"),
                               "content_digest": meta.get("content_digest")}
        if not isinstance(generations, dict) or not all(
                isinstance(entry, Mapping) for entry in generations.values()):
            return {**out, "state": "unknown",
                    "detail": "the store's recorded source generations are unreadable"}
        recorded = {source: entry.get("generation") for source, entry in generations.items()}
        out.update(mirror_generation=recorded.get("portal_api"), projected=sorted(recorded))
        if not any(isinstance(recorded.get(source), str) and recorded[source]
                   for source in IDENTITY_SOURCES):
            return {**out, "state": "unknown", "detail": (
                "the store records no Portal mirror or BC capture generation, so its "
                "provenance cannot be checked; re-project it with --project-ontology")}
        try:
            freshness = json.loads(meta.get("source_freshness") or "{}")
        except ValueError:
            freshness = None
        if not isinstance(freshness, dict):
            freshness = {}   # nothing to compare with: every captured relation is newer
        sources: dict[str, dict[str, Any]] = {
            source: {"state": "not_projected"} for source in held - set(recorded)}
        unreadable: dict[str, str] = {}
        for source in sorted(recorded):
            if source not in families:
                unreadable[source] = "no state directory holds a capture of this source"
            elif source not in held:
                sources[source] = {"state": "removed"}
            else:
                try:
                    with self.opener(source) as capture:
                        described = capture.describe()
                except (project.ProjectionError, sqlite3.Error, OSError) as error:
                    unreadable[source] = str(error)
                else:
                    sources[source] = _source_state(described, recorded[source],
                                                    freshness.get(source))
        sources.update({source: {"state": "unreadable"} for source in unreadable})
        for source, state in sources.items():
            state["label"] = SOURCE_LABELS.get(source, source)
        named = {state: sorted(source for source, entry in sources.items()
                               if entry["state"] == state)
                 for state in ("not_projected", "removed", "stale", "foreign")}
        portal = sources.get("portal_api", {})
        out.update(mirror_generation_now=portal.get("generation"),
                   not_projected=named["not_projected"], removed=named["removed"],
                   sources=dict(sorted(sources.items())))
        if named["foreign"]:
            return {**out, "state": "foreign", "foreign_sources": named["foreign"], "detail": (
                f"the store was projected from another {', '.join(named['foreign'])} "
                "capture generation than this state directory's; re-project it with "
                "--project-ontology")}
        if unreadable:
            why = "; ".join(f"{source}: {error}" for source, error in sorted(unreadable.items()))
            return {**out, "state": "unknown", "unverified_sources": sorted(unreadable),
                    "unreadable_sources": sorted(unreadable), "detail": (
                        f"the {', '.join(sorted(unreadable))} capture the store was "
                        f"projected from is unreadable here: {why}")[:300]}
        if not any(sources.get(source, {}).get("state") in ("current", "stale")
                   for source in IDENTITY_SOURCES):
            missing = [source for source in IDENTITY_SOURCES if source in recorded]
            return {**out, "state": "unknown", "unverified_sources": missing, "detail": (
                f"the {', '.join(missing)} capture the store was projected from is missing "
                "here, and no other Portal mirror or BC capture ties the store to this "
                "state directory")}
        if named["stale"] or named["removed"]:
            newer = [f"{source}/{relation}" for source in named["stale"]
                     for relation in sources[source]["newer_relations"]]
            said = []
            if named["stale"]:
                said.append(
                    f"the {', '.join(SOURCE_LABELS.get(s, s) for s in named['stale'])} "
                    "capture is newer than this store")
            if named["removed"]:
                said.append(
                    f"the {', '.join(SOURCE_LABELS.get(s, s) for s in named['removed'])} "
                    "capture it was projected from is gone here (its records are shown "
                    "as projected and no longer open)")
            return {**out, "state": "stale", "newer_relations": newer[:20], "detail": (
                "; ".join(said) + "; re-project with --project-ontology")}
        return {**out, "state": "current"}


def _source_state(described: Mapping[str, Any], generation: Any,
                  projected: Any) -> dict[str, Any]:
    """One recorded source's state from its capture's ``describe()`` now: foreign on
    another generation, else stale when a relation's newest capture differs from the one
    the store projected (``source_freshness``), else current."""
    if described.get("generation") != generation:
        return {"state": "foreign", "generation": described.get("generation")}
    now = described.get("relations") or {}
    then = projected if isinstance(projected, Mapping) else {}

    def last(entry: Any) -> Any:
        return entry.get("last_capture_id") if isinstance(entry, Mapping) else None

    newer = sorted(relation for relation in set(now) | set(then)
                   if last(now.get(relation)) != last(then.get(relation)))
    state = {"state": "stale", "newer_relations": newer[:20]} if newer else {"state": "current"}
    return {**state, "generation": generation}


def _file_signature(path: Path) -> tuple[Any, Any]:
    """(inode, size, modification time) of a capture file and of its WAL, each None
    when it is not a file."""
    out = []
    for part in (path, path.with_name(path.name + "-wal")):
        try:
            found = part.stat()
        except OSError:
            found = None
        out.append((found.st_ino, found.st_size, found.st_mtime_ns)
                   if found is not None and S_ISREG(found.st_mode) else None)
    return tuple(out)


def mirror_path(state_dir: Any) -> Path:
    """The source mirror file of a console state directory."""
    from operating.case_runtime import CASE_RUNTIME_DIR, MIRROR_FILE
    return Path(state_dir) / CASE_RUNTIME_DIR / MIRROR_FILE


def project_mirror(runtime_state_dir: Any, store_path: Any) -> dict[str, Any]:
    """Build the projection store from the console state directory's source mirror
    (offline: reads only captures already held; no source system is contacted)."""
    from ontology import project
    from ontology import schema as schema_loader

    mirror = mirror_path(runtime_state_dir)
    captures = project.MirrorCaptures(mirror)
    try:
        return project.build_store(store_path, schema_loader.load_schema(),
                                   {captures.source: captures})
    finally:
        captures.close()


__all__ = ["ACTION_STEPS", "APPROVAL_STAGES", "ApiOnlyAccess", "CaseIntents", "FollowUps",
           "HostAccess", "IDENTITY_SOURCES", "ObjectWorkspace", "SOURCE_LABELS",
           "WorkspaceActions",
           "StoreStatus", "approval_of", "audience_unit", "host_access", "mirror_path",
           "object_answers", "page_answers_for", "plans_for", "project_mirror",
           "record_of", "version_of"]
