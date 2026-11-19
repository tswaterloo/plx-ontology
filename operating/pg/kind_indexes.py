"""Build the per-kind chunk HNSW indexes on a populated database, outside any deploy.

Migration 0036 creates ``chunks_embedding_k_<kind>`` only while ``search.chunks`` is empty
(a fresh or test database). The served database gets them from this command, run after a
catch-up (docs/runbooks/search-kinds-1115.md)::

    python3 -m operating.pg.kind_indexes status
    python3 -m operating.pg.kind_indexes build --concurrently [--kinds mail,teams]
    python3 -m operating.pg.kind_indexes drop

``build`` runs ``CREATE INDEX CONCURRENTLY`` one kind at a time (chunk writes go on), with a
small maintenance budget (``--workers``, ``--maintenance-work-mem``) so search and the
catch-up keep the machine. It is idempotent and resumable: a valid index of the right
definition is skipped; an INVALID one (an interrupted concurrent build), one not ready, or
one under that name with another definition is dropped CONCURRENTLY and built again. Each
built index is checked valid before the next kind starts; one that is not stops the run
(exit 1). A plain (non-concurrent) build is refused on a populated table, an unknown kind
is refused, and a second build on the same database is refused while one runs.

Search uses an index only when it is valid and its predicate is exactly
``kind = '<kind>'`` (``operating.pg.dense.kind_indexes``), and only with
``PLX_SEARCH_KINDS=1``; building them changes nothing a reader sees until that flag is on.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Any, Sequence

from operating.pg.migrate import (CHUNKS_KIND_HNSW_KINDS, CHUNKS_KIND_HNSW_PREFIX,
                                  chunks_kind_hnsw_ddl)

# One kind-index build per database (not the migration lock, which a
# build of hours must never hold)
BUILD_LOCK_KEY = 0x504C58_4B49  # "PLXKI"
DEFAULT_WORKERS = 2
DEFAULT_MEMORY = "2GB"
_MEMORY = re.compile(r"^[1-9][0-9]{0,5}(kB|MB|GB)$")


def _kinds(kinds: Sequence[str] | None) -> list[str]:
    if kinds is None:
        return list(CHUNKS_KIND_HNSW_KINDS)
    unknown = sorted(set(kinds) - set(CHUNKS_KIND_HNSW_KINDS))
    if unknown or not kinds:
        raise ValueError(f"kind_indexes_unknown_kind:{unknown or 'none'}")
    return list(dict.fromkeys(kinds))


def status(conn: Any, kinds: Sequence[str] | None = None) -> dict[str, str]:
    """kind -> ``valid`` (search uses it), ``invalid`` (interrupted concurrent build or not
    ready), ``other`` (that name, another definition) or ``missing``."""
    from operating.pg.dense import _KIND_INDEX_DEF, _KIND_PRED
    rows = {name: (valid, ready, pred, definition, am) for name, valid, ready, pred,
            definition, am in conn.execute(
        "SELECT x.relname, i.indisvalid, i.indisready, pg_get_expr(i.indpred, i.indrelid), "
        "pg_get_indexdef(i.indexrelid), a.amname FROM pg_index i "
        "JOIN pg_class x ON x.oid = i.indexrelid JOIN pg_am a ON a.oid = x.relam "
        "WHERE i.indrelid = to_regclass('search.chunks')").fetchall()}
    out: dict[str, str] = {}
    for kind in _kinds(kinds):
        hit = rows.get(CHUNKS_KIND_HNSW_PREFIX + kind)
        if hit is None:
            out[kind] = "missing"
            continue
        valid, ready, pred, definition, am = hit
        m = _KIND_PRED.match(pred or "")
        if am != "hnsw" or not m or m.group(1) != kind \
                or not _KIND_INDEX_DEF.search(definition or ""):
            out[kind] = "other"
        elif not (valid and ready):
            out[kind] = "invalid"
        else:
            out[kind] = "valid"
    return out


def _populated(conn: Any) -> bool:
    return conn.execute("SELECT EXISTS (SELECT 1 FROM search.chunks LIMIT 1)").fetchone()[0]


def _drop(conn: Any, kind: str) -> None:
    from psycopg import sql
    conn.execute(sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS search.{}").format(
        sql.Identifier(CHUNKS_KIND_HNSW_PREFIX + kind)))


def build(conn: Any, kinds: Sequence[str] | None = None, *, concurrently: bool = True,
          workers: int = DEFAULT_WORKERS, maintenance_work_mem: str = DEFAULT_MEMORY,
          log: Any = None) -> dict[str, Any]:
    """Build every missing or broken per-kind index, one at a time (an autocommit
    connection: CREATE INDEX CONCURRENTLY cannot run in a transaction block). Returns
    {"built": [...], "skipped": [...], "failed": kind or None}."""
    wanted = _kinds(kinds)
    if not getattr(conn, "autocommit", False):
        raise ValueError("kind_indexes_needs_autocommit")
    if not _MEMORY.match(str(maintenance_work_mem)):
        raise ValueError(f"kind_indexes_bad_memory:{maintenance_work_mem!r}")
    workers = int(workers)
    if not 0 <= workers <= 16:
        raise ValueError(f"kind_indexes_bad_workers:{workers}")
    if conn.execute("SELECT to_regclass('search.chunks')").fetchone()[0] is None:
        raise ValueError("kind_indexes_no_chunks_table")
    if not concurrently and _populated(conn):
        raise ValueError("kind_indexes_populated_needs_concurrently")
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (BUILD_LOCK_KEY,)).fetchone()[0]:
        raise ValueError("kind_indexes_build_running")
    result: dict[str, Any] = {"built": [], "skipped": [], "failed": None}
    try:
        conn.execute("SET statement_timeout = 0")
        conn.execute(f"SET maintenance_work_mem = '{maintenance_work_mem}'")
        conn.execute(f"SET max_parallel_maintenance_workers = {workers}")
        for kind in wanted:
            state = status(conn, [kind])[kind]
            if state == "valid":
                result["skipped"].append(kind)
                continue
            started = time.monotonic()
            if state != "missing":
                if concurrently:
                    _drop(conn, kind)
                else:
                    conn.execute(f"DROP INDEX IF EXISTS search.{CHUNKS_KIND_HNSW_PREFIX}{kind}")
            ddl = chunks_kind_hnsw_ddl(kind)
            if concurrently:
                ddl = ddl.replace("CREATE INDEX IF NOT EXISTS",
                                  "CREATE INDEX CONCURRENTLY IF NOT EXISTS", 1)
            conn.execute(ddl)
            after = status(conn, [kind])[kind]
            if log is not None:
                log({"kind": kind, "was": state, "now": after,
                     "seconds": round(time.monotonic() - started, 1)})
            if after != "valid":
                result["failed"] = kind
                break
            result["built"].append(kind)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (BUILD_LOCK_KEY,))
    try:
        from operating.pg import dense
        dense.clear_cache()
    except Exception:  # noqa: BLE001 - the cache is per process; a server re-reads in 5 min
        pass
    return result


def drop(conn: Any, kinds: Sequence[str] | None = None) -> list[str]:
    """Drop the per-kind indexes CONCURRENTLY (the undo of ``build``)."""
    if not getattr(conn, "autocommit", False):
        raise ValueError("kind_indexes_needs_autocommit")
    dropped = []
    for kind, state in status(conn, kinds).items():
        if state != "missing":
            _drop(conn, kind)
            dropped.append(kind)
    return dropped


def main(argv: Sequence[str] | None = None) -> int:
    from operating.pg import config
    p = argparse.ArgumentParser(prog="python3 -m operating.pg.kind_indexes")
    p.add_argument("command", choices=("status", "build", "drop"))
    p.add_argument("--dsn", default=None)
    p.add_argument("--kinds", default=None, help="comma list (default: every listed kind)")
    p.add_argument("--concurrently", action="store_true", default=True)
    p.add_argument("--plain", dest="concurrently", action="store_false",
                   help="non-concurrent build; refused on a populated table")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    p.add_argument("--maintenance-work-mem", default=DEFAULT_MEMORY)
    a = p.parse_args(argv)
    kinds = [k for k in a.kinds.split(",") if k] if a.kinds is not None else None
    import psycopg

    def log(row: dict) -> None:
        print(json.dumps(row, sort_keys=True), flush=True)
    try:
        with psycopg.connect(a.dsn or config.dsn(), autocommit=True) as conn:
            if a.command == "status":
                print(json.dumps(status(conn, kinds), sort_keys=True))
                return 0
            if a.command == "drop":
                print(json.dumps({"dropped": drop(conn, kinds)}))
                return 0
            res = build(conn, kinds, concurrently=a.concurrently, workers=a.workers,
                        maintenance_work_mem=a.maintenance_work_mem, log=log)
            print(json.dumps(res, sort_keys=True))
            return 1 if res["failed"] else 0
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
