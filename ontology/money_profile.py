"""Write ``ontology/raw_money_profile.json``: per column of every finance-sensitive Portal
relation, what its values are across the relation, read once from the source mirror
(read-only). ``ontology.raw_markings`` decides money per column from it, never per cell.

Per column (``classify``): ``code`` when most of its non-zero figures are encodings (1e12
or more in size, or written with more than six decimal places: FileMaker's container data
such as ``receipt_raw_material.coadata``, 12- to 16-place doubles, a third negative, up to
-9.5e18), so a 1e14 "no limit" placeholder in some rows of an amount column never makes it
one; ``money`` when at least half of its cells are figures and at least 0.5 % of its
amount-sized figures are fractional amounts (``2210.400``; measured: amounts 2.4 % and
up, identifiers with a stray fraction 0.2 % and below; ``2225`` and FileMaker's ``1.0`` are
whole); ``whole`` when mostly figures otherwise (identifiers, compact dates, flags,
counts); ``ident`` when not, but mostly identifier tokens with at most 1 % written as
amounts (a customer PO number column whose numbers read ``4501.2``); ``text`` when not;
``empty`` when no sampled cell holds a value. A code or text column is read with a tripwire (``ontology.raw_markings``):
a cell written as an amount there is withheld, so "no limit" text in most rows of an
amount column never shows the amounts of the others. Only column names are written.

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
import re
import sqlite3
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "ontology" / "raw_money_profile.json"
FORMAT = "plx-raw-money-profile/v1"
# The classification rules' version: a profile written under other rules is never reused.
RULES = "2026-11-06.2"
FRACTIONAL_SHARE = 0.005
NUMERIC_SHARE = 0.5
# An identifier as written: a short token of letters, digits and . _ / # - (spaces too,
# Sage pads its fields), such as a customer PO number "4501.2" or "PO 4501-2".
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._/#-]{0,39}")
IDENTIFIER_SHARE = 0.5      # of a text column's values, for it to be an identifier column
IDENTIFIER_AMOUNTS = 0.01   # at most this share of its values written as amounts
HUGE = Decimal(10) ** 12
MONEY_DECIMALS = 6


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


def decimals(number: Decimal) -> int:
    """How many decimal places ``number`` is written with (``1234.567890123456``: 12)."""
    exponent = number.as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def money_shaped(number: Decimal) -> bool:
    """A fractional figure written as an amount is: under ``HUGE`` in size, with at most
    ``MONEY_DECIMALS`` decimal places (``2210.400``; never ``-4567.123456789012``)."""
    try:
        return abs(number) < HUGE and number != number.to_integral_value() \
            and decimals(number) <= MONEY_DECIMALS
    except ArithmeticError:
        return False


def classify(rows: Any) -> dict[str, str]:
    """{column: money | whole | code | text | empty} over ``rows`` (dicts). Per column:
    ``code`` when most of its non-zero figures are encodings (``HUGE`` or more in size, or
    written with more than ``MONEY_DECIMALS`` places: a double's noise), so a placeholder
    in a few rows of an amount column (a 1e14 "no limit") never makes it one; ``money``
    when at least ``NUMERIC_SHARE`` of its cells are figures and ``FRACTIONAL_SHARE`` of its
    amount-sized ones are fractional amounts; ``whole`` when mostly figures, too few of
    them such amounts; ``ident`` when not mostly figures but mostly identifier tokens
    (``IDENTIFIER``: a customer PO number column) with at most 1 % written as amounts;
    else ``text``. At read time a text column's amount-shaped cells trip, an identifier
    column's only those written with two or more decimal places (an amount's ``12.50``,
    never a PO number's ``4501.2``)."""
    filled: dict[str, int] = {}
    numeric: dict[str, int] = {}
    nonzero: dict[str, int] = {}
    encoded: dict[str, int] = {}
    amounts: dict[str, int] = {}
    idents: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for column, value in row.items():
            filled.setdefault(column, 0)
            if value is None or value == "" or isinstance(value, (dict, list)):
                continue
            filled[column] += 1
            if isinstance(value, str) and IDENTIFIER.fullmatch(value.strip()):
                idents[column] = idents.get(column, 0) + 1
            number = _number(value)
            if number is None:
                continue
            numeric[column] = numeric.get(column, 0) + 1
            if number == 0:
                continue   # a zero amount says nothing about the column's figures
            nonzero[column] = nonzero.get(column, 0) + 1
            try:
                large = abs(number) >= HUGE
            except ArithmeticError:   # an exponent past the context's range
                large = True
            if large or (decimals(number) > MONEY_DECIMALS
                         and number != number.to_integral_value()):
                encoded[column] = encoded.get(column, 0) + 1
            elif money_shaped(number):
                amounts[column] = amounts.get(column, 0) + 1
    out = {}
    for column, count in filled.items():
        figures, some = numeric.get(column, 0), nonzero.get(column, 0)
        sized = some - encoded.get(column, 0)
        if count == 0:
            out[column] = "empty"
        elif some and encoded.get(column, 0) * 2 >= some:
            out[column] = "code"
        elif figures >= NUMERIC_SHARE * count and amounts.get(column) \
                and amounts[column] >= FRACTIONAL_SHARE * sized:
            out[column] = "money"
        elif figures * 2 >= count:
            out[column] = "whole"
        elif idents.get(column, 0) >= IDENTIFIER_SHARE * count \
                and amounts.get(column, 0) <= IDENTIFIER_AMOUNTS * count:
            out[column] = "ident"
        else:
            out[column] = "text"
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
           "mirror_generation": generation, "rows_per_relation": rows, "rules": RULES,
           "mirror_fingerprint": fingerprint(mirror, relations),
           "profiled_at": datetime.datetime.now(datetime.timezone.utc).strftime(
               "%Y-%m-%dT%H:%M:%SZ"),
           "relations": {}}
    for relation, sampled, columns in found:
        out["relations"][relation] = {"rows": sampled, **{
            kind: sorted(c for c, k in columns.items() if k == kind)
            for kind in ("money", "whole", "code", "ident", "text", "empty")}}
    return out


def fingerprint(mirror: Path, relations: list[str] | None = None) -> str | None:
    """A cheap digest of what the mirror holds of ``relations`` (default the
    finance-sensitive ones): its generation and change epoch, each relation's write counter
    (``relation_change.n``, which the mirror's triggers raise on every row and version
    write: an absence swap that keeps the row count, a soft-delete sync) and its last
    capture, row count and last refresh (``relation_state``): two small reads. None when it
    cannot be read."""
    import hashlib
    if relations is None:
        from ontology import raw_markings
        from ontology import schema as ontology_schema
        domains = raw_markings._catalog_domains(str(ontology_schema.META_DIR))
        relations = sorted(r for r, d in domains.items()
                           if d in raw_markings.SENSITIVE_DOMAINS)
    try:
        db = sqlite3.connect(f"file:{mirror}?mode=ro", uri=True)
        try:
            meta = dict(db.execute("SELECT * FROM mirror_meta"))
            wanted = set(relations)
            state = sorted(row for row in db.execute(
                "SELECT relation, last_capture_id, row_count, last_refresh_at "
                "FROM relation_state") if row[0] in wanted)
            changes = sorted(row for row in db.execute(
                "SELECT relation, n FROM relation_change") if row[0] in wanted)
        finally:
            db.close()
    except sqlite3.Error:
        return None
    body = json.dumps([meta.get("generation"), meta.get("change_epoch"), state, changes],
                      default=str)
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def profile_state(state_dir: Path, *, rows: int = 30000, workers: int = 4,
                  force: bool = False) -> dict[str, Any]:
    """Profile a state directory's mirror into its own profile (``raw_markings.
    STATE_PROFILE``), which its readers prefer while it is the newer: what a projection
    build runs after it publishes. Skipped (``skipped: mirror_unchanged``) when the state's
    profile was written under these rules for the same mirror (``fingerprint``), unless
    ``force``: a build whose captures changed no finance-sensitive relation spends one
    small read, not a profiling pass. Returns counts."""
    from ontology import raw_markings
    out_path = Path(state_dir) / raw_markings.STATE_PROFILE
    if not force:
        now = fingerprint(Path(state_dir) / MIRROR)
        try:
            kept = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            kept = {}
        if now is not None and kept.get("mirror_fingerprint") == now \
                and kept.get("rules") == RULES:
            # Still current: say when it was last found so (``check`` ages it from then).
            kept["checked_at"] = _now()
            write(kept, out_path)
            return {"skipped": "mirror_unchanged", "mirror_fingerprint": now}
    doc = profile(Path(state_dir) / MIRROR, rows, workers)
    write(doc, out_path)
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
        # Its age: since it was profiled, or last found current for an unchanged mirror.
        at = datetime.datetime.strptime(
            max(str(doc.get("profiled_at") or ""), str(doc.get("checked_at") or "")),
            "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
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


def tripwire_scan(state_dir: Path, *, rows: int = 1000) -> dict[str, Any]:
    """The tripwire over a sample of the state's mirror (read-only): per finance-sensitive
    relation its first ``rows`` rows, read with the profile a reader of ``state_dir`` uses,
    counting the (relation, column) whose visible cells look like money (``raw_markings.
    _trips``: the profile is stale there) and those cells. The truth check reports it."""
    from ontology import raw_markings
    from ontology import schema as ontology_schema
    domains = raw_markings._catalog_domains(str(ontology_schema.META_DIR))
    relations = sorted(r for r, d in domains.items() if d in raw_markings.SENSITIVE_DOMAINS)
    hits: dict[str, int] = {}
    db = sqlite3.connect(f"file:{Path(state_dir) / MIRROR}?mode=ro", uri=True)
    try:
        for relation in relations:
            found = raw_markings.relation_markings(raw_markings.PORTAL, relation, None,
                                                   [state_dir])
            if not found.tripwire:
                continue
            for (text,) in db.execute(
                    "SELECT row_json FROM row_current WHERE relation = ? AND run_length = 1 "
                    "AND absent_since_capture IS NULL LIMIT ?", (relation, rows)):
                row = json.loads(text)
                for column, kind in found.tripwire.items():
                    if isinstance(row, dict) and column in row \
                            and raw_markings._trips(kind, row[column]):
                        key = f"{relation}.{column}"
                        hits[key] = hits.get(key, 0) + 1
    finally:
        db.close()
    return {"columns": len(hits), "cells": sum(hits.values()), "rows_per_relation": rows,
            "top": sorted(hits, key=lambda k: (-hits[k], k))[:20]}


def health(state_dir: Path, *, rows: int = 1000) -> dict[str, Any]:
    """``check`` and ``tripwire_scan`` together, never raising: what a health check (the
    truth check) runs after every publish and delta cycle."""
    try:
        out = check(state_dir)
    except Exception as error:  # noqa: BLE001 - a health line, never a crash
        out = {"ok": False, "problems": [f"check_failed:{type(error).__name__}"]}
    try:
        out["tripwire"] = tripwire_scan(state_dir, rows=rows)
    except Exception as error:  # noqa: BLE001
        out["tripwire"] = {"error": type(error).__name__}
    return out


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
