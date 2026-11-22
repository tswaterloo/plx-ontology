"""The chunk vectors' narrow table (migration 0052): built per kind, switched, followed.

``search.chunk_vectors`` (kind, content_sha, embed_model, embedding), LIST-partitioned by
kind, one HNSW per partition. ``search.vector_mode.mode`` says where vectors live:

* ``chunks`` (the default): writers UPDATE ``search.chunks.embedding``, readers read it;
* ``narrow``: writers append here (``store``), readers join here from ``search.chunks``
  on (kind, content_sha) under the reader's permission predicate on the chunk row
  (``operating.search.retrievers``, ``operating.pg.dense``), and what is pending is what
  has no row here (``pending_predicate``).

  python3 -m operating.pg.chunk_vectors status
  python3 -m operating.pg.chunk_vectors build [--kind K]...   each kind's partition: copied
      from search.chunks in content_sha ranges (one transaction each, under the WAL budget
      and the disk floor), its HNSW built once over the full table, then attached
  python3 -m operating.pg.chunk_vectors switch narrow|chunks  (narrow only once every
      embedded kind has its partition attached)
  python3 -m operating.pg.chunk_vectors catch-up              vectors written to
      search.chunks after a kind was copied (run after the switch)

A kind name is a ``[a-z][a-z0-9_]*`` literal (never anything else inlined into SQL).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from typing import Any, Sequence

KIND = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
PARENT = "search.chunk_vectors"
# one fill statement per content_sha range: 256 ranges by the hash's first two hex digits
RANGES = [f"{i:02x}" for i in range(256)]
HNSW_WITH = "WITH (m = 16, ef_construction = 64)"
MODE_SECONDS = 30.0
_mode_cache: dict[str, tuple[float, str]] = {}
_mode_lock = threading.Lock()


def _db(conn: Any) -> str:
    info = getattr(conn, "info", None)
    return (f"{getattr(info, 'host', '')}:{getattr(info, 'port', '')}/"
            f"{getattr(info, 'dbname', '')}")


def read_mode(conn: Any) -> str:
    """'narrow' or 'chunks' as the database says now ('chunks' before migration 0052)."""
    row = conn.execute("SELECT to_regclass('search.vector_mode') IS NOT NULL").fetchone()
    if not row[0]:
        return "chunks"
    row = conn.execute("SELECT mode FROM search.vector_mode WHERE singleton").fetchone()
    mode = row[0] if row else "chunks"
    if mode not in ("chunks", "narrow"):
        raise RuntimeError(f"vector_mode_unknown:{mode!r}")
    return mode


def mode(conn: Any) -> str:
    """``read_mode``, kept ``MODE_SECONDS`` per database (a switch reaches every process
    within that: a reader still on 'chunks' sees the vectors stored up to the switch)."""
    key, now = _db(conn), time.monotonic()
    with _mode_lock:
        hit = _mode_cache.get(key)
        if hit is not None and now - hit[0] < MODE_SECONDS:
            return hit[1]
    value = read_mode(conn)
    with _mode_lock:
        _mode_cache[key] = (now, value)
    return value


def clear_cache() -> None:
    with _mode_lock:
        _mode_cache.clear()


def narrow(conn: Any) -> bool:
    return mode(conn) == "narrow"


def pending_predicate(conn: Any) -> str:
    """The SQL predicate (over an unaliased ``search.chunks``, one ``%s`` = the model) of a
    chunk still without a vector of the model, in the database's mode."""
    if narrow(conn):
        return ("NOT EXISTS (SELECT 1 FROM search.chunk_vectors v WHERE v.kind = "
                "search.chunks.kind AND v.content_sha = search.chunks.content_sha AND "
                "v.embed_model = %s)")
    return "(embedding IS NULL OR embed_model IS DISTINCT FROM %s)"


def store(conn: Any, model: str, dim: int, shas: Sequence[str], texts: Sequence[str]) -> int:
    """Append one vector per (kind, hash) of the chunks holding each hash (``texts``: the
    vectors as halfvec text); a vector already there is kept. Returns the chunk rows
    the vectors now serve (as the UPDATE's rowcount did)."""
    conn.execute(
        f"INSERT INTO {PARENT} (kind, content_sha, embed_model, embedding) "
        f"SELECT DISTINCT c.kind, u.sha, %s, u.vec::halfvec({int(dim)}) "
        "FROM unnest(%s::text[], %s::text[]) AS u(sha, vec) "
        "JOIN search.chunks c ON c.content_sha = u.sha "
        "ON CONFLICT (kind, content_sha, embed_model) DO NOTHING",
        (model, list(shas), list(texts)))
    n = conn.execute("SELECT count(*) FROM search.chunks WHERE content_sha = ANY(%s)",
                     (list(shas),)).fetchone()[0]
    conn.commit()
    return int(n)


# --- building ---------------------------------------------------------------------------

def _part(kind: str) -> str:
    if not KIND.match(kind):
        raise ValueError(f"kind_invalid:{kind!r}")
    return f"chunk_vectors_k_{kind}"


def attached(conn: Any) -> dict[str, str]:
    """kind -> partition name of every kind partition attached to the parent."""
    rows = conn.execute(
        "SELECT c.relname, pg_get_expr(c.relpartbound, c.oid) FROM pg_inherits i "
        "JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = %s::regclass",
        (PARENT,)).fetchall()
    out = {}
    for name, bound in rows:
        m = re.match(r"^FOR VALUES IN \('([a-z][a-z0-9_]*)'\)$", bound or "")
        if m:
            out[m.group(1)] = name
    return out


def embedded_kinds(conn: Any) -> list[str]:
    """Kinds with at least one chunk holding a vector in search.chunks (a skip scan)."""
    from operating.pg import dense
    return [k for k in dense.present_kinds(conn)
            if conn.execute("SELECT EXISTS (SELECT 1 FROM search.chunks WHERE kind = %s "
                            "AND embedding IS NOT NULL)", (k,)).fetchone()[0]]


def _fill_sql(kind: str, table: str, lo: str, hi: str | None) -> str:
    """One range of a kind's vectors from search.chunks (one row per hash and model)."""
    upper = f" AND c.content_sha < '{hi}'" if hi else ""
    return (f"INSERT INTO {table} (kind, content_sha, embed_model, embedding) "
            f"SELECT DISTINCT ON (c.content_sha, c.embed_model) c.kind, c.content_sha, "
            f"c.embed_model, c.embedding FROM search.chunks c WHERE c.kind = '{kind}' "
            f"AND c.embedding IS NOT NULL AND c.embed_model IS NOT NULL "
            f"AND c.content_sha >= '{lo}'{upper} "
            f"ORDER BY c.content_sha, c.embed_model "
            f"ON CONFLICT (kind, content_sha, embed_model) DO NOTHING")


def _ranges() -> list[tuple[str, str | None]]:
    edges = [""] + RANGES[1:]
    return [(edges[i], edges[i + 1] if i + 1 < len(edges) else None)
            for i in range(len(edges))]


def _fill(dsn: str, kind: str, table: str, budget: Any, longest: float,
          workers: int) -> int:
    """Every range of a kind into ``table``, ``workers`` connections at once (the copy is
    random heap reads by hash: one connection read ~700 rows/s on the full-size copy);
    each range its own transaction, each asks the WAL budget first; the first failure
    stops the rest."""
    import concurrent.futures as cf
    import psycopg
    ranges = _ranges()
    stop = threading.Event()

    def run(part: list[tuple[str, str | None]]) -> int:
        n = 0
        with psycopg.connect(dsn, autocommit=True) as conn:
            for lo, hi in part:
                if stop.is_set():
                    break
                budget.wait(max_seconds=longest, stop=stop)
                with conn.transaction():
                    n += conn.execute(_fill_sql(kind, table, lo, hi)).rowcount
        return n

    parts = [ranges[i::max(1, workers)] for i in range(max(1, workers))]
    total = 0
    with cf.ThreadPoolExecutor(max_workers=len(parts)) as pool:
        futures = [pool.submit(run, p) for p in parts]
        try:
            for f in cf.as_completed(futures):
                total += f.result()
        except BaseException:
            stop.set()
            raise
    return total


def build_kind(dsn: str, kind: str, *, budget: Any = None, min_free: int | None = None,
               maintenance_mem: str = "8GB", log: Any = None,
               workers: int = 8) -> dict[str, Any]:
    """Copy, index and attach one kind's partition; a kind already attached is skipped.
    The standalone table survives a stop (the next build resumes its ranges by
    ``ON CONFLICT DO NOTHING``); it is attached only after its HNSW is built."""
    import psycopg
    from operating.pg import wal_budget
    part = _part(kind)
    table = f"search.{part}"
    log = log or (lambda e: print(json.dumps(e), file=sys.stderr, flush=True))
    started = time.monotonic()
    with psycopg.connect(dsn, autocommit=True) as conn:
        if kind in attached(conn):
            return {"kind": kind, "skipped": "attached"}
        floor = wal_budget.min_free_bytes() if min_free is None else min_free
        from operating.pg import business_comms
        path = business_comms.disk_path(conn)
        budget = budget or wal_budget.Budget.for_conn(conn, job=f"chunk_vectors:{kind}",
                                                      free_path=path, min_free=floor)
        longest = wal_budget.max_wait_seconds()
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (LIKE {PARENT} INCLUDING DEFAULTS "
                     f"INCLUDING CONSTRAINTS)")
        # the bound as a constraint: ATTACH then skips its validating scan
        conn.execute(f"DO $$ BEGIN ALTER TABLE {table} ADD CONSTRAINT {part}_kind CHECK "
                     f"(kind IS NOT NULL AND kind = '{kind}'); EXCEPTION WHEN "
                     f"duplicate_object THEN NULL; END $$")
        conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {part}_pkey ON {table} "
                     f"(kind, content_sha, embed_model)")
        rows = _fill(dsn, kind, table, budget, longest, workers)
        log({"kind": kind, "copied": rows, "seconds": round(time.monotonic() - started, 1)})
        budget.wait(max_seconds=longest)
        conn.execute(f"SET maintenance_work_mem = '{maintenance_mem}'")
        conn.execute("SET max_parallel_maintenance_workers = 7")
        conn.execute(f"CREATE INDEX IF NOT EXISTS {part}_hnsw ON {table} USING hnsw "
                     f"(embedding halfvec_cosine_ops) {HNSW_WITH}")
        indexed = round(time.monotonic() - started, 1)
        budget.wait(max_seconds=longest)
        with conn.transaction():
            # rows of this kind the writers put in the default partition meanwhile move in
            # first (ATTACH refuses a default partition holding rows of the new bound)
            conn.execute("SET LOCAL lock_timeout = '10s'")
            conn.execute(f"INSERT INTO {table} SELECT * FROM search.chunk_vectors_other "
                         f"WHERE kind = '{kind}' ON CONFLICT DO NOTHING")
            conn.execute(f"DELETE FROM search.chunk_vectors_other WHERE kind = '{kind}'")
            conn.execute(f"ALTER TABLE {PARENT} ATTACH PARTITION {table} "
                         f"FOR VALUES IN ('{kind}')")
            for role in ("plx_api", "plx_eval"):
                if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s",
                                (role,)).fetchone():
                    conn.execute(f"GRANT SELECT ON {table} TO {role}")
        n = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    return {"kind": kind, "rows": int(n), "copied": rows, "index_built_at_s": indexed,
            "seconds": round(time.monotonic() - started, 1), **budget.report()}


def build(dsn: str, kinds: Sequence[str] | None = None, **kw: Any) -> list[dict[str, Any]]:
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn:
        todo = list(kinds) if kinds else embedded_kinds(conn)
    for k in todo:
        _part(k)        # every name checked before any work
    return [build_kind(dsn, k, **kw) for k in todo]


def missing(conn: Any) -> dict[str, int]:
    """Per kind, the (hash, model) vectors search.chunks holds that this table does not."""
    rows = conn.execute(
        "SELECT c.kind, count(*) FROM (SELECT DISTINCT kind, content_sha, embed_model FROM "
        "search.chunks WHERE embedding IS NOT NULL AND embed_model IS NOT NULL) c "
        "WHERE NOT EXISTS (SELECT 1 FROM search.chunk_vectors v WHERE v.kind = c.kind AND "
        "v.content_sha = c.content_sha AND v.embed_model = c.embed_model) "
        "GROUP BY c.kind ORDER BY c.kind").fetchall()
    return {k: int(n) for k, n in rows}


def switch(dsn: str, to: str) -> dict[str, Any]:
    """Set the mode. 'narrow' is refused while an embedded kind has no attached partition
    (its readers would lose every vector of it until a build)."""
    import psycopg
    if to not in ("chunks", "narrow"):
        raise ValueError(f"vector_mode_unknown:{to!r}")
    with psycopg.connect(dsn, autocommit=True) as conn:
        if to == "narrow":
            lacking = sorted(set(embedded_kinds(conn)) - set(attached(conn)))
            if lacking:
                return {"refused": "kinds_not_built", "kinds": lacking}
        conn.execute("UPDATE search.vector_mode SET mode = %s, changed_at = now() "
                     "WHERE singleton", (to,))
    clear_cache()
    return {"mode": to}


def catch_up(dsn: str, *, budget: Any = None) -> dict[str, int]:
    """Vectors search.chunks gained after their kind was copied, appended (each kind's
    ranges one transaction each, under the WAL budget)."""
    import psycopg
    from operating.pg import wal_budget
    out: dict[str, int] = {}
    with psycopg.connect(dsn, autocommit=True) as conn:
        budget = budget or wal_budget.Budget.for_conn(conn, job="chunk_vectors:catch_up")
        longest = wal_budget.max_wait_seconds()
        for kind in sorted(attached(conn)):
            n = 0
            for lo, hi in _ranges():
                budget.wait(max_seconds=longest)
                with conn.transaction():
                    n += conn.execute(_fill_sql(kind, PARENT, lo, hi)).rowcount
            out[kind] = n
    return out


def status(dsn: str) -> dict[str, Any]:
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn:
        parts = attached(conn)
        sizes = {k: conn.execute("SELECT pg_total_relation_size(%s::regclass)",
                                 (f"search.{p}",)).fetchone()[0] for k, p in parts.items()}
        return {"mode": read_mode(conn), "attached": sorted(parts),
                "gb": {k: round(v / 1024 ** 3, 2) for k, v in sizes.items()}}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="chunk_vectors", description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=("status", "build", "switch", "catch-up", "missing"))
    ap.add_argument("to", nargs="?")
    ap.add_argument("--kind", action="append")
    ap.add_argument("--dsn")
    args = ap.parse_args(argv)
    from operating.pg import config
    dsn = args.dsn or config.dsn()
    if args.command == "status":
        out: Any = status(dsn)
    elif args.command == "build":
        out = build(dsn, args.kind)
    elif args.command == "switch":
        out = switch(dsn, args.to or "")
    elif args.command == "catch-up":
        out = catch_up(dsn)
    else:
        import psycopg
        with psycopg.connect(dsn, autocommit=True) as conn:
            out = missing(conn)
    print(json.dumps(out, default=str))
    return 1 if isinstance(out, dict) and out.get("refused") else 0


if __name__ == "__main__":
    sys.exit(main())
