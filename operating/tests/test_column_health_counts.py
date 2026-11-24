"""biz.column_health_counts (0069): the counts of biz.column_health and never its values."""
from __future__ import annotations

import unittest

from operating.tests import test_business_views as tbv


class Counts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from operating.pg.migrate import MIGRATIONS_DIR
        tbv.BusinessViews.setUpClass.__func__(cls)
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            for n in ("0060_view_state.sql", "0069_column_health_counts.sql",
                      "0079_column_health_facts.sql"):
                c.execute((MIGRATIONS_DIR / n).read_text())

    def test_the_view_has_the_counts_and_not_the_values(self):
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("INSERT INTO biz.column_health (view, \"column\", fill, distinct_n, "
                      "top_values, cut_share) VALUES ('v', 'c', 0.7, 3, "
                      "'[[\"secret value\", 5]]'::jsonb, 0.02) ON CONFLICT DO NOTHING")
            cols = [r[0] for r in c.execute("SELECT column_name FROM information_schema.columns "
                                            "WHERE table_schema = 'biz' AND table_name = "
                                            "'column_health_counts'").fetchall()]
            self.assertNotIn("top_values", cols)
            self.assertEqual(set(cols), {"view", "column", "fill", "distinct_n", "cut_share",
                                         "measured_at", "kind", "top_share", "range_min",
                                         "range_max"})
            row = c.execute("SELECT fill, distinct_n, cut_share FROM biz.column_health_counts "
                            "WHERE view = 'v' AND \"column\" = 'c'").fetchone()
            self.assertEqual(tuple(row), (0.7, 3, 0.02))
            c.execute("DELETE FROM biz.column_health WHERE view = 'v'")

    def test_a_dates_range_and_every_columns_commonest_share_are_open_an_amounts_range_is_not(self):
        """0079: the counts view shows top_share and kind for every column and the range of a
        DATE column only; a number's min and max stay in the closed table."""
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("INSERT INTO biz.column_health (view, \"column\", fill, kind, min_value, "
                      "max_value, top_share) VALUES ('w', 'ordered', 1, 'date', '2026-03-28', "
                      "'2026-11-20', 0.41), ('w', 'total', 1, 'number', '1.00', '3537000000', "
                      "0.01) ON CONFLICT DO NOTHING")
            got = {r[0]: r[1:] for r in c.execute(
                "SELECT \"column\", kind, top_share, range_min, range_max FROM "
                "biz.column_health_counts WHERE view = 'w'").fetchall()}
            self.assertEqual(got["ordered"], ("date", 0.41, "2026-03-28", "2026-11-20"))
            self.assertEqual(got["total"], ("number", 0.01, None, None))
            c.execute("DELETE FROM biz.column_health WHERE view = 'w'")


if __name__ == "__main__":
    unittest.main()
