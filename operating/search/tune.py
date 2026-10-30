"""Weight tuning harness: coordinate ascent over the ranking weights per intent,
maximising recall@10 + MRR on a set of questions with their known source documents.

    python3 -m operating.search.tune --eval questions.jsonl [--reader all]
        [--dsn postgresql:///plx] [--rerank on|off] [--min-per-intent 5]
        [--out report.json] [--write weights.tuned.json]

``questions.jsonl``: one object per line, ``{"question": str, "intent": str (optional),
"understanding": {...} (optional), "relevant": [source handle | object id |
duplicate group, ...]}`` (the ``scripts.eval_retrieval`` set, or the dev cards'
source documents; never the held-out set).

The retrievers and the cross-encoder run once per question (every candidate the fused
lists hold is reranked once); each weight setting then re-ranks those cached candidates
with the same ``rank`` the search uses, so a grid costs no database or model time. An
intent with fewer than ``--min-per-intent`` questions keeps the default weights (too
few to tune without fitting noise); every chosen value is reported beside its
before/after metric.

``--reader all`` reads every audience unit (an offline ranking measure over the
eval's own documents; nothing is shown to anyone). It is required: the harness never
picks a reader by default.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from operating.search import retrievers as rt
from operating.search.config import (FINAL_TERMS, RETRIEVERS, IntentWeights, Weights,
                                     load_weights)
from operating.search.core import (AUDIENCE_KEYS, _passage, _representatives, connect,
                                   rank)
from operating.search.fusion import fuse
from operating.search.reader import as_query

RETRIEVER_GRID = (0.0, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0)
TERM_GRID = (0.0, 0.05, 0.1, 0.2, 0.3, 0.45, 0.6)
HALF_LIFE_GRID = (7, 21, 45, 90, 180, 365, 3650)
DEPTH = 100
RERANK_CAP = 100


@dataclasses.dataclass
class Cached:
    question: str
    intent: str
    relevant: set[str]
    lists: dict[str, list[tuple[str, float]]]
    info: dict[str, dict[str, Any]]
    rerank: dict[str, float]
    # the targets (``scripts.eval_retrieval``): which target documents a candidate
    # row is (``match(row) -> target keys``) and how many can be found in a top 10
    # (``denominator``); None: ``relevant`` keys matched exactly, recall over all of them.
    match: Callable[[Mapping[str, Any]], set[str]] | None = None
    denominator: int | None = None


def load_eval(path: str | Path) -> list[dict[str, Any]]:
    text = Path(path).read_text(encoding="utf-8").strip()
    rows = json.loads(text) if text.startswith("[") else [
        json.loads(line) for line in text.splitlines() if line.strip()]
    out = []
    for row in rows:
        relevant = row.get("relevant")
        if (not isinstance(row.get("question"), str) or not isinstance(relevant, list)
                or not relevant):
            raise ValueError("eval_row_invalid: question and a non-empty relevant list")
        out.append(row)
    return out


def gather(rows: Sequence[Mapping[str, Any]], *, dsn: str | None, allowed: Iterable[str]
           | None, held: Iterable[str], embedder: Any, reranker: Any) -> list[Cached]:
    """Every retriever's list (DEPTH deep), the shown fields and one rerank score per
    representative candidate, per question."""
    conn = connect(dsn)
    out = []
    try:
        keys = AUDIENCE_KEYS.get(dsn or "", conn) if allowed is None else list(allowed)
        scope = {"allowed": keys, "held": sorted(held), "kinds": None}
        model = getattr(embedder, "model", None)
        for row in rows:
            query = as_query(row.get("understanding") or {
                "question": row["question"], "intent": row.get("intent")})
            texts = query.texts()
            linked = [(oid, 0.5 * s) for oid, s in query.candidate_ids()]
            jobs = {"exact_id": lambda: rt.exact(conn, scope, query.resolved_ids(),
                                                 query.id_tokens(), DEPTH),
                    "bm25": lambda: rt.bm25(conn, scope, texts, DEPTH),
                    "fuzzy": lambda: rt.fuzzy(conn, scope, query.question, DEPTH),
                    "graph": lambda: rt.graph(conn, scope, linked, DEPTH)}
            if embedder is not None:
                jobs["dense"] = lambda: rt.dense(conn, scope, embedder.embed(texts), model,
                                                 DEPTH)
            lists = {}
            for name, job in jobs.items():
                try:
                    lists[name] = job()
                except Exception as exc:  # noqa: BLE001 - left out, as search leaves it out
                    print(f"tune: {name} failed: {type(exc).__name__}", file=sys.stderr)
                    lists[name] = []
            union = list(dict.fromkeys(cid for ranked in lists.values() for cid, _ in ranked))
            info = rt.details(conn, scope, union)
            reps = _representatives([(cid, 0.0) for cid in union], info, RERANK_CAP)
            rerank: dict[str, float] = {}
            if reranker is not None and reps:
                scores = reranker.score(query.question, [_passage(info[c]) for c, _ in reps])
                rerank = {cid: min(max(float(s), 0.0), 1.0)
                          for (cid, _), s in zip(reps, scores)}
            out.append(Cached(query.question, query.intent, set(row["relevant"]), lists,
                              info, rerank))
    finally:
        conn.close()
    return out


def _keys(row: Mapping[str, Any]) -> set[str]:
    return {x for x in (row.get("source_handle"), row.get("object_id"), row.get("dup_group"))
            if x}


def ranked(item: Cached, weights: Weights, intent: IntentWeights, now: dt.datetime,
           k: int = 10) -> list[str]:
    fused = fuse(item.lists, intent, weights.fusion, weights.rrf_k)
    reps = _representatives(fused, item.info, max(weights.rerank_top, 3 * k))
    top = {cid for cid, _ in reps[:weights.rerank_top]}
    scores = {cid: s for cid, s in item.rerank.items() if cid in top}
    return [cid for _, cid, _ in rank(reps, item.info, item.lists, scores, intent, now)][:k]


def metrics(items: Sequence[Cached], weights: Weights, intent_of: Any,
            now: dt.datetime) -> dict[str, float]:
    """recall@10 (share of each question's relevant items in its top 10, averaged) and
    MRR (1 / rank of the first relevant hit, 0 when none in the top 10)."""
    if not items:
        return {"recall@10": 0.0, "mrr": 0.0, "n": 0}
    recall = mrr = 0.0
    for item in items:
        top = ranked(item, weights, intent_of(item), now)
        found: set[str] = set()
        first = None
        for position, cid in enumerate(top, start=1):
            row = item.info[cid]
            if item.match is None:
                hit = _keys(row) & item.relevant
                found |= hit
            else:   # one target per hit at most, as scripts.eval_retrieval counts
                hit = item.match(row)
                fresh = sorted(hit - found)
                if fresh:
                    found.add(fresh[0])
            if hit and first is None:
                first = position
        denominator = item.denominator or len(item.relevant)
        recall += min(1.0, len(found) / denominator) if denominator else 0.0
        mrr += 1.0 / first if first else 0.0
    return {"recall@10": round(recall / len(items), 4), "mrr": round(mrr / len(items), 4),
            "n": len(items)}


def _objective(m: Mapping[str, float]) -> float:
    return m["recall@10"] + m["mrr"]


def tune_intent(items: Sequence[Cached], weights: Weights, start: IntentWeights,
                now: dt.datetime, passes: int = 2) -> tuple[IntentWeights, dict, dict]:
    """Coordinate ascent from ``start``: each weight in turn takes the grid value that
    raises recall@10 + MRR most (a tie keeps the current value), ``passes`` times."""
    def score(candidate: IntentWeights) -> dict[str, float]:
        return metrics(items, weights, lambda _item: candidate, now)
    before = score(start)
    best, best_m = start, before
    for _ in range(passes):
        changed = False
        for name in RETRIEVERS:
            for value in RETRIEVER_GRID:
                trial = dataclasses.replace(best, retrievers=dict(best.retrievers, **{name: value}))
                m = score(trial)
                if _objective(m) > _objective(best_m) + 1e-9:
                    best, best_m, changed = trial, m, True
        for term in FINAL_TERMS:
            for value in TERM_GRID:
                trial = dataclasses.replace(best, **{term: value})
                m = score(trial)
                if _objective(m) > _objective(best_m) + 1e-9:
                    best, best_m, changed = trial, m, True
        for value in HALF_LIFE_GRID:
            trial = dataclasses.replace(best, half_life_days=float(value))
            m = score(trial)
            if _objective(m) > _objective(best_m) + 1e-9:
                best, best_m, changed = trial, m, True
        if not changed:
            break
    return best, before, best_m


def tune(items: Sequence[Cached], weights: Weights, *, min_per_intent: int = 5,
         now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.timezone.utc)
    report: dict[str, Any] = {"intents": {}, "overall": {}}
    chosen = dict(weights.intents)
    overall_before = metrics(items, weights, lambda i: weights.for_intent(i.intent), now)
    # The default weights are tuned on every question; an intent with enough questions
    # then gets its own.
    best, before, after = tune_intent(items, weights, weights.intents["default"], now)
    chosen["default"] = best
    report["intents"]["default"] = {"n": len(items), "before": before, "after": after,
                                    "weights": best.as_dict()}
    by_intent: dict[str, list[Cached]] = {}
    for item in items:
        by_intent.setdefault(item.intent, []).append(item)
    for intent, group in sorted(by_intent.items()):
        start = weights.intents.get(intent, chosen["default"])
        if len(group) < min_per_intent:
            report["intents"][intent] = {"n": len(group), "kept": "too few questions",
                                         "weights": start.as_dict()}
            continue
        best, before, after = tune_intent(group, weights, start, now)
        chosen[intent] = best
        report["intents"][intent] = {"n": len(group), "before": before, "after": after,
                                     "weights": best.as_dict()}
    tuned = dataclasses.replace(weights, intents=chosen)
    report["overall"] = {"before": overall_before,
                         "after": metrics(items, tuned, lambda i: tuned.for_intent(i.intent),
                                          now)}
    report["weights"] = tuned
    return report


def weights_json(weights: Weights, base: Mapping[str, Any], report: Mapping[str, Any]
                 ) -> dict[str, Any]:
    out = dict(base)
    out["intents"] = {name: w.as_dict() for name, w in weights.intents.items()}
    out["tuned"] = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "overall": report["overall"]}
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--eval", required=True)
    parser.add_argument("--reader", choices=("all",), required=True)
    parser.add_argument("--dsn")
    parser.add_argument("--rerank", choices=("on", "off"), default="on")
    parser.add_argument("--dense", choices=("on", "off"), default="on")
    parser.add_argument("--min-per-intent", type=int, default=5)
    parser.add_argument("--out")
    parser.add_argument("--write")
    args = parser.parse_args(argv)
    from operating.search.models import LocalReranker, default_embedder
    weights = load_weights()
    embedder = default_embedder() if args.dense == "on" else None
    reranker = None
    if args.rerank == "on":
        model = LocalReranker()
        reranker = type("R", (), {"score": staticmethod(model.score)})
    items = gather(load_eval(args.eval), dsn=args.dsn, allowed=None,
                   held=("finance", "l5", "pii", "purchasing_prices", "quality_controlled"),
                   embedder=embedder, reranker=reranker)
    report = tune(items, weights, min_per_intent=args.min_per_intent)
    tuned = report.pop("weights")
    text = json.dumps(report, indent=1)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    if args.write:
        from operating.search.config import WEIGHTS_PATH
        base = json.loads(WEIGHTS_PATH.read_text(encoding="utf-8"))
        Path(args.write).write_text(json.dumps(weights_json(tuned, base, report), indent=2)
                                    + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
