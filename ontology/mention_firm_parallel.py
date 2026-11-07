"""The ``unique_mentions`` step with the host's cores: its judging by worker processes.

``ontology.mention_firm.firm_links`` reads every object holding a mention lead (14M leads
on the live store) one at a time on one core. Here the build's open transaction is
committed (as ``ontology.finish_parallel`` does), the objects are cut into chunks in the
serial order, and ``PLX_BUILD_WORKERS`` processes, each with one read-only connection to
the build's file, judge a chunk with the serial function. The build's process collects
the chunks in order, so the rows and counts are the serial step's, row for row
(``ontology/tests/test_mention_firm.py`` compares the two). A build whose file cannot be
committed, too few objects, one worker, or a failing pool: the serial step.

Stdlib only.
"""

from __future__ import annotations

from collections import Counter
from concurrent import futures
import multiprocessing
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Sequence

from ontology import mention_firm as MF

CHUNK = 2000
MIN_OBJECTS = 4 * CHUNK
LAST: dict[str, Any] = {}
_WORKER: dict[str, Any] = {}


def _chunks(objects: Iterator[str], size: int = CHUNK) -> Iterator[list[str]]:
    chunk: list[str] = []
    for oid in objects:
        chunk.append(oid)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _init(store: str) -> None:
    conn = sqlite3.connect(Path(store).as_uri() + "?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only = 1")
    _WORKER["conn"] = conn


def _judge(chunk: Sequence[str], types: Sequence[str]
           ) -> tuple[list[dict[str, Any]], Counter, list]:
    counts: Counter[str] = Counter()
    pending: list = []
    rows = list(MF.firm_links(_WORKER["conn"], types, counts, objects=chunk,
                              pending=pending))
    return rows, counts, pending


def parallel_firm_links(builder: Any, types: Sequence[str], counts: Counter,
                        processes: int | None = None, own: Sequence[str] = ()
                        ) -> list[dict[str, Any]] | None:
    """The serial rows, judged by worker processes (the reused purchase order numbers
    they leave pending resolved here, once, ``MF.resolved_rows``); None when the step
    should run serially (module docstring)."""
    from ontology.finish_parallel import _committed, workers
    from ontology.store import main_not_imported

    conn = builder.connection
    count = workers(processes)
    temp = getattr(builder, "temp", None)
    if count < 2 or temp is None:
        LAST.update(mode="serial", reason="no_workers" if count < 2 else "no_build_file")
        return None
    objects = list(MF._from_objects(conn))
    if len(objects) < MIN_OBJECTS:
        LAST.update(mode="serial", reason="few_objects", objects=len(objects))
        return None
    if not _committed(conn):
        LAST.update(mode="serial", reason="uncommitted")
        return None
    rows: list[dict[str, Any]] = []
    pending: list = []
    try:
        with main_not_imported(), futures.ProcessPoolExecutor(
                max_workers=count, mp_context=multiprocessing.get_context("spawn"),
                initializer=_init, initargs=(str(Path(temp).resolve()),)) as pool:
            for found, tally, left in pool.map(_judge, _chunks(iter(objects)),
                                         [tuple(types)] * (len(objects) // CHUNK + 1)):
                rows += found
                counts.update(tally)
                pending += left
    except Exception as exc:  # noqa: BLE001 - judged serially instead
        LAST.update(mode="serial", reason=f"workers_failed:{type(exc).__name__}")
        counts.clear()
        return None
    rows += MF.resolved_rows(conn, pending, counts, own)
    LAST.update(mode="parallel", workers=count, objects=len(objects))
    return rows


def unique_mentions_step(builder: Any, projector: Any) -> dict[str, Any]:
    """The ``unique_mentions`` step (``ontology.mention_firm``), judged in parallel."""
    conn = builder.connection
    types = list((getattr(projector, "types", None) or {}).keys())
    counts: Counter[str] = Counter()
    from ontology.sender_parties import own_domains

    rows = parallel_firm_links(builder, types, counts, own=own_domains(projector))
    if rows is None:
        return MF.unique_mentions_step(builder, projector)
    conn.execute("DELETE FROM link_rows WHERE link_id >= ? AND link_id < ?",
                 (MF.PREFIX, MF._end(MF.PREFIX)))
    for start in range(0, len(rows), MF.BATCH):
        builder.add("links", rows[start:start + MF.BATCH])
    counts["links"] = len(rows)
    return {"counts": dict(counts), "link_tiers": {MF.FIRM: len(rows)} if rows else {}}


__all__ = ["LAST", "parallel_firm_links", "unique_mentions_step"]
