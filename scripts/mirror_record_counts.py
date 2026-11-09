#!/usr/bin/env python3
"""Records, not rows: how many business records the source mirror holds per relation.

``relation_state.row_count`` counts the mirror's present rows. For a relation the Portal
ETL re-inserts under fresh ids (``ontology.project.ETL_REINSERTED``: every run adds a
copy of each silver row, and incremental refreshes never see the old ids go), rows are
copies: ``public.app_production_task`` held 9,074,784 rows for 42,111 projected objects.
Coverage must quote records. A record here is what the projection keeps one object for:
the row's values less the minted columns (``id``, ``created_at``, ``updated_at``), the
same natural key ``ontology.project._natural_key`` uses. Every other relation's records
are its present rows (one per key; a duplicate-key run counts once).

Read-only: the mirror opens with ``mode=ro&immutable=1`` (nothing is written beside it;
an active writer's uncommitted WAL is not seen). Counting distinct records sorts on disk
in ``SQLITE_TMPDIR`` (set it to a volume with room for the largest relation).

    python3 -m scripts.mirror_record_counts --mirror <source-mirror.sqlite> [--relation R ...]

Prints JSON: per relation ``rows_current``, ``records``, ``copies_per_record`` and
``basis`` (``natural_key_less_minted`` or ``distinct_key``), plus totals.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Iterable, Mapping

from ontology.project import ETL_REINSERTED


def minted_columns(source: str = "portal_api") -> Mapping[str, frozenset[str]]:
    return ETL_REINSERTED.get(source, {})


def record_counts(conn: sqlite3.Connection, relations: Iterable[str] | None = None,
                  minted: Mapping[str, frozenset[str]] | None = None) -> dict[str, Any]:
    minted = minted_columns() if minted is None else minted
    wanted = None if relations is None else set(relations)
    out: dict[str, Any] = {}
    states = conn.execute("SELECT relation, row_count FROM relation_state ORDER BY relation")
    for relation, row_count in states.fetchall():
        if wanted is not None and relation not in wanted:
            continue
        present = "relation = ? AND absent_since_capture IS NULL"
        drop = sorted(minted.get(relation, ()))
        if drop:
            paths = ", ".join("?" for _ in drop)
            records = conn.execute(
                f"SELECT COUNT(DISTINCT json_remove(row_json, {paths})) FROM row_current "
                f"WHERE {present}", [f"$.{c}" for c in drop] + [relation]).fetchone()[0]
            basis = "natural_key_less_minted"
        else:
            records = conn.execute(f"SELECT COUNT(DISTINCT key_json) FROM row_current "
                                   f"WHERE {present}", (relation,)).fetchone()[0]
            basis = "distinct_key"
        out[relation] = {"rows_current": int(row_count), "records": int(records),
                         "copies_per_record": round(row_count / records, 1) if records else None,
                         "basis": basis}
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mirror", required=True, type=Path)
    parser.add_argument("--relation", action="append")
    args = parser.parse_args(argv)
    if not args.mirror.is_file():
        print(json.dumps({"error": "mirror_not_found"}))
        return 2
    conn = sqlite3.connect(f"file:{args.mirror}?mode=ro&immutable=1", uri=True)
    counts = record_counts(conn, args.relation)
    print(json.dumps({"relations": counts,
                      "rows_current": sum(c["rows_current"] for c in counts.values()),
                      "records": sum(c["records"] for c in counts.values())}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
