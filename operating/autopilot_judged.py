"""What a category's Auto rests on: its judged precision on held-out days.

A category of Assist (``autopilot_write.DRAFT_CHOICES``) earns Auto only when a replay of
real past days, graded item by item by a judge with a frozen rubric, shows the work Auto
would have performed by itself was right often enough: ``rules.gate`` (90% on at least 15
judged items), on days the rules were never tuned on (``autopilot_write.held_out_kind``: fresh days
from ``fresh_from``, or a clean past window). The write gate's replay counts
(``autopilot_write.record_scores``) say only that the person acted on the same thread or
record; they never measure whether the prepared reply or entry was the right one, so on
their own they earn nothing.

A judged run (``RUN_SCHEMA``) lists one row per replayed item: its category, the moment it
arrived (``at``), whether Auto would have performed the prepared work (``performed``: a
written reply or a system entry; a question to the person, a to-do or nothing is never
performed) and the judge's verdict (``right``, see ``verdict``). Judged = items performed;
matched = performed and right. ``record`` admits a run into ``JUDGED_FILE`` beside the
write gate's file; every item must lie in one held-out window (else nothing is admitted),
and the run's counts replace that category's earlier ones for the same scope (the number
shown is the newest measurement, never a sum of runs). The window is re-checked against
the rules at every read (``load``): days later marked tuned drop the score.

Scopes: ``all`` (the engine's precision over everyone's items) and ``person:<address>`` /
``person:<directory id>`` (one person's own items, a row's ``person`` and ``person_id``). A category passes for a person when the ``all`` score passes and
their own score, when one is on record, passes too. No file, an unreadable file or no
score: nothing passes.

Run::

    python3 -m operating.autopilot_judged record --run RUN.json --state STATE_DIR
    python3 -m operating.autopilot_judged show --state STATE_DIR
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, Optional, Sequence

JUDGED_FILE = "autopilot-judged.json"
SCHEMA = "plx-autopilot-judged/v1"
RUN_SCHEMA = "plx-autopilot-judged-run/v1"
ALL = "all"
_SCOPE = re.compile(r"^(?:all|person:[a-z0-9._%+'-]{1,64}@[a-z0-9.-]{1,190}"
                    r"|person:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def verdict(grade: Mapping[str, Any], quiet: bool) -> bool:
    """The frozen rubric's grade of one performed item as right or not: the same kind of
    step as the person on the same conversation or record, the same substance, nothing
    false, invented or harmful. On a quiet moment (the person did nothing) anything
    performed is wrong."""
    if quiet:
        return False
    return (grade.get("right_action") == "yes" and grade.get("right_values") == "yes"
            and grade.get("wrong_or_unsafe") == "no")


@dataclass(frozen=True)
class Score:
    """One category's judged precision for one scope."""
    judged: int
    matched: int
    window: tuple[str, str]
    kind: str            # fresh | historical (``held_out_kind``)
    source: str          # the run: set, engine, judge
    measured_on: str     # YYYY-MM-DD the judge graded it
    scope: str = "team"  # team: everyone's items; you: the person's own

    def percent(self) -> Optional[Decimal]:
        if self.judged <= 0:
            return None
        return (Decimal(self.matched) * 100 / Decimal(self.judged)).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP)

    def passes(self, precision: Decimal, minimum: int) -> bool:
        return self.judged >= minimum and 0 <= self.matched <= self.judged and \
            Decimal(self.matched) / Decimal(self.judged) >= precision


def _instant(raw: Any) -> datetime:
    out = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if out.tzinfo is None:
        raise ValueError("naive time")
    return out.astimezone(timezone.utc)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _categories() -> set[str]:
    from .autopilot_write import DRAFT_CHOICES
    return {cid for cid, _label, _flows in DRAFT_CHOICES}


def count_run(run: Mapping[str, Any], rules: Mapping[str, Any]
              ) -> dict[tuple[str, str], dict[str, Any]]:
    """(scope, category) -> counts and window of one judged run, checked: the run's schema,
    every row's category known, its moment readable and held out, its verdict present for
    a performed item, no item twice. Anything else is ``ValueError`` (nothing admitted)."""
    from .autopilot_write import held_out_kind
    if not isinstance(run, Mapping) or run.get("schema") != RUN_SCHEMA:
        raise ValueError("not a judged run")
    source = str(run.get("source") or "").strip()
    measured_on = str(run.get("measured_on") or "")
    if not source or not _DAY.match(measured_on):
        raise ValueError("run source or date missing")
    known = _categories()
    rows = run.get("items")
    if not isinstance(rows, list) or not rows:
        raise ValueError("no items")
    seen: set[str] = set()
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("item not an object")
        iid = str(row.get("item") or "")
        if not iid or iid in seen:
            raise ValueError("item id missing or repeated")
        seen.add(iid)
        cat = row.get("category")
        if cat not in known:
            raise ValueError(f"unknown category {cat!r}")
        at = _instant(row.get("at"))
        performed = row.get("performed")
        right = row.get("right")
        if not isinstance(performed, bool) or (performed and not isinstance(right, bool)):
            raise ValueError(f"item {iid}: verdict missing")
        # the person by address and, when given, by directory id: whichever identity
        # the opt-in carries finds their own score
        ids = [str(row.get(k) or "").strip().lower() for k in ("person", "person_id")]
        scopes = [ALL] + [f"person:{p}" for p in dict.fromkeys(ids) if p]
        for scope in scopes:
            if not _SCOPE.match(scope):
                raise ValueError(f"item {iid}: bad person")
            c = out.setdefault((scope, str(cat)), {"judged": 0, "matched": 0, "lo": at, "hi": at})
            c["lo"], c["hi"] = min(c["lo"], at), max(c["hi"], at)
            if performed:
                c["judged"] += 1
                c["matched"] += bool(right)
    for (scope, cat), c in out.items():
        window = [_iso(c["lo"]), _iso(c["hi"] + timedelta(seconds=1))]
        kind = held_out_kind(window, rules)
        if kind is None:
            raise ValueError(f"{cat}: items outside the held-out days")
        c.update(window=window, kind=kind, source=source[:200], measured_on=measured_on)
        del c["lo"], c["hi"]
    return out


def _read(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) and raw.get("schema") == SCHEMA else {}


def record(run_path: Path, state: Path, rules: Mapping[str, Any]) -> dict[str, Any]:
    """Admit one judged run into ``state / JUDGED_FILE`` (``count_run``): each
    (scope, category) it judged replaces the earlier score for it. Returns what was
    admitted; a run that fails a check admits nothing (``ValueError``)."""
    run = json.loads(Path(run_path).read_text(encoding="utf-8"))
    counted = count_run(run, rules)
    path = Path(state) / JUDGED_FILE
    doc = _read(path)
    scores = dict(doc.get("scores") or {})
    for (scope, cat), c in sorted(counted.items()):
        scores.setdefault(scope, {})[cat] = c
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"schema": SCHEMA, "scores": scores}, indent=1,
                              sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return {"admitted": [f"{s}/{c}" for s, c in sorted(counted)]}


def load(state: Optional[Path], rules: Mapping[str, Any]) -> dict[tuple[str, str], Score]:
    """(scope, category) -> its judged score, each re-checked now: counts that add up, a
    known category and a window still held out under the rules. Anything unreadable is
    left out (it passes nothing)."""
    from .autopilot_write import held_out_kind
    if state is None:
        return {}
    known = _categories()
    out: dict[tuple[str, str], Score] = {}
    for scope, cats in (_read(Path(state) / JUDGED_FILE).get("scores") or {}).items():
        if not isinstance(cats, Mapping) or not _SCOPE.match(str(scope)):
            continue
        for cat, c in cats.items():
            try:
                judged, matched = int(c["judged"]), int(c["matched"])
                window = (str(c["window"][0]), str(c["window"][1]))
                kind = held_out_kind(list(window), rules)
                source, measured_on = str(c["source"]), str(c["measured_on"])
            except (KeyError, IndexError, ValueError, TypeError, AttributeError):
                continue
            if cat not in known or kind is None or not 0 <= matched <= judged \
                    or not _DAY.match(measured_on):
                continue
            out[(str(scope), str(cat))] = Score(judged, matched, window, kind, source,
                                                measured_on,
                                                "team" if scope == ALL else "you")
    return out


def for_person(scores: Mapping[tuple[str, str], Score], person: str, category: str
               ) -> tuple[Optional[Score], Optional[Score]]:
    """(the ``all`` score, the person's own score) of a category; either may be None."""
    p = str(person or "").strip().lower()
    return scores.get((ALL, category)), scores.get((f"person:{p}", category))


def passes(scores: Mapping[tuple[str, str], Score], people: Iterable[str], category: str,
           precision: Decimal, minimum: int) -> bool:
    """The category earned Auto for one person (any of ``people``, their identities): the
    ``all`` score passes and none of their own scores on record fails."""
    found = [for_person(scores, p, category) for p in people]
    every = found[0][0] if found else None
    if every is None or not every.passes(precision, minimum):
        return False
    return all(own is None or own.passes(precision, minimum) for _all, own in found)


def shown(scores: Mapping[tuple[str, str], Score], people: Iterable[str], category: str,
          precision: Decimal, minimum: int) -> Optional[Score]:
    """The score shown beside a person's switch: the one that decides. Their own when one
    is on record and it fails, or when everyone's passes too; else everyone's (``scope``
    team; None when there is none)."""
    people = list(people)
    every = scores.get((ALL, category))
    own = next((o for p in people for o in [for_person(scores, p, category)[1]]
                if o is not None), None)
    if own is not None and (not own.passes(precision, minimum) or (
            every is not None and every.passes(precision, minimum))):
        return own
    return every


def main(argv: Sequence[str] | None = None) -> int:
    from .autopilot_write import load_rules
    p = argparse.ArgumentParser(prog="autopilot_judged")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--run", required=True)
    r.add_argument("--state", required=True)
    s = sub.add_parser("show")
    s.add_argument("--state", required=True)
    a = p.parse_args(argv)
    rules = load_rules()
    if a.cmd == "record":
        try:
            print(json.dumps(record(Path(a.run), Path(a.state), rules)))
        except (OSError, ValueError) as refused:
            print(json.dumps({"admitted": [], "refused": str(refused)[:200]}))
            return 2
        return 0
    gate = rules["gate"]
    for (scope, cat), sc in sorted(load(Path(a.state), rules).items()):
        print(json.dumps({"scope": scope, "category": cat, "judged": sc.judged,
                          "matched": sc.matched, "percent": str(sc.percent()),
                          "passes": sc.passes(Decimal(str(gate["precision"])),
                                              int(gate["min_judged"])),
                          "window": sc.window, "source": sc.source,
                          "measured_on": sc.measured_on}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
