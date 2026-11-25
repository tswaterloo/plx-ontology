"""The autopilot work queue and its registry rules: conditions, intents and their detection,
matching and the ``Queue`` store (``operating.autopilot``, which re-exports them)."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Iterable, Mapping, Sequence
from operating import declared_schema as _schemas
from operating.declared_schema import autopilot_queue as _AUTOPILOT_QUEUE

from .autopilot_records import OWN_POSTS_DAYS, _digest, _instant, _iso


SCHEMA = "plx-autopilot/v1"
KILL_ENV = "PLX_AUTOPILOT_OFF"
KILL_FILE = "autopilot.off"
ROLES_ENV = "PLX_AUTOPILOT_ROLES_FILE"
ROLES_FILE = "autopilot-roles.json"
QUEUE_FILE = "autopilot.sqlite"
CARD_PREFIX = "autopilot:"
EVENT_TYPES = ("message.received", "task.assigned", "record.changed", "qc.result",
               "approval.requested", "meeting.ended", "record.due", "question.asked",
               "assistant.look", "message.waiting")
OWNER_RULES = ("recipient", "assignee", "approvers", "organizer", "buyer", "addressee",
               "hinted")
ROLE_OWNER = re.compile(r"^role:[a-z_]{1,40}$")
# an item no one person's record names (a late PO no mail or code names a buyer for): one
# item the role holds (owner ``pool:<role>``), shown to each of its people until one takes
# it (``Queue.take``), never one copy per person
POOL_PREFIX = "pool:"


def pool_role(owner: str) -> str | None:
    """The role a pooled item's owner names, or None for a person's own item."""
    owner = str(owner or "")
    return owner[len(POOL_PREFIX):] if owner.startswith(POOL_PREFIX) else None
CONDITIONS = ("intents_any", "intents_none", "side_in", "status_in", "assigned", "open", "asks",
              "quality_in", "tested", "ended", "organizer_copy", "external_or_linked",
              "mailbox_role", "mailbox_role_none", "engaged", "correspondent", "engaged_or_asks",
              "answers_sender", "closing", "addressed_other", "quiet_sender", "object_type_in",
              "triage_none", "triage_work_in", "triage_work_none",
              "to_me", "after_own_post", "cc_min", "usual_answerer", "known_party", "any_of")
# conditions a triage reading of "reply" answers (``matches``): whether the text asks, is a
# closer, and the history guesses standing in for that reading (an asking sender the reader
# wrote to, a colleague they seldom answer by mail). Measured on fresh days 11-06..21: of the
# uncovered mail replies answering an incoming message, ``quiet_sender`` alone dropped 501
# and ``engaged_or_asks`` 274 (with both: 208)
READ_BY_TRIAGE = frozenset(("asks", "closing", "quiet_sender", "engaged_or_asks"))
DONE_RULES = ("owner_replied", "approval_decided", "task_closed", "batch_released",
              "owner_mailed_attendees", "question_answered", "person_decided")
MAX_RECORDS = 12          # business records one handler reads around its event
# a handler that failed or ran out of time on an event: the event is deferred and tried
# again by the next cycles, at most this many tries, then left ``failed`` (shown, never
# silently processed)
HANDLER_TRIES = 3
SUMMARY_CHARS = 160
WORK_LOG_ENV = "PLX_WORK_LOG_DIR"  # where the Workbench keeps its work log
REPLY_HOURS = 72          # an answer counts within this many hours (the replay's horizon)
TEXT_CHARS = 30000      # an event's text (the mail's own words; was 4,000, cut unsaid)

EMAIL_PROPS = ("subject", "sender", "to_addresses", "cc_addresses", "received_at", "sent_at",
               "conversation_id", "internet_message_id", "body_text", "body_preview", "mailbox")
BUSINESS = ("SalesOrder", "PurchaseOrder", "SalesInvoice", "Shipment", "Receipt", "Batch",
            "CustomerPurchaseOrder")
SIDE_LINKS = {"Email.sent_by_customer": "customer", "Email.sent_by_supplier": "supplier"}
SO_LINE_OF, SO_LINE_ITEM = "SalesOrderLine.line_of", "SalesOrderLine.for_item"
PO_LINE_OF, PO_LINE_ITEM = "PurchaseOrderLine.line_of", "PurchaseOrderLine.for_item"
# a PARTIAL shipment/receipt/invoice is not closed ("Partially Shipped" is still open work)
CLOSED = re.compile(r"(?<!partially )(?<!partial )\b(?:closed|complete[d]?|cancel(?:l?ed)?|void|shipped|invoiced|fully|done|"
                    r"merged|archived|posted|received)\b", re.I)
TASK_DONE = re.compile(r"^(?:done|merged|closed|cancel+ed|complete[d]?|archived|wont_?do)$",
                       re.I)

# Intents, generally: words of the message's own text (quoted history removed), never a
# party, a record or a person. A message may carry several.
_PO_WORDS = (r"(?:purchase\s+order|p\.?\s?o\.?(?=\s*(?:#|no\.?|number|\d))|\bpo\b(?=\s*#?\s*"
             r"[A-Z]{0,4}-?\d))")
INTENTS: dict[str, re.Pattern[str]] = {
    "customer_po": re.compile(
        r"(?:(?:attached|enclosed|please\s+find|here\s+is|sending|placing|new|our)\s+(?:is\s+|"
        r"are\s+)?(?:our\s+|the\s+|a\s+|attached\s+)?(?:new\s+|revised\s+)?" + _PO_WORDS
        + r"|\b(?:new|place\s+an?|placing\s+an?)\s+order\b|" + _PO_WORDS
        + r"\s+(?:attached|enclosed))", re.I),
    "status_request": re.compile(
        r"\b(?:status\s+(?:of|on|update)|update\s+on\s+(?:my|our|the|this)|when\s+(?:will|"
        r"can|do)\s+(?:we|i|it|they|the\s+order|our\s+order)\s+(?:ship|arrive|be\s+"
        r"(?:shipped|delivered|ready))|\beta\b|tracking\s+(?:number|info|#)|has\s+(?:it|the\s+"
        r"order|our\s+order)\s+shipped|ship\s+date\s*\?|(?:what|confirm)\s+(?:is\s+)?the\s+"
        r"ship\s+date)", re.I),
    "document_request": re.compile(
        r"\b(?:(?:send|provide|share|need|forward|request(?:ing)?|get)\b[^.?!\n]{0,40}\b(?:"
        r"coa|c\.o\.a|certificates?\s+of\s+analysis|sds|msds|safety\s+data\s+sheet|spec(?:"
        r"ification)?\s+sheet|packing\s+(?:slip|list)|bill\s+of\s+lading|bol|proof\s+of\s+"
        r"delivery|pod|copy\s+of\s+(?:the\s+|our\s+)?invoice|invoice\s+copy)|(?:coa|"
        r"certificates?\s+of\s+analysis|invoice\s+copy)\s+(?:for|please|needed|required))",
        re.I),
    "payment_question": re.compile(
        r"\b(?:(?:payment|remittance|paid|pay|balance|statement|outstanding|overdue|past\s+due|"
        r"credit\s+(?:note|memo))\b[^.?!\n]{0,60}\binvoices?\b|\binvoices?\b[^.?!\n]{0,60}"
        r"\b(?:payment|paid|pay|balance|overdue|past\s+due|outstanding|due\s+date|remittance)"
        r"\b)", re.I),
    "supplier_delay": re.compile(
        r"\b(?:delay(?:ed|s)?|back[\s-]?order(?:ed)?|shortage|short\s+shipp?(?:ed|ing)?|out\s+of"
        r"\s+stock|(?:pushed|push)\s+(?:out|back)|revised\s+(?:ship|delivery|eta)|new\s+(?:ship|"
        r"delivery)\s+date|unable\s+to\s+(?:ship|deliver|supply)|will\s+not\s+(?:ship|arrive)"
        r"\s+(?:until|before)|allocation|force\s+majeure)\b", re.I),
}
APOLOGY = re.compile(r"\b(?:sorry|apologi[sz]e|apologies)\b[^.?!\n]{0,30}\bdelay|\bdelay(?:ed)?\s+"
                     r"(?:in\s+)?(?:my|our)\s+(?:reply|response|getting\s+back)", re.I)
_NEGATED = re.compile(r"\b(?:no|not|never|without)\s+(?:\w+\s+){0,2}(?:delay|shortage|"
                      r"backorder|issue|problem)", re.I)
PO_NUMBER = re.compile(r"\b(?:p\.?\s?o\.?|purchase\s+order)\s*(?:#|no\.?|number|num)?\s*[:.]?\s*"
                       r"([A-Z]{0,4}-?\d[\w-]{2,20})", re.I)
ORDER_NUMBER = re.compile(r"\b(?:order|so|sales\s+order)\s*(?:#|no\.?|number)?\s*[:.]?\s*"
                          r"([A-Z]{0,4}-?\d{3,12})\b", re.I)
INVOICE_NUMBER = re.compile(r"\binv(?:oice)?\s*(?:#|no\.?|number)?\s*[:.]?\s*"
                            r"([A-Z]{0,4}-?\d{3,12})\b", re.I)


class RegistryInvalid(ValueError):
    pass


class HandlerTimeout(Exception):
    pass


def matches(when: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    """Whether an event meets an autopilot's conditions (each a general field test)."""
    intents = set(event.get("intents") or ())
    # the triage model read the message as owed a reply by a real person: its reading
    # stands for the cheap phrase and history guesses (``READ_BY_TRIAGE``): it asks, is no
    # closer, and the guesses standing in for that reading pass
    owed = event.get("triage") == "reply"
    if owed:
        event = {**event, "asks": True, "closing": False}
    for key, want in when.items():
        if owed and key in ("quiet_sender", "engaged_or_asks"):
            # a reply reading still needs a sender who is no stranger (the desk rule's
            # screen): a known customer, supplier or colleague, a known party, a thread the
            # person is in or a correspondent; a model misread never makes an item for a
            # cold sender
            if key == "engaged_or_asks" and not want or key == "engaged_or_asks" and \
                    not known_sender(event):
                return False
            continue
        if key == "any_of":
            # a list of condition sets, one of which must hold (each tested as a whole
            # ``when``); an empty or unreadable list: no
            if not isinstance(want, list) or not any(
                    isinstance(sub, Mapping) and sub and matches(sub, event) for sub in want):
                return False
            continue
        if key == "intents_any" and not intents & set(want):
            return False
        if key == "intents_none" and intents & set(want):
            return False
        if key == "side_in" and event.get("side") not in want:
            return False
        if key == "status_in" and str(event.get("status") or "").lower() not in \
                {str(w).lower() for w in want}:
            return False
        if key == "quality_in" and event.get("quality") not in want:
            return False
        if key == "object_type_in" and event.get("object_type") not in want:
            return False
        if key in ("assigned", "open", "tested", "ended", "organizer_copy",
                   "external_or_linked", "asks", "engaged", "correspondent", "closing",
                   "addressed_other", "to_me", "known_party") and \
                bool(event.get(key)) != bool(want):
            return False
        if key == "answers_sender":
            # [n, share]: the owner answered at least ``share`` of the sender's last
            # messages, ``n`` or more of them known, each within the answer window the
            # replay judges in (``sender_answered``: working hours, ``answer_window_hours``);
            # an event read without it: no
            n, answered = (list(event.get("sender_answered") or ()) + [0, 0])[:2]
            if not (isinstance(want, list) and len(want) == 2) or n < int(want[0]) or \
                    answered < float(want[1]) * n:
                return False
            continue
        if key == "quiet_sender":
            # [n, share, sides]: not a sender on one of ``sides`` whose last ``n`` or more
            # known messages the owner answered under ``share`` of (measured 2026-10-06..25:
            # a colleague's mail the owner answers elsewhere, in chat or in person, was
            # answered by mail about one time in four; an unknown sender passes)
            n, answered = (list(event.get("sender_history") or ()) + [0, 0])[:2]
            if event.get("side") in want[2] and n >= int(want[0]) and \
                    answered < float(want[1]) * n:
                return False
            continue
        if key == "after_own_post":
            # [minutes, posts]: the owner wrote in this conversation at most ``minutes``
            # before it, and ``posts`` or more times in the last ``OWN_POSTS_DAYS`` (an
            # event read without them, or unreadable: no)
            last, posts = (list(event.get("own_posts") or ()) + [None, 0])[:2]
            if not isinstance(last, (int, float)) or isinstance(last, bool) or \
                    last > want[0] or not isinstance(posts, int) or posts < want[1]:
                return False
            continue
        if key == "usual_answerer":
            # [n, share]: in this conversation, the owner was the first member to write
            # after at least ``share`` of the sender's messages of the last
            # ``OWN_POSTS_DAYS`` (``n`` or more of them known) before this one (an event
            # read without it: no). Measured on group chats 10-12..11-10, a question no one
            # @mentioned: the usual first answerer wrote within 24 h after 26 of 31, any
            # other member after 52 of 125
            n, mine = (list(event.get("answerer") or ()) + [0, 0])[:2]
            if not (isinstance(want, list) and len(want) == 2) or n < int(want[0]) or \
                    mine < float(want[1]) * n:
                return False
            continue
        if key == "cc_min":
            # at least ``want`` addresses copied on the message (an unreadable list: no)
            cc = event.get("cc")
            if not isinstance(cc, list) or len(cc) < want:
                return False
            continue
        if key == "triage_none":
            # the model's triage of the message (``chat_ask_model.KINDS``) is none of
            # ``want``; a message it did not read (no model, a failed call) passes
            if event.get("triage") in want:
                return False
            continue
        if key in ("triage_work_in", "triage_work_none"):
            # the model read the message as one of ``want`` and named work it leaves the
            # reader (``triage_work``): ``_in`` needs that reading (a message it did not
            # read never passes), ``_none`` passes every other message, unread ones too
            worked = event.get("triage") in want and bool(
                str(event.get("triage_work") or "").strip())
            if worked != (key == "triage_work_in"):
                return False
            continue
        if key == "engaged_or_asks" and bool(event.get("engaged") or (
                event.get("asks") and event.get("correspondent"))) != bool(want):
            return False
    return True


def known_sender(event: Mapping[str, Any]) -> bool:
    """Whether a message's sender is no stranger to its reader: a colleague, a known
    customer or supplier, a known party, or someone they wrote to or in whose thread they
    wrote (``engagement``)."""
    return event.get("side") in ("internal", "customer", "supplier") or bool(
        event.get("known_party") or event.get("engaged") or event.get("correspondent"))


def names_reader(text: Any, names: Iterable[str]) -> bool:
    """Whether a message names its reader in its words: one of ``names`` (their first
    names, two letters or more) as a whole word, any case."""
    own = text if isinstance(text, str) else ""
    return any(re.search(r"(?<![\w])" + re.escape(str(n)) + r"(?![\w])", own, re.I)
               for n in names if len(str(n)) >= 2)


# ----------------------------------------------------------------------------- queue

def order_key(number: Any) -> str:
    """An order or lot number compared as one identity: letters and digits only, case aside
    ("OR-1000154316" and "or1000154316" are one order); "" for none."""
    return re.sub(r"[^0-9A-Za-z]", "", str(number or "")).upper()


def lot_subject(lot: Any) -> str:
    """The subject key of an item about a lot ("lot:<number>"), "" with no number."""
    key = order_key(lot)
    return f"lot:{key}" if key else ""


def subject_of(item: Mapping[str, Any]) -> str:
    """The real-world thing an item is about, when its prepared work names one: the
    handler's ``subject``, else (an item made before handlers wrote one) a QC item's lot.
    Two open items of one owner with the same subject are one card: the newer replaces the
    older (``autopilot_engine.settle``, and the list the person reads). "" = no subject:
    the item stands alone."""
    prepared = ((item.get("body") or {}).get("prepared") or {})
    if not isinstance(prepared, Mapping):
        return ""
    subject = prepared.get("subject")
    if isinstance(subject, str) and subject.strip():
        return subject.strip()
    if "affected" in prepared and prepared.get("lot"):
        return lot_subject(prepared.get("lot"))
    return ""


def superseded(items: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Each item id an item of the same owner and subject made later replaces -> the id of
    the newest one (ties: the larger item id stands, so the choice is the same on every
    read)."""
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for item in items:
        subject = subject_of(item)
        if subject:
            groups.setdefault((str(item.get("owner") or ""), subject), []).append(item)
    out: dict[str, str] = {}
    for members in groups.values():
        members.sort(key=lambda i: (str(i.get("created_at") or ""), str(i.get("item_id") or "")))
        head = str(members[-1]["item_id"])
        for item in members[:-1]:
            out[str(item["item_id"])] = head
    return out


def newest_of(item_id: str, replaced: Mapping[str, str]) -> str:
    """The item that stands for ``item_id`` now: followed through every replacement."""
    seen = {item_id}
    while item_id in replaced and replaced[item_id] not in seen:
        item_id = replaced[item_id]
        seen.add(item_id)
    return item_id


SUPERSEDED_BY = "superseded_by"   # queue meta: replaced item id -> the item replacing it


class Queue:
    """The autopilot's own SQLite file (events, prepared items, rate counts, meta)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        _schemas.apply(self.conn, _AUTOPILOT_QUEUE)

    def close(self) -> None:
        self.conn.close()

    @property
    def killed_file(self) -> Path:
        return self.path.parent / KILL_FILE

    def add_event(self, event: Mapping[str, Any], now: str) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO events (event_id, event_type, object_id, object_type, at, "
            "landed_at, seen_at, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event["event_id"], event["event_type"], event["object_id"], event["object_type"],
             event.get("at"), event.get("landed_at"), now,
             json.dumps(event, sort_keys=True, default=str)))
        return cur.rowcount == 1

    def pending(self, limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT body FROM events WHERE state IN ('new', 'deferred') "
                                 "ORDER BY at, event_id LIMIT ?", (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def mark(self, event_id: str, state: str, reason: str | None, now: str) -> None:
        self.conn.execute("UPDATE events SET state = ?, reason = ?, processed_at = ? "
                          "WHERE event_id = ?", (state, reason, now, event_id))
        if state != "deferred":
            self.conn.execute("DELETE FROM meta WHERE key = ?", ("tries:" + event_id,))

    def tries(self, event_id: str) -> int:
        """How many failed handler tries an event has had so far (counts nothing)."""
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?",
                                ("tries:" + event_id,)).fetchone()
        try:
            return int(row[0]) if row is not None else 0
        except (TypeError, ValueError):
            return HANDLER_TRIES

    def tried(self, event_id: str) -> int:
        """Count one more failed handler try of an event; how many it has had."""
        key = "tries:" + event_id
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        try:
            tries = int(row[0]) + 1 if row is not None else 1
        except (TypeError, ValueError):
            tries = HANDLER_TRIES   # an unreadable count: no further tries
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                          (key, str(tries)))
        return tries

    def note_sent(self, notes: Iterable[Mapping[str, Any]]) -> int:
        """The owners' own sent messages (``discover``'s ``outbound``), as digests of the
        thread and each counterpart: what ``engagement`` reads (no address or text kept)."""
        rows = set()
        for n in notes:
            when = _instant(n.get("at"))
            if when is None:
                continue
            thread = _digest("thread", n.get("conversation") or n.get("message_id"))
            for addr in n.get("to") or ("",):
                rows.add((n["owner"], thread, _digest("party", str(addr).lower()),
                          _iso(when)))
        cur = self.conn.executemany("INSERT OR IGNORE INTO sent VALUES (?, ?, ?, ?)",
                                    sorted(rows))
        return cur.rowcount

    def note_routine(self, notes: Iterable[Mapping[str, Any]]) -> int:
        """The owners' own self-started internal mails (``routine_note``): the series and
        recipient digests, the message's record id and whether it greets someone; what
        ``autopilot_work.routine_due_events`` learns recurring updates from (no subject or
        address kept: the due event reads them from the store)."""
        rows = set()
        for n in notes:
            when = _instant(n.get("at"))
            if when is None or not n.get("series") or not n.get("owner"):
                continue
            rows.add((n["owner"], n["series"], _iso(when), str(n.get("message") or ""),
                      json.dumps(sorted({_digest("party", str(a).lower())
                                         for a in n.get("recipients") or ()})),
                      int(bool(n.get("greeted")))))
        return self.conn.executemany("INSERT OR IGNORE INTO routine VALUES (?, ?, ?, ?, ?, ?)",
                                     sorted(rows)).rowcount

    def routine(self, since: str, until: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT owner, series, at, message, recipients, greeted FROM routine "
            "WHERE at >= ? AND at < ? ORDER BY owner, series, at", (since, until)).fetchall()

    def note_inbound(self, events: Iterable[Mapping[str, Any]]) -> int:
        """Messages that reached an owner (thread and sender digests, time): with
        ``sent``, how often the owner answered a sender before (``reply_history``)."""
        rows = set()
        for e in events:
            when = _instant(e.get("at"))
            owner = str(e.get("mailbox") or "")
            if when is None or not owner or e.get("event_type", "message.received") != \
                    "message.received":
                continue
            rows.add((owner, _digest("thread", e.get("conversation_id") or e.get("message_id")),
                      _digest("party", str(e.get("sender") or "").lower()), _iso(when)))
        return self.conn.executemany("INSERT OR IGNORE INTO inbound VALUES (?, ?, ?, ?)",
                                     sorted(rows)).rowcount

    def reply_history(self, owner: str, sender: str, at: str, *,
                      horizon_hours: int = REPLY_HOURS, days: int = 60,
                      working: bool = False) -> tuple[int, int]:
        """(messages from ``sender`` to ``owner`` whose answer window closed before ``at``,
        how many of them the owner answered in the thread within ``horizon_hours``): known
        at ``at``, never later evidence. ``working``: the window is ``horizon_hours`` of
        Monday-to-Friday time (``autopilot_replay.answer_end``, the replay's own window),
        not clock hours."""
        start = _instant(at)
        if start is None or not sender:
            return 0, 0
        if working:
            return self._reply_history_working(owner, sender, start, horizon_hours, days)
        closed = _iso(start - timedelta(hours=horizon_hours))
        since = _iso(start - timedelta(days=days))
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(EXISTS (SELECT 1 FROM sent s WHERE s.owner = "
            "i.owner AND s.thread = i.thread AND s.at > i.at AND s.at <= strftime("
            "'%Y-%m-%dT%H:%M:%SZ', i.at, ?))), 0) FROM inbound i WHERE i.owner = ? AND "
            "i.counterpart = ? AND i.at >= ? AND i.at <= ?",
            (f"+{horizon_hours} hours", owner, _digest("party", sender.lower()), since, closed)
        ).fetchone()
        return int(row[0]), int(row[1])

    def _reply_history_working(self, owner: str, sender: str, start: datetime,
                               horizon_hours: int, days: int) -> tuple[int, int]:
        from .autopilot_replay import answer_end
        horizon = timedelta(hours=horizon_hours)
        since = _iso(start - timedelta(days=days))
        n = answered = 0
        # each message with the owner's first message in its thread after it (one read)
        for got, first in self.conn.execute(
                "SELECT i.at, (SELECT MIN(s.at) FROM sent s WHERE s.owner = i.owner AND "
                "s.thread = i.thread AND s.at > i.at) FROM inbound i WHERE i.owner = ? AND "
                "i.counterpart = ? AND i.at >= ? AND i.at < ?",
                (owner, _digest("party", sender.lower()), since, _iso(start))):
            when = _instant(got)
            if when is None:
                continue
            close = answer_end(when, horizon)
            if close > start:
                continue                    # its window is still open at ``at``
            n += 1
            reply = _instant(first) if first else None
            answered += reply is not None and reply <= close
        return n, answered

    def engagement(self, owner: str, conversation: str | None, sender: str, at: str,
                   *, contact_days: int = 60) -> dict[str, bool]:
        """Whether ``owner`` wrote in this thread, or to this sender in the last
        ``contact_days``, before ``at`` (their own sent mail only: no later evidence)."""
        start = _instant(at)
        if start is None:
            return {"engaged": False, "correspondent": False}
        at = _iso(start)
        engaged = bool(conversation) and self.conn.execute(
            "SELECT 1 FROM sent WHERE owner = ? AND thread = ? AND at < ? LIMIT 1",
            (owner, _digest("thread", conversation), at)).fetchone() is not None
        since = _iso(start - timedelta(days=contact_days))
        contact = bool(sender) and self.conn.execute(
            "SELECT 1 FROM sent WHERE owner = ? AND counterpart = ? AND at >= ? AND at < ? "
            "LIMIT 1", (owner, _digest("party", sender.lower()), since, at)
        ).fetchone() is not None
        return {"engaged": engaged, "correspondent": contact}

    def own_posts(self, owner: str, conversation: str | None, at: str
                  ) -> tuple[float | None, int]:
        """(minutes since ``owner`` last wrote in this conversation before ``at``, None when
        not in ``OWN_POSTS_DAYS``; how many times they wrote there in those days): their own
        sent messages only, never later evidence."""
        start = _instant(at)
        if start is None or not conversation:
            return None, 0
        row = self.conn.execute(
            "SELECT MAX(at), COUNT(DISTINCT at) FROM sent WHERE owner = ? AND thread = ? AND "
            "at >= ? AND at < ?", (owner, _digest("thread", conversation),
                                   _iso(start - timedelta(days=OWN_POSTS_DAYS)), _iso(start))
        ).fetchone()
        last = _instant(row[0]) if row and row[0] else None
        return (round((start - last).total_seconds() / 60, 1) if last else None,
                int(row[1] or 0) if row else 0)

    def usual_answerer(self, owner: str, sender: str, conversation: str | None, at: str
                       ) -> tuple[int, int]:
        """(the sender's messages in this conversation in the ``OWN_POSTS_DAYS`` before
        ``at`` that another member wrote after, before ``at``; how many of them ``owner``
        was that first member for): the members' own sent rows only, never later
        evidence."""
        start = _instant(at)
        if start is None or not conversation or not sender:
            return 0, 0
        thread, end = _digest("thread", conversation), _iso(start)
        rows = self.conn.execute(
            "SELECT (SELECT n.owner FROM sent n WHERE n.thread = s.thread AND n.at > s.at "
            "AND n.at < ? AND n.owner != s.owner ORDER BY n.at LIMIT 1) FROM (SELECT DISTINCT "
            "owner, thread, at FROM sent WHERE owner = ? AND thread = ? AND at >= ? AND "
            "at < ?) s", (end, sender, thread, _iso(start - timedelta(days=OWN_POSTS_DAYS)),
                         end)).fetchall()
        firsts = [r[0] for r in rows if r[0]]
        return len(firsts), sum(1 for r in firsts if r == owner)

    def writers(self, since: str) -> set[tuple[str, str]]:
        """(owner, thread digest) of every conversation an owner wrote in since ``since``:
        one read per cycle (``run_cycle`` keeps a group message no one addressed to a member
        only when the member writes there: a large chat never fans out to its readers)."""
        return {(o, t) for o, t in self.conn.execute(
            "SELECT DISTINCT owner, thread FROM sent WHERE at >= ?", (since,))}

    def thread_item_open(self, owner: str, autopilot: str, event: Mapping[str, Any]) -> bool:
        """Whether this autopilot already has an open item for ``owner`` on an earlier
        message of the event's thread that the owner has not written in since: one item
        per thread until they answer (a burst of messages is one piece of work)."""
        conversation = event.get("conversation_id")
        at = _instant(event.get("at"))
        if event.get("event_type") != "message.received" or not conversation or at is None:
            return False
        last = self.conn.execute(
            "SELECT MAX(at) FROM sent WHERE owner = ? AND thread = ? AND at < ?",
            (owner, _digest("thread", conversation), _iso(at))).fetchone()[0]
        floor = _instant(last) if last else None
        for (raw,) in self.conn.execute(
                "SELECT e.at FROM items i JOIN events e ON e.event_id = i.event_id WHERE "
                "i.owner = ? AND i.autopilot = ? AND i.state = 'open' AND "
                "json_extract(e.body, '$.conversation_id') = ?",
                (owner, autopilot, str(conversation))):
            then = _instant(raw)
            if then is not None and then <= at and (floor is None or then > floor):
                return True
        return False

    def answered_in_chat(self, owner: str, event: Mapping[str, Any], now: datetime,
                         names: Iterable[str] = ()) -> str | None:
        """Why a Teams message needs no item for ``owner`` by ``now``: "owner_replied" when
        they already wrote in its chat or thread after it (an item prepared after the answer
        would only be closed by it: 304 of 388 live items, 2026-11-13..17); in a group,
        "other_replied" when another person the message @mentions (neither the sender nor
        the owner) wrote there after it, and the message is not the owner's alone: it
        @mentions more than the owner and does not name the owner (``names``: their first names) in its text (an ask put to
        the owner stays theirs whoever else speaks; a group turn stays the owner's). Their
        own sent messages only, each between the message and ``now`` (a replay's clock:
        never later evidence). None: no answer known, or unreadable."""
        conversation = event.get("conversation_id")
        at = _instant(event.get("at"))
        if event.get("channel") not in ("teams", "mail") or not conversation or at is None \
                or now <= at:
            return None
        thread = _digest("thread", conversation)
        window = (_iso(at), _iso(now))
        if self.conn.execute("SELECT 1 FROM sent WHERE owner = ? AND thread = ? AND at > ? "
                             "AND at <= ? LIMIT 1", (owner, thread, *window)).fetchone():
            return "owner_replied"
        if event.get("channel") != "teams":
            return None            # a mail thread: only the owner's own later mail answers it
        sender = str(event.get("sender") or "")
        mentions = event.get("mentions")
        others = sorted({str(m) for m in mentions} - {owner, sender}) \
            if isinstance(mentions, list) else []
        names = [str(n) for n in names if len(str(n)) >= 2]
        # names not known: whether the text names the owner is not known either (kept)
        if not (event.get("group") and event.get("to_me") and sender and others and names) \
                or names_reader(event.get("text"), names):
            return None
        if self.conn.execute(
                "SELECT 1 FROM sent WHERE owner IN (SELECT value FROM json_each(?)) AND "
                "thread = ? AND at > ? AND at <= ? LIMIT 1",
                (json.dumps(others), thread, *window)).fetchone():
            return "other_replied"
        return None

    def answered_since(self, item: Mapping[str, Any]) -> bool:
        """Whether the item's owner already wrote in the item's conversation after its
        message (their own sent mail or chat messages, ``note_sent``); False when the
        conversation or time is not known. Read only."""
        row = self.conn.execute("SELECT body FROM events WHERE event_id = ?",
                                (item.get("event_id"),)).fetchone()
        try:
            body = json.loads(row[0]) if row else {}
        except ValueError:
            return False
        at, conv = _instant(body.get("at")), body.get("conversation_id")
        if not isinstance(body, dict) or not conv or at is None:
            return False
        return self.conn.execute(
            "SELECT 1 FROM sent WHERE owner = ? AND thread = ? AND at > ? LIMIT 1",
            (item["owner"], _digest("thread", conv), _iso(at))).fetchone() is not None

    def chat_ask(self, message_id: str, reader: str) -> bool | None:
        """The model's cached verdict on whether a chat message asks ``reader`` something
        (``operating.chat_ask_model``); None: not read yet."""
        row = self.conn.execute("SELECT asks FROM chat_asks WHERE message_id = ? AND "
                                "reader = ?", (message_id, reader)).fetchone()
        return None if row is None else bool(row[0])

    def chat_triage(self, message_id: str, reader: str) -> tuple[bool, str, str] | None:
        """The cached verdict with its triage reading: ``(asks, kind, work)``; kind and work
        "" for a plain yes/no verdict (``chat_ask_model.triage_of``); None: not read yet."""
        from .chat_ask_model import triage_of
        row = self.conn.execute("SELECT asks, reason FROM chat_asks WHERE message_id = ? "
                                "AND reader = ?", (message_id, reader)).fetchone()
        return None if row is None else (bool(row[0]), *triage_of(row[1]))

    def note_chat_asks(self, notes: Iterable[Mapping[str, Any]]) -> int:
        """Keep model verdicts (``message_id``, ``reader``, ``asks``, ``reason``, ``model``,
        ``at``): the first one for a message and reader stands."""
        n = 0
        for x in notes:
            n += self.conn.execute(
                "INSERT OR IGNORE INTO chat_asks VALUES (?, ?, ?, ?, ?, ?)",
                (str(x["message_id"]), str(x["reader"]), int(bool(x["asks"])),
                 x.get("reason"), x.get("model"), str(x["at"]))).rowcount
        return n

    def work_log_path(self) -> Path:
        """The Workbench work log beside this queue (``<state>/work-log``; the API's
        ``PLX_WORK_LOG_DIR`` overrides), read only."""
        override = os.environ.get(WORK_LOG_ENV)
        return (Path(override) if override else self.path.parent.parent / "work-log") \
            / "work-log.sqlite"

    def answered_in_workbench(self, mailboxes: Iterable[str], event: Mapping[str, Any],
                              now: datetime) -> bool:
        """Whether the owner (their ``mailboxes``) already answered this message from the
        Workbench (the approvals desk or an accepted autopilot item; the work log's
        follow-through loop names the mailbox and the thread or message it answered) after
        it arrived and by ``now``:
        then no item is prepared. Only the owner's own sends are read, as a yes or no; an
        unreadable or absent log answers no (an item is prepared, as before)."""
        at = _instant(event.get("at"))
        threads = [str(t) for t in (event.get("conversation_id"), event.get("message_id"))
                   if t]
        boxes = sorted({str(b).lower() for b in mailboxes if b and "@" in str(b)})
        if event.get("event_type") != "message.received" or not boxes or at is None \
                or not threads:
            return False
        path = self.work_log_path()
        if not path.exists():
            return False
        try:
            conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
            try:
                rows = conn.execute(
                    "SELECT at, json_extract(awaiting, '$.answers'), json_extract(awaiting, "
                    "'$.answered_at') FROM event WHERE awaiting IS NOT NULL AND kind IN "
                    "('action', 'autopilot') AND lower(json_extract(awaiting, '$.mailbox')) IN "
                    "(SELECT value FROM json_each(?)) AND EXISTS (SELECT 1 FROM "
                    "json_each(awaiting, '$.threads') WHERE value IN (SELECT value FROM "
                    "json_each(?)))",
                    (json.dumps(boxes), json.dumps(threads))).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return False
        for raw_sent, answers, answered_at in rows:
            sent = _instant(raw_sent)
            if sent is None or not at < sent <= now:
                continue
            if answers:
                # the send names the message it answered: that one, or one of the thread
                # that came before it; a later message (seen late) still gets its item
                try:
                    named = {str(a) for a in json.loads(answers)}
                except (TypeError, ValueError):
                    named = set()
                before = _instant(answered_at)
                if str(event.get("object_id") or "") not in named and \
                        (before is None or at > before):
                    continue
            return True
        return False

    def rate_take(self, owner: str, hour: str, cap: int) -> bool:
        row = self.conn.execute("SELECT n FROM rate WHERE owner = ? AND hour = ?",
                                (owner, hour)).fetchone()
        if row and row[0] >= cap:
            return False
        self.conn.execute("INSERT INTO rate VALUES (?, ?, 1) ON CONFLICT(owner, hour) DO "
                          "UPDATE SET n = n + 1", (owner, hour))
        return True

    def take(self, item_id: str, pool: str, owner: str,
             readdress: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> bool:
        """A pooled open item becomes ``owner``'s own (one person's: the first to take it;
        False when it is not open in ``pool`` any more: taken already, done or gone).
        ``readdress``: the item's prepared work decided again for ``owner`` (its message's
        recipients under the taker's own rules), written in the same transaction."""
        if not pool.startswith(POOL_PREFIX) or not owner or owner.startswith(POOL_PREFIX):
            return False
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN IMMEDIATE")      # one taker reads and writes at a time
            row = self.conn.execute("SELECT body FROM items WHERE item_id = ? AND owner = ? "
                                    "AND state = 'open'", (item_id, pool)).fetchone()
            if row is None:
                return False
            body = json.loads(row[0])
            if readdress is not None:
                body = {**body, "prepared": readdress(body.get("prepared") or {})}
            self.conn.execute("UPDATE items SET owner = ?, body = ? WHERE item_id = ?",
                              (owner, json.dumps(body, sort_keys=True, default=str), item_id))
        return True

    def add_item(self, item: Mapping[str, Any]) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO items (item_id, event_id, autopilot, owner, title, body, "
            "audiences, markings, citations, due_at, created_at, state, prepare_ms, latency_s) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (item["item_id"], item["event_id"], item["autopilot"], item["owner"], item["title"],
             json.dumps(item["body"], sort_keys=True, default=str),
             json.dumps(sorted(item["audiences"])), json.dumps(sorted(item["markings"])),
             json.dumps(item["citations"]), item["due_at"], item["created_at"],
             item.get("prepare_ms"), item.get("latency_s")))
        return cur.rowcount == 1

    def items(self, owners: Iterable[str] | None = None, states: Sequence[str] = ("open",)
              ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM items WHERE state IN (SELECT value FROM json_each(?))"
        params: list[Any] = [json.dumps(list(states))]
        if owners is not None:
            sql += " AND owner IN (SELECT value FROM json_each(?))"
            params.append(json.dumps(sorted(set(owners))))
        out = []
        for row in self.conn.execute(sql + " ORDER BY due_at, item_id", params):
            item = dict(row)
            for key in ("body", "audiences", "markings", "citations"):
                item[key] = json.loads(item[key])
            out.append(item)
        return out

    def item(self, item_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM items WHERE item_id = ?", (item_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        for key in ("body", "audiences", "markings", "citations"):
            item[key] = json.loads(item[key])
        return item

    def settle_item(self, item_id: str, state: str, reason: str, now: str) -> None:
        self.conn.execute("UPDATE items SET state = ?, done_reason = ?, done_at = ? "
                          "WHERE item_id = ? AND state = 'open'", (state, reason, now, item_id))

    @staticmethod
    def draftless(item: Mapping[str, Any]) -> bool:
        """An open item that prepared nothing to act on: no reply text, no "your call" card,
        no entry, action or card of any kind."""
        work = (item.get("body") or {}).get("prepared") or {}
        if not isinstance(work, Mapping):
            return False
        for slot in ("draft", "teams_draft"):
            if isinstance(work.get(slot), Mapping) and (work[slot].get("body") or
                                                        work[slot].get("choice")):
                return False
        return not any(work.get(k) for k in ("your_call", "write", "action", "card",
                                              "actions", "order"))

    # what an item can carry for its person to read even with nothing to send: the records
    # or lines it found, a question with its options, the evidence and recommendation of
    # an approval, a note or facts. Such an item informs; it is never set aside as empty.
    INFO_SLOTS = ("records", "links", "lines", "question", "options", "evidence",
                  "recommendation", "note", "facts", "context")

    @classmethod
    def informs(cls, item: Mapping[str, Any]) -> bool:
        work = (item.get("body") or {}).get("prepared") or {}
        return isinstance(work, Mapping) and any(work.get(k) for k in cls.INFO_SLOTS)

    def supersede_draftless(self, now: str, apply: bool = False,
                            autopilots: Iterable[str] | None = None) -> dict[str, Any]:
        """Set the open draft-less items aside as state ``superseded`` (a state, never a
        delete: ``unsupersede`` puts them back open): only a reply notice with nothing to
        send and nothing to read (``informs``), of the ``autopilots`` given (the caller's
        reply autopilots). Counts only, per autopilot; nothing changes unless ``apply``."""
        only = set(autopilots) if autopilots is not None else None
        ids, per = [], {}
        for item in self.items():
            if self.draftless(item) and not self.informs(item) \
                    and (only is None or item["autopilot"] in only):
                ids.append(item["item_id"])
                per[item["autopilot"]] = per.get(item["autopilot"], 0) + 1
        if apply and ids:
            with self.conn:
                self.conn.executemany(
                    "UPDATE items SET state = 'superseded', done_reason = 'superseded', "
                    "done_at = ? WHERE item_id = ? AND state = 'open'",
                    [(now, i) for i in ids])
                self.set_meta("superseded", {"at": now, "n": len(ids), "by_autopilot": per})
        return {"applied": bool(apply), "items": len(ids), "by_autopilot": dict(sorted(per.items()))}

    def unsupersede(self, apply: bool = False) -> int:
        """Put every superseded item back open (only the ones this command set aside)."""
        n = self.conn.execute("SELECT COUNT(*) FROM items WHERE state = 'superseded' AND "
                              "done_reason = 'superseded'").fetchone()[0]
        if apply and n:
            with self.conn:
                self.conn.execute("UPDATE items SET state = 'open', done_reason = NULL, "
                                  "done_at = NULL WHERE state = 'superseded' AND "
                                  "done_reason = 'superseded'")
        return n

    def meta(self, key: str) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))


def killed(queue: Queue, environ: Mapping[str, str]) -> bool:
    """The kill switch: the environment flag or the file beside the queue."""
    return str(environ.get(KILL_ENV, "")).strip().lower() in ("1", "true", "yes", "on") \
        or queue.killed_file.exists()


def intents_of(subject: str, text: str) -> list[str]:
    """The message's intents (``INTENTS``) from its subject (not on a reply or forward's
    quoted subject alone) and its own text."""
    from . import proactive_threads as pt
    body = f"{subject or ''}.\n{text or ''}"
    found = [name for name, rx in INTENTS.items() if rx.search(body)]
    if "supplier_delay" in found and _NEGATED.search(body) and \
            len(INTENTS["supplier_delay"].findall(body)) <= len(_NEGATED.findall(body)):
        found.remove("supplier_delay")
    if "supplier_delay" in found and APOLOGY.search(body) and \
            len(INTENTS["supplier_delay"].findall(body)) <= len(APOLOGY.findall(body)):
        found.remove("supplier_delay")     # "sorry for the delay" is no delay notice
    if pt.approval_ask(text) or pt.approval_ask(subject):
        found.append("approval_request")
    return found
