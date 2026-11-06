"""Row parity of the Postgres ontology with the generation it was loaded from.

``python3 -m operating.pg.ontology_parity STORE [--sample N] [--seed S]``: for ``N``
objects sampled from the store (read-only), every row a ``plx.object`` read is built
from (the object, its versions, removals, values, identifiers, links from and to it,
its event rows) is read from both stores and compared column for column. Objects whose
values were not loaded (``load_state`` ``values_skipped``) are compared without their
values (none since 0007: every value is loaded). Prints and returns ``{"objects", "equal", "differing": [first ids]}``.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from typing import Any, Sequence

from ontology import store as S
from ontology import store_ddl
from operating.pg.ontology_load import TABLES

# The logical rows a plx.object read is built from, in both stores (Postgres: the views
# over the compact tables).
_COLUMNS = {
    "objects": TABLES["objects"].columns[1:],   # without row_no
    "object_removals": TABLES["object_removals"].columns,
    "object_versions": TABLES["object_versions"].columns[1:],
    "identifiers": TABLES["identifiers"].columns,
    "event_objects": TABLES["event_objects"].columns,
    "links": store_ddl.LINK_COLUMNS,
    "object_values": store_ddl.VALUE_COLUMNS,
}


def _columns(table: str) -> tuple[str, ...]:
    return tuple(_COLUMNS[table])

_BY_OBJECT = {
    "objects": "object_id = ?",
    "object_removals": "object_id = ?",
    "object_versions": "object_id = ?",
    "identifiers": "object_id = ?",
    "links_from": "from_object = ?",
    "links_to": "to_object = ?",
    "event_objects": "object_id = ?",
}


def _sqlite_rows(conn: sqlite3.Connection, part: str, object_id: str) -> list[tuple]:
    if part == "object_values":
        out = []
        for batch in S._value_rows(conn, object_id, object_id + "\x00"):
            for row in batch:
                if row[0] == object_id:
                    out.append(row)
        return sorted(_norm(r) for r in out)
    table = "links" if part.startswith("links") else part
    names = ", ".join(_columns(table))
    return sorted(_norm(r) for r in conn.execute(
        f"SELECT {names} FROM {table} WHERE {_BY_OBJECT[part]}", (object_id,)))


def _pg_rows(cur: Any, part: str, object_id: str) -> list[tuple]:
    table = "links" if part.startswith("links") else part
    where = "object_id = %s" if part == "object_values" else \
        _BY_OBJECT[part].replace("?", "%s")
    cur.execute(f"SELECT {', '.join(_columns(table))} FROM ontology.{table} WHERE {where}",
                (object_id,))
    return sorted(_norm(r) for r in cur.fetchall())


def _norm(row: Sequence[Any]) -> tuple:
    """A row as comparable text: each cell (is it NULL, its text), so rows sort with NULLs."""
    return tuple((v is None, "" if v is None else str(v)) for v in row)


def parity(path: str, dsn: str, *, sample: int = 10_000, seed: int = 11) -> dict[str, Any]:
    import psycopg

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA cache_size = -1048576")
    with psycopg.connect(dsn) as pg, pg.cursor() as cur:
        cur.execute("SELECT value FROM ontology.load_state WHERE name = 'generation'")
        state = cur.fetchone()[0]
        skipped = tuple(state.get("values_skipped") or ())
        top = conn.execute("SELECT MAX(rowid) FROM objects").fetchone()[0]
        rng = random.Random(seed)
        ids: list[str] = []
        while len(ids) < sample:
            row = conn.execute("SELECT object_id FROM objects WHERE rowid >= ? LIMIT 1",
                               (rng.randint(1, top),)).fetchone()
            if row:
                ids.append(row[0])
        differing: list[str] = []
        parts = [*_BY_OBJECT, "object_values"]
        for object_id in ids:
            for part in parts:
                if part == "object_values" and any(object_id.startswith(p) for p in skipped):
                    continue
                if _sqlite_rows(conn, part, object_id) != _pg_rows(cur, part, object_id):
                    differing.append(f"{part}:{object_id}")
                    break
    conn.close()
    return {"objects": len(ids), "equal": len(ids) - len(differing),
            "differing": differing[:20]}


def main(argv: Sequence[str] | None = None) -> int:
    from operating.pg import config

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("store")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--sample", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=11)
    args = parser.parse_args(argv)
    result = parity(args.store, args.dsn or config.dsn(), sample=args.sample, seed=args.seed)
    print(json.dumps(result))
    return 0 if not result["differing"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
