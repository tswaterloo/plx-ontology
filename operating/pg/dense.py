"""Permission-filtered nearest-neighbour search over the search tables.

``filtered_knn`` runs the HNSW index with pgvector 0.8's iterative scan (relaxed order,
re-sorted), the reader's permission filter inside the same SQL. When the filter is so
selective that the iterative scan stops (``hnsw.max_scan_tuples``) before it finds ``k``
visible rows, it falls back to an exact scan over the visible rows only (the audience-key
btree finds them; few rows, since the filter was selective). So a reader never gets fewer
than ``k`` hits while ``k`` visible rows exist, and never a row outside the scope.
"""
from __future__ import annotations

from typing import Any, Sequence

import psycopg

from operating.pg.permissions import SearchScope
from operating.pg.pool import DEFAULT_EF_SEARCH, DEFAULT_MAX_SCAN_TUPLES, search_defaults

_COLUMNS = {"chunks": ("chunk_id", "embedding"), "entities": ("entity_id", "name_embedding")}
MAX_K = 1000


def _vector_text(vector: Sequence[float]) -> str:
    values = [float(v) for v in vector]
    if not values or any(v != v or v in (float("inf"), float("-inf")) for v in values):
        raise ValueError("vector_invalid")
    return "[" + ",".join(repr(v) for v in values) + "]"


def filtered_knn(conn: psycopg.Connection, scope: SearchScope, vector: Sequence[float],
                 k: int = 20, *, table: str = "chunks", kinds: Sequence[str] | None = None,
                 ef_search: int = DEFAULT_EF_SEARCH,
                 max_scan_tuples: int = DEFAULT_MAX_SCAN_TUPLES) -> dict[str, Any]:
    """``{"hits": [(id, cosine distance)], "path": "index"|"exact"|"empty"}``, nearest
    first, only rows the scope lets the reader see. ``index``: the planned query (the HNSW
    index with iterative scan; on a small or very selective visible set the planner may
    already pick an exact plan); ``exact``: the fallback after it returned fewer than k."""
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
    hnsw = (f"WITH hits AS MATERIALIZED (SELECT c.{id_col} AS id, {distance} AS d "
            f"FROM search.{table} c WHERE {' AND '.join(where)} ORDER BY {distance} "
            f"LIMIT %(k)s) SELECT id, d FROM hits ORDER BY d, id")
    with conn.transaction():
        search_defaults(conn, ef_search=max(ef_search, k), max_scan_tuples=max_scan_tuples,
                        local=True)
        rows = conn.execute(hnsw, params).fetchall()
    if len(rows) >= k:
        return {"hits": rows, "path": "index"}
    # "+ 0" keeps the planner off the HNSW index: an exact scan of the visible rows.
    exact = (f"SELECT c.{id_col}, {distance} AS d FROM search.{table} c "
             f"WHERE {' AND '.join(where)} ORDER BY ({distance}) + 0, c.{id_col} LIMIT %(k)s")
    with conn.transaction():
        rows = conn.execute(exact, params).fetchall()
    return {"hits": rows, "path": "exact"}
