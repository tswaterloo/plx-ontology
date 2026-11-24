"""A refresh's business views built side by side (``business_views.refresh``): every view
whose inputs moved is rebuilt whole, and one after the other they took 29 min a run on
11-18 (sales_invoice_lines 426 s, sales_orders 271 s, sales_order_lines 207 s, customer and
supplier balances 203 s and 128 s, ...), so the 30-minute timer ran back to back all day.

The stale business views are split over ``PLX_BIZ_VIEW_WORKERS`` processes (default 4),
longest first by each view's last build time (``biz.state.seconds``; a view never timed
goes first), each process building its views in the refresh's own order with its own reader
(``business_views.build``: a view failing keeps its old table and never stops the views
after it). Views that share a reader memo in one process still share it; across processes
a memo is read once per process. The communications tables (``View.fill``) are built after
every business view, in this process, as before (their rows name the business rows). The
access and column classes are allocated under a table lock (``access_ids``), so builds side
by side never collide. One worker, or one view: built here, as before.
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Any, Mapping, Sequence

VIEW_WORKERS_DEFAULT = 4


def view_workers() -> int:
    """``PLX_BIZ_VIEW_WORKERS``, else ``VIEW_WORKERS_DEFAULT``. Not a positive whole number:
    refused, never read as one."""
    raw = os.environ.get("PLX_BIZ_VIEW_WORKERS", "").strip()
    if not raw:
        return VIEW_WORKERS_DEFAULT
    try:
        n = int(raw)
    except ValueError:
        n = 0
    if n < 1:
        raise RuntimeError(f"PLX_BIZ_VIEW_WORKERS must be a positive whole number, not {raw!r}")
    return n


def plan(names: Sequence[str], seconds: Mapping[str, Any], workers: int) -> list[list[str]]:
    """``names`` in at most ``workers`` groups of about equal time: longest first (a view
    with no known time counts as the longest) to the least loaded group; each group keeps
    the order ``names`` gave. Empty groups are left out."""
    def took(name: str) -> float:
        value = seconds.get(name)
        return float(value) if isinstance(value, (int, float)) and value >= 0 else float("inf")
    groups: list[list[str]] = [[] for _ in range(max(1, min(workers, len(names))))]
    load = [0.0] * len(groups)
    for name in sorted(names, key=lambda n: (-took(n), names.index(n))):
        i = min(range(len(groups)), key=lambda k: (load[k], k))
        groups[i].append(name)
        load[i] += took(name) if took(name) != float("inf") else 1e9
    order = {n: i for i, n in enumerate(names)}
    return [sorted(g, key=order.__getitem__) for g in groups if g]


def _group(dsn: str, names: list[str], schema: Any, as_of: dt.date | None,
           digest: str | None) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """One process's views, each on its own (``_each``)."""
    from operating.pg import business_views as bv
    bv._stop_on_term()
    return _each(dsn, names, schema, as_of, digest)


def _each(dsn: str, names: Sequence[str], schema: Any, as_of: dt.date | None,
          digest: str | None) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """``business_views.build`` of each view of ``names`` on its own, in order: what was
    built, and each failed view's ``"<Type>: <message>"`` (its old table kept). One view's
    failure never drops the views after it. (Each call reads its own memos: views that
    shared a reader in one call now each read theirs; the price of knowing each failure.)
    ``build``'s message for one view is ``BUILD_FAILED`` + ``"<view>: <Type>: <message>"``;
    any other error is the view's as it is."""
    from operating.pg import business_views as bv
    built: list[dict[str, Any]] = []
    failed: dict[str, str] = {}
    for n in names:
        try:
            built += bv.build(dsn, [n], schema=schema, as_of=as_of, digest=digest)
        except Exception as exc:  # noqa: BLE001 - reported per view to the caller
            text = f"{type(exc).__name__}: {str(exc)[:800]}"
            own = f"RuntimeError: {BUILD_FAILED}{n}: "
            failed[n] = text[len(own):] if text.startswith(own) else text
    return built, failed


BUILD_FAILED = "views not built (old tables kept): "   # business_views.build's message


def build(dsn: str, names: Sequence[str], *, schema: Any = None,
          as_of: dt.date | None = None, digest: str | None = None,
          seconds: Mapping[str, Any] | None = None,
          workers: int | None = None) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """``business_views.build`` of ``names``, the business views side by side (module
    doc), then the communications tables here, each view on its own: (what was built, each
    failed view's ``"<Type>: <message>"``). Never raises for a view's failure; a worker
    process that died fails each of its views with that reason."""
    from operating.pg import business_views as bv
    workers = view_workers() if workers is None else workers
    plain = [n for n in names if getattr(bv.VIEWS[n], "fill", None) is None]
    comms = [n for n in names if n not in plain]
    if workers < 2 or len(plain) < 2:
        return _each(dsn, list(names), schema, as_of, digest)
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    if bv.SCHEMA != bv.SERVED_SCHEMA:
        # a scratch schema is made here once, before the processes (each also ensures it,
        # one at a time): made in every process at once, all but one failed on its name
        import psycopg
        with psycopg.connect(dsn, autocommit=True) as write:
            bv.ensure_scratch_schema(write)
    groups = plan(plain, seconds or {}, workers)
    out: list[dict[str, Any]] = []
    failed: dict[str, str] = {}
    # spawn: a child never inherits this process's open connections (the refresh lock's)
    from ontology import parent_watch
    # each worker ends with this process (a killed refresh leaves no build running on)
    with ProcessPoolExecutor(len(groups), mp_context=multiprocessing.get_context("spawn"),
                             **parent_watch.pool_init()) as pool:
        futures = [pool.submit(_group, dsn, g, schema, as_of, digest) for g in groups]
        for group, future in zip(groups, futures):
            try:
                got, errors = future.result()
            except Exception as exc:  # noqa: BLE001 - a process that died: its views failed
                reason = f"{type(exc).__name__}: {str(exc)[:300]}"
                got, errors = [], {n: reason for n in group}
            out.extend(got)
            failed.update(errors)
    if comms:
        got, errors = _each(dsn, comms, schema, as_of, digest)
        out.extend(got)
        failed.update(errors)
    return out, failed
