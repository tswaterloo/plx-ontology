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
