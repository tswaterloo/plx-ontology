"""The latest answer-quality score per question set and split, for the Trust page.

One small JSON file an eval run updates when it finishes (``record``) and the API reads
(``read``). Counts and rates only: no item ids, no questions, no answers, so a held-out
split's aggregate may be kept here without exposing an item::

    {"schema": "plx-eval-results/v1",
     "sets": {"<set>": {"dev":      {"at": "2026-11-11T19:00:00Z", "runs": 40, "items": 20,
                                     "correct": 31, "answered": 36, "scored": 40,
                                     "accuracy": "0.861", "coverage": "0.900",
                                     "evidence_label": "live-captures", "runner": "..."},
                        "held_out": {...}}}}

``accuracy`` is correct over answered and ``coverage`` answered over scored runs (the
``evals.score.outcome_summary`` rates, recomputed here from its counts, three decimals,
``null`` with a zero denominator). Each (set, split) keeps only its latest run. The file
lives in the state directory (``<state>/eval-results/results.json``; ``RESULTS_ENV``
overrides), never in the repository. Unreadable, another schema, an unknown split or a
count that does not add up: ``read`` drops the entry (the page then says "not measured
yet"), never a guessed number.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Optional

SCHEMA = "plx-eval-results/v1"
RESULTS_ENV = "PLX_EVAL_RESULTS_FILE"
RELATIVE = Path("eval-results") / "results.json"
SPLITS = ("dev", "held_out")
SET_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
LABELS = ("isolated-fixture", "injected-transport", "live-captures", "live-verified")


def path_for(state_dir: Optional[Path], environ: Optional[Mapping[str, str]] = None
             ) -> Optional[Path]:
    env = os.environ if environ is None else environ
    if env.get(RESULTS_ENV):
        return Path(env[RESULTS_ENV])
    return Path(state_dir) / RELATIVE if state_dir else None


def _rate(num: int, den: int) -> Optional[str]:
    if den <= 0:
        return None
    return str((Decimal(num) / Decimal(den)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def entry(outcomes: Mapping[str, Any], *, items: int, evidence_label: str, at: datetime,
          runner: str = "") -> dict[str, Any]:
    """One run's entry from ``outcome_summary`` counts."""
    if evidence_label not in LABELS:
        raise ValueError("unknown evidence label")
    runs, correct = int(outcomes["runs"]), int(outcomes["correct"])
    answered = int(outcomes["answered"])
    scored = runs - int(outcomes.get("unscored") or 0)
    if not 0 <= correct <= answered <= scored <= runs:
        raise ValueError("counts do not add up")
    return {"at": _iso(at), "runs": runs, "items": int(items), "correct": correct,
            "answered": answered, "scored": scored, "accuracy": _rate(correct, answered),
            "coverage": _rate(answered, scored), "evidence_label": evidence_label,
            "runner": str(runner)[:80]}


def _valid(e: Any) -> bool:
    try:
        runs, correct, answered, scored = (int(e[k]) for k in ("runs", "correct", "answered",
                                                               "scored"))
        datetime.fromisoformat(str(e["at"]).replace("Z", "+00:00"))
        return 0 <= correct <= answered <= scored <= runs and int(e["items"]) >= 0 \
            and e.get("evidence_label") in LABELS \
            and e.get("accuracy") == _rate(correct, answered) \
            and e.get("coverage") == _rate(answered, scored)
    except (KeyError, ValueError, TypeError):
        return False


def read(path: Optional[Path]) -> dict[str, dict[str, dict[str, Any]]]:
    """set -> split -> entry, only the entries that check out ({} when none)."""
    if path is None:
        return {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA \
            or not isinstance(raw.get("sets"), dict):
        return {}
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for name, splits in raw["sets"].items():
        if not SET_NAME.match(str(name)) or not isinstance(splits, dict):
            continue
        kept = {s: dict(e) for s, e in splits.items() if s in SPLITS and isinstance(e, dict)
                and _valid(e)}
        if kept:
            out[str(name)] = kept
    return out


def record(path: Path, set_name: str, split: str, outcomes: Mapping[str, Any], *,
           items: int, evidence_label: str, at: Optional[datetime] = None,
           runner: str = "") -> dict[str, Any]:
    """Keep this run as the latest of (``set_name``, ``split``); other entries kept."""
    if not SET_NAME.match(set_name):
        raise ValueError("set name")
    if split not in SPLITS:
        raise ValueError("split")
    new = entry(outcomes, items=items, evidence_label=evidence_label,
                at=at or datetime.now(timezone.utc), runner=runner)
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        sets = dict(raw.get("sets") or {}) if raw.get("schema") == SCHEMA else {}
    except (OSError, ValueError, AttributeError):
        sets = {}
    sets[set_name] = {**(sets.get(set_name) or {}), split: new}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"schema": SCHEMA, "sets": sets}, indent=1, sort_keys=True),
                   encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return new
