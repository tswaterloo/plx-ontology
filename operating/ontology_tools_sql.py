"""``plx.sql``: one read-only SQL ``SELECT`` per call over the reader-scoped business views
(``<biz schema>_sql.<view>``, ``operating.pg.sql_views``), its rows returned (at most
``MAX_ROWS``), so an agent can look at the data, refine and check its own totals the way
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
call for up to ``BUSY_WAIT_SECONDS`` (a psql session would wait on the lock), never past
the call's own limit; after the read the
tool checks the role and every setting are still its own and refuses the result otherwise.

The text is checked before it is sent (fail closed): one statement starting SELECT or WITH;
no ``;`` (one trailing is dropped), comment, dollar or escape string, parameter, or
statement keyword (DDL, DML, COPY, SET, transaction control, locking ``FOR UPDATE``);
no ``pg_*`` or ``information_schema`` name and no schema but the views'; every function
called is one of ``FUNCTIONS`` (plain SQL arithmetic, text, date, aggregate, window and
full-text functions: no ``set_config``, ``current_setting``, ``query_to_xml``, ``dblink``,
file or large-object function). The plan's cost is checked with EXPLAIN first
(``MAX_COST``); ``statement_timeout`` is ``STATEMENT_MS``; the answer is cut at
``MAX_ROWS`` rows and ``MAX_BYTES`` of text (``truncated``).

The answer names each row ``sql://<result id>#row=<n>`` and the result ``sql://<result
id>``: the query, the reader and the views it read are kept (``RESULTS``) so the answer
gate can bind a stated figure to the cell that holds it."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from operating.ontology_tools_identity import ToolError
from operating import read_ceiling
from operating.pg import business_views as bv
from operating.pg import sql_views

TOOL_NAME = "plx.sql"
TOOL_NAMES = (TOOL_NAME,)
# internal (never in the MCP catalog): plx.search / plx.open's mail and Teams path
COMMS_SEARCH = "plx.sql_comms_search"
COMMS_OPEN = "plx.sql_comms_open"
RECORDS_SEARCH = "plx.sql_records_search"   # plx.search's records leg (ontology_tools_search)
MAX_ROWS = 200
MAX_SQL = 8000
# the rows' text, measured as plx.sql writes it; the host sends a result twice (text and
# structured content, ``mcp_server._envelope_bytes``) within MAX_RESULT_BYTES (48,000):
# past ~23,000 here a whole result was refused (result_too_large), none of it shown
MAX_BYTES = 20_000
# a comms search whose every-word match finds fewer rows than this (and than its limit)
# fills its page with rows holding some of the words and rows close in meaning
# (``_comms_widened``)
WIDEN_BELOW = 3
# any other first page with query words also shows up to this many rows close in meaning
# that the word match did not list ("mouth rinse": 30 label files hold both words, the
# "Oral Rinse" mail is close in meaning)
MEANING_EXTRA = 5
# the meaning ranking's best objects read for it
MEANING_K = 40
# the longest the search waits, past its word match, for the meaning ranking it started
# beside it (live: the ranking 1.9-2.3 s, the word match 4-6 s)
MEANING_WAIT_S = 5.0
# a plx.search hit list's most (its contract's maxItems)
HITS_MAX = 50
MAX_CELL = 400
STATEMENT_MS = 10_000
# a FULL reader (no permission checks
# but the people exclusion and Freightcom, both inside the views) reads as an analyst at a
# psql prompt does: statements up to FULL_STATEMENT_MS, no plan-cost cap and no records
# scan guard (the ceiling run 11-18: plain Claude with 60 s psql scored 82/87 where the
# Ask, held to 10 s and a cost cap, scored 58)
FULL_STATEMENT_MS = 60_000
# plx.search's own legs (mail/Teams text, records) for a full reader: inside plx.search's
# 40 s call, leaving the documents leg its time (a common word over 3M mails took >10 s)
SEARCH_FULL_MS = 25_000
# plx.search's records leg: the label index read for its candidates (``_label_candidates``;
# a word the index finds: ~0.2 s live over 17M records). Past it, every label is read
# through the door as before.
CANDIDATE_MS = 3_000
# A third waits up to FULL_COSTLY_WAIT_SECONDS for a slot (holding no connection), then is
# told to narrow it
FULL_COSTLY_SLOTS = 2
FULL_COSTLY_LOCK = 0x706C7873
FULL_COSTLY_WAIT_SECONDS = 20.0
FULL_COSTLY_POLL_SECONDS = 0.5
MAX_COST = 5e7
SAMPLE_VALUES = 6
RESULTS_KEPT = 512
# a ``Busy`` refusal (a view swapped or re-created, the door re-decided after the people
# exclusion moved) is asked again every BUSY_POLL_SECONDS for up to this long in one call
BUSY_WAIT_SECONDS = 20.0
BUSY_POLL_SECONDS = 2.0

_CONTRACT = json.loads((Path(__file__).with_name("ontology_tool_contracts.json"))
                       .read_text(encoding="utf-8"))["tools"][TOOL_NAME]
DESCRIPTION: str = _CONTRACT["description"]
INPUT_SCHEMA: dict[str, Any] = _CONTRACT["input"]
TOOL: dict[str, Any] = {"name": TOOL_NAME, "description": DESCRIPTION,
                        "inputSchema": INPUT_SCHEMA, "annotations": {"readOnlyHint": True}}

FUNCTIONS = frozenset("""
count sum avg min max stddev stddev_samp stddev_pop variance var_samp var_pop
percentile_cont percentile_disc mode string_agg array_agg bool_and bool_or every
round trunc ceil ceiling floor abs sign mod power sqrt exp ln log div width_bucket
greatest least coalesce nullif date_trunc date_part extract age make_date make_interval
justify_days justify_interval isfinite to_char to_date to_number to_timestamp now
lower upper initcap length char_length trim btrim ltrim rtrim substring substr left right
position strpos replace split_part concat concat_ws reverse format translate
regexp_replace regexp_match regexp_matches regexp_count regexp_substr starts_with
cardinality array_length array_position array_remove array_to_string string_to_array
unnest generate_series row_number rank dense_rank percent_rank cume_dist ntile lag lead
first_value last_value nth_value to_tsvector to_tsquery plainto_tsquery phraseto_tsquery
websearch_to_tsquery ts_rank ts_rank_cd ts_headline
jsonb_each_text jsonb_object_keys jsonb_array_elements_text jsonb_typeof jsonb_array_length
jsonb_extract_path_text
corr covar_pop covar_samp regr_slope regr_intercept regr_r2 regr_count regr_avgx regr_avgy
""".split())
# words a "(" may follow that are SQL syntax, never a call
SYNTAX = frozenset("""
in exists any all some over filter within as on and or not from join using by where
select then else when case lateral row array values is between like ilike similar
distinct group with union intersect except having limit offset order partition rows
range groups nulls asc desc end inner left right full outer cross natural escape to at
zone recursive materialized only fetch first next ties cast interval
""".split())
STATEMENT_WORDS = frozenset("""
insert update delete merge create alter drop truncate grant revoke copy call do execute
prepare deallocate listen notify unlisten lock vacuum analyze analyse cluster reindex
refresh set reset show begin commit rollback savepoint release discard load import into
returning checkpoint security reassign abort start transaction
""".split())
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LOCKING = re.compile(r"\bfor\s+(update|share|no\s+key|key\s+share)\b")


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


def _bad(message: str, hint: str | None = None) -> ToolError:
    return ToolError("invalid_sql", message, hint)


def _tokens(sql: str) -> list[tuple[str, str]]:
    """(kind, text) tokens: word, qident (quoted identifier, its text), string, punct."""
    out, i, n = [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
        elif sql.startswith("--", i) or sql.startswith("/*", i):
            raise _bad("comments are not allowed", "send the query alone")
        elif ch == "$":
            raise _bad("$ (parameters, dollar-quoted strings) is not allowed")
        elif ch == "\\":
            raise _bad("backslash is not allowed")
        elif ch == "'":
            if out and out[-1][0] == "word" and out[-1][1].lower() in ("e", "u&", "u") \
                    and sql[i - 1] not in " \t\r\n(":
                raise _bad("escape strings (E'...', U&'...') are not allowed")
            j = i + 1
            while True:
                j = sql.find("'", j)
                if j < 0:
                    raise _bad("unterminated string")
                if sql.startswith("''", j):
                    j += 2
                    continue
                break
            out.append(("string", sql[i + 1:j].replace("''", "'")))
            i = j + 1
        elif ch == '"':
            j = sql.find('"', i + 1)
            if j < 0:
                raise _bad("unterminated quoted name")
            name = sql[i + 1:j]
            if '"' in name or not name:
                raise _bad("quoted names may not hold quotes")
            out.append(("qident", name))
            i = j + 1
        elif ch == "&" and out and out[-1] == ("word", "U") or ch == "&" and out \
                and out[-1] == ("word", "u"):
            raise _bad("U& escapes are not allowed")
        elif _WORD.match(sql, i):
            m = _WORD.match(sql, i)
            out.append(("word", m.group(0)))
            i = m.end()
        elif sql.startswith("::", i):
            out.append(("punct", "::"))
            i += 2
        else:
            out.append(("punct", ch))
            i += 1
    return out


def _column_list(tokens: list[tuple[str, str]], idx: int) -> bool:
    """Whether ``name (`` at ``idx`` is a column list, never a call: a CTE's
    (``WITH r(n) AS (``, after WITH, RECURSIVE or a comma) or a FROM item's alias without
    AS (``unnest(x) f(a)``, ``customers c(a, b)``). The parentheses must hold bare names
    separated by commas; in either place a call is not valid SQL, so nothing runs there."""
    j, names = idx + 2, 0
    while j < len(tokens):
        kind, tok = tokens[j]
        if kind not in ("word", "qident"):
            return False
        names += 1
        j += 1
        if j < len(tokens) and tokens[j] == ("punct", ","):
            j += 1
            continue
        break
    if not names or j >= len(tokens) or tokens[j] != ("punct", ")"):
        return False
    after = tokens[j + 1:j + 4]
    prev = tokens[idx - 1] if idx else ("", "")
    low = [(k, t.lower()) for k, t in after]
    if prev in (("punct", ","),) or (prev[0] == "word" and prev[1].lower() in (
            "with", "recursive")):
        # AS [NOT] [MATERIALIZED] ( : only a CTE reads like that
        rest = [t for k, t in low if k == "word"]
        if low[:1] == [("word", "as")] and (
                low[1:2] == [("punct", "(")] or low[1:3] == [("word", "materialized"),
                                                              ("punct", "(")]
                or rest[1:3] == ["not", "materialized"]):
            return True
        return False
    return prev == ("punct", ")") or (prev[0] in ("word", "qident")
                                      and prev[1].lower() in bv.VIEWS)


def check(sql: Any, *, schemas: frozenset[str] = frozenset(), allowed_schema: str = ""
          ) -> str:
    """The statement as it will run (one trailing ``;`` dropped), or ``invalid_sql``."""
    if not isinstance(sql, str) or not sql.strip():
        raise _bad("sql is one SELECT statement as text")
    if len(sql) > MAX_SQL:
        raise _bad(f"the statement is longer than {MAX_SQL} characters")
    if "\x00" in sql:
        raise _bad("the statement holds a NUL character")
    text = sql.strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    tokens = _tokens(text)
    if not tokens or tokens[0][0] != "word" or tokens[0][1].lower() not in ("select", "with"):
        raise _bad("only a SELECT (or WITH ... SELECT) is allowed")
    flat = " ".join(t.lower() for k, t in tokens if k == "word")
    if _LOCKING.search(flat):
        raise _bad("locking clauses (FOR UPDATE / SHARE) are not allowed")
    for idx, (kind, tok) in enumerate(tokens):
        if kind == "punct" and tok == ";":
            raise _bad("one statement per call: ';' is not allowed")
        if kind not in ("word", "qident"):
            continue
        low = tok.lower()
        if kind == "word" and low in STATEMENT_WORDS:
            raise _bad(f"'{tok}' is not allowed: plx.sql reads with one SELECT")
        if low.startswith("pg_") or low == "information_schema" or low.startswith("reg") \
                and low in ("regclass", "regproc", "regprocedure", "regoper", "regoperator",
                            "regnamespace", "regrole", "regtype", "regconfig",
                            "regdictionary", "regcollation"):
            raise _bad(f"system catalog names ('{tok}') are not allowed")
        nxt = tokens[idx + 1] if idx + 1 < len(tokens) else ("", "")
        prev = tokens[idx - 1] if idx else ("", "")
        if nxt == ("punct", ".") and low in schemas and low != allowed_schema:
            raise _bad(f"schema '{tok}' is not readable: read the views by their bare names",
                       "plx.sql {\"describe\": true} lists them")
        if nxt == ("punct", "(") and prev != ("punct", "::") and \
                not (prev[0] == "word" and prev[1].lower() == "as") and \
                not _column_list(tokens, idx):
            if kind == "word" and low in SYNTAX:
                continue
            if low not in FUNCTIONS:
                raise _bad(f"function '{tok}' is not allowed",
                           "allowed: plain aggregate, arithmetic, text, date, window and "
                           "full-text functions")
    return text


class _Results:
    """The results answered lately (bounded): id -> what the gate needs to bind to them."""

    def __init__(self, most: int = RESULTS_KEPT) -> None:
        self.most, self._lock, self._got = most, threading.Lock(), OrderedDict()

    def put(self, rid: str, entry: dict[str, Any]) -> None:
        with self._lock:
            self._got[rid] = entry
            self._got.move_to_end(rid)
            while len(self._got) > self.most:
                self._got.popitem(last=False)

    def get(self, rid: str) -> dict[str, Any] | None:
        with self._lock:
            return self._got.get(rid)


RESULTS = _Results()


def _actor_key(actor: Any) -> str:
    return json.dumps([getattr(actor, "tenant", None),
                       sorted(getattr(actor, "principals", ()) or ())], default=str)


# A cell the contract types bare (``cells`` items, ontology_tool_contracts.json): a
# number, an ISO date or time, or one whitespace-free code (currency, status, document
# number, handle). Anything else (names, descriptions, notes) leaves as an envelope.
CELL_PATTERN = (r"-?[0-9]{1,30}(?:\.[0-9]{1,30})?"
                r"|[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2}"
                r"(?:\.[0-9]{1,9})?)?(?:Z|[+-][0-9]{2}:?[0-9]{2})?)?"
                r"|[A-Za-z0-9][A-Za-z0-9_.:/@+=!,#%?&~-]{0,255}")
_CODE = re.compile(f"(?:{CELL_PATTERN})")


def _cell(value: Any) -> tuple[Any, bool]:
    """A result cell as the tools state it (number as Decimal text, date ISO, source text
    in an ``untrusted_text`` envelope; a code-like word bare) and whether it was cut."""
    if value is None or isinstance(value, bool):
        return value, False
    if isinstance(value, (int, Decimal)):
        return format(Decimal(value), "f"), False
    if isinstance(value, float):
        return format(Decimal(repr(value)), "f"), False
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat(), False
    if isinstance(value, (list, tuple)):
        got = [_cell(v) for v in value[:50]]
        return [g for g, _ in got], len(value) > 50 or any(c for _, c in got)
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if _CODE.fullmatch(text):
        return text, False
    cut = len(text) > MAX_CELL
    env: dict[str, Any] = {"untrusted_text": text[:MAX_CELL]}
    if cut:
        env["truncated"] = True
    return env, cut


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
            pool = _POOLS[dsn] = Pool(dsn, max_size=POOL_SIZE, timeout=5.0,
                                      configure=configure, connect_timeout=5,
                                      statement_timeout_ms=STATEMENT_MS)
        return pool


class _lent:
    """A connection of plx.sql's own pool for one call (the store's DSN); a store with no
    DSN: its own connection, held under its lock for the call."""

    def __init__(self, conn: Any) -> None:
        self.conn, self._cm = conn, None

    def __enter__(self) -> Any:
        dsn = getattr(self.conn, "dsn", None)
        if isinstance(dsn, str) and dsn:
            from operating.pg.pool import PoolTimeout
            self._cm = _pool(dsn).connection()
            try:
                return self._cm.__enter__()
            except PoolTimeout:
                self._cm = None
                raise ToolError("busy", f"{POOL_SIZE} plx.sql calls are already running",
                                "send this one again when one returns") from None
        self.conn._lock.acquire()
        return self.conn._pg

    def __exit__(self, *exc: Any) -> Any:
        if self._cm is not None:
            return self._cm.__exit__(*exc)
        self.conn._lock.release()
        return None


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
            return {"error": error}

    def _call(self, args: dict[str, Any], actor: Any) -> dict[str, Any]:
        unknown = sorted(set(args) - set(INPUT_SCHEMA["properties"]))
        modes = [k for k in ("sql", "describe", "measure") if k in args]
        loose = [k for k in ("by", "days", "compare") if k in args]
        if unknown or len(modes) != 1 or (loose and modes != ["measure"]):
            raise ToolError("invalid_request", "send {\"sql\": \"SELECT ...\"}, "
                            "{\"describe\": true | \"<view>\"} or {\"measure\": true | "
                            "\"<id>\"} (by, days and compare go with measure only)")
        if "measure" in args:
            return self._measure(args, actor)
        if "describe" in args:
            if _ceiling() is not None:
                return self._described_only(args["describe"])
            return self._describe(args["describe"], actor)
        conn, scope = self._reader(actor)
        moment = _ceiling()
        guarded = sql_views.guarded_schema()
        sql = check(args["sql"], schemas=self._schema_names(conn),
                    allowed_schema="" if moment is not None else guarded)
        if moment is not None:
            # a replay of a past moment (``read_ceiling``): the views read as then
            sql = read_ceiling.bounded_sql(sql, moment, guarded)
        return self._query(conn, scope, sql, actor)

    def _measure(self, args: dict[str, Any], actor: Any) -> dict[str, Any]:
        """``{"measure": ...}`` (``operating.measures``): true lists the catalogue's
        definitions (no data read); an id runs that entry's own statement exactly as a
        ``{"sql": ...}`` call would (same check, ceiling and door) and returns its
        definition beside the rows."""
        from operating import measures

        if args["measure"] is True:
            return measures.listing(full=self._full(actor))
        made, entry = measures.statement(args["measure"], args.get("by"), args.get("days"),
                                         args.get("compare", False))
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
        out = self._query(conn, scope, sql, actor)
        if "error" not in out:
            out["measure"] = entry
        return out

    # ------------------------------------------------------------------ reader

    def _reader(self, actor: Any) -> tuple[Any, Any]:
        """(the store's connection, the reader's scope now: ``base._scope``, the grant its
        door is decided from in each call's own transaction, ``reader_grant``)."""
        conn = getattr(getattr(self.base, "store", None), "conn", None)
        if conn is None or not getattr(conn, "postgres", False):
            raise ToolError("not_served", "plx.sql reads the Postgres store only",
                            "use plx.table")
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
        docs = {k: list(json.loads(v)) for k, v in document_marks.params(scope.held).items()
                if k in ("dm_ids", "dm_own")}
        settings = {   # the keys of ``COMMS_SETTINGS``
            "plx.reader_audience": json.dumps(sorted(scope.audience_keys)),
            "plx.reader_marks": json.dumps(sorted(scope.marking_sets)),
            "plx.reader_hidden": json.dumps(sorted(set(docs.get("dm_ids") or ())
                                                   | set(docs.get("dm_own") or ()))),
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
        """A statement's time: ``full`` True: FULL_STATEMENT_MS; a number: that many ms;
        else STATEMENT_MS; never past what is left of the call."""
        from operating import call_limits
        left = call_limits.seconds_left()
        if left is not None and left <= 0.5:
            raise ToolError("timeout", "no time is left in this call for plx.sql")
        ms = FULL_STATEMENT_MS if full is True else (
            full if isinstance(full, int) and not isinstance(full, bool) and full > 0
            else STATEMENT_MS)
        top = ms / 1000.0
        return top if left is None else max(0.5, min(top, left - 0.5))

    def _scoped(self, conn: Any, door: "Door | None", work: Any, *, role: bool = True,
                extra: Mapping[str, str] | None = None, after: Any = None,
                full: "bool | int" = False) -> Any:
        """``scoped_read`` on ONE connection of plx.sql's own pool for the whole call (the
        door decided, the query, its lineage). ``role`` False: this module's own fixed
        statements (``comms_search``), which read the text index beside the guarded views
        as the store's own role; the rows still come only through the guarded views and
        their settings. ``door`` None: no door (comms reads no door class:
        ``_comms_reader``)."""
        from operating import call_limits
        waited_until = time.monotonic() + BUSY_WAIT_SECONDS
        while True:
            seconds = self._seconds(full)
            try:
                with _lent(conn) as pg:
                    return scoped_read(pg, door, work, seconds, as_role=role, extra=extra,
                                       after=after)
            except Busy:
                # ask again only while the wait and a whole statement still fit the call
                left = call_limits.seconds_left()
                now = time.monotonic()
                if now + BUSY_POLL_SECONDS > waited_until or (
                        left is not None
                        and left - BUSY_POLL_SECONDS < STATEMENT_MS / 1000.0 + 1.0):
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

        def work(pg: Any) -> Any:
            views = [v for v in cs.present(pg, guarded) if cs.VIEWS[v][2] in wanted
                     and (files or v not in cs.SEARCH_ONLY)]
            who = cs.party(args["participant"], pg, biz) if args.get("participant") else None
            sql = cs.search_sql(views, guarded=guarded, biz=biz, query=q is not None,
                                party_kind=who.kind if who else None, since=since is not None,
                                until=until is not None,
                                cursor=cur is not None and cur[2] is not None,
                                cursor_tier=cur[0] if cur and cur[0] is not None else 0)
            if not sql:
                return views, who, []
            return views, who, pg.execute(sql, {
                "q": q, "party": who.value if who else None, "since": since, "until": until,
                "c_at": cur[1] if cur else None, "c_h": cur[2] if cur else None,
                "k": k, "k_own": own, "walk": cs.walk_rows(pg, q)}).fetchall()
        # a first page's meaning ranking runs beside the word search (``_comms_widened``)
        ranking = (self._meaning_started(args["query"], actor, wanted)
                   if words and cur is None else None)
        try:
            views, who, rows = self._comms_scoped(conn, settings, work, actor)
        except cs.SearchInputError as exc:
            raise ToolError("invalid_input", str(exc)) from None
        if not views:
            raise ToolError("not_served", "mail and Teams are not served as tables here")
        rows, nxt = self._gated_files(rows, actor, limit, k, cur)
        more = nxt is not None
        documents = cs.hits(rows, words=words, party_=who)
        searched = cs.searched(views, args, words)
        few = not more and len(rows) < min(limit, WIDEN_BELOW)
        room = min(max(limit - len(rows) if few else 0,
                       MEANING_EXTRA if ranking is not None else 0), HITS_MAX - len(rows))
        if words and cur is None and room > 0:
            # few rows hold every word: the rows holding some of them; and, on any first
            # page, the rows close in meaning; all through the same views
            # (``_comms_widened``)
            extra, how = self._comms_widened(
                conn, settings, guarded, actor, views, args["query"], words, who,
                since, until, room, {r[1] for r in rows}, ranking, few)
            documents += cs.hits(extra, words=words, party_=who)
            for hit, (held, near) in zip(documents[len(rows):], how or []):
                hit["match"] = held
                if near is not None:
                    # the meaning ranking's place: kept here only (``plx.search`` shows
                    # ``match``)
                    hit["meaning_rank"] = near
            if how:
                searched["widened"] = (
                    f"fewer than {WIDEN_BELOW} items held every word: also items holding "
                    "some of the words or close in meaning, each with its match" if few else
                    f"also up to {MEANING_EXTRA} items close in meaning that the word "
                    "match did not list, each with its match")
        out = {"documents": documents,
               "searched": searched, "gaps": [],
               "completeness": {"complete": not more, "truncated": more, "returned": len(rows),
                                "next_cursor": nxt if more and nxt else None}}
        if more:
            # a run of refused files longer than a cursor skips: stopped, said, no cursor
            out["completeness"]["reason"] = "page_cap" if nxt else "withheld_run"
        return out

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

    def _comms_widened(self, conn: Any, settings: Any, guarded: str, actor: Any,
                       views: Sequence[str],
                       query: str, words: Sequence[str], who: Any, since: Any, until: Any,
                       room: int, seen: set, ranking: Any, few: bool
                       ) -> tuple[list[Any], Any]:
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
        ids: list[str] = []
        if ranking is not None:
            try:
                ranked = plug._gated(ranking.result(timeout=MEANING_WAIT_S),
                                     self.base.policy, actor, self.base._item_allowed)
                ids = [h["object_id"] for h in ranked.hits][:MEANING_K]
            except Exception:  # noqa: BLE001 - late or failed: the words answer alone
                ranking.cancel()
                ids = []
        any_q = cs.tsquery_any(query) if few and not ids else None
        if any_q is None and not ids:
            return [], None
        k = 3 * room + len(seen) + 1

        def work(pg: Any) -> tuple[list[Any], list[Any]]:
            some: list[Any] = []
            if any_q is not None:
                sql = cs.search_sql(views, guarded=guarded, biz=bv.SCHEMA, query=True, party_kind=who.kind if who else None,
                                    since=since is not None, until=until is not None,
                                    cursor=False)
                some = pg.execute(sql, {
                    "q": any_q, "party": who.value if who else None, "since": since,
                    "until": until, "c_at": None, "c_h": None, "k": k, "k_own": k,
                    "walk": cs.walk_rows(pg, any_q)}).fetchall() if sql else []
            near: list[Any] = []
            if ids:
                sql = cs.meaning_sql(views, guarded=guarded, biz=bv.SCHEMA, party_kind=who.kind if who else None,
                                     since=since is not None, until=until is not None)
                near = pg.execute(sql, {
                    "ids": ids, "q": cs.tsquery_any(query) or cs.tsquery(query),
                    "q_all": cs.tsquery(query),
                    "party": who.value if who else None, "since": since,
                    "until": until}).fetchall() if sql else []
            return some, near
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
        biz = bv.SCHEMA
        handle = args.get("citation")
        offset = int(args.get("text_offset") or 0)
        most = int(args.get("max_chars") or 20000)

        def work(pg: Any) -> Any:
            views = cs.present(pg, guarded, opening=True)
            if not views:
                return None
            rows = pg.execute(cs.open_sql(views, guarded=guarded, biz=biz),
                              {"h": handle}).fetchall()
            if rows:
                return rows
            person = sql_views.person_schema()
            if guarded != person:   # the reader may read it, the AI may not
                mine = cs.present(pg, person, opening=True)
                if mine and pg.execute(cs.exists_sql(mine, biz=person), {"h": handle}
                                       ).fetchone()[0]:
                    return []
            return None
        moment = _ceiling()
        rows = self._comms_scoped(conn, settings, work, actor)
        if rows is None:
            if moment is not None:
                # under a ceiling no other path opens it (the old read is not bounded)
                raise ToolError("not_found", "no such message")
            return {"not_comms": True}
        if not rows:
            raise ToolError("egress_withheld", "this item is never shown to the assistant")
        if moment is not None and read_ceiling.after(rows[0][4], moment):
            raise ToolError("not_found", "no such message")   # it had not happened yet
        return cs.opened(rows, offset=offset, most=most)

    def _comms_scoped(self, conn: Any, settings: Mapping[str, str], work: Any,
                      actor: Any) -> Any:
        """``_scoped`` for the comms statements (as the store's own role: ``_scoped``). The
        store's role refused (its grants on the comms views missing: ``sql_views``
        install not run since the role was made): not ready, so the caller reads the old
        way instead of failing every search and open."""
        try:
            return self._scoped(conn, None, work, role=False, extra=settings,
                                full=SEARCH_FULL_MS if self._full(actor) else False)
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
                                full=SEARCH_FULL_MS if self._full(actor) else False)
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
        rows = rows[:limit]
        documents, searched = rs.hits(rows), rs.searched(args, words)
        if not off and not more and len(rows) < min(limit, WIDEN_BELOW):
            # few labels hold every word: the labels spelled close to the query ("Coop"
            # for "Co-operatives"), read through the same door
            near = self._records_close(conn, bound, dated, {r[0] for r in rows},
                                       min(MEANING_EXTRA, limit - len(rows)), read)
            for hit in rs.hits(near):
                hit["match"] = "close in spelling"
                documents.append(hit)
            if near:
                searched["widened"] = "also records whose name is spelled close to the query"
        out = {"documents": documents, "searched": searched, "gaps": [],
               "completeness": {"complete": not more, "truncated": more,
                                "returned": len(rows),
                                "next_cursor": f"r1:{off + limit}" if more and
                                off + limit <= rs.MAX_OFFSET else None}}
        if more:
            out["completeness"]["reason"] = "page_cap"
        return out

    def _records_close(self, conn: Any, bound: Mapping[str, Any], dated: Mapping[str, bool],
                       seen: set, room: int, read: Any) -> list[Any]:
        """Up to ``room`` rows of the reader's guarded ``records`` (``read``: the door's
        own statement) whose label is spelled close to the query, closest first, none in
        ``seen``: the store names the candidates by the label's trigram index
        (``rs.close_candidates_sql``, a read-only transaction of its own, at most
        ``CANDIDATE_MS``), the door reads the rows. None when the query is too short or the
        candidate read failed or ran out of time, or pg_trgm is not installed."""
        import psycopg
        from operating.pg import records_search as rs
        query = str(bound.get("q") or "")
        if room <= 0 or sum(ch.isalnum() for ch in query) < rs.CLOSE_MIN_LETTERS:
            return []
        ms = int(min(CANDIDATE_MS, self._seconds() * 1000))
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
                          cap: int, dated: Mapping[str, bool]) -> "list[str] | None":
        """``records_search``'s candidates (``rs.candidates_sql``): the store's own read of
        its records table (as the comms search reads its text index), in a read-only
        transaction of its own, at most ``CANDIDATE_MS``; never returned to the caller.
        None: no word the label index finds, or the read failed or ran out of time (the
        search then reads every label through the door, as before)."""
        import psycopg
        from operating.pg import records_search as rs
        positions = rs.indexed(words)
        if not positions:
            return None
        sql = rs.candidates_sql(positions, biz=bv.SCHEMA, **dated)
        ms = int(min(CANDIDATE_MS, self._seconds() * 1000))
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
        except (psycopg.Error, ToolError):
            return None

    # ------------------------------------------------------------------ query

    def _query(self, conn: Any, scope: Any, sql: str, actor: Any) -> dict[str, Any]:
        wrapped = f"SELECT * FROM (\n{sql}\n) AS plx_q"
        views = sorted(v for v in bv.VIEWS if re.search(rf"\b{v}\b", sql, re.I))
        derived = [v for v in COMMS_DERIVED if re.search(rf"\b{v}\b", sql, re.I)]
        # the communications views read their own settings beside the door's: only when
        # the statement names one or a view over them (unset, they show nothing: fail
        # closed)
        extra = self._comms_settings(scope, actor) \
            if derived or any(bv.VIEWS[v].fill for v in views) else None
        # the generic door tables (``generic_views``): refused while rebuilt, withheld
        # counts and build time as a curated view's
        views += [v for v in GENERIC if re.search(rf"\b{v}\b", sql, re.I)]
        full = self._full(actor)
        door = Door(reader_grant(scope, full=full))
        started = time.monotonic()

        def work(pg: Any) -> Any:
            refused = [v for v in views if v in door.decided["refused"]]
            if refused:
                # a view whose rows carry a mark it does not mask: no wait ends that
                raise ToolError("not_ready", f"{', '.join(refused)} cannot be read here "
                                "yet", "use plx.search or plx.table")
            (plan,) = pg.execute("EXPLAIN (FORMAT JSON) " + wrapped).fetchone()
            plan = plan if isinstance(plan, list) else json.loads(plan)
            cost = float(plan[0]["Plan"]["Total Cost"])
            if full:
                # a full reader: the statement's own time limit bounds it (FULL_STATEMENT_MS);
                # a costly one waits for one of the box's few slots for those (``_costly_slot``)
                if cost > MAX_COST:
                    _costly_slot(pg)
                cols, rows, stopped = _fetch(pg, sql)
                return cols, rows, stopped, _built(pg, views), _visible(pg, views, cols, rows)
            if cost > MAX_COST:
                raise ToolError("too_costly", f"the plan's cost ({cost:.3g}) is past "
                                f"{MAX_COST:.3g}: it would read too much",
                                "filter (where on a date or key) or aggregate first")
            if any(v in GENERIC_TABLES for v in views):
                # records/record_links: a plan reading past MAX_RECORDS rows of them is
                # refused with what to filter on (``generic_views.guard``)
                from operating.pg import generic_views
                generic_views.guard(plan, sql, generic_views.table_sizes(pg))
            cols, rows, stopped = _fetch(pg, sql)
            return cols, rows, stopped, _built(pg, views), _visible(pg, views, cols, rows)

        def lineage(pg: Any, got: Any) -> list[dict[str, Any]]:
            return _sources(pg, got[4])

        until = None
        while True:
            try:
                (cols, rows, stopped, built, _seen), sources = self._scoped(
                    conn, door, work, extra=extra, after=lineage, full=full)
                break
            except _NoSlot:
                # the slot wait holds no pooled connection: the try's transaction ended
                until = _costly_until() if until is None else until
                now = time.monotonic()
                if now >= until:
                    raise ToolError("busy", f"{FULL_COSTLY_SLOTS} long queries are already "
                                    "running", "filter (where on a date or key) or aggregate "
                                    "so it reads less, or send it again shortly") from None
                time.sleep(min(FULL_COSTLY_POLL_SECONDS, until - now))
        # per view read, per column, how many rows the reader may read hold a value it
        # may not (they read NULL there), from ``class_counts`` when its door was decided
        withheld = {v: dict(door.decided["withheld"][v]) for v in views
                    if door.decided["withheld"].get(v)}
        truncated = len(rows) > MAX_ROWS or stopped
        rows = rows[:MAX_ROWS]
        rid = hashlib.sha256(json.dumps([_actor_key(actor), sql, built, time.time()],
                                        default=str).encode()).hexdigest()[:20]
        result = f"sql://{rid}"
        shown, size, cut_cells = [], 0, 0
        for n, raw in enumerate(rows):
            # cells line up with ``columns``: under one contract-typed key (``cells``)
            # numbers, dates and codes stay bare at the output boundary; keyed by the
            # query's own column names every cell would leave as an envelope.
            cells: list[Any] = []
            for value in raw:
                cell, cut = _cell(value)
                cut_cells += cut
                cells.append(cell)
            entry = {"cite": f"{result}#row={n}", "cells": cells}
            size += len(json.dumps(entry, default=str))
            if size > MAX_BYTES:
                truncated = True
                break
            shown.append(entry)
        RESULTS.put(rid, {"actor": _actor_key(actor), "sql": sql, "columns": cols,
                          "rows": [list(r) for r in rows[:len(shown)]],
                          "complete": not truncated and not withheld, "views": views,
                          "built": built, "sources": sources})
        out: dict[str, Any] = {
            "result": result, "columns": cols, "rows": shown, "row_count": len(shown),
            "truncated": truncated, "views": views,
            # under a replay's ceiling the data are as of the replayed moment, never the
            # tables' build time (it would tell the model the real date)
            "as_of": (_ceiling().isoformat() if _ceiling() is not None else
                      _cell(built)[0] if built is not None else None),
            "seconds": round(time.monotonic() - started, 2),
            "partial": bool(withheld), "withheld": withheld}
        if sources:
            out["sources"] = len(sources)
        if truncated:
            out["note"] = (f"only the first {len(shown)} rows are shown: aggregate (count, "
                           "sum ... GROUP BY) or filter to see the rest")
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
        doc = {"rules": RULES, "views": _dictionary()}
        if what is True:
            return {"rules": RULES, "views": {
                name: f"{entry['about']} Columns: {', '.join(entry['columns'])}"
                for name, entry in doc["views"].items()}}
        if isinstance(what, str) and what in doc["views"]:
            return {**doc, "views": {what: doc["views"][what]}}
        return {**doc, "views": {"records": doc["views"]["records"]},
                "note": "SELECT type, name FROM record_types lists the record types"}

    def _describe(self, what: Any, actor: Any) -> dict[str, Any]:
        doc = {"rules": RULES, "views": _dictionary()}
        if what is True:
            # the list fits one result (each string is sent wrapped as untrusted text):
            # each view's purpose and column names in one line; a view's column meanings
            # and sample values are its own describe
            return {"rules": RULES, "views": {
                name: f"{entry['about']} Columns: {', '.join(entry['columns'])}"
                for name, entry in doc["views"].items()},
                "more": "plx.sql {\"describe\": \"<view>\"}: a view's column meanings, "
                        "sample rows and commonest values"}
        if isinstance(what, str) and (what in GENERIC or what in COMMS_DERIVED):
            return {**doc, "views": {what: doc["views"][what]}}
        if isinstance(what, str) and what not in bv.listed() and \
                re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", what):
            return self._describe_type(what, actor, doc)
        if not isinstance(what, str) or what not in bv.listed():
            raise ToolError("unknown_view", f"no view {str(what)[:64]!r}",
                            "views: " + ", ".join(sorted(bv.listed())) + "; or a record "
                            "type record_types lists")
        conn, scope = self._reader(actor)
        v = bv.VIEWS[what]
        extra = self._comms_settings(scope, actor) if v.fill else None

        def work(pg: Any) -> Any:
            sample = pg.execute(f"SELECT * FROM {what} LIMIT 3")
            cols = [d.name for d in sample.description]
            rows = [dict(zip(cols, (_cell(x)[0] for x in r))) for r in sample.fetchall()]
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
            return rows, common, total

        rows, common, total = self._scoped(conn, Door(reader_grant(
            scope, full=self._full(actor))), work,
                                           extra=extra)
        return {**doc, "views": {what: doc["views"][what]},
                "sample": {"rows_you_can_read": total, "rows": rows,
                           "commonest_values": common}}


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
                            "SELECT type, name FROM record_types lists the record types")
        entry, sample, common, total = got
        values: dict[str, list[Any]] = {}
        for key, value, n in common:
            if len(values.setdefault(key, [])) < SAMPLE_VALUES:
                values[key].append([_cell(value)[0], n])
        return {"rules": RULES, "record_type": {
            k: (_cell(v)[0] if not isinstance(v, (list, dict)) else v)
            for k, v in entry.items()},
            "sample": {"records_you_can_read": total,
                       "rows": [{"handle": h, "label": _cell(lb)[0], "record_date": d,
                                 "fields": {k: _cell(v)[0] for k, v in (f or {}).items()}}
                                for h, lb, d, f in sample],
                       "commonest_values": values,
                       "values_read_from": f"the first {TYPE_SAMPLE} records"}}


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
                        "use plx.search or plx.table") from None
    if got is None:
        raise ToolError("not_ready", "the business tables are not built for plx.sql "
                        "yet", "use plx.search or plx.table")
    stamp, serves = got
    if not serves:
        # fail closed: the people exclusion changed after the tables were decided
        # (the people exclusion's update follows a grown set in the set's own transaction:
        # a door behind it is one a follow failed on, decided whole within minutes, not
        # seconds: refused at once, not waited on)
        raise ToolError("not_ready", "the business tables are catching up with a change "
                        "to who is excluded", "use plx.search or plx.table")
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


def _costly_until() -> float:
    """When a costly statement stops waiting for a slot: FULL_COSTLY_WAIT_SECONDS from now,
    never past the call's own time less a quarter statement."""
    from operating import call_limits
    wait = FULL_COSTLY_WAIT_SECONDS
    left = call_limits.seconds_left()
    if left is not None:
        wait = min(wait, max(0.0, left - FULL_STATEMENT_MS / 1000.0 / 4))
    return time.monotonic() + wait


def scoped_read(pg: Any, door: Door | None, work: Any, seconds: float, *,
                person: bool = False, as_role: bool = True,
                extra: Mapping[str, str] | None = None, after: Any = None) -> Any:
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
            values = [*(settings[n] for n in names), where + ", pg_catalog",
                      str(int(seconds * 1000)), "2000", *([role] if as_role else [])]
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
        raise ToolError("timeout", f"the query ran past {seconds:.0f} s",
                        "narrow it (where on a date or a key), or aggregate") \
            from None
    except psycopg.errors.InsufficientPrivilege:
        raise ToolError("not_readable", "the statement reads something outside "
                        "the business views", "plx.sql {\"describe\": true} "
                        "lists what is readable") from None
    except psycopg.errors.UndefinedTable as exc:
        raise ToolError("unknown_view", _first_line(exc),
                        "plx.sql {\"describe\": true} lists the views") from None
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
    except psycopg.Error as exc:
        raise ToolError("query_failed", f"the store refused the query "
                        f"({type(exc).__name__}: {_first_line(exc)})") from None
    finally:
        if not pg.closed and pg.info.transaction_status == 0:   # idle: restorable
            pg.isolation_level, pg.read_only = kept


# what a statement meets when a table it planned on was dropped by a swap since
_SWAPPED = re.compile(r"could not open relation with OID|cache lookup failed for relation")


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
# cut past MAX_CELL (an array: its first 51 elements, each cut) by the statement itself, and
# rows stream from a server-side cursor that stops past FETCH_BYTES: a padded or doubled
# string never reaches this process whole.
_BOUNDED_CATEGORIES = frozenset("NBDT")
FETCH_BYTES = 4 * MAX_BYTES
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


def _fetch(pg: Any, sql: str) -> tuple[list[str], list[tuple], bool]:
    """``sql``'s columns and at most MAX_ROWS + 1 rows, and whether the byte cap stopped
    the stream (the caller marks the result truncated)."""
    described = pg.execute(f"SELECT * FROM (\n{sql}\n) AS plx_q LIMIT 0").description
    cols = [d.name for d in described]
    kinds = _type_categories(pg, {int(d.type_code) for d in described})
    names = [f"plx_c{i}" for i in range(len(cols))]
    shown = []
    for name, d in zip(names, described):
        kind = kinds.get(int(d.type_code), "X")
        if kind in _BOUNDED_CATEGORIES:
            shown.append(name)
        elif kind == "A":
            shown.append(f"CASE WHEN {name} IS NULL THEN NULL ELSE ARRAY(SELECT left(e::text, "
                         f"{MAX_CELL + 1}) FROM unnest({name}) e LIMIT 51) END AS {name}")
        else:
            shown.append(f"left({name}::text, {MAX_CELL + 1}) AS {name}")
    capped = (f"SELECT {', '.join(shown) or '*'} FROM (\n{sql}\n) AS plx_q"
              f"{'(' + ', '.join(names) + ')' if names else ''} LIMIT {MAX_ROWS + 1}")
    rows: list[tuple] = []
    size, stopped = 0, False
    with pg.cursor(name=f"plx_sql_{threading.get_ident()}") as cur:
        cur.execute(capped)
        while not stopped and len(rows) <= MAX_ROWS:
            batch = cur.fetchmany(FETCH_BATCH)
            if not batch:
                break
            for row in batch:
                size += sum(len(str(x)) for x in row) + 8
                if size > FETCH_BYTES:
                    stopped = True
                    break
                rows.append(row)
    return cols, rows, stopped


def _visible(pg: Any, views: list[str], cols: list[str], rows: list[Any]
             ) -> dict[str, list[str]]:
    """Per view read, the result's handles that view shows this reader (same scoped
    transaction): a literal handle of a row the reader cannot read has no sources."""
    if "handle" not in cols or not views:
        return {}
    at = cols.index("handle")
    handles = sorted({r[at] for r in rows if isinstance(r[at], str)})
    if not handles:
        return {}
    return {v: [h for (h,) in pg.execute(
        f"SELECT DISTINCT handle FROM {v} WHERE handle = ANY(%s::text[]) ORDER BY 1",
        (handles,)).fetchall()] for v in views if v in bv.VIEWS}


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


RULES = (
    "One SELECT per call over these views (bare names), at most 200 rows back. Each row is "
    "one real thing (its Portal, Sage, BC and FileMaker copies joined); records deleted at "
    "the source are left out; money columns sit beside a <column>_currency column: never "
    "add across currencies (GROUP BY the currency). Dates are business dates (America/"
    "New_York). sales_invoices.pilot = true marks BC pilot invoices (Sage is the system of "
    "record): leave them out unless asked. Text compares case-sensitively: use ILIKE "
    "'%word%' for names. A column in a row's withheld list is one you may not read (NULL). "
    "Each row has a handle: select it to cite the record. A result row's cells line up "
    "with its columns.")
# the generic views' rule (``records``), said only while they are served (``served_views``)
RULES_GENERIC = (
    " EVERY other captured record of every system (Mission Control, "
    "SharePoint lists, FileMaker, Portal, BC and Sage tables, Planner, records read out of "
    "mail ...) is in the generic views: record_types (the catalog of types: plain name, "
    "systems, fields with kind and meaning, label and date fields, links out/in), records "
    "(one row per record) and record_links (one row per link between two records). Filter "
    "records on type = '<Type>' and read a field as fields->>'name' (numbers: "
    "(fields->>'name')::numeric; money beside fields->>'name_currency', quantities beside "
    "fields->>'name_unit'). The same thing held by two systems is two records joined by an "
    "identity link (record_types.identity_links, in record_links). {describe: '<Type>'} "
    "shows a type's fields, sample records and commonest values.")
RULES_TAIL = " Mail text, chats and documents: plx.search."
RULES_BASE = RULES
RULES = RULES_BASE + RULES_GENERIC + RULES_TAIL

GENERIC_ABOUT = {
    "record_types": ("the catalog of every record type the store holds (generated from "
                     "the ontology: new sources and fields appear by themselves)", {
                         "type": "text: the type's id (records.type)",
                         "name": "text: its plain name", "description": "text",
                         "systems": "text[]: the systems holding records of it",
                         "sources": "text[]: the source tables (records.source)",
                         "label_fields": "text[]: fields that name a record",
                         "date_fields": "text[]: fields that date a record",
                         "identity_links": "text[]: link types joining copies of one thing",
                         "fields": "jsonb: [{name, kind, description}]",
                         "links_out": "jsonb: [{link_type, to}]",
                         "links_in": "jsonb: [{link_type, from}]",
                         "tables": "text[]: the views holding its records (a curated view, "
                                   "a mail/Teams view, or records)"}),
    "records": ("one row per current record of a type no curated view lists, as this "
                "reader may read it (count a type here)", {
        "record_id": "text: the record's id (record_links.from_id / to_id)",
        "type": "text: the record type (filter on it)", "system": "text",
        "source": "text: the source table", "label": "text: its display name or number",
        "record_date": "text YYYY-MM-DD: its key date (record_types.date_fields)",
        "first_seen": "text: when it was first captured", "changed_at": "text",
        "fields": "jsonb: field -> value (the authority-first value of each field)"}),
    "record_links": ("one row per live link between two records this reader may read", {
        "from_id": "text: records.record_id", "from_type": "text", "from_handle": "text",
        "link_type": "text", "to_id": "text: records.record_id", "to_type": "text",
        "to_handle": "text", "tier": "text: verified/reviewed/corroborated (solid), "
        "stored_by_text_join, lead (a guess)"}),
}
GENERIC = tuple(GENERIC_ABOUT)
# views over the communications views (``operating.pg.comms_findings``): a statement naming
# one reads with the communications settings (its rows are those of messages the reader's
# own mail and Teams views show; unset, it shows nothing)
COMMS_DERIVED_ABOUT = {
    "comms_findings": (
        "decisions, requests, complaints and praise read out of mail and Teams messages "
        "(one row per item a message states; a message this reader cannot read shows none; "
        "the summary is a model's one-line reading: open the handle with plx.open to quote "
        "the message). Customer feedback = kind complaint or praise; GROUP BY topic or "
        "customers for themes", {
            "kind": "text: decision, request, complaint or praise",
            "topic": "text: delivery, pricing, quality, documents, order, payment, product, "
                     "samples, packaging, schedule, stock, service or other",
            "summary": "text: one plain sentence saying what was decided, asked or "
                       "reported",
            "who": "text: who decided, asked or complained, as the message names them "
                   "('' not named)",
            "from_side": "text: who sent the message: us (our staff), customer, outside "
                         "(another party: supplier, carrier, vendor) or '' (unknown: a Teams "
                         "sender not matched to staff)",
            "said_at": "timestamptz: when the message was sent",
            "said_date": "date: its business date",
            "source": "text: email or teams",
            "sender": "text: the message's sender (an address for mail, a name in Teams)",
            "subject": "text: the mail subject or Teams chat topic",
            "thread": "text: the mail thread (emails.thread) or Teams chat id",
            "customers": "text[]: handles of customers rows the message is about (join "
                         "customers ON handle = ANY(customers))",
            "suppliers": "text[]: handles of suppliers rows it is about"}),
}
COMMS_DERIVED = tuple(COMMS_DERIVED_ABOUT)
GENERIC_TABLES = ("records", "record_links")
TYPE_SAMPLE = 500


_CATALOG: dict[str, Any] = {}


def catalog_lines(pg: Any = None) -> list[str]:
    """The record types the door holds as prompt lines, one per system: each type with its
    plain name, the fields that label and date it and, when a curated or communications
    view holds it instead of ``records``, that view; generated from the served
    catalog (``<biz>.record_types``, read by the store's role: no row of any record, no
    count). Kept per process until the catalog is built again; none when it cannot be read
    (the prompt then lists the views alone)."""
    from integrations.image_ocr_gpu import FREIGHTCOM
    try:
        if pg is None:
            from operating.pg import pool
            with pool.connect(connect_timeout=3, options="-c statement_timeout=2000") as c:
                return catalog_lines(c)
        if pg.execute("SELECT to_regclass(%s)", (f"{bv.SCHEMA}.record_types",)
                      ).fetchone()[0] is None:
            return []
        # the catalog's build and where each type is held (``generic_views.mark_tables``
        # moves ``tables`` without a new catalog build)
        (built,) = pg.execute(f"SELECT max(built_at) || md5(string_agg(type || ':' || "
                              "array_to_string(tables, ','), ';' ORDER BY type)) FROM "
                              f"{bv.SCHEMA}.record_types").fetchone()
        if _CATALOG.get("built") == built and built is not None:
            return list(_CATALOG["lines"])
        # the guarded view's own Freightcom rule (``generic_views.listed_sql``)
        from operating.pg import generic_views
        rows = pg.execute(
            f"SELECT t.type, t.name, t.systems, t.label_fields, t.date_fields, t.tables FROM "
            f"{bv.SCHEMA}.record_types t WHERE cardinality(t.tables) > 0 AND "
            f"{generic_views.listed_sql('t')} ORDER BY t.type").fetchall()
    except Exception:  # noqa: BLE001 - no catalog: the views alone
        return []
    by_system: dict[str, list[str]] = {}
    for tid, name, systems, labels, dates, tables in rows:
        if FREIGHTCOM.search(" ".join(map(str, (tid, name)))):
            continue   # the plain name too (made from the type: the same words)
        # a type held by a curated or comms view is named with it (records lacks it)
        bits = [f"label {'/'.join(labels[:2])}" if labels else "",
                f"date {'/'.join(dates[:2])}" if dates else "",
                "in " + "/".join(tables) if list(tables) != ["records"] else ""]
        shown = f"{tid} ({name}" + "".join(f"; {b}" for b in bits if b) + ")"
        for system in (systems or ["unknown"]):
            by_system.setdefault(str(system), []).append(shown)
    lines = [f"- {system}: " + ", ".join(sorted(types))
             for system, types in sorted(by_system.items())]
    _CATALOG.update(built=built, lines=lines)
    return list(lines)


_SERVED: dict[str, Any] = {}
SERVED_RECHECK_SECONDS = 60.0


def served_views(names: Sequence[str] | None = None, pg: Any = None) -> frozenset[str] | None:
    """Which of the views ``names`` (default: every view of ``_dictionary``) the served
    schema holds now (a business table built, the mail and Teams tables once their build
    is served, ``records`` once the generic build is): what the Ask's prompt lists, so the
    model is never sent to a table it would be refused (11-18: emails/teams_messages and
    records listed while not built, every mail question spent its calls on
    'unknown view'). Read by the store's role (no row of any table), kept per process
    ``SERVED_RECHECK_SECONDS``; None when it cannot be read (the prompt lists them all)."""
    wanted = tuple(names if names is not None else _dictionary())
    held = _SERVED.get(wanted)
    if held is not None and time.monotonic() - held[0] < SERVED_RECHECK_SECONDS:
        return held[1]
    try:
        if pg is None:
            from operating.pg import pool
            with pool.connect(connect_timeout=3, options="-c statement_timeout=2000") as c:
                return served_views(wanted, c)
        rows = pg.execute("SELECT name FROM unnest(%s::text[]) name WHERE "
                          "to_regclass(%s || '.' || name) IS NOT NULL",
                          (list(wanted), bv.SCHEMA)).fetchall()
    except Exception:  # noqa: BLE001 - not known: every view listed, as before
        return None
    got = frozenset(r[0] for r in rows)
    _SERVED[wanted] = (time.monotonic(), got)
    return got


def _dictionary() -> dict[str, Any]:
    out = {}
    for name, entry in bv.dictionary().items():
        cols = dict(entry["columns"])
        for cname, kind, cdoc in sql_views.SHOWN_COMMON:
            cols[cname] = f"{kind}: {cdoc}"
        out[name] = {"about": entry["about"], "columns": cols}
    for name, (about, cols) in {**GENERIC_ABOUT, **COMMS_DERIVED_ABOUT}.items():
        cols = dict(cols)
        if name != "record_types":
            for cname, kind, cdoc in sql_views.SHOWN_COMMON:
                cols[cname] = f"{kind}: {cdoc}"
        out[name] = {"about": about, "columns": cols}
    return out


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
