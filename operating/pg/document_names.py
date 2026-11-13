"""The document name index on Postgres: a trigram index of the name and path values.

``document_tables.find_documents`` finds the documents whose name or path holds a word. On
SQLite it reads the value word index (``value_text``); on Postgres that index is not built
(``value_cells_words``: hours over the whole store), so every call read each name and path
cell of the store (3.5 million, a 72 GB table) and matched it: 25-95 s per call measured on
the served store on 2026-11-13, past the 45 s tool limit on a cold call.

``value_cells_doc_names`` is a GIN trigram index (``pg_trgm``) of ``lower(value)`` over only
the cells of the name and path fields (``NAME_PROPS``), partial on those field ids as they
stand when it is built: the store keeps one field per (property, field path), and a delta
may add one later. ``covered_fields`` reads the fields the index holds from its own
predicate, so a reader asks the index for those and reads any field added since by the
field's own btree (``value_cells_by_field``); a store without the index (a SQLite store, a
generation swapped in without it) reads as before. Nothing here decides who may read what:
the index only narrows which cells are read, and the reader matches every word itself.

Build it without blocking the delta's writes:
    python3 -m operating.pg.document_names build [--dsn DSN]
(``CREATE INDEX CONCURRENTLY``; a field added since the last build: built again under a
new name, then swapped in.) ``status`` prints what it covers. Nothing else is written.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Iterable, Sequence

SCHEMA = "ontology"
INDEX = "value_cells_doc_names"
NAME_PROPS = ("name", "path")
# what ``pg_get_expr`` prints for the index's predicate: the field ids, nothing else
_PREDICATE = re.compile(r"^\(field = ANY \(ARRAY\[(\d+(?:, \d+)*)\]\)\)$"
                        r"|^\(field = (\d+)\)$")


def fields_of(cur: Any, props: Sequence[str] = NAME_PROPS) -> list[int]:
    cur.execute(f"SELECT field FROM {SCHEMA}.value_fields WHERE property = ANY(%s) "
                "ORDER BY field", (list(props),))
    return [int(row[0]) for row in cur.fetchall()]


def predicate_fields(text: Any) -> frozenset[int] | None:
    """The field ids an index predicate as ``pg_get_expr`` prints it names; None for any
    other predicate (it covers nothing a reader may rely on)."""
    match = _PREDICATE.match(str(text or ""))
    if match is None:
        return None
    return frozenset(int(v) for v in (match.group(1) or match.group(2)).split(", "))


COVERED_SQL = ("SELECT pg_get_expr(i.indpred, i.indrelid) FROM pg_index i "
               f"WHERE i.indexrelid = to_regclass('{SCHEMA}.{INDEX}') AND i.indisvalid "
               "AND i.indisready")


def covered_fields(cur: Any) -> frozenset[int]:
    """The fields the valid name index holds (none without one)."""
    cur.execute(COVERED_SQL)
    row = cur.fetchone()
    return (predicate_fields(row[0]) or frozenset()) if row else frozenset()


def _create(name: str, fields: Iterable[int], concurrently: bool) -> str:
    ids = ", ".join(str(int(f)) for f in sorted(fields))
    return (f"CREATE INDEX {'CONCURRENTLY ' if concurrently else ''}IF NOT EXISTS {name} "
            f"ON {SCHEMA}.value_cells USING gin (lower(value) gin_trgm_ops) "
            f"WHERE field IN ({ids})")


def build(dsn: str, *, concurrently: bool = True) -> dict[str, Any]:
    """The index over every name and path field there is now: nothing when the valid one
    covers them all; else built (under a new name when one exists, then swapped in). An
    invalid one left by a failed concurrent build is dropped first."""
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SET statement_timeout = 0")
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        fields = fields_of(cur)
        if not fields:
            return {"built": False, "reason": "no_name_fields"}
        have = covered_fields(cur)
        if set(fields) <= have:
            return {"built": False, "covered": sorted(have)}
        drop = "DROP INDEX CONCURRENTLY IF EXISTS" if concurrently else "DROP INDEX IF EXISTS"
        nxt = f"{INDEX}_next"
        cur.execute(f"{drop} {SCHEMA}.{nxt}")   # a failed build's invalid leftover
        cur.execute("SELECT 1 FROM pg_class WHERE oid = to_regclass(%s)",
                    (f"{SCHEMA}.{INDEX}",))
        exists = cur.fetchone() is not None
        cur.execute(_create(nxt if exists else INDEX, fields, concurrently))
        if exists:
            cur.execute(f"{drop} {SCHEMA}.{INDEX}")
            cur.execute(f"ALTER INDEX {SCHEMA}.{nxt} RENAME TO {INDEX}")
        return {"built": True, "covered": sorted(covered_fields(cur))}


def status(dsn: str) -> dict[str, Any]:
    import psycopg
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        fields = fields_of(cur)
        have = covered_fields(cur)
        return {"fields": fields, "covered": sorted(have),
                "uncovered": sorted(set(fields) - have)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.pg.document_names")
    parser.add_argument("command", choices=("build", "status"))
    parser.add_argument("--dsn", default=None)
    args = parser.parse_args(argv)
    from operating.pg import config
    dsn = args.dsn or config.dsn()
    out = build(dsn) if args.command == "build" else status(dsn)
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
