"""The measures' values, made by a builder when the views change and read by tiles, pages,
alerts and Ask instead of running each measure's statement per request (contract C3,
``biz.measure_values``, migration 0061; master plan S1, Ask plan V1 / request R2).

One builder, one table, the measure as the only definition: each measure's rows are made by
ONE statement from the same fields its statement is made from (view, value, the where with
its stated exclusions at their defaults: ``measures.effective_where``, per, date,
dimensions, rate parts), so a stored value and the statement can only differ if the data
moved between them; ``check`` proves they are equal in one snapshot.

Rows (C3: measure, version, dims, period, access_class, col_class, n, sum, as_of):

* ``version``: ``measures.version`` of the entry (a changed definition is never read from
  rows of the old one);
* ``period``: ``now`` (a measure with no date: as it stands), ``day:YYYY-MM-DD`` (the last
  ``DAY_PERIODS`` days) and ``month:YYYY-MM`` (all history) of the measure's date column in
  the company's calendar: any named period is a sum of its days or months;
* ``dims``: ``{}`` (the whole measure) or ``{<dimension>: <value as text>}`` for each declared
  dimension, plus ``_per`` (the currency or unit of a money or quantity measure: values in
  two currencies are never added) and, for a rate, ``_part``: ``num`` (the rate's rows) or
  ``den`` (the base); a reader divides the sums it holds;
* ``access_class`` / ``col_class``: the door's classes of the rows; a reader adds the rows of
  the classes it holds; ``n`` counts the measure's grain (rows with a value: a count of
  ``DISTINCT handle`` equals the row count, each row being one record), ``sum`` the value of a
  sum measure (NULL for a count).

Pre-summed only where sums are exact: a count of DISTINCT anything but the row's own
``handle``, or a view whose rows a reader sees through more than its classes (the comms
tables' units and owner-only rows, the findings that follow their messages), cannot be
added up per class and period. Those measures are ``served: statement`` (``servable``):
no rows here, the reader runs the statement through the door, and says so. No cap: every
class, period and dimension value is kept.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from decimal import Decimal
from typing import Any, Mapping, Sequence

ZONE = "America/New_York"
DAY_PERIODS = 400           # Ask spec R2: days for the last 400 days, months for all history
_VALUE = re.compile(r"^(sum|count)\((DISTINCT )?([a-z_]+|\*)\)$")
COLS = ("measure", "version", "dims", "period", "access_class", "col_class", "n", "sum",
        "as_of")


def parse_value(expr: str) -> tuple[str, bool, str]:
    """(fn, distinct, column) of a measure's value; a form this table cannot hold is refused
    (``ValueError``), never approximated."""
    got = _VALUE.match(expr.strip())
    if not got:
        raise ValueError(f"value {expr!r}: only sum(col), count(*), count(DISTINCT col)")
    fn, distinct, col = got.group(1), bool(got.group(2)), got.group(3)
    if fn == "sum" and (distinct or col == "*"):
        raise ValueError(f"value {expr!r} cannot be stored")
    return fn, distinct, col


def servable(m: Mapping[str, Any], door: str) -> str | None:
    """None when the measure's values add up per class and period; else why it is read by
    its statement (module doc). ``door``: ``class`` or ``unit`` (``door_of``)."""
    if door != "class":
        return "its view is read through the comms door (units, owner-only rows)"
    for expr in (m["value"], m.get("rate_value") or m["value"]):
        try:
            _fn, distinct, col = parse_value(expr)
        except ValueError as exc:
            return str(exc)
        if distinct and col != "handle":
            return f"a distinct count of {col} does not add up across classes and periods"
    return None


def door_of(conn: Any, view: str) -> str:
    """``class`` (the view's rows carry access_class / col_class) or ``unit`` (anything
    else: a comms table, a view following other rows): read from the table's columns."""
    cols = {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'biz' AND "
        "table_name = %s", (view,)).fetchall()}
    return "class" if {"access_class", "col_class", "ai_ok", "handle"} <= cols else "unit"


def statement(m: Mapping[str, Any]) -> str:
    """The ONE statement making every row of measure ``m`` (module doc)."""
    from operating import measures as M
    view = m["view"]
    where = M.effective_where(m) or "true"
    date = m.get("date") or None
    per = m.get("per") or None
    dims = list(m.get("dimensions") or ())
    parts = [("den", m["value"], None)]
    if M.is_rate(m):
        parts.append(("num", m.get("rate_value") or m["value"], m.get("rate_where")))
    selects = []
    for part, expr, cond in parts:
        fn, _distinct, col = parse_value(expr)
        n = "count(*)" if col in ("*", "handle") else f"count({col})"
        s = f"sum({col})" if fn == "sum" else "NULL::numeric"
        pcond = f" AND ({cond})" if cond else ""
        tag = f", '_part', '{part}'" if M.is_rate(m) else ""
        if date:
            periods = ("CROSS JOIN LATERAL (VALUES ('month:' || to_char(t.{d}, 'YYYY-MM')), "
                       "(CASE WHEN t.{d} > (now() AT TIME ZONE '{z}')::date - {days} THEN "
                       "'day:' || t.{d}::text END)) p(period)").format(d=date, z=ZONE,
                                                                       days=DAY_PERIODS)
            pwhere = f" AND t.{date} IS NOT NULL"
        else:
            periods, pwhere = "CROSS JOIN LATERAL (VALUES ('now')) p(period)", ""
        dimcols = "".join(f", {d} AS d_{d}" for d in dims)
        sets = ", ".join(["(p.period, t.access_class, t.col_class, t.per_v)"]
                         + [f"(p.period, t.access_class, t.col_class, t.per_v, d_{d})"
                            for d in dims])
        base = (tag + (", '_per', coalesce(t.per_v::text, '')" if per else ""))[2:]
        dimexpr = " || ".join(
            [f"jsonb_build_object({base})" if base else "'{}'::jsonb"]
            + [f"CASE WHEN GROUPING(d_{d}) = 0 THEN jsonb_build_object('{d}', d_{d}::text) "
               "ELSE '{}'::jsonb END" for d in dims])
        selects.append(
            f"SELECT p.period, {dimexpr} AS dims, t.access_class, t.col_class, "
            f"{n} AS n, {s} AS sum FROM (SELECT *, {per or 'NULL'} AS per_v{dimcols} "
            f"FROM biz.{view} WHERE ai_ok AND ({where}){pcond}) t {periods} "
            f"WHERE p.period IS NOT NULL{pwhere} GROUP BY GROUPING SETS ({sets})")
    return " UNION ALL ".join(selects)


def rows(conn: Any, m: Mapping[str, Any]) -> list[dict[str, Any]]:
    got = conn.execute(statement(m)).fetchall()
    return [dict(zip(("period", "dims", "access_class", "col_class", "n", "sum"), r))
            for r in got]


def combine(rows_: Sequence[Mapping[str, Any]], m: Mapping[str, Any],
            readable: Any = None) -> dict[str, Any]:
    """The measure's value from its rows of one period and one dims (``{}``) as a reader
    holding the rows' classes reads it: ``{per: value}``; a rate's percentage. A value
    column the reader may not read (``readable(col_class)`` false) counts nothing."""
    from operating import measures as M
    fn, _d, _c = parse_value(m["value"])
    acc: dict[tuple[str, str], list] = {}
    for r in rows_:
        if readable is not None and not readable(r["col_class"]):
            continue
        key = (r["dims"].get("_part", "den"), r["dims"].get("_per", ""))
        a = acc.setdefault(key, [0, None])
        a[0] += r["n"]
        if r["sum"] is not None:
            a[1] = (a[1] or Decimal(0)) + Decimal(r["sum"])
    pers = sorted({p for (_part, p) in acc}) or [""]

    def val(part: str, per: str) -> Any:
        a = acc.get((part, per))
        if a is None:
            return None if fn == "sum" else 0
        return a[1] if fn == "sum" else a[0]
    if M.is_rate(m):
        num, den = val("num", pers[0]), val("den", pers[0])
        return {"": None if not den else
                (Decimal(100) * Decimal(num) / Decimal(den)).quantize(Decimal("0.01"))}
    return {p: val("den", p) for p in pers}


def window_rows(got: Sequence[Mapping[str, Any]], m: Mapping[str, Any]) -> list:
    """The day rows of the measure's own window (``days``) and whole dims, or its ``now``
    rows: what its default statement counts."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    if not m.get("date"):
        return [r for r in got if r["period"] == "now" and set(r["dims"]) <= {"_part", "_per"}]
    today = dt.datetime.now(ZoneInfo(ZONE)).date()
    days = int(m.get("days", 30))
    first = today - dt.timedelta(days=days - 1)
    keep = {f"day:{first + dt.timedelta(days=i)}" for i in range(days)}
    return [r for r in got if r["period"] in keep and set(r["dims"]) <= {"_part", "_per"}]


def check(conn: Any, m: Mapping[str, Any], got: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The measure's own statement (``measures.statement``, default window, no breakdown)
    in the same snapshot over the rows the model door shows (``ai_ok``), against the stored
    rows: ``equal`` or both values."""
    from operating import measures as M
    sql, _cite = M.statement(m["id"])
    view = m["view"]
    sql = re.sub(rf"\bFROM {view}\b", f"FROM (SELECT * FROM biz.{view} WHERE ai_ok) AS {view}",
                 sql, count=1)
    cur = conn.execute(sql)
    names = [d.name for d in cur.description]
    res = [dict(zip(names, r)) for r in cur.fetchall()]
    per = m.get("per")
    if M.is_rate(m) or not per:
        theirs = {"": res[0]["value"] if res else None}
    else:
        theirs = {("" if r[per] is None else str(r[per])): r["value"] for r in res}
    ours = combine(window_rows(got, m), m)

    def norm(d: Mapping[str, Any], other: Mapping[str, Any]) -> dict[str, Any]:
        return {k: (None if v is None else str(Decimal(str(v)).normalize()))
                for k, v in d.items() if not (v in (None, 0) and k not in other)}
    a, b = norm(ours, theirs), norm(theirs, ours)
    return {"equal": a == b, **({} if a == b else {"stored": a, "statement": b})}


def build(read_dsn: str, write_dsn: str | None, names: Sequence[str] | None = None
          ) -> dict[str, Any]:
    """Make every servable measure's rows in ONE snapshot of ``read_dsn`` (the standby or
    the primary), check each against its statement in that snapshot, and write them to
    ``write_dsn``'s ``biz.measure_values`` (None: checked only). A measure that fails, or
    whose rows differ from its statement, is said in its result and written as nothing (its
    old rows stay, with their as_of)."""
    import psycopg
    from operating import measures as M
    catalogue = M.measures()
    names = list(names or catalogue)
    out: dict[str, Any] = {}
    made: dict[str, list[dict[str, Any]]] = {}
    with psycopg.connect(read_dsn) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        conn.execute("SET LOCAL search_path = biz")
        as_of = conn.execute("SELECT now()").fetchone()[0]
        for name in names:
            m = catalogue[name]
            why = ("a measure over records is read by its statement through the door "
                   "(its rows are not pre-summed)") if m.get("source") \
                else servable(m, door_of(conn, m["view"]))
            if why:
                out[name] = {"served": "statement", "why": why}
                continue
            t0 = time.monotonic()
            conn.execute("SAVEPOINT m")
            try:
                got = rows(conn, m)
                chk = check(conn, m, got)
            except psycopg.Error as exc:   # said in the result; the others go on
                conn.execute("ROLLBACK TO SAVEPOINT m")
                out[name] = {"error": f"{type(exc).__name__}: {exc}", "check": {"equal": False}}
                continue
            for r in got:
                r.update(measure=name, version=M.version(m), as_of=as_of)
            if chk["equal"]:   # never written when it differs from its statement
                made[name] = got
            out[name] = {"rows": len(got), "seconds": round(time.monotonic() - t0, 1),
                         "check": chk}
    if write_dsn is not None and made:
        write(write_dsn, made)
    return out


def write(dsn: str, made: Mapping[str, Sequence[Mapping[str, Any]]],
          schema: str = "biz") -> None:
    """Replace each measure's rows in one transaction (a reader sees the old set or the
    new one) and refresh ``class_counts`` for ``measure_values`` so the door tracks every
    ``col_class`` present."""
    import psycopg
    from operating.pg import door
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            conn.execute(f"DELETE FROM {schema}.measure_values WHERE measure = ANY(%s)",
                         (list(made),))
            with conn.cursor().copy(f"COPY {schema}.measure_values ({', '.join(COLS)}) FROM STDIN"
                                    ) as cp:
                for name, got in made.items():
                    for r in got:
                        cp.write_row([r["measure"], r["version"], json.dumps(r["dims"]),
                                      r["period"], r["access_class"], r["col_class"], r["n"],
                                      r["sum"], r["as_of"]])
            if door.present(conn, schema):
                door.recount(conn, schema, "measure_values")


# ---------------------------------------------------------------------- stored drill (GI5)

_DAY_BUFFER = 2   # stay 2 days inside DAY_PERIODS so timezone/midnight boundaries never drop a day


def period_keys(start: Any, end: Any, *, today: Any = None) -> list[str] | None:
    """Exact stored ``period`` keys (``month:YYYY-MM`` and ``day:YYYY-MM-DD``) covering the
    half-open date range ``[start, end)`` without overlap; None when any partial-month day
    falls older than ``DAY_PERIODS - _DAY_BUFFER`` days (caller falls back to statement)."""
    import datetime as dt
    from operating import measures as M
    if start >= end:
        return []
    now_day = today or M.business_today()
    day_floor = now_day - dt.timedelta(days=DAY_PERIODS - _DAY_BUFFER)
    out: list[str] = []
    cur = start
    while cur < end:
        nxt_month = M._months_on(cur, 1) if cur.day == 1 else None
        if nxt_month is not None and nxt_month <= end:
            out.append(f"month:{cur.year:04d}-{cur.month:02d}")
            cur = nxt_month
        else:
            if cur <= day_floor:
                return None
            out.append(f"day:{cur.isoformat()}")
            cur += dt.timedelta(days=1)
    return out


def _round1(val: Decimal) -> Decimal:
    from decimal import ROUND_HALF_UP
    return val.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _round2(val: Decimal) -> Decimal:
    from decimal import ROUND_HALF_UP
    return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _fmt(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val)


def _stored_value(acc: Mapping[str, list], fn: str, rate: bool) -> Any:
    if rate:
        num = acc.get("num")
        den = acc.get("den")
        if not den:
            return None
        d_val = den[1] if fn == "sum" else Decimal(den[0])
        if not d_val:
            return None
        n_val = Decimal(0) if not num else (num[1] or Decimal(0) if fn == "sum" else Decimal(num[0]))
        return _round2(Decimal(100) * n_val / Decimal(d_val))
    den = acc.get("den")
    if den is None:
        return 0 if fn == "count" else None
    return den[1] if fn == "sum" else den[0]


def _stored_rows_count(acc: Mapping[str, list]) -> int:
    den = acc.get("den")
    return int(den[0]) if den is not None else 0


def _compare_periods(m: Mapping[str, Any], period: Any, against: Any, days: Any
                     ) -> tuple[list[str], list[str], list[str] | None, Any, str] | None:
    """(cur_keys, prev_keys, prev_full_keys, earliest_start, period_text) or None when the
    requested periods cannot be answered from stored day/month keys."""
    import datetime as dt
    from operating import measures as M
    today = M.business_today()
    if period is not None:
        named = M._period(period, "period")
        if against == M.SAME_LAST_YEAR:
            earlier = M._year_before(named)
        else:
            earlier = M._period(against, "against") if against is not None else M._before(named)
        if earlier[0] < named[1] and named[0] < earlier[1]:
            return None
        earlier, full_earlier = M._aligned(named, earlier)
        cur_start, cur_end = named[0], named[1]
        prev_start, prev_end = earlier[0], earlier[1]
        full_start, full_end = (full_earlier[0], full_earlier[1]) if full_earlier else (None, None)
        earliest = min(cur_start, prev_start)
        _, cite = M.statement(m["id"], period=period, against=against, compare=True, unchecked=True)
        said = cite.get("period") or ""
    else:
        n = M._days(days, int(m.get("days", 30)))
        cur_start = today - dt.timedelta(days=n - 1)
        cur_end = today + dt.timedelta(days=1)
        prev_start = cur_start - dt.timedelta(days=n)
        prev_end = cur_start
        full_start = full_end = None
        earliest = prev_start
        _, cite = M.statement(m["id"], days=days, compare=True, unchecked=True)
        said = cite.get("period") or ""
    cur_keys = period_keys(cur_start, cur_end, today=today)
    prev_keys = period_keys(prev_start, prev_end, today=today)
    full_keys = period_keys(full_start, full_end, today=today) if full_start is not None else None
    if cur_keys is None or prev_keys is None or (full_start is not None and full_keys is None):
        return None
    return cur_keys, prev_keys, full_keys, earliest, said


def stored_compare(pg: Any, door: Any, measure_id: str, *, by: str | None = None,
                   period: str | None = None, against: str | None = None,
                   days: int | None = None, why: bool = False,
                   seconds: float = 5.0) -> dict[str, Any] | None:
    """Answer a measure's comparison, single-dimension drill (``by``), or dimension ranking
    (``why=True``) from ``biz_page.measure_values`` through the reader's door
    (``scoped_read(..., person=True)``). Returns ``{"columns", "rows", "period", "as_of",
    "source": "stored"}`` or None when the stored table cannot answer it completely and
    exact equality requires falling back to the live statement (e.g. unstored measure,
    ``dclasses`` held, ``measure_values`` refused, rows withheld by ``col_class``, uncovered
    history or period)."""
    import datetime as dt
    from operating import measures as M
    from operating.ontology_tools_sql import Door, ToolError, scoped_read
    m = M.typed().get(measure_id)
    if m is None or not m.get("date"):
        return None
    if servable(m, "class") is not None:
        return None
    if by is not None and by not in (m.get("dimensions") or ()):
        return None
    try:
        if not M._is_day_column(m["view"], m["date"]):
            return None
        spec = _compare_periods(m, period, against, days)
    except (ToolError, ValueError, KeyError):
        return None
    if spec is None:
        return None
    cur_keys, prev_keys, full_keys, earliest, period_text = spec
    all_keys = sorted(set(cur_keys) | set(prev_keys) | set(full_keys or ()))
    ver = M.version(m)
    fn, _dist, _col = parse_value(m["value"])
    rate = M.is_rate(m)
    per = m.get("per") or ""
    dims_list = list(m.get("dimensions") or ())
    early_prev_month = M._months_on(dt.date(earliest.year, earliest.month, 1), -1)
    early_m = f"month:{early_prev_month.year:04d}-{early_prev_month.month:02d}"
    early_d = f"day:{earliest.isoformat()}"

    def work(conn: Any) -> Any:
        decided = getattr(scoped_door, "decided", None) or {}
        if "measure_values" in (decided.get("refused") or ()):
            return None
        settings = decided.get("settings") or {}
        if settings.get("plx.dclasses") not in (None, "", "{}"):
            return None
        if conn.execute("SELECT to_regclass('measure_values')").fetchone()[0] is None:
            return None
        has_any, covered = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM measure_values WHERE measure = %s AND version = %s), "
            "EXISTS (SELECT 1 FROM measure_values WHERE measure = %s AND version = %s AND "
            "((period LIKE 'month:%%' AND period <= %s) OR (period LIKE 'day:%%' AND period <= %s)))",
            (measure_id, ver, measure_id, ver, early_m, early_d)).fetchone()
        if not has_any or not covered:
            return None
        if why:
            where_dim = "AND (dims - '_per' - '_part') <> '{}'::jsonb"
            params: tuple[Any, ...] = (measure_id, ver, all_keys)
        elif by is not None:
            where_dim = "AND (dims ? %s)"
            params = (measure_id, ver, all_keys, by)
        else:
            where_dim = "AND (dims - '_per' - '_part') = '{}'::jsonb"
            params = (measure_id, ver, all_keys)
        got = conn.execute(
            f"SELECT period, dims, n, sum, as_of, cardinality(withheld) > 0 "
            f"FROM measure_values WHERE measure = %s AND version = %s AND period = ANY(%s) "
            f"{where_dim}", params).fetchall()
        return got

    scoped_door = Door(door.grant)
    try:
        raw = scoped_read(pg, scoped_door, work, seconds, person=True)
    except ToolError:
        return None
    if raw is None:
        return None
    if any(r[5] for r in raw):
        return None   # masked cells: fall back to live statement for exact unread_cells / incomplete

    cur_set, prev_set = set(cur_keys), set(prev_keys)
    full_set = set(full_keys) if full_keys is not None else None
    as_of = min((r[4] for r in raw if r[4] is not None), default=None)

    def _accumulate(rows_in: Sequence[Any], dim_name: str | None) -> dict[tuple[Any, ...], dict[str, dict[str, list]]]:
        buckets: dict[tuple[Any, ...], dict[str, dict[str, list]]] = {}
        for p, dims_val, n_val, s_val, _as_of, _wh in rows_in:
            d = dims_val if isinstance(dims_val, Mapping) else json.loads(dims_val or "{}")
            if dim_name is not None and dim_name not in d:
                continue
            part = d.get("_part", "den")
            per_v = (d.get("_per") or None) if per else None
            key = ((d.get(dim_name),) if dim_name is not None else ()) + ((per_v,) if per else ())
            slot = buckets.setdefault(key, {"cur": {}, "prev": {}, "full": {}})
            checks = (("cur", cur_set), ("prev", prev_set)) + (
                (("full", full_set),) if full_set is not None else ())
            for which, pset in checks:
                if p in pset:
                    a = slot[which].setdefault(part, [0, None])
                    if n_val is not None:
                        a[0] += int(n_val)
                    if s_val is not None:
                        a[1] = (a[1] or Decimal(0)) + Decimal(str(s_val))
        return buckets

    def _build_compare(buckets: Mapping[tuple[Any, ...], dict[str, dict[str, list]]],
                       dim_name: str | None) -> list[dict[str, Any]]:
        out_rows = []
        for key, slot in buckets.items():
            cur_v = _stored_value(slot["cur"], fn, rate)
            prev_v = _stored_value(slot["prev"], fn, rate)
            full_v = _stored_value(slot["full"], fn, rate) if full_set is not None else None
            cur_n = _stored_rows_count(slot["cur"])
            prev_n = _stored_rows_count(slot["prev"])
            if (cur_v is None and cur_n > 0) or (prev_v is None and prev_n > 0):
                chg = None
            else:
                chg = Decimal(str(cur_v or 0)) - Decimal(str(prev_v or 0))
            rec: dict[str, Any] = {
                "current": cur_v, "previous": prev_v,
                "current_rows": cur_n, "previous_rows": prev_n,
                "unread_cells": 0, "data_from": None, "covered": True,
                "change": chg, "incomplete": False,
            }
            if full_set is not None:
                rec["previous_full"] = full_v
            idx = 0
            if dim_name is not None:
                rec[dim_name] = key[idx]
                idx += 1
            if per:
                rec[per] = key[idx]
            out_rows.append(rec)
        # shares per currency/unit
        by_per: dict[Any, Decimal] = {}
        for r in out_rows:
            if r["change"] is not None:
                pk = r.get(per) if per else ""
                by_per[pk] = by_per.get(pk, Decimal(0)) + r["change"]
        for r in out_rows:
            if rate or r["change"] is None:
                r["share_of_change_pct"] = None
            else:
                whole = by_per.get(r.get(per) if per else "", Decimal(0))
                r["share_of_change_pct"] = _round1(Decimal(100) * r["change"] / whole) if whole else None
        if dim_name is not None:
            out_rows.sort(key=lambda r: (
                0 if r["change"] is None else 1,
                -(abs(r["change"]) if r["change"] is not None else Decimal(0)),
                "" if r[dim_name] is None else str(r[dim_name])))
        return out_rows

    if why:
        cols = ["dimension", *([per] if per else []), "parts", "parts_for_half",
                "concentration_pct", "top_part", "top_change", "whole_change",
                "top_share_of_change_pct", "unknown_parts"]
        ranked_rows = []
        for dim_name in dims_list:
            comp = _build_compare(_accumulate(raw, dim_name), dim_name)
            if not comp:
                continue
            groups: dict[Any, list[dict[str, Any]]] = {}
            for r in comp:
                groups.setdefault(r.get(per) if per else "", []).append(r)
            for pk, items in sorted(groups.items(), key=lambda kv: "" if kv[0] is None else str(kv[0])):
                items.sort(key=lambda r: (
                    1 if r["change"] is None else 0,
                    -(abs(r["change"]) if r["change"] is not None else Decimal(0)),
                    "" if r[dim_name] is None else str(r[dim_name])))
                parts_n = len(items)
                moved = sum((abs(r["change"]) for r in items if r["change"] is not None), Decimal(0))
                whole_chg = sum((r["change"] for r in items if r["change"] is not None), Decimal(0))
                run_abs = Decimal(0)
                parts_half = None
                for rn, r in enumerate(items, 1):
                    if r["change"] is not None:
                        run_abs += abs(r["change"])
                        if run_abs * 2 >= moved and parts_half is None:
                            parts_half = rn
                conc = _round1(Decimal(100) * Decimal(parts_half) / Decimal(parts_n)) \
                    if parts_half is not None and parts_n else None
                top = items[0]
                top_share = (_round1(Decimal(100) * top["change"] / whole_chg)
                             if top["change"] is not None and whole_chg else None)
                unknown = sum(1 for r in items if r["change"] is None)
                ranked_rows.append((
                    (conc is None, conc if conc is not None else Decimal(0), dim_name,
                     "" if pk is None else str(pk)),
                    [dim_name, *([_fmt(pk)] if per else []), _fmt(parts_n), _fmt(parts_half),
                     _fmt(conc), _fmt(top[dim_name]), _fmt(top["change"]), _fmt(whole_chg),
                     _fmt(top_share), _fmt(unknown)]))
        ranked_rows.sort(key=lambda x: x[0])
        return {"columns": cols, "rows": [r[1] for r in ranked_rows], "period": period_text,
                "as_of": _fmt(as_of), "source": "stored"}

    comp = _build_compare(_accumulate(raw, by), by)
    cols = [*([by] if by else []), *([per] if per else []), "current", "previous",
            *(["previous_full"] if full_set is not None else []), "current_rows", "previous_rows",
            "unread_cells", "data_from", "covered", "change", "share_of_change_pct", "incomplete"]
    return {"columns": cols, "rows": [[_fmt(r.get(c)) for c in cols] for r in comp],
            "period": period_text, "as_of": _fmt(as_of), "source": "stored"}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="measure_values")
    ap.add_argument("command", choices=("build", "check"))
    ap.add_argument("--read-dsn")
    ap.add_argument("--dsn")
    ap.add_argument("--measure", action="append")
    args = ap.parse_args(argv)
    from operating.pg import config
    dsn = args.dsn or config.dsn()
    got = build(args.read_dsn or dsn, dsn if args.command == "build" else None, args.measure)
    print(json.dumps(got, default=str, indent=1))
    checked = {n: g for n, g in got.items() if "check" in g}
    bad = sorted(n for n, g in checked.items() if not g["check"]["equal"])
    print(json.dumps({"equal": len(checked) - len(bad), "of": len(checked), "different": bad,
                      "served_by_statement": sorted(n for n, g in got.items()
                                                    if g.get("served") == "statement")}))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
