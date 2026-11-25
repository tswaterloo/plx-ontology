"""Sample the bank, run the live Ask on it, grade automatically, report.

    python3 -m evals.qbank.runner run --n 300 --out DIR --api-args "..." --mint CMD --link FILE [--conc 6]
    python3 -m evals.qbank.runner grade --out DIR

``run`` draws a stratified sample (system x kind, proportional, at least one per stratum,
seeded), asks each item through the app's own Ask built in this process from
``operator_api serve`` flags (``scripts.eval_generative.in_process``: the signed-in reader,
the served data, nothing sent anywhere) with ``PLX_ASK_SIMPLE=1`` set here only; a follow-up's
turns share one conversation and carry the earlier turns as ``history``, as the app's client
does; its last answer is graded. Answers land in
``DIR/answers/`` (a re-run resumes, skipping answered items); ``grade`` writes
``DIR/report.json``: accuracy and latency (first words, complete; p50/p95) by system, type,
kind, variant and source. The HOLDOUT split needs ``QBANK_HOLDOUT=1``.
Concurrency is capped at 3 on the main box (Postgres near its connection limit); point
``--api-args --pg-dsn`` and ``QBANK_PG_HOST`` at a replica and set ``QBANK_REPLICA=1`` to go wider.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import random
import shlex
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import bank
from .grade import grade


MAX_CONC = 3   # main box: Postgres runs near its connection limit (11-18)


def eligible(split: str, root: Path = bank.ROOT) -> list[dict[str, Any]]:
    if split not in ("tune", "holdout"):
        raise SystemExit(f"unknown split {split!r}")
    if split == "holdout" and os.environ.get("QBANK_HOLDOUT") != "1":
        raise SystemExit("the HOLDOUT split is reserved: set QBANK_HOLDOUT=1 only there")
    path = root / (bank.TUNE if split == "tune" else bank.HOLDOUT)
    return [i for i in bank.load(path) if not i.get("stale") and i.get("grading")]


def sample(items: Sequence[Mapping[str, Any]], n: int, seed: int) -> list[Mapping[str, Any]]:
    """n items over (system, kind) strata: proportional, every stratum at least once."""
    strata: dict[tuple[str, str], list[Mapping[str, Any]]] = collections.defaultdict(list)
    for it in sorted(items, key=lambda i: i["id"]):
        strata[(it["system"], it["kind"])].append(it)
    rng = random.Random(seed)
    for v in strata.values():
        rng.shuffle(v)
    if n >= len(items):
        return [i for v in strata.values() for i in v]
    quota = {k: max(1, math.floor(n * len(v) / len(items))) for k, v in strata.items()}
    while sum(quota.values()) > n:
        k = max(quota, key=lambda s: (quota[s], rng.random()))
        quota[k] -= 1
    spare = [k for k in sorted(strata, key=lambda s: -len(strata[s])) if len(strata[k]) > quota[k]]
    while sum(quota.values()) < n and spare:
        for k in list(spare):
            if sum(quota.values()) >= n:
                break
            quota[k] += 1
            if quota[k] >= len(strata[k]):
                spare.remove(k)
    return [i for k, v in strata.items() for i in v[:quota[k]]]


def _file(out: Path, item_id: str) -> Path:
    return out / "answers" / (item_id.replace(":", "_") + ".json")


def ask_all(items: Sequence[Mapping[str, Any]], out: Path, api_args: str, mint: str, link: str, conc: int) -> int:
    from scripts import eval_generative as eg
    from scripts import eval_live_ops as lo
    from scripts.eval_ask_general import answer_text
    (out / "answers").mkdir(parents=True, exist_ok=True)
    email = str(json.loads(os.environ.get("PLX_EVAL_READER") or "{}").get("email") or "")
    state = os.environ.get(lo.real_state_env())
    eg.reader_cleared(email, os.environ, Path(state) if state else None)

    def work(backend: Any, auth: Any) -> int:
        from operator_api import models as m
        session = eg.Session(auth, shlex.split(mint), Path(link).expanduser())
        session.login(0)
        if str(getattr(session.reader(), "email", "")).lower() != email.lower():
            raise SystemExit("signed-in reader is not the cleared reader")
        lock = threading.Lock()

        def one(item: Mapping[str, Any]) -> None:
            path = _file(out, item["id"])
            if path.exists():
                return
            conv = f"qb-{item['id'].replace(':', '-')}-{int(time.time())}"
            t0 = time.monotonic()
            first = final = err = None
            turns: list[dict[str, Any]] = []
            history: list[Any] = []   # the app's client sends the earlier turns with each question
            for turn in item.get("turns") or [item["question"]]:
                ts = time.monotonic()
                final = None
                try:
                    with lock:
                        reader = session.reader()
                    req = m.AskRequest(question=turn, conversation_id=conv, history=history[-12:])
                    for event, payload in backend.ask(reader, req):
                        pl = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
                        if event == "token" and first is None and str((pl or {}).get("text") or "").strip():
                            first = round(time.monotonic() - t0, 2)
                        if event == "answer":
                            final = pl
                except Exception as e:  # noqa: BLE001 - recorded; the item grades as unanswered
                    err = f"{type(e).__name__}: {e}"
                said = answer_text(final) if final else ""
                turns.append({"turn": turn, "s": round(time.monotonic() - ts, 2), "text": said})
                history += [m.HistoryTurn(role="user", text=turn[:4000]), m.HistoryTurn(role="assistant", text=said[:4000] or "(no answer)")]
                if err:
                    break
            rec = dict(final or {})
            rec.update(text=answer_text(final) if final else "", first_words_s=first, total_s=round(time.monotonic() - t0, 2),
                       error=err, turns=turns, item_id=item["id"])
            path.write_text(json.dumps(rec, indent=1, default=str))
            print(item["id"], first, rec["total_s"], err, flush=True)

        with ThreadPoolExecutor(conc) as ex:
            list(ex.map(one, items))
        return len(items)

    return eg.in_process(shlex.split(api_args), work)


def pct(xs: list[float], q: float) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    return round(xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))], 1) if xs else None


def report(out: Path) -> dict[str, Any]:
    picked = json.loads((out / "sample.json").read_text())
    current = {i["id"]: i for i in bank.all_items() if not i.get("stale")}
    rows = []
    for item in picked:
        item = current.get(item["id"], item)   # the truth as last refreshed
        p = _file(out, item["id"])
        ans = json.loads(p.read_text()) if p.exists() else None
        ok, why = grade(item, ans)
        rows.append({"id": item["id"], "ok": ok, "why": why, "answered": ans is not None, "system": item["system"],
                     "type": item["type"], "kind": item["kind"], "variant": item["variant"], "source": item["source"],
                     "evidence": item.get("evidence", "sql"), "first_words_s": ans and ans.get("first_words_s"),
                     "total_s": ans and ans.get("total_s"), "error": ans and ans.get("error")})
    done = [r for r in rows if r["answered"]]

    def group(key: str) -> dict[str, Any]:
        by = collections.defaultdict(list)
        for r in done:
            by[r[key]].append(r)
        return {k: {"n": len(v), "correct": sum(r["ok"] for r in v), "pct": round(100 * sum(r["ok"] for r in v) / len(v), 1),
                    "complete_p50_s": pct([r["total_s"] for r in v], 0.5)} for k, v in sorted(by.items())}

    summ = {"sampled": len(rows), "answered": len(done), "correct": sum(r["ok"] for r in done),
            "pct": round(100 * sum(r["ok"] for r in done) / len(done), 1) if done else None,
            "errors": sum(bool(r["error"]) for r in done),
            "first_words_p50_s": pct([r["first_words_s"] for r in done], 0.5), "complete_p50_s": pct([r["total_s"] for r in done], 0.5),
            "complete_p95_s": pct([r["total_s"] for r in done], 0.95)}
    rep = {"summary": summ, **{f"by_{k}": group(k) for k in ("system", "type", "kind", "variant", "source", "evidence")},
           "misses_by_reason": dict(collections.Counter(w.split(" (")[0].split(":")[0] for r in done if not r["ok"] for w in r["why"][:1])
                                    .most_common()), "items": rows}
    (out / "report.json").write_text(json.dumps(rep, indent=1, default=str))
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evals.qbank.runner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--n", type=int, default=300)
    r.add_argument("--seed", type=int, default=1)
    r.add_argument("--split", default="tune")
    r.add_argument("--out", required=True)
    r.add_argument("--api-args", required=True)
    r.add_argument("--mint", required=True)
    r.add_argument("--link", required=True)
    r.add_argument("--conc", type=int, default=2)
    gr = sub.add_parser("grade")
    gr.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    out = Path(a.out)
    if a.cmd == "run" and a.conc > MAX_CONC and os.environ.get("QBANK_REPLICA") != "1":
        raise SystemExit(f"--conc {a.conc} > {MAX_CONC}: each Ask holds database connections the main box's API needs "
                         "(QBANK_REPLICA=1 only when the run's --pg-dsn is a replica)")
    if a.cmd == "run":
        out.mkdir(mode=0o700, parents=True, exist_ok=True)
        sp = out / "sample.json"
        if not sp.exists():   # a resumed run keeps its sample
            sp.write_text(json.dumps(sample(eligible(a.split), a.n, a.seed), default=str))
        ask_all(json.loads(sp.read_text()), out, a.api_args, a.mint, a.link, a.conc)
    rep = report(out)
    print(json.dumps({"summary": rep["summary"], "by_system": {k: v["pct"] for k, v in rep["by_system"].items()},
                      "by_kind": {k: v["pct"] for k, v in rep["by_kind"].items()}, "misses": rep["misses_by_reason"]}))
    return 0


if __name__ == "__main__":
    from scripts.eval_guard import enforce
    enforce()
    sys.exit(main())
