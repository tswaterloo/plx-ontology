"""The in-place step prototype fails closed wherever its reads could differ from the step
input's: packed layouts it does not know, reads after the step's own writes, reads of
local temp tables, a worker opening the builder's file, plans it does not carry."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
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


_FAKE_PART = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from ontology import pg_step_inplace as ip
argv = sys.argv[2:]
work = Path(argv[argv.index("--work") + 1])
k, n = map(int, argv[argv.index("--part") + 1].split("/"))
runs = work / "runs"
runs.write_text(str(int(runs.read_text()) + 1 if runs.exists() else 1))
if (work.parent / f"fail-{k}").exists():
    sys.exit(3)
objects = [o for o in (f"Email:m{i:03d}" for i in range(400)) if ip.object_part(o, n) == k][:2]
local = ip._local_store(work / "with_people.sqlite.part")
local.add("links", [{"link_id": f"person:{o}", "link_type": "Email.sent_by_person",
                     "from_object": o, "to_object": "Person:p", "tier": "corroborated",
                     "binding_kind": "mention", "reason": '[{"feature": "k"}]', "why_not": "[]",
                     "evidence_handles": '["h://m"]', "markings": "[]", "recorded_at": "t"}
                    for o in objects])
if not local.connection.in_transaction:
    local.connection.execute("BEGIN IMMEDIATE")
Path(local.keep_as_part()).replace(work / "with_people.sqlite")
(work / "with_people.json").write_text(json.dumps(
    {"seconds": 1, "links": len(objects), "reads": 1, "part": [k, n],
     "report": {"counts": {"Email.sent_by_person:linked": len(objects)}}}))
"""


class StepsInPartsTests(unittest.TestCase):
    """``with_people`` on n processes (``steps_in_parts``): every part runs once with its
    own ``--part k/n`` and no database on its command line; a part that fails fails the
    step; a rerun runs only the parts not done; the parts' links merged are each part's,
    once, and the totals are their sums. Fixture child processes, no database."""

    def test_parts_fail_closed_resume_and_merge(self) -> None:
        import json
        import os
        from types import SimpleNamespace
        from unittest import mock
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            child = tmp / "child.py"
            child.write_text(_FAKE_PART)
            work = tmp / "work"
            work.mkdir()
            (work / "with_people-parts").mkdir()
            narrowed = tmp / "narrowed.json"
            narrowed.write_text(json.dumps({"run": {"with_people": None}}))
            args = SimpleNamespace(work=str(work), plan="p.json", narrowed=str(narrowed),
                                   state_dir="s", stage_key=None, apply_now=False,
                                   concurrent=False)
            (work / "with_people-parts" / "fail-1").write_text("")
            cmd = json.dumps([sys.executable, str(child), str(root)])
            seen = []
            real = __import__("subprocess").Popen

            def popen(cmd_, **kw):
                seen.append(cmd_)
                return real(cmd_, **kw)
            with mock.patch.dict(os.environ, {"PLX_INPLACE_STEP_CMD": cmd}), \
                    mock.patch("subprocess.Popen", side_effect=popen):
                with self.assertRaisesRegex(ip.Refused, r"parts_failed:with_people:\[1\]"):
                    ip.steps_in_parts(args, "secret-dsn", "with_people", 4)
                self.assertFalse((work / "with_people.json").exists())
                (work / "with_people-parts" / "fail-1").unlink()
                got = ip.steps_in_parts(args, "secret-dsn", "with_people", 4)
                with self.assertRaisesRegex(ip.Refused, "parts_planned_otherwise"):
                    (work / "with_people.json").unlink()
                    ip.steps_in_parts(args, "secret-dsn", "with_people", 8)
            self.assertFalse(any("secret-dsn" in " ".join(c) for c in seen))
            self.assertEqual([c[c.index("--part") + 1] for c in seen],
                             ["0/4", "1/4", "2/4", "3/4", "1/4"])
            runs = [int((work / "with_people-parts" / str(k) / "runs").read_text())
                    for k in range(4)]
            self.assertEqual(runs, [1, 2, 1, 1])
            self.assertEqual(got["links"], 8)
            self.assertEqual(got["report"]["counts"]["Email.sent_by_person:linked"], 8)
            with sqlite3.connect(work / "with_people.sqlite") as db:
                links = db.execute("SELECT from_object FROM links").fetchall()
            self.assertEqual(len(links), 8)
            self.assertEqual(sorted({ip.object_part(o, 4) for (o,) in links}), [0, 1, 2, 3])
            for bad in (("mentions", 4), ("with_people", 1), ("with_people", 300)):
                with self.subTest(bad=bad), self.assertRaises(ip.Refused):
                    ip.steps_in_parts(args, "d", *bad)


class StreamedValueReadTests(unittest.TestCase):
    """The packed current-value read (``sender_parties._values``: ``with_people``'s ~11M
    message values, held whole ~20 GB on 11-11) is streamed from a server cursor when
    the session holds no open transaction: ``with_people``'s reads and link rows equal
    the whole read's row for row for 1, 10 and 60 messages (each naming one person in
    both To and Cc: the row kept for that link follows the read's order), its peak
    memory is a fraction of the whole read's, and a read inside an open transaction is
    read whole from that transaction (its uncommitted rows seen). Scratch Postgres."""

    MIGRATIONS = ("0006_ontology.sql", "0007_ontology_compact.sql")

    def setUp(self) -> None:
        import psycopg
        from operating.tests import pg_testdb
        name, self.dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, name)
        root = Path(__file__).resolve().parents[2] / "operating" / "pg" / "migrations"
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            for migration in self.MIGRATIONS:
                pg.execute((root / migration).read_text(encoding="utf-8"))
            pg.execute("INSERT INTO ontology.value_kinds VALUES (1, 'text', NULL, NULL, NULL, "
                       "'p', NULL, '[]'), (2, 'text', NULL, NULL, NULL, 'p', NULL, "
                       "'[\"pii\"]')")
            self.fields = {"kind": 1, "object_id": 2, "addresses": 3, "sender": 4,
                           "to_addresses": 5, "cc_addresses": 6}
            for prop, field in self.fields.items():
                pg.execute("INSERT INTO ontology.value_fields VALUES (%s, %s, %s)",
                           (field, prop, "$." + prop))
        self.ver = 0

    def _object(self, pg, oid: str, values: dict, kind: int = 1) -> None:
        self.ver += 1
        pg.execute("INSERT INTO ontology.object_versions VALUES (%s, %s, %s, 'd', 'l', '[]', "
                   "'2026-01-01T00:00:00Z', NULL)", (self.ver, oid, f"h://{oid}"))
        pg.execute("INSERT INTO ontology.value_sources VALUES (%s, %s, 'b', NULL, NULL, "
                   "'2026-01-02T00:00:00Z', NULL)", (self.ver, self.ver))
        for prop, value in values.items():
            pg.execute("INSERT INTO ontology.value_cells (src, field, kind, value) VALUES "
                       "(%s, %s, %s, %s)", (self.ver, self.fields[prop], kind, value))

    def _store(self, messages: int) -> None:
        import json
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as pg, pg.transaction():
            for p in range(4):
                self._object(pg, f"Person:p{p}", {
                    "kind": "staff", "object_id": f"0000000{p}-0000-0000-0000-000000000000",
                    "addresses": json.dumps([f"p{p}@ours.example"])}, kind=2)
            for m in range(messages):
                self._object(pg, f"Email:m{m:05d}", {
                    "sender": f"p{m % 4}@ours.example",
                    "to_addresses": json.dumps([f"p{(m + 1) % 4}@ours.example", "x@else.example"]),
                    "cc_addresses": json.dumps([f"p{(m + 1) % 4}@ours.example"])})

    def _split(self, pg, whole: bool):
        local = _local()
        split = ip.SplitConnection(local, pg)
        if whole:   # as before: the read fetched whole
            split._packed_read = lambda sql, params: pg.execute(sql, params)
        return split

    def _with_people(self, split) -> list:
        from collections import Counter
        from ontology import people_links as pl
        people = pl.read_people(split, ["ours.example"])
        counts: Counter = Counter()
        out = []
        version = {"l1_handle": "h://p", "markings": "[]", "recorded_at": ""}
        for value, link_type, holder, prop in pl.judge_values(split, people, pl.FAMILIES,
                                                              counts):
            out.append((tuple(value), prop, pl.link_row(value, link_type, holder, prop,
                                                        version)))
        return out + [dict(counts)]

    def _pg(self):
        from operating.pg.sqlite_compat import PgCompatConnection
        pg = PgCompatConnection(self.dsn, statement_timeout_ms=0)
        self.addCleanup(pg.close)
        return pg

    def test_streamed_reads_equal_the_whole_read_row_for_row_for_1_10_60(self) -> None:
        done = 0
        for n in (1, 10, 60):
            with self.subTest(messages=n):
                self._store(n - done)
                done = n
                pg = self._pg()
                whole = self._with_people(self._split(pg, True))
                executed: list[str] = []
                plain = pg.execute
                pg.execute = lambda sql, params=(): executed.append(sql) or plain(sql, params)
                streamed = self._with_people(self._split(pg, False))
                self.assertFalse([s for s in executed if "value_cells" in s])
                self.assertEqual(streamed, whole)
                # every message links its sender and its To/Cc person once each, the
                # address outside ours to nobody
                self.assertEqual(whole[-1]["Email.sent_by_person:linked"], n)
                self.assertEqual(whole[-1]["Email.addressed_to_person:linked"], 2 * n)
                self.assertEqual(whole[-1]["Email.addressed_to_person:no_person"], n)
                self.assertEqual(pg._streams_busy, set())

    def test_parts_together_read_and_link_what_the_whole_read_does_for_1_10_60(self) -> None:
        """``with_people`` on 4 parts (``--part k/4``): each part reads every Person and
        only the messages of its part; the parts' link rows together are the whole read's,
        each link once, in the part of the object it is from."""
        done = 0
        for n in (1, 10, 60):
            with self.subTest(messages=n):
                self._store(n - done)
                done = n
                pg = self._pg()
                whole = self._with_people(self._split(pg, False))
                parted, counts = [], {}
                for k in range(4):
                    split = self._split(pg, False)
                    split.part = (k, 4, ip.PARTED_STEPS["with_people"])
                    got = self._with_people(split)
                    for key, v in got[-1].items():
                        counts[key] = counts.get(key, 0) + v
                    for value, _prop, link in got[:-1]:
                        self.assertEqual(ip.object_part(link["from_object"], 4), k)
                        self.assertEqual(ip.object_part(value[0], 4), k)
                    parted += got[:-1]
                key = lambda r: (r[2]["link_id"], r[1], r[0])   # noqa: E731
                self.assertEqual(sorted(parted, key=key), sorted(whole[:-1], key=key))
                self.assertEqual(counts, whole[-1])
                if n == 60:   # the messages are spread over every part
                    self.assertEqual({ip.object_part(f"Email:m{m:05d}", 4) for m in range(n)},
                                     {0, 1, 2, 3})

    def test_a_parted_read_of_another_shape_is_refused(self) -> None:
        cells = ip.pg_form(sender_parties._CELLS.format(fields="1,2"))
        sql, params = ip.parted_read(cells, [], (1, 4, ("Person:",)))
        self.assertTrue(sql.startswith(cells) and sql.endswith(
            "mod(get_byte(decode(md5(v.object_id), 'hex'), 0), 4) = 1)"))
        self.assertEqual(params, ["Person:"])
        for bad in (cells + " ORDER BY v.object_id", cells + " LIMIT 5",
                    cells + " UNION SELECT * FROM x WHERE 1",
                    cells.replace("JOIN object_versions v ON", "JOIN object_versions o ON"),
                    cells.replace("FROM value_cells c",
                                  "FROM (SELECT * FROM value_cells) c")):
            with self.subTest(sql=bad[-60:]), self.assertRaises(ip.Refused):
                ip.parted_read(bad, [], (1, 4, ("Person:",)))
        for part in ((4, 4), (-1, 4), (0, 0), (0, 300)):
            with self.subTest(part=part), self.assertRaises(ip.Refused):
                ip.parted_read(cells, [], (*part, ()))
        # any other read of every value in a parted run is refused, never read whole
        split = ip.SplitConnection(_local(), _FakePg())
        split.part = (0, 4, ())
        with self.assertRaisesRegex(ip.Refused, "part_read_unparted"):
            split.execute("SELECT value FROM value_cells WHERE field = ?", (1,))

    def test_the_streamed_read_holds_a_fraction_of_the_whole_read(self) -> None:
        import gc
        import tracemalloc
        self._store(6000)
        pg = self._pg()

        def peak(split) -> tuple[int, int]:
            gc.collect()
            tracemalloc.start()
            try:
                rows = sum(1 for _ in sender_parties._values(
                    split, ["sender", "to_addresses", "cc_addresses"], ("Email:",)))
            finally:
                top = tracemalloc.get_traced_memory()[1]
                tracemalloc.stop()
            return rows, top
        (whole_rows, whole), (rows, streamed) = peak(self._split(pg, True)), \
            peak(self._split(pg, False))
        self.assertEqual((rows, whole_rows), (18000, 18000))
        self.assertLess(streamed * 4, whole, (streamed, whole))

    def test_a_read_inside_an_open_transaction_sees_its_uncommitted_rows(self) -> None:
        self._store(2)
        pg = self._pg()
        conn = pg._pg
        conn.execute("BEGIN")
        try:
            self._object(conn, "Email:new", {"sender": "p1@ours.example"})
            got = [r["object_id"] for r, _p in sender_parties._values(
                self._split(pg, False), ["sender"], ("Email:",))]
            self.assertEqual(pg._streams_open, 0)
        finally:
            conn.execute("ROLLBACK")
        self.assertEqual(sorted(got), ["Email:m00000", "Email:m00001", "Email:new"])
        after = [r["object_id"] for r, _p in sender_parties._values(
            self._split(pg, False), ["sender"], ("Email:",))]
        self.assertEqual(sorted(after), ["Email:m00000", "Email:m00001"])
        self.assertEqual(pg._streams_open, 1)


if __name__ == "__main__":
    unittest.main()
