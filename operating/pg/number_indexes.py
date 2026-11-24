"""Bytewise (``text_pattern_ops``) indexes on the business tables' number and code columns
(``business_views.NUMBER_COLS``), kept on the served tables by every build and refresh.

An equality or a prefix range (``=``, ``~>=~``, ``~<~``: leakproof operators) on such a
column reaches its index through the guarded person-only views, as the search box's number
legs do (``page_reads._find_sql``); ``lower(col) = q`` alone read every row (73-82k rows a
leg, live 11-22). A column a view masks per cell is no index's, whatever is built here.

Made by every ``business_views.refresh`` on the tables in place (a table built before them
gets them at the next refresh; one rebuilt, its predecessor dropped with that index, gets it
again at the end of the same refresh), never inside ``build``/``build_view``: the views'
code fingerprints (``business_views_code``) hash those, and a change there rebuilds every
table, the communications tables too. Named ``<table>_<col>_pattern``. A table without the
index reads as before, only slower."""

from __future__ import annotations

import logging
from typing import Any, Mapping

log = logging.getLogger(__name__)
SUFFIX = "_pattern"
# the columns Ask's mail and file searches filter on (Ask specs 14.1 T3/T4): an equality or a
# prefix on a subject, a sender domain, a mailbox or an attachment's file name. Made
# CONCURRENTLY (the table keeps serving), each after the disk budget grants its peak.
COMMS_INDEXES = (("emails", "subject"), ("emails", "sender_domain"), ("emails", "mailbox"),
                 ("attachments", "name"))
PEAK_FACTOR = 2.0       # a btree's final size is about the key bytes x 1.4; the sort needs as much again


def wanted(views: Mapping[str, Any], number_cols: tuple[str, ...]) -> list[tuple[str, str]]:
    """(table, column) of every text number column of the business views (a
    communications table, built by its ``fill``, has none of its own here)."""
    return [(name, c.name) for name, v in views.items() if getattr(v, "fill", None) is None
            for c in v.cols if c.name in number_cols and c.kind == "text"]


def ensure(write: Any, schema: str, views: Mapping[str, Any],
           number_cols: tuple[str, ...]) -> list[str]:
    """Make the missing indexes on the tables that exist; the ones made, by name."""
    made = []
    for table, col in wanted(views, number_cols):
        name = f"{table}_{col}{SUFFIX}"
        if len(name) > 63:   # a longer name would be cut and collide: never made
            continue
        if write.execute("SELECT to_regclass(%s)", (f"{schema}.{table}",)).fetchone()[0] is None:
            continue
        if write.execute("SELECT 1 FROM pg_indexes WHERE schemaname = %s AND indexname = %s",
                         (schema, name)).fetchone():
            continue   # in place (or the name is another table's: never a second one)
        write.execute(f'CREATE INDEX IF NOT EXISTS "{name}" ON {schema}.{table} '
                      f'("{col}" text_pattern_ops)')
        made.append(name)
    return made


def _estimate_gb(write: Any, schema: str, table: str, col: str) -> float:
    """Peak GB of the index: the average key length over a 1% page sample x the rows x 1.4
    (btree overhead) x ``PEAK_FACTOR`` (sort space); at least 0.05."""
    rows = write.execute("SELECT reltuples::bigint FROM pg_class WHERE oid = %s::regclass",
                         (f"{schema}.{table}",)).fetchone()[0]
    avg = write.execute(f'SELECT avg(octet_length("{col}")) FROM {schema}.{table} '
                        "TABLESAMPLE SYSTEM (1)").fetchone()[0] or 16
    return max(0.05, float(rows or 0) * (float(avg) + 8) * 1.4 * PEAK_FACTOR / 1024 ** 3)


def ensure_comms(write: Any, schema: str,
                 wanted_: tuple[tuple[str, str], ...] = COMMS_INDEXES) -> dict[str, Any]:
    """The communications tables' search indexes, made CONCURRENTLY (``write`` is an
    autocommit connection). ``made``: names built; ``refused``: name -> why (the disk budget
    refused, or the column is missing): said, retried at the next refresh; ``rebuilt``: an
    INVALID index (a failed concurrent build) dropped and made again."""
    from operating import disk_budget
    out: dict[str, Any] = {"made": [], "refused": {}, "rebuilt": []}
    for table, col in wanted_:
        name = f"{table}_{col}{SUFFIX}"
        if write.execute("SELECT to_regclass(%s)", (f"{schema}.{table}",)).fetchone()[0] is None:
            continue
        if not write.execute("SELECT 1 FROM information_schema.columns WHERE table_schema = %s "
                             "AND table_name = %s AND column_name = %s", (schema, table, col)
                             ).fetchone():
            out["refused"][name] = f"{table} has no column {col}"
            continue
        have = write.execute("SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = "
                             "i.indexrelid WHERE c.relname = %s AND c.relnamespace = %s::regnamespace",
                             (name, schema)).fetchone()
        if have and have[0]:
            continue
        if have:   # a failed concurrent build left it invalid: it is never used, never kept
            write.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {schema}."{name}"')
            out["rebuilt"].append(name)
        need = _estimate_gb(write, schema, table, col)
        try:
            disk_budget.reserve(f"index:{name}", need)
        except (disk_budget.Refused, OSError) as exc:   # OSError: the budget cannot be read: no index
            out["refused"][name] = f"{type(exc).__name__}: {exc}"
            log.warning("comms index %s not made: %s", name, exc)
            continue
        try:
            write.execute(f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{name}" ON {schema}.{table} '
                          f'("{col}" text_pattern_ops)')
            out["made"].append(name)
        finally:
            disk_budget.release(f"index:{name}")
    return out


def main(argv: list[str] | None = None) -> int:
    """``python3 -m operating.pg.number_indexes comms``: make the communications tables' search
    indexes (``ensure_comms``) in a unit of their own (``plx-comms-indexes``), never inside the
    refresh: a CREATE INDEX CONCURRENTLY is one statement of minutes, which the refresh's
    watchdog (no progress for ``WatchdogSec``) would read as a hang and stop, in a loop.
    Exit 0 with the made / refused / rebuilt lists printed (a refusal is said, retried at the
    next run); exit 1 on an error."""
    import argparse
    import json
    import sys
    ap = argparse.ArgumentParser(prog="number_indexes")
    ap.add_argument("command", choices=("comms",))
    ap.add_argument("--dsn")
    args = ap.parse_args(argv)
    import psycopg
    from operating.pg import config
    from operating.pg.business_views import BUILD_STATEMENT_MS, SCHEMA
    try:
        with psycopg.connect(args.dsn or config.dsn(), autocommit=True) as write:
            write.execute(f"SET statement_timeout = {BUILD_STATEMENT_MS}")
            got = ensure_comms(write, SCHEMA)
    except psycopg.Error as exc:
        print(json.dumps({"comms_indexes_failed": f"{type(exc).__name__}: {exc}"}),
              file=sys.stderr)
        return 1
    print(json.dumps(got))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
