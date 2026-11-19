"""IN-11 / AUD-02: the phrase-rule path (``operating.fast_answer``) on vs off, on the same
items, so the router call is removed only when the general (agent) path matches it.

    python3 -m evals.fast_answer_compare pick --root R --set held --n 40 --seed 1114 \\
        [--exclude IDS_FILE ...] > ids.txt
    python3 -m evals.fast_answer_compare plan --root R --set held --ids ids.txt \\
        --name in11-1114 --api-args "..."          # the two run commands, nothing run
    python3 -m evals.fast_answer_compare report --root R --on in11-1114-on \\
        --off in11-1114-off [--json OUT]

* **pick**: items of the set whose question the phrase-rule path takes
  (``fast_answer.serves``), minus every id in ``--exclude`` (items a run already spent),
  a seeded draw of ``--n``; one id per line. No item text is printed.
* **plan**: the two ``scripts.eval_generative run`` commands over the same ids, the off
  arm with ``PLX_FAST_ANSWER=off`` (``operator_api.backend.fast_off``: every question
  goes to the agent); each run under ``flock /data/scratch/eval.lock``, one arm after
  the other (never side by side: load changes the time limits, the 11-13 confound).
* **pick --per-family K --families OUT**: the draw per family instead (the recipe the
  phrase-rule path routes a question to, ``fast_answer.understand(...).intent``): at most
  ``K`` seeded ids of each, and ``OUT`` = ``{id: family}`` for the report.
* **report**: per arm correct / incomplete / wrong / no answer of the ids both arms
  graded (``verdicts.jsonl``), timeouts (``log.jsonl`` status), median and p90 seconds,
  the ids only one arm got right, and the decision: ``remove`` when the off arm is
  correct at least as often AND wrong no more often, ``keep`` otherwise,
  ``incomparable`` when the arms graded different ids or fewer than ``MIN_ITEMS``.
  With ``--families`` (pick's ``OUT``) the same per family too (``FAMILY_MIN_ITEMS``):
  the router goes only when no family's general path is worse (CAP-L180, DI-02).

Ids, outcomes and seconds only: no question or answer text is read or written.
Evidence: isolated-fixture tests (``evals/tests/test_fast_answer_compare.py``); a
report over real runs is ``live`` evidence of the runs it reads.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import shlex
import statistics
import sys
from typing import Any, Iterable, Mapping, Optional, Sequence

MIN_ITEMS = 15
FAMILY_MIN_ITEMS = 5
EVAL_LOCK = "/data/scratch/eval.lock"
OFF_ENV = "PLX_FAST_ANSWER"


def family(question: str) -> Optional[str]:
    """The recipe the phrase-rule path routes ``question`` to (None: it takes none)."""
    from operating.fast_answer import serves, understand
    if not serves(question):
        return None
    ask = understand(question)
    return None if ask is None else str(ask.intent)


def pick(items: Iterable[Mapping[str, Any]], n: int, seed: int,
         exclude: Iterable[str] = (), serves: Any = None) -> list[str]:
    """The seeded draw of ``n`` ids the phrase-rule path takes (module doc)."""
    if serves is None:
        from operating.fast_answer import serves
    spent = {str(i).strip() for i in exclude if str(i).strip()}
    ids = sorted(str(it["id"]) for it in items
                 if str(it.get("id")) not in spent and serves(str(it.get("question") or "")))
    random.Random(seed).shuffle(ids)
    return sorted(ids[:max(0, n)])


def pick_families(items: Iterable[Mapping[str, Any]], per_family: int, seed: int,
                  exclude: Iterable[str] = (), family_of: Any = None) -> dict[str, str]:
    """``{id: family}``: at most ``per_family`` seeded ids of each family (module doc)."""
    family_of = family if family_of is None else family_of
    spent = {str(i).strip() for i in exclude if str(i).strip()}
    groups: dict[str, list[str]] = {}
    for it in items:
        i = str(it.get("id"))
        if i in spent:
            continue
        name = family_of(str(it.get("question") or ""))
        if name is not None:
            groups.setdefault(name, []).append(i)
    out: dict[str, str] = {}
    for name in sorted(groups):
        ids = sorted(groups[name])
        random.Random(f"{seed}:{name}").shuffle(ids)
        out.update({i: name for i in ids[:max(0, per_family)]})
    return dict(sorted(out.items()))


def _lines(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text("utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def arm(run: Path) -> dict[str, dict[str, Any]]:
    """id -> {outcome, seconds, status} of one run (its last log line per id wins)."""
    log = {str(r.get("id")): r for r in _lines(run / "log.jsonl") if r.get("id")}
    out: dict[str, dict[str, Any]] = {}
    for v in _lines(run / "verdicts.jsonl"):
        i = str(v.get("item_id") or "")
        if not i:
            continue
        seen = log.get(i, {})
        seconds = seen.get("seconds")
        out[i] = {"outcome": str(v.get("outcome") or "no_answer"),
                  "seconds": float(seconds) if isinstance(seconds, (int, float)) else None,
                  "status": str(seen.get("status") or "unknown")}
    return out


def _stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts = {k: sum(r["outcome"] == k for r in rows)
              for k in ("correct", "incomplete", "wrong", "no_answer")}
    seconds = sorted(r["seconds"] for r in rows if r["seconds"] is not None)
    p90 = seconds[min(len(seconds) - 1, int(round(0.9 * (len(seconds) - 1))))] \
        if seconds else None
    return {**counts, "graded": len(rows),
            "share_correct": round(counts["correct"] / len(rows), 3) if rows else None,
            "timeouts": sum(r["status"] not in ("ok", "unknown") for r in rows),
            "median_seconds": round(statistics.median(seconds), 1) if seconds else None,
            "p90_seconds": p90}


def compare(on: Mapping[str, Mapping[str, Any]], off: Mapping[str, Mapping[str, Any]],
            min_items: int = MIN_ITEMS) -> dict[str, Any]:
    """The two arms over the ids both graded, and the decision (module doc)."""
    both = sorted(set(on) & set(off))
    only = sorted(set(on) ^ set(off))
    a, b = _stats([on[i] for i in both]), _stats([off[i] for i in both])
    if only or len(both) < min_items:
        decision = "incomparable"
        why = (f"{len(only)} ids graded in one arm only" if only else
               f"{len(both)} ids graded in both arms (under {min_items})")
    elif b["correct"] >= a["correct"] and b["wrong"] <= a["wrong"]:
        decision = "remove"
        why = (f"off {b['correct']}/{len(both)} correct, {b['wrong']} wrong vs on "
               f"{a['correct']}/{len(both)}, {a['wrong']} wrong: the general path matches")
    else:
        decision = "keep"
        why = (f"off {b['correct']}/{len(both)} correct, {b['wrong']} wrong vs on "
               f"{a['correct']}/{len(both)}, {a['wrong']} wrong")
    return {"items": len(both), "on": a, "off": b, "decision": decision, "why": why,
            "only_on_correct": [i for i in both if on[i]["outcome"] == "correct"
                                and off[i]["outcome"] != "correct"],
            "only_off_correct": [i for i in both if off[i]["outcome"] == "correct"
                                 and on[i]["outcome"] != "correct"],
            "unpaired": only}


def compare_families(on: Mapping[str, Mapping[str, Any]],
                     off: Mapping[str, Mapping[str, Any]], families: Mapping[str, str]
                     ) -> dict[str, Any]:
    """:func:`compare` over all ids, plus ``families``: the same per family (at least
    ``FAMILY_MIN_ITEMS`` each); the overall decision is ``remove`` only when every family's
    is (one ``keep`` keeps the router; one ``incomparable`` makes it incomparable)."""
    result = compare(on, off)
    per: dict[str, Any] = {}
    for name in sorted(set(families.values())):
        ids = {i for i, f in families.items() if f == name}
        per[name] = compare({i: v for i, v in on.items() if i in ids},
                            {i: v for i, v in off.items() if i in ids}, FAMILY_MIN_ITEMS)
    result["families"] = per
    decisions = {r["decision"] for r in per.values()}
    if result["decision"] == "remove" and decisions - {"remove"}:
        worst = "keep" if "keep" in decisions else "incomparable"
        result["why"] += "; families " + ", ".join(
            f"{k}={v['decision']}" for k, v in per.items() if v["decision"] != "remove")
        result["decision"] = worst
    return result


def table(result: Mapping[str, Any]) -> str:
    rows = ["| arm | correct | incomplete | wrong | no answer | timeouts | median s | p90 s |",
            "|---|---|---|---|---|---|---|---|"]
    for name in ("on", "off"):
        s = result[name]
        rows.append(f"| phrase-rule {name.upper()} | {s['correct']}/{s['graded']} | "
                    f"{s['incomplete']} | {s['wrong']} | {s['no_answer']} | {s['timeouts']} | "
                    f"{s['median_seconds']} | {s['p90_seconds']} |")
    for name, fam in (result.get("families") or {}).items():
        on, off = fam["on"], fam["off"]
        rows.append(f"| {name} | on {on['correct']}/{on['graded']} wrong {on['wrong']} | "
                    f"off {off['correct']}/{off['graded']} wrong {off['wrong']} | "
                    f"{fam['decision']} | | | | |")
    rows.append(f"Decision: {result['decision']} ({result['why']}).")
    return "\n".join(rows)


def plan(root: Path, split: str, ids: Sequence[str], name: str, api_args: str,
         concurrency: int = 1) -> list[str]:
    """The two run commands (module doc), on then off, one at a time under the lock."""
    joined = ",".join(ids)
    out = []
    for which in ("on", "off"):
        env = f"{OFF_ENV}=off " if which == "off" else ""
        out.append(f"flock {EVAL_LOCK} env {env}python3 -m scripts.eval_generative run "
                   f"--root {shlex.quote(str(root))} --set {split} --name {name}-{which} "
                   f"--concurrency {concurrency} --api-args {shlex.quote(api_args)} "
                   f"--ids {joined}")
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m evals.fast_answer_compare")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("pick")
    a.add_argument("--root", type=Path, required=True)
    a.add_argument("--set", required=True)
    a.add_argument("--n", type=int, default=40)
    a.add_argument("--seed", type=int, default=1114)
    a.add_argument("--exclude", type=Path, action="append", default=[])
    a.add_argument("--per-family", type=int, default=None,
                   help="draw at most this many ids per family instead of --n overall")
    a.add_argument("--families", type=Path, default=None,
                   help="with --per-family: write {id: family} here (for report)")
    b = sub.add_parser("plan")
    b.add_argument("--root", type=Path, required=True)
    b.add_argument("--set", required=True)
    b.add_argument("--ids", type=Path, required=True)
    b.add_argument("--name", required=True)
    b.add_argument("--api-args", required=True)
    b.add_argument("--concurrency", type=int, default=1)
    c = sub.add_parser("report")
    c.add_argument("--root", type=Path, required=True)
    c.add_argument("--on", required=True)
    c.add_argument("--off", required=True)
    c.add_argument("--json", type=Path)
    c.add_argument("--families", type=Path, default=None,
                   help="pick's {id: family} file: the decision per family too")
    args = p.parse_args(argv)
    if args.cmd == "pick":
        from scripts.eval_generative import load_items
        spent = [ln for f in args.exclude for ln in f.read_text("utf-8").split()]
        items = load_items(args.root, args.set)
        if args.per_family is not None:
            fams = pick_families(items, args.per_family, args.seed, spent)
            if args.families:
                args.families.write_text(json.dumps(fams, indent=1, sort_keys=True), "utf-8")
            print("\n".join(fams))
            return 0
        print("\n".join(pick(items, args.n, args.seed, spent)))
        return 0
    if args.cmd == "plan":
        ids = [ln.strip() for ln in args.ids.read_text("utf-8").splitlines() if ln.strip()]
        print("\n".join(plan(args.root, args.set, ids, args.name, args.api_args,
                             args.concurrency)))
        return 0
    on, off = arm(args.root / "runs" / args.on), arm(args.root / "runs" / args.off)
    if args.families:
        result = compare_families(on, off, json.loads(args.families.read_text("utf-8")))
    else:
        result = compare(on, off)
    if args.json:
        args.json.write_text(json.dumps(result, indent=1, sort_keys=True), "utf-8")
    print(table(result))
    return 0 if result["decision"] != "incomparable" else 3


if __name__ == "__main__":
    from scripts.eval_guard import enforce
    enforce()
    sys.exit(main())
