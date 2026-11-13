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

A long JSON list of strings (an owner's audience keys) is written once per session to a
temp table and read from it by its digest (``_kept_lists``), so the planner does not weigh
each member on every statement.

Rows are :class:`Row` (index and name access, ``keys()``, as ``sqlite3.Row``) with
SQLite's value types: a boolean is 0/1, an integral numeric an int, bytea bytes. A
Postgres error is raised as ``sqlite3.OperationalError``, so the tools' existing handling
(``store_unavailable``) applies. Stdlib and psycopg.

A lost connection (Postgres restarted, the backend terminated) is replaced: before each
statement the held connection is checked without a round trip (``pool.alive``) and, when
the server went away, opened again with the same session; a read-only statement
(``read_only``) that failed because the connection was lost under it runs once more on
the new one. A write, or any statement inside a transaction the caller opened, is never
run again: its error is raised and the next statement gets the new connection. Each loss
is logged once.
"""

from __future__ import annotations

from decimal import Decimal
from contextlib import contextmanager
import functools
import hashlib
import json
import logging
import re
import select
import sqlite3
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Sequence

MAX_ROUNDS = 24
# A streamed read (``PgCompatConnection.stream``) takes this many rows a round trip, on one
# of at most this many connections of its own (none free: the read is run whole).
STREAM_BATCH = 2000
STREAM_CONNECTIONS = 2
# The seconds a reconnect may take: the caller's lock is held meanwhile.
RECONNECT_TIMEOUT_S = 5
# How long a command left in progress is given to end after its cancel (``live``).
ABANDON_SECONDS = 2.0
log = logging.getLogger(__name__)
# What a read-only statement never holds (``read_only``): any of these words, wherever it
# stands (a string or a name that holds one only costs that statement its retry).
_NOT_READ_ONLY = re.compile(
    r"\b(INSERT|UPDATE|DELETE|REPLACE|UPSERT|MERGE|INTO|CREATE|DROP|ALTER|TRUNCATE|COPY"
    r"|GRANT|REVOKE|VACUUM|ANALYZE|REINDEX|CLUSTER|LOCK|CALL|DO|BEGIN|START|COMMIT"
    r"|END|ROLLBACK|SAVEPOINT|RELEASE|PREPARE|EXECUTE|DEALLOCATE|SET|RESET|ATTACH|DETACH"
    r"|NOTIFY|LISTEN|REFRESH|IMPORT|SECURITY|FOR\s+(?:NO\s+KEY\s+)?(?:UPDATE|SHARE|KEY)"
    r"|NEXTVAL|SETVAL|PG_\w+|LO_\w+|DBLINK\w*)\b", re.IGNORECASE)
_LEADING_COMMENTS = re.compile(r"^(?:\s+|--[^\n]*\n?|/\*.*?\*/)*", re.DOTALL)
STATEMENT_TIMEOUT_MS = 120_000
# How often a running statement checks that its client is still there (Postgres 14+): a
# process killed mid-read (an API stopped, a facts read's parent gone) otherwise leaves
# its statement scanning, and spilling to temp files, until it next sends a row.
CLIENT_CHECK = "5s"
# A table's SQLite rowid in the Postgres ontology.
ROWID = {"objects": "row_no", "link_rows": "row_no",
         "object_versions": "ver", "value_sources": "src", "text_fields": "field_id",
         "text_chunks": "chunk_id", "link_texts": "id", "link_skeletons": "id",
         "value_fields": "field", "value_kinds": "kind",
         "value_text": "row_no", "chunk_text": "chunk_id"}
# The word index's FTS5 tables and their Postgres tables (migration 0010).
WORD_TABLES = {"value_text": "value_words", "chunk_text": "chunk_words"}
# Columns compared with a json_each list of numbers (the list read as bigint).
INT_COLUMNS = frozenset({"rowid", "ver", "src", "field_id", "chunk_id", "layout", "field",
                         "kind", "id", "pos", "row_no", "ek"})
# A ``json_each(:name)`` IN list bound as an array: the parameter's suffix (text, bigint),
# its members read in Python (``_members``), else by the very SQL function (``_FALLBACK``).
LIST_SUFFIX = {False: "__list_text", True: "__list_int"}
_PLACEHOLDER = re.compile(r"%%|%s|%\(([A-Za-z_][A-Za-z0-9_]*)\)s")
_LISTED_AS = {"::text[]) AS u(value)": False, "::bigint[]) AS u(value)": True}
# A text IN list this long (JSON characters) is kept on the connection and read from there
# (``PgCompatConnection._kept_lists``): bound as constants, every statement was planned
# over each member (plan_cache_mode is force_custom_plan), so an owner's 4,488 audience
# keys (633 KB) cost 65-85 ms of planning per statement against 1-3 ms of execution, and
# a cold record page ran thousands of such statements (11-11: 45-150 s).
LONG_LIST_CHARS = 4096
# How many kept lists a connection holds before it empties them all and starts again.
MAX_KEPT_LISTS = 256
_KEPT_ANY = re.compile(
    r"(= ANY|<> ALL)\(ontology\.sq_json_array\((%s|%\(([A-Za-z_][A-Za-z0-9_]*)\)s)\)\)")
# A long list compared with a link's end (``links.from_object`` / ``to_object``): bound as an
# array, an index condition (``PgCompatConnection._link_end_lists``).
_LINK_END = re.compile(r"\b(?:[A-Za-z_][A-Za-z0-9_]*\.)?(?:from_object|to_object)\s*\Z")
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
    """SQL the compatibility layer does not run on Postgres (FTS5 MATCH while the word
    tables are not loaded, PRAGMA writes)."""


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
                    "USING", "INDEXED", "NOT", "UNION", "SET", "MATCH"):
                out[tokens[j][1]] = table
    return out


def _word_fence(tokens: list[tuple[str, str]], aliases: Mapping[str, str], table: str,
                left: int, operand: int) -> int | None:
    """The token naming the word table ``table`` in the FROM clause, when its MATCH can be
    read first as a subquery (``translate``): a named parameter, the table named once
    besides the MATCH's left side, under an alias of its own."""
    if not tokens[operand][1].startswith(":"):
        return None   # a positional parameter would move
    named = [i for i, (kind, text) in enumerate(tokens)
             if kind == "word" and text.lower() == table and i != left]
    if len(named) != 1 or not any(a != table and t == table for a, t in aliases.items()):
        return None
    return named[0]


def _matches(tokens: list[tuple[str, str]], aliases: Mapping[str, str]
             ) -> dict[int, tuple[int, int, str]]:
    """Each FTS5 ``<table or alias> MATCH <operand>`` over the word index: the MATCH
    token's index -> (left operand's index, operand's last index, the Postgres name of
    the word table's row: its alias, else the table)."""
    out: dict[int, tuple[int, int, str]] = {}
    for i, (kind, text) in enumerate(tokens):
        if kind != "word" or text.upper() != "MATCH":
            continue
        left = _prev(tokens, i - 1)
        name = tokens[left][1] if left >= 0 else ""
        table = aliases.get(name)
        if table not in WORD_TABLES:
            raise Untranslatable(f"full-text MATCH on {name or 'nothing'}")
        named = [a for a, t in aliases.items() if t == table and a != table]
        qualifier = name if name != table else (named[0] if len(named) == 1
                                               else WORD_TABLES[table])
        operand = _next(tokens, i + 1)
        if operand >= len(tokens) or tokens[operand][0] != "param":
            raise Untranslatable("full-text MATCH of no parameter")
        out[i] = (left, operand, qualifier)
    return out


@functools.lru_cache(maxsize=4096)
def match_parameters(sql: str) -> tuple[str | int, ...]:
    """The parameters of ``sql`` that are FTS5 queries (a ``MATCH`` operand): a name, or
    a ``?``'s position; their values are turned into ``tsquery`` text (``words.tsquery``)."""
    tokens = _tokens(sql)
    out: list[str | int] = []
    positions = {i: n for n, i in enumerate(k for k, t in enumerate(tokens)
                                             if t[0] == "param" and t[1] == "?")}
    for _left, operand, _qualifier in _matches(tokens, _aliases(tokens)).values():
        text = tokens[operand][1]
        out.append(positions[operand] if text == "?" else text[1:])
    return tuple(out)


@functools.lru_cache(maxsize=4096)
def translate(sql: str, functions: tuple[str, ...] = (),
              gated_schemes: tuple[str, ...] = (), words: Any = (),
              arrays: bool = False) -> str:
    """SQLite SQL -> Postgres SQL for psycopg (``%s`` / ``%(name)s`` parameters).
    ``functions``: names registered in Python (``sq_call``); ``gated_schemes``: the
    schemes ``plx_l9_gated`` keeps; ``words``: the FTS5 tables whose word index is
    loaded (``True``: both), so such a table reads it (``value_text`` as ``value_words``,
    ``chunk_text`` as ``chunk_words``,
    ``X MATCH :q`` as ``X.words @@ CAST(:q AS tsquery)``, the parameter
    turned into tsquery text by the connection: ``match_parameters``); without them a
    word-index query raises ``Untranslatable``."""
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
    replace.update(_index_hints(tokens))
    if arrays:
        replace.update(_json_lists(tokens))
    loaded = frozenset(WORD_TABLES) if words is True else frozenset(words or ())
    fenced: list[str] = []   # the word tables read first (``_word_fence``), as CTEs
    for at, (left, operand, qualifier) in _matches(tokens, aliases).items():
        table = aliases.get(tokens[left][1])
        if table not in loaded:
            raise Untranslatable("full-text MATCH: the word index is not on Postgres")
        fence = _word_fence(tokens, aliases, table, left, operand)
        if fence is not None:
            # the word rows first, from their GIN index, once (a materialized CTE): with
            # the match a join filter the planner read every row of the joined tables and
            # cut the words of each (12 s a page on 1.3 M value cells), and a subquery it
            # read again per outer row (100 s for a common word over 144 k chunks)
            name = f"sq_words_{len(fenced)}"
            fenced.append(f"{name} AS MATERIALIZED (SELECT * FROM {WORD_TABLES[table]} "
                          f"WHERE words @@ CAST(%({tokens[operand][1][1:]})s AS tsquery))")
            replace[fence] = name
            replace[left], replace[at], replace[operand] = "TRUE", "", ""
            continue
        replace[left] = f"{qualifier}.words"
        replace[at] = "@@ CAST("
        suffix[operand] = " AS tsquery)"
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
        elif kind == "word" and text.lower() in WORD_TABLES and text.lower() not in loaded:
            raise Untranslatable("the word index is not on Postgres")
        elif kind == "word" and text.lower() in WORD_TABLES:
            out.append(WORD_TABLES[text.lower()])
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
                # SQLite reads a JSON function's result inside another as JSON: only the
                # outermost is text (else ``json_group_array(json_array(a, b))`` would be
                # an array of strings, each an encoded array)
                p = _prev(tokens, i - 1)
                p2 = _prev(tokens, p - 1) if p >= 0 else -1
                nested = p2 >= 0 and tokens[p][1] == "(" and \
                    tokens[p2][1].lower() in ("json_array", "json_group_array")
                if not nested:
                    suffix[_close(tokens, j)] = "::text"
            elif call and name == "group_concat":
                close = _close(tokens, j)
                spans = _args(tokens, j, close)
                out.append("string_agg((")
                inner = translate("".join(t for _, t in tokens[spans[0][0]:spans[0][1]]),
                                  functions, gated_schemes, words, arrays)
                out.append(inner + ")::text, ")
                out.append(translate("".join(t for _, t in tokens[spans[1][0]:spans[1][1]]),
                                     functions, gated_schemes, words, arrays)
                           if len(spans) > 1 else "','")
                out.append(")")
                skip_until = close
            elif call and name in ("max", "min"):
                close = _close(tokens, j)
                spans = _args(tokens, j, close)
                if len(spans) > 1:
                    # SQLite's scalar max/min is NULL when any argument is; greatest/least
                    # skip NULLs. Each argument becomes a row of a VALUES list (written
                    # once, so a positional parameter is not repeated), NULL if any is.
                    out.append("(SELECT CASE WHEN count(sq_mm.v) < count(*) THEN NULL "
                               f"ELSE {name}(sq_mm.v) END FROM (VALUES ")
                    for _, end in spans[:-1]:
                        replace[end] = "),("
                    suffix[close] = ") AS sq_mm(v))"
                else:
                    out.append(text)
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
    text = "".join(out)
    if fenced:
        head = re.match(r"(?is)^\s*WITH(\s+RECURSIVE)?\s+", text)
        ctes = ", ".join(fenced)
        text = (text[:head.end()] + ctes + ", " + text[head.end():] if head
                else "WITH " + ctes + " " + text)
    return text


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


def _json_lists(tokens: list[tuple[str, str]]) -> dict[int, str]:
    """``x [NOT] IN (SELECT value FROM json_each(:list))`` as ``x = ANY(...)`` (``<> ALL``) of
    the list as an array (``ontology.sq_json_array``, migration 0011; of numbers for a
    numbered column, as ``json_each`` reads it): the token replacements that write it."""
    out: dict[int, str] = {}
    shape = ["(", "select", "value", "from", "json_each", "(", None, ")", ")"]
    for i, (kind, text) in enumerate(tokens):
        if kind != "word" or text.upper() != "IN":
            continue
        at, seen = i, []
        for want in shape:
            at = _next(tokens, at + 1)
            if at >= len(tokens):
                break
            got_kind, got = tokens[at]
            if want is None:
                if got_kind != "param":
                    break
            elif got.lower() != want:
                break
            seen.append(at)
        if len(seen) != len(shape):
            continue
        param = tokens[seen[6]][1]
        bound = "%s" if param == "?" else f"%({param[1:]})s"
        before = _prev(tokens, i - 1)
        negated = before >= 0 and tokens[before][1].upper() == "NOT"
        column = _prev(tokens, before - 1) if negated else before
        name = tokens[column][1].split(".")[-1].lower() if column >= 0 else ""
        function = "sq_json_array_int" if name in INT_COLUMNS else "sq_json_array"
        if negated:
            out[before] = ""
        out[i] = f"{'<> ALL' if negated else '= ANY'}(ontology.{function}({bound}))"
        for k in range(i + 1, seen[-1] + 1):
            out[k] = ""
    return out


def _index_hints(tokens: list[tuple[str, str]]) -> dict[int, str]:
    """SQLite's planner hint ``+column`` (no index on the column) where a condition starts
    (after WHERE/AND/OR/ON/NOT or an opening parenthesis), dropped: Postgres has no unary
    plus on text and plans without it. A ``+`` between two operands is an addition, kept."""
    out: dict[int, str] = {}
    for k, (_kind, text) in enumerate(tokens):
        if text != "+":
            continue
        before = _prev(tokens, k - 1)
        after = _next(tokens, k + 1)
        if before < 0 or after >= len(tokens) or tokens[after][0] != "word":
            continue
        if tokens[before][1] == "(" or (tokens[before][0] == "word" and tokens[before][1].upper()
                                         in ("WHERE", "AND", "OR", "ON", "NOT")):
            out[k] = ""
    return out


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
    """The rows of one statement, fetched whole. Each row is made (its values converted
    with ``convert``, then a ``Row`` unless ``plain``) as it is taken, not all up front:
    the driver's row list is the one full copy a result holds."""

    def __init__(self, names: tuple[str, ...], rows: list[tuple], rowcount: int,
                 plain: bool, *, convert: bool = False) -> None:
        self.description = tuple((n, None, None, None, None, None, None) for n in names)
        self.rowcount = rowcount
        self.lastrowid = None
        self._names, self._plain, self._convert = names, plain, convert
        self._rows = rows
        self._at = 0

    def _make(self, raw: Any) -> Any:
        values = tuple(_value(v) for v in raw) if self._convert else tuple(raw)
        return values if self._plain else Row(self._names, values)

    def fetchone(self) -> Any:
        if self._at >= len(self._rows):
            return None
        self._at += 1
        return self._make(self._rows[self._at - 1])

    def fetchmany(self, size: int = 1) -> list[Any]:
        out = [self._make(r) for r in self._rows[self._at:self._at + size]]
        self._at += len(out)
        return out

    def fetchall(self) -> list[Any]:
        out = [self._make(r) for r in self._rows[self._at:]]
        self._at = len(self._rows)
        return out

    def __iter__(self) -> Iterator[Any]:
        while self._at < len(self._rows):
            self._at += 1
            yield self._make(self._rows[self._at - 1])

    def close(self) -> None:
        self._rows = []


class StreamCursor:
    """The rows of one read taken from a server-side cursor ``batch`` rows at a time
    (``PgCompatConnection.stream``): what the caller has not asked for is not read. Holds
    its own connection's transaction until it is read to the end or closed."""

    def __init__(self, owner: "PgCompatConnection", conn: Any, cur: Any, batch: int,
                 plain: bool) -> None:
        self._owner, self._conn, self._cur, self._batch = owner, conn, cur, batch
        self.description = cur.description
        self._names = tuple(d.name for d in cur.description or ())
        self._plain = plain
        self.rowcount = -1
        self.lastrowid = None
        self.fetched = 0   # rows read from the server so far
        self._buffer: list[Any] = []
        self._at = 0
        self._done = False

    def _make(self, raw: Any) -> Any:
        values = tuple(_value(v) for v in raw)
        return values if self._plain else Row(self._names, values)

    def _fill(self) -> bool:
        """Whether a row is buffered, reading the next batch when none is."""
        if self._at < len(self._buffer):
            return True
        if self._done:
            return False
        import psycopg
        try:
            self._buffer, self._at = self._cur.fetchmany(self._batch), 0
        except psycopg.errors.QueryCanceled as exc:
            self.close()
            raise sqlite3.OperationalError("interrupted") from exc
        except psycopg.Error as exc:
            self.close()
            raise sqlite3.OperationalError(
                f"postgres: {type(exc).__name__}: {str(exc).splitlines()[0][:300]}"
            ) from exc
        self.fetched += len(self._buffer)
        if len(self._buffer) < self._batch:
            self.close(keep_buffer=True)
        return self._at < len(self._buffer)

    def fetchone(self) -> Any:
        if not self._fill():
            return None
        self._at += 1
        return self._make(self._buffer[self._at - 1])

    def fetchmany(self, size: int = 1) -> list[Any]:
        out = []
        while len(out) < size and self._fill():
            take = self._buffer[self._at:self._at + size - len(out)]
            self._at += len(take)
            out.extend(self._make(r) for r in take)
        return out

    def fetchall(self) -> list[Any]:
        out = []
        while self._fill():
            out.extend(self._make(r) for r in self._buffer[self._at:])
            self._at = len(self._buffer)
        return out

    def __iter__(self) -> Iterator[Any]:
        while (row := self.fetchone()) is not None:
            yield row

    def close(self, *, keep_buffer: bool = False) -> None:
        if not keep_buffer:
            self._buffer, self._at = [], 0
        if self._done:
            return
        self._done = True
        self._owner._stream_done(self._conn, self._cur)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001 - interpreter teardown
            pass


def read_only(sql: str) -> bool:
    """Whether ``sql`` (the caller's SQLite text) is one plain read that may run again
    after its connection was lost: it starts with SELECT, WITH or VALUES and carries no
    word of a write, a transaction or a session change (``_NOT_READ_ONLY``). Anything
    else, or anything unsure, is not."""
    if not isinstance(sql, str):
        return False
    body = _LEADING_COMMENTS.sub("", sql, count=1)
    head = re.match(r"[A-Za-z]+", body)
    if head is None or head.group(0).upper() not in ("SELECT", "WITH", "VALUES"):
        return False
    return ";" not in body.rstrip().rstrip(";") and not _NOT_READ_ONLY.search(body)


class PgCompatConnection:
    """A ``sqlite3.Connection`` look-alike over the Postgres ontology (module docstring).
    Thread-safe: one statement at a time."""

    in_transaction = False
    total_changes = 0
    # A caller whose SQLite query reads a table Postgres holds in another shape (the
    # packed value layouts: one Postgres cell per field) asks this to take its own form.
    postgres = True

    def __init__(self, dsn: str, *, statement_timeout_ms: int = STATEMENT_TIMEOUT_MS,
                 temp_limit: str | None = None) -> None:
        self.dsn = dsn
        self._statement_timeout_ms = int(statement_timeout_ms)
        # ``temp_limit``: the most temporary file space one statement of this session may
        # write (``temp_file_limit``, e.g. "1GB"); past it the statement fails, never the
        # disk. Only a superuser may set it: elsewhere the server's own limit holds.
        if temp_limit is not None and not re.fullmatch(r"[0-9]{1,9}(kB|MB|GB)", temp_limit):
            raise ValueError("temp_limit_unreadable")
        self._temp_limit = temp_limit
        self._lock = threading.RLock()
        self._closed = False
        self._down_logged = False   # a reconnect that failed is logged once, until one works
        self._functions: dict[str, Callable[[Any], Any]] = {}
        # statements whose parameters bind client side (``_run``)
        self._client_only: set[str] = set()
        self._trace: Callable[[str], Any] | None = None
        # A SQLite connection whose statements read with this one's functions (the word
        # index's, ``PgStoreReader.word_conn``): every registered function is registered
        # there too.
        self.mirror: sqlite3.Connection | None = None
        # The FTS5 tables whose word index is loaded (``PgStoreReader`` sets it): their
        # queries run here only then, else they raise (``translate``).
        self.words: tuple[str, ...] = ()
        self.row_factory: Any = sqlite3.Row
        # idle connections streamed reads run on, and how many are open (``stream``)
        self._streams: list[Any] = []
        self._streams_open = 0
        self._streams_lock = threading.Lock()
        # the stream connections reading now (``interrupt`` cancels their statements too)
        self._streams_busy: set[Any] = set()
        self._conn = self._open()
        from operating.ontology_tools_traverse import GATED_SCHEMES
        self._gated = tuple(sorted(GATED_SCHEMES))

    def _open(self, **kwargs: Any) -> Any:
        """A new connection with this connection's session (search path, time zone,
        statement timeout, planner settings, the item gate's temp tables)."""
        import psycopg

        # client-side binding: a parameter is a literal typed by its context, as in
        # SQLite (``? IS NULL`` with None, a text compared with a number)
        conn = psycopg.connect(self.dsn, autocommit=True, cursor_factory=psycopg.ClientCursor,
                               **kwargs)
        try:
            self._session(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    def _session(self, conn: Any) -> None:
        import psycopg

        statement_timeout_ms = self._statement_timeout_ms
        with conn.cursor() as cur:
            cur.execute("SET search_path = ontology, public")
            cur.execute("SET TimeZone = 'UTC'")
            cur.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
            for setting, value in (("client_connection_check_interval", CLIENT_CHECK),
                                   ("temp_file_limit", self._temp_limit)):
                if value is None:
                    continue
                try:
                    with conn.transaction():
                        cur.execute(f"SET {setting} = '{value}'")
                except psycopg.Error:
                    pass   # an older server, or no right to set it: its own setting holds
            # no JIT: these are short indexed reads, and a plan with a scan kind turned
            # off (the word windows) carries a cost far past ``jit_above_cost``, so
            # each one compiled for ~0.3 s before running for milliseconds
            cur.execute("SET jit = off")
            # each execution planned for its own parameters: a prepared statement keeps
            # its parse, but the generic plan Postgres picks after five runs cannot see
            # how many ids a JSON list holds, and read a find's candidate versions,
            # chunks and values 2-3x slower (p95 of a find 0.99 s against 0.47 s)
            cur.execute("SET plan_cache_mode = force_custom_plan")
            # JSON lists read as arrays once migration 0011 made their functions
            cur.execute("SELECT to_regproc('ontology.sq_json_array_int') IS NOT NULL")
            self.arrays = bool(cur.fetchone()[0])
            cur.execute("CREATE TEMP TABLE IF NOT EXISTS sq_calls (fn text, arg text, "
                        "result boolean, PRIMARY KEY (fn, arg))")
            cur.execute("CREATE TEMP TABLE IF NOT EXISTS sq_pending (fn text, arg text, "
                        "PRIMARY KEY (fn, arg))")
            # the long IN lists this session holds (``_kept_lists``): a new session holds none
            cur.execute("CREATE TEMP TABLE IF NOT EXISTS sq_lists (digest text PRIMARY KEY, "
                        "members text[] NOT NULL)")
            cur.execute("DELETE FROM sq_lists")
        self._kept: dict[str, str] = {}

    @contextmanager
    def deadline_bounded(self) -> Iterator[None]:
        """Each statement run in this block (on this thread, which holds the connection
        throughout) ends within the time the thread's SQL deadline left on entry (``call_limits.sql_deadline``), which
        Postgres statements do not otherwise see (``set_progress_handler`` is a no-op
        here): the session's ``statement_timeout`` lowered to the time left, a statement
        past it cancelled as ``sqlite3.OperationalError("interrupted")`` (as a SQLite
        interrupt). No deadline: as before; none left: interrupted at once."""
        from operating import call_limits
        left = call_limits.seconds_left()
        if left is None:
            yield
            return
        if left <= 0:
            raise sqlite3.OperationalError("interrupted")
        with self._lock:
            pg = self._pg
            (before,) = pg.execute("SHOW statement_timeout").fetchone()
            pg.execute(f"SET statement_timeout = {max(1, int(left * 1000))}")
            try:
                yield
            finally:
                try:
                    pg.execute(f"SET statement_timeout = '{before}'")
                except Exception:  # noqa: BLE001 - a lost session is opened anew with its own
                    pass

    # -- the held connection, replaced when lost --------------------------------------
    @property
    def _pg(self) -> Any:
        """The live psycopg connection (``live``): what the readers' own Postgres
        queries run on."""
        return self.live()

    def live(self) -> Any:
        """The held connection, opened again first when its server went away (module
        docstring). Lost while a transaction was open on it: the new connection is made
        and the loss raised (``sqlite3.OperationalError``), never hidden from the
        transaction's owner."""
        import psycopg
        from operating.pg.pool import alive

        with self._lock:
            if self._closed:
                raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
            conn = self._conn
            # read before the probe: a lost connection's status is unknown afterwards
            status = conn.pgconn.transaction_status if not conn.closed else None
            if status == psycopg.pq.TransactionStatus.ACTIVE:
                # Nothing of this process runs a statement while this lock is free, so a
                # command still in progress was abandoned part way (a tool call stopped at
                # its limit, dev 11-11): every later statement would fail "another command
                # is already in progress". It is cancelled and its results read (bounded),
                # then the connection replaced; inside a transaction the loss is raised.
                status = self._abandon(conn)
                self._reconnect("left with a command in progress; cancelled")
                if status in (psycopg.pq.TransactionStatus.INTRANS,
                              psycopg.pq.TransactionStatus.INERROR):
                    raise sqlite3.OperationalError(
                        "postgres: a statement was abandoned inside a transaction")
                if status is None:
                    raise sqlite3.OperationalError(
                        "postgres: a statement was abandoned and the connection's "
                        "transaction state could not be read")
                return self._conn
            if alive(conn):
                return conn
            self._reconnect("found lost before a statement")
            if status in (psycopg.pq.TransactionStatus.INTRANS,
                          psycopg.pq.TransactionStatus.INERROR):
                raise sqlite3.OperationalError(
                    "postgres: the connection was lost inside a transaction")
            return self._conn

    @staticmethod
    def _abandon(conn: Any) -> Any:
        """Cancel ``conn``'s command in progress and read its results within
        ``ABANDON_SECONDS`` (checked on every pass): the transaction status after it, None
        when that could not be learnt in time. A COPY left open is ended: ``COPY FROM
        STDIN`` (the item gate's ``sq_calls`` load) is ended with an error, ``COPY TO``
        data is read and dropped. The caller replaces the connection in every case and
        logs the one line."""
        import psycopg
        ExecStatus = psycopg.pq.ExecStatus
        deadline = time.monotonic() + ABANDON_SECONDS
        try:
            conn.cancel_safe(timeout=ABANDON_SECONDS) if hasattr(conn, "cancel_safe") \
                else conn.cancel()
            pgconn = conn.pgconn
            copying = False
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                pgconn.consume_input()
                if copying:
                    # COPY TO data still coming: read it and drop it (-1: the copy ended)
                    n, _ = pgconn.get_copy_data(1)
                    if n == 0:
                        select.select([pgconn.socket], [], [], left)
                    elif n < 0:
                        copying = False
                    continue
                if pgconn.is_busy():
                    select.select([pgconn.socket], [], [], left)
                    continue
                result = pgconn.get_result()
                if result is None:
                    break
                if result.status == ExecStatus.COPY_IN:
                    # the server waits for rows: end the copy with an error (it fails
                    # and loads nothing), then read on to its end
                    while (sent := pgconn.put_copy_end(b"abandoned")) == 0:
                        left = deadline - time.monotonic()   # 0: would block, try again
                        if left <= 0:
                            return None
                        select.select([], [pgconn.socket], [], left)
                    if sent < 0:
                        return None
                    while pgconn.flush() == 1:
                        left = deadline - time.monotonic()
                        if left <= 0:
                            return None
                        select.select([], [pgconn.socket], [], left)
                elif result.status in (ExecStatus.COPY_OUT, ExecStatus.COPY_BOTH):
                    copying = True
            status = pgconn.transaction_status
            return None if status == psycopg.pq.TransactionStatus.ACTIVE else status
        except Exception:  # noqa: BLE001 - unknown: the caller replaces it anyway
            return None

    def _lost(self) -> bool:
        from operating.pg.pool import alive
        return not alive(self._conn)

    def _reconnect(self, why: str, *, retry: bool = False) -> None:
        """Replace the held connection (bounded by ``RECONNECT_TIMEOUT_S``); one log line
        per loss, and one for a run of failed reconnects."""
        old = self._conn
        try:
            old.close()
        except Exception:  # noqa: BLE001 - already gone
            pass
        try:
            self._conn = self._open(connect_timeout=RECONNECT_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - raised as the store's error
            if not self._down_logged:
                log.warning("postgres connection lost (%s); reconnect failed: %s", why,
                            type(exc).__name__)
                self._down_logged = True
            raise sqlite3.OperationalError(
                f"postgres: reconnect failed: {type(exc).__name__}") from exc
        self._down_logged = False
        log.warning("postgres connection lost (%s); reconnected%s", why,
                    ", running the read once more" if retry else "")

    # -- sqlite3.Connection surface ------------------------------------------------
    def execute(self, sql: str, parameters: Any = ()) -> Cursor:
        """Run ``sql``; a read-only statement whose connection was lost under it runs
        once more on a new one (module docstring)."""
        import psycopg

        with self._lock:
            if self._trace is not None:
                self._trace(sql)
            retried = False
            while True:
                conn = self.live()
                status = conn.pgconn.transaction_status
                try:
                    return self._execute(sql, parameters)
                except sqlite3.OperationalError as exc:
                    cause = exc.__cause__
                    if not isinstance(cause, (psycopg.OperationalError,
                                              psycopg.InterfaceError)) or not self._lost():
                        raise
                    again = (not retried and read_only(sql)
                             and status == psycopg.pq.TransactionStatus.IDLE)
                    # the next statement gets a new connection either way
                    self._reconnect("lost during a statement", retry=again)
                    if not again:
                        raise
                    retried = True

    def _execute(self, sql: str, parameters: Any) -> Cursor:
        import psycopg

        names = tuple(sorted(self._functions))
        text = translate(sql, names, self._gated, self.words, self.arrays)
        params = self._params(parameters)
        if self.words and "MATCH" in sql.upper():
            params = self._word_queries(sql, params)
        gated = "ontology.sq_call(" in text
        # a word query read from its GIN index first (``translate``): the planner's
        # estimates of the joins after it are far off (a page of a common document
        # word: 120 s in its order, 0.7 s in the written one)
        fenced = text.startswith("WITH sq_words_")
        try:
            if "AS u(value)" in text:
                params = self._lists(text, params)
            if "sq_json_array(" in text:
                text, params = self._kept_lists(text, params)
            for _ in range(MAX_ROUNDS):
                with self._cursor(text) as cur:
                    if fenced:   # joined from the word rows, in the written order
                        cur.execute("SET join_collapse_limit = 1")
                    try:
                        rows, columns, count = self._run(cur, text, params)
                    finally:
                        if fenced:
                            cur.execute("RESET join_collapse_limit")
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
        return Cursor(columns, rows, count, plain, convert=True)

    def _cursor(self, text: str) -> Any:
        """A server-side cursor (the statement prepared once, its plan reused) unless the
        statement was seen to need client-side binding (``_run``)."""
        import psycopg
        if text in self._client_only:
            return psycopg.ClientCursor(self._conn)
        return psycopg.Cursor(self._conn)

    def _run(self, cur: Any, text: str, params: Any) -> tuple[list, tuple, int]:
        """(rows, column names, row count) of ``text`` with ``params``: prepared on the
        server (each statement planned once per connection, not on every call: 10-40 ms
        of a 60 ms read), else, when a parameter's type is not inferred from its place as a
        literal's would be (``? IS NULL``, an integer beside a text), bound client side as
        before; that statement is bound so from then on."""
        import psycopg

        def read(c: Any) -> tuple[list, tuple, int]:
            return (c.fetchall() if c.description else [],
                    tuple(d.name for d in c.description or ()), c.rowcount)
        if isinstance(cur, psycopg.ClientCursor) or not params:
            cur.execute(text, params)
            return read(cur)
        try:
            cur.execute(text, params, prepare=True)
            return read(cur)
        except (psycopg.errors.IndeterminateDatatype, psycopg.errors.UndefinedFunction,
                psycopg.errors.DatatypeMismatch, psycopg.errors.AmbiguousFunction,
                psycopg.errors.InvalidTextRepresentation, psycopg.errors.AmbiguousParameter,
                psycopg.errors.InvalidParameterValue, psycopg.errors.CannotCoerce):
            if len(self._client_only) > 4096:
                self._client_only.clear()
            self._client_only.add(text)
        with psycopg.ClientCursor(self._conn) as client:
            client.execute(text, params)
            return read(client)

    def stream(self, sql: str, parameters: Any = (), *, batch: int = STREAM_BATCH) -> Any:
        """A read whose rows are taken from a server-side cursor ``batch`` at a time as the
        caller goes (``StreamCursor``), on a connection of its own, so a scan that stops
        early reads no further and a long one holds one batch in memory, not the whole
        result. A statement that is not one plain read, that asks the item gate or a
        session's kept lists, or finds no stream connection free runs whole
        (``execute``), with the same rows."""
        import psycopg

        if not isinstance(batch, int) or isinstance(batch, bool) or batch < 1:
            raise ValueError(f"stream_batch_invalid:{batch!r}")
        if not read_only(sql):
            return self.execute(sql, parameters)
        with self._lock:
            names = tuple(sorted(self._functions))
            text = translate(sql, names, self._gated, self.words, self.arrays)
            if "ontology.sq_call(" in text or "sq_json_array(" in text \
                    or text in self._client_only:
                return self.execute(sql, parameters)
            conn = self._stream_conn()
            if conn is None:
                return self.execute(sql, parameters)
            with self._streams_lock:
                self._streams_busy.add(conn)
            cur = None
            try:
                params = self._params(parameters)
                if self.words and "MATCH" in sql.upper():
                    params = self._word_queries(sql, params)
                if "AS u(value)" in text:
                    params = self._lists(text, params)
                if text.startswith("WITH sq_words_"):   # as ``_execute`` reads it
                    conn.execute("SET LOCAL join_collapse_limit = 1")
                cur = conn.cursor(name=f"sq_stream_{id(conn):x}", scrollable=False,
                                  withhold=False)
                cur.itersize = batch
                cur.execute(text, params)
            except (psycopg.errors.IndeterminateDatatype, psycopg.errors.UndefinedFunction,
                    psycopg.errors.DatatypeMismatch, psycopg.errors.AmbiguousFunction,
                    psycopg.errors.InvalidTextRepresentation,
                    psycopg.errors.AmbiguousParameter, psycopg.errors.InvalidParameterValue,
                    psycopg.errors.CannotCoerce):
                # a parameter typed by its context: bound client side, whole (``_run``)
                self._stream_done(conn, cur)
                return self.execute(sql, parameters)
            except BaseException as exc:
                self._stream_done(conn, cur)
                if isinstance(exc, psycopg.errors.QueryCanceled):
                    raise sqlite3.OperationalError("interrupted") from exc
                if isinstance(exc, psycopg.Error):
                    raise sqlite3.OperationalError(
                        f"postgres: {type(exc).__name__}: "
                        f"{str(exc).splitlines()[0][:300]}") from exc
                raise
            if self._trace is not None:
                self._trace(sql)
            return StreamCursor(self, conn, cur, batch, self.row_factory is None)

    def _stream_conn(self) -> Any:
        """An idle stream connection, a new one while fewer than ``STREAM_CONNECTIONS``
        are open, else None."""
        import psycopg
        from operating.pg.pool import alive

        with self._streams_lock:
            while self._streams:
                conn = self._streams.pop()
                if alive(conn):
                    return conn
                self._streams_open -= 1
                conn.close()
            if self._streams_open >= STREAM_CONNECTIONS:
                return None
            self._streams_open += 1
        try:
            conn = psycopg.connect(self.dsn, autocommit=True)
            with conn.cursor() as cur:
                cur.execute("SET search_path = ontology, public")
                cur.execute("SET TimeZone = 'UTC'")
                cur.execute(f"SET statement_timeout = {int(self._statement_timeout_ms)}")
                cur.execute("SET jit = off")
                cur.execute("SET plan_cache_mode = force_custom_plan")
                cur.execute("SET default_transaction_read_only = on")
            conn.autocommit = False
        except BaseException:
            with self._streams_lock:
                self._streams_open -= 1
            raise
        return conn

    def _stream_done(self, conn: Any, cur: Any) -> None:
        """A stream's connection back to the idle list, its transaction ended (the
        server cursor with it); one that cannot be ended is closed."""
        with self._streams_lock:
            self._streams_busy.discard(conn)
        keep = False
        try:
            if cur is not None:
                cur.close()
            conn.rollback()
            keep = not conn.closed and not conn.broken
        except Exception:  # noqa: BLE001 - a broken stream connection is dropped
            keep = False
        with self._streams_lock:
            if keep and not self._closed:
                self._streams.append(conn)
                return
            self._streams_open -= 1
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

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
        """Cancel the statement this connection runs, and each stream it reads now (a
        server-side cursor runs on a connection of its own, ``stream``)."""
        with self._streams_lock:
            busy = list(self._streams_busy)
        for conn in (self._conn, *busy):
            try:
                conn.cancel_safe() if hasattr(conn, "cancel_safe") else conn.cancel()
            except Exception:  # noqa: BLE001 - one that cannot be reached: the others
                if conn is self._conn:
                    raise

    def close(self) -> None:
        self._closed = True
        self._conn.close()
        with self._streams_lock:
            idle, self._streams = self._streams, []
            self._streams_open -= len(idle)
        for conn in idle:
            conn.close()

    def __enter__(self) -> "PgCompatConnection":
        return self

    def __exit__(self, *_: Any) -> None:
        pass

    # -- internals -----------------------------------------------------------------
    @staticmethod
    def _word_queries(sql: str, params: Any) -> Any:
        """``params`` with each FTS5 query (``match_parameters``) as tsquery text; a query
        FTS5 would refuse raises as it does (``sqlite3.OperationalError``)."""
        from operating.pg.words import tsquery

        names = match_parameters(sql)
        if not names:
            return params
        if isinstance(params, dict):
            out = dict(params)
            for name in names:
                if name not in out:
                    raise sqlite3.ProgrammingError(f"no value for parameter {name}")
                out[name] = tsquery(out[name])
            return out
        values = list(params)
        for index in names:
            values[index] = tsquery(values[index])
        return tuple(values)

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

    def _kept_lists(self, text: str, params: Any) -> tuple[str, Any]:
        """``text`` and ``params`` with each long text IN list (``x = ANY(...)``, ``x <> ALL
        (...)`` of a JSON list of at least ``LONG_LIST_CHARS`` characters) read from this
        session's ``sq_lists`` row of its members, by the list's SHA-256, as ``x [NOT] IN
        (SELECT unnest(members) ...)``: the same rows (``x = ANY(a)`` is ``x IN (SELECT
        unnest(a))``, ``x <> ALL(a)`` its negation, NULLs included), planned without the
        members as constants. A list is written to the session once (outside any
        transaction, so it is committed at once and no rollback removes it; a new session
        starts with none, ``_session``); inside a caller's transaction, or for a list not
        of plain strings (``_members``), the statement keeps its text."""
        import psycopg

        named = isinstance(params, Mapping)
        values: Any = dict(params) if named else list(params)
        positions = {}   # placeholder start -> positional index
        at = -1
        for m in _PLACEHOLDER.finditer(text):
            if m.group(0) != "%%" and m.group(1) is None:
                at += 1
                positions[m.start()] = at
        found = []   # (match, name, positional index, the list's JSON text)
        ends = []    # the same, of a list compared with a link's end
        for m in _KEPT_ANY.finditer(text):
            name = m.group(3)
            index = positions.get(m.start(2)) if name is None else None
            if name is None:
                raw = values[index] if index is not None and index < len(values) else None
            else:
                raw = values.get(name) if named else None
            if isinstance(raw, str) and len(raw) >= LONG_LIST_CHARS:
                if _LINK_END.search(text, 0, m.start()):
                    ends.append((m, name, index, raw))
                else:
                    found.append((m, name, index, raw))
        found = self._link_end_lists(ends, found, values)
        idle = self._conn.pgconn.transaction_status == psycopg.pq.TransactionStatus.IDLE
        new = {raw for _, _, _, raw in found if raw is not None and raw not in self._kept}
        if idle and new and len(self._kept) + len(new) > MAX_KEPT_LISTS:
            # emptied before this statement keeps a list, never between two of its lists:
            # one already bound to its digest would read as empty (``NOT IN`` of nothing
            # excludes nothing)
            with self._conn.cursor() as cur:
                cur.execute("DELETE FROM sq_lists")
            self._kept.clear()
        out, last = [], 0
        for m, name, index, raw in found:
            if raw is None:
                # a link end's list, bound as an array of its members (``_link_end_lists``)
                bound = "%s" if name is None else f"%({name}__ends)s"
                out.append(text[last:m.start()])
                out.append(f"{m.group(1)}({bound}::text[])")
                last = m.end()
                continue
            digest = self._kept.get(raw)
            if digest is None:
                if not idle:
                    continue
                members = _members(raw, numeric=False)
                if members is None:
                    continue
                digest = hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()
                with self._conn.cursor() as cur:
                    cur.execute("INSERT INTO sq_lists (digest, members) VALUES (%s, %s) "
                                "ON CONFLICT (digest) DO NOTHING", (digest, members))
                self._kept[raw] = digest
            if name is None:
                values[index] = digest
                bound = "%s"
            else:
                values[f"{name}__kept"] = digest
                bound = f"%({name}__kept)s"
            out.append(text[last:m.start()])
            out.append(f"{'IN' if m.group(1) == '= ANY' else 'NOT IN'} (SELECT unnest("
                       f"kept.members) FROM pg_temp.sq_lists kept WHERE kept.digest = {bound})")
            last = m.end()
        if not out:
            return text, params
        out.append(text[last:])
        return "".join(out), (values if named else tuple(values))

    @staticmethod
    def _link_end_lists(ends: list, found: list, values: Any) -> list:
        """``found`` with each long list compared with a link's end (``ends``) bound as a
        text array of its members (``raw`` None in its entry: written ``x = ANY(%s::text[])``,
        ``<> ALL``), in the statement's order. Kept on the session, a list is a set the
        statement joins (a hashed subplan), never an index condition: ``from IN list OR to
        IN list`` then filtered every link of the asked types (69 s over 79M links for 360
        records); as an array it reads both ends' indexes (0.0 s). A list of record ids is
        small beside an audience (its planning cost is not the kept lists' reason). A list
        that is not plain strings keeps its text."""
        for m, name, index, raw in ends:
            members = _members(raw, numeric=False)
            if members is None:
                continue
            if name is None:
                values[index] = members
            else:
                values[f"{name}__ends"] = members
            found.append((m, name, index, None))
        return sorted(found, key=lambda entry: entry[0].start())

    def _members_of(self, raw: Any, numeric: bool) -> list[Any]:
        members = _members(raw, numeric=numeric)
        if members is None:
            with self._conn.cursor() as cur:
                cur.execute(_FALLBACK[numeric], (raw,))
                members = list(cur.fetchone()[0] or [])
        return members

    def _answer(self) -> int:
        """Answer the queued function calls in Python; how many were queued."""
        with self._conn.cursor() as cur:
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


__all__ = ["MAX_ROUNDS", "PgCompatConnection", "Row", "Untranslatable", "match_parameters",
           "translate"]
