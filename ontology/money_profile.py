"""Write ``ontology/raw_money_profile.json``: per column of every finance-sensitive Portal
relation, what its values are across the relation, read once from the source mirror
(read-only). ``ontology.raw_markings`` decides money per column from it, never per cell.

A column is ``money`` when it is mostly numeric (at least half of its non-empty cells) and
at least ``FRACTIONAL_SHARE`` of its non-zero numbers are fractional (``2210.400``;
``2225`` is whole, and so is FileMaker's ``1.0``), ``whole`` when it is mostly numeric
with fewer fractional figures (identifiers, compact dates, flags, counts: an invoice
number column holding a stray ``12.5`` among thousands of whole ones stays whole; real
amounts measured at 2.4 % or more, identifiers at 0.2 % or less), ``code`` when at least
``HUGE_SHARE`` of its non-zero numbers are ``HUGE`` or more in size (no amount is: FileMaker's
container data such as ``receipt_raw_material.coadata`` holds encoded references like
``-9.519e18``), ``text`` when it is not mostly numeric, ``empty`` when no sampled
cell holds a value. Only column names are written, never a value.

    python3 -m scripts.gen_raw_money_profile --mirror <source-mirror.sqlite> [--rows 30000]
        [--workers 4] [--out ontology/raw_money_profile.json]     # the committed profile
    python3 -m scripts.gen_raw_money_profile --state-dir <STATE>    # the state's own
    python3 -m scripts.gen_raw_money_profile --state-dir <STATE> --check [--max-age-days 7]

Every projection build profiles its state again after it publishes
(``operating.host.build_projection``); ``--check`` fails when the profile its readers use
does not profile the mirror generation served or is older than ``MAX_AGE_DAYS``.
"""

from __future__ import annotations

import argparse
import datetime
import json
import multiprocessing
import sqlite3
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "ontology" / "raw_money_profile.json"
FORMAT = "plx-raw-money-profile/v1"
FRACTIONAL_SHARE = 0.005
HUGE = Decimal(10) ** 12
HUGE_SHARE = 0.01


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
        except InvalidOperation:
            return None
    if isinstance(value, str) and 0 < len(value.strip()) <= 64:
        try:
            number = Decimal(value.strip())
        except InvalidOperation:
            return None
        return number if number.is_finite() else None
    return None


def classify(rows: Any) -> dict[str, str]:
    """{column: money | whole | code | text | empty} over ``rows`` (dicts)."""
    filled: dict[str, int] = {}
    numeric: dict[str, int] = {}
    fractional: dict[str, int] = {}
    zeros: dict[str, int] = {}
    huge: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for column, value in row.items():
            filled.setdefault(column, 0)
            if value is None or value == "" or isinstance(value, (dict, list)):
                continue
            filled[column] += 1
            number = _number(value)
            if number is None:
                continue
            if number == 0:
                zeros[column] = zeros.get(column, 0) + 1
                continue   # a zero amount says nothing about the column's figures
            numeric[column] = numeric.get(column, 0) + 1
            try:
                large = abs(number) >= HUGE
            except ArithmeticError:   # an exponent past the context's range
                large = True
            if large:
                huge[column] = huge.get(column, 0) + 1
            try:
                whole = number == number.to_integral_value()
            except ArithmeticError:
                whole = True
            if not whole:
                fractional[column] = fractional.get(column, 0) + 1
    out = {}
    for column, count in filled.items():
        if count == 0:
            out[column] = "empty"
        elif (numeric.get(column, 0) + zeros.get(column, 0)) * 2 < count:
            out[column] = "text"
        else:
            share = fractional.get(column, 0) / max(1, numeric.get(column, 0))
            if huge.get(column) and huge[column] >= HUGE_SHARE * numeric[column]:
                out[column] = "code"
            elif fractional.get(column) and share >= FRACTIONAL_SHARE:
                out[column] = "money"
            else:
                out[column] = "whole"
    return out


def _profile(job: tuple[str, str, int]) -> tuple[str, int, dict[str, str]]:
    mirror, relation, limit = job
    db = sqlite3.connect(f"file:{mirror}?mode=ro", uri=True)
    try:
        rows = [json.loads(r[0]) for r in db.execute(
            "SELECT row_json FROM row_current WHERE relation = ? AND run_length = 1 "
            "AND absent_since_capture IS NULL LIMIT ?", (relation, limit))]
    finally:
        db.close()
    return relation, len(rows), classify(rows)


MIRROR = Path("case-runtime") / "source-mirror.sqlite"   # a state directory's mirror
# How old the served profile may be before ``check`` fails: every projection build profiles
# again (``operating.host.build_projection``), so a week means builds stopped profiling.
MAX_AGE_DAYS = 7


def profile(mirror: Path, rows: int = 30000, workers: int = 4) -> dict[str, Any]:
    """The profile document of ``mirror`` (read-only)."""
    from ontology import raw_markings
    from ontology import schema as ontology_schema
    domains = raw_markings._catalog_domains(str(ontology_schema.META_DIR))
    relations = sorted(r for r, d in domains.items() if d in raw_markings.SENSITIVE_DOMAINS)
    generation = _generation(mirror)
    jobs = [(str(mirror), r, rows) for r in relations]
    if workers > 1:
        with multiprocessing.Pool(workers) as pool:
            found = pool.map(_profile, jobs)
    else:
        found = [_profile(job) for job in jobs]
    out = {"format": FORMAT,
           "note": "Per column of each finance-sensitive Portal relation (reporting-catalog "
                   "domains purchasing, orders, customers): money (mostly numeric, at least "
                   "0.5 % of its non-zero figures fractional; N.0 is whole), whole, code "
                   "(encoded references, figures of 1e12 and more), text or empty, read "
                   "from the source mirror by ontology/money_profile.py. Column names "
                   "only. Read by ontology/raw_markings.py; a column it does not list is "
                   "finance.",
           "mirror_generation": generation, "rows_per_relation": rows,
           "profiled_at": datetime.datetime.now(datetime.timezone.utc).strftime(
               "%Y-%m-%dT%H:%M:%SZ"),
           "relations": {}}
    for relation, sampled, columns in found:
        out["relations"][relation] = {"rows": sampled, **{
            kind: sorted(c for c, k in columns.items() if k == kind)
            for kind in ("money", "whole", "code", "text", "empty")}}
    return out


def _generation(mirror: Path) -> str | None:
    db = sqlite3.connect(f"file:{mirror}?mode=ro", uri=True)
    try:
        return dict(db.execute("SELECT * FROM mirror_meta")).get("generation")
    finally:
        db.close()


def write(doc: dict[str, Any], out: Path) -> None:
    """Write atomically: a reader never reads half a profile."""
    tmp = out.with_name(f".{out.name}.tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(out)


def profile_state(state_dir: Path, *, rows: int = 30000, workers: int = 4) -> dict[str, Any]:
    """Profile a state directory's mirror into its own profile (``raw_markings.
    STATE_PROFILE``), which its readers prefer while it is the newer: what a projection
    build runs after it publishes. Returns counts."""
    from ontology import raw_markings
    doc = profile(Path(state_dir) / MIRROR, rows, workers)
    write(doc, Path(state_dir) / raw_markings.STATE_PROFILE)
    return {"relations": len(doc["relations"]), "mirror_generation": doc["mirror_generation"],
            "money_columns": sum(len(v["money"]) for v in doc["relations"].values())}


def check(state_dir: Path, *, max_age_days: float = MAX_AGE_DAYS,
          now: datetime.datetime | None = None) -> dict[str, Any]:
    """Whether the profile a reader of ``state_dir`` uses is current: the state's own (else
    the committed one) profiles the mirror generation the state serves and is at most
    ``max_age_days`` old. ``ok`` False names each ``problem``."""
    from ontology import raw_markings
    state = Path(state_dir) / raw_markings.STATE_PROFILE
    used = state if state.is_file() else raw_markings.PROFILE
    problems: list[str] = []
    try:
        doc = json.loads(used.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "profile": str(used), "problems": ["profile_unreadable"]}
    try:
        served = _generation(Path(state_dir) / MIRROR)
    except sqlite3.Error:
        served = None
    if served is None:
        problems.append("mirror_unreadable")
    elif doc.get("mirror_generation") != served:
        problems.append("mirror_generation_not_profiled")
    try:
        at = datetime.datetime.strptime(str(doc.get("profiled_at")), "%Y-%m-%dT%H:%M:%SZ"
                                        ).replace(tzinfo=datetime.timezone.utc)
        age = ((now or datetime.datetime.now(datetime.timezone.utc)) - at).total_seconds()
        if age > max_age_days * 86400:
            problems.append("profile_too_old")
    except ValueError:
        age = None
        problems.append("profiled_at_unknown")
    return {"ok": not problems, "profile": str(used), "problems": problems,
            "profiled_at": doc.get("profiled_at"), "age_days": None if age is None
            else round(age / 86400, 2), "mirror_generation": doc.get("mirror_generation"),
            "served_generation": served}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--mirror", type=Path)
    parser.add_argument("--state-dir", type=Path,
                        help="profile its mirror into its own profile (with --check: check "
                             "the profile its readers use)")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 when the profile is older than --max-age-days or does "
                             "not profile the state's mirror generation")
    parser.add_argument("--max-age-days", type=float, default=MAX_AGE_DAYS)
    parser.add_argument("--rows", type=int, default=30000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    if args.check:
        if args.state_dir is None:
            parser.error("--check needs --state-dir")
        found = check(args.state_dir, max_age_days=args.max_age_days)
        print(json.dumps(found))
        return 0 if found["ok"] else 1
    if args.state_dir is not None:
        print(json.dumps(profile_state(args.state_dir, rows=args.rows,
                                       workers=args.workers)))
        return 0
    if args.mirror is None:
        parser.error("--mirror or --state-dir is required")
    doc = profile(args.mirror, args.rows, args.workers)
    write(doc, args.out)
    print(json.dumps({"relations": len(doc["relations"]), "money_columns": sum(
        len(v["money"]) for v in doc["relations"].values())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
