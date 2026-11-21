"""The built-in team, org and customer tiles in well under a second: their statements run
over copies of the mail, Teams and meeting rows that only the reader-independent part of
the comms door kept, made once per process for everyone and scoped at read.

Read through ``plx.sql`` as each viewer, the people statements took 3-11 s on live data and
two of them ran past its 10 s limit: the comms door decides, per row, the reader's units,
their hidden ids and the people exclusion inside a security-barrier view, and the planner
cannot push a window or a join through it. The same statements over plain copies of the
rows in the window take a few hundred milliseconds.

How (``Fast.get``): one transaction makes temporary tables under the views' own names
(``search_path`` is ``pg_temp`` alone, so a statement reads nothing else):

* a comms table (``business_comms.SPECS``): its rows of the last ``WINDOW_DAYS`` that the
  door would give ANY reader: the people exclusion's flag clear, Freightcom decided at
  build (``ai_ok``), a version the exclusion has decided (``ver <= covered_ver``), no object
  in ``ontology.people_excluded_objects`` now, a handle (``business_comms.view_ddl`` less
  the reader's own units and hidden ids);
* any other view a statement reads: its rows as built.

then runs every fast built-in's own statement (the same text the live path sends to
``plx.sql``) and keeps the results, with the exclusion's stamp they were made under, for
``insights_cache.fresh_seconds()``. A result is served only while the exclusion's stamp is
the one it was made under (an unreadable stamp serves nothing: the live path reads).

Who is served from here (``serves``; everyone else reads through their own door):

* a people view of scope "self" (rows of one person each): every reader; the Team rule
  (``insights.visible_rows``) then keeps their own row only, unless they see everyone. A
  person's own row counts their own mail, chats and meetings;
* a people view of scope "org": the readers the Team rule shows everyone (the Team page
  shows them every person's counts alike);
* a company-wide view (no person column): only a reader with full read over every source
  and every mailbox (``feedback.owner_open``), whose door holds every row anyway.

A people view may read only the comms tables and ``PEOPLE_VIEWS``; one that reads anything
else is never fast (its rows would carry numbers of records its reader may not read).

Process memory only; nothing is written. Evidence: isolated-fixture
(``operator_api/tests/test_insights.py`` Fast) and the live measure in MASTER."""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable, Mapping, Optional, Sequence

WINDOW_DAYS = 98          # the comms rows copied: 14 weeks (the longest fast window + a week)
STATEMENT_SECONDS = 60    # one copy's statement
TILE_SECONDS = 15         # one tile's statement over the copies
PEOPLE_VIEWS = ("staff",)  # the views a people statement may read beside the comms tables
TICK_SECONDS = 10.0       # how often the copies are checked against the exclusion's stamp
WARM_SECONDS = 3600.0     # kept ready this long after a fast tile was last opened
COPY_MOST = 50_000        # rows of another view copied whole; a bigger one is read live
EXCLUSION_STATE = "ontology.people_excluded_state"
EXCLUDED = "ontology.people_excluded_objects"
_INTERVAL = re.compile(r"interval\s+'(\d+)\s+(day|days|week|weeks)'", re.I)


class Unserved(Exception):
    """No fast answer now (no stamp, the copy failed): the live path reads."""


# the views made over the comms tables (``operating.pg.comms_findings``): each finding with the
# first copy of its message the reader's door shows, made here over the copies of those tables
DERIVED = {"comms_findings": ("emails", "teams_messages")}


def views_read(sql: str) -> list[str]:
    """The business views ``sql`` names (as ``plx.sql`` lists them), the views made over
    them (``DERIVED``) too."""
    from operating.pg import business_views as bv
    return sorted(v for v in (*bv.VIEWS, *DERIVED) if re.search(rf"\b{v}\b", sql, re.I))


def window_days(sql: str) -> int:
    """The longest look-back ``sql`` states (``interval 'N days|weeks'``, a week added for
    a ``date_trunc('week', ...)`` start), in days; 0 when it states none."""
    most = 0
    for n, unit in _INTERVAL.findall(sql):
        most = max(most, int(n) * (7 if unit.lower().startswith("week") else 1))
    if most and re.search(r"date_trunc\(\s*'week'", sql, re.I):
        most += 7
    return most


def eligible(spec: Any) -> bool:
    """A built-in whose statement may be answered here: declared ``fast``, its look-back
    inside the copied window when it reads a comms table, and a people view reading only
    the comms tables and ``PEOPLE_VIEWS``."""
    from operating.pg import business_comms as bc
    if not getattr(spec, "fast", False) or getattr(spec, "saved", False):
        return False
    views = views_read(spec.sql)
    comms = [v for v in views if v in bc.SPECS or v in DERIVED]
    if comms and not 0 < window_days(spec.sql) <= WINDOW_DAYS:
        return False
    if spec.person and any(v not in bc.SPECS and v not in PEOPLE_VIEWS for v in views):
        return False
    if spec.person and any(v in DERIVED for v in views):
        return False   # findings carry message text: a company-wide view only
    return True


def serves(spec: Any, reader: Any, everyone: bool) -> bool:
    """Whether ``reader`` is answered here for ``spec`` (see the module text)."""
    from .feedback import owner_open
    if not eligible(spec):
        return False
    if spec.person:
        return spec.scope == "self" or everyone
    return owner_open(getattr(reader, "session", None))


def exclusion_stamp(pg: Any) -> Optional[tuple]:
    """The people exclusion as it stands (which set, built, updated, the version it has
    decided up to); None when there is none (nothing is served)."""
    row = pg.execute(f"SELECT exclusion, built_at, coalesce(updated_at, ''), covered_ver "
                     f"FROM {EXCLUSION_STATE}").fetchall()
    return tuple(row[0]) if len(row) == 1 and row[0][3] is not None else None


_DIGEST_KEPT: list[tuple[str, str]] = []
_DIGEST_LOCK = threading.Lock()


def hidden_digest(text: str) -> str:
    """``text``'s short digest, kept for the very same text object (``hidden_json`` gives the
    same object while the set is the same: ``document_marks._own_json``), so a long list is
    hashed once per change, not once per tile (11-21: ~20 ms of a tile's time)."""
    with _DIGEST_LOCK:
        if _DIGEST_KEPT and _DIGEST_KEPT[0][0] is text:
            return _DIGEST_KEPT[0][1]
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    with _DIGEST_LOCK:
        _DIGEST_KEPT[:] = [(text, digest)]
    return digest


def hidden_json() -> str:
    """The ids hidden from every reader alike, as the comms door's ``plx.reader_hidden``
    holds them (``document_marks.params`` ``dm_own``: the PLX-issued documents). A full
    reader holds every marking set, so no document is withheld from them beyond these; any
    other reader is not served here for the findings (``serves``)."""
    from operating.search import document_marks
    return document_marks._own_json(document_marks._own_issued())


def _rule() -> str:
    """The reader-independent half of the comms door. Its unit and marks half (a row's
    audience and record markings) is not applied here: a person's own row on a self view
    counts every message of theirs, a finance-marked one too. Only counts are shown, and only
    the person's own (``serves``: self views); never another person's row or any text."""
    return ("t.excluded IS FALSE AND t.ai_ok AND t.handle IS NOT NULL AND t.ver <= %s AND "
            "t.object_ids[1] NOT IN (SELECT json_array_elements_text(%s::json)) AND "
            f"NOT EXISTS (SELECT 1 FROM {EXCLUDED} p WHERE p.object_id = t.object_ids[1])")


# the plain btree indexes of a table, as their column names in order (expression and
# partial indexes and other access methods are left out)
_INDEXES = ("SELECT array_agg(a.attname::text ORDER BY k.n) FROM pg_index i JOIN pg_class c ON "
            "c.oid = i.indexrelid JOIN pg_am am ON am.oid = c.relam AND am.amname = 'btree' "
            "CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY k(attnum, n) JOIN "
            "pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum WHERE "
            "i.indrelid = %s::regclass AND i.indexprs IS NULL AND i.indpred IS NULL AND NOT "
            "(0 = ANY(i.indkey::int2[])) GROUP BY i.indexrelid")


def _derived_ddl(name: str, schema: str, parts: Sequence[str]) -> str:
    """The temporary table ``name`` (``comms_findings``): the guarded view's own statement
    (``operating.pg.comms_findings.view_ddl``) over the copies of the comms tables made here
    (a finding with the first copy of its message that the copies hold, by handle), the rows
    of the window only."""
    from operating.pg import comms_findings as cf
    ddl = cf.view_ddl(schema, "pg_temp", parts)
    body = ddl.split(" AS ", 1)[1]   # SELECT ... (the view's columns are the table's)
    return (f"CREATE TEMP TABLE {name} ON COMMIT DROP AS {body} WHERE f.said_at >= now() - "
            f"make_interval(days => %s)")


def _error(exc: Exception) -> dict[str, str]:
    """A statement's failure as ``plx.sql`` states it (code, first line)."""
    import psycopg
    first = (str(exc).strip().splitlines() or [type(exc).__name__])[0][:300]
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return {"code": "timeout", "message": f"the query ran past {TILE_SECONDS} s"}
    if isinstance(exc, psycopg.errors.UndefinedTable):
        return {"code": "unknown_view", "message": first}
    if isinstance(exc, psycopg.errors.UndefinedColumn):
        return {"code": "sql_error", "message": first}
    return {"code": "query_failed", "message": first}


def compute(pg: Any, specs: Sequence[Any], covered: int) -> dict[str, dict[str, Any]]:
    """Every spec's answer (``{"got": plx.sql-shaped result, "read_at"}`` or
    ``{"error": {...}}``) from the copies made in one transaction on ``pg``."""
    from operating.ontology_tools_sql import MAX_ROWS, _cell
    from operating.pg import business_comms as bc
    from operating.pg import business_views as bv
    schema = bv.SCHEMA
    out: dict[str, dict[str, Any]] = {}
    made: list[str] = []
    with pg.transaction():
        pg.execute(f"SET LOCAL statement_timeout = {STATEMENT_SECONDS * 1000}")
        derived = {v for s in specs for v in views_read(s.sql) if v in DERIVED}
        names = sorted({v for s in specs for v in views_read(s.sql)}
                       | {d for v in derived for d in DERIVED[v]})
        have = {r[0] for r in pg.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (schema,))}
        state = {r[0]: (r[1], r[2]) for r in pg.execute(
            f"SELECT view, built_at, rows FROM {schema}.state WHERE view = ANY(%s)",
            (names,)).fetchall()}
        built = {v: b for v, (b, _n) in state.items()}
        big = {v for v in names if v not in bc.SPECS and v not in DERIVED and v in have
               and (state.get(v, (None, None))[1] is None or state[v][1] > COPY_MOST)}
        for name in names:
            if name in DERIVED:
                continue   # made after the tables it reads
            if name not in have or name in big:
                continue   # missing: its statements fail "does not exist" below, as live
            cols = [r[0] for r in pg.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = %s "
                "AND table_name = %s ORDER BY ordinal_position", (schema, name))]
            # the view's columns and the handle its guarded view shows beside them
            shown = [c.name for c in bv.VIEWS[name].cols if c.name in cols] + (
                ["handle"] if "handle" in cols else [])
            listed = ", ".join(f't."{c}"' for c in shown)
            # a copy left by an outer transaction on this connection is never read again
            pg.execute(f"DROP TABLE IF EXISTS pg_temp.{name}")
            if name in bc.SPECS:
                when = bc.SPECS[name].when
                pg.execute(f"CREATE TEMP TABLE {name} ON COMMIT DROP AS SELECT {listed} FROM "
                           f"{schema}.{name} t WHERE t.{when} >= now() - make_interval("
                           f"days => %s) AND {_rule()}", (WINDOW_DAYS, covered, hidden_json()))
            else:
                pg.execute(f"CREATE TEMP TABLE {name} ON COMMIT DROP AS SELECT {listed} FROM "
                           f"{schema}.{name} t")
            made.append(name)
            # the source's plain btree indexes over the copied columns (a statement's joins
            # and lookups use them as they do on the source)
            for (keys,) in pg.execute(_INDEXES, (f"{schema}.{name}",)).fetchall():
                if keys and all(k in shown for k in keys):
                    pg.execute(f"CREATE INDEX ON pg_temp.{name} ("
                               + ", ".join(f'"{k}"' for k in keys) + ")")
            pg.execute(f"ANALYZE pg_temp.{name}")
        for name in sorted(derived):
            parts = [t for t in DERIVED[name] if t in made]
            if len(parts) != len(DERIVED[name]) or name not in have:
                big.add(name)   # a table it reads (or its own) is missing: read live
                continue
            pg.execute(f"DROP TABLE IF EXISTS pg_temp.{name}")
            # (the findings table is read by its own name: the search path still holds it)
            pg.execute(_derived_ddl(name, schema, parts), (WINDOW_DAYS,))
            made.append(name)
            built[name] = min((built[t] for t in parts if built.get(t)), default=None)
            pg.execute(f"ANALYZE pg_temp.{name}")
        pg.execute("SET LOCAL search_path = pg_temp")
        pg.execute(f"SET LOCAL statement_timeout = {TILE_SECONDS * 1000}")
        for spec in specs:
            if big & set(views_read(spec.sql)):
                continue   # not made here: read live
            read_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            try:
                with pg.transaction():   # a savepoint: one failing statement fails alone
                    cur = pg.execute(spec.sql)
                    cols = [d.name for d in cur.description or ()]
                    rows = cur.fetchmany(MAX_ROWS + 1)
            except Exception as exc:  # noqa: BLE001 - said as plx.sql says it
                out[spec.id] = {"error": _error(exc)}
                continue
            views = views_read(spec.sql)
            stamps = [built[v] for v in views if built.get(v)]
            out[spec.id] = {"read_at": read_at, "got": {
                "columns": cols, "truncated": len(rows) > MAX_ROWS, "views": views,
                "as_of": min(stamps) if stamps else None,
                "rows": [{"cells": [_cell(v)[0] for v in r]} for r in rows[:MAX_ROWS]]}}
        for name in made:   # gone with the transaction, and now under an outer one too
            pg.execute(f"DROP TABLE IF EXISTS pg_temp.{name}")
    return out


class Fast:
    """The fast answers of this process: made at most once per ``fresh`` seconds and per
    exclusion stamp, one making at a time (others wait for it)."""

    def __init__(self, connect: Callable[[], Any], specs: Callable[[], Sequence[Any]], *,
                 fresh: Optional[float] = None, clock: Callable[[], float] = time.monotonic
                 ) -> None:
        from .insights_cache import fresh_seconds
        self.connect, self.specs, self.clock = connect, specs, clock
        self.fresh = fresh_seconds() if fresh is None else fresh
        self._made: Optional[tuple[tuple, float, dict[str, dict[str, Any]]]] = None
        self._flight: Optional[Future] = None
        self._lock = threading.Lock()
        self._wanted_until = 0.0
        self._thread: Optional[threading.Thread] = None
        from .shared_read import SharedRead
        # the tiles of one visit ask at once: one read, started after each asked
        self._exclusion_read = SharedRead(self._read_exclusion)

    def _read_exclusion(self) -> Any:
        with self.connect() as pg:
            return exclusion_stamp(pg)

    def stamp(self) -> tuple:
        """The exclusion's stamp now; ``Unserved`` when it cannot be read or there is none."""
        try:
            stamp = self._exclusion_read()
        except Exception as exc:  # noqa: BLE001 - undecidable: the live path reads
            raise Unserved(type(exc).__name__) from None
        if stamp is None:
            raise Unserved("no people exclusion")
        # the hidden ids the copies leave out: made again when they change
        return stamp + (hidden_digest(hidden_json()),)

    def get(self, spec: Any) -> dict[str, Any]:
        """``spec``'s answer (``{"got", "read_at"}``); its statement's failure as
        ``insights.why_unread`` says it; ``Unserved`` when nothing can be served now."""
        from .insights import why_unread
        self._wanted_until = self.clock() + WARM_SECONDS
        made = self._current(self.stamp())
        got = made.get(spec.id)
        if got is None:
            raise Unserved("not made")
        if "error" in got:
            raise why_unread(got["error"], spec.sql)
        return got

    def _current(self, stamp: tuple, again: bool = True, within: Optional[float] = None
                 ) -> dict[str, dict[str, Any]]:
        within = self.fresh if within is None else within
        with self._lock:
            made = self._made
            if made is not None and made[0] == stamp and self.clock() - made[1] < within:
                return made[2]
            flying, mine = self._flight, False
            if flying is None:
                flying = self._flight = Future()
                mine = True
        if not mine:
            got = flying.result()
            if got[0] != stamp:   # made under an older stamp: once more under ours
                if not again:
                    raise Unserved("the exclusion moved")
                return self._current(stamp, again=False)
            return got[2]
        try:
            specs = [s for s in self.specs() if eligible(s)]
            with self.connect() as pg:
                # the copies apply the set as it stands while they are made (at least as
                # grown as ``stamp``'s) and only versions ``stamp`` decided: valid for it
                answers = compute(pg, specs, int(stamp[3]))
            result = (stamp, self.clock(), answers)
        except BaseException as exc:
            with self._lock:
                self._flight = None
            flying.set_exception(exc if isinstance(exc, Unserved) else Unserved(repr(exc)))
            if isinstance(exc, Unserved):
                raise
            raise Unserved(type(exc).__name__) from None
        with self._lock:
            self._made, self._flight = result, None
        flying.set_result(result)
        return answers

    def prewarm(self) -> bool:
        """Make the shared answers now and keep them ready for ``WARM_SECONDS`` (the start,
        before the port opens: the first Insights visit then reads them as made, not the
        5-8 s of making them). False when nothing could be made."""
        self._wanted_until = self.clock() + WARM_SECONDS
        try:
            self._current(self.stamp())
        except Unserved:
            return False
        return True

    def tick(self) -> None:
        """Made again ahead of the next open while anyone opened a fast tile in the last
        ``WARM_SECONDS``: when the exclusion moved or the answers near ``fresh`` (reads
        meanwhile are served the answers still current)."""
        if self.clock() < self._wanted_until:
            self._current(self.stamp(), within=self.fresh * 0.8)

    def start(self) -> None:
        """The keep-ready loop on a daemon thread (once)."""
        if self._thread is not None:
            return

        def loop() -> None:
            while True:
                time.sleep(TICK_SECONDS)
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 - next tick; a read makes its own
                    pass
        self._thread = threading.Thread(target=loop, name="insights-fast", daemon=True)
        self._thread.start()
