"""The read tools' SQLite store queries, run on the Postgres ontology (``PLX_ONTOLOGY_PG``).

The read tools (``operating/ontology_tools*.py``, the proactive engine) hold their store
queries as SQLite SQL over the store's tables and views, with the reader's permission
predicates in the SQL itself: visible audience keys and permitted marking sets as JSON
lists (``json_each(:aud)``, ``json_each(:marks)``), the as-known-at time (``julianday``,
``plx_l9_time``), value markings, and the item gate as a function the tool registers for
its call. :class:`PgCompatConnection` is a ``sqlite3.Connection`` look-alike over the
``ontology`` schema (migrations 0006-0008) that runs that same SQL, translated
(:func:`translate`): parameters (``?``, ``:name``), the SQLite functions the tools use
(``ontology.sq_*``: same answers, NULL where SQLite gives NULL), GLOB, ``IS`` as
``IS NOT DISTINCT FROM``, ``rowid`` as each table's row number, ``INDEXED BY`` dropped. So
every permission predicate is compiled into the Postgres query exactly as written for
SQLite, and one rule serves both stores.

A function registered in Python (``create_function``: the item gates) is evaluated in
rounds: ``ontology.sq_call`` answers from the session's ``sq_calls`` table and queues a
call it has no answer for (answering NULL); after the statement the queue is answered in
Python and the statement runs again, until nothing is queued (``MAX_ROUNDS``). The final
rows are those the real answers give.

Rows are :class:`Row` (index and name access, ``keys()``, as ``sqlite3.Row``) with
SQLite's value types: a boolean is 0/1, an integral numeric an int, bytea bytes. A
Postgres error is raised as ``sqlite3.OperationalError``, so the tools' existing handling
(``store_unavailable``) applies. Stdlib and psycopg.
"""

from __future__ import annotations

from decimal import Decimal
import functools
import json
import re
import sqlite3
import threading
from typing import Any, Callable, Iterator, Mapping, Sequence

MAX_ROUNDS = 24
STATEMENT_TIMEOUT_MS = 120_000
# A table's SQLite rowid in the Postgres ontology.
ROWID = {"objects": "row_no", "link_rows": "row_no",
         "object_versions": "ver", "value_sources": "src", "text_fields": "field_id",
         "text_chunks": "chunk_id", "link_texts": "id", "link_skeletons": "id",
         "value_fields": "field", "value_kinds": "kind"}
# Columns compared with a json_each list of numbers (the list read as bigint).
INT_COLUMNS = frozenset({"rowid", "ver", "src", "field_id", "chunk_id", "layout", "field",
                         "kind", "id", "pos", "row_no", "ek"})
# A ``json_each(:name)`` IN list bound as an array: the parameter's suffix (text, bigint),
# its members read in Python (``_members``), else by the very SQL function (``_FALLBACK``).
LIST_SUFFIX = {False: "__list_text", True: "__list_int"}
_PLACEHOLDER = re.compile(r"%%|%s|%\(([A-Za-z_][A-Za-z0-9_]*)\)s")
_LISTED_AS = {"::text[]) AS u(value)": False, "::bigint[]) AS u(value)": True}
_FALLBACK = {False: "SELECT array(SELECT value FROM ontology.sq_json_each(%s))",
             True: "SELECT array(SELECT value FROM ontology.sq_json_each_int(%s))"}
_RENAME = {"julianday": "ontology.sq_julianday", "json_extract": "ontology.sq_json_extract",
           "json_array_length": "ontology.sq_json_array_length", "ifnull": "coalesce",
           "instr": "strpos", "plx_l9_time": "ontology.sq_l9_time"}
_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<comment>--[^\n]*)
  | (?P<str>'(?:[^']|'')*')
  | (?P<qid>"(?:[^"]|"")*")
  | (?P<num>\d+(?:\.\d+)?)
  | (?P<param>\?|:[A-Za-z_][A-Za-z0-9_]*)
  | (?P<word>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)
  | (?P<op>==|!=|<>|<=|>=|\|\||::|[-+*/%<>=(),.;~|&])
  | (?P<other>.)
""", re.VERBOSE | re.DOTALL)


class Untranslatable(sqlite3.OperationalError):
    """SQL the compatibility layer does not run on Postgres (FTS5 MATCH, PRAGMA writes)."""


def _tokens(sql: str) -> list[tuple[str, str]]:
    return [(m.lastgroup or "other", m.group()) for m in _TOKEN.finditer(sql)]


def _next(tokens: list[tuple[str, str]], i: int) -> int:
    """Index of the first non-blank token at or after ``i``."""
    while i < len(tokens) and tokens[i][0] in ("ws", "comment"):
        i += 1
    return i


def _prev(tokens: list[tuple[str, str]], i: int) -> int:
    while i >= 0 and tokens[i][0] in ("ws", "comment"):
        i -= 1
    return i


def _close(tokens: list[tuple[str, str]], open_at: int) -> int:
    """Index of the ')' closing the '(' at ``open_at``."""
    depth = 0
    for j in range(open_at, len(tokens)):
        if tokens[j][1] == "(":
            depth += 1
        elif tokens[j][1] == ")":
            depth -= 1
            if depth == 0:
                return j
    raise Untranslatable("unbalanced parentheses")


def _args(tokens: list[tuple[str, str]], open_at: int, close_at: int) -> list[tuple[int, int]]:
    """(start, end) token spans of the top-level arguments between the parentheses."""
    spans, depth, start = [], 0, open_at + 1
    for j in range(open_at + 1, close_at):
        if tokens[j][1] == "(":
            depth += 1
        elif tokens[j][1] == ")":
            depth -= 1
        elif tokens[j][1] == "," and depth == 0:
            spans.append((start, j))
            start = j + 1
    if start < close_at or spans:
        spans.append((start, close_at))
    return spans


def _aliases(tokens: list[tuple[str, str]]) -> dict[str, str]:
    """alias (and table name) -> table, for the tables with a mapped rowid."""
    out: dict[str, str] = {}
    for i, (kind, text) in enumerate(tokens):
        if kind == "word" and text.lower() in ROWID:
            table = text.lower()
            out[table] = table
            j = _next(tokens, i + 1)
            if j < len(tokens) and tokens[j][1].upper() == "AS":
                j = _next(tokens, j + 1)
            if j < len(tokens) and tokens[j][0] == "word" and tokens[j][1].upper() not in (
                    "ON", "WHERE", "JOIN", "LEFT", "CROSS", "INNER", "GROUP", "ORDER", "LIMIT",
                    "USING", "INDEXED", "NOT", "UNION", "SET"):
                out[tokens[j][1]] = table
    return out


@functools.lru_cache(maxsize=4096)
def translate(sql: str, functions: tuple[str, ...] = (),
              gated_schemes: tuple[str, ...] = ()) -> str:
    """SQLite SQL -> Postgres SQL for psycopg (``%s`` / ``%(name)s`` parameters).
    ``functions``: names registered in Python (``sq_call``); ``gated_schemes``: the
    schemes ``plx_l9_gated`` keeps."""
    stripped = sql.strip().rstrip(";").strip()
    head = stripped[:20].upper()
    if head.startswith("PRAGMA"):
        if re.match(r"(?is)^PRAGMA\s+data_version\s*$", stripped):
            return "SELECT ontology.sq_data_version()"
        return "SELECT NULL WHERE false"
    if head.startswith("EXPLAIN"):
        return "SELECT NULL WHERE false"
    tokens = _tokens(stripped)
    lowered = {f.lower() for f in functions}
    aliases = _aliases(tokens)
    out: list[str] = []
    suffix: dict[int, str] = {}   # token index -> text appended after it
    replace: dict[int, str] = {}  # token index -> text written instead of it
    replace.update(_case_booleans(tokens))
    skip_until = -1
    i = 0
    while i < len(tokens):
        kind, text = tokens[i]
        if i <= skip_until:
            i += 1
            continue
        upper = text.upper()
        if i in replace:
            out.append(replace[i])
        elif kind == "param":
            out.append("%s" if text == "?" else f"%({text[1:]})s")
        elif kind == "op" and text == "*" and _star(tokens, i, aliases):
            alias, columns = _star(tokens, i, aliases)
            if alias is None:
                out.append(", ".join(columns))
            else:
                out.pop()   # the "alias." already written
                out.pop()
                out.append(", ".join(f"{alias}.{c}" for c in columns))
        elif kind == "op" and text == "%":
            out.append("%%")
        elif kind == "op" and text == "==":
            out.append("=")
        elif kind == "str":
            out.append(text.replace("%", "%%"))
        elif kind == "qid":
            out.append(text.replace("%", "%%"))
        elif kind == "word":
            j = _next(tokens, i + 1)
            call = j < len(tokens) and tokens[j][1] == "("
            name = text.lower()
            if upper == "MATCH":
                raise Untranslatable("full-text MATCH")
            if call and name in lowered:
                out.append(f"ontology.sq_call('{name}', (")
                replace[_close(tokens, j)] = ")::text)"
                skip_until = j
            elif call and name == "plx_l9_gated":
                schemes = ", ".join("'" + s.replace("'", "''") + "'" for s in gated_schemes)
                out.append(f"ontology.sq_l9_gated(ARRAY[{schemes}]::text[], ")
                skip_until = j
            elif call and name == "json_each":
                p = _prev(tokens, i - 1)
                p2 = _prev(tokens, p - 1)
                p3 = _prev(tokens, p2 - 1)
                p4 = _prev(tokens, p3 - 1)
                p5 = _prev(tokens, p4 - 1)
                numeric = False
                listed = False   # ``x IN (SELECT value FROM json_each(:name))``
                if p >= 0 and tokens[p][1].upper() == "FROM" and p2 >= 0:
                    if tokens[p2][1].lower() == "value" and p3 >= 0 and \
                            tokens[p3][1].upper() == "SELECT" and p5 >= 0 and \
                            tokens[p4][1] == "(" and tokens[p5][1].upper() == "IN":
                        before = _prev(tokens, p5 - 1)
                        column = tokens[before][1].split(".")[-1].lower() if before >= 0 else ""
                        numeric = column in INT_COLUMNS
                        listed = True
                    elif tokens[p2][1].lower() in INT_COLUMNS and p3 >= 0 and \
                            tokens[p3][1].upper() == "AS":
                        numeric = True
                arg = _next(tokens, j + 1)
                shut = _next(tokens, arg + 1)
                if listed and arg < len(tokens) and tokens[arg][0] == "param" and \
                        shut < len(tokens) and \
                        tokens[shut][1] == ")" and _next(tokens, shut + 1) < len(tokens) and \
                        tokens[_next(tokens, shut + 1)][1] == ")":
                    # an IN list bound as a real array (``PgCompatConnection._lists``): the
                    # planner counts its members, so a list of one id is read by index, never
                    # as a hash join over a whole table guessed from a 200-row estimate
                    bound = ("%s" if tokens[arg][1] == "?"
                             else f"%({tokens[arg][1][1:]}{LIST_SUFFIX[numeric]})s")
                    out.append(f"unnest({bound}::{'bigint' if numeric else 'text'}[]) "
                               "AS u(value)")
                    skip_until = shut
                else:
                    out.append("ontology.sq_json_each_int" if numeric
                               else "ontology.sq_json_each")
            elif call and name in ("json_array", "json_group_array"):
                out.append("json_build_array" if name == "json_array" else "json_agg")
                suffix[_close(tokens, j)] = "::text"
            elif call and name == "group_concat":
                close = _close(tokens, j)
                spans = _args(tokens, j, close)
                out.append("string_agg((")
                inner = translate("".join(t for _, t in tokens[spans[0][0]:spans[0][1]]),
                                  functions, gated_schemes)
                out.append(inner + ")::text, ")
                out.append(translate("".join(t for _, t in tokens[spans[1][0]:spans[1][1]]),
                                     functions, gated_schemes) if len(spans) > 1 else "','")
                out.append(")")
                skip_until = close
            elif call and name in ("max", "min"):
                close = _close(tokens, j)
                many = len(_args(tokens, j, close)) > 1
                out.append(("greatest" if name == "max" else "least") if many else text)
            elif call and name in ("coalesce", "ifnull"):
                # SQLite's comparisons are 0/1: COALESCE(a <= b, 1) is a boolean there
                close = _close(tokens, j)
                spans = _args(tokens, j, close)
                last = [k for k in range(*spans[-1]) if tokens[k][0] not in ("ws", "comment")]
                if len(spans) > 1 and len(last) == 1 and tokens[last[0]][1] in ("0", "1") \
                        and _compares(tokens, *spans[0]):
                    replace[last[0]] = "TRUE" if tokens[last[0]][1] == "1" else "FALSE"
                out.append("coalesce")
            elif call and name in _RENAME:
                out.append(_RENAME[name])
            elif name == "rowid" or name.endswith(".rowid"):
                alias = text.split(".")[0] if "." in text else None
                table = aliases.get(alias) if alias else (
                    next(iter(set(aliases.values()))) if len(set(aliases.values())) == 1
                    else None)
                if table is None:
                    raise Untranslatable(f"rowid of an unmapped table ({text})")
                out.append((alias + "." if alias else "") + ROWID[table])
            elif upper == "CROSS" and _cross_join_on(tokens, i):
                # SQLite's CROSS JOIN ... ON (an inner join kept in the order written, so
                # the planner seeks the joined table): Postgres has no ON for a CROSS JOIN,
                # and plans the inner JOIN itself.
                skip_until = i
            elif upper == "INDEXED":
                k = _next(tokens, i + 1)
                skip_until = _next(tokens, k + 1)   # INDEXED BY <name>
            elif upper == "NOT" and tokens[_next(tokens, i + 1)][1].upper() == "INDEXED":
                skip_until = _next(tokens, i + 1)
            elif upper == "NOT" and tokens[_next(tokens, i + 1)][1].upper() == "GLOB":
                out.append("!~ ontology.sq_glob_re(")
                skip_until = _next(tokens, i + 1)
                end = _operand_end(tokens, _next(tokens, skip_until + 1))
                suffix[end] = ")" + suffix.get(end, "")
            elif upper == "GLOB":
                out.append("~ ontology.sq_glob_re(")
                end = _operand_end(tokens, _next(tokens, i + 1))
                suffix[end] = ")" + suffix.get(end, "")
            elif upper == "IS":
                k = _next(tokens, i + 1)
                follow = tokens[k][1].upper() if k < len(tokens) else ""
                if follow == "NOT":
                    k2 = _next(tokens, k + 1)
                    after = tokens[k2][1].upper() if k2 < len(tokens) else ""
                    if after in ("NULL", "TRUE", "FALSE", "DISTINCT"):
                        out.append(text)
                    else:
                        out.append("IS DISTINCT FROM")
                        skip_until = k
                elif follow in ("NULL", "TRUE", "FALSE", "DISTINCT"):
                    out.append(text)
                else:
                    out.append("IS NOT DISTINCT FROM")
            elif upper == "LIKE":
                out.append("ILIKE")   # SQLite's LIKE ignores case
            elif upper == "REAL" and tokens[_prev(tokens, i - 1)][1].upper() == "AS":
                out.append("double precision")
            elif upper == "LIMIT" and tokens[_next(tokens, i + 1)][1] == "-":
                k = _next(tokens, i + 1)
                if tokens[_next(tokens, k + 1)][1] == "1":
                    out.append("LIMIT ALL")
                    skip_until = _next(tokens, k + 1)
                else:
                    out.append(text)
            else:
                out.append(text)
        else:
            out.append(text)
        if i in suffix:
            out.append(suffix[i])
        i += 1
    return "".join(out)


# Tables with a row number column SQLite keeps as rowid: what ``*`` reads in SQLite.
STAR_COLUMNS = {
    "object_versions": ("object_id", "l1_handle", "digest", "lineage", "markings",
                        "recorded_at", "superseded_at"),
    "objects": ("object_id", "object_type", "binding_id", "native_key", "audience_key",
                "first_recorded_at", "missing_from_source_since"),
}


def _cross_join_on(tokens: list[tuple[str, str]], i: int) -> bool:
    """Whether the CROSS at ``i`` starts ``CROSS JOIN <table> [alias] ON``."""
    k = _next(tokens, i + 1)
    if k >= len(tokens) or tokens[k][1].upper() != "JOIN":
        return False
    t = _next(tokens, k + 1)
    a = _next(tokens, t + 1)
    if a < len(tokens) and tokens[a][0] == "word" and tokens[a][1].upper() not in (
            "ON", "JOIN", "WHERE", "CROSS", "LEFT", "INNER", "ORDER", "GROUP", "LIMIT"):
        a = _next(tokens, a + 1)
    return a < len(tokens) and tokens[a][1].upper() == "ON"


def _star(tokens: list[tuple[str, str]], i: int, aliases: Mapping[str, str]
          ) -> tuple[str | None, tuple[str, ...]] | None:
    """(alias, columns) when token ``i`` is the ``*`` of ``alias.*`` over a table of
    ``STAR_COLUMNS``; (None, columns) for a bare ``SELECT *`` over that table alone."""
    p = _prev(tokens, i - 1)
    if p >= 1 and tokens[p][1] == "." and tokens[p - 1][0] == "word":
        alias = tokens[p - 1][1]
        table = aliases.get(alias)
        return (alias, STAR_COLUMNS[table]) if table in STAR_COLUMNS else None
    tables = set(aliases.values())
    if p >= 0 and tokens[p][1].upper() in ("SELECT", "DISTINCT") and len(tables) == 1 and \
            next(iter(tables)) in STAR_COLUMNS and not any(
                t[1].upper() in ("JOIN", ",") for t in tokens[i + 1:]
                if t[0] in ("word", "op")):
        return None, STAR_COLUMNS[next(iter(tables))]
    return None


_COMPARISONS = frozenset({"<", ">", "<=", ">=", "=", "==", "!=", "<>", "LIKE", "IN", "IS",
                          "GLOB", "EXISTS", "BETWEEN", "AND", "OR", "NOT"})


def _compares(tokens: list[tuple[str, str]], start: int, end: int) -> bool:
    """Whether the expression in [start, end) is a comparison (at its own depth)."""
    depth = 0
    for k in range(start, end):
        text = tokens[k][1]
        if text == "(":
            depth += 1
        elif text == ")":
            depth -= 1
        elif depth == 0 and text.upper() in _COMPARISONS:
            return True
    return False


def _unwrap(tokens: list[tuple[str, str]], a: int, b: int) -> tuple[int, int]:
    """[a, b) without the parentheses around the whole of it."""
    seen = [j for j in range(a, b) if tokens[j][0] not in ("ws", "comment")]
    while seen and tokens[seen[0]][1] == "(" and _close(tokens, seen[0]) == seen[-1]:
        a, b = seen[0] + 1, seen[-1]
        seen = seen[1:-1]
    return a, b


def _case_booleans(tokens: list[tuple[str, str]]) -> dict[int, str]:
    """0/1 literals that are a CASE branch beside a comparison branch, as FALSE/TRUE
    (SQLite's comparisons are 0/1; Postgres refuses a CASE of boolean and integer)."""
    out: dict[int, str] = {}
    stack: list[list[tuple[int, int]]] = []   # per open CASE: its branch spans
    starts: list[int] = []
    for k, (kind, text) in enumerate(tokens):
        word = text.upper() if kind == "word" else ""
        if word == "CASE":
            stack.append([])
            starts.append(-1)
        elif stack and word in ("THEN", "ELSE"):
            if starts[-1] >= 0:
                stack[-1].append((starts[-1], k))
            starts[-1] = k + 1
        elif stack and word == "WHEN":
            if starts[-1] >= 0:
                stack[-1].append((starts[-1], k))
            starts[-1] = -1
        elif stack and word == "END":
            if starts[-1] >= 0:
                stack[-1].append((starts[-1], k))
            branches = stack.pop()
            starts.pop()
            bare = []
            for a, b in branches:
                seen = [j for j in range(a, b) if tokens[j][0] not in ("ws", "comment")]
                if len(seen) == 1 and tokens[seen[0]][1] in ("0", "1"):
                    bare.append(seen[0])
            if bare and any(_compares(tokens, *_unwrap(tokens, a, b)) for a, b in branches):
                for j in bare:
                    out[j] = "TRUE" if tokens[j][1] == "1" else "FALSE"
    return out


def _operand_end(tokens: list[tuple[str, str]], start: int) -> int:
    """Last token index of the operand starting at ``start`` (a literal, parameter,
    name, or a parenthesised / called expression)."""
    if start >= len(tokens):
        raise Untranslatable("GLOB without a pattern")
    if tokens[start][1] == "(":
        return _close(tokens, start)
    j = _next(tokens, start + 1)
    if tokens[start][0] == "word" and j < len(tokens) and tokens[j][1] == "(":
        return _close(tokens, j)
    return start


def _value(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    return value


def _members(raw: Any, *, numeric: bool) -> list[Any] | None:
    """The members of a JSON list ``ontology.sq_json_each`` (``_int``) reads, when the
    text is a JSON array of strings or nulls (of integers): exactly that function's
    values; None for anything else (another JSON shape, a number in a text list, a
    character Postgres JSON refuses), left to the function."""
    if not isinstance(raw, str):
        return None
    try:
        items = json.loads(raw, parse_constant=_refuse)
    except (ValueError, _Refused):
        return None
    if not isinstance(items, list):
        return None
    if numeric:
        return items if all(type(v) is int and -2**63 <= v < 2**63 for v in items) else None
    for v in items:
        if v is not None and (not isinstance(v, str) or "\x00" in v
                              or any("\ud800" <= ch <= "\udfff" for ch in v)):
            return None
    return items


class _Refused(Exception):
    pass


def _refuse(_: str) -> Any:
    raise _Refused   # NaN / Infinity: not JSON to Postgres


class Row:
    """A result row as ``sqlite3.Row`` gives it: by index, by column name, ``keys()``."""

    __slots__ = ("_values", "_names")

    def __init__(self, names: tuple[str, ...], values: tuple[Any, ...]) -> None:
        self._names, self._values = names, values

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            try:
                return self._values[self._names.index(key)]
            except ValueError:
                lowered = key.lower()
                for n, name in enumerate(self._names):
                    if name.lower() == lowered:
                        return self._values[n]
                raise IndexError(f"No item with that key: {key}") from None
        return self._values[key]

    def keys(self) -> list[str]:
        return list(self._names)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, Row):
            return self._names == other._names and self._values == other._values
        return tuple(self._values) == other

    def __hash__(self) -> int:
        return hash(self._values)

    def __repr__(self) -> str:
        return f"Row({dict(zip(self._names, self._values))!r})"


class Cursor:
    """The rows of one statement, fetched whole."""

    def __init__(self, names: tuple[str, ...], rows: list[tuple], rowcount: int,
                 plain: bool) -> None:
        self.description = tuple((n, None, None, None, None, None, None) for n in names)
        self.rowcount = rowcount
        self.lastrowid = None
        self._rows = [tuple(r) if plain else Row(names, r) for r in rows]
        self._at = 0

    def fetchone(self) -> Any:
        if self._at >= len(self._rows):
            return None
        self._at += 1
        return self._rows[self._at - 1]

    def fetchmany(self, size: int = 1) -> list[Any]:
        out = self._rows[self._at:self._at + size]
        self._at += len(out)
        return out

    def fetchall(self) -> list[Any]:
        out = self._rows[self._at:]
        self._at = len(self._rows)
        return out

    def __iter__(self) -> Iterator[Any]:
        while self._at < len(self._rows):
            self._at += 1
            yield self._rows[self._at - 1]

    def close(self) -> None:
        self._rows = []


class PgCompatConnection:
    """A ``sqlite3.Connection`` look-alike over the Postgres ontology (module docstring).
    Thread-safe: one statement at a time."""

    in_transaction = False
    total_changes = 0
    # A caller whose SQLite query reads a table Postgres holds in another shape (the
    # packed value layouts: one Postgres cell per field) asks this to take its own form.
    postgres = True

    def __init__(self, dsn: str, *, statement_timeout_ms: int = STATEMENT_TIMEOUT_MS) -> None:
        import psycopg

        self.dsn = dsn
        # client-side binding: a parameter is a literal typed by its context, as in
        # SQLite (``? IS NULL`` with None, a text compared with a number)
        self._pg = psycopg.connect(dsn, autocommit=True, cursor_factory=psycopg.ClientCursor)
        self._lock = threading.RLock()
        self._functions: dict[str, Callable[[Any], Any]] = {}
        self._trace: Callable[[str], Any] | None = None
        # A SQLite connection whose statements read with this one's functions (the word
        # index's, ``PgStoreReader.word_conn``): every registered function is registered
        # there too.
        self.mirror: sqlite3.Connection | None = None
        self.row_factory: Any = sqlite3.Row
        with self._pg.cursor() as cur:
            cur.execute("SET search_path = ontology, public")
            cur.execute("SET TimeZone = 'UTC'")
            cur.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
            cur.execute("CREATE TEMP TABLE IF NOT EXISTS sq_calls (fn text, arg text, "
                        "result boolean, PRIMARY KEY (fn, arg))")
            cur.execute("CREATE TEMP TABLE IF NOT EXISTS sq_pending (fn text, arg text, "
                        "PRIMARY KEY (fn, arg))")
        from operating.ontology_tools_traverse import GATED_SCHEMES
        self._gated = tuple(sorted(GATED_SCHEMES))

    # -- sqlite3.Connection surface ------------------------------------------------
    def execute(self, sql: str, parameters: Any = ()) -> Cursor:
        import psycopg

        with self._lock:
            if self._trace is not None:
                self._trace(sql)
            names = tuple(sorted(self._functions))
            try:
                text = translate(sql, names, self._gated)
            except Untranslatable:
                raise
            params = self._params(parameters)
            gated = "ontology.sq_call(" in text
            try:
                if "AS u(value)" in text:
                    params = self._lists(text, params)
                for _ in range(MAX_ROUNDS):
                    with self._pg.cursor() as cur:
                        cur.execute(text, params)
                        rows = cur.fetchall() if cur.description else []
                        columns = tuple(d.name for d in cur.description or ())
                        count = cur.rowcount
                    if not gated or not self._answer():
                        break
                else:
                    raise sqlite3.OperationalError("item gate did not settle")
            except psycopg.errors.QueryCanceled as exc:
                raise sqlite3.OperationalError("interrupted") from exc
            except psycopg.Error as exc:
                raise sqlite3.OperationalError(
                    f"postgres: {type(exc).__name__}: {str(exc).splitlines()[0][:300]}"
                ) from exc
            plain = self.row_factory is None
            return Cursor(columns, [tuple(_value(v) for v in r) for r in rows], count, plain)

    def executemany(self, sql: str, seq: Any) -> Cursor:
        last = Cursor((), [], 0, True)
        for parameters in seq:
            last = self.execute(sql, parameters)
        return last

    def create_function(self, name: str, narg: int, func: Callable[..., Any] | None,
                        **kwargs: Any) -> None:
        with self._lock:
            if self.mirror is not None:   # the word index's SQLite connection runs them too
                self.mirror.create_function(name, narg, func, **kwargs)
            if name in ("plx_l9_time", "plx_l9_gated"):
                return   # ported to SQL (ontology.sq_l9_time / sq_l9_gated)
            key = name.lower()
            if func is None:
                self._functions.pop(key, None)
                with self._pg.cursor() as cur:
                    cur.execute("DELETE FROM sq_calls WHERE fn = %s", (key,))
                return
            if narg != 1:
                raise sqlite3.NotSupportedError("only one-argument functions are compiled")
            self._functions[key] = func

    def set_progress_handler(self, *_: Any) -> None:
        """No step callbacks on Postgres: ``statement_timeout`` bounds a statement."""

    def set_trace_callback(self, callback: Callable[[str], Any] | None) -> None:
        """``callback`` gets each statement's SQLite text as it runs (as sqlite3 does)."""
        self._trace = callback

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def interrupt(self) -> None:
        self._pg.cancel_safe() if hasattr(self._pg, "cancel_safe") else self._pg.cancel()

    def close(self) -> None:
        self._pg.close()

    def __enter__(self) -> "PgCompatConnection":
        return self

    def __exit__(self, *_: Any) -> None:
        pass

    # -- internals -----------------------------------------------------------------
    @staticmethod
    def _params(parameters: Any) -> Any:
        if isinstance(parameters, Mapping):
            return dict(parameters)
        return tuple(parameters or ())

    def _lists(self, text: str, params: Any) -> Any:
        """``params`` with each IN list ``translate`` binds as an array (``unnest(...)``):
        the members ``ontology.sq_json_each`` (``_int``) gives for the JSON text bound,
        read in Python when it is a plain list of strings (of integers), else by that
        function itself."""
        named = isinstance(params, Mapping)
        out: Any = dict(params) if named else list(params)
        at = -1
        for m in _PLACEHOLDER.finditer(text):
            if m.group(0) == "%%":
                continue
            if m.group(1) is None:
                at += 1
            if text[max(0, m.start() - 7):m.start()] != "unnest(":
                continue
            numeric = next((n for tail, n in _LISTED_AS.items()
                            if text.startswith(tail, m.end())), None)
            if numeric is None:
                continue
            if m.group(1) is None:
                if named or at >= len(out):
                    raise sqlite3.ProgrammingError("a listed parameter has no value")
                out[at] = self._members_of(out[at], numeric)
            else:
                key = m.group(1)
                base = key[:-len(LIST_SUFFIX[numeric])]
                if not named or not key.endswith(LIST_SUFFIX[numeric]) or base not in params:
                    raise sqlite3.ProgrammingError(f"no value for parameter :{base}")
                out[key] = self._members_of(params[base], numeric)
        return out if named else tuple(out)

    def _members_of(self, raw: Any, numeric: bool) -> list[Any]:
        members = _members(raw, numeric=numeric)
        if members is None:
            with self._pg.cursor() as cur:
                cur.execute(_FALLBACK[numeric], (raw,))
                members = list(cur.fetchone()[0] or [])
        return members

    def _answer(self) -> int:
        """Answer the queued function calls in Python; how many were queued."""
        with self._pg.cursor() as cur:
            cur.execute("DELETE FROM sq_pending RETURNING fn, arg")
            pending = cur.fetchall()
            if not pending:
                return 0
            answers = []
            for fn, arg in pending:
                func = self._functions.get(fn)
                try:
                    answers.append((fn, arg, bool(func(arg)) if func else False))
                except Exception:  # noqa: BLE001 - a failing gate denies
                    answers.append((fn, arg, False))
            with cur.copy("COPY sq_calls (fn, arg, result) FROM STDIN") as copy:
                for row in answers:
                    copy.write_row(row)
        return len(pending)


__all__ = ["MAX_ROUNDS", "PgCompatConnection", "Row", "Untranslatable", "translate"]
