"""``plx.sql``: one read-only SQL ``SELECT`` per call over the reader-scoped business views
(``<biz schema>_sql.<view>``, ``operating.pg.sql_views``), every row of its result kept (``operating.result_rows``: in
memory up to ``HELD_ROWS``, past that on disk) and shown a page at a time, so an agent can look at the data, refine and check its own totals the way
an analyst at a SQL prompt does. ``{"describe": true}`` lists the views and their columns in
plain words; ``{"describe": "<view>"}`` adds a few rows and the commonest values of its text
columns, read as the reader.

Permissions are decided inside Postgres, never by this module: the statement runs in a
READ ONLY transaction as the NOLOGIN role ``plx_sql_reader`` (``SET LOCAL ROLE``), which may
read only the guarded views (DOOR v2, ``operating.pg.door``, the canonical classes
contract): each view keeps a row only when the reader holds its access class (every
audience key and marking set the row was read from; a row read from a document: none of
its own or read records is one the reader may not open) and no text of it names Freightcom
(the owner's no-AI rule); a cell whose marking or ``needs`` the reader lacks reads NULL, is
named in the row's ``withheld`` and counted (the answer is partial). The reader's scope
(``door.reader_scope``: its held classes as ranges, hidden handles and per-column
allow-lists, once per grant and door stamp) is decided in the call's own REPEATABLE READ
transaction (the door's stamp and the people exclusion read as its first statement) and set
as transaction-local settings with the role in one statement; a schema without the door, or
whose door is behind the people exclusion, is refused (``not_ready``); a refusal that only
lasts while a writer swaps or re-decides the tables (``Busy``) is asked again inside the
call until it ends (a psql session would wait on the lock); after the read the
tool checks the role and every setting are still its own and refuses the result otherwise.

The text is checked before it is sent (fail closed): one statement starting SELECT or WITH;
no ``;`` (one trailing is dropped), comment, dollar or escape string, parameter, or
statement keyword (DDL, DML, COPY, SET, transaction control, locking ``FOR UPDATE``);
no ``pg_*`` or ``information_schema`` name and no schema but the views'; every function
called is one of ``FUNCTIONS`` (plain SQL arithmetic, text, date, aggregate, window and
full-text functions: no ``set_config``, ``current_setting``, ``query_to_xml``, ``dblink``,
file or large-object function). The plan's cost is checked with EXPLAIN first
(``MAX_COST``); ``statement_timeout`` is none; one answer shows ``MAX_BYTES`` of text (the
rest is read with ``page``; ``truncated`` says so).

The answer names each row ``sql://<result id>#row=<n>`` and the result ``sql://<result
id>``: the query, the reader and the views it read are kept (``RESULTS``) so the answer
gate can bind a stated figure to the cell that holds it."""
from __future__ import annotations

import concurrent.futures as cf
import contextvars
import datetime as dt
import hashlib
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from operating.ontology_tools_identity import ToolError
from operating import insight_statements as ins
from operating import read_ceiling
from operating import result_profile
from operating.ontology_tools_sql_rows import (  # noqa: F401  (re-exported)
    MAX_BYTES, SHOWN_ROWS, CELL_PAGE, cell_text, _piece, RESULTS_KEPT, RESULTS_BYTES, CELL_PATTERN,
    _CODE, _cell, _Results, RESULTS, _actor_key, _shown, _narrowed, _profiled)
from operating.pg import business_views as bv
from operating.pg import sql_views
from operating.ontology_tools_sql_check import (  # noqa: F401  (re-exported)
    FUNCTIONS, STATEMENT_WORDS, SYNTAX, _LOCKING, _WORD, _bad, _column_list,
    _tokens, check)

TOOL_NAME = "plx.sql"
TOOL_NAMES = (TOOL_NAME,)
# internal (never in the MCP catalog): plx.search / plx.open's mail and Teams path
COMMS_SEARCH = "plx.sql_comms_search"
COMMS_OPEN = "plx.sql_comms_open"
RECORDS_SEARCH = "plx.sql_records_search"   # plx.search's records leg (ontology_tools_search)
from operating.result_rows import HELD_ROWS, DiskRefused, RowStore  # noqa: E402  (every row is kept: no cap)
# the rows' text, measured as plx.sql writes it: the result as the client shows it stays
# within ``mcp_server_evidence.CLIENT_SHOWN_CHARS`` (48,000) with its columns and notes; the
# rows past it are cut (``truncated``) and read with another statement (OFFSET, narrower)
# a comms search whose every-word match finds fewer rows than this (and than its limit)
# fills its page with rows holding some of the words and rows close in meaning
# (``_comms_widened``)
WIDEN_BELOW = 3
# any other first page with query words also shows up to this many rows close in meaning
# that the word match did not list ("mouth rinse": 30 label files hold both words, the
# "Oral Rinse" mail is close in meaning)
MEANING_EXTRA = 5
# a full reader's first page with query words also shows up to this many mails whose
# attachment's own text holds every word that the word match did not list
# (``_comms_attached``: "QuikPAK orders appearing" was only in a PDF's text)
ATTACH_EXTRA = 5
# the attachment passes run beside their word searches (``_comms_search``)
_ATTACH_POOL = cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="plx-attach")
# the meaning ranking's best objects read for it
MEANING_K = 40
# the ranking's rows are read ahead for this many of its objects (the gate keeps MEANING_K)
MEANING_READ_K = 80
# a plx.search hit list's most (its contract's maxItems)
HITS_MAX = 50
# Ask's reads have no time limit (no cut-offs): a statement,
# a search leg or a pass beside it runs to its end (statement_timeout 0). What stays is
# Postgres's own lock protection (``lock_ms``), the connection's own connect timeout, and
# the pool's queue for a free connection (a call waits its turn): none ends a read that is
# running. Where a heavy read runs (the standby, a Spark replica) is how the live app is
# protected, never a limit.
# A third waits for a slot (holding no connection), however long, and then runs
FULL_COSTLY_SLOTS = 2
FULL_COSTLY_LOCK = 0x706C7873
FULL_COSTLY_POLL_SECONDS = 0.5
MAX_COST = 5e7
SAMPLE_VALUES = 6
# a ``Busy`` refusal (a view swapped or re-created, the door re-decided after the people
# exclusion moved: Postgres's own lock protection, kept) is asked again every
# BUSY_POLL_SECONDS for up to this long in one call, as a psql session waits on a lock
BUSY_WAIT_SECONDS = 20.0
BUSY_POLL_SECONDS = 2.0

_CONTRACT = json.loads((Path(__file__).with_name("ontology_tool_contracts.json"))
                       .read_text(encoding="utf-8"))["tools"][TOOL_NAME]
DESCRIPTION: str = _CONTRACT["description"]
INPUT_SCHEMA: dict[str, Any] = _CONTRACT["input"]
TOOL: dict[str, Any] = {"name": TOOL_NAME, "description": DESCRIPTION,
                        "inputSchema": INPUT_SCHEMA, "annotations": {"readOnlyHint": True}}

def _ceiling() -> "dt.datetime | None":
    """The replay's read ceiling (``read_ceiling``); unreadable: every read refused."""
    try:
        return read_ceiling.ceiling()
    except read_ceiling.CeilingInvalid as exc:
        raise ToolError("not_ready", str(exc)) from None


class Busy(ToolError):
    """``not_ready`` that ends by itself within seconds (the tables are being swapped or
    their door re-decided): ``SqlTool._scoped`` asks again before answering it."""

    def __init__(self, message: str) -> None:
        super().__init__("not_ready", message, "the tables were being swapped; send the "
                         "query again once, then answer from what you have")


# plx.sql's own connections, one per call running at once (``POOL_SIZE`` per database):
# the session's parallel calls never share one, and a call stopped at its time limit
# holds only its own until the statement timeout ends it (on the host's shared
# connection the next statement failed "another command is already in progress").
POOL_SIZE = 4
_POOLS: dict[str, Any] = {}
_POOLS_LOCK = threading.Lock()


def _pool(dsn: str) -> Any:
    from operating.pg.pool import Pool

    def configure(pg: Any) -> None:
        pg.autocommit = True

    with _POOLS_LOCK:
        pool = _POOLS.get(dsn)
        if pool is None:
            pool = _POOLS[dsn] = Pool(dsn, max_size=POOL_SIZE, timeout=None,
                                      configure=configure, connect_timeout=5,
                                      statement_timeout_ms=0)
        return pool


_ROUTE = threading.local()   # how this thread's last pooled read was routed (``_lent``)


def last_route() -> tuple[str, str | None]:
    """("standby" | "primary", why the primary) of this thread's last read of the Ask's pool."""
    return getattr(_ROUTE, "last", ("primary", None))


class _lent:
    """A connection of plx.sql's own pool for one call: the standby's when it answers as a
    replica (``standby_route``: heavy reads are protected by where they run, never by a
    limit), else the primary's (the store's DSN), with why. A call waits for a free
    connection however long it takes. A store with no DSN: its own connection, held under its
    lock for the call."""

    def __init__(self, conn: Any) -> None:
        self.conn, self._cm = conn, None

    def __enter__(self) -> Any:
        dsn = getattr(self.conn, "dsn", None)
        if isinstance(dsn, str) and dsn:
            from operating import standby_route
            target, why = standby_route.route(dsn)
            if why is None:
                try:
                    self._cm = _pool(target).connection()
                    got = self._cm.__enter__()
                    _ROUTE.last = ("standby", None)
                    return got
                except Exception as exc:  # noqa: BLE001 - the primary reads, and says so
                    self._cm = None
                    standby_route.failed(target, type(exc).__name__)
                    why = f"the standby could not be reached ({type(exc).__name__})"
            _ROUTE.last = ("primary", why)
            self._cm = _pool(dsn).connection()
            return self._cm.__enter__()
        _ROUTE.last = ("primary", "no database address for this store")
        self.conn._lock.acquire()
        return self.conn._pg

    def __exit__(self, *exc: Any) -> Any:
        if self._cm is not None:
            return self._cm.__exit__(*exc)
        self.conn._lock.release()
        return None


def psycopg_errors() -> type:
    import psycopg
    return psycopg.Error


def data_as_of(pg: Any) -> dict[str, Any]:
    """Where the read this connection just made came from and as of when: a standby's
    replay time (the newest primary commit it holds), the primary's now, and when the
    standby was meant but not used, why. Read on the connection that read (never another)."""
    from_, why = last_route()
    recovering, replayed, now = pg.execute(
        "SELECT pg_is_in_recovery(), pg_last_xact_replay_timestamp(), now()").fetchone()
    at = replayed if recovering and replayed is not None else now
    out: dict[str, Any] = {"read_from": "standby" if recovering else "primary",
                           "as_of": at.isoformat(timespec="seconds")}
    if not recovering and why:
        out["note"] = f"read on the primary: {why}"
    return out


class SqlTool:
    def __init__(self, base: Any) -> None:
        self.base = base
        self._schemas: frozenset[str] | None = None

    def call(self, name: str, arguments: Mapping[str, Any], actor: Any) -> dict[str, Any]:
        if name != TOOL_NAME:
            return {"error": {"code": "unknown_tool", "message": f"{name} is not plx.sql"}}
        try:
            return self._call(dict(arguments or {}), actor)
        except ToolError as exc:
            error = {"code": exc.code, "message": exc.message[:600]}
            if exc.hint:
                error["hint"] = exc.hint[:600]
            if exc.gap is not None:
                # nothing covers what was asked: said, and kept on the gap list (the read is
                # answered, never refused for it)
                from operating import gap_list
                error["gap"] = gap_list.said(*exc.gap, gap_list.record(*exc.gap))
            return {"error": error}

    def _call(self, args: dict[str, Any], actor: Any) -> dict[str, Any]:
        unknown = sorted(set(args) - set(INPUT_SCHEMA["properties"]))
        if "names" in args:
            if len(args) != 1:
                raise ToolError("invalid_request", "names goes alone: {\"names\": \"<words>\"}")
            # what a name names (``name_finder``): one statement over the reader's own views,
            # run as any other (same door, result id, profile and scope line)
            from operating import name_finder
            try:
                found = self._call({"sql": name_finder.statement(args["names"])}, actor)
            except name_finder.NamesInputError as exc:
                raise ToolError("invalid_request", str(exc)) from None
            if "scope" in found:   # one statement per kind joined: the generic line would read as one filter
                found["scope"] = name_finder.scope(args["names"], found.get("scope"))
            return found
        modes = [k for k in ("sql", "describe", "measure", "page", "chain") if k in args]
        loose = [k for k in ("by", "days", "compare", "period", "against", "filter",
                             "unusual", "forecast", "parameters", "why") if k in args]
        chained = [k for k in ("hops", "party", "type") if k in args]
        if unknown or len(modes) != 1 or (loose and modes != ["measure"]) \
                or (chained and modes != ["chain"]) \
                or (("offset" in args or "column" in args or "from" in args)
                    and modes != ["page"]) or ("from" in args and "column" not in args):
            raise ToolError("invalid_request", "send {\"sql\": \"SELECT ...\"}, "
                            "{\"describe\": true | \"<view>\"}, {\"names\": \"<words>\"} or {\"measure\": true | "
                            "\"<id>\"} or {\"page\": \"sql://<result>\", \"offset\": N} "
                            "(with \"column\": C, \"from\": K for the rest of one long cell) or "
                            "{\"chain\": \"<record handle or number>\", \"hops\": N} (by, "
                            "days, period, against, compare, filter, unusual and forecast go "
                            "with measure only; hops, party and type with chain only; offset "
                            "with page only)")
        if "page" in args:
            return self._page(str(args["page"]), args.get("offset", 0), actor,
                              args.get("column"), args.get("from", 0))
        if "measure" in args:
            return self._measure(args, actor)
        if "chain" in args:
            if _ceiling() is not None:
                raise ToolError("not_ready", "a chain is not read as of a past moment: "
                                "read the links with a statement")
            from operating import ontology_tools_chain
            conn, scope = self._reader(actor)
            return ontology_tools_chain.walk(self, conn, scope, actor, args)
        if "describe" in args:
            if _ceiling() is not None:
                return self._described_only(args["describe"])
            return self._describe(args["describe"], actor)
        conn, scope = self._reader(actor)
        return self._query(conn, scope, self._statement(conn, args["sql"]), actor)

    def _statement(self, conn: Any, sql: Any) -> str:
        """``sql`` as it will run: checked (fail closed) and, under a replay's ceiling,
        bounded to that moment; the one gate of every statement this tool reads."""
        moment = _ceiling()
        guarded = sql_views.guarded_schema()
        sql = check(sql, schemas=self._schema_names(conn),
                    allowed_schema="" if moment is not None else guarded)
        if moment is not None:
            # a replay of a past moment (``read_ceiling``): the views read as then
            sql = read_ceiling.bounded_sql(sql, moment, guarded)
        return sql

    def _page(self, result: str, offset: Any, actor: Any, column: Any = None,
              start: Any = 0) -> dict[str, Any]:
        """``{"page": "sql://<id>", "offset": n}``: the held rows of an earlier result of
        this reader's from row ``n`` (the same cites, the same profile). A result this host
        no longer holds (or another reader's) is refused: run the query again."""
        rid = result.split("//", 1)[-1]
        kept = RESULTS.get(rid)
        if kept is None or kept.get("actor") != _actor_key(actor):
            raise ToolError("not_found", f"{result} is not held here (results are kept for "
                            f"the last {RESULTS_KEPT} reads of this session)",
                            "run the query again")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ToolError("invalid_request", "offset is a row number (0 or more)")
        rows = kept["rows"]
        if offset >= len(rows):
            raise ToolError("invalid_request", f"{result} holds rows 0 to {len(rows) - 1}")
        if column is not None:
            return self._cell_page(result, kept, offset, column, start)
        shown, _cut, cut_cells = _shown(result, kept["columns"], rows, offset,
                                        kept.get("profile"))
        profile = {**kept.get("profile", {}), "shown": len(shown)}
        nxt = offset + len(shown)
        if nxt < len(rows):
            profile["page"] = (f"rows {nxt} to {len(rows) - 1} are held: read them with "
                               f'{{"page": "{result}", "offset": {nxt}}}')
        out: dict[str, Any] = {"result": result,
                               "scope": result_profile.scope_line(
                                   kept.get("sql") or "", kept.get("views") or [], profile,
                                   len(shown)),
                               **({"continue": {"shown": len(shown), "from": offset,
                                                "rows_found": len(rows),
                                                "next": {"page": result, "offset": nxt}}}
                                  if nxt < len(rows) else {}),
                               "columns": kept["columns"], "rows": shown,
                               "row_count": len(shown), "profile": profile,
                               "truncated": nxt < len(rows) or not kept.get("complete", True),
                               "views": kept.get("views", [])}
        if cut_cells:
            out["cells_cut"] = cut_cells
        return out

    def _cell_page(self, result: str, kept: Mapping[str, Any], row: int, column: Any,
                   start: Any) -> dict[str, Any]:
        """``{"page": id, "offset": row, "column": c, "from": k}``: ``CELL_PAGE`` characters of
        one held cell from character ``k`` (an array: its JSON text), so a long cell is read
        whole in pieces; ``next`` is the character to continue from (null: the end)."""
        cols = kept["columns"]
        if isinstance(column, str) and column in cols:
            column = cols.index(column)
        if not isinstance(column, int) or isinstance(column, bool) or not 0 <= column < len(cols):
            raise ToolError("invalid_request", f"column is a column number 0 to {len(cols) - 1} "
                            f"or one of {cols}")
        if not isinstance(start, int) or isinstance(start, bool) or start < 0:
            raise ToolError("invalid_request", "from is a character number (0 or more)")
        value = kept["rows"][row][column]
        if value is None:
            return {"result": result, "cite": f"{result}#row={row}", "column": cols[column],
                    "cell": None, "chars": 0, "from": 0, "next": None}
        text = cell_text(value)
        if start > len(text):
            raise ToolError("invalid_request", f"the cell holds {len(text)} characters")
        env, _ = _piece(text, start)
        end = start + len(env["untrusted_text"])
        return {"result": result, "cite": f"{result}#row={row}", "column": cols[column],
                "cell": env, "chars": len(text), "from": start,
                "next": end if end < len(text) else None}

    def _measure(self, args: dict[str, Any], actor: Any) -> dict[str, Any]:
        """``{"measure": ...}`` (``operating.measures``): true lists the catalogue's
        definitions (no data read); an id runs that entry's own statement exactly as a
        ``{"sql": ...}`` call would (same check, ceiling and door) and returns its
        definition beside the rows."""
        from operating import measures

        if args["measure"] is True:
            return measures.listing(full=self._full(actor))
        if "forecast" in args:
            other = sorted(k for k in ("days", "compare", "period", "against", "unusual")
                           if k in args)
            if other:
                raise ToolError("invalid_request", "a forecast reads its own history (the "
                                "last full periods): no " + ", ".join(other) + " with it")
            made, entry = measures.forecast_statement(args["measure"], args["forecast"],
                                                      args.get("by"), args.get("filter"))
        else:
            made, entry = measures.statement(args["measure"], args.get("by"),
                                             args.get("days"), args.get("compare", False),
                                             args.get("period"), args.get("against"),
                                             args.get("filter"), args.get("unusual", False),
                                             parameters=args.get("parameters"),
                                             why=args.get("why", False))
        if entry.get("scope") == "full" and not self._full(actor):
            # Insights lists such a report only for the readers who see everyone
            raise ToolError("not_readable", f"{entry['id']} is for readers who see everyone")
        conn, scope = self._reader(actor)
        moment = _ceiling()
        guarded = sql_views.guarded_schema()
        sql = check(made, schemas=self._schema_names(conn),
                    allowed_schema="" if moment is not None else guarded)
        if moment is not None:
            sql = read_ceiling.bounded_sql(sql, moment, guarded)
        missing: set[str] = set()
        while True:
            try:
                out = self._query(conn, scope, sql, actor)
                break
            except ToolError as exc:
                # the scan of every measure: a view not served here (not built, being
                # rebuilt) leaves its measures out, named, never the whole scan
                gone = re.search(r'relation "([a-z_]+)" does not exist|^([a-z_]+) is not built '
                                 'yet', exc.message or "")
                name = next((g for g in gone.groups() if g), None) if gone else None
                if entry["id"] != measures.SCAN or exc.code not in ("unknown_view", "not_ready") \
                        or name is None or name in missing or len(missing) >= 5:
                    raise
                missing.add(name)
                made, entry = measures.statement(measures.SCAN, None, args.get("days"),
                                                 unusual=True, without=frozenset(missing))
                sql = check(made, schemas=self._schema_names(conn),
                            allowed_schema="" if moment is not None else guarded)
                if moment is not None:
                    sql = read_ceiling.bounded_sql(sql, moment, guarded)
        if "error" not in out:
            if "forecast" in entry:
                return self._forecasted(out, entry)
            out["measure"] = entry
            from operating.measure_exclusions import excluded
            out["excluded"] = excluded(self, args, actor, conn, scope, guarded, moment)
        return out

    def _forecasted(self, out: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
        """A forecast's history result with its forecast rows (``measures.forecasted``)
        under the same result id: a chart of it draws the history and the forecast; the
        statement kept with it reads the history only."""
        from operating import measures
        rid = out["result"].split("//", 1)[1]
        kept = RESULTS.get(rid)
        if kept is None or out.get("truncated"):
            raise ToolError("invalid_request", "the history is longer than one result: "
                            "forecast by month, or filter to fewer parts")
        cols, rows, cite = measures.forecasted(kept["columns"], list(kept["rows"]), entry)
        RESULTS.put(rid, {**kept, "columns": cols, "rows": rows})
        out["columns"] = cols
        out["rows"] = [{"cite": f"{out['result']}#row={n}", "cells": [_cell(v)[0] for v in r]}
                       for n, r in enumerate(rows)]
        out["row_count"] = len(rows)
        out["measure"] = {**entry, "forecast": cite}
        return out

    # ------------------------------------------------------------------ reader

    def _reader(self, actor: Any) -> tuple[Any, Any]:
        """(the store's connection, the reader's scope now: ``base._scope``, the grant its
        door is decided from in each call's own transaction, ``reader_grant``)."""
        conn = getattr(getattr(self.base, "store", None), "conn", None)
        if conn is None or not getattr(conn, "postgres", False):
            raise ToolError("not_served", "plx.sql reads the Postgres store only",
                            "use plx.search")
        return conn, self.base._scope(actor, self.base.clock())

    def _comms_reader(self, actor: Any) -> tuple[Any, dict[str, str]]:
        """``comms_search`` / ``comms_open``: the communications views' settings only. Their
        rule reads no door class (``business_comms.view_ddl``) and the settings are decided
        from the reader's scope on every call (never cached), so no door stamp binds them;
        an unset setting still shows nothing."""
        conn, scope = self._reader(actor)
        return conn, self._comms_settings(scope, actor)

    def _comms_settings(self, scope: Any, actor: Any) -> dict[str, str]:
        """The communications views' reader settings (``business_comms.view_ddl``): only
        for a statement naming one of them (``_query``), the comms search and open; never
        for one over the business tables (the reader's whole grant as JSON, megabytes for
        staff, was sent with every call and read back by its check)."""
        from operating.search import document_marks
        from operating.pg import business_comms as bc
        docs = {k: list(json.loads(v)) for k, v in document_marks.params(scope.held).items()
                if k in ("dm_ids", "dm_own")}
        settings = {   # the keys of ``COMMS_SETTINGS``
            "plx.reader_audience": json.dumps(sorted(scope.audience_keys)),
            "plx.reader_marks": json.dumps(sorted(scope.marking_sets)),
            # the reader's own withheld documents; PLX's own sales documents (``dm_own``, 38,478
            # ids, 10.4 MB a statement) only until every comms view reads them from their table
            # (``business_comms.own_issued_in_views``: before the views are installed the
            # setting is all they read)
            "plx.reader_hidden": json.dumps(sorted(
                set(docs.get("dm_ids") or ())
                | (set() if bc.own_issued_in_views() else set(docs.get("dm_own") or ())))),
            # the people exclusion list this process holds (its one-way tag), for the
            # communications views; '' when it cannot be decided (they then show nothing)
            "plx.reader_people": _people_tag(),
            # the Teams chats the reader may read and from which message creation on
            "plx.reader_teams": json.dumps(_teams_windows(self.base, actor)),
            # the transcript item rule: full reader or own identities (participants)
            "plx.reader_self": json.dumps(_transcript_reader(self.base, actor))}
        return settings

    def _schema_names(self, conn: Any) -> frozenset[str]:
        if self._schemas is None:
            with _lent(conn) as pg:
                rows = pg.execute("SELECT nspname FROM pg_namespace").fetchall()
            self._schemas = frozenset(r[0].lower() for r in rows) | {"pg_catalog", "public"}
        return self._schemas

    def _seconds(self, full: "bool | int" = False) -> float:
        """A statement's time limit in seconds: 0, none (``statement_timeout`` 0). ``full``
        is kept for the callers that still pass it; no read of the Ask is cut."""
        return 0.0

    def _scoped(self, conn: Any, door: "Door | None", work: Any, *, role: bool = True,
                extra: Mapping[str, str] | None = None, after: Any = None,
                full: "bool | int" = False) -> Any:
        """``scoped_read`` on ONE connection of plx.sql's own pool for the whole call (the
        door decided, the query, its lineage). ``role`` False: this module's own fixed
        statements (``comms_search``), which read the text index beside the guarded views
        as the store's own role; the rows still come only through the guarded views and
        their settings. ``door`` None: no door (comms reads no door class:
        ``_comms_reader``)."""
        waited_until = time.monotonic() + BUSY_WAIT_SECONDS
        while True:
            seconds = self._seconds(full)
            try:
                with _lent(conn) as pg:
                    return scoped_read(pg, door, work, seconds, as_role=role, extra=extra,
                                       after=after)
            except Busy:
                # a view swap or a door re-decide ends within seconds: ask again, as a lock
                # wait would
                if time.monotonic() + BUSY_POLL_SECONDS > waited_until:
                    raise
                time.sleep(BUSY_POLL_SECONDS)

    # ------------------------------------------------------------------ comms search

    def guarded(self, fn: Any, arguments: Any, actor: Any) -> dict[str, Any]:
        try:
            return fn(dict(arguments or {}), actor)
        except ToolError as exc:
            return {"error": {"code": exc.code, "message": exc.message[:600]}}

    # The AI's path (the internal plx.sql_comms_* tools, under the model) reads the guarded
    # views of ``<biz>_sql``: no Freightcom row (``ai_ok``). A person's own pages and search
    # read ``<biz>_person`` (``person_search`` / ``person_open``, never a registered tool):
    # the same permission rule without ``ai_ok``; the AI's role holds no grant on it.

    def comms_search(self, args: dict[str, Any], actor: Any) -> dict[str, Any]:
        """The AI's search: ``_comms_search`` over the guarded views."""
        return self._comms_search(args, actor, sql_views.guarded_schema())

    def person_search(self, args: dict[str, Any], actor: Any) -> dict[str, Any]:
        """A person's own search (the app's pages, never the model): Freightcom mail and
        messages the reader may read are shown."""
        return self._comms_search(args, actor, sql_views.person_schema())

    def comms_open(self, args: dict[str, Any], actor: Any) -> dict[str, Any]:
        """The AI's open: ``_comms_open`` over the guarded views."""
        return self._comms_open(args, actor, sql_views.guarded_schema())

    def person_open(self, args: dict[str, Any], actor: Any) -> dict[str, Any]:
        """A person's own open (never the model)."""
        return self._comms_open(args, actor, sql_views.person_schema())

    def _comms_search(self, args: dict[str, Any], actor: Any, guarded: str
                      ) -> dict[str, Any]:
        """One ranked top-k read of mail, Teams and (a full reader's) SharePoint files the
        reader may read (``comms_search``): ``query`` words and/or ``participant``,
        ``since``/``until``, ``sources`` (graph_mail, teams, sharepoint), ``limit``,
        ``cursor``. One statement through the views of ``guarded``; None of it is read when
        they are not served (``not_served``). ``searched.sources`` names what was searched:
        files only for a full reader on a host with an item gate (the caller searches them
        the old way otherwise), each file row then passed through that gate before it is
        returned (``business_comms.FILES``); a refused row is left out unannounced, as the
        old search leaves it out, and the next page starts after the last row read."""
        from operating.pg import business_comms as bc
        from operating.pg import comms_search as cs
        conn, settings = self._comms_reader(actor)
        biz = bv.SCHEMA
        limit = int(args.get("limit") or 20)
        try:
            words = cs.terms(args["query"]) if args.get("query") else []
            q = cs.tsquery(args["query"]) if words else None
            since = cs.instant(args.get("since"))
            moment = _ceiling()
            until = cs.instant(args.get("until") if moment is None else
                               read_ceiling.clamp_until(args.get("until"), moment), end=True)
            cur = cs.read_cursor(args["cursor"]) if args.get("cursor") else None
        except cs.SearchInputError as exc:
            raise ToolError("invalid_input", str(exc)) from None
        wanted = set(args.get("sources") or [s for _, _, s in cs.VIEWS.values()])
        files = "sharepoint" in wanted and self._files_reader(actor)
        settings = {**settings, bc.FILES_SETTING: bc.FILES_SEARCH if files else ""}
        # room for the file rows the item gate may refuse, and the refused rows the
        # cursor skips (read again past its returned row, refused again)
        skip = cur[3] if cur else 0
        k = (2 * limit + 1 if files else limit + 1) + skip
        own = limit + 1 + skip   # mail and Teams rows: never refused (``search_sql``)

        def search_rows(pg: Any, q: Any, cur: Any) -> Any:
            views = [v for v in cs.present(pg, guarded) if cs.VIEWS[v][2] in wanted
                     and (files or v not in cs.SEARCH_ONLY)]
            who = cs.party(args["participant"], pg, biz) if args.get("participant") else None
            side = cs.sidecar(pg) if q is not None else {}
            sql = cs.search_sql(views, guarded=guarded, biz=biz, query=q is not None,
                                party_kind=who.kind if who else None, since=since is not None,
                                until=until is not None,
                                cursor=cur is not None and cur[2] is not None,
                                cursor_tier=cur[0] if cur and cur[0] is not None else 0,
                                side=frozenset(side))
            if not sql:
                return views, who, []
            return views, who, pg.execute(sql, {
                "q": q, "party": who.value if who else None, "since": since, "until": until,
                "c_at": cur[1] if cur else None, "c_h": cur[2] if cur else None,
                "k": k, "k_own": own, "walk": cs.walk_rows(pg, q),
                **{n: v for one in side.values() for n, v in one.items()}}).fetchall()

        read_at: dict[str, Any] = {}

        def work(pg: Any, *_: Any) -> Any:
            got = search_rows(pg, q, cur)
            # where this very read ran and as of when: asked on the same connection, in the
            # same snapshot and route as the rows (never by a statement of its own after)
            read_at["data_as_of"] = data_as_of(pg)
            return got
        # a first page's meaning ranking runs beside the word search (``_comms_widened``)
        ranking = (self._meaning_started(args["query"], actor, wanted)
                   if words and cur is None else None)
        # and, once that ranking is in, its rows read beside the word passes on their own
        # pooled connection (``_meaning_rows``): the widening only merges them (they ran
        # one after the other: words, stems, then the wait and its statement, 5-15 s a call
        # on 11-21). A participant's search binds the party first: read after, as before.
        meaning = (_ATTACH_POOL.submit(
            contextvars.copy_context().run, self._meaning_rows, conn, settings, guarded,
            actor, wanted, files, args["query"], since, until, ranking)
            if ranking is not None and not args.get("participant") else None)
        attach_depth: dict[str, Any] = {}
        # and a first page's attachment pass (``_comms_attached``): its own statements on
        # its own pooled connection with the same settings; its rows are kept only after
        # the word rows are known (none of theirs again)
        attaching = (_ATTACH_POOL.submit(
            contextvars.copy_context().run, self._comms_attached, conn, settings, guarded,
            actor, q, None, since, until, ATTACH_EXTRA,
            set(), limit + MEANING_EXTRA + 1, attach_depth)
            if words and cur is None and "graph_mail" in wanted and not args.get("participant")
            and self._files_reader(actor) else None)
        from operating.call_phases import phase

        def stemmed(pg: Any, *_: Any) -> Any:
            # the rows holding every word by its stem ("renovating" for "Renovation"),
            # through the same views (``search_rows``); none when the stems are the words
            (stems,) = pg.execute("SELECT plainto_tsquery('english'::regconfig, %s)"
                                  "::text", (args["query"],)).fetchone()
            sq = cs.stem_tsquery(stems)
            if not sq or sq == q:
                return None, None, []
            got = search_rows(pg, sq, None)
            return got
        # a first page's stem pass runs beside the word pass on its own pooled connection
        # with the same settings (``_comms_scoped``, as the meaning and attachment passes):
        # its rows are used only when the word pass finds few, and pass the item gate
        # then; they ran one after the other (words, then stems: 1.8 s p50 of a search's
        # time on 6 of 11 HARD20 calls, 11-21). A participant's search binds the party in
        # the word pass: its stems are read after, as before.
        stemming = (_ATTACH_POOL.submit(
            contextvars.copy_context().run, self._comms_scoped, conn, settings, stemmed,
            actor) if words and cur is None and not args.get("participant") else None)
        try:
            with phase("comms.words"):
                views, who, rows = self._comms_scoped(conn, settings, work, actor)
        except cs.SearchInputError as exc:
            if stemming is not None:
                stemming.cancel()
            raise ToolError("invalid_input", str(exc)) from None
        except BaseException:
            if stemming is not None:
                stemming.cancel()
            raise
        if not views:
            if stemming is not None:
                stemming.cancel()
            raise ToolError("not_served", "mail and Teams are not served as tables here")
        rows, nxt = self._gated_files(rows, actor, limit, k, cur)
        more = nxt is not None
        documents = cs.hits(rows, words=words, party_=who)
        searched = cs.searched(views, args, words)
        searched["data_as_of"] = read_at["data_as_of"]   # the word pass's own read
        few = not more and len(rows) < min(limit, WIDEN_BELOW)
        if stemming is not None and not (few and words and cur is None):
            stemming.cancel()   # not needed: a pass already running ends on its own budget
        if few and words and cur is None:
            # few rows hold every word as typed: the rows holding every word by its stem,
            # through the same views and item gate
            try:
                with phase("comms.stems"):
                    if stemming is not None:
                        _, _, srows = stemming.result()
                    else:
                        _, _, srows = self._comms_scoped(conn, settings, stemmed, actor)
                srows, _ = self._gated_files(srows, actor, limit - len(rows), k, None)
            except (ToolError, cs.SearchInputError, cf.TimeoutError):
                if stemming is not None:
                    stemming.cancel()
                srows = []
            held = {r[1] for r in rows}
            srows = [r for r in srows if r[1] not in held][:limit - len(rows)]
            for hit in cs.hits(srows, words=words, party_=who):
                hit["match"] = "every word, by its stem"
                documents.append(hit)
            if srows:
                searched["widened"] = "fewer items held every word as typed: also items " \
                    "holding every word by its stem"
            rows = rows + srows
            few = len(rows) < min(limit, WIDEN_BELOW)
        room = min(max(limit - len(rows) if few else 0,
                       MEANING_EXTRA if ranking is not None else 0), HITS_MAX - len(rows))
        if words and cur is None and room > 0:
            # few rows hold every word: the rows holding some of them; and, on any first
            # page, the rows close in meaning; all through the same views
            # (``_comms_widened``)
            with phase("comms.widened"):
                extra, how = self._comms_widened(
                    conn, settings, guarded, actor, views, args["query"], words, who,
                    since, until, room, {r[1] for r in rows}, ranking, few, meaning)
            documents += cs.hits(extra, words=words, party_=who)
            for hit, (held, near) in zip(documents[len(rows):], how or []):
                hit["match"] = held
                if near is not None:
                    # the meaning ranking's place: kept here only (``plx.search`` shows
                    # ``match``)
                    hit["meaning_rank"] = near
            if how:
                searched["widened"] = "; ".join(filter(None, (searched.get("widened"), (
                    f"fewer than {WIDEN_BELOW} items held every word: also items holding "
                    "some of the words or close in meaning, each with its match" if few else
                    f"also up to {MEANING_EXTRA} items close in meaning that the word "
                    "match did not list, each with its match"))))
        # a participant's search: the pass needs the party ``search_rows`` resolved, so it
        # runs after the word search (as before the parallel pass), bound to that party
        after = (attaching is None and who is not None and words and cur is None
                 and "graph_mail" in wanted and self._files_reader(actor))
        if (attaching is not None or after) and "emails" in views:
            if attaching is None:
                with phase("comms.attachments"):
                    attached = self._comms_attached(
                        conn, settings, guarded, actor, q, who, since, until, ATTACH_EXTRA,
                        set(), limit + MEANING_EXTRA + 1, attach_depth)
            else:
                try:
                    with phase("comms.attachments_wait"):
                        attached = attaching.result()
                except Exception:  # noqa: BLE001 - late or failed: the word page answers alone
                    attaching.cancel()
                    attached = []
                    attach_depth["unread"] = True
            seen = {h["citation"] for h in documents}
            attached = [r for r in attached if r[1] not in seen][
                :max(0, min(ATTACH_EXTRA, HITS_MAX - len(documents)))]
            for hit in cs.hits(attached, words=words, party_=who):
                hit["match"] = "every word, in an attachment's text"
                documents.append(hit)
            if attached:
                searched["widened"] = "; ".join(filter(None, (
                    searched.get("widened"), f"also up to {ATTACH_EXTRA} mails whose "
                    "attachment's text holds every word, each with its match")))
        depth = cs.depth_said(args.get("query") if words else None, attach_depth,
                              later=cur is not None,
                              attachments="graph_mail" in wanted and "emails" in views
                              and bool(self._files_reader(actor)))
        if depth:
            searched["depth"] = depth
        out = {"documents": documents,
               "searched": searched, "gaps": [],
               "completeness": {"complete": not more, "truncated": more, "returned": len(rows),
                                "next_cursor": nxt if more and nxt else None}}
        if more:
            # a run of refused files longer than a cursor skips: stopped, said, no cursor
            out["completeness"]["reason"] = "page_cap" if nxt else "withheld_run"
            if words and cur is None and not args.get("participant"):
                # how many matched in all (A1a), when one statement can count them
                total = self._comms_count(conn, settings, guarded, actor, views, q, since,
                                          until)
                if total is not None:
                    out["completeness"]["total"] = total
        return out

    def _comms_attached(self, conn: Any, settings: Any, guarded: str, actor: Any, q: str,
                        who: Any, since: Any, until: Any, room: int, seen: set,
                        spare: int = 0, depth: Optional[dict] = None) -> list[Any]:
        """Up to ``room`` rows of the reader's mail (``comms_search.attachment_sql``, the
        same guarded view as the word search) whose attachment's text holds every word of
        ``q``, newest first: none in ``seen`` or that a word page lists (``listed_sql``),
        none whose attachment the reader is kept from (``attachment_blocked_sql``),
        The read failing or
        late: none (the word search answers alone). ``depth`` (when given) is told how
        deep the pass looked (``comms_search.depth_said``)."""
        from operating.pg import comms_search as cs
        depth = {} if depth is None else depth
        if room <= 0:
            return []

        def work(pg: Any) -> list[Any]:
            common = cs.walk_rows(pg, q) > 1
            depth["common"] = common
            mails = None if common else [r[0] for r in pg.execute(
                cs.attachment_mails_sql(), {"q": q, "mails": cs.ATTACH_MAILS})]
            if mails is not None:
                depth["mails"] = len(mails)
            if mails == []:
                return []
            sql = cs.attachment_sql(guarded=guarded, party_kind=who.kind if who else None,
                                    since=since is not None, until=until is not None,
                                    common=common)
            rows = [r for r in pg.execute(sql, {
                "q": q, "party": who.value if who else None, "since": since,
                "until": until, "k": 2 * room + len(seen) + spare, "walk": cs.ATTACH_WALK,
                "mails": mails}) if r[1] not in seen]
            if not rows:
                return []
            listed = {r[0] for r in pg.execute(cs.listed_sql(bv.SCHEMA), {
                "handles": [r[1] for r in rows], "q": q})}
            blocked = {r[0] for r in pg.execute(cs.attachment_blocked_sql(), {
                "ids": [r[-1] for r in rows]})}
            return [r for r in rows if r[1] not in listed and r[-1] not in blocked]
        try:
            rows = self._comms_scoped(conn, settings, work, actor)
        except ToolError:
            depth["unread"] = True
            return []
        depth["read"] = True
        return rows[:room]

    def _meaning_started(self, query: str, actor: Any, wanted: set) -> Any:
        """The hybrid ranking (``operating/search``) of ``query`` over the mail, Teams
        and files chunks ``wanted`` names, started on a search thread (the database and
        the models, never this reader's store or gate); None when search is off."""
        from operating.search import plug
        if not plug.enabled():
            return None
        kinds = [k for src, ks in (("graph_mail", ("mail",)), ("teams", ("teams",)),
                                   ("sharepoint", ("sharepoint", "sheet_row")))
                 if src in wanted for k in ks]
        # not ``plug.ranked``: that records the call's ranking mode, which binds a
        # documents cursor; this ranking is only a candidate list
        return plug._POOL.submit(plug._rank, self.base.policy, actor, query, kinds=kinds)

    def _meaning_rows(self, conn: Any, settings: Any, guarded: str, actor: Any,
                      wanted: set, files: bool, query: str, since: Any, until: Any,
                      ranking: Any) -> tuple[Any, list[Any]]:
        """(the meaning ranking, its objects' rows): the ranking waited for at most
        the model service's own answer (no wait is cut), then every object it names read through the same guarded views
        as the word search (``meaning_sql``, in the reader's own scope) on a connection of
        its own, so this runs beside the word passes; the caller gates the ranking
        (``plug._gated``) and keeps only the rows of the objects that passed. No objects:
        no statement."""
        from operating.pg import comms_search as cs
        raw = ranking.result()
        everyone = [h["object_id"] for h in getattr(raw, "hits", None) or ()
                    if isinstance(h, Mapping) and isinstance(h.get("object_id"), str)]
        everyone = list(dict.fromkeys(everyone))[:MEANING_READ_K]
        if not everyone:
            return raw, []

        def work(pg: Any) -> list[Any]:
            views = [v for v in cs.present(pg, guarded) if cs.VIEWS[v][2] in wanted
                     and (files or v not in cs.SEARCH_ONLY)]
            sql = cs.meaning_sql(views, guarded=guarded, biz=bv.SCHEMA, party_kind=None,
                                 since=since is not None, until=until is not None)
            return pg.execute(sql, {
                "ids": everyone, "q": cs.tsquery_any(query) or cs.tsquery(query),
                "q_all": cs.tsquery(query), "party": None, "since": since,
                "until": until}).fetchall() if sql else []
        return raw, self._comms_scoped(conn, settings, work, actor)

    def _comms_widened(self, conn: Any, settings: Any, guarded: str, actor: Any,
                       views: Sequence[str],
                       query: str, words: Sequence[str], who: Any, since: Any, until: Any,
                       room: int, seen: set, ranking: Any, few: bool,
                       meaning: Any = None) -> tuple[list[Any], Any]:
        """Up to ``room`` more rows: the rows ``ranking`` (``_meaning_started``) ranks
        close in meaning that the word search lists on no page, and, for a query whose every-word match found ``few``, the rows
        holding any of its words (``comms_search.tsquery_any``) when there is no such
        ranking (search off, failed or late: the hybrid ranking already weighs the words
        a row holds); most words held first, then the meaning rank. Every row is read
        through the same guarded views as the word search (``search_sql`` /
        ``meaning_sql``) in the same scoped reader, and each file row passes the item
        gate: the ranking only names candidates, never a row. Returns the rows and, per
        row, how it matched ("n of m words" and/or "close in meaning") with its meaning
        rank (None when the ranking did not name it); ``([], None)`` when neither ran."""
        from operating.pg import comms_search as cs
        from operating.search import plug
        from operating.call_phases import phase
        ids: list[str] = []
        read_ahead = None   # the ranking's rows read beside the word passes
        if meaning is not None:
            try:
                with phase("comms.meaning_wait"):
                    raw, read_ahead = meaning.result()
                ranked = plug._gated(raw, self.base.policy, actor, self.base._item_allowed)
                ids = [h["object_id"] for h in ranked.hits][:MEANING_K]
            except Exception:  # noqa: BLE001 - late or failed: the words answer alone
                meaning.cancel()
                ranking.cancel()
                ids, read_ahead = [], None
        elif ranking is not None:
            try:
                with phase("comms.meaning_wait"):
                    raw = ranking.result()
                ranked = plug._gated(raw, self.base.policy, actor, self.base._item_allowed)
                ids = [h["object_id"] for h in ranked.hits][:MEANING_K]
            except Exception:  # noqa: BLE001 - late or failed: the words answer alone
                ranking.cancel()
                ids = []
        any_q = cs.tsquery_any(query) if few and not ids else None
        if any_q is None and not ids:
            return [], None
        k = 3 * room + len(seen) + 1

        def work(pg: Any) -> list[Any]:
            some: list[Any] = []
            if any_q is not None:
                side = cs.sidecar(pg)
                sql = cs.search_sql(views, guarded=guarded, biz=bv.SCHEMA, query=True, party_kind=who.kind if who else None,
                                    since=since is not None, until=until is not None,
                                    cursor=False, side=frozenset(side))
                some = pg.execute(sql, {
                    "q": any_q, "party": who.value if who else None, "since": since,
                    "until": until, "c_at": None, "c_h": None, "k": k, "k_own": k,
                    "walk": cs.walk_rows(pg, any_q),
                    **{n: v for one in side.values() for n, v in one.items()}}).fetchall() if sql else []
            near: list[Any] = []
            if ids and read_ahead is not None:
                # read ahead for every ranked object; kept: the ones the gate passed
                held = set(ids)
                near = [r for r in read_ahead if r[-1] in held]
            elif ids:
                sql = cs.meaning_sql(views, guarded=guarded, biz=bv.SCHEMA, party_kind=who.kind if who else None,
                                     since=since is not None, until=until is not None)
                near = pg.execute(sql, {
                    "ids": ids, "q": cs.tsquery_any(query) or cs.tsquery(query),
                    "q_all": cs.tsquery(query),
                    "party": who.value if who else None, "since": since,
                    "until": until}).fetchall() if sql else []
            return some, near
        if any_q is None and read_ahead is not None:
            some, near = work(None)   # nothing left to read
        else:
            with phase("comms.widen_sql"):
                some, near = self._comms_scoped(conn, settings, work, actor)
        rank = {i: n for n, i in enumerate(ids, start=1)}
        found: dict[str, dict[str, Any]] = {}
        for at, row in enumerate(some):
            if row[1] not in seen:
                found.setdefault(row[1], {"row": row, "order": at,
                                          "held": max(1, cs.words_held(row, words))})
        for row in near:
            # a row the word search matches is on this page or a later one: never here
            # too (a full page's extra came back on the next page)
            if row[1] in seen or row[-1] not in rank or row[-2]:
                continue
            got = found.setdefault(row[1], {"row": row, "order": len(some),
                                            "held": cs.words_held(row, words)})
            got["meaning"] = min(got.get("meaning", 10 ** 6), rank[row[-1]])
        n = len(set(w.casefold() for w in words))
        # few: most words held first; else the word match already lists rows holding
        # every word, so the rows that do not (a synonym, "Oral Rinse") come first
        ordered = sorted(found.values(), key=lambda f: (
            (-(f["held"] + (1 if "meaning" in f else 0)) if few else f["held"] >= n),
            f.get("meaning", 10 ** 6), f["order"]))
        kept: list[Any] = []
        how: list[tuple[str, Any]] = []
        for f in ordered:
            if len(kept) >= room:
                break
            row = f["row"]
            if row[0] in cs.SEARCH_ONLY and not self.base._item_allowed(actor, row[1]):
                continue
            kept.append(row)
            how.append((", ".join(
                ([f"{min(f['held'], n)} of {n} words"] if f["held"] else [])
                + (["close in meaning"] if "meaning" in f else [])), f.get("meaning")))
        return kept, how

    def _files_reader(self, actor: Any) -> bool:
        """Whether this search reads SharePoint files from their table: a full reader
        (``_full``) on a host whose item gate decides each file (none: the old way)."""
        return getattr(self.base, "item_gate", None) is not None and self._full(actor)

    def _gated_files(self, rows: Sequence[Any], actor: Any, limit: int, k: int,
                     cur: Optional[Sequence[Any]] = None) -> tuple[list[Any], Optional[str]]:
        """``rows`` (newest first, as read, at most ``k``) with every SharePoint file row
        the item gate refuses this reader left out, cut to ``limit``; and the next page's
        cursor (None: nothing follows; "": more follows past what a cursor may skip). The
        cursor is built from the last row RETURNED (or, none returned, the cursor this page
        started from) and the count of refused rows read after it, never from a refused
        row: its handle and date stay out, and the next page reads those rows again,
        refuses them again and goes on past them (``comms_search.cursor_of``)."""
        from operating.pg import comms_search as cs
        handles = [r[1] for r in rows if r[0] in cs.SEARCH_ONLY]
        ahead = getattr(getattr(self.base, "item_gate", None), "prefetch", None)
        if handles and callable(ahead):
            try:
                ahead(actor, handles)
            except Exception:  # noqa: BLE001 - a read ahead decides nothing
                pass
        kept: list[Any] = []
        after = 0     # rows read since the last one kept (all refused)
        for row in rows:
            if row[0] in cs.SEARCH_ONLY and not self.base._item_allowed(actor, row[1]):
                after += 1
                continue
            if len(kept) == limit:
                return kept, cs.cursor_of(kept[-1])          # one more row exists
            kept.append(row)
            after = 0
        if not rows or len(rows) < k:
            return kept, None
        # every row read was taken or refused: a full read may have more after its last
        # (this page read from its cursor's returned row on, so ``after`` already counts
        # the rows that cursor skipped)
        if after > cs.MAX_SKIP:
            return kept, ""
        if kept:
            return kept, cs.cursor_of(kept[-1], after)
        if cur is None or cur[2] is None:   # none returned: from the start, past them all
            return kept, cs.cursor_at(None, None, None, after)
        return kept, cs.cursor_at(cur[0], cur[1], cur[2], after)

    def _comms_open(self, args: dict[str, Any], actor: Any, guarded: str) -> dict[str, Any]:
        """One mail or Teams item's text by its handle through the views of ``guarded``
        (``comms_search.open_sql``). ``{"not_comms": true}``: no comms row this reader may
        read has the handle: none at all, or one outside the reader's scope (the same
        answer, so it never tells that an unreadable handle is a mail or Teams item; the
        caller reads it the old way, whose own gates refuse it). A row the reader may read
        that is kept from the AI (Freightcom, ``ai_ok``; only on the AI's views): withheld,
        the answer the old path gives for it."""
        from operating.pg import comms_search as cs
        conn, settings = self._comms_reader(actor)
        if self._files_reader(actor):   # a full reader reads files through the door alone
            from operating.pg import business_comms as bc
            settings = {**settings, bc.FILES_SETTING: bc.FILES_FULL}
        biz = bv.SCHEMA
        handle = args.get("citation")
        offset = int(args.get("text_offset") or 0)
        most = int(args.get("max_chars") or 20000)
        near = args.get("near") or ()
        if not (isinstance(near, (list, tuple)) and len(near) <= cs.MAX_TERMS
                and all(isinstance(w, str) and 0 < len(w) <= 100 for w in near)):
            raise ToolError("invalid_input", "near: up to 16 words")

        shared: list[dict[str, Any]] = []

        def work(pg: Any) -> Any:
            views = cs.present(pg, guarded, opening=True)
            if not views:
                return None
            rows = pg.execute(cs.open_sql(views, guarded=guarded, biz=biz),
                              {"h": handle}).fetchall()
            if rows:
                if rows[0][0] == "teams_messages":
                    shared.extend(self._shared_files(pg, guarded, handle))
                return rows
            person = sql_views.person_schema()
            if guarded != person:   # the reader may read it, the AI may not
                mine = cs.present(pg, person, opening=True)
                if mine and pg.execute(cs.exists_sql(mine, biz=person), {"h": handle}
                                       ).fetchone()[0]:
                    return []
            every = cs.present(pg, biz, opening=True) if moment is not None else ()
            if every and pg.execute(cs.exists_sql(every, biz=biz), {"h": handle}
                                    ).fetchone()[0]:
                return "comms"      # a mail or Teams item of any scope: below
            return None
        moment = _ceiling()
        rows = self._comms_scoped(conn, settings, work, actor)
        if rows == "comms":
            # under a replay's ceiling a mail or Teams item outside the reader's views is
            # opened by no other path (the old read of a message is not bounded)
            raise ToolError("not_found", "no such message")
        if rows is None:
            # no mail or Teams item at all (a PO, an order, a file): the old read, which
            # under a ceiling shows it only with a time before the moment
            # (``ontology_tools_search._open_single``; 11-22: every Portal PO a replayed
            # job opened answered "no such message")
            return {"not_comms": True}
        if not rows:
            raise ToolError("egress_withheld", "this item is never shown to the assistant")
        if moment is not None and read_ceiling.after(rows[0][4], moment):
            raise ToolError("not_found", "no such message")   # it had not happened yet
        out = cs.opened(rows, offset=offset, most=most, near=tuple(near))
        if shared:
            out["shared_files"] = shared
        return out

    SHARED_FILES_MAX = 20

    def _shared_files(self, pg: Any, guarded: str, handle: str) -> list[dict[str, Any]]:
        """The files a Teams message shares, linked by exact address
        (``operating.pg.teams_file_links``), each only when the reader's own guarded message
        AND file views show their rows (``<guarded>.teams_message_files``): a reader who may
        not read a file sees none. No such view (not built here): none, never an error."""
        from operating.pg import comms_search as cs
        from operating.pg import teams_file_links as tfl
        if pg.execute("SELECT to_regclass(%s)", (f"{guarded}.{tfl.VIEW}",)).fetchone()[0] is None:
            return []
        rows = pg.execute(
            f"SELECT file_handle, file_name, file_path, mime_type, via FROM {guarded}.{tfl.VIEW} "
            "WHERE message_handle = %(h)s ORDER BY file_name, file_handle LIMIT %(n)s",
            {"h": handle, "n": self.SHARED_FILES_MAX}).fetchall()
        return [{"citation": fh, "name": cs._text(name, fh, 300) if name else None,
                 "path": cs._text(path, fh, 500) if path else None,
                 "mime_type": mime, "linked_by": via}
                for fh, name, path, mime, via in rows]

    def _comms_count(self, conn: Any, settings: Mapping[str, str], guarded: str, actor: Any,
                     views: Sequence[str], q: Any, since: Any, until: Any) -> Optional[int]:
        """Every row of ``views`` the reader may read holding every query word in the dates
        (``comms_search.count_sql``, one statement); None (logged) when
        it cannot count: files among the views (each is re-gated after the statement), or
        the statement failed."""
        from operating.pg import comms_search as cs
        if any(v in cs.SEARCH_ONLY for v in views):
            return None
        if not cs.count_sql(views, guarded=guarded, biz=bv.SCHEMA, since=since is not None,
                            until=until is not None):
            return None

        def work(pg: Any, *_: Any) -> Any:
            side = cs.sidecar(pg)
            sql = cs.count_sql(views, guarded=guarded, biz=bv.SCHEMA, since=since is not None,
                               until=until is not None, side=frozenset(side))
            return pg.execute(sql, {"q": q, "since": since, "until": until,
                                    **{n: v for one in side.values() for n, v in one.items()}}
                              ).fetchone()[0]
        try:
            got = self._comms_scoped(conn, settings, work, actor)
        except Exception as exc:  # noqa: BLE001 - not counted: said in the coverage, logged
            logging.getLogger("operating.ontology_tools_sql").warning(json.dumps(
                {"search_count_failed": type(exc).__name__}))
            return None
        return int(got) if got is not None else None

    def _comms_scoped(self, conn: Any, settings: Mapping[str, str], work: Any,
                      actor: Any) -> Any:
        """``_scoped`` for the comms statements (as the store's own role: ``_scoped``). The
        store's role refused (its grants on the comms views missing: ``sql_views``
        install not run since the role was made): not ready, so the caller reads the old
        way instead of failing every search and open."""
        try:
            return self._scoped(conn, None, work, role=False, extra=settings,
                                full=True)
        except ToolError as exc:
            if exc.code != "not_readable":
                raise
            raise ToolError("not_ready", "mail and Teams are not readable as tables here "
                            "yet") from None

    # ------------------------------------------------------------------ records search

    def records_search(self, args: dict[str, Any], actor: Any) -> dict[str, Any]:
        """``plx.search``'s records leg (``operating.pg.records_search``): label words over
        the reader's guarded ``records`` view through the same door as ``plx.sql``
        (decided in the statement's own snapshot, the generic cost guard applied). Not
        built here: ``not_served``; being rebuilt: ``not_ready`` (the leg is skipped)."""
        from operating.pg import generic_views
        from operating.pg import records_search as rs
        conn, scope = self._reader(actor)
        limit = int(args.get("limit") or 20)
        try:
            words = rs.terms(args.get("query"))
            moment = _ceiling()
            since = rs.day(args.get("since"))
            until = rs.day(args.get("until"))
            if moment is not None:
                # the day before the ceiling's (a record date says only its day)
                last = (read_ceiling.day_of(moment) - dt.timedelta(days=1)).isoformat()
                until = last if until is None or until > last else until
            off = rs.read_cursor(args["cursor"]) if args.get("cursor") else 0
        except rs.SearchInputError as exc:
            raise ToolError("invalid_input", str(exc)) from None
        dated = {"since": since is not None, "until": until is not None}
        bound = rs.params(words, str(args.get("query")), since=since, until=until,
                          k=limit + 1, off=off)
        door = Door(reader_grant(scope, full=self._full(actor)))

        def read(sql: str, bound: dict[str, Any]) -> Any:
            def work(pg: Any) -> Any:
                if pg.execute("SELECT to_regclass('records')").fetchone()[0] is None:
                    raise ToolError("not_served", "records are not served as a table here")
                if "records" in door.decided["refused"]:
                    raise ToolError("not_ready", "records is being rebuilt")
                (plan,) = pg.execute("EXPLAIN (FORMAT JSON) " + sql, bound).fetchone()
                plan = plan if isinstance(plan, list) else json.loads(plan)
                generic_views.guard(plan, sql, generic_views.table_sizes(pg))
                return pg.execute(sql, bound).fetchall()
            return self._scoped(conn, door, work,
                                full=True)
        # the labels holding the words, by the label index, as handles in the hits' order
        # (``rs.candidates_sql``); the hits are read through the reader's door among them.
        # Every match was a candidate, or the page filled from the first ones (any later
        # match orders after them): the same hits as reading every label through the door;
        # else that whole read (the cost guard may refuse it: a gap, as before)
        cap = rs.candidates(off, limit + 1)
        handles = self._label_candidates(conn, words, bound, cap, dated)
        rows = None
        if handles is not None:
            rows = read(rs.search_sql(len(words), among=True, **dated),
                        {**bound, "hs": handles})
            if len(handles) >= cap and len(rows) <= limit:
                rows = None
        if rows is None:
            rows = read(rs.search_sql(len(words), **dated), bound)
        more = len(rows) > limit
        capped = more and off + limit > rs.MAX_OFFSET
        rows = rows[:limit]
        documents, searched = rs.hits(rows), rs.searched(args, words, capped=capped)
        if not off and not more and len(rows) < min(limit, WIDEN_BELOW):
            # few labels hold every word: the labels holding every word by its stem
            # ("calibrating" for "calibration"), then those spelled close to the query
            # ("Coop" for "Co-operatives"), read through the same door
            room = min(MEANING_EXTRA, limit - len(rows))
            stemmed = self._records_stemmed(conn, bound, dated, {r[0] for r in rows},
                                            room, read)
            for hit in rs.hits(stemmed):
                hit["match"] = "every word, by its stem"
                documents.append(hit)
            # a word naming a record by number ("purchase order 5626" for "PO#5626"): the
            # labels holding every such word, through the same door
            seen = {r[0] for r in rows} | {r[0] for r in stemmed}
            idents = rs.numbers(words)
            numbered = []
            if idents and len(idents) < len(words) and room > len(stemmed):
                nb = rs.params(idents, str(args.get("query")), since=since, until=until,
                               k=rs.CLOSE_CAP, off=0)
                nb.update({f"r{i}": rs.whole(w) for i, w in enumerate(idents)})
                hs = self._label_candidates(conn, idents, nb, rs.CLOSE_CAP, dated,
                                            whole=True)
                if hs:
                    numbered = rs.by_number(
                        [r for r in read(rs.search_sql(len(idents), among=True, **dated),
                                         {**nb, "hs": hs}) if r[0] not in seen],
                        idents, words)[:room - len(stemmed)]
            for hit in rs.hits(numbered):
                hit["match"] = "every number in the query"
                documents.append(hit)
            seen |= {r[0] for r in numbered}
            # words naming a record type ("assembly orders for tubes"): that type's
            # records whose label holds the other words by their stem, same door
            typed = self._records_typed(conn, bound, dated, words, seen,
                                        room - len(stemmed) - len(numbered), read)
            for hit in rs.hits(typed):
                hit["match"] = "every other word, in a record of the type the query names"
                documents.append(hit)
            seen |= {r[0] for r in typed}
            near = self._records_close(conn, bound, dated, seen,
                                       room - len(stemmed) - len(numbered) - len(typed),
                                       read)
            for hit in rs.hits(near):
                hit["match"] = "close in spelling"
                documents.append(hit)
            widened = (["also records whose name holds every word by its stem"]
                       if stemmed else []) + (
                ["also records whose name holds every number in the query"]
                if numbered else []) + (
                ["also records of the type the query names whose name holds its other "
                 "words"] if typed else []) + (
                ["also records whose name is spelled close to the query"] if near else [])
            if widened:
                searched["widened"] = "; ".join(widened)
        out = {"documents": documents, "searched": searched, "gaps": [],
               "completeness": {"complete": not more, "truncated": more,
                                "returned": len(rows),
                                "next_cursor": f"r1:{off + limit}" if more and
                                not capped else None}}
        if more:
            out["completeness"]["reason"] = "depth_cap" if capped else "page_cap"
        return out

    def _records_stemmed(self, conn: Any, bound: Mapping[str, Any],
                         dated: Mapping[str, bool], seen: set, room: int, read: Any
                         ) -> list[Any]:
        """Up to ``room`` rows of the reader's guarded ``records`` (``read``: the door's
        own statement) whose label holds every word of the query by its English stem,
        none in ``seen``: the store names the candidates (``rs.stem_candidates_sql``, a
        read-only transaction of its own, ), the door reads the
        rows. None when no stem is long enough for the label index or the candidate read
        failed or ran out of time."""
        import psycopg
        from operating.pg import records_search as rs
        query = str(bound.get("q") or "")
        if room <= 0 or not query:
            return []
        ms = 0   # no limit
        try:
            with _lent(conn) as pg:
                kept = (pg.isolation_level, pg.read_only)
                try:
                    pg.read_only = True
                    with pg.transaction():
                        pg.execute("SELECT set_config('statement_timeout', %s, true), "
                                   "set_config('search_path', 'pg_catalog', true)", (str(ms),))
                        stq = pg.execute("SELECT plainto_tsquery('english'::regconfig, %s)"
                                         "::text", (query,)).fetchone()[0]
                        pats = rs.stems(stq)
                        handles = [r[0] for r in pg.execute(rs.stem_candidates_sql(
                            len(pats), biz=bv.SCHEMA, **dated), {
                                **bound, "stq": stq, "cap": rs.CLOSE_CAP,
                                **{f"s{i}": x for i, x in enumerate(pats)}})] if pats else []
                finally:
                    if not pg.closed and pg.info.transaction_status == 0:
                        pg.isolation_level, pg.read_only = kept
        except (psycopg.Error, ToolError, rs.SearchInputError):
            return []
        handles = [h for h in handles if h not in seen]
        if not handles:
            return []
        rows = read(rs.among_sql(**dated), {**bound, "hs": handles, "k": room})
        return [r for r in rows if r[0] not in seen][:room]

    def _records_typed(self, conn: Any, bound: Mapping[str, Any], dated: Mapping[str, bool],
                       words: Sequence[str], seen: set, room: int, read: Any) -> list[Any]:
        """Up to ``room`` rows of the reader's guarded ``records`` (``read``: the door's
        own statement) of a type the query names by its plain name's stems
        (``record_types.name``, ``rs.named_types``) whose label holds the query's other
        words by their stem, none in ``seen``: the store names the candidates (a read-only
        transaction of its own, ), the door reads the rows."""
        import psycopg
        from operating.pg import records_search as rs
        if room <= 0 or not words:
            return []
        ms = 0   # no limit
        handles: list[str] = []
        try:
            with _lent(conn) as pg:
                kept = (pg.isolation_level, pg.read_only)
                try:
                    pg.read_only = True
                    with pg.transaction():
                        pg.execute("SELECT set_config('statement_timeout', %s, true), "
                                   "set_config('search_path', 'pg_catalog', true)", (str(ms),))
                        word_stems = {w: rs.lexemes(t) for w, t in pg.execute(
                            "SELECT w, plainto_tsquery('english'::regconfig, w)::text "
                            "FROM unnest(%s::text[]) w", (list(dict.fromkeys(words)),))}
                        type_stems = [(t, rs.lexemes(n)) for t, n in pg.execute(
                            "SELECT type, plainto_tsquery('english'::regconfig, "
                            f"coalesce(name, ''))::text FROM {bv.SCHEMA}.record_types")]
                        for types, rest in rs.named_types(type_stems, word_stems):
                            stq = pg.execute("SELECT plainto_tsquery('english'::regconfig, "
                                             "%s)::text", (" ".join(rest),)).fetchone()[0]
                            pats = rs.stems(stq)
                            if not pats:
                                continue
                            handles += [r[0] for r in pg.execute(rs.stem_candidates_sql(
                                len(pats), biz=bv.SCHEMA, typed=True, **dated), {
                                    **bound, "stq": stq, "cap": rs.CLOSE_CAP,
                                    "types": types,
                                    **{f"s{i}": x for i, x in enumerate(pats)}})]
                finally:
                    if not pg.closed and pg.info.transaction_status == 0:
                        pg.isolation_level, pg.read_only = kept
        except (psycopg.Error, ToolError, rs.SearchInputError):
            return []
        handles = [h for h in dict.fromkeys(handles) if h not in seen]
        if not handles:
            return []
        rows = read(rs.among_sql(**dated), {**bound, "hs": handles, "k": room})
        return [r for r in rows if r[0] not in seen][:room]

    def _records_close(self, conn: Any, bound: Mapping[str, Any], dated: Mapping[str, bool],
                       seen: set, room: int, read: Any) -> list[Any]:
        """Up to ``room`` rows of the reader's guarded ``records`` (``read``: the door's
        own statement) whose label is spelled close to the query, closest first, none in
        ``seen``: the store names the candidates by the label's trigram index
        (``rs.close_candidates_sql``, a read-only transaction of its own, at most
        the label index), the door reads the rows. None when the query is too short or the
        candidate read failed or ran out of time, or pg_trgm is not installed."""
        import psycopg
        from operating.pg import records_search as rs
        query = str(bound.get("q") or "")
        if room <= 0 or sum(ch.isalnum() for ch in query) < rs.CLOSE_MIN_LETTERS:
            return []
        ms = 0   # no limit
        try:
            with _lent(conn) as pg:
                kept = (pg.isolation_level, pg.read_only)
                try:
                    pg.read_only = True
                    with pg.transaction():
                        pg.execute("SELECT set_config('statement_timeout', %s, true), "
                                   "set_config('search_path', 'pg_catalog', true), "
                                   "set_config('pg_trgm.similarity_threshold', %s, true)",
                                   (str(ms), str(rs.CLOSE_SIMILARITY)))
                        trgm = pg.execute("SELECT extnamespace::regnamespace::text FROM "
                                          "pg_extension WHERE extname = 'pg_trgm'").fetchone()
                        handles = [r[0] for r in pg.execute(rs.close_candidates_sql(
                            biz=bv.SCHEMA, trgm=trgm[0], **dated),
                            {**bound, "cap": rs.CLOSE_CAP})] if trgm else []
                finally:
                    if not pg.closed and pg.info.transaction_status == 0:
                        pg.isolation_level, pg.read_only = kept
        except (psycopg.Error, ToolError):
            return []
        handles = [h for h in handles if h not in seen]
        if not handles:
            return []
        rows = read(rs.among_sql(**dated), {**bound, "hs": handles, "k": room})
        return [r for r in rows if r[0] not in seen][:room]

    def _label_candidates(self, conn: Any, words: Sequence[str], bound: Mapping[str, Any],
                          cap: int, dated: Mapping[str, bool], whole: bool = False
                          ) -> "list[str] | None":
        """``records_search``'s candidates (``rs.candidates_sql``): the store's own read of
        its records table (as the comms search reads its text index), in a read-only
        transaction of its own, ; never returned to the caller.
        None: no word the label index finds, or the read failed or ran out of time (the
        search then reads every label through the door, as before)."""
        import psycopg
        from operating.pg import records_search as rs
        positions = rs.indexed(words)
        if not positions:
            return None
        sql = rs.candidates_sql(positions, biz=bv.SCHEMA, whole=whole, **dated)
        ms = 0   # no limit
        try:
            with _lent(conn) as pg:
                kept = (pg.isolation_level, pg.read_only)
                try:
                    pg.read_only = True
                    with pg.transaction():
                        pg.execute("SELECT set_config('statement_timeout', %s, true), "
                                   "set_config('search_path', 'pg_catalog', true)", (str(ms),))
                        return [r[0] for r in pg.execute(sql, {**bound, "cap": cap})]
                finally:
                    if not pg.closed and pg.info.transaction_status == 0:
                        pg.isolation_level, pg.read_only = kept
        except (psycopg.Error, ToolError) as e:
            import logging
            logging.getLogger(__name__).warning(
                "label candidates unavailable, reading every label: %s", (str(e).splitlines() or [""])[0])
            return None

    # ------------------------------------------------------------------ query

    def _read(self, conn: Any, scope: Any, sql: str, actor: Any) -> dict[str, Any]:
        """One checked statement read as ``actor`` through the door (the one read of
        ``plx.sql``: ``_query`` shows it, ``chain`` walks with it): its columns, every row,
        the profile, the sources and what the reader's door withheld."""
        wrapped = f"SELECT * FROM (\n{sql}\n) AS plx_q"
        views = sorted(v for v in bv.VIEWS if re.search(rf"\b{v}\b", sql, re.I))
        derived = [v for v in COMMS_DERIVED if re.search(rf"\b{v}\b", sql, re.I)]
        # the communications views read their own settings beside the door's: only when
        # the statement names one or a view over them (unset, they show nothing: fail
        # closed)
        extra = self._comms_settings(scope, actor) \
            if derived or any(bv.VIEWS[v].fill for v in views) else None
        if extra is not None and ("sharepoint_files" in views
                                  or "teams_message_files" in derived) \
                and self._files_reader(actor):
            from operating.pg import business_comms as bc
            extra = {**extra, bc.FILES_SETTING: bc.FILES_FULL}   # a full reader: the door alone
        # the generic door tables (``generic_views``): refused while rebuilt, withheld
        # counts and build time as a curated view's
        views += [v for v in GENERIC if re.search(rf"\b{v}\b", sql, re.I)]
        full = self._full(actor)
        door = Door(reader_grant(scope, full=full))

        def work(pg: Any) -> Any:
            refused = [v for v in views if v in door.decided["refused"]]
            if refused:
                # a view whose rows carry a mark it does not mask: no wait ends that
                raise ToolError("not_ready", f"{', '.join(refused)} cannot be read here "
                                "yet", "use plx.search")
            (plan,) = pg.execute("EXPLAIN (FORMAT JSON) " + wrapped).fetchone()
            plan = plan if isinstance(plan, list) else json.loads(plan)
            cost = float(plan[0]["Plan"]["Total Cost"])
            # every reader reads as an analyst at a psql prompt does (owner 11-22: the same
            # database reach for all, the permission door unchanged): the statement's own
            # time limit: none; a costly one waits for one of the
            # box's few slots for those (``_costly_slot``)
            if cost > MAX_COST:
                _costly_slot(pg)
            cols, rows, stopped, profile = _fetch(pg, sql)
            return (cols, rows, stopped, _built(pg, views), _visible(pg, views, cols, rows),
                    profile, data_as_of(pg))

        def lineage(pg: Any, got: Any) -> list[dict[str, Any]]:
            return _sources(pg, got[4])

        while True:
            try:
                (cols, rows, stopped, built, _seen, profile, read_at), sources = self._scoped(
                    conn, door, work, extra=extra, after=lineage, full=True)
                break
            except _NoSlot:
                # the slot wait holds no pooled connection: the try's transaction ended
                time.sleep(FULL_COSTLY_POLL_SECONDS)   # queued behind the box's slots, never refused
        # per view read, per column, how many rows the reader may read hold a value it
        # may not (they read NULL there), from ``class_counts`` when its door was decided
        withheld = {v: dict(door.decided["withheld"][v]) for v in views
                    if door.decided["withheld"].get(v)}
        return {"cols": cols, "rows": rows, "stopped": stopped, "built": built,
                "profile": profile, "sources": sources, "read_at": read_at, "views": views,
                "withheld": withheld}

    def _query(self, conn: Any, scope: Any, sql: str, actor: Any) -> dict[str, Any]:
        started = time.monotonic()
        return self._present(sql, self._read(conn, scope, sql, actor), actor, started)

    def _present(self, sql: str, got: dict[str, Any], actor: Any, started: float
                 ) -> dict[str, Any]:
        """A read (``_read``) as the answer: every row held under a new result id, the
        first page shown, the profile, what was withheld and the note on a cut."""
        cols, rows, stopped, built, profile, sources, read_at = (
            got["cols"], got["rows"], got["stopped"], got["built"], got["profile"],
            got["sources"], got["read_at"])
        views, withheld = got["views"], got["withheld"]
        truncated = False   # every row is kept: what one answer shows is paged (``_shown``)
        # the rows found: the profile's exact count
        found = profile["rows"] if profile.get("exact") else len(rows)
        rid = hashlib.sha256(json.dumps([_actor_key(actor), sql, built, time.time()],
                                        default=str).encode()).hexdigest()[:20]
        result = f"sql://{rid}"
        shown, cut, cut_cells = _shown(result, cols, rows, 0, profile)
        truncated = truncated or cut
        # the host holds every row of the result under its id: the
        # rest of a result is read by paging it ({"page": result, "offset": n}), and the
        # answer's references resolve against it
        RESULTS.put(rid, {"actor": _actor_key(actor), "sql": sql, "columns": cols,
                          "rows": rows,
                          "complete": not truncated and not withheld, "views": views,
                          "built": built, "sources": sources, "profile": profile})
        profile = {**profile, "shown": len(shown)}
        if withheld:
            # a cell the reader may not read is NULL in every row: its nulls count it too
            profile["nulls_include_withheld"] = True
        if len(shown) < len(rows):
            profile["page"] = (f"rows {len(shown)} to {len(rows) - 1} are held: read them "
                               f'with {{"page": "{result}", "offset": {len(shown)}}}')
        out: dict[str, Any] = {
            "result": result,
            # what this result covers and where it goes on, at its top (the host writes
            # both: the model reads them before the rows)
            "scope": result_profile.scope_line(sql, views, profile, len(shown)),
            **({"continue": {"shown": len(shown), "rows_found": found,
                             "next": {"page": result, "offset": len(shown)}}}
               if len(shown) < len(rows) else {}),
            "columns": cols, "rows": shown, "row_count": len(shown),
            "profile": profile,
            "truncated": truncated, "views": views,
            # under a replay's ceiling the data are as of the replayed moment, never the
            # tables' build time (it would tell the model the real date)
            "as_of": (_ceiling().isoformat() if _ceiling() is not None else
                      _cell(built)[0] if built is not None else None),
            "seconds": round(time.monotonic() - started, 2),
            "partial": bool(withheld), "withheld": withheld,
            # where the rows were read and as of when (the standby's replay time)
            "data_as_of": read_at}
        if sources:
            out["sources"] = len(sources)
        if truncated:
            # how much was not seen: the model reads on before resting on the unseen rows
            # (a newest-first mail read cut at its 28th of 176 rows hid the later reply)
            out["rows_found"] = found
            out["note"] = (f"only the first {len(shown)} of {found} rows are shown; the "
                           "profile's counts and sums cover the whole result. Read the "
                           "others by paging (profile.page) or with fewer or narrower "
                           "columns or a filter past the last row shown")
        if cut_cells:
            out["cells_cut"] = cut_cells
        return out

    # ------------------------------------------------------------------ describe

    def _full(self, actor: Any) -> bool:
        """Whether ``actor`` is a full reader (``people_excluded.full_reader``): its door
        also holds the classes only a full reader holds (``access_classes.FULL_KEY``)."""
        from operating.pg import people_excluded
        try:
            return bool(people_excluded.full_reader(self.base, actor))
        except Exception:  # noqa: BLE001 - undecidable: not a full reader (fail closed)
            return False

    def _described_only(self, what: Any) -> dict[str, Any]:
        """``describe`` under a read ceiling: the views' meanings, never a sample row or
        commonest value (they are today's)."""
        if isinstance(what, str) and what.startswith(ins.INSIGHT):
            return ins.described(what)
        doc = {"rules": RULES, "views": _dictionary()}
        if what is True:
            return {"rules": RULES, "views": {
                name: f"{entry['about']} Columns: {', '.join(entry['columns'])}"
                for name, entry in doc["views"].items()}, **ins.listed()}
        if isinstance(what, str) and what in doc["views"]:
            return {**doc, "views": {what: doc["views"][what]}}
        return {**doc, "views": {"records": doc["views"]["records"]},
                "note": "SELECT type, name FROM record_types lists the record types"}

    def _describe(self, what: Any, actor: Any) -> dict[str, Any]:
        if isinstance(what, str) and what.startswith(ins.INSIGHT):
            return ins.described(what)
        doc = {"rules": RULES, "views": _dictionary()}
        if what is True:
            # the list fits one result (each string is sent wrapped as untrusted text):
            # each view's purpose and column names in one line; a view's column meanings
            # and sample values are its own describe
            return {"rules": RULES, "views": {
                name: f"{entry['about']} Columns: {', '.join(entry['columns'])}"
                for name, entry in doc["views"].items()},
                "more": "plx.sql {\"describe\": \"<view>\"}: a view's column meanings, "
                        "sample rows and commonest values", **ins.listed()}
        if isinstance(what, str) and (what in GENERIC or what in COMMS_DERIVED):
            return {**doc, "views": {what: doc["views"][what]}}
        if isinstance(what, str) and what not in bv.listed() and \
                re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", what):
            return self._describe_type(what, actor, doc)
        if not isinstance(what, str) or what not in bv.listed():
            raise ToolError("unknown_view", f"no view {str(what)[:64]!r}",
                            "views: " + ", ".join(sorted(bv.listed())) + "; or a record "
                            "type record_types lists").uncovered("view", str(what))
        conn, scope = self._reader(actor)
        v = bv.VIEWS[what]
        extra = self._comms_settings(scope, actor) if v.fill else None

        def work(pg: Any) -> Any:
            sample = pg.execute(f"SELECT * FROM {what} LIMIT 3")
            cols = [d.name for d in sample.description]
            rows = [dict(zip(cols, (_cell(x)[0] for x in r))) for r in sample.fetchall()]
            if v.fill:
                # a mail / Teams / files view holds millions of rows: its commonest values and
                # count are never sampled live (``describe emails`` timed out at 10 s), they
                # are read from what the refresh measured (``biz.column_health``)
                return rows, None, None, self._health(pg, what)
            common: dict[str, Any] = {}
            ends = time.monotonic() + 4.0
            for c in v.cols:
                if c.kind not in ("text", "currency", "boolean") or time.monotonic() > ends:
                    continue
                got = pg.execute(
                    f'SELECT "{c.name}", count(*) FROM {what} WHERE "{c.name}" IS NOT NULL '
                    f"GROUP BY 1 ORDER BY 2 DESC LIMIT {SAMPLE_VALUES}").fetchall()
                common[c.name] = [[_cell(val)[0], n] for val, n in got]
            (total,) = pg.execute(f"SELECT count(*) FROM {what}").fetchone()
            return rows, common, total, None

        rows, common, total, health = self._scoped(conn, Door(reader_grant(
            scope, full=self._full(actor))), work,
                                                   extra=extra)
        if common is None:
            return {**doc, "views": {what: doc["views"][what]},
                    "sample": {"rows": rows, "column_health": health["columns"],
                               "column_health_note": health["note"]}}
        return {**doc, "views": {what: doc["views"][what]},
                "sample": {"rows_you_can_read": total, "rows": rows,
                           "commonest_values": common}}

    @staticmethod
    def _health(pg: Any, view: str) -> dict[str, Any]:
        """What the last refresh measured of ``view``'s columns (``biz.column_health``: fill,
        distinct count, cut share), or the reason it is not there: not built yet, or this
        reader's role may not read it. Never a live scan."""
        try:
            with pg.transaction():
                got = pg.execute(
                    f'SELECT "column", fill, distinct_n, cut_share, measured_at FROM '
                    f"{bv.SCHEMA}.column_health WHERE view = %s ORDER BY 1", (view,)).fetchall()
        except Exception as exc:  # noqa: BLE001 - said in the result, never a live scan
            logging.getLogger("operating.ontology_tools_sql").info(json.dumps(
                {"describe_health_unavailable": type(exc).__name__}))
            return {"columns": [], "note": "how full each column is has not been measured "
                    "for this view yet (read a few rows with LIMIT, filter on a date)"}
        if not got:
            return {"columns": [], "note": "how full each column is has not been measured "
                    "for this view yet"}
        return {"columns": [{"column": c, "fill": None if f is None else round(float(f), 4),
                             "distinct": d, "cut_share": None if k is None else round(
                                 float(k), 4), "measured": str(at)[:10]}
                            for c, f, d, k, at in got],
                "note": "measured by the last refresh on a 2% sample of the rows"}

    def _describe_type(self, what: str, actor: Any, doc: dict[str, Any]) -> dict[str, Any]:
        """One record type as this reader may read it (through the door): its catalog
        row, three records and the commonest values of its fields over the first
        ``TYPE_SAMPLE`` records of it the reader may read."""
        conn, scope = self._reader(actor)

        def work(pg: Any) -> Any:
            if pg.execute("SELECT to_regclass('records')").fetchone()[0] is None:
                return None
            cur = pg.execute("SELECT * FROM record_types WHERE type = %s", (what,))
            cols = [d.name for d in cur.description]
            got = cur.fetchone()
            if got is None:
                return None
            entry = dict(zip(cols, got))
            sample = pg.execute("SELECT handle, label, record_date, fields FROM records "
                                "WHERE type = %s ORDER BY record_date DESC NULLS LAST "
                                "LIMIT 3", (what,)).fetchall()
            common = pg.execute(
                "SELECT e.key, e.value, count(*) FROM (SELECT fields FROM records WHERE "
                f"type = %s LIMIT {TYPE_SAMPLE}) r, jsonb_each_text(r.fields) e "
                "GROUP BY 1, 2 ORDER BY 1, 3 DESC, 2", (what,)).fetchall()
            (total,) = pg.execute("SELECT count(*) FROM records WHERE type = %s",
                                  (what,)).fetchone()
            return entry, sample, common, total

        got = self._scoped(conn, Door(reader_grant(scope, full=self._full(actor))), work)
        if got is None:
            raise ToolError("unknown_view", f"no view or record type {what[:64]!r}",
                            "plx.sql {\"describe\": true} lists the views; "
                            "SELECT type, name FROM record_types lists the record types"
                            ).uncovered("view", what)
        entry, sample, common, total = got
        values: dict[str, list[Any]] = {}
        for key, value, n in common:
            if len(values.setdefault(key, [])) < SAMPLE_VALUES:
                values[key].append([value, n])
        return _fitted_type({"rules": RULES, "record_type": {
            k: (_cell(v)[0] if not isinstance(v, (list, dict)) else v)
            for k, v in entry.items()}}, total, sample, values)


# A record type's describe fits one response (``_fits_response``): a type
# whose records carry many long fields (a certificate's extracted text) gave
# result_too_large, and the reader saw none of its fields. Cut in this order, each said in
# ``trimmed``: free text (each value once) out of the commonest values, then those values
# and the sample's field texts shorter, then fewer sample rows, then fewer values per field;
# when no cut fits, the whole sample (the host then sends it once as text, or refuses).


DESCRIBE_STEPS = (("free_text", None), ("text", 160), ("rows", 1), ("text", 80),
                  ("values", 3), ("text", 40), ("values", 1))


def _fits_response(out: Mapping[str, Any]) -> bool:
    """Whether ``out`` fits one response as the host sends it (wrapped for a client,
    written twice: ``mcp_server._envelope_bytes``) and is shown whole by the client
    (``mcp_server.value_shown_fits``)."""
    from operating import mcp_server
    return mcp_server._envelope_bytes(dict(out)) <= mcp_server.ASK_RESULT_BYTES \
        and mcp_server.value_shown_fits(dict(out), TOOL_NAME)


def _short(value: Any, most: int | None) -> Any:
    cell = _cell(value)[0]
    if most is not None and isinstance(cell, dict) and len(cell["untrusted_text"]) > most:
        return {"untrusted_text": cell["untrusted_text"][:most], "truncated": True}
    return cell


def _fitted_type(head: dict[str, Any], total: int, sample: Sequence[Any],
                 values: Mapping[str, list[Any]]) -> dict[str, Any]:
    """``head`` with its sample (``sample``: (handle, label, record_date, fields) rows;
    ``values``: field -> [[value, count], ...] commonest first) cut to fit one response."""
    most: int | None = None
    rows, per, free = len(sample), SAMPLE_VALUES, False
    trimmed: list[str] = []
    steps = iter(DESCRIBE_STEPS)
    while True:
        kept = {k: [[_short(v, most), n] for v, n in vals[:per]] for k, vals in values.items()
                if not (free and vals and all(n == 1 for _, n in vals))}
        out = {**head, "sample": {
            "records_you_can_read": total,
            "rows": [{"handle": h, "label": _cell(lb)[0], "record_date": d,
                      "fields": {k: _short(v, most) for k, v in (f or {}).items()}}
                     for h, lb, d, f in sample[:rows]],
            "commonest_values": kept,
            "values_read_from": f"the first {TYPE_SAMPLE} records"}}
        if trimmed:
            out["sample"]["trimmed"] = trimmed
        if not trimmed:
            first = out
        if _fits_response(out):
            return out
        step = next(steps, None)
        if step is None:
            # no cut fits (the type's own catalog entry is the bulk): the whole sample as
            # before, for the host to send as text once or refuse
            return first
        kind, n = step
        if kind == "free_text":
            free = True
            trimmed.append("fields whose sampled values each occur once (free text) left "
                           "out of commonest_values")
        elif kind == "text":
            most = n
            trimmed.append(f"texts cut to {n} characters")
        elif kind == "rows":
            rows = min(rows, n)
            trimmed.append(f"{n} sample row")
        else:
            per = n
            trimmed.append(f"{n} commonest value(s) per field")


def _teams_windows(base: Any, actor: Any) -> dict[str, str]:
    """The reader's Teams chat windows by the host's capture (``business_comms.
    teams_windows``); none without a case-runtime item gate (no Teams message)."""
    from operating.pg import business_comms
    runtime = getattr(getattr(base, "item_gate", None), "runtime", None)
    if runtime is None:
        return {}
    try:
        return business_comms.teams_windows(runtime, actor)
    except Exception:  # noqa: BLE001 - undecidable: no Teams message (fail closed)
        return {}


def _transcript_reader(base: Any, actor: Any) -> dict[str, Any]:
    """The reader's transcript rule settings (``business_comms.transcript_reader``); none
    without a case-runtime item gate or on any failure (no transcript: fail closed)."""
    from operating.pg import business_comms
    runtime = getattr(getattr(base, "item_gate", None), "runtime", None)
    try:
        return business_comms.transcript_reader(runtime, actor) if runtime is not None \
            else {"full": False, "ids": []}
    except Exception:  # noqa: BLE001 - undecidable: no transcript (fail closed)
        return {"full": False, "ids": []}

from operating.pg.access_classes import FULL_KEY  # noqa: E402


def _people_tag() -> str:
    from operating.pg import people_excluded
    return people_excluded.current_exclusion() or ""


class Grant:
    """What a reader's door is decided from (``door.reader_scope``): its audience keys,
    marking sets and held markings, whether it holds every document marking and their
    digest (the door cache's key with the schema and the door stamp), made once per grant
    (``reader_grant``: the very objects the scope holds). The documents it may not open
    (``documents``) are read only for a door with a document-resolved class."""

    __slots__ = ("refs", "keys", "sets", "held", "every_doc", "digest")

    def __init__(self, refs: tuple, keys: tuple, sets: tuple, held: frozenset,
                 every_doc: bool, digest: str) -> None:
        self.refs, self.keys, self.sets, self.held = refs, keys, sets, held
        self.every_doc, self.digest = every_doc, digest

    def documents(self) -> tuple[tuple, bool, str]:
        """(the documents the reader may not open: marked beyond its markings and the
        PLX-issued ones held as a supplier's; whether the marked documents could be read;
        a digest of both). The marked set is read once per process and kept
        (``document_marks``: ~1 s, the first call's longest step when read for every
        reader, 2026-11-18); each list is sorted and digested once per set."""
        from operating.search import document_marks
        marked = document_marks.withheld_set(self.held)
        own = document_marks._own_issued()
        hidden, digest = _hidden((marked[0] if marked is not None else None) or _EMPTY,
                                 own or _EMPTY)
        return hidden, marked is not None, f"{digest}:{int(marked is not None)}"


class Door:
    """One call's door: ``grant``'s, decided by ``scoped_read`` in the call's own
    transaction (``decided``: ``door.reader_scope``'s answer at that snapshot's stamp)."""

    __slots__ = ("grant", "decided")

    def __init__(self, grant: Grant) -> None:
        self.grant = grant
        self.decided: dict[str, Any] = {}


_EMPTY: frozenset[str] = frozenset()
GRANTS_KEPT = 64
# reader grants by the identity of what they were made from (``reader_grant``), each
# holding those objects so no id is reused while it is kept; the hidden documents by the
# identity of their two sets (shared by every reader of one marked set)
_GRANTS: "OrderedDict[tuple[int, ...], Grant]" = OrderedDict()
_HIDDEN: "OrderedDict[tuple[int, int], tuple[tuple, tuple, str]]" = OrderedDict()
_GRANTS_LOCK = threading.Lock()


def reader_grant(scope: Any, *, full: bool = False) -> Grant:
    """``scope``'s ``Grant``, kept by the identity of the reader's grant objects
    (``reader_grants`` keeps one tuple per policy, so a session's calls hand the same
    ones): sorted and digested once, never per call (the grant and the hidden documents
    were JSON-encoded and hashed on every call, 11-16 MB for a staff reader, 2026-11-18).
    New objects (a policy decided again): made again from the values."""
    from operating.search import document_marks
    refs = (scope.audience_keys, scope.marking_sets, getattr(scope, "held", ()) or ())
    # kept by identity only when none of them can change under the entry
    kept = all(isinstance(x, (tuple, frozenset)) for x in refs)
    key = tuple(id(x) for x in refs) + (int(bool(full)),)
    with _GRANTS_LOCK:
        hit = _GRANTS.get(key) if kept else None
        if hit is not None and all(a is b for a, b in zip(hit.refs, refs)):
            _GRANTS.move_to_end(key)
            return hit
    # ``FULL_KEY`` is never a scope's own key: only a full reader's grant carries it
    keys = tuple(sorted({k for k in refs[0] if isinstance(k, str) and k != FULL_KEY}
                        | ({FULL_KEY} if full else set())))
    sets = tuple(sorted({m for m in refs[1] if isinstance(m, str)}))
    held = frozenset(h for h in refs[2] if isinstance(h, str))
    every = document_marks.holds_every(held)
    digest = hashlib.sha256(json.dumps([keys, sets, sorted(held), every]).encode()
                            ).hexdigest()
    got = Grant(refs, keys, sets, held, every, digest)
    if not kept:
        return got
    with _GRANTS_LOCK:
        _GRANTS[key] = got
        while len(_GRANTS) > GRANTS_KEPT:
            _GRANTS.popitem(last=False)
    return got


def _hidden(ids: frozenset[str], own: frozenset[str]) -> tuple[tuple, str]:
    """(the sorted hidden documents, their digest), kept by the two sets' identity."""
    key = (id(ids), id(own))
    with _GRANTS_LOCK:
        hit = _HIDDEN.get(key)
        if hit is not None and hit[0][0] is ids and hit[0][1] is own:
            return hit[1], hit[2]
    hidden = tuple(sorted(set(ids) | set(own)))
    digest = hashlib.sha256("\n".join(hidden).encode()).hexdigest()
    with _GRANTS_LOCK:
        _HIDDEN[key] = ((ids, own), hidden, digest)
        while len(_HIDDEN) > 8:
            _HIDDEN.popitem(last=False)
    return hidden, digest


def reader_door(base: Any, actor: Any) -> Door:
    """The door of what ``base`` (the host's ``OntologyTools``) grants ``actor`` under its
    policy now, decided in the reading transaction (``scoped_read``): the one place a
    reader's scope for the business views is computed (``plx.sql`` and ``plx.page``)."""
    from operating.pg import people_excluded
    try:
        full = bool(people_excluded.full_reader(base, actor))
    except Exception:  # noqa: BLE001 - undecidable: not a full reader (fail closed)
        full = False
    return Door(reader_grant(base._scope(actor, base.clock()), full=full))


def _decide(pg: Any, grant: Grant) -> dict[str, Any]:
    """``grant``'s door at this transaction's snapshot: read ONE statement (the door's
    stamp and whether it is decided against the people exclusion as it is now), then the
    door kept for (grant, schema, stamp), else ``door.reader_scope`` here, in the same
    snapshot. No door (an old-shape schema) or one behind the people exclusion: refused.
    Every writer that changes what a door decides moves the stamp in its own transaction
    (``door._bump``), so a door kept for this stamp is this snapshot's; none is kept past
    ``DOOR_MAX_AGE_SECONDS`` either way."""
    import psycopg
    from operating.pg import door
    src = bv.SCHEMA
    try:
        got = door.check(pg, src)
    except (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled,
            psycopg.errors.UndefinedTable):
        # a swap holding the door's tables or dropping one: ends within seconds
        raise Busy("the business tables are being rebuilt") from None
    except psycopg.Error:
        # anything else does not end by itself: refused at once, never waited on
        raise ToolError("not_ready", "the business tables cannot be read right now",
                        "use plx.search") from None
    if got is None:
        raise ToolError("not_ready", "the business tables are not built for plx.sql "
                        "yet", "use plx.search")
    stamp, serves = got
    if not serves:
        # fail closed: the people exclusion changed after the tables were decided
        # (the people exclusion's update follows a grown set in the set's own transaction:
        # a door behind it is one a follow failed on, decided whole within minutes, not
        # seconds: refused at once, not waited on)
        raise ToolError("not_ready", "the business tables are catching up with a change "
                        "to who is excluded", "use plx.search")
    key = (grant.digest, src, stamp)
    now = time.monotonic()
    with _DOOR_LOCK:
        hit = _DOOR.get(key)
    if hit is not None and now - hit[0] <= DOOR_MAX_AGE_SECONDS and (
            hit[2] is None or grant.documents()[2] == hit[2]):
        with _DOOR_LOCK:
            if key in _DOOR:
                _DOOR.move_to_end(key)
        return hit[1]
    used: list[str] = []

    def documents() -> tuple[tuple, bool]:
        # read only for a door with a document-resolved class (its hidden handles)
        hidden, known, digest = grant.documents()
        used.append(digest)
        return hidden, known
    decided = door.reader_scope(
        pg, src, audience_keys=grant.keys, marking_sets=grant.sets, held=grant.held,
        holds_every_doc=grant.every_doc, hidden_docs=documents)
    if _ranges_only(pg):
        # every guarded view reads the ranges: the int4[] lists of the views made before
        # them (``door.RANGE_SETTINGS``) are not sent (any view re-created moves the stamp)
        decided = {**decided, "settings": {
            k: v for k, v in decided["settings"].items()
            if k not in door.RANGE_SETTINGS and not k.startswith("plx.u_")}}
    with _DOOR_LOCK:
        # (when, the door, the documents' digest it was decided with: None, none read)
        _DOOR[key] = (now, decided, used[0] if used else None)
        while len(_DOOR) > DOORS_KEPT:
            _DOOR.popitem(last=False)
    return decided


def _ranges_only(pg: Any) -> bool:
    """Whether every guarded business view (the AI's and the person's pages') reads the
    held classes as ranges (``door.view_ddl`` now: its rule coerces the setting to
    int4multirange), none an older int4[] list. Read from the catalog's stored rules, never
    through ``pg_views``: its ``pg_get_viewdef`` locks each view, so one view held by a
    refresh's swap (dropped and made again, then minutes of lineage in that transaction)
    made every reader's door wait for the swap (11-19 sampler: plx.sql stalled 2-4 s and
    retried). A rule that does not show the coercion counts as the older form: the int4[]
    lists are then sent too, which every view reads correctly."""
    names = [n for n, v in bv.VIEWS.items() if not v.fill]
    (old,) = pg.execute(
        "SELECT count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON "
        "n.oid = c.relnamespace JOIN pg_catalog.pg_rewrite r ON r.ev_class = c.oid WHERE "
        "c.relkind = 'v' AND n.nspname = ANY(%s) AND c.relname = ANY(%s) AND position("
        "(':resulttype ' || 'pg_catalog.int4multirange'::regtype::oid || ' ') in "
        "r.ev_action::text) = 0",
        ([sql_views.guarded_schema(), sql_views.page_schema()], names)).fetchone()
    return old == 0


class _NoSlot(Exception):
    """No costly slot was free in this try (``_costly_slot``): the call's transaction ends,
    its connection goes back, and ``_query`` tries again after FULL_COSTLY_POLL_SECONDS."""


def _costly_slot(pg: Any) -> None:
    """Hold one of FULL_COSTLY_SLOTS box-wide slots for the rest of ``pg``'s transaction
    (``pg_try_advisory_xact_lock``: released at its end, whatever happens); none free:
    ``_NoSlot`` (the wait is ``_query``'s, holding no connection)."""
    for slot in range(FULL_COSTLY_SLOTS):
        if pg.execute("SELECT pg_try_advisory_xact_lock(%s, %s)",
                      (FULL_COSTLY_LOCK, slot)).fetchone()[0]:
            return
    raise _NoSlot()


def scoped_read(pg: Any, door: Door | None, work: Any, seconds: float, *,
                person: bool = False, as_role: bool = True,
                extra: Mapping[str, str] | None = None, after: Any = None,
                lock_ms: int = 2000) -> Any:
    """Run ``work(pg)`` on the idle connection ``pg`` (the caller holds it alone) in one
    REPEATABLE READ, READ ONLY transaction (one snapshot): ``door``'s door decided first
    (``_decide``: its stamp and the people exclusion read in this snapshot), its settings,
    ``extra``'s, the search path, ``seconds`` as the statement limit and the role set in
    one statement, then ``work``; afterwards the role and every setting are checked to be
    still ours (else nothing it read is returned). ``after(pg, got)``: read after that
    check as the store's own role in the same snapshot (``plx.sql``'s lineage); then
    ``(got, after's)`` is returned. The one door ``plx.sql`` and ``plx.page`` both read
    through.

    ``person``: a person's page (``plx.page``, never a model tool) reads the person-only
    views (``sql_views.page_schema``, no ``ai_ok`` filter: Freightcom rows a person may
    see) as ``plx_page_reader``; the default, every model path, reads the AI's views.

    ``as_role`` False (``SqlTool.comms_search``/``comms_open`` only: this module's fixed
    statements, never a caller's SQL) runs as the store's own role, which reads the text
    index beside the communications views; ``door`` None decides no door (the
    communications views read no door class: ``extra`` holds their settings)."""
    import psycopg
    role = sql_views.PAGE_ROLE if person else sql_views.ROLE
    where = sql_views.page_schema() if person else sql_views.guarded_schema()
    kept = (pg.isolation_level, pg.read_only)
    try:
        # BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY (a read-only one never fails to
        # serialize): the door check and the query read one snapshot
        pg.isolation_level, pg.read_only = psycopg.IsolationLevel.REPEATABLE_READ, True
        with pg.transaction():
            settings: dict[str, str] = {}
            if door is not None:
                door.decided = _decide(pg, door.grant)
                settings.update(door.decided["settings"])
            settings.update(extra or {})
            names = list(settings)
            local = [*names, "search_path", "statement_timeout", "lock_timeout",
                     *(["role"] if as_role else [])]
            # plx_ops: the text[] @> / && a guarded view's filter may run before its guard
            # (migration 0051; an absent schema is passed over, the filters then slower)
            values = [*(settings[n] for n in names), where + ", plx_ops, pg_catalog",
                      str(int(seconds * 1000)), str(int(lock_ms)), *([role] if as_role else [])]
            pg.execute("SELECT " + ", ".join("set_config(%s, %s, true)" for _ in local),
                       [x for pair in zip(local, values) for x in pair])
            got = work(pg)
            row = pg.execute(
                "SELECT current_user, current_setting('transaction_read_only'), "
                + ", ".join("current_setting(%s, true)" for _ in names),
                names).fetchone()
            if (as_role and row[0] != role) or row[1] != "on" or \
                    list(row[2:]) != [settings[n] for n in names]:
                raise ToolError("integrity", "the statement changed its own scope; "
                                "nothing it read is returned")
            if after is None:
                return got
            if as_role:
                pg.execute("SET LOCAL ROLE NONE")
            return got, after(pg, got)
    except psycopg.errors.QueryCanceled:
        raise ToolError("timeout", f"the query ran past {seconds:.0f} s" if seconds else
                        "the query was cancelled",
                        "narrow it (where on a date or a key), or aggregate") \
            from None
    except psycopg.errors.InsufficientPrivilege:
        raise ToolError("not_readable", "the statement reads something outside "
                        "the business views", "plx.sql {\"describe\": true} "
                        "lists what is readable") from None
    except psycopg.errors.UndefinedTable as exc:
        leaf = (_MISSING_RELATION.findall(_first_line(exc)) or [""])[0].rsplit(".", 1)[-1]
        if leaf.lower() in COMMS_DERIVED_ABOUT:   # made by the first reading run: typed
            raise ToolError("not_ready", f"{leaf} is not built yet: the message readings it is "
                            "made from have not been read in", "ask again later") from None
        raise ToolError("unknown_view", _first_line(exc),
                        "plx.sql {\"describe\": true} lists the views").uncovered(
                            "view", leaf or _first_line(exc)) from None
    except (psycopg.errors.SyntaxError, psycopg.errors.UndefinedColumn,
            psycopg.errors.UndefinedFunction, psycopg.errors.DatatypeMismatch,
            psycopg.errors.GroupingError, psycopg.errors.AmbiguousColumn,
            psycopg.errors.InvalidTextRepresentation,
            psycopg.errors.InvalidDatetimeFormat,
            psycopg.errors.DivisionByZero) as exc:
        raise ToolError("sql_error", _first_line(exc),
                        "fix the statement and send it again") from None
    except psycopg.errors.ReadOnlySqlTransaction:
        raise _bad("plx.sql only reads") from None
    except (psycopg.errors.LockNotAvailable, psycopg.errors.InternalError_) as exc:
        # a refresh swapping the tables under the statement (its lock held past
        # lock_timeout, or the old table dropped after the plan opened it: "could not open
        # relation with OID"): ends within seconds, so ``SqlTool._scoped`` asks again
        if isinstance(exc, psycopg.errors.LockNotAvailable) or _SWAPPED.search(str(exc)):
            raise Busy("the business tables were being swapped") from None
        raise ToolError("query_failed", f"the store refused the query "
                        f"({type(exc).__name__}: {_first_line(exc)})") from None
    except psycopg.errors.SerializationFailure:
        # a read-only snapshot never fails to serialize: this is a standby replaying the
        # primary's changes under the statement ("conflict with recovery"), which ends
        # by itself, so ``SqlTool._scoped`` asks again
        raise Busy("the standby was applying the primary's changes") from None
    except psycopg.Error as exc:
        raise ToolError("query_failed", f"the store refused the query "
                        f"({type(exc).__name__}: {_first_line(exc)})") from None
    finally:
        if not pg.closed and pg.info.transaction_status == 0:   # idle: restorable
            pg.isolation_level, pg.read_only = kept


# what a statement meets when a table it planned on was dropped by a swap since
_SWAPPED = re.compile(r"could not open relation with OID|cache lookup failed for relation")


_MISSING_RELATION = re.compile(r'relation "([^"]+)" does not exist')


def _first_line(exc: Exception) -> str:
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0][:300]


DOORS_KEPT = 256
# a reader's door is decided again at least this often, whatever the stamp says
DOOR_MAX_AGE_SECONDS = 120

# DOOR v2 reader doors: (grant digest, schema, stamp) -> (when decided, door.reader_scope's
# answer, the digest of the documents it read: None when it read none)
_DOOR: "OrderedDict[tuple[str, str, int], tuple[float, dict[str, Any], str | None]]" = \
    OrderedDict()
_DOOR_LOCK = threading.Lock()


# the communications views' settings (``SqlTool._comms_settings``): beside the door's, never
# part of the door
COMMS_SETTINGS = frozenset({"plx.reader_audience", "plx.reader_marks", "plx.reader_hidden",
                            "plx.reader_people", "plx.reader_teams", "plx.reader_self"})


# Column types read as they are (bounded in size); every other column comes back as text
# text, whole (owner 11-23: no cell is cut; a long cell is paged for display, see
# ``ontology_tools_sql_rows.CELL_PAGE``), and rows stream from a server-side cursor in
# batches into the result store.
_BOUNDED_CATEGORIES = frozenset("NBDT")
FETCH_BATCH = 50


# pg_type categories of the built-in types (oids below FirstNormalObjectId: the same in
# every database, never changed); any other type is looked up per call
_BUILTIN_OID_END = 16384
_CATEGORIES: dict[int, str] = {}


def _type_categories(pg: Any, oids: set[int]) -> dict[int, str]:
    kinds = {o: _CATEGORIES[o] for o in oids if o in _CATEGORIES}
    wanted = sorted(oids - set(kinds))
    if wanted:
        for oid, kind in pg.execute("SELECT oid::int8, typcategory::text FROM "
                                    "pg_catalog.pg_type WHERE oid = ANY(%s::oid[])",
                                    (wanted,)).fetchall():
            kinds[int(oid)] = kind
            if int(oid) < _BUILTIN_OID_END:
                _CATEGORIES[int(oid)] = kind
    return kinds


def _fetch(pg: Any, sql: str) -> tuple[list[str], RowStore, bool, dict[str, Any]]:
    """``sql``'s columns and EVERY row of its result, run once in this snapshot and kept in a
    ``RowStore`` (memory up to ``HELD_ROWS``, then disk): nothing is cut by a row count or a
    byte count. Returns the columns, the store, False (nothing stopped the read: the name
    kept for the caller) and the host's profile of the whole result (``result_profile``:
    exact counts, NULLs, distinct values, sums per currency; from the rows when they are few,
    else one aggregate statement over the same query)."""
    head = pg.execute(f"SELECT * FROM (\n{sql}\n) AS plx_q LIMIT 0")
    described = head.description
    cols = [d.name for d in described]
    kinds = _type_categories(pg, {int(d.type_code) for d in described})
    found = result_profile.origins(pg, head)
    names = [f"plx_c{i}" for i in range(len(cols))]
    shown = []
    for name, d in zip(names, described):
        kind = kinds.get(int(d.type_code), "X")
        if kind in _BOUNDED_CATEGORIES:
            shown.append(name)
        elif kind == "A":
            shown.append(f"CASE WHEN {name} IS NULL THEN NULL ELSE ARRAY(SELECT e::text "
                         f"FROM unnest({name}) e) END AS {name}")
        else:
            shown.append(f"{name}::text AS {name}")
    every = (f"SELECT {', '.join(shown) or '*'} FROM (\n{sql}\n) AS plx_q"
             f"{'(' + ', '.join(names) + ')' if names else ''}")
    store = RowStore()
    try:
        with pg.cursor(name=f"plx_sql_{threading.get_ident()}") as cur:
            cur.execute(every)
            while True:
                batch = cur.fetchmany(FETCH_BATCH)
                if not batch:
                    break
                store.extend(batch)
        cats = [{"N": result_profile.NUMBER, "D": result_profile.DATE}.get(
            kinds.get(int(d.type_code), "X"), result_profile.OTHER) for d in described]
        # the profile reads the rows when all are in memory, else one aggregate statement
        profile = _profiled(pg, sql, cols, store.held(), store.spilled(), cats, found, HELD_ROWS)
    except DiskRefused as why:
        store.close()
        raise ToolError("disk_full", str(why), "narrow the statement, or ask again later") from None
    except BaseException:
        store.close()
        raise
    return cols, store, False, {**profile, **result_profile.statement_facts(sql, profile)}


VISIBLE_HANDLES = 5000   # handles asked of a view per statement (every handle is asked)


def _visible(pg: Any, views: list[str], cols: list[str], rows: Any
             ) -> dict[str, list[str]]:
    """Per view read, the result's handles that view shows this reader (same scoped
    transaction): a literal handle of a row the reader cannot read has no sources. EVERY row
    of the result is checked, in statements of ``VISIBLE_HANDLES`` handles."""
    if "handle" not in cols or not views:
        return {}
    at = cols.index("handle")
    handles = sorted({r[at] for r in rows if isinstance(r[at], str)})
    if not handles:
        return {}
    out: dict[str, list[str]] = {}
    for v in views:
        if v not in bv.VIEWS:
            continue
        seen: set[str] = set()
        for i in range(0, len(handles), VISIBLE_HANDLES):
            seen.update(h for (h,) in pg.execute(
                f"SELECT DISTINCT handle FROM {v} WHERE handle = ANY(%s::text[])",
                (handles[i:i + VISIBLE_HANDLES],)).fetchall())
        out[v] = sorted(seen)
    return out


def _sources(pg: Any, seen: Mapping[str, list[str]]) -> list[dict[str, Any]]:
    """DOOR v2: the records behind the handles the reader got from the views
    (``_visible``: a handle the query only wrote out as a literal gets none), read from
    ``lineage`` after the query (its own transaction, as the store's role, once the scope
    check passed), never by it: what the answer's Sources line opens."""
    pairs = [(v, h) for v, hs in seen.items() for h in hs]
    if not pairs:
        return []
    from operating.pg import door
    got = door.lineage(pg, bv.SCHEMA, pairs)
    return [{"view": v, "handle": h, **got[(v, h)]} for v, h in pairs if (v, h) in got]


def _built(pg: Any, views: list[str]) -> str | None:
    """When the oldest view read was built (the views are rebuilt every 30 minutes)."""
    if not views:
        return None
    got = pg.execute("SELECT min(built_at) FROM view_state WHERE view = ANY(%s)",
                     (views,)).fetchone()
    return got[0] if got else None


from operating.ontology_tools_sql_catalog import (  # noqa: F401  (re-exported)
    RULES, RULES_GENERIC, RULES_TAIL, RULES_BASE, GENERIC_ABOUT, GENERIC, COMMS_DERIVED_ABOUT,
    COMMS_DERIVED, GENERIC_TABLES, TYPE_SAMPLE, _CATALOG, CATALOG_KEYS, catalog_keys,
    catalog_lines, _SERVED, SERVED_RECHECK_SECONDS, served_views, _dictionary)


def register(table: dict[str, dict[str, Any]], base: Any) -> SqlTool:
    tool = SqlTool(base)
    if TOOL_NAME in table:
        raise ValueError(f"tool already registered: {TOOL_NAME}")
    table[TOOL_NAME] = dict(TOOL, handler=lambda arguments, actor: tool.call(
        TOOL_NAME, arguments, actor))
    # the simple Ask's plx.search / plx.open over mail and Teams (``ontology_tools_search``)
    table[COMMS_SEARCH] = {"name": COMMS_SEARCH, "handler": lambda arguments, actor:
                           tool.guarded(tool.comms_search, arguments, actor)}
    table[COMMS_OPEN] = {"name": COMMS_OPEN, "handler": lambda arguments, actor:
                         tool.guarded(tool.comms_open, arguments, actor)}
    table[RECORDS_SEARCH] = {"name": RECORDS_SEARCH, "handler": lambda arguments, actor:
                             tool.guarded(tool.records_search, arguments, actor)}
    return tool


__all__ = ["COMMS_OPEN", "COMMS_SEARCH", "RECORDS_SEARCH", "TOOL", "TOOL_NAME", "TOOL_NAMES", "Door", "Grant", "SqlTool",
           "RESULTS", "check", "reader_door", "reader_grant", "register", "scoped_read"]
