"""The record page's ``alias`` items (speed specs 8.3 ``alias``, the old page's ``choices``): a bare
PO number is no one record (numbers repeat across systems and counterparties), so for each current
identifier of a purchase order (``ontology.identifiers``: every kind and role, the identifier as
the table normalises it) one item PER BUSINESS ROW that holds it (copies of one order are one
candidate): ``page_kind`` ``alias``, ``page_key`` the identifier, payload = the record id to open
and the row's number, counterparty, systems and status, exactly what ``page_sql.choices`` labels.

PERMISSION. An item carries its candidate row's own access class (the purchase order row), so a
reader who may not read a candidate never receives it and nothing says it exists (``choices``:
"left out, never counted"); lineage = the row's plus the record id, so the people exclusion
follows it (``page_items.apply``). The payload holds only the four label columns; the build
REFUSES when any column class of a candidate row marks or needs one of them (no item could carry
it for every reader) and when a matching identifier is itself marked (the old route sent a
marked identifier to the full find): nothing is dropped silently.

  python3 -m operating.pg.record_alias build [--schema S] [--dsn DSN]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from typing import Any, Sequence

from operating.pg import business_views as bv
from operating.pg import page_items

KIND = "alias"
# the payload's keys and the row columns they are read from (the old ``choices`` label's inputs)
LABEL = {"number": "po_number", "party": "supplier", "systems": "source_systems",
         "status": "status"}
CANDIDATES = (
    "SELECT i.normalized AS code, m.hdr, min(m.record_id) AS rep FROM ontology.identifiers i "
    "JOIN rp_map m ON m.record_id = i.object_id WHERE i.superseded_at IS NULL AND "
    "i.normalized IS NOT NULL AND i.normalized <> '' GROUP BY i.normalized, m.hdr")


def _check(pg: Any, s: str) -> None:
    marked = pg.execute(
        "SELECT count(*) FROM ontology.identifiers i JOIN rp_map m ON m.record_id = i.object_id "
        "WHERE i.superseded_at IS NULL AND COALESCE(NULLIF(i.markings, ''), '[]') <> '[]'"
    ).fetchone()[0]
    if marked:
        raise RuntimeError(f"{marked} identifiers of purchase orders are marked: the alias items "
                           "cannot carry them; the page's reader decides (not applied)")
    held = pg.execute(
        f"SELECT DISTINCT x.k FROM ({CANDIDATES}) q JOIN {s}.purchase_orders p ON p.handle = "
        f"q.hdr JOIN {s}.col_classes c ON c.id = p.col_class CROSS JOIN LATERAL (SELECT "
        "jsonb_object_keys(c.marks) k UNION SELECT jsonb_object_keys(c.needs)) x WHERE "
        "x.k = ANY(%s)", (list(LABEL.values()),)).fetchall()
    if held:
        raise RuntimeError(f"{[r[0] for r in held]} are marked columns of a purchase order that holds an identifier: "
                           "the alias label would leak them (not applied)")


def code_fingerprint() -> str | None:
    from ontology.build_keys import code_digest
    digest = code_digest([("operating.pg.record_alias", "build"),
                          ("operating.pg.record_alias", "stage_sql"),
                          ("operating.pg.page_items", "apply")])
    return digest[:32] if digest else None


def stage_sql(s: str, t: str) -> str:
    cols = ("(page_key, section, seq, payload, src_handle, source_systems, access_class, "
            "col_class, ai_ok, ver, biz, cellcol, page_kind, handle, as_of, dep_ver, code_fp, "
            "src_view, lin_ids)")
    label = ", ".join(f"'{k}', p.{c}" for k, c in LABEL.items())
    return (f"INSERT INTO {s}.{t} {cols} SELECT q.code, 'alias', (row_number() OVER (PARTITION BY "
            "q.code ORDER BY q.hdr) - 1)::int, jsonb_build_object('record_id', q.rep, "
            f"{label}), p.handle, p.source_systems, p.access_class, 0, p.ai_ok, 0, true, NULL, "
            f"'{KIND}', '', %s::timestamptz, 0, '', 'purchase_orders', ARRAY[q.rep] FROM "
            f"({CANDIDATES}) q JOIN {s}.purchase_orders p ON p.handle = q.hdr")


def build(dsn: str, schema: str | None = None) -> dict[str, Any]:
    """Stage every code's candidates and apply them in place (``page_items.apply``)."""
    import psycopg
    from operating.pg import record_pages
    s = schema or bv.SCHEMA
    started = time.monotonic()
    with psycopg.connect(dsn, autocommit=True) as pg:
        with page_items.room(pg, 0.1, "record_alias.build"):
            page_items.ensure(pg, s)
            stamp = pg.execute(f"SELECT built_at FROM {s}.state WHERE view = 'purchase_orders'"
                               ).fetchone()
            if stamp is None:
                raise RuntimeError(f"{s}.state has no build time for purchase_orders")
            as_of = dt.datetime.fromisoformat(stamp[0]) if isinstance(stamp[0], str) else stamp[0]
            pg.execute("SET statement_timeout = '900s'")
            stage = page_items.stage(pg, KIND, s)
            with pg.transaction():
                record_pages.members(pg, s)
                _check(pg, s)
                pg.execute(stage_sql(s, stage.split(".", 1)[1]), (as_of.isoformat(),))
            out = page_items.apply(pg, KIND, stage, keys=None, code_fp=code_fingerprint() or "",
                                   job="record_alias.build", schema=s, notes={})
    return {**out, "seconds": round(time.monotonic() - started, 1)}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m operating.pg.record_alias")
    ap.add_argument("command", choices=("build",))
    ap.add_argument("--schema")
    ap.add_argument("--dsn")
    args = ap.parse_args(argv)
    from operating.pg import config
    print(json.dumps(build(args.dsn or config.dsn(), args.schema)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
