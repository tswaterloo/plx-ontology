"""Page numbers through the one read door (ARCH-FINAL 5): a page endpoint asks the host's
``plx.page`` (``operating.page_reads``) for a few named SQL reads over the reader-scoped
business views, as the reader, and builds its model from the rows. No inbox, no per-item
gate: the views scope rows and cells inside Postgres.

Behind the switch ``PLX_PAGES_DOOR=1`` (off by default until the business model is built
and refreshed on the served store); off, every page reads as before.

The call runs on the access worker (short jobs only: never behind a page's long read or an
Ask) or, without one, first on the read queue; its SQL runs on the page pool's own
connections. A failed or timed-out read raises ``PageDoorUnavailable``: a page then says
its numbers are unavailable, never "none"."""
from __future__ import annotations

import os
import re
import threading
import time
from contextlib import nullcontext
from urllib.parse import quote
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Collection, Mapping, Optional, Sequence

from . import models as m
from .serving import object_url, open_url, record_words, system_of

SWITCH = "PLX_PAGES_DOOR"
PAGE_TIMEOUT = 5.0
TOOL = "plx.page"


class PageDoorUnavailable(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


PAGE_KINDS = ("customer", "chain", "supplier", "purchase_order", "sales_order", "product",
              "batch", "supplier_invoice")


def on(kind: Optional[str] = None) -> bool:
    """``PLX_PAGES_DOOR``: ``1`` every page through the door; else a comma list of the page
    kinds switched on (``customer,chain,sales_order``: those pages and their links only).
    Without ``kind``: whether every kind is on (the search box, Home's late POs)."""
    raw = os.environ.get(SWITCH, "").strip()
    if raw == "1":
        return True
    if kind is None:
        return False
    return kind in {k.strip() for k in raw.split(",")} and kind in PAGE_KINDS


def read(backend: Any, reader: Any, reads: Sequence[Mapping[str, Any]], *, ids: bool = False
         ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, str]]]:
    """(each read's rows as dicts by column, the record each returned handle opens)."""
    args = {"reads": [dict(r) for r in reads], "ids": ids}
    out = _call(backend, reader, args)
    if not isinstance(out, Mapping) or "error" in out or not isinstance(out.get("reads"),
                                                                       Mapping):
        err = (out or {}).get("error") if isinstance(out, Mapping) else None
        raise PageDoorUnavailable(str((err or {}).get("code") or "unreadable"),
                                  str((err or {}).get("message") or ""))
    rows = {name: [dict(zip(got["columns"], row)) for row in got["rows"]]
            for name, got in out["reads"].items()}
    return rows, dict(out.get("ids") or {})


# the record page each kind of record opens through the door (``record_url``, ``resolve``)
DOOR_KINDS = {"PurchaseOrder": "purchase_order", "SalesOrder": "sales_order",
              "Customer": "customer", "Supplier": "supplier", "Product": "product",
              "Batch": "batch"}
_VIEW_OF = {"purchase_order": "purchase_orders", "sales_order": "sales_orders",
            "customer": "customers", "supplier": "suppliers", "product": "products",
            "batch": "batches", "supplier_invoice": "supplier_invoices"}
# only the parties' and orders' kinds have a page of their own address (/account, /order);
# an item's, a batch's and an invoice's open as the record page, which reads their door page
# beside it
_KIND_OF_VIEW = {v: k for k, v in _VIEW_OF.items()
                 if k not in ("product", "batch", "supplier_invoice")}


def _call(backend: Any, reader: Any, args: Mapping[str, Any]) -> Any:
    """``plx.page``'s answer to ``args`` for ``reader``. A backend that keeps doors
    (``operator_api.doors``: every served API) reads with the reader's door decided ahead, on
    this thread, through the page pool: no worker, no workspace, no decision in the request
    (speed specs 9.3 V1, 1.2.2). A reader with no door ahead is decided now on this thread
    (``Doors.get`` logs ``door_not_ahead``, alarmed); one that cannot be decided is refused
    (``not_ready``), never read with another door. A backend without doors (tools, tests of
    the old host) asks the host's ``plx.page`` on its worker, as before."""
    doors = getattr(backend, "doors", None)
    if doors is not None:
        held = doors.get(reader) or doors.decide_now(reader)
        if held is None:
            raise PageDoorUnavailable("not_ready", "this reader's door is not decided")
        from operating.ontology_tools_sql import Door
        from operating.page_reads import PageReads
        # a fresh Door over the held grant: ``_decide`` re-checks its stamp and the people
        # exclusion in the read's own snapshot (scoped_read), as every page read does
        return PageReads(None).call(args, None, door=Door(held[0].grant), dsn=held[1])
    from .page_core import CORE_PRIORITY
    worker = getattr(backend, "access_worker", None) or backend.reads
    try:
        return worker.run(reader, lambda view: view._call(TOOL, args),
                          timeout=_timeout(worker, reader), priority=CORE_PRIORITY)
    except TimeoutError:
        raise PageDoorUnavailable("timeout") from None


def resolve(backend: Any, reader: Any, kind: str, object_id: str) -> str:
    """The door's row ``kind``'s record ``object_id`` was read into (its handle, for the
    page's own read through the door); ``NotFound`` without one."""
    view = _VIEW_OF.get(kind)
    if view is None or not object_id or len(object_id) > 700:
        raise NotFound(kind)
    out = _call(backend, reader, {"resolve": {"view": view, "object_id": object_id}})
    if not isinstance(out, Mapping) or "error" in out:
        err = (out or {}).get("error") if isinstance(out, Mapping) else None
        raise PageDoorUnavailable(str((err or {}).get("code") or "unreadable"))
    if not isinstance(out.get("handle"), str):
        raise NotFound(kind)
    return out["handle"]


def record_url(table: Optional[str], handle: Optional[str], otype: str = "",
               oid: str = "") -> str:
    """Where a record a door read returned opens: its record page through the door while
    the door is on (``table``: its business table), else the record's own page, else its
    source."""
    kind = _KIND_OF_VIEW.get(table or "")
    if kind and on(kind) and isinstance(handle, str) and handle:
        return account_url(kind, handle)
    if otype and oid:
        return object_url(otype, oid)
    return open_url(handle) if isinstance(handle, str) and handle else ""


# A reader's first read on a worker decides their workspace and policy first (2-4 s on a
# fresh process, more under load: 11-18 walk, the late list's first read ran past 5 s and
# said "could not be read"): that one read waits up to this long, every later one
# PAGE_TIMEOUT.
PAGE_COLD_TIMEOUT = 20.0


def _timeout(worker: Any, reader: Any, warm: float = PAGE_TIMEOUT) -> float:
    """``warm`` (PAGE_TIMEOUT) once a job finished for the reader on every worker of
    ``worker``'s queue (``Worker.warm_for``: their policy is decided on whichever takes
    the read; a workspace merely made, its first job still deciding, is not), else
    PAGE_COLD_TIMEOUT (never less than ``warm``)."""
    warm_for = getattr(worker, "warm_for", None)
    if not callable(warm_for) or getattr(getattr(reader, "session", None), "principals",
                                         None) is None:
        return warm
    return warm if warm_for(reader) else max(warm, PAGE_COLD_TIMEOUT)


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value if isinstance(value, str) and value else None


def _money(amount: Any, currency: Any) -> Optional[m.Money]:
    if amount is None or not isinstance(currency, str) or not currency:
        return None
    try:
        text = format(Decimal(str(amount)), "f")
    except ArithmeticError:
        return None
    return m.Money(amount=text, currency=currency)


def late_po(row: Mapping[str, Any], ids: Mapping[str, Mapping[str, str]]) -> m.LatePo:
    """One late purchase order of ``late_po_list`` as the late list shows it."""
    handle = row.get("handle")
    rec = ids.get(handle) if isinstance(handle, str) else None
    oid = str(rec["object_id"]) if rec else str(handle or row.get("po_number") or "")
    url = record_url("purchase_orders", handle, str(rec["object_type"]) if rec else "",
                     oid if rec else "")
    due = _iso(row.get("requested_date"))
    promised = _iso(row.get("promised_date"))
    days = row.get("days_late")
    reason = (f"Needed by {due}, still open" if due else "Past its needed-by date")
    if promised and promised != due:
        reason += f"; supplier promised {promised}"
    return m.LatePo(
        object_id=oid, label=f"PO {row.get('po_number') or '?'}",
        supplier=row.get("supplier") if isinstance(row.get("supplier"), str) else None,
        due_date=due, days_late=int(days) if isinstance(days, int) else None,
        amount=_money(row.get("total"), row.get("total_currency")), reason=reason,
        open_url=url, sources=[m.Source(system=system_of(handle), handle=handle,
                                        label=record_words(handle), open_url=open_url(handle))]
        if isinstance(handle, str) else [])


def late_pos(backend: Any, reader: Any, *, offset: int = 0, limit: int = 25
             ) -> tuple[int, list[m.LatePo], Optional[str]]:
    """(every late PO the reader may see, the page of them from ``offset``, the views'
    as-of), all from one transaction."""
    rows, ids = read(backend, reader, [
        {"name": "late_po_count"},
        {"name": "late_po_list", "params": {"limit": limit, "offset": offset}},
        {"name": "views_as_of"}], ids=True)
    total = int(rows["late_po_count"][0]["late"])
    built = [r.get("built_at") for r in rows["views_as_of"] if r.get("view") == "purchase_orders"]
    return total, [late_po(r, ids) for r in rows["late_po_list"]], (
        _iso(built[0]) if built else None)




# ---- the SQL alerts (``operating.pg.alerts``) ---------------------------------------
# Behind ``PLX_ALERTS_SQL=1`` (off: Home, the late list and the briefing read the gap
# inbox as before). On, they read the alerts table through the same door: the guarded
# ``alerts`` view shows an alert only when the reader may read every row it was computed
# from, as the door is at read time (people exclusion, classes and column classes now).
# Only the alert types on for the reader (on by default, or named by one of their own
# watch rules) and routed to them (``briefing.Routing``: their departments or role) are
# listed; a type on but routed elsewhere is counted as other teams'. A type whose rule has
# not finished within ``alerts.STALE_MINUTES`` shows "unavailable since", never old items.

ALERTS_SWITCH = "PLX_ALERTS_SQL"


def alerts_on() -> bool:
    return os.environ.get(ALERTS_SWITCH, "").strip() == "1"


def alert_types(enabled: Sequence[str] = (), *, home: bool = False,
                suggestions: bool = False) -> list[str]:
    """The SQL rules' alert types on for a reader: on by default in the registry or
    switched on by them (``enabled``), and (``home``) reaching My work. A type the
    registry or the API does not know is never asked for (fail closed)."""
    from typing import get_args
    from operating import alert_kinds
    from operating.pg.alerts import RULES
    known = set(get_args(m.EventType))
    out = []
    for name in sorted(RULES):
        if name not in known or alert_kinds.entry(name) is None:
            continue
        if home and not alert_kinds.home(name):
            continue
        if alert_kinds.shown(name, enabled) or (suggestions and alert_kinds.suggested(name)):
            out.append(name)
    return out


def _paged(name: str) -> bool:
    """A rule whose alerts this page path can show (a mail rule's never are: the page
    view has no comms door, so its type is said unavailable here, never "none")."""
    from operating.pg.alerts import RULES
    rule = RULES.get(name)
    return rule is not None and not rule.comms


def routed(event_type: str, routing: Any) -> bool:
    """Routed to this reader (``briefing.Routing``); unknown routing routes nothing."""
    from operating import briefing as bf
    spec = bf.EVENT_TYPES.get(event_type)
    routes = getattr(routing, "routes", None)
    return bool(spec and callable(routes) and routes(spec.get("departments") or ()))


class ReaderScope:
    """What a reader's alerts are chosen by: their last visit, routing and own types."""

    def __init__(self, visit: Optional[str], routing: Any, enabled: Sequence[str],
                 rules: Any = None) -> None:
        self.visit, self.routing, self.enabled = visit, routing, tuple(enabled)
        # (the reader's watch-rule store, its stamp taken before the rules were read):
        # a kept scope is served only while the store's stamp is the same
        self.rules = rules


def _file_stamp(path: Any) -> Any:
    """(size, mtime) of a SQLite store and its write-ahead log (None: absent)."""
    out = []
    for p in (str(path), str(path) + "-wal"):
        try:
            st = os.stat(p)
            out.append((st.st_size, st.st_mtime_ns))
        except OSError:
            out.append(None)
    return tuple(out)


def _rule_types(view: Any, seen: Optional[list] = None) -> list[str]:
    """The alert types the reader's own watch rules name (their signal kinds as types);
    ``seen`` gets (their store, its stamp taken before reading it)."""
    from operating import briefing as bf
    from operating import watchlists as wl
    from .watch import view_context
    ctx = view_context(view)
    if ctx.path is None:
        return []
    if seen is not None:
        seen.append((ctx.path, _file_stamp(ctx.path)))
    out: set[str] = set()
    for rule in wl.WatchStore(ctx.path).rules(ctx.reader):
        spec = rule.get("spec") if isinstance(rule, Mapping) else None
        signal = spec.get("signal") if isinstance(spec, Mapping) else None
        kinds = signal.get("kinds") if isinstance(signal, Mapping) else None
        for kind in kinds if isinstance(kinds, (list, tuple)) else ():
            if isinstance(kind, str):
                out.add(bf.TYPE_OF_KIND.get(kind, kind))
    return sorted(out)


# A reader's scope read once serves the burst of door reads one screen makes (Home: its
# at-risk list, the briefing and each group's next page: 7 queued jobs per visit, 11-18
# walk) for this long; a visit marked seen drops it (``forget_scope``), a change to the
# reader's watch rules (their store's stamp) reads it again.
SCOPE_SECONDS = 5.0
_SCOPES_LOCK = threading.Lock()


def _scopes(backend: Any) -> dict:
    """The scopes kept on ``backend`` (each backend its own: never another's)."""
    with _SCOPES_LOCK:
        got = getattr(backend, "_door_scopes", None)
        if got is None:
            got = {}
            setattr(backend, "_door_scopes", got)
        return got


def _scope_key(reader: Any) -> tuple:
    principals = getattr(getattr(reader, "session", None), "principals", None) or ()
    return (getattr(reader, "key", None), getattr(reader, "mode", None),
            ",".join(sorted(principals)))


def forget_scope(backend: Any, reader: Any) -> None:
    """The reader's kept scope dropped (their visit was just marked seen)."""
    kept = _scopes(backend)
    with _SCOPES_LOCK:
        kept.pop(_scope_key(reader), None)


def reader_scope(backend: Any, reader: Any) -> ReaderScope:
    """``_reader_scope`` kept per reader (key, mode, principals) on ``backend`` for
    ``SCOPE_SECONDS`` while their watch-rule store is unchanged."""
    kept, key = _scopes(backend), _scope_key(reader)
    now = time.monotonic()
    with _SCOPES_LOCK:
        hit = kept.get(key)
    if hit is not None and now - hit[0] <= SCOPE_SECONDS and (
            hit[1].rules is None or _file_stamp(hit[1].rules[0]) == hit[1].rules[1]):
        return hit[1]
    got = _reader_scope(backend, reader)
    with _SCOPES_LOCK:
        kept[key] = (now, got)
        if len(kept) > 512:
            for k in [k for k, v in kept.items() if now - v[0] > SCOPE_SECONDS]:
                kept.pop(k, None)
    return got


def _reader_scope(backend: Any, reader: Any) -> ReaderScope:
    """The reader's last visit, routing and watch-rule types, read as the reader. What
    cannot be read fails toward fewer alerts: no rules switch nothing on, the visit unknown
    counts every alert as new. A routing that cannot be read at all (the worker failed or
    timed out, or the routing lookup raised) raises ``PageDoorUnavailable``: never a
    reader routed to nothing, which would read as an empty, complete briefing."""
    from operating import briefing as bf
    from .page_parts import last_visit_of
    from .page_core import CORE_PRIORITY
    worker = getattr(backend, "access_worker", None) or backend.reads

    def job(view: Any) -> ReaderScope:
        try:
            visit = last_visit_of(view)[1]
        except Exception:  # noqa: BLE001 - no visit known
            visit = None
        try:
            routing = view.routing()
        except Exception:  # noqa: BLE001 - unresolved: unavailable, not routed nothing
            return None  # type: ignore[return-value]
        seen: list = []
        # one request: the reader's policy kept for the session (``HostAccess.request``),
        # not decided again outside one at each visit
        request = getattr(getattr(view, "access", None), "request", None)
        try:
            with request() if callable(request) else nullcontext():
                enabled = _rule_types(view, seen)
        except Exception:  # noqa: BLE001 - rules unreadable: only the default-on types
            enabled = []
        return ReaderScope(visit if isinstance(visit, str) else None, routing, enabled,
                           seen[0] if seen else None)
    try:
        got = worker.run(reader, job, timeout=_timeout(worker, reader), priority=CORE_PRIORITY)
    except Exception:  # noqa: BLE001 - nothing known of the reader
        raise PageDoorUnavailable("routing_unread") from None
    if not isinstance(got, ReaderScope):
        raise PageDoorUnavailable("routing_unread")
    return got


def _when(value: Any) -> Optional[str]:
    """A run time in US Eastern words: "Nov 18, 3:07 PM ET"."""
    if not isinstance(value, datetime):
        return None
    from zoneinfo import ZoneInfo
    t = value.astimezone(ZoneInfo("America/New_York"))
    return f"{t:%b} {t.day}, {t:%I:%M %p}".replace(" 0", " ") + " ET"


def runs_state(rows: Sequence[Mapping[str, Any]], now: Optional[datetime] = None
               ) -> dict[str, tuple[bool, Optional[datetime]]]:
    """Per rule (fresh, its last finished run) from ``alert_runs``: fresh when that run is
    within ``alerts.STALE_MINUTES``; a rule with no row: not fresh, never finished."""
    from datetime import timedelta, timezone
    from operating.pg.alerts import RULES, STALE_MINUTES
    now = now or datetime.now(timezone.utc)
    got = {str(r.get("rule")): r.get("ok_at") for r in rows}
    out: dict[str, tuple[bool, Optional[datetime]]] = {}
    for name in RULES:
        ok = got.get(name)
        ok = ok if isinstance(ok, datetime) and _paged(name) else None
        out[name] = (ok is not None and now - ok <= timedelta(minutes=STALE_MINUTES), ok)
    return out


RUNS_SECONDS = 30   # how long the alert rules' freshness is held before it is read again
RUNS_AHEAD_SECONDS = 15   # past this age the next read is started off the request (a request never waits at the 30 s edge)
_RUNS_LOCK = threading.Lock()


def _read_runs(backend: Any) -> Any:
    got: Any = "unread"
    try:
        from operating.pg import business_views as bv
        pool = backend.pg() if getattr(backend, "pg_dsn", None) else None
        if pool is not None:
            with pool.connection() as conn:
                rows = conn.execute(
                    f"SELECT rule, ok_at FROM {bv.SCHEMA}.alert_runs").fetchall()
            state = runs_state([{"rule": r[0], "ok_at": r[1]} for r in rows])
            got = tuple(sorted((name, fresh) for name, (fresh, _ok) in state.items()))
    except Exception:  # noqa: BLE001 - unread: the constant
        got = "unread"
    return got


def _refresh_runs(backend: Any) -> None:
    """The read of the alert runs, kept for the next ``runs_fresh`` (off any request)."""
    try:
        got = _read_runs(backend)
        with _RUNS_LOCK:
            backend.__dict__["_runs_fresh"] = (time.monotonic(), got)
    finally:
        with _RUNS_LOCK:
            backend.__dict__["_runs_ahead"] = False


def runs_fresh(backend: Any) -> Any:
    """Which alert rules are current now (``alerts.STALE_MINUTES``), as one value for a kept
    screen's inputs: when the rules stop running it changes, so a Home, briefing or sidebar
    kept from before is read again (and says unavailable) instead of being served as current.
    Read from the alert runs on the backend's Postgres pool, held ``RUNS_SECONDS``, and read
    again off the request once the held one is ``RUNS_AHEAD_SECONDS`` old (the answer is
    never older than ``RUNS_SECONDS``: a request that finds it that old reads it itself);
    a pool or table that cannot be read gives one constant ("unread"): the screens then
    stand on the read's own stale check (``alerts_stale``) as before."""
    now = time.monotonic()
    with _RUNS_LOCK:
        held = backend.__dict__.get("_runs_fresh")
        if held is not None and now - held[0] < RUNS_SECONDS:
            if now - held[0] >= RUNS_AHEAD_SECONDS and not backend.__dict__.get("_runs_ahead"):
                backend.__dict__["_runs_ahead"] = True
                threading.Thread(target=_refresh_runs, args=(backend,), daemon=True,
                                 name="alert-runs-ahead").start()
            return held[1]
    got = _read_runs(backend)
    with _RUNS_LOCK:
        backend.__dict__["_runs_fresh"] = (now, got)
    return got


def alerts_stale(label: str, since: Optional[datetime]) -> Any:
    from .errors import UserFacing
    when = _when(since)
    return UserFacing("alerts_stale", f"{label} are unavailable" + (
        f" since {when}" if when else "") + ": their check has not run on the current data. "
        "They show again after its next run.", 404)


def open_po_ids(backend: Any, reader: Any, limit: int) -> list[str]:
    """The record ids of the open purchase orders this reader may see, late first then newest
    (``open_po_list``), at most ``limit``: the warm-up's pages."""
    rows, ids = read(backend, reader, [{"name": "open_po_list", "params": {"limit": limit}}],
                     ids=True)
    out = []
    for r in rows["open_po_list"]:
        rec = ids.get(r.get("handle"))
        if rec and rec.get("object_type") == "PurchaseOrder" and rec.get("object_id"):
            out.append(str(rec["object_id"]))
    return list(dict.fromkeys(out))


def alert_late_pos(backend: Any, reader: Any, *, offset: int = 0, limit: int = 25
                   ) -> tuple[int, list[m.LatePo], Optional[str]]:
    """``late_pos`` from the po_late alerts: the alert joined to its order's guarded row;
    as of the rule's last finished run. Its rule not run within two refresh cycles:
    ``alerts_stale`` (raised), never the old list."""
    rows, ids = read(backend, reader, [
        {"name": "alerts_late_po_count"},
        {"name": "alerts_late_po_list", "params": {"limit": limit, "offset": offset}},
        {"name": "alert_runs"}], ids=True)
    fresh, ok = runs_state(rows["alert_runs"]).get("po_late", (False, None))
    if not fresh:
        raise alerts_stale("Late purchase orders", ok)
    total = int(rows["alerts_late_po_count"][0]["late"])
    return total, [late_po(r, ids) for r in rows["alerts_late_po_list"]], _iso(ok)


def alert_home(backend: Any, reader: Any, *, limit: int = 25
               ) -> tuple[int, list[m.LatePo], Optional[str], list[m.AtRisk],
                          dict[str, Optional[datetime]], int]:
    """Home's late POs (``alert_late_pos``) and its at-risk list (the open alerts of the
    home types other than late POs routed to the reader) in ONE
    read through the door (one transaction) after the reader's scope: two door reads
    queued one after the other on the access workers cost Home 0.3-0.8 s each warm (11-18
    walk). (late total, the first ``limit`` late, as of, at risk, the stale home types, how many
    open alerts of the at-risk types the reader may read in all: the list is its first
    ``limit``).
    The late POs' rule not current: ``alerts_stale`` (raised), never the old list."""
    scope = reader_scope(backend, reader)
    types = [t for t in alert_types(scope.enabled, home=True)
             if t != "po_late" and routed(t, scope.routing)]
    reads: list[dict[str, Any]] = [
        {"name": "alerts_late_po_count"},
        {"name": "alerts_late_po_list", "params": {"limit": limit, "offset": 0}},
        {"name": "alert_runs"}]
    if types:
        reads += [{"name": "alerts_count", "params": {"rules": ",".join(types)}},
                  {"name": "alerts_open", "params": {"rules": ",".join(types),
                                                     "limit": limit, "offset": 0}}]
    rows, ids = read(backend, reader, reads, ids=True)
    runs = runs_state(rows["alert_runs"])
    fresh, ok = runs.get("po_late", (False, None))
    if not fresh:
        raise alerts_stale("Late purchase orders", ok)
    total = int(rows["alerts_late_po_count"][0]["late"])
    late = [late_po(r, ids) for r in rows["alerts_late_po_list"]]
    risk = []
    for row in rows.get("alerts_open", []):
        oid, _otype, url, sources = _alert_ref(row, ids)
        risk.append(m.AtRisk(object_id=oid, label=alert_label(row), kind=str(row.get("kind")),
                             reason=str(row.get("why") or ""), open_url=url, sources=sources))
    # as the briefing's: a mail type is never paged here (its group says unavailable), so
    # it never holds Home in "refreshing" (live 11-21: needs_my_reply kept Home's
    # "Refreshing" on for good and the app polled /api/home every few seconds)
    stale = {t: runs.get(t, (False, None))[1] for t in types
             if _paged(t) and not runs.get(t, (False, None))[0]}
    risk_total = sum(int(r["open"]) for r in rows.get("alerts_count", ()))
    return total, late, _iso(ok), risk, stale, max(risk_total, len(risk))


def late_suppliers(backend: Any, reader: Any, *, limit: int = 10
                   ) -> tuple[int, list[tuple[str, int]], int]:
    """The sidebar's late POs: (every late PO the reader may see, the ``limit`` suppliers
    with the most of them as (name, count) most first, how many suppliers have one), in one
    transaction; under ``PLX_ALERTS_SQL`` from the po_late alerts as Home reads them (the
    rule not current: ``alerts_stale``, raised, never an old count)."""
    alerts = alerts_on()
    count, by = (("alerts_late_po_count", "alerts_late_po_suppliers") if alerts
                 else ("late_po_count", "late_po_suppliers"))
    reads: list[dict[str, Any]] = [{"name": count},
                                   {"name": by, "params": {"limit": limit}}]
    if alerts:
        reads.append({"name": "alert_runs"})
    rows, _ids = read(backend, reader, reads)
    if alerts:
        fresh, ok = runs_state(rows["alert_runs"]).get("po_late", (False, None))
        if not fresh:
            raise alerts_stale("Late purchase orders", ok)
    got = rows[by]
    return (int(rows[count][0]["late"]),
            [(str(r["supplier"]), int(r["late"])) for r in got],
            int(got[0]["suppliers"]) if got else 0)


def _alert_ref(row: Mapping[str, Any], ids: Mapping[str, Mapping[str, str]]
               ) -> tuple[str, str, str, list[m.Source]]:
    """(object id, object type, open url, sources) of an alert's record."""
    handle = str(row.get("handle") or "")
    rec = ids.get(handle)
    otype = str(rec["object_type"]) if rec else ""
    oid = str(rec["object_id"]) if rec else handle
    url = record_url(row.get("record_table"), handle, otype, oid if rec else "")
    return oid, otype, url, [m.Source(system=system_of(handle), handle=handle,
                                      label=record_words(handle), open_url=open_url(handle))]


def _subject(row: Mapping[str, Any], otype: str) -> str:
    """The alerted record's own name ("BE FIT NOW", "106001"); without one (a record this
    reader may not see in ``records``) its kind in words ("Purchase order"), never the
    source system's table words ("BC production", "Portal customer")."""
    name = str(row.get("subject") or "").strip()
    if name:
        return name[:200]
    if otype:
        words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", otype).lower()
        return words[:1].upper() + words[1:]
    return record_words(str(row.get("handle") or ""))


def alert_label(row: Mapping[str, Any]) -> str:
    from operating import alert_kinds
    entry = alert_kinds.entry(str(row.get("rule"))) or {}
    return str(entry.get("label") or alert_kinds.kind_label(str(row.get("kind")))
               or row.get("rule"))


def alerts_read(backend: Any, reader: Any, types: Sequence[str], *, limit: int = 25,
                offset: int = 0, counted: Sequence[str] = ()
                ) -> tuple[dict[str, int], list[dict[str, Any]], dict[str, dict[str, str]],
                           dict[str, tuple[bool, Optional[datetime]]]]:
    """(open alerts per type of ``types`` + ``counted``, the page of ``types``' oldest
    first, the record each opens, each rule's (fresh, last finished run)), in one
    transaction; a stale rule's alerts are neither counted nor listed (the read itself
    leaves them out). Nothing asked: only the runs are read."""
    counts, rows, ids, runs, _impact = alerts_read_with_impact(
        backend, reader, types, limit=limit, offset=offset, counted=counted)
    return counts, rows, ids, runs


IMPACT_ROWS = 3   # the biggest alerts of one Financials kind Home lists (app: its section)


def alerts_read_with_impact(backend: Any, reader: Any, types: Sequence[str], *, limit: int = 25,
                            offset: int = 0, counted: Sequence[str] = (),
                            impact: Sequence[str] = (), impact_rows: int = IMPACT_ROWS
                            ) -> tuple[dict[str, int], list[dict[str, Any]],
                                       dict[str, dict[str, str]],
                                       dict[str, tuple[bool, Optional[datetime]]],
                                       list[dict[str, Any]]]:
    """``alerts_read`` and, in the same transaction, the ``impact`` types' biggest
    ``impact_rows`` open alerts each (money impact first: ``alerts_impact_top``)."""
    reads: list[dict[str, Any]] = [{"name": "alert_runs"}]
    every = list(dict.fromkeys([*types, *counted, *impact]))
    if every:
        reads.append({"name": "alerts_count", "params": {"rules": ",".join(every)}})
    if types:
        reads.append({"name": "alerts_open", "params": {"rules": ",".join(types),
                                                        "limit": limit, "offset": offset}})
    if impact:
        reads.append({"name": "alerts_impact_top", "params": {"rules": ",".join(impact),
                                                              "limit": impact_rows}})
    rows, ids = read(backend, reader, reads, ids=bool(types or impact))
    counts = {str(r["rule"]): int(r["open"]) for r in rows.get("alerts_count", ())}
    return (counts, rows.get("alerts_open", []), ids, runs_state(rows["alert_runs"]),
            rows.get("alerts_impact_top", []))


def alert_item(row: Mapping[str, Any], ids: Mapping[str, Mapping[str, str]], rank: int,
               last_visit: Optional[str]) -> m.BriefingItem:
    """One open alert as a briefing item (Watching lists and counts these)."""
    from operating import alert_kinds
    from operating.briefing import NEXT_ACTIONS
    oid, otype, url, sources = _alert_ref(row, ids)
    rule = str(row.get("rule"))
    # the kind's own next step (the one the briefing's items carry); a kind without one
    # keeps the plain step
    step = NEXT_ACTIONS.get(rule) or ("Open the record and check it.", rule)
    seen = _iso(row.get("first_seen"))
    new = bool(seen and (last_visit is None or seen > last_visit))
    return m.BriefingItem(
        rank=rank, id=f"alert:{rule}:{row.get('handle')}:{row.get('topic') or ''}",
        event_type=rule, label=alert_label(row), evidence="live", object_id=oid,
        object_type=otype, subject_label=_subject(row, otype),
        summary=str(row.get("why") or ""),
        next_action=m.NextAction(text=step[0], rule=step[1]),
        new_since_visit=new, new_this_generation=new, alert_kind=alert_kinds.kind_of(rule),
        default_on=alert_kinds.default_on(rule), home=alert_kinds.home(rule),
        section=alert_kinds.section(rule), first_seen=seen, open_url=url, sources=sources)


# what each money impact is, in the words a row says it with (``amount_basis``)
IMPACT_BASIS = {"price_below_cost": "below_cost", "customer_price_drop": "price_drop"}


def _impact_item(row: Mapping[str, Any], ids: Mapping[str, Mapping[str, str]], rank: int,
                 last_visit: Optional[str]) -> m.BriefingItem:
    """One open alert of a Financials type as a briefing item with its money impact (the
    amount, in the currency of its own lines; none when the impact could not be worked out)."""
    item = alert_item(row, ids, rank, last_visit)
    impact, cur = row.get("impact"), row.get("cur")
    if impact is not None and cur:
        item.amount = m.Money(amount=f"{Decimal(str(impact)):.2f}", currency=str(cur))
        item.amount_basis = IMPACT_BASIS.get(item.event_type)
    return item


SUGGESTION_ALERTS_READ = 500   # open alerts read for the suggestion cards (the page cap)


def alert_suggestion_items(backend: Any, reader: Any, types: Collection[str], *,
                           limit: int = SUGGESTION_ALERTS_READ) -> dict[str, Any]:
    """The open alerts of ``types`` (the suggestion kinds' event types) on for the reader
    and routed to them, as briefing items (``alert_item`` dumped), oldest first, for the
    suggestion cards: the same alerts Home lists, so each card lands on a row Home shows.
    ``status`` "read" or "unavailable" (a failed scope or read: never an empty, complete
    list); ``complete`` only when every asked type is current and every open alert was
    read; ``as_of`` the oldest last finished run among them. A type the registry or the
    API does not know is never asked for (``alert_types``)."""
    try:
        scope = reader_scope(backend, reader)
    except PageDoorUnavailable:
        return {"status": "unavailable", "items": [], "complete": False}
    wanted = set(types)
    asked = [t for t in alert_types(scope.enabled, suggestions=True)
             if t in wanted and routed(t, scope.routing)]
    # a type this page path cannot show (a mail rule: no comms door here) is said unavailable,
    # never counted as none
    unavailable = [t for t in asked if not _paged(t)]
    mine = [t for t in asked if _paged(t)]
    if not mine:
        return {"status": "read", "items": [], "complete": not unavailable, "as_of": None,
                "unavailable_types": unavailable}
    try:
        counts, rows, ids, runs = alerts_read(backend, reader, mine, limit=limit)
    except PageDoorUnavailable:
        return {"status": "unavailable", "items": [], "complete": False}
    items = [alert_item(r, ids, i + 1, scope.visit).model_dump(mode="json")
             for i, r in enumerate(rows)]
    current = all(runs.get(t, (False, None))[0] for t in mine)
    return {"status": "read", "items": items,
            "rank_basis": "business impact first, then the oldest open alert",
            "complete": current and not unavailable
            and sum(counts.get(t, 0) for t in mine) <= len(rows),
            "unavailable_types": unavailable,
            "as_of": _oldest({t: runs.get(t, (False, None))[1] for t in mine})}


def _oldest(times: Mapping[str, Optional[datetime]]) -> Optional[str]:
    known = [t for t in times.values() if isinstance(t, datetime)]
    return _iso(min(known)) if known else None


def alert_briefing(backend: Any, reader: Any, *, since_visit: bool = False,
                   top: int = 7) -> m.Briefing:
    """The briefing (Home's counts and Watching) from the alerts table: every type on for
    the reader and routed to them, counted per type (as of its rule's last finished run);
    the oldest ``top`` one by one; a type not current is a group saying "unavailable
    since", with no items; types on but routed elsewhere counted as other teams'. A failed
    read is ``unavailable``, never an empty briefing."""
    from operating import alert_kinds
    try:
        scope = reader_scope(backend, reader)
    except PageDoorUnavailable:
        return m.Briefing(status="unavailable", count=0, new_since_visit=0,
                          since_visit=since_visit, other_teams=0, rank_basis="",
                          routing_basis="", complete=False, digest_preview="")
    on = alert_types(scope.enabled)
    mine = [t for t in on if routed(t, scope.routing)]
    others = [t for t in on if t not in mine and _paged(t)]
    # the types the registry gathers into a Home section (Financials) are counted and their
    # biggest few read by money impact; the oldest-first top items are the others'
    sectioned = [t for t in mine if _paged(t) and alert_kinds.section(t)]
    try:
        counts, rows, ids, runs, biggest = alerts_read_with_impact(
            backend, reader, [t for t in mine if _paged(t) and t not in sectioned],
            limit=top, counted=[*others, *sectioned], impact=sectioned)
    except PageDoorUnavailable:
        return m.Briefing(status="unavailable", count=0, new_since_visit=0,
                          since_visit=since_visit, other_teams=0, rank_basis="",
                          routing_basis="", complete=False, digest_preview="")
    items = [alert_item(r, ids, i + 1, scope.visit) for i, r in enumerate(rows)]
    # a gathered type counts and lists only while its rule is current, like every other
    items += [_impact_item(r, ids, len(items) + i + 1, scope.visit)
              for i, r in enumerate(biggest) if runs.get(str(r.get("rule")), (False, None))[0]]
    if since_visit:
        items = [i for i in items if i.new_since_visit]
    # a paged type not current: the briefing is refreshing (a mail type is never paged
    # here: its group says unavailable, it does not hold the briefing in "refreshing")
    stale = {t: runs.get(t, (False, None))[1] for t in mine
             if _paged(t) and not runs.get(t, (False, None))[0]}
    groups = []
    for t in mine:
        fresh, ok = runs.get(t, (False, None))
        n = counts.get(t, 0) if fresh else 0
        if fresh and not n:
            continue
        groups.append(m.BriefingGroup(
            event_type=t, label=alert_label({"rule": t}), count=n,
            section=alert_kinds.section(t),
            in_top=sum(1 for i in items if i.event_type == t),
            new_since_visit=sum(1 for i in items if i.event_type == t and i.new_since_visit),
            acknowledged=0, as_of=_iso(ok) if fresh else None, unavailable=not fresh,
            unavailable_since=None if fresh else _iso(ok)))
    mine_counts = {t: n for t, n in counts.items() if t in mine}
    total = sum(mine_counts.values())
    as_of = [ok for t in mine if runs.get(t, (False, None))[0]
             for ok in [runs[t][1]] if ok is not None]
    return m.Briefing(
        status="read", count=total, new_since_visit=sum(1 for i in items if i.new_since_visit),
        since_visit=since_visit, other_teams=sum(n for t, n in counts.items() if t in others),
        home_count=sum(n for t, n in mine_counts.items() if alert_kinds.needs_you(t)),
        watching_count=sum(n for t, n in mine_counts.items() if not alert_kinds.home(t)),
        rank_basis="oldest first", routing_basis="every alert type on for you, routed to "
        "your departments, that you may read (routing: "
        f"{getattr(scope.routing, 'basis', '') or 'unknown'})", last_visit=scope.visit,
        as_of=_iso(min(as_of)) if as_of else None,
        complete=not stale, refreshing=bool(stale), checked_on=_oldest(stale),
        more=total > len(items), items=items, groups=groups, digest_preview="")


def alert_briefing_items(backend: Any, reader: Any, event_type: str, *, offset: int = 0,
                         limit: int = 25) -> m.BriefingItems:
    """One type's open alerts a page at a time; a type not on for the reader or not routed
    to them: none. A failed read: ``alerts_not_read`` (raised); a type not current:
    ``alerts_stale`` (raised), never its old items."""
    from .briefs import alerts_not_read
    try:
        scope = reader_scope(backend, reader)
    except PageDoorUnavailable:
        raise alerts_not_read() from None
    if event_type not in alert_types(scope.enabled) or not routed(event_type, scope.routing):
        return m.BriefingItems(event_type=event_type, total=0, offset=offset)
    try:
        counts, rows, ids, runs = alerts_read(
            backend, reader, [event_type] if _paged(event_type) else [], limit=limit,
            offset=offset)
    except PageDoorUnavailable:
        raise alerts_not_read() from None
    fresh, ok = runs.get(event_type, (False, None))
    if not fresh:
        raise alerts_stale(alert_label({"rule": event_type}), ok)
    total = counts.get(event_type, 0)
    return m.BriefingItems(event_type=event_type, total=total, offset=offset,
                           items=[alert_item(r, ids, offset + i + 1, scope.visit)
                                  for i, r in enumerate(rows)],
                           more=offset + len(rows) < total)


# ---- the one search box ---------------------------------------------------------------

SEARCH_TYPES = {"customer": "Customer", "chain": "Chain", "supplier": "Supplier",
                "sales_order": "SalesOrder", "purchase_order": "PurchaseOrder",
                "product": "Product", "batch": "Batch", "sales_invoice": "SalesInvoice",
                "supplier_invoice": "SupplierInvoice", "shipment": "Shipment",
                "receipt": "Receipt", "lot": "Lot", "credit_note": "SalesCreditMemo",
                "purchase_invoice": "PurchaseInvoice"}
_SYSTEMS = {"bc": "BC", "fm": "FileMaker", "filemaker": "FileMaker", "mc": "MC",
            "api": "Portal", "portal": "Portal", "sage": "Sage", "vmc": "VMC"}


def account_url(kind: str, handle: Optional[str] = None, chain: Optional[str] = None) -> str:
    if kind == "chain":
        return f"/account?kind=chain&chain={quote(chain or '', safe='')}"
    page = "order" if kind in ("purchase_order", "sales_order") else "account"
    return f"/{page}?kind={kind}&h={quote(handle or '', safe='')}"


def find_url(text: Any) -> Optional[str]:
    """Records' search for a number or code a cell shows (an item, an order, a PO): the
    records that carry it, from the one search, with the best match first."""
    t = str(text).strip() if text is not None else ""
    return f"/records?q={quote(t, safe='')}" if t and len(t) <= 80 else None


def _row_url(row: Mapping[str, Any], ids: Mapping[str, Mapping[str, str]]) -> Optional[str]:
    """The record page of the record a returned row is (``ids``: the record each handle
    opens), None when the door named none."""
    rec = ids.get(str(row.get("handle") or ""))
    if rec and rec.get("object_id") and rec.get("object_type"):
        return object_url(rec["object_type"], rec["object_id"])
    return None


def codes_text(codes: Any, primary: Any = None) -> Optional[str]:
    """One code up front, the rest behind: 'sage:ACME, portal:ACME, bc:C01' with primary ACME
    is 'ACME. Same code in Portal, Sage. Other systems: BC C01'. Without a primary code the
    systems' own codes are listed ('Portal ACME, Sage ACME')."""
    if not isinstance(codes, str) or not codes.strip():
        return None
    pairs = []
    for part in codes.split(","):
        system, _, code = part.strip().partition(":")
        if code:
            pairs.append((_SYSTEMS.get(system.lower(), system.title()), code))
    pairs = sorted(dict.fromkeys(pairs))
    if not pairs:
        return None
    if not isinstance(primary, str) or not primary.strip():
        return ", ".join(f"{s} {c}" for s, c in pairs)
    same = [s for s, c in pairs if c == primary]
    other = [f"{s} {c}" for s, c in pairs if c != primary]
    text = primary
    if same:
        text += f". Same code in {', '.join(same)}"
    if other:
        text += f". Other systems: {', '.join(other)}"
    return text


def search(backend: Any, reader: Any, q: str, limit: int) -> list[m.Suggestion]:
    """Every kind of record by name or number, one result per real thing, ranked (the
    ``find`` read); a chain's head account is the chain's result, never a second one."""
    from operating.page_reads import OBJECT_KINDS
    text = " ".join(q.split())[:100]
    if len(text) < 2:
        return []
    rows, ids = read(backend, reader, [{"name": "find", "params": {
        "q": text, "limit": min(50, limit + 10)}}], ids=True)
    hits = rows["find"]
    if not hits and len(text) >= 4:
        # nothing by name or number: a close spelling, one more read (never merged into a find
        # that found something)
        try:
            got, more = read(backend, reader, [{"name": "find_near", "params": {
                "q": text, "limit": min(10, limit)}}], ids=True)
            hits, ids = got["find_near"], {**ids, **more}
        except PageDoorUnavailable:
            pass   # no close spelling could be read: the exact search stands, said as no match
    chains = {str(r["title"]).lower() for r in hits if r["kind"] == "chain" and r["title"]}
    out: list[m.Suggestion] = []
    for r in hits:
        title = r.get("title")
        if not isinstance(title, str) or not title:
            continue
        kind, handle = str(r["kind"]), r.get("handle")
        if kind == "customer" and isinstance(r.get("context"), str) \
                and r["context"].lower() == title.lower() and title.lower() in chains:
            continue   # the chain's head account: found as the chain
        if kind in OBJECT_KINDS:
            rec = ids.get(handle) if isinstance(handle, str) else None
            url = object_url(str(rec["object_type"]), str(rec["object_id"])) if rec else (
                open_url(handle) if isinstance(handle, str) else "")
        elif kind == "chain":
            url = account_url("chain", chain=title)
        elif isinstance(handle, str):
            url = account_url(kind, handle)
        else:
            continue
        rank = int(r.get("rank") or 0)
        sub = []
        if kind == "chain":
            sub.append(f"Chain of {r.get('members')} accounts")
        for key in ("context", "code"):
            v = r.get(key)
            if isinstance(v, str) and v and v != title:
                sub.append(f"PO {v}" if kind in ("sales_order", "sales_invoice") and key == "code" else v)
        if isinstance(r.get("status"), str) and kind in ("sales_order", "purchase_order"):
            sub.append(r["status"])
        out.append(m.Suggestion(
            object_id=str(handle or f"chain:{title}"), type=SEARCH_TYPES[kind],
            label=title,
            sublabel=" · ".join(sub) or None,
            match="exact" if rank <= 1 else "prefix" if rank <= 4 else "fuzzy",
            score=round(1 - rank / 10, 2), open_url=url))
        if len(out) >= limit:
            break
    return out


# ---- record pages ----------------------------------------------------------------------

BUCKETS = ("current", "1-30", "31-60", "61-90", "over 90")
LIST_ROWS = 25


def business_day() -> str:
    from operating.pg import business_views as bv
    return bv.today().isoformat()


def _hidden(row: Mapping[str, Any], col: str) -> bool:
    return col in (row.get("withheld") or ())


def _t(row: Mapping[str, Any], col: str, href: Optional[str] = None) -> m.BizCell:
    v = row.get(col)
    if v is None:
        return m.BizCell(hidden=_hidden(row, col))
    return m.BizCell(text=str(v), href=href)


def _d(row: Mapping[str, Any], col: str) -> m.BizCell:
    return m.BizCell(day=_iso(row.get(col)), hidden=row.get(col) is None and _hidden(row, col))


def _n(row: Mapping[str, Any], col: str) -> m.BizCell:
    v = row.get(col)
    if v is None:
        return m.BizCell(hidden=_hidden(row, col))
    return m.BizCell(number=format(Decimal(str(v)).normalize(), "f")
                     if isinstance(v, Decimal) else str(v))


def _m(row: Mapping[str, Any], amount: str, currency: Optional[str] = None) -> m.BizCell:
    money = _money(row.get(amount), row.get(currency or f"{amount}_currency"))
    return m.BizCell(money=money, hidden=money is None and _hidden(row, amount))


def _sum(amount: Any, currency: Any, hidden: Any = 0) -> m.BizCell:
    money = _money(amount if amount is not None else (0 if not hidden else None), currency)
    return m.BizCell(money=money, hidden=bool(hidden) and money is None)


def _hidden_note(n: int, what: str) -> Optional[str]:
    return (f"{n} {what} you can't see {'is' if n == 1 else 'are'} left out of the totals."
            if n else None)


def _owed(rows: list[Mapping[str, Any]], title: str) -> m.BizTable:
    """What is owed, by currency and aging bucket (each currency its own row). A bucket
    holding any amount the reader may not see is shown hidden, and so is its currency's
    total: never the sum of the amounts they may see as if it were all (11-18 staff walk: a
    supplier's CAD 126,051.50 owed read "0.00" to a reader without finance)."""
    by: dict[Any, dict[str, Any]] = {}
    hidden = 0
    for r in rows:
        cur = by.setdefault(r.get("currency"), {"_docs": 0, "_hidden": set()})
        bucket = r.get("aging_bucket")
        cur[bucket] = cur.get(bucket, Decimal(0)) + Decimal(str(r.get("amount") or 0))
        cur["_docs"] += int(r.get("documents") or 0)
        if int(r.get("hidden") or 0):
            cur["_hidden"].add(bucket)
        hidden += int(r.get("hidden") or 0)
    out = []
    for currency in sorted(by, key=lambda c: (c is None, c or "")):
        if currency is None:
            continue   # amounts whose currency is withheld: counted in the note only
        got = by[currency]
        withheld = got["_hidden"]
        total = sum((v for k, v in got.items() if k not in ("_docs", "_hidden")), Decimal(0))
        out.append([m.BizCell(text=currency)]
                   + [m.BizCell(hidden=True) if b in withheld
                      else _sum(got.get(b, Decimal(0)), currency) for b in BUCKETS]
                   + [m.BizCell(hidden=True) if withheld else _sum(total, currency),
                      m.BizCell(number=str(got["_docs"]))])
    hidden += by.get(None, {}).get("_docs", 0)
    note = _hidden_note(hidden, "open document" if hidden == 1 else "open documents")
    return m.BizTable(key="owed", title=title, columns=[
        "Currency", "Not yet due", "1-30 days", "31-60 days", "61-90 days", "Over 90 days",
        "Total", "Documents"], rows=out, total=sum(int(r.get("documents") or 0) for r in rows),
        note=note or (None if out else "Nothing open."))


def _years(rows: list[Mapping[str, Any]], title: str, as_of: str) -> m.BizTable:
    out, hidden = [], 0
    for r in rows:
        cur = r.get("currency")
        hidden += sum(int(r.get(f"{k}_hidden") or 0)
                      for k in ("this_year", "last_year_to_date", "last_year"))
        if cur is None:
            continue
        out.append([m.BizCell(text=cur), _sum(r.get("this_year"), cur),
                    _sum(r.get("last_year_to_date"), cur), _sum(r.get("last_year"), cur)])
    year = int(as_of[:4])
    return m.BizTable(key="years", title=title, columns=[
        "Currency", f"{year} to {as_of[5:]}", f"{year - 1} to {as_of[5:]}", f"All of {year - 1}"],
        rows=out, note=_hidden_note(hidden, "amount" if hidden == 1 else "amounts")
        or (None if out else f"Nothing since 1 January {year - 1}."))


def _open_totals(rows: list[Mapping[str, Any]]) -> tuple[int, int, list[str], int]:
    """(orders, late, 'CAD 1,234.00'-style totals, orders with no value shown)."""
    n = sum(int(r.get("orders") or 0) for r in rows)
    late = sum(int(r.get("late") or 0) for r in rows)
    unvalued = sum(int(r.get("unvalued") or 0) for r in rows)
    totals = [f"{r['currency']} {Decimal(str(r['value'])):,.2f}" for r in rows
              if r.get("currency") and r.get("value") is not None]
    return n, late, totals, unvalued


def _orders_note(n: int, late: int, totals: list[str], unvalued: int, shown: int) -> str:
    if not n:
        return "None open."
    parts = [f"{n} open" + (f", {late} late" if late else "")]
    if totals:
        parts.append("worth " + " and ".join(totals))
    if unvalued:
        parts.append(f"{unvalued} without a value you can see")
    if shown < n:
        parts.append(f"the first {shown} shown")
    return "; ".join(parts) + "."


def _sources(accounts: list[Mapping[str, Any]], built: Mapping[str, Any],
             views: Sequence[str], held_in: Any = None) -> str:
    """The systems the page's records come from (the accounts' codes, or a record's own
    ``source_systems``: "bc,portal" or a list) and the oldest read's time."""
    systems = sorted({(codes_text(f"{p}") or "").split(" ")[0] for a in accounts
                      for p in str(a.get("codes") or "").split(",") if ":" in p} - {""})
    named = held_in.split(",") if isinstance(held_in, str) else held_in \
        if isinstance(held_in, (list, tuple)) else ()
    systems = sorted(set(systems) | {_SYSTEMS.get(str(n).strip().lower(), str(n).strip().title())
                                     for n in named if str(n).strip()})
    stamps = [built[v] for v in views if isinstance(built.get(v), datetime)]
    when = None
    if stamps:
        from zoneinfo import ZoneInfo
        t = min(stamps).astimezone(ZoneInfo("America/New_York"))
        when = f"{t:%b} {t.day}, {t:%I:%M %p}".replace(" 0", " ") + " ET"
    return ("Sources: " + (", ".join(systems) if systems else "the business records")
            + (f"; as of {when}" if when else ""))


def _built(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    return {r.get("view"): r.get("built_at") for r in rows}


class NotFound(LookupError):
    pass


def account(backend: Any, reader: Any, kind: str, *, handle: str = "", chain: str = ""
            ) -> m.BizPage:
    """The account page of a customer (``handle``), a chain (``chain``: its store accounts
    rolled up) or a supplier (``handle``), all from one transaction through the door."""
    started = time.perf_counter()
    day = business_day()
    if kind == "supplier":
        return _supplier(backend, reader, handle, day, started)
    if kind not in ("customer", "chain") or not (handle if kind == "customer" else chain):
        raise NotFound(kind)
    key = {"handle": handle if kind == "customer" else "", "chain": chain if kind == "chain"
           else ""}
    reads = [{"name": "customer_accounts", "params": key},
             {"name": "customer_open_orders", "params": {**key, "limit": LIST_ROWS}},
             {"name": "customer_open_totals", "params": key},
             {"name": "customer_owed", "params": key},
             {"name": "customer_invoices", "params": {**key, "limit": 10}},
             {"name": "customer_sales_years", "params": {**key, "as_of": day}},
             {"name": "views_as_of"}]
    if kind == "customer":
        reads.append({"name": "customer_chain_size", "params": {"handle": handle}})
    rows, ids = read(backend, reader, reads, ids=True)
    accounts = rows["customer_accounts"]
    if not accounts:
        raise NotFound(kind)
    head = accounts[0]
    facts: list[m.BizFact] = []
    if kind == "customer":
        title = str(head.get("name") or head.get("customer_code") or "Customer")
        n_chain = int((rows.get("customer_chain_size") or [{}])[0].get("accounts") or 0)
        if head.get("chain") and n_chain > 1:
            facts.append(m.BizFact(label="Chain", value=m.BizCell(
                text=f"{head['chain']} ({n_chain} accounts)",
                href=account_url("chain", chain=str(head["chain"])))))
        codes = codes_text(head.get("codes"), head.get("customer_code"))
        facts.append(m.BizFact(label="Codes", value=m.BizCell(
            text=codes or head.get("customer_code"), hidden=not codes and _hidden(head, "codes"))))
        for label, col in (("Currency", "currency"), ("Rep", "salesperson"),
                           ("Contact", "contact_person"), ("Email", "email"),
                           ("Phone", "phone")):
            if head.get(col) is not None or _hidden(head, col):
                facts.append(m.BizFact(label=label, value=_t(head, col)))
        if head.get("credit_limit") is not None or _hidden(head, "credit_limit"):
            facts.append(m.BizFact(label="Credit limit", value=_m(head, "credit_limit")))
        if head.get("blocked"):
            facts.append(m.BizFact(label="Blocked", value=_t(head, "blocked")))
        subtitle = "Customer"
    else:
        title = chain
        subtitle = f"Chain of {len(accounts)} accounts"
        currencies = sorted({a["currency"] for a in accounts if a.get("currency")})
        if currencies:
            facts.append(m.BizFact(label="Currency", value=m.BizCell(text=", ".join(currencies))))
        reps = sorted({a["salesperson"] for a in accounts if a.get("salesperson")})
        if reps:
            facts.append(m.BizFact(label="Rep", value=m.BizCell(text=", ".join(reps))))
        code = next((a.get("chain_code") for a in accounts if a.get("chain_code")), None)
        if code:
            facts.append(m.BizFact(label="Chain code", value=m.BizCell(text=str(code))))
    sections: list[m.BizTable] = []
    if kind == "chain":
        sections.append(m.BizTable(key="accounts", title="Accounts", columns=[
            "Account", "Code", "Currency", "Rep"], rows=[[
                _t(a, "name", account_url("customer", a.get("handle"))), _t(a, "customer_code"),
                _t(a, "currency"), _t(a, "salesperson")] for a in accounts],
            total=len(accounts)))
    n, late, totals, unvalued = _open_totals(rows["customer_open_totals"])
    listed = rows["customer_open_orders"]
    cols = ["Order", "Customer PO", "Ordered", "Required by", "Days late", "Value"]
    if kind == "chain":
        cols.insert(1, "Account")
    sections.append(m.BizTable(key="open_orders", title="Open orders", columns=cols, rows=[
        [_t(o, "order_number", account_url("sales_order", o.get("handle")))]
        + ([_t(o, "customer")] if kind == "chain" else [])
        + [_t(o, "customer_po"), _d(o, "order_date"), _d(o, "required_date"),
           _n(o, "days_late") if (o.get("days_late") or 0) > 0 else m.BizCell(),
           _m(o, "value")] for o in listed], total=n,
        note=_orders_note(n, late, totals, unvalued, len(listed))))
    sections.append(_owed(rows["customer_owed"], "What they owe"))
    inv = rows["customer_invoices"]
    sections.append(m.BizTable(key="invoices", title="Last invoices", columns=[
        "Invoice", "Date", "Due", "Order", "Total", "Still owed"], rows=[[
            _t(i, "invoice_number", _row_url(i, ids)), _d(i, "invoice_date"), _d(i, "due_date"),
            _t(i, "order_number", find_url(i.get("order_number"))), _m(i, "total"),
            _m(i, "remaining")] for i in inv],
        note=None if inv else "No invoices."))
    sections.append(_years(rows["customer_sales_years"], "Sales this year and last (before tax)",
                           day))
    built = _built(rows["views_as_of"])
    return m.BizPage(kind=kind, title=title, subtitle=subtitle, facts=facts,
                     sections=sections, as_of=day, sources=_sources(accounts, built, (
                         "customers", "sales_orders", "customer_balances", "sales_invoices")),
                     took_ms=int((time.perf_counter() - started) * 1000))


def _supplier(backend: Any, reader: Any, handle: str, day: str, started: float) -> m.BizPage:
    if not handle:
        raise NotFound("supplier")
    key = {"handle": handle}
    rows, ids = read(backend, reader, [
        {"name": "supplier_account", "params": key},
        {"name": "supplier_open_pos", "params": {**key, "limit": LIST_ROWS}},
        {"name": "supplier_open_totals", "params": key},
        {"name": "supplier_owed", "params": key},
        {"name": "supplier_invoices", "params": {**key, "limit": 10}},
        {"name": "supplier_buys_years", "params": {**key, "as_of": day}},
        {"name": "supplier_same_name", "params": key},
        {"name": "views_as_of"}], ids=True)
    if not rows["supplier_account"]:
        raise NotFound("supplier")
    head = rows["supplier_account"][0]
    facts = []
    codes = codes_text(head.get("codes"), head.get("supplier_code"))
    facts.append(m.BizFact(label="Codes", value=m.BizCell(
        text=codes or head.get("supplier_code"), hidden=not codes and _hidden(head, "codes"))))
    for label, col in (("Currency", "currency"), ("Status", "status"),
                       ("Incoterms", "incoterms"), ("Lead time (days)", "lead_time_days"),
                       ("Email", "email"), ("Phone", "phone"), ("Blocked", "blocked")):
        if head.get(col) is not None or _hidden(head, col):
            facts.append(m.BizFact(label=label, value=_t(head, col)))
    n, late, totals, unvalued = _open_totals(rows["supplier_open_totals"])
    listed = rows["supplier_open_pos"]
    sections = [m.BizTable(key="open_orders", title="Open purchase orders", columns=[
        "PO", "Ordered", "Needed by", "Promised", "Days late", "Total"], rows=[[
            _t(p, "po_number", account_url("purchase_order", p.get("handle"))),
            _d(p, "order_date"), _d(p, "requested_date"), _d(p, "promised_date"),
            _n(p, "days_late") if p.get("verdict") == "late" else m.BizCell(),
            _m(p, "total")] for p in listed], total=n,
        note=_orders_note(n, late, totals, unvalued, len(listed)))]
    sections.append(_owed(rows["supplier_owed"], "What we owe them"))
    inv = rows["supplier_invoices"]
    sections.append(m.BizTable(key="invoices", title="Last invoices", columns=[
        "Document", "Type", "Date", "Due", "PO", "Amount", "Still owed"], rows=[[
            _t(i, "document_number", _row_url(i, ids)), _t(i, "document_type"),
            _d(i, "document_date"), _d(i, "due_date"),
            _t(i, "po_number", find_url(i.get("po_number"))), _m(i, "original_amount"),
            _m(i, "amount_due")] for i in inv], note=None if inv else "No invoices."))
    sections.append(_years(rows["supplier_buys_years"], "Ordered this year and last", day))
    same = rows.get("supplier_same_name") or []
    if same:
        # BC keeps a payment vendor ("X (EFT)") beside the purchasing vendor the POs name:
        # each its own account, listed here, never added into this one
        sections.append(m.BizTable(key="same_name", title="Other accounts by this name",
                                   columns=["Account", "Code", "Open POs"], rows=[[
                                       _t(r, "name", account_url("supplier", r.get("handle"))),
                                       _t(r, "supplier_code"), _n(r, "open_pos")]
                                       for r in same], total=len(same),
                                   note="Separate accounts in the source systems; their "
                                   "orders and balances are on their own pages."))
    built = _built(rows["views_as_of"])
    return m.BizPage(kind="supplier", title=str(head.get("name") or head.get("supplier_code")
                                                or "Supplier"), subtitle="Supplier",
                     facts=facts, sections=sections, as_of=day,
                     sources=_sources([head], built, ("suppliers", "purchase_orders",
                                                      "supplier_balances", "supplier_invoices")),
                     took_ms=int((time.perf_counter() - started) * 1000))


RECORD_SWITCH = "PLX_RECORD_DOOR"


def record_door_on() -> bool:
    """``PLX_RECORD_DOOR=1``: a purchase order's page reads its header, cells and lines from
    the precomputed page (``operating.pg.record_pages``) in one read; off (the default), from
    the business views as before. Exactly ``1``: anything else (a space, ``true``) is off."""
    return os.environ.get(RECORD_SWITCH, "") == "1"


def _payload(item: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """An item's payload with its numbers as Decimals of the stored scale (the text read:
    1993.30 stays 1993.30, as the business view's numeric gives it), None when it is NULL."""
    import json
    text = item.get("payload_text")
    if not isinstance(text, str):
        return None
    got = json.loads(text, parse_float=Decimal)
    return got if isinstance(got, dict) else None


def _from_record_page(items: Sequence[Mapping[str, Any]]
                      ) -> Optional[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """A purchase order's header row and line rows as the business views give them, rebuilt
    from the record page's items (``operating.pg.record_pages``): the header's payload plus each
    cell item's one column, a cell whose payload the reader may not read (NULL, its column named
    by its handle's last part) listed in ``withheld``. None when the page cannot say it whole
    (no header, a header read as a whole refused): the caller reads the views."""
    items = [{**i, "payload": _payload(i)} for i in items]
    header = [i for i in items if i["section"] == "header"]
    if len(header) != 1 or not isinstance(header[0].get("payload"), dict):
        return None
    h: dict[str, Any] = dict(header[0]["payload"])
    h["handle"] = header[0]["src_handle"]
    hidden: list[str] = list(header[0].get("withheld") or ())
    for i in items:
        if i["section"] != "cell":
            continue
        col = str(i["handle"]).rsplit("#", 1)[-1]
        if isinstance(i.get("payload"), dict) and col in i["payload"]:
            h[col] = i["payload"][col]
        else:
            h.setdefault(col, None)
            hidden.append(col)
    h["withheld"] = hidden
    by_line: dict[str, dict[str, Any]] = {}
    order_: list[str] = []
    for i in items:
        if i["section"] == "lines":
            if not isinstance(i.get("payload"), dict):
                return None
            row = dict(i["payload"])
            row["handle"] = i["src_handle"]
            row["withheld"] = list(i.get("withheld") or ())
            by_line[i["src_handle"]] = row
            order_.append(i["src_handle"])
    for i in items:
        if i["section"] != "line_cell":
            continue
        row = by_line.get(i["src_handle"])
        if row is None:
            continue                  # a cell of a line the reader may not read
        col = str(i["handle"]).rsplit("#", 1)[-1]
        if isinstance(i.get("payload"), dict) and col in i["payload"]:
            row[col] = i["payload"][col]
        else:
            row.setdefault(col, None)
            row["withheld"].append(col)
    return h, [by_line[k] for k in order_]


RECORD_PAGE_LIMIT = 2000
_DOOR_COUNTS = {"served": 0, "fell_back": 0}
_DOOR_LOG_EVERY = 3600.0   # seconds between the log lines of how often the door served
_DOOR_LAST = [time.monotonic()]


def count_door(served: bool) -> None:
    """Count one purchase order page read through the record door (served from the items, or
    fell back to the views) and log the counts about once an hour, then start again: how often
    the precomputed page helps."""
    import logging
    _DOOR_COUNTS["served" if served else "fell_back"] += 1
    now = time.monotonic()
    if now - _DOOR_LAST[0] >= _DOOR_LOG_EVERY:
        logging.getLogger("operator_api.page_sql").info(
            "record door: %d served, %d fell back in the last %.0f s",
            _DOOR_COUNTS["served"], _DOOR_COUNTS["fell_back"], now - _DOOR_LAST[0])
        _DOOR_COUNTS.update(served=0, fell_back=0)
        _DOOR_LAST[0] = now


def order_from_record(backend: Any, reader: Any, record_id: str) -> Optional[m.BizPage]:
    """A purchase order's page from the record page of ``record_id`` (one indexed read, the
    reader's door), or None when it cannot be built whole from it (no page items for the record,
    a refused header, a page cut at the read's limit, more lines than the page shows): the
    caller then reads the business views."""
    if not record_id or len(record_id) > 700:
        return None
    rows, _ = read(backend, reader, [
        {"name": "record_page", "params": {"record_id": record_id,
                                           "limit": RECORD_PAGE_LIMIT}},
        {"name": "views_as_of"}])
    items = rows["record_page"]
    if not items or len(items) >= RECORD_PAGE_LIMIT:
        return None
    built = items_built(rows["views_as_of"])
    if built is None:
        return None          # the items are older than a view they rest on (or say nothing)
    got = _from_record_page(items)
    if got is None or len(got[1]) > MAX_LINES:
        return None
    # the page's as-of is the items' own, never the views' (they may be newer)
    return order(backend, reader, "purchase_order", got[0].get("handle") or "",
                 loaded=(got[0], got[1], [{"view": "purchase_orders", "built_at": built},
                                          {"view": "purchase_order_lines", "built_at": built}]))


# the views the page's header, cells and lines are built from: their build stamps (``view_state``).
# The same freshness test as ``record_pages.stale`` (the order views' stamps plus the links'
# count and newest version, which the door cannot see: and this page shows no link). Never the
# ``records`` stamp: the generic refresh rewrites it every cycle (generic_records.py), which
# ``stale`` ignores, so it would send most reads back to the views.
ITEM_SOURCES = ("purchase_orders", "purchase_order_lines")


def items_built(states: Sequence[Mapping[str, Any]]) -> Optional[datetime]:
    """When the record page's items were built, or None when they cannot be trusted: no stamp
    of their own, or either order view they are built from (``ITEM_SOURCES``)
    built AFTER them (the follow-up build failed or has not run: their rows are old, never
    served under a fresh as-of). An unreadable stamp is not trusted either."""
    def when(value: Any) -> Optional[datetime]:
        try:
            got = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        return got if got.tzinfo else got.replace(tzinfo=timezone.utc)
    stamps = {r.get("view"): r.get("built_at") for r in states}
    from operating.pg.page_items import state_view
    mine = when(stamps.get(state_view("record"))) if state_view("record") in stamps else None
    if mine is None:
        return None
    for view in ITEM_SOURCES:
        if view not in stamps:
            continue
        theirs = when(stamps[view])
        if theirs is None or theirs > mine:
            return None
    return mine


def order(backend: Any, reader: Any, kind: str, handle: str,
          loaded: Optional[tuple[dict[str, Any], list[dict[str, Any]], list[Any]]] = None
          ) -> m.BizPage:
    """A purchase order or a customer order: its header, then its lines. ``loaded``: the
    header, lines and views' times already read (the record page's: ``order_from_record``)."""
    started = time.perf_counter()
    if kind not in ("purchase_order", "sales_order") or not handle:
        raise NotFound(kind)
    if loaded is not None:
        h, lines, as_of = loaded
        rows = {"views_as_of": as_of}
    else:
        rows, _ = read(backend, reader, [
            {"name": kind, "params": {"handle": handle}},
            {"name": f"{kind}_lines", "params": {"handle": handle, "limit": MAX_LINES}},
            {"name": "views_as_of"}])
        if not rows[kind]:
            raise NotFound(kind)
        h, lines = rows[kind][0], rows[f"{kind}_lines"]
    facts: list[m.BizFact] = []
    if kind == "purchase_order":
        title = _numbered("PO", h.get("po_number"))
        party = _t(h, "supplier", account_url("supplier", h["supplier_handle"])
                   if h.get("supplier_handle") else None)
        facts.append(m.BizFact(label="Supplier", value=party))
        spec = (("Status", "status", _t), ("Ordered", "order_date", _d),
                ("Needed by", "requested_date", _d), ("Promised", "promised_date", _d),
                ("Purchaser", "purchaser", _t))
        money = "total"
        cols = ["Item", "Description", "Ordered", "Received", "Invoiced", "Unit",
                "Unit price", "Line total", "Expected"]
        body = [[_t(r, "item_code", find_url(r.get("item_code"))), _t(r, "description"),
                 _n(r, "ordered_quantity"),
                 _n(r, "received_quantity"), _n(r, "invoiced_quantity"), _t(r, "uom"),
                 _m(r, "unit_price"), _m(r, "line_total"), _d(r, "expected_date")]
                for r in lines]
        if h.get("verdict") == "late":
            facts.append(m.BizFact(label="Days late", value=_n(h, "days_late")))
    else:
        title = _numbered("SO", h.get("order_number"))
        party = _t(h, "customer", account_url("customer", h["customer_handle"])
                   if h.get("customer_handle") else None)
        facts.append(m.BizFact(label="Customer", value=party))
        spec = (("Status", "status", _t), ("Customer PO", "customer_po", _t),
                ("Ordered", "order_date", _d), ("Required by", "required_date", _d),
                ("Rep", "salesperson", _t))
        money = "value"
        cols = ["Item", "Name", "Ordered", "Shipped", "Still to ship", "Unit", "Unit price",
                "Amount"]
        body = [[_t(r, "item_code", find_url(r.get("item_code"))),
                 _t(r, "item_name") if r.get("item_name") is not None
                 else _t(r, "description"), _n(r, "ordered_quantity"), _n(r, "shipped_quantity"),
                 _n(r, "outstanding_quantity"), _t(r, "uom"), _m(r, "unit_price"),
                 _m(r, "line_amount")] for r in lines]
        if h.get("status") == "open" and (h.get("days_late") or 0) > 0:
            # only a live order is late: one with nothing left to ship, or that nobody has
            # worked for longer than closed orders take, is open on the books and said so
            not_live = {"nothing_outstanding": "Every line states nothing left to ship",
                        "stale": "Nobody has ordered or invoiced it for longer than closed "
                                 "orders take"}.get(h.get("open_state") or "")
            facts.append(m.BizFact(label="Past its required date, not counted late",
                                   value=m.BizCell(text=not_live)) if not_live
                         else m.BizFact(label="Days late", value=_n(h, "days_late")))
    for label, col, cell in spec:
        if h.get(col) is not None or _hidden(h, col):
            facts.append(m.BizFact(label=label, value=cell(h, col)))
    note = None if lines else "No lines on record."
    if kind == "purchase_order" and h.get("status") == "open" and all(
            h.get(c) is None and not _hidden(h, c) for c in ("requested_date", "promised_date")):
        # a PO nobody dated can never be late: say so beside the other facts, never leave it
        # out (live 11-23: 102 of 111 open POs carry no needed-by or promised date)
        facts.append(m.BizFact(label="Needed by", value=m.BizCell(
            text="No date on record: no needed-by or promised date from the PO, its lines or "
                 "the supplier")))
    zero = h.get("total")
    if money == "total" and zero is not None and Decimal(str(zero)) == 0 and (
            h.get("lines_total") is None or Decimal(str(h["lines_total"])) == 0) \
            and h.get("total_basis") != "lines":
        # the source states 0.00 and no line is priced: no amount is known, never "$0.00"
        facts.append(m.BizFact(label="Total", value=m.BizCell(
            text="Not on record (the source states 0.00 and no line is priced)")))
    elif money == "total" and h.get("total_basis") == "lines":
        # no copy states a total above 0: the order is worth its priced lines, said so
        facts.append(m.BizFact(label="Total (sum of lines)", value=_m(h, "total")))
    else:
        facts.append(m.BizFact(label="Total" if money == "total" else "Value",
                               value=_m(h, money)))
        lt, total = h.get("lines_total"), h.get("total")
        if money == "total" and lt is not None and (
                total is None or Decimal(str(lt)) != Decimal(str(total))
                or h.get("lines_total_currency") != h.get("total_currency")):
            # the header and its lines disagree (tax, freight, a header not updated): both
            facts.append(m.BizFact(label="Lines total", value=_m(h, "lines_total")))
    if kind == "sales_order" and lines and all(
            r.get("shipped_quantity") is None and not _hidden(r, "shipped_quantity")
            for r in lines):
        note = "Shipped and still-to-ship quantities are not on these order lines."
    expected = h.get("line_count")
    if isinstance(expected, int) and expected > len(lines):
        note = f"{len(lines)} of {expected} lines shown."
    built = _built(rows["views_as_of"])
    table = "purchase_orders" if kind == "purchase_order" else "sales_orders"
    return m.BizPage(kind=kind, title=title, subtitle=(
        "Purchase order" if kind == "purchase_order" else "Customer order"), facts=facts,
        sections=[m.BizTable(key="lines", title="Lines", columns=cols, rows=body,
                             total=len(lines), note=note)],
        sources=_sources([], built, (table, f"{table[:-1]}_lines"), h.get("source_systems")),
        as_of=None, took_ms=int((time.perf_counter() - started) * 1000))


MAX_LINES = 500

def record_views() -> list[m.RecordView]:
    """Every list Records offers: one per business view the page door serves, from the
    views' own metadata (``operating.record_lists``)."""
    from operating import record_lists
    return [m.RecordView(view=s.view, title=s.title) for s in record_lists.specs().values()]


def _cell(spec_col: Any, row: Mapping[str, Any], href: Optional[str]) -> m.BizCell:
    """One list cell, as its column's kind reads it (money with its currency, a day, a
    number, yes/no, text); the row's link goes on the column named for it."""
    name = spec_col.name
    if spec_col.kind == "money":
        return _m(row, name, spec_col.currency)
    if spec_col.kind == "date":
        return _d(row, name)
    if spec_col.kind == "number":
        return _n(row, name)
    if spec_col.kind == "boolean":
        v = row.get(name)
        return m.BizCell(text=None if v is None else ("Yes" if v else "No"),
                         hidden=v is None and _hidden(row, name))
    return _t(row, name, href)


def record_list(backend: Any, reader: Any, view: str, *, filters: Optional[Mapping[str, str]] = None,
                q: str = "", sort: str = "", direction: str = "", offset: int = 0,
                limit: int = 25, with_filters: bool = True) -> m.RecordList:
    """A Records list of one business view through the reader's door (a row the reader may
    not read is not here; a cell they may not read is hidden): its columns, filters and sort
    come from the view's metadata (``operating.record_lists``). An unknown view, sort column
    or direction, filter or filter value is refused (``NotFound``), never ignored."""
    from operating import record_lists as rl
    started = time.perf_counter()
    spec = rl.specs().get(view)
    if spec is None or len(q) > rl.MAX_SEARCH:
        raise NotFound(view)
    sort, direction = sort or spec.sort, direction or spec.direction
    if spec.column(sort) is None or direction not in ("asc", "desc"):
        raise NotFound(view)
    bound: dict[str, Any] = {}
    for name, value in (filters or {}).items():
        f = spec.filter(name)
        if f is None or len(value) > rl.MAX_FILTER_VALUE or (
                f.kind == "boolean" and value not in ("", "yes", "no")):
            raise NotFound(view)
        bound[rl.param(name)] = value
    for f in spec.filters:
        bound.setdefault(rl.param(f.name), "")
    if spec.search:
        bound["q"] = q.strip()
    reads: list[dict[str, Any]] = [
        {"name": rl.list_name(view, sort, direction),
         "params": {**bound, "limit": limit, "offset": offset}},
        {"name": f"list__{view}__count", "params": bound}, {"name": "views_as_of"}]
    choices = with_filters and any(f.kind == "choice" for f in spec.filters)
    if choices:
        reads.append({"name": f"list__{view}__choices"})
    rows, ids = read(backend, reader, reads, ids=True)
    total = int(rows[f"list__{view}__count"][0]["n"])
    first = spec.columns[0].name
    body = []
    for r in rows[rl.list_name(view, sort, direction)]:
        href = _row_url(r, ids)
        body.append([_cell(c, r, href if c.name == first else None) for c in spec.columns])
    offered: list[m.RecordFilter] = []
    if with_filters:
        by_col: dict[str, list[m.RecordFilterOption]] = {}
        for r in rows.get(f"list__{view}__choices", []):
            by_col.setdefault(str(r["col"]), []).append(m.RecordFilterOption(
                value=str(r["value"]), label=str(r["value"]), count=int(r["n"])))
        for f in spec.filters:
            options = ([m.RecordFilterOption(value="", label="All")] + by_col.get(f.name, [])
                       if f.kind == "choice" else [
                m.RecordFilterOption(value="", label="All"),
                m.RecordFilterOption(value="yes", label="Yes"),
                m.RecordFilterOption(value="no", label="No")])
            offered.append(m.RecordFilter(key=f.name, label=f.label, options=options))
    built = _built(rows["views_as_of"]).get(view)
    after = offset + len(body)
    return m.RecordList(
        view=view, title=spec.title,
        table=m.BizTable(key=view, title=spec.title, columns=[c.label for c in spec.columns],
                         rows=body, total=total,
                         note=None if body else "Nothing on record matches these filters."),
        column_keys=[c.name for c in spec.columns], sort=sort, direction=direction,
        searchable=bool(spec.search), filters=offered, total=total, offset=offset,
        more=after < total, next_offset=after, as_of=_iso(built))


def _qty(row: Mapping[str, Any], col: str, unit: Optional[str] = None) -> m.BizCell:
    """A quantity with its unit said beside it ("120 kg")."""
    cell = _n(row, col)
    if cell.number is not None and unit:
        cell.number = f"{cell.number} {unit}"
    return cell


def item(backend: Any, reader: Any, handle: str) -> m.BizPage:
    """An item's page: what it is, its stock on hand from the stock view (its own unit and
    the day the view was built), and its recent batches (each linked to its record)."""
    started = time.perf_counter()
    if not handle:
        raise NotFound("product")
    key = {"handle": handle}
    rows, ids = read(backend, reader, [
        {"name": "product", "params": key},
        {"name": "product_stock", "params": key},
        {"name": "product_batches", "params": {**key, "limit": 25}},
        {"name": "views_as_of"}], ids=True)
    if not rows["product"]:
        raise NotFound("product")
    h = rows["product"][0]
    facts: list[m.BizFact] = []
    for label, col, cell in (("Item code", "item_code", _t), ("Type", "item_type", _t),
                             ("Category", "category", _t), ("Unit", "uom", _t),
                             ("Active", "active", _t), ("Blocked", "blocked", _t)):
        if h.get(col) is not None or _hidden(h, col):
            facts.append(m.BizFact(label=label, value=cell(h, col)))
    for label, col in (("Unit cost", "unit_cost"), ("Unit price", "unit_price")):
        if h.get(col) is not None or _hidden(h, col):
            facts.append(m.BizFact(label=label, value=_m(h, col)))
    stock = rows["product_stock"]
    if stock:
        st = stock[0]
        facts.insert(0, m.BizFact(label="On hand", value=_qty(st, "on_hand", st.get("on_hand_unit"))))
    batches = rows["product_batches"]
    built = _built(rows["views_as_of"])
    sections = [m.BizTable(key="batches", title="Recent batches", columns=[
        "Batch", "Lot", "Status", "QC", "Scheduled", "Planned", "Produced"], rows=[[
            _t(b, "batch_code", _row_url(b, ids)), _t(b, "lot_number"), _t(b, "status"),
            _t(b, "quality_status"), _d(b, "scheduled_date"), _n(b, "planned_quantity"),
            _n(b, "produced_quantity")] for b in batches], total=len(batches),
        note=None if batches else "No batch on record makes this item.")]
    note = None if stock else "No source states a quantity on hand for this item."
    return m.BizPage(
        kind="product", title=str(h.get("name") or h.get("item_code") or "Item"),
        subtitle="Item", facts=facts, sections=sections,
        sources=_sources([], built, ("products", "stock", "batches")) +
        (f" {note}" if note else ""), as_of=_iso(stock[0].get("as_of")) if stock else None,
        took_ms=int((time.perf_counter() - started) * 1000))


def batch(backend: Any, reader: Any, handle: str) -> m.BizPage:
    """A production batch's page: its product, lot, status, dates, quantities and QC."""
    started = time.perf_counter()
    if not handle:
        raise NotFound("batch")
    rows, _ = read(backend, reader, [{"name": "batch", "params": {"handle": handle}},
                                     {"name": "views_as_of"}])
    if not rows["batch"]:
        raise NotFound("batch")
    h = rows["batch"][0]
    facts: list[m.BizFact] = []
    for label, col, cell in (
            ("Product", "product", _t), ("Item code", "item_code", _t), ("Lot", "lot_number", _t),
            ("Status", "status", _t), ("QC status", "quality_status", _t),
            ("Scheduled", "scheduled_date", _d), ("Made", "realized_date", _d),
            ("Released", "released_date", _d), ("Planned", "planned_quantity", _n),
            ("Produced", "produced_quantity", _n), ("Line", "production_line", _t),
            ("Customer PO", "po_number", _t), ("Tested by", "tested_by", _t),
            ("Checked by", "checked_by", _t)):
        if h.get(col) is not None or _hidden(h, col):
            facts.append(m.BizFact(label=label, value=cell(h, col)))
    if h.get("on_hold") is True:
        # a cancelled batch is on no hold (the lists and the measures leave it out); its QC
        # status line still says what was recorded
        cancelled = str(h.get("status") or "").lower() == "cancelled"
        facts.insert(0, m.BizFact(label="Hold", value=m.BizCell(
            text="Cancelled; QC status still reads on hold" if cancelled
            else "QC holds this batch")))
    qc = [(label, col) for label, col in (
        ("pH after 24 h", "qc_ph_24h"), ("Viscosity after 24 h", "qc_viscosity_24h"),
        ("Plate count / yeast and mould", "qc_tpc_yeast")) if h.get(col) is not None
        or _hidden(h, col)]
    sections = [m.BizTable(key="qc", title="QC readings", columns=["Reading", "Result"],
                           rows=[[m.BizCell(text=label), _t(h, col)] for label, col in qc],
                           total=len(qc))] if qc else []
    built = _built(rows["views_as_of"])
    return m.BizPage(
        kind="batch", title=_numbered("Batch", h.get("batch_code")), subtitle="Production batch",
        facts=facts, sections=sections, sources=_sources([], built, ("batches",)), as_of=None,
        took_ms=int((time.perf_counter() - started) * 1000))


def supplier_invoice(backend: Any, reader: Any, handle: str) -> m.BizPage:
    """A supplier invoice's page (a Sage payables document or a BC purchase invoice): who
    billed it, its number, type and dates, what it was for and what is still owed."""
    started = time.perf_counter()
    if not handle:
        raise NotFound("supplier_invoice")
    rows, _ = read(backend, reader, [{"name": "supplier_invoice", "params": {"handle": handle}},
                                     {"name": "views_as_of"}])
    if not rows["supplier_invoice"]:
        raise NotFound("supplier_invoice")
    h = rows["supplier_invoice"][0]
    party = _t(h, "supplier", account_url("supplier", h["supplier_handle"])
               if h.get("supplier_handle") else None)
    facts: list[m.BizFact] = [m.BizFact(label="Supplier", value=party)]
    for label, col, cell in (
            ("Supplier code", "supplier_code", _t), ("Document", "document_number", _t),
            ("Type", "document_type", _t), ("Dated", "document_date", _d),
            ("Due", "due_date", _d), ("Amount", "original_amount", _m),
            ("Still owed", "amount_due", _m),
            ("Purchase order", "po_number", lambda r, c: _t(r, c, find_url(r.get(c)))),
            ("For", "description", _t), ("Supplier group", "vendor_group", _t),
            ("Kind of payee", "payee_class", _t)):
        if h.get(col) is not None or _hidden(h, col):
            facts.append(m.BizFact(label=label, value=cell(h, col)))
    if h.get("paid") is not None:
        facts.append(m.BizFact(label="Paid", value=m.BizCell(text="Yes" if h["paid"] else "No")))
    if h.get("pilot") is True:
        facts.append(m.BizFact(label="Source", value=m.BizCell(
            text="A Business Central pilot record, not the system of record")))
    built = _built(rows["views_as_of"])
    return m.BizPage(
        kind="supplier_invoice", title=_numbered("Invoice", h.get("document_number")),
        subtitle="Supplier invoice", facts=facts, sections=[],
        sources=_sources([], built, ("supplier_invoices",)), as_of=None,
        took_ms=int((time.perf_counter() - started) * 1000))


def _numbered(word: str, number: Any) -> str:
    """'PO 106093'; a number that already says it ('PO-1') is said once."""
    text = str(number or "").strip()
    return text if text.upper().startswith(word) else f"{word} {text}".strip()

__all__ = ["SWITCH", "NotFound", "PageDoorUnavailable", "account", "account_url", "batch",
           "codes_text", "item", "late_po", "late_pos", "record_list", "record_views", "on", "order", "read", "search",
           "supplier_invoice"]


# ---- a number several records hold: the record page's choices ------------------------
# A bare PO or order number is no one record (numbers repeat across systems and
# counterparties: two suppliers' orders share a PO100xxx): the page lists the business
# records it names, one per business row (copies of one order are one row), by number and
# counterparty, each read through the reader's door (a row they may not read is left out,
# never counted). At most CHOICES_MOST records are looked at.
CHOICES_MOST = 12
_SYSTEM_WORDS = {"bc": "BC", "portal": "Portal", "filemaker": "FileMaker", "sage": "Sage"}
# a business row's number and counterparty columns, by page kind (the door's own reads)
_CHOICE_COLS = {"purchase_order": ("po_number", "supplier"),
                "sales_order": ("order_number", "customer")}


def _choice_label(row: Mapping[str, Any], number: str, party: str) -> str:
    bits = [str(row[c]) for c in (number, party) if row.get(c) is not None]
    systems = ", ".join(_SYSTEM_WORDS.get(s, s) for s in
                        str(row.get("source_systems") or "").split(",") if s)
    tail = ", ".join(x for x in (systems, str(row.get("status") or "")) if x)
    return " · ".join(bits) + (f" ({tail})" if tail else "")


def choices(backend: Any, reader: Any, object_ids: Sequence[str]) -> list[m.ObjectRef]:
    """The records ``object_ids`` name that ``reader`` may open, one per business row."""
    out: list[m.ObjectRef] = []
    seen: set[str] = set()
    plain_ids: list[str] = []
    for oid in sorted(object_ids)[:CHOICES_MOST]:
        otype = oid.split(":", 1)[0]
        kind = DOOR_KINDS.get(otype)
        if kind not in _CHOICE_COLS or not on(kind):
            plain_ids.append(oid)
            continue
        try:
            handle = resolve(backend, reader, kind, oid)
        except NotFound:
            plain_ids.append(oid)   # no business row: the record's own page
            continue
        if handle in seen:
            continue   # another copy of the same order
        seen.add(handle)
        rows, _ = read(backend, reader, [{"name": kind, "params": {"handle": handle}}])
        if not rows.get(kind):
            continue   # a row this reader may not read: left out, never counted
        out.append(m.ObjectRef(object_id=oid, type=otype,
                               label=_choice_label(rows[kind][0], *_CHOICE_COLS[kind])[:200],
                               open_url=account_url(kind, handle)))
    if plain_ids:
        worker = getattr(backend, "access_worker", None) or backend.reads
        from .page_core import CORE_PRIORITY
        from .serving import plain
        try:
            words = worker.run(reader, lambda v: v.access.object_words(plain_ids),
                               timeout=_timeout(worker, reader), priority=CORE_PRIORITY)
        except TimeoutError:
            raise PageDoorUnavailable("timeout") from None
        for oid in plain_ids:
            got = (words or {}).get(oid)
            if got is None:
                continue   # not one this reader may read
            otype = oid.split(":", 1)[0]
            out.append(m.ObjectRef(object_id=oid, type=otype,
                                   label=str(plain(got.get("display")) or otype)[:200],
                                   open_url=object_url(otype, oid)))
    return out


def choices_page(backend: Any, reader: Any, object_type: str, ref: str,
                 object_ids: Sequence[str]) -> m.ObjectPage:
    """The record page of a reference several records hold: its choices (``choices``);
    ``RecordNotFound`` when this reader may open none of them."""
    from .errors import RecordNotFound
    try:
        found = choices(backend, reader, object_ids)
    except PageDoorUnavailable:
        raise TimeoutError("workbench_busy") from None   # as a read worker's: try again
    if not found:
        raise RecordNotFound()
    what, n = found[0].type, len(found)
    text = (f"{n} records have the number {ref}: numbers repeat across systems and "
            "counterparties. Pick the one you mean." if n > 1 else "")
    return m.ObjectPage(object_id=ref, type=what, label=ref, summary=text,
                        status=m.ObjectStatus(state="choose", text=text), choices=found,
                        freshness=m.Freshness(),
                        complete=True)
