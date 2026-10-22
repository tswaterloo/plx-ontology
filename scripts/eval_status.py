"""Print the evaluation summary line for the ``state.status.eval`` field.

Reads a live-check report (``results/latest.json``, schema
``plx-live-verify/v1``), whose top-level ``eval`` map holds each ``eval.*`` check's lifted
``eval`` block keyed by check id (``verify/live_verify.py`` ``eval_blocks``), and prints
one line, e.g.::

    eval.dev_baseline: dev_set 3/30 (F1 0/12 F2 1/6 F3 2/6 F4 0/6) attempted 2/6 \
needs_agent 24 pin_drift 0 [live-read] pass | eval.unfamiliar_sample: unfamiliar_find_root/v1 21/26 [live-read] pass

A held-out block (``eval.heldout_product``) reads as a dev one under its ``held_out`` rate.
Blocks without either rate (another eval's metric) show their ``pass`` rate. The
check's verifier ``result`` (pass/fail/error/skipped) closes each part, so a failed or
stale check is never read as a rate. A report without an ``eval`` map prints
``eval: none``. ``--json`` prints ``{"line": ..., "checks": {...}}`` instead, with each
check's rates, families and result only (counts and codes; the blocks carry no values).

    python3 -m scripts.eval_status results/latest.json [--json]

Exit 0 when the report was read (even with no eval block), 2 when it is unreadable.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from evals.baseline import summary_line


def check_summary(check_id: str, block: Mapping[str, Any]) -> dict[str, Any]:
    """The compact per-check record saved with the line."""
    out: dict[str, Any] = {"result": block.get("result"),
                           "evidence_label": block.get("evidence_label"),
                           "complete": block.get("complete")}
    rate = next((key for key in ("dev_set", "held_out") if key in block), None)
    if rate is not None:
        out.update({rate: block.get(rate)}, attempted=block.get("attempted"),
                   needs_agent=block.get("needs_agent"), pin_drift=block.get("pin_drift"),
                   by_family={family: entry.get("pass") for family, entry in
                              sorted((block.get("by_family") or {}).items())
                              if isinstance(entry, Mapping)})
    else:
        out["pass"] = block.get("pass")
    return out


def part(check_id: str, block: Mapping[str, Any]) -> str:
    if "dev_set" in block or "held_out" in block:
        text = summary_line(block)
    else:
        label = block.get("evidence_label")
        text = f"{block.get('metric') or 'pass'} {block.get('pass')} [{label}]"
    return f"{check_id}: {text} {block.get('result') or 'unknown'}"


def status(report: Mapping[str, Any]) -> dict[str, Any]:
    evals = report.get("eval") if isinstance(report, Mapping) else None
    if not isinstance(evals, Mapping) or not evals:
        return {"line": "eval: none", "checks": {}}
    checks = {check_id: block for check_id, block in sorted(evals.items())
              if isinstance(block, Mapping)}
    line = " | ".join(part(check_id, block) for check_id, block in checks.items())
    return {"line": line or "eval: none",
            "checks": {check_id: check_summary(check_id, block)
                       for check_id, block in checks.items()}}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_status",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("report", type=Path, help="results/latest.json")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("eval: report unreadable", file=sys.stderr)
        return 2
    result = status(report)
    print(json.dumps(result, sort_keys=True) if args.json else result["line"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
