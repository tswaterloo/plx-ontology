"""The 1-bit vector copy for restricted readers (migration 0056): filled, switched, followed.

``search.reader_bits`` (chunk_id, audience_key, markings, kind, embed_model, bits) holds one
binary-quantized vector per embedded chunk, clustered by audience, kept current by a
trigger on ``search.chunks``. A reader who may not read every audience unit (a staff
member: the shared units and their own mail and chats) is searched by scanning their own
rows in Hamming order and checking the best ``RECHECK`` again with the full vector on
``search.chunks`` under the same permission predicate (``operating.search.retrievers``,
shape "bits"); a reader of every unit keeps the whole-table HNSW.

  python3 -m operating.pg.reader_bits status
  python3 -m operating.pg.reader_bits stage           on a build copy: search.reader_bits_staging
      (UNLOGGED, every embedded chunk's bits), made from search.chunks in one statement
  python3 -m operating.pg.reader_bits fill [--source chunks|staging]
      one INSERT per audience key, in key order (the clustering), ``ON CONFLICT DO
      NOTHING`` (a row the trigger wrote is newer and kept), under the WAL budget and the
      disk floor; resumes after the last key done (``reader_bits_state.fill_after``)
  python3 -m operating.pg.reader_bits prune            rows whose chunk is gone or no longer
      embedded (a staging copy older than the trigger's writes), per audience key
  python3 -m operating.pg.reader_bits switch on|off    'on' refused until a fill finished

Readers never depend on this table for safety: a candidate is re-read from
``search.chunks`` with the reader's predicate, so a stale row costs recall only.
Prints JSON lines of counts only.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from typing import Any, Sequence

TABLE = "search.reader_bits"
STAGING = "search.reader_bits_staging"
STATE = "search.reader_bits_state"
# The full-vector re-check of the best rows by Hamming distance (11-22, 20 real questions
# vs exact, recall@10 / warm p95: 8,000 gave 0.96 / 0.37 s for a 624k three-mailbox scope
# and 1.0 / 0.46 s for a staff reader (935k shared + a 162k mailbox); 4,000 gave 0.92 and
# 0.995).
RECHECK = 8000
# A reader is "restricted" (this path) when they may read fewer than this share of the
# audience units; a reader of (nearly) every unit keeps the whole-table HNSW.
BROAD_SHARE = 0.5
# Each statement of the bits scan is cancelled past this; the search then runs on the
# whole-table index (as without the table).
TIMEOUT_MS = 3000
# Parallel workers of one bits scan (11-22: measured with 4).
WORKERS = 4
STATE_SECONDS = 30.0

_cache: dict[tuple[str, str], tuple[float, Any]] = {}
_lock = threading.Lock()


def _db(conn: Any) -> str:
    info = getattr(conn, "info", None)
    return (f"{getattr(info, 'host', '')}:{getattr(info, 'port', '')}/"
            f"{getattr(info, 'dbname', '')}")


def _cached(conn: Any, name: str, load: Any) -> Any:
    key, now = (_db(conn), name), time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < STATE_SECONDS:
            return hit[1]
    value = load()
    with _lock:
        _cache[key] = (now, value)
    return value


def clear_cache() -> None:
    with _lock:
        _cache.clear()


_TRIGGERS = ("reader_bits_sync", "reader_bits_drop", "reader_bits_truncate")


def read_ready(conn: Any) -> bool:
    """True only when the switch is on AND the three triggers that keep the table
    current are enabled on search.chunks (a table rebuilt without them would go stale)."""
    row = conn.execute("SELECT to_regclass('search.reader_bits_state') IS NOT NULL").fetchone()
    if not row[0]:
        return False
    row = conn.execute("SELECT ready FROM search.reader_bits_state WHERE singleton").fetchone()
    if not row or row[0] is not True:
        return False
    n = conn.execute(
        "SELECT count(*) FROM pg_trigger WHERE tgrelid = to_regclass('search.chunks') "
        "AND tgname = ANY(%s) AND tgenabled <> 'D'", (list(_TRIGGERS),)).fetchone()[0]
    return int(n) == len(_TRIGGERS)


def ready(conn: Any) -> bool:
    """``read_ready``, kept ``STATE_SECONDS`` per database."""
    return bool(_cached(conn, "ready", lambda: read_ready(conn)))


def unit_count(conn: Any) -> int:
    """The audience units (0 when the table is not there), kept per database."""
    def load() -> int:
        if not conn.execute("SELECT to_regclass('search.audience_units') IS NOT NULL"
                            ).fetchone()[0]:
            return 0
        return int(conn.execute("SELECT count(*) FROM search.audience_units").fetchone()[0])
    return int(_cached(conn, "units", load))


def restricted(conn: Any, allowed: Sequence[str]) -> bool:
    """True for a reader of fewer than ``BROAD_SHARE`` of the audience units; a database
    with no unit list says False (the whole-table index, as before)."""
    units = unit_count(conn)
    return units > 0 and len(set(allowed)) < BROAD_SHARE * units


def _dim(conn: Any) -> int:
    row = conn.execute(
        "SELECT a.atttypmod FROM pg_attribute a WHERE a.attrelid = 'search.chunks'::regclass "
        "AND a.attname = 'embedding'").fetchone()
    if not row or not row[0] or row[0] < 1:
        raise RuntimeError("chunks_embedding_dim_unread")
    return int(row[0])


def stage(dsn: str) -> dict[str, Any]:
    """On a build copy (never the box: it reads every vector): the staging table, then
    its (audience_key) index for the fill's per-key reads."""
    import psycopg
    t = time.monotonic()
    with psycopg.connect(dsn, autocommit=True) as conn:
        dim = _dim(conn)
        conn.execute(f"DROP TABLE IF EXISTS {STAGING}")
        conn.execute(
            f"CREATE UNLOGGED TABLE {STAGING} AS SELECT chunk_id, audience_key, markings, kind, "
            f"embed_model, binary_quantize(embedding)::bit({dim}) AS bits FROM search.chunks "
            "WHERE embedding IS NOT NULL AND embed_model IS NOT NULL ORDER BY audience_key")
        conn.execute(f"CREATE INDEX ON {STAGING} (audience_key)")
        n = conn.execute(f"SELECT count(*) FROM {STAGING}").fetchone()[0]
    return {"staged": int(n), "seconds": round(time.monotonic() - t, 1)}


def _source_sql(source: str, dim: int) -> str:
    if source == "chunks":
        return (f"SELECT chunk_id, audience_key, markings, kind, embed_model, "
                f"binary_quantize(embedding)::bit({dim}) FROM search.chunks "
                "WHERE audience_key = %(k)s AND embedding IS NOT NULL "
                "AND embed_model IS NOT NULL")
    if source == "staging":
        return (f"SELECT chunk_id, audience_key, markings, kind, embed_model, bits "
                f"FROM {STAGING} WHERE audience_key = %(k)s")
    raise ValueError(f"source_unknown:{source!r}")


def _keys(conn: Any, source: str, after: str | None) -> list[str]:
    table = {"chunks": "search.chunks", "staging": STAGING, "bits": TABLE}[source]
    # a skip scan over the audience btree (one probe per key)
    rows = conn.execute(
        f"WITH RECURSIVE k(key) AS (SELECT min(audience_key) FROM {table} "
        "WHERE audience_key > %(a)s UNION ALL SELECT (SELECT min(t.audience_key) FROM "
        f"{table} t WHERE t.audience_key > k.key) FROM k WHERE k.key IS NOT NULL) "
        "SELECT key FROM k WHERE key IS NOT NULL", {"a": after or ""}).fetchall()
    return [r[0] for r in rows]


def fill(dsn: str, *, source: str = "chunks", budget: Any = None,
         min_free: int | None = None) -> dict[str, Any]:
    """Every audience key's rows from ``source``, one transaction per key in key order,
    after the last key done; the WAL budget is asked between keys (never inside one)."""
    import psycopg
    from operating.pg import business_comms, wal_budget
    t = time.monotonic()
    with psycopg.connect(dsn, autocommit=True) as conn:
        dim = _dim(conn)
        sql = f"INSERT INTO {TABLE} {_source_sql(source, dim)} ON CONFLICT (chunk_id) DO NOTHING"
        floor = wal_budget.min_free_bytes() if min_free is None else min_free
        budget = budget or wal_budget.Budget.for_conn(
            conn, job="reader_bits:fill", free_path=business_comms.disk_path(conn),
            min_free=floor)
        longest = wal_budget.max_wait_seconds()
        after = conn.execute(f"SELECT fill_after FROM {STATE} WHERE singleton").fetchone()[0]
        keys = _keys(conn, source, after)
        rows = 0
        for key in keys:
            budget.wait(max_seconds=longest)
            with conn.transaction():
                rows += conn.execute(sql, {"k": key}).rowcount or 0
                conn.execute(f"UPDATE {STATE} SET fill_after = %s, changed_at = now() "
                             "WHERE singleton", (key,))
        conn.execute(f"UPDATE {STATE} SET fill_after = NULL, filled_at = now(), "
                     "changed_at = now() WHERE singleton")
    return {"source": source, "keys": len(keys), "inserted": rows, "resumed_after": bool(after),
            "seconds": round(time.monotonic() - t, 1), **budget.report()}


def prune(dsn: str, *, budget: Any = None) -> dict[str, Any]:
    """Rows whose chunk is gone, or holds no vector now, removed (one key at a time)."""
    import psycopg
    from operating.pg import wal_budget
    with psycopg.connect(dsn, autocommit=True) as conn:
        budget = budget or wal_budget.Budget.for_conn(conn, job="reader_bits:prune")
        longest = wal_budget.max_wait_seconds()
        removed = 0
        for key in _keys(conn, "bits", None):
            budget.wait(max_seconds=longest)
            removed += conn.execute(
                f"DELETE FROM {TABLE} b WHERE b.audience_key = %(k)s AND NOT EXISTS "
                "(SELECT 1 FROM search.chunks c WHERE c.chunk_id = b.chunk_id "
                "AND c.embedding IS NOT NULL AND c.embed_model = b.embed_model)",
                {"k": key}).rowcount or 0
    return {"pruned": removed}


def switch(dsn: str, to: str) -> dict[str, Any]:
    import psycopg
    if to not in ("on", "off"):
        raise ValueError(f"switch_unknown:{to!r}")
    with psycopg.connect(dsn, autocommit=True) as conn:
        if to == "on":
            filled, after = conn.execute(
                f"SELECT filled_at, fill_after FROM {STATE} WHERE singleton").fetchone()
            if filled is None or after is not None:
                return {"refused": "not_filled"}
        conn.execute(f"UPDATE {STATE} SET ready = %s, changed_at = now() WHERE singleton",
                     (to == "on",))
        clear_cache()
        return {"ready": read_ready(conn)}


def status(dsn: str) -> dict[str, Any]:
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn:
        ready_now = read_ready(conn)
        row = conn.execute(f"SELECT filled_at, fill_after FROM {STATE} WHERE singleton"
                           ).fetchone()
        n = conn.execute("SELECT reltuples::bigint FROM pg_class WHERE oid = "
                         "'search.reader_bits'::regclass").fetchone()[0]
    return {"ready": ready_now, "filled_at": None if row[0] is None else row[0].isoformat(),
            "fill_in_progress": row[1] is not None, "rows_estimate": int(n)}


def main(argv: Sequence[str] | None = None) -> int:
    from operating.pg import config
    ap = argparse.ArgumentParser(prog="reader_bits", description=__doc__.split("\n")[0])
    ap.add_argument("--dsn", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("stage")
    f = sub.add_parser("fill")
    f.add_argument("--source", choices=("chunks", "staging"), default="chunks")
    sub.add_parser("prune")
    s = sub.add_parser("switch")
    s.add_argument("to", choices=("on", "off"))
    args = ap.parse_args(argv)
    dsn = args.dsn or config.dsn()
    if args.cmd == "status":
        out = status(dsn)
    elif args.cmd == "stage":
        out = stage(dsn)
    elif args.cmd == "fill":
        out = fill(dsn, source=args.source)
    elif args.cmd == "prune":
        out = prune(dsn)
    else:
        out = switch(dsn, args.to)
    print(json.dumps(out))
    return 2 if "refused" in out else 0


if __name__ == "__main__":
    sys.exit(main())
