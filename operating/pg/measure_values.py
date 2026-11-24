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
            why = servable(m, door_of(conn, m["view"]))
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


def write(dsn: str, made: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
    """Replace each measure's rows in one transaction (a reader sees the old set or the
    new one)."""
    import psycopg
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            conn.execute("DELETE FROM biz.measure_values WHERE measure = ANY(%s)",
                         (list(made),))
            with conn.cursor().copy(f"COPY biz.measure_values ({', '.join(COLS)}) FROM STDIN"
                                    ) as cp:
                for name, got in made.items():
                    for r in got:
                        cp.write_row([r["measure"], r["version"], json.dumps(r["dims"]),
                                      r["period"], r["access_class"], r["col_class"], r["n"],
                                      r["sum"], r["as_of"]])


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
