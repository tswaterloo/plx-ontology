"""Autopilot: every new message, task, approval, QC result, meeting and record change becomes
a typed event, and each event an autopilot declares prepares the work, ready for one click.

* **Event feed** (``discover``): after each row-delta cycle (``ontology.delta``) the records
  the newest delta layer owns that this feed has not taken in (``layer_ids``), or for a
  replay the store's event index over a time window (``window_ids``), are read and typed by
  the registry's ``sources`` (``operating/autopilot_registry.json``): an inbound mail or
  one-to-one Teams message (``message.received``, with its classified ``intents`` and the
  sender's ``side``), a task assigned to someone (``task.assigned``), an order, PO,
  receipt, shipment or invoice new or changed (``record.changed``), a QC result
  (``qc.result``), an approval request (``approval.requested``) and a meeting that ended
  (``meeting.ended``). An event's id is the digest of its type, record and version
  (a message: its internet message id and mailbox), so a record taken in twice is one
  event; each event is processed once (``Queue``: insert-or-ignore, ``processed`` mark).
* **Registry** (``load_registry``): an autopilot is ``on`` (event type) + ``when``
  (conditions on the event's general fields) -> ``handler`` (one of ``HANDLERS``: general
  preparation, never per record) -> ``owner`` (``OWNER_RULES``) -> ``due_minutes``. An
  unknown event type, handler, business rule or condition refuses the whole registry
  (``RegistryInvalid``): nothing runs on a registry this code does not understand.
* **Prepared work** (``Queue`` items): what happened, the prepared work (answer, drafted
  reply, dry-run write intent spec, impacted records, recommendation), the evidence (cited
  handles), the audiences of every record read and the markings. The queue is per person:
  ``visible`` shows an item only to its owner and to a manager the owner granted the
  owner's team (``inbox_triage.WorkloadGrants``), and only when that reader may read every
  audience, holds every marking and may open every cited handle (fail closed). ``settle``
  marks items done from evidence (the owner replied in the conversation, the approval was
  decided, the task closed, the lot released). ``digest`` gives a person's daily digest and
  nudges for items past due.
* **Approve** never sends or writes here: an item's draft is a card (``card_of``) that
  ``operator_api`` routes through ``card_actions.ActionDesk`` (preview, the owner's confirm,
  the people exclusion, the live-send gate); a write spec becomes a
  ``write_intents.WriteDesk`` intent prepared by the autopilot (dry run until the owner's
  flags and Vince's write scopes exist, and approved by a person who did not prepare it).

Safety: the people exclusion is read first and fails closed (unavailable: the cycle is
``blocked``; a mailbox, owner or participant it does not clear: no item); mail work is
prepared only for the mailbox's owner (and To, not Cc) or a role owner; per-owner hourly
rate limit (``limits.per_owner_per_hour``: the rest wait for the next cycle); every handler
runs under a deadline (``limits.handler_seconds``, SQLite progress handler); kill switch
(``KILL_ENV`` or the ``KILL_FILE`` beside the queue): nothing is read or prepared, nothing is
approved. Reads only the store and delta layers; writes only its own queue file.

Run::

    python3 -m operating.autopilot follow --store STORE --queue QUEUE
    python3 -m operating.autopilot replay --store STORE --queue QUEUE --days 7 [--layer L]
"""

from __future__ import annotations

import argparse
import contextlib
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from .citation_schemes import HANDLE_PREFIX
from operating import declared_schema as _schemas
from operating.declared_schema import autopilot_queue as _AUTOPILOT_QUEUE

SCHEMA = "plx-autopilot/v1"
REGISTRY_FILE = Path(__file__).with_name("autopilot_registry.json")
KILL_ENV = "PLX_AUTOPILOT_OFF"
KILL_FILE = "autopilot.off"
ROLES_ENV = "PLX_AUTOPILOT_ROLES_FILE"
ROLES_FILE = "autopilot-roles.json"
QUEUE_FILE = "autopilot.sqlite"
CARD_PREFIX = "autopilot:"
EVENT_TYPES = ("message.received", "task.assigned", "record.changed", "qc.result",
               "approval.requested", "meeting.ended", "record.due", "question.asked")
OWNER_RULES = ("recipient", "assignee", "approvers", "organizer", "buyer", "addressee",
               "hinted")
ROLE_OWNER = re.compile(r"^role:[a-z_]{1,40}$")
CONDITIONS = ("intents_any", "intents_none", "side_in", "status_in", "assigned", "open", "asks",
              "quality_in", "tested", "ended", "organizer_copy", "external_or_linked",
              "mailbox_role", "mailbox_role_none", "engaged", "correspondent", "engaged_or_asks",
              "answers_sender", "closing", "addressed_other", "quiet_sender", "object_type_in",
              "to_me", "after_own_post", "cc_min")
DONE_RULES = ("owner_replied", "approval_decided", "task_closed", "batch_released",
              "owner_mailed_attendees", "question_answered")
MAX_RECORDS = 12          # business records one handler reads around its event
MAX_LINES = 50            # order lines one availability check reads
# a handler that failed or ran out of time on an event: the event is deferred and tried
# again by the next cycles, at most this many tries, then left ``failed`` (shown, never
# silently processed)
HANDLER_TRIES = 3
SUMMARY_CHARS = 160
WORK_LOG_ENV = "PLX_WORK_LOG_DIR"  # where the Workbench keeps its work log
REPLY_HOURS = 72          # an answer counts within this many hours (the replay's horizon)
TEXT_CHARS = 4000

EMAIL_PROPS = ("subject", "sender", "to_addresses", "cc_addresses", "received_at", "sent_at",
               "conversation_id", "internet_message_id", "body_preview", "mailbox")
BUSINESS = ("SalesOrder", "PurchaseOrder", "SalesInvoice", "Shipment", "Receipt", "Batch",
            "CustomerPurchaseOrder")
SIDE_LINKS = {"Email.sent_by_customer": "customer", "Email.sent_by_supplier": "supplier"}
SO_LINE_OF, SO_LINE_ITEM = "SalesOrderLine.line_of", "SalesOrderLine.for_item"
PO_LINE_OF, PO_LINE_ITEM = "PurchaseOrderLine.line_of", "PurchaseOrderLine.for_item"
CLOSED = re.compile(r"\b(?:closed|complete[d]?|cancel+ed|void|shipped|invoiced|fully|done|"
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


# ----------------------------------------------------------------------------- registry

def load_registry(path: Path | str | None = None) -> dict[str, Any]:
    """The registry, validated whole (module doc); anything unknown refuses it."""
    raw = json.loads(Path(path or REGISTRY_FILE).read_text(encoding="utf-8"))
    return validate_registry(raw)


def validate_registry(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or raw.get("schema") != "plx-autopilot-registry/v1":
        raise RegistryInvalid("registry_schema")
    sources = raw.get("sources")
    if not isinstance(sources, Mapping) or not sources:
        raise RegistryInvalid("registry_sources")
    for otype, spec in sources.items():
        if not isinstance(spec, Mapping) or spec.get("event") not in EVENT_TYPES:
            raise RegistryInvalid(f"registry_source_event:{otype}")
    seen: set[str] = set()
    for ap in raw.get("autopilots") or ():
        if not isinstance(ap, Mapping) or not re.fullmatch(r"[a-z_]{2,40}", str(ap.get("id"))):
            raise RegistryInvalid("registry_autopilot_id")
        if ap["id"] in seen:
            raise RegistryInvalid(f"registry_autopilot_duplicate:{ap['id']}")
        seen.add(ap["id"])
        if ap.get("on") not in EVENT_TYPES:
            raise RegistryInvalid(f"registry_event:{ap['id']}")
        if ap.get("handler") not in HANDLERS:
            raise RegistryInvalid(f"registry_handler:{ap['id']}")
        owner = ap.get("owner")
        if owner not in OWNER_RULES and not ROLE_OWNER.match(str(owner)):
            raise RegistryInvalid(f"registry_owner:{ap['id']}")
        when = ap.get("when") or {}
        if not isinstance(when, Mapping) or set(when) - set(CONDITIONS):
            raise RegistryInvalid(f"registry_condition:{ap['id']}")
        quiet = when.get("quiet_sender")
        if quiet is not None and not (
                isinstance(quiet, list) and len(quiet) == 3 and isinstance(quiet[0], int)
                and quiet[0] >= 1 and isinstance(quiet[1], (int, float))
                and 0 < quiet[1] <= 1 and isinstance(quiet[2], list)
                and set(quiet[2]) <= {"internal", "customer", "supplier", "unknown"}):
            raise RegistryInvalid(f"registry_condition:{ap['id']}")
        share = when.get("answers_sender")
        if share is not None and not (
                isinstance(share, list) and len(share) == 2 and isinstance(share[0], int)
                and share[0] >= 1 and isinstance(share[1], (int, float))
                and 0 < share[1] <= 1):
            raise RegistryInvalid(f"registry_condition:{ap['id']}")
        turn = when.get("after_own_post")
        if turn is not None and not (
                isinstance(turn, list) and len(turn) == 2
                and all(isinstance(x, int) and not isinstance(x, bool) and x >= 1
                        for x in turn)):
            raise RegistryInvalid(f"registry_condition:{ap['id']}")
        cc_min = when.get("cc_min")
        if cc_min is not None and not (isinstance(cc_min, int) and not isinstance(cc_min, bool)
                                       and cc_min >= 1):
            raise RegistryInvalid(f"registry_condition:{ap['id']}")
        if ap.get("done_when") not in DONE_RULES:
            raise RegistryInvalid(f"registry_done:{ap['id']}")
        if not isinstance(ap.get("due_minutes"), int) or ap["due_minutes"] <= 0:
            raise RegistryInvalid(f"registry_due:{ap['id']}")
        for flag in ("enabled", "fallback"):
            if flag in ap and not isinstance(ap[flag], bool):
                raise RegistryInvalid(f"registry_{flag}:{ap['id']}")
        if ap["on"] == "record.due" and not isinstance(ap.get("params") or {}, Mapping):
            raise RegistryInvalid(f"registry_params:{ap['id']}")
        marks = ap.get("markings") or []
        if not isinstance(marks, list) or not all(isinstance(m, str) for m in marks):
            raise RegistryInvalid(f"registry_markings:{ap['id']}")
    limits = raw.get("limits") or {}
    for key in ("per_owner_per_hour", "handler_seconds", "cycle_seconds",
                "max_events_per_cycle"):
        if not isinstance(limits.get(key), (int, float)) or limits[key] <= 0:
            raise RegistryInvalid(f"registry_limit:{key}")
    return dict(raw)


def matches(when: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    """Whether an event meets an autopilot's conditions (each a general field test)."""
    intents = set(event.get("intents") or ())
    for key, want in when.items():
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
                   "addressed_other", "to_me") and \
                bool(event.get(key)) != bool(want):
            return False
        if key == "answers_sender":
            # [n, share]: the owner answered at least ``share`` of the sender's last
            # messages, ``n`` or more of them known
            n, answered = (list(event.get("sender_history") or ()) + [0, 0])[:2]
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
        if key == "cc_min":
            # at least ``want`` addresses copied on the message (an unreadable list: no)
            cc = event.get("cc")
            if not isinstance(cc, list) or len(cc) < want:
                return False
            continue
        if key == "engaged_or_asks" and bool(event.get("engaged") or (
                event.get("asks") and event.get("correspondent"))) != bool(want):
            return False
    return True


# ----------------------------------------------------------------------------- queue

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
                      horizon_hours: int = REPLY_HOURS, days: int = 60) -> tuple[int, int]:
        """(messages from ``sender`` to ``owner`` whose answer window closed before ``at``,
        how many of them the owner answered in the thread within ``horizon_hours``): known
        at ``at``, never later evidence."""
        start = _instant(at)
        if start is None or not sender:
            return 0, 0
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

    def meta(self, key: str) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))


def killed(queue: Queue, environ: Mapping[str, str]) -> bool:
    """The kill switch: the environment flag or the file beside the queue."""
    return str(environ.get(KILL_ENV, "")).strip().lower() in ("1", "true", "yes", "on") \
        or queue.killed_file.exists()


# ----------------------------------------------------------------------------- reading

def _digest(*parts: Any) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:32]


def _read(st: Any, ids: Iterable[str], props: Sequence[str]) -> dict[str, dict[str, Any]]:
    from . import proactive_threads as pt
    ids = sorted(set(ids))
    return dict(pt._read(st, ids, props)) if ids else {}


def _v(row: Mapping[str, Any] | None, prop: str) -> str:
    cell = (row or {}).get(prop) or {}
    value = cell.get("value") if isinstance(cell, Mapping) else None
    return "" if value is None else str(value)


def _types(st: Any, ids: Iterable[str]) -> dict[str, tuple[str, str]]:
    """object -> (type, audience) of current, not removed records."""
    from ontology import store as ostore
    out: dict[str, tuple[str, str]] = {}
    ids = sorted(set(ids))
    for start in range(0, len(ids), 500):
        for oid, otype, aud in st.conn.execute(
                "SELECT o.object_id, o.object_type, o.audience_key FROM objects o WHERE "
                "o.object_id IN (SELECT value FROM json_each(?)) AND NOT "
                + ostore.removed_at_sql("o.object_id"),
                (json.dumps(ids[start:start + 500]),)):
            out[oid] = (otype, aud)
    return out


def _linked(st: Any, oid: str, types: Sequence[str] | None = None) -> list[tuple[str, str]]:
    """(link type, other end) of the current links at ``oid``."""
    out = []
    for row in st.touching([oid], types):
        link_type, a, b = row[2], row[3], row[4]
        out.append((link_type, b if a == oid else a))
    return out


def _version_digest(st: Any, oid: str) -> str:
    rows = st.conn.execute("SELECT digest FROM object_versions WHERE object_id = ? ORDER BY "
                           "digest", (oid,)).fetchall()
    return _digest(*[r[0] for r in rows])


def _instant(raw: Any) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        at = datetime.fromisoformat(text[:32]) if "T" in text else \
            datetime.fromisoformat(text[:10] + "T00:00:00+00:00")
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _addresses(raw: Any) -> list[str]:
    text = str(raw or "")
    return sorted({a.lower() for a in re.findall(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", text)})


def _internal(address: str, domains: Sequence[str]) -> bool:
    return any(address.lower().endswith("@" + d.lower()) for d in domains)


def _decimal(raw: Any) -> Decimal | None:
    try:
        return Decimal(str(raw)) if raw not in (None, "") else None
    except InvalidOperation:
        return None


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


# ----------------------------------------------------------------------------- events

def _people_maps(people: Any) -> tuple[dict[str, str], dict[str, set[str]], tuple[str, ...]]:
    from . import proactive_threads as pt
    directory = {str(a).lower(): str(o).lower()
                 for a, o in (getattr(people, "directory", None) or {}).items()}
    addresses_of: dict[str, set[str]] = defaultdict(set)
    for address, oid in directory.items():
        addresses_of[oid].add(address)
    return directory, addresses_of, tuple(pt._domains(people))


def window_ids(st: Any, registry: Mapping[str, Any], since: str, until: str) -> list[str]:
    """The records the store's event index names in [since, until) (a replay's feed)."""
    kinds = list(registry.get("store_events") or ())
    # the events first (CROSS JOIN keeps that order): the store has no time index, and the
    # planner's choice, every event-object pair by object, read about 7 times as much
    # (measured on the 2026-11 store: 165 s against 24 s for one day, the same records)
    rows = st.conn.execute(
        "SELECT DISTINCT eo.object_id FROM events e CROSS JOIN event_objects eo ON "
        "eo.event_id = e.event_id WHERE e.event_type IN (SELECT value FROM json_each(?)) AND "
        "e.time >= ? AND e.time < ?", (json.dumps(kinds), since, until)).fetchall()
    ids = {r[0] for r in rows}
    # a task event names its task: the task is the record the feed types
    tasks = [o for o in ids if o.startswith("MCTaskEvent:")]
    if tasks:
        for oid in tasks:
            ids.update(other for _t, other in _linked(st, oid) if other.startswith("MCTask:"))
    ids.update(_value_window(st, registry, since, until))
    return sorted(ids)


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


def layer_ids(store: Path, queue: Queue, stamp: str | None,
              cap: int | None = None) -> tuple[set[str], list[str], int]:
    """(the records the served delta layers own that this feed has not taken in yet, those
    layers, how many records of them were taken in unread). Only from a store that holds
    them: Postgres (``stamp`` its ``<digest>@<data version>``, the generation is the part
    before the ``@``), and there only the layers it has applied (a layer it has not is
    taken in by a later cycle). The SQLite generation file alone holds none of the layers'
    records: no layer is taken in, since one taken in unread would lose its events. A layer
    that would bring the records read past ``cap`` (by the size the pointer states) is
    taken in without being opened (the window feed covers it, ``follow_ids``); one whose
    size it does not state is read only as the cycle's first layer."""
    from . import delta_overlay
    from .proactive_incremental import _applied, generation_digest
    applied = _applied(store)
    if applied is None:
        return set(), [], 0
    digest = generation_digest(stamp)
    try:
        raw = json.loads(delta_overlay.pointer_path(store).read_text())
    except (OSError, ValueError):
        return set(), [], 0
    if not isinstance(raw, dict) or not digest or raw.get("base_digest") != digest:
        return set(), [], 0
    layers = [(str(x["store"]), x.get("owned")) for x in raw.get("layers") or ()
              if isinstance(x, dict) and x.get("store")]
    if not layers and raw.get("store"):
        layers = [(str(raw["store"]), None)]
    seen = {row[0] for row in queue.conn.execute(
        "SELECT layer FROM layers WHERE base_digest = ?", (digest,))}
    owned: set[str] = set()
    taken: list[str] = []
    unread = read = 0
    for layer, size in layers:
        if layer in seen or Path(layer).name not in applied:
            continue
        known = isinstance(size, int)
        if cap is not None and (read + size > cap if known else bool(taken)):
            taken.append(layer)
            unread += size if known else 0
            continue
        try:
            with contextlib.closing(sqlite3.connect(f"file:{layer}?mode=ro", uri=True)) as c:
                owned.update(r[0] for r in c.execute("SELECT object_id FROM delta_owned"))
        except sqlite3.Error:
            continue     # pruned or unreadable now: a later layer holds its records
        taken.append(layer)
        read += size if known else len(owned)
    return owned, taken, unread


# Live follow's feed (``follow_ids``). The served delta layers each own every record changed
# since the generation (2026-11-13: 0.66-0.8 M records a layer), so typing a layer's records
# every cycle is not a feed: above ``FOLLOW_LAYER_CAP`` a layer is taken in unopened and the
# feed is the store's event index over the last ``FOLLOW_HORIZON``, the very feed the replay
# gate scores (``window_ids``). A cycle reads the whole horizon whenever the store changed
# since the last one (its content stamp: Postgres's moves with every applied layer), so a
# record that lands up to the horizon after its own time is still seen (an event read twice
# is one event); a store unchanged holds nothing new and is not read again (a re-read of the
# horizon took 141 s of the cycle's 240 s handling budget on 2026-11-13, every 5 minutes).
# A message or meeting older than the horizon is not prepared (``run_cycle``
# ``horizon``): the replay judges an answer within ``REPLY_HOURS``, and a first cycle would
# otherwise prepare a week of answered mail.
FOLLOW_LAYER_CAP = 50_000
FOLLOW_HORIZON = timedelta(hours=REPLY_HOURS)
HORIZON_EVENTS = ("message.received", "meeting.ended")


def follow_ids(st: Any, store: Path, queue: Queue, registry: Mapping[str, Any],
               now: datetime) -> tuple[set[str], list[str], dict[str, Any]]:
    """(the records one live cycle types, the layers it takes in, what it read)."""
    ids, taken, unread = layer_ids(store, queue, st.digest, FOLLOW_LAYER_CAP)
    read: dict[str, Any] = {"layers": len(taken), "layer_records": len(ids),
                            "layer_records_unread": unread}
    if st.digest and queue.meta("follow_stamp") == st.digest:
        read["window"], read["window_records"] = None, 0     # the store is unchanged
        return set(ids), taken, read
    since = now - FOLLOW_HORIZON
    window = window_ids(st, registry, _iso(since), _iso(now))
    read["window"], read["window_records"] = [_iso(since), _iso(now)], len(window)
    return set(ids) | set(window), taken, read


def discover(st: Any, ids: Iterable[str], registry: Mapping[str, Any], people: Any, *,
             landed_at: str | None = None, outbound: list | None = None,
             now: datetime | None = None, feed_lag: Mapping[str, timedelta] | None = None
             ) -> tuple[list[dict[str, Any]], Counter]:
    """Typed events of the changed records ``ids`` (module doc); ``outbound`` collects the
    mailbox owners' own sent messages (thread, recipients, time) for ``Queue.note_sent``.
    ``feed_lag`` (a replay of past days): per record type, how long after its time the live
    feed first holds it (the capture's cadence); a decision taken before then was already
    taken when the feed saw the request, so no item could have been prepared for it."""
    sources = registry["sources"]
    typed = _types(st, ids)
    by_type: dict[str, list[str]] = defaultdict(list)
    skipped: Counter = Counter()
    for oid, (otype, _aud) in typed.items():
        if otype in sources:
            by_type[otype].append(oid)
    directory, addresses_of, domains = _people_maps(people)
    events: list[dict[str, Any]] = []
    for otype, oids in sorted(by_type.items()):
        kind = sources[otype]["event"]
        if otype == "Email":
            events += _mail_events(st, oids, typed, people, directory, addresses_of, domains,
                                   skipped, outbound)
        elif otype in TEAMS_TYPES:
            events += _teams_events(st, oids, typed, people, directory, skipped, outbound,
                                    unaddressed=True)
        else:
            events += _record_events(st, otype, kind, oids, typed, sources[otype], skipped,
                                     now=now, lag=(feed_lag or {}).get(otype))
    for e in events:
        e["landed_at"] = landed_at
    return events, skipped


def _mail_events(st: Any, oids: Sequence[str], typed: Mapping[str, tuple[str, str]],
                 people: Any, directory: Mapping[str, str],
                 addresses_of: Mapping[str, set[str]], domains: Sequence[str],
                 skipped: Counter, outbound: list | None = None) -> list[dict[str, Any]]:
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
                _party_side(st, sender, domains)
        records = sorted({o for _t, o in links if o.split(":", 1)[0] in BUSINESS})
        message_id = _v(row, "internet_message_id") or oid
        at = _v(row, "received_at") or _v(row, "sent_at")
        out.append({
            "event_id": _digest("message.received", message_id, mailbox),
            "event_type": "message.received", "object_id": oid, "object_type": "Email",
            "channel": "mail", "at": at, "mailbox": mailbox, "audience": aud,
            "sender": sender, "to": to, "cc": cc, "to_me": bool(mine & set(to)),
            "subject": subject[:200], "text": text[:TEXT_CHARS],
            "conversation_id": _v(row, "conversation_id"), "message_id": message_id,
            "intents": intents_of(subject, text), "side": side, "records": records[:MAX_RECORDS],
            "asks": bool(it.reply_ask(text) or it.direct_question(text)),
            "closing": closing(text),
            "addressed_other": addressed_other(text, mine, [*to, *cc]),
            "cleared": cleared,
            "handles": [c["l1_handle"] for c in row.values() if c.get("l1_handle")][:3]})
    return out


TEAMS_TYPES = ("TeamsChatMessage", "TeamsChannelMessage")
# how far back ``Queue.own_posts`` counts a person's own messages in a conversation
OWN_POSTS_DAYS = 14
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
    out = []
    for oid in oids:
        aud = typed[oid][1]
        channel = aud.startswith("teams:channel:")
        if not (channel or aud.startswith("teams:chat:")) or not people.unit_clear(aud):
            skipped["teams_not_chat_or_not_cleared"] += 1
            continue
        row = rows.get(oid) or {}
        sender = teams_sender(_v(row, "from"), directory)
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
                "sender": sender, "to": [owner], "cc": [], "to_me": owner in owners,
                **({} if owner in owners else {"unaddressed": True}), "subject": "",
                "text": text[:TEXT_CHARS], "conversation_id": conversation, "message_id": oid,
                "intents": intents_of("", text), "side": "internal",
                "records": records[:MAX_RECORDS], "group": group, "cleared": cleared,
                "asks": bool(it.reply_ask(text) or it.direct_question(text)),
                "closing": closing(text, chat=True), "handles": _handles_of(row)[:3]})
    return out


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


RECORD_PROPS = {
    "MCTask": ("task_id", "title", "description", "stage", "assignee", "accountable_owner",
               "updated_at", "created_at"),
    "PlannerTask": ("title", "description", "assignees", "completed_at", "due_at",
                    "percent_complete", "created_at", "plan_title"),
    "MCApproval": ("gate_id", "task_id", "status", "reason", "requested_by", "requested_at",
                   "decided_by", "decided_at", "scope"),
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
                 "fields": {p: _v(row, p)[:300] for p in props if p and _v(row, p)}}
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


# ----------------------------------------------------------------------------- owners

def _roles(environ: Mapping[str, str], queue_dir: Path) -> dict[str, list[str]]:
    path = Path(environ.get(ROLES_ENV) or queue_dir / ROLES_FILE)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    roles = raw.get("roles") if isinstance(raw, Mapping) else None
    if not isinstance(roles, Mapping):
        return {}
    oid = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    return {str(k): [str(x).lower() for x in v if oid.match(str(x).lower())]
            for k, v in roles.items() if isinstance(v, list)}


def _role_members(role: str, roles: Mapping[str, list[str]],
                  directory: Mapping[str, str]) -> set[str]:
    """A role's people (the roles file); the owner role falls back to the approver roster."""
    return set(roles.get(role) or (_approvers(directory) if role == "owner" else ()))


def _approvers(directory: Mapping[str, str]) -> list[str]:
    path = Path(__file__).with_name("approver_roster.json")
    try:
        roster = json.loads(path.read_text(encoding="utf-8")).get("approvers") or {}
    except (OSError, ValueError):
        return []
    out = []
    for address, who in roster.items():
        oid = (who or {}).get("oid") or directory.get(str(address).lower())
        if oid:
            out.append(str(oid).lower())
    return sorted(set(out))


DECIDER_DAYS = 30


def mc_project(audience: str) -> str:
    """An MC record's project: its audience unit's last segment
    (``mc:<scope>/approvals/<project>``); none: ""."""
    head, _, rest = str(audience or "").partition(":")
    return rest.rsplit("/", 1)[-1] if head == "mc" and "/" in rest else ""


def deciders(st: Any, directory: Mapping[str, str], now: datetime, *,
             days: int = DECIDER_DAYS, project: str | None = None) -> set[str]:
    """The people (directory ids) who decided an approval in the ``days`` before ``now``;
    ``project``: in that MC project only. A decider the directory does not know is no one
    (never a guess). The records are read once per store connection."""
    cache = st.__dict__.setdefault("_autopilot_decisions", None)
    if cache is None:
        rows = st.conn.execute(
            "SELECT object_id, audience_key FROM objects WHERE object_type = 'MCApproval'"
        ).fetchall()
        units = {r[0]: r[1] for r in rows}
        cache = []
        for oid, r in _read(st, list(units), ("decided_by", "decided_at")).items():
            at, by = _instant(_v(r, "decided_at")), _v(r, "decided_by").lower()
            if at is not None and by in directory:
                cache.append((at, directory[by], mc_project(units.get(oid, ""))))
        st.__dict__["_autopilot_decisions"] = cache
    since = now - timedelta(days=days)
    return {who for at, who, proj in cache
            if since <= at < now and (project is None or proj == project)}


def owners_of(rule: str, event: Mapping[str, Any], *, directory: Mapping[str, str],
              addresses_of: Mapping[str, set[str]], roles: Mapping[str, list[str]],
              decided: set[str] | None = None) -> tuple[list[str], str | None]:
    """(owner oids, None) or ([], the reason no owner is known: no item). ``decided``: who
    decided approvals in the event's MC project in the 30 days before it (``deciders``):
    they get an approval; none: every approver."""
    if rule == "recipient":
        if event.get("event_type") != "message.received":
            return [], "owner_rule_mismatch"
        owner = str(event.get("mailbox") or "")
        if not owner or not (addresses_of.get(owner) or event.get("channel") == "teams"):
            return [], "owner_unknown"
        if not event.get("to_me") and not event.get("unaddressed"):
            return [], "owner_not_addressed"     # Cc or Bcc: the To owner prepares it
        return [owner], None
    if rule == "assignee":
        found = []
        for a in event.get("assignees") or ():
            oid = a if re.fullmatch(r"[0-9a-f-]{36}", a) else directory.get(a)
            if oid:
                found.append(oid)
        return (sorted(set(found)), None) if found else ([], "assignee_unknown")
    if rule == "approvers":
        found = sorted(decided) if decided else _approvers(directory)
        # a request goes to someone other than the person who asked for it whenever anyone
        # else can decide: the project's other recent deciders, else the other approvers
        # (3 of them in projects only he had decided in); the asker keeps it only
        # when no one else can decide. An asker the directory does not know changes nothing.
        asker = str(directory.get(str((event.get("fields") or {}).get("requested_by") or "")
                                  .strip().lower()) or "").lower()
        if asker and asker in {o.lower() for o in found}:
            others = [o for o in found if o.lower() != asker] or [
                o for o in _approvers(directory) if o.lower() != asker]
            found = others or found
        return (found, None) if found else ([], "approvers_unknown")
    if rule == "addressee":
        if event.get("event_type") != "question.asked":
            return [], "owner_rule_mismatch"
        owner = handle_person(str(event.get("addressee") or ""), directory,
                              event.get("addressee_domains") or ())
        return ([owner], None) if owner else ([], "addressee_unknown")
    if rule == "organizer":
        owner = str(event.get("mailbox") or "")
        if not owner or directory.get(str(event.get("organizer") or "")) != owner:
            return [], "not_organizer_copy"
        return [owner], None
    if rule == "buyer":
        hint = str(event.get("owner_hint") or "")
        if hint and hint in addresses_of:
            return [hint], None                   # who sent the PO to the supplier
        found = roles.get("purchasing") or []
        return (found, None) if found else ([], "buyer_unknown")
    if rule == "hinted":
        # the person the records show doing this work for this party (``owner_hint``),
        # else nobody: never a whole role for one record
        hint = str(event.get("owner_hint") or "")
        return ([hint], None) if hint and hint in addresses_of else ([], "owner_unknown")
    if rule.startswith("role:"):
        found = roles.get(rule[len("role:"):]) or _approvers(directory)
        return (found, None) if found else ([], "role_unassigned")
    return [], "owner_rule_unknown"


# ----------------------------------------------------------------------------- handlers

class Ctx:
    """What a handler reads with: the store, the run's day, the deadline; every record it
    reads is noted (``objects``) so the item carries their audiences."""

    def __init__(self, st: Any, as_of: date, deadline: float, domains: Sequence[str]) -> None:
        self.st, self.as_of, self.deadline, self.domains = st, as_of, deadline, domains
        self.objects: set[str] = set()
        self.handles: list[str] = []
        self.capture: Any = getattr(st, "autopilot_capture", None)

    def check(self) -> None:
        if time.monotonic() > self.deadline:
            raise HandlerTimeout()

    def read(self, ids: Iterable[str], props: Sequence[str]) -> dict[str, dict[str, Any]]:
        self.check()
        ids = [i for i in ids if i]
        found = _read(self.st, ids, props)
        self.objects.update(found)
        return found

    def linked(self, oid: str, types: Sequence[str] | None = None) -> list[str]:
        self.check()
        return [o for _t, o in _linked(self.st, oid, types)]

    def linked_many(self, oids: Iterable[str], types: Sequence[str] | None = None) -> list[str]:
        """The other ends of the current links at any of ``oids``."""
        self.check()
        oids = sorted(set(oids))
        if not oids:
            return []
        out = []
        for row in self.st.touching(oids, types):
            a, b = row[3], row[4]
            out.append(b if a in oids else a)
        return list(dict.fromkeys(out))

    def touching(self, oids: Iterable[str], types: Sequence[str] | None = None
                 ) -> list[tuple[str, str, str]]:
        """(link type, from, to) of the current links at any of ``oids``."""
        self.check()
        oids = sorted(set(oids))
        return [(row[2], row[3], row[4]) for row in self.st.touching(oids, types)] if oids \
            else []

    def cite(self, row: Mapping[str, Any] | None, *props: str) -> None:
        for p in props or tuple((row or {}).keys()):
            h = ((row or {}).get(p) or {}).get("l1_handle")
            if h and h not in self.handles:
                self.handles.append(h)

    def find(self, prefix: str, prop: str, value: str) -> list[str]:
        """The records of ``prefix`` whose ``prop`` is exactly ``value`` (word index)."""
        from . import proactive_threads as pt
        self.check()
        words = [w for w in re.split(r"[^0-9A-Za-z]+", value) if w]
        if not words:
            return []
        hits = pt._word_ids(self.st, " AND ".join('"' + w + '"' for w in words), [prop],
                            prefix + ":%") or set()
        rows = _read(self.st, sorted(hits)[:50], [prop])
        norm = re.sub(r"[^0-9a-z]", "", value.lower())
        return sorted(o for o, r in rows.items()
                      if re.sub(r"[^0-9a-z]", "", _v(r, prop).lower()) == norm)


TOKEN = re.compile(r"(?<![\w-])([A-Za-z]{0,6}-?[A-Za-z0-9]*\d[A-Za-z0-9-]*)(?![\w-])")
ID_KINDS_SKIPPED = ("item_code",)          # an item code names a product, not a transaction
ID_TYPES = ("SalesOrder", "PurchaseOrder", "SalesInvoice", "Shipment", "Receipt", "Batch",
            "CustomerPurchaseOrder")
MAX_PER_TOKEN = 3


def identified(ctx: "Ctx", *texts: str) -> list[str]:
    """The transaction records the texts name by an identifier the store holds (the one
    identifier table: customer PO, order, PO, invoice, shipment, lot numbers), a customer
    PO resolved to its orders; a token naming more than ``MAX_PER_TOKEN`` records is
    ambiguous and left out (conservative identity)."""
    tokens = []
    for t in texts:
        for m in TOKEN.finditer(t or ""):
            tok = m.group(1).strip("-")
            if 4 <= len(tok) <= 25 and sum(c.isdigit() for c in tok) >= 3 and \
                    not re.fullmatch(r"(?:19|20)\d{2}(?:-\d{1,2}){0,2}", tok):
                tokens.append(tok)
    tokens = list(dict.fromkeys(tokens))[:20]
    if not tokens:
        return []
    ctx.check()
    wanted = sorted({x for t in tokens for x in (t, t.upper(), t.lower())})
    rows = ctx.st.conn.execute(
        "SELECT normalized, identifier_kind, object_id FROM identifiers WHERE normalized IN "
        "(SELECT value FROM json_each(?))", (json.dumps(wanted),)).fetchall()
    by_token: dict[str, set[str]] = defaultdict(set)
    for norm, kind, oid in rows:
        if kind not in ID_KINDS_SKIPPED and oid.split(":", 1)[0] in ID_TYPES:
            by_token[norm.upper()].add(oid)
    out: list[str] = []
    for tok in tokens:
        found = by_token.get(tok.upper(), set())
        orders = set()
        for oid in sorted(found):
            if oid.startswith("CustomerPurchaseOrder:"):
                orders.update(o for o in ctx.linked(oid, ("SalesOrder.references_customer_po",))
                              if o.startswith("SalesOrder:"))
            else:
                orders.add(oid)
        if 0 < len(orders) <= MAX_PER_TOKEN:
            out.extend(sorted(orders))
    return list(dict.fromkeys(out))[:MAX_RECORDS]


def _numbers(rx: re.Pattern[str], *texts: str) -> list[str]:
    out: list[str] = []
    for t in texts:
        for m in rx.finditer(t or ""):
            if m.group(1) not in out and any(c.isdigit() for c in m.group(1)):
                out.append(m.group(1))
    return out[:5]


def _greeting(sender: str) -> str:
    local = sender.split("@", 1)[0].split(".")[0]
    return f"Hi {local.capitalize()}," if local.isalpha() and len(local) > 1 else "Hello,"


def _reply(event: Mapping[str, Any], lines: Sequence[str]) -> dict[str, Any]:
    subject = event.get("subject") or ""
    if subject and not re.match(r"^\s*re\s*:", subject, re.I):
        subject = "RE: " + subject
    body = "\n".join([_greeting(event.get("sender") or ""), "", *lines, "",
                      "Best regards,"])
    return {"channel": event.get("channel") or "email", "to": [{"address": event.get("sender")}],
            "subject": subject[:200], "body": body, "in_reply_to": event.get("object_id")}


def _order_facts(ctx: Ctx, order: str) -> dict[str, Any]:
    row = ctx.read([order], ("order_number", "customer_po", "status", "required_date",
                             "order_date")).get(order) or {}
    ctx.cite(row, "order_number", "status")
    facts: dict[str, Any] = {"object_id": order, "order_number": _v(row, "order_number"),
                             "customer_po": _v(row, "customer_po"), "status": _v(row, "status"),
                             "required_date": _v(row, "required_date")[:10]}
    ships = ctx.linked(order, ("Shipment.ships",))[:5]
    srows = ctx.read(ships, ("shipment_number", "ship_date", "carrier", "tracking_number",
                             "status"))
    facts["shipments"] = [{"object_id": s, "number": _v(r, "shipment_number"),
                           "ship_date": _v(r, "ship_date")[:10], "carrier": _v(r, "carrier"),
                           "tracking": _v(r, "tracking_number")} for s, r in sorted(srows.items())]
    for r in srows.values():
        ctx.cite(r, "ship_date", "tracking_number")
    invs = ctx.linked(order, ("SalesInvoice.bills",))[:5]
    irows = ctx.read(invs, ("invoice_number", "bc_number", "invoice_date"))
    facts["invoices"] = [{"object_id": i, "number": _v(r, "invoice_number") or _v(r, "bc_number"),
                          "date": _v(r, "invoice_date")[:10]} for i, r in sorted(irows.items())]
    return facts


def _qty_unit(row: Mapping[str, Any] | None, *props: str) -> str | None:
    """The folded unit the first stated of ``props`` is in (its cell's unit, else the
    row's ``uom`` text); None when no unit is stated."""
    from ontology.units import unit_token
    for prop in props:
        cell = (row or {}).get(prop)
        if isinstance(cell, Mapping) and cell.get("value") not in (None, ""):
            unit = unit_token(cell.get("unit")) if cell.get("unit") else None
            if unit:
                return unit
    return unit_token(_v(row, "uom")) if _v(row, "uom") else None


def supply_flows(ctx: Ctx, po_lines: Sequence[str], unit: str | None
                 ) -> tuple[list[tuple[date, Decimal, str]], str | None]:
    """The open purchase order supply of these PO lines: (day, quantity due, line), every
    line read with its order's header (a closed or cancelled order supplies nothing,
    whatever date the line holds), in ``unit`` only. A line in another unit or none
    makes the whole supply unknown (reason ``unit_mismatch``/``unit_unknown``): units are
    never converted."""
    prow = ctx.read(po_lines, ("ordered_quantity", "received_quantity",
                               "bc_expected_receipt_date", "uom"))
    # every line's order header, read once for all lines
    order_of: dict[str, str] = {}
    for _t, a, b in ctx.touching(sorted(prow), (PO_LINE_OF,)) if prow else ():
        order_of.setdefault(a, b)
    heads = ctx.read(sorted(set(order_of.values())), ("expected_date", "status"))
    flows = []
    for pl in sorted(prow):
        p = prow[pl]
        due = (_decimal(_v(p, "ordered_quantity")) or Decimal(0)) - \
            (_decimal(_v(p, "received_quantity")) or Decimal(0))
        head = heads.get(order_of.get(pl, ""))
        if head and CLOSED.search(_v(head, "status")):
            continue                       # a closed or cancelled order supplies nothing
        if due <= 0:
            continue
        at = _instant(_v(p, "bc_expected_receipt_date")) or \
            _instant(_v(head, "expected_date"))
        if at is None or at.date() < ctx.as_of - timedelta(days=30):
            continue
        line_unit = _qty_unit(p, "ordered_quantity", "received_quantity")
        if line_unit is None or unit is None:
            return [], "unit_unknown"
        if line_unit != unit:
            return [], "unit_mismatch"
        flows.append((max(at.date(), ctx.as_of), due, pl))
        ctx.cite(p, "bc_expected_receipt_date", "ordered_quantity")
    return sorted(flows), None


def availability(ctx: Ctx, order: str) -> dict[str, Any]:
    """Available-to-promise date for an order's open lines: on hand first, then open
    purchase order lines by their expected date, each consumed once across the order's
    lines (two lines of one item draw on one running allocation: 6 + 6 under 10 on hand is
    not both today). Quantities compare only in one stated unit (the order line's, the
    stock's, each PO line's; never converted). Another order's demand is not netted (stated
    in ``basis``). Unknown (``date`` None) when any line's item, quantity, unit or supply
    date is unstated, or the order has more lines than one check reads
    (``lines_not_read``)."""
    found = ctx.linked(order, (SO_LINE_OF,))
    lines = found[:MAX_LINES]
    rows = ctx.read(lines, ("ordered_quantity", "shipped_quantity", "outstanding_quantity",
                            "uom", "description"))
    dates: list[date] = []
    out_lines, unknown = [], []
    reasons: dict[str, int] = {}
    demand: dict[str, list[tuple[str, Decimal, str | None, Mapping[str, Any]]]] = {}
    for line in sorted(rows):
        r = rows[line]
        need = _decimal(_v(r, "outstanding_quantity"))
        if need is None:
            ordered, shipped = _decimal(_v(r, "ordered_quantity")), _decimal(
                _v(r, "shipped_quantity")) or Decimal(0)
            need = None if ordered is None else ordered - shipped
        if need is None:
            unknown.append(line)
            reasons["quantity_unknown"] = reasons.get("quantity_unknown", 0) + 1
            continue
        if need <= 0:
            continue
        items = ctx.linked(line, (SO_LINE_ITEM,))
        if len(items) != 1:
            unknown.append(line)
            reasons["item_unknown"] = reasons.get("item_unknown", 0) + 1
            continue
        demand.setdefault(items[0], []).append(
            (line, need, _qty_unit(r, "outstanding_quantity", "ordered_quantity",
                                   "shipped_quantity"), r))
    for item in sorted(demand):
        irow = ctx.read([item], ("quantity_on_hand", "code", "name")).get(item) or {}
        on_hand = _decimal(_v(irow, "quantity_on_hand")) or Decimal(0)
        stock_unit = _qty_unit(irow, "quantity_on_hand") if _v(irow, "quantity_on_hand") \
            else None
        ctx.cite(irow, "quantity_on_hand")
        units = {u for _l, _n, u, _r in demand[item]}
        unit = next(iter(units)) if len(units) == 1 else None
        why = None
        if unit is None:
            why = "unit_unknown" if None in units or not units else "unit_mismatch"
        elif on_hand and stock_unit != unit:
            why = "unit_unknown" if stock_unit is None else "unit_mismatch"
        # the item's supply, one running ledger: on hand today, then each open PO line by
        # its date; each line of the order draws on what the lines before it left
        ledger: list[tuple[date, Decimal]] = [(ctx.as_of, on_hand)] if on_hand > 0 else []
        drawn = Decimal(0)
        flows_read = False
        for line, need, _u, r in demand[item]:
            when: date | None = None
            if why is None:
                drawn += need
                covered = sum((q for _d, q in ledger), Decimal(0))
                if covered < drawn and not flows_read:
                    flows_read = True
                    po_lines = ctx.linked(item, (PO_LINE_ITEM,))[:MAX_LINES]
                    flows, why = supply_flows(ctx, po_lines, unit)
                    ledger += [(at, due) for at, due, _pl in flows]
                if why is None:
                    running = Decimal(0)
                    for at, qty in ledger:
                        running += qty
                        if running >= drawn:
                            when = at
                            break
            out_lines.append({"line": line, "item": _v(irow, "code") or _v(irow, "name"),
                              "need": str(need), "uom": _v(r, "uom"), "on_hand": str(on_hand),
                              "available": when.isoformat() if when else None,
                              **({"unknown": why} if why else {})})
            if when is None:
                unknown.append(line)
                key = why or "supply_short"
                reasons[key] = reasons.get(key, 0) + 1
            else:
                dates.append(when)
    if len(found) > MAX_LINES:
        reasons["lines_not_read"] = len(found) - MAX_LINES
    atp = max(dates).isoformat() if dates and not unknown and len(found) <= MAX_LINES \
        else None
    out = {"date": atp, "lines": out_lines, "unknown_lines": len(unknown),
           "basis": "on hand, then open purchase order lines by expected date, each "
                    "consumed once across this order's lines; other orders' demand is not "
                    "netted"}
    if reasons:
        out["unknown_reasons"] = reasons
    if len(found) > MAX_LINES:
        out["lines_read"], out["lines_total"] = MAX_LINES, len(found)
    return out


def _pack_link(oid: str) -> dict[str, str]:
    from urllib.parse import quote
    otype = oid.split(":", 1)[0]
    return {"label": "Document pack (COA, packing slip, invoice)",
            "href": f"/pack/{otype}/{quote(oid, safe='')}"}


def h_order_entry(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
    pos = _numbers(PO_NUMBER, event.get("subject", ""), event.get("text", ""))
    customer = next(iter(ctx.linked(event["object_id"], ("Email.sent_by_customer",))), None)
    if customer is None:
        named = sorted(o for side, o in _parties(ctx.st, event.get("sender") or "")
                       if side == "customer")
        customer = named[0] if len(named) == 1 else None   # one customer, else unknown
    crow = ctx.read([customer], ("name", "code", "erp_code")).get(customer) if customer else None
    if not pos and re.match(r"^\s*(?:re|aw|sv)\s*:", event.get("subject") or "", re.I):
        return {"skip": "reply_without_po_number"}
    named = identified(ctx, event.get("subject", ""), event.get("text", ""))
    if named and all(o.startswith(("PurchaseOrder:", "Receipt:")) for o in named):
        return {"skip": "names_our_purchase_order"}   # a supplier writing about our PO
    entered = [(pos[0] if pos else "", o) for o in named if o.startswith("SalesOrder:")]
    for po in pos:
        entered += [(po, o) for o in ctx.find("SalesOrder", "customer_po", po)]
    out: dict[str, Any] = {"po_numbers": pos, "customer": _v(crow, "name") or None,
                           "links": [], "gaps": []}
    if entered:
        po, order = entered[0]
        facts = _order_facts(ctx, order)
        atp = availability(ctx, order)
        out.update(summary=f"PO {po} is already entered as order {facts['order_number']} "
                           f"({facts['status'] or 'status not stated'}).",
                   order=facts, availability=atp)
        when = (f"We can ship on or after {atp['date']}." if atp["date"] else
                "We will confirm the ship date shortly.")
        out["draft"] = _reply(event, [
            f"Thank you for your purchase order {po}. It is entered as our order "
            f"{facts['order_number']}.", when])
        out["links"] = [_pack_link(order)]
        out["done_hint"] = "order_entered"
        return out
    out["summary"] = (f"New PO {pos[0] if pos else '(number not found)'}"
                      + (f" from {out['customer']}" if out["customer"] else "")
                      + ": not entered yet; order entry prepared as a dry-run write intent.")
    if crow and (_v(crow, "code") or _v(crow, "erp_code")) and pos:
        ctx.cite(crow, "code", "erp_code")
        out["write"] = {"system": "bc", "operation": "sales_order_create",
                        "payload": {"customerNumber": _v(crow, "code") or _v(crow, "erp_code"),
                                    "externalDocumentNumber": pos[0][:35],
                                    "orderDate": ctx.as_of.isoformat()},
                        "reason": f"Customer PO {pos[0]} received by mail; lines to be "
                                  "entered from the PO document before approval.",
                        "precondition": {"party": _v(crow, "code") or _v(crow, "erp_code")}}
        out["gaps"].append("lines_from_po_document")
    else:
        out["gaps"].append("customer_or_po_number_unknown")
    out["availability"] = {"date": None, "basis": "the order's lines are not entered yet"}
    out["draft"] = _reply(event, [
        f"Thank you for your purchase order{(' ' + pos[0]) if pos else ''}. We have received "
        "it and will send the order confirmation with the ship date shortly."])
    return out


def _records_named(ctx: Ctx, event: Mapping[str, Any]) -> list[str]:
    texts = (event.get("subject", ""), event.get("text", ""))
    found = [o for o in event.get("records") or () if o.startswith(
        ("SalesOrder:", "Shipment:", "SalesInvoice:"))]
    found += [o for o in identified(ctx, *texts) if o.startswith(
        ("SalesOrder:", "Shipment:", "SalesInvoice:"))]
    for po in _numbers(PO_NUMBER, *texts):
        found += ctx.find("SalesOrder", "customer_po", po)
    for no in _numbers(ORDER_NUMBER, *texts):
        found += ctx.find("SalesOrder", "order_number", no)
    for no in _numbers(INVOICE_NUMBER, *texts):
        found += ctx.find("SalesInvoice", "invoice_number", no)
    return list(dict.fromkeys(found))[:MAX_RECORDS]


def h_answer_request(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
    named = _records_named(ctx, event)
    orders = [o for o in named if o.startswith("SalesOrder:")]
    for inv in (o for o in named if o.startswith("SalesInvoice:")):
        orders += ctx.linked(inv, ("SalesInvoice.bills",))
    for sh in (o for o in named if o.startswith("Shipment:")):
        orders += ctx.linked(sh, ("Shipment.ships",))
    seen: set[str] = set()
    unique = []
    for o in dict.fromkeys(orders):
        if o in seen:
            continue
        seen.update(ctx.linked(o, ("SalesOrder.same_record_as",)))
        unique.append(o)
    orders = unique[:5]
    wants_docs = "document_request" in (event.get("intents") or ())
    out: dict[str, Any] = {"orders": [], "links": [], "gaps": []}
    lines = []
    for order in orders:
        f = _order_facts(ctx, order)
        out["orders"].append(f)
        ship = f["shipments"][0] if f["shipments"] else None
        bits = [f"Order {f['order_number']}" + (f" (your PO {f['customer_po']})"
                                                 if f["customer_po"] else "")
                + f": {f['status'] or 'in progress'}"]
        if ship and ship["ship_date"]:
            bits.append(f"shipped {ship['ship_date']}" + (f" via {ship['carrier']}"
                                                           if ship["carrier"] else "")
                        + (f", tracking {ship['tracking']}" if ship["tracking"] else ""))
        elif f["required_date"]:
            bits.append(f"required by {f['required_date']}")
        lines.append("; ".join(bits) + ".")
        if wants_docs:
            out["links"].append(_pack_link(order))
    if not orders:
        return {"skip": "no_record_named"}       # nothing to answer from: no item
    if False:
        out["gaps"].append("no_record_named")
        out["summary"] = "Asks for status or documents; no order, invoice or shipment named."
        lines = ["Could you send the PO or order number this is about? We will reply with "
                 "the status and documents right away."]
    else:
        out["summary"] = f"Asks about {len(orders)} order(s): " + " ".join(lines)[:SUMMARY_CHARS]
        if wants_docs:
            # the send carries no attachments (autopilot_write builds the message from the
            # body alone): the reply never says they are attached. The reviewer attaches
            # the pack (``links``) before sending; the gap says so (AUTOMATION-010)
            out["gaps"].append("docs_not_attached")
    out["draft"] = _reply(event, lines)
    return out


def h_delay_impact(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
    pos = [o for o in event.get("records") or () if o.startswith("PurchaseOrder:")]
    pos += [o for o in identified(ctx, event.get("subject", ""), event.get("text", ""))
            if o.startswith("PurchaseOrder:")]
    for no in _numbers(PO_NUMBER, event.get("subject", ""), event.get("text", "")):
        pos += ctx.find("PurchaseOrder", "po_number", no)
    pos = list(dict.fromkeys(pos))[:5]
    out: dict[str, Any] = {"purchase_orders": [], "impacted": [], "options": [], "gaps": []}
    impacted: dict[str, dict[str, Any]] = {}
    for po in pos:
        head = ctx.read([po], ("po_number", "expected_date", "status", "vendor_name")).get(po)
        ctx.cite(head, "po_number", "expected_date")
        out["purchase_orders"].append({"object_id": po, "number": _v(head, "po_number"),
                                       "expected": _v(head, "expected_date")[:10],
                                       "supplier": _v(head, "vendor_name")})
        for pl in ctx.linked(po, (PO_LINE_OF,))[:MAX_LINES]:
            for item in ctx.linked(pl, (PO_LINE_ITEM,))[:1]:
                irow = ctx.read([item], ("code", "name", "quantity_on_hand")).get(item)
                others = [x for x in ctx.linked(item, (PO_LINE_ITEM,)) if x != pl][:20]
                if others:
                    out["options"].append({"item": _v(irow, "code") or _v(irow, "name"),
                                           "other_open_po_lines": len(others),
                                           "on_hand": _v(irow, "quantity_on_hand") or None})
                for sl in ctx.linked(item, (SO_LINE_ITEM,))[:200]:
                    for order in ctx.linked(sl, (SO_LINE_OF,))[:1]:
                        if order in impacted:
                            continue
                        r = ctx.read([order], ("order_number", "status", "required_date")
                                     ).get(order)
                        if not r or CLOSED.search(_v(r, "status")):
                            continue
                        cust = ctx.linked(order, ("SalesOrder.ordered_by",))[:1]
                        crow = ctx.read(cust, ("name", "email")).get(cust[0]) if cust else None
                        impacted[order] = {"object_id": order,
                                           "order_number": _v(r, "order_number"),
                                           "required_date": _v(r, "required_date")[:10],
                                           "customer": _v(crow, "name"),
                                           "customer_email": (_addresses(_v(crow, "email"))
                                                              or [None])[0],
                                           "item": _v(irow, "code") or _v(irow, "name")}
                        ctx.cite(r, "order_number", "required_date")
    out["impacted"] = sorted(impacted.values(), key=lambda x: x["required_date"] or "9")[:20]
    if not pos:
        return {"skip": "no_purchase_order_named"}
    out["summary"] = (f"Supplier delay or shortage on {len(pos)} PO(s); "
                      f"{len(out['impacted'])} open customer order(s) use the items.")
    first = out["impacted"][0] if out["impacted"] else None
    if first and first["customer_email"]:
        out["customer_notice"] = {
            "channel": "email", "to": [{"address": first["customer_email"]}],
            "subject": f"Update on your order {first['order_number']}",
            "body": "\n".join(["Hello,", "", f"A supplier has told us of a delay affecting "
                               f"your order {first['order_number']}. We are working on "
                               "alternatives and will confirm the new date shortly.", "",
                               "Best regards,"])}
    out["draft"] = _reply(event, ["Thank you for letting us know. Please confirm the new ship "
                                  "date and the quantity you can ship now."])
    return out


def h_approval(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
    from . import proactive_threads as pt
    out: dict[str, Any] = {"gaps": [], "evidence": []}
    if event.get("object_type") == "MCApproval":
        f = event.get("fields") or {}
        task = next(iter(ctx.linked(event["object_id"], ("MCApproval.gate_of",))), None)
        trow = ctx.read([task], ("title", "stage", "task_id")).get(task) if task else None
        siblings = [o for o in ctx.linked(task, ("MCApproval.gate_of",))
                    if o != event["object_id"]] if task else []
        srows = ctx.read(siblings, ("status", "requested_at", "decided_at", "reason"))
        newer = [o for o, r in srows.items()
                 if _v(r, "requested_at") > f.get("requested_at", "") and
                 _v(r, "status").lower() == "pending"]
        approved = sorted((o for o, r in srows.items()
                           if _v(r, "status").lower() in ("approved", "granted")
                           and _v(r, "requested_at") < f.get("requested_at", "")
                           # granted before this request (a replay reads today's records)
                           and _v(r, "decided_at")[:19] <= f.get("requested_at", "")[:19]),
                          key=lambda o: _v(srows[o], "requested_at"))
        stage = _v(trow, "stage")
        if newer:
            rec, reasons = "superseded", ["a newer pending approval exists for the same task"]
        elif approved:
            last = srows[approved[-1]]
            rec, reasons = "approve", [
                f"an earlier approval of this task was granted on "
                f"{_v(last, 'decided_at')[:10] or _v(last, 'requested_at')[:10]}",
                f"task stage: {stage or 'not stated'}"]
        else:
            rec, reasons = "review", [
                f"no earlier approval of this task; task stage: {stage or 'not stated'}",
                f"requested by {f.get('requested_by') or 'unknown'} on "
                f"{(f.get('requested_at') or '')[:10]}"]
        out.update(
            summary=f"Approval requested for {_v(trow, 'task_id') or f.get('task_id')}: "
                    f"{(f.get('reason') or '')[:120]}",
            recommendation=rec, reasons=reasons,
            evidence=[{"object_id": o} for o in [task, *newer, *approved[-2:]] if o])
        ctx.cite(trow, "title", "stage")
        from . import mc_digest as md
        ctx.check()
        out["mc"] = md.item_facts(f.get("reason"), f.get("task_id"), md.history(
            ctx.st, str(event.get("audience") or ""), f.get("requested_at")))
        scope = f.get("scope")
        if isinstance(scope, str):
            try:
                scope = json.loads(scope)
            except ValueError:
                scope = None
        out["action"] = {"kind": "mc_approval", "gate": f.get("gate_id"),
                         "task_id": f.get("task_id") or _v(trow, "task_id") or None,
                         "requested_by": f.get("requested_by") or None,
                         "scope": scope if isinstance(scope, dict) else None,
                         "note": "Decide in Mission Control; the Workbench never decides it "
                                 "unless this workflow's write gate is live "
                                 "(operating.autopilot_write)."}
        return out
    family = pt.approval_family(event.get("text") or "")
    out.update(summary=f"Approval asked ({family}): {(event.get('subject') or '')[:120]}",
               recommendation="review", reasons=[f"family: {family}"],
               evidence=[{"object_id": o} for o in event.get("records") or ()])
    out["draft"] = _reply(event, ["Thanks, I am reviewing this and will confirm today."])
    return out


def h_task(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
    f = event.get("fields") or {}
    text = f"{f.get('title', '')}\n{f.get('description', '')}"
    probe = {"subject": f.get("title", ""), "text": f.get("description", ""), "records": []}
    named = _records_named(ctx, probe)
    for no in _numbers(PO_NUMBER, text):
        named += ctx.find("PurchaseOrder", "po_number", no)
    named = list(dict.fromkeys(named))[:MAX_RECORDS]
    facts = [_order_facts(ctx, o) for o in named if o.startswith("SalesOrder:")][:5]
    if not facts:
        # a task naming no order we can answer from would only restate itself: no item
        return {"skip": "no_record_named" if not named else "nothing_to_answer"}
    out = {"summary": f"Task: {f.get('title', '')[:140]}",
           "records": named, "orders": facts, "gaps": [] if named else ["no_record_named"],
           "links": [{"label": "Open the task's records", "href": f"/object/{o}"}
                     for o in named[:5]]}
    if facts:
        out["answer"] = [f"Order {x['order_number']}: {x['status'] or 'in progress'}"
                         + (f", shipped {x['shipments'][0]['ship_date']}"
                            if x["shipments"] and x["shipments"][0]["ship_date"] else "")
                         for x in facts]
    return out


def h_question(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
    """A question an MC agent asked the owner on a task: the question, its options, the
    asker's recommended option with its reason as the draft answer, the task, and the
    business records the question names with their state. The answer is the owner's to give
    in Mission Control: nothing here answers, writes or sends."""
    q = event.get("question") or {}
    text, options = str(q.get("text") or ""), [str(o) for o in q.get("options") or ()]
    if not text or not options:
        return {"skip": "question_without_options"}
    task = str(event.get("task") or "")
    title = ""
    if task:
        row = ctx.read([task], ("title", "stage")).get(task) or {}
        ctx.cite(row, "title")
        title = _v(row, "title")
    named = _records_named(ctx, {"subject": title, "text": f"{text}\n{q.get('context') or ''}",
                                 "records": []})[:MAX_RECORDS]
    facts = [_order_facts(ctx, o) for o in named if o.startswith("SalesOrder:")][:5]
    rec = q.get("recommended")
    out: dict[str, Any] = {
        "summary": text[:140], "question": text, "context": str(q.get("context") or ""),
        "options": options, "task_id": q.get("task_id"), "task_title": title[:200],
        "records": named, "orders": facts,
        "links": ([{"label": "Open the task", "href": f"/object/{task}"}] if task else [])
        + [{"label": "Open a record it names", "href": f"/object/{o}"} for o in named[:4]],
        "note": "Answer in Mission Control; nothing is answered from here."}
    if task:
        out["task"] = {"object_id": task}     # the question is the item, not the whole task
    if isinstance(rec, int) and 0 <= rec < len(options):
        out["draft"] = {"choice": rec, "option": options[rec],
                        "reason": str(q.get("reason") or ""),
                        "basis": "the asking agent's recommendation"}
    else:
        out["gaps"] = ["no_recommended_option"]
    return out


def h_ledger(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
    named = identified(ctx, event.get("subject", ""), event.get("text", ""))
    invs = [o for o in [*(event.get("records") or ()), *named] if o.startswith("SalesInvoice:")]
    for order in (o for o in named if o.startswith("SalesOrder:")):
        invs += ctx.linked(order, ("SalesInvoice.bills",))
    for no in _numbers(INVOICE_NUMBER, event.get("subject", ""), event.get("text", "")):
        invs += ctx.find("SalesInvoice", "invoice_number", no) or \
            ctx.find("SalesInvoice", "bc_number", no)
    for order in (o for o in event.get("records") or () if o.startswith("SalesOrder:")):
        invs += ctx.linked(order, ("SalesInvoice.bills",))
    invs = list(dict.fromkeys(invs))[:5]
    if not invs:
        return {"skip": "no_invoice_named"}
    rows = ctx.read(invs, ("invoice_number", "bc_number", "invoice_date", "due_date",
                           "total_amount", "remaining_amount", "currency", "status"))
    ledger, lines = [], []
    for inv, r in sorted(rows.items()):
        ctx.cite(r, "total_amount", "remaining_amount", "due_date")
        total, left = _decimal(_v(r, "total_amount")), _decimal(_v(r, "remaining_amount"))
        cur = _v(r, "currency")
        number = _v(r, "invoice_number") or _v(r, "bc_number")
        ledger.append({"object_id": inv, "number": number, "date": _v(r, "invoice_date")[:10],
                       "due": _v(r, "due_date")[:10], "total": str(total) if total is not None
                       else None, "remaining": str(left) if left is not None else None,
                       "currency": cur or None})
        if left is not None and left == 0:
            lines.append(f"Invoice {number} is paid in full.")
        elif left is not None:
            lines.append(f"Invoice {number}: {left} {cur} remains open"
                         + (f", due {_v(r, 'due_date')[:10]}." if _v(r, "due_date") else "."))
        else:
            lines.append(f"Invoice {number}: the open balance is not stated in our records.")
    out = {"ledger": ledger, "gaps": [] if ledger else ["no_invoice_named"],
           "summary": (" ".join(lines)[:SUMMARY_CHARS] if lines else
                       "Invoice or payment question; no invoice named.")}
    out["draft"] = _reply(event, lines or [
        "Could you send the invoice number? We will check the payment status right away."])
    return out


def h_qc(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
    f = event.get("fields") or {}
    oid = event["object_id"]
    orders: list[str] = []
    for no in re.findall(r"[A-Z]{0,4}-?\d{3,12}", f.get("order_list", "")):
        orders += ctx.find("SalesOrder", "order_number", no)
    for ao in ctx.linked(oid, ("Batch.requested_by",))[:5]:
        orders += [o for o in ctx.linked(ao) if o.startswith("SalesOrder:")]
    orders = list(dict.fromkeys(orders))[:10]
    product = next(iter(ctx.linked(oid, ("Batch.makes_bulk",))), None)
    prow = ctx.read([product], ("code", "name")).get(product) if product else None
    affected, drafts = [], []
    for order in orders:
        r = ctx.read([order], ("order_number", "status", "required_date")).get(order)
        if not r or CLOSED.search(_v(r, "status")):
            continue
        cust = ctx.linked(order, ("SalesOrder.ordered_by",))[:1]
        crow = ctx.read(cust, ("name", "email")).get(cust[0]) if cust else None
        affected.append({"object_id": order, "order_number": _v(r, "order_number"),
                         "required_date": _v(r, "required_date")[:10],
                         "customer": _v(crow, "name")})
        email = (_addresses(_v(crow, "email")) or [None])[0]
        if email:
            drafts.append({"channel": "email", "to": [{"address": email}],
                           "subject": f"Update on your order {_v(r, 'order_number')}",
                           "body": "Hello,\n\nA quality check on a lot for your order "
                                   f"{_v(r, 'order_number')} needs more time. We will "
                                   "confirm the ship date shortly.\n\nBest regards,"})
    lot = f.get("lot_number") or f.get("batch_number") or ""
    return {"summary": f"Lot {lot} is {event.get('quality')}"
                       + (f" ({_v(prow, 'code') or _v(prow, 'name')})" if prow else "")
                       + f"; {len(affected)} open order(s) affected.",
            "lot": lot, "quality": event.get("quality"),
            "results": {k: v for k, v in f.items() if k.startswith("qc_")},
            "affected": affected, "customer_notices": drafts[:5],
            "gaps": [] if orders else ["no_order_named"]}


def h_meeting(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
    f = event.get("fields") or {}
    parties = event.get("parties") or []
    prow = ctx.read(parties, ("name",))
    open_orders = []
    for p in parties[:3]:
        for o in ctx.linked(p, ("SalesOrder.ordered_by",))[:200]:
            r = ctx.read([o], ("order_number", "status", "required_date")).get(o)
            if r and not CLOSED.search(_v(r, "status")):
                open_orders.append({"object_id": o, "order_number": _v(r, "order_number"),
                                    "status": _v(r, "status"),
                                    "required_date": _v(r, "required_date")[:10]})
    open_orders = sorted(open_orders, key=lambda x: x["required_date"] or "9")[:10]
    external = [a for a in event.get("attendees") or () if not _internal(a, ctx.domains)]
    lines = ["Thank you for your time today. A short recap and next steps:", ""]
    lines += [f"- Order {o['order_number']}: {o['status']}"
              + (f", required by {o['required_date']}" if o["required_date"] else "")
              for o in open_orders] or ["- (add the agreed next steps)"]
    return {"summary": f"Meeting ended: {f.get('subject', '')[:120]}",
            "parties": [_v(r, "name") for r in prow.values()], "open_orders": open_orders,
            "draft": {"channel": "email", "to": [{"address": a} for a in external[:10]],
                      "subject": f"Follow-up: {f.get('subject', '')}"[:200],
                      "body": "\n".join(["Hello,", "", *lines, "", "Best regards,"])},
            "gaps": [] if external else ["no_external_attendee"]}


def _work(name: str) -> Callable[[Ctx, Mapping[str, Any]], dict[str, Any]]:
    def run(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
        from . import autopilot_work as aw
        return getattr(aw, name)(ctx, event)
    run.__name__ = name
    return run


def _receivables(name: str) -> Callable[[Ctx, Mapping[str, Any]], dict[str, Any]]:
    def run(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
        from . import autopilot_receivables as ar
        return getattr(ar, name)(ctx, event)
    run.__name__ = name
    return run


HANDLERS: dict[str, Callable[[Ctx, Mapping[str, Any]], dict[str, Any]]] = {
    "order_entry": _work("h_order_entry"), "po_followup": _work("h_po_followup"),
    "supplier_eta": _work("h_supplier_eta"), "desk": _work("h_desk"),
    "reply_prep": _work("h_reply_prep"), "invoice_send": _work("h_invoice_send"),
    "routine_update": _work("h_routine_update"),
    "rd_status_update": _work("h_rd_status_update"),
    "invoice_entry": _receivables("h_invoice_entry"),
    "shipment_posting": _receivables("h_shipment_posting"),
    "cash_application": _receivables("h_cash_application"),
    "answer_request": h_answer_request,
    "delay_impact": h_delay_impact, "approval_recommendation": h_approval,
    "task_prepare": h_task, "ledger_answer": h_ledger, "qc_impact": h_qc,
    "question_prepare": h_question,
    "meeting_followup": h_meeting}


def _meeting_extra(st: Any, event: dict[str, Any], domains: Sequence[str]) -> None:
    if event.get("event_type") == "meeting.ended":
        external = any(not _internal(a, domains) for a in event.get("attendees") or ())
        event["external_or_linked"] = bool(external or event.get("parties"))
        event["organizer_copy"] = True   # the business rule checks the organizer's own copy


# ----------------------------------------------------------------------------- engine

def _iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_cycle(st: Any, queue: Queue, ids: Iterable[str], *, people: Any,
              registry: Mapping[str, Any] | None = None, as_of: date | None = None,
              environ: Mapping[str, str] | None = None, now: datetime | None = None,
              landed_at: str | None = None, settle_items: bool = True,
              only_owners: Iterable[str] | None = None,
              gate: Callable[..., bool] | None = None,
              rate_by_event_hour: bool = False,
              feed_lag: Mapping[str, timedelta] | None = None,
              read_work_log: bool = False, event_queue: bool = False,
              horizon: datetime | None = None) -> dict[str, Any]:
    """One cycle: type the changed records, store each event once, prepare the work of each
    new event every matching autopilot declares, settle items their evidence closed.
    ``only_owners``: prepare only for these people (a replay of one role); ``gate(owner,
    workflow, roles)``: prepare only what passes it (the live cycle and a replay into a
    queue: the workflows a role's replay passed, in that role, ``gate_from``).
    ``rate_by_event_hour``: the hourly cap counts each event in the hour it happened (a
    replay runs one cycle per past day; live, a cycle's hour is the one it runs in).
    ``read_work_log``: skip a message the owner already answered from the Workbench
    (``Queue.answered_in_workbench``). Live only: the ``follow`` command alone passes it;
    every other caller (a replay, its catch-up ``_drain``, tests) never opens the log, so a
    replay never learns what the Workbench did after the day it judges.
    ``event_queue``: hand the events out through the ordered event queue when
    ``PLX_AUTOPILOT_EVQ`` is on (``OrderedFeed``). Live only: the ``follow`` command alone
    passes it, so a replay or its catch-up never touches the live queue's order.
    ``horizon``: a message or meeting before it (or with no time) is not stored as an event
    (live follow, ``FOLLOW_HORIZON``); a sender's history still counts it."""
    environ = os.environ if environ is None else environ
    registry = registry or load_registry()
    now = now or datetime.now(timezone.utc)
    as_of = as_of or now.date()
    started = time.monotonic()
    summary: dict[str, Any] = {"schema": SCHEMA, "status": "complete", "events_new": 0,
                               "events_seen": 0, "prepared": Counter(), "skipped": Counter(),
                               "sent": 0, "writes": 0}
    if killed(queue, environ):
        summary["status"] = "off"
        return _plain(summary)
    if people is None or getattr(people, "unavailable", None):
        summary["status"] = "blocked"          # the people exclusion fails closed
        summary["reason"] = "people_exclusion_unavailable"
        return _plain(summary)
    limits = registry["limits"]
    outbound: list[dict[str, Any]] = []
    events, skipped = discover(st, ids, registry, people, landed_at=landed_at,
                               outbound=outbound, now=now, feed_lag=feed_lag)
    with queue.conn:
        summary["sent_noted"] = queue.note_sent(outbound)
        queue.note_routine(outbound)
        # a group message no one addressed to the owner is not a message to them: a
        # sender's answered share (``reply_history``) never counts it
        queue.note_inbound(e for e in events if e.get("event_type") == "message.received"
                           and not e.get("unaddressed"))
    if any(e.get("unaddressed") for e in events):
        # only a member who wrote in the conversation in the turn rule's days can have a
        # turn there (``own_posts`` decides, from the messages before the event): the
        # others' copies are never stored
        oldest = min((_instant(e.get("at")) for e in events if e.get("unaddressed")
                      and _instant(e.get("at"))), default=now)
        wrote = queue.writers(_iso(oldest - timedelta(days=OWN_POSTS_DAYS)))
        events = [e for e in events if not e.get("unaddressed") or (
            str(e.get("mailbox") or ""), _digest("thread", e.get("conversation_id"))) in wrote]
    if horizon is not None:
        kept = []
        for e in events:
            at = _instant(e.get("at")) if e.get("event_type") in HORIZON_EVENTS else horizon
            if at is None or at < horizon:
                skipped["before_horizon"] += 1
            else:
                kept.append(e)
        events = kept
    discovered = time.monotonic()
    summary["skipped"].update(skipped)
    directory, addresses_of, domains = _people_maps(people)
    roles = _roles(environ, queue.path.parent)
    stamp = _iso(now)
    rd_day = None
    try:
        from . import autopilot_work as aw
        events += aw.po_due_events(st, registry, as_of, domains, directory, addresses_of)
        events += aw.invoice_due_events(st, registry, as_of, domains, directory)
        events += aw.routine_due_events(st, queue, registry, as_of, people)
        # a project's state is read as it stood at the day's start: once a day is enough
        # (its 90-day mail read is the slowest of these)
        if queue.meta("rd_due_day") != as_of.isoformat():
            events += aw.rd_due_events(st, registry, as_of, people)
            rd_day = as_of.isoformat()
        from . import autopilot_receivables as ar
        events += ar.due_events(st, registry, as_of, people)
    except sqlite3.Error:
        summary["skipped"]["due_events_unreadable"] += 1
    with queue.conn:
        for e in events:
            summary["events_seen"] += 1
            summary["events_new"] += queue.add_event(e, stamp)
        if rd_day:
            queue.set_meta("rd_due_day", rd_day)
    ordered = None
    if event_queue and event_queue_on(environ):
        try:
            ordered = OrderedFeed(queue, environ)
        except Exception as error:  # noqa: BLE001 - fail closed: the events wait, unordered never
            summary["status"] = "blocked"
            summary["reason"] = f"event_queue_unavailable:{type(error).__name__}"
            return _plain(summary)
    feed = ordered.events(int(limits["max_events_per_cycle"])) if ordered else \
        iter(queue.pending(int(limits["max_events_per_cycle"])))
    while True:
        if time.monotonic() - started > limits["cycle_seconds"]:
            summary["status"] = "partial"      # the rest stay new: the next cycle goes on
            break
        event = next(feed, None)
        if event is None:
            break
        _meeting_extra(st, event, domains)
        if event.get("event_type") == "message.received" and event.get("mailbox") and \
                event.get("at"):
            event.update(queue.engagement(str(event["mailbox"]), event.get("conversation_id"),
                                          str(event.get("sender") or ""), str(event["at"])))
            event["sender_history"] = list(queue.reply_history(
                str(event["mailbox"]), str(event.get("sender") or ""), str(event["at"])))
            event["own_posts"] = list(queue.own_posts(
                str(event["mailbox"]), event.get("conversation_id"), str(event["at"])))
        state, reason = _process(st, queue, event, registry, people=people,
                                 directory=directory, addresses_of=addresses_of, roles=roles,
                                 domains=domains, as_of=as_of, now=now, summary=summary,
                                 only_owners=None if only_owners is None else set(only_owners),
                                 gate=gate, rate_by_event_hour=rate_by_event_hour,
                                 read_work_log=read_work_log)
        with queue.conn:
            queue.mark(event["event_id"], state, reason, stamp)
        if ordered:
            ordered.settle(event["event_id"], state, reason)   # after the mark: a crash between redelivers
    if ordered:
        if ordered.unknown:
            summary["skipped"]["unknown_event_type"] += ordered.unknown
        ordered.close()
    summary["discover_seconds"] = round(discovered - started, 2)
    if settle_items:
        summary["settled"] = settle(st, queue, registry, people, now=now)
    summary["seconds"] = round(time.monotonic() - started, 2)
    return _plain(summary)


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


def _process(st: Any, queue: Queue, event: dict[str, Any], registry: Mapping[str, Any], *,
             people: Any, directory: Mapping[str, str], addresses_of: Mapping[str, set[str]],
             roles: Mapping[str, list[str]], domains: Sequence[str], as_of: date,
             now: datetime, summary: dict[str, Any], only_owners: set[str] | None = None,
             gate: Callable[..., bool] | None = None, rate_by_event_hour: bool = False,
             read_work_log: bool = False) -> tuple[str, str | None]:
    from . import workflows as wf
    limits = registry["limits"]
    if event.get("event_type") == "message.received" and not event.get("cleared"):
        summary["skipped"]["participants_not_cleared"] += 1
        return "skipped", "participants_not_cleared"
    if pay_or_hr(event):
        summary["skipped"]["pay_or_hr"] += 1       # never prepared, never shown
        return "skipped", "pay_or_hr"
    deferred = False
    reasons = []
    failed: list[str] = []     # handler failures and timeouts: the event is tried again
    for ap in registry["autopilots"]:
        if ap["on"] != event["event_type"] or not matches(ap.get("when") or {}, event):
            continue
        if ap.get("enabled") is False:
            continue                       # switched off in the registry (``off_reason``)
        when = ap.get("when") or {}
        if event.get("unaddressed") and "after_own_post" not in when:
            continue    # a group message not addressed to the owner: only a turn rule answers it
        box = str(event.get("mailbox") or "")
        role = when.get("mailbox_role")
        if role and box not in _role_members(role, roles, directory):
            continue
        role = when.get("mailbox_role_none")
        if role and box in _role_members(role, roles, directory):
            continue
        decided = None
        if ap["owner"] == "approvers":
            try:
                project = mc_project(str(event.get("audience") or ""))
                decided = deciders(st, directory, min(_instant(event.get("at")) or now, now),
                                   project=project) if project else None
            except sqlite3.Error:
                decided = None
        owners, why = owners_of(ap["owner"], event, directory=directory,
                                addresses_of=addresses_of, roles=roles, decided=decided)
        if why:
            summary["skipped"][f"{ap['id']}:{why}"] += 1
            reasons.append(why)
            continue
        owners = [o for o in owners if people.identity_clear(o)]
        if not owners:
            summary["skipped"][f"{ap['id']}:owner_not_cleared"] += 1
            continue
        if only_owners is not None:
            owners = [o for o in owners if o in only_owners]
            if not owners:
                continue                       # another role's work (a role's replay)
        if gate is not None:
            workflow = wf.of_item(ap["id"], event)
            owners = [o for o in owners if gate(o, workflow, item_roles(ap))]
            if not owners:
                summary["skipped"][f"{ap['id']}:workflow_not_passed"] += 1
                continue
        if ap.get("fallback"):
            # only for an owner no other autopilot prepared this event for
            owners = [o for o in owners if not queue.conn.execute(
                "SELECT 1 FROM items WHERE event_id = ? AND owner = ? LIMIT 1",
                (event["event_id"], o)).fetchone()]
            if not owners:
                continue
        happened = _instant(event.get("at")) if rate_by_event_hour else None
        hour = (happened if happened and happened <= now else now).strftime("%Y-%m-%dT%H")
        prepared = None
        for owner in owners:
            item_id = "ap_" + _digest(ap["id"], event["event_id"], owner)[:24]
            if queue.item(item_id) is not None:
                continue
            if queue.thread_item_open(owner, ap["id"], event):
                summary["skipped"][f"{ap['id']}:thread_item_open"] += 1
                continue
            if read_work_log and queue.answered_in_workbench(
                    {owner, *addresses_of.get(owner, ())}, event, now):
                # live only (an explicit opt-in): a replay never reads the Workbench
                summary["skipped"][f"{ap['id']}:answered_in_workbench"] += 1
                continue
            if not queue.rate_take(owner, hour, int(limits["per_owner_per_hour"])):
                summary["skipped"][f"{ap['id']}:rate_limited"] += 1
                deferred = True
                continue
            if prepared is None:
                prepared = _prepare(st, ap, event, as_of, domains,
                                    float(limits["handler_seconds"]))
            if prepared.get("skip"):
                summary["skipped"][f"{ap['id']}:{prepared['skip']}"] += 1
                reasons.append(prepared["skip"])
                break
            if prepared.get("error"):
                summary["skipped"][f"{ap['id']}:{prepared['error']}"] += 1
                reasons.append(prepared["error"])
                failed.append(prepared["error"])
                break
            due = now + timedelta(minutes=int(ap["due_minutes"]))
            landed = _instant(event.get("seen_at") or event.get("landed_at") or event.get("at"))
            with queue.conn:
                queue.add_item({
                    "item_id": item_id, "event_id": event["event_id"], "autopilot": ap["id"],
                    "owner": owner, "title": f"{ap['label']}: "
                    + str(prepared["work"].get("summary") or "")[:SUMMARY_CHARS],
                    "body": {"happened": _happened(event), "prepared": prepared["work"],
                             "done_when": ap["done_when"], "label": ap["label"]},
                    "audiences": prepared["audiences"],
                    "markings": sorted(set(ap.get("markings") or ()) | prepared["markings"]),
                    "citations": prepared["citations"], "due_at": _iso(due),
                    "created_at": _iso(now), "prepare_ms": prepared["ms"],
                    "latency_s": round((now - landed).total_seconds(), 1) if landed else None})
            summary["prepared"][ap["id"]] += 1
    if failed:
        # what was prepared stands (an item is made once per event and owner); the event
        # is tried again, up to HANDLER_TRIES, then left failed
        why = ",".join(sorted(set(failed)))
        with queue.conn:
            tries = queue.tried(event["event_id"])
        if tries >= HANDLER_TRIES:
            summary["skipped"]["handler_failed_for_good"] += 1
            return "failed", why
        return "deferred", why
    if deferred:
        return "deferred", "rate_limited"
    return "processed", ",".join(sorted(set(reasons))) or None


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
    if event.get("fields"):
        out["fields"] = {k: v for k, v in event["fields"].items()
                         if k in ("title", "task_id", "reason", "lot_number", "subject",
                                  "stage", "requested_by", "po_number", "order_date")}
    return out


def _prepare(st: Any, ap: Mapping[str, Any], event: Mapping[str, Any], as_of: date,
             domains: Sequence[str], seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    ctx = Ctx(st, as_of, deadline, domains)
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
    typed = _types(st, read)
    audiences = {aud for _t, aud in typed.values()}
    if event.get("audience"):
        audiences.add(event["audience"])
    markings: set[str] = set()
    for row in _markings(st, read):
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


# ----------------------------------------------------------------------------- reading side

def visible(queue: Queue, *, reader: str, may_read: Callable[[str], bool],
            held: Iterable[str], decide: Callable[[Sequence[str]], Mapping[str, Any]],
            grants: Mapping[str, Any] | None = None,
            states: Sequence[str] = ("open",)) -> list[dict[str, Any]]:
    """The items this reader may see (module doc), fail closed."""
    from .proactive_commitments import owner_ids
    mine = owner_ids(reader)
    if not mine:
        return []
    grants = grants or {}
    teams = grants.get("teams") or {}
    held_teams = set()
    for oid in mine:
        held_teams.update((grants.get("managers") or {}).get(oid) or ())
    reports = {o for o, t in teams.items() if t in held_teams}
    owners = mine | reports
    held = set(held)
    out = []
    for item in queue.items(owners, states):
        try:
            if not all(may_read(a) for a in item["audiences"]):
                continue
            if not set(item["markings"]) <= held:
                continue
            cited = item_handles(item)
            if cited:
                refused = decide(cited)
                # every handle decided readable, or the item is not shown (fail closed)
                if not isinstance(refused, Mapping) or \
                        any(h not in refused or refused[h] is not None for h in cited):
                    continue
        except Exception:  # noqa: BLE001 - an undecidable item is not shown
            continue
        item["mine"] = item["owner"] in mine
        out.append(item)
    return out


_CITED = re.compile(HANDLE_PREFIX)


def item_handles(item: Mapping[str, Any]) -> list[str]:
    """Every citation of an item: its ``citations`` and any handle its prepared work or
    event names (an item stored before the list was whole kept only its first 40)."""
    out = list(item.get("citations") or ())

    def walk(value: Any) -> None:
        if isinstance(value, str):
            if _CITED.match(value):
                out.append(value)
        elif isinstance(value, Mapping):
            for v in value.values():
                walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v)
    walk(item.get("body") or {})
    return list(dict.fromkeys(out))


def digest(items: Sequence[Mapping[str, Any]], now: datetime) -> dict[str, Any]:
    """A person's daily digest: counts per autopilot, items due today, nudges for items
    past due (oldest first)."""
    stamp = _iso(now)
    by = Counter(i["autopilot"] for i in items)
    late = sorted((i for i in items if i["due_at"] < stamp), key=lambda i: i["due_at"])
    today = [i for i in items if i["due_at"][:10] == stamp[:10]]
    from . import autopilot_work as aw
    from . import mc_digest as md
    return {"open": len(items), "by_autopilot": dict(sorted(by.items())),
            "due_today": len(today), "brief": aw.brief(items, now),
            "mc_approvals": md.digest(items, now),
            "nudges": [{"item_id": i["item_id"], "title": i["title"], "due_at": i["due_at"],
                        "text": f"Waiting since {i['created_at'][:16].replace('T', ' ')} UTC: "
                                f"{i['title']}"} for i in late[:20]]}


def card_of(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """The item's drafted message as a card ``card_actions.ActionDesk`` accepts (sent only
    through its preview and the owner's confirm); None without an email draft."""
    work = (item.get("body") or {}).get("prepared") or {}
    draft = work.get("draft") or {}
    to = [t.get("address") for t in draft.get("to") or () if t.get("address")]
    cites = [c for c in item.get("citations") or () if isinstance(c, str)]
    if draft.get("channel") not in ("email", "mail") or not to or not cites:
        return None
    return {"id": CARD_PREFIX + item["item_id"], "headline": str(item.get("title"))[:300],
            "subject": {"label": str(draft.get("subject") or item.get("title"))[:200]},
            "action": {"text": ""},
            "draft": {"channel": "email", "to": {"address": to[0], "source": cites[0]},
                      "subject": draft.get("subject"), "body": draft.get("body"), "facts": []},
            "teams_draft": None, "playbook": True}


def write_spec(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """The item's dry-run write intent spec for ``WriteDesk.prepare`` (by the autopilot)."""
    work = (item.get("body") or {}).get("prepared") or {}
    spec = work.get("write")
    if not isinstance(spec, Mapping) or not item.get("citations"):
        return None
    return {**spec, "citations": list(item["citations"])[:20]}


def _plain(summary: dict[str, Any]) -> dict[str, Any]:
    return {k: dict(v) if isinstance(v, Counter) else v for k, v in summary.items()}


# ----------------------------------------------------------------------------- CLI

# Which store a replay or score reads (``_store``): the SQLite generation file alone, or the
# Postgres ontology the API reads in mode 1 (current to the last delta layer applied).
STORE_SOURCES = ("sqlite", "postgres")


def _store(path: str, source: str | None = None) -> Any:
    """The store at ``path`` (``proactive_engine._Store``, its reads through
    ``proactive_engine._connect``). ``source``: ``"postgres"`` turns on the API's mode 1
    (``PLX_ONTOLOGY_PG=1``) in this process, so every read runs on Postgres through the
    compat layer (``operating.pg.sqlite_compat``) when Postgres holds the generation at
    ``path``; ``"sqlite"`` turns it off, so only the file is read; None: as the environment
    says. The store opened must be the one asked for: Postgres not serving that
    generation (not loaded, a load in progress, unreachable) is ``store_source_mismatch``,
    never a quiet read of the older SQLite file."""
    from ontology import store as ostore
    from . import proactive_engine as pe
    from .pg import ontology_reader
    if source is not None and source not in STORE_SOURCES:
        raise ValueError(f"unknown store source {source!r}")
    if source == "postgres" and not ontology_reader.enabled():
        os.environ[ontology_reader.ENV] = "1"
    elif source == "sqlite":
        if ontology_reader.pg_only():
            raise ostore.StoreError("store_source_mismatch", "postgres_only")
        os.environ.pop(ontology_reader.ENV, None)
    st = pe._Store(Path(path), threads=4)
    if source is not None and store_source(st)["store"] != source:
        st.close()
        raise ostore.StoreError("store_source_mismatch", f"{source}_not_served")
    return st


def store_source(st: Any) -> dict[str, Any]:
    """Which store ``st`` reads and what it held when opened (a SQLite generation's content
    digest; Postgres: its generation's digest and data version), for a result's
    provenance."""
    return {"store": "sqlite" if isinstance(st.conn, sqlite3.Connection) else "postgres",
            "stamp": st.digest}


def _drain(st: Any, queue: Queue, people: Any, registry: Mapping[str, Any],
           now: datetime, **scope: Any) -> dict[str, int]:
    """A replay's further cycles until no event waits (each cycle bounded in time); the
    rate limit's hour moves on with each cycle, as it would live."""
    total: Counter = Counter()
    for n in range(1, 500):
        left = queue.conn.execute("SELECT COUNT(*) FROM events WHERE state IN ('new', "
                                  "'deferred')").fetchone()[0]
        if not left:
            break
        out = run_cycle(st, queue, [], people=people, registry=registry,
                        now=now + timedelta(hours=n), settle_items=False, **scope)
        total.update(out.get("prepared") or {})
        total.update({f"skipped:{k}": v for k, v in (out.get("skipped") or {}).items()})
        if out.get("status") not in ("complete", "partial"):
            break
    return dict(total)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def main(argv: Sequence[str] | None = None) -> int:
    from . import proactive_engine as pe
    p = argparse.ArgumentParser(prog="python3 -m operating.autopilot")
    p.add_argument("command", choices=("follow", "replay"))
    p.add_argument("--state-dir", default=None,
                   help="the served generation's store and <state>/autopilot/ queue")
    p.add_argument("--store", default=None)
    p.add_argument("--queue", default=None)
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--until", default=None, help="replay window end (ISO), default now")
    p.add_argument("--layer", default=None, help="replay: also the records this layer owns")
    p.add_argument("--registry", default=None)
    p.add_argument("--capture-state", default=None,
                   help="state dir whose mail capture holds attachment text (read-only)")
    a = p.parse_args(argv)
    if a.state_dir:
        state = Path(a.state_dir)
        current = json.loads((state / "projection" / "CURRENT").read_text())
        a.store = a.store or str(state / "projection" / "generations"
                                 / str(current["generation"]) / "store.sqlite")
        a.queue = a.queue or str(state / "autopilot" / QUEUE_FILE)
    if not a.store or not a.queue:
        p.error("--state-dir, or --store and --queue, are required")
    registry = load_registry(a.registry)
    queue = Queue(a.queue)
    store = Path(a.store)
    # the delta pointer sits beside the served projection (``host.projection_path``, as the
    # proactive pass reads it), not beside a generation's file
    if a.state_dir:
        from .host import projection_path
        layers_at = projection_path(Path(a.state_dir))
    else:
        layers_at = store
    people = pe._people(store)
    st = _store(a.store)
    capture_dir = a.capture_state or a.state_dir
    if capture_dir and (Path(capture_dir) / "mail-capture" / "capture.db").exists():
        from . import autopilot_work as aw
        st.autopilot_capture = aw.CaptureTexts(capture_dir)
    if capture_dir and (Path(capture_dir) / "mc-capture" / "capture.sqlite").exists():
        from . import autopilot_work as aw
        st.autopilot_mc = aw.MCQuestions(capture_dir)
    try:
        # follow and replay both write the queue people read: each prepares only the
        # workflows a role's replay passed, in that role (``gate_from``)
        gate, passed = gate_from(os.environ, queue.path.parent)
        # Draft (``load_draft``): live follow only. The configured people's workflows are
        # prepared before their gate passed; an accept still goes through the write
        # path's own gates. A replay never drafts: it would fill the shared queue with
        # a past window's items nobody passed.
        drafted = draft_owners(load_draft(), _people_maps(people)[0], people) \
            if a.command == "follow" else {}
        gate = with_draft(gate, drafted)
        for oid, flows in drafted.items():
            passed = {**passed, oid: sorted(set(passed.get(oid) or ()) | flows)}
        if not passed:                # off until a role's replay passed (``gate_from``)
            print(json.dumps({"schema": SCHEMA, "status": "off",
                              "reason": "no_workflow_passed_replay"}))
            return 0
        if a.command == "follow":
            now = _utcnow()
            ids, taken, read = follow_ids(st, layers_at, queue, registry, now)
            summary = run_cycle(st, queue, ids, people=people, registry=registry, gate=gate,
                                now=now, read_work_log=True, event_queue=True,
                                horizon=now - FOLLOW_HORIZON)
            summary["read"] = read
            summary["workflows_on"] = {"owners": len(passed),
                                       "workflows": sorted({w for v in passed.values()
                                                            for w in v})}
            if summary["status"] in ("complete", "partial"):
                from .proactive_incremental import generation_digest
                with queue.conn:
                    for layer in taken:   # kept by generation, as ``layer_ids`` reads them
                        queue.conn.execute("INSERT OR IGNORE INTO layers VALUES (?, ?)",
                                           (layer, generation_digest(st.digest)))
                    if read.get("window") is not None:
                        queue.set_meta("follow_stamp", st.digest)
            summary["layers_taken"] = len(taken)
        else:
            until = a.until or _iso(datetime.now(timezone.utc))
            since = _iso(_instant(until) - timedelta(days=a.days))
            ids = window_ids(st, registry, since, until)
            summary = run_cycle(st, queue, ids, people=people, registry=registry,
                                now=_instant(until), settle_items=False, gate=gate)
            summary["drained"] = _drain(st, queue, people, registry, _instant(until),
                                        gate=gate)
            if a.layer:
                with contextlib.closing(sqlite3.connect(f"file:{a.layer}?mode=ro", uri=True)
                                        ) as conn:
                    owned = [r[0] for r in conn.execute("SELECT object_id FROM delta_owned")]
                lst = _store(a.layer)
                try:
                    summary["layer"] = run_cycle(lst, queue, owned, people=people,
                                                 registry=registry, now=_instant(until),
                                                 settle_items=False, gate=gate)
                    summary["layer"]["drained"] = _drain(lst, queue, people, registry,
                                                         _instant(until), gate=gate)
                finally:
                    lst.close()
            summary["window"] = [since, until]
        print(json.dumps(summary, indent=1, sort_keys=True, default=str))
    finally:
        st.close()
        queue.close()
    return 0 if summary.get("status") in ("complete", "partial", "off") else 1


if __name__ == "__main__":
    sys.exit(main())
