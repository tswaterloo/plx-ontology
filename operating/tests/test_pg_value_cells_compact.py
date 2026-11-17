"""GA-F13: ``operating.pg.value_cells_compact`` on a generation loaded into a throwaway
Postgres (the fixture store, its word index, the production-only ``value_cells_by_field``,
the document-name index, money cells with a currency and its citation): with writers
changing 1, 10 and 60 sources while it fills, a stopped fill resumed from its marks, the
swap, the undo and the finish, every reader reads exactly the rows the old table gave
(the value views column for column, word queries, document names); the budget and disk
gate, a wrong copy, an unknown dependent and a TRUNCATE while it runs are refused."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import io
import os
import tempfile
import unittest
import unittest.mock

from operating.tests import pg_testdb

ROLE = "plx_test_f13_reader"
WORD_ASKS = ('"a"*', '"order"', '"po"*', '"1"*', '"ship"* OR "inv"*', '"cafe" AND "1234"',
             '"zzqqxnothing"')


class ValueCellsCompactTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        from ontology import project_incremental as inc
        from ontology.generator.fixture import sources
        from ontology.schema import load_schema
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        clock = lambda: datetime(2026, 1, 3, tzinfo=timezone.utc)  # noqa: E731
        schema = load_schema()
        cls.store = inc.build_generation(Path(cls.tmp.name) / "state", schema,
                                         sources(schema, 6), clock=clock, workers=1).store

    def setUp(self):
        import psycopg
        if not os.environ.get("PLX_PG_DATA_DIR"):
            # the run's login (plx_test, ARCH T7) may not read the server's data_directory,
            # so the plan's free-disk read would refuse; the test server is on this host
            patch = unittest.mock.patch.dict(os.environ, {"PLX_PG_DATA_DIR": self.tmp.name})
            patch.start()
            self.addCleanup(patch.stop)
        from operating.pg import document_names as dn, ontology_load
        from operating.pg.migrate import migrate
        self.name, self.dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, self.name)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            migrate(pg)
        ontology_load.load(self.store, self.dsn, workers=1, replace=True, log=io.StringIO())
        dn.build(self.dsn, concurrently=False)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            # the served table's production-only index and grants
            pg.execute("CREATE INDEX value_cells_by_field ON ontology.value_cells (field, src)")
            pg.execute(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
                       f"'{ROLE}') THEN CREATE ROLE {ROLE}; END IF; END $$")
            pg.execute(f"GRANT SELECT ON ontology.value_cells TO {ROLE}")
            pg.execute("ALTER TABLE ontology.value_cells SET (autovacuum_vacuum_scale_factor "
                       "= 0.01, autovacuum_vacuum_cost_limit = 1000)")
            # money cells: a number, a currency and the currency's citation
            pg.execute("INSERT INTO ontology.value_kinds (kind, value_type, unit, parser, "
                       "markings) VALUES (900001, 'money', NULL, 'made-up', '[]')")
            src, field = pg.execute("SELECT src, max(field) FROM ontology.value_cells "
                                    "GROUP BY src ORDER BY src LIMIT 1").fetchone()
            pg.execute("INSERT INTO ontology.value_fields VALUES (900001, 'amount_due', "
                       "'row.amount_due'), (900002, 'name', 'row.cafe_name')")
            pg.execute("INSERT INTO ontology.value_cells VALUES (%s, 900001, 900001, "
                       "'1234.50', 1234.50, 'CAD', 'row.currency_code')", (src,))
            (text_kind,) = pg.execute("SELECT kind FROM ontology.value_kinds WHERE "
                                      "value_type = 'text' LIMIT 1").fetchone()
            pg.execute("INSERT INTO ontology.value_cells (src, field, kind, value) VALUES "
                       "(%s, 900002, %s, 'Ünïcode CAFÉ PO-1234')", (src, text_kind))
            pg.execute("VACUUM ANALYZE ontology.value_cells")
            self.text_kind = text_kind

    # --- what the readers read ----------------------------------------------------------

    def sql(self, statement, params=None):
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            cur = pg.execute(statement, params)
            return cur.fetchall() if cur.description else None

    def reads(self) -> dict:
        """Every reader path over the value cells: each value view whole (an order-free
        digest of every column of every row, and its row count), the word queries the
        tools send, the documents found by name."""
        import json
        from operating import document_tables as dt
        from operating.pg import ontology_reader
        from operating.pg.ontology_reader import VALUE_WORD_JOIN
        from operating.pg.sqlite_compat import PgCompatConnection
        out = {}
        for view, cols in (("object_values_typed", "*"), ("object_values", "*"),
                           ("object_values_numbered", "*"),
                           ("value_words", "row_no, src, field, words::text"),
                           ("value_cells", "*")):
            rows = sorted(json.dumps(r, default=str) for r in self.sql(
                f"SELECT row_to_json(x) FROM (SELECT {cols} FROM ontology.{view}) x"))
            out[view] = (len(rows), hashlib.sha256("\n".join(rows).encode()).hexdigest())
        pg = PgCompatConnection(self.dsn)
        try:
            pg.words = True
            sql = ("SELECT v.object_id, v.property, v.field_path, v.value, v.currency, "
                   "v.currency_citation FROM " + VALUE_WORD_JOIN +
                   " WHERE value_text MATCH :q AND f.rowid > :after")
            out["words"] = {q: sorted(map(tuple, pg.execute(sql, {"q": q, "after": 0})))
                            for q in WORD_ASKS}
        finally:
            pg.close()
        reader = ontology_reader.PgStoreReader.open(self.dsn)
        try:
            with reader.lock:
                out["documents"] = [[(d.object_id, d.handle, d.name) for d in
                                     dt.find_documents(reader.conn, ask).docs]
                                    for ask in (["cafe"], ["doc"], ["a*"])]
        finally:
            reader.close() if hasattr(reader, "close") else None
        out["money"] = self.sql("SELECT value, value_num, currency, currency_citation, "
                                "value_type FROM ontology.object_values_typed WHERE "
                                "property = 'amount_due'")
        return out

    def catalog(self) -> dict:
        """What the served table carries: index names and definitions (as on the served
        name), the key's constraint, owner and grants, options, word statistics target,
        the views reading it."""
        rows = self.sql(
            "SELECT c.relname, regexp_replace(pg_get_indexdef(i.indexrelid), ' ON \\S+ ', "
            "' ON t ') FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE i.indrelid = 'ontology.value_cells'::regclass ORDER BY 1")
        return {
            "indexes": rows,
            "pkey": self.sql("SELECT conname FROM pg_constraint WHERE conrelid = "
                             "'ontology.value_cells'::regclass AND contype = 'p'"),
            "acl": self.sql("SELECT relowner::regrole::text, relacl::text, reloptions "
                            "FROM pg_class WHERE oid = 'ontology.value_cells'::regclass"),
            "word_stats": self.sql("SELECT attstattarget FROM pg_attribute WHERE attrelid = "
                                   "'ontology.value_cells_words'::regclass AND attnum = 1"),
            "views": self.sql(
                "SELECT DISTINCT r.ev_class::regclass::text FROM pg_depend d JOIN pg_rewrite "
                "r ON r.oid = d.objid WHERE d.refobjid = 'ontology.value_cells'::regclass "
                "AND r.ev_class <> 'ontology.value_cells'::regclass ORDER BY 1"),
            "triggers": self.sql("SELECT tgname FROM pg_trigger WHERE tgrelid = "
                                 "'ontology.value_cells'::regclass AND NOT tgisinternal"),
        }

    def write(self, n: int, tag: str) -> None:
        """A delta's apply on ``n`` sources: a source's cells deleted and written again
        with one value changed, one cell added, one cell's value updated in place, one
        cell deleted."""
        srcs = [r[0] for r in self.sql(
            "SELECT DISTINCT src FROM ontology.value_cells WHERE field < 900000 "
            "ORDER BY src DESC LIMIT %s", (n * 3,))]
        redo, upd, gone = srcs[:n], srcs[n:2 * n], srcs[2 * n:3 * n]
        import psycopg
        with psycopg.connect(self.dsn) as pg:   # one transaction per source, as an apply
            for src in redo:
                pg.execute("CREATE TEMP TABLE keep ON COMMIT DROP AS SELECT * FROM "
                           "ontology.value_cells WHERE src = %s", (src,))
                pg.execute("DELETE FROM ontology.value_cells WHERE src = %s", (src,))
                pg.execute("UPDATE keep SET value = value || %s WHERE value_num IS NULL AND "
                           "field = (SELECT min(field) FROM keep WHERE value_num IS NULL)",
                           (f" {tag}",))
                pg.execute("INSERT INTO ontology.value_cells SELECT * FROM keep")
                pg.execute("INSERT INTO ontology.value_cells (src, field, kind, value) VALUES "
                           "(%s, 900002, %s, %s) ON CONFLICT (src, field) DO UPDATE SET "
                           "value = excluded.value", (src, self.text_kind, f"added {tag} po-{src}"))
                pg.commit()
        if upd:
            self.sql("UPDATE ontology.value_cells SET value = value || %s WHERE src = "
                     "ANY(%s) AND value_num IS NULL AND value IS NOT NULL",
                     (f" upd{tag}", upd))
        if gone:
            self.sql("DELETE FROM ontology.value_cells WHERE src = ANY(%s) AND field = "
                     "(SELECT min(field) FROM ontology.value_cells WHERE src = ANY(%s))",
                     (gone, gone))

    def session(self, **kw):
        from operating.pg import value_cells_compact as vc
        return vc.Session(self.dsn, workers=4, maintenance_mem="64MB", yield_poll_s=0.05,
                          **kw)

    # --- the conversion ------------------------------------------------------------------

    def test_readers_read_the_same_rows_through_fill_writes_resume_swap_undo_and_finish(self):
        from operating.pg import value_cells_compact as vc
        catalog = self.catalog()
        self.assertEqual(len(catalog["indexes"]), 4, catalog)
        cards = iter((1, 10, 60))

        class Stop(Exception):
            pass

        def after(i):
            if i in (0, 2, 4):
                self.write(next(cards, 1), f"t{i}")
            if i == 5:
                raise Stop()   # the fill stopped part way (a killed job)

        s = self.session(after_range=after)
        s.workers = 1   # ranges in order, so the stop lands where it is asked
        with self.assertRaises(Stop):
            vc.convert(s, budget_s=3600, reserve=0)
        state = self.sql("SELECT count(*) FROM ontology.value_cells_compact_marks")[0][0]
        self.assertEqual(state, 6, "the ranges committed before the stop are marked")
        self.assertGreater(self.sql("SELECT count(*) FROM ontology.value_cells_compact_log")
                           [0][0], 60, "every write while it fills is logged")
        with self.assertRaisesRegex(Exception, "TRUNCATE refused"):
            self.sql("TRUNCATE ontology.value_cells")

        s = self.session(after_range=lambda i: self.write(1, f"r{i}") if i == 8 else None)
        out = vc.convert(s, budget_s=3600, reserve=0)
        self.assertEqual(out["fill"]["ranges_resumed"], 6, out)
        self.assertEqual(out["check"]["differ"], 0, out)
        self.write(10, "beforeswap")   # written after the check: caught up in the swap
        before = self.reads()
        swapped = vc.swap(self.session())
        self.assertEqual(sorted(swapped["views"]), ["ontology.object_values_typed",
                                                    "ontology.value_words"])
        self.assertEqual(self.reads(), before, "every reader reads the same rows")
        after_cat = self.catalog()
        self.assertEqual({k: v for k, v in after_cat.items() if k != "triggers"},
                         {k: v for k, v in catalog.items() if k != "triggers"})
        self.assertNotEqual(self.sql("SELECT 'ontology.value_cells'::regclass::oid")[0][0],
                            self.sql("SELECT 'ontology.value_cells_precompact'::regclass::oid"
                                     )[0][0])
        self.assertEqual(self.sql(f"SELECT has_table_privilege('{ROLE}', "
                                  "'ontology.value_cells', 'SELECT')")[0][0], True)

        # writes after the swap go to the new table and an undo carries them back
        self.write(60, "afterswap")
        written = self.reads()
        self.assertNotEqual(written, before)
        undone = vc.undo(self.session())
        self.assertTrue(undone["undone"])
        self.assertEqual(self.reads(), written, "the undone table holds the later writes")
        self.assertEqual(self.catalog(), {**catalog, "triggers": []})
        self.assertEqual(self.sql("SELECT to_regclass('ontology.value_cells_compact'), "
                                  "to_regclass('ontology.value_cells_precompact'), "
                                  "to_regclass('ontology.value_cells_compact_state')"),
                         [(None, None, None)])

        # again, then finished: the old table checked and dropped, nothing left behind
        vc.convert(self.session(), budget_s=3600, reserve=0, do_swap=True)
        self.write(10, "beforefinish")
        final = self.reads()
        done = vc.finish(self.session())
        self.assertEqual(done["check"]["differ"], 0)
        self.assertGreater(done["freed_bytes"], 0)
        self.assertEqual(self.reads(), final)
        self.assertEqual(self.catalog(), {**catalog, "triggers": []})
        self.assertEqual(self.sql(
            "SELECT count(*) FROM pg_class WHERE relnamespace = 'ontology'::regnamespace "
            "AND relname LIKE 'value_cells_%%' AND relname ~ '(compact|precompact|_cmp)'"),
            [(0,)])
        self.assertEqual(self.sql("SELECT count(*) FROM pg_proc WHERE proname LIKE "
                                  "'value_cells_compact%%'"), [(0,)])
        # the key holds: one row per (src, field)
        self.assertEqual(self.sql("SELECT count(*) - count(DISTINCT (src, field)) FROM "
                                  "ontology.value_cells"), [(0,)])

    def test_a_wrong_copy_unknown_dependent_or_over_budget_is_refused(self):
        from operating.pg import value_cells_compact as vc
        from operating.pg.link_partitions import OverBudget, Refused
        clean = self.catalog()
        # over the time budget, or the disk: refused before any write
        with self.assertRaises(OverBudget):
            vc.convert(self.session(), budget_s=0.001, reserve=0)
        with self.assertRaises(OverBudget):
            vc.convert(self.session(), budget_s=3600, reserve=1 << 60)
        with self.assertRaisesRegex(OverBudget, "free_disk_unknown"):
            vc.gate({"total_s": 1, "bytes": 1}, budget_s=10, free=None, reserve=0)
        with self.assertRaisesRegex(OverBudget, "projection_without_bytes"):
            vc.gate({"total_s": 1, "bytes": float("nan")}, budget_s=10, free=1 << 40,
                    reserve=0)
        # the command line converts only on a rehearsal that ran every phase clean
        run = {"readers_same_after_swap": True, "check": {"differ": 0}}
        good = {"runs": [{**run, "readers_same_after_undo": True},
                         {**run, "finish": {"check": {"differ": 0, "rows": 5}}}],
                "writer": {"transactions": 9, "error": None},
                "rates": {k: 1.5 for k in vc.RATES}}
        self.assertEqual(vc.rehearsed_rates(good), {k: 1.5 for k in vc.RATES})
        for broken, why in (({**good, "runs": good["runs"][:1]}, "incomplete"),
                            ({**good, "writer": {"error": "boom"}}, "incomplete"),
                            ({**good, "runs": [good["runs"][0], {**run}]}, "finish"),
                            ({**good, "runs": [{**run}, good["runs"][1]]}, "undo"),
                            ({**good, "rates": {**good["rates"], "swap_s": 0}}, "swap_s"),
                            ({**good, "rates": {}}, "rate")):
            with self.assertRaisesRegex(Refused, why):
                vc.rehearsed_rates(broken)
        self.assertEqual(vc.main(["convert", "--dsn", self.dsn]), 2, "no rehearsal: refused")
        self.assertEqual(self.catalog(), clean)
        self.assertEqual(self.sql("SELECT to_regclass('ontology.value_cells_compact_state')"),
                         [(None,)])
        # a trigger this module would not carry over: refused, nothing made
        self.sql("CREATE FUNCTION ontology.f13_t() RETURNS trigger LANGUAGE plpgsql AS "
                 "$$ BEGIN RETURN NULL; END $$")
        self.sql("CREATE TRIGGER someone_elses AFTER INSERT ON ontology.value_cells "
                 "FOR EACH STATEMENT EXECUTE FUNCTION ontology.f13_t()")
        with self.assertRaisesRegex(Refused, "trigger:someone_elses"):
            vc.convert(self.session(), budget_s=3600, reserve=0)
        self.sql("DROP TRIGGER someone_elses ON ontology.value_cells")
        # a copy that differs from the served table (a cell changed behind the log's back)
        s = self.session()
        vc.prepare(s, budget_s=3600, reserve=0)
        vc.fill(s)
        vc.build_indexes(s)
        vc.vacuum(s)
        self.sql("UPDATE ontology.value_cells_compact SET value_num = value_num + 0.01 "
                 "WHERE currency = 'CAD'")
        with self.assertRaises(vc.Mismatch):
            vc.check(s)
        with self.assertRaisesRegex(Refused, "not_checked"):
            vc.swap(s)
        reads = self.reads()
        vc.undo(s)
        self.assertEqual(self.catalog(), clean)
        self.assertEqual(self.reads(), reads)


    def test_writers_in_flight_low_disk_unfinished_f3_and_locks_are_handled(self):
        """Review 82d27fa0ef: (1) a writer still in flight while a catch-up reads the log
        keeps its entry (two writers: one commits before the read, one during it); (2) a
        finish that fails part way leaves the old table and the undo triggers together;
        (3) prepare waits for a busy table with a lock timeout, a swap facing more logged
        sources than its bound lets go and catches up outside first; (4) an unfinished F3
        refuses unless overridden; (5) the fill stops resumable when the disk falls under
        the reserve, and the projection counts the WAL."""
        import threading
        import psycopg
        from operating.pg import value_cells_compact as vc
        from operating.pg.link_partitions import Refused
        (src_a, src_b) = [r[0] for r in self.sql(
            "SELECT DISTINCT src FROM ontology.value_cells WHERE value_num IS NULL AND "
            "field < 900000 ORDER BY src LIMIT 2")]

        # (4) GA-F3 recorded as unfinished: refused before anything is made
        self.sql("CREATE TABLE ontology.f3_batches (phase text NOT NULL, n int NOT NULL, "
                 "key text NOT NULL, lo text NOT NULL, hi text, PRIMARY KEY (phase, n))")
        self.sql("INSERT INTO ontology.f3_batches VALUES ('objects', 0, 'T', ''), "
                 "('links', 1, 'L', '')")
        self.sql("INSERT INTO ontology.load_state (name, value) VALUES ('f3:objects:T:0', "
                 "'{}')")
        with self.assertRaisesRegex(Refused, "f3_unfinished: links 1 batches left"):
            vc.convert(self.session(), budget_s=3600, reserve=0)
        self.assertEqual(self.sql("SELECT to_regclass('ontology.value_cells_compact_state')"),
                         [(None,)])
        self.sql("INSERT INTO ontology.load_state (name, value) VALUES ('f3:links:L:1', '{}')")

        # (5) WAL counted; the disk falls under the reserve part way: stopped, resumable
        proj = vc.project(1000, workers=4, btrees=3, max_wal=1 << 20)
        self.assertEqual(proj["bytes"], proj["new_bytes"] + proj["wal_bytes"])
        self.assertEqual(proj["wal_bytes"], min(proj["new_bytes"], 2 << 20))
        seen = []

        def free(cur):
            seen.append(1)
            return 10 << 30 if len(seen) <= 3 else 1 << 30

        s = self.session(free=free)
        s.workers = 1
        # (3) prepare waits while a writer holds the table, then proceeds
        holder = psycopg.connect(self.dsn)
        holder.execute("LOCK TABLE ontology.value_cells IN ROW EXCLUSIVE MODE")
        s.lock_timeout_ms = 100
        threading.Timer(1.0, holder.commit).start()
        with self.assertRaises(vc.DiskLow):
            vc.convert(s, budget_s=3600, reserve=5 << 30)
        holder.close()
        self.assertEqual(self.sql("SELECT count(*) FROM ontology.value_cells_compact_marks"),
                         [(3,)], "the ranges done before the disk ran low are kept")
        s = self.session()
        out = vc.convert(s, budget_s=3600, reserve=0)
        self.assertEqual(out["fill"]["ranges_resumed"], 3)

        # (1) two writers: B commits before the catch-up reads the log, A is in flight
        w_a = psycopg.connect(self.dsn)
        w_a.execute("UPDATE ontology.value_cells SET value = 'writer A' WHERE src = %s AND "
                    "value_num IS NULL", (src_a,))
        self.sql("UPDATE ontology.value_cells SET value = 'writer B' WHERE src = %s AND "
                 "value_num IS NULL", (src_b,))

        class Cur:
            def __init__(self, cur):
                self.cur = cur

            def __getattr__(self, name):
                return getattr(self.cur, name)

            def execute(self, sql, *args):
                out = self.cur.execute(sql, *args)
                if sql.startswith("INSERT INTO compact_ids"):
                    w_a.commit()   # A commits after the log was read, before its delete
                return out

        with s.connect(autocommit=False) as conn, conn.cursor() as cur:
            vc.catch_up(Cur(cur), vc.TABLE, vc.NEW, vc.LOG)
            conn.commit()
        w_a.close()
        self.assertEqual(self.sql("SELECT DISTINCT src FROM "
                                  "ontology.value_cells_compact_log"), [(src_a,)],
                         "the in-flight writer's entry stays for the next catch-up")
        self.assertEqual(vc.check(s)["differ"], 0)
        self.assertEqual(self.sql("SELECT DISTINCT value FROM ontology.value_cells_compact "
                                  "WHERE src = %s AND value_num IS NULL", (src_a,)),
                         [("writer A",)])

        # (3) more logged sources than the locked bound: let go, catch up, swap
        # (the writes land just after the swap's catch-up outside the lock)
        self.write(10, "backlog")
        s.locked_catch_up_max = 5
        real_catch_up, calls = vc._catch_up_txn, []

        def late(*args):
            calls.append(args)
            return 0 if len(calls) == 1 else real_catch_up(*args)
        with unittest.mock.patch.object(vc, "_catch_up_txn", late):
            swapped = vc.swap(s)
        self.assertGreaterEqual(swapped["swap_attempts"], 2, swapped)
        self.assertEqual(self.sql("SELECT count(*) FROM ontology.value_cells_compact_undo"),
                         [(0,)])

        # (2) a finish failing part way changes nothing: undo still restores every write
        self.write(10, "afterswap")
        written = self.reads()
        real = vc._drop_functions

        def boom(cur):
            raise RuntimeError("crash inside finish")
        vc._drop_functions = boom
        try:
            with self.assertRaisesRegex(RuntimeError, "crash inside finish"):
                vc.finish(s)
        finally:
            vc._drop_functions = real
        self.assertEqual(self.sql("SELECT to_regclass('ontology.value_cells_precompact') "
                                  "IS NOT NULL, (SELECT count(*) FROM pg_trigger WHERE "
                                  "tgname LIKE 'value_cells_compact_undo%%')"), [(True, 4)])
        vc.undo(s)
        self.assertEqual(self.reads(), written)


if __name__ == "__main__":
    unittest.main()
