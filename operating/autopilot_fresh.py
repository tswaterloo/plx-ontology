"""The fresh-day scorer's result, published for the app: per role and workflow, how many
fresh days are scored and how far the workflow is from its write gate (the countdown on
the workflow page and the Trust page).

The scorer (``fresh_score.sh``: ``operating.autopilot_replay`` over the days after the
write rules' ``fresh_from``, never tuned on) writes one run folder: ``RUN-INFO`` and per
role ``<role>/result.json`` (``plx-autopilot-replay/v1``). ``publish`` reads that folder
and writes ``FILE`` into the write path's state directory (counts and ids only, never a
record or a message). It admits a role only when its result is complete and its window
starts at or after ``fresh_from`` (as ``autopilot_write.record_scores`` does); anything
else is listed as refused and left out. Publishing never switches anything on: the write
gate (``autopilot_write.WriteGate``) and the owner's switches are untouched.

The countdown (``countdown``) restates the write gate's own rule (``rules.gate``:
precision on at least ``min_judged`` judged items from fresh days) from the counts,
recomputed at every read. Missing, unreadable or another schema:
no countdown ("not scored yet"), never a made-up number.

Run (the scorer calls it at the end of a run)::

    python3 -m operating.autopilot_fresh publish RUN_DIR --state STATE_DIR
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Optional, Sequence

SCHEMA = "plx-autopilot-fresh/v1"
FILE = "autopilot-fresh.json"
RESULT_SCHEMA = "plx-autopilot-replay/v1"


def _instant(value: Any) -> datetime:
    out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if out.tzinfo is None:
        raise ValueError("naive instant")
    return out


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rules() -> Mapping[str, Any]:
    from . import autopilot_write as aw
    return aw.load_rules()


def publish(run_dir: Path, state_dir: Path, *, rules: Optional[Mapping[str, Any]] = None,
            now: Optional[datetime] = None) -> dict[str, Any]:
    """Write ``state_dir/FILE`` from one scorer run folder; returns what was admitted and
    refused (role -> reason). Roles a run did not score keep their last published entry
    (the scorer runs roles one by one and resumes)."""
    rules = _rules() if rules is None else rules
    fresh_from = _instant(rules["fresh_from"])
    run_dir, state_dir = Path(run_dir), Path(state_dir)
    path = state_dir / FILE
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        roles = dict(doc.get("roles") or {}) if doc.get("schema") == SCHEMA else {}
    except (OSError, ValueError, AttributeError):
        roles = {}
    admitted, refused = [], {}
    for p in sorted(run_dir.glob("*/result.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            refused[p.parent.name] = "unreadable"
            continue
        role = str(r.get("role") or p.parent.name)
        try:
            if r.get("schema") != RESULT_SCHEMA:
                raise ValueError("schema")
            if r.get("status") != "complete":
                refused[role] = f"status:{r.get('status')}"
                continue
            start, end = _instant(r["window"][0]), _instant(r["window"][1])
            if start < fresh_from:
                refused[role] = "not_fresh"
                continue
            splits = r.get("splits") or {}
            gated = (splits.get("held_out") or {}).get("workflows") if splits \
                else r.get("workflows")
            flows = {}
            for flow, v in (gated or {}).items():
                judged, matched = int(v["judged"]), int(v["matched"])
                if not 0 <= matched <= judged:
                    raise ValueError("counts")
                flows[str(flow)] = {"judged": judged, "matched": matched,
                                    "prepared": int(v.get("prepared") or 0)}
            if end <= start:
                raise ValueError("window")
            entry = {
                     "window": [_iso(start), _iso(end)],
                     "data_end": str(r.get("data_end") or ""),
                     "run": run_dir.name, "workflows": flows}
        except (KeyError, IndexError, ValueError, TypeError, AttributeError):
            refused[role] = "unreadable"
            continue
        roles[role] = entry
        admitted.append(role)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"schema": SCHEMA, "published_at": _iso(
        now or datetime.now(timezone.utc)), "roles": roles}, indent=1, sort_keys=True),
        encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return {"admitted": sorted(admitted), "refused": refused}


def read(state_dir: Optional[Path]) -> dict[str, Any]:
    """The published document (``{}`` when missing, unreadable or another schema)."""
    if state_dir is None:
        return {}
    try:
        doc = json.loads((Path(state_dir) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) and doc.get("schema") == SCHEMA \
        and isinstance(doc.get("roles"), dict) else {}


def countdown(entry: Optional[Mapping[str, Any]], workflow: str,
              rules: Optional[Mapping[str, Any]] = None) -> Optional[dict[str, Any]]:
    """One role's workflow against the write gate's rule (items, not days): judged and
    matched on fresh days, what the gate needs, the precision so far and whether it passes. None when
    the role was not scored (nothing to count)."""
    if not entry:
        return None
    rules = _rules() if rules is None else rules
    need = int(rules["gate"]["min_judged"])
    bar = Decimal(str(rules["gate"]["precision"]))
    try:
        counts = (entry.get("workflows") or {}).get(workflow) or {"judged": 0, "matched": 0}
        judged, matched = int(counts["judged"]), int(counts["matched"])
        window = [str(x) for x in entry.get("window") or ()]
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
    pct = None
    if judged:
        pct = float((Decimal(matched) * 100 / Decimal(judged)).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP))
    passes = judged >= need and 0 <= matched <= judged and \
        Decimal(matched) / Decimal(judged) >= bar
    return {"judged": judged, "matched": matched, "need_judged": need,
            "need_percent": float((bar * 100).quantize(Decimal("0.1"))), "percent": pct,
            "passes": passes, "window": window, "data_end": entry.get("data_end") or None}


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m operating.autopilot_fresh")
    sub = p.add_subparsers(dest="command", required=True)
    pub = sub.add_parser("publish")
    pub.add_argument("run_dir", type=Path)
    pub.add_argument("--state", type=Path, required=True,
                     help="the write path's state directory ($state_dir/autopilot-write)")
    a = p.parse_args(argv)
    out = publish(a.run_dir, a.state)
    print(json.dumps(out, indent=1, sort_keys=True))
    return 0 if out["admitted"] or not out["refused"] else 1


if __name__ == "__main__":
    sys.exit(main())
