"""The applied-cycle change feed (migration 0026, ``operating.pg.change_feed``) on the test
cluster: the delta apply writes the records it really changed in its own transaction (a
failed apply leaves no row, a layer re-owning records unchanged adds none), each consumer
reads past its own cursor and resumes from it, rows every consumer passed are pruned, and
without the migration nothing reads or writes it."""

from __future__ import annotations

import io
import json
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

from operating.tests import pg_testdb
from operating.tests.test_pg_ontology_delta import MIGRATIONS

FEED_MIGRATION = Path(__file__).resolve().parents[1] / "pg" / "migrations" / \
    "0026_change_feed.sql"


def _db(cls, *, feed: bool) -> str:
    """A throwaway database with the ontology's migrations and, with ``feed``, the feed's
    tables (``change_feed.DDL``: migration 0026 lands apart, after the train)."""
    from operating.pg import change_feed
    name, dsn = pg_testdb.create_db()
    cls.addClassCleanup(pg_testdb.drop_db, name)
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as pg:
        for migration in MIGRATIONS:
            pg.execute(migration.read_text(encoding="utf-8"))
        if feed:
            pg.execute(change_feed.DDL)
    return dsn


class TheMigrationIsTheModulesDDL(unittest.TestCase):
    def test_the_migration_file_and_the_ddl_agree_once_both_are_here(self):
        from operating.pg import change_feed
        self.assertNotIn('"char"', change_feed.DDL)   # gen_prisma reads no "char" column
        if not FEED_MIGRATION.exists():
            self.skipTest("migration 0026 lands apart (after the train)")
        self.assertEqual(FEED_MIGRATION.read_text(encoding="utf-8"), change_feed.DDL)


class DeltaWritesTheFeed(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dsn = _db(cls, feed=True)

    def layer(self):
        from ontology.tests.test_delta import DeltaEquivalenceTests
        from ontology.tests.test_projection import T1, T2
        from operating.pg import ontology_load
        fixture = DeltaEquivalenceTests("test_changed_records_equal_a_full_build_and_nothing_"
                                        "else_is_owned")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.capture(T1)
        base = fixture.full("gen1.sqlite")
        ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())
        fixture.fake.set("public.app_purchase_order", "po-a", status="RECEIVED")
        fixture.fake.set("public.app_purchase_order_line", "l1", purchase_order_id="po-b")
        fixture.capture(T2)
        store = fixture.cycle(base)["store"]
        with sqlite3.connect(store) as conn:
            owned = {r[0] for r in conn.execute("SELECT object_id FROM delta_owned")}
        return store, owned

    def rows(self, cycle_name):
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            return pg.execute(
                "SELECT f.object_id, f.object_type, f.kind FROM ontology.change_feed f JOIN "
                "ontology.change_cycles c USING (cycle_id) WHERE c.name = %s ORDER BY f.seq",
                (cycle_name,)).fetchall()

    def test_the_apply_writes_only_what_it_changed_and_a_failed_one_nothing(self):
        from operating.pg import ontology_delta, ontology_migrate
        import psycopg
        store, owned = self.layer()
        name = f"delta:{Path(store).name}"
        with psycopg.connect(self.dsn) as pg:
            before = pg.execute("SELECT count(*), (SELECT count(*) FROM "
                                "ontology.change_cycles) FROM ontology.change_feed").fetchone()
        # a failure after the feed rows are written (the owned log, then the record)
        with mock.patch.object(ontology_migrate, "log_owned", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                ontology_delta.apply_layer(store, self.dsn)
        with psycopg.connect(self.dsn) as pg:
            self.assertEqual(pg.execute(
                "SELECT count(*), (SELECT count(*) FROM ontology.change_cycles) FROM "
                "ontology.change_feed").fetchone(), before)

        applied = ontology_delta.apply_layer(store, self.dsn)
        self.assertTrue(applied["applied"], applied)
        rows = self.rows(name)
        ids = [r[0] for r in rows]
        self.assertEqual(len(ids), len(set(ids)))            # one row a record
        self.assertEqual(applied["feed"]["rows"], len(rows))
        self.assertTrue(rows)
        kinds = {r[0]: r[2] for r in rows}
        self.assertTrue(set(kinds.values()) <= {"n", "c", "r", "l"}, kinds)
        # the order whose status moved, and the line that moved to another order
        changed = {oid for oid, k in kinds.items() if k in ("c", "n")}
        self.assertTrue(any(o.startswith("PurchaseOrder") and "po-a" in o for o in changed),
                        rows)
        self.assertTrue(any("l1" in o for o in changed), rows)
        # a row's own rows moved only when it was owned; any other record only by a link
        for oid, kind in kinds.items():
            if oid not in owned:
                self.assertEqual(kind, "l", oid)
        # every record of a row exists (or was removed), and its type is known
        self.assertTrue(all(r[1] for r in rows), rows)
        self.assertLess(len([k for k in kinds.values() if k == "c"]), len(owned) + 1)

        # the same layer applied again (its record removed): it owns the same records with
        # the same rows, so the cycle adds no row
        with psycopg.connect(self.dsn) as pg:
            pg.execute("DELETE FROM ontology.load_state WHERE name = %s", (name,))
        again = ontology_delta.apply_layer(store, self.dsn)
        self.assertTrue(again["applied"], again)
        self.assertEqual(again["feed"]["rows"], 0, again["feed"])
        self.assertEqual(len(self.rows(name)), len(rows))   # only the first cycle's


class ReadIncrementally(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dsn = _db(cls, feed=True)
        cls.bare = _db(cls, feed=False)

    def setUp(self):
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("TRUNCATE ontology.change_feed, ontology.change_cycles, "
                       "ontology.change_cursors")

    def write(self, ids, *, generation="g1", name="delta:x"):
        from operating.pg import change_feed
        import psycopg
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            return change_feed.record_ids(cur, name, "delta", generation, ids, "c")

    def read(self, consumer, **kw):
        from operating.pg import change_feed
        with change_feed.connect(self.dsn) as pg:
            return change_feed.read_since(pg, consumer, **kw)

    def commit(self, batch):
        from operating.pg import change_feed
        self.assertTrue(change_feed.commit_to(self.dsn, batch))

    def test_each_consumer_reads_past_its_own_cursor_and_resumes_there(self):
        self.write(["A:1", "A:2"])
        first = self.read("proactive_incremental")
        self.assertEqual(first.state, "start")     # its own way once, then the feed
        self.assertEqual(first.changes, ())
        self.commit(first)
        self.write(["A:3"])
        self.write(["A:1", "A:4"])
        batch = self.read("proactive_incremental")
        self.assertEqual((batch.state, sorted(batch.ids())), ("ok", ["A:1", "A:3", "A:4"]))
        # not committed (the pass failed): the same rows again
        self.assertEqual(self.read("proactive_incremental").ids(), batch.ids())
        self.commit(batch)
        self.assertEqual(self.read("proactive_incremental").changes, ())
        # another consumer's cursor is its own
        self.commit(self.read("ai_watcher"))
        self.write(["A:5"])
        self.assertEqual(self.read("ai_watcher").ids(), {"A:5"})
        self.assertEqual(self.read("proactive_incremental").ids(), {"A:5"})
        # a cursor never moves back
        from operating.pg import change_feed
        with change_feed.connect(self.dsn) as pg:
            change_feed.commit(pg, "ai_watcher", 0)
            self.assertEqual(change_feed.position(pg, "ai_watcher"), batch.end)

    def test_unknown_inputs_fail_closed(self):
        from operating.pg import change_feed
        with change_feed.connect(self.dsn) as pg:
            for name in ("proactive", "Truth_check", "_pruned", "x; DROP", ""):
                with self.assertRaises(ValueError):
                    change_feed.read_since(pg, name)
                with self.assertRaises(ValueError):
                    change_feed.commit(pg, name, 1)
            for bad in (-1, True, "3", 1.5):
                with self.assertRaises(ValueError):
                    change_feed.commit(pg, "ai_watcher", bad)
            with self.assertRaises(ValueError):
                change_feed.read_since(pg, "ai_watcher", limit=0)
            with self.assertRaises(ValueError):
                change_feed.record_ids(pg.cursor(), "x", "elsewhere", None, ["A:1"])
            with self.assertRaises(ValueError):
                change_feed.record_ids(pg.cursor(), "x", "delta", None, ["A:1"], "m")
        with self.assertRaises(ValueError):
            change_feed.read(self.dsn, "nobody")

    def test_without_the_migration_nothing_reads_or_writes(self):
        from operating.pg import change_feed
        import psycopg
        with change_feed.connect(self.bare) as pg:
            self.assertIsNone(change_feed.read_since(pg, "truth_check"))
            self.assertEqual(change_feed.status(pg), {"present": False})
        with psycopg.connect(self.bare) as pg, pg.cursor() as cur:
            self.assertFalse(change_feed.snapshot(cur))
            self.assertIsNone(change_feed.record_ids(cur, "x", "delta", None, ["A:1"]))
            self.assertIsNone(change_feed.record_scope(cur, "x", None, ["A:"]))
            cur.execute("SELECT to_regclass('pg_temp.feed_before')")
            self.assertIsNone(cur.fetchone()[0])
        # an unreachable database: None (the consumer's own way), never an error
        self.assertIsNone(change_feed.read("postgresql://127.0.0.1:1/none", "truth_check"))
        self.assertFalse(change_feed.commit_to("postgresql://127.0.0.1:1/none",
                                               change_feed.Batch("truth_check", "ok", 0, 1)))

    def test_no_row_lost_or_doubled_at_any_cycle_size(self):
        self.commit(self.read("vendor_watch"))
        expected: list[str] = []
        for n in (1, 10, 60, 1000):
            ids = [f"B:{n}:{i}" for i in range(n)]
            self.write(ids + ids[:1])        # a repeated id is one row
            expected += ids
        batch = self.read("vendor_watch")
        got = [c.object_id for c in batch.changes]
        self.assertEqual(sorted(got), sorted(expected))
        self.assertEqual([c.seq for c in batch.changes], sorted(c.seq for c in batch.changes))
        # read in parts: every row once
        self.setUp()
        self.commit(self.read("vendor_watch"))
        self.write(expected)
        seen: list[str] = []
        while True:
            part = self.read("vendor_watch", limit=97)
            if not part.changes:
                break
            self.assertTrue(part.overflow or len(part.changes) <= 97)
            seen += [c.object_id for c in part.changes]
            self.commit(part)
        self.assertEqual(sorted(seen), sorted(expected))

    def test_types_generation_and_migrations(self):
        self.commit(self.read("autopilot_follow"))
        self.write(["Email:1", "PurchaseOrder:1", "Item:1"])
        self.write(["Email:2"], generation="g0")
        from operating.pg import change_feed
        import psycopg
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            cur.execute("UPDATE ontology.change_feed SET object_type = split_part(object_id, "
                        "':', 1)")
            change_feed.record_scope(cur, "migration:m1", "g1", ["Item:"])
        self.write(["Gone:1"])   # no record to type it by: it may be of any type
        batch = self.read("autopilot_follow", types=["Email", "PurchaseOrder"],
                          generation="g1")
        self.assertEqual(batch.ids(), {"Email:1", "PurchaseOrder:1", "Gone:1"})
        self.assertEqual(batch.migrated, ["Item:"])
        self.assertEqual(batch.generations, frozenset({"g0", "g1"}))
        # the cursor moves past what was filtered out
        self.commit(batch)
        self.assertEqual(self.read("autopilot_follow").changes, ())

    def test_rows_every_consumer_passed_are_pruned_and_a_cursor_behind_is_a_gap(self):
        from operating.pg import change_feed
        import psycopg
        for consumer in ("proactive_incremental", "ai_watcher"):
            self.commit(self.read(consumer))
        self.write([f"C:{i}" for i in range(5)])
        self.commit(self.read("proactive_incremental"))
        # ai_watcher has not read them: kept
        self.write(["C:9"])
        self.assertEqual(len(self.read("ai_watcher").changes), 6)
        self.commit(self.read("ai_watcher"))
        self.write(["C:10"])   # the writer prunes what both passed
        with change_feed.connect(self.dsn) as pg:
            self.assertEqual(change_feed.status(pg)["rows"], 2)   # C:9 (proactive) and C:10
        # a consumer quiet past the stale window no longer holds rows; past the keep
        # window rows go whatever the cursors say, and a cursor below them reads a gap
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("UPDATE ontology.change_cycles SET applied_at = now() - interval "
                       "'30 days'")
        self.write(["C:11"])
        gap = self.read("proactive_incremental")
        self.assertEqual(gap.state, "gap")
        self.assertEqual(gap.changes, ())
        self.commit(gap)
        self.write(["C:12"])
        self.assertEqual(self.read("proactive_incremental").ids(), {"C:12"})
        with change_feed.connect(self.dsn) as pg:
            held = change_feed.status(pg)
        self.assertLessEqual(held["rows"], 2, held)


LINK_COLS = ("link_id", "link_type", "from_object", "to_object", "tier", "binding_kind",
             "link_contract", "reason", "why_not", "evidence_handles", "reviewed_by",
             "reviewed_at", "review_action_id", "markings", "recorded_at", "superseded_at")


def _link(link_id, kind, a, b, tier="verified"):
    return (link_id, kind, a, b, tier, "fk", None, "r", "", "[]", None, None, None, "[]",
            "2026-11-01T00:00:00Z", None)


class InPlaceWritersWriteTheFeed(unittest.TestCase):
    """Every path that changes served rows outside a delta apply writes the feed in its
    own transaction: the staged step apply and its undo (also a delta layer's undo), a
    link apply's kinds and their undo, a chunked migration's switch, the people-exclusion
    purge (types only, never an id)."""

    @classmethod
    def setUpClass(cls):
        cls.dsn = _db(cls, feed=True)

    def setUp(self):
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("TRUNCATE ontology.change_feed, ontology.change_cycles, "
                       "ontology.change_cursors, ontology.link_rows, ontology.objects")
            pg.execute("DROP SCHEMA IF EXISTS feed_test_stage CASCADE")

    def cycles(self):
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            rows = pg.execute("SELECT c.name, c.source, f.object_id, f.kind FROM "
                              "ontology.change_feed f JOIN ontology.change_cycles c USING "
                              "(cycle_id) ORDER BY f.seq").fetchall()
        out: dict[str, set] = {}
        for name, source, oid, kind in rows:
            out.setdefault(name, set()).add((source, oid, kind))
        return out

    def serve(self, links):
        import psycopg
        with psycopg.connect(self.dsn) as pg:
            with pg.cursor() as cur:
                cur.executemany(f"INSERT INTO ontology.link_rows ({', '.join(LINK_COLS)}) "
                                f"VALUES ({', '.join(['%s'] * len(LINK_COLS))})", links)

    def test_a_staged_step_apply_and_its_undo(self):
        import tempfile
        from operating.pg import step_staging
        self.serve([_link("t:1", "K", "A:1", "B:1"), _link("t:2", "K", "A:2", "B:2")])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Path(tmp.name) / "step.sqlite"
        with sqlite3.connect(store) as conn:   # t:1 changed, t:2 gone, t:3 new
            conn.execute(f"CREATE TABLE links ({', '.join(LINK_COLS)})")
            conn.executemany(f"INSERT INTO links VALUES ({', '.join(['?'] * len(LINK_COLS))})",
                             [_link("t:1", "K", "A:1", "B:1", "corroborated"),
                              _link("t:3", "K", "A:3", "B:3")])
        with mock.patch.object(step_staging, "SCHEMA", "feed_test_stage"):
            step_staging.stage(self.dsn, "run", "s1", 0, store,
                               [{"step": "s1", "prefix": "t:", "end": "t;", "from": None}])
            step_staging.apply_now(self.dsn, "run", "s1")
            ends = {"A:1", "B:1", "A:2", "B:2", "A:3", "B:3"}
            self.assertEqual(self.cycles(), {"step:s1:0": {("links", o, "l")
                                                           for o in ends}})
            step_staging.undo(self.dsn, "s1")
        got = self.cycles()
        self.assertEqual({o for _s, o, _k in got["undo:s1:0"]}, ends)

    def test_a_link_apply_kind_and_its_undo_record_their_ends(self):
        import psycopg
        from operating.pg import change_feed
        self.serve([_link("x:9", "K", "A:9", "B:9")])
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            cur.execute("CREATE SCHEMA feed_test_stage")
            cur.execute("CREATE TABLE feed_test_stage.undo_deleted (LIKE ontology.link_rows)")
            cur.execute("CREATE TABLE feed_test_stage.undo_inserted (link_id text, "
                        "link_type text)")
            cur.execute("INSERT INTO feed_test_stage.undo_deleted SELECT * FROM "
                        "ontology.link_rows WHERE link_id = 'x:9'")
            cur.execute("DELETE FROM ontology.link_rows WHERE link_id = 'x:9'")
            cur.execute(f"INSERT INTO ontology.link_rows ({', '.join(LINK_COLS)}) VALUES "
                        f"({', '.join(['%s'] * len(LINK_COLS))})", _link("x:8", "K", "A:8",
                                                                         "B:8"))
            cur.execute("INSERT INTO feed_test_stage.undo_inserted VALUES ('x:8', 'K')")
            change_feed.record_link_ends(cur, "links:m:K", "feed_test_stage", ("K",),
                                         step_column="link_type")
        self.assertEqual({o for _s, o, _k in self.cycles()["links:m:K"]},
                         {"A:9", "B:9", "A:8", "B:8"})
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            for bad in ("x; DROP", "Stage"):
                with self.assertRaises(ValueError):
                    change_feed.record_link_ends(cur, "n", bad, ("K",))
            with self.assertRaises(ValueError):
                change_feed.record_link_ends(cur, "n", "feed_test_stage", ("K",),
                                             step_column="1=1 OR link_type")

    def test_a_chunked_migrations_switch_writes_its_scopes(self):
        import psycopg
        from operating.pg import migrate_chunks
        record = {"id": "m1", "from": "a", "to": "b", "scope": ["PurchaseOrder:"],
                  "delta_mark": 0}
        steps = {"id": "s1", "from": {}, "to": "c", "scope": ["Email:"], "delta_mark": 0,
                 "steps": ["mentions"]}
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            with mock.patch.object(migrate_chunks, "steps_from", lambda r: "x"):
                migrate_chunks._final(cur, record, steps, "k")
        with psycopg.connect(self.dsn) as pg:
            rows = pg.execute("SELECT c.name, f.object_type, f.kind FROM ontology.change_feed "
                              "f JOIN ontology.change_cycles c USING (cycle_id) ORDER BY "
                              "c.name").fetchall()
        self.assertEqual(rows, [("migration:m1", "PurchaseOrder:", "m"),
                                ("migration:steps:s1", "Email:", "m")])

    def test_the_exclusion_purge_writes_types_never_ids(self):
        import psycopg
        from operating import mail_exclusion_purge as purge
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            cur.execute("INSERT INTO ontology.objects (object_id, object_type, binding_id, "
                        "native_key, audience_key, first_recorded_at) VALUES "
                        "('Email:secret-1', 'Email', 'b', 'k', 'a', 'x')")
            cur.execute("INSERT INTO ontology.object_versions (ver, object_id, l1_handle, "
                        "digest, lineage, markings, recorded_at) VALUES (1, 'Email:secret-1', "
                        "'graph:mail:secret-1', 'd', 'l', '[]', 'x')")
        self.serve([_link("m:1", "Email.mentions_purchase_order", "Email:secret-1",
                          "PurchaseOrder:7")])
        out = purge.purge_pg(self.dsn, ["graph:mail:secret-1"], apply=True)
        self.assertEqual(out["objects"], 1)
        with psycopg.connect(self.dsn) as pg:
            rows = pg.execute("SELECT c.source, f.object_id, f.object_type, f.kind FROM "
                              "ontology.change_feed f JOIN ontology.change_cycles c USING "
                              "(cycle_id)").fetchall()
            names = pg.execute("SELECT name FROM ontology.change_cycles").fetchall()
        self.assertEqual(sorted(rows), [("purge", "*", "Email:", "m"),
                                        ("purge", "*", "PurchaseOrder:", "m")])
        self.assertNotIn("secret", json.dumps(rows + names))
        # a dry run writes nothing
        purge.purge_pg(self.dsn, ["graph:mail:secret-2"], apply=False)
        with psycopg.connect(self.dsn) as pg:
            self.assertEqual(pg.execute("SELECT count(*) FROM ontology.change_cycles"
                                        ).fetchone()[0], 1)


class AConsumerThatCannotWriteItsCursor(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dsn = _db(cls, feed=True)

    def test_it_reads_its_own_way_and_says_so_and_health_names_it(self):
        import contextlib
        import io as _io
        from datetime import datetime, timezone
        from operating import ops_health
        from operating.pg import change_feed
        err = _io.StringIO()
        with mock.patch.object(change_feed, "writable", lambda cur: False), \
                contextlib.redirect_stderr(err):
            self.assertIsNone(change_feed.read(self.dsn, "truth_check"))
        self.assertIn("CHANGE FEED: truth_check: cannot write its cursor", err.getvalue())
        import psycopg
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            change_feed.record_ids(cur, "delta:x", "delta", "g", ["A:1"], "c")
            cur.execute("UPDATE ontology.change_cycles SET applied_at = now() - "
                        "interval '8 hours'")
            change_feed.commit(cur, "ai_watcher", 0)
            cur.execute("UPDATE ontology.change_cursors SET updated_at = now() - "
                        "interval '7 hours'")
        t = ops_health.targets({})
        check = ops_health.change_feed_check(datetime.now(timezone.utc), t, self.dsn)
        self.assertEqual(check["state"], "breach", check)
        self.assertIn("truth_check (never read it)", check["detail"])
        self.assertIn("ai_watcher (1 rows behind", check["detail"])
        self.assertEqual(ops_health.change_feed_check(datetime.now(timezone.utc), t, None)
                         ["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
