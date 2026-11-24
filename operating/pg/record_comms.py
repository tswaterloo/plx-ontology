"""``<biz>.record_comms``: the content-free index of which messages name which record (speed specs
8.2, SV3 rev 2). One row per (page key, comms view, message): the message's handle, its time (order
only) and the source record handle that matched (``via``). No subject, sender or text, so nothing
here is a comms fingerprint input and nothing is revealed by the table itself: a read always joins it
to the guarded ``<biz>_person.<view>``, whose own predicate drops what the reader may not see.

The link is ``<view>.records`` (the source record handles a message names), joined to
``<biz>.records.handle`` (not unique: a handle can be several records) and spread over every member
record of the same purchase order (``record_pages.members``), because record pages are keyed per member
record today. ``build`` diff-applies in one transaction: rows gone are deleted, new ones inserted, a
changed time or ``via`` updated; nothing else is written.

  python3 -m operating.pg.record_comms build [--schema S] [--dsn DSN]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any, Sequence

from operating.pg import business_views as bv

TABLE = "record_comms"
MIGRATION = Path(__file__).with_name("migrations") / "0068_record_comms.sql"
# (comms view, the column that orders it): the four views a record's messages live in
VIEWS = (("emails", "received_at"), ("attachments", "seen_at"),
         ("teams_messages", "created_at"), ("sharepoint_files", "modified_at"))
COLUMNS = ("record_id", "comm_view", "comm_handle", "at", "via")


def ensure(pg: Any, schema: str | None = None) -> None:
    from operating.pg import business_comms as bc
    from operating.pg import sql_views
    s = schema or bv.SCHEMA
    pg.execute(MIGRATION.read_text(encoding="utf-8").replace("biz_person", f"{s}_person")
               .replace("biz.", f"{s}."))
    # the count-only view of the messages the reader's scope holds back, made from the comms
    # views' own conditions (``business_comms.hidden_ddl``); only over the tables built here
    person = sql_views.person_schema(s)
    built = [bv.VIEWS[n] for n, _ in VIEWS if n in bv.VIEWS and pg.execute(
        "SELECT to_regclass(%s)", (f"{s}.{n}",)).fetchone()[0] is not None]
    if built:
        sql_views.put(pg, f"{person}.record_comms_hidden", bc.hidden_ddl(s, person, built))
        sql_views._grant(pg, sql_views.PERSON_READERS, f"SELECT ON {person}.record_comms_hidden")


def build(dsn: str, schema: str | None = None) -> dict[str, Any]:
    """Stage every (page key, message) pair and make the table equal it, in place. Returns the
    counts per view and the handles of ``records`` that matched no record (listed by prefix:
    ``unmatched``, never dropped silently)."""
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as pg:
        return apply(pg, schema)


def apply(pg: Any, schema: str | None = None) -> dict[str, Any]:
    """``build`` on the caller's autocommit connection (its statement count is the same for any
    number of messages: every step is set-based)."""
    from operating.pg import record_pages
    s = schema or bv.SCHEMA
    started = time.monotonic()
    for needed in ("records", "purchase_orders", "lineage"):
        if pg.execute("SELECT to_regclass(%s)", (f"{s}.{needed}",)).fetchone()[0] is None:
            raise RuntimeError(f"{s}.{needed} is not built: no record comms")
    from operating.pg import page_items
    with page_items.room(pg, 0.5, "record_comms.build"):   # the WAL budget and the one disk budget
        ensure(pg, s)
        pg.execute("SET statement_timeout = '900s'")
        with pg.transaction():
            record_pages.members(pg, s)
            pg.execute("DROP TABLE IF EXISTS pg_temp.rc_stage, pg_temp.rc_unmatched, pg_temp.rc_pages")
            pg.execute("CREATE TEMP TABLE rc_stage (record_id text, comm_view text, "
                       "comm_handle text, at timestamptz, via text)")
            pg.execute("CREATE TEMP TABLE rc_unmatched (comm_view text, via text)")
            # a record's member records (itself included) for every page key
            pg.execute("CREATE TEMP TABLE rc_pages AS SELECT DISTINCT a.record_id AS src, "
                       "b.record_id AS page FROM rp_map a JOIN rp_map b USING (hdr)")
            pg.execute("CREATE INDEX ON rc_pages (src)")
            for view, at in VIEWS:
                if pg.execute("SELECT to_regclass(%s)", (f"{s}.{view}",)).fetchone()[0] is None:
                    continue
                pg.execute(
                    "INSERT INTO rc_stage SELECT DISTINCT ON (page, e.handle) page, %s, e.handle, "
                    f"e.{at}, h FROM (SELECT e.handle, e.{at}, h FROM {s}.{view} e CROSS JOIN "
                    "LATERAL unnest(e.records) h WHERE e.records <> '{}') e "
                    f"JOIN {s}.records r ON r.handle = e.h "
                    "CROSS JOIN LATERAL (SELECT src, page FROM rc_pages WHERE src = r.record_id "
                    "UNION SELECT r.record_id, r.record_id) p(src, page) "
                    "ORDER BY page, e.handle, e.h", (view,))
                pg.execute(
                    "INSERT INTO rc_unmatched SELECT DISTINCT %s, h FROM (SELECT unnest(records) "
                    f"h FROM {s}.{view} WHERE records <> '{{}}') x WHERE NOT EXISTS (SELECT 1 "
                    f"FROM {s}.records r WHERE r.handle = x.h)", (view,))
            gone = pg.execute(
                f"DELETE FROM {s}.{TABLE} t WHERE NOT EXISTS (SELECT 1 FROM rc_stage st WHERE "
                "st.record_id = t.record_id AND st.comm_view = t.comm_view AND "
                "st.comm_handle = t.comm_handle)").rowcount
            made = pg.execute(
                f"INSERT INTO {s}.{TABLE} AS t SELECT * FROM rc_stage ON CONFLICT (record_id, "
                "comm_view, comm_handle) DO UPDATE SET at = EXCLUDED.at, via = EXCLUDED.via WHERE "
                "(t.at, t.via) IS DISTINCT FROM (EXCLUDED.at, EXCLUDED.via) RETURNING (xmax = 0)"
            ).fetchall()
            per_view = dict(pg.execute("SELECT comm_view, count(*) FROM rc_stage GROUP BY 1"
                                       ).fetchall())
            lost = pg.execute("SELECT comm_view, split_part(via, '/', 3), count(*) FROM "
                              "rc_unmatched GROUP BY 1, 2 ORDER BY 3 DESC").fetchall()
        inserted = sum(1 for x in made if x[0])
        return {"rows": per_view, "inserted": inserted, "updated": len(made) - inserted,
                "deleted": gone, "unmatched": [list(x) for x in lost],
                "seconds": round(time.monotonic() - started, 1)}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m operating.pg.record_comms")
    ap.add_argument("command", choices=("build",))
    ap.add_argument("--schema")
    ap.add_argument("--dsn")
    args = ap.parse_args(argv)
    from operating.pg import config
    print(json.dumps(build(args.dsn or config.dsn(), args.schema)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
