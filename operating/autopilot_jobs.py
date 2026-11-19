"""Autopilot jobs: the system work an event calls for, prepared by the same brain as the Ask.

Owner 11-18: "why is it not writing to systems and doing FULL JOBS, not just email and Teams
replies". A supplier's order confirmation should leave the PO's expected date changed, a
customer's PO a sales order, a routine Mission Control approval its decision, a meeting's
action items Planner tasks, a QC result the batch record, a requested document change the
edit. None of that is code per case here (``docs/ARCH`` 22: one brain): the model reads the
event and the records with the Ask's own tools as the person the item is for, and proposes
each change through the Ask's own write tool contract (``plx.prepare_write`` and
``plx.edit_document``, the same names and schemas). Rules here are only the opt-in and the
safety around it.

* **Which events.** Every event a ``job`` autopilot of the registry takes for a person whose
  workflow is switched on for them (the engine's gate, the same opt-in as the drafts). A
  cheap first call (``TRIAGE_ENV``, a small model; ``off``: every event goes on) answers
  whether the event asks for a change in a business system at all; most mail does not, and
  no tool session is started for it.
* **The session** (``Lookup.run`` with ``tools``: the Simple Ask's reader host as the person,
  ``operator_api.autopilot_context``): plx.sql / plx.search / plx.open through the
  permission door, plus the two write tools bound to a *proposal* list
  (``proposal_tools``): a call is checked here the way the write path will check its shape
  (a system and operation a write adapter knows, the adapter's own field and target
  checks; a mail or Teams message only to someone other than the event's sender
  (``not_a_reply``: the reply to the sender is the drafts' item), never FileMaker or Sage, never
  Freightcom) and kept; nothing is prepared, written or sent. The model is told the
  operations and fields each system takes (``catalogue``, read from the adapters).
* **What the item holds** (``Job``): one line of what the event asks for and up to ``MOST``
  proposed changes, each the tool's own arguments. The person reviews each in the app: the
  API prepares it then through the prepared-writes desk (``operating.prepared_writes``:
  the record read from its system, before and after, could they make it themselves, the
  people exclusion, the two-step confirm), so the card is never the model's word.
* **Never to the model** (``autopilot_drafter.screened`` over the whole prompt, and
  ``thread_screen`` + Freightcom over what the tools returned): a message the people
  exclusion bars or cannot decide, pay or HR, Freightcom. A hit: no job.

Nothing here writes or sends. Evidence: isolated-fixture
(``operating/tests/test_autopilot_jobs.py``); live-unverified.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re
import subprocess
import time
from typing import Any, Callable, Mapping, Optional, Sequence

MODEL_ENV = "PLX_AUTOPILOT_JOB_MODEL"          # unset: the draft model; off: no jobs
TRIAGE_ENV = "PLX_AUTOPILOT_JOB_TRIAGE_MODEL"  # unset: TRIAGE; off: no first call
PER_CYCLE_ENV = "PLX_AUTOPILOT_JOB_PER_CYCLE"
PARALLEL_ENV = "PLX_AUTOPILOT_JOB_PARALLEL"
TRIAGE = "haiku"
TRIAGE_SECONDS = 60.0
PER_CYCLE = 8          # sessions started a cycle (the rest wait: ``job_busy``)
PARALLEL = 3           # sessions at once (each a model process and a reader host)
MOST = 4               # proposed changes one event may carry
EVENT_CHARS = 6000
BRIEF_CHARS = 3000     # a brief the model writes for an event that asks for one
FEATURE = "autopilot_job"
# the systems a job may change; mail and Teams replies are the drafts' (one item each)
SYSTEMS = ("bc", "portal", "mc", "planner", "calendar", "sharepoint")
# never proposed by a job: a delete (no event of a business system asks the autopilot to
# remove a record: a person does that), a file's bytes (``plx.edit_document`` edits one)
NOT_PROPOSED = frozenset({"file_upload", "file_replace"})
# a message the event calls for to someone other than its sender (ask a colleague to do
# their part, a supplier for a quote or a document, tell a customer, pass it on to who
# handles it): the write path's message shapes, the operation the catalogue names
MESSAGES = {"mail": "send_email", "teams": "chat_message"}
DOC_TOOL_NAME = "plx.edit_document"


@dataclass
class Job:
    """What the session prepared: ``summary`` (one line: what the event asks to be done)
    and ``proposals`` (each a write tool's arguments, ``tool`` naming which)."""
    summary: str
    proposals: list[dict[str, Any]] = field(default_factory=list)
    brief: str = ""          # only for an event that asks for one (``fields.brief``)


# ----------------------------------------------------------------------------- the event

def described(event: Mapping[str, Any], text: str, linked: Sequence[str] = ()) -> str:
    """The event as the model reads it: what happened, when, who, its handle (to open it
    whole with plx.open), the records the store links it to (``linked``: their handles)
    and its text, cut to ``EVENT_CHARS``."""
    kind = str(event.get("event_type") or "")
    channel = str(event.get("channel") or "")
    what = {"message.received": "A Teams message" if channel == "teams" else "An email",
            "meeting.ended": "A meeting ended", "approval.requested":
            "A Mission Control approval was requested", "qc.result": "A QC result",
            "task.assigned": "A task was assigned", "record.changed": "A record changed",
            "question.asked": "A question was asked in Mission Control",
            "record.due": "Recurring work of the person's is due",
            "assistant.look": "The person's hourly look"}.get(kind, kind)
    lines = [f"Event: {what} ({event.get('object_type') or 'record'})"]
    for label, key in (("When", "at"), ("From", "sender_name"), ("From address", "sender"),
                       ("Subject", "subject"), ("Side", "side"), ("Status", "status"),
                       ("Quality", "quality")):
        if event.get(key):
            lines.append(f"{label}: {str(event[key])[:200]}")
    for key in ("to", "cc", "attendees"):
        found = [str(a) for a in event.get(key) or () if a][:12]
        if found:
            lines.append(f"{key.capitalize()}: {', '.join(found)}")
    fields = event.get("fields") if isinstance(event.get("fields"), Mapping) else {}
    for key, value in list(fields.items())[:20]:
        if value not in (None, "", [], {}):
            lines.append(f"{key}: {str(value)[:300]}")
    handles = [str(h) for h in event.get("handles") or () if h][:4]
    if handles:
        lines.append("Open it whole with plx.open: " + ", ".join(handles))
    linked = [str(h) for h in linked if h][:8]
    if linked:
        lines.append("Records it names (open with plx.open): " + ", ".join(linked))
    body = (text or str(event.get("text") or "")).strip()
    if body:
        lines += ["", "Text:", body[:EVENT_CHARS]]
    return "\n".join(lines)


def catalogue() -> list[str]:
    """One line per system and operation a job may propose, with the target ids and the
    fields it takes, read from the write adapters themselves (a new adapter or route is
    listed without a change here)."""
    from . import autopilot_write_adapters as wa
    from . import write_intents as wi
    out: list[str] = []
    for adapter in wa.default_adapters({}).values():
        system = str(adapter.system)
        if system not in SYSTEMS:
            continue
        for operation in sorted(adapter.operations):
            if operation in NOT_PROPOSED or operation.endswith("_delete"):
                continue
            target: Sequence[str] = sorted(k for k in adapter.TARGET_KEYS
                                           if k not in ("user", "acting_for"))
            if system == "bc":
                spec = wi.OPERATIONS.get(operation) or {}
                target = ("company_name", "record_id") if spec.get("method") == "PATCH" \
                    else ("company_name",) if spec else ("company_name", "document_id") + (
                        ("line_id",) if operation in getattr(adapter, "LINE_UPDATES", {})
                        else ())
            elif system == "sharepoint":
                target = ("site_id", "list_id") + (
                    ("item_id",) if operation == "list_item_update" else ())
            elif operation.endswith("_create"):
                target = ()
            fields: Sequence[str] = ()
            route = getattr(wa, "PORTAL_ROUTES", {}).get(operation) if system == "portal" else None
            if route is not None:
                if route.get("asked"):
                    continue                  # not exposed by the Portal yet
                target, fields = route["target"], sorted(route.get("fields") or ())
            for table in ("DOC_UPDATES", "LINE_UPDATES", "LINE_OPERATIONS"):
                spec = (getattr(adapter, table, None) or {}).get(operation)
                if spec:
                    fields = spec.get("fields") or ()
            if system == "bc" and not fields:
                spec = wi.OPERATIONS.get(operation) or {}
                fields = tuple(spec.get("fields") or ())
                if spec.get("lines"):        # a new order carries its lines (deep insert)
                    fields += (f"lines [{', '.join(spec['lines'][1])}]",)
            if system == "planner":
                fields = ("planId", "bucketId", "title", "dueDateTime", "assignees") \
                    if operation == "task_create" else tuple(getattr(adapter, "UPDATE_FIELDS", ()))
            if system == "calendar":
                fields = ("response (accept, decline or tentative)", "comment") \
                    if operation == "event_respond" else tuple(getattr(adapter, "FIELDS", ()))
            if system == "mc":
                # the gate's scope is read from Mission Control when the change is
                # prepared (``prepared_read``), never proposed
                fields = ("decision (approve)",) if operation == "approval_decide" else (
                    ("notes",) if operation == "task_note"
                    else tuple(getattr(adapter, "PATCH_KEYS", ())))
            if system == "sharepoint":
                fields = ("the list's own column names",)
            line = f"{system} {operation}: target {', '.join(target) or '-'}"
            if fields:
                line += f"; change {', '.join(fields)}"
            out.append(line)
    out.append("sharepoint document text or cells: plx.edit_document (citation, find/replace "
               "or sheet/cell/value)")
    # a message to someone other than the event's sender (the write path's own draft
    # shapes, ``prepared_writes.DRAFT_FIELDS``); a reply to the sender is the drafts' work
    for system, operation in MESSAGES.items():
        fields = pw_draft_fields(system)
        out.append(f"{system} {operation}: target {', '.join(fields['target']) or '-'}; "
                   f"change {', '.join(fields['change'])} (a new message to someone other "
                   f"than the event's sender)")
    return out


def pw_draft_fields(system: str) -> dict[str, list[str]]:
    from . import prepared_writes as pw
    spec = pw.DRAFT_FIELDS[system]
    return {"target": sorted(spec["target"]), "change": sorted(spec["change"])}


TASK = """You prepare the system work an event calls for, for {person} at PLX, the way a careful \
colleague who does this job would: read the event, look up the records it names with the tools \
(as {person} may see them), and for each change a business system needs because of it, call \
plx.prepare_write (plx.edit_document for a document's own text or cells) with the record's ids \
exactly as the records hold them. Each call only proposes the change: {person} reviews it in the \
app and nothing is written until they confirm.

What counts: a change staff would key into a system because of this event. For example a \
supplier confirming or moving a ship date: the purchase order's expected date (and a note); a \
customer's purchase order: their sales order; an approval asked of {person}: the decision \
(approve) when the request and the records show it is complete and its stated checks pass, \
saying what you checked in the reason ({person} still decides: confirming is their approval); \
a meeting's agreed actions: tasks; a QC result: the batch or QC record; a requested change to \
a document: the edit; a meeting invitation to {person}: the answer in their calendar \
(calendar event_respond: accept, or decline or tentative when their calendar holds another \
meeting then; the event's id as their calendar holds it); a message {person} would send to someone else because of it (asking a \
colleague to do their part, a supplier for a quote, a sample or a document, telling a customer \
or a carrier, passing the message on to whoever handles it): mail send_email or teams \
chat_message, in {person}'s own voice, with the addresses as the records or the thread hold \
them. Never a reply to the message's sender (replies are prepared separately), never a \
change the event does not state or clearly imply, never a value you did not read or were not \
given in the event (a date, quantity, price or id). When the record cannot be found, or the \
change is already in the system, propose nothing for it. Most events need nothing: then call no \
tool.

Write operations (system operation: target ids; fields of change):
{catalogue}

Answer last with one JSON object only: {{"summary": "<one line: what the event asks to be done, \
in {person}'s terms; empty if nothing>", "nothing": "<why nothing was proposed, or empty>", \
"brief": "<only when the event asks for a brief: the brief, else empty>"}}

{event}"""


TRIAGE_PROMPT = """Would someone at PLX key something into a business system (Portal, Business \
Central, Mission Control, Planner, a calendar, a SharePoint list or document) because of this \
event? Yes when it states or asks for a change to an order, purchase order, shipment, batch or QC \
record, task, approval, meeting action or document: for example a supplier confirming, moving or \
missing a ship, delivery or pickup date, quantity or price on a PO; a customer sending, changing \
or cancelling an order; tracking or a BOL for a shipment; a QC result or hold; an approval asked \
for; actions agreed in a meeting; a correction asked to a document or record; or when the \
reader must pass it on or get someone else to act: ask a colleague to do their part, ask a \
supplier for a quote, sample or document, tell a customer or carrier; a meeting invitation \
to answer. No when it is only chat, thanks, a question to answer, marketing, a newsletter or an automatic \
notification about something already in the system. When unsure, answer yes. Answer with one \
JSON object only: {{"work": true or false, "what": "<the change, a few words, or empty>"}}

{event}"""


# a replay of a past event (``as_of``: the grading harness, never the live cycle): the
# records show what happened since, which the model must not take for the state then
AS_OF = ("It is {as_of} now, when this event arrived: act as of then. The records may "
         "already show changes made after it; propose the change as it was needed then "
         "(prior: the value before it, as the record's history shows).")


def job_prompt(event_text: str, person: str, as_of: str = "") -> str:
    text = TASK.format(person=person or "the person", event=event_text,
                       catalogue="\n".join(catalogue()))
    return text + ("\n\n" + AS_OF.format(as_of=as_of) if as_of else "")


_CANNOT_SET = re.compile(r"^(?P<system>[a-z]+)(?: [a-z]+)?: (?P<op>[\w.-]+)(?: line)? cannot set "
                         r"(?P<fields>[^()]+?) \(it sets")


def _words(name: str) -> set[str]:
    """A field name's words: ``expected_date``, ``expectedReceiptDate`` -> expected, date
    (and receipt)."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    return {w for w in re.split(r"[^a-z0-9]+", spaced.lower()) if len(w) > 1}


def field_hints(problems: Sequence[str]) -> list[str]:
    """For each "cannot set" refusal of a write, the fields of the same system closest to
    each field it named (a shared word; the operation first, then the system's others),
    read from the adapters (``catalogue``): so a model that wrote ``expected_date`` is
    told where the system keeps it and can try again with the system's own name."""
    lines = catalogue()
    out: list[str] = []
    for problem in problems:
        found = _CANNOT_SET.match(str(problem))
        if not found:
            continue
        system, op = found["system"], found["op"]
        offered: list[tuple[str, str]] = []
        for line in lines:
            head, _sep, rest = line.partition(": ")
            parts = head.split(" ", 1)
            if parts[0] != system or "; change " not in rest or len(parts) < 2:
                continue
            # an order's lines (``lines [...]``) are set inside its create, not as fields
            for f in rest.split("; change ", 1)[1].split("lines [", 1)[0].split(", "):
                f = f.split(" ", 1)[0].strip()
                if f and re.fullmatch(r"[A-Za-z_][\w]*", f):
                    offered.append((parts[1], f))
        for name in (n.strip() for n in found["fields"].split(",")):
            want = _words(name)
            scored = sorted(((len(want & _words(f)), o != op, o, f) for o, f in offered
                             if want & _words(f)), key=lambda x: (x[1], -x[0], x[2], x[3]))
            # this operation's closest field first, then the system's others
            mine = [x for x in scored if not x[1]][:1]
            others = sorted((x for x in scored if x[1]), key=lambda x: (-x[0], x[2], x[3]))
            picked = mine + others[:3 - len(mine)]
            if picked:
                near = ", ".join(f"{o} sets {f}" for _n, _other, o, f in picked)
                out.append(f"For {name}: {near}. Use the system's own field name (and its "
                           "operation) and try again.")
    return out


def triage_prompt(event_text: str) -> str:
    return TRIAGE_PROMPT.format(event=event_text[:3000])


def _json(text: Any) -> Optional[dict[str, Any]]:
    if not isinstance(text, str):
        return None
    found = re.search(r"\{.*\}", text.strip(), re.S)
    if not found:
        return None
    try:
        got = json.loads(found.group(0))
    except ValueError:
        try:
            # a model's text field with a raw line break in it (a reply written out)
            got = json.loads(found.group(0), strict=False)
        except ValueError:
            return None
    return got if isinstance(got, dict) else None


# ----------------------------------------------------------------------------- proposals

def check(tool: str, arguments: Any) -> list[str]:
    """Why a proposal cannot stand ([] when it can): its shape as the write path will check
    it, with no system read (the desk reads the record when the person reviews it)."""
    from . import autopilot_write as aw
    from . import autopilot_write_adapters as wa
    from . import prepared_writes as pw
    if not isinstance(arguments, Mapping):
        return ["Arguments must be an object."]
    if pw._freightcom(arguments):
        return ["That carrier is never acted on through the Workbench."]
    if tool == DOC_TOOL_NAME:
        unknown = set(arguments) - set(pw.DOC_TOOL_SPEC["inputSchema"]["properties"])  # type: ignore[index]
        if unknown:
            return ["Unknown fields: " + ", ".join(sorted(unknown)[:5])]
        if not str(arguments.get("citation") or "").startswith("sharepoint://"):
            return ["Give the document's sharepoint:// citation as read."]
        if not (("find" in arguments and "replace" in arguments)
                or ("cell" in arguments and "value" in arguments)):
            return ["Say find and replace (text) or sheet, cell and value (a sheet)."]
        return []
    unknown = set(arguments) - pw.TOOL_FIELDS
    if unknown:
        return ["Unknown fields: " + ", ".join(sorted(unknown)[:5])]
    system = str(arguments.get("system") or "").strip().lower()
    if system in pw.NEVER:
        return [pw.NEVER[system]]
    if system in pw.DRAFT_SYSTEMS:
        return _message_problems(system, arguments)
    if system not in SYSTEMS:
        return [f"No write path for {system or 'that system'}: one of {', '.join(SYSTEMS)}."]
    operation = str(arguments.get("operation") or "")
    if operation in NOT_PROPOSED or operation.endswith("_delete"):
        return ["An autopilot never proposes deleting a record or replacing a file's bytes."]
    if not isinstance(arguments.get("target"), Mapping) or \
            not isinstance(arguments.get("change"), Mapping):
        return ["target and change must be objects."]
    if not str(arguments.get("reason") or "").strip():
        return ["Say why in one line (reason)."]
    request = {k: v for k, v in arguments.items() if k != "citations"}
    try:
        item = pw.item_of(request, "proposal@local", "job_probe", "job")
    except pw.Refused as refused:
        return refused.problems
    actions = aw.actions_of(item)
    if len(actions) != 1 or actions[0].get("kind") not in aw.KINDS:
        return [f"No write path for {system} {str(arguments.get('operation') or '')[:40]}."]
    adapter = wa.default_adapters({}).get(str(actions[0]["kind"]))
    if adapter is None:
        return [f"No write path for {system}."]
    try:
        adapter.request(actions[0])
    except wa.AdapterRefused as refused:
        # what the record holds now (its etag, its values before, an MC gate's scope) is
        # read from the system when the person reviews the change: only the proposal's own
        # faults stand
        return [p for p in refused.problems
                if not re.search(r"precondition|etag|as read|prior|not configured"
                                 r"|^mc approval: scope ", str(p), re.I)]
    except Exception as exc:  # noqa: BLE001 - a shape the adapter cannot build
        return [f"{system}: {type(exc).__name__}"]
    return []


def _message_problems(system: str, arguments: Mapping[str, Any]) -> list[str]:
    """A proposed message's own faults: its shape as the write path builds it (recipients,
    a body, nothing the card would not show)."""
    from . import autopilot_write as aw
    from . import prepared_writes as pw
    change = arguments.get("change")
    target = arguments.get("target") if arguments.get("target") is not None else {}
    if not isinstance(change, Mapping) or not isinstance(target, Mapping):
        return ["target and change must be objects."]
    if not str(arguments.get("reason") or "").strip():
        return ["Say why in one line (reason)."]
    if not str(change.get("body") or "").strip():
        return [f"{system}: the message needs a body."]
    if system == "mail" and not _addresses(change.get("to")):
        return ["mail: say who it goes to (to: addresses)."]
    if system == "teams" and not (_addresses(change.get("members")) or target.get("chat_id")):
        return ["teams: say who it goes to (members: addresses) or the chat (target chat_id)."]
    request = {k: v for k, v in arguments.items() if k != "citations"}
    try:
        item = pw.item_of(request, "proposal@local", "job_probe", "job")
    except pw.Refused as refused:
        return refused.problems
    if len(aw.actions_of(item)) != 1:
        return [f"No write path for that {system} message."]
    return []


def _addresses(value: Any) -> list[str]:
    found = value if isinstance(value, (list, tuple)) else [value] if value else []
    return [str(a).strip().lower() for a in found if isinstance(a, str) and "@" in a]


def addressed(proposal: Mapping[str, Any]) -> set[str]:
    """Who a proposed message is addressed to (to, a chat's members; never its cc); empty
    for a system change."""
    args = proposal.get("arguments") if isinstance(proposal.get("arguments"), Mapping) else {}
    change = args.get("change") if isinstance(args.get("change"), Mapping) else {}
    if str(args.get("system") or "").lower() not in MESSAGES:
        return set()
    return {a for k in ("to", "members") for a in _addresses(change.get(k))}


def not_a_reply(proposals: Sequence[Mapping[str, Any]], event: Mapping[str, Any]
                ) -> list[dict[str, Any]]:
    """The proposals without a message addressed to the event's sender or posted into the
    event's own chat: that is a reply, the drafts' item, never a second one here. A message
    to someone else that copies the sender (asking a supplier for what the colleague asked
    about) stays."""
    who = str(event.get("sender") or "").strip().lower()
    chat = str(event.get("conversation_id") or event.get("chat_id") or "")
    out = []
    for p in proposals:
        args = p.get("arguments") if isinstance(p.get("arguments"), Mapping) else {}
        target = args.get("target") if isinstance(args.get("target"), Mapping) else {}
        if (who and who in addressed(p)) or (
                chat and str(args.get("system") or "").lower() == "teams"
                and str(target.get("chat_id") or "") == chat):
            continue
        out.append(dict(p))
    return out


def proposal_tools(into: list[dict[str, Any]]) -> list[Any]:
    """``plx.prepare_write`` and ``plx.edit_document`` (the Ask's own specs) bound to the
    list ``into``: a call that passes ``check`` is kept there (at most ``MOST``); nothing
    is prepared, written or sent."""
    from .agent_session import LocalTool
    from . import prepared_writes as pw

    def bound(name: str) -> Callable[[dict[str, Any]], Any]:
        def call(arguments: dict[str, Any]) -> Any:
            problems = check(name, arguments)
            if problems:
                return {"refused": problems[:4] + field_hints(problems)[:3]}
            if len(into) >= MOST:
                return {"refused": [f"At most {MOST} changes per event."]}
            into.append({"tool": name, "arguments": dict(arguments)})
            return {"proposed": len(into),
                    "note": "Proposed for the person to review and confirm in the app; "
                            "nothing is written."}
        return call

    spec = {**pw.TOOL_SPEC, "description": "Propose a change in a business system for the "
            "person to review and confirm (nothing is written). " + str(pw.TOOL_SPEC[
                "description"]).split(". ", 2)[-1]}
    return [LocalTool(spec=spec, call=bound(pw.TOOL_NAME)),
            LocalTool(spec=pw.DOC_TOOL_SPEC, call=bound(DOC_TOOL_NAME))]


def kept(proposals: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The proposals as an item keeps them: each checked again (a stored item is read back
    by the API, never trusted blindly), at most ``MOST``."""
    out: list[dict[str, Any]] = []
    for p in proposals:
        tool = str(p.get("tool") or "")
        args = p.get("arguments")
        if tool in (DOC_TOOL_NAME, "plx.prepare_write") and not check(tool, args):
            out.append({"tool": tool, "arguments": dict(args)})  # type: ignore[arg-type]
    return out[:MOST]


# ----------------------------------------------------------------------------- screens

# the people a non-message event names (a gate's requester and decider, a meeting's
# organizer and attendees, a task's assignee): each is cleared by the people exclusion
PEOPLE_KEYS = ("sender", "organizer", "requested_by", "decided_by", "assignee", "assigned_to")


def screen(event: Mapping[str, Any], people: Any, owner: str, prompt: str, text: str
           ) -> Optional[str]:
    """Why this event may not go to the model for ``owner`` (None: it may). A message: the
    drafts' screen (``autopilot_drafter.screened``: cleared by the feed, sender and reader
    cleared, pay or HR, every person its whole thread names, Freightcom). Any other event:
    the owner and every person it names cleared (``PEOPLE_KEYS`` of the event and its
    fields, attendees), pay or HR, the same thread and Freightcom screens over its text.
    Undecidable: refused (fail closed)."""
    from .autopilot_drafter import screened, thread_screen
    from .autopilot_engine import pay_or_hr
    from integrations.image_ocr_gpu import touches_freightcom
    if event.get("event_type") == "message.received":
        return screened(event, people, owner, prompt, text)
    fields = event.get("fields") if isinstance(event.get("fields"), Mapping) else {}
    named = [owner, *(event.get(k) for k in PEOPLE_KEYS), *(fields.get(k) for k in PEOPLE_KEYS),
             *(event.get("attendees") or ()), *(fields.get("attendees") or ()
                                                if isinstance(fields.get("attendees"), list)
                                                else ())]
    try:
        if not all(people.identity_clear(str(n)) for n in named if isinstance(n, str) and n):
            return "not_cleared"
    except Exception:  # noqa: BLE001 - undecidable: never read
        return "not_cleared"
    if pay_or_hr(event):
        return "pay_or_hr"
    why = thread_screen(people, f"{event.get('subject') or ''}\n{text or prompt}")
    if why:
        return why
    if touches_freightcom(prompt, text, json.dumps(fields, default=str),
                          event.get("audience"), event.get("records")):
        return "freightcom"
    return None


# ----------------------------------------------------------------------------- the model

class Jobber:
    """Runs one job per event on a pool (as ``autopilot_drafter.Writer`` runs drafts):
    ``run(..., key=...)`` answers ``job_pending`` until its session is done, then the
    ``Job`` or why there is none. ``call`` stands in for ``subprocess.run`` (the triage);
    ``lookup`` is the tool session (``Lookup``)."""

    def __init__(self, model: str, *, triage: Optional[str] = TRIAGE, lookup: Any = None,
                 binary: Optional[str] = None, home: Optional[str] = None,
                 per_cycle: Optional[int] = None, parallel: Optional[int] = None,
                 call: Optional[Callable[..., Any]] = None) -> None:
        from . import chat_ask_model as cm
        self.model = model
        self.triage = triage
        self.lookup = lookup
        self.binary = binary or os.environ.get(cm.BINARY_ENV) or cm.BINARY
        self.home = home or os.environ.get(cm.HOME_ENV) or cm.CLAUDE_HOME
        self.per_cycle = per_cycle if per_cycle is not None else int(
            os.environ.get(PER_CYCLE_ENV) or PER_CYCLE)
        self.parallel = max(1, parallel if parallel is not None else int(
            os.environ.get(PARALLEL_ENV) or PARALLEL))
        self.call = call or subprocess.run
        self.calls = 0
        self._pool: Any = None
        self._running: dict[str, Any] = {}

    def new_cycle(self) -> None:
        self.calls = 0

    @property
    def busy(self) -> bool:
        return self.calls >= self.per_cycle

    def pending(self) -> list[str]:
        return list(self._running)

    def wait(self, seconds: float) -> None:
        from concurrent.futures import wait
        if self._running:
            wait(list(self._running.values()), timeout=max(0.0, seconds))

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    def _triaged(self, event_text: str) -> Optional[bool]:
        """True/False from the small model; None when it failed (the session decides)."""
        from . import model_ledger
        from .agent_environment import agent_environment
        if not self.triage:
            return True
        prompt = triage_prompt(event_text)
        argv = [self.binary, "-p", "--setting-sources", "", "--tools", "",
                "--no-session-persistence", "--max-turns", "1", "--output-format", "json",
                "--model", self.triage]
        env = dict(agent_environment(os.environ))
        env["HOME"] = self.home
        env["CLAUDE_CONFIG_DIR"] = os.path.join(self.home, ".claude")
        started = time.monotonic()
        try:
            done = self.call(argv, input=prompt, capture_output=True, text=True,
                             timeout=TRIAGE_SECONDS, env=env, start_new_session=True)
            reply = json.loads(done.stdout or "{}")
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            model_ledger.record(FEATURE + "_triage", prompt=prompt, ok=False,
                                seconds=time.monotonic() - started, reason=type(exc).__name__)
            return None
        good = isinstance(reply, dict) and not reply.get("is_error")
        turns, tokens = model_ledger.usage_of(reply if isinstance(reply, dict) else {})
        model_ledger.record(FEATURE + "_triage", prompt=prompt, ok=good, turns=turns,
                            tokens=tokens, seconds=time.monotonic() - started)
        got = _json(reply.get("result")) if good else None
        if got is None or not isinstance(got.get("work"), bool):
            return None
        return got["work"]

    def _session(self, owner: str, event_text: str, person: str, message: bool = True,
                 as_of: str = "", brief_ok: bool = False, reads: bool = True
                 ) -> tuple[Optional[Job], str, list[str]]:
        # ``reads``: the assistant's (``autopilot_agent``); a job always reads (what it
        # proposes to an outside reader is held by ``autopilot_outbound`` instead)
        # only a message is sorted first: an approval asked or a meeting held is itself
        # work to look at (and far fewer)
        if message and self._triaged(event_text) is False:
            return None, "no_system_work", []
        if self.lookup is None:
            return None, "job_no_session", []
        proposals: list[dict[str, Any]] = []
        ran = self.lookup.run(owner, job_prompt(event_text, person, as_of),
                              tools=proposal_tools(proposals), job=True)
        if ran is None:
            return None, "job_no_session", []
        final, looked = ran
        got = _json(final) or {}
        summary = str(got.get("summary") or "").strip()[:300]
        brief = str(got.get("brief") or "").strip()[:BRIEF_CHARS] if brief_ok else ""
        if not proposals and not brief:
            return None, "no_system_work", [str(t) for t in looked]
        return Job(summary or ("A brief to read" if not proposals
                               else "System changes to review"), proposals, brief), "", \
            [str(t) for t in looked]

    def run(self, event: Mapping[str, Any], owner: str, event_text: str, person: str,
            people: Any, *, key: Optional[str] = None, as_of: str = "", reads: bool = True
            ) -> tuple[Optional[Job], str]:
        """(``Job``, "") or (None, why): ``no_system_work``, a screen (the drafter's),
        ``job_failed`` (tried again), ``job_no_session``, or with a ``key``
        ``job_pending`` (running on the pool: ask again with the same key). ``as_of``: a
        past event replayed for a grade (``AS_OF``); never set by the live cycle."""
        running = self._running.get(key) if key else None
        if running is not None:
            if not running.done():
                return None, "job_pending"
            del self._running[key]
            try:
                got, why, looked = running.result()
            except Exception:  # noqa: BLE001 - a failed session is tried again
                return None, "job_failed"
            return self._finish(got, why, looked, people, event, person)
        from .card_text import mask_served
        from . import chat_ask_model as cm
        prompt = mask_served(job_prompt(event_text, person))
        why = screen(event, people, owner, prompt, event_text)
        if why:
            return None, why
        if not cm.credential_path_safe(self.binary, self.home):
            return None, "job_model_unsafe"
        self.calls += 1
        message = event.get("event_type") == "message.received"
        # a brief (the work is to read, not to change a system): only an event that asks
        # for one, never a message's
        fields = event.get("fields") if isinstance(event.get("fields"), Mapping) else {}
        brief_ok = not message and fields.get("brief") is True
        if key is None:
            return self._finish(*self._session(owner, event_text, person, message, as_of,
                                               brief_ok, reads), people, event, person)
        if self._pool is None:
            from concurrent.futures import ThreadPoolExecutor
            self._pool = ThreadPoolExecutor(self.parallel, thread_name_prefix="job")
        self._running[key] = self._pool.submit(self._session, owner, event_text, person,
                                               message, as_of, brief_ok, reads)
        return None, "job_pending"

    @staticmethod
    def _finish(got: Optional[Job], why: str, looked: Sequence[str], people: Any,
                event: Optional[Mapping[str, Any]] = None, person: str = ""
                ) -> tuple[Optional[Job], str]:
        if got is None:
            return None, why
        from .autopilot_drafter import thread_screen
        from integrations.image_ocr_gpu import touches_freightcom
        seen = "\n".join([*looked, json.dumps(got.proposals, default=str), got.summary,
                          got.brief])
        hit = thread_screen(people, seen)
        if hit:
            return None, hit
        if touches_freightcom(seen):
            return None, "freightcom"
        proposals = kept(got.proposals)
        if event is not None and event.get("event_type") == "message.received":
            proposals = not_a_reply(proposals, event)
        if not proposals and not got.brief:
            return None, "no_system_work"
        return Job(got.summary, proposals, got.brief), ""


# The tool session a job runs as its person: the drafter's (``autopilot_drafter.
# LOOKUP_FACTORY``, registered by ``operator_api.autopilot_context``).
def from_environ(environ: Mapping[str, str]) -> Optional[Jobber]:
    """The live jobber: the model ``MODEL_ENV`` names, else the draft model; None when
    neither is set or it is ``off`` (a test's or a replay's environment never calls one)."""
    from . import autopilot_drafter as ad
    name = str(environ.get(MODEL_ENV) or environ.get(ad.MODEL_ENV) or "").strip()
    if not name or name.lower() in ("off", "none", "0"):
        return None
    triage = str(environ.get(TRIAGE_ENV) or TRIAGE).strip()
    lookup = None
    if ad.LOOKUP_FACTORY is not None:
        try:
            lookup = ad.LOOKUP_FACTORY()
        except Exception:  # noqa: BLE001 - no session: no jobs
            lookup = None
    return Jobber(name, triage=None if triage.lower() in ("off", "none", "0") else triage,
                  lookup=lookup)
