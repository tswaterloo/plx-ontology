"""How full each column of the business views is (contract C6, ``biz.column_health``,
migration 0060; master plan RC6 / G5): measured after the refresh built a view, so a feature
built on a column nobody profiled (a 255-character preview, a 100% NULL due date) shows
itself in data, not in a sweep.

Per view, ONE statement over the view (the comms views on a 2% ``TABLESAMPLE``): per column
the fill (share of rows not NULL), the distinct count (scalar columns), ``cut_share`` (the
share of filled text values exactly at the column's largest length, when values differ in
length and several different values stop at that one length: the shape of a cut preview; a
code column of one fixed length reads 0; the reader of the health judges the number, no list
of suspect lengths here), and up to ``TOP`` short values with their counts. ``top_values`` is NULL for a column a marking may hide on some rows
(``biz.col_classes`` marks of the view's own column classes) and for every comms view (its
rows are per mailbox): the table is readable without the door, so it holds no value a
reader could not read.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Sequence

SAMPLE_PERCENT = 2   # the comms views: millions of rows, measured on a sample (stated: rows)
TOP = 12             # most common values kept per column (Ask spec R3: up to 12 short values)
SHORT = 64           # a value longer than this is not a "short value" (R3): not listed
EXTRA = ("comms_findings",)   # biz tables measures read that are not business views
_SCALAR = {"text", "character varying", "integer", "bigint", "numeric", "double precision",
           "real", "smallint", "boolean", "date", "timestamp with time zone",
           "timestamp without time zone"}
_TEXT = {"text", "character varying"}


def _columns(conn: Any, schema: str, view: str) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        (schema, view)).fetchall()]


def _marked(conn: Any, schema: str, view: str, has_col_class: bool) -> set[str]:
    """Columns some row of the view carries a marking for (their values may be hidden)."""
    if not has_col_class:
        return set()
    return {r[0] for r in conn.execute(
        f"SELECT DISTINCT jsonb_object_keys(c.marks) FROM {schema}.col_classes c "
        f"WHERE c.id IN (SELECT DISTINCT col_class FROM {schema}.{view})").fetchall()}


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def statement(schema: str, view: str, cols: Sequence[tuple[str, str]], sampled: bool,
              hide: set[str]) -> str:
    """The one statement measuring every column of ``view`` (module doc): one pass for the
    counts and longest lengths, one for the rows at those lengths, one per listed column
    for its most common values; each column's result is one jsonb array
    ``[name, fill, distinct_n, top_values, cut_share]`` of the single row returned."""
    src = f"{schema}.{_q(view)}" + (f" TABLESAMPLE SYSTEM ({SAMPLE_PERCENT})" if sampled else "")
    aggs, cuts, outs = ["count(*) AS n"], ["0 AS z"], []
    for i, (name, kind) in enumerate(cols):
        c = _q(name)
        scalar, text = kind in _SCALAR, kind in _TEXT
        aggs.append(f"count({c}) AS f{i}")
        aggs.append(f"count(DISTINCT {c}) AS d{i}" if scalar else f"NULL::bigint AS d{i}")
        aggs.append(f"max(length({c})) AS m{i}, min(length({c})) AS l{i}" if text
                    else f"NULL::int AS m{i}, NULL::int AS l{i}")
        cuts.append(f"count(*) FILTER (WHERE length({c}) = a.m{i}) AS k{i}, count(DISTINCT {c}) "
                    f"FILTER (WHERE length({c}) = a.m{i}) AS u{i}" if text
                    else f"NULL::bigint AS k{i}, NULL::bigint AS u{i}")
        top = ("NULL::jsonb" if not scalar or name in hide or sampled else
               f"(SELECT jsonb_agg(jsonb_build_array(v, cnt)) FROM (SELECT {c}::text AS v, "
               f"count(*) AS cnt FROM s WHERE {c} IS NOT NULL AND length({c}::text) <= {SHORT} "
               f"GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {TOP}) t)")
        outs.append(f"jsonb_build_array({name!r}::text, a.f{i}::float8 / nullif(a.n, 0), a.d{i}, "
                    f"{top}, CASE WHEN a.m{i} > a.l{i} AND k.u{i} >= 2 THEN k.k{i}::float8 / "
                    f"nullif(a.f{i}, 0) ELSE 0 END)")
    return (f"WITH s AS MATERIALIZED (SELECT * FROM {src}), a AS (SELECT {', '.join(aggs)} "
            f"FROM s), k AS (SELECT {', '.join(cuts)} FROM s, a) SELECT {', '.join(outs)} "
            "FROM a, k")


def measured(conn: Any, schema: str, view: str, sampled: bool) -> list[list[Any]] | None:
    """Per column of ``view``: [name, fill, distinct_n, top_values, cut_share] (ONE
    statement); None when the view has no table."""
    cols = _columns(conn, schema, view)
    if not cols:
        return None
    hide = _marked(conn, schema, view, any(c == "col_class" for c, _k in cols))
    return list(conn.execute(statement(schema, view, cols, sampled, hide)).fetchone())


def export(conn: Any, schema: str, views: Iterable[str], comms: Iterable[str]
           ) -> dict[str, dict[str, dict[str, Any]]]:
    """{view: {column: {fill, distinct_n, cut_share}}} measured now, written nowhere: the
    nightly column-health export the fill test reads (no values in it)."""
    comms = set(comms)
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for view in views:
        got = measured(conn, schema, view, view in comms)
        if got is None:
            continue
        out[view] = {c: {"fill": None if f is None else round(float(f), 4), "distinct_n": d,
                         "cut_share": None if k is None else round(float(k), 4)}
                     for c, f, d, _top, k in got}
    return out


def measure(conn: Any, schema: str, views: Iterable[str], comms: Iterable[str]
            ) -> dict[str, int]:
    """Measure ``views`` (``comms``: the ones measured on a sample, values never kept) and
    replace their rows in ``<schema>.column_health``; returns the columns measured per view.
    A view not built (no table) is left out and said so in the result as -1."""
    comms = set(comms)
    out: dict[str, int] = {}
    for view in views:
        cols = _columns(conn, schema, view)
        if not cols:
            out[view] = -1
            continue
        sampled = view in comms
        hide = _marked(conn, schema, view, any(c == "col_class" for c, _k in cols))
        got = list(conn.execute(statement(schema, view, cols, sampled, hide)).fetchone())
        with conn.transaction():
            conn.execute(f"DELETE FROM {schema}.column_health WHERE view = %s", (view,))
            for col, fill, distinct_n, top, cut_share in got:
                conn.execute(
                    f'INSERT INTO {schema}.column_health (view, "column", fill, distinct_n, '
                    "top_values, cut_share, measured_at) VALUES (%s, %s, %s, %s, %s, %s, now())",
                    (view, col, fill, distinct_n, None if top is None else json.dumps(top),
                     cut_share))
        out[view] = len(got)
    return out


def read(conn: Any, schema: str, view: str | None = None) -> list[Mapping[str, Any]]:
    cols = ("view", "column", "fill", "distinct_n", "top_values", "cut_share", "measured_at")
    where, args = ("WHERE view = %s", (view,)) if view else ("", ())
    return [dict(zip(cols, r)) for r in conn.execute(
        f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM {schema}.column_health '
        f'{where} ORDER BY view, "column"', args).fetchall()]


def main(argv: Sequence[str] | None = None) -> int:
    """``python3 -m operating.pg.column_health export [--dsn DSN]``: the export, as JSON on
    stdout (read-only: a standby works)."""
    import argparse
    import psycopg
    import sys
    ap = argparse.ArgumentParser(prog="column_health")
    ap.add_argument("command", choices=("export",))
    ap.add_argument("--dsn")
    args = ap.parse_args(argv)
    from operating.pg import business_views as bv, config
    with psycopg.connect(args.dsn or config.dsn(), autocommit=True) as conn:
        # the views, and the findings table measures read (its rows follow their messages:
        # no values kept, as for the comms views)
        names = [n for n in bv.VIEWS] + list(EXTRA)
        got = export(conn, bv.SCHEMA, names,
                     [n for n in names if n in EXTRA or bv.VIEWS[n].fill is not None])
    sys.stdout.write(json.dumps(got, indent=1, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
