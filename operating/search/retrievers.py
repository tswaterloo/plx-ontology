"""The retrievers over ``search.chunks`` (CONTRACT.md schema), each one SQL query that
carries the reader's permission predicate in its WHERE clause, before any rank or limit:

    audience_key = ANY(allowed) AND markings <@ held

so a row the reader may not read never takes a slot of a retriever's list.

* ``exact``: chunks of a resolved object, or naming one of its ids (``linked_ids``).
* ``graph``: chunks of objects linked to a resolved object (the caller's neighbours) and
  of the candidates of an ambiguous entity.
* ``dense``: pgvector HNSW kNN with iterative index scans (pgvector 0.8,
  ``hnsw.iterative_scan = relaxed_order``) so the permission filter keeps recall.
* ``bm25``: Okapi BM25 (k1 1.2, b 0.75) with OR semantics (a document need not hold every
  word; a lexeme in more than a quarter of the rows is left out while rarer ones remain):
  every readable row of each rare lexeme is a candidate, common lexemes come from the
  column statistics, and candidates are ranked before any cut (``bm25``).
* ``fuzzy``: trigram word similarity of the question's plain words (letters only) to the
  chunk header (title, ids, what it is about): typo tolerance.

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


# A lexeme in at most this many readable rows is "rare": all of its rows are candidates,
# read with its exact count and term frequencies in one index scan. Rarer than any word
# Postgres keeps column statistics for (``most_common_elems``, about 1% of rows and up).
RARE_CAP = 2000


class _Common:
    """The table's most common lexemes and their row shares, from Postgres's own column
    statistics of ``tsv`` (``pg_stats.most_common_elems``), kept 10 minutes: a common word
    is never scanned to be counted."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: dict[str, tuple[float, dict[str, float], float]] = {}

    def get(self, conn: Any) -> tuple[dict[str, float], float]:
        key = _db(conn)
        now = time.monotonic()
        with self._lock:
            hit = self._held.get(key)
            if hit is not None and now - hit[0] < 600:
                return hit[1], hit[2]
        row = conn.execute(
            "SELECT most_common_elems::text::text[], most_common_elem_freqs FROM pg_stats "
            "WHERE schemaname = 'search' AND tablename = 'chunks' AND attname = 'tsv'"
        ).fetchone()
        shares: dict[str, float] = {}
        floor = 1.0
        if row and row[0] and row[1]:
            elems, freqs = list(row[0]), list(row[1])
            shares = {e: float(f) for e, f in zip(elems, freqs[:len(elems)])}
            floor = min(shares.values(), default=1.0)
        with self._lock:
            self._held[key] = (now, shares, floor)
        return shares, floor


COMMON = _Common()

_COUNT_OF = f"""SELECT l, (SELECT count(*) FROM (SELECT 1 FROM search.chunks c
                                WHERE c.tsv @@ quote_literal(l)::tsquery AND {PERM} {KINDS}
                                LIMIT %(cap)s) x)
                FROM unnest(%(scan)s::text[]) AS l"""
_ROWS_OF = f"""SELECT l.lexeme, c.chunk_id, length(c.tsv),
                   {_ITEM_KEY.replace("i.", "c.")},
                   ARRAY(SELECT u.lexeme || ' ' || coalesce(array_length(u.positions, 1), 1)
                         FROM unnest(c.tsv) u WHERE u.lexeme = ANY(%(lexemes)s::text[]))
               FROM unnest(%(scan)s::text[]) AS l(lexeme),
               LATERAL (SELECT c.chunk_id, c.tsv, c.source_handle FROM search.chunks c
                        WHERE c.tsv @@ quote_literal(l.lexeme)::tsquery AND {PERM} {KINDS}
                        LIMIT %(cap)s) c"""


def bm25(conn: Any, scope: dict[str, Any], texts: Sequence[str], limit: int
         ) -> list[tuple[str, float]]:
    """Okapi BM25 over the rows this reader may read, ranked before anything is cut:

    * a lexeme Postgres's column statistics hold as common (``COMMON``, 1% of the rows
      or more) is never scanned; its document frequency is its share of the readable
      rows (an aggregate of the column, never a hidden row's words);
    * every other lexeme's readable rows are read in one index scan each, up to
      ``RARE_CAP`` + 1: at most ``RARE_CAP`` is rare (its exact count over the readable
      rows and every one of its rows a candidate, so a rare decisive token is never lost
      to disk order), more is common (counted as between the cap and the statistics);
    * only when the rare lexemes give fewer candidates than the pool are rows holding
      every common lexeme added;
    * every candidate is scored over all query lexemes; at most ``PER_ITEM`` chunks of
      one source item are kept."""
    terms = words(texts)
    if not terms:
        return []
    lexemes = _lexemes(conn, terms)
    if not lexemes:
        return []
    n, avgdl = STATS.get(conn, scope)
    n = max(n, 1)
    shares, floor = COMMON.get(conn)
    total = max(_table_rows(conn), n)
    common = {l: max(1, int(shares[l] * n)) for l in lexemes if l in shares}
    scan = [l for l in lexemes if l not in common]
    df: dict[str, int] = dict(common)
    cand: dict[str, tuple[int, str, dict[str, int]]] = {}
    if scan:
        # first a capped count (the index and row visibility only), then the rows and term
        # frequencies of the rare lexemes alone
        counted = dict(conn.execute(_COUNT_OF, _params(scope, scan=scan, cap=RARE_CAP + 1)
                                    ).fetchall())
        rare_scan = [l for l in scan if 0 < int(counted.get(l) or 0) <= RARE_CAP]
        for lexeme in scan:
            got = int(counted.get(lexeme) or 0)
            # common, though rarer than the statistics hold: between the two
            df[lexeme] = got if got <= RARE_CAP else int(
                math.sqrt(RARE_CAP * max(floor * total, RARE_CAP)))
        if rare_scan:
            for _lexeme, chunk_id, length, item, tfs in conn.execute(
                    _ROWS_OF, _params(scope, lexemes=list(lexemes), scan=rare_scan,
                                      cap=RARE_CAP)).fetchall():
                cand[chunk_id] = (max(int(length), 1), item, {
                    t.rsplit(" ", 1)[0]: int(t.rsplit(" ", 1)[1]) for t in tfs or ()})
    pool = max(BM25_POOL, limit * DEEP)
    present = [l for l in lexemes if df.get(l)]
    if n >= COMMON_MIN_ROWS:
        heavy = [l for l in present if df[l] > COMMON_SHARE * n]
        if len(heavy) < len(present):
            present = [l for l in present if l not in heavy]
    rare = {l for l in present if df[l] <= RARE_CAP}
    frequent = [l for l in present if l not in rare]
    if len(cand) < pool and frequent:
        both = " & ".join(f"'{l}'" for l in frequent)   # lexemes: no quote can occur
        for chunk_id, length, item, tfs in conn.execute(
                f"""SELECT c.chunk_id, length(c.tsv), {_ITEM_KEY.replace("i.", "c.")},
                       ARRAY(SELECT u.lexeme || ' ' || coalesce(array_length(u.positions, 1), 1)
                             FROM unnest(c.tsv) u WHERE u.lexeme = ANY(%(lexemes)s::text[]))
                    FROM search.chunks c
                    WHERE c.tsv @@ %(both)s::tsquery AND {PERM} {KINDS}
                    LIMIT %(cap)s""",
                _params(scope, lexemes=list(lexemes), both=both, cap=RARE_CAP)).fetchall():
            cand.setdefault(chunk_id, (max(int(length), 1), item, {
                t.rsplit(" ", 1)[0]: int(t.rsplit(" ", 1)[1]) for t in tfs or ()}))
    scored_lexemes = set(present)
    scores = []
    for chunk_id, (length, item, tfs) in cand.items():
        score = 0.0
        for lexeme, freq in tfs.items():
            if lexeme not in scored_lexemes:
                continue
            d = int(df.get(lexeme) or 0)
            idf = math.log(1 + (n - d + 0.5) / (d + 0.5))
            score += idf * freq * (BM25_K1 + 1) / (
                freq + BM25_K1 * (1 - BM25_B + BM25_B * length / avgdl))
        if score > 0:
            scores.append((chunk_id, score, item))
    scores.sort(key=lambda kv: (-kv[1], kv[0]))
    kept: list[tuple[str, float]] = []
    per_item: dict[str, int] = {}
    for chunk_id, score, item in scores:   # at most PER_ITEM chunks of one source item
        if per_item.get(item, 0) < PER_ITEM:
            per_item[item] = per_item.get(item, 0) + 1
            kept.append((chunk_id, score))
            if len(kept) >= limit:
                break
    return kept


def _table_rows(conn: Any) -> int:
    """The table's row estimate (``pg_class.reltuples``), for the statistics' shares."""
    row = conn.execute("SELECT reltuples FROM pg_class WHERE oid = 'search.chunks'::regclass"
                       ).fetchone()
    return max(int(row[0] or 0), 1) if row else 1


def fuzzy(conn: Any, scope: dict[str, Any], question: str, limit: int
          ) -> list[tuple[str, float]]:
    """Trigram word similarity of each content word (4+ letters) to the chunk header,
    summed: a misspelt name or subject word still finds its chunk."""
    # Plain words only: a typo is of a word, and a compound token ("lot-trace",
    # "map.md", "procure-to-gl") no row holds as typed is not one; its trigram scan of
    # every header ran past the statement timeout (3.2 s, measured) and found nothing.
    terms = [t for t in words([question]) if len(t) >= 4 and t.isalpha()][:8]
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
