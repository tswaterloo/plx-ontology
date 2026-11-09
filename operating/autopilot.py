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

SCHEMA = "plx-autopilot/v1"
REGISTRY_FILE = Path(__file__).with_name("autopilot_registry.json")
KILL_ENV = "PLX_AUTOPILOT_OFF"
KILL_FILE = "autopilot.off"
ROLES_ENV = "PLX_AUTOPILOT_ROLES_FILE"
ROLES_FILE = "autopilot-roles.json"
QUEUE_FILE = "autopilot.sqlite"
CARD_PREFIX = "autopilot:"
EVENT_TYPES = ("message.received", "task.assigned", "record.changed", "qc.result",
               "approval.requested", "meeting.ended", "record.due")
OWNER_RULES = ("recipient", "assignee", "approvers", "organizer", "buyer")
ROLE_OWNER = re.compile(r"^role:[a-z_]{1,40}$")
CONDITIONS = ("intents_any", "intents_none", "side_in", "status_in", "assigned", "open", "asks",
              "quality_in", "tested", "ended", "organizer_copy", "external_or_linked",
              "mailbox_role", "mailbox_role_none")
DONE_RULES = ("owner_replied", "approval_decided", "task_closed", "batch_released",
              "owner_mailed_attendees")
MAX_RECORDS = 12          # business records one handler reads around its event
MAX_LINES = 50            # order lines one availability check reads
SUMMARY_CHARS = 160
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
        if ap.get("done_when") not in DONE_RULES:
            raise RegistryInvalid(f"registry_done:{ap['id']}")
        if not isinstance(ap.get("due_minutes"), int) or ap["due_minutes"] <= 0:
            raise RegistryInvalid(f"registry_due:{ap['id']}")
        if "enabled" in ap and not isinstance(ap["enabled"], bool):
            raise RegistryInvalid(f"registry_enabled:{ap['id']}")
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
        if key in ("assigned", "open", "tested", "ended", "organizer_copy",
                   "external_or_linked", "asks") and bool(event.get(key)) != bool(want):
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
        self.conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, object_id TEXT NOT NULL,
                object_type TEXT NOT NULL, at TEXT, landed_at TEXT, seen_at TEXT NOT NULL,
                body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'new', reason TEXT,
                processed_at TEXT);
            CREATE INDEX IF NOT EXISTS events_state ON events(state);
            CREATE TABLE IF NOT EXISTS items (
                item_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, autopilot TEXT NOT NULL,
                owner TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
                audiences TEXT NOT NULL, markings TEXT NOT NULL, citations TEXT NOT NULL,
                due_at TEXT NOT NULL, created_at TEXT NOT NULL, state TEXT NOT NULL,
                done_reason TEXT, done_at TEXT, prepare_ms INTEGER, latency_s REAL);
            CREATE INDEX IF NOT EXISTS items_owner ON items(owner, state);
            CREATE TABLE IF NOT EXISTS rate (owner TEXT, hour TEXT, n INTEGER,
                PRIMARY KEY (owner, hour));
            CREATE TABLE IF NOT EXISTS layers (layer TEXT PRIMARY KEY, base_digest TEXT);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);""")

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
    rows = st.conn.execute(
        "SELECT DISTINCT eo.object_id FROM events e JOIN event_objects eo ON eo.event_id = "
        "e.event_id WHERE e.event_type IN (SELECT value FROM json_each(?)) AND e.time >= ? "
        "AND e.time < ?", (json.dumps(kinds), since, until)).fetchall()
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
    from ontology import store as ostore
    out: set[str] = set()
    for otype, prop in (registry.get("value_windows") or {}).items():
        ids = [r[0] for r in st.conn.execute(
            "SELECT o.object_id FROM objects o WHERE o.object_type = ? AND NOT "
            + ostore.removed_at_sql("o.object_id"), (otype,))]
        for start in range(0, len(ids), 5000):
            rows = _read(st, ids[start:start + 5000], (prop,))
            out.update(o for o, r in rows.items() if since <= _v(r, prop) < until)
    return out


def layer_ids(store: Path, queue: Queue, digest: str | None) -> tuple[set[str], list[str]]:
    """The records the served delta layers own that this feed has not taken in yet."""
    from .proactive_incremental import layer_owned
    owned, taken, _waiting = layer_owned(store, digest, queue.conn, None)
    return owned, taken


def discover(st: Any, ids: Iterable[str], registry: Mapping[str, Any], people: Any, *,
             landed_at: str | None = None) -> tuple[list[dict[str, Any]], Counter]:
    """Typed events of the changed records ``ids`` (module doc)."""
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
                                   skipped)
        elif otype == "TeamsChatMessage":
            events += _teams_events(st, oids, typed, people, directory, skipped)
        else:
            events += _record_events(st, otype, kind, oids, typed, sources[otype], skipped)
    for e in events:
        e["landed_at"] = landed_at
    return events, skipped


def _mail_events(st: Any, oids: Sequence[str], typed: Mapping[str, tuple[str, str]],
                 people: Any, directory: Mapping[str, str],
                 addresses_of: Mapping[str, set[str]], domains: Sequence[str],
                 skipped: Counter) -> list[dict[str, Any]]:
    from . import inbox_triage as it
    from . import proactive_threads as pt
    out = []
    rows = _read(st, oids, EMAIL_PROPS)
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
            continue
        subject, text = _v(row, "subject"), pt._own(_v(row, "body_preview"))
        if pt.automated(sender, text, subject) or it.bulk(sender, text, subject):
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
            "cleared": cleared,
            "handles": [c["l1_handle"] for c in row.values() if c.get("l1_handle")][:3]})
    return out


def _teams_events(st: Any, oids: Sequence[str], typed: Mapping[str, tuple[str, str]],
                  people: Any, directory: Mapping[str, str], skipped: Counter
                  ) -> list[dict[str, Any]]:
    from . import proactive_threads as pt
    from .proactive_ops import declared_sources
    members = getattr(people, "chat_members", None) or {}
    text_props = tuple(dict.fromkeys(
        p for ps in declared_sources()["ask_text"].values() for p in ps))
    rows = _read(st, oids, ("created_at", "from", "deleted_at") + text_props)
    out = []
    for oid in oids:
        aud = typed[oid][1]
        if not aud.startswith("teams:chat:") or not people.unit_clear(aud):
            skipped["teams_not_one_to_one_or_not_cleared"] += 1
            continue
        who = sorted({str(x).lower() for x in members.get(aud[len("teams:chat:"):], ())})
        row = rows.get(oid) or {}
        sender = _v(row, "from").lower()
        sender = directory.get(sender, sender)
        if len(who) != 2 or sender not in who or _v(row, "deleted_at"):
            skipped["teams_not_one_to_one"] += 1
            continue
        owner = next(x for x in who if x != sender)
        text = pt._own(next((_v(row, p) for p in text_props if _v(row, p)), ""))
        out.append({
            "event_id": _digest("message.received", oid), "event_type": "message.received",
            "object_id": oid, "object_type": "TeamsChatMessage", "channel": "teams",
            "at": _v(row, "created_at"), "mailbox": owner, "audience": aud, "sender": sender,
            "to": [owner], "cc": [], "to_me": True, "subject": "", "text": text[:TEXT_CHARS],
            "conversation_id": aud, "message_id": oid, "intents": intents_of("", text),
            "side": "internal", "records": [], "cleared": all(people.identity_clear(x)
                                                               for x in who),
            "handles": [c["l1_handle"] for c in row.values() if c.get("l1_handle")][:3]})
    return out


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
                   "decided_by", "decided_at"),
    "Batch": ("batch_number", "lot_number", "status", "quality_status", "tested_by",
              "checked_by", "released_at", "order_list", "po_number", "qc_ph",
              "qc_viscosity", "qc_appearance", "qc_odour", "qc_solids"),
    "Meeting": ("mailbox", "subject", "organizer", "attendees", "start", "end",
                "is_cancelled"),
}


def _record_events(st: Any, otype: str, kind: str, oids: Sequence[str],
                   typed: Mapping[str, tuple[str, str]], spec: Mapping[str, Any],
                   skipped: Counter) -> list[dict[str, Any]]:
    props = RECORD_PROPS.get(otype, (spec.get("time"), "status", "order_number", "po_number",
                                     "invoice_number", "shipment_number", "receipt_number"))
    rows = _read(st, oids, [p for p in props if p])
    now = datetime.now(timezone.utc)
    out = []
    for oid in oids:
        row = rows.get(oid) or {}
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
        event["event_id"] = _digest(kind, oid, _version_digest(st, oid))
        out.append(event)
    return out


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


def owners_of(rule: str, event: Mapping[str, Any], *, directory: Mapping[str, str],
              addresses_of: Mapping[str, set[str]], roles: Mapping[str, list[str]]
              ) -> tuple[list[str], str | None]:
    """(owner oids, None) or ([], the reason no owner is known: no item)."""
    if rule == "recipient":
        if event.get("event_type") != "message.received":
            return [], "owner_rule_mismatch"
        owner = str(event.get("mailbox") or "")
        if not owner or not (addresses_of.get(owner) or event.get("channel") == "teams"):
            return [], "owner_unknown"
        if not event.get("to_me"):
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
        found = _approvers(directory)
        return (found, None) if found else ([], "approvers_unknown")
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


def availability(ctx: Ctx, order: str) -> dict[str, Any]:
    """Available-to-promise date for an order's open lines: on hand first, then open
    purchase order lines by their expected date (Decimal, per the line's own unit; another
    order's demand is not netted: stated in ``basis``). Unknown when any line's item,
    quantity or supply date is unstated."""
    lines = ctx.linked(order, (SO_LINE_OF,))[:MAX_LINES]
    rows = ctx.read(lines, ("ordered_quantity", "shipped_quantity", "outstanding_quantity",
                            "uom", "description"))
    dates: list[date] = []
    out_lines, unknown = [], []
    for line in sorted(rows):
        r = rows[line]
        need = _decimal(_v(r, "outstanding_quantity"))
        if need is None:
            ordered, shipped = _decimal(_v(r, "ordered_quantity")), _decimal(
                _v(r, "shipped_quantity")) or Decimal(0)
            need = None if ordered is None else ordered - shipped
        if need is None:
            unknown.append(line)
            continue
        if need <= 0:
            continue
        items = ctx.linked(line, (SO_LINE_ITEM,))
        if len(items) != 1:
            unknown.append(line)
            continue
        item = items[0]
        irow = ctx.read([item], ("quantity_on_hand", "code", "name")).get(item) or {}
        on_hand = _decimal(_v(irow, "quantity_on_hand")) or Decimal(0)
        ctx.cite(irow, "quantity_on_hand")
        when: date | None = ctx.as_of if on_hand >= need else None
        supply = on_hand
        if when is None:
            po_lines = ctx.linked(item, (PO_LINE_ITEM,))[:MAX_LINES]
            prow = ctx.read(po_lines, ("ordered_quantity", "received_quantity",
                                       "bc_expected_receipt_date"))
            flows = []
            for pl, p in prow.items():
                due = (_decimal(_v(p, "ordered_quantity")) or Decimal(0)) - \
                    (_decimal(_v(p, "received_quantity")) or Decimal(0))
                at = _instant(_v(p, "bc_expected_receipt_date"))
                if at is None:
                    po = ctx.linked(pl, (PO_LINE_OF,))
                    head = ctx.read(po[:1], ("expected_date", "status")).get(po[0]) if po else {}
                    if head and CLOSED.search(_v(head, "status")):
                        continue
                    at = _instant(_v(head, "expected_date"))
                if due > 0 and at is not None and at.date() >= ctx.as_of - timedelta(days=30):
                    flows.append((max(at.date(), ctx.as_of), due, pl))
            for at, due, pl in sorted(flows):
                supply += due
                ctx.cite(prow[pl], "bc_expected_receipt_date", "ordered_quantity")
                if supply >= need:
                    when = at
                    break
        out_lines.append({"line": line, "item": _v(irow, "code") or _v(irow, "name"),
                          "need": str(need), "uom": _v(r, "uom"), "on_hand": str(on_hand),
                          "available": when.isoformat() if when else None})
        if when is None:
            unknown.append(line)
        else:
            dates.append(when)
    atp = max(dates).isoformat() if dates and not unknown else None
    return {"date": atp, "lines": out_lines, "unknown_lines": len(unknown),
            "basis": "on hand, then open purchase order lines by expected date; other "
                     "orders' demand is not netted"}


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
            lines.append("The requested documents are attached.")
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
                           and _v(r, "requested_at") < f.get("requested_at", "")),
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
        out["action"] = {"kind": "mc_approval", "gate": f.get("gate_id"),
                         "note": "Decide in Mission Control; the Workbench never decides it."}
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


HANDLERS: dict[str, Callable[[Ctx, Mapping[str, Any]], dict[str, Any]]] = {
    "order_entry": _work("h_order_entry"), "po_followup": _work("h_po_followup"),
    "supplier_eta": _work("h_supplier_eta"), "desk": _work("h_desk"),
    "answer_request": h_answer_request,
    "delay_impact": h_delay_impact, "approval_recommendation": h_approval,
    "task_prepare": h_task, "ledger_answer": h_ledger, "qc_impact": h_qc,
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
              gate: Callable[[str, str], bool] | None = None) -> dict[str, Any]:
    """One cycle: type the changed records, store each event once, prepare the work of each
    new event every matching autopilot declares, settle items their evidence closed.
    ``only_owners``: prepare only for these people (a replay of one role); ``gate(owner,
    workflow)``: prepare only what passes it (the live cycle: the workflows a role's replay
    passed, ``gate_from``)."""
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
    events, skipped = discover(st, ids, registry, people, landed_at=landed_at)
    discovered = time.monotonic()
    summary["skipped"].update(skipped)
    directory, addresses_of, domains = _people_maps(people)
    roles = _roles(environ, queue.path.parent)
    stamp = _iso(now)
    try:
        from . import autopilot_work as aw
        events += aw.po_due_events(st, registry, as_of, domains, directory, addresses_of)
    except sqlite3.Error:
        summary["skipped"]["due_events_unreadable"] += 1
    with queue.conn:
        for e in events:
            summary["events_seen"] += 1
            summary["events_new"] += queue.add_event(e, stamp)
    for event in queue.pending(int(limits["max_events_per_cycle"])):
        if time.monotonic() - started > limits["cycle_seconds"]:
            summary["status"] = "partial"      # the rest stay new: the next cycle goes on
            break
        _meeting_extra(st, event, domains)
        state, reason = _process(st, queue, event, registry, people=people,
                                 directory=directory, addresses_of=addresses_of, roles=roles,
                                 domains=domains, as_of=as_of, now=now, summary=summary,
                                 only_owners=None if only_owners is None else set(only_owners),
                                 gate=gate)
        with queue.conn:
            queue.mark(event["event_id"], state, reason, stamp)
    summary["discover_seconds"] = round(discovered - started, 2)
    if settle_items:
        summary["settled"] = settle(st, queue, registry, people, now=now)
    summary["seconds"] = round(time.monotonic() - started, 2)
    return _plain(summary)


def _process(st: Any, queue: Queue, event: dict[str, Any], registry: Mapping[str, Any], *,
             people: Any, directory: Mapping[str, str], addresses_of: Mapping[str, set[str]],
             roles: Mapping[str, list[str]], domains: Sequence[str], as_of: date,
             now: datetime, summary: dict[str, Any], only_owners: set[str] | None = None,
             gate: Callable[[str, str], bool] | None = None) -> tuple[str, str | None]:
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
    for ap in registry["autopilots"]:
        if ap["on"] != event["event_type"] or not matches(ap.get("when") or {}, event):
            continue
        if ap.get("enabled") is False:
            continue                       # switched off in the registry (``off_reason``)
        when = ap.get("when") or {}
        box = str(event.get("mailbox") or "")
        role = when.get("mailbox_role")
        if role and box not in _role_members(role, roles, directory):
            continue
        role = when.get("mailbox_role_none")
        if role and box in _role_members(role, roles, directory):
            continue
        owners, why = owners_of(ap["owner"], event, directory=directory,
                                addresses_of=addresses_of, roles=roles)
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
            owners = [o for o in owners if gate(o, workflow)]
            if not owners:
                summary["skipped"][f"{ap['id']}:workflow_not_passed"] += 1
                continue
        hour = now.strftime("%Y-%m-%dT%H")
        prepared = None
        for owner in owners:
            item_id = "ap_" + _digest(ap["id"], event["event_id"], owner)[:24]
            if queue.item(item_id) is not None:
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
                break
            due = now + timedelta(minutes=int(ap["due_minutes"]))
            landed = _instant(event.get("landed_at") or event.get("at"))
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
              ) -> tuple[Callable[[str, str], bool], dict[str, list[str]]]:
    """The live gate: an owner's workflow is on only when a replay of a role the owner was
    measured in judged at least ``GATE_MIN_JUDGED`` items of it with at least
    ``GATE_PRECISION`` matched (recomputed here from the counts, never a stored verdict).
    No file, an unreadable one or another schema: nothing is on (fail closed)."""
    path = Path(environ.get(GATE_ENV) or queue_dir / GATE_FILE)
    on: dict[str, set[str]] = defaultdict(set)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema") != GATE_SCHEMA:
            raise ValueError("schema")
        for role in (raw.get("roles") or {}).values():
            members = [str(m).lower() for m in role.get("members") or ()]
            for name, counts in (role.get("workflows") or {}).items():
                judged, matched = int(counts["judged"]), int(counts["matched"])
                if judged >= GATE_MIN_JUDGED and 0 <= matched <= judged and \
                        Decimal(matched) / Decimal(judged) >= GATE_PRECISION:
                    for m in members:
                        on[m].add(str(name))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        on = defaultdict(set)
    passed = {m: sorted(w) for m, w in on.items()}
    return (lambda owner, workflow: workflow in on.get(str(owner).lower(), ())), passed


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
    cites = list(dict.fromkeys([*(event.get("handles") or ()), *ctx.handles]))[:40]
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


def settle(st: Any, queue: Queue, registry: Mapping[str, Any], people: Any, *,
           now: datetime) -> Counter:
    """Mark open items done from evidence (module doc)."""
    from . import proactive_threads as pt
    done: Counter = Counter()
    directory, addresses_of, _domains = _people_maps(people)
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
            elif rule == "approval_decided" and oid:
                r = _read(st, [oid], ("status",)).get(oid)
                if r and _v(r, "status").lower() not in ("", "pending"):
                    reason = "approval_decided"
            elif rule == "task_closed" and oid:
                r = _read(st, [oid], ("stage", "completed_at")).get(oid)
                if r and (TASK_DONE.match(_v(r, "stage")) or _v(r, "completed_at")):
                    reason = "task_closed"
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
            refused = decide(item["citations"]) if item["citations"] else {}
            if any(v is not None for v in (refused or {}).values()):
                continue
        except Exception:  # noqa: BLE001 - an undecidable item is not shown
            continue
        item["mine"] = item["owner"] in mine
        out.append(item)
    return out


def digest(items: Sequence[Mapping[str, Any]], now: datetime) -> dict[str, Any]:
    """A person's daily digest: counts per autopilot, items due today, nudges for items
    past due (oldest first)."""
    stamp = _iso(now)
    by = Counter(i["autopilot"] for i in items)
    late = sorted((i for i in items if i["due_at"] < stamp), key=lambda i: i["due_at"])
    today = [i for i in items if i["due_at"][:10] == stamp[:10]]
    from . import autopilot_work as aw
    return {"open": len(items), "by_autopilot": dict(sorted(by.items())),
            "due_today": len(today), "brief": aw.brief(items, now),
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

def _store(path: str) -> Any:
    from . import proactive_engine as pe
    return pe._Store(Path(path), threads=4)


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
    people = pe._people(store)
    st = _store(a.store)
    capture_dir = a.capture_state or a.state_dir
    if capture_dir and (Path(capture_dir) / "mail-capture" / "capture.db").exists():
        from . import autopilot_work as aw
        st.autopilot_capture = aw.CaptureTexts(capture_dir)
    try:
        if a.command == "follow":
            ids, taken = layer_ids(store, queue, st.digest)
            gate, passed = gate_from(os.environ, queue.path.parent)
            if not passed:            # off until a role's replay passed (``gate_from``)
                print(json.dumps({"schema": SCHEMA, "status": "off",
                                  "reason": "no_workflow_passed_replay"}))
                return 0
            summary = run_cycle(st, queue, ids, people=people, registry=registry, gate=gate)
            summary["workflows_on"] = {"owners": len(passed),
                                       "workflows": sorted({w for v in passed.values()
                                                            for w in v})}
            if summary["status"] in ("complete", "partial"):
                with queue.conn:
                    for layer in taken:
                        queue.conn.execute("INSERT OR IGNORE INTO layers VALUES (?, ?)",
                                           (layer, st.digest))
        else:
            until = a.until or _iso(datetime.now(timezone.utc))
            since = _iso(_instant(until) - timedelta(days=a.days))
            ids = window_ids(st, registry, since, until)
            summary = run_cycle(st, queue, ids, people=people, registry=registry,
                                now=_instant(until), settle_items=False)
            summary["drained"] = _drain(st, queue, people, registry, _instant(until))
            if a.layer:
                with contextlib.closing(sqlite3.connect(f"file:{a.layer}?mode=ro", uri=True)
                                        ) as conn:
                    owned = [r[0] for r in conn.execute("SELECT object_id FROM delta_owned")]
                lst = _store(a.layer)
                try:
                    summary["layer"] = run_cycle(lst, queue, owned, people=people,
                                                 registry=registry, now=_instant(until),
                                                 settle_items=False)
                    summary["layer"]["drained"] = _drain(lst, queue, people, registry,
                                                         _instant(until))
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
