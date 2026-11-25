"""Daily AR balance snapshots (PLAN-P2-A5): ``biz.ar_daily_snapshots`` (migration 0087) and
its guarded views ``biz_sql.ar_daily_snapshots`` and ``biz_page.ar_daily_snapshots``.

``biz.customer_balances`` holds only what is open *now*, so AR trend over time could not
be answered. Each run of ``snapshot`` writes (idempotently, replacing only that day's rows)
the day's open AR totals by ``(snapshot_date, currency, aging_bucket, document_type)``
grouped by resolved DOOR v2 ``access_class`` and ``ai_ok``:

* Row-level permissions from ``biz.customer_balances.access_class`` (``audience_keys``,
  ``rec_marks``, ``doc_marks``, ``blocked``, including ``OWNER_KEY`` for excluded people)
  and every column-level marking or ``needs`` from ``biz.customer_balances.col_class`` on
  the snapshotted columns (``amount_due``, ``amount_due_currency``, ``aging_bucket``,
  ``document_type``, ``days_overdue``, ``as_of``) are strictly folded (fail-closed union)
  into the snapshot row's ``access_class``.
* ``biz_sql.ar_daily_snapshots`` (``plx_sql_reader``, ``ai_only=True``) and
  ``biz_page.ar_daily_snapshots`` (``plx_page_reader``, ``ai_only=False``) are built via
  ``door.view_ddl``. Direct table grants to reader roles are revoked.

CLI: ``python3 -m operating.pg.ar_daily_snapshots run [--dsn ...] [--date YYYY-MM-DD]``.
Also invoked after ``customer_balances`` refreshes when ``biz.ar_daily_snapshots`` exists.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

TABLE = "ar_daily_snapshots"
SOURCE = "customer_balances"
ZONE = "America/New_York"
SNAP_COLS = ("amount_due", "amount_due_currency", "aging_bucket", "document_type",
             "days_overdue", "as_of")

DOC = ("Daily snapshots of open Sage accounts receivable (from customer_balances): one row "
       "per snapshot_date, currency, aging_bucket, document_type and access group (SUM over "
       "the rows you read for a day's total). Enables AR trend over time (net, gross, and "
       "overdue open balances). Business Central's aged receivables are not in this view.")

COLS = (
    ("snapshot_date", "date", "the business day (America/New_York) the AR snapshot was taken"),
    ("currency", "text", "currency code of the balances (e.g. CAD, USD)"),
    ("aging_bucket", "text", "days_overdue bucket on snapshot_date: current, 1-30, 31-60, "
     "61-90, over 90, or unknown"),
    ("document_type", "text", "Sage AR document kind: invoice, credit note, receipt, "
     "prepayment, unapplied cash, debit note, interest, other, or unknown"),
    ("document_count", "integer", "number of open AR documents in this group on snapshot_date"),
    ("net_amount_due", "numeric", "net remaining balance open on snapshot_date in currency "
     "(credits, receipts and prepayments negative)"),
    ("gross_amount_due", "numeric", "gross positive remaining balance open on snapshot_date "
     "in currency (amount_due > 0)"),
    ("overdue_amount_due", "numeric", "overdue positive remaining balance on snapshot_date "
     "in currency (amount_due > 0 and days_overdue > 0)"),
)


def _view() -> Any:
    from operating.pg.business_views import Col
    return SimpleNamespace(name=TABLE, doc=DOC, cols=[Col(*c) for c in COLS], fallback=None)


def _parse_marks(raw: Any) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {raw} if raw else set()
        if isinstance(parsed, list):
            return {str(x) for x in parsed if x}
        return {str(parsed)} if parsed else set()
    if isinstance(raw, (list, tuple, set)):
        return {str(x) for x in raw if x}
    return set()


def _fold_pattern(
    ac_row: tuple[Sequence[str], Sequence[str], Sequence[str], bool],
    cc_row: tuple[Mapping[str, Any] | None, Mapping[str, Any] | None] | None,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], bool]:
    """Fold a source row's ``access_classes`` pattern and its ``col_classes`` marks/needs
    for ``SNAP_COLS`` into one fail-closed ``access_classes`` pattern."""
    aud = set(ac_row[0] or ())
    rec_marks = set(ac_row[1] or ())
    doc_marks = set(ac_row[2] or ())
    blocked = bool(ac_row[3])

    col_mark_tokens: set[str] = set()
    if cc_row is not None:
        marks_map, needs_map = cc_row[0] or {}, cc_row[1] or {}
        for col in SNAP_COLS:
            if col in marks_map:
                col_mark_tokens |= _parse_marks(marks_map[col])
            need = needs_map.get(col)
            if isinstance(need, Mapping):
                aud |= set(need.get("audience_keys") or ())
                for rm in need.get("rec_marks") or ():
                    rec_marks.add(rm)
                doc_marks |= set(need.get("doc_marks") or ())
                if need.get("blocked"):
                    blocked = True

    if col_mark_tokens:
        # Fold column markings into rec_marks as a canonical JSON marking set so any
        # reader lacking any of those markings is refused the entire snapshot row.
        rec_marks.add(json.dumps(sorted(col_mark_tokens), separators=(",", ":")))
    if not rec_marks:
        rec_marks.add("[]")
    if not aud:
        blocked = True
    return (tuple(sorted(k for k in aud if k)),
            tuple(sorted(rec_marks)),
            tuple(sorted(doc_marks)),
            blocked)


def _resolve_day(write: Any, schema: str, snapshot_date: dt.date | None) -> dt.date:
    if snapshot_date is not None:
        return snapshot_date
    row = write.execute(f"SELECT max(as_of) FROM {schema}.{SOURCE}").fetchone()
    if row and row[0] is not None:
        return row[0]
    return write.execute(
        f"SELECT (now() AT TIME ZONE '{ZONE}')::date").fetchone()[0]


def snapshot(write: Any, schema: str = "biz",
             snapshot_date: dt.date | None = None) -> dict[str, Any]:
    """Snapshot ``<schema>.customer_balances`` into ``<schema>.ar_daily_snapshots`` for
    ``snapshot_date`` (default: ``max(as_of)`` from ``customer_balances``). Idempotent:
    replaces only ``snapshot_date``'s rows in one transaction, keeping all earlier days."""
    from operating.pg import business_views_access as acc
    from operating.pg import door

    if write.execute("SELECT to_regclass(%s)", (f"{schema}.{TABLE}",)).fetchone()[0] is None:
        return {"skipped": f"{schema}.{TABLE} missing (apply migration 0087)"}
    if write.execute("SELECT to_regclass(%s)", (f"{schema}.{SOURCE}",)).fetchone()[0] is None:
        return {"skipped": f"{schema}.{SOURCE} missing"}

    with write.transaction():
        day = _resolve_day(write, schema, snapshot_date)
        pairs = write.execute(
            f"SELECT DISTINCT access_class, col_class FROM {schema}.{SOURCE}"
        ).fetchall()
        ac_ids = sorted({int(a) for a, _ in pairs})
        cc_ids = sorted({int(c) for _, c in pairs if c})

        ac_rows: dict[int, tuple[Any, Any, Any, bool]] = {}
        if ac_ids:
            for r in write.execute(
                f"SELECT id, audience_keys, rec_marks, doc_marks, blocked "
                f"FROM {schema}.{door.CLASSES} WHERE id = ANY(%s)", (ac_ids,)
            ).fetchall():
                ac_rows[int(r[0])] = (r[1], r[2], r[3], bool(r[4]))

        cc_rows: dict[int, tuple[Any, Any]] = {}
        if cc_ids:
            for r in write.execute(
                f"SELECT id, marks, needs FROM {schema}.{door.COL_CLASSES} "
                f"WHERE id = ANY(%s)", (cc_ids,)
            ).fetchall():
                cc_rows[int(r[0])] = (r[1], r[2])

        folded: dict[tuple[int, int], tuple[tuple[str, ...], tuple[str, ...],
                                            tuple[str, ...], bool]] = {}
        for a, c in pairs:
            ac = ac_rows.get(int(a))
            if ac is None:
                # Unknown access_class -> fail closed (blocked)
                folded[(int(a), int(c))] = ((), ("[]",), (), True)
            else:
                folded[(int(a), int(c))] = _fold_pattern(ac, cc_rows.get(int(c)))

        resolved_ids = acc.access_ids(write, folded.values()) if folded else {}
        write.execute(
            "CREATE TEMP TABLE ar_snap_map (src_ac integer, src_cc integer, "
            "dst_ac integer) ON COMMIT DROP"
        )
        if folded:
            with write.cursor() as cur:
                cur.executemany(
                    "INSERT INTO ar_snap_map VALUES (%s, %s, %s)",
                    [(a, c, resolved_ids[acc._key(p)]) for (a, c), p in folded.items()],
                )

        write.execute(
            f"""
            CREATE TEMP TABLE ar_snap_day ON COMMIT DROP AS
            SELECT %(day)s::date AS snapshot_date,
                   COALESCE(NULLIF(trim(cb.amount_due_currency), ''), 'UNKNOWN') AS currency,
                   COALESCE(NULLIF(trim(cb.aging_bucket), ''), 'unknown') AS aging_bucket,
                   COALESCE(NULLIF(trim(cb.document_type), ''), 'unknown') AS document_type,
                   count(*)::integer AS document_count,
                   COALESCE(sum(cb.amount_due), 0)::numeric AS net_amount_due,
                   COALESCE(sum(CASE WHEN cb.amount_due > 0 THEN cb.amount_due ELSE 0 END),
                            0)::numeric AS gross_amount_due,
                   COALESCE(sum(CASE WHEN cb.amount_due > 0 AND COALESCE(cb.days_overdue, 0) > 0
                                     THEN cb.amount_due ELSE 0 END),
                            0)::numeric AS overdue_amount_due,
                   m.dst_ac AS access_class,
                   0::integer AS col_class,
                   cb.ai_ok AS ai_ok
            FROM {schema}.{SOURCE} cb
            JOIN ar_snap_map m ON m.src_ac = cb.access_class AND m.src_cc = cb.col_class
            GROUP BY 1, 2, 3, 4, 9, 10, 11
            """,
            {"day": day},
        )
        write.execute(
            "ALTER TABLE ar_snap_day ADD COLUMN handle text"
        )
        write.execute(
            "UPDATE ar_snap_day SET handle = 'ar_daily_snapshots:' || snapshot_date || ':' "
            "|| currency || ':' || substr(md5(concat_ws('|', snapshot_date, currency, "
            "aging_bucket, document_type, access_class, ai_ok)), 1, 16)"
        )

        # Track whether any existing row's class changed for door revocation epoch
        revoked = False
        if door.present(write, schema):
            old_classes = {r[0] for r in write.execute(
                f"SELECT DISTINCT access_class FROM {schema}.{TABLE} "
                f"WHERE snapshot_date = %s", (day,)).fetchall()}
            new_classes = {r[0] for r in write.execute(
                "SELECT DISTINCT access_class FROM ar_snap_day").fetchall()}
            revoked = bool(old_classes - new_classes)

        removed = write.execute(
            f"DELETE FROM {schema}.{TABLE} WHERE snapshot_date = %s", (day,)
        ).rowcount
        added = write.execute(
            f"INSERT INTO {schema}.{TABLE} "
            "(snapshot_date, currency, aging_bucket, document_type, document_count, "
            "net_amount_due, gross_amount_due, overdue_amount_due, access_class, "
            "col_class, ai_ok, handle) "
            "SELECT snapshot_date, currency, aging_bucket, document_type, document_count, "
            "net_amount_due, gross_amount_due, overdue_amount_due, access_class, "
            "col_class, ai_ok, handle FROM ar_snap_day"
        ).rowcount
        total = write.execute(f"SELECT count(*) FROM {schema}.{TABLE}").fetchone()[0]
        days = write.execute(
            f"SELECT count(DISTINCT snapshot_date) FROM {schema}.{TABLE}"
        ).fetchone()[0]

        write.execute(f"DELETE FROM {schema}.{door.COUNTS} WHERE view = %s", (TABLE,))
        write.execute(
            f"INSERT INTO {schema}.{door.COUNTS} (view, access_class, col_class, rows) "
            f"SELECT %s, access_class, col_class, count(*) FROM {schema}.{TABLE} GROUP BY 2, 3",
            (TABLE,),
        )
        if revoked:
            door.ensure(write, schema)
            door.bump_revoked(write, schema)

        return {
            "snapshot_date": day.isoformat(),
            "removed_rows": removed,
            "added_rows": added,
            "total_rows": total,
            "days": days,
        }


def ensure_views(write: Any, schema: str = "biz") -> dict[str, bool]:
    """Create/update guarded views ``<schema>_sql.ar_daily_snapshots`` (``plx_sql_reader``,
    ``ai_only=True``) and ``<schema>_page.ar_daily_snapshots`` (``plx_page_reader``,
    ``ai_only=False``) via ``door.view_ddl``."""
    from operating.pg import door, sql_views

    if write.execute("SELECT to_regclass(%s)", (f"{schema}.{TABLE}",)).fetchone()[0] is None:
        return {"sql": False, "page": False}

    v = _view()
    dst = sql_views.guarded_schema(schema)
    page = sql_views.page_schema(schema)
    out = {"sql": False, "page": False}
    made_any = False

    with write.transaction():
        if write.execute("SELECT to_regnamespace(%s)", (dst,)).fetchone()[0] is not None:
            made = sql_views.put(write, f"{dst}.{TABLE}",
                                 door.view_ddl(write, v, schema, dst, ai_only=True))
            sql_views._grant(write, (sql_views.ROLE,), f"SELECT ON {dst}.{TABLE}")
            out["sql"] = True
            made_any |= made
        if write.execute("SELECT to_regnamespace(%s)", (page,)).fetchone()[0] is not None:
            made = sql_views.put(write, f"{page}.{TABLE}",
                                 door.view_ddl(write, v, schema, page, ai_only=False))
            sql_views._grant(write, (sql_views.PAGE_ROLE,), f"SELECT ON {page}.{TABLE}")
            out["page"] = True
            made_any |= made
        if made_any:
            door._bump(write, schema)
    return out


def run(write: Any, schema: str = "biz",
        snapshot_date: dt.date | None = None) -> dict[str, Any]:
    """Write today's AR snapshot and ensure both guarded views."""
    out = snapshot(write, schema=schema, snapshot_date=snapshot_date)
    if "skipped" not in out:
        out["views"] = ensure_views(write, schema=schema)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ar_daily_snapshots")
    ap.add_argument("command", choices=("run",))
    ap.add_argument("--dsn")
    ap.add_argument("--schema", default="biz")
    ap.add_argument("--date", dest="snapshot_date",
                    help="Override snapshot_date (YYYY-MM-DD); default max(as_of)")
    args = ap.parse_args(argv)
    day = dt.date.fromisoformat(args.snapshot_date) if args.snapshot_date else None
    import psycopg
    from operating.pg import config
    with psycopg.connect(args.dsn or config.dsn(), autocommit=True) as write:
        write.execute("SET statement_timeout = 300000")
        try:
            got = run(write, schema=args.schema, snapshot_date=day)
        except psycopg.Error as exc:
            print(json.dumps({"ar_daily_snapshots_failed": f"{type(exc).__name__}: {exc}"}),
                  file=sys.stderr)
            return 1
    print(json.dumps(got))
    return 0


if __name__ == "__main__":
    sys.exit(main())
