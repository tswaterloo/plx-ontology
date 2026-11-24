"""Served pages (speed plan section 2.4): a GET route is the reader's session, then one read
through the reader's door, passed through. No kept page, no "reading" answer, no access check
of a copy kept for another moment: the door decides every read at its own snapshot (``door.
check`` in the same transaction, ``ontology_tools_sql._decide``), so what a read returns is
what the reader may see now, and there is nothing older to check.

``SRC`` holds where the request's page came from, for the latency log (``app``): ``items``
(the precomputed page, ``operating.pg.record_pages``) or ``views`` (the business views read in
that statement). A purchase order page whose precomputed items cannot say it whole (none for
the record, items older than a view they rest on, more lines than the page shows) reads the
views and says so in ``SRC``; it is never computed in Python."""
from __future__ import annotations

import contextvars
import json
import logging
from typing import Any, Optional, Sequence

from . import errors
from . import models as m

SRC: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("plx_served_src",
                                                                   default=None)


def biz_page(backend: Any, reader: Any, kind: str, handle: str = "", chain: str = "",
             record: str = "") -> m.BizPage:
    """An account page (customer, chain, supplier) or a record's page (purchase order, sales
    order, product, batch, supplier invoice) from the business tables through the reader's
    door (switch ``PLX_PAGES_DOOR``). ``record``: a record id instead of the row's handle."""
    from . import page_sql
    if not page_sql.on(kind):
        raise errors.UserFacing("pages_off", "These record pages aren't switched on on "
                                "this server yet.", 404)
    try:
        if kind == "purchase_order" and record and not handle and page_sql.record_door_on():
            done = page_sql.order_from_record(backend, reader, record)
            page_sql.count_door(done is not None)
            if done is not None:
                SRC.set("items")
                return done
        SRC.set("views")
        at = page_sql.resolve(backend, reader, kind, record) \
            if record and not handle and kind != "chain" else handle
        if kind in ("purchase_order", "sales_order"):
            return page_sql.order(backend, reader, kind, at)
        if kind == "product":
            return page_sql.item(backend, reader, at)
        if kind == "batch":
            return page_sql.batch(backend, reader, at)
        if kind == "supplier_invoice":
            return page_sql.supplier_invoice(backend, reader, at)
        return page_sql.account(backend, reader, kind, handle=at, chain=chain)
    except page_sql.NotFound:
        raise errors.UserFacing("not_found", "No such record that you can open.",
                                404) from None
    except page_sql.PageDoorUnavailable as exc:
        why = ("took too long" if exc.code == "timeout" else
               "is busy" if exc.code == "busy" else
               "isn't built yet" if exc.code == "not_ready" else "couldn't be read")
        raise errors.UserFacing("page_unavailable", f"The record's figures {why}; "
                                "try again in a minute.", 503) from None


# ---- the served store (speed specs 1.2): one statement over ``<biz>_page.page_items`` ---------

READ_SECONDS = 0         # no statement limit (business rule 11-23: speed comes from indexes and precompute; 0 = none); lock_timeout stays
LOCK_MS = 100            # an apply holds row locks only; past this the read answers busy
REQUEST_LOCK_MS = 50     # a build request waits no longer for its row
# the page's data conflicts (items: ``operating.pg.record_history.conflict_items``) in the same
# statement as the page: the record's history head says whether they were built (its ``conflicts``
# state), then one item per value of a raw conflict and per early receipt
CONFLICT_PART = (
    "(SELECT (SELECT h.payload -> 'conflicts' FROM page_items h WHERE h.page_kind = 'history' AND "
    "h.page_key = %(key)s AND h.section = 'history_head') AS head, COALESCE(jsonb_agg("
    "jsonb_build_array(c.section, c.payload) ORDER BY c.section, c.seq), '[]'::jsonb) AS rows "
    "FROM page_items c WHERE %(kind)s = 'record' AND c.page_kind = 'history' AND c.page_key = "
    "%(key)s AND c.section IN ('conflict', 'received')) k")
PAGE_SQL = (
    "SELECT COALESCE(jsonb_object_agg(s.section, s.items), '{}'::jsonb)::text, "
    "min(s.as_of), max(s.stale_since), sum(s.n), max(k.head::text)::jsonb, max(k.rows::text)::jsonb "
    "FROM (SELECT section, "
    "jsonb_agg(jsonb_build_object('v', payload, 'withheld', withheld, 'col', cellcol) "
    "ORDER BY seq) AS items, min(as_of) AS as_of, max(stale_since) AS stale_since, "
    "count(*) AS n FROM page_items WHERE page_kind = %(kind)s AND page_key = %(key)s "
    "AND (%(sections)s::text[] IS NULL OR section = ANY(%(sections)s::text[])) "
    "GROUP BY section) s, LATERAL " + CONFLICT_PART)
# a record page with no item the reader may see: does the record exist for this reader (the
# door's records), and has the page items at all (one the door hides entirely is told from one
# not built yet; ``biz_page.page_built`` answers for the key given, never lists keys)
EXISTS_SQL = "SELECT 1 FROM records WHERE record_id = %(key)s"
BUILT_SQL = "SELECT page_built(%(kind)s, %(key)s)"
requests_failed = 0      # build requests not written (counted, logged; the page said not_run)


class Unavailable(Exception):
    """The store could not answer now (503 with ``code``: busy, timeout, not_ready)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _door_for(backend: Any, reader: Any) -> tuple[Any, str, Optional[dict]]:
    """The reader's door decided ahead (``operator_api.doors``), else decided now (logged
    ``door_not_ahead`` by ``Doors.get``: a refresher bug, alarmed) or not served."""
    doors = getattr(backend, "doors", None)
    if doors is None:
        raise Unavailable("not_ready")
    held = doors.get(reader) or doors.decide_now(reader)
    if held is None:
        raise Unavailable("not_ready")
    return held if len(held) == 3 else (*held, None)


def read(backend: Any, reader: Any, kind: str, key: str,
         sections: Optional[Sequence[str]] = None,
         comms_after: Optional[tuple[str, str, str]] = None) -> tuple[bytes, dict[str, str]]:
    """(the page's body, its headers): one READ ONLY transaction on the page pool, on the
    calling thread: the door's stamp and the people exclusion checked, the reader's settings,
    the one statement (``PAGE_SQL``). The sections' JSON is passed through as Postgres wrote
    it. No item: ``absent`` decides (never computed here)."""
    from operating.ontology_tools_sql import Busy, Door, ToolError, scoped_read
    from operating.page_reads import pool_for
    from operating.pg.pool import PoolTimeout
    door, dsn, comms = _door_for(backend, reader)
    args = {"kind": kind, "key": key, "sections": list(sections) if sections else None}

    def work(pg: Any) -> Any:
        if kind == "alias":
            return _alias_rows(pg, key, args), (False, False), None
        text, as_of, stale, n, head, rows = pg.execute(PAGE_SQL, args).fetchone()
        row = (text, as_of, stale, n)
        if row[3] is None and kind == "record":
            exists = pg.execute(EXISTS_SQL, args).fetchone() is not None
            return row, (exists, exists and bool(pg.execute(BUILT_SQL, args).fetchone()[0])), None
        return row, (False, False), (head, [tuple(r) for r in rows]) \
            if kind == "record" else None

    def after(pg: Any, got: Any) -> Any:
        kept = got[2]
        cites = sorted({h for _s, p in (kept[1] if kept else ()) if p
                        for h in (p.get("need") or {}).get("cites") or ()})
        ok = _readable_cites(pg, sql_views.person_schema(), _built_views(pg, comms), cites) \
            if cites else set()
        return _comms(pg, kind, key, comms_after, comms), ok
    from operating.pg import sql_views
    try:
        with pool_for(dsn).connection() as pg:
            got = scoped_read(
                pg, Door(door.grant), work, READ_SECONDS, person=True, lock_ms=LOCK_MS,
                extra=comms, after=after if comms else None)
            (text, as_of, stale, n), exists, kept = got[0] if comms else got
            comms_text, cites_ok = got[1] if comms else (None, set())
    except PoolTimeout:
        raise Unavailable("busy") from None
    except Busy:
        raise Unavailable("busy") from None
    except ToolError as exc:
        raise Unavailable("timeout" if exc.code == "timeout" else "not_ready") from None
    if n is None:
        return absent(kind, key, exists[0], dsn, built=exists[1])
    status = "stale" if stale is not None else "ok"
    why = (f', "why": "updated {_et(as_of)}, newer data is being prepared"'
           if stale is not None else "")
    mail = comms_text if comms_text is not None else json.dumps(
        {"status": "off", "why": "this reader's mail settings are not decided yet"})
    tail = (b', "comms": ' + mail.encode() + b', "conflicts": ' + json.dumps(
        record_conflicts(kept, cites_ok if comms else None), default=str).encode()
        if kind == "record" else b"")
    body = (f'{{"kind": {json.dumps(kind)}, "key": {json.dumps(key)}, "status": "{status}", '
            f'"as_of": "{as_of.isoformat()}"{why}, "sections": ').encode() \
        + text.encode() + tail + b"}"
    SRC.set("stale" if stale is not None else "items")
    return body, {"X-PLX-As-Of": as_of.isoformat(), "X-PLX-Src": SRC.get() or "items"}


def alias_keys(code: str) -> list[str]:
    """The identifiers a typed code is looked up as: as written (trimmed) and upper-cased, the
    two ``identifier_records`` reads (one definition of the lookup)."""
    return sorted({code.strip(), code.strip().upper()} - {""})


def _alias_rows(pg: Any, code: str, args: dict) -> tuple:
    """The candidates the reader may open for a typed code, one statement per way of writing it
    (``alias_keys``), their items together once per record id. (text, as_of, stale, n) like a page's;
    n None when the reader may open none (the page then reads as not found)."""
    items: dict[str, Any] = {}
    as_ofs, stales = [], []
    for k in alias_keys(code):
        text, as_of, stale, n, _head, _rows = pg.execute(PAGE_SQL, {**args, "key": k}).fetchone()
        if n is None:
            continue
        as_ofs.append(as_of)
        stales.append(stale)
        for item in json.loads(text).get("alias") or ():
            items.setdefault(str((item.get("v") or {}).get("record_id")), item)
    if not items:
        return None, None, None, None
    return (json.dumps({"alias": list(items.values())}), min(as_ofs),
            max((x for x in stales if x is not None), default=None), len(items))


def record_conflicts(kept: Any, cites_ok: Optional[set]) -> dict:
    """The record page's conflict cards for THIS reader: the values the door let through (a value
    whose handle this reader may not open is not a row they receive; one that rests on mail, a
    chat or a file whose item gate they fail is dropped here by ``payload.need.cites``) run
    through ``operating.answer_conflicts.tool_conflicts``, the rules ``backend._conflicts`` runs,
    then the order-date-against-receipt card (``backend.received_conflict``). With one value of a
    disagreement left there is no card, and nothing says a hidden value exists. ``{"status",
    "items"}``; ``not_built`` when the record's history was stored without them (never an empty
    list read as none)."""
    from operating.answer_conflicts import tool_conflicts
    from .backend import received_conflict
    from .backend_ask import AskMixin
    meta, rows = kept
    meta = meta or {}
    if not meta.get("built"):
        return {"status": "not_built", "items": []}
    ok = cites_ok if cites_ok is not None else set()
    subject = meta.get("subject") or ""
    values: dict[int, list[dict]] = {}
    early: list[tuple[str, str]] = []
    first: dict[str, Any] = {}
    for section, p in rows:
        if not p or any(h not in ok for h in (p.get("need") or {}).get("cites") or ()):
            continue                 # emptied by the people exclusion, or an item gate they fail
        if section == "conflict":
            values.setdefault(p["c"], []).append(p)
        else:
            early.append((p["when"], p["handle"]))
            first = {"ordered": p["ordered"], "order_handle": p["order_handle"]}
    raw = []
    for c in sorted(values):
        mine = sorted(values[c], key=lambda p: p["j"])
        raw.append({"kind": mine[0]["kind"], "subject": mine[0]["subject"],
                    "tier": mine[0]["tier"], "values": [p["v"] for p in mine],
                    "resolution": {"as_of": [p["as_of"] for p in mine]}})

    def source(handle: str, **kw: Any) -> Any:
        return AskMixin._source(None, handle, openable=True, **kw)

    cards = []
    for found in tool_conflicts(raw, visible=set, subject=subject):
        cards.append(m.Conflict(field=found["field"], subject=subject, values=[
            m.ConflictValue(value=v["value"], trusted=v["trusted"], why=v["why"],
                            source=source(v["source"], fields={found["field"]: v["value"]}))
            for v in found["values"]]))
    if early:
        cards.append(received_conflict(source, subject, {**first, "early": early}))
    return {"status": "ok", "items": [c.model_dump(mode="json") for c in cards]}


FILES_FILTER = (" AND (current_setting('plx.reader_files', true) = 'full' OR c.handle IN (SELECT "
                "json_array_elements_text(NULLIF(current_setting('plx.reader_files_ok', true), '')"
                "::json)))")
PAGE = 200      # messages per page of a kind: a page of keyset paging, not a cut; ``next`` reaches the rest
# (view, the person view's columns of the item: payload key = column, an optional text expression)
COMMS_KINDS = (("emails", ("subject", "sender", "mailbox"), "left(c.said, 200)"),
               ("attachments", ("name", "mailbox", "extension"), None),
               ("teams_messages", ("subject", "sender", "chat"), None),
               ("sharepoint_files", ("name", "path"), None))


def comms_sql(person: str, built: Optional[Sequence[str]] = None, hidden: bool = False) -> str:
    """The page's linked messages: the content-free index (``record_comms``) joined to each
    GUARDED person view in the same statement, so a row the reader may not read is dropped by the
    view's own predicate (the reader's six settings are set for this transaction). Per kind: the
    reader's EXACT visible count and one keyset page, newest first (``PAGE`` rows after the cursor
    ``after_at`` / ``after_h``, the cursor only for the kind named ``view``); ``next`` is the cursor of
    the following page, null at the end. Nothing is cut: paging reaches every message."""
    parts, out = [], []
    for view, cols, start in COMMS_KINDS:
        if built is not None and view not in built:
            continue
        # a file is kept only for a FULL reader (the door alone) or when its handle is among the
        # linked files this reader's item gate allowed (decided with the door, ``FILES_OK``)
        gate = (FILES_FILTER if view == "sharepoint_files" else "")
        payload = ", ".join(f"'{c}', c.{c}" for c in cols) + (f", 'start', {start}" if start else "")
        parts.append(
            f"w_{view} AS MATERIALIZED (SELECT rc.at, c.handle, jsonb_build_object({payload}) AS p "
            f"FROM {person}.record_comms rc JOIN {person}.{view} c ON c.handle = rc.comm_handle "
            f"WHERE rc.record_id = %(key)s AND rc.comm_view = '{view}'{gate}), "
            f"p_{view} AS MATERIALIZED (SELECT * FROM w_{view} WHERE (%(view)s::text IS DISTINCT FROM "
            f"'{view}' OR %(after_h)s::text IS NULL OR (at, handle) < (%(after_at)s::timestamptz, "
            f"%(after_h)s::text)) ORDER BY at DESC, handle DESC LIMIT {PAGE + 1})")
        out.append(
            f"'{view}', jsonb_build_object('total', (SELECT count(*) FROM w_{view}), 'hidden', "
            + (f"(SELECT COALESCE(sum(n), 0) FROM {person}.record_comms_hidden WHERE record_id = "
               f"%(key)s AND comm_view = '{view}')" if hidden else "NULL") + f", 'items', "
            f"COALESCE((SELECT jsonb_agg(jsonb_build_object('handle', handle, 'at', at, 'v', p) "
            f"ORDER BY at DESC, handle DESC) FROM (SELECT * FROM p_{view} ORDER BY at DESC, handle "
            f"DESC LIMIT {PAGE}) t), '[]'::jsonb), 'next', (SELECT CASE WHEN count(*) > {PAGE} THEN "
            f"(SELECT jsonb_build_object('at', at, 'handle', handle) FROM (SELECT * FROM p_{view} "
            f"ORDER BY at DESC, handle DESC OFFSET {PAGE - 1} LIMIT 1) z) END FROM p_{view}))")
    return ("WITH " + ", ".join(parts) + " SELECT jsonb_build_object('status', "
            "CASE WHEN %(failed)s::jsonb <> '{}'::jsonb THEN 'failed' WHEN %(lost)s::jsonb <> "
            "'[]'::jsonb OR %(off)s::jsonb <> '{}'::jsonb THEN 'partial' ELSE 'ok' END, "
            "'not_built', %(lost)s::jsonb, 'off', %(off)s::jsonb, 'failed', %(failed)s::jsonb, "
            f"{', '.join(out)})::text")


FILES_OFF = "this host has no item gate for files, so none is listed here"


def _comms(pg: Any, kind: str, key: str, cursor: Optional[tuple[str, str, str]] = None,
           settings: Optional[dict] = None) -> Optional[str]:
    """The comms section of a record page (``comms_sql``), read as the store's own role in the
    reading transaction; None for a page kind that has none."""
    if kind != "record":
        return None
    from operating.pg import sql_views
    view, at, handle = cursor or (None, None, None)
    person = sql_views.person_schema()
    names = [v for v, _, _ in COMMS_KINDS]
    built = [r[0] for r in pg.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE "
        "n.nspname = %s AND c.relname = ANY(%s)", (person, names + ["record_comms", "record_comms_hidden"]))]
    if "record_comms" not in built:   # no index yet: said, never an empty list
        return json.dumps({"status": "not_run", "why": "the page's message index is not built"})
    from operating.pg import business_comms as bc
    off = {}
    if (settings or {}).get(bc.FILES_SETTING) not in (bc.FILES_FULL, bc.FILES_SEARCH) \
            and "sharepoint_files" in built:
        built.remove("sharepoint_files")
        off["sharepoint_files"] = FILES_OFF      # typed off, with its reason; never total 0 and ok
    failed = {}
    state = (settings or {}).get("plx.reader_files_state") or ""
    if state and "sharepoint_files" in built:
        built.remove("sharepoint_files")
        failed["sharepoint_files"] = state      # typed failed with its reason; never "0 files"
    lost = [v for v in names if v not in built and v not in off and v not in failed]   # no view here
    return pg.execute(comms_sql(person, built, "record_comms_hidden" in built),
                      {"key": key, "view": view, "after_at": at, "after_h": handle,
                       "lost": json.dumps(lost), "off": json.dumps(off),
                       "failed": json.dumps(failed)}).fetchone()[0]


def absent(kind: str, key: str, exists: Optional[bool], dsn: str, built: bool = False
           ) -> tuple[bytes, dict[str, str]]:
    """A page with no item the reader may see: a record the reader may not see and one that
    does not exist both answer 404; one the reader may see whose items are all hidden from
    this reader answers ``partial`` with nothing in it and says so (never asks for a build);
    one that is not built yet answers ``not_run`` and asks for it."""
    if not exists:
        raise errors.UserFacing("not_found", "No such record that you can open.", 404)
    if built:
        SRC.set("hidden")
        body = json.dumps({"kind": kind, "key": key, "status": "partial", "as_of": None,
                           "why": "nothing on this page is open to you", "sections": {}}).encode()
        return body, {"X-PLX-Src": "hidden"}
    SRC.set("not_run")
    request_build(dsn, kind, key)
    body = json.dumps({"kind": kind, "key": key, "status": "not_run", "as_of": None,
                       "why": "this page is being prepared", "sections": {}}).encode()
    return body, {"X-PLX-Src": "not_run"}


PENDING: contextvars.ContextVar[Optional[tuple[str, str, str]]] = contextvars.ContextVar(
    "plx_served_build_request", default=None)


def request_build(dsn: str, kind: str, key: str) -> None:
    """Ask the builders for this page (after the response: the route's background task
    writes it, ``write_request``)."""
    PENDING.set((dsn, kind, key))


def write_request(dsn: str, kind: str, key: str) -> None:
    """One ``page_requests`` row (``ON CONFLICT DO NOTHING``); a failure is counted and
    logged, never the reader's (the page already said it is being prepared)."""
    global requests_failed
    import psycopg
    from operating.pg import sql_views
    try:   # its own short connection: the page pool's are read only
        with psycopg.connect(dsn, connect_timeout=2) as pg:
            with pg.transaction():
                pg.execute(f"SET LOCAL lock_timeout = '{REQUEST_LOCK_MS}ms'")
                # the API's login holds no access to the business schema: the page schema's
                # function writes the one row (migration 0064)
                pg.execute(f"SELECT {sql_views.page_schema()}.request_build(%s, %s)",
                           (kind, key))
    except Exception as exc:  # noqa: BLE001 - counted and logged: the builders' order only
        requests_failed += 1
        logging.getLogger("operator_api.served").warning(json.dumps(
            {"page_request_failed": type(exc).__name__, "failed": requests_failed}))


def _et(when: Any) -> str:
    from zoneinfo import ZoneInfo
    return when.astimezone(ZoneInfo("America/New_York")).strftime("%-I:%M %p ET")


# ---- the History tab, served (speed specs 9.3 V2; items: ``operating.pg.record_history``) ----------

HISTORY_ENTRIES = 25     # entries per page of the tab: a page of keyset paging, not a cut
HISTORY_CHUNK = 400      # items read per statement while a page fills (display paging only)
HISTORY_HEAD_SQL = (
    "SELECT payload, withheld, stale_since, as_of FROM page_items WHERE page_kind = 'history' "
    "AND page_key = %(key)s AND section = 'history_head'")
HISTORY_SQL = (
    "SELECT seq, payload, withheld FROM page_items WHERE page_kind = 'history' AND page_key = "
    "%(key)s AND section = 'history' AND seq < %(before)s ORDER BY seq DESC LIMIT %(n)s")
# anything the reader's door holds back of this history (the count view never counts the owner's
# alone) and any item read withheld: the tab says some changes are not shown, never how many
HISTORY_HIDDEN_SQL = (
    "SELECT COALESCE((SELECT sum(n) FROM page_items_hidden WHERE page_kind = 'history' AND "
    "page_key = %(key)s AND section = 'history'), 0) + (SELECT count(*) FROM page_items WHERE "
    "page_kind = 'history' AND page_key = %(key)s AND section = 'history' AND withheld <> '{}')")


def _held_classes_sql() -> str:
    from operating.pg import door
    return ("SELECT COALESCE(array_agg(t.access_class), '{}') FROM unnest(%(cids)s::int[]) "
            f"AS t(access_class) WHERE {door.held_class_sql().replace('t.handle', 'NULL::text')}")


CITE_VIEWS = (("graph-mail", "emails"), ("teams-chat", "teams_messages"),
              ("teams-channel", "teams_messages"), ("sharepoint", "sharepoint_files"))


def _cite_key(handle: str) -> tuple[Optional[str], str]:
    """(the communications view that guards ``handle``, the handle as that view holds it): a
    message's attachment or page (``...#...``) is its message's; another scheme has no view."""
    scheme = handle.split("://", 1)[0]
    view = next((v for s, v in CITE_VIEWS if s == scheme), None)
    return view, (handle.split("#", 1)[0] if view == "emails" else handle)


def _readable_cites(pg: Any, person: str, built: Sequence[str], cites: Sequence[str]) -> set:
    """The item-gated handles of ``cites`` this reader may read: each through its communications
    view's own guard (the reader's comms settings are this transaction's), a file also through the
    reader's item-gate set (``FILES_FILTER``); one ``handle = ANY`` lookup per view (its handle
    index). A handle of another scheme, or of a view not built here, is not readable."""
    wanted: dict[str, set[str]] = {}
    for h in cites:
        view, key = _cite_key(h)
        if view is not None and view in built:
            wanted.setdefault(view, set()).add(key)
    parts, args = [], {}
    for view, keys in sorted(wanted.items()):
        parts.append(f"SELECT c.handle FROM {person}.{view} c WHERE c.handle = ANY(%({view})s::text[])"
                     + (FILES_FILTER if view == "sharepoint_files" else ""))
        args[view] = sorted(keys)
    found = {r[0] for r in pg.execute(" UNION ".join(parts), args).fetchall()} if parts else set()
    return {h for h in cites if _cite_key(h)[1] in found and _cite_key(h)[0] in wanted}


def _built_views(pg: Any, settings: Optional[dict]) -> list[str]:
    """The communications views (mail, chats, files) built here that this reader's settings let
    a cited handle be read through (a file is not readable without the files setting, or while
    its state is failing)."""
    from operating.page_reads import FILES_STATE
    from operating.pg import business_comms as bc, sql_views
    settings = settings or {}
    built = [r[0] for r in pg.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE "
        "n.nspname = %s AND c.relname = ANY(%s)",
        (sql_views.person_schema(), ["emails", "teams_messages", "sharepoint_files"]))]
    if settings.get(bc.FILES_SETTING) not in (bc.FILES_FULL, bc.FILES_SEARCH) or \
            settings.get(FILES_STATE):
        built = [v for v in built if v != "sharepoint_files"]   # a file: not readable
    return built


def history(backend: Any, reader: Any, key: str, cursor: Optional[str] = None) -> dict:
    """A record's History tab from the served store (the shape of ``models.RecordHistory``),
    newest first, a page of ``HISTORY_ENTRIES`` entries from ``cursor`` (``s<seq>``): each entry
    built by the tab's own ``history._entry`` from the stored change. A change whose mail, file or
    chat the reader may not read, or whose values are withheld, is left out and ``partial`` says
    so; a corroborated link whose corroboration they may not read reads as the lead it is.
    ``RecordNotFound`` for a record the reader may not open (as an unknown one); ValueError for
    a cursor that is not one."""
    from operating.ontology_tools_sql import Busy, Door, ToolError, scoped_read
    from operating.page_reads import FILES_STATE, pool_for
    from operating.pg import business_comms as bc, sql_views
    from operating.pg.pool import PoolTimeout
    from ontology.mentions import CORROBORATED, TIER
    from . import history as hist
    from .errors import RecordNotFound
    from .serving import object_url
    before = 2 ** 31 - 1
    if cursor is not None:
        if not cursor.startswith("s") or not cursor[1:].isdigit() or int(cursor[1:]) >= before:
            raise ValueError("invalid_cursor")
        before = int(cursor[1:])
    door, dsn, comms = _door_for(backend, reader)
    if comms is None:
        raise Unavailable("not_ready")     # the mail settings decide what a change may cite
    person = sql_views.person_schema()

    def first(pg: Any) -> Any:
        return _built_views(pg, comms)

    def chunk(head: bool, upto: int) -> tuple[Any, Any]:
        def work(pg: Any) -> Any:
            args = {"key": key, "before": upto, "n": HISTORY_CHUNK}
            got = {"rows": pg.execute(HISTORY_SQL, args).fetchall()}
            if head:
                got["head"] = pg.execute(HISTORY_HEAD_SQL, args).fetchone()
                got["hidden"] = pg.execute(HISTORY_HIDDEN_SQL, args).fetchone()[0]
                if got["head"] is None:
                    got["exists"] = pg.execute(EXISTS_SQL, args).fetchone() is not None
            cids = sorted({c["c"] for _, p, _ in got["rows"] if p
                           for c in (p.get("need") or {}).get("corr") or ()})
            got["held"] = set(pg.execute(_held_classes_sql(), {"cids": cids}).fetchone()[0]) \
                if cids else set()
            return got

        def after(pg: Any, got: Any) -> Any:
            cites = sorted({h for _, p, _ in got["rows"] if p
                            for h in [*((p.get("need") or {}).get("cites") or ()),
                                      *(c["cite"] for c in (p.get("need") or {}).get("corr") or ()
                                        if c.get("cite"))]})
            if not cites:
                return set()
            return _readable_cites(pg, person, first(pg), cites)
        try:
            with pool_for(dsn).connection() as pg:
                return scoped_read(pg, Door(door.grant), work, READ_SECONDS, person=True,
                                   lock_ms=LOCK_MS, extra=comms, after=after)
        except (PoolTimeout, Busy):
            raise Unavailable("busy") from None
        except ToolError as exc:
            raise Unavailable("timeout" if exc.code == "timeout" else "not_ready") from None

    got, readable = chunk(True, before)
    head = got["head"]
    if head is None:
        if got.get("exists"):            # a record they may open whose history is not stored yet
            SRC.set("not_run")
            raise Unavailable("not_built")
        raise RecordNotFound()           # absent and withheld read the same
    # anything hidden from this reader, or left out even of the owner's full read (no one sees it)
    partial = bool(got["hidden"]) or bool((head[0] or {}).get("partial"))
    entries: list[tuple[int, Any]] = []      # (the event item's seq, its entry)
    pending: dict[int, list[Any]] = {}       # ev -> its change payloads read so far
    rows, more = got["rows"], len(got["rows"]) == HISTORY_CHUNK
    held, cites_ok = got["held"], readable
    nxt = None
    while True:
        for seq, payload, withheld in rows:
            if payload is None:              # withheld for this reader: said, never shown
                partial = True
                continue
            if "change" in payload:
                pending.setdefault(payload["ev"], []).append(payload["change"])
                continue
            event = dict(payload["event"])
            changes = pending.pop(payload["ev"], [])
            need = payload.get("need") or {}
            if any(h not in cites_ok for h in need.get("cites") or ()):
                partial = True               # rests on mail, a file or a chat they may not read
                continue
            if event.get("kind") in ("first_recorded", "changed"):
                if event["kind"] == "changed" and not changes:
                    continue                 # every change of it withheld (said above)
                event["changes"] = sorted(changes, key=lambda c: c.get("property") or "")
            link = event.get("link")
            if link is not None and link.get("tier") == CORROBORATED and not all(
                    c["c"] > 0 and c["c"] in held and (not c.get("cite") or c["cite"] in cites_ok)
                    for c in need.get("corr") or ()):
                event["link"] = {**link, "tier": TIER}   # mentions.reader_view's rule
            entry = hist._entry(event, object_url)
            if entry is None:
                raise RuntimeError(f"history item {seq} of {key} is not an entry")
            entries.append((seq, entry))
            if len(entries) == HISTORY_ENTRIES:
                break
        if len(entries) == HISTORY_ENTRIES:
            last = entries[-1][0]
            # a next page when an older item is left (it may still be one this reader is shown
            # nothing of: that page then says so)
            nxt = f"s{last}" if (more or any(q < last for q, _, _ in rows)) else None
            break
        if not more:
            break
        upto = rows[-1][0]
        (got, readable) = chunk(False, upto)
        rows, more = got["rows"], len(got["rows"]) == HISTORY_CHUNK
        held, cites_ok = got["held"], readable
    # the label's own markings withheld: the record's name as the tab says it without one
    from . import presentation
    meta = head[0] or {}
    kind = meta.get("type") or key.split(":", 1)[0]
    label = meta.get("label") or presentation.record_label(None, kind, key)[:200]  # guard-ok: the old tab's own label length (history._label)
    shown = [e for _, e in entries]
    message = hist.PARTIAL if partial else (hist.NOTHING if not shown and cursor is None
                                            else None)
    if head[2] is not None:     # what it was stored from has moved: said with its age, rebuild asked
        request_build(dsn, "history", key)
        age = (f"This history was stored {_et(head[3])}; newer changes are being prepared."
               if head[3] is not None else "Newer changes to this history are being prepared.")
        message = age if message is None else f"{message} {age}"
        SRC.set("stale")
    SRC.set(SRC.get() if SRC.get() == "stale" else "items")
    return m.RecordHistory(object_id=key, type=kind, label=label, entries=shown,
                           next_cursor=nxt, partial=partial, message=message)
