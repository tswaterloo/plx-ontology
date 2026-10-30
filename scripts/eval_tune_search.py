"""Tune the hybrid search weights (``operating/search/weights.json``) on half of the graded
cards, as the eval's reader, and keep the other half for the report.

    python3 -m scripts.eval_tune_search --host-config FILE --out DIR
        [--phrasings original,m1,m2,m3] [--rerank-cap 60] [--min-per-intent 5]

* **Split** (``split``): the graded cards of each set, ordered by a hash of their item id,
  alternate between ``tune`` and ``held_out`` (each set on both sides; the same split on
  every run). ``DIR/split.json`` lists both halves; ``scripts.eval_retrieval --items``
  measures the held-out half.
* **Candidates** (``operating.search.tune.gather``): every retriever's list and one
  rerank score per candidate, per tune question, searched as the reader of
  ``PLX_EVAL_READER`` on the host of ``--host-config`` (``scripts.eval_retrieval.
  live_host``, its in-process session): the read policy that host decides for a tool
  call scopes the retrievers in SQL, and a candidate its item gate refuses is dropped
  before any weight is tried (the product skips it when shown). Nothing is shown to
  anyone; no question is logged.
* **Targets**: the (``scripts.eval_retrieval.targets``): a candidate matches a target
  document by any of its handles or its unique file name, or names the card's record;
  recall@10 counts each target once, over ``min(targets, 10)``, as the eval does.
* **Tuning** (``operating.search.tune.tune``): coordinate ascent per intent on the tune
  half only. ``DIR/tune-report.json`` holds before / after on the tune half;
  ``DIR/weights.tuned.json`` the weights (copy to ``operating/search/weights.json`` after
  the held-out half confirms them).

Read-only: the projection and capture stores and the search tables are only read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


def split(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """``{"tune": [item ids], "held_out": [item ids]}``: per set, cards ordered by the
    sha256 of their item id alternate between the halves (deterministic)."""
    by_set: dict[str, list[str]] = {}
    for row in rows:
        by_set.setdefault(str(row["case"]), []).append(str(row["item"]["id"]))
    out: dict[str, list[str]] = {"tune": [], "held_out": []}
    for case in sorted(by_set):
        ordered = sorted(by_set[case], key=lambda i: hashlib.sha256(i.encode()).hexdigest())
        for n, item_id in enumerate(ordered):
            out["tune" if n % 2 == 0 else "held_out"].append(item_id)
    return {name: sorted(ids) for name, ids in out.items()}


def matcher(wanted: Sequence[Mapping[str, Any]]) -> Any:
    """A candidate row (``search.chunks`` fields) -> the target keys it matches, as
    ``scripts.eval_retrieval.matches`` matches a shown hit (its handle, title, text)."""
    from scripts.eval_retrieval import matches

    def match(row: Mapping[str, Any]) -> set[str]:
        hit = {"handle": row.get("source_handle"), "title": row.get("title"),
               "snippet": row.get("body")}
        return {t["key"] for t in wanted if matches(hit, t)}
    return match


def gated(item: Any, allowed: Any) -> Any:
    """``item`` (``tune.Cached``) without the candidates the reader's item gate refuses."""
    keep = {cid for cid, row in item.info.items() if allowed(row["source_handle"])}
    item.info = {cid: row for cid, row in item.info.items() if cid in keep}
    item.lists = {name: [(cid, s) for cid, s in ranked if cid in keep]
                  for name, ranked in item.lists.items()}
    item.rerank = {cid: s for cid, s in item.rerank.items() if cid in keep}
    return item


def main(argv: Sequence[str] | None = None) -> int:
    import os
    from operating.search import tune
    from operating.search.config import WEIGHTS_PATH, load_weights
    from operating.search.models import LocalReranker, default_embedder
    from scripts import eval_case_cards as cards
    from scripts import eval_messy as messy
    from scripts import eval_retrieval as er

    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_tune_search",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--host-config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--phrasings", default="original,m1,m2,m3")
    parser.add_argument("--rerank-cap", type=int, default=60)
    parser.add_argument("--min-per-intent", type=int, default=5)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)
    kinds = [k.strip() for k in args.phrasings.split(",") if k.strip()]
    if not kinds or set(kinds) - set(er.PHRASINGS) or not 1 <= args.rerank_cap <= 100:
        parser.error("phrasings or rerank cap invalid")

    sets = cards.card_sets()
    compiled, _ = cards.compile_cards(sets, now=cards._now())
    halves = split(compiled)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "split.json").write_text(json.dumps(halves, indent=1) + "\n", "utf-8")
    picked = set(halves["tune"])
    rows = [r for r in compiled if r["item"]["id"] in picked]
    rows = rows[:args.limit] if args.limit > 0 else rows
    variants = messy.by_card(messy.load(messy.VARIANTS_PATH))
    registry = json.loads(cards.REGISTRY_PATH.read_text("utf-8")).get("rows") or []
    by_sha, sha_of = er.registry_handles(registry)
    catalog = er.catalog_documents(json.loads(er.CATALOG_PATH.read_text("utf-8")))

    host = er.live_host(os.environ, args.host_config)
    try:
        _session, server = host.agent_session()
        from evals.harness_reads import Session
        Session(server).host_code("plx.schema", {})   # its tools, as a tool call builds them
        ontology = server.ontology
        actor = server.case_runtime.employee_gate(server.actor)
        from operating.pg.permissions import reader_scope
        from operating.query_understanding import understand
        from operating.search.core import connect
        tune.RERANK_CAP = args.rerank_cap
        model = LocalReranker()
        model.warm()
        reranker = type("R", (), {"score": staticmethod(model.score)})
        embedder = default_embedder()
        items = []
        for n, row in enumerate(rows, 1):
            wanted = er.targets(row, by_sha, sha_of, catalog)
            if not wanted:
                continue
            texts = er.phrasings(row, messy.card_of(sets, str(row["case"]),
                                                     str(row["card_id"])),
                                 variants.get((str(row["case"]), str(row["card_id"]))))
            with ontology.request(), ontology.per_call():
                bound = getattr(ontology, "_call_bound", None)
                if bound is not None:
                    bound.search = True
                tools = ontology.tools
                policy = tools.policy = ontology.policy_now(actor)
                conn = connect()
                try:
                    scope = reader_scope(conn, policy, actor)
                finally:
                    conn.close()
                for kind in kinds:
                    if kind not in texts:
                        continue
                    understanding = understand(texts[kind], actor=actor, policy=policy,
                                               llm=False)
                    (item,) = tune.gather(
                        [{"question": texts[kind], "understanding": understanding,
                          "relevant": [t["key"] for t in wanted]}],
                        dsn=None, allowed=scope.allowed_audience_keys,
                        held=scope.held_markings, embedder=embedder, reranker=reranker)
                    item.match = matcher(wanted)
                    item.denominator = min(len(wanted), 10)
                    items.append(gated(item, lambda h: tools._item_allowed(actor, h)))
            print(f"cards: {n}/{len(rows)}", file=sys.stderr, flush=True)
    finally:
        host.close()
    weights = load_weights()
    report = tune.tune(items, weights, min_per_intent=args.min_per_intent)
    tuned = report.pop("weights")
    report["basis"] = {"cards": len(rows), "questions": len(items), "phrasings": kinds,
                       "rerank_cap": args.rerank_cap, "split": "split.json:tune"}
    (args.out / "tune-report.json").write_text(json.dumps(report, indent=1) + "\n", "utf-8")
    base = json.loads(WEIGHTS_PATH.read_text(encoding="utf-8"))
    (args.out / "weights.tuned.json").write_text(
        json.dumps(tune.weights_json(tuned, base, report), indent=2) + "\n", "utf-8")
    print(json.dumps(report["overall"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
