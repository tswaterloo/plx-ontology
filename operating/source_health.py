"""How full the columns of the catalogue's measures over records are (``source`` in
``operating/measures.json``; master plan G5 for sources the typed views do not hold).

``python3 -m operating.source_health export [--dsn DSN] [--out PATH]`` reads each such
measure's derived table (``measure_sources.from_sql`` over ``biz.records``/``biz.record_links``,
every row of the type, no row filter) on a database that holds the business tables (the
standby), and writes the source-health report JSON (default ``fill_contract.SOURCE_HEALTH``)::

    {"measured_at": ..., "sources": {"source:<measure id>": {<column>: {"fill", "rows",
      "filled", "distinct_n"}}}}

``fill`` is the share of the type's records whose column holds a value; the measure's
``requires`` lines read it (``fill_contract``). A column is measured on what is stored, never
on a sample. Reads only counts; no cell value is written.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from operating import fill_contract, measure_sources as srcs


# the door's ``records`` reads a record's day as COALESCE(declared day, stored day)
# (generic_views.DATES_DDL); the same here, so a declared date is measured as it is read
RECORDS = ("(SELECT x.handle, x.type, x.fields, COALESCE(d.day, x.record_date) AS record_date "
           "FROM biz.records x LEFT JOIN biz.record_dates d ON d.type = x.type AND d.rid = x.rid)")


def measure(conn: Any, m: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The fill of every column of measure ``m``'s source, one pass over its rows."""
    cols = srcs.columns_of(m)
    base = srcs.from_sql(m, records=RECORDS, links="biz.record_links")
    sel = ", ".join(["count(*)"] + [f"count({c}), count(DISTINCT {c})" for c in cols])
    got = conn.execute(f"SELECT {sel} FROM {base}").fetchone()
    rows = int(got[0])
    out = {}
    for i, c in enumerate(cols):
        filled, distinct = int(got[1 + 2 * i]), int(got[2 + 2 * i])
        out[c] = {"fill": round(filled / rows, 4) if rows else None, "rows": rows,
                  "filled": filled, "distinct_n": distinct}
    return out


def export(dsn: str, path: Path = fill_contract.SOURCE_HEALTH) -> dict[str, Any]:
    import psycopg
    from operating import measures
    made: dict[str, Any] = {}
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET default_transaction_read_only = on")
        conn.execute("SET search_path = biz, public")
        for mid, m in measures.measures().items():
            if m.get("source"):
                made[f"source:{mid}"] = measure(conn, m)
    doc = {"measured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "sources": made}
    path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m operating.source_health")
    ap.add_argument("command", choices=("export",))
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--out", type=Path, default=fill_contract.SOURCE_HEALTH)
    args = ap.parse_args(argv)
    doc = export(args.dsn, args.out)
    for key, cols in doc["sources"].items():
        print(key, {c: v["fill"] for c, v in cols.items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
