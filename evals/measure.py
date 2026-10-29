"""What an investigation cost and left open, per question, beside the manual process (ENG-11).

**Per run** (``of_run``, ``plx-eval-measure/v1``): a pure function of a recorded transcript
and its score inputs, so a replayed transcript measures identically.

* ``time_to_first_cited_evidence_ms``: when the first tool result that holds a handle the
  final answer cites came back (the event's ``t_ms``, milliseconds since the run started;
  ``evals.runner`` stamps the events of in-process agents). None when the answer cites
  nothing a tool returned, or the events carry no time (a Claude Code session's stream
  has none): unmeasured, never zero.
* ``time_to_answer_ms``: wall time from the prompt to the final message
  (``transcript.timing.answer_ms``).
* ``tool_calls``.
* ``resolution``: ``answered`` (a stated answer, right or wrong), ``justified_abstention``
  (abstained where the item expects it), ``unanswered`` (no answer, or an abstention where
  the item has one), or None for a ``blocked`` run, which says nothing about the agent.
* ``unsupported_assertions``: answer values that state a value with no citation the
  harness could re-read (``evidence[handle].ok``), plus numbers in the answer text that no
  value claim covers (the claim gate's ``claim_unstructured``). A resolvable citation of a
  wrong value is supported (and scored wrong), not counted here.
* ``review_items``: what the run leaves for a person: ``proposals`` (successful calls to a
  ``propose`` tool), ``identity_reviews`` (distinct identity edges no person confirmed,
  tier ``lead`` or ``contradicted``, among the objects' ``identity`` edges, or a
  ``plx.identity`` result's, the run read) and ``unresolved_conflicts`` (distinct conflict
  subjects in tool results or the answer's ``CONFLICT_REASON`` gaps: the Workbench
  never picks between sources). A tool path whose answer is the tool result
  (``of_calls``, e.g. ``scripts/eval_unfamiliar_sample.py``'s ``plx.find``) adds the root
  choice an ambiguous or lead-only find leaves to a person.

``summarize`` puts these beside coverage and accuracy in ``evals.score.aggregate`` and in the
live eval blocks; it holds counts and milliseconds only, never a value or an item id.

**Manual baseline** (``evals/manual_baseline.schema.json``, ``plx-eval-manual-baseline/v1``):
a person investigates the same item the way the work is done today and records the
stopwatch times. Records live in ``MANUAL_DIR`` (or a directory given to the CLI); an
invalid record is refused, never skipped. ``compare`` pairs records with live runs of the
same item (evidence label ``live-captures`` / ``live-read``: a fixture run timed against a
person on real systems is no comparison) and reports only the paired items; with no pair it
says ``unmeasured`` and carries no time, difference or saving at all. Nothing in the
product states a time saving except through a paired ``compare``.

CLI (``python3 -m evals.measure compare --scores <run dir> [--manual <dir>]``) reads the
``*.score.json`` files ``evals.runner`` wrote and prints the comparison (counts only).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import statistics
import sys
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

MEASURE_FORMAT = "plx-eval-measure/v1"
MANUAL_FORMAT = "plx-eval-manual-baseline/v1"
COMPARISON_FORMAT = "plx-eval-manual-comparison/v1"
RESOLUTIONS = ("answered", "justified_abstention", "unanswered")
REVIEW_KINDS = ("proposals", "identity_reviews", "unresolved_conflicts")
REVIEW_TIERS = frozenset({"lead", "contradicted"})
# The one gap reason for a conflict between source statements: the baseline emits it, the
# runner prompt asks for it, the use-case compiler expects it and the scorer matches it.
CONFLICT_REASON = "conflicting_sources"
LIVE_LABELS = frozenset({"live-captures", "live-read"})
WALL_CLOCK = ("first_cited_evidence_ms", "answer_ms")  # summary keys holding wall times
REPO_ROOT = Path(__file__).resolve().parents[1]
MANUAL_SCHEMA_PATH = REPO_ROOT / "evals" / "manual_baseline.schema.json"
MANUAL_DIR = REPO_ROOT / "evals" / "manual"
_PROPOSE = re.compile(r"propose")


class MeasureError(ValueError):
    """A manual baseline record that is not a valid ``plx-eval-manual-baseline/v1``, or a
    measure with a resolution outside ``RESOLUTIONS``."""


def _walk(node: Any) -> Iterable[Mapping[str, Any]]:
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            yield current
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _failed(event: Mapping[str, Any]) -> bool:
    result = event.get("result")
    return bool(event.get("is_error")) or (isinstance(result, Mapping) and (
        result.get("ok") is False or "error" in result))


def cited_handles(answer: Mapping[str, Any] | None) -> list[str]:
    return list(dict.fromkeys(
        str(cite.get("l1_handle")) for value in (answer or {}).get("values") or []
        if isinstance(value, Mapping) for cite in value.get("citations") or []
        if isinstance(cite, Mapping) and cite.get("l1_handle")))


def _cites(result: Any) -> bool:
    return any(isinstance(node.get(key), str) and node[key]
               for node in _walk(result) for key in ("l1_handle", "citation"))


def first_cited_ms(events: Sequence[Mapping[str, Any]],
                   handles: Iterable[str] | None) -> int | None:
    """``t_ms`` of the first tool call whose result holds one of ``handles`` (None: any
    citation, for a path whose answer is the tool result itself, e.g. a ``plx.find``)."""
    wanted = None if handles is None else [handle for handle in handles if handle]
    if wanted == []:
        return None
    for event in events:
        if event.get("type") != "tool_call" or not isinstance(event.get("t_ms"), int) \
                or _failed(event):
            continue
        result = event.get("result")
        if _cites(result) if wanted is None else any(
                handle in json.dumps(result, ensure_ascii=False, default=str)
                for handle in wanted):
            return event["t_ms"]
    return None


def unsupported_assertions(answer: Mapping[str, Any] | None, evidence: Mapping[str, Any],
                           gate_failures: Sequence[Mapping[str, Any]] = ()) -> int:
    """Stated answer values without a resolvable citation, plus uncovered text numbers. A
    calculated value cites its inputs (``calculation.inputs``: the format carries no
    ``citations`` beside a calculation): it is supported only if every one of them
    resolves, since an input the run did not resolve is a figure nothing backs."""
    def resolved(cite: Any) -> bool:
        handle = cite.get("l1_handle") if isinstance(cite, Mapping) else None
        return (isinstance(handle, str) and isinstance(evidence.get(handle), Mapping)
                and bool(evidence[handle].get("ok")))

    count = 0
    for value in (answer or {}).get("values") or []:
        if not isinstance(value, Mapping) or value.get("value") is None:
            continue
        calculation = value.get("calculation")
        inputs = calculation.get("inputs") if isinstance(calculation, Mapping) else None
        if isinstance(inputs, list) and inputs:
            supported = all(resolved(cite) for cite in inputs)
        else:
            supported = any(resolved(cite) for cite in value.get("citations") or [])
        if not supported:
            count += 1
    return count + sum(1 for failure in gate_failures
                       if failure.get("code") == "claim_unstructured")


def review_items(events: Sequence[Mapping[str, Any]],
                 answer: Mapping[str, Any] | None) -> dict[str, int]:
    proposals = sum(1 for event in events if event.get("type") == "tool_call"
                    and _PROPOSE.search(str(event.get("name") or "")) and not _failed(event))
    edges: set[str] = set()
    conflicts: set[str] = set()
    for event in events:
        if event.get("type") != "tool_call" or _failed(event):
            continue
        keys = ("identity", "edges") if event.get("name") == "plx.identity" else ("identity",)
        for node in _walk(event.get("result")):
            for key in keys:
                listed = node.get(key)
                for edge in listed if isinstance(listed, list) else []:
                    if isinstance(edge, Mapping) and isinstance(edge.get("link_id"), str) \
                            and edge.get("tier") in REVIEW_TIERS:
                        edges.add(edge["link_id"])
            listed = node.get("conflicts")
            for conflict in listed if isinstance(listed, list) else []:
                if isinstance(conflict, Mapping) and conflict.get("subject"):
                    conflicts.add(str(conflict["subject"]))
    for gap in (answer or {}).get("gaps") or []:
        if isinstance(gap, Mapping) and gap.get("reason") == CONFLICT_REASON:
            conflicts.add(str(gap.get("subject")))
    counts = {"proposals": proposals, "identity_reviews": len(edges),
              "unresolved_conflicts": len(conflicts)}
    return {**counts, "total": sum(counts.values())}


def resolution_of(outcome: str, answer: Mapping[str, Any] | None,
                  abstained: bool) -> str | None:
    """``evals.score`` outcome -> resolution (None: blocked, not about the agent)."""
    if outcome == "blocked":
        return None
    if outcome == "justified_abstention":
        return "justified_abstention"
    return "answered" if answer is not None and not abstained else "unanswered"


def of_run(transcript: Mapping[str, Any], answer: Mapping[str, Any] | None, *,
           outcome: str, abstained: bool,
           gate_failures: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """The measurement of one recorded run (module docstring): ``answer`` is the scored
    answer (None when missing or malformed), ``abstained`` ``evals.score.abstained`` of it,
    ``gate_failures`` the claim gate's failures over it."""
    events = [e for e in transcript.get("events") or [] if isinstance(e, Mapping)]
    answer_ms = (transcript.get("timing") or {}).get("answer_ms")
    return {"format": MEASURE_FORMAT,
            "evidence_label": transcript.get("evidence_label"),
            "time_to_first_cited_evidence_ms": first_cited_ms(events, cited_handles(answer)),
            "time_to_answer_ms": answer_ms if isinstance(answer_ms, int) else None,
            "tool_calls": sum(1 for e in events if e.get("type") == "tool_call"),
            "resolution": resolution_of(outcome, answer, abstained),
            "unsupported_assertions": unsupported_assertions(
                answer, transcript.get("evidence") or {}, gate_failures),
            "review_items": review_items(events, answer)}


class CallLog:
    """A tool-call function that records each call as a transcript-shaped event with its
    ``t_ms``, for paths that call tools directly instead of through ``evals.runner``."""

    def __init__(self, call: Callable[[str, Mapping[str, Any]], Any]) -> None:
        self.call, self.events = call, []
        self.started = time.monotonic()

    def __call__(self, name: str, arguments: Mapping[str, Any]) -> Any:
        result = self.call(name, arguments)
        self.events.append({"seq": len(self.events), "type": "tool_call", "name": name,
                            "result": result, "t_ms": self.elapsed_ms()})
        return result

    def elapsed_ms(self) -> int:
        return round((time.monotonic() - self.started) * 1000)


def of_calls(log: CallLog, *, evidence_label: str | None, resolution: str | None,
             unsupported: int, identity_reviews: int = 0) -> dict[str, Any]:
    """The measurement of a tool path's answer (``of_run``'s shape): the answer is the
    tools' own result, so the first result holding any citation is the first evidence;
    ``identity_reviews`` adds the choices the path leaves to a person (an ambiguous root)."""
    review = review_items(log.events, None)
    review["identity_reviews"] += identity_reviews
    review["total"] += identity_reviews
    return {"format": MEASURE_FORMAT, "evidence_label": evidence_label,
            "time_to_first_cited_evidence_ms": first_cited_ms(log.events, None),
            "time_to_answer_ms": log.elapsed_ms(), "tool_calls": len(log.events),
            "resolution": resolution, "unsupported_assertions": unsupported,
            "review_items": review}


def _spread(values: Sequence[int | None]) -> dict[str, Any]:
    measured = sorted(v for v in values if isinstance(v, (int, float)))
    out: dict[str, Any] = {"measured": len(measured), "unmeasured": len(values) - len(measured)}
    if measured:
        out.update(median=round(statistics.median(measured)), max=round(measured[-1]))
    return out


def summarize(measures: Iterable[Mapping[str, Any] | None]) -> dict[str, Any]:
    """Resolution counts (the unanswered questions beside coverage), unsupported assertions,
    review burden, tool calls and the two wall times (median and max over the runs that
    recorded them; the rest are counted ``unmeasured``). Flat, so the live verifier's
    counts-only record keeps every number."""
    rows = [m for m in measures if isinstance(m, Mapping)]
    out: dict[str, Any] = {"runs": len(rows), **dict.fromkeys(RESOLUTIONS, 0), "blocked": 0}
    for row in rows:
        resolution = row.get("resolution") or "blocked"
        if resolution not in (*RESOLUTIONS, "blocked"):
            raise MeasureError(f"unknown_resolution:{str(resolution)[:40]}")
        out[resolution] += 1
    out["unsupported_assertions"] = sum(int(r.get("unsupported_assertions") or 0)
                                        for r in rows)
    out["runs_with_unsupported"] = sum(1 for r in rows if r.get("unsupported_assertions"))
    for kind in REVIEW_KINDS:
        out[kind] = sum(int((r.get("review_items") or {}).get(kind) or 0) for r in rows)
    out["review_items"] = sum(out[kind] for kind in REVIEW_KINDS)
    out["tool_calls"] = sum(int(r.get("tool_calls") or 0) for r in rows)
    out["first_cited_evidence_ms"] = _spread([r.get("time_to_first_cited_evidence_ms")
                                              for r in rows])
    out["answer_ms"] = _spread([r.get("time_to_answer_ms") for r in rows])
    return out


def without_wall_clock(document: Any) -> Any:
    """``document`` without wall times: a run measure's two times and the ``median`` and
    ``max`` of a summary's time spreads; what a reproducible fixture output keeps (they
    differ on every run)."""
    if isinstance(document, Mapping):
        out = {k: without_wall_clock(v) for k, v in document.items()}
        if document.get("format") == MEASURE_FORMAT:
            out.pop("time_to_first_cited_evidence_ms", None)
            out.pop("time_to_answer_ms", None)
        for key in WALL_CLOCK:
            if isinstance(out.get(key), Mapping) and "measured" in out[key]:
                out[key] = {k: v for k, v in out[key].items() if k not in ("median", "max")}
        return out
    if isinstance(document, list):
        return [without_wall_clock(v) for v in document]
    return document


# --------------------------------------------------------------------------- manual

def validate_manual(record: Any) -> list[str]:
    from ontology.json_contract import validate
    return validate(record, json.loads(MANUAL_SCHEMA_PATH.read_text(encoding="utf-8")))


def load_manual(directory: Path | None = None) -> list[dict[str, Any]]:
    """Every manual baseline record under ``directory`` (default ``MANUAL_DIR``; absent:
    none). A file that is not a valid record is refused (``MeasureError``)."""
    root = MANUAL_DIR if directory is None else directory
    records = []
    for path in sorted(root.rglob("*.json")) if root.is_dir() else []:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise MeasureError(f"unreadable:{path.name}") from error
        errors = validate_manual(record)
        if errors:
            raise MeasureError(f"invalid:{path.name}:{errors[0].split(':', 1)[0]}")
        records.append(record)
    return records


def _median(values: Sequence[float]) -> int:
    return round(statistics.median(values))


def compare(runs: Sequence[tuple[str, Mapping[str, Any]]],
            records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Live runs ``(item_id, measure)`` against manual records of the same items. Only
    items with both are compared; without any pair the result is ``unmeasured`` and holds
    no time and no difference. Counts and milliseconds only, never an item id."""
    live: dict[str, list[Mapping[str, Any]]] = {}
    for item_id, measure in runs:
        if measure.get("evidence_label") in LIVE_LABELS and measure.get("resolution"):
            live.setdefault(item_id, []).append(measure)
    manual: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        manual.setdefault(record["item_id"], []).append(record)
    paired = sorted(set(live) & set(manual))
    base = {"format": COMPARISON_FORMAT, "live_items": len(live),
            "manual_items": len(manual), "paired_items": len(paired),
            "unpaired_live_items": len(set(live) - set(manual))}
    if not paired:
        reason = "no_live_runs" if not live else "no_paired_manual_baseline"
        return {**base, "status": "unmeasured", "reason": reason}
    out: dict[str, Any] = {**base, "status": "paired"}
    for name, agent_key, manual_key in (
            ("time_to_answer_ms", "time_to_answer_ms", "time_to_answer_seconds"),
            ("time_to_first_evidence_ms", "time_to_first_cited_evidence_ms",
             "time_to_first_evidence_seconds")):
        pairs = []
        for item_id in paired:
            agent = [m[agent_key] for m in live[item_id] if isinstance(m.get(agent_key), int)]
            person = [r[manual_key] * 1000 for r in manual[item_id]
                      if isinstance(r.get(manual_key), (int, float))]
            if agent and person:
                pairs.append((statistics.median(agent), statistics.median(person)))
        out[name] = {"pairs": len(pairs)} if not pairs else {
            "pairs": len(pairs), "agent_median": _median([a for a, _ in pairs]),
            "manual_median": _median([p for _, p in pairs]),
            "median_paired_difference": _median([p - a for a, p in pairs])}
    agent_runs = [m for item_id in paired for m in live[item_id]]
    people = [r for item_id in paired for r in manual[item_id]]
    out["answered"] = {"agent": sum(m["resolution"] == "answered" for m in agent_runs),
                       "agent_runs": len(agent_runs),
                       "manual": sum(r["resolution"] == "answered" for r in people),
                       "manual_records": len(people)}
    for key in ("unsupported_assertions", "review_items"):
        recorded = [r[key] for r in people if isinstance(r.get(key), int)]
        agent_total = sum(int(m[key]["total"] if key == "review_items" else m[key])
                          for m in agent_runs)
        out[key] = {"agent": agent_total, "agent_runs": len(agent_runs),
                    "manual": sum(recorded) if recorded else None,
                    "manual_records": len(recorded)}
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m evals.measure",
                                     description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cmp_ = sub.add_parser("compare", help="live runs' measures against manual baselines")
    cmp_.add_argument("--scores", type=Path, required=True,
                      help="a run directory of *.score.json files (evals.runner --out)")
    cmp_.add_argument("--manual", type=Path, default=None,
                      help=f"manual baseline records (default {MANUAL_DIR})")
    args = parser.parse_args(argv)
    try:
        runs = []
        for path in sorted(args.scores.resolve().rglob("*.score.json")):
            score = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(score.get("measure"), Mapping):
                runs.append((score["item_id"], score["measure"]))
        result = compare(runs, load_manual(args.manual))
    except (MeasureError, OSError, ValueError, KeyError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


__all__ = ["COMPARISON_FORMAT", "CallLog", "MANUAL_FORMAT", "MEASURE_FORMAT", "MeasureError",
           "cited_handles", "compare", "load_manual", "of_calls", "of_run", "summarize",
           "unsupported_assertions", "validate_manual", "without_wall_clock"]


if __name__ == "__main__":
    sys.exit(main())
