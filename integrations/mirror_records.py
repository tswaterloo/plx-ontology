"""Records, not rows: how many business records the source mirror holds per relation.

``relation_state.row_count`` counts the mirror's present rows. For a relation the Portal
ETL re-inserts under fresh ids (``ontology.project.ETL_REINSERTED``: every run adds a
copy of each silver row, and incremental refreshes never see the old ids go), rows are
copies: ``public.app_production_task`` held 9,074,784 rows for a few tens of thousands
of records. Coverage and status quote records. A record is what the projection keeps one
object for: the row's values less the minted columns (``id``, ``created_at``,
``updated_at``), the natural key ``ontology.project._natural_key`` uses. Every other
relation's records are its present keys (a duplicate-key run counts once).

The same counting as ``scripts.mirror_record_counts``, as a product
function the mirror's coverage reads. Read-only: it only runs ``SELECT``s.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Iterable, Mapping

NATURAL_KEY = "natural_key_less_minted"
DISTINCT_KEY = "distinct_key"


def minted_columns(source: str = "portal_api") -> Mapping[str, frozenset[str]]:
    """relation -> the columns the ETL mints per copy (lazy: ``ontology.project`` is
    large and imports the mirror's own package)."""
    from ontology.project import ETL_REINSERTED
    return ETL_REINSERTED.get(source, {})


def relation_records(conn: sqlite3.Connection, relation: str, rows_current: int,
                     minted: Mapping[str, frozenset[str]]) -> dict[str, Any]:
    """``records``, ``copies_per_record`` and ``basis`` of one relation's present rows."""
    present = "relation = ? AND absent_since_capture IS NULL"
    drop = sorted(minted.get(relation, ()))
    if drop:
        paths = ", ".join("?" for _ in drop)
        records = conn.execute(
            f"SELECT COUNT(DISTINCT json_remove(row_json, {paths})) FROM row_current "
            f"WHERE {present}", [f"$.{c}" for c in drop] + [relation]).fetchone()[0]
        basis = NATURAL_KEY
    else:
        records = conn.execute(f"SELECT COUNT(DISTINCT key_json) FROM row_current "
                               f"WHERE {present}", (relation,)).fetchone()[0]
        basis = DISTINCT_KEY
    records = int(records or 0)
    return {"records": records,
            "copies_per_record": round(rows_current / records, 1) if records else None,
            "basis": basis}


def record_counts(conn: sqlite3.Connection, relations: Iterable[str] | None = None,
                  minted: Mapping[str, frozenset[str]] | None = None) -> dict[str, Any]:
    """relation -> ``rows_current``, ``records``, ``copies_per_record``, ``basis`` for
    every relation in ``relation_state`` (or only ``relations``)."""
    minted = minted_columns() if minted is None else minted
    wanted = None if relations is None else set(relations)
    out: dict[str, Any] = {}
    for relation, row_count in conn.execute(
            "SELECT relation, row_count FROM relation_state ORDER BY relation").fetchall():
        if wanted is not None and relation not in wanted:
            continue
        out[relation] = {"rows_current": int(row_count),
                         **relation_records(conn, relation, int(row_count), minted)}
    return out


__all__ = ["DISTINCT_KEY", "NATURAL_KEY", "minted_columns", "record_counts",
           "relation_records"]
