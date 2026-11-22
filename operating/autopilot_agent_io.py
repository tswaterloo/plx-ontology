"""What the assistant (``operating.autopilot_agent``) is given at a moment and what of its
decision may stand: one place, read by the live cycle (``operating.autopilot._model_agent``)
and by a replay of past moments, so a replay measures the product's own path.

* ``agent_input``: the event as the model reads it (``autopilot_jobs.described``), the
  person's habit with this sender (``history_line``), the invitation on their calendar a
  message is about (``invitation_lines``, read under the handler's ``ctx`` into the
  material), and for a conversation with an outside reader only that party's own records'
  lines (``autopilot_outbound``: the session then gets no read tool).
* ``agent_output``: the outbound check on the reply, the options and every proposed message
  (``autopilot_outbound.held``: a held reply becomes a question card saying why, a held
  message is dropped and named), then the drafter's own reply checks (a figure in no
  material, a hollow thanks, a copy tic).
* ``look_events``: the time-driven moments (the day's start and each working hour) of the
  people who switched the assistant's look on: one event per person per hour.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any, Callable, Mapping, Optional, Sequence

from . import autopilot_outbound as ob

LOOK_ITEMS = 2            # what one look may raise, changes and to-dos together
LOOK_EVENT = "assistant.look"
# the working hours a look runs in (US Eastern, Monday to Friday): the first is the day's
# start (what is due today), the others the hourly look
LOOK_HOURS = range(7, 18)
INVITES = 2


def _digest(*parts: Any) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:32]


def history_line(event: Mapping[str, Any]) -> str:
    """The person's own habit with this sender and in this conversation (the queue's
    ``reply_history`` / ``own_posts`` the cycle puts on a message: known before it, never
    later), as a line the model reads; "" when nothing is known."""
    parts = []
    hist = event.get("sender_history")
    if isinstance(hist, (list, tuple)) and len(hist) == 2 and int(hist[0] or 0) > 0:
        parts.append(f"the person answered {int(hist[1] or 0)} of the last {int(hist[0])} "
                     "messages this sender sent them, each within a day")
    posts = event.get("own_posts")
    if isinstance(posts, (list, tuple)) and len(posts) == 2 and int(posts[1] or 0) > 0:
        parts.append(f"they wrote in this conversation {int(posts[1])} time(s) recently")
    elif event.get("engaged"):
        parts.append("they have written in this conversation before")
    return ("History: " + "; ".join(parts) + ".") if parts else ""


HABIT_MIN = 3        # a sender's messages to the person known before their habit counts
HABIT_SHARE = 0.5    # ... and the share of them the person answered within a day
# a mail's Exchange kind (``autopilot_events.item_kind``) that never wants a reply: an answer
# to an invitation, a cancellation, an automatic reply, a bounce
NO_REPLY_KINDS = frozenset({"meeting_response", "meeting_cancel", "auto_reply", "undeliverable"})


def reply_due(event: Mapping[str, Any]) -> str:
    """Whether a reply from the person is due on this message, from what is computed on it
    (its addressing, the ask rules' reading and the person's own habit; never a model's
    judgement): "asked" when it is put to them (in To, named, @mentioned) and the ask rules
    read an ask in it; "turn" when it asks nothing certain but they are in this exchange
    (they wrote in the conversation recently, or usually answer this sender there), or it is
    put to them and they usually answer this sender (``HABIT_SHARE`` of ``HABIT_MIN`` or
    more), or it is put to them and its ask is undecided by the rules; "" otherwise (copied,
    a group note for others, greeting someone else, a notice): no reply or question there.
    The mail's Exchange kind (``item_kind``) comes first: an invitation put to them is
    "asked"; an invitation's answer, a cancellation, an automatic reply or a bounce is "".
    A message on the person's replies to write (``owed``: the drafts path) is their "turn".
    A field missing or unreadable counts as not shown (fail closed: no reply due)."""
    def pair(key: str) -> tuple[int, int]:
        v = event.get(key)
        try:
            return (int(v[0] or 0), int(v[1] or 0)) if isinstance(v, (list, tuple)) \
                and len(v) == 2 else (0, 0)
        except (TypeError, ValueError):
            return 0, 0
    direct = any(event.get(k) is True for k in ("to_me", "named_me", "mentioned"))
    kind = event.get("item_kind")
    if kind in NO_REPLY_KINDS:
        return ""
    if event.get("owed") is True:
        # a line on the person's replies to write (``reply_drafts.line_draft``): the line
        # itself says their reply is due (dev 11-22: 2 of 10 cards on copied mail the person
        # answered were dropped here)
        return "turn"
    if direct and kind == "meeting_request":
        return "asked"     # an invitation put to them asks for their answer by what it is
    exchange = in_exchange(event)
    if event.get("addressed_other") is True and not exchange:
        return ""
    if direct and event.get("asks") is True:
        return "asked"
    n, answered = pair("sender_history")
    habit = n >= HABIT_MIN and answered >= HABIT_SHARE * n
    if exchange or direct and (habit or event.get("ask_check") == "model"):
        return "turn"
    return ""


WARN_FIGURE = ("Check before sending: a date or figure in this reply is not in the message or "
               "the records it read.")


def gated(got: Any, event: Mapping[str, Any]) -> Any:
    """``got`` (an assistant decision on a message) with only what ``reply_due`` allows: no
    reply due -> no reply, question or to-do (a change the message's records call for still
    stands). Where one is due (asked or a turn), the person's open question stays: measured
    on set E, cards on mail put to them that the ask rules read no ask in were 7 of the 14
    right items an "open question only when asked" rule removed."""
    due = reply_due(event)
    if not due:
        got.reply, got.ask, got.todos = "", None, []
    # only their turn: a written reply stands, and so does their call with the answers the
    # check kept (two or more, each judged against the message): the person picks one
    return got


def in_exchange(event: Mapping[str, Any]) -> bool:
    """Whether the person is in a group conversation's exchange a message not addressed to
    them belongs to: they wrote there in the queue's recent days (``own_posts``), or they are
    the member who answers this sender there (``answerer``)."""
    def count(key: str) -> int:
        v = event.get(key)
        try:
            return int(v[1] or 0) if isinstance(v, (list, tuple)) and len(v) == 2 else 0
        except (TypeError, ValueError):
            return 0
    return count("own_posts") > 0 or count("answerer") > 0


def agent_input(event: Mapping[str, Any], text: str, material: Mapping[str, Any],
                addresses_of: Mapping[str, Any]) -> tuple[str, bool]:
    """(the event as the model reads it, whether the session may read the records): every
    reader of a reply is the company's own (``autopilot.ours_only``) -> reads; else the
    outside party's own records' lines only."""
    from .autopilot import ours_only
    from .autopilot_jobs import described
    if event.get("event_type") == LOOK_EVENT:
        return "", True
    linked = [str(h) for h in material.get("linked") or () if isinstance(h, str)]
    account = [str(a) for a in material.get("account") or ()]
    inside = ours_only(event, addresses_of)
    out = described(event, text, linked)
    extra = [x for x in (history_line(event), kind_line(event)) if x] + \
        [str(x) for x in material.get("invites") or ()]
    if extra:
        out += "\n" + "\n".join(extra)
    if not inside:
        out += "\nTheir own records (all you may use about them):\n" + (
            "\n".join(f"- {a}" for a in account) if account else "- none found")
    return out, inside


def kind_line(event: Mapping[str, Any]) -> str:
    """What the mail is by its Exchange kind (``item_kind``), for the session and the check
    alike: an invitation's answer is the person's own choice between accepting and declining
    (an invitation mail often carries no words of its own)."""
    if event.get("item_kind") == "meeting_request":
        return ("Exchange marks this mail as a meeting invitation to them: what it asks of "
                "them is their answer, accept, tentatively accept or decline.")
    return ""


def reply_readers(event: Mapping[str, Any], mine: Sequence[str]) -> list[str]:
    """Who reads a reply to ``event`` besides its person: a mail's sender and everyone it
    went to or copied; a Teams chat's outside members, else the chat itself (members
    unknown: ``autopilot_outbound`` checks it as an outside reader)."""
    if event.get("channel") == "teams":
        return [str(a) for a in event.get("outside") or ()] or [
            f"chat:{event.get('conversation_id') or event.get('chat_id') or ''}"]
    others = event.get("reply_all") if isinstance(event.get("reply_all"), Mapping) else {}
    own = {str(a).lower() for a in mine}
    found = [event.get("sender"), *(event.get("to") or ()), *(event.get("cc") or ()),
             *(others.get("to") or ()), *(others.get("cc") or ())]
    return [str(a) for a in dict.fromkeys(found) if isinstance(a, str) and a
            and a.lower() not in own]


def held_note(summary: str, back: Sequence[Mapping[str, Any]]) -> str:
    """The item's line with what was held back (the person writes those themselves)."""
    if not back:
        return summary
    notes = [f"a message to {', '.join(b['to'])[:120]} was held back: "
             + ob.reason_text(b["why"], "the recipient") for b in back[:3]]
    return (summary + " (" if summary else "(") + "; ".join(notes) + ")"


def outbound_proposals(proposals: Sequence[Mapping[str, Any]], event: Mapping[str, Any],
                       addresses_of: Mapping[str, Any], owner: str, text: str,
                       account: Sequence[str], checks: Any
                       ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(the proposals that may stand, the messages held back: {to, why}). A proposed mail
    or Teams message whose readers include anyone outside the company is checked
    (``autopilot_outbound.held``) against what those readers already have: the event's
    message when every one of them read it, and of its party's lines those naming only
    each reader's own records; else nothing."""
    mine = sorted(addresses_of.get(owner) or ())
    seen = {a.lower() for a in reply_readers(event, mine) if "@" in a}
    kept: list[dict[str, Any]] = []
    back: list[dict[str, Any]] = []
    ctx = None
    for p in proposals:
        if not ob.is_message(p):
            kept.append(dict(p))
            continue
        to = ob.recipients(p)
        far = [a for a in to if a.startswith("chat:") or "@" in a]
        if ctx is None and checks is not None and far:
            ctx = checks()
        their = {a.lower() for a in to if "@" in a}
        read = bool(their) and their <= seen
        allowed = "\n".join([str(event.get("subject") or ""), text]) if read else ""
        why = ob.held(ctx, ob.body_of(p), to, allowed, account if read else ()) if far else ""
        if why:
            back.append({"to": to, "why": why})
        else:
            kept.append(dict(p))
    return kept, back


def agent_output(got: Any, event: Mapping[str, Any], owner: str, text: str,
                 material: Mapping[str, Any], addresses_of: Mapping[str, Any], checks: Any,
                 person: str, *, review: bool = False
                 ) -> tuple[Any, list[dict[str, Any]], str]:
    """(the decision as it may stand, the messages held back, why nothing stands when it is
    empty). ``checks``: builds the store's ``Ctx`` for the outbound check (None: an outside
    reader's text is held, fail closed). ``review``: a draft the person reads before anything
    goes (Assist): a reply the outbound, figure or support check flags is KEPT with
    ``warning`` saying why; otherwise (Autopilot) it is held as the open question."""
    from . import autopilot_drafter as ad
    from .autopilot import ours_only
    from .autopilot_engine import sender_label
    account = [str(a) for a in material.get("account") or ()]
    teams = event.get("channel") == "teams"
    sender = sender_label(event.get("sender"), event.get("sender_name"))
    m = ad.Material(reader="", channel="teams" if teams else "mail", sender=sender,
                    subject=str(event.get("subject") or ""), text=text[:ad.MESSAGE_CHARS],
                    address=person, relation=ad.relation_of(event),
                    account=account + [str(x) for x in material.get("invites") or ()],
                    looked_up=list(getattr(got, "looked", ()) or ()))
    mine = sorted(addresses_of.get(owner) or ())
    look = event.get("event_type") == LOOK_EVENT
    readers = [] if (look or ours_only(event, addresses_of)) else reply_readers(event, mine)
    allowed = "\n".join([str(event.get("subject") or ""), text])
    ctx = checks() if (checks is not None and readers) else None
    held_back: list[dict[str, Any]] = []
    quality = ""
    if got.reply and review and getattr(got, "fixed", ""):
        # the support check's repair stands in for the flagged reply (the person reads it
        # before it goes; the outbound check below still runs on it)
        got.reply, got.warning = got.fixed, ""
    if got.reply:
        # the outbound check first: a reply that would carry another party's business to
        # an outside reader is a card that says why, never a silent drop
        stop = ob.held(ctx, got.reply, readers, allowed, account)
        quality = "" if stop else "nothing_useful" if ad._HOLLOW.match(got.reply) else \
            (ad.check(got.reply, m) or ad._tics(got.reply) or "")
        if review and (stop or quality == "figure_not_in_material" or getattr(got, "warning", "")):
            # the person reads it before it goes: their draft stays, the check's finding
            # beside it (never swapped for a card)
            got.warning = ("Check before sending: " + ob.reason_text(stop, sender)) if stop \
                else got.warning or WARN_FIGURE
            stop, quality = "", ""
        elif getattr(got, "warning", ""):
            # the support check flagged the reply and nobody reads it first: open question
            from .autopilot_check import ASK_PLAIN
            got.reply, got.warning = "", ""
            got.ask = {"question": ASK_PLAIN, "options": []}
        if stop:
            keep = got.ask["options"] if got.ask else []
            got.ask = {"question": "I held back the reply: " + ob.reason_text(stop, sender)
                       + ". What should it say?", "options": keep}
            held_back.append({"to": readers, "why": stop})
        if quality or stop:
            got.reply = ""
    if got.ask:
        got.ask["options"] = [
            o for o in got.ask["options"] if not ad._HOLLOW.match(o["reply"])
            and ad.check(o["reply"], m) is None and not ad._tics(o["reply"])
            and not ob.held(ctx, o["reply"], readers, allowed, account)]
        from .autopilot_check import ready_options
        got.ask["options"] = ready_options(got.ask["options"])
        if len(got.ask["options"]) == 1:
            # one answer left is a guess at the person's choice (``autopilot_check``)
            got.ask["options"] = []
        if look:
            # a look has no message to answer: its question is a to-do on the item
            got.todos = [*got.todos, {"system": "", "record": "", "what": got.ask["question"]}]
            got.ask = None
    if look:
        # a look raises at most LOOK_ITEMS things (changes first): more is a list nobody
        # asked for
        got.proposals = list(got.proposals)[:LOOK_ITEMS]
        got.todos = list(got.todos)[:max(0, LOOK_ITEMS - len(got.proposals))]
    got.proposals, back = outbound_proposals(got.proposals, event, addresses_of, owner, text,
                                             account, checks)
    if back:
        held_back += back
        got.summary = held_note(got.summary, back)
    rsvp = invitation_todo(event, material)
    if rsvp is not None and not any("event_respond" in json.dumps(p, default=str)
                                    for p in got.proposals):
        got.todos = [*got.todos, rsvp]
    why = "" if not got.empty else quality or ("outbound_held" if held_back else "nothing_to_do")
    return got, held_back, why


# a session that ran and left nothing on an invitation: its answer is still the person's
RSVP_AFTER = frozenset({"nothing_to_do", "check_dropped", "check_failed"})


def invitation_todo(event: Mapping[str, Any], material: Mapping[str, Any]
                    ) -> Optional[dict[str, str]]:
    """An invitation put to the person (``reply_due`` "asked" by its Exchange kind) whose
    calendar entry is not readable here (no ``invites`` line: most mailboxes' calendars are
    not captured): the answer as a to-do with what the mail says (its title, who sent it,
    when), done in their calendar. None for anything else, or when the entry is readable
    (then the session answers it with calendar event_respond)."""
    from .autopilot_engine import sender_label
    from .autopilot_records import _instant
    if event.get("item_kind") != "meeting_request" or reply_due(event) != "asked" \
            or material.get("invites"):
        return None
    title = " ".join(_INVITE_PREFIX.sub("", str(event.get("subject") or "")).split())[:160]
    at = _instant(event.get("at"))
    if not title or at is None:
        return None
    from zoneinfo import ZoneInfo
    local = at.astimezone(ZoneInfo("America/New_York"))
    sent = f"{local:%a %b} {local.day}, {local:%I:%M %p}".replace(" 0", " ") + " ET"
    who = sender_label(event.get("sender"), event.get("sender_name"))
    return {"system": "calendar", "record": title,
            "what": f"Answer the invitation \"{title}\" from {who} (sent {sent}) in your "
                    "calendar: accept or decline. Its calendar entry is not readable here."}


def invitation_only(event: Mapping[str, Any], material: Mapping[str, Any], why: str) -> Any:
    """A decision holding only ``invitation_todo``'s to-do, when the session ran and left
    nothing (``RSVP_AFTER``) on such an invitation; None otherwise."""
    from .autopilot_agent import Decision
    rsvp = invitation_todo(event, material) if why in RSVP_AFTER else None
    return None if rsvp is None else Decision(summary=f"Invitation: {rsvp['record']}",
                                              todos=[rsvp])


# ----------------------------------------------------------------------------- invitations

_INVITE_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|invitation|updated invitation|"
                            r"invitation \(updated\))\s*:\s*)+", re.I)


def _subject(text: Any) -> str:
    return " ".join(_INVITE_PREFIX.sub("", str(text or "")).lower().split())


def _calendar(ctx: Any, mailbox: str) -> list[tuple[str, dict[str, Any]]]:
    """The person's calendar events (object id, row), read once per store."""
    from .autopilot_records import _read
    st = ctx.st

    def make() -> list[tuple[str, dict[str, Any]]]:
        ids = [r[0] for r in st.conn.execute(
            "SELECT object_id FROM objects WHERE object_type = 'CalendarEvent' AND "
            "object_id LIKE ?", (f'%"mailbox":"{mailbox}"%',))]
        out: list[tuple[str, dict[str, Any]]] = []
        for start in range(0, len(ids), 5000):
            out += list(_read(st, ids[start:start + 5000],
                              ("subject", "start", "end", "is_organizer",
                               "is_cancelled")).items())
        return out
    return st.memo(f"autopilot:calendar:{mailbox}", make) if hasattr(st, "memo") else make()


def _graph_id(oid: str) -> str:
    """The calendar event's Graph id its object id carries (``{"id": ...}``); "" when none."""
    m = re.search(r'"id"\s*:\s*"([^"]+)"', oid)
    return m.group(1) if m else ""


def invitation_lines(ctx: Any, event: Mapping[str, Any]) -> list[str]:
    """For a mail about a meeting on the person's own calendar (same subject, starting after
    the mail, not cancelled, not theirs to organize): the event's id to answer it with
    (calendar event_respond) and what else is on their calendar then. At most
    ``INVITES``; [] for anything else."""
    from .autopilot_records import _instant, _v
    box = str(event.get("mailbox") or "")
    subject = _subject(event.get("subject"))
    at = _instant(event.get("at"))
    if event.get("event_type") != "message.received" or event.get("channel") == "teams" \
            or not re.fullmatch(r"[0-9a-f-]{36}", box) or not subject or at is None:
        return []
    rows = _calendar(ctx, box)
    out = []
    for oid, r in rows:
        start, end = _instant(_v(r, "start")), _instant(_v(r, "end"))
        if _subject(_v(r, "subject")) != subject or start is None or start < at \
                or _v(r, "is_cancelled").lower() == "true" \
                or _v(r, "is_organizer").lower() == "true" or not _graph_id(oid):
            continue
        end = end or start + timedelta(minutes=30)
        busy = [_v(x, "subject") for o, x in rows if o != oid
                and _v(x, "is_cancelled").lower() != "true"
                and (_instant(_v(x, "start")) or end) < end
                and (_instant(_v(x, "end")) or start) > start][:3]
        out.append(f"Invitation on the person's calendar: \"{_v(r, 'subject')}\" "
                   f"{start.isoformat()} to {end.isoformat()}; answer it with calendar "
                   f"event_respond, target event_id {_graph_id(oid)}"
                   + (f"; their calendar also holds then: {'; '.join(busy)}" if busy
                      else "; nothing else on their calendar then") + ".")
        if len(out) >= INVITES:
            break
    return out


# ----------------------------------------------------------------------------- looks

def look_events(owners: Sequence[str], now: datetime,
                changes: Optional[Callable[[str, str], Sequence[Mapping[str, Any]]]] = None
                ) -> list[dict[str, Any]]:
    """One look per person per working hour (US Eastern, Monday to Friday, ``LOOK_HOURS``):
    its event id is the person's and the hour's (the queue keeps it once); the first hour is
    the day's start. None outside working hours. ``changes(owner, at)``: the records that
    moved to a step of the person's since the last look (computed, never the model's
    reading), carried on the event (``look_changes``); none given -> an empty list, and a
    look with an empty list runs no session (``autopilot_agent.Assistant``)."""
    from zoneinfo import ZoneInfo
    local = now.astimezone(ZoneInfo("America/New_York"))
    if local.weekday() >= 5 or local.hour not in LOOK_HOURS:
        return []
    hour = local.replace(minute=0, second=0, microsecond=0)
    kind = "day start" if local.hour == LOOK_HOURS[0] else "hourly look"
    at = hour.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [{"event_id": _digest("look", o, at), "event_type": LOOK_EVENT,
             "object_id": f"Look:{o}:{at}", "object_type": "Look", "at": at,
             "owner_hint": o, "text": "",
             "fields": _look_fields(kind, changes(o, at) if changes else [])}
            for o in sorted(set(owners))]


def _look_fields(kind: str, rows: Any) -> dict[str, Any]:
    return {"look": kind, "changes": look_changes(rows), "changes_total": look_total(rows)}


LOOK_CHANGES = 20   # what one look's list carries (the newest first, as computed)
# A list longer than this is one item: its count and the newest few (``LIST_SHOWN``), no
# session. Measured on the dev replays (sets D-G) before this: of 27 looks whose list ran
# past 8 records, 22 prepared something and none was the person's right next step.
LIST_LONG = 8
LIST_SHOWN = 3


def previous_look(at: datetime) -> datetime:
    """The look before the one at ``at`` (``look_events``' hours): an hour before, or for
    the day's start the last working hour of the previous working day (US Eastern). What
    changed in between is that look's list (``autopilot_changes``)."""
    from zoneinfo import ZoneInfo
    local = at.astimezone(ZoneInfo("America/New_York"))
    hour = local.replace(minute=0, second=0, microsecond=0)
    if local.hour > LOOK_HOURS[0] and local.hour <= LOOK_HOURS[-1] and local.weekday() < 5:
        return (hour - timedelta(hours=1)).astimezone(timezone.utc)
    day = hour.date() - timedelta(days=1) if local.hour <= LOOK_HOURS[0] else hour.date()
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return datetime.combine(day, datetime.min.time().replace(hour=LOOK_HOURS[-1]),
                            hour.tzinfo).astimezone(timezone.utc)


def _listed(rows: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for r in rows if isinstance(rows, (list, tuple)) else ():
        if isinstance(r, Mapping) and isinstance(r.get("record"), str) and r["record"].strip() \
                and isinstance(r.get("line"), str) and r["line"].strip():
            out.append({"record": r["record"].strip(), "line": r["line"].strip()})
    return out


def look_changes(rows: Any) -> list[dict[str, str]]:
    """The computed change list as a look carries it: rows with a non-empty ``record`` (the
    key an item must name) and ``line`` (what changed, for the model), at most
    ``LOOK_CHANGES``; anything else is dropped (an unreadable list is empty: no look)."""
    return _listed(rows)[:LOOK_CHANGES]


def look_total(rows: Any) -> int:
    """How many usable rows the computed list held before ``look_changes`` cut it."""
    return len(_listed(rows))


def changes_total(fields: Mapping[str, Any], changes: Sequence[Any]) -> int:
    """A look event's list size: its ``changes_total`` when a whole number at least the rows
    carried, else the rows carried (an odd or missing total never shrinks the list)."""
    got = fields.get("changes_total")
    n = len(changes)
    return got if isinstance(got, int) and not isinstance(got, bool) and got > n else n


def long_list(changes: Sequence[Mapping[str, str]], total: int) -> Optional[dict[str, str]]:
    """The one to-do a look makes of a list longer than ``LIST_LONG``: the count and the
    newest ``LIST_SHOWN`` rows (the list is newest first); None for a shorter list."""
    if total <= LIST_LONG or not changes:
        return None
    shown = list(changes[:LIST_SHOWN])
    what = (f"{total} records moved to a step of yours since the last look; the newest "
            f"{len(shown)}: " + "; ".join(f"[{c['record']}] {c['line']}" for c in shown) + ".")
    return {"system": "", "record": shown[0]["record"], "what": what}


def on_changes(got: Any, changes: Sequence[Mapping[str, str]]) -> Any:
    """``got`` (a look's decision) with only the changes and to-dos that name a record of the
    computed list (its ``record`` key in what the item says or targets, any case); a look
    never asks or replies. Measured on the dev sets before this: looks left to find their
    own work raised 0 right items of 16 after the support check (standing backlog)."""
    # a key is named whole (never "IN-1" inside "IN-10")
    keys = [re.compile(r"(?<![\w-])" + re.escape(str(c["record"]).strip().lower())
                       + r"(?![\w-])") for c in changes if str(c.get("record") or "").strip()]

    def names(x: Any) -> bool:
        text = json.dumps(x, ensure_ascii=False, default=str).lower()
        return any(k.search(text) for k in keys)
    if got.ask and str(got.ask.get("question") or "").strip():
        # a look has no message to answer: its question is a to-do (``agent_output``)
        got.todos = [*got.todos, {"system": "", "record": "", "what": got.ask["question"]}]
    got.reply, got.ask = "", None
    got.proposals = [p for p in got.proposals if names(p.get("arguments"))]
    got.todos = [t for t in got.todos if names(t)]
    return got
