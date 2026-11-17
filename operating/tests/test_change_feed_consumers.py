"""Every consumer of the change feed (``operating.pg.change_feed``, migration 0026) on the
test cluster: each reads only the rows past its own cursor, resumes from it after a run
that did not commit, and keeps its own way when the feed is not there."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from operating.tests.test_pg_change_feed import _db


def write(dsn: str, ids, generation, *, name="delta:x", kind="c"):
    from operating.pg import change_feed
    import psycopg
    with psycopg.connect(dsn) as pg, pg.cursor() as cur:
        out = change_feed.record_ids(cur, name, "delta", generation, ids, kind)
        # records the test database does not hold: typed by their id's prefix
        cur.execute("UPDATE ontology.change_feed SET object_type = split_part(object_id, "
                    "':', 1) WHERE object_type IS NULL")
        return out


def empty(dsn: str) -> None:
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute("TRUNCATE ontology.change_feed, ontology.change_cycles, "
                   "ontology.change_cursors")


def position(dsn: str, consumer: str):
    from operating.pg import change_feed
    with change_feed.connect(dsn) as pg:
        return change_feed.position(pg, consumer)


class ProactiveIncremental(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dsn = _db(cls, feed=True)
        cls.bare = _db(cls, feed=False)

    def setUp(self):
        from operating import proactive_engine as pe
        from operating import proactive_incremental as pi
        from operating.pg import ontology_reader
        from operating.tests.test_proactive_engine import AS_OF, people
        from operating.tests.test_proactive_incremental import MOMENT, world
        empty(self.dsn)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        pi.INPUTS.clear()
        self.addCleanup(pi.INPUTS.clear)
        w, self.ids = world()
        self.store = store = root / "store.sqlite"
        w.build(store)
        pe.run(store, pe.inbox_path(store), AS_OF, clock=lambda: MOMENT, people=people())
        with sqlite3.connect(store) as conn:
            self.digest = conn.execute("SELECT value FROM store_meta WHERE key = "
                                       "'content_digest'").fetchone()[0]
        self.delta = root / "delta"
        self.delta.mkdir()
        self.layers: list[dict] = []
        self.applied: set[str] = set()
        real_meta = pe._Store.meta
        version = [100]
        self.version = version

        def stamped(st, key):
            value = real_meta(st, key)
            return f"{value}@{version[0]}" if key == "content_digest" else value
        self.target = [self.dsn]
        for patch in (mock.patch.object(pe._Store, "meta", stamped),
                      mock.patch.object(pi, "_applied", lambda _s: set(self.applied)),
                      mock.patch.object(pi, "_pg_neighbors", lambda ids, out:
                                        pi._file_neighbors(store, ids, out)),
                      mock.patch.object(ontology_reader, "dsn", lambda: self.target[0])):
            patch.start()
            self.addCleanup(patch.stop)
        self.run_pass = lambda: pi.run_pass(store, clock=lambda: MOMENT, as_of=AS_OF,
                                            people=people(), wait=True)

    def publish(self, name, owned):
        layer = self.delta / name
        with sqlite3.connect(layer) as conn:
            conn.execute("CREATE TABLE delta_owned (object_id TEXT PRIMARY KEY)")
            conn.executemany("INSERT INTO delta_owned VALUES (?)", [(o,) for o in owned])
        self.layers.insert(0, {"store": str(layer), "owned": len(owned)})
        (self.delta / "CURRENT.json").write_text(json.dumps({
            "format": "plx-delta/v1", "base_digest": self.digest,
            "store": self.layers[0]["store"], "layers": self.layers}))
        self.applied.add(name)
        self.version[0] += 1

    def taken(self):
        from operating import proactive_engine as pe
        from operating import proactive_incremental as pi
        with sqlite3.connect(pi.changes_path(pe.inbox_path(self.store))) as conn:
            return {Path(r[0]).name for r in conn.execute("SELECT layer FROM layers")}

    def test_the_pass_reads_past_its_cursor_and_resumes_there(self):
        from operating.pg import change_feed
        ids = self.ids
        self.publish("delta-1.sqlite", [ids["unconfirmed"], ids["expired"]])
        first = self.run_pass()       # first read: the layers, as before; then the cursor
        self.assertEqual((first["changed"], first["feed"]["state"]), (2, "start"), first)
        self.assertTrue(first["feed"]["committed"])
        # the next layer owns two records (cumulative), the feed says one changed
        self.publish("delta-2.sqlite", [ids["unconfirmed"], ids["expired"], ids["overdue"]])
        write(self.dsn, [ids["overdue"]], self.digest, name="delta:delta-2.sqlite")
        second = self.run_pass()
        self.assertEqual((second["candidates"], second["feed"]["state"]), (1, "ok"), second)
        self.assertEqual(second["changed"], 1)
        self.assertEqual(self.taken(), {"delta-1.sqlite", "delta-2.sqlite"})  # unopened
        self.assertEqual(self.run_pass()["candidates"], 0)    # nothing new
        # a pass whose cursor is not written reads the same rows again
        write(self.dsn, [ids["late_with_lead"]], self.digest)
        with mock.patch.object(change_feed, "commit_to", return_value=False):
            lost = self.run_pass()
        self.assertEqual((lost["candidates"], lost["feed"]["committed"]), (1, False), lost)
        again = self.run_pass()
        self.assertEqual((again["candidates"], again["changed"]), (1, 0), again)  # same print
        self.assertEqual(self.run_pass()["candidates"], 0)
        # another generation's rows are not this one's changes
        write(self.dsn, [ids["expired"]], "sha256:another")
        self.assertEqual(self.run_pass()["candidates"], 0)
        # a migration's scope: the rules judged whole
        import psycopg
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            change_feed.record_scope(cur, "migration:m", self.digest, ["PurchaseOrder:"])
        whole = self.run_pass()
        self.assertEqual((whole["state"], whole["flooded"]), ("ran", 0), whole)
        self.assertEqual(self.run_pass()["candidates"], 0)   # judged once

    def test_without_the_feed_the_layers_are_read_as_before(self):
        ids = self.ids
        for target in (self.bare, "postgresql://127.0.0.1:1/none"):
            self.target[0] = target
            self.publish(f"delta-{target[-4:]}.sqlite", [ids["unconfirmed"]])
            out = self.run_pass()
            self.assertNotIn("feed", out)
            self.assertEqual(out["candidates"], 1, out)
        self.assertIsNone(position(self.dsn, "proactive_incremental"))

    def test_a_gap_reads_every_layer_again(self):
        ids = self.ids
        self.publish("delta-1.sqlite", [ids["unconfirmed"], ids["expired"]])
        self.run_pass()
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as pg:   # rows past it were pruned
            pg.execute("INSERT INTO ontology.change_cursors (consumer, position) VALUES "
                       "('_pruned', 1000000) ON CONFLICT (consumer) DO UPDATE SET "
                       "position = excluded.position")
        gap = self.run_pass()
        self.assertEqual((gap["feed"]["state"], gap["candidates"]), ("gap", 2), gap)
        self.assertGreaterEqual(position(self.dsn, "proactive_incremental"), 1000000)


class AiWatcher(unittest.TestCase):
    """Postgres the only store: the watcher's changes are the feed's rows past its cursor
    (before the feed: the versions past its own version mark)."""

    @classmethod
    def setUpClass(cls):
        cls.dsn = _db(cls, feed=True)
        import io
        from ontology.tests.test_delta import DeltaEquivalenceTests
        from ontology.tests.test_projection import T1
        from operating.pg import ontology_load
        fixture = DeltaEquivalenceTests("test_changed_records_equal_a_full_build_and_nothing_"
                                        "else_is_owned")
        fixture.setUp()
        cls.addClassCleanup(fixture.doCleanups)
        fixture.capture(T1)
        cls.now = T1
        ontology_load.load(fixture.full("gen1.sqlite"), cls.dsn, workers=1, replace=True,
                           log=io.StringIO())
        import psycopg
        with psycopg.connect(cls.dsn) as pg:
            cls.generation = pg.execute("SELECT value FROM ontology.store_meta WHERE key = "
                                        "'content_digest'").fetchone()[0]
            cls.orders = [r[0] for r in pg.execute(
                "SELECT object_id FROM ontology.objects WHERE object_type = 'PurchaseOrder' "
                "ORDER BY 1")]

    def setUp(self):
        from operating.pg import ontology_reader
        empty(self.dsn)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.watch = Path(tmp.name) / "watch"
        self.store = Path(tmp.name) / "none.sqlite"
        for patch in (mock.patch.dict("os.environ", {"PLX_ONTOLOGY_PG": "only"}),
                      mock.patch.object(ontology_reader, "dsn", lambda: self.dsn)):
            patch.start()
            self.addCleanup(patch.stop)

    def discover(self, ws):
        from datetime import timedelta
        from operating import ai_watcher as aw
        return aw.discover(ws, self.store, now=self.now + timedelta(hours=2), guard=None,
                           window_hours=24 * 3650)

    def test_the_watcher_reads_past_its_cursor_and_resumes_there(self):
        from contextlib import closing
        from operating import ai_watcher as aw
        from operating.pg import change_feed
        self.assertGreaterEqual(len(self.orders), 2)
        a, b = self.orders[:2]
        with closing(aw.open_store(self.watch)) as ws:
            self.assertEqual(self.discover(ws)["layers"], 0)   # the first look
            start = position(self.dsn, "ai_watcher")
            self.assertIsNotNone(start)
            write(self.dsn, [a], self.generation)
            found = self.discover(ws)
            self.assertEqual((found["layers"], found["owned"]), (1, 1), found)
            queued = {r[0] for r in ws.execute("SELECT object_id FROM queue")}
            self.assertEqual(queued, {a})
            self.assertEqual(self.discover(ws)["layers"], 0)   # nothing new
            # a run whose cursor is not written: the batch is recorded, the next run
            # moves the cursor without queueing it twice
            write(self.dsn, [b], self.generation)
            with mock.patch.object(change_feed, "commit_to", return_value=False):
                self.assertEqual(self.discover(ws)["layers"], 1)
            moved = position(self.dsn, "ai_watcher")
            self.assertEqual(self.discover(ws)["layers"], 0)
            self.assertGreater(position(self.dsn, "ai_watcher"), moved)
            # another generation's rows, a migration's scope: noted, not queued
            write(self.dsn, [a], "sha256:another")
            import psycopg
            with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
                change_feed.record_scope(cur, "migration:m", self.generation,
                                         ["PurchaseOrder:"])
            self.assertEqual(self.discover(ws)["layers"], 0)
            self.assertEqual({r[0] for r in ws.execute("SELECT object_id FROM queue")},
                             {a, b})
            # a batch past the cap: set aside whole, the cursor past it
            with mock.patch.object(aw, "PG_BATCH_OBJECTS", 1):
                write(self.dsn, self.orders[:3], self.generation)
                self.assertEqual(self.discover(ws)["layers"], 0)
            self.assertEqual(self.discover(ws)["layers"], 0)

    def test_a_gap_reads_the_versions_past_the_last_mark(self):
        """Rows pruned past the cursor: the versions numbered since the last batch the
        watcher recorded are looked at once, then the feed is followed again."""
        from contextlib import closing
        import psycopg
        from operating import ai_watcher as aw
        a, b = self.orders[:2]
        with closing(aw.open_store(self.watch)) as ws:
            self.discover(ws)
            write(self.dsn, [a], self.generation)
            self.assertEqual(self.discover(ws)["owned"], 1)
            with psycopg.connect(self.dsn) as pg:   # b changes; the feed's row of it is lost
                pg.execute("UPDATE ontology.object_versions SET superseded_at = "
                           "'2026-09-29T13:30:00Z' WHERE object_id = %s AND superseded_at "
                           "IS NULL", (b,))
                pg.execute("INSERT INTO ontology.object_versions (ver, object_id, l1_handle, "
                           "digest, lineage, markings, recorded_at) SELECT max(ver) + 1, %s, "
                           "'h:new', 'changed', '[]', '[]', '2026-09-29T13:30:00Z' FROM "
                           "ontology.object_versions", (b,))
                pg.execute("INSERT INTO ontology.change_cursors (consumer, position) VALUES "
                           "('_pruned', 1000000)")
            found = self.discover(ws)
            self.assertEqual(found["layers"], 1, found)
            self.assertIn(b, {r[0] for r in ws.execute("SELECT object_id FROM queue")})
            self.assertGreaterEqual(position(self.dsn, "ai_watcher"), 1000000)
            self.assertEqual(self.discover(ws)["layers"], 0)   # following the feed again

    def test_without_the_feed_the_versions_are_read(self):
        from contextlib import closing
        from operating import ai_watcher as aw
        from operating.pg import change_feed
        with mock.patch.object(change_feed, "read", return_value=None):
            with closing(aw.open_store(self.watch)) as ws:
                changes = aw._postgres_changes(ws, "2026-11-14T00:00:00Z")
                self.assertEqual(changes[1], [])        # the first look: from here on
                self.assertIsNone(changes[4])
                marks = [r[0] for r in ws.execute("SELECT store FROM layers")]
        self.assertTrue(marks and all(m.startswith("postgres:") for m in marks), marks)


class AutopilotFollow(unittest.TestCase):
    """The live feed's layer records: the feed's rows past its cursor, the applied layers
    taken in unopened (the window feed is unchanged by this)."""

    @classmethod
    def setUpClass(cls):
        cls.dsn = _db(cls, feed=True)

    def setUp(self):
        from operating import autopilot as ap
        from operating import proactive_incremental as pi
        from operating.pg import ontology_reader
        empty(self.dsn)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.store = root / "store.sqlite"
        self.queue = ap.Queue(root / "autopilot" / "autopilot.sqlite")
        self.addCleanup(self.queue.close)
        delta = root / "delta"
        delta.mkdir()
        self.layer = delta / "delta-1.sqlite"
        with sqlite3.connect(self.layer) as conn:   # owns everything since the generation
            conn.execute("CREATE TABLE delta_owned (object_id TEXT PRIMARY KEY)")
            conn.executemany("INSERT INTO delta_owned VALUES (?)",
                             [(f"Email:{i}",) for i in range(5)])
        (delta / "CURRENT.json").write_text(json.dumps({
            "format": "plx-delta/v1", "base_digest": "g1", "store": str(self.layer),
            "layers": [{"store": str(self.layer), "owned": 5}]}))
        self.target = [self.dsn]
        for patch in (mock.patch.object(pi, "_applied", lambda _s: {"delta-1.sqlite"}),
                      mock.patch("operating.delta_overlay.pointer_path",
                                 lambda _s: delta / "CURRENT.json"),
                      mock.patch.object(ontology_reader, "dsn", lambda: self.target[0]),
                      mock.patch.object(ap, "window_ids", lambda *a, **k: set())):
            patch.start()
            self.addCleanup(patch.stop)
        self.st = mock.Mock(digest="g1@7")

    def follow(self):
        from datetime import datetime, timezone
        from operating import autopilot as ap
        return ap.follow_ids(self.st, self.store, self.queue, {}, datetime.now(timezone.utc))

    def commit(self, batch):
        from operating.pg import change_feed
        self.assertTrue(change_feed.commit_to(self.dsn, batch))

    def test_follow_reads_past_its_cursor(self):
        ids, taken, read, batch = self.follow()     # first read: the layer, as before
        self.assertEqual((len(ids), read["feed"]["state"]), (5, "start"))
        self.commit(batch)
        self.queue.conn.execute("INSERT INTO layers VALUES (?, ?)", (str(self.layer), "g1"))
        self.queue.conn.commit()
        write(self.dsn, ["Email:3"], "g1")
        ids, taken, read, batch = self.follow()
        self.assertEqual((ids, taken, read["feed"]["state"]), ({"Email:3"}, [], "ok"))
        # not committed: the same rows; committed: nothing new
        self.assertEqual(self.follow()[0], {"Email:3"})
        self.commit(batch)
        self.assertEqual(self.follow()[0], set())
        # more than the cap: left to the window, the cursor past them
        from operating import autopilot as ap
        write(self.dsn, [f"Email:{i}" for i in range(10)], "g1")
        with mock.patch.object(ap, "FOLLOW_LAYER_CAP", 3):
            ids, _taken, read, batch = self.follow()
        self.assertEqual((ids, read["layer_records_unread"]), (set(), 3))
        self.commit(batch)
        self.assertEqual(self.follow()[0], set())

    def test_without_the_feed_the_layers_are_read(self):
        self.target[0] = "postgresql://127.0.0.1:1/none"
        ids, taken, read, batch = self.follow()
        self.assertEqual((len(ids), [Path(t).name for t in taken], batch),
                         (5, ["delta-1.sqlite"], None))
        self.assertNotIn("feed", read)


class VendorWatch(unittest.TestCase):
    """The vendor stage runs again only when the feed past its cursor holds a mail or
    purchase order change (or a migration's scope), on a new business day, after an
    incomplete stage or a full run in the same pass; without the feed, always."""

    @classmethod
    def setUpClass(cls):
        cls.dsn = _db(cls, feed=True)

    def setUp(self):
        empty(self.dsn)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.inbox = Path(tmp.name) / "inbox.sqlite"
        self.inbox.write_bytes(b"")
        self.target = [self.dsn]
        self.stages = []
        self.status = ["complete"]

    def run_pass(self, day, refresh="never", serves=True):
        from datetime import date
        from types import SimpleNamespace
        from operating import case_workspace_live as live
        from operating import host, proactive_engine as pe, proactive_pool
        from operating import vendor_watch as vw
        from operating.pg import ontology_reader

        class Session:
            def __init__(self, *a, **k):
                pass

            def close(self):
                pass

        def fake_run(*a, **k):
            self.stages.append(day)
            return {"detectors": {"vendor_terms_unapplied": self.status[0]}}
        config = SimpleNamespace(tenant="t", principal="p", mirror_audience="aud",
                                 sources=())
        with mock.patch.object(live, "offline_runtime", lambda *a, **k: mock.Mock()), \
                mock.patch.object(live, "_Session", Session), \
                mock.patch.object(host, "engine_readers", lambda c: None), \
                mock.patch.object(host, "projection_path", lambda d: Path("store")), \
                mock.patch.object(proactive_pool, "from_host_config", lambda *a, **k: None), \
                mock.patch("integrations.source_mirror.AudienceMap.from_file",
                           lambda p: object()), \
                mock.patch.object(pe, "served_behind", lambda s: False), \
                mock.patch.object(pe, "engine_marking_grants", lambda a: {}), \
                mock.patch.object(pe, "inbox_path", lambda s: self.inbox), \
                mock.patch.object(pe, "refresh_pass", lambda *a, **k: {"detectors": {}}), \
                mock.patch.object(pe, "run", fake_run), \
                mock.patch.object(ontology_reader, "serves", lambda s: serves), \
                mock.patch.object(ontology_reader, "dsn", lambda: self.target[0]), \
                mock.patch("operating.meeting_followup.cycle", lambda *a, **k: {"ran": 1}):
            return vw.run_pass(Path("state"), config, seconds=100, bound=1,
                               as_of=date(2026, 11, day), refresh=refresh)

    def test_the_stage_runs_only_when_what_it_reads_moved(self):
        first = self.run_pass(14)
        self.assertEqual((first["feed"]["state"], self.stages), ("start", [14]))
        self.assertEqual(first["meetings"], {"ran": 1})
        quiet = self.run_pass(14)
        self.assertEqual((quiet["vendor_stage"], self.stages), ("unchanged", [14]))
        self.assertEqual(quiet["meetings"], {"ran": 1})        # meetings always run
        write(self.dsn, ["Item:1"], "g1")                       # not what it reads
        self.assertEqual(self.run_pass(14)["vendor_stage"], "unchanged")
        write(self.dsn, ["Email:1"], "g1")                      # a reply arrived
        self.run_pass(14)
        self.assertEqual(self.stages, [14, 14])
        self.assertEqual(self.run_pass(14)["vendor_stage"], "unchanged")
        self.run_pass(15)                                       # a new business day
        self.assertEqual(self.stages, [14, 14, 15])
        import psycopg
        from operating.pg import change_feed
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            change_feed.record_scope(cur, "migration:m", "g1", ["Item:"])
        self.run_pass(15)                                       # a migration's scope
        self.assertEqual(len(self.stages), 4)
        self.status[0] = "truncated"                            # left orders unread
        write(self.dsn, ["PurchaseOrder:1"], "g1")
        self.run_pass(15)
        self.run_pass(15)
        self.assertEqual(len(self.stages), 6)
        self.status[0] = "complete"
        self.run_pass(15)
        self.run_pass(15, refresh="always")                      # a full run went first
        self.assertEqual(len(self.stages), 8)

    def test_without_the_feed_the_stage_always_runs(self):
        for serves, target in ((False, self.dsn), (True, "postgresql://127.0.0.1:1/none")):
            self.target[0] = target
            for _ in range(2):
                out = self.run_pass(14, serves=serves)
                self.assertIsNone(out["vendor_stage"])
                self.assertIsNone(out["feed"])
        self.assertEqual(len(self.stages), 4)


class TruthCheck(unittest.TestCase):
    """The check says what data it is current to and which claims' records changed since
    the last check, reading only the feed's rows past its cursor."""

    @classmethod
    def setUpClass(cls):
        cls.dsn = _db(cls, feed=True)

    def setUp(self):
        empty(self.dsn)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)

    def run_check(self, dsn=None):
        from operating import truth_check as tc
        results = [{"object_id": oid, "order": order, "kind": "late_po", "late_claim": True,
                    "contradicted": False, "verdict": "supported", "reasons": []}
                   for oid, order in (("PurchaseOrder:1", "PurchaseOrder:1"),
                                      ("PurchaseOrderLine:7", "PurchaseOrder:2"))]
        with mock.patch.object(tc, "mirror_rows", lambda *a: {}), \
                mock.patch.object(tc, "bc_records", lambda *a: []), \
                mock.patch.object(tc, "people_guard", lambda *a: (None, "test")), \
                mock.patch.object(tc, "inbox_claims", lambda p: []), \
                mock.patch.object(tc, "check", lambda *a: {"results": [dict(r) for r in
                                                                      results],
                                                           "by_kind": {}, "late_list": None}), \
                mock.patch.object(tc, "probes", lambda *a: []), \
                mock.patch.object(tc, "detector_health", lambda *a: []), \
                mock.patch.object(tc, "_money_profile", lambda s: {}), \
                mock.patch.object(tc, "_derived_code", lambda s: None):
            out = tc.run(self.state, pg_dsn=dsn or self.dsn)
        status = json.loads((self.state / "truth-check" / "status.json").read_text())
        return out, status

    def test_the_check_reads_past_its_cursor(self):
        write(self.dsn, ["PurchaseOrder:9"], "g1", name="delta:first")
        out, status = self.run_check()
        self.assertEqual((out["data_through"]["state"], out["data_through"]["cycle"]),
                         ("start", "delta:first"))
        self.assertIsNone(status["claims_changed_since_last"])   # first read: not known
        self.assertTrue(status["data_through_applied_at"])
        write(self.dsn, ["PurchaseOrder:2", "Item:4"], "g1", name="delta:second")
        out, status = self.run_check()
        marks = {r["object_id"]: r["changed_since_last_check"] for r in out["results"]}
        self.assertEqual(marks, {"PurchaseOrder:1": False, "PurchaseOrderLine:7": True})
        self.assertEqual((status["claims_changed_since_last"], out["data_through"]["cycle"],
                          out["data_through"]["changed_records"]), (1, "delta:second", 2))
        out, status = self.run_check()                     # nothing since
        self.assertEqual(status["claims_changed_since_last"], 0)
        self.assertEqual(out["data_through"]["cycle"], "delta:second")

    def test_without_the_feed_nothing_is_said(self):
        for dsn in ("postgresql://127.0.0.1:1/none",):
            out, status = self.run_check(dsn)
            self.assertIsNone(out["data_through"])
            self.assertIsNone(status["data_through_applied_at"])
            self.assertNotIn("changed_since_last_check", out["results"][0])


if __name__ == "__main__":
    unittest.main()
