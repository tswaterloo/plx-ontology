"""The host's profile of one ``plx.sql`` result (Ask plan 4.3, A1): the exact row count and,
per column, how many rows are NULL, how many distinct values it holds, and for numbers the
sum, minimum and maximum (dates: the earliest and latest), computed by the host over the
WHOLE result, never by the model reading rows. A result too big to hold (``MAX_ROWS`` or
the fetch's byte cap) is profiled by one aggregate statement over the same query in the
same scoped transaction, so the counts stay exact; when that statement fails the profile
says so and covers the rows held (never a silent partial count).

Money never mixes: a column the business views type ``money`` sums per value of its own
currency column (``<column>_currency``) when the result holds it, else its sum is left out
with the reason; a computed number sums per currency when the result holds exactly one
currency column. Which column is money or currency is read from the database's own
column provenance (the result column's source table and column, through subqueries and
aliases) and the views' declared column kinds, never guessed from names or values."""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional, Sequence

from operating.pg import business_views as bv

LOG = logging.getLogger("operating.result_profile")

NUMBER, DATE, OTHER = "N", "D", "X"
NO_CURRENCY = "none"
# a column with at most this many distinct values lists each value with its row count
# ("values"), so a count by status or by party is read, never counted off the rows; a
# column with more says so ("values_note") and is grouped with a query. The bound keeps
# one result inside the client's window (``ontology_tools_sql.MAX_BYTES``).
VALUES_MOST = 12
VALUE_CHARS = 120   # and only short values (codes, statuses, names), never long texts   # the key of a sum over rows whose currency cell is NULL

_RELS: dict[int, tuple[str, dict[int, str]]] = {}   # table oid -> (relname, attnum -> name)
_RELS_LOCK = threading.Lock()
_RELS_MOST = 4096


def origins(pg: Any, cursor: Any) -> list[tuple[Optional[str], Optional[str]]]:
    """Per result column, the (relation name, column name) it reads unchanged, or
    (None, None) for a computed one: the server's own RowDescription (table oid and column
    number), resolved through ``pg_attribute``. Unreadable provenance is (None, None):
    the column is then profiled as a plain number or text, never as money."""
    try:
        res = cursor.pgresult
        pairs = [(int(res.ftable(i)), int(res.ftablecol(i))) for i in range(res.nfields)]
    except Exception:  # noqa: BLE001 - a cursor without provenance: every column computed
        LOG.warning("result_profile: column provenance unreadable; money columns unknown")
        return [(None, None)] * len(cursor.description or ())
    wanted = sorted({oid for oid, _ in pairs if oid and oid not in _RELS})
    if wanted:
        got: dict[int, tuple[str, dict[int, str]]] = {}
        for oid, rel, num, name in pg.execute(
                "SELECT c.oid::int8, c.relname::text, a.attnum::int, a.attname::text "
                "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_attribute a ON a.attrelid = "
                "c.oid WHERE c.oid = ANY(%s::oid[]) AND a.attnum > 0", (wanted,)).fetchall():
            got.setdefault(int(oid), (rel, {}))[1][int(num)] = name
        with _RELS_LOCK:
            if len(_RELS) > _RELS_MOST:
                _RELS.clear()
            _RELS.update(got)
    out: list[tuple[Optional[str], Optional[str]]] = []
    for oid, num in pairs:
        rel = _RELS.get(oid)
        out.append((rel[0], rel[1].get(num)) if rel and num > 0 else (None, None))
    return out


def roles(found: Sequence[tuple[Optional[str], Optional[str]]]
          ) -> tuple[list[Optional[str]], dict[int, Optional[int]]]:
    """Per column its declared kind where it reads a business view's column ("money",
    "currency" or None), and for each money column the index of its own currency column in
    the result (None: the result does not hold it)."""
    kinds: list[Optional[str]] = []
    for rel, name in found:
        view = bv.VIEWS.get(rel or "")
        col = view.col(name) if view is not None and name else None
        kinds.append(col.kind if col is not None and col.kind in ("money", "currency")
                     else None)
    pair: dict[int, Optional[int]] = {}
    for i, kind in enumerate(kinds):
        if kind == "money":
            rel, name = found[i]
            pair[i] = next((j for j, (r, n) in enumerate(found)
                            if r == rel and n == f"{name}_currency"), None)
    return kinds, pair


def _decimal(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        try:
            d = Decimal(repr(value))
        except InvalidOperation:
            return None
        return d if d.is_finite() else None
    return None


def _text(value: Any) -> str:
    return format(value, "f") if isinstance(value, Decimal) else (
        value.isoformat() if isinstance(value, (dt.date, dt.datetime)) else str(value))


def _shown_value(key: Any) -> Any:
    """A listed value as a result cell is shown: source text in its ``untrusted_text``
    envelope (``ontology_tools_sql_rows._cell``), a code bare."""
    if isinstance(key, tuple):
        return [_shown_value(k) for k in key]
    if isinstance(key, (Decimal, dt.date, dt.datetime)):
        return _text(key)
    if isinstance(key, str):
        from operating.ontology_tools_sql_rows import _cell
        return _cell(key)[0]
    return key


def _key(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_key(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, default=str)
    return value


def _split(cats: Sequence[str], kinds: Sequence[Optional[str]],
           pair: Mapping[int, Optional[int]]) -> dict[int, Any]:
    """Per numeric column how its sum is split: an index of the currency column, None (one
    plain sum) or a reason string (no sum: its currency is not in the result)."""
    currencies = [i for i, k in enumerate(kinds) if k == "currency"]
    out: dict[int, Any] = {}
    for i, cat in enumerate(cats):
        if cat != NUMBER:
            continue
        if kinds[i] == "money":
            out[i] = pair.get(i) if pair.get(i) is not None else "currency_missing"
        elif kinds[i] is None and found_none(i, pair) and len(currencies) > 1:
            # a computed number beside several currency columns: which one it is in is
            # not known, so it is never summed across them
            out[i] = "currency_ambiguous"
        else:
            out[i] = currencies[0] if len(currencies) == 1 else None
    return out


def found_none(i: int, pair: Mapping[int, Optional[int]]) -> bool:
    """Whether column ``i`` is no money column of a view (computed, or another kind)."""
    return i not in pair


def _listed(columns: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Per column its entry, as a list (never a map keyed by the query's column names:
    every key the model is sent is one the contract types)."""
    return [{"column": name, **entry} for name, entry in columns.items()]


def column(profile: Mapping[str, Any], name: str) -> Optional[Mapping[str, Any]]:
    """One column's entry of a profile, by its name (None: no such column)."""
    for entry in profile.get("columns") or ():
        if isinstance(entry, Mapping) and entry.get("column") == name:
            return entry
    return None


# a computed number with no currency column in the result is summed as plain numbers; said,
# so a money figure is never printed as if its currency were known
PLAIN_NOTE = ("summed as plain numbers: a computed column and no currency column in this "
              "result; a money figure needs its currency column in the select")


def _sum_note(cols: Sequence[str], found: Sequence[tuple[Optional[str], Optional[str]]],
              i: int) -> str:
    if found[i][1] is None:
        return ("not summed: a computed number beside several currency columns (which "
                "currency it is in is not known): GROUP BY its currency")
    return (f"not summed: its currency column {found[i][1]}_currency is not in this result "
            "(amounts in different currencies never add); add it to the select")


def from_rows(cols: Sequence[str], rows: Sequence[Sequence[Any]], cats: Sequence[str],
              found: Sequence[tuple[Optional[str], Optional[str]]]) -> dict[str, Any]:
    """The profile of a result held whole (``rows`` is every row the query returned)."""
    kinds, pair = roles(found)
    split = _split(cats, kinds, pair)
    columns: dict[str, Any] = {}
    for i, name in enumerate(cols):
        values = [r[i] for r in rows]
        present = [v for v in values if v is not None]
        counts: dict[Any, int] = {}
        for v in present:
            counts[_key(v)] = counts.get(_key(v), 0) + 1
        entry: dict[str, Any] = {"nulls": len(values) - len(present), "distinct": len(counts)}
        if kinds[i] == "money":
            entry["kind"] = "money"   # the source column's declared kind, not a guess
        if 0 < len(counts) <= VALUES_MOST and len(counts) < len(present) \
                and all(len(str(k)) <= VALUE_CHARS for k in counts):
            entry["values"] = [{"cell": _shown_value(k), "n": n} for k, n in
                               sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))]
        if cats[i] == NUMBER:
            nums = [d for d in map(_decimal, present) if d is not None]
            if nums:
                entry["min"], entry["max"] = _text(min(nums)), _text(max(nums))
            how = split.get(i)
            if isinstance(how, str):
                entry["sum"], entry["note"] = None, _sum_note(cols, found, i)
            elif how is None:
                entry["sum"] = _text(sum(nums, Decimal(0)))
                if found[i] == (None, None):
                    entry["note"] = PLAIN_NOTE
            else:
                per: dict[str, Decimal] = {}
                for r in rows:
                    d = _decimal(r[i])
                    if d is not None:
                        k = r[how] if isinstance(r[how], str) and r[how] else NO_CURRENCY
                        per[k] = per.get(k, Decimal(0)) + d
                entry["sum"] = [{"currency": k, "sum": _text(v)} for k, v in sorted(per.items())]
        elif cats[i] == DATE and present:
            entry["min"], entry["max"] = _text(min(present)), _text(max(present))
        columns[name] = entry
    return {"rows": len(rows), "exact": True, "columns": _listed(columns)}


def aggregate_sql(sql: str, cols: Sequence[str], cats: Sequence[str],
                  found: Sequence[tuple[Optional[str], Optional[str]]]) -> tuple[str, Any]:
    """One statement profiling ``sql`` whole (for a result too big to hold): grouped by the
    result's one currency column with a ROLLUP total when there is one. Returns the
    statement and the column index it is grouped by (None: ungrouped)."""
    kinds, pair = roles(found)
    split = _split(cats, kinds, pair)
    by = {how for how in split.values() if isinstance(how, int)}
    group = by.pop() if len(by) == 1 else None
    names = [f"plx_c{i}" for i in range(len(cols))]
    parts = ["count(*)"]
    for i, n in enumerate(names):
        # distinct values of a text column are counted on the md5 of the value (a hash of
        # 128 bits, exact barring a collision of two different values): counting the wide
        # values themselves sorts them whole (a 5 MB-cell result took over 14 minutes)
        parts += [f"count({n})", f"count(DISTINCT {'md5(' + n + '::text)' if cats[i] == OTHER else n + '::text'})"]
        if cats[i] in (NUMBER, DATE):
            parts += [f"min({n})", f"max({n})"]
        if cats[i] == NUMBER:
            parts.append(f"sum({n})")
    head = (f"GROUPING({names[group]}), {names[group]}::text, " if group is not None
            else "0, NULL::text, ")
    tail = f" GROUP BY ROLLUP ({names[group]})" if group is not None else ""
    return (f"SELECT {head}{', '.join(parts)} FROM (\n{sql}\n) AS plx_q({', '.join(names)})"
            f"{tail}", group)


def from_aggregate(cols: Sequence[str], cats: Sequence[str],
                   found: Sequence[tuple[Optional[str], Optional[str]]],
                   got: Sequence[Sequence[Any]], group: Optional[int]) -> dict[str, Any]:
    """``aggregate_sql``'s rows read back into a profile (the same shape as ``from_rows``)."""
    kinds, pair = roles(found)
    split = _split(cats, kinds, pair)
    total = next((r for r in got if r[0] == 1 or group is None), None)
    if total is None:   # no rows at all: ROLLUP still gives its total row
        total = got[0] if got else None
    per_currency = [r for r in got if group is not None and r[0] == 0]
    columns: dict[str, Any] = {}
    n_rows = int(total[2]) if total is not None else 0

    def at(row: Sequence[Any], i: int) -> list[Any]:
        # the row's cells for column i: count, distinct, [min, max], [sum]
        pos = 3
        for j in range(i):
            pos += 2 + (2 if cats[j] in (NUMBER, DATE) else 0) + (1 if cats[j] == NUMBER else 0)
        width = 2 + (2 if cats[i] in (NUMBER, DATE) else 0) + (1 if cats[i] == NUMBER else 0)
        return list(row[pos:pos + width])

    for i, name in enumerate(cols):
        cells = at(total, i) if total is not None else [0, 0]
        entry: dict[str, Any] = {"nulls": n_rows - int(cells[0] or 0),
                                 "distinct": int(cells[1] or 0)}
        if kinds[i] == "money":
            entry["kind"] = "money"
        if cats[i] in (NUMBER, DATE) and cells[2] is not None:
            entry["min"], entry["max"] = _text(cells[2]), _text(cells[3])
        if cats[i] == NUMBER:
            how = split.get(i)
            if isinstance(how, str):
                entry["sum"], entry["note"] = None, _sum_note(cols, found, i)
            elif how is None or group is None or how != group:
                if how is None:
                    entry["sum"] = _text(_decimal(cells[4]) or Decimal(0)) if cells[4] is not None else "0"
                    if found[i] == (None, None):
                        entry["note"] = PLAIN_NOTE
                else:   # split by a currency column the statement did not group by
                    entry["sum"] = None
                    entry["note"] = ("not summed for a result this big with several "
                                         "currency columns: sum with a query GROUP BY currency")
            else:
                entry["sum"] = sorted(({"currency": r[1] or NO_CURRENCY,
                                        "sum": _text(_decimal(at(r, i)[4]) or Decimal(0))}
                                       for r in per_currency if at(r, i)[4] is not None),
                                      key=lambda x: x["currency"])
        columns[name] = entry
    return {"rows": n_rows, "exact": True, "columns": _listed(columns),
            "note": "values per column are not listed for a result this big: GROUP BY to count them; "
                    "distinct counts of text columns are counts of the md5 of each value (exact "
                    "unless two different values share one 128-bit hash)"}


# ----------------------------------------------------------------------------- the statement

def statement_facts(sql: str, profile: Mapping[str, Any]) -> dict[str, Any]:
    """What the statement's own top-level LIMIT and GROUP BY say about its result (Ask plan
    A1d/A1e), added to ``profile``: ``limit_hit`` (the rows returned equal the statement's
    own LIMIT n, so more may exist) with the note saying so; ``grouped_by`` (the GROUP BY
    list as written, one entry each) with ``total_over_groups`` (each numeric column's sum
    over the groups, so a figure for the whole is read from it, never from one group's
    row). Read by the show-the-work tokenizer, never by pattern on the text; a statement it
    cannot read whole adds nothing (and never a claim)."""
    from operating import sql_work
    toks = sql_work.tokens(sql or "")
    if not toks:
        return {}
    out: dict[str, Any] = {}
    depth, tops = 0, []        # tops: (index, word) of the clause words at depth 0
    for i, t in enumerate(toks):
        if t.kind == "punct" and t.text == ")":
            depth -= 1
        if depth == 0 and t.kind == "word":
            tops.append((i, t.text.lower()))
        if t.kind == "punct" and t.text == "(":
            depth += 1
    ends = sql_work._ENDS | {"select", "from", "where", "with"}
    for k, (i, word) in enumerate(tops):
        if word == "limit" and i + 1 < len(toks) and toks[i + 1].kind == "number":
            n = toks[i + 1].text
            rows = profile.get("rows")
            if n.isdigit() and isinstance(rows, int) and int(n) > 0 and rows == int(n) \
                    and profile.get("exact") is not False:
                out["limit_hit"] = True
                out["limit_note"] = (f"stopped at its own LIMIT {n}: more rows may exist")
        if word == "group" and i + 1 < len(toks) and toks[i + 1].text.lower() == "by":
            end = next((j for j, w in tops if j > i + 1 and w in ends), len(toks))
            items, level, cur = [], 0, []
            for t in toks[i + 2:end]:
                if t.kind == "punct" and t.text in "()":
                    level += 1 if t.text == "(" else -1
                if t.kind == "punct" and t.text == "," and level == 0:
                    items.append(cur)
                    cur = []
                else:
                    cur.append(t)
            items.append(cur)
            texts = [sql[c[0].start:c[-1].end] for c in items if c]
            if texts:
                out["grouped_by"] = texts
                add = _additive(toks, tops)
                sums = {c["column"]: c["sum"] for c in profile.get("columns") or ()
                        if isinstance(c, Mapping) and c.get("sum") is not None
                        and c["column"] in add}
                if sums:
                    out["total_over_groups"] = sums
    return out


def _additive(toks: Sequence[Any], tops: Sequence[tuple[int, str]]) -> set[str]:
    """The output names of the statement's top-level select items that are a bare ``count``
    or ``sum`` (optionally cast, then an alias): the columns whose total over the groups is
    the figure for the whole. An average, a share, a ratio, a min or max, or any other
    expression is not additive and is left out, never guessed from its name."""
    sel = next((i for i, w in tops if w == "select"), None)
    end = next((i for i, w in tops if sel is not None and i > sel and w == "from"), len(toks))
    if sel is None:
        return set()
    items, level, cur = [], 0, []
    for t in toks[sel + 1:end]:
        if t.kind == "punct" and t.text in "()":
            level += 1 if t.text == "(" else -1
        if t.kind == "punct" and t.text == "," and level == 0:
            items.append(cur)
            cur = []
        else:
            cur.append(t)
    items.append(cur)
    out: set[str] = set()
    for item in items:
        if len(item) < 3 or item[0].kind != "word" or item[0].text.lower() not in ("count", "sum") \
                or item[1].text != "(":
            continue
        if item[2].kind == "word" and item[2].text.lower() == "distinct":
            continue   # per-group distinct counts do not add up to the distinct count of all
        depth, close = 0, None
        for k, t in enumerate(item[1:], 1):
            depth += (t.text == "(") - (t.text == ")") if t.kind == "punct" else 0
            if depth == 0:
                close = k
                break
        rest = item[close + 1:] if close else None
        if rest is None:
            continue
        while len(rest) >= 3 and rest[0].text == ":" and rest[1].text == ":" \
                and rest[2].kind == "word":
            rest = rest[3:]   # ::type
        if len(rest) == 2 and rest[0].kind == "word" and rest[0].text.lower() == "as" \
                and rest[1].kind in ("word", "qident"):
            out.add(rest[1].text.strip('"'))
        elif len(rest) == 1 and rest[0].kind in ("word", "qident"):
            out.add(rest[0].text.strip('"'))
        elif not rest:
            out.add(item[0].text.lower())   # an unaliased count(*) is named count; sum is sum
    return out


# ----------------------------------------------------------------------------- the scope line

_EXCLUDES = ("<>", "!=")


def _excludes(condition: str) -> bool:
    """Whether one WHERE condition as written removes rows by what it is not: a ``<>`` or
    ``!=`` comparison, or a NOT word. Read from the statement's tokens, never from words
    of the question (``sql_work.tokens``)."""
    from operating import sql_work
    toks = sql_work.tokens(condition) or []
    return any(t.kind == "word" and t.text.lower() == "not" for t in toks) \
        or any(op in condition for op in _EXCLUDES)


def scope_line(sql: str, views: Sequence[str], profile: Mapping[str, Any], shown: int
               ) -> str:
    """One line the host writes from the statement (Fable spec 16.4, F-R1): the views read,
    each WHERE condition as written, the exclusions among them (``<>``, ``!=``, NOT), a LIMIT
    reached, the GROUP BY, and how many rows of the whole result are shown. The model reads
    what a result covers from this line, not from its own memory of the statement. Anything
    the statement reader cannot read whole is said so, never left out."""
    from operating import sql_work
    work = sql_work.work(sql, views)
    parts = []
    parts.append("reads " + (", ".join(work["views"] or list(views)) if work else
                             ", ".join(views) or "no named view"))
    if work is None:
        return "; ".join(parts + ["the statement could not be read for its conditions"])
    conds = [f["text"] for f in work["filters"]]
    parts.append("where " + " AND ".join(conds) if conds else "no WHERE condition")
    if work["more_filters"]:
        parts.append(f"{work['more_filters']} more conditions not listed here (see the statement)")
    out = [c for c in conds if _excludes(c)]
    if out:
        parts.append("excludes: " + "; ".join(out))
    if profile.get("limit_hit"):
        parts.append(str(profile.get("limit_note") or "stopped at its own LIMIT"))
    if profile.get("grouped_by"):
        parts.append("grouped by " + ", ".join(str(g) for g in profile["grouped_by"]))
    rows = profile.get("rows")
    parts.append(f"{shown} of {rows if rows is not None else 'an uncounted number of'} rows shown")
    return "; ".join(parts)
