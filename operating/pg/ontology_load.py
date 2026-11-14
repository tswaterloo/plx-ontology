"""Bulk load of a projection generation into the Postgres ontology (``ontology.*``,
migrations ``0006_ontology`` and ``0007_ontology_compact``), beside the SQLite store that
keeps serving.

``python3 -m operating.pg.ontology_load STORE [--workers N] [--tables ...] [--replace]
[--min-free-gb G]``

Every table a read tool uses is loaded: objects, removals, versions, the values (their
sources, fields, kinds and one row per value: a packed record unpacked), links (packed:
skeletons and slot texts), identifiers, events and their objects, and the document text
index (fields and zlib chunks), and the values' word index (migration 0010,
``value_words``: the generation's packed sources and a GIN index over the value cells;
``chunk_words``: each text chunk's words, cut by SQLite's FTS5 tokenizer, ``docs/pg-cutover.md``). Each
table is read from the generation (read-only) in rowid ranges by ``--workers`` processes,
each COPYing its range over its own connection.

Resumable per table and per range: a range's rows and its ``ontology.load_ranges`` row
commit in one transaction, so a load stopped anywhere (killed, the disk floor) is run
again with the same arguments and copies only the ranges not yet recorded; a table whose
ranges are all in and whose indexes are built is recorded under ``table:<name>`` in
``ontology.load_state`` and skipped. A table's secondary indexes and primary key are
dropped before its first range and built once after its last (one sort each); their
definitions are kept in ``load_state`` (``indexes:<name>``) so a resumed load builds them.
``--replace`` empties the tables named and their records first.

A table the load deferred (``DEFERRABLE``) is filled later on the ontology already served
and taking delta layers: a live fill (``_fills``). Its primary key and btree indexes stay
(a layer upserts and deletes by them), only its GIN word index is dropped and built again
CONCURRENTLY; the generation's ``store_meta`` is not written again (the layers moved its
freshness keys); rows whose field or chunk a layer removed meanwhile are deleted after the
last range under the layers' lock (``_ORPHANS``); and no sequence moves back.

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
    # The values' word index (migration 0010, ``WORD_INDEX``): no rows of its own.
    Table("value_words", "value_cells", "", (), whole=True),
    # The document words (migration 0010): each chunk's words by FTS5's own tokenizer.
    Table("chunk_words", "text_chunks", "SELECT chunk_id, body FROM text_chunks",
          ("chunk_id", "words"), every=250_000),
)}
ORDER = tuple(TABLES)
# Tables a load may leave for later: the document text chunks (about 20 GB on gen 11),
# read from the SQLite generation meanwhile, as the document words always are; and the
# values' word index (hours to build: word search reads the SQLite generation meanwhile).
DEFERRABLE = ("text_chunks", "value_words", "chunk_words")
# Tables a whole ontology is read and takes delta layers without: until the word index is
# built, word search reads the SQLite generation (``ontology_reader``; with no SQLite
# generation served, a word query raises: ``ontology_reader.pg_only``).
OPTIONAL = ("value_words", "chunk_words")
# The word index's steps: what Postgres-only serving needs beside the tables.
WORDS = ("value_words", "chunk_words")
# The value words: a GIN index of ``ontology.sq_words`` over the value cells (migration
# 0010's ``value_words``), built as the ``value_words`` step (Postgres 17 builds it on one
# core: ~15 s per 600k cells measured under load). Its function is always ``ontology``'s,
# so the index moves with its table when a generation loaded beside is swapped in.
# The GIN indexes the schema declares on a deferred table (0010): what a live fill builds
# when the table holds none (a failed build's invalid index was dropped by hand), so the
# fill never records the table loaded without its word index.
DECLARED_GIN = {"chunk_words": ("CREATE INDEX chunk_words_words ON ontology.chunk_words "
                                "USING gin (words)",)}
WORD_INDEX = ("CREATE INDEX IF NOT EXISTS value_cells_words ON {schema}.value_cells "
              "USING gin (ontology.sq_words(value)) WHERE value_num IS NULL "
              "AND value IS NOT NULL")
# The statistics target of the words (``ANALYZE`` keeps ten times as many frequent words).
WORD_STATISTICS = 1000
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
                rows: Any = conn.execute(sql, args)
                if name == "chunk_words":   # the words of a batch of chunks at a time
                    from operating.pg.words import chunk_word_rows
                    rows = chunk_word_rows(rows)
                for row in rows:
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


def _drop_indexes(cur: Any, name: str, *, live: bool = False) -> list[str]:
    """Drop ``name``'s indexes and primary key; returns the statements that rebuild them
    (kept in ``load_state`` so a resumed load still has them).

    ``live``: the table is filled on an ontology already served and taking delta layers
    (``_fills``). Its primary key and unique constraints stay (a layer's upsert conflicts
    on them, and adding one back locks the table), and so do its btree indexes (a layer's
    delete finds its rows by them); only its GIN indexes go, rebuilt CONCURRENTLY after
    the last range (``_rebuild_live``): a word index the readers check for validity
    (``ontology_reader._WORDS_SQL``), so the words are read from the SQLite generation
    until it is whole."""
    if live:
        kept = _state(cur, f"indexes:{name}")
        if kept is None:
            cur.execute("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s "
                        "AND tablename = %s AND indexdef LIKE '%% USING gin %%'",
                        (SCHEMA, name))
            kept = [d for _i, d in cur.fetchall()] or list(DECLARED_GIN.get(name, ()))
            _put(cur, f"indexes:{name}", kept)
        if any(not str(d).startswith("CREATE ") for d in kept):
            # kept by a full load that dropped the key too: not a live fill's to finish
            raise RuntimeError(f"index_state_of_a_full_load:{name}: finish it with the "
                               "load that began it, or clear indexes:{name}")
        for statement in kept:
            cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {SCHEMA}.{_index_name(statement)}")
        return list(kept)
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
        # a partitioned table's index reads ``ON ONLY`` (its own, no partition's); made
        # again over the parent it must build every partition's too
        statements += [d.replace(" ON ONLY ", " ON ", 1) for i, d in cur.fetchall()
                       if i not in owned]
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


def _index_name(statement: str) -> str:
    """The index a ``pg_indexes.indexdef`` statement creates (unqualified)."""
    words = statement.split()
    at = words.index("INDEX") + 1
    if words[at] == "CONCURRENTLY":
        at += 1
    if words[at:at + 3] == ["IF", "NOT", "EXISTS"]:
        at += 3
    return words[at].rsplit(".", 1)[-1]


def _rebuild_live(cur: Any, statements: Sequence[str], *, locked: bool = False) -> None:
    """Build a live fill's dropped indexes CONCURRENTLY (readers and layers go on); an
    invalid one a stopped build left is dropped first, else IF NOT EXISTS would keep it.

    ``locked``: a plain build instead, under the table's SHARE lock (readers go on, a
    layer's writes to the table wait for it). A concurrent build's validation sorts one
    entry per indexed word and row into temp files (a full ``chunk_words`` needs ~100 GB,
    past ``temp_file_limit`` and the free disk: it failed so on 11-13); a plain build
    sorts nothing to disk."""
    for statement in statements:
        index = _index_name(statement)
        cur.execute("SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = "
                    "i.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = %s AND c.relname = %s", (SCHEMA, index))
        row = cur.fetchone()
        if row is not None and row[0]:
            continue
        if row is not None:
            cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {SCHEMA}.{index}")
        if locked:
            cur.execute(statement)
            continue
        cur.execute(statement.replace("CREATE INDEX ", "CREATE INDEX CONCURRENTLY ", 1)
                    .replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX CONCURRENTLY ", 1))


# Rows of a live fill whose owner a delta layer removed after the generation was loaded
# (the layer deleted the field, or the chunk, and found none of the deferred rows to
# delete with it): deleted after the last range, under the layers' lock, so no chunk of a
# removed field (and its words) comes back.
_ORPHANS = {
    "text_chunks": ("DELETE FROM {schema}.text_chunks c WHERE c.chunk_id > %s AND "
                    "c.chunk_id <= %s AND NOT EXISTS (SELECT 1 FROM {schema}.text_fields f "
                    "WHERE f.field_id = c.field_id)"),
    "chunk_words": ("DELETE FROM {schema}.chunk_words w WHERE w.chunk_id > %s AND "
                    "w.chunk_id <= %s AND NOT EXISTS (SELECT 1 FROM {schema}.text_chunks c "
                    "WHERE c.chunk_id = w.chunk_id)"),
}
# The sweep's row numbers per locked step: the layers wait at most one step, never the
# whole sweep.
ORPHAN_STEP = 500_000
# Chunks whose words are made from their Postgres body per batch (``_complete_words``).
WORD_BATCH = 5_000
_LOCK = "SELECT pg_advisory_lock(hashtext('ontology_delta'))"
_UNLOCK = "SELECT pg_advisory_unlock(hashtext('ontology_delta'))"


def _sweep_orphans(cur: Any, name: str, top: int) -> int:
    """Delete ``name``'s rows of a live fill whose owner is gone (``_ORPHANS``), in steps
    of ``ORPHAN_STEP`` row numbers, each under the layers' lock."""
    deleted = 0
    for low in range(0, top, ORPHAN_STEP):
        cur.execute(_LOCK)
        try:
            cur.execute(_ORPHANS[name].format(schema=SCHEMA), (low, min(low + ORPHAN_STEP, top)))
            deleted += cur.rowcount
        finally:
            cur.execute(_UNLOCK)
    return deleted


def _add_words(cur: Any, after: int) -> tuple[int, int]:
    """Words for every chunk numbered past ``after`` that has none, made from its Postgres
    body (``words.chunk_word_rows``, FTS5's tokenizer, as a layer's): (words added, the
    last chunk looked at). A chunk a layer gave words meanwhile keeps the layer's."""
    from operating.pg.words import chunk_word_rows
    added = 0
    cur.execute("CREATE TEMP TABLE IF NOT EXISTS fill_words (chunk_id bigint, words tsvector)")
    while True:
        cur.execute(f"SELECT c.chunk_id, c.body FROM {SCHEMA}.text_chunks c WHERE "
                    f"c.chunk_id > %s AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.chunk_words w "
                    "WHERE w.chunk_id = c.chunk_id) ORDER BY c.chunk_id LIMIT %s",
                    (after, WORD_BATCH))
        rows = cur.fetchall()
        if not rows:
            return added, after
        after = rows[-1][0]
        cur.execute("TRUNCATE fill_words")
        with cur.copy("COPY fill_words (chunk_id, words) FROM STDIN") as copy:
            for row in chunk_word_rows(rows):
                copy.write_row(row)
        cur.execute(f"INSERT INTO {SCHEMA}.chunk_words (chunk_id, words) SELECT chunk_id, "
                    "words FROM fill_words ON CONFLICT (chunk_id) DO NOTHING")
        added += cur.rowcount


def _complete_words(cur: Any) -> int:
    """Every chunk Postgres holds has its words before ``chunk_words`` is recorded loaded
    (recording it moves document word search to Postgres: a chunk without words would be
    found by no word). The generation's chunks got theirs from the SQLite generation; the
    rest (a layer's or a migration's) get them from their Postgres body: first unlocked,
    then, under the layers' lock (which every writer of chunks takes), the chunks numbered
    past the highest one there was when the first pass began. Raises when a chunk is still
    without words."""
    cur.execute(_LOCK)
    try:   # no writer is mid-way: every chunk up to this number is committed
        cur.execute(f"SELECT coalesce(max(chunk_id), 0) FROM {SCHEMA}.text_chunks")
        mark = int(cur.fetchone()[0])
    finally:
        cur.execute(_UNLOCK)
    added, _last = _add_words(cur, 0)
    cur.execute(_LOCK)
    try:
        more, _last = _add_words(cur, mark)
        cur.execute(f"SELECT count(*) FROM {SCHEMA}.text_chunks c WHERE c.chunk_id > %s AND "
                    f"NOT EXISTS (SELECT 1 FROM {SCHEMA}.chunk_words w WHERE "
                    "w.chunk_id = c.chunk_id)", (mark,))
        left = int(cur.fetchone()[0])
    finally:
        cur.execute(_UNLOCK)
    # and none up to the mark (one scan, unlocked: such a chunk never loses its words)
    cur.execute(f"SELECT count(*) FROM {SCHEMA}.text_chunks c WHERE c.chunk_id <= %s AND "
                f"NOT EXISTS (SELECT 1 FROM {SCHEMA}.chunk_words w WHERE "
                "w.chunk_id = c.chunk_id)", (mark,))
    left += int(cur.fetchone()[0])
    if left:
        raise RuntimeError(f"chunks_without_words:{left}")
    return added + more


def _fills(held: Any, tables: Sequence[str], replace: bool) -> frozenset[str]:
    """The tables of this load that fill a deferred table of the generation already
    loaded (``held``): live, beside readers and delta layers (``_drop_indexes``)."""
    if replace or not held:
        return frozenset()
    return frozenset(t for t in tables if t in (held.get("deferred") or ()))


def _ranges_top(path: str, table: Table) -> int:
    """The generation's highest row number of ``table``'s source: the rows a fill copied
    are at or below it, a layer's are past it (``SEQUENCES``)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(f"SELECT max(rowid) FROM {table.source}").fetchone()[0] or 0
    finally:
        conn.close()


def _write_meta(cur: Any, meta: dict[str, str]) -> None:
    """The generation's ``store_meta`` (a delta layer later updates its freshness keys)."""
    cur.execute(f"DELETE FROM {SCHEMA}.store_meta")
    with cur.copy(f"COPY {SCHEMA}.store_meta (key, value) FROM STDIN") as copy:
        for key, value in sorted(meta.items()):
            copy.write_row((key, value))


def _settings(cur: Any) -> None:
    for key, value in SESSION_SETTINGS.items():
        cur.execute(f"SET {key} = '{value}'")


def _word_index(path: str, dsn: str, started: float, *, live: bool = False,
                locked: bool = False) -> dict[str, Any]:
    """The ``value_words`` step: the generation's packed sources (``packed_sources``: a
    source holding a packed record, whose values SQLite never word-indexes) and the value
    cells' word index, built when missing (a value_cells load rebuilds it with the table's
    other indexes). Once per generation (``table:value_words``). ``live``: the ontology is
    served and takes layers (``_fills``), so the index is built CONCURRENTLY (a plain build
    would hold the layers' writes to the value cells for its whole run)."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as pg, pg.cursor() as cur:
        done = _state(cur, "table:value_words")
        cur.execute("SELECT 1 FROM pg_indexes WHERE schemaname = %s "
                    "AND indexname = 'value_cells_words'", (SCHEMA,))
        held = cur.fetchone() is not None
        if done is not None and held:
            return {**done, "skipped": "loaded"}
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            packed = [row[0] for row in conn.execute(
                "SELECT DISTINCT src FROM value_cells WHERE field IS NULL")]
            top = conn.execute("SELECT coalesce(max(src), 0) FROM value_sources").fetchone()[0]
        finally:
            conn.close()
        with pg.transaction():
            # the generation's; a delta's (numbered past them) are its own
            cur.execute(f"DELETE FROM {SCHEMA}.packed_sources WHERE src <= %s", (top,))
            with cur.copy(f"COPY {SCHEMA}.packed_sources (src) FROM STDIN") as copy:
                for src in packed:
                    copy.write_row((src,))
        if live:
            # a source a layer removed since the generation was loaded does not come back
            cur.execute(_LOCK)
            try:
                cur.execute(f"DELETE FROM {SCHEMA}.packed_sources p WHERE p.src <= %s AND NOT "
                            f"EXISTS (SELECT 1 FROM {SCHEMA}.value_sources s WHERE "
                            "s.src = p.src)", (top,))
            finally:
                cur.execute(_UNLOCK)
            _settings(cur)
            _rebuild_live(cur, [WORD_INDEX.format(schema=SCHEMA)], locked=locked)
        elif not held:
            _settings(cur)
            cur.execute(WORD_INDEX.format(schema=SCHEMA))
        cur.execute(f"ANALYZE {SCHEMA}.packed_sources")
        # the words' statistics (the planner's and the readers' estimate of a word's
        # matches, ``ontology_reader.WordWindows``): kept for 10,000 frequent words
        cur.execute(f"ALTER INDEX {SCHEMA}.value_cells_words ALTER COLUMN 1 "
                    f"SET STATISTICS {WORD_STATISTICS}")
        cur.execute(f"ANALYZE {SCHEMA}.value_cells")
        cur.execute("SELECT pg_relation_size(%s::regclass)", (f"{SCHEMA}.value_cells_words",))
        out = {"rows": len(packed), "index_seconds": round(time.monotonic() - started, 1),
               "bytes": int(cur.fetchone()[0])}
        _put(cur, "table:value_words", out)
    return out


def load(path: str | Path, dsn: str, *, tables: Sequence[str] = ORDER, workers: int = 4,
         replace: bool = False, log: Any = sys.stderr, min_free: int = 0,
         deferred: Sequence[str] = (), state_dir: str | Path | None = None,
         steps_digest: str | None = None, locked_index: bool = False) -> dict[str, Any]:
    """Load ``tables`` of the generation store at ``path`` into ``ontology.*`` (module
    docstring); returns rows and seconds per table. ``deferred``: tables left out for now
    (recorded in ``generation``; readers read them from the SQLite generation until a
    later load fills them, ``operating.pg.ontology_reader``). ``state_dir``: the state
    directory whose next build's room the load keeps free (``build_floor``).
    ``steps_digest``: the build steps' code that built a generation that does not record it
    (``store_meta`` ``steps_digest``; without either the deploy check refuses until
    ``seed-steps``). ``locked_index``: a live fill builds its word indexes plainly, under
    the table's lock, not CONCURRENTLY (``_rebuild_live``)."""
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
    if steps_digest is not None and "steps_digest" not in meta:
        meta["steps_digest"] = steps_digest
    generation = {"store": path, "content_digest": meta.get("content_digest"),
                  "built_at": meta.get("built_at"), "deferred": sorted(deferred)}
    report: dict[str, Any] = {}
    with psycopg.connect(dsn, autocommit=True) as pg, pg.cursor() as cur:
        held = _state(cur, "generation")
        # no delta layer applies while the tables are emptied (``ontology_delta``'s lock;
        # after this, the missing table records refuse every layer until the load is whole)
        cur.execute("SELECT pg_advisory_lock(hashtext('ontology_delta'))")
        if replace:
            # a deferred table is emptied too: rows of another generation (the document
            # words of chunk ids the new one numbers again) are never read as this one's
            for name in [*tables, *(t for t in ORDER if t in deferred)]:
                # value_words is a view: its rows are the packed sources
                cur.execute(f"TRUNCATE {SCHEMA}.packed_sources" if name == "value_words"
                            else f"TRUNCATE {SCHEMA}.{name}")
                cur.execute(f"DELETE FROM {SCHEMA}.load_ranges WHERE table_name = %s", (name,))
                cur.execute(f"DELETE FROM {SCHEMA}.load_state WHERE name = %s",
                            (f"table:{name}",))
            cur.execute(f"DELETE FROM {SCHEMA}.load_state WHERE name LIKE 'delta%%'")
        elif held and held.get("content_digest") != generation["content_digest"]:
            raise RuntimeError("ontology_holds_another_generation: load with --replace")
        # tables a load deferred, filled now on an ontology that is served and takes layers
        fills = _fills(held, tables, replace)
        if "chunk_words" in fills and "text_chunks" in (held or {}).get("deferred", ()) \
                and "text_chunks" not in tables:
            # its orphans are told by the chunks it indexes: those must be in first
            cur.execute("SELECT pg_advisory_unlock(hashtext('ontology_delta'))")
            raise ValueError("chunk_words_before_text_chunks: load text_chunks first")
        if not replace and held:   # a table deferred before stays so until it is loaded
            generation["deferred"] = sorted(set(generation["deferred"])
                                            | set(held.get("deferred") or ()))
        _put(cur, "generation", {**(held or {}), **generation} if not replace else generation)
        cur.execute(f"SELECT value FROM {SCHEMA}.store_meta WHERE key = 'content_digest'")
        written = cur.fetchone()
        if not replace and held and written is not None \
                and written[0] == generation["content_digest"]:
            # the generation's meta is in, and the layers applied since moved its freshness
            # keys (and migrations its digests): rewriting it would set them back
            pass
        else:
            if not replace and held:   # resuming: what migrations moved since the load stays
                cur.execute(f"SELECT key, value FROM {SCHEMA}.store_meta WHERE key IN "
                            "('schema_digest', 'steps_digest')")
                meta.update(dict(cur.fetchall()))
            _write_meta(cur, meta)
        cur.execute("SELECT pg_advisory_unlock(hashtext('ontology_delta'))")
    for name in tables:
        table = TABLES[name]
        started = time.monotonic()
        if name == "value_words":
            report[name] = _word_index(path, dsn, started, live=name in fills,
                                       locked=locked_index)
            print(f"ontology_load {name} {json.dumps(report[name])}", file=log, flush=True)
            continue
        with psycopg.connect(dsn, autocommit=True) as pg, pg.cursor() as cur:
            done = _state(cur, f"table:{name}")
            if done is not None:
                report[name] = {**done, "skipped": "loaded"}
                print(f"ontology_load {name} already loaded {json.dumps(done)}", file=log,
                      flush=True)
                continue
            statements = _drop_indexes(cur, name, live=name in fills)
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
            orphans = words_added = 0
            if name in fills:
                if name == "chunk_words":
                    words_added = _complete_words(cur)
                if name in _ORPHANS:
                    orphans = _sweep_orphans(cur, name, _ranges_top(path, table))
                _rebuild_live(cur, statements, locked=locked_index)
            else:
                for statement in statements:
                    cur.execute(statement)
            # built: the kept definitions (schema-qualified) are no longer needed
            cur.execute(f"DELETE FROM {SCHEMA}.load_state WHERE name = %s", (f"indexes:{name}",))
            if name == "chunk_words":
                cur.execute(f"ALTER TABLE {SCHEMA}.chunk_words ALTER COLUMN words "
                            f"SET STATISTICS {WORD_STATISTICS}")
            cur.execute(f"ANALYZE {SCHEMA}.{name}")
            if name in SEQUENCES:
                sequence, column = SEQUENCES[name]
                # a live fill never moves a sequence back (a layer's deleted rows took
                # numbers past the table's highest)
                held_last = (f", (SELECT last_value FROM {SCHEMA}.{sequence})"
                             if name in fills else "")
                cur.execute(f"SELECT setval('{SCHEMA}.{sequence}', greatest(coalesce("
                            f"(SELECT max({column}) FROM {SCHEMA}.{name}), 0), 1{held_last}))")
            report[name] = {"rows": total - orphans + words_added, "rows_this_run": rows,
                            "orphans_deleted": orphans, "live": name in fills,
                            "words_added": words_added,
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
    parser.add_argument("--locked-index", action="store_true",
                        help="a live fill builds its word indexes under the table's lock "
                             "(layers' writes to it wait), not CONCURRENTLY")
    parser.add_argument("--no-build-reserve", action="store_true",
                        help="keep only --min-free-gb, not the next projection build's room")
    parser.add_argument("--defer", nargs="*", default=[], choices=DEFERRABLE)
    parser.add_argument("--steps-ref", default=None,
                        help="the commit that built a generation recording no steps_digest")
    args = parser.parse_args(argv)
    dsn = args.dsn or config.dsn()
    import signal

    def stop(*_: Any) -> None:   # a TERM stops the pool too: no range runs on orphaned
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        steps = None
        if args.steps_ref:
            from ontology.step_migrate import steps_digest
            steps = steps_digest(args.steps_ref)
        load(args.store, dsn, tables=args.tables, workers=args.workers,
             replace=args.replace, min_free=int(args.min_free_gb * 1024 ** 3),
             locked_index=args.locked_index,
             deferred=args.defer,
             state_dir=None if args.no_build_reserve else state_of(args.store),
             steps_digest=steps)
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
