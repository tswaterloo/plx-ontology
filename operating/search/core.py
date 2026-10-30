"""``search(understanding, actor, *, k=20, kinds=None) -> SearchResult`` (CONTRACT.md): hybrid retrieval over ``search.chunks`` for one reader.

1. The reader's scope: the audience units of the chunk table the reader's ReadPolicy
   grants and the markings they hold. No policy: nothing is searched (fail closed).
2. Retrievers in parallel, each with the permission predicate inside its SQL (exact ids,
   graph neighbours, dense kNN over the chunks embedded so far, BM25 with OR semantics,
   trigram typo match), plus the hidden count.
3. Fusion (RRF, or weighted sum), one candidate per source item (its best chunk) and per
   duplicate group, cross-encoder rerank of the top ``rerank_top``, then the intent's
   authority and recency weights.
4. The item gate on every hit about to be shown; a denied item is counted as hidden.
5. Hidden counts by owner or team (never a title, snippet, sender or handle) and every
   query logged to ``search.query_log`` with each shown hit's signal values.

Vectors find; records answer; the gate checks: nothing here supplies a value to an
answer. A hit's ``snippet`` is the search text (normalised, with its contextual header
stripped), not a quote: a quote is read through ``plx.read`` from the cited source.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from operating.search import retrievers as rt
from operating.search.config import (Weights, dsn as default_dsn, load_owners,
                                     load_weights, owner_of)
from operating.search.fusion import authority_signal, final_score, fuse, recency_signal
from operating.search.reader import Query, SearchReader, as_query

MAX_K = 100
SNIPPET_CHARS = 300
RERANK_PASSAGE_CHARS = 1200
RERANK_FLOOR = 0.1
STATEMENT_TIMEOUT_MS = 3000
_UNSET: Any = object()

Neighbours = Callable[[Sequence[str], Sequence[tuple[str, float]]],
                      Sequence[tuple[str, float]]]


@dataclass
class SearchResult:
    hits: list[dict[str, Any]]
    hidden: dict[str, Any]
    timings: dict[str, float]
    notes: list[str] = field(default_factory=list)
    query_log_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"hits": self.hits, "hidden": self.hidden, "timings": self.timings,
                "notes": self.notes, "query_log_id": self.query_log_id}


def connect(target: str | None = None) -> Any:
    """A connection that gives up on a dead server in 5 s and on any statement after
    ``STATEMENT_TIMEOUT_MS`` (the retrievers' own connections and the main one alike)."""
    import psycopg
    return psycopg.connect(target or default_dsn(), autocommit=True, connect_timeout=5,
                           options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")


class _Keys:
    """The chunk table's audience units, re-read at most once a minute per database."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, list[str]]] = {}

    def get(self, target: str, conn: Any) -> list[str]:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(target)
            if hit is not None and now - hit[0] < 60:
                return hit[1]
        keys = rt.audience_keys(conn)
        with self._lock:
            self._cache[target] = (now, keys)
        return keys

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


AUDIENCE_KEYS = _Keys()


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000.0, 1)


def _snippet(body: str, terms: Sequence[str]) -> str:
    text = " ".join((body or "").split())
    at = -1
    for term in terms:
        found = re.search(r"(?<!\w)" + re.escape(term), text, re.IGNORECASE)
        if found:
            at = found.start()
            break
    start = max(0, at - SNIPPET_CHARS // 3) if at >= 0 else 0
    piece = text[start:start + SNIPPET_CHARS]
    return ("…" if start else "") + piece + ("…" if start + SNIPPET_CHARS < len(text) else "")


def _empty(notes: list[str], timings: dict[str, float]) -> SearchResult:
    return SearchResult([], {"any": False, "shared_units": [], "private": False},
                        timings, notes)


def search(understanding: Any, actor: Any, *, k: int = 20,
           kinds: Sequence[str] | None = None, question: str | None = None,
           policy: Any = None, item_gate: Callable[[Any, str], bool] | None = None,
           neighbours: Neighbours | None = None, dsn: str | None = None,
           embedder: Any = _UNSET, reranker: Any = _UNSET,
           weights: Weights | None = None, owners: Mapping[str, str] | None = None,
           log: bool = True, hidden: bool = True, now: dt.datetime | None = None
           ) -> SearchResult:
    """See the module docstring. ``actor``: a ``SearchReader``, or an ``Actor`` with its
    ``policy`` (``ReadPolicy``) and ``item_gate(actor, handle)``. ``neighbours(resolved,
    candidates)``: (object_id, weight) of objects linked to the question's objects as
    this reader may see them (the projection's links); None: only the ambiguous
    entities' candidates are expanded."""
    started = time.perf_counter()
    timings: dict[str, float] = {}
    notes: list[str] = []
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= MAX_K:
        raise ValueError("search_k_invalid")
    if kinds is not None and (isinstance(kinds, str) or not all(
            isinstance(x, str) and x for x in kinds)):
        raise ValueError("search_kinds_invalid")
    query = as_query(understanding, question)
    lane_a = None   # (policy, actor): the scope is the reader_scope
    if isinstance(actor, SearchReader):
        reader = actor
    elif policy is not None:
        reader = SearchReader.from_policy(policy, actor, item_gate=item_gate)
        lane_a = (policy, actor)
    else:
        return _empty(["no_read_policy"], {"total": _ms(started)})
    if not query.question:
        return _empty(["empty_question"], {"total": _ms(started)})
    weights = weights or load_weights()
    intent = weights.for_intent(query.intent)
    owners = load_owners() if owners is None else owners
    embedder = _default_embedder() if embedder is _UNSET else embedder
    reranker = _default_reranker() if reranker is _UNSET else reranker
    now = now or dt.datetime.now(dt.timezone.utc)
    target = dsn or default_dsn()

    conn = connect(target)
    try:
        t = time.perf_counter()
        pg_scope = _pg_scope(conn, target, reader, lane_a, owners)
        allowed = list(pg_scope.allowed_audience_keys)
        scope = {"allowed": allowed, "held": list(pg_scope.held_markings),
                 "kinds": list(kinds) if kinds is not None else None}
        timings["scope"] = _ms(t)

        texts = query.texts()
        vectors: list[list[float]] = []
        model = getattr(embedder, "model", None)
        if embedder is not None:
            t = time.perf_counter()
            try:
                vectors = embedder.embed(texts)
            except Exception as exc:  # noqa: BLE001 - dense is skipped, stated
                notes.append("dense_unavailable:" + type(exc).__name__)
                vectors = []
            timings["embed"] = _ms(t)
        else:
            notes.append("dense_unavailable:no_embedder")

        resolved = query.resolved_ids()
        candidates = query.candidate_ids()
        # Each list reads at least k: a k past the per-retriever depth never loses rows.
        n = max(weights.candidates_per_retriever, k)
        jobs: dict[str, Callable[[Any], Any]] = {
            "exact_id": lambda c: rt.exact(c, scope, resolved, query.id_tokens(), n),
            "bm25": lambda c: rt.bm25(c, scope, texts, n),
            "fuzzy": lambda c: rt.fuzzy(c, scope, query.question, n),
        }
        if vectors and model:
            jobs["dense"] = lambda c: rt.dense(c, scope, vectors, model, n)
        if resolved or candidates:
            def graph_job(c: Any) -> Any:
                linked: list[tuple[str, float]] = []
                if neighbours is not None and resolved:
                    linked = list(neighbours(resolved, candidates))
                # An ambiguous entity's candidates are leads at half their score.
                linked += [(oid, 0.5 * score) for oid, score in candidates]
                return rt.graph(c, scope, linked, n)
            jobs["graph"] = graph_job
        if hidden:
            jobs["hidden"] = lambda c: _hidden_count(c, pg_scope, query, kinds)
        if not allowed:
            # Nothing granted: no retriever can return a row; only the hidden count runs.
            jobs = {k: v for k, v in jobs.items() if k == "hidden"}
            notes.append("no_granted_unit")
        lists, hidden_lists = _run(jobs, target, timings, notes)

        t = time.perf_counter()
        fused = fuse(lists, intent, weights.fusion, weights.rrf_k)
        timings["fuse"] = _ms(t)
        pool_size = max(weights.rerank_top, 3 * k)
        t = time.perf_counter()
        info = rt.details(conn, scope, [cid for cid, _ in fused[:pool_size * 3]])
        timings["details"] = _ms(t)
        reps = _representatives(fused, info, pool_size)
        terms = rt.words([query.question])
        rerank_scores: dict[str, float] = {}
        if reranker is not None and reps and weights.rerank_top > 0:
            t = time.perf_counter()
            top = reps[:weights.rerank_top]
            passages = [_passage(info[cid]) for cid, _ in top]
            scores, problem = reranker.score(query.question, passages,
                                             weights.rerank_timeout_s)
            if scores is not None and len(scores) == len(top):
                rerank_scores = {cid: min(max(float(s), 0.0), 1.0)
                                 for (cid, _), s in zip(top, scores)}
            elif problem:
                notes.append(problem)
            timings["rerank"] = _ms(t)
        elif reranker is None:
            notes.append("rerank_off")
        scored = rank(reps, info, lists, rerank_scores, intent, now)

        t = time.perf_counter()
        hits: list[dict[str, Any]] = []
        denied: list[tuple[str, str]] = []   # (audience_key, duplicate key)
        taken: set[str] = set()              # source items shown
        for score, cid, signals in scored:
            if len(hits) >= k:
                break
            row = info[cid]
            if row["source_handle"] in taken:
                continue       # already shown (a copy stood in for another hit)
            if not reader.item_allowed(row["source_handle"]):
                denied.append((row["audience_key"], row["dup_group"] or row["source_handle"]))
                # another copy of the same text the reader may open stands in for it
                row = _readable_copy(row, info, reader, taken)
                if row is None:
                    continue
                cid = row["chunk_id"]
            taken.add(row["source_handle"])
            hits.append({"handle": row["source_handle"], "object_id": row["object_id"],
                         "kind": row["kind"], "title": row["title"],
                         "snippet": _snippet(row["body"], terms), "score": round(score, 6),
                         "signals": signals, "chunk_id": cid,
                         "occurred_at": (row["occurred_at"].isoformat()
                                         if row["occurred_at"] else None),
                         "authority": row["authority"], "dup_group": row["dup_group"],
                         "also_in": {"count": 0, "handles": []}})
        timings["gate"] = _ms(t)
        groups = [h["dup_group"] for h in hits if h["dup_group"]]
        if groups:
            t = time.perf_counter()
            # Every readable copy of the shown clusters (permission predicate in SQL,
            # then the item gate per copy); a hidden copy is never listed or located.
            from operating.search import copies as cp
            from operating.pg.permissions import units_in
            try:
                found = cp.lookup(conn, scope, groups)
                units = units_in(conn)
            except Exception as exc:  # noqa: BLE001 - copies unknown, stated
                notes.append("copies_unavailable:" + type(exc).__name__)
                found, units = {}, {}
            for hit in hits:
                if hit["dup_group"] in found:
                    hit["also_in"] = cp.also_in(hit, found[hit["dup_group"]],
                                                reader.item_allowed, units)
            timings["duplicates"] = _ms(t)
        shown_groups = {h["dup_group"] or h["handle"] for h in hits}
        hidden_out = (_with_denied(hidden_lists, denied, shown_groups, pg_scope)
                      if hidden else {"asked": False})
        timings["total"] = _ms(started)
        result = SearchResult(hits, hidden_out, timings, notes)
        if log:
            t = time.perf_counter()
            result.query_log_id = _log(conn, reader.label, query, result)
            timings["log"] = _ms(t)
        return result
    finally:
        conn.close()


def rank(reps: Sequence[tuple[str, float]], info: Mapping[str, Mapping[str, Any]],
         lists: Mapping[str, Sequence[tuple[str, float]]], rerank_scores: Mapping[str, float],
         intent: Any, now: dt.datetime) -> list[tuple[float, str, dict[str, Any]]]:
    """(score, chunk_id, signals) best first: the reranked candidates, then (only when a
    k reaches past the reranked top) the rest by their unreranked score; a reranked and
    an unreranked score are never compared. The rerank relevance counts relative to the
    best reranked candidate, floored at ``RERANK_FLOOR``: when the cross-encoder finds
    every passage weak, it moves the order little. Pure: the tuning harness re-ranks
    cached candidates with it (``operating/search/tune.py``)."""
    best = reps[0][1] if reps else 1.0
    top = max(max(rerank_scores.values(), default=0.0), RERANK_FLOOR)
    first, rest = [], []
    for cid, fused_score in reps:
        row = info[cid]
        reranked = rerank_scores.get(cid)
        authority = authority_signal(row["authority"])
        recency = recency_signal(row["occurred_at"], now, intent.half_life_days)
        fused_norm = fused_score / best if best else 0.0
        signals = {name: _signal(lists.get(name), cid)
                   for name in ("exact_id", "graph", "dense", "bm25", "fuzzy")}
        score = final_score(fused_norm, None if reranked is None else reranked / top,
                            authority, recency, intent, exact=signals["exact_id"],
                            reranked_query=bool(rerank_scores))
        signals.update(authority=round(authority, 4),
                       recency=None if recency is None else round(recency, 4),
                       rerank=None if reranked is None else round(reranked, 4),
                       fused=round(fused_norm, 4))
        (rest if rerank_scores and reranked is None else first).append((score, cid, signals))
    first.sort(key=lambda s: (-s[0], s[1]))
    rest.sort(key=lambda s: (-s[0], s[1]))
    return first + rest


def _default_embedder() -> Any:
    from operating.search.models import default_embedder
    return default_embedder()


def _default_reranker() -> Any:
    from operating.search.models import default_reranker
    return default_reranker()


def _run(jobs: Mapping[str, Callable[[Any], Any]], target: str,
         timings: dict[str, float], notes: list[str]
         ) -> tuple[dict[str, list[tuple[str, float]]], dict[str, Any]]:
    """Each job on its own connection, in parallel. A failing retriever is left out and
    named in ``notes``; a failing hidden count is a note too (never a guessed count)."""
    def run(name: str, job: Callable[[Any], Any]) -> tuple[str, Any, float, str | None]:
        t = time.perf_counter()
        try:
            with connect(target) as c:
                # A list that cannot finish in time (statement_timeout) is left out
                # (named in notes), never waited for: the others still rank.
                return name, job(c), _ms(t), None
        except Exception as exc:  # noqa: BLE001 - one retriever down: the rest rank
            return name, None, _ms(t), type(exc).__name__
    lists: dict[str, list[tuple[str, float]]] = {}
    hidden: dict[str, Any] = {}
    if not jobs:
        return lists, hidden
    with cf.ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="plx-search") as pool:
        for name, value, ms, error in pool.map(lambda kv: run(*kv), jobs.items()):
            timings[name] = ms
            if error is not None:
                notes.append(f"{name}_failed:{error}")
                if name == "hidden":
                    hidden = {"failed": True}
            elif name == "hidden":
                hidden = value
            else:
                lists[name] = value
    return lists, hidden


def _signal(ranked: Sequence[tuple[str, float]] | None, cid: str) -> float | None:
    for chunk_id, score in ranked or ():
        if chunk_id == cid:
            return round(score, 6)
    return None


def _readable_copy(row: Mapping[str, Any], info: Mapping[str, Mapping[str, Any]],
                   reader: SearchReader, taken: set[str]) -> Mapping[str, Any] | None:
    """A copy of a denied representative's cluster among the candidates read (readable
    by the SQL predicate) that the item gate lets this reader open: highest tier, then
    earliest, then id (``chunking.dedup.representative``); None without one."""
    group = row["dup_group"]
    if not group:
        return None
    members = [r for r in info.values() if r["dup_group"] == group
               and r["source_handle"] != row["source_handle"]
               and r["source_handle"] not in taken]
    from operating.chunking.dedup import representative
    while members:
        best = representative([(r["chunk_id"], r["authority"] or 0,
                                r["occurred_at"].isoformat() if r["occurred_at"] else None)
                               for r in members])
        pick = next(r for r in members if r["chunk_id"] == best)
        if reader.item_allowed(pick["source_handle"]):
            return pick
        members = [r for r in members if r["source_handle"] != pick["source_handle"]]
    return None


def _representatives(fused: Sequence[tuple[str, float]], info: Mapping[str, dict],
                     limit: int) -> list[tuple[str, float]]:
    """One candidate per source item (its best chunk) and per duplicate group (its best
    copy), in fused order; a chunk the details read did not return (not readable) is
    dropped."""
    out: list[tuple[str, float]] = []
    seen: set[str] = set()
    for cid, score in fused:
        row = info.get(cid)
        if row is None:
            continue
        keys = {"h:" + row["source_handle"]}
        if row["dup_group"]:
            keys.add("g:" + row["dup_group"])
        if keys & seen:
            continue
        seen |= keys
        out.append((cid, score))
        if len(out) >= limit:
            break
    return out


def _passage(row: Mapping[str, Any]) -> str:
    """The contextual header (source, title, date, ids, what it is about) and the body:
    the header is what lets the cross-encoder see that a bare reply is about the order
    asked (0.21 against 0.01 without it on the fixture's late-order mail)."""
    return ((row.get("header") or row.get("title") or "") + "\n"
            + (row.get("body") or ""))[:RERANK_PASSAGE_CHARS]


def _pg_scope(conn: Any, target: str, reader: SearchReader, lane_a: Any,
              owners: Mapping[str, str]) -> Any:
    """The reader's ``SearchScope``: the ``reader_scope`` (policy, people exclusion,
    owner labels) for a policy reader; for a bare ``SearchReader`` the same shape from its
    own decisions under the same people exclusion (an excluded person's unit is never
    searched nor counted), a shared unit named by the owner an operator mapped
    (``PLX_SEARCH_OWNERS``) or the label, a personal one unnamed."""
    from operating.pg.permissions import SearchScope, reader_scope
    if lane_a is not None:
        return reader_scope(conn, *lane_a)
    from operating.pg.permissions import (WITHHELD_EXCLUSION, _label, is_excluded,
                                          is_shared, load_exclusion, units_in)
    exclusion = load_exclusion()
    units = units_in(conn)
    keys = [k for k in AUDIENCE_KEYS.get(target, conn)
            if not is_excluded(k, units.get(k), exclusion)]
    allowed = reader.allowed_keys(keys)
    countable: dict[str, str | None] = {}
    for key in keys:
        mapped = owner_of(key, owners)
        countable[key] = (None if not is_shared(key, units.get(key)) else
                          mapped if mapped != "another team" else _label(key, units.get(key)))
    withheld = WITHHELD_EXCLUSION if exclusion.unavailable else None
    return SearchScope(tuple(allowed), tuple(sorted(reader.held)),
                       {} if withheld else countable, withheld)


def _hidden_count(conn: Any, pg_scope: Any, query: Query, kinds: Sequence[str] | None
                  ) -> dict[str, Any]:
    """the ``hidden_counts`` (counts by owner, never a title): rows naming the
    resolved objects when the question has any, else rows holding every content word of
    the question (an AND match: a count of what clearly matches, never inflated by one
    common word)."""
    from operating.pg.permissions import hidden_counts
    resolved = query.resolved_ids()
    if resolved:
        return hidden_counts(conn, pg_scope, linked_ids=resolved, kinds=kinds)
    terms = rt.words([query.question])
    if not terms:
        return {"count": 0, "by_owner": []}
    return hidden_counts(conn, pg_scope, text=" ".join(terms), kinds=kinds)


def _with_denied(hidden: Mapping[str, Any], denied: Sequence[tuple[str, str]],
                 shown: set[str], pg_scope: Any) -> dict[str, Any]:
    """What a reader may be told about matches they cannot open (decision 2026-10-29):
    the names of the shared units holding matching hidden rows, never how many, and one
    unnamed ``private`` flag for everything else; excluded people never appear.
    Shown candidates the item gate denied are
    added the same way. A failed or withheld count is ``unknown``, never "nothing"."""
    if not hidden or hidden.get("failed") or hidden.get("count") is None:
        return {"any": None, "shared_units": [], "private": None, "unknown": True}
    shared = {o["owner_or_team"] for o in hidden.get("by_owner", ())
              if o.get("named", True) and o.get("count")}
    private = any(not o.get("named", True) and o.get("count")
                  for o in hidden.get("by_owner", ()))
    for audience, group in denied:
        if group in shown or audience not in pg_scope.countable:
            continue
        label = pg_scope.countable.get(audience)
        if label is None:
            private = True
        else:
            shared.add(label)
    return {"any": bool(shared or private), "shared_units": sorted(shared),
            "private": private}


def _log(conn: Any, actor: str, query: Query, result: SearchResult) -> int | None:
    """One ``search.query_log`` row: the question, intent, entities, rewrites, every shown
    hit's signal values and the timings. A failing log never fails the search."""
    results = {"hits": [{"rank": i + 1, "handle": h["handle"], "chunk_id": h["chunk_id"],
                         "object_id": h["object_id"], "kind": h["kind"], "score": h["score"],
                         "signals": h["signals"]} for i, h in enumerate(result.hits)],
               "hidden": result.hidden, "notes": result.notes}
    entities = [{"text": e.text, "resolved": e.resolved, "ambiguous": e.ambiguous,
                 "candidates": [list(c) for c in e.candidates[:5]]} for e in query.entities]
    try:
        row = conn.execute(
            """INSERT INTO search.query_log (actor, question, intent, entities, rewrites,
                                              results, timings)
               VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb) RETURNING id""",
            (actor, query.question, query.intent, json.dumps(entities),
             json.dumps(list(query.rewrites)), json.dumps(results, default=str),
             json.dumps(result.timings))).fetchone()
        _maybe_prune(conn)
        return int(row[0])
    except Exception as exc:  # noqa: BLE001 - logged to stderr, never fatal
        print(f"search.query_log write failed: {type(exc).__name__}", file=sys.stderr)
        return None


def retention_days() -> int:
    """How long ``search.query_log`` rows are kept (``PLX_SEARCH_LOG_RETENTION_DAYS``,
    default 30; an unreadable or non-positive value keeps the default)."""
    try:
        days = int(os.environ.get("PLX_SEARCH_LOG_RETENTION_DAYS") or 30)
    except ValueError:
        return 30
    return days if days > 0 else 30


def prune_query_log(conn: Any, days: int | None = None) -> int:
    """Delete query log rows older than the retention; the number deleted."""
    days = retention_days() if days is None else days
    return conn.execute("DELETE FROM search.query_log WHERE at < now() - make_interval("
                        "days => %s)", (int(days),)).rowcount


_PRUNED: dict[str, float] = {}


def _maybe_prune(conn: Any) -> None:
    """Prune on write, at most once an hour per database and process."""
    key = rt._db(conn)
    now = time.monotonic()
    if now - _PRUNED.get(key, -1e9) >= 3600:
        _PRUNED[key] = now
        prune_query_log(conn)
