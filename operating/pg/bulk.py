"""Bulk vector loads: drop the chunks' HNSW index before a large embedding load and
rebuild it after (one build is far cheaper than ~10^5 incremental inserts into the graph).

While the index is absent, ``operating.pg.dense.filtered_knn`` still answers through its
exact path (fewer than k index hits -> an exact scan of the visible rows), so search keeps
working, slower. The index definition is the migration's (0001_search,
``set_embedding``); ``operating.pg.schema_check`` reports it missing until rebuilt.
``python3 -m operating.embed reindex`` rebuilds it after an interrupted load.

The binary-quantized index (``chunks_embedding_bq_hnsw``, migration 0034, searched with
``PLX_SEARCH_BQ=1``) is dropped and rebuilt with it: left in place, every loaded row would
be an incremental graph insert. So are the per-kind ones (``chunks_embedding_k_<kind>``,
migration 0036, searched with ``PLX_SEARCH_KINDS=1``), but they are rebuilt only with that
flag on (14 more graphs about double the rebuild); off, they stay dropped until
``python3 -m operating.pg.kind_indexes build``.
"""
from __future__ import annotations

from typing import Any

# the bit index's DDL lives with the migration runner (its embedding switch rebuilds it), so
# the runner never imports this module and with it the search read path
from operating.pg.migrate import (CHUNKS_BQ_HNSW, CHUNKS_KIND_HNSW_KINDS, LOCK_KEY,
                                  chunks_bq_hnsw_ddl, chunks_kind_hnsw_ddl,
                                  drop_chunks_kind_hnsw, kinds_flag_on)

CHUNKS_HNSW = "chunks_embedding_hnsw"
CHUNKS_HNSW_DDL = ("CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON search.chunks USING "
                   "hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64)")

def vector_index_present(conn: Any) -> bool:
    return conn.execute("SELECT to_regclass('search.chunks_embedding_hnsw')").fetchone()[0] \
        is not None


def drop_vector_index(conn: Any) -> bool:
    """Drop the chunks' HNSW indexes (under the migration lock). True when it existed. The
    caller's open transaction is committed first, so the drop's lock ends with its own
    short transaction and never waits on (or holds up searches behind) earlier work."""
    conn.commit()
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        present = vector_index_present(conn)
        conn.execute(f"DROP INDEX IF EXISTS search.{CHUNKS_HNSW}")
        conn.execute(f"DROP INDEX IF EXISTS search.{CHUNKS_BQ_HNSW}")
        drop_chunks_kind_hnsw(conn)
    return present


def rebuild_vector_index(conn: Any, *, maintenance_work_mem: str = "8GB",
                         parallel_workers: int = 7) -> None:
    """Build the chunks' HNSW indexes (parallel build, enough memory for the graph). Pass a
    connection with no failed transaction (``rebuild_on_fresh``)."""
    conn.commit()
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        conn.execute(f"SET LOCAL maintenance_work_mem = '{maintenance_work_mem}'")
        conn.execute(f"SET LOCAL max_parallel_maintenance_workers = {int(parallel_workers)}")
        conn.execute(CHUNKS_HNSW_DDL)
        from operating.pg.dense import vector_dim
        dim = vector_dim(conn, "chunks")
        if dim is not None:
            conn.execute(chunks_bq_hnsw_ddl(dim))
        if kinds_flag_on():
            for kind in CHUNKS_KIND_HNSW_KINDS:
                conn.execute(chunks_kind_hnsw_ddl(kind))


def rebuild_on_fresh(dsn: str, **kwargs: Any) -> None:
    """Rebuild on a new connection: a load whose transaction failed cannot hide its error
    behind the rebuild's, nor leave the index missing."""
    import psycopg
    with psycopg.connect(dsn) as fresh:
        rebuild_vector_index(fresh, **kwargs)


__all__ = ["rebuild_on_fresh", "CHUNKS_BQ_HNSW", "CHUNKS_HNSW", "CHUNKS_HNSW_DDL", "chunks_bq_hnsw_ddl", "drop_vector_index", "rebuild_vector_index",
           "vector_index_present"]
