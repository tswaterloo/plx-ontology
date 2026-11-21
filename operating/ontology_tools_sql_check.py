"""plx.sql's check of a statement before it is sent (split from ``ontology_tools_sql``,
whose docstring states the rules; fail closed): one SELECT or WITH, no statement
keyword, comment, dollar or escape string, parameter or locking clause, only the views'
schema, and every function called one of ``FUNCTIONS``. ``ontology_tools_sql``
re-exports every name, so callers and tests keep reading them there."""
from __future__ import annotations

import re
from typing import Any

from operating.ontology_tools_identity import ToolError
from operating.pg import business_views as bv

MAX_SQL = 8000

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
grouping
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
            if kind == "word" and low == "sets" and prev[0] == "word" \
                    and prev[1].lower() == "grouping":
                continue   # GROUP BY GROUPING SETS (...), never a call named sets
            if low not in FUNCTIONS:
                raise _bad(f"function '{tok}' is not allowed",
                           "allowed: plain aggregate, arithmetic, text, date, window and "
                           "full-text functions")
    return text
