"""The reader-scoped views ``plx.sql`` reads (migration 0042_sql_reader): one
``security_barrier`` view ``<biz schema>_sql.<view>`` over each built business table
``<biz schema>.<view>`` (``operating.pg.business_views``), owned by the build's role and
granted to the NOLOGIN role ``plx_sql_reader`` only (a communications view also to the
store's roles, ``COMMS_READERS``). That role holds no other grant: no ontology table, no
business table, no function of ours.

Each view is DOOR v2's (``operating.pg.door.view_ddl``, the canonical classes contract of
migration 0041): a row is there when its ``access_class`` is among the reader's
(transaction-local settings the tool sets before it switches to the role) and ``ai_ok``
(no text of it names Freightcom); a cell whose marking or ``needs`` the reader lacks reads
NULL and its column is named in ``withheld``. An unset setting reads as no scope: no row
(fail closed). A table without the class columns (an old-shape table not yet rebuilt or
converted, ``door.convert``) gets NO view: plx.sql cannot read it (fail closed).
A communications table (``View.fill``: emails, teams_messages) gets its own guarded view
(``business_comms.view_ddl``: unit held, ``ai_ok``, the people exclusion, hidden ids and
the Teams window inside the view, from the ``SETTINGS`` the tool sets), and a person's
own view in ``person_schema`` (the same rule without ``ai_ok``: Freightcom is kept from
the AI only), which ``ROLE`` cannot read.

The business build swaps a table in one transaction (``build_view``); it calls ``drop``
before and ``create`` after the rename, so the guarded view always reads the live table;
``create`` then has the door follow the people exclusion (``door.follow``).
A PERSON's pages (``plx.page``, never a model tool) read a second set of views,
``<biz schema>_page.<view>`` (migration 0043_page_reader), granted to the NOLOGIN role
``plx_page_reader`` only: the same door WITHOUT the ``ai_ok`` predicate (Freightcom data never goes through an AI model, people may still see it). The AI's role
holds no grant on that schema, so no statement of the model's can read a Freightcom row.
``create`` and ``drop`` keep both sets in step.

``python3 -m operating.pg.sql_views install`` (re)creates the schemas, the roles' grants
and every view over the tables built so far (after migrations 0042 and 0043, or for a
scratch schema ``PLX_BIZ_SCHEMA``)."""
from __future__ import annotations

import argparse
import sys
from typing import Any, Sequence

from operating.pg import business_views as bv

ROLE = "plx_sql_reader"
# the store's login roles the comms search/open statement runs as (``ontology_tools_sql``
# ``_comms_scoped``: it reads the text index beside the comms views, which ``ROLE`` may
# not): USAGE on the guarded schema and SELECT on its communications views only; on the
# person schema (its views) the API's role only. Never ``ROLE`` on the person schema.
COMMS_READERS = ("plx_api", "plx_eval")
PERSON_READERS = ("plx_api",)
# the communications views' reader settings (``business_comms.view_ddl``)
SETTINGS = ("plx.reader_audience", "plx.reader_marks", "plx.reader_hidden")
PAGE_ROLE = "plx_page_reader"
# the old-shape (v1) permission and lineage columns: never in a v2 table
INTERNAL = ("object_ids", "audience_keys", "rec_marks", "col_marks", "cites")
SHOWN_COMMON = (("handle", "text", "the row's record handle (cite it)"),
                ("withheld", "text[]", "columns of this row whose value you may not read "
                                       "(they read NULL)"))


def guarded_schema(schema: str | None = None) -> str:
    return f"{schema or bv.SCHEMA}_sql"


def person_schema(schema: str | None = None) -> str:
    """A person's own communications views (``business_comms.view_ddl(ai=False)``): no
    grant to ``ROLE`` or PUBLIC; only the store's role reads them, for the app's pages."""
    return f"{schema or bv.SCHEMA}_person"


def page_schema(schema: str | None = None) -> str:
    """The person-only views' schema (no ``ai_ok`` filter; ``PAGE_ROLE`` only)."""
    return f"{schema or bv.SCHEMA}_page"


def _setting(name: str) -> str:
    return f"ARRAY(SELECT json_array_elements_text(current_setting('{name}', true)::json))"


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _grant(write: Any, roles: Sequence[str], what: str) -> None:
    """``GRANT <what> TO`` each of ``roles`` that exists (idempotent)."""
    for role in roles:
        write.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
                      f"'{role}') THEN EXECUTE 'GRANT {what} TO {role}'; END IF; END $$")


def drop(write: Any, name: str, schema: str | None = None) -> None:
    write.execute(f"DROP VIEW IF EXISTS {guarded_schema(schema)}.{name}")
    if write.execute("SELECT to_regnamespace(%s)", (person_schema(schema),)
                     ).fetchone()[0] is not None:
        write.execute(f"DROP VIEW IF EXISTS {person_schema(schema)}.{name}")
    write.execute(f"DROP VIEW IF EXISTS {page_schema(schema)}.{name}")


def create(write: Any, v: bv.View, schema: str | None = None) -> None:
    """The guarded view of ``v`` (its schema made first when missing) and its grant."""
    dst = guarded_schema(schema)
    if write.execute("SELECT to_regnamespace(%s)", (dst,)).fetchone()[0] is None:
        return   # migration 0042 (or ``install``) not applied: plx.sql is off, nothing to do
    drop(write, v.name, schema)
    from operating.pg import door
    src = schema or bv.SCHEMA
    if v.fill:   # a communications table: its own guarded view rule (``business_comms``)
        from operating.pg import business_comms
        write.execute(business_comms.view_ddl(v, src, dst))
        person = person_schema(schema)
        if write.execute("SELECT to_regnamespace(%s)", (person,)).fetchone()[0] is not None:
            write.execute(business_comms.view_ddl(v, src, person, ai=False))
            _grant(write, PERSON_READERS, f"SELECT ON {person}.{v.name}")
        _grant(write, COMMS_READERS, f"SELECT ON {dst}.{v.name}")
        # plx.sql reads these views beside the door's: the door follows the exclusion set
        # (its v2 tables only) and every reader's door is decided again (as below)
        door.follow(write, src, v.name)
        door._bump(write, src)
    elif not door.is_v2(write, src, v.name):
        return   # an old-shape table: no view (fail closed) until it is rebuilt or converted
    else:
        write.execute(door.view_ddl(write, v, src, dst))
        page = page_schema(schema)
        has_page = write.execute("SELECT to_regnamespace(%s)", (page,)
                                 ).fetchone()[0] is not None
        if has_page:   # migration 0043 applied: a person's pages read this one (no ai_ok)
            write.execute(door.view_ddl(write, v, src, page, ai_only=False))
        door.follow(write, src, v.name)
        # the table, its lineage and its class counts may all be new (a rebuild's swap):
        # every reader's door (plx.sql's cache, keyed on the stamp) is decided again, in
        # this same transaction, so no reader keeps a scope computed against the old rows
        door._bump(write, src)
        if has_page:
            _grant(write, (PAGE_ROLE,), f"SELECT ON {page}.{v.name}")
    _grant(write, (ROLE,), f"SELECT ON {dst}.{v.name}")


def ensure(write: Any, schema: str | None = None) -> None:
    """Migrations 0042's and 0043's DDL for ``schema`` (the AI's and the person's guarded
    schemas, the roles, their grants)."""
    from operating.pg.migrate import MIGRATIONS_DIR
    ddl = (MIGRATIONS_DIR / "0042_sql_reader.sql").read_text(encoding="utf-8")
    write.execute(ddl.replace("biz_sql", guarded_schema(schema)))
    # the store's roles reach the communications views there (``COMMS_READERS``; SELECT
    # on those views only, granted as each is made by ``create``)
    _grant(write, COMMS_READERS, f"USAGE ON SCHEMA {guarded_schema(schema)}")
    # a person's own communications views: readable by the store's role only
    person = person_schema(schema)
    write.execute(f"CREATE SCHEMA IF NOT EXISTS {person}")
    write.execute(f"REVOKE ALL ON SCHEMA {person} FROM PUBLIC")
    write.execute(f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') "
                  f"THEN EXECUTE 'REVOKE ALL ON SCHEMA {person} FROM {ROLE}'; END IF; END $$")
    _grant(write, PERSON_READERS, f"USAGE ON SCHEMA {person}")
    ddl = (MIGRATIONS_DIR / "0043_page_reader.sql").read_text(encoding="utf-8")
    write.execute(ddl.replace("biz_page", page_schema(schema)))


def install(dsn: str, schema: str | None = None) -> list[str]:
    """The schema, the role and a guarded view over every business table built now."""
    import psycopg
    src = schema or bv.SCHEMA
    made = []
    with psycopg.connect(dsn, autocommit=True) as write:
        with write.transaction():
            ensure(write, schema)
            dst = guarded_schema(schema)
            # when each table was built, never how many rows it holds (a count over rows
            # the reader may not read). Dropped first: CREATE OR REPLACE cannot drop a column
            for where, role in ((dst, ROLE), (page_schema(schema), PAGE_ROLE)):
                write.execute(f"DROP VIEW IF EXISTS {where}.view_state")
                write.execute(f"CREATE VIEW {where}.view_state AS SELECT view, built_at "
                              f"FROM {src}.state")
                _grant(write, (role,), f"SELECT ON {where}.view_state")
            for name, v in bv.VIEWS.items():
                if write.execute("SELECT to_regclass(%s)", (f"{src}.{name}",)
                                 ).fetchone()[0] is None:
                    continue
                create(write, v, schema)
                made.append(f"{guarded_schema(schema)}.{name}")
    return made


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m operating.pg.sql_views")
    ap.add_argument("command", choices=("install",))
    ap.add_argument("--dsn")
    args = ap.parse_args(argv)
    from operating.pg import config
    for name in install(args.dsn or config.dsn()):
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
