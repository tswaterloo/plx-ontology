"""Decisions, requests and customer feedback read out of mail and Teams text, as rows a
reader sees only when they may read the message each came from.

The findings are fields of the one reading per message (``operating.assist_readings``,
``assist.readings.findings``: contract C4); there is no second pass over the messages and no
table of its own. This module is only the guarded view plx.sql (and the Insights tiles over
it) reads.

WHO SEES A ROW (``view_ddl``): the guarded view ``<schema>_sql.comms_findings`` (plx.sql's
AI door) shows a finding only with a copy of its message the reader reads NOW in their own
guarded ``emails`` / ``teams_messages`` view (unit held, the people exclusion, hidden ids,
the Teams history window: all of that view's rule, at read time). A message the reader
cannot read shows no finding of it; a message excluded or removed later hides its findings
at once (and the next reading run deletes its reading). The business build re-creates the
view with the communications views (``sql_views.drop`` / ``create``): it never blocks their
swap.

Evidence: isolated-fixture (``operating/tests/test_assist_readings.py``)."""
from __future__ import annotations

from typing import Any, Sequence

from operating.pg import business_views as bv

VIEW = "comms_findings"
SOURCES = ("emails", "teams_messages")
READINGS = "assist.readings"
# The findings' one face outside the Assist schema: exactly the reading columns a finding row
# needs (no act, actor, quote or text fields). The guarded view and the Insights fast copy
# (``operator_api.insights_fast``, on the API's role) read it; the API's role holds SELECT on
# this view and USAGE on the schema, nothing else of ``assist``. So the findings jsonb (each
# finding's kind, topic, summary, who) is readable by plx_api at the database level: code
# only (the fast copy), never reader-reachable (plx.sql runs as plx_sql_reader, which reads
# findings only through the guarded view, per message the reader's own door shows).
FEED = "assist.findings_feed"
FEED_ROLE = "plx_api"


def feed_ddl() -> list[str]:
    """Migration 0063's findings feed (a test holds them equal to the file): the view, and its
    grant to ``FEED_ROLE`` when that role exists (a database without it grants nothing)."""
    return [
        f"CREATE OR REPLACE VIEW {FEED} AS SELECT source, handles, said_at, from_side, "
        f"findings FROM {READINGS} WHERE findings <> '[]'::jsonb",
        f"REVOKE ALL ON {FEED} FROM PUBLIC",
        "DO $feed$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
        f"'{FEED_ROLE}') THEN GRANT USAGE ON SCHEMA assist TO {FEED_ROLE}; GRANT SELECT ON "
        f"{FEED} TO {FEED_ROLE}; END IF; END $feed$",
    ]


def present(pg: Any, guarded: str) -> list[str]:
    """The communications views the guarded schema serves now (in ``SOURCES`` order)."""
    rows = pg.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = "
                      "c.relnamespace WHERE n.nspname = %s AND c.relkind = 'v' AND "
                      "c.relname = ANY(%s)", (guarded, list(SOURCES))).fetchall()
    have = {r[0] for r in rows}
    return [v for v in SOURCES if v in have]


def view_ddl(src: str, dst: str, sources: Sequence[str]) -> str:
    """``CREATE VIEW <dst>.comms_findings``: each finding of a reading with the first copy of
    its message the reader's own guarded view shows now (security barrier; the inner views'
    rule is theirs, read with the reader's settings). The row's sender, thread and parties
    are the visible copy's; ``handle`` is that copy's (cite it, open it with plx.open).
    ``src`` is the business schema the readings' messages come from (the readings table is
    ``FEED`` whatever it is)."""
    del src
    parts = []
    for view in sources:
        if view == "emails":
            parts.append(f"SELECT e.handle, e.sender, e.subject, e.thread, e.customers, "
                         f"e.suppliers FROM {dst}.emails e WHERE f.source = 'emails' "
                         "AND e.handle = ANY(f.handles)")
        else:
            # a Teams reading's handles are its one message: equality on that handle reads
            # the handle index alone (``= ANY`` made the planner AND it with the unit index)
            parts.append(f"SELECT t.handle, t.sender, t.subject, t.chat, t.customers, "
                         f"t.suppliers FROM {dst}.teams_messages t WHERE f.source = "
                         "'teams_messages' AND t.handle = f.handles[1]")
    if not parts:
        raise ValueError("no communications view to guard the findings with")
    return (f"CREATE OR REPLACE VIEW {dst}.{VIEW} WITH (security_barrier) AS SELECT i.item->>'kind' AS "
            "kind, i.item->>'topic' AS topic, i.item->>'summary' AS summary, "
            "coalesce(i.item->>'who', '') AS who, f.from_side, f.said_at, (f.said_at AT TIME "
            f"ZONE '{bv.BUSINESS_TZ}')::date AS said_date, CASE f.source WHEN 'emails' THEN "
            "'email' ELSE 'teams' END AS source, v.sender, v.subject, v.thread, v.customers, "
            f"v.suppliers, v.handle, '{{}}'::text[] AS withheld FROM {FEED} f CROSS JOIN "
            "LATERAL jsonb_array_elements(f.findings) i(item) CROSS JOIN LATERAL (SELECT * "
            "FROM (" + " UNION ALL ".join(parts) + ") x(handle, sender, subject, thread, "
            "customers, suppliers) ORDER BY x.handle LIMIT 1) v")


def drop_view(write: Any, schema: str | None = None) -> None:
    """Before a communications view is dropped (a build's swap): the findings view reads it."""
    from operating.pg import sql_views
    write.execute(f"DROP VIEW IF EXISTS {sql_views.guarded_schema(schema)}.{VIEW}")


def create_view(write: Any, schema: str | None = None) -> bool:
    """The guarded findings view over the communications views served now, granted to
    plx.sql's role only. Nothing when the readings table or the guarded schema is missing
    (fail closed: no view, plx.sql cannot read it; it says "not built yet")."""
    from operating.pg import sql_views
    dst = sql_views.guarded_schema(schema)
    if write.execute("SELECT to_regclass(%s) IS NULL OR to_regnamespace(%s) IS NULL",
                     (FEED, dst)).fetchone()[0]:
        return False
    sources = present(write, dst)
    if not sources:
        write.execute(f"DROP VIEW IF EXISTS {dst}.{VIEW}")
        return False
    # one CREATE OR REPLACE statement: readers never see the view gone or short of a source
    write.execute(view_ddl(schema or bv.SCHEMA, dst, sources))
    write.execute(f"REVOKE ALL ON {dst}.{VIEW} FROM PUBLIC")
    sql_views._grant(write, (sql_views.ROLE,), f"SELECT ON {dst}.{VIEW}")
    return True
