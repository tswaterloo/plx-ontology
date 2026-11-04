"""The ``sender_corroboration`` finishing step with the host's cores.

Measured on the live gen 8 build: about 1,000 s on one core. Per mail document of a
corroborable type the step reads the message's links (its carrier references), and per
lead link the message's sender, its dates, the target's order date and the target's
party's domains (``ontology.sender_corroboration``), each a few queries of the build's
file, while the host's other cores stood idle. Here ``PLX_BUILD_WORKERS`` processes make
those reads, document by document and lead by lead, over read-only connections to the
build's file; the build's process lists the documents and the leads, screens each lead
and writes every row, in the serial order (``ontology/tests/test_sender_parallel.py``
compares the two: every store table and the step's report).

How (:func:`sender_corroboration_step`): the rows the step replaces are deleted and the
build's open transaction is committed (as ``ontology.finish_parallel`` does), so a
worker's connection reads what the build's process reads; the carrier leads of a type are
added and committed before its firm links are judged. A message's sender is read once,
over the window of its first lead (``sender_corroboration._firm_links``), so each worker is
handed that window with each lead. A build whose rows cannot be committed, a pool that
cannot start or a worker that fails: the step's remaining work runs here, as the serial
step's. Fewer than ``min_items`` documents or leads of a type: read here (the workers
cost more than they save).

Stdlib only.
"""

from __future__ import annotations

import concurrent.futures as futures
import multiprocessing
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology import mentions
from ontology import sender_corroboration as sc

MIN_ITEMS = 512          # documents or leads below which they are read here
TASK_ITEMS = 128         # documents or leads a worker reads in one task
WORKER_CACHE_KIB = 65536
# Measurement of the last run in this process (tests and the build log read it).
LAST: dict[str, Any] = {}


def sender_corroboration_step(builder: Any, projector: Any, *, processes: int | None = None,
                              min_items: int = MIN_ITEMS) -> dict[str, Any]:
    """``sender_corroboration.sender_corroboration_step`` with its reads made by
    ``processes`` worker processes (module docstring); the same rows, the same report."""
    from ontology.finish_parallel import _committed, workers

    started = time.monotonic()
    LAST.clear()
    count = workers(processes)
    path = getattr(builder, "temp", None)
    if count < 2 or path is None or not Path(path).is_file():
        LAST.update(mode="serial", reason="no_workers" if count < 2 else "no_build_file")
        report = sc.sender_corroboration_step(builder, projector)
        LAST["seconds"] = round(time.monotonic() - started, 3)
        return report
    conn = builder.connection
    schema_links = getattr(projector, "links", {}) or {}
    for prefix in (sc.FIRM_PREFIX, sc.CARRIER_PREFIX):
        conn.execute("DELETE FROM links WHERE substr(link_id, 1, ?) = ?", (len(prefix), prefix))
    facts = sc._Facts(conn)
    declared = mentions.mention_link_types(schema_links)
    parties: sc._Parties | None = None
    own = sc._own_domains(conn)
    report: dict[str, Any] = {"counts": {}, "link_tiers": {}}
    pool = _Pool(str(Path(path).resolve()), count, min_items)
    LAST.update(mode="parallel", workers=count)
    try:
        for doc_type, link_type in sorted(sc.corroborable_types(schema_links).items()):
            target_type = (schema_links.get(link_type) or {}).get("to")
            email_link = declared.get(("Email", target_type))
            each = None
            if email_link is not None and pool.usable(_committed(conn)):
                def each(docs: list[dict[str, Any]], args=(link_type, target_type, email_link)
                         ) -> Iterable[list[dict[str, Any]]]:
                    return pool.run("carrier", docs, args, lambda: (
                        sc._carrier_rows(facts, doc, *args) for doc in docs))
            carrier = sc._carrier_links(facts, doc_type, link_type, target_type, email_link,
                                        each=each)
            if carrier:
                builder.add("links", carrier)
            judge = None
            if pool.usable(_committed(conn)):
                def judge(leads: list[dict[str, Any]], screened: list[tuple[str, Any]],
                          windows: Mapping[str, tuple[str, str | None]]
                          ) -> Iterable[tuple[Any, ...]]:
                    items = [(lead, kind, value, windows.get(value) if kind == "sender"
                              else None) for lead, (kind, value) in zip(leads, screened)]
                    return pool.run("firm", items, (sorted(own),), lambda: here(
                        leads, screened, windows))
            if parties is None and judge is None:
                parties = sc._Parties(facts)

            def here(leads: Any, screened: Any, windows: Any) -> Iterable[tuple[Any, ...]]:
                nonlocal parties
                if parties is None:
                    parties = sc._Parties(facts)
                return sc._verdicts(facts, parties, leads, screened, windows, own)
            firm, counts = sc._firm_links(facts, parties, link_type, own, carrier,
                                          judge=judge)
            if firm:
                builder.add("links", firm)
            report["counts"][link_type] = {"carrier_leads": len(carrier), **counts}
            tiers = report["link_tiers"]
            tiers[mentions.TIER] = tiers.get(mentions.TIER, 0) + len(carrier)
            tiers[sc.FIRM_TIER] = tiers.get(sc.FIRM_TIER, 0) + len(firm)
    finally:
        pool.close()
        LAST.update(pool.stats, reason=pool.reason,
                    seconds=round(time.monotonic() - started, 3))
        if not pool.stats["tasks"]:
            LAST["mode"] = "serial"
    return report


class _Pool:
    """The step's worker processes, started on the first task; ``run`` gives each item's
    result in order, or ``here()``'s (the build's process) when the items are few, the
    rows could not be committed or a worker failed."""

    def __init__(self, store: str, count: int, min_items: int) -> None:
        self.store, self.count, self.min_items = store, count, min_items
        self._pool: Any = None
        self._broken = False
        self.reason: str | None = None
        self.stats = {"tasks": 0, "items": 0, "items_here": 0, "failed_tasks": 0}

    def usable(self, committed: bool) -> bool:
        if not committed and self.reason is None:
            self.reason = "uncommitted"
        return committed and not self._broken

    def run(self, kind: str, items: Sequence[Any], args: tuple[Any, ...],
            here: Callable[[], Iterable[Any]]) -> Iterable[Any]:
        if len(items) < self.min_items or not self._started():
            self.stats["items_here"] += len(items)
            return here()
        from ontology.store import main_not_imported

        try:
            with main_not_imported():   # a worker starts on a submit
                tasks = [self._pool.submit(_task, kind, list(items[at:at + TASK_ITEMS]),
                                           args)
                         for at in range(0, len(items), TASK_ITEMS)]
            out = [result for task in tasks for result in task.result()]
        except Exception as exc:  # noqa: BLE001 - read here instead, as serially
            self._broken = True
            self.reason = f"workers_failed:{type(exc).__name__}"
            self.stats["failed_tasks"] += 1
            self.stats["items_here"] += len(items)
            return here()
        self.stats["tasks"] += len(tasks)
        self.stats["items"] += len(items)
        return out

    def _started(self) -> bool:
        from ontology.store import main_not_imported

        if self._pool is None and not self._broken:
            try:
                with main_not_imported():
                    self._pool = futures.ProcessPoolExecutor(
                        max_workers=self.count,
                        mp_context=multiprocessing.get_context("spawn"),
                        initializer=_init, initargs=(self.store,))
            except Exception as exc:  # noqa: BLE001 - read here
                self._broken = True
                self.reason = f"workers_failed:{type(exc).__name__}"
        return self._pool is not None and not self._broken

    def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)


# --------------------------------------------------------------------------- the workers

_W: dict[str, Any] = {}


def _init(store: str) -> None:
    conn = sqlite3.connect(Path(store).as_uri() + "?mode=ro", uri=True, timeout=60)
    conn.execute(f"PRAGMA cache_size = -{WORKER_CACHE_KIB}")
    _W.clear()
    _W.update(facts=sc._Facts(conn), parties=None, senders={})


def _task(kind: str, items: list[Any], args: tuple[Any, ...]) -> list[Any]:
    facts = _W["facts"]
    if kind == "carrier":
        return [sc._carrier_rows(facts, doc, *args) for doc in items]
    own = set(args[0])
    out = []
    for lead, screen, value, window in items:
        if screen == "count":
            out.append((screen, value))
            continue
        if _W["parties"] is None:   # read once by each worker (only reads)
            _W["parties"] = sc._Parties(facts)
        senders = _W["senders"]
        key = (value, window)
        if key not in senders:
            found = sc._sender(facts, value, window)
            senders[key] = None if found is None else (found[0], dict(found[1]))
        verdict = sc._verdict(facts, _W["parties"], lead, senders[key], own)
        if verdict[0] == "passed":   # rows as plain values, to be sent back
            group, ((feature, handles, marks), sender_row) = verdict[1], verdict[2]
            verdict = ("passed", group, ((feature, handles, marks), dict(sender_row)))
        out.append(verdict)
    return out


__all__ = ["LAST", "MIN_ITEMS", "TASK_ITEMS", "sender_corroboration_step"]
