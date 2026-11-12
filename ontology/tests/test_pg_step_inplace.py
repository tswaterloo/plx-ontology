"""The in-place step prototype fails closed wherever its reads could differ from the step
input's: packed layouts it does not know, reads after the step's own writes, reads of
local temp tables, a worker opening the builder's file, plans it does not carry."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from ontology import mc_task_links, pg_step_inplace as ip, sender_parties


class _FakePg:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str, params=()):
        self.statements.append(sql)
        return sqlite3.connect(":memory:").execute("SELECT 1")

    def create_function(self, *args, **kwargs) -> None:
        pass


def _local() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("CREATE TABLE link_rows (link_id TEXT); CREATE TABLE objects "
                       "(object_id TEXT);")
    return conn


class PgFormTests(unittest.TestCase):
    def test_the_steps_packed_value_reads_take_postgres_shape(self) -> None:
        for cells in (mc_task_links._CELLS, sender_parties._CELLS):
            got = ip.pg_form(cells.format(fields="1,2"))
            self.assertNotIn("value_layout_fields", got)
            self.assertNotRegex(got, r"\blf\.")
            self.assertIn("FROM value_cells c JOIN value_sources s", got)
            self.assertIn("c.field IN (1,2)", got)

    def test_any_other_packed_layout_read_is_refused(self) -> None:
        for sql in ("SELECT lf.path FROM value_layout_fields lf",
                    "SELECT c.src FROM value_cells c JOIN value_layout_fields lf "
                    "ON lf.layout = c.layout"):
            with self.assertRaises(ip.Refused):
                ip.pg_form(sql)


class SplitConnectionTests(unittest.TestCase):
    def test_reads_go_to_postgres_and_writes_stay_local(self) -> None:
        pg, local = _FakePg(), _local()
        split = ip.SplitConnection(local, pg)
        split.execute("SELECT to_object FROM link_rows WHERE from_object = ?", ("A:1",))
        cur = split.cursor()
        cur.row_factory = sqlite3.Row
        cur.execute("WITH x AS (SELECT 1) SELECT * FROM x")
        split.execute("INSERT INTO objects VALUES ('B:1')")
        self.assertEqual(len(pg.statements), 2)
        self.assertEqual(split.reads, 2)
        self.assertEqual(local.execute("SELECT count(*) FROM objects").fetchone()[0], 1)

    def test_a_read_after_the_step_wrote_is_refused(self) -> None:
        for table, read in (("link_rows", "SELECT * FROM links WHERE link_id = 'x'"),
                            ("objects", "SELECT * FROM object_versions")):
            pg, local = _FakePg(), _local()
            split = ip.SplitConnection(local, pg)
            split.execute(f"INSERT INTO {table} VALUES ('a')")
            with self.assertRaises(ip.Refused):
                split.execute(read)
            self.assertEqual(pg.statements, [])

    def test_a_read_of_a_local_temp_table_is_refused(self) -> None:
        split = ip.SplitConnection(_local(), _FakePg())
        split.execute("CREATE TEMP TABLE t (x)")
        with self.assertRaises(ip.Refused):
            split.execute("SELECT x FROM temp.t")

    def test_a_worker_opening_the_builders_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            builder = ip.SplitBuilder(type("B", (), {"connection": _local()})(), _FakePg(),
                                      ip._poison(Path(d)))
            conn = sqlite3.connect(f"file:{builder.temp}?mode=ro", uri=True)
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("SELECT count(*) FROM link_rows").fetchone()
            conn.close()


class PlanRefusalTests(unittest.TestCase):
    def _plan(self, run, remark=None):
        return {"id": "p", "steps": {"run": run, "refused": [], "remark": remark,
                                     "steps_from": "a", "steps_to": "b"},
                "units": {"id": "u", "units": {}}}

    def test_plans_it_does_not_carry_are_refused_before_postgres(self) -> None:
        steps = {"roles": lambda b, p: {}}
        with tempfile.TemporaryDirectory() as d:
            for plan, reason in ((self._plan({"roles": ["Customer"]}), "from_types"),
                                 (self._plan({"roles": None}, ["text_index"]), "remark")):
                with self.assertRaisesRegex(ip.Refused, reason):
                    ip.build_layer(plan, {}, steps, "dbname=none_such", Path(d) / "o.sqlite")


if __name__ == "__main__":
    unittest.main()
