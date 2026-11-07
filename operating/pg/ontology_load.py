"""Bulk load of a projection generation into the Postgres ontology (``ontology.*``,
migrations ``0006_ontology`` and ``0007_ontology_compact``), beside the SQLite store that
keeps serving.

``python3 -m operating.pg.ontology_load STORE [--workers N] [--tables ...] [--replace]
[--min-free-gb G]``

Every table a read tool uses is loaded: objects, removals, versions, the values (their
sources, fields, kinds and one row per value: a packed record unpacked), links (packed:
skeletons and slot texts), identifiers, events and their objects, and the document text
index (fields and zlib chunks). Each table is read from the generation (read-only) in
rowid ranges by ``--workers`` processes, each COPYing its range over its own connection.

Resumable per table and per range: a range's rows and its ``ontology.load_ranges`` row
commit in one transaction, so a load stopped anywhere (killed, the disk floor) is run
again with the same arguments and copies only the ranges not yet recorded; a table whose
ranges are all in and whose indexes are built is recorded under ``table:<name>`` in
``ontology.load_state`` and skipped. A table's secondary indexes and primary key are
dropped before its first range and built once after its last (one sort each); their
definitions are kept in ``load_state`` (``indexes:<name>``) so a resumed load builds them.
``--replace`` empties the tables named and their records first.

The generation is recorded under ``generation`` (its path and content digest); a load
over a different generation is refused unless ``--replace``, and every range checks that
the store file still holds the recorded generation (a generation published or pruned
mid-load stops it: ``GenerationChanged``). ``--min-free-gb``: free space
the file system of the database must keep; a range is not started below it
(``DiskFloor``; the load stops, resumable). A generation of a state directory's
projection (``<state>/projection/...``) on the database's file system also keeps the next
projection build's room free (``operating.host.build_reserve``: the next cold build's
estimate), whichever floor is higher. The loader's own sessions set
``maintenance_work_mem`` and ``work_mem`` (``SESSION_SETTINGS``).

Stdlib and psycopg.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from typing import Any, Callable, Iterator, Sequence

from ontology import store_ddl

# The schema a load writes (``PLX_ONTOLOGY_SCHEMA``): ``ontology``, or ``ontology_next`` for
# a generation loaded beside the served one (``operating.pg.ontology_swap``).
SCHEMA = os.environ.get("PLX_ONTOLOGY_SCHEMA", "ontology")
if SCHEMA not in ("ontology", "ontology_next"):
    raise ValueError(f"PLX_ONTOLOGY_SCHEMA must be ontology or ontology_next, not {SCHEMA!r}")
NUMERIC = ("integer", "decimal", "money", "quantity")
TIMES = ("date", "timestamp")
SESSION_SETTINGS = {"maintenance_work_mem": "2GB", "work_mem": "256MB",
                    "max_parallel_maintenance_workers": "6", "synchronous_commit": "off"}

_LINK_PLAIN = ("link_id", "link_type", "from_object", "to_object", "tier", "binding_kind",
               "link_contract", "reason_id", "why_not", "evidence_id", "reviewed_by",
               "reviewed_at", "review_action_id", "ek")
_LINK_TAIL = ("markings", "recorded_at", "superseded_at")


class Table:
    """One target table: the SQLite rows it is copied from (a rowid range of ``source``),
    the Postgres columns, and ``shape`` turning a source row into target rows."""

    def __init__(self, name: str, source: str, select: str, columns: Sequence[str], *,
                 every: int = 500_000, shape: Callable[..., Any] | None = None,
                 whole: bool = False) -> None:
        self.name, self.source, self.select = name, source, select
        self.columns, self.every, self.shape, self.whole = tuple(columns), every, shape, whole


def _cells(context: dict[str, Any], row: Sequence[Any]) -> Iterator[tuple]:
    """A ``value_cells`` row as one row per value: a cell, or a packed record's values."""
    src, field, layout, value, currency, cite = row
    positions = context["layouts"][layout]
    numeric = context["numeric"]
    if field is not None:
        kind = positions[0][1]
        yield (src, field, kind, value, _number(value) if kind in numeric else None,
               currency, cite)
        return
    for (f, k), item in zip(positions, json.loads(value)):
        if item is not None and not isinstance(item, str):
            item = json.dumps(item, separators=(",", ":"))
        yield (src, f, k, item, _number(item) if k in numeric else None, None, None)


def _link(context: dict[str, Any], row: Sequence[Any]) -> Iterator[tuple]:
    row_no, row = row[0], row[1:]
    plain = row[:len(_LINK_PLAIN)]
    slots = [s for s in row[len(_LINK_PLAIN):len(_LINK_PLAIN) + store_ddl.LINK_SLOTS]
             if s is not None]
    yield (row_no, *plain, slots, *row[len(_LINK_PLAIN) + store_ddl.LINK_SLOTS:], None, None)


def _slot_columns() -> str:
    return ", ".join(f"s{k}" for k in range(store_ddl.LINK_SLOTS))


TABLES: dict[str, Table] = {t.name: t for t in (
    Table("objects", "objects",
          "SELECT rowid, object_id, object_type, binding_id, native_key, audience_key, "
          "first_recorded_at, missing_from_source_since FROM objects",
          ("row_no", "object_id", "object_type", "binding_id", "native_key", "audience_key",
           "first_recorded_at", "missing_from_source_since")),
    Table("object_removals", "object_removals",
          "SELECT object_id, basis, since, until, replaced_basis, replaced_by "
          "FROM object_removals",
          ("object_id", "basis", "since", "until", "replaced_basis", "replaced_by")),
    Table("object_versions", "object_versions",
          "SELECT rowid, object_id, l1_handle, digest, lineage, markings, recorded_at, "
          "superseded_at FROM object_versions",
          ("ver", "object_id", "l1_handle", "digest", "lineage", "markings", "recorded_at",
           "superseded_at")),
    Table("value_fields", "value_fields",
          "SELECT field, property, field_path FROM value_fields",
          ("field", "property", "field_path"), whole=True),
    Table("value_kinds", "value_kinds",
          f"SELECT kind, {', '.join(store_ddl.KIND_COLUMNS)} FROM value_kinds",
          ("kind", *store_ddl.KIND_COLUMNS), whole=True),
    Table("value_sources", "value_sources",
          "SELECT src, ver, binding_id, digest, lineage, recorded_at, superseded_at "
          "FROM value_sources",
          ("src", "ver", "binding_id", "digest", "lineage", "recorded_at", "superseded_at"),
          every=1_000_000),
    Table("identifiers", "identifiers",
          "SELECT identifier_kind, normalized, raw, role, object_id, property, l1_handle, "
          "field_path, lineage, markings, recorded_at, superseded_at FROM identifiers",
          ("identifier_kind", "normalized", "raw", "role", "object_id", "property",
           "l1_handle", "field_path", "lineage", "markings", "recorded_at", "superseded_at")),
    Table("events", "events",
          "SELECT event_id, event_type, time_basis, time, window_start, window_end, "
          "l1_handle, field_path, digest, lineage, markings, recorded_at, superseded_at "
          "FROM events",
          ("event_id", "event_type", "time_basis", "time", "window_start", "window_end",
           "l1_handle", "field_path", "digest", "lineage", "markings", "recorded_at",
           "superseded_at")),
    Table("event_objects", "event_objects",
          "SELECT event_id, object_id, qualifier FROM event_objects",
          ("event_id", "object_id", "qualifier"), every=1_000_000),
    Table("link_skeletons", "link_skeletons", "SELECT id, text FROM link_skeletons",
          ("id", "text")),
    Table("link_texts", "link_texts", "SELECT id, text FROM link_texts", ("id", "text"),
          every=1_000_000),
    Table("link_rows", "link_rows",
          f"SELECT rowid, {', '.join(_LINK_PLAIN)}, {_slot_columns()}, "
          f"{', '.join(_LINK_TAIL)} FROM link_rows",
          ("row_no", *_LINK_PLAIN, "slots", *_LINK_TAIL, "reason", "evidence_handles"),
          shape=_link),
    Table("text_fields", "text_fields",
          "SELECT field_id, object_id, version_handle, citation, field_path, document_kind, "
          "sender_class, markings, text_digest FROM text_fields",
          ("field_id", "object_id", "version_handle", "citation", "field_path",
           "document_kind", "sender_class", "markings", "text_digest")),
    Table("text_chunks", "text_chunks", "SELECT chunk_id, field_id, start, body FROM "
          "text_chunks", ("chunk_id", "field_id", "start", "body"), every=250_000),
    Table("value_cells", "value_cells",
          "SELECT src, field, layout, value, currency, currency_citation FROM value_cells",
          ("src", "field", "kind", "value", "value_num", "currency", "currency_citation"),
          every=1_000_000, shape=_cells),
)}
ORDER = tuple(TABLES)
# Tables a load may leave for later: the document text chunks (about 20 GB on gen 11),
# read from the SQLite generation meanwhile, as the word index is.
DEFERRABLE = ("text_chunks",)
# Sequences a delta draws new row numbers from: past every loaded one.
SEQUENCES = {"objects": ("objects_row_no", "row_no"),
             "link_rows": ("link_rows_row_no", "row_no"),
             "object_versions": ("object_versions_ver", "ver"),
             "value_fields": ("value_fields_field", "field"),
             "value_kinds": ("value_kinds_kind", "kind"),
             "value_sources": ("value_sources_src", "src"),
             "link_texts": ("link_texts_id", "id"),
             "link_skeletons": ("link_skeletons_id", "id"),
             "text_fields": ("text_fields_id", "field_id"),
             "text_chunks": ("text_chunks_id", "chunk_id")}


class DiskFloor(RuntimeError):
    """The database's file system fell below the free space the load must leave."""


class GenerationChanged(RuntimeError):
    """The store file is no longer the generation the load started from."""


def _number(value: Any) -> str | None:
    if value is None:
        return None
    try:
        from decimal import Decimal
        number = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return str(number) if number.is_finite() else None


def _context(path: str) -> dict[str, Any]:
    """What a range worker needs to shape rows: the layouts' (field, kind) positions and
    the numeric kinds."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        layouts: dict[int, list[tuple[int, int]]] = {}
        for layout, _pos, field, kind in conn.execute(
                "SELECT layout, pos, field, kind FROM value_layout_fields ORDER BY layout, pos"):
            layouts.setdefault(layout, []).append((field, kind))
        numeric = {row[0] for row in conn.execute(
            "SELECT kind FROM value_kinds WHERE value_type IN "
            f"({', '.join('?' * len(NUMERIC))})", NUMERIC)}
    finally:
        conn.close()
    return {"layouts": layouts, "numeric": numeric}


_WORKER_CONTEXT: dict[tuple, dict[str, Any]] = {}


def free_path(dsn: str) -> str | None:
    """A readable directory on the database's file system: ``PLX_PG_DATA_DIR``, else the
    server's data directory or its nearest readable parent."""
    folder = os.environ.get("PLX_PG_DATA_DIR")
    if folder:
        return folder
    try:
        import psycopg
        with psycopg.connect(dsn) as pg:
            data = Path(pg.execute("SHOW data_directory").fetchone()[0])
    except Exception:  # noqa: BLE001 - not superuser or no server: no floor
        return None
    for candidate in (data, *data.parents):
        try:
            shutil.disk_usage(candidate)
            return str(candidate)
        except OSError:
            continue
    return None


def state_of(store: str | Path) -> Path | None:
    """The state directory whose projection holds ``store`` (``<state>/projection/...``),
    None for a store of no state directory."""
    for parent in Path(store).resolve().parents:
        if parent.name == "projection" and (parent / "CURRENT").is_file():
            return parent.parent
    return None


def build_floor(folder: str | None, min_free: int, state_dir: str | Path | None
                ) -> tuple[str | None, int, dict[str, Any] | None]:
    """(the folder measured, the floor, the reserve): ``min_free``, raised to the next
    projection build's room of ``state_dir`` (``operating.host.build_reserve``) when the
    database's file system is the state directory's, or cannot be told (``folder`` None:
    the state directory's is measured)."""
    if state_dir is None:
        return folder, min_free, None
    # Builds retired (``PLX_BUILDS_OFF=1``, the ingest's "builds": "off"): no projection build
    # will need room, so only ``min_free`` holds.
    if os.environ.get("PLX_BUILDS_OFF") == "1":
        return folder, min_free, None
    try:
        shared = folder is None or os.stat(folder).st_dev == os.stat(state_dir).st_dev
    except OSError:
        shared = True
    if not shared:
        return folder, min_free, None
    from operating.host import build_reserve
    reserve = build_reserve(state_dir)
    return folder or str(state_dir), max(min_free, reserve["reserve_bytes"]), reserve


def _free_bytes(folder: str | None) -> int | None:
    return shutil.disk_usage(folder).free if folder else None


def _clean(value: Any) -> Any:
    # Postgres text holds no NUL; the SQLite store may (a mail body's field). Counted.
    return value.replace("\x00", "") if isinstance(value, str) and "\x00" in value else value


def _load_range(path: str, dsn: str, name: str, low: int | None, high: int | None,
                floor: tuple[str | None, int]) -> tuple[int, int]:
    """Copy one rowid range of ``name``; returns (rows, NULs dropped). Already recorded:
    (-1, 0)."""
    import psycopg

    folder, min_free = floor
    table = TABLES[name]
    key = ("" if low is None else str(low), "" if high is None else str(high))
    with psycopg.connect(dsn) as pg, pg.cursor() as cur:
        cur.execute("SET synchronous_commit = off")
        cur.execute(f"SELECT 1 FROM {SCHEMA}.load_ranges WHERE table_name = %s AND low = %s "
                    "AND high = %s", (name, *key))
        if cur.fetchone() is not None:
            return -1, 0
        free = _free_bytes(folder)
        if min_free and free is not None and free < min_free:
            raise DiskFloor(f"{free} bytes free, below {min_free}")
        held = os.stat(path)
        identity = (path, held.st_ino, held.st_mtime_ns)   # a file at a reused path differs
        if table.shape is not None and identity not in _WORKER_CONTEXT:
            _WORKER_CONTEXT.clear()
            _WORKER_CONTEXT[identity] = _context(path)
        context = _WORKER_CONTEXT.get(identity, {})
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("PRAGMA cache_size = -131072")
        # the file at ``path`` must still be the generation the load records (a new
        # generation published, or this one pruned, mid-load: stop; resume with --replace)
        cur.execute(f"SELECT value->>'content_digest' FROM {SCHEMA}.load_state "
                    "WHERE name = 'generation'")
        expected = (cur.fetchone() or (None,))[0]
        found = conn.execute("SELECT value FROM store_meta WHERE key = 'content_digest'"
                             ).fetchone()
        if (found[0] if found else None) != expected:
            conn.close()
            raise GenerationChanged(f"{path} holds {found and found[0]}, the load {expected}")
        where, args = [], []
        if low is not None:
            where.append("rowid >= ?")
            args.append(low)
        if high is not None:
            where.append("rowid < ?")
            args.append(high)
        sql = table.select + (" WHERE " + " AND ".join(where) if where else "")
        count = nuls = 0
        try:
            with cur.copy(f"COPY {SCHEMA}.{name} ({', '.join(table.columns)}) "
                          "FROM STDIN") as copy:
                for row in conn.execute(sql, args):
                    for out in (table.shape(context, row) if table.shape else (row,)):
                        if any(isinstance(v, str) and "\x00" in v for v in out):
                            out = tuple(_clean(v) for v in out)
                            nuls += 1
                        copy.write_row(out)
                        count += 1
        finally:
            conn.close()
        cur.execute(f"INSERT INTO {SCHEMA}.load_ranges (table_name, low, high, rows) "
                    "VALUES (%s, %s, %s, %s)", (name, *key, count))
    return count, nuls


def _ranges(path: str, table: Table) -> list[tuple[int | None, int | None]]:
    if table.whole:
        return [(None, None)]
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        top = conn.execute(f"SELECT max(rowid) FROM {table.source}").fetchone()[0] or 0
    finally:
        conn.close()
    cuts = list(range(table.every + 1, top + 1, table.every))
    bounds: list[int | None] = [None, *cuts, None]
    return list(zip(bounds[:-1], bounds[1:]))


class _NoPool:
    def __enter__(self) -> "_NoPool":
        return self

    def __exit__(self, *_: Any) -> None:
        pass


def _state(cur: Any, name: str) -> Any:
    cur.execute(f"SELECT value FROM {SCHEMA}.load_state WHERE name = %s", (name,))
    row = cur.fetchone()
    return row[0] if row else None


def _put(cur: Any, name: str, value: Any) -> None:
    cur.execute(f"INSERT INTO {SCHEMA}.load_state (name, value) VALUES (%s, %s) ON CONFLICT "
                "(name) DO UPDATE SET value = excluded.value, updated_at = now()",
                (name, json.dumps(value)))


def _drop_indexes(cur: Any, name: str) -> list[str]:
    """Drop ``name``'s indexes and primary key; returns the statements that rebuild them
    (kept in ``load_state`` so a resumed load still has them)."""
    kept = _state(cur, f"indexes:{name}")
    if kept is not None:
        statements = list(kept)
    else:
        cur.execute("SELECT conname, pg_get_constraintdef(c.oid) FROM pg_constraint c "
                    "WHERE conrelid = %s::regclass AND contype IN ('p', 'u')",
                    (f"{SCHEMA}.{name}",))
        constraints = cur.fetchall()
        cur.execute("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = "
                    "%s AND tablename = %s", (SCHEMA, name))
        owned = {c[0] for c in constraints}
        statements = [f"ALTER TABLE {SCHEMA}.{name} ADD CONSTRAINT {c} {d}"
                      for c, d in constraints]
        statements += [d for i, d in cur.fetchall() if i not in owned]
        _put(cur, f"indexes:{name}", statements)
    cur.execute("SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass "
                "AND contype IN ('p', 'u')", (f"{SCHEMA}.{name}",))
    for (constraint,) in cur.fetchall():
        cur.execute(f"ALTER TABLE {SCHEMA}.{name} DROP CONSTRAINT {constraint}")
    cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname = %s "
                "AND tablename = %s", (SCHEMA, name))
    for (index,) in cur.fetchall():
        cur.execute(f"DROP INDEX IF EXISTS {SCHEMA}.{index}")
    return statements


def _write_meta(cur: Any, meta: dict[str, str]) -> None:
    """The generation's ``store_meta`` (a delta layer later updates its freshness keys)."""
    cur.execute(f"DELETE FROM {SCHEMA}.store_meta")
    with cur.copy(f"COPY {SCHEMA}.store_meta (key, value) FROM STDIN") as copy:
        for key, value in sorted(meta.items()):
            copy.write_row((key, value))


def _settings(cur: Any) -> None:
    for key, value in SESSION_SETTINGS.items():
        cur.execute(f"SET {key} = '{value}'")


def load(path: str | Path, dsn: str, *, tables: Sequence[str] = ORDER, workers: int = 4,
         replace: bool = False, log: Any = sys.stderr, min_free: int = 0,
         deferred: Sequence[str] = (), state_dir: str | Path | None = None
         ) -> dict[str, Any]:
    """Load ``tables`` of the generation store at ``path`` into ``ontology.*`` (module
    docstring); returns rows and seconds per table. ``deferred``: tables left out for now
    (recorded in ``generation``; readers read them from the SQLite generation until a
    later load fills them, ``operating.pg.ontology_reader``). ``state_dir``: the state
    directory whose next build's room the load keeps free (``build_floor``)."""
    import psycopg

    path = str(path)
    unknown = [t for t in tables if t not in TABLES]
    if unknown:
        raise ValueError(f"unknown tables: {unknown}")
    unknown = [t for t in deferred if t not in DEFERRABLE]
    if unknown:
        raise ValueError(f"tables that cannot be deferred: {unknown}")
    tables = [t for t in ORDER if t in tables and t not in deferred]
    folder, min_free, reserve = build_floor(
        free_path(dsn) if min_free or state_dir is not None else None, min_free, state_dir)
    floor = (folder if min_free else None, min_free)
    if reserve is not None and log is not None:
        print(json.dumps({"floor_bytes": min_free, "build_reserve": reserve},
                         sort_keys=True), file=log, flush=True)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        meta = dict(conn.execute("SELECT key, value FROM store_meta "
                                 "WHERE key NOT IN ('content_digest_parts')"))
    generation = {"store": path, "content_digest": meta.get("content_digest"),
                  "built_at": meta.get("built_at"), "deferred": sorted(deferred)}
    report: dict[str, Any] = {}
    with psycopg.connect(dsn, autocommit=True) as pg, pg.cursor() as cur:
        held = _state(cur, "generation")
        # no delta layer applies while the tables are emptied (``ontology_delta``'s lock;
        # after this, the missing table records refuse every layer until the load is whole)
        cur.execute("SELECT pg_advisory_lock(hashtext('ontology_delta'))")
        if replace:
            for name in tables:
                cur.execute(f"TRUNCATE {SCHEMA}.{name}")
                cur.execute(f"DELETE FROM {SCHEMA}.load_ranges WHERE table_name = %s", (name,))
                cur.execute(f"DELETE FROM {SCHEMA}.load_state WHERE name = %s",
                            (f"table:{name}",))
            cur.execute(f"DELETE FROM {SCHEMA}.load_state WHERE name LIKE 'delta%%'")
        elif held and held.get("content_digest") != generation["content_digest"]:
            raise RuntimeError("ontology_holds_another_generation: load with --replace")
        if not replace and held:   # a table deferred before stays so until it is loaded
            generation["deferred"] = sorted(set(generation["deferred"])
                                            | set(held.get("deferred") or ()))
        _put(cur, "generation", {**(held or {}), **generation} if not replace else generation)
        _write_meta(cur, meta)
        cur.execute("SELECT pg_advisory_unlock(hashtext('ontology_delta'))")
    for name in tables:
        table = TABLES[name]
        started = time.monotonic()
        with psycopg.connect(dsn, autocommit=True) as pg, pg.cursor() as cur:
            done = _state(cur, f"table:{name}")
            if done is not None:
                report[name] = {**done, "skipped": "loaded"}
                print(f"ontology_load {name} already loaded {json.dumps(done)}", file=log,
                      flush=True)
                continue
            statements = _drop_indexes(cur, name)
        ranges = _ranges(path, table)
        rows = nuls = resumed = 0
        if workers <= 1:   # in this process (a small store: tests, a fixture)
            for low, high in ranges:
                count, dropped = _load_range(path, dsn, name, low, high, floor)
                resumed += count < 0
                rows += max(count, 0)
                nuls += dropped
            ranges_pool = None
        else:
            ranges_pool = ranges
        with (futures.ProcessPoolExecutor(
                max_workers=max(1, min(workers, len(ranges))),
                mp_context=multiprocessing.get_context("spawn")) if ranges_pool
              else _NoPool()) as pool:
            jobs = [pool.submit(_load_range, path, dsn, name, low, high, floor)
                    for low, high in ranges_pool or ()]
            try:
                for job in futures.as_completed(jobs):
                    count, dropped = job.result()
                    if count < 0:
                        resumed += 1
                    else:
                        rows += count
                        nuls += dropped
            except BaseException:
                for job in jobs:
                    job.cancel()
                if isinstance(pool, futures.ProcessPoolExecutor):
                    for process in list(getattr(pool, "_processes", {}).values()):
                        process.terminate()   # their open range rolls back
                raise
        loaded = time.monotonic()
        with psycopg.connect(dsn, autocommit=True) as pg, pg.cursor() as cur:
            _settings(cur)
            cur.execute(f"SELECT coalesce(sum(rows), 0) FROM {SCHEMA}.load_ranges "
                        "WHERE table_name = %s", (name,))
            total = int(cur.fetchone()[0])
            for statement in statements:
                cur.execute(statement)
            # built: the kept definitions (schema-qualified) are no longer needed
            cur.execute(f"DELETE FROM {SCHEMA}.load_state WHERE name = %s", (f"indexes:{name}",))
            cur.execute(f"ANALYZE {SCHEMA}.{name}")
            if name in SEQUENCES:
                sequence, column = SEQUENCES[name]
                cur.execute(f"SELECT setval('{SCHEMA}.{sequence}', greatest(coalesce("
                            f"(SELECT max({column}) FROM {SCHEMA}.{name}), 0), 1))")
            report[name] = {"rows": total, "rows_this_run": rows,
                            "ranges": len(ranges), "ranges_resumed": resumed,
                            "nul_rows": nuls, "copy_seconds": round(loaded - started, 1),
                            "index_seconds": round(time.monotonic() - loaded, 1)}
            cur.execute("SELECT pg_total_relation_size(%s::regclass)", (f"{SCHEMA}.{name}",))
            report[name]["bytes"] = int(cur.fetchone()[0])
            _put(cur, f"table:{name}", report[name])
        print(f"ontology_load {name} {json.dumps(report[name])}", file=log, flush=True)
    with psycopg.connect(dsn, autocommit=True) as pg, pg.cursor() as cur:
        generation["deferred"] = sorted(set(generation["deferred"]) - set(tables))
        # a deferred table's rows a delta writes meanwhile take numbers past the
        # generation's, so the later load of the generation's rows never collides
        for name in generation["deferred"]:
            if name in SEQUENCES:
                sequence, _column = SEQUENCES[name]
                with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
                    top = conn.execute(f"SELECT max(rowid) FROM {TABLES[name].source}"
                                       ).fetchone()[0] or 0
                cur.execute(f"SELECT setval('{SCHEMA}.{sequence}', greatest(%s, (SELECT "
                            f"last_value FROM {SCHEMA}.{sequence})))", (top,))
        _put(cur, "generation", {**generation, "tables": {
            name: _state(cur, f"table:{name}") for name in ORDER}})
    return report


def main(argv: Sequence[str] | None = None) -> int:
    from operating.pg import config

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("store")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--tables", nargs="*", default=list(ORDER))
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--min-free-gb", type=float, default=0.0)
    parser.add_argument("--no-build-reserve", action="store_true",
                        help="keep only --min-free-gb, not the next projection build's room")
    parser.add_argument("--defer", nargs="*", default=[], choices=DEFERRABLE)
    args = parser.parse_args(argv)
    dsn = args.dsn or config.dsn()
    import signal

    def stop(*_: Any) -> None:   # a TERM stops the pool too: no range runs on orphaned
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        load(args.store, dsn, tables=args.tables, workers=args.workers,
             replace=args.replace, min_free=int(args.min_free_gb * 1024 ** 3),
             deferred=args.defer,
             state_dir=None if args.no_build_reserve else state_of(args.store))
    except DiskFloor as low:
        print(f"ontology_load stopped at the disk floor: {low}; run again to resume",
              file=sys.stderr, flush=True)
        return 3
    except GenerationChanged as moved:
        print(f"ontology_load stopped: the generation changed ({moved}); load the served "
              "generation with --replace", file=sys.stderr, flush=True)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
