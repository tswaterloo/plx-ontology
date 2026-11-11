"""The ``mentions`` finishing step with the host's cores: corroboration by worker processes.

Measured on the live gen 8 build: more than four hours of the ``mentions`` step on one
core in ``ontology.mentions.corroborate_links`` (each lead link's ``_agreeing`` reading
the party, the target and the conversation's other messages), while 15 cores stood idle.
Here that reading is done by ``PLX_BUILD_WORKERS`` processes; what the step writes and
reports is the serial step's, row for row (``ontology/tests/test_finish_parallel.py``
compares the two). ``ontology.mentions`` is not changed: its memo key and the derivation
code digest its bytes (``ontology.code_equivalence``), so this module calls its functions
and is imported by the build's steps only (``operating.build_steps``), never by a module
a derived record is computed by.

How it runs (:func:`store_mention_links`, the step):

1. The mention links are resolved as the serial step resolves them
   (``mentions._store_links``, the memo and the document workers as before), but the rows
   it adds go to a working file beside the build's (:class:`_Diverted`, in the batches
   and the order they were added), so the build's file holds nothing new.
2. The build's open transaction is committed (the rows of the steps before and the
   handle index; a build kept to resume commits them at each step anyway), so its file
   holds exactly what the serial step's ``corroborate_links`` would read, but this
   step's links. Each worker opens it read-only, attaches the working file and reads the
   links through a ``TEMP`` view named ``links`` (temp names are found first): the file's
   links and this step's. The workers decide, document by document in ``from_object``
   order (the serial order), which lead links rise and what they then state; nothing is
   written while they read.
3. The build's process adds the step's links to the generation exactly as the serial
   step did (the same batches, in order), then writes each raise in the serial order with
   the serial statement.

The one thing a serial raise reads that an earlier raise wrote is the markings of an
earlier document's link in the same conversation (``_petra_thread`` carries them): the
view tags each mention link's markings with its link id (:data:`_TAG`), and the build's
process adds to a raise the markings an earlier raise added to a link it read. Every other
column a raise reads is one an earlier raise leaves as it was (the first evidence handle,
the mention feature, the times, the vetoes).

A build whose file cannot be committed (a deferred foreign key not whole), a pool that
cannot start or a worker that fails: the links are added and corroborated serially
(``mentions.corroborate_links``), as before. Each worker holds one read-only connection
(``WORKER_CACHE_KIB`` of page cache) and one task of documents at a time.

Stdlib only.
"""

from __future__ import annotations

import concurrent.futures as futures
import contextlib
import json
import multiprocessing
import os
from pathlib import Path
import pickle
import secrets
import shutil
import sqlite3
import tempfile
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from ontology import mentions as M
from ontology.store_ddl import LINK_COLUMNS

# Below this many documents with a lead mention link the workers cost more than they save.
MIN_DOCUMENTS = 2000
TASK_DOCUMENTS = 64          # documents a worker decides in one task
WORKER_CACHE_KIB = 131072    # page cache of a worker's read-only connection (128 MiB)
# A mention link's markings as a worker reads them carry this mark, then its link id.
_TAG = "\x00plx-link:"
_SCRATCH_LINKS = "links"
_SCRATCH_THREADS = "threads"
# Measurement of the last run in this process (tests and the build log read it).
LAST: dict[str, Any] = {}


def workers(requested: int | None = None) -> int:
    """The processes the step may use (``ontology.project_parallel.workers``); 1 when that
    count is refused."""
    from ontology import project_parallel
    from ontology.project import ProjectionError

    try:
        return project_parallel.workers(requested)
    except ProjectionError:
        return 1


# --------------------------------------------------------------------------- the step

def store_mention_links(builder: Any, projector: Any, *,
                        documents: Iterable[M.Document] | None = None, aliases: Any = None,
                        processes: int | None = None, min_documents: int = MIN_DOCUMENTS,
                        min_groups: int | None = None) -> dict[str, Any]:
    """``ontology.mentions.store_mention_links``, its corroboration by ``processes`` worker
    processes (``workers``) when the build's file can be read by them and at least
    ``min_documents`` documents hold a lead mention link (module docstring); the same
    rows, the same report."""
    count = workers(processes)
    path = getattr(builder, "temp", None)
    if count < 2 or path is None or not Path(path).is_file():
        LAST.clear()
        LAST.update(mode="serial", reason="no_workers" if count < 2 else "no_build_file")
        return M.store_mention_links(builder, projector, documents=documents, aliases=aliases)
    conn = builder.connection
    links = getattr(projector, "links", {}) or {}
    declared = M.mention_link_types(links)
    groups = (M.source_document_groups(projector) if documents is None
              else ((None, lambda d=d: ([d], True)) for d in documents))
    conn.execute(f"CREATE INDEX IF NOT EXISTS {M.HANDLE_INDEX} ON object_versions(l1_handle)")
    unanswered: dict[str, list[dict[str, Any]]] = {}
    context = getattr(projector, "build_context", None)
    LAST.clear()
    started = time.monotonic()
    scratch = Path(path).with_name(f".{Path(path).name}.mentions-{secrets.token_hex(6)}")
    try:
        side = _Scratch(scratch)
        try:
            memo = context.document_memo() if context is not None else None
            changed = context.changed_key if context is not None else None
            reader = (_group_reader(builder, conn, scratch, declared, aliases, memo, changed,
                                    count, min_groups) if documents is None else None)
            try:
                with reader.patched() if reader is not None else _nothing():
                    report = M._store_links(
                        _Diverted(builder, side), conn, declared, groups, aliases,
                        unanswered, reader.memo_view if reader is not None and
                        memo is not None else memo, changed)
            finally:
                if reader is not None:
                    reader.close()
                    LAST.update({"links.mode": "parallel" if reader.stats["groups"]
                                 else "serial", "links.reason": reader.reason,
                                 **{f"links.{k}": v for k, v in reader.stats.items()}})
            LAST["seconds.links"] = round(time.monotonic() - started, 3)
            types = sorted(M.corroborable_link_types(links))
            decided = None
            if types and side.documents(types) >= min_documents:
                decided = _decide(builder, conn, side, types, count)
            else:
                LAST.update(mode="serial", reason="few_documents")
            at = time.monotonic()
            side.replay(builder, count)
            LAST["seconds.add"] = round(time.monotonic() - at, 3)
            at = time.monotonic()
            raised = (_apply(conn, decided) if decided is not None
                      else M.corroborate_links(conn, types))
            LAST["seconds.raise"] = round(time.monotonic() - at, 3)
        finally:
            side.remove()
        report["counts"].update(M.propose_reference_aliases(conn, unanswered, aliases))
    finally:
        conn.execute(f"DROP INDEX IF EXISTS {M.HANDLE_INDEX}")
    report["counts"]["corroborated"] = raised
    report["link_tiers"] = {M.TIER: report["counts"]["links"] - raised, M.CORROBORATED: raised}
    LAST["seconds"] = round(time.monotonic() - started, 3)
    return report


@contextlib.contextmanager
def _nothing() -> Iterator[None]:
    yield


def _group_reader(builder: Any, conn: sqlite3.Connection, scratch: Path,
                  declared: Mapping[tuple[str, str], str], aliases: Any, memo: Any,
                  changed: Any, count: int, min_groups: int | None) -> Any:
    """The step's documents read and their links found by ``count`` workers
    (``ontology.finish_documents``), once the build's rows are committed so the workers
    read them; None (the links found here, as before) when they cannot be."""
    from ontology.finish_documents import GroupReader

    if not _committed(conn):
        LAST["links.reason"] = "uncommitted"
        return None
    reader = GroupReader(store=str(Path(builder.temp).resolve()), conn=conn,
                         scratch=scratch, declared=declared, aliases=aliases, memo=memo,
                         changed=changed, count=count,
                         **({} if min_groups is None else {"min_groups": min_groups}))
    if not reader.start():
        LAST["links.reason"] = reader.reason
        return None
    return reader


def _committed(conn: sqlite3.Connection) -> bool:
    """Commit the build's open transaction and open the next; False when it cannot be
    committed (a deferred foreign key not whole: the rows stay open)."""
    try:
        if conn.in_transaction:
            conn.execute("COMMIT")
    except sqlite3.Error:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        return False
    conn.execute("BEGIN IMMEDIATE")
    return True


class _Scratch:
    """The step's working file: its links in the order they were added (``seq``, with the
    ``batch`` of each ``builder.add``) and, for the workers, the conversation values the
    serial step reads into ``temp.plx_mention_threads``. Removed with its spill."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.spill = path.with_name(path.name + ".raised")
        self.batches = 0
        self.conn: sqlite3.Connection | None = sqlite3.connect(str(path), isolation_level=None)
        self.conn.execute("PRAGMA journal_mode = OFF")
        self.conn.execute("PRAGMA synchronous = OFF")
        self.conn.execute(f"CREATE TABLE {_SCRATCH_LINKS} (seq INTEGER PRIMARY KEY, "
                          f"batch INTEGER NOT NULL, {', '.join(LINK_COLUMNS)})")
        self.conn.execute("BEGIN")

    def add(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self.batches += 1
        self.conn.executemany(
            f"INSERT INTO {_SCRATCH_LINKS} (batch, {', '.join(LINK_COLUMNS)}) VALUES "
            f"({', '.join('?' for _ in range(len(LINK_COLUMNS) + 1))})",
            [(self.batches, *(row.get(c) for c in LINK_COLUMNS)) for row in rows])

    def documents(self, types: Sequence[str]) -> int:
        """Documents with a lead mention link of ``types`` among this step's links."""
        marks = ", ".join("?" for _ in types)
        return self.conn.execute(
            f"SELECT COUNT(DISTINCT from_object) FROM {_SCRATCH_LINKS} WHERE binding_kind = ? "
            f"AND tier = ? AND link_type IN ({marks})", (M.BINDING_KIND, M.TIER, *types)
        ).fetchone()[0]

    def prepare(self, conn: sqlite3.Connection, thread_property: str) -> None:
        """Index the links for the workers and add the conversation values of ``conn``
        (the build's), then close: the workers read the file."""
        db = self.conn
        db.execute(f"CREATE INDEX {_SCRATCH_LINKS}_from ON {_SCRATCH_LINKS}"
                   "(from_object, link_type)")
        db.execute(f"CREATE INDEX {_SCRATCH_LINKS}_to ON {_SCRATCH_LINKS}(to_object, link_type)")
        db.execute(f"CREATE TABLE {_SCRATCH_THREADS} (value, object_id, l1_handle)")
        cursor = conn.execute("SELECT value, object_id, l1_handle FROM object_values "
                              "WHERE property = ? AND value IS NOT NULL", (thread_property,))
        while True:
            rows = cursor.fetchmany(M.STORE_LINK_BATCH * 20)
            if not rows:
                break
            db.executemany(f"INSERT INTO {_SCRATCH_THREADS} VALUES (?, ?, ?)", rows)
        db.execute(f"CREATE INDEX {_SCRATCH_THREADS}_by_value ON {_SCRATCH_THREADS}(value)")
        db.execute("COMMIT")
        db.execute("BEGIN")

    def replay(self, builder: Any, count: int = 1) -> None:
        """Add the step's links to the build, as the serial step added them: by ``count``
        worker processes writing parts of consecutive links, merged in order by SQL
        (``project_parallel.merge_link_parts``: the same rows, row ids and text ids),
        when the links are many; else here, batch by batch."""
        (total,) = self.conn.execute(f"SELECT count(*) FROM {_SCRATCH_LINKS}").fetchone()
        if count >= 2 and total >= REPLAY_PARTS_MIN and \
                getattr(builder, "connection", None) is not None and \
                _committed(builder.connection):   # else the rows stay open: added here
            return self._replay_parts(builder, count, total)
        batch: list[dict[str, Any]] = []
        at = None
        cursor = self.conn.execute(f"SELECT batch, {', '.join(LINK_COLUMNS)} FROM "
                                   f"{_SCRATCH_LINKS} ORDER BY seq")
        for row in cursor:
            if row[0] != at and batch:
                builder.add("links", batch)
                batch = []
            at = row[0]
            batch.append(dict(zip(LINK_COLUMNS, row[1:])))
        if batch:
            builder.add("links", batch)

    def _replay_parts(self, builder: Any, count: int, total: int) -> None:
        from ontology.project_parallel import merge_link_parts
        from ontology.step_migrate import _parts_in_order
        self.conn.execute("COMMIT")   # the workers read the file
        size = max(REPLAY_PART_ROWS, -(-total // (count * 8)))
        (low, high) = self.conn.execute(f"SELECT min(seq), max(seq) FROM {_SCRATCH_LINKS}"
                                        ).fetchone()
        jobs = [(str(self.path), a, min(a + size, high + 1))
                for a in range(low, high + 1, size)]
        LAST["replay.parts"] = len(jobs)
        folder = self.path.with_name(self.path.name + ".parts")
        folder.mkdir(exist_ok=True)
        conn = builder.connection
        try:
            (rows,) = conn.execute("SELECT count(*) FROM link_rows").fetchone()
            merge_link_parts(builder, _parts_in_order(_replay_part, jobs, count, folder),
                             rebuild_indexes=4 * total >= rows)   # sort once: many
        finally:
            conn.execute("BEGIN IMMEDIATE")
            shutil.rmtree(folder, ignore_errors=True)
            self.conn.execute("BEGIN")

    def remove(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None
        for path in (self.path, self.spill, Path(str(self.path) + "-journal")):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


REPLAY_PARTS_MIN = 200_000   # fewer links: replayed here (the parts cost more)
REPLAY_PART_ROWS = 50_000


def _replay_part(path: str, scratch: str, low: int, high: int) -> str:
    """A worker's part of the replay: the links of ``seq`` in [``low``, ``high``)."""
    from ontology.project_parallel import link_part
    conn = sqlite3.connect(f"file:{scratch}?mode=ro", uri=True)
    try:
        return link_part(path, (dict(zip(LINK_COLUMNS, r)) for r in conn.execute(
            f"SELECT {', '.join(LINK_COLUMNS)} FROM {_SCRATCH_LINKS} WHERE seq >= ? AND "
            "seq < ? ORDER BY seq", (low, high))))
    finally:
        conn.close()


class _Diverted:
    """The builder as ``mentions._store_links`` sees it: its links go to the working file
    (:class:`_Scratch`), counted as the builder counts them; nothing else is written."""

    def __init__(self, builder: Any, side: _Scratch) -> None:
        self._builder, self._side = builder, side

    def add(self, table: str, rows: Iterable[Mapping[str, Any]]) -> int:
        if table != "links":   # the step adds links only; anything else is not this step
            raise ValueError(f"mention_step_unexpected_table:{table}")
        rows = list(rows)
        self._side.add(rows)
        return len(rows)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._builder, name)


# --------------------------------------------------------------------------- the workers

def _decide(builder: Any, conn: sqlite3.Connection, side: _Scratch, types: Sequence[str],
            count: int) -> Path | None:
    """Every raise of this step's corroboration, decided by ``count`` worker processes and
    spilled in the serial order (``_spill`` records); None (corroborate serially) when the
    build's rows cannot be committed or a worker fails."""
    from ontology.store import main_not_imported

    at = time.monotonic()
    try:
        conn.execute("COMMIT")
    except sqlite3.Error as exc:   # a deferred foreign key not whole: the rows stay open
        LAST.update(mode="serial", reason=f"uncommitted:{type(exc).__name__}")
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        return None
    conn.execute("BEGIN IMMEDIATE")
    held = M._domain_holders(conn, M.MENTION_FEATURES["party_domain"]["property"])
    side.prepare(conn, M.MENTION_FEATURES["petra_thread"]["document_property"])
    LAST["seconds.prepare"] = round(time.monotonic() - at, 3)
    at = time.monotonic()
    store = str(Path(builder.temp).resolve())
    raised = documents = 0
    try:
        reader = _reader(store, str(side.path))
        with open(side.spill, "wb") as spill, main_not_imported(), \
                futures.ProcessPoolExecutor(
                    max_workers=count, mp_context=multiprocessing.get_context("spawn"),
                    initializer=_init, initargs=(store, str(side.path), held)) as pool:
            pending: list[Any] = []
            window = count * 3
            chunks = _chunks(reader, types)
            exhausted = False
            while pending or not exhausted:
                while not exhausted and len(pending) < window:
                    chunk = next(chunks, None)
                    if chunk is None:
                        exhausted = True
                        break
                    documents += len(chunk)
                    pending.append(pool.submit(_decide_chunk, chunk, tuple(types)))
                if not pending:
                    break
                found = pending.pop(0).result()
                for document, rises in found:
                    raised += len(rises)
                    pickle.dump((document, rises), spill, protocol=pickle.HIGHEST_PROTOCOL)
        reader.close()
    except Exception as exc:  # noqa: BLE001 - corroborated serially instead, as before
        LAST.update(mode="serial", reason=f"workers_failed:{type(exc).__name__}")
        return None
    LAST.update(mode="parallel", workers=count, documents=documents, raised=raised,
                **{"seconds.decide": round(time.monotonic() - at, 3)})
    return side.spill


def _chunks(reader: sqlite3.Connection, types: Sequence[str]) -> Iterator[list[str]]:
    """The documents the serial step corroborates (its own selection over the same links),
    in its order, ``TASK_DOCUMENTS`` at a time: one sorted pass over the stored link rows
    and the step's (the serial step's pages of ``STORE_LINK_BATCH``, read through the
    union view, would each scan every link after the page)."""
    marks = ", ".join("?" for _ in types)
    where = f"binding_kind = ? AND tier = ? AND link_type IN ({marks}) AND from_object > ''"
    args = (M.BINDING_KIND, M.TIER, *types)
    cursor = reader.execute(
        f"SELECT from_object FROM main.link_rows WHERE {where} UNION "
        f"SELECT from_object FROM plx_step.{_SCRATCH_LINKS} WHERE {where} ORDER BY 1",
        args + args)
    chunk: list[str] = []
    for (document,) in cursor:
        chunk.append(document)
        if len(chunk) >= TASK_DOCUMENTS:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _reader(store: str, scratch: str) -> sqlite3.Connection:
    """A read-only connection to the build's file whose ``links`` are its own and the
    step's (``scratch``), each mention link's markings tagged with its id (:data:`_TAG`),
    and whose ``temp.plx_mention_threads`` is the step's conversation values."""
    conn = sqlite3.connect(Path(store).as_uri() + "?mode=ro", uri=True, timeout=60)
    try:
        conn.execute(f"PRAGMA cache_size = -{WORKER_CACHE_KIB}")
        conn.execute("ATTACH DATABASE ? AS plx_step", (Path(scratch).as_uri() + "?mode=ro",))
        tag = "char(0) || 'plx-link:' || link_id"
        # Every query a worker makes names the document (``from_object``): its index is
        # the one to use (the target's holds every mention of a busy order).
        columns = ", ".join(
            f"CASE WHEN binding_kind = '{M.BINDING_KIND}' THEN json_insert(markings, '$[#]', "
            f"{tag}) ELSE markings END AS markings" if c == "markings"
            else "+to_object AS to_object" if c == "to_object" else c
            for c in LINK_COLUMNS)
        conn.execute(f"CREATE TEMP VIEW links AS SELECT {columns} FROM main.links UNION ALL "
                     f"SELECT {columns} FROM plx_step.{_SCRATCH_LINKS}")
        conn.execute(f"CREATE TEMP VIEW {M._THREADS.split('.')[1]} AS SELECT value, object_id, "
                     f"l1_handle FROM plx_step.{_SCRATCH_THREADS}")
    except BaseException:
        conn.close()
        raise
    return conn


_WORKER: dict[str, Any] = {}


def _init(store: str, scratch: str, held: Mapping[str, set[str]]) -> None:
    _WORKER["conn"] = _reader(store, scratch)
    _WORKER["held"] = M._suffix_holders(held)


def _decide_chunk(documents: Sequence[str], types: Sequence[str]
                  ) -> list[tuple[str, list[tuple[Any, ...]]]]:
    conn, held = _WORKER["conn"], _WORKER["held"]
    out = []
    for document in documents:
        rises = _rises(conn, document, types, held)
        if rises:
            out.append((document, rises))
    return out


def _rises(conn: sqlite3.Connection, document: str, types: Sequence[str],
           held: Callable[[str], set[str]]) -> list[tuple[Any, ...]]:
    """What ``mentions._corroborate_document`` writes for ``document``: per raised link,
    in its order, (link id, reason, why_not, evidence, the marks its markings join, the
    link's own marks); the marks carry the tags of the mention links they came from."""
    rows = M._query(conn, "SELECT * FROM links WHERE from_object = ? AND binding_kind = ? "
                    "ORDER BY link_id", (document, M.BINDING_KIND)).fetchall()
    named: dict[tuple[str, str], dict[str, list[Any]]] = {}
    for row in rows:
        named.setdefault((json.loads(row["evidence_handles"])[0], row["link_type"]),
                         {}).setdefault(row["to_object"], []).append(row)
    out = []
    for row in rows:
        if row["tier"] != M.TIER or row["link_type"] not in types:
            continue
        agreed = M._agreeing(conn, row, named, held)
        if agreed is None or 1 + sum(f["feature"] != "same_record_twin"
                                     for f, _c, _m in agreed) < M.CORROBORATION_MIN:
            continue
        reason = json.loads(row["reason"]) + [feature for feature, _cites, _marks in agreed]
        why_not = [f for f in json.loads(row["why_not"]) if f.get("feature") != "uncorroborated"]
        evidence = list(dict.fromkeys(json.loads(row["evidence_handles"])
                                      + [h for _f, cites, _m in agreed for h in cites]))
        marks = sorted({m for t in (row["markings"], *(m for _f, _c, ms in agreed for m in ms))
                        for m in json.loads(t or "[]")})
        out.append((row["link_id"], json.dumps(reason, sort_keys=True, separators=(",", ":")),
                    json.dumps(why_not, sort_keys=True, separators=(",", ":")),
                    json.dumps(evidence, separators=(",", ":")), marks,
                    [m for m in json.loads(row["markings"]) if not m.startswith(_TAG)]))
    return out


# --------------------------------------------------------------------------- the writes

def _spilled(path: Path) -> Iterator[tuple[str, list[tuple[Any, ...]]]]:
    with open(path, "rb") as spill:
        while True:
            try:
                yield pickle.load(spill)
            except EOFError:
                return


def _apply(conn: sqlite3.Connection, spill: Path) -> int:
    """Write the decided raises in the serial order with the serial statement; returns how
    many. A raise joins the markings an earlier document's raise added to a mention link
    it read (module docstring)."""
    added: dict[str, frozenset[str]] = {}   # link id -> markings its raise added
    raised = 0
    for _document, rises in _spilled(spill):
        grown = []
        for link_id, reason, why_not, evidence, marks, own in rises:
            joined = {m for m in marks if not m.startswith(_TAG)}
            for m in marks:
                if m.startswith(_TAG):
                    joined |= added.get(m[len(_TAG):], frozenset())
            markings = json.dumps(sorted(joined), separators=(",", ":"))
            conn.execute("UPDATE links SET tier = ?, reason = ?, why_not = ?, "
                         "evidence_handles = ?, markings = ? WHERE link_id = ?",
                         (M.CORROBORATED, reason, why_not, evidence, markings, link_id))
            raised += 1
            more = joined - set(own)
            if more:
                grown.append((link_id, frozenset(more)))
        # a document's own links are read before any of its raises (the serial order)
        added.update(grown)
    return raised


__all__ = ["LAST", "MIN_DOCUMENTS", "TASK_DOCUMENTS", "WORKER_CACHE_KIB",
           "store_mention_links", "workers"]
