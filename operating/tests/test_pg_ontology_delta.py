"""A row-delta layer applied to the Postgres ontology (``operating.pg.ontology_delta``):
over a loaded generation, each object the layer owns then holds exactly a full build's
rows of the newer captures; a layer of another generation or applied before changes
nothing."""

from __future__ import annotations

import os
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from operating.tests import pg_testdb

MIGRATIONS = [Path(__file__).resolve().parents[1] / "pg" / "migrations" / name
              for name in ("0006_ontology.sql", "0007_ontology_compact.sql",
                           "0008_ontology_read.sql")]


class ReloadRoomTests(unittest.TestCase):
    """Live 2026-11-06: the Postgres copy (a fixed 150 GB floor) left a cold build too
    little room. A load keeps the next build's room (``build_reserve``) and a new
    generation's reload starts only when it fits beside it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        generations = self.state / "projection" / "generations"
        for name, size in (("00000001", 300), ("00000002", 400)):
            (generations / name).mkdir(parents=True)
            (generations / name / "store.sqlite").write_bytes(b"\0" * size)
        self.source = generations / "00000002" / "store.sqlite"
        reserve = mock.patch("operating.host.build_reserve",
                             return_value={"reserve_bytes": 1000})
        reserve.start()
        self.addCleanup(reserve.stop)

    def room(self, free, *, in_place=True, min_free=0):
        from operating.pg import ontology_delta, ontology_load
        with mock.patch.object(ontology_load, "_free_bytes", return_value=free):
            return ontology_delta.reload_room(self.state, self.source, held_bytes=500,
                                              in_place=in_place, min_free=min_free)

    def test_a_reload_waits_until_it_fits_beside_the_builds_room(self):
        # in place: the held copy (500) is emptied first, the load writes 500, 1000 kept
        self.assertEqual((self.room(999)["state"], self.room(999)["previous_bytes"]),
                         ("waiting_for_prune", 300))
        self.assertTrue(self.room(1000)["fits"])
        # beside the held copy: room for both
        self.assertEqual(self.room(1499, in_place=False)["state"], "waiting_for_prune")
        self.assertTrue(self.room(1500, in_place=False)["fits"])
        # a higher fixed floor still counts
        self.assertEqual(self.room(1000, min_free=2000)["floor_bytes"], 2000)
        # the previous generation pruned: what is short is space
        (self.state / "projection" / "generations" / "00000001" / "store.sqlite").unlink()
        self.assertEqual(self.room(999)["state"], "waiting_for_space")

    def test_the_load_floor_is_the_builds_room_on_its_file_system(self):
        from operating.pg import ontology_load
        self.assertEqual(ontology_load.build_floor(None, 0, self.state)[1:],
                         (1000, {"reserve_bytes": 1000}))
        self.assertEqual(ontology_load.build_floor(str(self.state), 5000, self.state)[1], 5000)
        self.assertEqual(ontology_load.build_floor("/x", 7, None), ("/x", 7, None))
        # builds retired: no build's room is held, only the fixed floor
        with mock.patch.dict(os.environ, {"PLX_BUILDS_OFF": "1"}):
            self.assertEqual(ontology_load.build_floor(None, 7, self.state), (None, 7, None))
        (self.state / "projection" / "CURRENT").write_text("{}")
        self.assertEqual(ontology_load.state_of(self.source), self.state.resolve())
        self.assertIsNone(ontology_load.state_of(self.state / "elsewhere.sqlite"))


class DeltaToPostgresTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)
        import psycopg
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            for migration in MIGRATIONS:
                pg.execute(migration.read_text(encoding="utf-8"))

    def test_owned_objects_hold_the_full_builds_rows_once(self):
        from ontology.tests.test_delta import DeltaEquivalenceTests
        from ontology.tests.test_projection import T1, T2
        from operating.pg import ontology_delta, ontology_load
        from operating.pg.ontology_parity import _BY_OBJECT, _pg_rows, _sqlite_rows
        import psycopg

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
        report = fixture.cycle(base)
        full = fixture.full("gen2.sqlite")

        applied = ontology_delta.apply_layer(report["store"], self.dsn)
        self.assertTrue(applied["applied"], applied)
        with sqlite3.connect(report["store"]) as layer:
            owned = [r[0] for r in layer.execute("SELECT object_id FROM delta_owned")]
        self.assertGreaterEqual(len(owned), 3)
        conn = sqlite3.connect(f"file:{full}?mode=ro", uri=True)
        self.addCleanup(conn.close)
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            for oid in owned:
                for part in [*_BY_OBJECT, "object_values"]:
                    self.assertEqual(_sqlite_rows(conn, part, oid), _pg_rows(cur, part, oid),
                                     f"{part}:{oid}")
        again = ontology_delta.apply_layer(report["store"], self.dsn)
        self.assertEqual((again["applied"], again["reason"]), (False, "already_applied"))
        # a load in progress (a table not recorded loaded) takes no layer
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            kept = pg.execute("SELECT value FROM ontology.load_state WHERE name = "
                              "'table:link_rows'").fetchone()[0]
            pg.execute("DELETE FROM ontology.load_state WHERE name = 'table:link_rows'")
            self.assertEqual(ontology_delta.apply_layer(report["store"], self.dsn)["reason"],
                             "generation_loading")
            pg.execute("INSERT INTO ontology.load_state (name, value) VALUES "
                       "('table:link_rows', %s)", (psycopg.types.json.Jsonb(kept),))

        # follow: what a state directory serves; the layer above is applied once, and a
        # served generation Postgres does not hold is reported, never half applied
        import json
        state = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, state, True)
        (state / "projection" / "generations" / "00000001").mkdir(parents=True)
        (state / "projection" / "delta").mkdir()
        (state / "projection" / "generations" / "00000001" / "store.sqlite").hardlink_to(base)
        (state / "projection" / "ontology.sqlite").hardlink_to(base)
        with sqlite3.connect(base) as conn:
            digest = conn.execute("SELECT value FROM store_meta WHERE key = "
                                  "'content_digest'").fetchone()[0]
        (state / "projection" / "delta" / "CURRENT.json").write_text(json.dumps(
            {"base_digest": digest, "layers": [{"store": report["store"], "owned": 3}]}))
        out = ontology_delta.follow(state, self.dsn, log=io.StringIO())
        self.assertEqual((out["state"], out["layers"]["applied_now"], "loaded" in out),
                         ("current", 0, False))
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("UPDATE ontology.load_state SET value = jsonb_set(value, "
                       "'{content_digest}', '\"sha256:other\"') WHERE name = 'generation'")
        out = ontology_delta.follow(state, self.dsn, reload=False, log=io.StringIO())
        self.assertEqual(out["state"], "generation_differs")
        # a reload in place waits for requests begun on Postgres before the publish
        out = ontology_delta.follow(state, self.dsn, log=io.StringIO())
        self.assertEqual(out["state"], "waiting_for_readers")


    def test_over_a_postgres_base_each_cycle_brings_only_its_changes_and_nothing_folds(self):
        """``PLX_DELTA_BASE_PG``: the closure reads Postgres, each cycle's layer is applied
        in the cycle, the next cycle projects only its own changes over it, and the fold
        has nothing to do; every owned object then holds a full build's rows."""
        from datetime import timedelta
        from ontology import delta
        from ontology.tests.test_delta import DeltaEquivalenceTests, normalizer
        from ontology.tests.test_projection import T1, T2
        from operating.pg import ontology_load
        from operating.pg.ontology_parity import _BY_OBJECT, _pg_rows, _sqlite_rows
        import psycopg

        fixture = DeltaEquivalenceTests("test_changed_records_equal_a_full_build_and_nothing_"
                                        "else_is_owned")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.capture(T1)
        base = fixture.full("gen1.sqlite")
        ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())
        environ = {"PLX_DELTA_BASE_PG": "1", "PLX_PG_DSN": self.dsn}

        def cycle(**options):
            return delta.build_delta(fixture.state, fixture.mirror_path, fixture.root / "delta",
                                     base_path=base, schema=fixture.schema,
                                     normalizer=normalizer, environ=environ, **options)

        fixture.fake.set("public.app_purchase_order", "po-a", status="RECEIVED")
        fixture.capture(T2)
        first = cycle()
        self.assertEqual((first["layer"], first["postgres"]["applied_now"]), ("added", 1),
                         first)
        fixture.fake.set("public.app_purchase_order_line", "l1", purchase_order_id="po-b")
        fixture.capture(T2 + timedelta(hours=1))
        second = cycle()
        self.assertEqual((second["layer"], second["postgres"]["applied_now"]), ("added", 1),
                         second)
        self.assertEqual(second["followed"]["portal_api"], 1)   # only its own change
        self.assertEqual(cycle(compact_only=True)["layer"],
                         "none: Postgres holds every layer")
        full = fixture.full("gen3.sqlite")
        conn = sqlite3.connect(f"file:{full}?mode=ro", uri=True)
        self.addCleanup(conn.close)
        owned = set()
        for report in (first, second):
            with sqlite3.connect(report["store"]) as layer:
                owned |= {r[0] for r in layer.execute("SELECT object_id FROM delta_owned")}
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            for oid in sorted(owned):
                for part in [*_BY_OBJECT, "object_values"]:
                    self.assertEqual(_sqlite_rows(conn, part, oid), _pg_rows(cur, part, oid),
                                     f"{part}:{oid}")


    def test_a_new_generation_loads_beside_the_served_one_and_swaps_in_at_once(self):
        """``ontology_swap``: generation 2 loads into ``ontology_next`` while generation 1
        is served, then one swap; every row read afterwards is generation 2's."""
        from ontology.tests.test_delta import DeltaEquivalenceTests
        from ontology.tests.test_projection import T1, T2
        from operating.pg import ontology_load, ontology_swap
        from operating.pg.ontology_parity import parity
        from unittest import mock
        import psycopg

        fixture = DeltaEquivalenceTests("test_changed_records_equal_a_full_build_and_nothing_"
                                        "else_is_owned")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.capture(T1)
        first = fixture.full("gen1.sqlite")
        ontology_load.load(first, self.dsn, workers=1, replace=True, log=io.StringIO())
        fixture.fake.set("public.app_purchase_order", "po-a", status="RECEIVED")
        fixture.capture(T2)
        second = fixture.full("gen2.sqlite")
        self.assertTrue(ontology_swap.prepare_next(self.dsn))
        with mock.patch.object(ontology_load, "SCHEMA", ontology_swap.NEXT):
            ontology_load.load(second, self.dsn, workers=1, log=io.StringIO())
        self.assertFalse(ontology_swap.prepare_next(   # resumed, kept
            self.dsn, sqlite3.connect(second).execute(
                "SELECT value FROM store_meta WHERE key = 'content_digest'").fetchone()[0]))
        self.assertEqual(parity(str(first), self.dsn, sample=20)["differing"], [])
        ontology_swap.swap(self.dsn)
        self.assertEqual(parity(str(second), self.dsn, sample=40)["differing"], [])
        with psycopg.connect(self.dsn) as pg:
            self.assertIsNone(pg.execute("SELECT 1 FROM pg_namespace WHERE nspname = "
                                         "'ontology_next'").fetchone())
            self.assertGreater(pg.execute("SELECT count(*) FROM ontology.links").fetchone()[0], 0)
            self.assertGreater(pg.execute("SELECT ontology.sq_data_version()").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()


class LayerFreshnessTests(unittest.TestCase):
    """A layer's freshness reaches ``ontology.store_meta`` with its per-relation
    completeness: a family the layer read takes the layer's facts, or none, so a
    build-time "read in full" never vouches for a newer partial read."""

    def test_relation_completeness_follows_the_layer_per_family(self):
        import json
        from operating.pg import ontology_delta
        layer = sqlite3.connect(":memory:")
        layer.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
        partial = {"orders": {"completeness": "partial", "stale": False}}
        complete = {"orders": {"completeness": "complete", "stale": False}}
        layer.executemany("INSERT INTO store_meta VALUES (?, ?)", [
            ("delta_families", json.dumps(["bc", "mail"])),
            ("relation_completeness", json.dumps({"bc": partial}))])
        held = {"relation_completeness": json.dumps(
            {"bc": complete, "mail": complete, "portal_api": complete})}
        written = {}

        class Cursor:
            def execute(self, sql, params=()):
                if sql.startswith("INSERT"):
                    written[params[0]] = params[1]

            def fetchall(self):
                return list(held.items())

        ontology_delta._freshness(layer, Cursor())
        self.assertEqual(json.loads(written["relation_completeness"]),
                         {"bc": partial, "portal_api": complete})
