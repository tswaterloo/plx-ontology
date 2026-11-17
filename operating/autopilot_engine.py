"""The autopilot engine around ``run_cycle``: discovery, the ordered feed, draft context,
recipients, gates and settling an item (``operating.autopilot``, which re-exports them)."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from .autopilot_records import _addresses, _digest, _instant, _internal, _iso, _read, _types, _v
from .autopilot_queue import HandlerTimeout, Queue, TASK_DONE
from .autopilot_events import (
    TEAMS_TYPES,
    _mail_events,
    _people_maps,
    _record_events,
    _teams_events,
    approvals_cleared,
)
from .autopilot_handlers import Ctx, HANDLERS, OverlayCtx, _held, identified


def discover(st: Any, ids: Iterable[str], registry: Mapping[str, Any], people: Any, *,
             landed_at: str | None = None, outbound: list | None = None,
             now: datetime | None = None, feed_lag: Mapping[str, timedelta] | None = None,
             base: Any = None) -> tuple[list[dict[str, Any]], Counter]:
    """Typed events of the changed records ``ids`` (module doc); ``outbound`` collects the
    mailbox owners' own sent messages (thread, recipients, time) for ``Queue.note_sent``.
    ``feed_lag`` (a replay of past days): per record type, how long after its time the live
    feed first holds it (the capture's cadence); a decision taken before then was already
    taken when the feed saw the request, so no item could have been prepared for it.
    ``base``: the served store, when ``st`` is a scratch store of just-captured messages
    (``operating.autopilot_fast``): a sender's side is read from its parties, and a message
    the scratch store links to no record names its records by the identifiers in its words
    (``identified``: the closure's mention links come only with the delta layer)."""
    sources = registry["sources"]
    typed = _types(st, ids)
    by_type: dict[str, list[str]] = defaultdict(list)
    skipped: Counter = Counter()
    from operating.pg.own_issued import hidden_ids
    own = hidden_ids()   # PLX's own sales documents held as supplier documents: no AP event
    for oid, (otype, _aud) in typed.items():
        if otype in sources and oid not in own:
            by_type[otype].append(oid)
    directory, addresses_of, domains = _people_maps(people)
    events: list[dict[str, Any]] = []
    for otype, oids in sorted(by_type.items()):
        kind = sources[otype]["event"]
        if otype == "Email":
            events += _mail_events(st, oids, typed, people, directory, addresses_of, domains,
                                   skipped, outbound, parties=base)
        elif otype in TEAMS_TYPES:
            events += _teams_events(st, oids, typed, people, directory, skipped, outbound,
                                    unaddressed=True)
        else:
            events += _record_events(st, otype, kind, oids, typed, sources[otype], skipped,
                                     now=now, lag=(feed_lag or {}).get(otype))
    if base is not None:
        ctx = Ctx(base, (now or datetime.now(timezone.utc)).date(), time.monotonic() + 60,
                  domains)
        for e in events:
            if e.get("event_type") == "message.received" and not e.get("records"):
                try:
                    e["records"] = identified(ctx, str(e.get("subject") or ""),
                                              str(e.get("text") or ""))
                except HandlerTimeout:
                    skipped["records_unread"] += 1
    events = approvals_cleared(st, events, people, skipped)
    for e in events:
        e["landed_at"] = landed_at
    return events, skipped


EVQ_ENV = "PLX_AUTOPILOT_EVQ"
EVQ_DSN_ENV = "PLX_AUTOPILOT_EVQ_DSN"
EVQ_TOPIC = "autopilot"
# Lower first. An approval or a customer's message is handled before routine updates of
# other records; events about one entity keep their order whatever their priority.
EVENT_PRIORITY = {"approval.requested": 10, "message.received": 20, "task.assigned": 20,
                  "question.asked": 20, "qc.result": 30, "meeting.ended": 30,
                  "record.due": 40, "record.changed": 40}
CUSTOMER_MESSAGE_PRIORITY = 10
DEFERRED_RETRY_S = 300.0


def event_queue_on(environ: Mapping[str, str]) -> bool:
    return str(environ.get(EVQ_ENV) or "") in ("1", "true", "yes", "on")


def event_group(event: Mapping[str, Any]) -> str:
    """The entity an event is about, whose events are handled in order: a mail or chat
    thread, else the record (PO, task, approval...) by its type and id."""
    thread = event.get("conversation_id")
    if thread:
        return f"thread:{thread}"
    return f"{event.get('object_type') or 'object'}:{event.get('object_id') or event['event_id']}"


def event_priority(event: Mapping[str, Any]) -> int:
    kind = str(event.get("event_type") or "")
    if kind not in EVENT_PRIORITY:
        raise ValueError(f"unknown event type {kind!r}")   # registry types only
    if kind == "message.received" and event.get("side") == "customer":
        return CUSTOMER_MESSAGE_PRIORITY
    return EVENT_PRIORITY[kind]


class OrderedFeed:
    """The cycle's events through the ordered event queue (``operating.jobq.events``).
    Each autopilot queue file has a topic of its own (``topic_of``: an id kept in its meta),
    so one queue never hands out, holds back or finishes another's events. Every pending
    event of this queue is published, by its ``event_id``, grouped by ``event_group``, at
    ``event_priority``; one this topic already finished or dead-lettered while the local
    queue still holds it pending is revived (an event is never lost while its queue waits
    on it). Then the group heads are handed out, best priority first. The SQLite events table
    stays the record of each event's state; Postgres is the delivery order. ``settle``
    follows ``mark``: processed or skipped acks; a rate-limit deferral returns the event
    after ``DEFERRED_RETRY_S`` without spending a delivery; a handler failure that is retried
    spends one; failed goes to the dead letter. An event of a type with no priority is
    marked failed locally (``unknown_event_type``) and never published. A crash between
    the mark and the settle redelivers the event, and ``_process`` makes an item once per
    event and owner (its id is their digest)."""

    def __init__(self, queue: "Queue", environ: Mapping[str, str]) -> None:
        from operating.jobq import events as ev
        from operating.jobq import queue as jq
        self.local = queue
        self.evq = ev.EventQueue(environ.get(EVQ_DSN_ENV) or jq.dsn(environ),
                                 topic=topic_of(queue))
        self.consumer = f"autopilot:{os.getpid()}"
        self.held: dict[str, Any] = {}
        self.unknown = 0

    def events(self, limit: int):
        pending, refused = [], []
        for e in self.local.pending(1_000_000):
            try:
                pending.append((e, event_priority(e)))
            except ValueError:
                refused.append(e["event_id"])
        if refused:
            stamp = _iso(datetime.now(timezone.utc))
            with self.local.conn:
                for event_id in refused:
                    self.local.mark(event_id, "failed", "unknown_event_type", stamp)
            self.unknown = len(refused)
        self.evq.publish(({"event_id": e["event_id"], "event_type": e["event_type"],
                           "group_key": event_group(e), "priority": p,
                           "body": {"event_id": e["event_id"]}} for e, p in pending),
                         revive=True)
        bodies = {e["event_id"]: e for e, _p in pending}
        for _ in range(limit):
            got = self.evq.receive(self.consumer)
            if not got:
                return
            msg = got[0]
            event = bodies.get(msg.event_id)
            if event is None:          # this queue's event, no longer pending here: done
                self.evq.ack(msg)
                continue
            self.held[msg.event_id] = msg
            yield event

    def settle(self, event_id: str, state: str, reason: str | None) -> None:
        msg = self.held.pop(event_id, None)
        if msg is None:
            return
        if state == "deferred":
            self.evq.nack(msg, reason or "deferred", delay_s=DEFERRED_RETRY_S,
                          spend=reason != "rate_limited")
        elif state == "failed":
            self.evq.nack(msg, reason or "failed", dead=True)
        else:
            self.evq.ack(msg)

    def close(self) -> None:
        for msg in self.held.values():    # handed out but not handled: back at once
            self.evq.nack(msg, "cycle ended", delay_s=0)
        self.held.clear()
        self.evq.close()


def topic_of(queue: "Queue") -> str:
    """The event-queue topic of one autopilot queue file: an id the file is given once and
    keeps (``meta`` ``evq_topic``), so each person's, role's or replay's queue has its own,
    and a queue file made anew where an old one was never inherits the old one's events."""
    topic = queue.meta("evq_topic")
    if not topic:
        topic = f"{EVQ_TOPIC}:{hashlib.sha256(os.urandom(32)).hexdigest()[:20]}"
        with queue.conn:
            queue.set_meta("evq_topic", topic)
    return topic
DRAFT_CONTEXT_SECONDS = 3600     # a long-running follow makes a fresh provider hourly


def _with_context(provider: Any, owner: str, autopilot: str, work: Mapping[str, Any],
                  event: Mapping[str, Any]) -> tuple[Mapping[str, Any], Optional[dict]]:
    """``work`` with the owner's template applied and the ids it used (``used``: memory
    item ids and a template id, nothing else); unchanged with None when there is no
    provider, it fails, or it answers anything but ids."""
    if provider is None:
        return work, None
    try:
        got, used = provider.for_draft(owner, autopilot, work, event)
    except Exception:  # noqa: BLE001 - memory never stops a draft
        return work, None
    if used is None:
        return got if isinstance(got, Mapping) else work, None
    memory = used.get("memory") if isinstance(used, Mapping) else None
    related = used.get("related", []) if isinstance(used, Mapping) else None
    template = used.get("template") if isinstance(used, Mapping) else None
    if not all(isinstance(ids, list) and all(type(i) is int and i > 0 for i in ids)
               for ids in (memory, related)) \
            or not (template is None or (type(template) is int and template > 0)) \
            or not isinstance(got, Mapping):
        return work, None     # anything but ids: nothing used, nothing recorded
    if not memory and not related and template is None:
        return got, None
    # ``memory``: the entries that changed the draft; ``related``: kept, not applied
    return got, {"memory": memory[:20], "related": [i for i in related if i not in memory][:20],
                 "template": template}


# the prepared work's email drafts (``_recipients``): one draft, or a list of drafts
EMAIL_SLOTS = ("draft", "customer_notice")
EMAIL_LISTS = ("customer_notices",)


def _recipient_lessons(learned: Any, owner: str) -> dict[str, str]:
    """The owner's learned recipient lessons (address -> ``never`` / ``to`` / ``cc``,
    ``assist.resolve``'s ``learned``) when the lessons carry them (``recipients``); none
    otherwise or when reading them fails."""
    fn = getattr(learned, "recipients", None) if learned else None
    got = _learned_call(fn, owner, default={}) if callable(fn) else {}
    if not isinstance(got, Mapping):
        return {}
    return {str(a).lower(): str(v) for a, v in got.items()
            if isinstance(a, str) and v in ("never", "to", "cc")}


def _recipients(work: Mapping[str, Any], event: Mapping[str, Any], owner: str, people: Any,
                learned: Mapping[str, str]) -> dict[str, Any]:
    """``work`` with every email draft's To and Cc decided for ``owner`` by
    ``assist.resolve_reply`` (the people exclusion, the owner's own addresses, their learned
    lessons, ``assist.REPLY_ALL_MAX``) from what the handler proposed: the first To first
    (who the reply answers, or the address a record states), the rest in order. Each kept
    draft says, per address, why it is there and the citation it was read from (the
    draft's ``source``, else the event's message) and what is asked instead of a guess
    (``recipients``). Fails closed: the exclusion unavailable, or the first recipient
    withheld or no address, and the draft is dropped (a gap says so); asked only (an
    automated address), it is kept with no To, so it cannot be sent."""
    from . import assist
    cite = next((h for h in event.get("handles") or () if isinstance(h, str) and h), None) \
        or event.get("object_id")
    gaps: list[str] = []

    def decide(draft: Any) -> Any:
        if not isinstance(draft, Mapping) or draft.get("channel") not in ("email", "mail"):
            return draft
        to = [t.get("address") if isinstance(t, Mapping) else t for t in draft.get("to") or ()]
        cc = [t.get("address") if isinstance(t, Mapping) else t for t in draft.get("cc") or ()]
        try:
            got = assist.resolve_reply(sender=to[0] if to else None, to=to[1:], cc=cc,
                                       me=owner, people=people, learned=learned,
                                       source=draft.get("source") or cite)
        except assist.AssistRefused as refused:
            gaps.append(f"recipients_{refused}")
            return None
        if not got.to and not got.ask:
            gaps.append("recipients_withheld" if got.withheld else "recipients_unresolved")
            return None
        new = {k: v for k, v in draft.items() if k not in ("to", "cc", "source")}
        new["to"] = [{"address": a} for a in got.to]
        if got.cc:
            new["cc"] = [{"address": a} for a in got.cc]
        new["recipients"] = {"why": dict(got.basis), "source": dict(got.sources),
                             **({"withheld": got.withheld} if got.withheld else {}),
                             **({"ask": list(got.ask)} if got.ask else {})}
        return new

    out = dict(work)
    for slot in EMAIL_SLOTS:
        if slot in out:
            kept = decide(out[slot])
            if kept is None:
                out.pop(slot)
            else:
                out[slot] = kept
    for slot in EMAIL_LISTS:
        if isinstance(out.get(slot), list):
            out[slot] = [d for d in map(decide, out[slot]) if d is not None]
    if gaps:
        out["gaps"] = list(dict.fromkeys([*(work.get("gaps") or ()), *gaps]))
    return out


def _learned_call(fn: Callable[..., Any], *args: Any, default: Any, **kw: Any) -> Any:
    """A lesson applied, or ``default`` when applying it fails: learning never stops a
    cycle nor changes what is prepared on its own failure."""
    try:
        return fn(*args, **kw)
    except Exception:  # noqa: BLE001 - the item is prepared as without lessons
        return default


def pay_or_hr(event: Mapping[str, Any]) -> bool:
    """Whether an event's own words are pay, personal financial or HR-confidential matter
    (``chunking.sensitive.personal_financial``, the text index's rule): no item for it."""
    from .chunking.sensitive import personal_financial
    fields = event.get("fields") or {}
    title = str(event.get("subject") or fields.get("title") or fields.get("subject") or "")
    text = "\n".join(str(x) for x in (title, event.get("text") or "",
                                        fields.get("reason") or "") if x)
    try:
        return personal_financial(text, title=title or None) is not None
    except Exception:  # noqa: BLE001 - undecidable: withheld (fail closed)
        return True


GATE_FILE = "autopilot-gate.json"
GATE_ENV = "PLX_AUTOPILOT_GATE_FILE"
GATE_SCHEMA = "plx-autopilot-gate/v1"
GATE_PRECISION = Decimal("0.9")
GATE_MIN_JUDGED = 15


def gate_from(environ: Mapping[str, str], queue_dir: Path
              ) -> tuple[Callable[..., bool], dict[str, list[str]]]:
    """The live gate, per role: a workflow is on for an owner only in a role whose replay
    measured the owner (a member) and judged at least ``GATE_MIN_JUDGED`` items of it with
    at least ``GATE_PRECISION`` matched (recomputed here from the counts, never a stored
    verdict). ``gate(owner, workflow, roles)``: ``roles`` are the roles the item reaches
    its owner through (``item_roles``); a pass in one role never switches the workflow on
    in another. An item of the owner's own (no role: their mail, their tasks) is on only
    when a role of theirs passed it and none of their roles measured it and failed (a
    ``person:`` replay is their whole day, so it counts for every item of theirs). No
    file, an unreadable one or another schema: nothing is on (fail closed)."""
    path = Path(environ.get(GATE_ENV) or queue_dir / GATE_FILE)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    return gate_of(raw)


def gate_of(raw: Any) -> tuple[Callable[..., bool], dict[str, list[str]]]:
    """``gate_from`` over a gate document already read (``None`` or anything that is not
    a ``GATE_SCHEMA`` document: nothing is on)."""
    # role -> (members, workflows passed, workflows measured)
    table: dict[str, tuple[set[str], set[str], set[str]]] = {}
    try:
        if not isinstance(raw, dict) or raw.get("schema") != GATE_SCHEMA:
            raise ValueError("schema")
        for name, role in (raw.get("roles") or {}).items():
            members = {str(m).lower() for m in role.get("members") or ()}
            ok, seen = set(), set()
            for flow, counts in (role.get("workflows") or {}).items():
                judged, matched = int(counts["judged"]), int(counts["matched"])
                seen.add(str(flow))
                if judged >= GATE_MIN_JUDGED and 0 <= matched <= judged and \
                        Decimal(matched) / Decimal(judged) >= GATE_PRECISION:
                    ok.add(str(flow))
            table[str(name)] = (members, ok, seen)
    except (ValueError, TypeError, KeyError, AttributeError):
        table = {}

    def gate(owner: str, workflow: str, roles: Iterable[str] = ()) -> bool:
        owner = str(owner).lower()
        mine = {n: t for n, t in table.items() if owner in t[0]}
        personal = [t for n, t in mine.items() if n.startswith("person:")]
        if any(workflow in t[1] for t in personal):
            return True
        if any(workflow in t[2] for t in personal):
            return False                    # the person's own day failed it
        roles = set(roles)
        if roles:
            return any(workflow in mine[r][1] for r in roles if r in mine)
        return any(workflow in t[1] for t in mine.values()) and \
            not any(workflow in t[2] and workflow not in t[1] for t in mine.values())

    passed: dict[str, set[str]] = defaultdict(set)
    for members, ok, _seen in table.values():
        for m in members:
            passed[m] |= ok
    return gate, {m: sorted(w) for m, w in passed.items() if w}


DRAFT_FILE = Path(__file__).with_name("autopilot_draft.json")
DRAFT_ENV = "PLX_AUTOPILOT_DRAFT_FILE"
DRAFT_SCHEMA = "plx-autopilot-draft/v1"
# never prepared in draft, whatever the config says: no rule reaches the gate's bar on it
# (dev ceiling ~75-81%, 2026-11-11) and a wrong external reply costs the most
DRAFT_NEVER = frozenset({"other_external"})


def load_draft(environ: Mapping[str, str] | None = None) -> dict[str, list[str]]:
    """The draft config (``autopilot_draft.json``, ``DRAFT_ENV`` overrides the path): per
    person (an address or a person id), the workflows prepared for them in Draft before
    their replay gate passed. Draft only prepares: an item still waits on its owner's
    accept, and anything it sends or writes goes through the write path's own gates and
    the owner's confirm (``autopilot_write``), unchanged. Unreadable, another schema, an
    unknown workflow or one of ``DRAFT_NEVER``: nothing is in draft (the whole file is
    refused, as the registry is)."""
    env = os.environ if environ is None else environ
    path = Path(env.get(DRAFT_ENV) or DRAFT_FILE)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping) or raw.get("schema") != DRAFT_SCHEMA:
            return {}
        from . import workflows as wf
        known = wf.names()
        out: dict[str, list[str]] = {}
        for who, flows in (raw.get("people") or {}).items():
            if not isinstance(flows, list) or not all(isinstance(f, str) for f in flows):
                return {}
            if any(f not in known or f in DRAFT_NEVER for f in flows):
                return {}
            out[str(who).strip().lower()] = sorted(set(flows))
        return out
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def draft_owners(draft: Mapping[str, Sequence[str]], directory: Mapping[str, str],
                 people: Any) -> dict[str, frozenset[str]]:
    """Person id -> the workflows in draft for them: an address resolved through the
    people directory, an id taken as it is; each cleared by the people exclusion (an
    unresolved or excluded person gets nothing)."""
    guid = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    out: dict[str, frozenset[str]] = {}
    for who, flows in draft.items():
        oid = who if guid.match(who) else directory.get(who, "")
        if not oid or people is None or getattr(people, "unavailable", None) \
                or not people.identity_clear(oid):
            continue
        out[oid] = frozenset(out.get(oid, frozenset()) | set(flows))
    return out


def with_draft(gate: Callable[..., bool], drafted: Mapping[str, frozenset[str]]
               ) -> Callable[..., bool]:
    """The prepare gate with Draft: a workflow in draft for its owner is prepared for them
    (only for them: another owner of the same event still needs the replay gate)."""
    if not drafted:
        return gate

    def drafted_gate(owner: str, workflow: str, roles: Iterable[str] = ()) -> bool:
        return workflow in drafted.get(str(owner).lower(), ()) or gate(owner, workflow, roles)
    return drafted_gate


def item_roles(ap: Mapping[str, Any]) -> frozenset[str]:
    """The roles an autopilot's item reaches its owner through: its business rule's role
    (``role:<x>``; ``buyer`` is purchasing, ``approvers`` the owner role) and the mailbox
    role it requires; none for an item of the owner's own."""
    rule, when = str(ap.get("owner") or ""), ap.get("when") or {}
    out = set()
    if rule.startswith("role:"):
        out.add(rule[len("role:"):])
    elif rule == "buyer":
        out.add("purchasing")
    elif rule == "approvers":
        out.add("owner")
    if when.get("mailbox_role"):
        out.add(str(when["mailbox_role"]))
    return frozenset(out)


def _happened(event: Mapping[str, Any]) -> dict[str, Any]:
    keep = ("event_type", "object_id", "object_type", "channel", "at", "sender", "subject",
            "intents", "side", "status", "quality", "records", "mailbox")
    out = {k: event[k] for k in keep if event.get(k) not in (None, "", [])}
    name = person_name(event.get("sender_name"))
    if name:
        out["sender_name"] = name
    if event.get("channel") == "teams" and event.get("event_type") == "message.received":
        # a chat message has no subject: the item carries a short cut of its own text (the
        # item is shown only to a reader every citation of it, the message's, is readable
        # to). An event typed without the text (the fast feed's scratch store may hold no
        # text index) leaves it to ``autopilot.name_teams_items``, which reads the served
        # store's text or marks that the message has none
        snippet = message_snippet(event.get("text"))
        if snippet:
            out["snippet"] = snippet
    if event.get("fields"):
        out["fields"] = {k: v for k, v in event["fields"].items()
                         if k in ("title", "task_id", "reason", "lot_number", "subject",
                                  "stage", "requested_by", "po_number", "order_date")}
    return out


UNNAMED_SENDER = "Someone"
SNIPPET_CHARS = 200
NAME_CHARS = 120
NO_TEXT = "(no text in this message: an attachment, image or card)"
NOT_READ = "(message text not read yet)"
_ID_LIKE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                      r"|[0-9a-z:._@-]*\d[0-9a-z:._@-]*", re.IGNORECASE)


def person_name(value: Any) -> str:
    """A person's display name as the source stated it (a Teams message's
    ``from.display_name``): one line, at most ``NAME_CHARS``; "" for anything that is not a
    name (empty, an object id or other id-shaped token)."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())[:NAME_CHARS].strip()
    return "" if not text or _ID_LIKE.fullmatch(text) else text


def message_snippet(text: Any) -> str:
    """A message's first ``SNIPPET_CHARS`` characters on one line, card numbers and
    credential-shaped values masked (``card_text.mask_served``, the serving rule) on the
    whole text before any cut, so no cut (here, or a title's) leaves part of a number the
    serving mask no longer recognises; "" when it has no text."""
    from .card_text import mask_served
    if not isinstance(text, str):
        return ""
    flat = " ".join(re.sub(r"<[^>]{0,400}>", " ", text).split())
    if not flat:
        return ""
    flat = " ".join(mask_served(flat).split())
    if len(flat) <= SNIPPET_CHARS:
        return flat
    cut = flat[:SNIPPET_CHARS]
    return (cut.rsplit(" ", 1)[0] if " " in cut[SNIPPET_CHARS // 2:] else cut) + "\u2026"


def sender_label(sender: Any, name: Any = None) -> str:
    """How a sender is shown to a person: the name the source stated (``person_name``),
    else an address as is; anything else (a Teams sender's bare object id with no name,
    nothing) reads "Someone". Fail-closed: a value that is not an address is never shown
    as a name."""
    named = person_name(name)
    if named:
        return named
    text = str(sender or "").strip()
    return text if "@" in text else UNNAMED_SENDER


def shown_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """A display copy of an item: its sender reads through ``sender_label`` (the stated
    name, an address or "Someone"), and an item prepared before that rule has the raw id it
    wrote into its title and summary replaced too, so no re-prepare is needed. A Teams
    message's item carries ``message``: its snippet, or why there is none. Only for what a
    person is shown (lists, digest, audit titles): the stored item keeps the raw sender,
    which the people exclusion and the recipient checks read, and the write path reads the
    stored item."""
    body = dict(item.get("body") or {})
    happened = dict(body.get("happened") or {})
    prepared = dict(body.get("prepared") or {})
    title = str(item.get("title") or "")
    raw = str(happened.get("sender") or "").strip()
    shown = sender_label(raw, happened.get("sender_name"))
    if raw and shown != raw:
        happened["sender"] = shown
        # the old text began "<id> is waiting..."; a case-insensitive match covers the
        # lowercased id ``teams_sender`` stored. A short or spaced value is not an id:
        # the rest of the text is left alone
        if len(raw) >= 8 and not any(c.isspace() for c in raw):
            pattern = re.compile(re.escape(raw), re.IGNORECASE)
            title = pattern.sub(lambda _m: shown, title)
            if isinstance(prepared.get("summary"), str):
                prepared["summary"] = pattern.sub(lambda _m: shown, prepared["summary"])
    happened.pop("sender_name", None)
    if happened.get("channel") == "teams" and happened.get("event_type") == "message.received":
        happened["message"] = happened.get("snippet") or (
            NO_TEXT if happened.get("snippet_missing") == "no_text" else NOT_READ)
    if "happened" in body:
        body["happened"] = happened
    if "prepared" in body:
        body["prepared"] = prepared
    return {**item, "title": title, "body": body}


def _prepare(st: Any, ap: Mapping[str, Any], event: Mapping[str, Any], as_of: date,
             domains: Sequence[str], seconds: float, *, over: Any = None) -> dict[str, Any]:
    """The work of one event; ``over``: a store whose records are read first
    (``OverlayCtx``: the fast feed's scratch store of an MC gate's closure)."""
    deadline = time.monotonic() + seconds
    ctx = OverlayCtx(st, over, as_of, deadline, domains) if over is not None else \
        Ctx(st, as_of, deadline, domains)
    began = time.monotonic()
    st.watch(lambda: 1 if time.monotonic() > deadline else 0)
    try:
        work = HANDLERS[ap["handler"]](ctx, event)
    except HandlerTimeout:
        return {"error": "handler_timeout"}
    except sqlite3.OperationalError as exc:
        if "interrupt" in str(exc):
            return {"error": "handler_timeout"}
        return {"error": "handler_failed"}
    except Exception:  # noqa: BLE001 - one handler's failure never stops the cycle
        return {"error": "handler_failed"}
    finally:
        st.watch(None)
    if isinstance(work, Mapping) and work.get("skip"):
        return {"skip": str(work["skip"])}
    read = set(ctx.objects) | {event["object_id"]}
    mine = _held(over, read) if over is not None else set()
    typed = {**_types(st, read - mine), **(_types(over, mine) if mine else {})}
    audiences = {aud for _t, aud in typed.values()}
    if event.get("audience"):
        audiences.add(event["audience"])
    markings: set[str] = set()
    for row in [*_markings(st, read - mine), *(_markings(over, mine) if mine else [])]:
        markings.update(row)
    # every record the work rests on: the reader's check (``visible``) covers each one
    cites = list(dict.fromkeys([*(event.get("handles") or ()), *ctx.handles]))
    return {"work": work, "audiences": audiences, "markings": markings, "citations": cites,
            "ms": int((time.monotonic() - began) * 1000)}


def _markings(st: Any, ids: Iterable[str]) -> list[list[str]]:
    """The markings of the values the handler read (a finance figure marks the item)."""
    out = []
    ids = sorted(set(ids))
    for start in range(0, len(ids), 500):
        for (raw,) in st.conn.execute(
                "SELECT DISTINCT markings FROM object_values WHERE superseded_at IS NULL AND "
                "object_id IN (SELECT value FROM json_each(?))",
                (json.dumps(ids[start:start + 500]),)):
            try:
                value = json.loads(raw) if raw else []
            except ValueError:
                value = ["unreadable"]
            out.append([str(v) for v in value] if isinstance(value, list) else [str(value)])
    return out


def _event_body(queue: Queue, event_id: str) -> dict[str, Any]:
    row = queue.conn.execute("SELECT body FROM events WHERE event_id = ?",
                             (event_id,)).fetchone()
    try:
        body = json.loads(row[0]) if row else {}
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def settle(st: Any, queue: Queue, registry: Mapping[str, Any], people: Any, *,
           now: datetime) -> Counter:
    """Mark open items done from evidence (module doc)."""
    from . import proactive_threads as pt
    done: Counter = Counter()
    directory, addresses_of, domains = _people_maps(people)
    stamp = _iso(now)
    for item in queue.items():
        rule = item["body"].get("done_when")
        happened = item["body"].get("happened") or {}
        oid = happened.get("object_id")
        reason = None
        try:
            if rule == "owner_replied" and happened.get("channel") == "mail":
                ev = queue.conn.execute("SELECT body FROM events WHERE event_id = ?",
                                        (item["event_id"],)).fetchone()
                body = json.loads(ev[0]) if ev else {}
                conv, at = body.get("conversation_id"), body.get("at") or ""
                mine = addresses_of.get(item["owner"], set())
                if conv and mine:
                    members = pt._members(st, {conv}, True).get(conv, [])
                    rows = _read(st, members, ("sender", "sent_at"))
                    if any((_addresses(_v(r, "sender")) or [""])[0] in mine
                           and _v(r, "sent_at") > at for r in rows.values()):
                        reason = "owner_replied"
            elif rule == "owner_replied" and happened.get("channel") == "teams":
                # AUTOMATION-009: the owner's own message in the same chat or thread
                # after this one (``note_sent``: the thread digest and time)
                body = _event_body(queue, item["event_id"])
                after = _instant(body.get("at"))
                conv = body.get("conversation_id")
                if conv and after is not None and queue.conn.execute(
                        "SELECT 1 FROM sent WHERE owner = ? AND thread = ? AND at > ? LIMIT 1",
                        (item["owner"], _digest("thread", conv), _iso(after))).fetchone():
                    reason = "owner_replied"
            elif rule == "owner_mailed_attendees":
                # AUTOMATION-009: the organizer wrote to an outside attendee (whom the
                # follow-up goes to, ``h_meeting``) after the meeting ended; a mail to a
                # colleague who attended is no follow-up
                body = _event_body(queue, item["event_id"])
                ended = _instant(body.get("at"))
                parties = sorted({_digest("party", str(a).lower())
                                  for a in body.get("attendees") or ()
                                  if not _internal(str(a).lower(), domains)
                                  and str(a).lower() not in addresses_of.get(item["owner"], set())
                                  and str(a).lower() != item["owner"]})
                if ended is not None and parties and queue.conn.execute(
                        "SELECT 1 FROM sent WHERE owner = ? AND at > ? AND counterpart IN "
                        "(SELECT value FROM json_each(?)) LIMIT 1",
                        (item["owner"], _iso(ended), json.dumps(parties))).fetchone():
                    reason = "owner_mailed_attendees"
            elif rule == "approval_decided" and oid:
                r = _read(st, [oid], ("status",)).get(oid)
                if r and _v(r, "status").lower() not in ("", "pending"):
                    reason = "approval_decided"
            elif rule == "task_closed" and oid:
                r = _read(st, [oid], ("stage", "completed_at")).get(oid)
                if r and (TASK_DONE.match(_v(r, "stage")) or _v(r, "completed_at")):
                    reason = "task_closed"
            elif rule == "question_answered" and oid:
                reader = getattr(st, "autopilot_mc", None)
                got = st.conn.execute("SELECT native_key FROM objects WHERE object_id = ?",
                                      (oid,)).fetchone()
                key = reader.key_of(got[0]) if reader is not None and got else None
                q = reader.asked(*key) if key else None
                if q and (q["answered_at"] or q["withdrawn_at"]):
                    reason = "question_answered"
            elif rule == "batch_released" and oid:
                r = _read(st, [oid], ("quality_status",)).get(oid)
                if r and _v(r, "quality_status") == "Released":
                    reason = "batch_released"
        except sqlite3.Error:
            continue
        if reason:
            with queue.conn:
                queue.settle_item(item["item_id"], "done", reason, stamp)
            done[reason] += 1
    return done
