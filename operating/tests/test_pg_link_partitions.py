"""``operating.pg.link_partitions`` on a throwaway database: the conversion keeps every
link row and the readers' answers, a type's rebuild swaps only its partitions, a write
during a rebuild is caught up or fails it closed, and the budget refuses before writing."""
from __future__ import annotations

import unittest

from operating.tests import pg_testdb

BIG, SMALL, OTHER, ODD = "Big.uses", "Small.for", "Other.of", "Odd'type.x"
SERVED_INDEXES = (
    "ALTER TABLE ontology.link_rows ADD CONSTRAINT link_rows_pkey PRIMARY KEY (link_id)",
    "CREATE INDEX link_rows_by_row_no ON ontology.link_rows (row_no)",
    "CREATE INDEX link_rows_by_type ON ontology.link_rows (link_type, tier)",
    "CREATE INDEX link_rows_from ON ontology.link_rows (from_object, link_type)",
    "CREATE INDEX link_rows_to ON ontology.link_rows (to_object, link_type)",
    'CREATE INDEX link_rows_lead_from ON ontology.link_rows (from_object, link_id COLLATE "C") '
    "WHERE tier = 'lead' AND superseded_at IS NULL",
    "CREATE INDEX link_rows_contra_from ON ontology.link_rows (from_object) INCLUDE "
    "(to_object, link_type) WHERE tier = 'contradicted'",
    "CREATE INDEX link_rows_odd_slots ON ontology.link_rows USING gin ((slots[1:ek])) "
    "WHERE evidence_id IS NOT NULL")


def _row(link_id, link_type, frm, to, *, tier="verified", markings="2", sup=None, row_no=None):
    return (link_id, link_type, frm, to, tier, "fk", None, None, "", None, None, None, None, 0,
            [], markings, "2026-11-01T00:00:00Z", sup, f"reason {link_id}", "[]", row_no)


def _rows(n: int):
    """``n`` rows of each type: unit links (``L:``) and a step's (``mention:``) of BIG,
    superseded versions and restricted markings among them."""
    out = []
    for i in range(n):
        out.append(_row(f"L:{i:04d}b", BIG, f"Big:{i}", f"Item:{i % 3}",
                        sup="2026-11-02T00:00:00Z" if i % 2 else None))
        out.append(_row(f"mention:{i:04d}", BIG, f"Doc:{i}", f"Item:{i % 3}", tier="lead",
                        markings="9"))
        out.append(_row(f"L:{i:04d}s", SMALL, f"Small:{i}", f"Big:{i}"))
        out.append(_row(f"L:{i:04d}o", OTHER, f"Other:{i}", f"Big:{i}", markings="5"))
        out.append(_row(f"L:{i:04d}q", ODD, f"Odd:{i}", f"Small:{i}"))
    return out


class LinkPartitionTests(unittest.TestCase):
    """One database for the class (dropping one waits on every backend of the server);
    each test starts from the served unpartitioned table of the migrations."""

    @classmethod
    def setUpClass(cls):
        from operating.pg import migrate
        cls.name, cls.dsn = pg_testdb.create_db()
        with pg_testdb.psycopg.connect(cls.dsn, autocommit=True) as conn:
            migrate.migrate(conn)
            cls.view = conn.execute("SELECT pg_get_viewdef('ontology.links'::regclass, true)"
                                    ).fetchone()[0]
            # the migrations' partitioned table (0017), kept to compare the conversion's with
            cls.migrated = sorted(conn.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'ontology' "
                "AND tablename = 'link_rows'").fetchall())
            conn.execute("CREATE TABLE public.served_link_rows (LIKE ontology.link_rows "
                         "INCLUDING CONSTRAINTS)")

    @classmethod
    def tearDownClass(cls):
        pg_testdb.drop_db(cls.name)

    def setUp(self):
        from unittest import mock
        from operating.pg import link_partitions as lp
        self.lp = lp
        # the disk gate reads the test server's own disk (often the root file system, under
        # the 20 GB reserve on a busy host): these tests measure the layout, not this host's
        # disk; the gate's own tests call gate() with explicit numbers or stop this patch
        self.free_patch = mock.patch.object(lp, "free_bytes", lambda cur: 1 << 50)
        self.free_patch.start()
        self.addCleanup(mock.patch.stopall)
        with self.conn() as c:
            c.execute("DROP SCHEMA IF EXISTS ontology_next CASCADE")
            c.execute("DROP VIEW IF EXISTS ontology.links")
            for (t,) in c.execute("SELECT relname FROM pg_class WHERE relnamespace = "
                                  "'ontology'::regnamespace AND relname LIKE 'link_rows%%' "
                                  "AND relkind IN ('r', 'p') AND NOT relispartition").fetchall():
                c.execute(f'DROP TABLE ontology."{t}" CASCADE')
            c.execute("DROP TABLE IF EXISTS public.step_out, public.bad")
            # the served database's table: unpartitioned, keyed by the link id, its seven
            # indexes (GA-F7 dropped the byte-order twin) and the autovacuum options (0007-0014)
            c.execute("CREATE TABLE ontology.link_rows (LIKE public.served_link_rows "
                      "INCLUDING CONSTRAINTS) WITH (autovacuum_vacuum_scale_factor = 0.01)")
            c.execute("ALTER TABLE ontology.link_rows ALTER COLUMN row_no SET DEFAULT "
                      "nextval('ontology.link_rows_row_no'), ALTER COLUMN ek SET DEFAULT 0, "
                      "ALTER COLUMN slots SET DEFAULT '{}'")
            for sql in SERVED_INDEXES:
                c.execute(sql)
            c.execute(f"CREATE VIEW ontology.links AS {self.view}")
        self.s = lp.Session(self.dsn, workers=3)

    def conn(self):
        return pg_testdb.psycopg.connect(self.dsn, autocommit=True)

    def load(self, rows):
        cols = ", ".join(self.lp.COLUMNS[:-1])
        with self.conn() as c, c.cursor() as cur:
            with cur.copy(f"COPY ontology.link_rows ({cols}) FROM STDIN") as cp:
                for r in rows:
                    cp.write_row(r[:-1])
            cur.execute("ANALYZE ontology.link_rows")

    def snapshot(self, where="TRUE"):
        with self.conn() as c:
            return sorted(map(tuple, c.execute(
                f"SELECT * FROM ontology.links WHERE {where} ORDER BY link_id").fetchall()))

    def reads(self):
        """The readers' lookup shapes (``ontology_reader``'s slices included)."""
        from operating.pg import ontology_reader as r
        p = {"schemes": ["sharepoint"], "odd": [], "name": BIG, "owned": ["Big:1", "Item:0"],
             "lo": "L:", "hi": "L;"}
        with self.conn() as c:
            return {
                "from": c.execute("SELECT link_id FROM ontology.link_rows WHERE from_object = "
                                  "'Big:1'").fetchall(),
                "to": sorted(c.execute("SELECT link_id FROM ontology.link_rows WHERE to_object "
                                       "= 'Item:0'").fetchall()),
                "id": c.execute("SELECT markings, superseded_at FROM ontology.links WHERE "
                                "link_id = 'mention:0001'").fetchall(),
                "prefix": c.execute('SELECT count(*) FROM ontology.link_rows WHERE link_id '
                                    'COLLATE "C" >= \'mention:\' AND link_id COLLATE "C" < '
                                    "'mention;'").fetchall(),
                "typed": sorted(c.execute(r.link_slice_sql(typed=True), p).fetchall()),
                "owned": sorted(c.execute(r.link_slice_sql(owned=True), p).fetchall()),
                "ranged": sorted(c.execute(r.link_slice_sql(ranged=True), p).fetchall()),
            }

    def parts(self):
        with self.conn() as c:
            return dict(c.execute(
                "SELECT c.relname, c.relfilenode FROM pg_inherits i JOIN pg_class c ON "
                "c.oid = i.inhrelid WHERE i.inhparent = 'ontology.link_rows'::regclass"))

    def convert(self, n, buckets=2, finish=True):
        """BIG (2n rows) alone, the three others in ``buckets`` shared partitions; the
        old table dropped (``finish``) unless the undo is under test."""
        self.load(_rows(n))
        before, reads = self.snapshot(), self.reads()
        out = self.lp.convert(self.s, own_min=2 * n, buckets=buckets)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.reads(), reads)
        if finish:
            self.lp.finish(self.s)
        return out

    def test_conversion_keeps_every_row_and_answer_at_1_10_60_rows_a_type(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                if n != 1:
                    self.setUp()
                out = self.convert(n)
                self.assertEqual(out["rename_attempts"], 1)
                with self.conn() as c:
                    self.assertEqual(c.execute("SELECT relkind FROM pg_class WHERE oid = "
                                               "'ontology.link_rows'::regclass").fetchone()[0], "p")
                    # every row in the partition naming its type; none in the default
                    self.assertEqual(c.execute(
                        "SELECT count(*) FROM ontology.link_rows WHERE tableoid::regclass::text "
                        "LIKE '%default%'").fetchone()[0], 0)
                    # the served writers' shapes still work: a delta's delete by object and
                    # copy of a row of a type no partition names (to the default)
                    c.execute("DELETE FROM ontology.link_rows WHERE from_object IN "
                              "(SELECT 'Big:0')")
                    c.execute("INSERT INTO ontology.link_rows (link_id, link_type, from_object, "
                              "to_object, tier, binding_kind, why_not, markings, recorded_at, "
                              "reason, evidence_handles) VALUES ('L:new', 'New.t', 'New:1', "
                              "'Big:1', 'lead', 'fk', '', '2', 'x', 'r', '[]')")
                    self.assertIn("default", c.execute(
                        "SELECT tableoid::regclass::text FROM ontology.link_rows WHERE link_id "
                        "= 'L:new'").fetchone()[0])
                    self.assertEqual(c.execute("SELECT count(*) FROM ontology.links WHERE "
                                               "from_object = 'Big:0'").fetchone()[0], 0)
                    self.assertIsNotNone(c.execute(
                        "SELECT row_no FROM ontology.link_rows WHERE link_id = 'L:new'"
                    ).fetchone()[0])

    def test_rebuild_of_one_type_swaps_only_its_partition(self):
        self.convert(10)
        others = self.snapshot(f"link_type <> '{BIG}'")
        before = self.parts()
        with self.conn() as c:
            c.execute("CREATE TABLE public.step_out AS SELECT * FROM ontology.link_rows "
                      "WHERE link_type = %s", (BIG,))
            c.execute("UPDATE public.step_out SET tier = 'contradicted' WHERE link_id LIKE 'L:%'")
            c.execute("DELETE FROM public.step_out WHERE link_id = 'L:0003b'")
            want = sorted(c.execute("SELECT link_id, tier FROM public.step_out").fetchall())
        out = self.lp.rebuild(self.s, self.lp.Rebuild((BIG,), "public.step_out"))
        after = self.parts()
        self.assertEqual(len(after), len(before))
        self.assertEqual(sum(1 for n, f in before.items() if after.get(n) == f), len(before) - 1)
        self.assertEqual(self.snapshot(f"link_type <> '{BIG}'"), others)
        with self.conn() as c:
            self.assertEqual(sorted(c.execute("SELECT link_id, tier FROM ontology.link_rows "
                                              "WHERE link_type = %s", (BIG,)).fetchall()), want)
            # no table or trigger left behind
            self.assertEqual(c.execute("SELECT count(*) FROM pg_class WHERE relname LIKE "
                                       "'link_rows_%%' AND relkind = 'r' AND NOT relispartition "
                                       "AND relname NOT IN ('link_rows_change_log', 'link_rows_rebuilds', "
                                       "'link_rows_retired', 'link_rows_unpartitioned')").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT count(*) FROM pg_trigger t JOIN pg_class r ON r.oid = t.tgrelid "
                                       "WHERE t.tgname LIKE 'log_%%' AND r.relname LIKE 'link_rows%%'").fetchone()[0], 0)
        self.assertEqual(out["caught_up"], 0)

    def test_step_change_replaces_only_the_steps_rows_of_shared_types(self):
        self.convert(10)
        with self.conn() as c, c.cursor() as cur:
            cur.execute("CREATE TABLE public.step_out AS SELECT * FROM ontology.link_rows "
                        "WHERE link_id LIKE 'mention:%%' AND link_id < 'mention:0005'")
            units = c.execute("SELECT * FROM ontology.links WHERE link_id LIKE 'L:%' "
                              "ORDER BY link_id").fetchall()
            change = self.lp.step_change(cur, ["mention:"], "public.step_out")
            self.assertEqual(change.types, (BIG,))
            # a step's new row outside its prefixes would duplicate another owner's
            cur.execute("CREATE TABLE public.bad AS SELECT * FROM public.step_out")
            cur.execute("UPDATE public.bad SET link_id = 'L:0000b' WHERE link_id = 'mention:0000'")
            with self.assertRaises(self.lp.Refused):
                self.lp.step_change(cur, ["mention:"], "public.bad")
        self.lp.rebuild(self.s, change)
        with self.conn() as c:
            self.assertEqual(c.execute("SELECT * FROM ontology.links WHERE link_id LIKE 'L:%' "
                                       "ORDER BY link_id").fetchall(), units)
            self.assertEqual(c.execute("SELECT count(*) FROM ontology.link_rows WHERE link_id "
                                       "LIKE 'mention:%'").fetchone()[0], 5)

    def test_a_write_during_the_rebuild_is_caught_up_or_fails_it_closed(self):
        self.convert(10, buckets=1)
        lp = self.lp
        insert = ("INSERT INTO ontology.link_rows (link_id, link_type, from_object, to_object, "
                  "tier, binding_kind, why_not, markings, recorded_at, reason, evidence_handles)"
                  " VALUES (%s, %s, 'W:1', 'Big:1', 'lead', 'fk', '', '2', 'x', 'r', '[]')")

        class Writer(lp.Session):
            """A delta writing just before the swap."""
            row: tuple = ()

            def timed(self, key, fn):
                if key == "swap" and self.row:
                    with pg_testdb.psycopg.connect(self.dsn, autocommit=True) as c:
                        c.execute(insert, self.row)
                return super().timed(key, fn)

        # BIG and SMALL share a bucket or not; the write is of a kept type of the same
        # partition as the rebuilt one, or of the rebuilt type itself
        with self.conn() as c:
            c.execute("CREATE TABLE public.step_out AS SELECT * FROM ontology.link_rows "
                      "WHERE link_type = %s", (SMALL,))
            part = c.execute("SELECT tableoid::regclass::text FROM ontology.link_rows WHERE "
                             "link_type = %s LIMIT 1", (SMALL,)).fetchone()[0]
            kept_type = c.execute("SELECT link_type FROM ontology.link_rows WHERE "
                                  f"tableoid = '{part}'::regclass AND link_type <> %s LIMIT 1",
                                  (SMALL,)).fetchone()
        w = Writer(self.dsn, workers=2)
        self.assertIsNotNone(kept_type)   # SMALL shares its partition (one bucket)
        if kept_type:
            w.row = ("L:during", kept_type[0])
            out = lp.rebuild(w, lp.Rebuild((SMALL,), "public.step_out"))
            self.assertEqual(out["caught_up"], 1)
            with self.conn() as c:
                self.assertEqual(c.execute("SELECT count(*) FROM ontology.link_rows WHERE "
                                           "link_id = 'L:during'").fetchone()[0], 1)
        before, parts = self.snapshot(), self.parts()
        w = Writer(self.dsn, workers=2)
        w.row = ("L:during2", SMALL)
        with self.assertRaises(lp.Changed):
            lp.rebuild(w, lp.Rebuild((SMALL,), "public.step_out"))
        # nothing swapped: the served partitions and rows as they were (plus the write)
        self.assertEqual(self.parts(), parts)
        self.assertEqual([r for r in self.snapshot() if r[0] != "L:during2"], before)
        with self.conn() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM pg_trigger t JOIN pg_class r ON r.oid = t.tgrelid "
                                       "WHERE t.tgname LIKE 'log_%%' AND r.relname LIKE 'link_rows%%'").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT count(*) FROM pg_class WHERE relname LIKE "
                                       "'link_rows_%%' AND relkind = 'r' AND NOT relispartition "
                                       "AND relname NOT IN ('link_rows_change_log', 'link_rows_rebuilds', "
                                       "'link_rows_retired', 'link_rows_unpartitioned')").fetchone()[0], 0)

    def test_a_reader_holding_the_table_makes_the_swap_give_up_cleanly(self):
        self.convert(10)
        before, parts = self.snapshot(), self.parts()
        with self.conn() as c:
            c.execute("CREATE TABLE public.step_out AS SELECT * FROM ontology.link_rows "
                      "WHERE link_type = %s", (BIG,))
        reader = pg_testdb.psycopg.connect(self.dsn)   # a transaction left open
        self.addCleanup(reader.close)
        reader.execute("SELECT count(*) FROM ontology.links").fetchone()
        with self.assertRaises(self.lp.Refused):
            self.lp.rebuild(self.s, self.lp.Rebuild((BIG,), "public.step_out"),
                            lock_timeout_ms=100, tries=2)
        self.assertEqual(self.parts(), parts)
        self.assertEqual(self.snapshot(), before)
        # the cleanup did not wait on the reader: the log trigger it could not drop is
        # retired and logs nothing; once the reader ends, the next sweep drops it
        with self.conn() as c:
            c.execute("UPDATE ontology.link_rows SET tier = tier WHERE link_type = %s", (BIG,))
            self.assertEqual(c.execute("SELECT count(*) FROM ontology.link_rows_change_log"
                                       ).fetchone()[0], 0)
            reader.rollback()
            self.assertEqual(self.lp.sweep(c), 0)
            self.assertEqual(c.execute("SELECT count(*) FROM pg_trigger t JOIN pg_class r ON r.oid = t.tgrelid "
                                       "WHERE t.tgname LIKE 'log_%%' AND r.relname LIKE 'link_rows%%'").fetchone()[0], 0)

    def test_a_new_type_joins_its_buckets_partition_or_gets_its_own_when_large(self):
        self.convert(10)
        lp = self.lp
        with self.conn() as c, c.cursor() as cur:
            before = {p.name: p for p in lp.layout(cur)}
            bucketed = {p.bucket for p in before.values() if p.bucket is not None}
            # a small new type whose bucket has a partition, and a large one
            small = next(f"Brand{i}.new" for i in range(200)
                         if lp.bucket(f"Brand{i}.new", 2) in bucketed)
            c.execute("CREATE TABLE public.step_out AS SELECT * FROM ontology.link_rows LIMIT 0")
            new_row = ("INSERT INTO public.step_out (link_id, link_type, from_object, to_object, "
                       "tier, binding_kind, why_not, markings, recorded_at, reason, "
                       "evidence_handles, ek, slots) SELECT 'L:n' || %s || g, %s, 'Brand:1', "
                       "'Big:0', 'lead', 'fk', '', '2', 'x', 'r', '[]', 0, '{}' FROM "
                       "generate_series(1, %s) g")
            c.execute(new_row, ("s", small, 1))
            # a delta wrote a row of it meanwhile: in the default until placed
            c.execute("INSERT INTO ontology.link_rows SELECT * FROM public.step_out")
        out = lp.rebuild(self.s, lp.Rebuild((small,), "public.step_out"))
        self.assertEqual(out["plan"]["new_types"], [small])
        with self.conn() as c, c.cursor() as cur:
            home = c.execute("SELECT tableoid::regclass::text FROM ontology.link_rows WHERE "
                             "link_type = %s", (small,)).fetchall()
            self.assertEqual(len(home), 1)        # one row: the default's copy is gone
            self.assertRegex(home[0][0], r"_b%d_" % lp.bucket(small, 2))
            after = lp.layout(cur)
            self.assertEqual(len(after), len(before))   # no partition added
            self.assertEqual(lp.audit(cur), {"ids_held_twice": 0, "default_rows": 0})
            c.execute("TRUNCATE public.step_out")
            c.execute(new_row, ("b", "Huge.new", 400))
        lp.rebuild(self.s, lp.Rebuild(("Huge.new",), "public.step_out"))
        with self.conn() as c, c.cursor() as cur:
            self.assertIn(("Huge.new",), {p.types for p in lp.layout(cur)})
            self.assertEqual(len(lp.layout(cur)), len(before) + 1)
            self.assertEqual(c.execute("SELECT count(*) FROM ontology.link_rows WHERE "
                                       "link_type = 'Huge.new'").fetchone()[0], 400)

    def test_the_budget_refuses_before_writing(self):
        self.convert(10)
        before = self.parts()
        slow = dict(self.lp.RATES, fill_rows_s_per_worker=1e-6)
        with self.conn() as c, c.cursor() as cur:
            with self.assertRaises(self.lp.OverBudget):
                self.lp.plan(cur, self.lp.Rebuild((BIG,)), rates=slow)
        with self.assertRaises(self.lp.OverBudget):
            self.lp.rebuild(self.s, self.lp.Rebuild((BIG,)), budget_s=0.5)
        self.assertEqual(self.parts(), before)
        # an unpartitioned table, an unknown option: refused
        with self.conn() as c, c.cursor() as cur:
            cur.execute("ALTER TABLE ontology.link_rows RENAME TO link_rows_x")
            cur.execute("CREATE TABLE ontology.link_rows (LIKE ontology.link_rows_x)")
            with self.assertRaises(self.lp.Refused):
                self.lp.plan(cur, self.lp.Rebuild((BIG,)))
        self.assertEqual(self.lp.main(["plan", "--dsn", self.dsn, "--type", BIG]), 2)
        # a rebuild without the new rows would empty the type: refused
        self.assertEqual(self.lp.main(["rebuild", "--dsn", self.dsn, "--type", BIG]), 2)
        with self.assertRaises(SystemExit):
            self.lp.main(["drop", "--dsn", self.dsn])

    def test_the_generation_swap_moves_the_partitions(self):
        from operating.pg import ontology_swap
        self.convert(1)
        ontology_swap.prepare_next(self.dsn)
        with self.conn() as c:
            c.execute("DROP TABLE ontology_next.link_rows CASCADE")
            c.execute("CREATE TABLE ontology_next.link_rows (LIKE ontology.link_rows) "
                      "PARTITION BY LIST (link_type)")
            c.execute("ALTER TABLE ontology_next.link_rows ALTER COLUMN row_no SET DEFAULT "
                      "nextval('ontology_next.link_rows_row_no')")
            c.execute("CREATE TABLE ontology_next.lr_n1 PARTITION OF ontology_next.link_rows "
                      "DEFAULT")
            c.execute("INSERT INTO ontology_next.link_rows SELECT * FROM ontology.link_rows "
                      "WHERE link_type = %s", (BIG,))
        ontology_swap.swap(self.dsn)
        with self.conn() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM ontology.links").fetchone()[0], 2)
            self.assertEqual(c.execute("SELECT relnamespace::regnamespace::text FROM pg_class "
                                       "WHERE relname = 'lr_n1'").fetchone()[0], "ontology")

    def test_the_conversion_gives_the_migrations_indexes_and_its_undo_keeps_later_writes(self):
        lp = self.lp
        self.convert(10, finish=False)
        with self.conn() as c, c.cursor() as cur:
            # the same parent indexes a new database's migration 0017 makes
            got = sorted(c.execute("SELECT indexname, indexdef FROM pg_indexes WHERE "
                                   "schemaname = 'ontology' AND tablename = 'link_rows'"
                                   ).fetchall())
            self.assertEqual(got, self.migrated)
            # the partitions carry the served table's autovacuum options
            self.assertTrue(all(
                r[0] and "autovacuum_vacuum_scale_factor=0.01" in r[0] for r in c.execute(
                    "SELECT c.reloptions FROM pg_inherits i JOIN pg_class c ON c.oid = "
                    "i.inhrelid WHERE i.inhparent = 'ontology.link_rows'::regclass")))
            # while the old table is kept, no rebuild runs (it would escape the undo log)
            with self.assertRaises(lp.Refused):
                lp.plan(cur, lp.Rebuild((BIG,)))
            c.execute("UPDATE ontology.link_rows SET tier = 'contradicted' WHERE link_id = "
                      "'L:0001s'")
            c.execute("DELETE FROM ontology.link_rows WHERE link_id = 'L:0002o'")
            c.execute("INSERT INTO ontology.link_rows (link_id, link_type, from_object, "
                      "to_object, tier, binding_kind, why_not, markings, recorded_at, reason, "
                      "evidence_handles) VALUES ('L:after', 'New.t', 'New:1', 'Big:1', 'lead', "
                      "'fk', '', '2', 'x', 'r', '[]')")
        want = self.snapshot()
        out = lp.undo(self.s)
        self.assertEqual(out["caught_up"], 3)
        with self.conn() as c:
            self.assertEqual(c.execute("SELECT relkind FROM pg_class WHERE oid = "
                                       "'ontology.link_rows'::regclass").fetchone()[0], "r")
            self.assertEqual(c.execute("SELECT count(*) FROM pg_trigger t JOIN pg_class r ON r.oid = t.tgrelid "
                                       "WHERE t.tgname LIKE 'log_%%' AND r.relname LIKE 'link_rows%%'").fetchone()[0], 0)
        self.assertEqual(self.snapshot(), want)
        with self.assertRaises(lp.Refused):
            lp.undo(self.s)
        with self.assertRaises(lp.Refused):
            lp.finish(self.s)
        # a reload's TRUNCATE logs no rows: undo after one refuses, nothing renamed
        self.lp.convert(self.s, own_min=20, buckets=2)
        with self.conn() as c:
            c.execute("TRUNCATE ontology.link_rows")
        with self.assertRaisesRegex(lp.Refused, "truncated"):
            lp.undo(self.s)
        with self.conn() as c:
            self.assertEqual(c.execute("SELECT relkind FROM pg_class WHERE oid = "
                                       "'ontology.link_rows'::regclass").fetchone()[0], "p")
            self.assertEqual(c.execute("SELECT count(*) FROM ontology.link_rows_unpartitioned"
                                       ).fetchone()[0], len(want))
            # back to the rows before the truncate, unpartitioned, for the rest of the test
            c.execute("DROP TABLE ontology.link_rows CASCADE")
            c.execute("ALTER TABLE ontology.link_rows_unpartitioned RENAME TO link_rows")
            c.execute(f"CREATE VIEW ontology.links AS {self.view}")
            c.execute("DELETE FROM ontology.link_rows_rebuilds")
            c.execute("DELETE FROM ontology.link_rows_change_log")
        # converted again after an undo
        self.lp.convert(self.s, own_min=20, buckets=2)
        lp.finish(self.s)
        self.assertEqual(self.snapshot(), want)
        with self.conn() as c:
            self.assertIsNone(c.execute("SELECT to_regclass('ontology.link_rows_unpartitioned')"
                                        ).fetchone()[0])
            self.assertEqual(c.execute("SELECT count(*) FROM pg_trigger t JOIN pg_class r ON r.oid = t.tgrelid "
                                       "WHERE t.tgname LIKE 'log_%%' AND r.relname LIKE 'link_rows%%'").fetchone()[0], 0)

    def test_new_rows_of_a_link_id_another_type_holds_are_refused(self):
        lp = self.lp
        self.convert(10)
        before, parts = self.snapshot(), self.parts()
        with self.conn() as c:
            c.execute("CREATE TABLE public.step_out AS SELECT * FROM ontology.link_rows "
                      "WHERE link_type = %s", (SMALL,))
            # one new row reuses an id OTHER holds in another partition
            c.execute("UPDATE public.step_out SET link_id = 'L:0004o' WHERE link_id = 'L:0004s'")
        with self.assertRaisesRegex(lp.Refused, "link_id_held_by_another_type:1"):
            lp.rebuild(self.s, lp.Rebuild((SMALL,), "public.step_out"))
        self.assertEqual((self.snapshot(), self.parts()), (before, parts))
        with self.conn() as c:
            # or the same id twice in the new rows under two types
            c.execute("UPDATE public.step_out SET link_id = 'L:0004s' WHERE link_id = 'L:0004o'")
            c.execute("INSERT INTO public.step_out SELECT * FROM ontology.link_rows WHERE "
                      "link_id = 'L:0006q'")
            c.execute("UPDATE public.step_out SET link_id = 'L:0007s' WHERE link_id = 'L:0006q'")
        with self.assertRaisesRegex(lp.Refused, "link_id_held_by_another_type"):
            lp.rebuild(self.s, lp.Rebuild((SMALL, ODD), "public.step_out"))
        self.assertEqual((self.snapshot(), self.parts()), (before, parts))

    def test_a_delta_apply_holds_the_rebuild_and_the_swap_waits_for_it(self):
        lp = self.lp
        self.convert(10)
        before, parts = self.snapshot(), self.parts()
        with self.conn() as c:
            c.execute("CREATE TABLE public.step_out AS SELECT * FROM ontology.link_rows "
                      "WHERE link_type = %s", (BIG,))
        delta = pg_testdb.psycopg.connect(self.dsn)     # a delta layer applying
        self.addCleanup(delta.close)
        delta.execute("SELECT pg_advisory_xact_lock(hashtext('ontology_delta'))")
        # the swap never takes the table from under it: refused after its tries
        s = lp.Session(self.dsn, workers=2, yield_to_delta=False)
        with self.assertRaisesRegex(lp.Refused, "swap_lock_not_acquired"):
            lp.rebuild(s, lp.Rebuild((BIG,), "public.step_out"), tries=2)
        self.assertEqual((self.snapshot(), self.parts()), (before, parts))
        # the parallel statements start only once it is done
        s = lp.Session(self.dsn, workers=2, yield_poll_s=0.05)
        import threading
        from unittest import mock
        waiting = threading.Event()
        real = lp.delta_applying

        def seen(cur):
            held = real(cur)
            if held:
                waiting.set()   # a statement is holding back for the delta
            return held

        def release():   # 0.5 s after the first statement found the delta applying
            waiting.wait(30)
            time.sleep(0.5)
            delta.rollback()
        import time
        threading.Thread(target=release, daemon=True).start()
        with mock.patch.object(lp, "delta_applying", seen):
            out = lp.rebuild(s, lp.Rebuild((BIG,), "public.step_out"))
        self.assertTrue(waiting.is_set())
        self.assertGreater(s.yielded_s, 0.3)
        self.assertEqual(out["swap_waited_for_delta"], 0)
        self.assertEqual(self.snapshot(), before)

    def test_migration_0017_partitions_an_empty_table_and_leaves_a_full_one(self):
        from operating.pg import migrate
        sql = next(p for _v, _n, p in migrate.migration_files()
                   if p.name.startswith("0017_")).read_text()
        self.load(_rows(1))
        before = self.snapshot()
        with self.conn() as c:
            c.execute(sql)     # holding rows: left as it is, later migrations still apply
            self.assertEqual(c.execute("SELECT relkind FROM pg_class WHERE oid = "
                                       "'ontology.link_rows'::regclass").fetchone()[0], "r")
        self.assertEqual(self.snapshot(), before)
        self.lp.convert(self.s, own_min=2, buckets=2)
        self.lp.finish(self.s)
        parts = self.parts()
        with self.conn() as c:
            c.execute(sql)     # partitioned already: nothing to do
        self.assertEqual((self.snapshot(), self.parts()), (before, parts))

    def test_the_gate_fails_closed_on_a_projection_without_numbers(self):
        lp = self.lp
        for bad in ({}, {"total_s": 1.0}, {"total_s": float("nan"), "bytes": 1},
                    {"total_s": "1", "bytes": 1}, {"total_s": -1.0, "bytes": 1}):
            with self.subTest(bad=bad), self.assertRaises(lp.OverBudget):
                lp.gate(bad, free=100 << 30)
        lp.gate({"total_s": 10.0, "bytes": 1}, free=100 << 30)
        # the disk: past the free space less the reserve, or unknown
        with self.assertRaises(lp.OverBudget):
            lp.gate({"total_s": 10.0, "bytes": 5 << 30}, free=24 << 30)
        with self.assertRaisesRegex(lp.OverBudget, "free_disk_unknown"):
            lp.gate({"total_s": 10.0, "bytes": 1}, free=None)
        import os
        from unittest import mock
        self.convert(1)
        self.free_patch.stop()   # the real reading, from a folder that cannot be read
        with mock.patch.dict(os.environ, {"PLX_PG_DATA_DIR": "/nonexistent/di03"}), \
                self.conn() as c, c.cursor() as cur:
            self.assertIsNone(lp.free_bytes(cur))
            with self.assertRaisesRegex(lp.OverBudget, "free_disk_unknown"):
                lp.plan(cur, lp.Rebuild((BIG,)))


if __name__ == "__main__":
    unittest.main()
