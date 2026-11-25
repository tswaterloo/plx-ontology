"""Reading helpers of the autopilot: property reads, typed links, instants and addresses of a
projected record (``operating.autopilot``, which re-exports them)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence


# ----------------------------------------------------------------------------- links

def shown(text: Any, most: int) -> str:
    """``text`` to ``most`` characters; when cut, the cut is said in the text itself, so
    the model reading it knows there was more."""
    text = str(text or "")
    if len(text) <= most:
        return text
    return f"{text[:most]} [cut: {len(text) - most} more characters not shown]"


def shown_list(items: Sequence[Any], most: int, *, newest: bool = True) -> list[Any]:
    """At most ``most`` of ``items`` (the newest when ``newest``), with a line saying how
    many were left out when any were."""
    items = list(items)
    if len(items) <= most:
        return items
    left = len(items) - most
    kept = items[-most:] if newest else items[:most]
    note = f"[{left} {'earlier' if newest else 'more'} not shown]"
    return [note, *kept] if newest else [*kept, note]


def object_href(object_id: Any) -> str:
    """The app address of a record's page, ``/object/<type>/<id>`` (the type is the id's own
    prefix, ``SalesOrder:portal...`` -> ``SalesOrder``); a card link built any other way opens
    Page not found."""
    from urllib.parse import quote
    oid = str(object_id or "")
    kind = oid.split(":", 1)[0] if ":" in oid else "Object"
    return f"/object/{quote(kind, safe='')}/{quote(oid, safe='')}"


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


def body_texts(st: Any, handles: Sequence[str], *, field_path: str = "row.body_text") -> dict[str, str]:
    """handle -> full message body text held by the store's text index (one read per 500
    handles); a handle without one, or an unreadable index, is left out."""
    import sqlite3
    from .mail_text import joined_chunks
    rows: dict[str, list[tuple[int, bytes]]] = {}
    unique = sorted({h for h in handles if h})
    if not unique:
        return {}
    try:
        for start in range(0, len(unique), 500):
            for handle, at, body in st.conn.execute(
                    "SELECT f.citation, k.start, k.body FROM text_fields f JOIN text_chunks k "
                    "ON k.field_id = f.field_id WHERE f.citation IN (SELECT value FROM "
                    "json_each(?)) AND f.field_path = ? ORDER BY f.citation, k.start",
                    (json.dumps(unique[start:start + 500]), field_path)):
                rows.setdefault(handle, []).append((at, body))
        return {handle: joined_chunks(parts) for handle, parts in rows.items() if parts}
    except (sqlite3.Error, ValueError, TypeError, AttributeError):
        return {}


def body_texts_by_oid(st: Any, oids: Sequence[str], *, field_path: str = "row.body_text") -> dict[str, str]:
    """object_id -> full message body text from ``object_versions`` + ``text_fields``/``text_chunks``
    (one batched lookup per 500 object ids)."""
    import sqlite3
    unique = sorted({o for o in oids if o})
    if not unique:
        return {}
    oid_to_handle: dict[str, str] = {}
    try:
        for start in range(0, len(unique), 500):
            for oid, handle in st.conn.execute(
                    "SELECT object_id, l1_handle FROM object_versions WHERE object_id IN "
                    "(SELECT value FROM json_each(?)) AND superseded_at IS NULL",
                    (json.dumps(unique[start:start + 500]),)):
                if handle:
                    oid_to_handle[oid] = handle
    except (sqlite3.Error, ValueError, TypeError, AttributeError):
        return {}
    by_handle = body_texts(st, list(oid_to_handle.values()), field_path=field_path)
    return {oid: by_handle[h] for oid, h in oid_to_handle.items() if h in by_handle and by_handle[h]}


def message_body(row: Mapping[str, Any] | None, bodies: Mapping[str, str] | None = None) -> str:
    """Full message text from the store's text index (``bodies``) or ``body_text`` property,
    falling back to ``body_preview`` only when full text is genuinely absent."""
    if bodies and row:
        for cell in row.values():
            if isinstance(cell, Mapping) and cell.get("l1_handle"):
                full = bodies.get(str(cell["l1_handle"]))
                if full:
                    return full
    return _v(row, "body_text") or _v(row, "body_preview")


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
# how far back ``Queue.own_posts`` counts a person's own messages in a conversation
OWN_POSTS_DAYS = 14


def _iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def own_sent(st: Any, boxes: Mapping[str, Iterable[str]], since: str, until: str
             ) -> list[dict[str, Any]]:
    """The mail each mailbox's owner sent in [since, until), read from the captured mail in
    the store (the copy in their own mailbox: audience ``mail:<mailbox>``, sent from one of
    their addresses), as ``Queue.note_sent`` notes (owner, conversation, message id,
    recipients, time). ``boxes``: mailbox -> its owner's addresses."""
    own = {f"mail:{str(b).lower()}": (str(b), {str(a).lower() for a in addrs})
           for b, addrs in boxes.items()}
    if not own:
        return []
    ids = sorted({r[0] for r in st.conn.execute(
        "SELECT DISTINCT eo.object_id FROM events e CROSS JOIN event_objects eo ON "
        "eo.event_id = e.event_id WHERE e.event_type = 'Email.received' AND e.time >= ? AND "
        "e.time < ?", (since, until)).fetchall() if str(r[0]).startswith("Email:")})
    out: list[dict[str, Any]] = []
    for start in range(0, len(ids), 2000):
        typed = _types(st, ids[start:start + 2000])
        mine = {o: own[aud.lower()] for o, (_t, aud) in typed.items() if aud.lower() in own}
        rows = _read(st, mine, ("sender", "to_addresses", "cc_addresses", "conversation_id",
                                "internet_message_id", "sent_at", "received_at"))
        for oid, r in rows.items():
            box, addrs = mine[oid]
            sender = (_addresses(_v(r, "sender")) or [""])[0]
            at = _v(r, "sent_at") or _v(r, "received_at")
            if sender not in addrs or not at or not since <= at < until:
                continue
            out.append({"owner": box, "conversation": _v(r, "conversation_id") or None,
                        "message_id": _v(r, "internet_message_id") or oid, "at": at,
                        "to": _addresses(_v(r, "to_addresses")) +
                        _addresses(_v(r, "cc_addresses"))})
    return out
