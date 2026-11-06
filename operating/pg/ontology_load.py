"""Bulk load of a projection generation into the Postgres ontology (``ontology.*``,
migration ``0006_ontology``), beside the SQLite store that keeps serving.

``python3 -m operating.pg.ontology_load STORE [--workers N] [--tables ...]
[--values-skip-prefix Rel] [--replace]``

Each table is read from the generation (read-only) in key ranges by ``--workers``
processes, each COPYing its rows over its own connection; the secondary indexes of a
table are dropped before its rows go in and built once after (one sort each, never an
insert per row into a growing index). The rows are the store's logical rows, column for
column (``ontology.store.StoreBuilder._INSERT``): the values view and the links view
unpacked from their packed layouts, so a read of either store is equal (``parity``).
``values.value_num`` / ``value_time`` are the value read as a number or a time when its
type is one.

``--values-skip-prefix``: object types whose values are not loaded (the raw Sage ledgers,
``Rel*``, are half the store's objects; disk is the bound). ``ontology.load_state``
``generation`` records the store, its content digest and what was loaded.

Stdlib and psycopg.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import io
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Iterable, Iterator, Sequence

from ontology import store as S
from ontology import store_ddl

TABLES = ("objects", "object_removals", "object_versions", "identifiers", "events",
          "event_objects", "links", "object_values")
NUMERIC = ("integer", "decimal", "money", "quantity")
TIMES = ("date", "timestamp")
# Rows of a table each worker reads and COPYs in one task (objects, for the values).
TASK_ROWS = 200_000
TASK_VALUE_OBJECTS = 20_000
# The key a table's ranges are cut on (``ontology.store._CHUNK_KEYS``'s).
_KEYS = {"objects": ("objects", "object_id"), "object_removals": ("object_removals", "object_id"),
         "object_versions": ("object_versions", "object_id"),
         "identifiers": ("identifiers", "object_id"), "events": ("events", "event_id"),
         "event_objects": ("event_objects", "event_id"), "links": ("link_rows", "link_id"),
         "object_values": ("objects", "object_id")}


def _columns(table: str) -> tuple[str, ...]:
    if table == "object_values":
        return tuple(store_ddl.VALUE_COLUMNS) + ("value_num", "value_time")
    return tuple(S.StoreBuilder._INSERT[table])


def _bounds(path: str, table: str, every: int) -> list[Any]:
    source, column = _KEYS[table]
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        found = conn.execute(
            f"SELECT k FROM (SELECT {column} AS k, row_number() OVER (ORDER BY {column}) AS n "
            f"FROM {source}) WHERE (n - 1) % ? = 0 AND n > 1 ORDER BY k", (every,)).fetchall()
    finally:
        conn.close()
    return [None, *sorted({row[0] for row in found}), None]


def _number(value: Any) -> str | None:
    if value is None:
        return None
    try:
        from decimal import Decimal, InvalidOperation
        number = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return str(number) if number.is_finite() else None


def _copy_text(value: Any) -> str:
    if value is None:
        return "\\N"
    text = value if isinstance(value, str) else str(value)
    return (text.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n")
            .replace("\r", "\\r"))


def _rows(conn: sqlite3.Connection, table: str, low: Any, high: Any,
          skip: Sequence[str]) -> Iterator[Sequence[Any]]:
    column = _KEYS[table][1] if table != "object_values" else "object_id"
    if table == "object_values":
        for batch in S._value_rows(conn, low, high):
            for row in batch:
                if skip and any(row[0].startswith(prefix) for prefix in skip):
                    continue
                kind = row[2]
                yield (*row, _number(row[3]) if kind in NUMERIC else None,
                       row[3] if kind in TIMES else None)
        return
    where, args = S._range(column, low, high)
    names = ", ".join(_columns(table))
    yield from conn.execute(f"SELECT {names} FROM {table}{where}", args)


class DiskFloor(RuntimeError):
    """The database's file system fell below the free space the load must leave."""


def _free_bytes(dsn: str) -> int | None:
    import shutil
    folder = os.environ.get("PLX_PG_DATA_DIR")
    return shutil.disk_usage(folder).free if folder else None


def _load_range(path: str, dsn: str, table: str, low: Any, high: Any,
                skip: Sequence[str], min_free: int = 0) -> int:
    import psycopg

    free = _free_bytes(dsn)
    if min_free and free is not None and free < min_free:
        raise DiskFloor(f"{free} bytes free, below {min_free}")

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA cache_size = -262144")
    count = 0
    try:
        with psycopg.connect(dsn) as pg, pg.cursor() as cur:
            cur.execute("SET synchronous_commit = off")
            with cur.copy(f"COPY ontology.{table} ({', '.join(_columns(table))}) "
                          "FROM STDIN") as copy:
                buffer = io.StringIO()
                for row in _rows(conn, table, low, high, skip):
                    buffer.write("\t".join(_copy_text(v) for v in row))
                    buffer.write("\n")
                    count += 1
                    if buffer.tell() > 4 << 20:
                        copy.write(buffer.getvalue())
                        buffer = io.StringIO()
                copy.write(buffer.getvalue())
    finally:
        conn.close()
    return count


def _secondary_indexes(cur: Any, table: str) -> list[tuple[str, str]]:
    cur.execute("SELECT i.indexname, i.indexdef FROM pg_indexes i WHERE i.schemaname = "
                "'ontology' AND i.tablename = %s AND i.indexname NOT IN (SELECT "
                "conname FROM pg_constraint WHERE contype IN ('p', 'u'))", (table,))
    return list(cur.fetchall())


def load(path: str | Path, dsn: str, *, tables: Sequence[str] = TABLES, workers: int = 4,
         skip: Sequence[str] = (), replace: bool = False, log: Any = sys.stderr,
         min_free: int = 0) -> dict[str, Any]:
    """Load ``tables`` of the generation store at ``path`` into ``ontology.*``; returns
    rows and seconds per table. ``min_free``: bytes the database's file system
    (``PLX_PG_DATA_DIR``) must keep free; a range is not loaded below it (``DiskFloor``)."""
    import psycopg

    path = str(path)
    report: dict[str, Any] = {}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        meta = dict(conn.execute("SELECT key, value FROM store_meta "
                                 "WHERE key NOT IN ('content_digest_parts')"))
    for table in tables:
        started = time.monotonic()
        with psycopg.connect(dsn, autocommit=True) as pg, pg.cursor() as cur:
            if replace:
                cur.execute(f"TRUNCATE ontology.{table}")
            indexes = _secondary_indexes(cur, table)
            for name, _ in indexes:
                cur.execute(f"DROP INDEX IF EXISTS ontology.{name}")
        every = TASK_VALUE_OBJECTS if table == "object_values" else TASK_ROWS
        bounds = _bounds(path, table, every)
        with futures.ProcessPoolExecutor(
                max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
            done = list(pool.map(_load_range, [path] * (len(bounds) - 1),
                                 [dsn] * (len(bounds) - 1), [table] * (len(bounds) - 1),
                                 bounds[:-1], bounds[1:], [tuple(skip)] * (len(bounds) - 1),
                                 [min_free] * (len(bounds) - 1)))
        loaded = time.monotonic()
        with psycopg.connect(dsn, autocommit=True) as pg, pg.cursor() as cur:
            cur.execute("SET maintenance_work_mem = '2GB'")
            for _, definition in indexes:
                cur.execute(definition)
            cur.execute(f"ANALYZE ontology.{table}")
        report[table] = {"rows": sum(done), "copy_seconds": round(loaded - started, 1),
                         "index_seconds": round(time.monotonic() - loaded, 1),
                         "ranges": len(bounds) - 1}
        print(f"ontology_load {table} {json.dumps(report[table])}", file=log, flush=True)
    with psycopg.connect(dsn, autocommit=True) as pg, pg.cursor() as cur:
        if replace:
            cur.execute("TRUNCATE ontology.store_meta")
        with cur.copy("COPY ontology.store_meta (key, value) FROM STDIN") as copy:
            for key, value in sorted(meta.items()):
                copy.write_row((key, value))
        cur.execute("INSERT INTO ontology.load_state (name, value) VALUES ('generation', %s) "
                    "ON CONFLICT (name) DO UPDATE SET value = excluded.value, updated_at = now()",
                    (json.dumps({"store": path, "content_digest": meta.get("content_digest"),
                                 "tables": report, "values_skipped": list(skip)}),))
    return report


def main(argv: Sequence[str] | None = None) -> int:
    from operating.pg import config

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("store")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--tables", nargs="*", default=list(TABLES))
    parser.add_argument("--values-skip-prefix", nargs="*", default=[])
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--min-free-gb", type=float, default=0.0)
    args = parser.parse_args(argv)
    dsn = args.dsn or config.dsn()
    load(args.store, dsn, tables=args.tables, workers=args.workers,
         skip=args.values_skip_prefix, replace=args.replace,
         min_free=int(args.min_free_gb * 1024 ** 3))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
