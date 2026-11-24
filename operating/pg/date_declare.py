"""Which field is each record type's date of record, and what each of its dates means
(ASK-GENIE spec 20 #1 / G7 / G9; KNOWN-GAPS D3).

The generic records (Sage, Business Central, FileMaker, SharePoint lists ...) carry every
column as text, so ``record_types.date_fields`` was empty for most types and 9% of
``records`` had a ``record_date``. Naming a date field from its column name alone is a guess,
so a declaration here is made from a measurement of the type's own values:

* ``profile``: for every field of a type, over EVERY record of it (no sample), how many cells
  are filled, how many read as a calendar day, the oldest and newest day, how many distinct
  days, the share held by the commonest day, how many lie after today, and how often the day
  equals the day the record was first captured / last changed (a field that is the capture
  day is the system's own stamp, not a business date). Three sample records per field.
* ``operating/pg/date_declarations.json``: per type, the declared dates in order (the first is
  the date of record), each with what it means and the evidence it was declared on, or the
  reason a type has no date. ``check`` re-reads the profile and fails a declaration the
  data no longer supports. The file is DATA: ``generic_views.catalog_rows`` merges it into
  ``record_types.date_fields`` and ``record_types.dates`` (one definition), and
  ``generic_records`` already derives ``record_date`` from ``date_fields``.
* ``apply``: writes the declared date of the records already built into the side table
  ``biz.record_dates`` (type, rid, ver, day, field), never into ``biz.records``: an UPDATE of
  that 17 GB heap is non-HOT over 8 indexes (25-40 GB of WAL and 15-20 GB of growth against
  60 GB of headroom), the side table about 0.8 GB. The guarded ``records`` view reads
  ``COALESCE(record_dates.day, record_date)`` (``generic_views``). One type at a time in key
  ranges, each batch asking the WAL budget first (standby lag AND the disk floor,
  ``wal_budget.Budget``, the one definition); a rebuild writes the same value through the
  catalog and the side row, kept for its ``ver`` only, is then never read.
* a stamp (``role`` stamp: a system's own last-write day, ``STAMP_FIELDS``) is never declared
  the date of record when a business date is filled in 90% of the records (``LEAD_SHARE``);
  when it has to stand, the dictionary says so (role stamp) and the Ask catalog marks it.
* RUNBOOK order: deploy the code, run ``generic_views.build_catalog`` (so ``record_types``
  carries the declarations: a record read again later gets its date from it), then ``apply``,
  then ``share``. ``apply`` refuses (``CatalogBehind``) while the catalog lacks a declared
  type's dates.

    python3 -m operating.pg.date_declare profile --out FILE [--type T ...] [--dsn DSN]
    python3 -m operating.pg.date_declare check --profile FILE
    python3 -m operating.pg.date_declare share [--dsn DSN]     # dated share per type, now
    python3 -m operating.pg.date_declare declared-share [--dsn DSN]   # read-only: what apply writes
    python3 -m operating.pg.date_declare apply [--type T ...] [--dsn DSN]
    python3 -m operating.pg.date_declare prune [--dsn DSN]     # side rows no record reads
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

FILE = Path(__file__).with_name("date_declarations.json")
BIZ = "biz"
# A cell reads as a calendar day when it is an ISO day (optionally with a time) or eight
# digits YYYYMMDD with a real month and day in 1990-2100. Dates in other shapes (a
# dd/mm/yyyy text) are reported as "undated cells" in the profile, never guessed.
ISO = "^(19[9][0-9]|20[0-9]{2}|2100)-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])([T ].*)?$"
COMPACT = "^(19[9][0-9]|20[0-9]{2}|2100)(0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])$"


def day_sql(v: str) -> str:
    """SQL: the calendar day (YYYY-MM-DD) text ``v`` reads as, else NULL."""
    return (f"CASE WHEN {v} ~ '{ISO}' THEN left({v}, 10) WHEN {v} ~ '{COMPACT}' THEN "
            f"substr({v}, 1, 4) || '-' || substr({v}, 5, 2) || '-' || substr({v}, 7, 2) END")


# the first declared date of a type is its date of record; its role says whether it is a
# business date (record), the system's own last-write day (stamp), or another date (other)
ROLES = ("record", "stamp", "other")
# The columns documented to hold a system's own last-write day (Sage AUDTDATE / DATELSTEDT, a
# row's updated/modified time in Silver, Portal, FileMaker, Business Central, Graph): role stamp
# wherever declared. A business date filled in at least LEAD_SHARE of the records leads over one.
STAMP_FIELDS = frozenset((
    "audtdate", "updated_at", "last_synced_at", "datelstedt", "dtelstedit", "dateedit",
    "last_updated", "last_modified", "last_edited", "bc_last_modified_at",
    "last_modified_date_time", "fm_modified_at", "portal_updated_at"))
LEAD_SHARE = 0.9
DATED_OF_FILLED = 0.99   # a declared date's filled cells all read as days (to 1%)


def profile_sql() -> str:
    """One statement for one type (``%(t)s``): per field the measurements above. Reads
    ``fields_open``: only what a reader without markings may read can date a record."""
    return f"""
WITH c AS (
  SELECT r.record_id, left(r.first_seen, 10) AS fs, left(r.changed_at, 10) AS ch,
         e.key AS k, btrim(e.value) AS v
  FROM {BIZ}.records r, jsonb_each_text(r.fields_open) e WHERE r.type = %(t)s),
kd AS (SELECT k, {day_sql('v')} AS d, count(*) AS n,
              count(*) FILTER (WHERE v <> '' AND v !~ '^0+([.]0+)?$') AS filled,
              count(*) FILTER (WHERE {day_sql('v')} = ch) AS same_changed,
              count(*) FILTER (WHERE {day_sql('v')} = fs) AS same_first,
              min(record_id || ' = ' || v) AS sample
       FROM c GROUP BY k, {day_sql('v')})
SELECT k, sum(n)::bigint AS cells, sum(filled)::bigint AS filled,
       (sum(n) FILTER (WHERE d IS NOT NULL))::bigint AS dated,
       min(d) AS oldest, max(d) AS newest, count(d) AS days,
       max(n) FILTER (WHERE d IS NOT NULL) AS top_n,
       coalesce(sum(n) FILTER (WHERE d > to_char(now() AT TIME ZONE 'America/New_York',
                                                  'YYYY-MM-DD')), 0)::bigint AS future,
       coalesce(sum(same_changed), 0)::bigint AS same_changed,
       coalesce(sum(same_first), 0)::bigint AS same_first,
       (array_agg(sample ORDER BY d DESC) FILTER (WHERE d IS NOT NULL))[1:3] AS samples
FROM kd GROUP BY k HAVING sum(n) FILTER (WHERE d IS NOT NULL) > 0 ORDER BY k"""


def profile_type(conn: Any, type_id: str) -> dict[str, dict[str, Any]]:
    """Field -> measurements for one record type (module doc), every record read."""
    out = {}
    for (k, cells, filled, dated, oldest, newest, days, top_n, future, same_changed,
         same_first, samples) in conn.execute(profile_sql(), {"t": type_id}).fetchall():
        out[k] = {"cells": cells, "filled": filled, "dated": dated, "oldest": oldest,
                  "newest": newest, "days": days, "top_day_share": round(top_n / dated, 4),
                  "future": future, "same_as_changed_day": round(same_changed / dated, 4),
                  "same_as_first_seen_day": round(same_first / dated, 4),
                  "samples": list(samples or [])}
    return out


def types_with_rows(conn: Any, only: Sequence[str] = ()) -> list[tuple[str, int]]:
    rows = conn.execute(f"SELECT type, rows FROM {BIZ}.record_types ORDER BY rows DESC").fetchall()
    return [(t, n) for t, n in rows if not only or t in only]


def profile(conn: Any, only: Sequence[str] = (), log: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {"measured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                           "types": {}}
    for type_id, n in types_with_rows(conn, only):
        t0 = time.time()
        fields = profile_type(conn, type_id)
        out["types"][type_id] = {"records": n, "fields": fields}
        if log:
            log(f"{type_id} {n} records {len(fields)} date-like fields {time.time() - t0:.0f}s")
    return out


# ------------------------------------------------------------------ declarations

def load(path: Path | None = None) -> dict[str, Any]:
    """The declarations (``types``: type -> {dates: [...], undated: reason}); a file that
    cannot be read is an error, never an empty set."""
    return json.loads((path or FILE).read_text())


def fields_of(entry: Mapping[str, Any]) -> list[str]:
    return [d["field"] for d in entry.get("dates", ())]


def declared_for(type_id: str, decl: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    return ((decl or load())["types"]).get(type_id)


def means(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The ``record_types.dates`` value of a declared type: field, role, meaning, and the
    measured facts a reader needs to trust it (never the sample rows)."""
    return [{k: d[k] for k in ("field", "role", "means", "dated_share", "oldest", "newest",
                               "future_share") if k in d}
            for d in entry.get("dates", ())]


def problems(decl: Mapping[str, Any], prof: Mapping[str, Any]) -> list[str]:
    """What a declaration claims that the profile does not show. Every declared date needs a
    meaning, a role, a profiled field, and (the date of record) a dated share equal to what
    it states; a type with no date needs its reason."""
    bad = []
    for type_id, entry in sorted(decl["types"].items()):
        held = (prof["types"].get(type_id) or {})
        if not held:
            bad.append(f"{type_id}: not in the profile")
            continue
        dates = entry.get("dates") or []
        if not dates:
            if not entry.get("undated"):
                bad.append(f"{type_id}: no date and no reason")
            continue
        if dates[0].get("role") == "stamp":
            better = [d["field"] for d in dates[1:] if d.get("role") != "stamp"
                      and d.get("dated_share", 0) >= LEAD_SHARE]
            if better:
                bad.append(f"{type_id}: the date of record is a stamp ({dates[0].get('field')}) "
                           f"but {better} is a business date filled in >= {LEAD_SHARE:.0%}")
        for d in dates:
            f0 = held["fields"].get(d.get("field"))
            if f0 is not None and f0["filled"] and f0["dated"] / f0["filled"] < DATED_OF_FILLED:
                bad.append(f"{type_id}.{d['field']}: only {f0['dated']} of {f0['filled']} filled "
                           "cells read as a calendar day")
            f = held["fields"].get(d.get("field"))
            if d.get("role") not in ROLES or not d.get("means"):
                bad.append(f"{type_id}.{d.get('field')}: needs a role in {ROLES} and a meaning")
            if (d.get("field") in STAMP_FIELDS) != (d.get("role") == "stamp"):
                bad.append(f"{type_id}.{d.get('field')}: role {d.get('role')!r} but "
                           f"{'is' if d.get('field') in STAMP_FIELDS else 'is not'} a "
                           "last-write stamp column")
            if f is None:
                bad.append(f"{type_id}.{d.get('field')}: no day-shaped values in the profile")
                continue
            share = f["dated"] / max(1, entry["records"])
            if abs(share - d.get("dated_share", -1)) > 0.005:
                bad.append(f"{type_id}.{d['field']}: states dated_share {d.get('dated_share')} "
                           f"but the profile has {share:.4f}")
            if not d.get("evidence"):
                bad.append(f"{type_id}.{d['field']}: no evidence")
    return bad


def share_sql(side: bool = True) -> str:
    """Per type, the records and those with a day (``record_date``, or the side table's day
    for the record's version, which wins: what the guarded ``records`` view shows)."""
    return (f"SELECT t.type, t.rows, count({'COALESCE(d.day, r.record_date)' if side else 'r.record_date'})"
            f" FROM {BIZ}.record_types t LEFT JOIN {BIZ}.records r ON r.type = t.type"
            + (f" LEFT JOIN {BIZ}.record_dates d ON d.type = r.type AND d.rid = r.rid AND "
               "d.ver = r.ver" if side else "")
            + " GROUP BY t.type, t.rows ORDER BY t.rows DESC")


def dated_shares(conn: Any) -> dict[str, tuple[int, int]]:
    """Type -> (records, records with a record_date), counted live."""
    side = conn.execute("SELECT to_regclass(%s)", (f"{BIZ}.record_dates",)).fetchone()[0] \
        is not None
    return {t: (int(n), int(d)) for t, n, d in conn.execute(share_sql(side)).fetchall()}


def declared_shares(conn: Any, decl: Mapping[str, Any] | None = None
                    ) -> dict[str, tuple[int, int]]:
    """Type -> (records, records whose declared date fields give a day): what ``apply``
    writes, counted without writing (read-only)."""
    out = {}
    for type_id, entry in sorted((decl or load())["types"].items()):
        n = conn.execute(f"SELECT count(*) FROM {BIZ}.records WHERE type = %s",
                         (type_id,)).fetchone()[0]
        fields = fields_of(entry)
        got = conn.execute(f"SELECT count(*) FROM {BIZ}.records r WHERE r.type = %s AND "
                           f"{day_expr(fields)} IS NOT NULL", (type_id,)).fetchone()[0] \
            if fields else 0
        out[type_id] = (int(n), int(got))
    return out


# ------------------------------------------------------------------ apply

BATCH = 50_000


def day_expr(fields: Sequence[str]) -> str:
    """SQL over ``r.fields_open``: the first declared field (in order) that reads as a day."""
    parts = []
    for f in fields:
        lit = "'" + f.replace("'", "''") + "'"
        parts.append(f"(SELECT {day_sql('x.v')} FROM (SELECT btrim(r.fields_open ->> {lit}) "
                     "AS v) x)")
    return "COALESCE(" + ", ".join(parts) + ")"


def pick_sql(fields: Sequence[str]) -> str:
    """SQL (a LATERAL ``c``) over ``r.fields_open``: the first declared field, in order, that
    reads as a day, with that day (``c.field``, ``c.day``)."""
    rows = []
    for f in fields:
        lit = "'" + f.replace("'", "''") + "'"
        rows.append(f"({lit}, (SELECT {day_sql('x.v')} FROM (SELECT btrim(r.fields_open ->> "
                    f"{lit}) AS v) x))")
    return ("LATERAL (SELECT v.field, v.day FROM (VALUES " + ", ".join(
        f"({i}, {r[1:-1]})" for i, r in enumerate(rows)) + ") v(ord, field, day) WHERE v.day "
        "IS NOT NULL ORDER BY v.ord LIMIT 1) c")


def apply_type(conn: Any, type_id: str, fields: Sequence[str], log: Any = None,
               budget: Any = None) -> int:
    """Write the day the declared fields give each record of the type whose ``record_date``
    is not that day (none, or an older rule's) into ``biz.record_dates`` (module doc), in key ranges of ``BATCH`` rids,
    one transaction each. Before every batch the WAL budget is asked (``budget.wait``: the
    standby's lag AND the disk floor; never inside a transaction). A record already holding
    its day for its version is left alone, so a run can be repeated or resumed. Returns the
    rows written."""
    from operating.pg import generic_views as gv
    behind = catalog_behind(conn, {"types": {type_id: {"dates": [{"field": f} for f in fields]}}})
    if behind:
        raise CatalogBehind(f"{type_id}: record_types.date_fields lacks the declared dates; run "
                            "generic_views.build_catalog first")
    lo, hi = conn.execute(f"SELECT min(rid), max(rid) FROM {BIZ}.records WHERE type = %s",
                          (type_id,)).fetchone()
    if lo is None:
        return 0
    if budget is None:
        budget = _budget(conn)
    longest = _longest()
    written, pick = 0, pick_sql(fields)
    for start in range(lo, hi + 1, BATCH):
        budget.wait(max_seconds=longest)
        cur = conn.execute(
            f"INSERT INTO {BIZ}.{gv.DATES_TABLE} AS d (type, rid, ver, day, field) "
            f"SELECT r.type, r.rid, r.ver, c.day, c.field FROM {BIZ}.records r, {pick} "
            "WHERE r.type = %s AND r.record_date IS DISTINCT FROM c.day AND r.rid >= %s AND "
            "r.rid < %s "
            "ON CONFLICT (type, rid) DO UPDATE SET ver = EXCLUDED.ver, day = EXCLUDED.day, "
            "field = EXCLUDED.field WHERE (d.ver, d.day, d.field) IS DISTINCT FROM "
            "(EXCLUDED.ver, EXCLUDED.day, EXCLUDED.field)", (type_id, start, start + BATCH))
        written += cur.rowcount
        if log and budget.pauses:
            log(f"{type_id}: {budget.report()}")
    return written


def _budget(conn: Any) -> Any:
    from operating.pg import business_comms, wal_budget
    return wal_budget.Budget.for_conn(conn, job="date_declare:apply",
                                      free_path=business_comms.disk_path(conn),
                                      min_free=wal_budget.min_free_bytes())


def _longest() -> float:
    from operating.pg import wal_budget
    return wal_budget.max_wait_seconds()


class CatalogBehind(RuntimeError):
    """The served ``record_types`` does not carry the declarations (``build_catalog`` has not
    run since they changed): a record read again after ``apply`` would be dated by the older
    catalog, and its side day (for the version it was written for) would be lost."""


def catalog_behind(conn: Any, decl: Mapping[str, Any] | None = None) -> list[str]:
    """The declared types whose ``record_types.date_fields`` do not start with the declared
    fields in order (a type the catalog lacks included)."""
    held = {t: list(f or []) for t, f in conn.execute(
        f"SELECT type, date_fields FROM {BIZ}.record_types").fetchall()}
    return [t for t, e in sorted((decl or load())["types"].items())
            if fields_of(e) and held.get(t, [])[:len(fields_of(e))] != fields_of(e)]


def prune(conn: Any) -> int:
    """Delete the side rows no record reads any more (the record is gone or its version
    moved on: it read its own date at its rebuild)."""
    from operating.pg import generic_views as gv
    return conn.execute(
        f"DELETE FROM {BIZ}.{gv.DATES_TABLE} d WHERE NOT EXISTS (SELECT 1 FROM {BIZ}.records r "
        "WHERE r.type = d.type AND r.rid = d.rid AND r.ver = d.ver)"
    ).rowcount


# ------------------------------------------------------------------ main

def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m operating.pg.date_declare")
    ap.add_argument("command", choices=("profile", "check", "share", "declared-share", "apply", "prune"))
    ap.add_argument("--dsn")
    ap.add_argument("--out")
    ap.add_argument("--profile", dest="profile_file")
    ap.add_argument("--type", action="append", default=[])
    args = ap.parse_args(argv)
    import psycopg
    from operating.pg import config
    dsn = args.dsn or config.dsn()
    if args.command == "check":
        bad = problems(load(), json.loads(Path(args.profile_file).read_text()))
        print("\n".join(bad) or "every declaration is supported by the profile")
        return 1 if bad else 0
    with psycopg.connect(dsn, autocommit=True) as conn:
        if args.command == "profile":
            result = profile(conn, args.type, log=lambda m: print(m, file=sys.stderr, flush=True))
            Path(args.out).write_text(json.dumps(result))
        elif args.command == "share":
            print(json.dumps(dated_shares(conn)))
        elif args.command == "declared-share":
            print(json.dumps(declared_shares(conn)))
        elif args.command == "prune":
            print(json.dumps({"pruned": prune(conn)}))
        else:
            from operating.pg import generic_views as gv
            conn.execute(gv.DATES_DDL.format(s=BIZ))
            budget = _budget(conn)
            decl = load()["types"]
            behind = [t for t in catalog_behind(conn) if not args.type or t in args.type]
            if behind:
                raise CatalogBehind(f"{len(behind)} declared types are not in record_types "
                                    f"({', '.join(behind)}): run build_catalog first")
            total = 0
            for type_id, entry in sorted(decl.items()):
                if args.type and type_id not in args.type:
                    continue
                if fields_of(entry):
                    n = apply_type(conn, type_id, fields_of(entry), budget=budget,
                                   log=lambda m: print(m, file=sys.stderr, flush=True))
                    total += n
                    print(json.dumps({"type": type_id, "written": n}), flush=True)
            print(json.dumps({"total_written": total, "budget": budget.report()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
