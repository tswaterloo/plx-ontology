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
            for n in ("0060_view_state.sql", "0069_column_health_counts.sql"):
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
                                         "measured_at"})
            row = c.execute("SELECT fill, distinct_n, cut_share FROM biz.column_health_counts "
                            "WHERE view = 'v' AND \"column\" = 'c'").fetchone()
            self.assertEqual(tuple(row), (0.7, 3, 0.02))
            c.execute("DELETE FROM biz.column_health WHERE view = 'v'")


if __name__ == "__main__":
    unittest.main()
