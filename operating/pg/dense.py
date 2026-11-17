"""Permission-filtered nearest-neighbour search over the search tables.

``filtered_knn`` runs the HNSW index with pgvector 0.8's iterative scan (relaxed order,
re-sorted), the reader's permission filter inside the same SQL. When the filter is so
selective that the iterative scan stops (``hnsw.max_scan_tuples``) before it finds ``k``
visible rows, it falls back to an exact scan over the visible rows only (the audience-key
btree finds them; few rows, since the filter was selective). So a reader never gets fewer
than ``k`` hits while ``k`` visible rows exist (unless the exact scan overruns
``EXACT_TIMEOUT_MS``: then the index rows found so far, path ``partial``), and never a row
outside the scope.

``PLX_SEARCH_BQ=1`` (default off) switches chunk search to the binary-quantized HNSW
(``chunks_embedding_bq_hnsw``, migration 0034): the index orders by the Hamming distance
of the sign bits and yields ``bq_candidates(k)`` rows (200-400; never fewer than k), and
those are re-ranked by the exact halfvec cosine distance. The permission filter is the same
WHERE clause inside the index query. A bit index is 1/16 of the halfvec one, which is what
lets every mail chunk stay in memory. Any other flag value is off.
"""
from __future__ import annotations

import os
from typing import Any, Mapping, Sequence

import psycopg

from operating.pg.permissions import SearchScope
from operating.pg.pool import DEFAULT_EF_SEARCH, DEFAULT_MAX_SCAN_TUPLES, search_defaults

_COLUMNS = {"chunks": ("chunk_id", "embedding"), "entities": ("entity_id", "name_embedding")}
MAX_K = 1000
BQ_FLAG = "PLX_SEARCH_BQ"
BQ_MIN_CANDIDATES, BQ_MAX_CANDIDATES = 200, 400
# hnsw.ef_search caps at 1000 in pgvector, so a bit-index candidate list does too.
BQ_EF_CAP = 1000
EXACT_TIMEOUT_MS = 5000


def bq_enabled(env: Mapping[str, str] | None = None) -> bool:
    """The binary-quantized search flag: on only for ``1``/``on``/``true`` (any case);
    unset, empty or any other value keeps the halfvec index."""
    value = (os.environ if env is None else env).get(BQ_FLAG, "")
    return value.strip().lower() in ("1", "on", "true")


def bq_candidates(need: int) -> int:
    """Bit-index candidates re-ranked for ``need`` final rows: twice ``need`` within
    200-400, never fewer than ``need`` (at most the ef_search cap)."""
    return min(BQ_EF_CAP, max(need, min(BQ_MAX_CANDIDATES, max(BQ_MIN_CANDIDATES, 2 * need))))


def bq_order(column: str, query: str, dim: int) -> str:
    """The Hamming-distance ORDER BY expression; it must match the index expression
    ``(binary_quantize(embedding))::bit(dim)`` for the planner to use the index."""
    if not isinstance(dim, int) or isinstance(dim, bool) or not 1 <= dim <= 64000:
        raise ValueError(f"bq_dim_invalid:{dim!r}")
    return (f"binary_quantize({column})::bit({dim}) <~> "
            f"binary_quantize({query}::halfvec)::bit({dim})")


def vector_dim(conn: Any, table: str = "chunks") -> int | None:
    """The declared dimension of ``table``'s vector column, None when unread."""
    if table not in _COLUMNS:
        raise ValueError(f"search_table_unknown:{table}")
    row = conn.execute(
        "SELECT a.atttypmod FROM pg_attribute a WHERE a.attrelid = %s::regclass "
        "AND a.attname = %s", (f"search.{table}", _COLUMNS[table][1])).fetchone()
    return int(row[0]) if row and row[0] and row[0] > 0 else None


def _vector_text(vector: Sequence[float]) -> str:
    values = [float(v) for v in vector]
    if not values or any(v != v or v in (float("inf"), float("-inf")) for v in values):
        raise ValueError("vector_invalid")
    return "[" + ",".join(repr(v) for v in values) + "]"


def filtered_knn(conn: psycopg.Connection, scope: SearchScope, vector: Sequence[float],
                 k: int = 20, *, table: str = "chunks", kinds: Sequence[str] | None = None,
                 ef_search: int = DEFAULT_EF_SEARCH,
                 max_scan_tuples: int = DEFAULT_MAX_SCAN_TUPLES,
                 bq: bool | None = None) -> dict[str, Any]:
    """``{"hits": [(id, cosine distance)], "path": "index"|"exact"|"partial"|"empty"}``,
    nearest first, only rows the scope lets the reader see. ``index``: the planned query
    (the HNSW index with iterative scan; on a small or very selective visible set the
    planner may already pick an exact plan); ``exact``: the fallback after it returned fewer
    than k; ``partial``: the fallback overran ``EXACT_TIMEOUT_MS``, so the index rows only.
    ``bq`` (default: the ``PLX_SEARCH_BQ`` flag) takes the bit-index candidates and
    re-ranks them exactly; only ``chunks`` has a bit index."""
    if table not in _COLUMNS:
        raise ValueError(f"search_table_unknown:{table}")
    if not isinstance(k, int) or isinstance(k, bool) or not 1 <= k <= MAX_K:
        raise ValueError("knn_k_invalid")
    if not isinstance(scope, SearchScope) or scope.empty:
        return {"hits": [], "path": "empty"}
    id_col, vec_col = _COLUMNS[table]
    where = [scope.clause("c"), f"c.{vec_col} IS NOT NULL"]
    extra: dict[str, Any] = {"q": _vector_text(vector), "k": k}
    if kinds is not None:
        if table != "chunks" or isinstance(kinds, str) or not all(
                isinstance(v, str) for v in kinds):
            raise ValueError("knn_kinds_invalid")
        where.append("c.kind = ANY(%(kinds)s::text[])")
        extra["kinds"] = list(kinds)
    params = scope.params(**extra)
    distance = f"c.{vec_col} <=> %(q)s::halfvec"
    dim = vector_dim(conn, table) if (bq_enabled() if bq is None else bq) \
        and table == "chunks" else None
    if dim is not None:
        # Bit-index candidates (Hamming order, permission filter in the scan), then the
        # exact cosine distance of each candidate's halfvec decides the order.
        params["cand"] = cand = bq_candidates(k)
        hnsw = (f"WITH cand AS MATERIALIZED (SELECT c.{id_col} AS id, {distance} AS d "
                f"FROM search.{table} c WHERE {' AND '.join(where)} "
                f"ORDER BY {bq_order(f'c.{vec_col}', '%(q)s', dim)} LIMIT %(cand)s) "
                f"SELECT id, d FROM cand ORDER BY d, id LIMIT %(k)s")
        ef = min(BQ_EF_CAP, max(ef_search, cand))
    else:
        hnsw = (f"WITH hits AS MATERIALIZED (SELECT c.{id_col} AS id, {distance} AS d "
                f"FROM search.{table} c WHERE {' AND '.join(where)} ORDER BY {distance} "
                f"LIMIT %(k)s) SELECT id, d FROM hits ORDER BY d, id")
        ef = max(ef_search, k)
    with conn.transaction():
        search_defaults(conn, ef_search=ef, max_scan_tuples=max_scan_tuples, local=True)
        rows = conn.execute(hnsw, params).fetchall()
    if len(rows) >= k:
        return {"hits": rows, "path": "index"}
    # "+ 0" keeps the planner off the HNSW indexes: an exact scan of the visible rows,
    # bounded by EXACT_TIMEOUT_MS; past it the index rows found so far stand (``partial``)
    # rather than a request that reads every visible row of a 27M-row table.
    exact = (f"SELECT c.{id_col}, {distance} AS d FROM search.{table} c "
             f"WHERE {' AND '.join(where)} ORDER BY ({distance}) + 0, c.{id_col} LIMIT %(k)s")
    try:
        with conn.transaction():
            # set_config(..., true) is SET LOCAL; inside a caller's transaction this block
            # is a savepoint whose release would keep it, so the old value is put back.
            before = conn.execute("SELECT current_setting('statement_timeout')").fetchone()[0]
            conn.execute("SELECT set_config('statement_timeout', %s, true)",
                         (f"{int(EXACT_TIMEOUT_MS)}ms",))
            full = conn.execute(exact, params).fetchall()
            conn.execute("SELECT set_config('statement_timeout', %s, true)", (before,))
    except psycopg.errors.QueryCanceled:
        return {"hits": rows, "path": "partial"}
    return {"hits": full, "path": "exact"}
