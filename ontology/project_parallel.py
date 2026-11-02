"""Parallel cold projection: the build's units projected by worker processes.

A cold build (``ontology.project_incremental.build_generation``) projected every unit on
one thread: measured on the live host (2026-10-31, 2026-11-01) about 9 h of a 13 h build,
the ``load`` and ``values`` phases most of it, one binding at a time. The units of a plan
(``ontology.project_incremental.plan``) are what an incremental build already projects
apart from each other: a partial :class:`ontology.project.Projector` (``scope`` the
bindings they depend on, ``emit`` the units) writes exactly the rows and report entries a
whole projection writes for them. Here the units are cut into tasks (:func:`tasks`), each
task is projected by a worker process into a store of its own (a *part*), and the build's
process merges every part into the generation it builds, the rows through
``StoreBuilder.add`` (the content digest is of the rows, not of how they were laid down)
and the report from the units' parts (``merge_reports``), so the generation and its report
equal a serial cold build's: one content digest.

Tasks. A ``same_record`` link unit compares the stored values of its two ends while it
projects (``Projector._conflicts`` reads them from the store being written), so it is one
task with the binding units of both ends; a sharded binding's shard units are one task
with the binding's unit. Every other unit is a task alone. A task's measured seconds are
kept (``units.json`` ``parallel.seconds``) so the next build starts the longest first.

Sources. A worker opens each capture source again from how the build opened it
(``ontology.project.Reopenable``) and reads it only when its ``watermark`` equals the one the
build pinned (``project_incremental._pin``): equal marks mean equal bytes, so the worker
projects the records the build fingerprinted. A source that cannot be opened again (one
without ``opened_with``, a class a worker cannot import, arguments that do not pickle) or
whose watermark is unknown makes the build serial (:class:`ParallelUnavailable`), as does a
worker whose watermark differs or a report entry outside every unit; nothing is guessed.

Readings and fields kept between builds (``integrations.captured_text.KeptReadings``,
``ontology.document_fields.KeptFields``) keep one writer: a worker reads them through a
read-only connection of its own and hands what it hit and what it read anew back to the
build, which records it.

Workers: ``PLX_BUILD_WORKERS`` (1 = serial), else up to ``MAX_WORKERS`` (one CPU left to
the build's own merge and one to the host). Started with ``spawn``: a worker never
inherits the host's threads, locks or open SQLite connections. Each worker holds one
partial projector at a time (its bounded working file and caches, ``Projector``), so the
build's memory is about ``workers`` times a serial build's.

Stdlib only.
"""

from __future__ import annotations

import concurrent.futures as futures
import contextlib
from dataclasses import dataclass, field
import json
import multiprocessing
import os
from pathlib import Path
import pickle
import secrets
import shutil
import sqlite3
import sys
import time
from typing import Any, Callable, Iterator, Mapping, Sequence

from ontology.project import ProjectionError, Projector, binding_unit
from ontology import store_ddl
from ontology.store import StoreBuilder, canonical, main_not_imported

WORKERS_ENV = "PLX_BUILD_WORKERS"
MAX_WORKERS = 12
# Below this many bytes of capture files a build is projected serially: starting the
# workers costs more than they save.
MIN_CAPTURE_BYTES = 64 * 1024 * 1024
# The memos a projection reads captured files through, whose kept store a worker reads.
_KEPT = (("integrations.captured_text", "_CACHE"), ("ontology.document_fields", "_FIELDS"))


class ParallelUnavailable(Exception):
    """This build cannot be projected in parallel; it is projected serially."""


def workers(requested: int | None = None) -> int:
    """The worker count: ``requested``, else ``PLX_BUILD_WORKERS``, else the CPUs less two
    (at most ``MAX_WORKERS``). A count that is not a positive integer is refused."""
    if requested is None:
        raw = os.environ.get(WORKERS_ENV)
        if raw is None or raw == "":
            return max(1, min(MAX_WORKERS, (os.cpu_count() or 1) - 2))
        try:
            requested = int(raw)
        except ValueError:
            raise ProjectionError("build_workers_invalid", raw) from None
    if requested < 1:
        raise ProjectionError("build_workers_invalid", str(requested))
    return requested


def tasks(the_plan: Any) -> list[tuple[str, ...]]:
    """The plan's units cut into tasks (module docstring), each sorted, in unit order."""
    parent = {unit: unit for unit in the_plan.units}

    def find(unit: str) -> str:
        while parent[unit] != unit:
            parent[unit] = parent[parent[unit]]
            unit = parent[unit]
        return unit

    def join(a: str, b: str) -> None:
        if a in parent and b in parent:
            parent[find(a)] = find(b)

    for unit, row in the_plan.rows.items():
        if row[0] == "links" and row[4] == "same_record":
            join(unit, binding_unit(row[2]))
            join(unit, binding_unit(row[3]))
        elif row[0] == "shard":
            join(unit, binding_unit(row[1]))
    groups: dict[str, list[str]] = {}
    for unit in sorted(the_plan.units):
        groups.setdefault(find(unit), []).append(unit)
    return sorted(tuple(group) for group in groups.values())


def _kept_memo(module: str, memo: str) -> Any:
    """One of the ``_KEPT`` memos, imported by name from a fixed list."""
    from integrations import captured_text
    from ontology import document_fields

    return getattr({"integrations.captured_text": captured_text,
                    "ontology.document_fields": document_fields}[module], memo)


def _source_spec(name: str, source: Any, relations: Sequence[str], mark: Any
                 ) -> tuple[Any, ...]:
    inner = getattr(source, "_inner", source)   # project_incremental._Pinned
    opened = getattr(inner, "opened_with", None)
    kind = type(inner)
    if opened is None:
        raise ParallelUnavailable(f"source_not_reopenable:{name}")
    if mark is None:
        raise ParallelUnavailable(f"source_watermark_unknown:{name}")
    try:
        # the class travels by reference: pickling fails unless it is importable as itself
        spec = (kind, opened[0], opened[1], tuple(relations), mark)
        pickle.dumps(spec)
    except ParallelUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - any reason a worker cannot open it
        raise ParallelUnavailable(f"source_not_reopenable:{name}:{type(exc).__name__}") \
            from None
    return spec


def capture_bytes(marks: Mapping[str, Any]) -> int:
    """The bytes of the capture files the pinned watermarks name (each file's size, as
    ``ontology.project._file_marks`` records it)."""
    total = 0

    def walk(item: Any) -> None:
        nonlocal total
        if isinstance(item, list):
            if len(item) == 6 and isinstance(item[0], str) and all(
                    isinstance(v, int) for v in item[1:]):
                total += item[3]
                return
            for sub in item:
                walk(sub)
    for entry in marks.values():
        try:
            walk(json.loads(entry.get("mark") or "null"))
        except ValueError:
            continue
    return total


@dataclass
class Result:
    """What a parallel projection hands its build: each unit's report part, each
    task's seconds (by its first unit), the summed working-file peak of the tasks that
    ran at once (at most), the worker count and the merge's seconds."""

    parts: dict[str, dict[str, Any]] = field(default_factory=dict)
    seconds: dict[str, float] = field(default_factory=dict)
    work_peak: int = 0
    workers: int = 0
    merge_seconds: float = 0.0
    part_bytes: int = 0


# --------------------------------------------------------------------------- worker

_STATE: dict[str, Any] = {}


class _ChildKept:
    """A worker's view of a kept store (``KeptReadings``/``KeptFields``): hits read
    through its own read-only connection, new entries held for the build to record."""

    def __init__(self, kind: Any, path: str, code: str) -> None:
        self.kind, self.code = kind, code
        self.used: list[str] = []
        self.kept: list[tuple[str, Any]] = []
        try:
            self._db: sqlite3.Connection | None = sqlite3.connect(
                f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        except sqlite3.Error:
            self._db = None

    def get(self, key: Any) -> tuple[Any] | None:
        name = self.kind._key(key)
        if name is None or self._db is None:
            return None
        try:
            row = self._db.execute(f"SELECT code, value FROM {self.kind.TABLE} WHERE key = ?",
                                   (name,)).fetchone()
            if row is None or row[0] != self.code:
                return None
            found = (self.kind.decode(row[1]),)
        except Exception:  # noqa: BLE001 - an unreadable entry is a miss
            return None
        self.used.append(name)
        return found

    def put(self, key: Any, found: Any) -> None:
        name = self.kind._key(key)
        if name is None:
            return
        try:
            value = self.kind.encode(found)
        except Exception:  # noqa: BLE001 - not kept: read again next time
            return
        if value is not None:
            self.kept.append((name, value))

    def take(self) -> tuple[list[str], list[tuple[str, Any]]]:
        out = (self.used, self.kept)
        self.used, self.kept = [], []
        return out


def _init(payload: bytes) -> None:
    from ontology.project_incremental import _Pinned

    config = pickle.loads(payload)
    _STATE.clear()
    _STATE.update(config)
    sources: dict[str, Any] = {}
    try:
        for name, (kind, args, kwargs, relations, mark) in config["sources"].items():
            source = kind(*args, **kwargs)
            records = source.records(list(relations))
            own = source.watermark()
            if own is None or canonical(own) != mark:
                _STATE["unavailable"] = f"source_changed:{name}"
                return
            sources[name] = _Pinned(source, records)
    except Exception as exc:  # noqa: BLE001 - reported per task; the build goes serial
        _STATE["unavailable"] = f"source_not_reopened:{type(exc).__name__}:{exc}"[:300]
        return
    _STATE["opened"] = sources
    _STATE["kept_views"] = _kept_views(config["kept"])


def _kept_views(kept: Sequence[tuple[Any, ...]]) -> list[_ChildKept]:
    """This worker's read-only views of the build's kept stores, set as their memos'
    backing."""
    views = []
    for module, memo, kind, path, code in kept:
        child = _ChildKept(kind, path, code)
        _kept_memo(module, memo).backing = child
        views.append(child)
    return views


def _kept_stores() -> list[tuple[Any, ...]]:
    """The build's kept stores a worker reads (``_KEPT`` memos with a backing that records
    what a worker hands back), each flushed first, so a worker sees what is kept so far."""
    kept = []
    for module, memo in _KEPT:
        backing = _kept_memo(module, memo).backing
        if backing is not None and hasattr(backing, "absorb"):
            backing.flush()
            kept.append((module, memo, type(backing), str(backing.path), backing.code))
    return kept


def _run(index: int, units: tuple[str, ...], scope: tuple[str, ...]) -> dict[str, Any]:
    if "unavailable" in _STATE:
        return {"index": index, "unavailable": _STATE["unavailable"]}
    started = time.monotonic()
    part = Path(_STATE["dir"]) / f"part-{index:05d}.sqlite"
    builder = StoreBuilder(part, foreign_keys=False)
    try:
        projector = Projector(_STATE["schema"], _STATE["opened"],
                              normalizer=_STATE["normalizer"],
                              as_known_at=_STATE["as_known_at"],
                              batch_size=_STATE["batch_size"], scratch_dir=_STATE["dir"],
                              scope=scope, emit=units, shards=_STATE["shards"])
        peak = [0]

        def stage(_name: str, work: int, _store: int) -> None:
            peak[0] = max(peak[0], work)
        projector.on_stage = stage
        projector.project_into(builder)
        path = builder.keep_as_part()
    except BaseException:
        builder.abort()
        raise
    report = projector.report
    return {"index": index, "part": str(path), "unattributed": report.unattributed,
            "parts": {unit: report.parts[unit].as_dict() for unit in units
                      if unit in report.parts},
            "kept": [view.take() for view in _STATE.get("kept_views", [])],
            "work_peak": peak[0], "seconds": round(time.monotonic() - started, 3)}


# --------------------------------------------------------------------------- build side

def merge_part(builder: StoreBuilder, part: Path, batch: int, *, links: bool = False
               ) -> None:
    """Copy the rows of a part into ``builder``'s generation as stored, in a transaction
    of their own (the generation's connection is outside one: :func:`project`), by SQL
    over the part attached: objects, removals, identifiers and events as they
    are; versions and value sources under ids after the generation's own (a source names
    its version by rowid); value fields, kinds and layouts and link texts and skeletons
    interned in the generation (fields, kinds and layouts by the builder's own interning,
    so a later ``add`` finds them) and every id naming one mapped. The view rows
    (``object_values``, ``links``) are the part's: the content digest is of them.
    ``links`` False: every row but the links and event objects; True: those (they may
    name objects of any part, so they are merged once every part's objects are: a
    foreign key is never left pending). ``batch`` is unused (kept for callers)."""
    main = builder.connection
    main.execute("ATTACH DATABASE ? AS part", (str(part),))
    try:
        main.execute("BEGIN IMMEDIATE")
        try:
            _merge_rows(builder, main, links)
            main.execute("COMMIT")
        except BaseException:
            if main.in_transaction:
                main.execute("ROLLBACK")
            raise
    finally:
        main.execute("DETACH DATABASE part")


def _columns(main: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in main.execute(f"PRAGMA part.table_info({table})")]


def _copy(main: sqlite3.Connection, table: str, names: Sequence[str],
          select: Sequence[str] | None = None, joins: str = "", alias: str = "t") -> None:
    """INSERT the part's rows of ``table`` (in its stored order) into the generation's:
    ``names`` from ``select`` (the expressions over ``alias``; None: the same names)."""
    picked = select or [f"{alias}.{name}" for name in names]
    main.execute(f"INSERT INTO main.{table} ({', '.join(names)}) SELECT {', '.join(picked)} "
                 f"FROM part.{table} {alias}{joins}")


def _merge_rows(builder: StoreBuilder, main: sqlite3.Connection, links: bool) -> None:
    D = store_ddl
    if links:
        _copy(main, "event_objects", _columns(main, "event_objects"))
        _merge_links(main)
        return
    for table in ("objects", "object_removals", "identifiers", "events"):
        _copy(main, table, _columns(main, table))
    ver = main.execute("SELECT COALESCE(MAX(rowid), 0) FROM main.object_versions").fetchone()[0]
    names = _columns(main, "object_versions")
    _copy(main, "object_versions", ["rowid", *names],
          [f"t.rowid + {int(ver)}", *(f"t.{n}" for n in names)], " ORDER BY t.rowid")
    fields = {row[0]: builder._intern_shared(
                  "value_fields", "field", D.FIELD_COLUMNS, dict(zip(D.FIELD_COLUMNS, row[1:])))
              for row in main.execute(
                  f"SELECT field, {', '.join(D.FIELD_COLUMNS)} FROM part.value_fields "
                  "ORDER BY field").fetchall()}
    kinds = {row[0]: builder._intern_shared(
                 "value_kinds", "kind", D.KIND_COLUMNS, dict(zip(D.KIND_COLUMNS, row[1:])))
             for row in main.execute(
                 f"SELECT kind, {', '.join(D.KIND_COLUMNS)} FROM part.value_kinds "
                 "ORDER BY kind").fetchall()}
    layouts = {layout: builder._layout(tuple((fields[f], kinds[k])
                                             for f, k in json.loads(text)))
               for layout, text in main.execute(
                   "SELECT layout, fields FROM part.value_layouts ORDER BY layout").fetchall()}
    _map(main, "plx_merge_field", fields)
    _map(main, "plx_merge_layout", layouts)
    src = main.execute("SELECT COALESCE(MAX(src), 0) FROM main.value_sources").fetchone()[0]
    names = _columns(main, "value_sources")
    shift = {"src": f"t.src + {int(src)}", "ver": f"t.ver + {int(ver)}"}
    _copy(main, "value_sources", names, [shift.get(n, f"t.{n}") for n in names],
          " ORDER BY t.src")
    _copy(main, "value_cells", ["src", "field", "layout", *D.CELL_COLUMNS],
          [f"t.src + {int(src)}", "f.new", "l.new", *(f"t.{n}" for n in D.CELL_COLUMNS)],
          " LEFT JOIN temp.plx_merge_field f ON f.old = t.field"
          " JOIN temp.plx_merge_layout l ON l.old = t.layout ORDER BY t.rowid")


def _map(main: sqlite3.Connection, table: str, pairs: Mapping[int, int]) -> None:
    """``temp.<table>``: the part's ids -> the generation's."""
    main.execute(f"CREATE TEMP TABLE IF NOT EXISTS {table} "
                 "(old INTEGER PRIMARY KEY, new INTEGER NOT NULL)")
    main.execute(f"DELETE FROM temp.{table}")
    main.executemany(f"INSERT INTO temp.{table} (old, new) VALUES (?, ?)", pairs.items())


def _merge_links(main: sqlite3.Connection) -> None:
    for table, mapped in (("link_texts", "plx_merge_text"),
                          ("link_skeletons", "plx_merge_skeleton")):
        # The texts the generation lacks, in the part's order; then every one mapped.
        main.execute(f"INSERT OR IGNORE INTO main.{table} (text) "
                     f"SELECT text FROM part.{table} ORDER BY id")
        main.execute(f"CREATE TEMP TABLE IF NOT EXISTS {mapped} "
                     "(old INTEGER PRIMARY KEY, new INTEGER NOT NULL)")
        main.execute(f"DELETE FROM temp.{mapped}")
        main.execute(f"INSERT INTO temp.{mapped} (old, new) SELECT p.id, m.id "
                     f"FROM part.{table} p JOIN main.{table} m ON m.text = p.text")
    names = _columns(main, "link_rows")
    slots = {f"s{k}" for k in range(store_ddl.LINK_SLOTS)}
    picked = []
    for name in names:
        if name in ("reason_id", "evidence_id"):
            picked.append(f"(SELECT new FROM temp.plx_merge_skeleton WHERE old = t.{name})")
        elif name in slots:
            picked.append(f"(SELECT new FROM temp.plx_merge_text WHERE old = t.{name})")
        else:
            picked.append(f"t.{name}")
    _copy(main, "link_rows", names, picked, " ORDER BY t.rowid")


def project(builder: StoreBuilder, *, schema: Any, sources: Mapping[str, Any],
            marks: Mapping[str, Any], the_plan: Any, normalizer: Any, as_known_at: Any,
            batch_size: int, shards: Mapping[str, int], scratch_dir: Path, count: int,
            hints: Mapping[str, float] | None = None,
            checkpoint: Callable[[], None] | None = None) -> Result:
    """Project every unit of ``the_plan`` into ``builder``'s (unseeded, empty) open
    generation with ``count`` worker processes (module docstring). Raises
    :class:`ParallelUnavailable` when the build must be projected serially: before any
    row is written when a source cannot be opened again, else after (the caller then
    restarts the builder)."""
    if not the_plan.acyclic:
        raise ParallelUnavailable("cyclic_lineage")
    relations: dict[str, set[str]] = {}
    for _, b in the_plan.bindings.values():
        if b["source"] in sources:
            relations.setdefault(b["source"], set()).add(b["relation"])
    specs = {name: _source_spec(name, sources[name], sorted(wanted),
                                (marks.get(name) or {}).get("mark"))
             for name, wanted in sorted(relations.items())}
    kept = _kept_stores()
    try:
        payload = pickle.dumps({
            "schema": schema, "normalizer": normalizer, "as_known_at": as_known_at,
            "batch_size": batch_size, "shards": dict(shards), "sources": specs,
            "kept": kept})
    except Exception as exc:  # noqa: BLE001 - a schema or normalizer a worker cannot get
        raise ParallelUnavailable(f"build_not_picklable:{type(exc).__name__}") from None
    work = scratch_dir / f".plx-parallel-{secrets.token_hex(6)}"
    work.mkdir(mode=0o700)
    # A worker's directory is its build's: part and working files (captured rows, 0600).
    payload = pickle.dumps({**pickle.loads(payload), "dir": str(work)})
    order = tasks(the_plan)
    scopes = [tuple(sorted(set().union(*(the_plan.units[u] for u in task))))
              for task in order]
    hints = hints or {}
    # Longest first: a task the last build measured by its seconds, an unmeasured one
    # first of all (it may be long), then by the bindings it reads.
    queue = sorted(range(len(order)), key=lambda i: (
        order[i][0] in hints, -hints.get(order[i][0], 0.0), -len(scopes[i]), i))
    result = Result(workers=count)
    builder.packed_bindings = frozenset(
        bid for bid, (_, b) in the_plan.bindings.items() if b.get("value_storage") == "packed")
    peaks: list[int] = []
    merged: list[Path] = []
    # The generation's secondary indexes are built once the rows are in (one sorted pass
    # each, where the merge would update them row by row); the unique ones stay.
    indexes = [sql for (sql,) in builder.connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL "
        "AND sql NOT LIKE 'CREATE UNIQUE%' ORDER BY name")]
    for name, in builder.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL "
            "AND sql NOT LIKE 'CREATE UNIQUE%' ORDER BY name").fetchall():
        builder.connection.execute(f'DROP INDEX "{name}"')
    # Each part is merged in a transaction of its own (it is attached to the generation's
    # connection, which SQLite allows only outside one); the build's own is opened again
    # once every part is in. A build that cannot finish is laid down again from empty
    # (``StoreBuilder.restart``).
    builder.connection.execute("COMMIT")
    pool = futures.ProcessPoolExecutor(max_workers=count,
                                       mp_context=multiprocessing.get_context("spawn"),
                                       initializer=_init, initargs=(payload,))
    try:
        with main_not_imported():
            pending = {pool.submit(_run, i, order[i], scopes[i]) for i in queue}
        while pending:
            if checkpoint is not None:
                checkpoint()
            done, pending = futures.wait(pending, timeout=5,
                                         return_when=futures.FIRST_COMPLETED)
            for future in done:
                out = future.result()
                if "unavailable" in out:
                    raise ParallelUnavailable(out["unavailable"])
                if out["unattributed"]:
                    raise ParallelUnavailable("report_entry_outside_every_unit")
                started = time.monotonic()
                for (module, memo, *_), (used, new) in zip(kept, out["kept"]):
                    _kept_memo(module, memo).backing.absorb(used, new)
                merge_part(builder, Path(out["part"]), batch_size)
                merged.append(Path(out["part"]))
                result.merge_seconds += time.monotonic() - started
                result.parts.update(out["parts"])
                result.seconds[order[out["index"]][0]] = out["seconds"]
                peaks.append(out["work_peak"])
        # Every part's objects are in: their links and event objects now (in task order).
        started = time.monotonic()
        result.part_bytes = sum(path.stat().st_size for path in merged)
        for path in sorted(merged):
            if checkpoint is not None:
                checkpoint()
            merge_part(builder, path, batch_size, links=True)
            path.unlink()
        for sql in indexes:
            builder.connection.execute(sql)
        builder.connection.execute("BEGIN IMMEDIATE")
        result.merge_seconds += time.monotonic() - started
    except BaseException:
        for process in list(getattr(pool, "_processes", {}).values()):
            process.terminate()
        pool.shutdown(wait=True, cancel_futures=True)
        shutil.rmtree(work, ignore_errors=True)
        raise
    pool.shutdown(wait=True)
    shutil.rmtree(work, ignore_errors=True)
    # Disk beside the generation: the working files of the tasks that ran at once (at
    # most) and the parts held until their links are merged.
    result.work_peak = sum(sorted(peaks, reverse=True)[:count]) + result.part_bytes
    result.merge_seconds = round(result.merge_seconds, 3)
    return result


# --------------------------------------------------------------------------- parsing

# A capture source's documentFields records (``ontology.document_fields.captured``) below
# this many are parsed in the build's own process; above, by the build's workers in tasks
# of ``PARSE_CHUNK`` records (``DocumentPool.captured``).
MIN_PARSE_RECORDS = 64
PARSE_CHUNK = 8
PARSE_WINDOW = 4
PARSE_HELD = 64


def _parse_tasks(records: Sequence[Any]) -> list[list[int]]:
    """The indexes of ``records`` cut into parse tasks: the records of one file's bytes
    (``content_sha256``) in one task, so one worker reads them once (its memos, as the
    build's own process would), files packed in their first record's order up to
    ``PARSE_CHUNK`` records a task (a file with more is a task alone)."""
    files: dict[Any, list[int]] = {}
    for index, rec in enumerate(records):
        row = getattr(rec, "row", None)
        digest = row.get("content_sha256") if isinstance(row, Mapping) else None
        files.setdefault(digest if isinstance(digest, str) else ("record", index),
                         []).append(index)
    tasks: list[list[int]] = []
    for indexes in files.values():
        if not tasks or len(tasks[-1]) + len(indexes) > PARSE_CHUNK:
            tasks.append([])
        tasks[-1].extend(indexes)
    return [task for task in tasks if task]


def _init_parse(kept: Sequence[tuple[Any, ...]]) -> None:
    _STATE.clear()
    _STATE["kept_views"] = _kept_views(kept)


def _parse(chunk: Sequence[tuple[Any, str | None]], blobs: str
           ) -> tuple[list[list[Any]], list[tuple[list[str], list[tuple[str, Any]]]]]:
    """The documentFields records of each captured file record of ``chunk`` (with its
    provenance signal), and what this worker's views of the kept stores hit and kept."""
    from ontology.document_fields import captured_one

    found = [list(captured_one(rec, blobs, issued)) for rec, issued in chunk]
    return found, [view.take() for view in _STATE.get("kept_views", [])]


def _derive(task: str, documents: Sequence[Any]) -> list[Any]:
    """What ``task`` derives from each of ``documents``: ``mentions``, a document's
    mentions (``ontology.mentions.document_mentions``); ``text``, its text index facts and
    chunks (``ontology.text_index.document_chunks``)."""
    from ontology.mentions import document_mentions
    from ontology.text_index import document_chunks

    derive = {"mentions": document_mentions, "text": document_chunks}[task]
    return [derive(document) for document in documents]


_SOURCES: dict[bytes, Any] = {}   # a document worker's sources opened again, by spec


def _opened(spec: tuple[Any, ...]) -> Any:
    """The capture source of ``spec`` (``_source_spec``) opened again in this worker, its
    snapshot's watermark the build's; else raises (the build then reads it itself)."""
    from ontology.project_incremental import _Pinned

    key = pickle.dumps(spec)
    if key not in _SOURCES:
        kind, args, kwargs, relations, mark = spec
        try:
            source = kind(*args, **kwargs)
            source.records(list(relations))
            own = source.watermark()
            _SOURCES[key] = source if own is not None and canonical(own) == mark else \
                ParallelUnavailable("source_changed")
        except Exception as exc:  # noqa: BLE001 - this source is read by the build
            _SOURCES[key] = ParallelUnavailable(f"source_not_reopened:{type(exc).__name__}")
    found = _SOURCES[key]
    if isinstance(found, Exception):
        raise found
    return getattr(found, "_inner", found) if isinstance(found, _Pinned) else found


def _load(task: str, spec: tuple[Any, ...], loads: Sequence[tuple[str, tuple[Any, ...]]]
          ) -> tuple[list[tuple[list[Any], bool, list[Any]]],
                     list[tuple[list[str], list[tuple[str, Any]]]]]:
    """Each group of ``loads`` (``ontology.project.GroupLoad.portable``) read from the
    source of ``spec`` opened again: its documents, whether their readings are
    remembered and what ``task`` (``_derive``) derives from each; and what this worker's
    views of the kept stores hit and kept."""
    from ontology.project import GROUP_LOADS

    source = _opened(spec)
    out = []
    for method, args in loads:
        documents, remembered = getattr(source, {m: m for m in GROUP_LOADS}[method])(*args)
        out.append((documents, remembered, _derive(task, documents)))
    return out, [view.take() for view in _STATE.get("kept_views", [])]


class DocumentPool:
    """The worker processes a build reads its captured documents with (``DOCUMENTS`` while
    :func:`document_pool` holds it): their fields (:meth:`captured`), their mentions and
    their text index chunks (:meth:`derive`, for the steps' ``ontology.mentions.read_ahead``).

    Measured on the live gen 7 cold build: 12,851 s of 62,994 s in ``fingerprint.shards``,
    the first read of every documentFields relation parsing each of 250,663 documents on
    one thread (no field kept yet). Here a source's records are cut into tasks of
    ``PARSE_CHUNK`` records in their order; a worker parses a task's records by
    ``document_fields.captured_one``, the function the build's own process runs, reading
    the kept readings and fields through read-only views of its own (``_ChildKept``), and
    hands back their records and what it hit and kept; the build records those in the kept
    stores (their one writer, as for a parallel projection: the same keys and the same
    values a serial parse keeps) and yields the records in the records' order. A task whose
    worker raised or died is parsed here (a failure that is the document's raises here as
    it would serially); after a worker died every later task is. Started with ``spawn``,
    at most ``count`` workers, each with the bounded memos of a serial parse
    (``integrations.captured_text``), so memory is about ``count`` times a serial parse's.
    ``engaged``: whether this build reads documents in parallel (a serial build does not);
    :meth:`close` stops the workers (started again on the next use: the build closes them
    before its projection, whose workers then have the memory)."""

    def __init__(self, count: int) -> None:
        self.count, self.engaged, self.broken = count, False, False
        self._pool: Any = None
        self._kept: Any = None
        self.tasks = self.fallback = 0
        self._specs: dict[int, tuple[Any, ...]] = {}

    def know(self, sources: Mapping[str, Any], marks: Mapping[str, Any],
             relations: Mapping[str, Sequence[str]]) -> None:
        """The build's pinned ``sources`` a worker may open again to read their document
        groups (:meth:`load`): each that remembers how it was opened, under the
        watermark the build pinned (``marks``) over the ``relations`` it reads."""
        self._specs = {}
        for name, source in sources.items():
            try:
                spec = _source_spec(name, source, sorted(relations.get(name, ())),
                                    (marks.get(name) or {}).get("mark"))
            except ParallelUnavailable:
                continue
            self._specs[id(getattr(source, "_inner", source))] = spec

    def knows(self, source: Any) -> bool:
        """Whether a worker may open ``source`` again (:meth:`know`)."""
        return id(source) in self._specs

    def load(self, task: str, loads: Sequence[Any]
             ) -> Callable[[], list[tuple[list[Any], bool, list[Any]]] | None] | None:
        """Each of ``loads`` (``ontology.project.GroupLoad`` of one source) read by a worker
        that opened its source again, with what ``task`` derives from each document: a
        call that waits for them (None when the worker failed: the caller reads them
        itself); None when no worker can (no workers, or a source a worker cannot open)."""
        spec = self._specs.get(id(loads[0].source)) if loads else None
        if spec is None or not self.usable():
            return None
        kept = self._kept if self._pool is not None else _kept_stores()
        pool = self._pool if self._pool is not None else self._pool_for(kept)
        try:
            with main_not_imported():
                future = pool.submit(_load, task, spec, [load.portable() for load in loads])
        except Exception as exc:  # noqa: BLE001 - read by the caller instead
            self.broken = self.broken or isinstance(exc, (futures.BrokenExecutor,
                                                          RuntimeError))
            return None

        def result() -> list[tuple[list[Any], bool, list[Any]]] | None:
            try:
                found, taken = future.result()
            except Exception as exc:  # noqa: BLE001 - read by the caller instead
                self.broken = self.broken or isinstance(exc, futures.BrokenExecutor)
                self.fallback += 1
                return None
            for (module, memo, *_), (used, new) in zip(self._kept or [], taken):
                _kept_memo(module, memo).backing.absorb(used, new)
            return found
        self.tasks += 1
        return result

    def _pool_for(self, kept: list[tuple[Any, ...]]) -> Any:
        if self._pool is not None and self._kept != kept:
            self.close()
        if self._pool is None:
            self._kept = kept
            self._pool = futures.ProcessPoolExecutor(
                max_workers=self.count, mp_context=multiprocessing.get_context("spawn"),
                initializer=_init_parse, initargs=(kept,))
        return self._pool

    def captured(self, records: Sequence[Any], signals: Sequence[str | None],
                 blobs: Any) -> Iterator[Any]:
        """``document_fields.captured``'s records of ``records`` (``signals``: each one's
        provenance signal), in their order."""
        from ontology.document_fields import captured_one

        if not self.usable() or len(records) < MIN_PARSE_RECORDS:
            for rec, issued in zip(records, signals):
                yield from captured_one(rec, blobs, issued)
            return
        kept = _kept_stores()
        pool = self._pool_for(kept)
        tasks = _parse_tasks(records)
        task_of = {index: number for number, task in enumerate(tasks) for index in task}
        here = object()   # a record parsed in this process when it is yielded
        results: dict[int, Any] = {}
        pending: dict[int, Any] = {}
        submitted = 0

        def send(number: int) -> None:
            nonlocal submitted
            # Every task up to the one wanted is sent, then more while fewer than
            # ``PARSE_WINDOW`` a worker are running (a long one at the head of the order
            # stops none of the others) and at most ``PARSE_HELD`` a worker wait to be
            # yielded.
            running = sum(1 for future in pending.values() if not future.done())
            while submitted < len(tasks) and not self.broken and (
                    submitted <= number or (running < self.count * PARSE_WINDOW
                                            and len(pending) < self.count * PARSE_HELD)):
                running += 1
                with main_not_imported():
                    pending[submitted] = pool.submit(
                        _parse, [(records[i], signals[i]) for i in tasks[submitted]],
                        str(blobs))
                submitted += 1

        def complete(number: int) -> None:
            send(number)
            while number in pending and not pending[number].done():
                futures.wait([future for future in pending.values() if not future.done()],
                             return_when=futures.FIRST_COMPLETED)
                send(number)   # the workers that finished get the next tasks meanwhile
            future = pending.pop(number, None)
            found = None
            if future is not None:
                self.tasks += 1
                try:
                    found, taken = future.result()
                except Exception as exc:  # noqa: BLE001 - parsed here instead
                    self.broken = self.broken or isinstance(exc, futures.BrokenExecutor)
                    self.fallback += 1
                else:
                    for (module, memo, *_), (used, new) in zip(kept, taken):
                        _kept_memo(module, memo).backing.absorb(used, new)
            for at, index in enumerate(tasks[number]):
                results[index] = here if found is None else found[at]

        try:
            for index, (rec, issued) in enumerate(zip(records, signals)):
                if index not in results:
                    complete(task_of[index])
                found = results.pop(index)
                if found is here:   # its worker failed, or none is left: parsed here
                    yield from captured_one(rec, blobs, issued)
                else:
                    yield from found
        finally:
            for future in pending.values():
                future.cancel()

    def usable(self) -> bool:
        """Whether this build reads documents by its workers now."""
        return self.engaged and not self.broken and self.count > 1

    def derive(self, task: str, documents: Sequence[Any]
               ) -> Callable[[], list[Any] | None] | None:
        """What ``task`` (``_DERIVE``) derives from each of ``documents``
        (``ontology.mentions.Document``), by a worker: a call that waits for it (None when
        the worker raised or died: the caller derives it itself); None when this build has
        no workers to ask."""
        if not self.usable() or not documents:
            return None
        pool = self._pool if self._pool is not None else self._pool_for(_kept_stores())
        try:
            with main_not_imported():
                future = pool.submit(_derive, task, list(documents))
        except Exception as exc:  # noqa: BLE001 - derived by the caller instead
            self.broken = self.broken or isinstance(exc, (futures.BrokenExecutor,
                                                          RuntimeError))
            return None

        def result() -> list[Any] | None:
            try:
                return future.result()
            except Exception as exc:  # noqa: BLE001 - derived by the caller instead
                self.broken = self.broken or isinstance(exc, futures.BrokenExecutor)
                self.fallback += 1
                return None
        self.tasks += 1
        return result

    def close(self, *, failed: bool = False) -> None:
        pool, self._pool = self._pool, None
        if pool is None:
            return
        if failed:
            for process in list(getattr(pool, "_processes", {}).values()):
                process.terminate()
        pool.shutdown(wait=True, cancel_futures=True)


# The build's document workers while :func:`document_pool` holds them; None: a document
# is read in the process that asks.
DOCUMENTS: DocumentPool | None = None


@contextlib.contextmanager
def document_pool(count: int) -> Iterator[DocumentPool]:
    """While open, captured documents are read through a :class:`DocumentPool` of
    ``count`` workers (``DOCUMENTS``), once the build engages it."""
    global DOCUMENTS
    held, pool = DOCUMENTS, DocumentPool(count)
    DOCUMENTS = pool
    failed = True
    try:
        yield pool
        failed = False
    finally:
        DOCUMENTS = held
        pool.close(failed=failed)


__all__ = ["DOCUMENTS", "DocumentPool", "MAX_WORKERS", "MIN_CAPTURE_BYTES",
           "MIN_PARSE_RECORDS", "ParallelUnavailable", "Result", "WORKERS_ENV",
           "capture_bytes", "document_pool", "merge_part", "project", "tasks", "workers"]
