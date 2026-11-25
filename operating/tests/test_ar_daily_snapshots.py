"""Tests for biz.ar_daily_snapshots (migration 0087, PLAN-P2-A5): idempotent migration and
daily snapshot writer, multi-day trend retention, strict DOOR v2 permission folding
(access_classes + col_classes marks/needs), guarded views biz_sql/biz_page, and ar_trend
measure."""
from __future__ import annotations

import datetime as dt
import unittest
from decimal import Decimal

from operating import measures
from operating.pg import ar_daily_snapshots as ads
from operating.pg import door, sql_views
from operating.tests import test_sql_views_install as tsv


class ArDailySnapshots(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from operating.pg.migrate import MIGRATIONS_DIR
        cls._install = classmethod(tsv.InstallOnLiveStore._install.__func__)
        tsv.InstallOnLiveStore.setUpClass.__func__(cls)
        sql = (MIGRATIONS_DIR / "0087_ar_daily_snapshots.sql").read_text(encoding="utf-8")
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            # Migration must be idempotent
            c.execute(sql)
            c.execute(sql)

    def conn(self):
        import psycopg
        c = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(c.close)
        return c

    def test_snapshot_matches_customer_balances_and_keeps_multiple_days(self):
        c = self.conn()
        c.execute("DELETE FROM biz.ar_daily_snapshots")
        day1 = dt.date(2026, 11, 24)
        day2 = dt.date(2026, 11, 25)

        got1 = ads.run(c, snapshot_date=day1)
        self.assertEqual(got1["snapshot_date"], day1.isoformat())
        self.assertEqual(got1["days"], 1)
        self.assertTrue(got1["views"]["sql"])
        self.assertTrue(got1["views"]["page"])

        # Compare snapshot totals against biz.customer_balances
        want = c.execute(
            "SELECT COALESCE(NULLIF(trim(amount_due_currency), ''), 'UNKNOWN'), "
            "COALESCE(NULLIF(trim(aging_bucket), ''), 'unknown'), "
            "COALESCE(NULLIF(trim(document_type), ''), 'unknown'), "
            "count(*), COALESCE(sum(amount_due), 0), "
            "COALESCE(sum(CASE WHEN amount_due > 0 THEN amount_due ELSE 0 END), 0), "
            "COALESCE(sum(CASE WHEN amount_due > 0 AND COALESCE(days_overdue, 0) > 0 "
            "THEN amount_due ELSE 0 END), 0) "
            "FROM biz.customer_balances GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
        ).fetchall()
        have = c.execute(
            "SELECT currency, aging_bucket, document_type, "
            "sum(document_count)::bigint, sum(net_amount_due), "
            "sum(gross_amount_due), sum(overdue_amount_due) "
            "FROM biz.ar_daily_snapshots WHERE snapshot_date = %s "
            "GROUP BY 1, 2, 3 ORDER BY 1, 2, 3", (day1,)
        ).fetchall()
        self.assertEqual(have, want)

        # Idempotent re-run on same day replaces only that day's rows
        got1_again = ads.run(c, snapshot_date=day1)
        self.assertEqual(got1_again["removed_rows"], got1["added_rows"])
        self.assertEqual(got1_again["added_rows"], got1["added_rows"])
        self.assertEqual(got1_again["days"], 1)

        # Second day is appended without touching day1 (AR trend over time)
        got2 = ads.run(c, snapshot_date=day2)
        self.assertEqual(got2["snapshot_date"], day2.isoformat())
        self.assertEqual(got2["days"], 2)
        self.assertEqual(got2["total_rows"], got1["added_rows"] + got2["added_rows"])

    def test_guarded_views_fail_closed_and_fold_column_markings(self):
        c = self.conn()
        c.execute("DELETE FROM biz.ar_daily_snapshots")
        day = dt.date(2026, 11, 25)
        ads.run(c, snapshot_date=day)

        dst = sql_views.guarded_schema()
        page = sql_views.page_schema()

        # Unset reader settings -> 0 rows (fail closed) on both biz_sql and biz_page
        with c.transaction():
            c.execute(f"SET LOCAL ROLE {sql_views.ROLE}")
            self.assertEqual(c.execute(f"SELECT count(*) FROM {dst}.ar_daily_snapshots").fetchone()[0], 0)
        with c.transaction():
            c.execute(f"SET LOCAL ROLE {sql_views.PAGE_ROLE}")
            self.assertEqual(c.execute(f"SELECT count(*) FROM {page}.ar_daily_snapshots").fetchone()[0], 0)

        # Every finance-marked row in customer_balances must have its marking folded into
        # ar_daily_snapshots.access_class (rec_marks), so a reader holding only '[]' classes
        # sees none of them.
        classes = c.execute(
            "SELECT DISTINCT s.access_class, a.rec_marks "
            "FROM biz.ar_daily_snapshots s JOIN biz.access_classes a ON a.id = s.access_class"
        ).fetchall()
        all_ids = [r[0] for r in classes]
        if all_ids:
            expect_ai = c.execute(
                "SELECT count(*) FROM biz.ar_daily_snapshots WHERE ai_ok"
            ).fetchone()[0]
            with c.transaction():
                c.execute("SELECT set_config(%s, %s, true)", (door.HELD, door.ranges(all_ids)))
                c.execute("SELECT set_config('plx.d_hidden', '{}', true)")
                c.execute(f"SET LOCAL ROLE {sql_views.ROLE}")
                n_sql = c.execute(f"SELECT count(*) FROM {dst}.ar_daily_snapshots").fetchone()[0]
                self.assertEqual(n_sql, expect_ai)

        # Verify col_class marks folding unit behavior directly
        folded = ads._fold_pattern(
            (["api:sage.ar_open_document"], ["[]"], [], False),
            ({"amount_due": '["finance","pii"]'}, {}),
        )
        self.assertEqual(folded[0], ("api:sage.ar_open_document",))
        self.assertIn('["finance","pii"]', folded[1])
        self.assertFalse(folded[3])

        # Empty audience_keys must fail closed (blocked=True)
        blocked = ads._fold_pattern(([], ["[]"], [], False), None)
        self.assertTrue(blocked[3])

    def test_ar_trend_measure_registered_and_valid(self):
        m = measures.measures().get("ar_trend")
        self.assertIsNotNone(m)
        self.assertEqual(m["view"], "ar_daily_snapshots")
        self.assertEqual(m["date"], "snapshot_date")
        self.assertEqual(m["per"], "currency")
        sql, entry = measures.statement("ar_trend", by="aging_bucket")
        self.assertIn("ar_daily_snapshots", sql)
        self.assertEqual(entry["id"], "ar_trend")


if __name__ == "__main__":
    unittest.main()
