"""The Ask's offline answer bench: committed fixture items through the workspace's Ask.

    python3 -m scripts.eval_ask_bench --work-dir DIR [--bench evals/bench/ask_cards.json]
        [--only ID ...] [--session-seconds 300] [--planner-timeout 120] [--model M]

Every card of the bench file (``plx-ask-bench/v1``: ``id``, ``family``, ``expected``
answerability) names a dev item of the fixture world under ``evals/dev``; an id that is
not one, or whose ``expected`` is not the item's ``known_outcome.answerability``, refuses
the bench (exit 1) before any session. Each item runs once through the path
``eval.ask_agent`` measures (``scripts.eval_real_agent.AskAgent``: the reader's
``plx.find`` picks the root, ``ObjectWorkspace.ask_run`` runs ``operating.agent_loop.run``
unchanged, its bounds, the evidence rule and the claim gate) as the fixture principal on
the fixture world's projection, read at the world's now, and is scored by
``evals/score.py`` against the item's oracle outcome. The planner is
``operating.agent_loop.ClaudeCodePlanner`` (a headless Claude Code session; it spends
the operator's plan); tests inject one.

Per card: ``result`` one of ``correct`` (scored correct), ``wrong`` (an answer scored
wrong), ``refused`` (the claim gate refused every claim: confident unsupported, never
shown as an answer), ``abstained`` (abstained or escalated) or ``blocked`` (the root was
withheld, the session failed, or the scorer excluded the run); the stop class and reason,
``reason_type`` (the loop's own stop, or the planner's reason when it opens with one of
``operating.agent_loop.ABSTAIN_REASONS``, else ``untyped``), the tools called in order and
the scorer's codes. Totals: per result, ``answered_correct`` over cards,
``confident_unsupported`` (refused answers plus scored ones the scorer finds
unsupported), ``wrong_cited`` (answers scored wrong whose claims all passed their
citation checks: ``scripts.eval_real_agent.wrong_cited``) and ``untyped_abstentions``.
Evidence: ``isolated-fixture`` data;
``agent_evidence`` ``real-claude-session`` or ``injected-transport``; never live. Counts,
ids and enums only: transcripts and planner turns stay in ``--work-dir``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Sequence

from evals import runner
from evals import score as scoring
from scripts import eval_real_agent as era

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "evals" / "bench" / "ask_cards.json"
FORMAT = "plx-ask-bench/v1"
REPORT = "plx-ask-bench-report/v1"
RESULTS = ("correct", "wrong", "refused", "abstained", "blocked")
GATE_REFUSED = ("no_checked_claims", "no_cited_claims")


class BenchError(ValueError):
    pass


def load_bench(path: Path = BENCH, dev_dir: Path = era.DEV_DIR) -> list[dict[str, Any]]:
    """The bench's items, in its order: each card a dev item under ``dev_dir`` whose
    answerability is the card's ``expected`` (else ``BenchError``, nothing runs)."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, Mapping) or doc.get("format") != FORMAT \
            or doc.get("evidence_label") != "isolated-fixture":
        raise BenchError("bench_format")
    dev = {item["id"]: item for item in runner.load_items(dev_dir, split="dev")}
    items, seen = [], set()
    for card in doc.get("cards") or []:
        item = dev.get(card.get("id")) if isinstance(card, Mapping) else None
        if item is None or card["id"] in seen:
            raise BenchError(f"bench_card_unknown:{card.get('id') if isinstance(card, Mapping) else card}")
        if card.get("expected") != item["known_outcome"]["answerability"] \
                or card.get("family") != item["family"]:
            raise BenchError(f"bench_card_mismatch:{card['id']}")
        seen.add(card["id"])
        items.append(item)
    if not items:
        raise BenchError("bench_empty")
    return items


def bench_parts(work: Path, items: Sequence[Mapping[str, Any]]
                ) -> tuple[Callable[[Mapping[str, Any]], Any], Callable[[Mapping[str, Any]], Any]]:
    """(host_for, pin_for) of ``items`` on the fixture world: ``eval_real_agent``'s fixture
    sample over these items (its product build, store check, hosts and run pins)."""
    from unittest import mock
    with mock.patch.object(era, "sample_items",
                           lambda: {"exposed": [], "fixture_sample": list(items)}):
        _, host_for, pin_for = era.fixture_parts(work)["fixture_sample"]
    return host_for, pin_for


def reason_type(stop: Mapping[str, Any]) -> str | None:
    """The typed reason of a stop that is not an answer: the loop's own stop class, or the
    planner's ``<code>: <detail>`` with a code of ``ABSTAIN_REASONS``; else ``untyped``."""
    from operating.agent_loop import ABSTAIN_REASONS
    cls = stop.get("class")
    if cls == "answered":
        return None
    if cls not in ("planner_abstained", "planner_escalated"):
        return str(cls)
    code = str(stop.get("reason") or "").split(":", 1)[0].strip()
    return code if code in ABSTAIN_REASONS else "untyped"


def card_row(item: Mapping[str, Any], score: Mapping[str, Any],
             transcript: Mapping[str, Any]) -> dict[str, Any]:
    agent = transcript.get("agent") or {}
    stop = agent.get("stop") or {}
    outcome = scoring.outcome_of(score)
    if agent.get("root") == "withheld" or "stop" not in agent or outcome == "blocked":
        result = "blocked"
    elif outcome == "correct":
        result = "correct"
    elif stop.get("class") in GATE_REFUSED and (agent.get("claims") or {}).get("unsupported"):
        result = "refused"
    elif stop.get("class") != "answered":
        result = "abstained"
    else:
        result = "wrong"
    row = {"id": item["id"], "family": item["family"],
           "expected": item["known_outcome"]["answerability"], "result": result,
           "stop": stop.get("class") or agent.get("session") or agent.get("root"),
           "reason_type": reason_type(stop) if stop else None,
           "tools": [e.get("name") for e in transcript.get("events") or []
                     if e.get("type") == "tool_call"],
           "codes": sorted(score.get("codes") or []),
           "steps": stop.get("steps"),
           "refused": (agent.get("claims") or {}).get("refused") or []}
    row["confident_unsupported"] = result == "refused" or scoring.confident_unsupported(score)
    row["wrong_cited"] = result == "wrong" and era.wrong_cited(score)
    return row


def totals(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"cards": len(rows)}
    out.update({name: sum(r["result"] == name for r in rows) for name in RESULTS})
    out["answered_correct_rate"] = round(out["correct"] / len(rows), 4) if rows else None
    out["confident_unsupported"] = sum(bool(r["confident_unsupported"]) for r in rows)
    out["wrong_cited"] = sum(bool(r["wrong_cited"]) for r in rows)
    out["untyped_abstentions"] = sum(r["result"] == "abstained"
                                     and r["reason_type"] == "untyped" for r in rows)
    stops: dict[str, int] = {}
    for row in rows:
        stops[str(row["stop"])] = stops.get(str(row["stop"]), 0) + 1
    out["stops"] = dict(sorted(stops.items()))
    return out


def run_bench(items: Sequence[Mapping[str, Any]], planner: Any, work: Path, *,
              session_seconds: float = 300.0, planner_timeout: int = 120,
              model: str | None = None) -> list[dict[str, Any]]:
    """One Ask session per item (k=1); the per-card rows, in ``items``' order."""
    from operating import agent_loop
    _, host_for, pin_for = era.ask_parts({"bench": (items, *bench_parts(work, items))})["bench"]
    agent = era.AskAgent(planner, bounds=agent_loop.Bounds(), session_seconds=session_seconds,
                         planner_timeout=planner_timeout, model=model)
    out = work / "runs"
    scores = runner.run_items(items, agent, host_for, out=out, runs=1,
                              evidence_label="isolated-fixture", pin_for=pin_for)
    transcripts = {t["item_id"]: t for t in (
        json.loads(p.read_text(encoding="utf-8")) for p in out.rglob("*.transcript.json"))}
    return [card_row(item, score, transcripts[item["id"]])
            for item, score in zip(items, scores)]


def main(argv: Sequence[str] | None = None, *, planner: Any = None) -> int:
    """``planner``: an injected Ask planner (tests); its runs are ``injected-transport``."""
    from operating import agent_loop
    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_ask_bench",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--bench", type=Path, default=BENCH)
    parser.add_argument("--only", action="append", default=[],
                        help="run only this bench card (repeatable)")
    parser.add_argument("--session-seconds", type=float, default=300.0)
    parser.add_argument("--planner-timeout", type=int, default=120)
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument("--model")
    args = parser.parse_args(argv)
    try:
        items = load_bench(args.bench)
        if args.only:
            unknown = sorted(set(args.only) - {item["id"] for item in items})
            if unknown:
                raise BenchError(f"bench_card_unknown:{unknown[0]}")
            items = [item for item in items if item["id"] in args.only]
        args.work_dir.mkdir(parents=True, exist_ok=True)
        injected = planner is not None
        planner = planner or agent_loop.ClaudeCodePlanner(
            binary=args.claude_bin, model=args.model, timeout=args.planner_timeout)
        rows = run_bench(items, planner, args.work_dir, session_seconds=args.session_seconds,
                         planner_timeout=args.planner_timeout, model=args.model)
    except (BenchError, runner.RunnerError, scoring.ScoreError, ValueError, OSError) as exc:
        print(json.dumps({"error": type(exc).__name__, "code": str(exc)[:200]}),
              file=sys.stderr)
        return 1
    report = {"schema": REPORT, "evidence_label": "isolated-fixture",
              "agent_evidence": "injected-transport" if injected else "real-claude-session",
              "adapter_status": "live-unverified", "bench": str(args.bench.name),
              "totals": totals(rows), "cards": rows}
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
