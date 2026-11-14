"""The content digest of the segments an incremental build wrote, on the build's cores.

A seeded build (an incremental generation) digests again only the segments it wrote
(``ontology.store.content_digest_parts`` with the seed's parts and the dirty marks), and
did so on one core: a build that writes all mail (a reader change, a mail re-projection)
and every link a ``post_project`` step writes again (every build) digests most of the
store's rows that way. :class:`ParallelBuilder` is the store builder of a generation build
(``ontology.project_incremental``): the same builder, whose digest of written segments is
made by worker processes (:func:`parallel_segment_parts`) when it digests in parallel
(``digest_workers`` above one, a file of ``DIGEST_PARALLEL_MIN_BYTES`` or more).

The same parts, byte for byte: each job is one read a worker makes through its own
read-only connection of the committed rows, returning per segment the lines the serial
digest feeds that segment from the rows it read, in order (``ontology.store``'s own
``_segment_texts``); the build's process feeds them to each segment's SHA-256 job by job,
in the order the jobs were listed, and the jobs of one (table, segment) are listed in the
table's order:

* a table ordered by object id first: the segment's object id range cut into ranges of
  ``_CHUNK_ROWS`` objects (``_CHUNK_VALUE_OBJECTS`` for the values);
* the links and the events (segmented by type): the table cut into key ranges
  (``ontology.store._chunk_bounds``), each read for the marked types only;
* any other (identifiers and event objects, ordered by another key; ``store_meta``; the
  ids without a binding prefix): the segment read whole by one worker.

``ontology.store`` is not changed: its bytes are part of the derivation code
(``ontology.project.derivation_code``), which every derived relation's fingerprint holds,
so a change there would have the next build read and derive every mail and file again.
This module is imported by the generation build only, never by a module a derived record
or a unit's rows are computed by.

Stdlib only.
"""

from __future__ import annotations

import concurrent.futures as futures
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping

from ontology import store as S
from ontology import parent_watch

_OBJECT_ORDERED = ("objects", "object_removals", "object_versions", "object_values")


def _segment_bounds(conn: sqlite3.Connection, low: str, high: str, every: int) -> list[Any]:
    """[low, b1, ..., high]: object ids cutting [``low``, ``high``) into ranges of
    ``every`` objects."""
    found = conn.execute(
        "SELECT object_id FROM (SELECT object_id, row_number() OVER (ORDER BY object_id) AS n "
        "FROM objects WHERE object_id >= ? AND object_id < ?) WHERE n % ? = 1 AND n > 1 "
        "ORDER BY object_id", (low, high, every)).fetchall()
    return [low, *(row[0] for row in found), high]


def segment_jobs(conn: sqlite3.Connection, marks: set[tuple[str, str]]
                 ) -> list[tuple[str, str, str, tuple[Any, ...]]]:
    """(table, segment, kind, arguments) of every read of the marked segments, each
    table's in its order (module docstring)."""
    jobs: list[tuple[str, str, str, tuple[Any, ...]]] = []
    for table, order in S._TABLE_ORDER:
        by = S._SEGMENT_BY[table]
        segments = [""] if by is None else sorted(s for kind, s in marks if kind == by)
        if not segments:
            continue
        if by in ("link_type", "event_type"):
            bounds = S._chunk_bounds(conn, table)
            for low, high in zip(bounds, bounds[1:]):
                jobs.append((table, "", "typed", (order, low, high, tuple(segments))))
            continue
        for segment in segments:
            if by is None or not segment or table not in _OBJECT_ORDERED:
                jobs.append((table, segment, "whole", (order, by is None)))
                continue
            every = S._CHUNK_VALUE_OBJECTS if table == "object_values" else S._CHUNK_ROWS
            bounds = _segment_bounds(conn, segment, segment[:-1] + ";", every)
            for low, high in zip(bounds, bounds[1:]):
                jobs.append((table, segment, "range", (order, low, high)))
    return jobs


_READERS: dict[str, sqlite3.Connection] = {}


def segment_job(path: str, table: str, segment: str, kind: str, args: tuple[Any, ...]
                ) -> tuple[bytes, dict[str, bytes]]:
    """A worker's part of the digest of marked segments: (header, segment -> its lines in
    order) of the rows the job reads (``segment_jobs``)."""
    conn = _READERS.get(path)
    if conn is None:
        conn = _READERS[path] = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("PRAGMA cache_size = -262144")
    if kind == "range":
        order, low, high = args
        selected = S._segment_texts(conn, table, order, (low, high), "", (), None)
    elif kind == "typed":
        order, low, high, types = args
        where, params = S._chunk_where(table, low, high)
        where = (where + " AND " if where else " WHERE ") + \
            f"{S._SEGMENT_BY[table]} IN (SELECT value FROM json_each(?))"
        selected = S._segment_texts(conn, table, order, None, where,
                                    [*params, json.dumps(list(types))], None)
    else:
        order, whole = args
        if whole:   # a table of one segment (``store_meta``)
            selected = S._segment_texts(conn, table, order, None, "", (), None)
        elif segment:
            selected = S._segment_texts(conn, table, order, (segment, segment[:-1] + ";"),
                                        "", (), None)
        else:   # ids without a binding prefix: no range holds only them
            selected = S._segment_texts(conn, table, order, None, "", (), "")
    lines: dict[str, list[bytes]] = {}
    header = b""
    for header, found, line in selected:
        lines.setdefault(found, []).append(line)
    return header, {found: b"".join(items) for found, items in lines.items()}


def parallel_segment_parts(path: str | Path, conn: sqlite3.Connection,
                           previous: Mapping[str, str], dirty: Any, workers: int
                           ) -> dict[str, str]:
    """``ontology.store.content_digest_parts(conn, previous, dirty)``, the marked
    segments' rows read and written out as text by ``workers`` processes over the
    committed rows of the file at ``path`` (module docstring): the same parts."""
    marks = set(dirty)
    jobs = segment_jobs(conn, marks)
    parts = dict(previous)
    for table, _order in S._TABLE_ORDER:   # every marked segment is digested again
        by = S._SEGMENT_BY[table]
        for segment in ([""] if by is None else [s for kind, s in marks if kind == by]):
            parts.pop(S._part(table, segment), None)
    hashes: dict[tuple[str, str], Any] = {}
    window = max(2, workers * 3)
    with S.main_not_imported(), futures.ProcessPoolExecutor(
            max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
            **parent_watch.pool_init()) as pool:
        pending: list[Any] = []
        at = 0
        while at < len(jobs) or pending:
            while at < len(jobs) and len(pending) < window:
                table, segment, kind, args = jobs[at]
                pending.append((table, pool.submit(segment_job, str(path), table, segment,
                                                   kind, args)))
                at += 1
            table, future = pending.pop(0)
            header, lines = future.result()
            for segment, text in lines.items():
                digest = hashes.get((table, segment))
                if digest is None:
                    digest = hashes[(table, segment)] = hashlib.sha256(header)
                digest.update(text)
    parts.update({S._part(table, segment): digest.hexdigest()
                  for (table, segment), digest in hashes.items()})
    return parts


class ParallelBuilder(S.StoreBuilder):
    """``ontology.store.StoreBuilder`` whose digest of the segments a seeded build wrote
    is made by worker processes (:func:`parallel_segment_parts`) when it digests in
    parallel; everything else is the builder's own. :meth:`digest_and_swap` is the
    builder's, but for that one call. The commit's check and digest tell the builder's
    ``seconds`` when they start (a build's step timings, ``ontology.build_timings``)."""

    def _begin(self, name: str) -> None:
        begin = getattr(self.seconds, "begin", None)
        if callable(begin):
            begin(name)

    def _check_committed(self, workers: int) -> list[str]:
        self._begin("check")
        return super()._check_committed(workers)

    def digest_and_swap(self, dirty: list[tuple[str, str]] | None = None) -> str:
        conn = self.connection
        workers = self.digest_workers
        self._begin("digest")
        if dirty is None or self._seed_parts is None or S._ALL in dirty or workers < 2 \
                or self.temp.stat().st_size < S.DIGEST_PARALLEL_MIN_BYTES:
            return super().digest_and_swap(dirty)
        try:
            started = time.monotonic()
            if conn.in_transaction:
                conn.execute("COMMIT")
            parts = parallel_segment_parts(self.temp, conn, self._seed_parts, dirty, workers)
            conn.execute("BEGIN IMMEDIATE")
            self.digest_mode = "segments"
            self.digest_segments = len(dirty)
            digest = S.digest_of_parts(parts)
            self.set_meta("content_digest", digest)
            self.set_meta(S.DIGEST_PARTS, {"format": S.DIGEST_FORMAT, "parts": parts})
            conn.execute("DELETE FROM store_meta WHERE key = ?", (S.CHECKPOINT_META,))
            self.seconds["digest"] = round(time.monotonic() - started, 3)
            conn.execute("COMMIT")
            conn.close()
            self._conn = None
            with open(self.temp, "rb") as handle:
                os.fsync(handle.fileno())
            os.replace(self.temp, self.target)
            self.kept = False
            S._fsync_dir(self.target.parent)
            return digest
        except BaseException:
            self.abort()
            raise


__all__ = ["ParallelBuilder", "parallel_segment_parts", "segment_job", "segment_jobs"]
