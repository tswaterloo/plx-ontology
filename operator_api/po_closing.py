"""Closing a purchase order inside the Workbench: a person's "closed: why, who, when".

It is a Workbench state only: never a write to Business Central, the Portal or Sage. The rows
live in ``assist.po_closures`` (migration 0080), append-only: a 'closed' row closes the order, a
later 'reopened' row undoes it, and the newest row of an order decides. A closed order is left
out of every late list and count for everyone (the late reads take ``closed_param`` as their
``closed`` parameter, ``operating.page_reads``); its own page shows who closed it, when and why,
and any receipt or message that arrived after.

Anyone who can see the order may close it or undo a close: the order is read through the same
door as the reader's late list (``page_sql.read``), so a reader who may not see it is told it is
not found.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from . import models as m
from .errors import UserFacing

CHUNK = 1000             # record ids asked of the message index at once
MAX_REASON = 300          # a note, not a document: longer is refused with a said reason, never cut


def _pool(backend: Any) -> Any:
    return getattr(getattr(backend, "memory", None), "pool", None)


def closed_param(backend: Any) -> str:
    """The handles of the orders now closed, one per line, for the late reads' ``closed`` parameter.
    A backend with no Postgres store has no closures. A store that cannot be read raises: a late
    list is never shown with the closed orders back in it."""
    pool = _pool(backend)
    if pool is None:
        return ""
    with pool.connection() as pg:
        rows = pg.execute(
            "SELECT handle FROM (SELECT DISTINCT ON (handle) handle, action FROM "
            "assist.po_closures ORDER BY handle, closure_id DESC) n WHERE action = 'closed' "
            "ORDER BY handle").fetchall()
    return "\n".join(r[0] for r in rows)


def _messages(backend: Any, record_ids: list[str]) -> dict[str, list[datetime]]:
    """The times of the mail and Teams posts that name each of these records, from the content-free
    index (``biz_person.record_comms``: no subject, sender or text). A store that cannot be read
    raises: a late list is never grouped on a guess about messages."""
    pool = _pool(backend)
    if pool is None or not record_ids:
        return {}
    from operating.pg import sql_views
    with pool.connection() as pg:
        rows = pg.execute(
            f"SELECT record_id, at FROM {sql_views.person_schema()}.record_comms WHERE "
            "record_id = ANY(%s) AND comm_view IN ('emails', 'teams_messages') AND at IS NOT NULL",
            (record_ids,)).fetchall()
    out: dict[str, list[datetime]] = {}
    for rid, at in rows:
        out.setdefault(rid, []).append(at)
    return out


def params(backend: Any, reader: Any) -> dict[str, str]:
    """The ``closed`` and ``alive`` parameters of the late reads: the orders a person closed, and
    the very late orders a mail or Teams post names on or after their needed-by date (a sign of
    life the door cannot read itself). One definition of "stale" for Home, the late list, the
    sidebar and the order's page (``operating.page_reads.STALE``)."""
    from . import page_sql
    closed = closed_param(backend)
    from operating.page_reads import MAX_ROWS
    cands: list[dict[str, Any]] = []
    after = ""
    while True:                      # every page of candidates, until a short one: no row is cut
        rows, _ids = page_sql.read(backend, reader, [
            {"name": "late_po_candidates", "params": {"closed": closed, "after": after}}])
        page = rows["late_po_candidates"]
        cands += page
        if len(page) < MAX_ROWS:
            break
        after = page[-1]["handle"]
    by_record: dict[str, list[datetime]] = {}
    ids = [r for c in cands for r in (c.get("record_ids") or [])]
    for k in range(0, len(ids), CHUNK):      # the message index in chunks of ids
        by_record.update(_messages(backend, ids[k:k + CHUNK]))
    alive = []
    for c in cands:
        need = c.get("requested_date")
        if any(at.date() >= need for r in (c.get("record_ids") or []) for at in by_record.get(r, [])
               ) if need is not None else False:
            alive.append(c["handle"])
    return {"closed": closed, "alive": "\n".join(alive)}


def stamp(backend: Any) -> Optional[int]:
    """The newest closure row (a kept screen is served again once it moves)."""
    pool = _pool(backend)
    if pool is None:
        return None
    with pool.connection() as pg:
        return pg.execute("SELECT max(closure_id) FROM assist.po_closures").fetchone()[0]


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None


def _signs(backend: Any, reader: Any, handle: str, since: Optional[datetime]
           ) -> Optional[dict[str, Any]]:
    """The order's receipts, and the mail and Teams posts after ``since`` (all when None), as this
    reader sees the order; None when the reader may not see it."""
    from . import page_sql
    rows, _ids = page_sql.read(backend, reader, [
        {"name": "po_signs", "params": {"handle": handle}}])
    if not rows["po_signs"]:
        return None
    row = dict(rows["po_signs"][0])
    got = _messages(backend, list(row.get("record_ids") or []))
    row["messages"] = len({(r, at) for r, ats in got.items() for at in ats
                           if since is None or at > since})
    return row


def status(backend: Any, reader: Any, handle: str, *, must_see: bool = False) -> m.PoClosure:
    """The order's close state, and what showed up after a person closed it (a closed order
    never reopens by itself: it says what is new and leaves the choice to a person)."""
    if must_see and _signs(backend, reader, handle, None) is None:
        raise UserFacing("not_found", "That purchase order is not on your list.", 404)
    pool = _pool(backend)
    if pool is None:
        return m.PoClosure(closed=False)
    with pool.connection() as pg:
        row = pg.execute(
            "SELECT action, reason, by_person, at, receipts FROM assist.po_closures WHERE "
            "handle = %s ORDER BY closure_id DESC LIMIT 1", (handle,)).fetchone()
    if row is None or row[0] != "closed":
        return m.PoClosure(closed=False)
    new: list[str] = []
    got = _signs(backend, reader, handle, row[3])
    if got is not None:
        more = int(got["receipts"]) - int(row[4] or 0)
        if more > 0:
            new.append("A receipt was recorded" if more == 1 else f"{more} receipts were recorded")
        if int(got["messages"]) > 0:
            n = int(got["messages"])
            new.append("A message names it" if n == 1 else f"{n} messages name it")
    return m.PoClosure(closed=True, reason=row[1], by=row[2], at=_iso(row[3]),
                       new_since=[w + " after it was closed" for w in new])


def _append(backend: Any, reader: Any, handle: str, action: str, reason: Optional[str]
            ) -> m.PoClosure:
    if not isinstance(handle, str) or not handle:
        raise UserFacing("bad_request", "Say which purchase order.", 400)
    note = (reason or "").strip() or None
    if note and len(note) > MAX_REASON:
        raise UserFacing("bad_request", f"Keep the reason under {MAX_REASON} characters.", 400)
    signs = _signs(backend, reader, handle, None)     # the reader's door: None = may not see it
    if signs is None:
        raise UserFacing("not_found", "That purchase order is not on your list.", 404)
    pool = _pool(backend)
    if pool is None:
        raise UserFacing("unavailable", "Closing is not available here.", 503)
    now = status(backend, reader, handle)
    if (action == "closed") == now.closed:        # closing a closed order, or undoing an open one
        return now
    with pool.connection() as pg:
        pg.execute("INSERT INTO assist.po_closures (handle, action, reason, by_person, receipts) "
                   "VALUES (%s, %s, %s, %s, %s)",
                   (handle, action, note, reader.display_name, int(signs["receipts"])))
        pg.commit()           # the pool's hand-back rolls an open transaction back
    return status(backend, reader, handle)


def close(backend: Any, reader: Any, handle: str, reason: Optional[str]) -> m.PoClosure:
    return _append(backend, reader, handle, "closed", reason)


def reopen(backend: Any, reader: Any, handle: str) -> m.PoClosure:
    return _append(backend, reader, handle, "reopened", None)


def handle_of(backend: Any, reader: Any, handle: Optional[str], object_id: Optional[str]) -> str:
    """The order's handle from either id; not found when the reader's door does not hold it."""
    if handle:
        return handle
    if not object_id:
        raise UserFacing("bad_request", "Say which purchase order.", 400)
    from . import page_sql
    try:
        return page_sql.resolve(backend, reader, "purchase_order", object_id)
    except page_sql.NotFound:
        raise UserFacing("not_found", "That purchase order is not on your list.", 404) from None
