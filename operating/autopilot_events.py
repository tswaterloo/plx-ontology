"""Turning the projection into autopilot events: mail, Teams, records and questions
(``operating.autopilot``, which re-exports them)."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from .autopilot_records import (
    _addresses,
    _digest,
    _instant,
    _internal,
    _iso,
    _linked,
    _read,
    _v,
    _version_digest,
)
from .autopilot_queue import (
    BUSINESS,
    EMAIL_PROPS,
    MAX_RECORDS,
    REPLY_HOURS,
    SIDE_LINKS,
    TASK_DONE,
    TEXT_CHARS,
    intents_of,
)


# ----------------------------------------------------------------------------- events

def _people_maps(people: Any) -> tuple[dict[str, str], dict[str, set[str]], tuple[str, ...]]:
    from . import proactive_threads as pt
    directory = {str(a).lower(): str(o).lower()
                 for a, o in (getattr(people, "directory", None) or {}).items()}
    addresses_of: dict[str, set[str]] = defaultdict(set)
    for address, oid in directory.items():
        addresses_of[oid].add(address)
    return directory, addresses_of, tuple(pt._domains(people))


def _value_window(st: Any, registry: Mapping[str, Any], since: str, until: str,
                  people: Any = None) -> set[str]:
    """The records of the types the event index does not date (``value_windows``: type ->
    its time property) whose time falls in [since, until), listed by the type index."""
    import bisect
    out: set[str] = set()
    lo, hi = _instant(since), _instant(until)
    for otype, prop in (registry.get("value_windows") or {}).items():
        dated = _dated(st, otype, prop)
        start = bisect.bisect_left(dated, (lo,)) if lo else 0
        end = bisect.bisect_left(dated, (hi,)) if hi else len(dated)
        out.update(o for _t, o in dated[start:end])
    return out


def _dated(st: Any, otype: str, prop: str) -> list[tuple[datetime, str]]:
    """(time, id) of every current record of ``otype``, sorted: read once per store
    connection (a replay asks for each of its days; a time the record does not state
    leaves it out)."""
    from ontology import store as ostore
    cache = st.__dict__.setdefault("_autopilot_dated", {})
    if (otype, prop) not in cache:
        ids = [r[0] for r in st.conn.execute(
            "SELECT o.object_id FROM objects o WHERE o.object_type = ? AND NOT "
            + ostore.removed_at_sql("o.object_id"), (otype,))]
        dated = []
        for start in range(0, len(ids), 5000):
            for o, r in _read(st, ids[start:start + 5000], (prop,)).items():
                at = _instant(_v(r, prop))
                if at is not None:
                    dated.append((at, o))
        cache[(otype, prop)] = sorted(dated)
    return cache[(otype, prop)]
FOLLOW_HORIZON = timedelta(hours=REPLY_HOURS)
HORIZON_EVENTS = ("message.received", "meeting.ended")


# With the change feed (migration 0026, ``operating.pg.change_feed``) the layers' records
# are instead the records the applied cycles really changed past this feed's cursor
# (``feed_ids``), the applied layers taken in unopened; more than ``FOLLOW_LAYER_CAP`` of
# them, or a migration's scope, are left to the window feed like a big layer. The first
# read and a gap read the layers as above once. The cursor moves when the cycle's layers
# are marked taken (``main``).
FEED_CONSUMER = "autopilot_follow"


# An MC approval's and its task's projected properties, each under the name MC's own row
# gives it (``autopilot_fast.mc_clear`` reads those: ``GATE_PEOPLE``, ``TASK_PEOPLE``);
# every other property read is scanned for addresses under its projected name.
MC_GATE_NAMES = {"requested_by": "requestedBy", "decided_by": "decidedBy"}
MC_TASK_NAMES = {"assignee": "assignee", "accountable_owner": "accountableOwner",
                 "reporter": "reporter"}
MC_GATE_READ = ("gate_id", "task_id", "status", "reason", "requested_by", "requested_runtime",
                "requested_at", "decided_by", "decided_at", "note", "workbench_intent_id",
                "gate_scope", "proposal_action", "proposal_summary", "proposal_risk")
MC_TASK_READ = ("task_id", "title", "description", "stage", "bucket", "priority", "assignee",
                "reporter", "accountable_owner", "created_at", "updated_at", "completed_at",
                "estimate", "due_date")


def _mc_row(row: Mapping[str, Any], names: Mapping[str, str]) -> dict[str, Any]:
    return {names.get(prop, prop): cell.get("value") for prop, cell in row.items()
            if isinstance(cell, Mapping) and cell.get("value") not in (None, "")}


def approvals_cleared(st: Any, events: list[dict[str, Any]], people: Any,
                      skipped: Counter) -> list[dict[str, Any]]:
    """The events less each MC approval the people exclusion does not clear, by the fast
    feed's one rule (``autopilot_fast.mc_clear``): its unit, its requester (required) and
    decider, its task's (``MCApproval.gate_of``) assignee, accountable owner and reporter,
    and every address any of their properties holds; a bare handle is the directory's
    addresses with that local part, one no address has withholds it. Fail closed: a read
    that fails or an exclusion that raises withholds every approval of the cycle. A
    withheld approval is never stored as an event (``approval_not_cleared``). With the MC
    capture reader (``st.autopilot_mc``, live follow and a replay) the same check also runs
    on the gate's and its task's current capture rows (``autopilot_fast.capture_rows``):
    the task's co-assignees, its activity and comments and the gate's proposal are not
    projected, and the fast feed reads them there; both feeds enforce one rule. The
    sibling-gate rule too: another gate of the same task (projected, ``MCApproval.gate_of``,
    or in the capture, ``autopilot_fast.gate_closure``) naming a person not cleared
    withholds the approval, as the fast feed never makes its event."""
    from ontology.project import native_key_of
    from .autopilot_fast import capture_rows, gate_closure, mc_clear
    gates = [e for e in events if e.get("object_type") == "MCApproval"]
    if not gates:
        return events
    withheld: set[int] = set()
    reader = getattr(st, "autopilot_mc", None)
    try:
        oids = sorted({str(e["object_id"]) for e in gates})
        tasks_of = {oid: sorted({o for _t, o in _linked(st, oid, ("MCApproval.gate_of",))})
                    for oid in oids}
        gates_of = {t: sorted({o for _l, o in _linked(st, t, ("MCApproval.gate_of",))})
                    for ts in tasks_of.values() for t in ts}
        rows = _read(st, sorted(set(oids).union(*gates_of.values())), MC_GATE_READ)
        task_rows = _read(st, list(gates_of), MC_TASK_READ)
        for e in gates:
            oid = str(e["object_id"])
            gate = _mc_row(rows.get(oid) or {}, MC_GATE_NAMES)
            tasks = [_mc_row(task_rows.get(t) or {}, MC_TASK_NAMES) for t in tasks_of[oid]]
            unit = str(e.get("audience") or "")
            others = sorted({g for t in tasks_of[oid] for g in gates_of[t]} - {oid})
            if not all(mc_clear(people, unit, gate, task) for task in (tasks or [None])) \
                    or not all(mc_clear(people, unit, _mc_row(rows.get(g) or {}, MC_GATE_NAMES),
                                        None) for g in others):
                withheld.add(id(e))
                continue
            if reader is not None:
                try:
                    key = json.loads(native_key_of(oid))
                except (ValueError, IndexError):
                    key = {}
                key = key if isinstance(key, dict) else {}
                raw = capture_rows(reader.conn, key.get("scope"), key.get("id"))
                if raw is not None and (
                        not mc_clear(people, raw[0] or unit, raw[1], raw[2])
                        or gate_closure(reader.conn, key["scope"], raw[1].get("task_id"),
                                        f"{key['scope']}/approvals/{key['id']}", people,
                                        raw[2]) is None):
                    withheld.add(id(e))
    except Exception:  # noqa: BLE001 - an exclusion that cannot decide withholds
        withheld = {id(e) for e in gates}
    if withheld:
        skipped["approval_not_cleared"] += len(withheld)
    return [e for e in events if id(e) not in withheld]


def _mail_events(st: Any, oids: Sequence[str], typed: Mapping[str, tuple[str, str]],
                 people: Any, directory: Mapping[str, str],
                 addresses_of: Mapping[str, set[str]], domains: Sequence[str],
                 skipped: Counter, outbound: list | None = None, parties: Any = None
                 ) -> list[dict[str, Any]]:
    from . import inbox_triage as it
    from . import proactive_threads as pt
    out = []
    rows = _read(st, oids, EMAIL_PROPS)
    machine = it.automated_mail()   # triage's test; a cycle is no window: its form rules
    for oid in oids:
        row = rows.get(oid) or {}
        aud = typed[oid][1]
        mailbox = _v(row, "mailbox").lower()
        if not mailbox or aud != f"mail:{mailbox}":
            skipped["mailbox_unknown"] += 1
            continue
        if not people.unit_clear(aud):
            skipped["mailbox_not_cleared"] += 1   # dropped unread
            continue
        sender = (_addresses(_v(row, "sender")) or [""])[0]
        mine = addresses_of.get(mailbox, set())
        if not sender or sender in mine:
            skipped["not_inbound"] += 1
            if sender and outbound is not None and (_v(row, "sent_at") or _v(row, "received_at")):
                to = _addresses(_v(row, "to_addresses")) + _addresses(_v(row, "cc_addresses"))
                outbound.append({
                    "owner": mailbox, "conversation": _v(row, "conversation_id"),
                    "message_id": _v(row, "internet_message_id") or oid, "to": to,
                    "at": _v(row, "sent_at") or _v(row, "received_at"),
                    **routine_note(_v(row, "subject"), _v(row, "body_preview"), sender, to,
                                   mine, domains, oid)})
            continue
        subject, text = _v(row, "subject"), pt._own(_v(row, "body_preview"))
        if machine(sender, text, subject):
            skipped["automated"] += 1
            continue
        to, cc = _addresses(_v(row, "to_addresses")), _addresses(_v(row, "cc_addresses"))
        participants = [a for a in [sender, *to, *cc] if _internal(a, domains)]
        cleared = all(people.identity_clear(a) for a in participants)
        links = _linked(st, oid)
        side = next((SIDE_LINKS[t] for t, _o in links if t in SIDE_LINKS), None)
        if side is None:
            side = "internal" if _internal(sender, domains) else \
                _party_side(parties or st, sender, domains)
        records = sorted({o for _t, o in links if o.split(":", 1)[0] in BUSINESS})
        message_id = _v(row, "internet_message_id") or oid
        at = _v(row, "received_at") or _v(row, "sent_at")
        out.append({
            "event_id": _digest("message.received", message_id, mailbox),
            "event_type": "message.received", "object_id": oid, "object_type": "Email",
            "channel": "mail", "at": at, "mailbox": mailbox, "audience": aud,
            "sender": sender, "to": to, "cc": cc, "to_me": bool(mine & set(to)),
            "named_me": named_in(text, mine),
            "subject": subject[:200], "text": text[:TEXT_CHARS],
            "conversation_id": _v(row, "conversation_id"), "message_id": message_id,
            "intents": intents_of(subject, text), "side": side, "records": records[:MAX_RECORDS],
            # the cheap rules' certain asks; a mail put to the reader that they find none
            # in (a request without a question form, a chase, a problem waiting on the
            # reader) is the model's to read (``ask_check``: ``autopilot._judge_asks``):
            # replayed on 2026-07 mail, 40 of 41 replies that no rule noticed were to mail
            # the phrase rules read as asking nothing
            **_mail_ask(it, text, subject, to_me=bool(mine & set(to))),
            "closing": closing(text),
            "addressed_other": addressed_other(text, mine, [*to, *cc]),
            "reply_all": reply_all(sender, to, cc, mine),
            "cleared": cleared,
            "handles": [c["l1_handle"] for c in row.values() if c.get("l1_handle")][:3],
            "item_kind": item_kind(c.get("l1_handle") for c in row.values())})
    return out


# What kind of Exchange item a mail is, as the first byte of its change key carries it (the
# part after "@" in its graph-mail handle; base64). Read over every captured mail of six
# months: 0x0B on 2,427 mails (meeting requests), 0x0C on 1,544 (1,481 titled Accepted,
# Declined or Tentative), 0x0D on 591 (548 titled Canceled), 0x1F on 423 (422 titled
# Automatic reply), 0x17 on 197 (all Undeliverable), 0x09 on the other 243,655.
ITEM_KINDS = {0x09: "message", 0x0B: "meeting_request", 0x0C: "meeting_response",
              0x0D: "meeting_cancel", 0x17: "undeliverable", 0x1F: "auto_reply"}
_CHANGE_KEY = re.compile(r"^graph-mail://[^@\s]+@([A-Za-z0-9+/_-]{4})")


def item_kind(handles: Iterable[Any]) -> str:
    """The mail's Exchange item kind (``ITEM_KINDS``) from its handles' change key; "" when
    no handle carries one or its kind is not one of those (read as an ordinary message)."""
    import base64
    for handle in handles:
        m = _CHANGE_KEY.match(str(handle or ""))
        if not m:
            continue
        try:
            first = base64.b64decode(m.group(1).replace("-", "+").replace("_", "/"))[0]
        except (ValueError, IndexError):
            continue
        return ITEM_KINDS.get(first, "")
    return ""


_ROLE_WORDS = frozenset((
    "info", "sales", "orders", "order", "billing", "admin", "accounts", "office", "purchasing",
    "support", "service", "team", "hello", "contact", "dropship", "customerservice", "noreply",
    "operations", "logistics", "accounting", "finance", "shipping", "receiving", "warehouse"))


def named_in(text: str, addresses: Any) -> bool:
    """The message's own text calls the person by the name their own addresses carry (the
    first word of ``first.last@``, or a whole ``first@`` local part; a role mailbox's local
    part names nobody): "Vince, can you ..." or "@Vince". Capitalised, as a name is written;
    names under three letters are too ambiguous to count."""
    names = set()
    for a in addresses or ():
        first = re.split(r"[._-]", str(a).split("@", 1)[0])[0]
        if first.isalpha() and len(first) >= 3 and first.lower() not in _ROLE_WORDS:
            names.add(first.capitalize())
    return any(re.search(rf"(?<![\w@.]){re.escape(n)}\b(?!@)", text or "") for n in names)


TEAMS_TYPES = ("TeamsChatMessage", "TeamsChannelMessage")
TEAMS_BODY = "row.body_text"


def _teams_events(st: Any, oids: Sequence[str], typed: Mapping[str, tuple[str, str]],
                  people: Any, directory: Mapping[str, str], skipped: Counter,
                  outbound: list | None = None, *, words: bool = True,
                  unaddressed: bool = False) -> list[dict[str, Any]]:
    """Teams chat and channel messages as ``message.received`` events (``words`` False:
    who reached whom, when, in which conversation only; a replay's history seed). A one-to-one chat's
    message is the other member's; a group chat's or a channel's is only each member it
    @mentions (a group message is everyone's to read and no one's to answer: measured, a
    person writes in the chat after three in four of the others' messages, under the gate).
    ``unaddressed`` (the live feed and a replay's days, not a history seed): a group chat's
    other members get the message too, ``to_me`` false and ``unaddressed`` set, for the
    autopilots that answer a person's own turn in a conversation (``after_own_post``); a
    channel's members are not captured, so a channel message stays the mentioned's only.
    A channel's conversation is its thread (the root post and its replies). Every person
    of the chat (a channel: the sender and the mentioned) must be cleared; the sender's own
    message is their side of the conversation (``Queue.note_sent``). The text is the
    message's own (the store's text index; the declared text property otherwise)."""
    from . import proactive_threads as pt
    from .proactive_ops import declared_sources
    members = getattr(people, "chat_members", None) or {}
    text_props = tuple(dict.fromkeys(
        p for ps in declared_sources()["ask_text"].values() for p in ps))
    rows = _read(st, oids, ("created_at", "from", "deleted_at", "mentions", "reply_to_id")
                 + text_props)
    bodies = teams_bodies(st, [h for r in rows.values() for h in _handles_of(r)[:1]]) \
        if words else {}
    # a sender's address (the directory maps each address to its person): the name shown
    # when the message states none
    address_of: dict[str, str] = {}
    persons = set(directory.values())
    for address, person in sorted(directory.items()):
        if "@" in address:
            address_of.setdefault(person, address)
    out = []
    for oid in oids:
        aud = typed[oid][1]
        channel = aud.startswith("teams:channel:")
        if not (channel or aud.startswith("teams:chat:")) or not people.unit_clear(aud):
            skipped["teams_not_chat_or_not_cleared"] += 1
            continue
        row = rows.get(oid) or {}
        sender = teams_sender(_v(row, "from"), directory)
        sender_name = teams_sender_name(_v(row, "from")) or address_of.get(sender, "")
        if _v(row, "deleted_at") or not sender:
            skipped["teams_sender_not_member"] += 1
            continue
        named = set(mentioned(_v(row, "mentions"), directory)) - {sender}
        if channel:
            # a channel's people are not captured: the sender and the people it mentions
            # who are in the directory (never a guess at its members)
            who = sorted({sender, *(x for x in named if x in directory.values())})
            group, conversation = True, teams_thread(aud, oid, _v(row, "reply_to_id"))
        else:
            who = chat_people(aud[len("teams:chat:"):], members, directory)
            if len(who) < 2 or sender not in who:
                skipped["teams_sender_not_member"] += 1
                continue
            group, conversation = len(who) > 2, aud
        # who reads a reply besides directory people: a chat's members the directory does
        # not hold (a guest's #EXT# UPN or outside address, an unknown id); a channel's
        # members are not captured, so unknown (None: ``senders_own`` holds)
        outside = None if channel else sorted(x for x in who if x not in persons)
        if outbound is not None and _v(row, "created_at"):
            # the sender's own message: their side of the chat (a group's: the thread only)
            outbound.append({"owner": sender, "conversation": conversation, "message_id": oid,
                             "to": [] if group else [x for x in who if x != sender],
                             "at": _v(row, "created_at")})
        owners = sorted(named & set(who)) if group else [x for x in who if x != sender]
        cleared = all(people.identity_clear(x) for x in who)
        # a chat holding someone the exclusion bars: never an event for a member no one
        # addressed (an addressed one is kept and skipped as not cleared, as before)
        others = sorted(set(who) - set(owners) - {sender}) \
            if group and not channel and unaddressed and cleared else []
        if not owners and not others:
            skipped["teams_group_not_mentioned"] += 1
            continue
        handle = (_handles_of(row) or [""])[0]
        text = pt._own(bodies.get(handle) or next(
            (_v(row, p) for p in text_props if _v(row, p)), ""))
        records = sorted({o for _t, o in _linked(st, oid) if o.split(":", 1)[0] in BUSINESS}
                         ) if words else []
        from . import inbox_triage as it
        for owner in owners + others:
            out.append({
                "event_id": _digest("message.received", oid, owner) if group
                else _digest("message.received", oid),
                "event_type": "message.received", "object_id": oid,
                "object_type": typed[oid][0], "channel": "teams",
                "at": _v(row, "created_at"), "mailbox": owner, "audience": aud,
                "sender": sender, **({"sender_name": sender_name} if sender_name else {}),
                "to": [owner], "cc": [], "to_me": owner in owners,
                **({} if owner in owners else {"unaddressed": True}), "subject": "",
                "text": text[:TEXT_CHARS], "conversation_id": conversation, "message_id": oid,
                "intents": intents_of("", text), "side": "internal", "outside": outside,
                "records": records[:MAX_RECORDS], "group": group, "cleared": cleared,
                # a message whose text is not read yet (the store's text index
                # lags its records): its ask is unknown, so it is kept; the cycle that
                # reads the text later closes its item when it asks nothing
                # (``autopilot.name_teams_items``)
                "asks": chat_ask(text, mentioned=owner in named) if text.strip() else True,
                # the model reads whether it asks (``autopilot._judge_asks``)
                **({"ask_check": "model"} if text.strip() and
                   chat_ask(text, mentioned=owner in named) else {}),
                "mentioned": owner in named, "mentions": sorted(named & set(who)),
                "closing": closing(text, chat=True), "handles": _handles_of(row)[:3]})
    return out


def _mail_ask(it: Any, text: str, subject: str, *, to_me: bool) -> dict[str, Any]:
    """A mail's ``asks``: True when the phrase rules find an ask (``inbox_triage``); else
    False, and for a mail sent to the reader (in To) whose text the model may read (two
    words or more, no notice that no reply is wanted) ``ask_check`` "model": the chat ask
    model reads it (``autopilot._ask_wanted``) and its "yes" sets it True. With no model
    configured it stays False (the phrase rules' answer)."""
    from .chat_ask_model import certain_no
    if it.reply_ask(text) or it.direct_question(text):
        return {"asks": True}
    if to_me and not certain_no(text, mentioned=True) and \
            not it.NO_REPLY.search(text) and not it.NOTICE.search(text):
        return {"asks": False, "ask_check": "model"}
    return {"asks": False}


def reply_all(sender: str, to: Sequence[str], cc: Sequence[str], mine: Iterable[str]
              ) -> dict[str, list[str]]:
    """Who else a reply to this mail goes to, as people answer (``assist.reply_base``): the
    base ``assist.resolve_reply`` decides each owner's recipients from (``_recipients``),
    capped there at ``assist.REPLY_ALL_MAX`` a line (the event's own To and Cc bound it)."""
    from . import assist
    return assist.reply_base(sender, to, cc, mine)


def _handles_of(row: Mapping[str, Any]) -> list[str]:
    return list(dict.fromkeys(c["l1_handle"] for c in row.values()
                              if isinstance(c, Mapping) and c.get("l1_handle")))


def teams_thread(audience: str, oid: str, reply_to: str) -> str:
    """A channel message's conversation: its thread (the root post's id; a root post is
    its own), within the channel."""
    if reply_to:
        return f"{audience}#{reply_to}"
    found = re.search(r'"id"\s*:\s*"([^"]+)"', oid)
    return f"{audience}#{found.group(1) if found else oid}"


def teams_bodies(st: Any, handles: Sequence[str]) -> dict[str, str]:
    """handle -> the message's body text held by the store's text index (one read for a
    batch); a handle without one, or an unreadable index, is left out (never a guess)."""
    from .mail_text import joined_chunks
    rows: dict[str, list[tuple[int, bytes]]] = {}
    handles = sorted(set(h for h in handles if h))
    try:
        for start in range(0, len(handles), 500):
            for handle, at, body in st.conn.execute(
                    "SELECT f.citation, k.start, k.body FROM text_fields f JOIN text_chunks k "
                    "ON k.field_id = f.field_id WHERE f.citation IN (SELECT value FROM "
                    "json_each(?)) AND f.field_path = ? ORDER BY f.citation, k.start",
                    (json.dumps(handles[start:start + 500]), TEAMS_BODY)):
                rows.setdefault(handle, []).append((at, body))
        # chunks overlap (``text_index.CHUNK_OVERLAP``): each one laid at its offset
        out = {handle: joined_chunks(parts) for handle, parts in rows.items()}
    except (sqlite3.Error, ValueError, TypeError):
        return {}
    return out


# A note that closes its own loop: it opens by acknowledging ("Noted, thanks!", "Perfect, thank
# you"), or the writer states their own next step ("I'll circle back next week", "we will wait
# for it", "I will send an invite", "the team is on it"). Measured on dev days 2026-10-06..25:
# such notes in a thread the reader wrote in were answered about one time in eight.
CLOSING_ACK = re.compile(
    r"^\W*(?:(?:hi|hello|hey|dear|good\s+(?:morning|afternoon|evening))\b[^,.!\n]{0,30}"
    r"[,.!\n]\s*)?(?:ok(?:ay)?|great|perfect|awesome|excellent|wonderful|sounds\s+good|got\s+it|"
    r"noted|received|will\s+do|thanks?|thank\s+you|many\s+thanks|much\s+appreciated|"
    r"appreciate\s+it|understood|confirmed|no\s+problem)\b"
    r"(?!\s+(?:connecting|meeting|talking|speaking|chatting))", re.I)
CLOSING_STEP = re.compile(
    r"\b(?:i|we|team|they)(?:'ll|’ll|\s+will|\s+shall)\s+(?:\w+\s+){0,2}(?:review|follow\s+up|"
    r"get\s+back|circle\s+back|send\s+(?:you\s+)?(?:an?\s+)?invite|wait|keep\s+you\s+posted|"
    r"update\s+you|be\s+in\s+touch|reach\s+out)\b|\b(?:is|are)\s+on\s+it\b", re.I)
CLOSING_WORDS = 30
# A signature's start the shared ``SIGNOFF`` does not read ("Kindest regards", a phone's line).
CLOSING_SIGNATURE = re.compile(r"\b(?:kindest|warmest|best)\s+(?:regards|wishes)\b|"
                               r"\bsent\s+from\s+my\b", re.I)


# A chat message's casual lead-in before its acknowledgement ("lol okay thanks", "Sure will
# do!", "yep will do"), and a bare apology ("Sorry bout that"). Measured on dev Teams items
# (Vince, Stephen, purchasing, customer service; 2026-10-26..11-05): such notes were answered
# 2 times in 6 against about 9 in 10 for the rest. Chat only: mail was not measured.
CHAT_LEAD = r"(?:(?:sure|yes|yep|yeah|yup|lol|haha|ah|oh|ok(?:ay)?|k)\b[,!.]?\s*)"
CLOSING_ACK_CHAT = re.compile(CLOSING_ACK.pattern.replace(
    r"[,.!\n]\s*)?(?:ok(?:ay)?|", r"[,.!\n]\s*)?" + CHAT_LEAD + r"?(?:ok(?:ay)?|", 1), re.I)
CLOSING_SORRY_CHAT = re.compile(r"^\W*" + CHAT_LEAD + r"?(?:sorry|my\s+bad|apologies)\b"
                                r"(?:\s+(?:a)?bout\s+that)?\W*$", re.I)


def closing(text: Any, *, chat: bool = False) -> bool:
    """Whether a message's own text closes its loop (``CLOSING_ACK`` / ``CLOSING_STEP``): a
    short note that asks nothing (``reply_ask``, ``direct_question``) and carries no question
    mark before its sign-off. A reply to it is not owed. ``chat``: a Teams message, whose
    acknowledgement may follow a casual lead-in or be a bare apology (``CLOSING_ACK_CHAT``)."""
    from . import inbox_triage as it
    own = it.URL.sub(" ", it.FIRST_CONTACT.sub(" ", text if isinstance(text, str) else ""))
    for found in it.SIGNOFF.finditer(own):
        if re.search(r"[A-Za-z]{2,}", own[:found.start()]):
            own = own[:found.start() + 1]   # keep a "?" that ends the sentence before it
            break
    cut = CLOSING_SIGNATURE.search(own)
    own = own[:cut.start()] if cut else own
    if not own.strip() or "?" in own or it.reply_ask(own) or it.direct_question(own):
        return False
    if len(re.findall(r"[A-Za-z]{2,}", own)) > CLOSING_WORDS:
        return False
    if chat and (CLOSING_ACK_CHAT.search(own) or CLOSING_SORRY_CHAT.match(own)):
        return True
    return bool(CLOSING_ACK.search(own) or CLOSING_STEP.search(own))


def chat_ask(text: Any, *, mentioned: bool = False) -> bool:
    """Whether a chat message may ask its reader something (a Teams message's ``asks``):
    False only when it certainly asks nothing (``chat_ask_model.certain_no``: no words, a
    note to the whole chat that does not @mention the reader). Whether the rest ask is read
    by a small model (``chat_ask_model.Judge``, the event's ``ask_check``), never a phrase
    list: owner walk 2026-11-17, a hand-written list dropped real asks without a "?"."""
    from .chat_ask_model import certain_no
    return not certain_no(text, mentioned=mentioned)


GREETING = re.compile(r"^\W*(?:hi|hello|hey|dear|good\s+(?:morning|afternoon|evening)|morning|"
                      r"afternoon)\s*,?\s+([A-Za-z][\w'-]*)(?:\s*(?:,|and|&)\s*"
                      r"([A-Za-z][\w'-]*))?", re.I)


REPLY_PREFIX = re.compile(r"^\s*(?:re|fw|fwd|aw|tr|sv|wg|accepted|declined|tentative|"
                          r"cancel+ed|automatic reply|out of office)\s*:", re.I)
_SERIES_DROP = re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|mon|tue|wed|"
                          r"thu|fri|sat|sun)[a-z]*\b|\d+")


def routine_series(subject: str, sender: str, recipients: Iterable[str], own: Iterable[str],
                   domains: Sequence[str]) -> str | None:
    """The series key of a mail its sender started to colleagues: the subject without
    dates, numbers and punctuation (``Weekly report 2026-10-19`` and ``Weekly report Oct 26``
    are one series), as a digest. None for a reply or forward, a calendar answer, a mail with an outside
    or no colleague recipient, or a subject of fewer than two words."""
    to = [a.lower() for a in recipients if a]
    mine = {a.lower() for a in own} | {sender.lower()}
    if not subject or REPLY_PREFIX.match(subject) or not to or \
            not all(_internal(a, domains) for a in to) or not set(to) - mine:
        return None
    key = " ".join(re.sub(r"[^a-z]+", " ", _SERIES_DROP.sub(" ", subject.lower())).split())
    return _digest("series", key) if len(re.findall(r"[a-z]{3,}", key)) >= 2 else None


def routine_note(subject: str, preview: str, sender: str, to: Sequence[str],
                 own: Iterable[str], domains: Sequence[str], oid: str) -> dict[str, Any]:
    """The ``Queue.note_routine`` fields of one sent mail (empty: not a series mail)."""
    series = routine_series(subject, sender, to, own, domains)
    if series is None:
        return {}
    return {"series": series, "message": oid,
            "recipients": [a for a in to if a.lower() not in {x.lower() for x in own}],
            "greeted": bool(GREETING.match(preview or ""))}


def _first_names(addresses: Iterable[str]) -> set[str]:
    """The first name an address's local part starts with (``jane.doe@`` -> ``jane``)."""
    out = set()
    for address in addresses:
        first = re.split(r"[._+\-0-9]", str(address).split("@", 1)[0].lower())[0]
        if len(first) >= 2:
            out.add(first)
    return out


def addressed_other(text: str, mine: Iterable[str], recipients: Iterable[str]) -> bool:
    """Whether a message opens by greeting another of its recipients, not the mailbox's
    owner ("Hi Sam," on a mail to Sam with the owner copied in): the note is Sam's to answer.
    A name is one of the owner's (or another recipient's) when either starts with the other
    ("Hi V", "Hi Chris" to christopher@); a greeting naming neither, or no greeting: no."""
    found = GREETING.match(text or "")
    if not found:
        return False
    named = {g.lower() for g in found.groups() if g}
    own = _first_names(mine)
    others = _first_names(r for r in recipients if str(r).lower() not in
                          {str(m).lower() for m in mine}) - own

    def one_of(name: str, pool: set[str]) -> bool:
        return any(name == p or (len(name) >= 3 and (p.startswith(name) or name.startswith(p)))
                   for p in pool)
    if not own or any(one_of(n, own) for n in named):
        return False
    return any(one_of(n, others) for n in named)


ONE_TO_ONE = re.compile(r"^19:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})_"
                        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
                        r"@unq\.gbl\.spaces$", re.I)


def chat_people(chat: str, members: Mapping[str, Sequence[str]],
                directory: Mapping[str, str]) -> list[str]:
    """The people of a Teams chat, one id each. A one-to-one chat's id names its two
    members' object ids (``19:<id>_<id>@unq.gbl.spaces``): those, and only when the
    capture recorded the chat's members (else no one: never a guess). A group chat's
    captured members are object ids and addresses of the same people, each read through
    the directory so a person counts once (before, a one-to-one chat's two ids and two
    addresses read as a four-person group, and no one-to-one message reached anyone)."""
    if chat not in members:
        return []
    pair = ONE_TO_ONE.match(chat)
    if pair:
        return sorted({pair.group(1).lower(), pair.group(2).lower()})
    return sorted({directory.get(x, x) for x in (str(m).strip().lower()
                                                   for m in members[chat] or ()) if x})


def mentioned(raw: str, directory: Mapping[str, str]) -> list[str]:
    """The people a chat message @mentions (the capture's ``[{"id", "target_id", ...}]``),
    each read through the directory; unreadable: none (never a guess)."""
    try:
        value = json.loads(raw) if raw else []
    except ValueError:
        return []
    out = []
    for m in value if isinstance(value, list) else ():
        if not isinstance(m, Mapping):
            continue
        for key in ("target_id", "id"):
            x = str(m.get(key) or "").strip().lower()
            if x:
                out.append(directory.get(x, x))
    return out


def teams_sender(raw: str, directory: Mapping[str, str]) -> str:
    """A chat message's sender id: the capture stores ``{"display_name", "id"}``; a bare
    id or address is read through the directory. Unreadable: "" (never a guess)."""
    text = str(raw or "").strip()
    if text.startswith("{"):
        try:
            value = json.loads(text)
        except ValueError:
            return ""
        text = str(value.get("id") or "") if isinstance(value, Mapping) else ""
    text = text.lower()
    return directory.get(text, text)


def teams_sender_name(raw: str) -> str:
    """A chat message's sender as the message names them (the capture's
    ``from.display_name``); "" when it states none or is unreadable (never a guess)."""
    from .autopilot_engine import person_name
    text = str(raw or "").strip()
    if not text.startswith("{"):
        return ""
    try:
        value = json.loads(text)
    except ValueError:
        return ""
    return person_name(value.get("display_name")) if isinstance(value, Mapping) else ""


PARTY_ADDRESSES = {"Customer": ("customer", ("email", "email_2", "email_3", "bc_email")),
                   "Supplier": ("supplier", ("rep_email", "csr_email", "bc_email"))}
PUBLIC_MAIL = re.compile(r"@(?:gmail|googlemail|yahoo|ymail|hotmail|outlook|live|msn|aol|"
                         r"icloud|me|mac|proton(?:mail)?|gmx|mail|yandex|zoho|shaw|rogers|"
                         r"telus|sympatico|bell|videotron|comcast|verizon|att)\.", re.I)


def _party_addresses(st: Any) -> dict[str, set[str]]:
    """address or domain -> the sides (customer, supplier) whose records state it: a public
    mail domain counts by its whole address only. Read once per run."""
    def make() -> dict[str, set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        for otype, (side, props) in PARTY_ADDRESSES.items():
            ids = [r[0] for r in st.conn.execute(
                "SELECT object_id FROM objects WHERE object_type = ?", (otype,))]
            for start in range(0, len(ids), 5000):
                for oid, row in _read(st, ids[start:start + 5000], props).items():
                    for p in props:
                        for address in _addresses(_v(row, p)):
                            out[address].add((side, oid))
                            if not PUBLIC_MAIL.search(address):
                                out["@" + address.split("@", 1)[1]].add((side, oid))
        return dict(out)
    return st.memo("autopilot:party_addresses", make) if hasattr(st, "memo") else make()


def _party_side(st: Any, sender: str, domains: Sequence[str]) -> str:
    """The sender's side by the customer and supplier records stating its address (else
    its domain); both or neither: unknown."""
    sides = {side for side, _o in _parties(st, sender)}
    return next(iter(sides)) if len(sides) == 1 else "unknown"


def _parties(st: Any, sender: str) -> set[tuple[str, str]]:
    """(side, party record) of the records stating the sender's address, else its domain."""
    found = _party_addresses(st)
    return set(found.get(sender) or (set() if PUBLIC_MAIL.search(sender) else
                                     found.get("@" + sender.split("@", 1)[-1])) or ())


# An event field is cut to 300 characters, except these: an MC gate's own scope is parsed
# as JSON (cut, it would not parse and the gate could never be routine), and the proposal
# text is scanned whole by the routine rules' deny terms (operating.autopilot_write).
FIELD_LIMITS = {"gate_scope": 4000, "proposal_action": 2000, "proposal_summary": 2000}

RECORD_PROPS = {
    "MCTask": ("task_id", "title", "description", "stage", "assignee", "accountable_owner",
               "updated_at", "created_at"),
    "PlannerTask": ("title", "description", "assignees", "completed_at", "due_at",
                    "percent_complete", "created_at", "plan_title"),
    "MCApproval": ("gate_id", "task_id", "status", "reason", "requested_by", "requested_at",
                   "decided_by", "decided_at", "gate_scope", "proposal_action",
                   "proposal_summary", "proposal_risk"),
    "MCTaskEvent": ("task_id", "kind", "at"),
    "Batch": ("batch_number", "lot_number", "status", "quality_status", "tested_by",
              "checked_by", "released_at", "order_list", "po_number", "qc_ph",
              "qc_viscosity", "qc_appearance", "qc_odour", "qc_solids"),
    "Meeting": ("mailbox", "subject", "organizer", "attendees", "start", "end",
                "is_cancelled"),
}


def _record_events(st: Any, otype: str, kind: str, oids: Sequence[str],
                   typed: Mapping[str, tuple[str, str]], spec: Mapping[str, Any],
                   skipped: Counter, now: datetime | None = None,
                   lag: timedelta | None = None) -> list[dict[str, Any]]:
    props = RECORD_PROPS.get(otype, (spec.get("time"), "status", "order_number", "po_number",
                                     "invoice_number", "shipment_number", "receipt_number"))
    rows = _read(st, oids, [p for p in props if p])
    cycle_at = now
    # whether a meeting has ended is judged at the cycle's time (a replay's day), never
    # the wall clock
    now = cycle_at or datetime.now(timezone.utc)
    out = []
    for oid in oids:
        row = rows.get(oid) or {}
        if otype == "MCTaskEvent" and _v(row, "kind") != "question.asked":
            continue                   # only a question asked of a person is an event here
        # when the feed first held the record: the cycle's time, or (a replay) the record's
        # own time plus the feed's lag when that is earlier
        seen = cycle_at
        happened = _instant(_v(row, spec.get("time") or ""))
        held = _first_held(st, otype, oid, happened) \
            if cycle_at is not None and lag is not None else None
        if held is not None:
            # the live feed's own first read of it (the MC capture's version time): its
            # cadence that day, a fast poll's minutes or a stalled feed's hours alike
            seen = held
        elif cycle_at is not None and lag is not None and happened is not None:
            seen = min(cycle_at, happened + lag)
        event = {"event_type": kind, "object_id": oid, "object_type": otype,
                 "audience": typed[oid][1], "at": _v(row, spec.get("time") or ""),
                 "status": _v(row, "status") or _v(row, "stage"),
                 "handles": [c["l1_handle"] for c in row.values() if c.get("l1_handle")][:3],
                 "fields": {p: _v(row, p)[:FIELD_LIMITS.get(p, 300)]
                            for p in props if p and _v(row, p)}}
        if otype in ("MCTask", "PlannerTask"):
            assignees = ([_v(row, "assignee")] if otype == "MCTask"
                         else [a for a in _json_list(_v(row, "assignees"))])
            event["assignees"] = [a.lower() for a in assignees if a]
            event["assigned"] = bool(event["assignees"])
            event["open"] = not (TASK_DONE.match(_v(row, "stage") or "") or
                                 _v(row, "completed_at"))
        elif otype == "MCApproval":
            # a decision recorded after the feed saw the request was not taken yet then (a
            # replay of past days reads today's record): the request was pending then
            decided = _instant(_v(row, "decided_at"))
            if seen is not None and decided is not None and decided > seen:
                event["status"] = "pending"
                event["fields"] = {k: v for k, v in event["fields"].items()
                                   if k not in ("status", "decided_by", "decided_at")}
        elif otype == "Batch":
            event["quality"] = _v(row, "quality_status")
            event["tested"] = bool(_v(row, "tested_by") or _v(row, "checked_by") or any(
                _v(row, p) for p in props if p.startswith("qc_"))) or \
                event["quality"] == "Rejected"
        elif otype == "Meeting":
            end = _instant(_v(row, "end"))
            event["at"] = _v(row, "end")
            event["ended"] = bool(end and end <= now and _v(row, "is_cancelled").lower()
                                  not in ("true", "1"))
            organizer = _addresses(_v(row, "organizer"))
            event["organizer"] = organizer[0] if organizer else ""
            event["mailbox"] = _v(row, "mailbox").lower()
            event["attendees"] = _addresses(_v(row, "attendees"))
            parties = [o for t, o in _linked(st, oid) if t in ("Meeting.with_customer",
                                                               "Meeting.with_supplier")]
            event["parties"] = sorted(parties)[:MAX_RECORDS]
        elif otype == "MCTaskEvent":
            if not _question_event(st, oid, event, seen, skipped):
                continue
        if seen is not None and lag is not None:
            event["seen_at"] = _iso(seen)
            event["seen_from"] = "capture" if held is not None else "feed_lag"
        event["event_id"] = _digest(kind, oid, _version_digest(st, oid))
        if otype == "Meeting" and not event["ended"]:
            # a meeting read before its end is another event than the one after it: the
            # earlier one, consumed unmatched, never swallows the follow-up (an ended
            # meeting keeps the id it always had)
            event["event_id"] = _digest(kind, oid, _version_digest(st, oid), "not_ended")
        out.append(event)
    return out


def _first_held(st: Any, otype: str, oid: str, happened: datetime | None) -> datetime | None:
    """A replay: when the live MC feed first held an MC record (``MCQuestions.first_held``),
    else None (no capture reader, a backfilled record or no version time)."""
    reader = getattr(st, "autopilot_mc", None)
    if reader is None or happened is None or otype not in getattr(reader, "CAPTURED_AS", {}):
        return None
    got = st.conn.execute("SELECT native_key FROM objects WHERE object_id = ?",
                          (oid,)).fetchone()
    return reader.first_held(otype, got[0], happened) if got else None


def _question_event(st: Any, oid: str, event: dict[str, Any], seen: datetime | None,
                    skipped: Counter) -> bool:
    """Fill a ``question.asked`` event from the MC capture (``autopilot_work.MCQuestions``):
    the addressee, the question as asked and whether it was still open when the feed saw
    it. No reader, no question or no addressee: no event (fail closed)."""
    reader = getattr(st, "autopilot_mc", None)
    got = st.conn.execute("SELECT native_key FROM objects WHERE object_id = ?",
                          (oid,)).fetchone()
    key = reader.key_of(got[0]) if reader is not None and got else None
    q = reader.asked(*key) if key else None
    if q is None or not q["addressee"]:
        skipped["question_unreadable"] += 1
        return False
    closed = [t for t in (_instant(q["answered_at"]), _instant(q["withdrawn_at"])) if t]
    task = next((o for t, o in _linked(st, oid, ("MCTaskEvent.event_of",))), None)
    event["event_type"] = "question.asked"
    event["addressee"] = q["addressee"]
    # who usually answers this addressee's questions (MC handles), as of when it was asked:
    # the item is prepared for them too (Vince answered 54 of his 102 answers of a week
    # to questions addressed to others)
    answerers = getattr(reader, "answerers", None)
    event["answerers"] = answerers(q["addressee"], q["asked_at"] or str(
        event.get("at") or "")) if callable(answerers) else []
    event["addressee_domains"] = sorted(reader.home_domains())
    event["open"] = not closed or (seen is not None and min(closed) > seen)
    event["status"] = "open" if event["open"] else "closed"
    event["question_key"] = f"mcq:{key[0]}:{q['question_id']}"
    event["task"] = task
    event["text"] = q["text"]
    event["question"] = {k: q[k] for k in ("question_id", "task_id", "text", "context",
                                           "options", "recommended", "reason", "asked_at")}
    event["fields"] = {"task_id": q["task_id"]}
    return True


def handle_person(handle: str, directory: Mapping[str, str],
                  domains: Iterable[str] | None = None) -> str | None:
    """The person a handle names: an address the directory holds, or a bare name that is
    the local part of addresses of exactly one person in ``domains`` (the domains the
    source itself writes full addresses in; none given: every domain). Conservative: two
    people sharing the name there, or none, is no person."""
    handle = str(handle or "").strip().lower()
    if not handle:
        return None
    if "@" in handle:
        return directory.get(handle)
    if not re.fullmatch(r"[a-z][a-z0-9._-]{1,40}", handle):
        return None
    home = {str(d).lower() for d in domains} if domains is not None else None
    found = {oid for address, oid in directory.items()
             if "@" in address and address.split("@", 1)[0] == handle
             and (home is None or address.rsplit("@", 1)[1] in home)}
    return next(iter(found)) if len(found) == 1 else None


def _json_list(raw: str) -> list[str]:
    try:
        value = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []
