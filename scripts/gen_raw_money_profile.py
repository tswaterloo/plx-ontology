"""Write ``ontology/raw_money_profile.json``: per column of every finance-sensitive Portal
relation, what its values are across the relation, read once from the source mirror
(read-only). ``ontology.raw_markings`` decides money per column from it, never per cell.

A column is ``money`` when it is mostly numeric (at least half of its non-empty cells) and
at least ``FRACTIONAL_SHARE`` of its non-zero numbers are fractional (``2210.400``;
``2225`` is whole, and so is FileMaker's ``1.0``), ``whole`` when it is mostly numeric
with fewer fractional figures (identifiers, compact dates, flags, counts: an invoice
number column holding a stray ``12.5`` among thousands of whole ones stays whole; real
amounts measured at 2.4 % or more, identifiers at 0.2 % or less), ``text`` when it is not mostly numeric, ``empty`` when no sampled
cell holds a value. Only column names are written, never a value.

    python3 -m scripts.gen_raw_money_profile --mirror <source-mirror.sqlite> [--rows 30000]
        [--workers 4] [--out ontology/raw_money_profile.json]
"""

from __future__ import annotations

import argparse
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
    """{column: money | whole | text | empty} over ``rows`` (dicts)."""
    filled: dict[str, int] = {}
    numeric: dict[str, int] = {}
    fractional: dict[str, int] = {}
    zeros: dict[str, int] = {}
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
            out[column] = "money" if fractional.get(column) and share >= FRACTIONAL_SHARE \
                else "whole"
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--mirror", required=True, type=Path)
    parser.add_argument("--rows", type=int, default=30000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    from ontology import raw_markings
    from ontology import schema as ontology_schema
    domains = raw_markings._catalog_domains(str(ontology_schema.META_DIR))
    relations = sorted(r for r, d in domains.items() if d in raw_markings.SENSITIVE_DOMAINS)
    db = sqlite3.connect(f"file:{args.mirror}?mode=ro", uri=True)
    generation = dict(db.execute("SELECT * FROM mirror_meta")).get("generation")
    db.close()
    with multiprocessing.Pool(args.workers) as pool:
        found = pool.map(_profile, [(str(args.mirror), r, args.rows) for r in relations])
    out = {"format": FORMAT,
           "note": "Per column of each finance-sensitive Portal relation (reporting-catalog "
                   "domains purchasing, orders, customers): money (mostly numeric, at least "
                   "0.5 % of its non-zero figures fractional; N.0 is whole), whole, text or "
                   "empty, read from the "
                   "source mirror by scripts/gen_raw_money_profile.py. Column names only. "
                   "Read by ontology/raw_markings.py; a column it does not list is finance.",
           "mirror_generation": generation, "rows_per_relation": args.rows,
           "relations": {}}
    for relation, sampled, columns in found:
        out["relations"][relation] = {"rows": sampled, **{
            kind: sorted(c for c, k in columns.items() if k == kind)
            for kind in ("money", "whole", "text", "empty")}}
    args.out.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"relations": len(found), "money_columns": sum(
        len(v["money"]) for v in out["relations"].values())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
