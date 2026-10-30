"""The retrievers over ``search.chunks`` (CONTRACT.md schema), each one SQL query that
carries the reader's permission predicate in its WHERE clause, before any rank or limit:

    audience_key = ANY(allowed) AND markings <@ held

so a row the reader may not read never takes a slot of a retriever's list.

* ``exact``: chunks of a resolved object, or naming one of its ids (``linked_ids``).
* ``graph``: chunks of objects linked to a resolved object (the caller's neighbours) and
  of the candidates of an ambiguous entity.
* ``dense``: pgvector HNSW kNN with iterative index scans (pgvector 0.8,
  ``hnsw.iterative_scan = relaxed_order``) so the permission filter keeps recall.
* ``bm25``: Postgres full text with OR semantics (a tsquery of the question's lexemes
  joined by ``|``, as ``websearch_to_tsquery`` reads ``a or b``: a document need not hold
  every word; a lexeme in more than a quarter of the rows is left out while rarer ones
  remain), candidates by ``ts_rank_cd``, then scored BM25 (k1 1.2, b 0.75) with document
  frequencies counted over the rows this reader may read only.
* ``fuzzy``: trigram word similarity of the question's words to the chunk header (title,
  ids, what it is about): typo tolerance.

Hidden matches are counted by the ``operating.pg.permissions.hidden_counts``.
"""
from __future__ import annotations

import math
import re
import threading
import time
from typing import Any, Iterable, Mapping, Sequence

PERM = ("c.audience_key = ANY(%(allowed)s::text[]) "
        "AND c.markings <@ %(held)s::text[]")
KINDS = "AND (%(kinds)s::text[] IS NULL OR c.kind = ANY(%(kinds)s::text[]))"

# Words the OR query leaves out (English stopwords plus question filler): an OR query
# keeping "why" or "the" matches every row.
STOPWORDS = frozenset("""
a about above after again against all am an and any are aren as at be because been before
being below between both but by can cannot could did do does doing don down during each
few for from further had has have having he her here hers herself him himself his how i if
in into is isn it its itself just let me more most my myself no nor not of off on once only
or other ought our ours ourselves out over own same she should so some such than that the
their theirs them themselves then there these they this those through to too under until up
very was we were what when where which while who whom why with would you your yours
yourself yourselves whats hows whos thats theres tell show give find get got know please
anything something thing things any guy guys stuff hey hi ok okay""".split())

_WORD = re.compile(r"[^\W_][\w'.\-/]*", re.UNICODE)
BM25_K1, BM25_B = 1.2, 0.75
BM25_POOL = 200
FUZZY_THRESHOLD = 0.5
FUZZY_POOL = 3000
COMMON_SHARE = 0.25
COMMON_MIN_ROWS = 1000
DF_CAP = 20000
POOL_SCAN = 20000
MAX_TERMS = 24
# Chunks one source item (a document without its page, a message, a map) may hold in
# one retriever's list, and how much deeper than its list a retriever reads to fill it:
# a 60-stage map or a workbook's rows would otherwise take every slot, leaving the
# fusion a handful of documents (8 from 100 chunks, measured), most of which the item
# gate may then refuse.
PER_ITEM = 2
DEEP = 4
_ITEM_KEY = r"regexp_replace(i.source_handle, '[#&]page=[0-9]+$', '')"


def _per_item(inner: str, limit_param: str = "limit") -> str:
    """``inner`` (``chunk_id, s`` best first, at most ``DEEP`` x limit rows) kept to
    ``PER_ITEM`` chunks per source item, best ``limit`` rows."""
    return f"""SELECT chunk_id, s FROM (
                   SELECT q.chunk_id, q.s, row_number() OVER (
                       PARTITION BY {_ITEM_KEY} ORDER BY q.s DESC, q.chunk_id) AS n
                   FROM ({inner}) q JOIN search.chunks i ON i.chunk_id = q.chunk_id) x
               WHERE n <= {PER_ITEM}
               ORDER BY s DESC, chunk_id
               LIMIT %({limit_param})s"""


def words(texts: Iterable[str], *, limit: int = MAX_TERMS) -> list[str]:
    """The content words of ``texts`` (lowercased, stopwords and 1-letter words out),
    in first-seen order, at most ``limit``."""
    out: list[str] = []
    for text in texts:
        for raw in _WORD.findall(text or ""):
            word = raw.strip("'.-/").lower()
            if len(word) < 2 or word in STOPWORDS or word in out:
                continue
            out.append(word)
            if len(out) >= limit:
                return out
    return out


def _params(scope: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return dict(scope, **extra)


# --------------------------------------------------------------------------- retrievers

def exact(conn: Any, scope: dict[str, Any], ids: Sequence[str], tokens: Sequence[str],
          limit: int) -> list[tuple[str, float]]:
    if not ids and not tokens:
        return []
    forms = list(dict.fromkeys([*tokens, *(t.upper() for t in tokens)]))
    rows = conn.execute(
        f"""SELECT c.chunk_id,
                   CASE WHEN c.object_id = ANY(%(ids)s::text[]) THEN 1.0
                        WHEN c.linked_ids && %(ids)s::text[] THEN 0.9 ELSE 0.7 END AS s
            FROM search.chunks c
            WHERE (c.object_id = ANY(%(ids)s::text[]) OR c.linked_ids && %(ids)s::text[]
                   OR c.linked_ids && %(forms)s::text[])
              AND {PERM} {KINDS}
            ORDER BY s DESC, c.authority DESC, c.occurred_at DESC NULLS LAST, c.chunk_id
            LIMIT %(limit)s""",
        _params(scope, ids=list(ids), forms=forms, limit=limit)).fetchall()
    return [(r[0], float(r[1])) for r in rows]


def graph(conn: Any, scope: dict[str, Any], neighbours: Sequence[tuple[str, float]],
          limit: int) -> list[tuple[str, float]]:
    """``neighbours``: (object_id, weight 0..1) of objects linked to the question's
    objects; a chunk scores its best neighbour's weight."""
    if not neighbours:
        return []
    ids = [n for n, _ in neighbours]
    weights = [float(w) for _, w in neighbours]
    rows = conn.execute(
        f"""WITH n(id, w) AS (SELECT * FROM unnest(%(ids)s::text[], %(w)s::float8[]))
            SELECT c.chunk_id, max(n.w) AS s
            FROM search.chunks c JOIN n ON c.object_id = n.id OR n.id = ANY(c.linked_ids)
            WHERE (c.object_id = ANY(%(ids)s::text[]) OR c.linked_ids && %(ids)s::text[])
              AND {PERM} {KINDS}
            GROUP BY c.chunk_id, c.authority, c.occurred_at
            ORDER BY s DESC, c.authority DESC, c.occurred_at DESC NULLS LAST, c.chunk_id
            LIMIT %(limit)s""",
        _params(scope, ids=ids, w=weights, limit=limit)).fetchall()
    return [(r[0], float(r[1])) for r in rows]


def _vector(values: Sequence[float], dim: int | None = None) -> str:
    """A halfvec literal; zero-padded to the column's ``dim``."""
    values = [float(v) for v in values]
    if dim is not None and len(values) < dim:
        values += [0.0] * (dim - len(values))
    return "[" + ",".join(repr(v) for v in values) + "]"


def column_dim(conn: Any) -> int | None:
    """The dimension of ``search.chunks.embedding`` (its type modifier), None if unread."""
    try:
        row = conn.execute(
            """SELECT a.atttypmod FROM pg_attribute a
               WHERE a.attrelid = 'search.chunks'::regclass AND a.attname = 'embedding'"""
        ).fetchone()
    except Exception:  # noqa: BLE001 - unpadded; a mismatch fails the dense list only
        return None
    return int(row[0]) if row and row[0] and row[0] > 0 else None


def dense(conn: Any, scope: dict[str, Any], vectors: Sequence[Sequence[float]], model: str,
          limit: int, *, ef_search: int = 100) -> list[tuple[str, float]]:
    """kNN per query vector (the question and each rewrite), best cosine similarity per
    chunk. Only chunks embedded by ``model`` (the query's model) are compared."""
    if not vectors:
        return []
    dim = column_dim(conn)
    best: dict[str, float] = {}
    with conn.transaction():
        conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        conn.execute(f"SET LOCAL hnsw.ef_search = {min(max(int(ef_search), limit * DEEP), 1000)}")
        conn.execute("SET LOCAL hnsw.max_scan_tuples = 40000")
        for vector in vectors:
            rows = conn.execute(
                _per_item(f"""SELECT c.chunk_id, 1 - (c.embedding <=> %(v)s::halfvec) AS s
                    FROM search.chunks c
                    WHERE c.embed_model = %(model)s AND {PERM} {KINDS}
                    ORDER BY c.embedding <=> %(v)s::halfvec
                    LIMIT %(deep)s"""),
                _params(scope, v=_vector(vector, dim), model=model, limit=limit,
                        deep=limit * DEEP)).fetchall()
            for chunk_id, sim in rows:
                if sim is not None and float(sim) > best.get(chunk_id, -2.0):
                    best[chunk_id] = float(sim)
    return sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


def _db(conn: Any) -> str:
    """Which database a connection reads (cache keys never cross databases)."""
    info = getattr(conn, "info", None)
    return f"{getattr(info, 'host', '')}:{getattr(info, 'port', '')}/{getattr(info, 'dbname', '')}"


class _Stats:
    """(row count, mean document length) of a reader's readable rows, kept 5 minutes
    per scope: the BM25 corpus is what the reader may read, so a hidden row's words
    never shift a visible row's score."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[tuple, tuple[float, int, float]] = {}

    def get(self, conn: Any, scope: dict[str, Any]) -> tuple[int, float]:
        key = (_db(conn), tuple(scope["allowed"]), tuple(scope["held"]),
               tuple(scope["kinds"] or ()))
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and now - hit[0] < 300:
                return hit[1], hit[2]
        # A 2 % block sample (scaled): a full count reads every row of a large table.
        row = conn.execute(
            f"SELECT count(*), coalesce(avg(length(c.tsv)), 1) FROM search.chunks c "
            f"TABLESAMPLE SYSTEM (2) REPEATABLE (7) WHERE {PERM} {KINDS}", scope).fetchone()
        n, avgdl = int(row[0]) * 50, float(row[1] or 1.0)
        if n < 5000:   # small: count exactly
            row = conn.execute(
                f"SELECT count(*), coalesce(avg(length(c.tsv)), 1) FROM search.chunks c "
                f"WHERE {PERM} {KINDS}", scope).fetchone()
            n, avgdl = int(row[0]), float(row[1] or 1.0)
        with self._lock:
            if len(self._cache) > 512:
                self._cache.clear()
            self._cache[key] = (now, n, avgdl)
        return n, avgdl


STATS = _Stats()


def _lexemes(conn: Any, terms: Sequence[str]) -> list[str]:
    """The index lexemes of the content words: English stems of every word, plus the
    words with a digit as typed (ids and codes, in the ``simple`` half of the tsv)."""
    codes = [t for t in terms if any(ch.isdigit() for ch in t)]
    return [r[0] for r in conn.execute(
        """SELECT DISTINCT lexeme FROM (
               SELECT unnest(tsvector_to_array(to_tsvector('english', %(text)s))) AS lexeme
               UNION SELECT unnest(tsvector_to_array(to_tsvector('simple', %(codes)s)))) q
           ORDER BY lexeme""",
        {"text": " ".join(terms), "codes": " ".join(codes)}).fetchall()]


def _doc_freq(conn: Any, lexemes: Sequence[str], where: str, params: dict[str, Any]
              ) -> dict[str, int]:
    """Rows holding each lexeme, counted up to ``DF_CAP`` (a lexeme at the cap is common:
    its exact count would read a large share of the table and barely moves its IDF)."""
    return dict(conn.execute(
        f"""SELECT l, (SELECT count(*) FROM (SELECT 1 FROM search.chunks c
                       WHERE c.tsv @@ quote_literal(l)::tsquery AND {where}
                       LIMIT {DF_CAP}) x)
            FROM unnest(%(lexemes)s::text[]) AS l""",
        dict(params, lexemes=list(lexemes))).fetchall())


def _discriminating(df: Mapping[str, int], n: int) -> list[str]:
    """The lexemes present, without those held by more than ``COMMON_SHARE`` of the rows
    (an OR query on "order" matches nearly everything and says little) unless nothing
    else is left; in a corpus under ``COMMON_MIN_ROWS`` rows every lexeme is kept."""
    present = sorted(l for l, d in df.items() if d > 0)
    if n < COMMON_MIN_ROWS:
        return present   # a small corpus: every word still tells rows apart
    rare = [l for l in present if df[l] <= COMMON_SHARE * n and df[l] < DF_CAP]
    return rare or present


# An OR query of lexemes: any one matches (never all of them).
_OR_TSQUERY = ("array_to_string(ARRAY(SELECT quote_literal(l) FROM "
               "unnest(%(lexemes)s::text[]) l), ' | ')::tsquery")


def bm25(conn: Any, scope: dict[str, Any], texts: Sequence[str], limit: int
         ) -> list[tuple[str, float]]:
    """Okapi BM25 over the rows this reader may read: document frequencies, row count
    and mean length are the readable rows', so a hidden row's words never shift a
    visible row's score. Candidates are the rows holding ANY discriminating query
    lexeme, the best ``BM25_POOL`` by ``ts_rank_cd`` (cover density), then scored."""
    terms = words(texts)
    if not terms:
        return []
    lexemes = _lexemes(conn, terms)
    if not lexemes:
        return []
    df = _doc_freq(conn, lexemes, f"{PERM} {KINDS}", scope)
    n, avgdl = STATS.get(conn, scope)
    n = max(n, 1)
    keep = _discriminating(df, n)
    if not keep:
        return []
    params = _params(scope, lexemes=keep, pool=max(BM25_POOL, limit * DEEP))
    # Ranked among at most POOL_SCAN matching rows, so a query left with only common
    # words stays bounded on a large table.
    pool = conn.execute(
        f"""SELECT c.chunk_id, length(c.tsv), {_ITEM_KEY.replace("i.", "c.")} FROM (
                SELECT c.chunk_id, c.tsv, c.source_handle FROM search.chunks c
                WHERE c.tsv @@ {_OR_TSQUERY} AND {PERM} {KINDS} LIMIT {POOL_SCAN}) c
            ORDER BY ts_rank_cd(c.tsv, {_OR_TSQUERY}, 32) DESC, c.chunk_id
            LIMIT %(pool)s""", params).fetchall()
    if not pool:
        return []
    lengths = {r[0]: max(int(r[1]), 1) for r in pool}
    item_of = {r[0]: r[2] for r in pool}
    tf: dict[str, dict[str, int]] = {}
    for chunk_id, lexeme, count in conn.execute(
            """SELECT c.chunk_id, u.lexeme, coalesce(array_length(u.positions, 1), 1)
               FROM search.chunks c, unnest(c.tsv) u
               WHERE c.chunk_id = ANY(%(ids)s::text[]) AND u.lexeme = ANY(%(lexemes)s::text[])""",
            {"ids": list(lengths), "lexemes": lexemes}).fetchall():
        tf.setdefault(chunk_id, {})[lexeme] = int(count)
    scores = []
    for chunk_id, length in lengths.items():
        score = 0.0
        for lexeme, freq in tf.get(chunk_id, {}).items():
            d = int(df.get(lexeme) or 0)
            idf = math.log(1 + (n - d + 0.5) / (d + 0.5))
            score += idf * freq * (BM25_K1 + 1) / (
                freq + BM25_K1 * (1 - BM25_B + BM25_B * length / avgdl))
        if score > 0:
            scores.append((chunk_id, score))
    scores.sort(key=lambda kv: (-kv[1], kv[0]))
    kept: list[tuple[str, float]] = []
    per_item: dict[str, int] = {}
    for chunk_id, score in scores:   # at most PER_ITEM chunks of one source item
        item = item_of[chunk_id]
        if per_item.get(item, 0) < PER_ITEM:
            per_item[item] = per_item.get(item, 0) + 1
            kept.append((chunk_id, score))
            if len(kept) >= limit:
                break
    return kept


def fuzzy(conn: Any, scope: dict[str, Any], question: str, limit: int
          ) -> list[tuple[str, float]]:
    """Trigram word similarity of each content word (4+ letters) to the chunk header,
    summed: a misspelt name or subject word still finds its chunk."""
    terms = [t for t in words([question]) if len(t) >= 4 and not t.isdigit()][:8]
    if not terms:
        return []
    # Only the words no readable row holds as typed (a typo, a variant spelling): a word
    # that matches exactly is the BM25 list's, and would otherwise match every header.
    terms = [r[0] for r in conn.execute(
        f"""SELECT t FROM unnest(%(terms)s::text[]) WITH ORDINALITY AS u(t, i)
            WHERE NOT EXISTS (SELECT 1 FROM search.chunks c
                              WHERE c.tsv @@ plainto_tsquery('simple', t) AND {PERM} {KINDS})
            ORDER BY i""", _params(scope, terms=terms)).fetchall()]
    if not terms:
        return []
    # One OR arm per word, so the trigram index on header (chunks_header_trgm) serves
    # each arm. The matching rows are scored among at most FUZZY_POOL of them: a typo of
    # a common word ("recieved") matches tens of thousands of headers, and reading and
    # scoring all of them (5 s) is past the statement timeout; the first FUZZY_POOL read
    # in 0.3 s. Such a word tells few rows apart anyway (as BM25 drops common lexemes).
    arms = " OR ".join(f"%(t{i})s <%% c.header" for i in range(len(terms)))
    with conn.transaction():
        conn.execute(f"SET LOCAL pg_trgm.word_similarity_threshold = {FUZZY_THRESHOLD}")
        rows = conn.execute(
            _per_item(f"""SELECT c.chunk_id,
                       (SELECT sum(word_similarity(t, c.header)) FROM unnest(%(terms)s::text[]) t
                        WHERE t <%% c.header) AS s
                FROM (SELECT c.chunk_id, c.header FROM search.chunks c
                      WHERE ({arms}) AND {PERM} {KINDS}
                      LIMIT {FUZZY_POOL}) c
                ORDER BY s DESC NULLS LAST, c.chunk_id
                LIMIT %(deep)s"""),
            _params(scope, terms=terms, limit=limit, deep=limit * DEEP,
                    **{f"t{i}": t for i, t in enumerate(terms)})).fetchall()
    return [(r[0], float(r[1] or 0.0)) for r in rows if r[1]]


# --------------------------------------------------------------------------- details

DETAIL_COLUMNS = ("chunk_id", "source_handle", "object_id", "kind", "title", "author_role",
                  "occurred_at", "header", "body", "dup_group", "authority", "audience_key")


def details(conn: Any, scope: dict[str, Any], chunk_ids: Sequence[str]) -> dict[str, dict]:
    """The shown fields of readable chunks (the permission predicate again: a row a
    retriever did not filter is never read here either)."""
    if not chunk_ids:
        return {}
    rows = conn.execute(
        f"""SELECT {', '.join('c.' + col for col in DETAIL_COLUMNS)}
            FROM search.chunks c
            WHERE c.chunk_id = ANY(%(ids)s::text[]) AND {PERM}""",
        _params(scope, ids=list(chunk_ids))).fetchall()
    return {r[0]: dict(zip(DETAIL_COLUMNS, r)) for r in rows}


def audience_keys(conn: Any) -> list[str]:
    """Every audience unit the chunk table holds (a skip scan over its index)."""
    rows = conn.execute(
        """WITH RECURSIVE k(key) AS (
               (SELECT audience_key FROM search.chunks ORDER BY audience_key LIMIT 1)
               UNION ALL
               SELECT (SELECT audience_key FROM search.chunks WHERE audience_key > k.key
                       ORDER BY audience_key LIMIT 1) FROM k WHERE k.key IS NOT NULL)
           SELECT key FROM k WHERE key IS NOT NULL""").fetchall()
    return [r[0] for r in rows]
