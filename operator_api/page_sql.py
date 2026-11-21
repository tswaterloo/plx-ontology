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
import threading
import time
from contextlib import nullcontext
from urllib.parse import quote
from datetime import date, datetime
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


PAGE_KINDS = ("customer", "chain", "supplier", "purchase_order", "sales_order")


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
    from .page_core import CORE_PRIORITY
    worker = getattr(backend, "access_worker", None) or backend.reads
    args = {"reads": [dict(r) for r in reads], "ids": ids}
    try:
        out = worker.run(reader, lambda view: view._call(TOOL, args),
                         timeout=_timeout(worker, reader), priority=CORE_PRIORITY)
    except TimeoutError:
        raise PageDoorUnavailable("timeout") from None
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
              "Customer": "customer", "Supplier": "supplier"}
_VIEW_OF = {"purchase_order": "purchase_orders", "sales_order": "sales_orders",
            "customer": "customers", "supplier": "suppliers"}
_KIND_OF_VIEW = {v: k for k, v in _VIEW_OF.items()}


def resolve(backend: Any, reader: Any, kind: str, object_id: str) -> str:
    """The door's row ``kind``'s record ``object_id`` was read into (its handle, for the
    page's own read through the door); ``NotFound`` without one."""
    view = _VIEW_OF.get(kind)
    if view is None or not object_id or len(object_id) > 700:
        raise NotFound(kind)
    worker = getattr(backend, "access_worker", None) or backend.reads
    from .page_core import CORE_PRIORITY
    try:
        out = worker.run(reader, lambda v: v._call(TOOL, {"resolve": {
            "view": view, "object_id": object_id}}), timeout=_timeout(worker, reader),
            priority=CORE_PRIORITY)
    except TimeoutError:
        raise PageDoorUnavailable("timeout") from None
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
    reason = (f"Requested by {due}, still open" if due else "Past its requested-by date")
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
_RUNS_LOCK = threading.Lock()


def runs_fresh(backend: Any) -> Any:
    """Which alert rules are current now (``alerts.STALE_MINUTES``), as one value for a kept
    screen's inputs: when the rules stop running it changes, so a Home, briefing or sidebar
    kept from before is read again (and says unavailable) instead of being served as current.
    Read from the alert runs on the backend's Postgres pool, held ``RUNS_SECONDS``; a pool
    or table that cannot be read gives one constant ("unread"): the screens then stand on
    the read's own stale check (``alerts_stale``) as before."""
    now = time.monotonic()
    with _RUNS_LOCK:
        held = backend.__dict__.get("_runs_fresh")
        if held is not None and now - held[0] < RUNS_SECONDS:
            return held[1]
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
    with _RUNS_LOCK:
        backend.__dict__["_runs_fresh"] = (now, got)
    return got


def alerts_stale(label: str, since: Optional[datetime]) -> Any:
    from .errors import UserFacing
    when = _when(since)
    return UserFacing("alerts_stale", f"{label} are unavailable" + (
        f" since {when}" if when else "") + ": their check has not run on the current data. "
        "They show again after its next run.", 404)


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
                          dict[str, Optional[datetime]]]:
    """Home's late POs (``alert_late_pos``) and its at-risk list (the open alerts of the
    home types other than late POs routed to the reader) in ONE
    read through the door (one transaction) after the reader's scope: two door reads
    queued one after the other on the access workers cost Home 0.3-0.8 s each warm (11-18
    walk). (late total, the first ``limit`` late, as of, at risk, the stale home types).
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
    stale = {t: runs.get(t, (False, None))[1] for t in types
             if not runs.get(t, (False, None))[0]}
    return total, late, _iso(ok), risk, stale


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
    reads: list[dict[str, Any]] = [{"name": "alert_runs"}]
    every = list(dict.fromkeys([*types, *counted]))
    if every:
        reads.append({"name": "alerts_count", "params": {"rules": ",".join(every)}})
    if types:
        reads.append({"name": "alerts_open", "params": {"rules": ",".join(types),
                                                        "limit": limit, "offset": offset}})
    rows, ids = read(backend, reader, reads, ids=bool(types))
    counts = {str(r["rule"]): int(r["open"]) for r in rows.get("alerts_count", ())}
    return counts, rows.get("alerts_open", []), ids, runs_state(rows["alert_runs"])


def alert_item(row: Mapping[str, Any], ids: Mapping[str, Mapping[str, str]], rank: int,
               last_visit: Optional[str]) -> m.BriefingItem:
    """One open alert as a briefing item (Watching lists and counts these)."""
    from operating import alert_kinds
    oid, otype, url, sources = _alert_ref(row, ids)
    rule = str(row.get("rule"))
    seen = _iso(row.get("first_seen"))
    new = bool(seen and (last_visit is None or seen > last_visit))
    return m.BriefingItem(
        rank=rank, id=f"alert:{rule}:{row.get('handle')}:{row.get('topic') or ''}",
        event_type=rule, label=alert_label(row), evidence="live", object_id=oid,
        object_type=otype, subject_label=record_words(str(row.get("handle") or "")),
        summary=str(row.get("why") or ""),
        next_action=m.NextAction(text="Open the record and check it.", rule=rule),
        new_since_visit=new, new_this_generation=new, alert_kind=alert_kinds.kind_of(rule),
        default_on=alert_kinds.default_on(rule), home=alert_kinds.home(rule),
        first_seen=seen, open_url=url, sources=sources)


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
    try:
        counts, rows, ids, runs = alerts_read(backend, reader, [t for t in mine if _paged(t)],
                                              limit=top, counted=others)
    except PageDoorUnavailable:
        return m.Briefing(status="unavailable", count=0, new_since_visit=0,
                          since_visit=since_visit, other_teams=0, rank_basis="",
                          routing_basis="", complete=False, digest_preview="")
    items = [alert_item(r, ids, i + 1, scope.visit) for i, r in enumerate(rows)]
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
        home_count=sum(n for t, n in mine_counts.items() if alert_kinds.home(t)),
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
                "product": "Product", "batch": "Batch"}
_SYSTEMS = {"bc": "BC", "fm": "FileMaker", "filemaker": "FileMaker", "mc": "MC",
            "api": "Portal", "portal": "Portal", "sage": "Sage", "vmc": "VMC"}


def account_url(kind: str, handle: Optional[str] = None, chain: Optional[str] = None) -> str:
    if kind == "chain":
        return f"/account?kind=chain&chain={quote(chain or '', safe='')}"
    page = "order" if kind in ("purchase_order", "sales_order") else "account"
    return f"/{page}?kind={kind}&h={quote(handle or '', safe='')}"


def codes_text(codes: Any) -> Optional[str]:
    """'sage:ACME, portal:ACME' as 'Portal ACME, Sage ACME' (each system's own code)."""
    if not isinstance(codes, str) or not codes.strip():
        return None
    out = []
    for part in codes.split(","):
        system, _, code = part.strip().partition(":")
        if code:
            out.append(f"{_SYSTEMS.get(system.lower(), system.title())} {code}")
    return ", ".join(sorted(dict.fromkeys(out))) or None


def search(backend: Any, reader: Any, q: str, limit: int) -> list[m.Suggestion]:
    """Every kind of record by name or number, one result per real thing, ranked (the
    ``find`` read); a chain's head account is the chain's result, never a second one."""
    text = " ".join(q.split())[:100]
    if len(text) < 2:
        return []
    rows, ids = read(backend, reader, [{"name": "find", "params": {
        "q": text, "limit": min(50, limit + 10)}}], ids=True)
    hits = rows["find"]
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
        if kind in ("product", "batch"):
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
                sub.append(f"PO {v}" if kind == "sales_order" and key == "code" else v)
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
             views: Sequence[str]) -> str:
    systems = sorted({(codes_text(f"{p}") or "").split(" ")[0] for a in accounts
                      for p in str(a.get("codes") or "").split(",") if ":" in p} - {""})
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
    rows, _ = read(backend, reader, reads)
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
        codes = codes_text(head.get("codes"))
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
            _t(i, "invoice_number"), _d(i, "invoice_date"), _d(i, "due_date"),
            _t(i, "order_number"), _m(i, "total"), _m(i, "remaining")] for i in inv],
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
    rows, _ = read(backend, reader, [
        {"name": "supplier_account", "params": key},
        {"name": "supplier_open_pos", "params": {**key, "limit": LIST_ROWS}},
        {"name": "supplier_open_totals", "params": key},
        {"name": "supplier_owed", "params": key},
        {"name": "supplier_invoices", "params": {**key, "limit": 10}},
        {"name": "supplier_buys_years", "params": {**key, "as_of": day}},
        {"name": "supplier_same_name", "params": key},
        {"name": "views_as_of"}])
    if not rows["supplier_account"]:
        raise NotFound("supplier")
    head = rows["supplier_account"][0]
    facts = []
    codes = codes_text(head.get("codes"))
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
        "PO", "Ordered", "Requested by", "Promised", "Days late", "Total"], rows=[[
            _t(p, "po_number", account_url("purchase_order", p.get("handle"))),
            _d(p, "order_date"), _d(p, "requested_date"), _d(p, "promised_date"),
            _n(p, "days_late") if p.get("verdict") == "late" else m.BizCell(),
            _m(p, "total")] for p in listed], total=n,
        note=_orders_note(n, late, totals, unvalued, len(listed)))]
    sections.append(_owed(rows["supplier_owed"], "What we owe them"))
    inv = rows["supplier_invoices"]
    sections.append(m.BizTable(key="invoices", title="Last invoices", columns=[
        "Document", "Type", "Date", "Due", "PO", "Amount", "Still owed"], rows=[[
            _t(i, "document_number"), _t(i, "document_type"), _d(i, "document_date"),
            _d(i, "due_date"), _t(i, "po_number"), _m(i, "original_amount"),
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


def order(backend: Any, reader: Any, kind: str, handle: str) -> m.BizPage:
    """A purchase order or a customer order: its header, then its lines."""
    started = time.perf_counter()
    if kind not in ("purchase_order", "sales_order") or not handle:
        raise NotFound(kind)
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
                ("Requested by", "requested_date", _d), ("Promised", "promised_date", _d),
                ("Purchaser", "purchaser", _t))
        money = "total"
        cols = ["Item", "Description", "Ordered", "Received", "Invoiced", "Unit",
                "Unit price", "Line total", "Expected"]
        body = [[_t(r, "item_code"), _t(r, "description"), _n(r, "ordered_quantity"),
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
        body = [[_t(r, "item_code"), _t(r, "item_name") if r.get("item_name") is not None
                 else _t(r, "description"), _n(r, "ordered_quantity"), _n(r, "shipped_quantity"),
                 _n(r, "outstanding_quantity"), _t(r, "uom"), _m(r, "unit_price"),
                 _m(r, "line_amount")] for r in lines]
        if h.get("status") == "open" and (h.get("days_late") or 0) > 0:
            facts.append(m.BizFact(label="Days late", value=_n(h, "days_late")))
    for label, col, cell in spec:
        if h.get(col) is not None or _hidden(h, col):
            facts.append(m.BizFact(label=label, value=cell(h, col)))
    note = None if lines else "No lines on record."
    if money == "total" and h.get("total_basis") == "lines":
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
        sources=_sources([], built, (table, f"{table[:-1]}_lines")),
        as_of=None, took_ms=int((time.perf_counter() - started) * 1000))


MAX_LINES = 500


def _numbered(word: str, number: Any) -> str:
    """'PO 106093'; a number that already says it ('PO-1') is said once."""
    text = str(number or "").strip()
    return text if text.upper().startswith(word) else f"{word} {text}".strip()

__all__ = ["SWITCH", "NotFound", "PageDoorUnavailable", "account", "account_url", "codes_text",
           "late_po", "late_pos", "on", "order", "read", "search"]
