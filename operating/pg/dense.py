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

``PLX_SEARCH_KINDS=1`` (default off; ``operating.search.retrievers.dense`` reads it) changes
the chunk search's shape, not its filter: a reader whose audience units hold at most
``NARROW_ROWS`` chunks gets the exact nearest rows of that set (an index walk under so
selective a filter stops at ``hnsw.max_scan_tuples`` first); anyone else gets one walk per
kind over the per-kind partial indexes (``kind_indexes``; built after a catch-up by
``python3 -m operating.pg.kind_indexes build``), unioned, and an exact scan of each kind
without a valid index. The narrow exact scan is bounded by ``NARROW_TIMEOUT_MS`` per
statement (past it, the whole-table index). The permission predicate is the same WHERE
clause in every branch.
"""
from __future__ import annotations

import os
import re
import threading
import time
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
# ef_search floor of the narrow table's walks (one node per text: a text many chunks share
# was one node, not many): full-scale copy 11-21, 20 real query vectors, broad reader:
# distance recall@10 vs the chunks path 0.945 at ef 100 (one query 0/10), 1.0 at 200 and 400;
# warm p50 0.19 / 0.70 / 1.04 s vs 0.13 s on the chunks path; one-mailbox reader 1.0 at all.
NARROW_EF_SEARCH = 200


KINDS_FLAG = "PLX_SEARCH_KINDS"
# A reader whose audience units hold at most this many chunks is answered by an exact scan.
NARROW_ROWS = 20000
# Each statement of that exact scan is cancelled past this (measured cold 2.8 s at 20k rows on
# the 10% copy); the search then runs on the whole-table index instead.
NARROW_TIMEOUT_MS = 5000
# Each per-kind walk's ef_search (pgvector's cap is 1000): 400 gave recall@10 0.94, 800 0.99
# on the 10% copy of the served chunks.
KIND_EF_SEARCH = 800
_KIND = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_KIND_PRED = re.compile(r"^\(kind = '([a-z][a-z0-9_]{0,40})'::text\)$")
_KIND_INDEX_DEF = re.compile(r" USING hnsw \(embedding halfvec_cosine_ops\) ")
_CACHE_SECONDS = 300.0
_cache: dict[tuple[str, str], tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def kinds_enabled(env: Mapping[str, str] | None = None) -> bool:
    """The per-kind search flag: on only for ``1``/``on``/``true`` (any case)."""
    value = (os.environ if env is None else env).get(KINDS_FLAG, "")
    return value.strip().lower() in ("1", "on", "true")


def _db_key(conn: Any) -> str:
    info = getattr(conn, "info", None)
    return (f"{getattr(info, 'host', '')}:{getattr(info, 'port', '')}/"
            f"{getattr(info, 'dbname', '')}")


def _cached(conn: Any, name: str, load: Any) -> Any:
    key, now = (_db_key(conn), name), time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < _CACHE_SECONDS:
            return hit[1]
    value = load()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def clear_cache() -> None:
    """Forget the per-database index and kind lists (a test that adds or drops them)."""
    with _cache_lock:
        _cache.clear()


def kind_indexes(conn: Any) -> frozenset[str]:
    """The kinds with a valid, ready partial halfvec cosine HNSW on ``search.chunks`` whose
    predicate is exactly ``kind = '<kind>'`` (kept 5 minutes per database). An index being
    built CONCURRENTLY (not valid yet) or with any other predicate is not used."""
    def load() -> frozenset[str]:
        rows = conn.execute(
            "SELECT pg_get_expr(i.indpred, i.indrelid), pg_get_indexdef(i.indexrelid) "
            "FROM pg_index i JOIN pg_class x ON x.oid = i.indexrelid "
            "JOIN pg_am a ON a.oid = x.relam "
            "WHERE i.indrelid = to_regclass('search.chunks') AND a.amname = 'hnsw' "
            "AND i.indisvalid AND i.indisready AND i.indpred IS NOT NULL").fetchall()
        kinds = set()
        for pred, definition in rows:
            m = _KIND_PRED.match(pred or "")
            if m and _KIND_INDEX_DEF.search(definition or ""):
                kinds.add(m.group(1))
        return frozenset(kinds)
    return _cached(conn, "kind_indexes", load)


def present_kinds(conn: Any) -> tuple[str, ...]:
    """Every distinct ``search.chunks.kind`` (a skip scan over the kind btree, one probe per
    kind; kept 5 minutes per database)."""
    def load() -> tuple[str, ...]:
        rows = conn.execute(
            "WITH RECURSIVE k(kind) AS (SELECT min(kind) FROM search.chunks UNION ALL "
            "SELECT (SELECT min(c.kind) FROM search.chunks c WHERE c.kind > k.kind) FROM k "
            "WHERE k.kind IS NOT NULL) SELECT kind FROM k WHERE kind IS NOT NULL").fetchall()
        return tuple(r[0] for r in rows)
    return _cached(conn, "present_kinds", load)


def narrow(conn: Any, allowed: Sequence[str], limit: int = NARROW_ROWS) -> bool:
    """True when the reader's audience units hold at most ``limit`` chunks (an index-only
    count over the audience btree, stopped at ``limit`` + 1): every visible row is in those
    units, so an exact scan of them reads at most ``limit`` rows."""
    if not allowed:
        return True
    n = conn.execute(
        "SELECT count(*) FROM (SELECT 1 FROM search.chunks c "
        "WHERE c.audience_key = ANY(%(a)s::text[]) LIMIT %(n)s) x",
        {"a": list(allowed), "n": int(limit) + 1}).fetchone()[0]
    return int(n) <= limit


def kind_plan(conn: Any, kinds: Sequence[str] | None
              ) -> tuple[list[str], list[str]] | None:
    """(kinds walked through their own index, kinds scanned exactly) for a reader's kind
    filter (None = every kind); None when no kind has an index or the kinds without one
    hold more than ``NARROW_ROWS`` chunks (an exact scan of them would be slow: the caller
    keeps the whole-table index). A kind name outside ``[a-z][a-z0-9_]*`` is never inlined
    into SQL: it is scanned exactly, as a bound parameter."""
    wanted = list(present_kinds(conn)) if kinds is None else list(dict.fromkeys(kinds))
    indexed = kind_indexes(conn)
    walk = sorted(k for k in wanted if k in indexed and _KIND.match(k))
    rest = sorted(k for k in wanted if k not in walk)
    if not walk:
        return None
    if rest:
        key = "rest_rows:" + ",".join(rest)
        n = _cached(conn, key, lambda: int(conn.execute(
            "SELECT count(*) FROM (SELECT 1 FROM search.chunks c "
            "WHERE c.kind = ANY(%(k)s::text[]) LIMIT %(n)s) x",
            {"k": rest, "n": NARROW_ROWS + 1}).fetchone()[0]))
        if n > NARROW_ROWS:
            return None
    return walk, rest


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
    from operating.pg import chunk_vectors
    if table == "chunks" and chunk_vectors.narrow(conn):
        return _narrow_knn(conn, scope, vector, k, kinds, ef_search, max_scan_tuples)
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


def _narrow_knn(conn: psycopg.Connection, scope: SearchScope, vector: Sequence[float],
                k: int, kinds: Sequence[str] | None, ef_search: int,
                max_scan_tuples: int) -> dict[str, Any]:
    """``filtered_knn`` over ``search.chunk_vectors`` ('narrow' mode): the partitioned HNSW
    in distance order joined to the chunks holding each text, the scope's predicate on the
    chunk row; the same exact fallback (over the joined visible rows) when the walk stops
    short of ``k``."""
    where = [scope.clause("c")]
    extra: dict[str, Any] = {"q": _vector_text(vector), "k": k}
    if kinds is not None:
        if isinstance(kinds, str) or not all(isinstance(v, str) for v in kinds):
            raise ValueError("knn_kinds_invalid")
        where.append("c.kind = ANY(%(kinds)s::text[]) AND v.kind = ANY(%(kinds)s::text[])")
        extra["kinds"] = list(kinds)
    params = scope.params(**extra)
    distance = "v.embedding <=> %(q)s::halfvec"
    source = ("search.chunk_vectors v JOIN search.chunks c ON c.kind = v.kind "
              "AND c.content_sha = v.content_sha")
    hnsw = (f"WITH hits AS MATERIALIZED (SELECT c.chunk_id AS id, {distance} AS d "
            f"FROM {source} WHERE {' AND '.join(where)} ORDER BY {distance} "
            f"LIMIT %(k)s) SELECT id, d FROM hits ORDER BY d, id")
    with conn.transaction():
        search_defaults(conn, ef_search=min(1000, max(ef_search, k, NARROW_EF_SEARCH)),
                        max_scan_tuples=max_scan_tuples, local=True)
        # each partition's walk feeds a merge that takes its input as sorted: relaxed order
        # let a merge cut the nearest rows off (1 of 20 live queries, full-scale copy 11-21)
        conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")
        rows = conn.execute(hnsw, params).fetchall()
    if len(rows) >= k:
        return {"hits": rows, "path": "index"}
    exact = (f"SELECT c.chunk_id, {distance} AS d FROM {source} WHERE {' AND '.join(where)} "
             f"ORDER BY ({distance}) + 0, c.chunk_id LIMIT %(k)s")
    try:
        with conn.transaction():
            before = conn.execute("SELECT current_setting('statement_timeout')").fetchone()[0]
            conn.execute("SELECT set_config('statement_timeout', %s, true)",
                         (f"{int(EXACT_TIMEOUT_MS)}ms",))
            full = conn.execute(exact, params).fetchall()
            conn.execute("SELECT set_config('statement_timeout', %s, true)", (before,))
    except psycopg.errors.QueryCanceled:
        return {"hits": rows, "path": "partial"}
    return {"hits": full, "path": "exact"}
