"""Build migration 0039's date index on a populated database, outside any deploy.

Migration 0039 creates ``ontology.value_cells_dates`` only while ``value_cells`` is empty.
The served database gets it from this command::

    python3 -m operating.pg.date_index status
    python3 -m operating.pg.date_index build      # CREATE INDEX CONCURRENTLY
    python3 -m operating.pg.date_index drop       # DROP INDEX CONCURRENTLY (the undo)

``build`` is idempotent: a valid index of the definition is skipped; an INVALID one (an
interrupted concurrent build) or one of another definition is dropped CONCURRENTLY and
built again, then checked valid (exit 1 when not). Value writes go on during the build.
Nothing a reader sees changes: the newest-first lists (``operating.fast_listing``) only
read faster.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Sequence

NAME = "value_cells_dates"
PREDICATE = "^[12][0-9]{3}-[01][0-9]-[0-3][0-9]"   # = fast_listing.DATE_SHAPE
DDL = (f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {NAME} ON ontology.value_cells "
       f"(field, value) WHERE value ~ '{PREDICATE}'")
_MEMORY = re.compile(r"^\d{1,5}(?:kB|MB|GB)$")


def status(conn: Any) -> str:
    """``valid``, ``invalid`` (an interrupted build), ``other`` (that name, another
    definition) or ``missing``."""
    row = conn.execute(
        "SELECT i.indisvalid AND i.indisready, pg_get_indexdef(i.indexrelid) FROM pg_index i "
        "WHERE i.indexrelid = to_regclass('ontology.' || %s)", (NAME,)).fetchone()
    if row is None:
        return "missing"
    definition = row[1] or ""
    if "(field, value)" not in definition or PREDICATE not in definition:
        return "other"
    return "valid" if row[0] else "invalid"


def build(conn: Any, *, maintenance_work_mem: str = "1GB", workers: int = 4) -> str:
    if not getattr(conn, "autocommit", False):
        raise ValueError("date_index_needs_autocommit")
    if not _MEMORY.match(maintenance_work_mem) or not 0 <= int(workers) <= 16:
        raise ValueError("date_index_bad_budget")
    state = status(conn)
    if state == "valid":
        return "skipped"
    conn.execute("SET statement_timeout = 0")
    conn.execute(f"SET maintenance_work_mem = '{maintenance_work_mem}'")
    conn.execute(f"SET max_parallel_maintenance_workers = {int(workers)}")
    if state != "missing":
        conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS ontology.{NAME}")
    conn.execute(DDL)
    return "built" if status(conn) == "valid" else "failed"


def main(argv: Sequence[str] | None = None) -> int:
    from operating.pg import config
    p = argparse.ArgumentParser(prog="python3 -m operating.pg.date_index")
    p.add_argument("command", choices=("status", "build", "drop"))
    p.add_argument("--dsn", default=None)
    p.add_argument("--maintenance-work-mem", default="1GB")
    p.add_argument("--workers", type=int, default=4)
    a = p.parse_args(argv)
    import psycopg
    try:
        with psycopg.connect(a.dsn or config.dsn(), autocommit=True) as conn:
            if a.command == "status":
                print(json.dumps({"index": NAME, "state": status(conn)}))
                return 0
            if a.command == "drop":
                conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS ontology.{NAME}")
                print(json.dumps({"index": NAME, "state": status(conn)}))
                return 0
            done = build(conn, maintenance_work_mem=a.maintenance_work_mem,
                         workers=a.workers)
            print(json.dumps({"index": NAME, "build": done}))
            return 1 if done == "failed" else 0
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
