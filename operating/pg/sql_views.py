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
scratch schema ``PLX_BIZ_SCHEMA``). It runs on the live store while people ask: each step
is a transaction of its own that waits at most ``LOCK_TIMEOUT_MS`` for a lock and is tried
again (``short``), a view whose definition is unchanged (its ``COMMENT`` holds the hash of
its DDL, ``put``) is not touched, and the door's exclusion follow-up (``door.follow``,
seconds per table) runs outside any view's lock. A reader therefore waits at most about
``LOCK_TIMEOUT_MS`` behind it, never the whole install (one transaction over every view
held each view's ACCESS EXCLUSIVE lock until the last was made: 245 s of blocked reads)."""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
from typing import Any, Callable, Sequence

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
# install's lock budget per step: a reader queued behind a step's lock request waits at
# most this long; the step gives way (its transaction rolled back) and is tried again
LOCK_TIMEOUT_MS = 2000
LOCK_TRIES = 90
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


def short(write: Any, step: Callable[[Any], Any], what: str, *,
          lock_timeout_ms: int | None = None, tries: int | None = None) -> Any:
    """``step(write)`` in a transaction of its own (``write`` in autocommit) that waits at
    most ``lock_timeout_ms`` for any lock; on a lock timeout (or a deadlock with a refresh)
    it is rolled back and tried again after a short pause, so a long reader delays only this step and the readers
    queued behind it wait no longer than the timeout. RuntimeError after ``tries``."""
    import psycopg
    wait_ms = LOCK_TIMEOUT_MS if lock_timeout_ms is None else int(lock_timeout_ms)
    n = LOCK_TRIES if tries is None else int(tries)
    for attempt in range(n):
        try:
            with write.transaction():
                write.execute(f"SET LOCAL lock_timeout = {wait_ms}")
                return step(write)
        except (psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected):
            time.sleep(min(0.25 * (attempt + 1), 3.0))
    raise RuntimeError(f"{what}: a lock stayed held through {n} tries of {wait_ms} ms")


def _refs(write: Any, name: str) -> list[str]:
    """The relations view ``name`` reads, by their names now (a table renamed away by a
    rebuild's swap reads under its new name)."""
    return [r[0] for r in write.execute(
        "SELECT DISTINCT n.nspname || '.' || c.relname FROM pg_rewrite r JOIN pg_depend d "
        "ON d.classid = 'pg_rewrite'::regclass AND d.objid = r.oid AND d.refclassid = "
        "'pg_class'::regclass AND d.refobjid <> r.ev_class JOIN pg_class c ON c.oid = "
        "d.refobjid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE r.ev_class = "
        "to_regclass(%s) ORDER BY 1", (name,)).fetchall()]


def tag(ddl: str, refs: Sequence[str]) -> str:
    """The comment ``put`` keeps on a view: the hash of the DDL it was made from and of
    the names of the relations it read when made."""
    text = ddl + "\n" + "\n".join(sorted(refs))
    return "plx-ddl:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def put(write: Any, name: str, ddl: str, *,
        before: Callable[[], Any] | None = None) -> bool:
    """View ``name`` made from ``ddl`` (``CREATE VIEW name ...``) unless it already was:
    its comment is the tag of ``ddl`` and of the relations it reads under their names now
    (a table swapped since, the old one renamed, changes them). An unchanged view takes no
    lock. Else it is dropped and made again (CREATE OR REPLACE cannot drop or retype a
    column; in the caller's transaction no reader sees it missing) and tagged; ``before``
    is called first (it drops a view that reads this one, which would block the DROP).
    True when it was made."""
    have = write.execute("SELECT obj_description(to_regclass(%s), 'pg_class')",
                         (name,)).fetchone()[0]
    if have is not None and have == tag(ddl, _refs(write, name)):
        return False
    if before is not None:
        before()
    write.execute(f"DROP VIEW IF EXISTS {name}")
    write.execute(ddl)
    write.execute(f"COMMENT ON VIEW {name} IS {_literal(tag(ddl, _refs(write, name)))}")
    return True


def drop(write: Any, name: str, schema: str | None = None) -> None:
    from operating.pg import comms_findings
    if name in comms_findings.SOURCES:
        # the findings view reads the communications views: dropped first, made again by
        # ``create`` (it would block this DROP)
        comms_findings.drop_view(write, schema)
    write.execute(f"DROP VIEW IF EXISTS {guarded_schema(schema)}.{name}")
    if write.execute("SELECT to_regnamespace(%s)", (person_schema(schema),)
                     ).fetchone()[0] is not None:
        write.execute(f"DROP VIEW IF EXISTS {person_schema(schema)}.{name}")
    write.execute(f"DROP VIEW IF EXISTS {page_schema(schema)}.{name}")


def create(write: Any, v: bv.View, schema: str | None = None, *, follow: bool = True) -> None:
    """The guarded view of ``v`` (its schema made first when missing) and its grant, in
    the caller's transaction (a build's swap): made, then the door follows the people
    exclusion and every reader's door is decided again. ``follow`` False: the caller decided
    the table's rows against the exclusion already (a swap that did it on the staged table,
    ``door.exclude(staged=)``, so its locks never wait on that scan)."""
    drop(write, v.name, schema)
    make(write, v, schema, follow=follow)


def make(write: Any, v: bv.View, schema: str | None = None, *,
         follow: bool = False) -> bool | None:
    """``v``'s views and grants. ``follow``: the door then follows the people exclusion
    (``door.follow``) before the stamp is bumped. None when ``v`` gets no view (plx.sql off,
    or an old-shape table: fail closed); else whether a view was (re)made, the readers'
    doors then decided again (``door._bump``)."""
    dst = guarded_schema(schema)
    if write.execute("SELECT to_regnamespace(%s)", (dst,)).fetchone()[0] is None:
        return None   # migration 0042 (or ``install``) not applied: plx.sql is off
    from operating.pg import door
    src = schema or bv.SCHEMA
    made = False
    if v.fill:   # a communications table: its own guarded view rule (``business_comms``)
        from operating.pg import business_comms, comms_findings
        # the findings view reads the guarded mail and Teams views: dropped before one of
        # them is made again (it would block the DROP) and made again after, in this same
        # transaction, so no reader sees it missing
        findings = v.name in comms_findings.SOURCES
        remade = put(write, f"{dst}.{v.name}", business_comms.view_ddl(v, src, dst),
                     before=(lambda: comms_findings.drop_view(write, schema))
                     if findings else None)
        made |= remade
        if findings and (remade or write.execute(
                "SELECT to_regclass(%s) IS NULL",
                (f"{dst}.{comms_findings.VIEW}",)).fetchone()[0]):
            _findings(write, schema)
        person = person_schema(schema)
        if write.execute("SELECT to_regnamespace(%s)", (person,)).fetchone()[0] is not None:
            made |= put(write, f"{person}.{v.name}",
                        business_comms.view_ddl(v, src, person, ai=False))
            _grant(write, PERSON_READERS, f"SELECT ON {person}.{v.name}")
        _grant(write, COMMS_READERS, f"SELECT ON {dst}.{v.name}")
        _drop_one(write, f"{page_schema(schema)}.{v.name}")   # never a person-page view
    elif not door.is_v2(write, src, v.name):
        drop(write, v.name, schema)   # an old-shape table: no view (fail closed)
        return None
    else:
        made |= put(write, f"{dst}.{v.name}", door.view_ddl(write, v, src, dst))
        page = page_schema(schema)
        if write.execute("SELECT to_regnamespace(%s)", (page,)).fetchone()[0] is not None:
            # migration 0043 applied: a person's pages read this one (no ai_ok)
            made |= put(write, f"{page}.{v.name}",
                        door.view_ddl(write, v, src, page, ai_only=False))
            _grant(write, (PAGE_ROLE,), f"SELECT ON {page}.{v.name}")
    if follow:
        # plx.sql reads these views beside the door's: the door follows the exclusion set
        door.follow(write, src, v.name)
    if made or follow:
        # the table, its lineage and its class counts may all be new (a rebuild's swap):
        # every reader's door (plx.sql's cache, keyed on the stamp) is decided again, in
        # this same transaction, so no reader keeps a scope computed against the old rows
        door._bump(write, src)
    _grant(write, (ROLE,), f"SELECT ON {dst}.{v.name}")
    return made


def _drop_one(write: Any, name: str) -> None:
    if write.execute("SELECT to_regclass(%s)", (name,)).fetchone()[0] is not None:
        write.execute(f"DROP VIEW IF EXISTS {name}")


def _findings(write: Any, schema: str | None) -> None:
    """The findings view (``comms_findings``) over the communications views made now. Its
    failure never fails their swap: without it plx.sql reads no finding (fail closed)."""
    from operating.pg import comms_findings
    try:
        with write.transaction():
            comms_findings.create_view(write, schema)
    except Exception as exc:  # noqa: BLE001 - no findings view; the swap goes on
        print(f"comms findings view not made: {type(exc).__name__}", file=sys.stderr)


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


class InstallFailed(RuntimeError):
    """Steps of ``install`` that failed (each named with its error); every other step ran."""


def install(dsn: str, schema: str | None = None) -> list[str]:
    """The schema, the role and a guarded view over every business table built now, on
    the live store: every step a short transaction of its own (``short``), unchanged views
    untouched (``put``), the exclusion follow-up of each table outside its view's lock.
    A step that fails rolls back alone (its view stays as it was) and the others still
    run; then ``InstallFailed`` names every failed step (a non-zero exit: never a
    half-made install reported as done)."""
    import psycopg
    from operating.pg import door, generic_views
    src = schema or bv.SCHEMA
    made: list[str] = []
    failed: list[str] = []

    def step(write: Any, work: Callable[[Any], Any], what: str) -> Any:
        try:
            return short(write, work, what)
        except (psycopg.Error, RuntimeError) as exc:
            failed.append(f"{what}: {type(exc).__name__}: {str(exc).strip()[:200]}")
            return None

    with psycopg.connect(dsn, autocommit=True) as write:
        short(write, lambda w: ensure(w, schema), "ensure")
        dst = guarded_schema(schema)
        # when each table was built, never how many rows it holds (a count over rows
        # the reader may not read)
        for where, role in ((dst, ROLE), (page_schema(schema), PAGE_ROLE)):
            def state(w: Any, where: str = where, role: str = role) -> None:
                put(w, f"{where}.view_state",
                    f"CREATE VIEW {where}.view_state AS SELECT view, built_at FROM {src}.state")
                _grant(w, (role,), f"SELECT ON {where}.view_state")
            step(write, state, f"{where}.view_state")
        for name, v in bv.VIEWS.items():
            if write.execute("SELECT to_regclass(%s)", (f"{src}.{name}",)
                             ).fetchone()[0] is None:
                continue
            if (v.fill or door.is_v2(write, src, name)) and write.execute(
                    "SELECT to_regnamespace(%s)", (dst,)).fetchone()[0] is not None:
                # the exclusion follow-up first, in its own transaction: it deletes rows
                # (seconds over the lineage) and needs no lock a reader of the view holds
                step(write, lambda w, name=name: door.follow(w, src, name),
                     f"{src}.{name} follow")
            if step(write, lambda w, v=v: make(w, v, schema), f"{dst}.{name}") is not None:
                made.append(f"{dst}.{name}")
        # the generic door (``generic_views``): the catalog, and records/record_links
        # once ``generic_records`` built them
        made += generic_views.make_views(write, schema,
                                         step=lambda f, what: step(write, f, what))
    if failed:
        raise InstallFailed("; ".join(failed))
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
