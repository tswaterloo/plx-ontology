"""Migration 0051: a text[] ``@>`` / ``&&`` filter on a guarded (``security_barrier``)
view reads the column's GIN index, the view's guard still decides every row, and no other
array comparison runs before the guard (throwaway Postgres database; skipped without one).

The guarded views (``biz_sql.emails`` and the rest) admit a row by settings the session
holds; before 0051 a reader's array filter ran after that guard over every admitted row
(live 11-21: ``emails WHERE records @> ARRAY[..]`` 21.7 s). The test builds the same shape
(a table, a GIN index on its text[] column, a security_barrier view whose guard reads a
session setting) and reads it as the plx.sql door does (search_path ``<views>, plx_ops,
pg_catalog``). The record/json probe (``ARRAY[ROW(col, '{}'::json)] @> ..``) errors only
when the columns before the json one are equal; run before the guard it would tell a
hidden row's value, so it must give the same answer whether the guess is right or not.
"""
from __future__ import annotations

from pathlib import Path
import unittest

from operating.tests import pg_testdb

MIGRATION = (Path(__file__).resolve().parents[1] / "pg" / "migrations"
             / "0051_text_array_ops.sql")
PATH = "v, plx_ops, pg_catalog"   # the door's order (ontology_tools_sql)


class TextArrayFilters(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import psycopg
        cls.psycopg = psycopg
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            if not pg.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
                              ).fetchone()[0]:
                raise unittest.SkipTest("migration 0051 needs a superuser (LEAKPROOF, "
                                        "the GIN array_ops family)")
            pg.execute("CREATE SCHEMA v")
            pg.execute("CREATE TABLE t (id int PRIMARY KEY, unit text NOT NULL,"
                       " secret text NOT NULL, records text[] NOT NULL)")
            # 20,000 rows of units 'a' (admitted) and 'b' (not), each naming one of 2,000
            # records; unit 'b' rows hold the secret 'hidden-<id>'
            pg.execute("INSERT INTO t SELECT g, CASE WHEN g % 2 = 0 THEN 'a' ELSE 'b' END,"
                       " 'hidden-' || g, ARRAY['r-' || (g % 2000)]"
                       " FROM generate_series(1, 20000) g")
            pg.execute("CREATE INDEX t_records ON t USING gin (records)")
            pg.execute("CREATE VIEW v.v WITH (security_barrier) AS"
                       " SELECT id, secret, records FROM t WHERE unit = ANY (string_to_array("
                       "current_setting('test.units', true), ','))")
            pg.execute(MIGRATION.read_text(encoding="utf-8"))
            pg.execute("ANALYZE t")

    def run_sql(self, sql: str, units: str = "a") -> list[tuple]:
        with self.psycopg.connect(self.dsn) as pg:
            pg.execute("SELECT set_config('test.units', %s, false),"
                       " set_config('search_path', %s, false)", (units, PATH))
            return pg.execute(sql).fetchall()

    def test_only_the_text_array_operators_are_leakproof(self) -> None:
        got = dict(self.run_sql("SELECT p.pronamespace::regnamespace || '.' || p.proname, p.proleakproof FROM"
                                " pg_proc p WHERE p.proname IN ('arraycontains',"
                                " 'arrayoverlap', 'text_array_contains',"
                                " 'text_array_overlap')"))
        self.assertEqual(got, {
            "pg_catalog.arraycontains": False, "pg_catalog.arrayoverlap": False,
            "plx_ops.text_array_contains": True, "plx_ops.text_array_overlap": True})

    def test_a_text_array_filter_reads_the_index_and_the_guard_decides_the_rows(self):
        for op in ("@>", "&&"):
            plan = "\n".join(r[0] for r in self.run_sql(
                f"EXPLAIN SELECT id FROM v WHERE records {op} ARRAY['r-7']"))
            with self.subTest(op=op):
                self.assertIn("t_records", plan)
        with self.psycopg.connect(self.dsn, autocommit=True) as pg:
            for n in (1, 10, 60):
                # n admitted rows and n not admitted name the record
                pg.execute("INSERT INTO t SELECT 100000 + g + %s * 1000,"
                           " CASE WHEN g <= %s THEN 'a' ELSE 'b' END, 'x', ARRAY[%s, 'r-1']"
                           " FROM generate_series(1, 2 * %s) g", (n, n, f"r-hit-{n}", n))
        for n in (1, 10, 60):
            for op in ("@>", "&&"):
                with self.subTest(n=n, op=op):
                    ids = [r[0] for r in self.run_sql(
                        f"SELECT id FROM v WHERE records {op} ARRAY['r-hit-{n}'] ORDER BY id")]
                    self.assertEqual(ids, [100000 + g + n * 1000 for g in range(1, n + 1)])
                    # a reader admitted to nothing gets nothing, by the index or not
                    self.assertEqual(self.run_sql(
                        f"SELECT id FROM v WHERE records {op} ARRAY['r-hit-{n}']", ""), [])

    def test_a_record_array_probe_gives_no_signal_for_hidden_rows(self) -> None:
        # 'hidden-1' is unit 'b' (never admitted); 'hidden-nope' is in no row
        for guess in ("hidden-1", "hidden-nope"):
            for op in ("@>", "&&"):
                probe = (f"SELECT count(*) FROM v WHERE ARRAY[ROW(secret, '{{}}'::json)]"
                         f" {op} ARRAY[ROW('{guess}'::text, '{{}}'::json)]")
                with self.subTest(guess=guess, op=op):
                    self.assertEqual(self.run_sql(probe, ""), [(0,)])
                    self.assertEqual(self.run_sql(probe, "a"), [(0,)])
        # the probe is a real oracle once a row is admitted: it errors on a match, so the
        # guard (not the data) is what keeps the hidden rows silent above
        with self.assertRaises(self.psycopg.Error):
            self.run_sql("SELECT count(*) FROM v WHERE ARRAY[ROW(secret, '{}'::json)]"
                         " @> ARRAY[ROW('hidden-1'::text, '{}'::json)]", "a,b")


if __name__ == "__main__":
    unittest.main()
