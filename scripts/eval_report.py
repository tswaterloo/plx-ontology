"""Accuracy report of recorded case-card runs, against the "good enough" bar.

    python3 -m scripts.eval_report <run dir> [<run dir> ...] [--as-saved]
        [--bar evals/bar.json] [--out DIR]

A run dir is a scoreboard run (``scripts.eval_case_cards run --work-dir <dir>/work``): its
``work/runs/<item id>/p0-r0.score.json`` and ``.transcript.json`` files. Runs are read in
the order given and the latest score of each card is its **headline** outcome (a later
run overrides an earlier one, as a partial re-run of a run's failures does); a card scored
in two or more runs gives **stability**: for the bar, a card's latest two runs on the
product version of its latest run agree (a run is ``DIR@VERSION``; runs given no version
are one version), and beside it every run given agrees (``all_runs``). Every transcript is scored again with this
checkout's compiled cards and scorer (``evals.score.score``), so runs scored by older
scorer versions and cards whose expected answer changed are read alike; ``--as-saved``
reads the saved scores as written instead.

Every card this checkout compiles (``scripts.eval_case_cards.compile_cards``) is in the
denominator: a gradable card is one not marked unscored; a gradable card no run scored
counts ``not_run`` and is never correct (fail closed: a run of half the cards never reads
as a pass). A refused card is listed and counted apart.

Per card, from its headline score:

- **correct with evidence**, raw: outcome ``correct`` (a rubric card is correct only with
  an evidenced claim, scorer /17); **audited**: and not resting only on own-document
  claims no audit judged (``evidence.own_document_only`` without ``own_document_audited``,
  scorer /21);
- **confident wrong** (the key risk number): ``wrong`` while stating an answer (not
  abstained); ``confident_unsupported`` is its subset the claim gate refused;
- **justified abstention**: the scorer's ``justified_abstention`` (it named the gap and the
  card allows no answer); an **unjustified abstention** is ``wrong`` while abstaining;
- **blocked**: the run says nothing about the agent (``blocked``: a failed session, an
  original the host did not hold, a read gap); with ``not_run`` it is **unsupported or
  blocked**.

Groups: per use case (the card's set), per layer and per question type (``question_type``:
lookup, list, why/trace, compute, document; a card may name its own). Answer time: p50 and
p95 (nearest rank) of ``measure.time_to_answer_ms`` (else ``metrics.duration_ms``) of the
headline runs. The bar (``evals/bar.json``) lists targets over these metrics, each pass or
fail; a metric with nothing to measure fails (``no_data``).

Writes ``eval-report.json`` and ``eval-report.md`` to ``--out`` (default the last run dir)
and prints the markdown. Counts, rates and card ids only: no answer text.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "plx-eval-report/v1"
BAR_PATH = ROOT / "evals" / "bar.json"
BAR_FORMAT = "plx-eval-bar/v1"
QUESTION_TYPES = ("lookup", "list", "why/trace", "compute", "document")
GRADED = ("correct", "wrong", "justified_abstention", "blocked", "not_run")
_COMPUTE = re.compile(r"\b(how (much|many|long)|total|sum|average|per[- ]unit|margin|"
                      r"percent(age)?|days? of supply|is left|remaining)\b", re.I)
_TRACE = re.compile(r"^\s*why\b|\b(why|trace|chain|traced|lineage|reach(ed)?|led to|"
                    r"caused|from which|which .{0,40} (carries|fulfils|fulfills|"
                    r"produced|paid for|supports?))\b", re.I)
_LIST = re.compile(r"^\s*(list|which\b.{0,60}\b(\w+s)\b|what (are|were)\b)", re.I)


def question_type(question: str, layer: str | None, own: str | None = None) -> str:
    """A card's question type: its own ``question_type`` when it names one of
    ``QUESTION_TYPES``; else compute (an arithmetic card, or a quantity asked), why/trace
    (a why, or a chain of records: a tracing card), list (``List``/``Which ...s``/``What
    are``), document (what a case's documents say: a comprehension card), else lookup."""
    if own in QUESTION_TYPES:
        return own
    if layer == "arithmetic" or _COMPUTE.search(question):
        return "compute"
    if layer == "tracing" or _TRACE.search(question):
        return "why/trace"
    if _LIST.search(question):
        return "list"
    if layer == "comprehension":
        return "document"
    return "lookup"


# --------------------------------------------------------------------------- read

def cards(sets: Mapping[str, Any] | None = None, now: str = "2026-01-01T00:00:00Z"
          ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """(item id -> card row ``{case, card_id, layer, type, unscored, item}``, refused rows)
    of the cards this checkout compiles."""
    from scripts import eval_case_cards as tool

    sets = tool.card_sets() if sets is None else sets
    compiled, refused = tool.compile_cards(sets, now=now)
    own = {(case, str(c.get("id"))): c.get("question_type")
           for case, (doc, _) in sets.items() for c in tool._cards(doc)
           if isinstance(c, Mapping)}
    out = {}
    for row in compiled:
        item = row["item"]
        question = str(item["question"]["text"]).split(": ", 1)[-1]
        out[item["id"]] = {"case": row["case"], "card_id": row["card_id"],
                           "layer": row["layer"],
                           "type": question_type(question, row["layer"],
                                                 own.get((row["case"], row["card_id"]))),
                           "unscored": bool(row.get("unscored")), "item": item}
    return out, [{"case": r["case"], "card_id": r["card_id"], "reasons": r["reasons"]}
                 for r in refused]


def run_scores(run: Path, items: Mapping[str, Mapping[str, Any]], rescore: bool
               ) -> dict[str, dict[str, Any]]:
    """Item id -> the run's score of its first phrasing (``p0-r0``), as saved or, with
    ``rescore``, scored again from its transcript with this checkout's item."""
    from evals import score as scoring

    out = {}
    for path in sorted((Path(run) / "work" / "runs").glob("*/p0-r0.score.json")):
        item_id = path.parent.name
        if rescore and item_id in items:
            transcript = json.loads(path.with_name("p0-r0.transcript.json")
                                    .read_text("utf-8"))
            out[item_id] = scoring.score(items[item_id]["item"], transcript)
        else:
            out[item_id] = json.loads(path.read_text("utf-8"))
    return out


# --------------------------------------------------------------------------- measure

def classify(score: Mapping[str, Any] | None) -> dict[str, Any]:
    """One card's headline classes from its score (module docstring); None: not run."""
    if score is None:
        return {"outcome": "not_run"}
    from evals import score as scoring

    outcome = scoring.outcome_of(score)
    evidence = score.get("evidence") or {}
    abstained = bool(score.get("abstained"))
    measure = score.get("measure") or {}
    ms = measure.get("time_to_answer_ms")
    if not isinstance(ms, (int, float)):
        ms = (score.get("metrics") or {}).get("duration_ms")
    return {"outcome": outcome,
            "correct_raw": outcome == "correct",
            "correct_audited": outcome == "correct" and not (
                evidence.get("own_document_only") and not evidence.get("own_document_audited")),
            "confident_wrong": outcome == "wrong" and not abstained,
            "confident_unsupported": scoring.confident_unsupported(score)
            and outcome == "wrong",
            "unjustified_abstention": outcome == "wrong" and abstained,
            "ms": ms if isinstance(ms, (int, float)) and not isinstance(ms, bool) else None,
            "codes": sorted(score.get("codes") or [])}


def rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def tally(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Counts and rates of some gradable cards' headline classes."""
    n = len(rows)
    count = {key: sum(bool(r.get(key)) for r in rows) for key in (
        "correct_raw", "correct_audited", "confident_wrong", "confident_unsupported",
        "unjustified_abstention")}
    outcomes = {o: sum(r["outcome"] == o for r in rows) for o in GRADED}
    abstentions = outcomes["justified_abstention"] + count["unjustified_abstention"]
    return {"cards": n, **outcomes, **count,
            "unsupported_or_blocked": outcomes["blocked"] + outcomes["not_run"],
            "correct_with_evidence_raw_rate": rate(count["correct_raw"], n),
            "correct_with_evidence_audited_rate": rate(count["correct_audited"], n),
            "confident_wrong_rate": rate(count["confident_wrong"], n),
            "justified_abstention_rate": rate(outcomes["justified_abstention"], n),
            "unsupported_or_blocked_rate": rate(outcomes["blocked"] + outcomes["not_run"], n),
            "abstentions": abstentions,
            "abstentions_justified_rate": rate(outcomes["justified_abstention"], abstentions)}


def percentile(values: Iterable[float], share: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


def stability(history: Mapping[str, Sequence[tuple[str, str]]]) -> dict[str, Any]:
    """The bar's stability: per card, its latest two runs on the product version of its
    latest run (``(version, outcome)`` in run order) agree; a card with fewer such runs is
    not compared. Beside it, ``all_runs``: every run of a card, any version, agrees."""
    latest_two = {}
    for item, runs in history.items():
        same = [outcome for version, outcome in runs if version == runs[-1][0]]
        if len(same) >= 2:
            latest_two[item] = same[-2:]
    stable = [item for item, pair in latest_two.items() if pair[0] == pair[1]]
    every = {item: {o for _, o in runs} for item, runs in history.items() if len(runs) >= 2}
    return {"compared": len(latest_two), "stable": len(stable),
            "stability_rate": rate(len(stable), len(latest_two)),
            "changed": sorted(item for item in latest_two if item not in stable),
            "all_runs": {"compared": len(every),
                         "stable": sum(len(o) == 1 for o in every.values()),
                         "stability_rate": rate(sum(len(o) == 1 for o in every.values()),
                                                len(every))}}


def check_bar(bar: Mapping[str, Any], metrics: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each bar target with its measured value and ``pass``/``fail`` (``no_data`` when the
    metric has no value: fail closed)."""
    ops = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, "==": lambda a, b: a == b}
    out = []
    for target in bar.get("targets") or []:
        value = metrics.get(target["metric"])
        floor = target.get("min")
        floor_value = metrics.get(floor["metric"]) if isinstance(floor, Mapping) else None
        if value is None or (floor is not None and floor_value is None):
            verdict = "fail"
            why = "no_data"
        elif floor is not None and not ops[floor["op"]](floor_value, floor["value"]):
            verdict, why = "fail", f"{floor['metric']} {floor_value} not {floor['op']} " \
                                   f"{floor['value']}"
        else:
            verdict = "pass" if ops[target["op"]](value, target["value"]) else "fail"
            why = None
        out.append({"id": target["id"], "metric": target["metric"], "op": target["op"],
                    "target": target["value"], "value": value, "result": verdict,
                    **({"why": why} if why else {}), "label": target.get("label", "")})
    return out


def run_version(run: Any) -> tuple[Path, str]:
    """A run argument ``DIR`` or ``DIR@VERSION`` (the product version it ran on; runs
    given no version are one version)."""
    text = str(run)
    path, sep, version = text.rpartition("@")
    return (Path(path), version) if sep and path else (Path(text), "")


def report(runs: Sequence[Path], *, rescore: bool = False,
           bar: Mapping[str, Any] | None = None, compiled: tuple | None = None
           ) -> dict[str, Any]:
    """The report document (module docstring)."""
    items, refused = compiled or cards()
    history: dict[str, list[tuple[str, str]]] = {}
    latest: dict[str, Mapping[str, Any]] = {}
    per_run = []
    for given in runs:
        run, version = run_version(given)
        scores = run_scores(run, items, rescore)
        per_run.append({"run": str(run), "version": version, "scored": len(scores),
                        "scorers": sorted({str(s.get("scorer")) for s in scores.values()})})
        for item_id, one in scores.items():
            if item_id in items:
                history.setdefault(item_id, []).append((version, classify(one)["outcome"]))
                latest[item_id] = one
    rows = []
    for item_id, card in sorted(items.items(), key=lambda kv: (kv[1]["case"],
                                                               kv[1]["card_id"])):
        row = {"item_id": item_id, "case": card["case"], "card_id": card["card_id"],
               "layer": card["layer"], "type": card["type"],
               **classify(latest.get(item_id))}
        if card["unscored"] or row["outcome"] == "unscored":
            row["outcome"] = "unscored"
        rows.append(row)
    graded = [r for r in rows if r["outcome"] != "unscored"]
    total = tally(graded)
    times = [r["ms"] for r in graded if r.get("ms") is not None]
    stable = stability({k: v for k, v in history.items()
                        if not items[k]["unscored"]})
    total.update({"unscored": len(rows) - len(graded), "refused": len(refused),
                  "answer_ms_p50": percentile(times, 0.5),
                  "answer_ms_p95": percentile(times, 0.95), "timed_runs": len(times),
                  "stability_rate": stable["stability_rate"],
                  "stability_compared_share": rate(stable["compared"], len(graded))})

    def grouped(key: str, keys: Iterable[str]) -> dict[str, Any]:
        return {k: tally([r for r in graded if r[key] == k]) for k in keys
                if any(r[key] == k for r in graded)}

    doc = {"schema": SCHEMA, "rescored": rescore, "runs": per_run,
           "basis": "headline = each card's latest score across the runs given; "
                    "denominator = every gradable card this checkout compiles (not run = "
                    "not correct); stability = cards scored in two or more runs",
           "totals": total,
           "by_use_case": grouped("case", sorted({r["case"] for r in rows})),
           "by_layer": grouped("layer", sorted({r["layer"] for r in rows})),
           "by_question_type": grouped("type", QUESTION_TYPES),
           "stability": stable,
           "unscored_cards": [r["card_id"] for r in rows if r["outcome"] == "unscored"],
           "refused_cards": refused,
           "not_correct": [{k: r[k] for k in ("case", "card_id", "item_id", "outcome",
                                              "codes") if k in r}
                           for r in graded if not r.get("correct_raw")]}
    if bar is not None:
        doc["bar"] = {"source": bar.get("source", ""), "targets": check_bar(bar, total)}
        doc["bar"]["passed"] = all(t["result"] == "pass" for t in doc["bar"]["targets"])
    return doc


# --------------------------------------------------------------------------- render

def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def markdown(doc: Mapping[str, Any]) -> str:
    t = doc["totals"]
    lines = ["# Eval accuracy report", "",
             f"Runs: {', '.join(Path(r['run']).name + ' (' + str(r['scored']) + ' scored, ' + '/'.join(r['scorers']) + ')' for r in doc['runs'])}"
             f"{'; re-scored with this checkout' if doc['rescored'] else '; saved scores'}.",
             f"Basis: {doc['basis']}.", "",
             "| Measure | Cards | Rate |", "|---|---|---|",
             f"| Gradable cards | {t['cards']} | |",
             f"| Correct with evidence, audited | {t['correct_audited']} | {_pct(t['correct_with_evidence_audited_rate'])} |",
             f"| Correct with evidence, raw | {t['correct_raw']} | {_pct(t['correct_with_evidence_raw_rate'])} |",
             f"| Confident wrong (key risk) | {t['confident_wrong']} | {_pct(t['confident_wrong_rate'])} |",
             f"| of which the claim gate refused | {t['confident_unsupported']} | |",
             f"| Justified abstention | {t['justified_abstention']} | {_pct(t['justified_abstention_rate'])} |",
             f"| Unjustified abstention | {t['unjustified_abstention']} | |",
             f"| Unsupported or blocked (blocked {t['blocked']}, not run {t['not_run']}) | {t['unsupported_or_blocked']} | {_pct(t['unsupported_or_blocked_rate'])} |",
             f"| Unscored (not gradable) | {t['unscored']} | |",
             f"| Refused at compile | {t['refused']} | |",
             f"| Answer time p50 / p95 | {t['timed_runs']} runs | {_fmt_ms(t['answer_ms_p50'])} / {_fmt_ms(t['answer_ms_p95'])} |",
             f"| Stable, latest 2 runs of one version | {doc['stability']['stable']} of {doc['stability']['compared']} | {_pct(doc['stability']['stability_rate'])} |",
             f"| Stable across all runs given | {doc['stability']['all_runs']['stable']} of {doc['stability']['all_runs']['compared']} | {_pct(doc['stability']['all_runs']['stability_rate'])} |",
             ""]
    if "bar" in doc:
        lines += [f"## Good-enough bar: {'PASS' if doc['bar']['passed'] else 'FAIL'}", "",
                  "| Target | Measured | Result |", "|---|---|---|"]
        for target in doc["bar"]["targets"]:
            value = target["value"]
            shown = _pct(value) if isinstance(value, float) and value <= 1 and \
                "rate" in target["metric"] else str(value)
            lines.append(f"| {target['label'] or target['id']} ({target['metric']} "
                         f"{target['op']} {target['target']}) | {shown} | "
                         f"{target['result'].upper()}"
                         f"{' (' + target['why'] + ')' if target.get('why') else ''} |")
        lines.append("")
    for title, key in (("Per use case", "by_use_case"), ("Per question type",
                                                         "by_question_type"),
                       ("Per layer", "by_layer")):
        lines += [f"## {title}", "", "| Group | Cards | Correct (audited) | Correct (raw) | "
                  "Confident wrong | Justified abst. | Blocked/not run |",
                  "|---|---|---|---|---|---|---|"]
        for name, g in doc[key].items():
            lines.append(f"| {name} | {g['cards']} | {g['correct_audited']} "
                         f"({_pct(g['correct_with_evidence_audited_rate'])}) | "
                         f"{g['correct_raw']} | {g['confident_wrong']} | "
                         f"{g['justified_abstention']} | {g['unsupported_or_blocked']} |")
        lines.append("")
    if doc["stability"]["changed"]:
        lines += ["Changed between runs: " + ", ".join(doc["stability"]["changed"]), ""]
    if doc["not_correct"]:
        lines += ["## Not correct", "", "| Case | Card | Outcome | Codes |", "|---|---|---|---|"]
        lines += [f"| {r['case']} | {r['card_id']} | {r['outcome']} | "
                  f"{', '.join(r.get('codes') or [])} |" for r in doc["not_correct"]]
        lines.append("")
    if doc["unscored_cards"]:
        lines += ["Unscored: " + ", ".join(doc["unscored_cards"]), ""]
    return "\n".join(lines)


def _fmt_ms(ms: Any) -> str:
    return "n/a" if ms is None else f"{ms / 1000:.0f} s"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--as-saved", action="store_true",
                        help="read the saved scores instead of scoring the transcripts again")
    parser.add_argument("--bar", type=Path, default=BAR_PATH)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    missing = [str(r) for r in args.runs
               if not (run_version(r)[0] / "work" / "runs").is_dir()]
    if missing:
        print(f"eval_report: not a run dir (no work/runs): {', '.join(missing)}",
              file=sys.stderr)
        return 1
    bar = json.loads(args.bar.read_text("utf-8"))
    if bar.get("format") != BAR_FORMAT:
        print(f"eval_report: {args.bar} is not {BAR_FORMAT}", file=sys.stderr)
        return 1
    doc = report(args.runs, rescore=not args.as_saved, bar={**bar, "source": str(args.bar)})
    out = args.out or run_version(args.runs[-1])[0]
    out.mkdir(parents=True, exist_ok=True)
    text = markdown(doc)
    (out / "eval-report.json").write_text(json.dumps(doc, indent=1) + "\n", "utf-8")
    (out / "eval-report.md").write_text(text + "\n", "utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
