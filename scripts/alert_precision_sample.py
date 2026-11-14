#!/usr/bin/env python3
"""Draw the alert precision sample and write the judged result back (AL-01).

    python3 scripts/alert_precision_sample.py sample --run RUN.json --out JUDGE.tsv
        [--seed N] [--per-type 15] [--registry alert_kinds.json]
    python3 scripts/alert_precision_sample.py apply --judged JUDGE.tsv --source TEXT
        [--on YYYY-MM-DD] [--registry alert_kinds.json]

``sample``: RUN.json is a served alert run (``{"gaps": [...]}`` or ``{"items": [...]}``, each
item with ``id``, ``kind`` (gap kind) or ``event_type``, ``summary`` and cited ``groups``).
For every type the registry keeps off, a seeded sample of ``--per-type`` items is written
to a TSV with the evidence each item cites; the last column ``judgement`` is blank for the
human to fill with ``y`` (the alert is right) or ``n``. A type with fewer items than asked
is sampled whole and will fall under MIN_CHECKED when applied (it stays off).

``apply``: each type with judged rows gets ``precision`` {value, checked, correct, on,
source}. Only a measurement with checked >= MIN_CHECKED is written. ``default`` is never
set to "on" here (switching a type on for everyone is a decision, not a side effect); a
type that is on and now measures below BAR or under MIN_CHECKED is turned off, home with it.
An unknown type, an unknown judgement value or a duplicate row fails the whole apply
before anything is written.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from operating import alert_kinds as ak  # noqa: E402
from operating.briefing import EVENT_TYPES  # noqa: E402

COLUMNS = ["type", "id", "object_id", "summary", "evidence", "judgement"]


def _types_of_gap_kind(types: list[dict]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for t in types:
        spec = EVENT_TYPES.get(t["type"])
        for k in (spec["kinds"] if spec and spec["kinds"] else (t["type"],)):
            out.setdefault(k, []).append(t["type"])
    return out


def _evidence(item: dict) -> str:
    cites = []
    for group in item.get("groups") or []:
        for c in group:
            cites.append(str(c[0] if isinstance(c, (list, tuple)) else c))
    for c in item.get("extra") or []:
        cites.append(str(c[0] if isinstance(c, (list, tuple)) else c))
    return " | ".join(dict.fromkeys(cites))


def _clean(text: object) -> str:
    return " ".join(str(text or "").split())


def draw(run: dict, registry: dict, *, seed: int, per_type: int) -> list[dict]:
    off = [t for t in registry["types"] if t["default"] != "on"]
    by_kind = _types_of_gap_kind(registry["types"])
    pools: dict[str, dict[str, dict]] = {t["type"]: {} for t in off}
    for item in run.get("gaps", run.get("items", [])):
        names = [item["event_type"]] if item.get("event_type") else by_kind.get(item.get("kind"), [])
        for name in names:
            if name in pools and item.get("id") is not None:
                pools[name].setdefault(str(item["id"]), item)
    rows: list[dict] = []
    for name in sorted(pools):
        ids = sorted(pools[name])
        rng = random.Random(f"{seed}|{name}")
        for i in sorted(rng.sample(ids, min(per_type, len(ids)))):
            it = pools[name][i]
            rows.append({"type": name, "id": i, "object_id": _clean(it.get("object_id")),
                         "summary": _clean(it.get("summary")), "evidence": _clean(_evidence(it)),
                         "judgement": ""})
    return rows


def write_tsv(rows: list[dict], out: Path) -> None:
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, COLUMNS, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def tally(judged: Path, known: set[str]) -> dict[str, tuple[int, int]]:
    """type -> (checked, correct); raises ValueError on anything unreadable."""
    counts: dict[str, list[int]] = {}
    seen: set[tuple[str, str]] = set()
    with judged.open(encoding="utf-8", newline="") as fh:
        for n, row in enumerate(csv.DictReader(fh, delimiter="\t"), 2):
            name, mark = (row.get("type") or "").strip(), (row.get("judgement") or "").strip().lower()
            if name not in known:
                raise ValueError(f"line {n}: unknown type {name!r}")
            if (name, row.get("id") or "") in seen:
                raise ValueError(f"line {n}: duplicate item {name}/{row.get('id')}")
            seen.add((name, row.get("id") or ""))
            if mark == "":
                continue   # not judged: not counted
            if mark not in ("y", "n"):
                raise ValueError(f"line {n}: judgement {mark!r} is not y or n")
            c = counts.setdefault(name, [0, 0])
            c[0] += 1
            c[1] += mark == "y"
    return {k: (v[0], v[1]) for k, v in counts.items()}


def apply(judged: Path, registry: dict, *, source: str, on: str) -> dict[str, str]:
    counts = tally(judged, {t["type"] for t in registry["types"]})
    report: dict[str, str] = {}
    by_name = {t["type"]: t for t in registry["types"]}
    for name, (checked, correct) in sorted(counts.items()):
        t = by_name[name]
        if checked < ak.MIN_CHECKED:
            report[name] = f"skipped: {checked} judged < {ak.MIN_CHECKED}"
            if t["default"] == "on":
                t["default"], t["home"] = "off", False
                report[name] += "; was on, now off"
            continue
        p = {"value": round(correct / checked, 3), "checked": checked, "correct": correct,
             "on": on, "source": source}
        t["precision"] = p
        t.pop("unmeasured", None)
        report[name] = f"{correct}/{checked} = {p['value']}"
        if t["default"] == "on" and not ak.passes_bar(p):
            t["default"], t["home"] = "off", False
            report[name] += "; below the bar, now off"
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--run", required=True, type=Path)
    s.add_argument("--out", required=True, type=Path)
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--per-type", type=int, default=ak.MIN_CHECKED)
    s.add_argument("--registry", type=Path, default=ak.PATH)
    a = sub.add_parser("apply")
    a.add_argument("--judged", required=True, type=Path)
    a.add_argument("--source", required=True)
    a.add_argument("--on", default=date.today().isoformat())
    a.add_argument("--registry", type=Path, default=ak.PATH)
    args = ap.parse_args(argv)
    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    if args.cmd == "sample":
        rows = draw(json.loads(args.run.read_text(encoding="utf-8")), registry,
                    seed=args.seed, per_type=args.per_type)
        write_tsv(rows, args.out)
        print(f"{len(rows)} items for {len({r['type'] for r in rows})} types -> {args.out}")
        return 0
    try:
        report = apply(args.judged, registry, source=args.source, on=args.on)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    args.registry.write_text(json.dumps(registry, indent=1, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    for name, line in report.items():
        print(f"{name}: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
