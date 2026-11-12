"""A row-delta layer applied to the Postgres ontology (``operating.pg.ontology_delta``):
over a loaded generation, each object the layer owns then holds exactly a full build's
rows of the newer captures; a layer of another generation or applied before changes
nothing."""

from __future__ import annotations

import os
import gzip
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from operating.tests import pg_testdb

MIGRATIONS = [Path(__file__).resolve().parents[1] / "pg" / "migrations" / name
              for name in ("0006_ontology.sql", "0007_ontology_compact.sql",
                           "0008_ontology_read.sql", "0009_schema_migrations.sql",
                           "0010_ontology_words.sql", "0011_ontology_word_order.sql",
                           "0013_link_rows_step_reads.sql", "0014_ontology_autovacuum.sql")]


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
        # the owned objects' words are the full build's: a word of a replaced value is
        # gone, a new one found (every term either generation indexes, every value row)
        from operating.pg.ontology_reader import VALUE_WORD_JOIN
        from operating.pg.sqlite_compat import PgCompatConnection
        words = PgCompatConnection(self.dsn)
        self.addCleanup(words.close)
        words.words = True
        conn.execute("CREATE VIRTUAL TABLE temp.v_new USING fts5vocab(main, value_text, row)")
        terms = {r[0] for r in conn.execute("SELECT term FROM temp.v_new")}
        with sqlite3.connect(f"file:{base}?mode=ro", uri=True) as old:
            old.execute("CREATE VIRTUAL TABLE temp.v_old USING fts5vocab(main, value_text, row)")
            terms |= {r[0] for r in old.execute("SELECT term FROM temp.v_old")}
        sql = ("SELECT v.object_id, v.property, v.field_path, v.value FROM {} "
               "WHERE value_text MATCH :q AND v.object_id IN (SELECT value FROM json_each(:o))")
        mine = json.dumps(owned)
        found = 0
        for term in sorted(terms):
            query = {"q": f'"{term}"', "o": mine}
            expected = sorted(tuple(r) for r in conn.execute(sql.format(
                "value_text f JOIN object_values_numbered v ON v.row_no = f.rowid"), query))
            self.assertEqual(sorted(tuple(r) for r in words.execute(
                sql.format(VALUE_WORD_JOIN), query)), expected, term)
            found += bool(expected)
        self.assertGreater(found, 2)

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
        # Postgres the only store with its SQLite generation retired: the layers follow
        # over the generation Postgres holds, nothing is loaded; one not whole is refused
        served = state / "projection" / "ontology.sqlite"
        served.unlink()
        with mock.patch.dict(os.environ, {"PLX_ONTOLOGY_PG": "only",
                                          "PLX_ONTOLOGY_DEFER": ""}):
            out = ontology_delta.follow(state, self.dsn, log=io.StringIO())
            self.assertEqual((out["state"], out["held"], "loaded" in out),
                             ("current", digest, False), out)
            with mock.patch.object(ontology_delta, "apply_listed",
                                   side_effect=AssertionError("applied")), \
                    mock.patch("operating.pg.ontology_load.ORDER",
                               ("objects", "a_table_never_loaded")):
                out = ontology_delta.follow(state, self.dsn, log=io.StringIO())
            self.assertEqual(out["state"], "not_whole_and_no_generation")
        served.hardlink_to(base)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("UPDATE ontology.load_state SET value = jsonb_set(value, "
                       "'{content_digest}', '\"sha256:other\"') WHERE name = 'generation'")
        out = ontology_delta.follow(state, self.dsn, reload=False, log=io.StringIO())
        self.assertEqual(out["state"], "generation_differs")
        # a reload in place waits for requests begun on Postgres before the publish
        out = ontology_delta.follow(state, self.dsn, log=io.StringIO())
        self.assertEqual(out["state"], "waiting_for_readers")


    def assert_cover(self, kept, read, families):
        """``kept`` holds every fact a full read finds (a slice never narrower, so no
        absence is claimed that a full read would withhold)."""
        from operating import ontology_tools_traverse as tt
        # the layer mark a reader notes beside its facts (``_carried``) is not a fact
        self.assertEqual(set(kept) - {tt._LAYER_MARK}, set(read) - {tt._LAYER_MARK})
        self.assertLessEqual(set(read["audience_keys"]), set(kept["audience_keys"]))
        for name, keys in read["audiences"].items():
            self.assertLessEqual(set(keys), set(kept["audiences"][name]))
        for part, held in zip(read["event_types"], kept["event_types"]):
            for name, kinds in part.items():
                self.assertLessEqual(kinds, held[name])
        for kind in tt._SLICE_ROWS:
            for name, (marks, handles) in read[("slices", kind)].items():
                held_marks, held_handles = kept[("slices", kind)][name]
                for mark, until in marks.items():
                    self.assertGreaterEqual(held_marks[mark], until)
                if held_handles is not None:
                    self.assertIsNotNone(handles)
                    for handle, until in handles.items():
                        self.assertGreaterEqual(held_handles[handle], until)
        for family in families:
            span, held = read[("cell_span", family)], kept[("cell_span", family)]
            if span is not None:
                self.assertTrue(held[0] <= span[0] and span[1] <= held[1], family)

    def test_an_applied_layer_keeps_the_traverse_facts_of_the_version_it_made(self):
        """The follower keeps the facts of every data version (``prime_pg_facts`` after a
        load, ``fold_layer_facts`` per layer): a host opening after a layer loads them at
        once, never reading them on a request, and they cover every fact a full read
        finds (a slice never narrower, so no absence is claimed that a full read would
        withhold); a layer applied while something else changed keeps nothing."""
        from unittest import mock
        from ontology.tests.test_delta import DeltaEquivalenceTests
        from ontology.tests.test_projection import T1, T2
        from operating import ontology_tools as ot, ontology_tools_evidence as ev
        from operating import ontology_tools_traverse as tt
        from operating.pg import ontology_delta, ontology_load
        from operating.pg.ontology_reader import PgStoreReader
        import psycopg

        facts_dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, facts_dir, True)
        self.enterContext(mock.patch.dict(os.environ, {"PLX_PG_FACTS_DIR": facts_dir}))
        tt._LOADED.clear()
        self.addCleanup(tt._LOADED.clear)
        fixture = DeltaEquivalenceTests("test_changed_records_equal_a_full_build_and_nothing_"
                                        "else_is_owned")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.capture(T1)
        base = fixture.full("gen1.sqlite")
        ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())
        self.assertTrue(tt.prime_pg_facts(self.dsn))          # the follower, after a load
        fixture.fake.set("public.app_purchase_order", "po-a", status="RECEIVED")
        fixture.capture(T2)
        layer = fixture.cycle(base)["store"]
        applied = ontology_delta.apply_layer(layer, self.dsn)
        self.assertTrue(applied["applied"] and applied["facts_kept"], applied)

        def opened():
            reader = PgStoreReader.open(self.dsn, only=False)
            self.addCleanup(reader.close)
            return tt.TraverseTools(ot.OntologyTools(reader, ot.ReadPolicy("plx", {})))

        families = ev.STORE_DOCUMENT_FAMILIES
        tt._LOADED.clear()   # a later process
        later = opened()
        self.assertTrue(later.prime_lazily(families))         # loaded at once, no scan
        kept = dict(later.store.facts[1])
        full = opened()
        with full.store.lock:
            full._audience_keys(), full._audiences(), full._event_type_maps()
            for kind in tt._SLICE_ROWS:
                full._slices(kind)
            for family in families:
                full.cell_span(family)
        read = full.store.facts[1]
        self.assert_cover(kept, read, families)
        # something else changed after the version a fold reads from: nothing is folded
        with psycopg.connect(self.dsn) as pg:
            before = pg.execute("SELECT ontology.sq_data_version()").fetchone()[0]
            pg.execute("UPDATE ontology.load_state SET updated_at = now() "
                       "WHERE name = 'generation'")
        self.assertFalse(tt.fold_layer_facts(self.dsn, layer, [], before - 1,
                                             (f"delta:{Path(layer).name}", "delta_layers")))

    def test_a_host_reads_the_facts_whole_in_a_process_of_its_own(self):
        """A host over a version with no facts kept reads them off the request path in a
        process of its own (a whole read's rows never stay in a serving process): this
        process reads nothing whole, the facts are kept, and a later host loads them at
        once. A read that cannot run keeps nothing and says so."""
        from unittest import mock
        from ontology.tests.test_delta import DeltaEquivalenceTests
        from ontology.tests.test_projection import T1
        from operating import ontology_tools as ot, ontology_tools_evidence as ev
        from operating import ontology_tools_traverse as tt
        from operating.pg import ontology_load
        from operating.pg.ontology_reader import PgStoreReader

        facts_dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, facts_dir, True)
        self.enterContext(mock.patch.dict(os.environ, {"PLX_PG_FACTS_DIR": facts_dir}))
        tt._LOADED.clear()
        self.addCleanup(tt._LOADED.clear)
        fixture = DeltaEquivalenceTests("test_changed_records_equal_a_full_build_and_nothing_"
                                        "else_is_owned")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.capture(T1)
        ontology_load.load(fixture.full("gen1.sqlite"), self.dsn, workers=1, replace=True,
                           log=io.StringIO())

        def opened():
            reader = PgStoreReader.open(self.dsn, only=False)
            self.addCleanup(reader.close)
            return tt.TraverseTools(ot.OntologyTools(reader, ot.ReadPolicy("plx", {})))

        families = ev.STORE_DOCUMENT_FAMILIES
        tt._BACKGROUND.clear()
        host = opened()
        whole = AssertionError("read whole in the serving process")
        with mock.patch.object(tt, "_read_pg_facts_parallel", side_effect=whole):
            self.assertFalse(host.prime_lazily(families))       # read in the background
            self.assertTrue(host._pending[2].wait(300))
        tt._LOADED.clear()
        later = opened()
        self.assertTrue(later.prime_lazily(families))           # kept: loaded at once
        self.assertIn("audience_keys", later.store.facts[1])
        import psycopg
        gone = psycopg.conninfo.make_conninfo(self.dsn, dbname=f"{self.name}_no_such_db")
        self.assertFalse(tt._keep_pg_facts_apart(gone, families))

    def test_a_host_folds_the_layers_since_the_kept_version_instead_of_reading_it_whole(self):
        """A layer applied without its facts kept (a follower that did not fold): the
        host's background read folds the facts kept for the version before it over the
        layer file, never scanning the store, and they cover a full read; a load_state
        change that is no layer's, or a layer file no longer there, folds nothing."""
        from unittest import mock
        from ontology.tests.test_delta import DeltaEquivalenceTests
        from ontology.tests.test_projection import T1, T2
        from operating import ontology_tools as ot, ontology_tools_evidence as ev
        from operating import ontology_tools_traverse as tt
        from operating.pg import ontology_delta, ontology_load
        from operating.pg.ontology_reader import PgStoreReader
        import psycopg

        facts_dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, facts_dir, True)
        self.enterContext(mock.patch.dict(os.environ, {"PLX_PG_FACTS_DIR": facts_dir}))
        tt._LOADED.clear()
        self.addCleanup(tt._LOADED.clear)
        fixture = DeltaEquivalenceTests("test_changed_records_equal_a_full_build_and_nothing_"
                                        "else_is_owned")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.capture(T1)
        base = fixture.full("gen1.sqlite")
        ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())
        self.assertTrue(tt.prime_pg_facts(self.dsn))
        fixture.fake.set("public.app_purchase_order", "po-a", status="RECEIVED")
        fixture.capture(T2)
        layer = Path(fixture.cycle(base)["store"])
        with mock.patch.object(tt, "fold_layer_facts", return_value=False):
            applied = ontology_delta.apply_layer(layer, self.dsn)
        self.assertTrue(applied["applied"] and not applied["facts_kept"], applied)

        def opened():
            reader = PgStoreReader.open(self.dsn, only=False)
            self.addCleanup(reader.close)
            return tt.TraverseTools(ot.OntologyTools(reader, ot.ReadPolicy("plx", {})))

        families = ev.STORE_DOCUMENT_FAMILIES
        tt._LOADED.clear()
        self.assertFalse(opened()._kept_facts(*tt._store_identity(opened().store.conn),
                                              families))   # nothing kept for this version
        # Postgres's record of the layer's objects short and its file gone: nothing
        # folded (the version is read in full)
        with mock.patch.object(tt, "_recorded_owned", return_value=None):
            self.assertFalse(tt.catch_up_pg_facts(self.dsn, tempfile.mkdtemp()))
        # the layer's file gone (a host without the delta folder, Postgres alone): its
        # objects are read from Postgres's record of the apply, never the whole store
        host = opened()
        with mock.patch.object(tt, "_read_pg_facts_parallel",
                               side_effect=AssertionError("read the store whole")):
            host._pg_prime_reader(families, None)
        tt._LOADED.clear()
        later = opened()
        self.assertTrue(later.prime_lazily(families))   # loaded at once, no scan
        full = opened()
        with full.store.lock:
            full._audience_keys(), full._audiences(), full._event_type_maps()
            for kind in tt._SLICE_ROWS:
                full._slices(kind)
            for family in families:
                full.cell_span(family)
        self.assert_cover(dict(later.store.facts[1]), full.store.facts[1], families)
        # a change that is no layer's since the kept version: nothing folded
        marker, now = tt._store_identity(later.store.conn)
        tt._save_facts(marker, {**now, "data_version": str(int(now["data_version"]) - 1)},
                       later.store.facts[1])
        with psycopg.connect(self.dsn) as pg:
            pg.execute("UPDATE ontology.load_state SET updated_at = now() "
                       "WHERE name = 'generation'")
        self.assertFalse(tt.catch_up_pg_facts(self.dsn, layer.parent))

    def test_a_reader_carries_its_audiences_and_odd_links_over_a_layer_only_growing(self):
        """Live 2026-11-11: after every layer a page's first hop read the audiences per
        type and the odd slot links whole again (half a minute each under load). A reader
        holding them carries them over a layer, each with the layer's objects' own rows
        added, without a whole read, and they cover a full read of the new version; when
        the layer's odd links cannot be read by object they are read again, never
        guessed."""
        from ontology.tests.test_delta import DeltaEquivalenceTests
        from ontology.tests.test_projection import T1, T2
        from operating import ontology_tools as ot, ontology_tools_traverse as tt
        from operating.pg import ontology_delta, ontology_load
        from operating.pg.ontology_reader import PgStoreReader

        facts_dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, facts_dir, True)
        self.enterContext(mock.patch.dict(os.environ, {"PLX_PG_FACTS_DIR": facts_dir}))
        tt._LOADED.clear()
        self.addCleanup(tt._LOADED.clear)
        fixture = DeltaEquivalenceTests("test_changed_records_equal_a_full_build_and_nothing_"
                                        "else_is_owned")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.capture(T1)
        base = fixture.full("gen1.sqlite")
        ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())

        def opened():
            reader = PgStoreReader.open(self.dsn, only=False)
            self.addCleanup(reader.close)
            return tt.TraverseTools(ot.OntologyTools(reader, ot.ReadPolicy("plx", {})))

        holders = {"carried": opened(), "unreadable": opened()}
        for tools in holders.values():
            with tools.store.lock:
                held = tools._audiences()
                link_types = [r[0] for r in tools.store.conn.execute(
                    "SELECT DISTINCT link_type FROM links")]
                tools._slices_of("link", link_types)
                self.assertIn(tt._LINK_ODD, tools.store.facts[1])
        fixture.fake.set("public.app_purchase_order", "po-a", status="RECEIVED")
        fixture.capture(T2)
        layer = Path(fixture.cycle(base)["store"])
        with mock.patch.object(tt, "fold_layer_facts", return_value=False):
            applied = ontology_delta.apply_layer(layer, self.dsn)
        self.assertTrue(applied["applied"], applied)
        # the audience keys of the layer's objects taken out of what one reader holds
        # (as if the layer brought them): the carry adds them back from its own rows.
        # This layer's alone: the class database keeps earlier tests' layers.
        owned_keys = {tuple(r) for r in holders["carried"].store.conn.execute(
            "SELECT object_type, audience_key FROM objects WHERE object_id IN "
            "(SELECT object_id FROM ontology.applied_owned WHERE layer = :layer)",
            {"layer": applied["layer"]})}
        self.assertTrue(owned_keys)
        holders["carried"].store.facts[1]["audiences"] = {
            k: tuple(a for a in v if (k, a) not in owned_keys) for k, v in held.items()}

        full = opened()
        with full.store.lock:
            fresh_audiences, fresh_odd = full._audiences(), full.store.link_odd()
        whole = mock.Mock(side_effect=AssertionError("read whole again"))
        carried = holders["carried"]
        with carried.store.lock, mock.patch.object(carried.store, "link_odd", whole), \
                mock.patch.object(carried, "_grouped_slices", whole):
            facts = carried._current_facts("anything")
            self.assertIn("audiences", facts)
            for name, keys in fresh_audiences.items():
                self.assertLessEqual(set(keys), set(facts["audiences"][name]), name)
            self.assertLessEqual(set(fresh_odd), set(facts[tt._LINK_ODD]))
            self.assertEqual(carried._audiences(), facts["audiences"])
        # the layer's odd links unreadable by object (past the cap): not carried, so
        # the next hop reads them whole again
        other = holders["unreadable"]
        with other.store.lock, mock.patch.object(other.store, "link_odd_of",
                                                 return_value=None):
            facts = other._current_facts("anything")
            self.assertNotIn(tt._LINK_ODD, facts)
            self.assertIn("audiences", facts)

    def test_the_link_slice_read_natively_equals_the_composed_read_and_a_prime_is_loud(self):
        """Real ontology 2026-11-07: the link slice composed 25M packed evidence texts
        (minutes), and a prime over which a delta layer landed kept only its last facts
        and returned False in silence. The native read gives the composed read's slice on
        every evidence shape (plain, packed, a gated skeleton literal, an odd slot text,
        an object skeleton, a literal percent, a backslash, a surplus slot); a prime over
        a moving data version keeps nothing and says why, and reads again when allowed."""
        from ontology.tests.test_delta import DeltaEquivalenceTests
        from ontology.tests.test_projection import T1
        from operating import ontology_tools as ot, ontology_tools_traverse as tt
        from operating.pg import ontology_load
        from operating.pg.ontology_reader import PgStoreReader
        import psycopg

        facts_dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, facts_dir, True)
        self.enterContext(mock.patch.dict(os.environ, {"PLX_PG_FACTS_DIR": facts_dir}))
        tt._LOADED.clear()
        self.addCleanup(tt._LOADED.clear)
        fixture = DeltaEquivalenceTests("test_changed_records_equal_a_full_build_and_nothing_"
                                        "else_is_owned")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.capture(T1)
        base = fixture.full("gen1.sqlite")
        ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())
        texts = {9001: "graph-mail://m/1", 9002: "api://public.x/1", 9003: 'graph-mail://q"x',
                 9004: "sharepoint://s/2", 9005: "teams-chat://c/3"}
        skeletons = {9101: '["%s","%s"]', 9102: '["sharepoint://lit/9","%s"]',
                     9103: '{"a":"%s"}', 9104: '["100%% api://p","%s"]',
                     9105: '["x\\u0041","%s"]', 9106: '["%s"]'}
        rows = [  # (id, type, evidence_id, ek, slots, evidence_handles, superseded_at)
            ("L1", "A.b", 9101, 2, [9001, 9002], None, None),
            ("L2", "A.b", 9102, 1, [9004], None, "2026-01-02T00:00:00Z"),
            ("L3", "A.b", 9101, 2, [9003, 9004], None, None),   # an odd slot text
            ("L4", "C.d", 9103, 1, [9005], None, None),         # not an array
            ("L5", "C.d", 9104, 1, [9001], None, "2025-05-05"),
            ("L6", "C.d", 9105, 1, [9004], None, None),
            ("L7", "C.d", 9106, 2, [9002, 9005], None, None),   # a surplus slot
            ("L8", "E.f", None, 0, [], '["graph-mail://p/7"]', "not a time"),
            ("L9", "E.f", 9101, 3, [9004, 9005, 9001], None, None),  # the reason's slot
        ]
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            for tid, text in texts.items():
                pg.execute("INSERT INTO ontology.link_texts VALUES (%s, %s)", (tid, text))
            for kid, text in skeletons.items():
                pg.execute("INSERT INTO ontology.link_skeletons VALUES (%s, %s)", (kid, text))
            for lid, ltype, evidence, ek, slots, plain, until in rows:
                pg.execute(
                    "INSERT INTO ontology.link_rows (link_id, link_type, from_object, "
                    "to_object, tier, binding_kind, why_not, markings, recorded_at, reason, "
                    "evidence_id, ek, slots, evidence_handles, superseded_at) VALUES "
                    "(%s, %s, 'a', 'b', 'T1', 'key', '', %s, '2026-01-01', 'r', %s, %s, %s, "
                    "%s, %s)", (lid, ltype, f'["m{lid[-1]}"]', evidence,
                                ek if lid != "L9" else 2, slots, plain, until))

        def slice_of(native):
            reader = PgStoreReader.open(self.dsn, only=False)
            self.addCleanup(reader.close)
            if not native:
                reader.slice_rows = lambda kind, schemes: None
            else:
                self.assertIsNotNone(reader.slice_rows("link", sorted(tt.GATED_SCHEMES)))
            tools = tt.TraverseTools(ot.OntologyTools(reader, ot.ReadPolicy("plx", {})))
            with reader.lock:
                return tools._slices("link")

        composed = slice_of(False)
        self.assertEqual(slice_of(True), composed)
        # read side by side over one snapshot, each kind cut into key ranges: the same
        # facts as one reader reading them one after another
        whole = PgStoreReader.open(self.dsn, only=False, text_store=base)
        self.addCleanup(whole.close)
        serial = tt.TraverseTools(ot.OntologyTools(whole, ot.ReadPolicy("plx", {})))
        families = ("graph-mail", "portal")
        with whole.lock:
            expected = {key: tt._fact_value(serial, key) for key in tt._fact_keys(families)}
        cuts = {"link": [None, "L3", "L7", None], "object": [None, "N", "S", None],
                "event": [None, "e", None]}
        with mock.patch.object(tt, "_pg_key_bounds",
                               side_effect=lambda cur, kind, pieces: cuts[kind]):
            identity, parallel = tt._read_pg_facts_parallel(
                self.dsn, families, text_store=base, workers=3)
        self.assertEqual(set(parallel), set(expected))
        for key, value in expected.items():
            self.assertEqual(parallel[key], value, key)
        self.assertEqual(parallel[("slices", "link")], composed)
        # a layer's objects' links read by index (from or to them) give the composed
        # read's slice of those links
        # and one type's links by its index give the composed read's slice of that type
        with whole.lock:
            by_index = serial._read_slice_rows("link", ["a", "zz-none"])
            typed = {name: serial._read_slices("link", name) for name in composed}
            whole.slice_rows = lambda kind, schemes, **kw: None
            by_view = serial._read_slice_rows("link", ["a", "zz-none"])
        self.assertEqual(by_index, by_view)
        for name, value in composed.items():
            self.assertEqual(typed[name], {name: value}, name)
        self.assertEqual(set(by_index), {"A.b", "C.d", "E.f"})   # the inserted links
        # each type read alone (a page's hops, ``_slices_of``) scans for the odd slot
        # texts once per store state, not once per type; too many odd links (None):
        # every type is read the composed way, with the same facts
        for odd_result in ("real", None):
            per_type = PgStoreReader.open(self.dsn, only=False, text_store=base)
            self.addCleanup(per_type.close)
            real_odd, scans = per_type.link_odd, []
            per_type.link_odd = lambda: scans.append(1) or (
                real_odd() if odd_result == "real" else None)
            hops = tt.TraverseTools(ot.OntologyTools(per_type, ot.ReadPolicy("plx", {})))
            with per_type.lock:
                got = {name: hops._slices_of("link", [name]) for name in composed}
                got.update({name: hops._slices_of("link", [name]) for name in composed})
            for name, value in composed.items():
                self.assertEqual(got[name], {name: value}, (odd_result, name))
            self.assertEqual(len(scans), 1, odd_result)
        self.assertEqual(set(composed["A.b"][1]), {"graph-mail://m/1", "sharepoint://s/2",
                                                   "sharepoint://lit/9"})
        # an object skeleton and a surplus slot give nothing; an escaped literal (read
        # as before) gives its slot
        self.assertEqual(set(composed["C.d"][1]), {"graph-mail://m/1", "sharepoint://s/2"})

        # a writer moves the data version while the facts are read: they are read over
        # one snapshot and kept, in one attempt, under the version they were read at
        # (never under the version the writer made); that one is then read anew
        read = tt._fact_value
        moved = []

        def moving(tools, key):
            if not moved:
                moved.append(key)
                with psycopg.connect(self.dsn, autocommit=True) as pg:
                    pg.execute("UPDATE ontology.load_state SET updated_at = now() + "
                               "interval '1 second' WHERE name = 'generation'")
            return read(tools, key)

        with psycopg.connect(self.dsn) as pg:
            before = pg.execute("SELECT ontology.sq_data_version()").fetchone()[0]
        with mock.patch.object(tt, "_fact_value", moving):
            self.assertTrue(tt.prime_pg_facts(self.dsn, text_store=base, attempts=1))
        self.assertTrue(moved)
        marker = [str(Path(facts_dir) / p) for p in os.listdir(facts_dir)
                  if p.startswith("pg-") and not p.endswith(".lock")][0]
        self.assertEqual(tt._kept_identity(marker)["data_version"], str(before))
        with psycopg.connect(self.dsn) as pg:
            self.assertGreater(pg.execute("SELECT ontology.sq_data_version()").fetchone()[0],
                               before)
        self.assertFalse(tt.catch_up_pg_facts(self.dsn, facts_dir))   # no layer's change
        self.assertTrue(tt.prime_pg_facts(self.dsn, text_store=base, attempts=1))
        tt._LOADED.clear()
        later = PgStoreReader.open(self.dsn, only=False, text_store=base)
        self.addCleanup(later.close)
        tools = tt.TraverseTools(ot.OntologyTools(later, ot.ReadPolicy("plx", {})))
        self.assertTrue(tools.prime_lazily(__import__(
            "operating.ontology_tools_evidence", fromlist=["x"]).STORE_DOCUMENT_FAMILIES))

    def test_over_a_postgres_base_each_cycle_brings_only_its_changes_and_nothing_folds(self):
        """``PLX_DELTA_BASE_PG``: the closure reads Postgres, each cycle's layer is applied
        in the cycle, the next cycle projects only its own changes over it, and the fold
        has nothing to do; every owned object then holds a full build's rows. With
        Postgres the only store (``PLX_ONTOLOGY_PG=only``) a cycle does so with no SQLite
        generation on disk."""
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

        def retired(**options):
            # Postgres the only store: the SQLite generation is gone (compressed cold) and
            # the cycle projects against Postgres with no switch of its own
            cold = base.with_name(base.name + ".cold")
            base.rename(cold)
            try:
                return delta.build_delta(
                    fixture.state, fixture.mirror_path, fixture.root / "delta",
                    base_path=base, schema=fixture.schema, normalizer=normalizer,
                    environ={"PLX_ONTOLOGY_PG": "only", "PLX_DELTA_BASE_PG": "0",
                             "PLX_PG_DSN": self.dsn}, **options)
            finally:
                cold.rename(base)

        fixture.fake.set("public.app_purchase_order", "po-a", status="RECEIVED")
        fixture.capture(T2)
        first = cycle()
        self.assertEqual((first["layer"], first["postgres"]["applied_now"]), ("added", 1),
                         first)
        fixture.fake.set("public.app_purchase_order_line", "l1", purchase_order_id="po-b")
        fixture.capture(T2 + timedelta(hours=1))
        second = retired()
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


    def test_a_cycle_that_fails_after_its_closure_resumes_from_it_while_it_holds(self):
        """Over the Postgres base a cycle keeps its closure and mention listing until it
        publishes: one that failed later (11-11: the disk filled at projection, 17 min of
        closure lost) resumes from them, with the same result as a fresh cycle; a record
        changed again since is the only one closed again."""
        from datetime import timedelta
        from unittest import mock
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
        checkpoint = fixture.root / "delta" / delta.CHECKPOINT

        def cycle(**options):
            return delta.build_delta(fixture.state, fixture.mirror_path, fixture.root / "delta",
                                     base_path=base, schema=fixture.schema,
                                     normalizer=normalizer, environ=environ, **options)

        full_disk = sqlite3.OperationalError("database or disk is full")
        closure = delta.closure
        fixture.fake.set("public.app_purchase_order", "po-a", status="RECEIVED")
        fixture.fake.set("public.app_purchase_order", "po-b", status="CANCELLED")
        fixture.capture(T2)
        with mock.patch.object(delta, "project_layer", side_effect=full_disk):
            with self.assertRaises(sqlite3.OperationalError):
                cycle()
        self.assertTrue(checkpoint.is_file())
        # failed again after the checkpoint: still held, the closure is never read again
        with mock.patch.object(delta, "closure", side_effect=AssertionError("re-read")), \
                mock.patch.object(delta, "project_layer", side_effect=full_disk):
            with self.assertRaises(sqlite3.OperationalError):
                cycle()
        held = json.loads(gzip.open(checkpoint, "rt").read())
        self.assertGreater(sum(map(len, held["read"].values())), 1, held["read"])
        # a record changed again since: only it is closed again, the rest is resumed
        fixture.fake.set("public.app_purchase_order", "po-a", status="CLOSED")
        fixture.capture(T2 + timedelta(hours=1))
        reads = []
        with mock.patch.object(delta, "closure",
                               side_effect=lambda p, b, c, part, *a, **k: reads.append(
                                   {s: list(v) for s, v in part.items() if v})
                               or closure(p, b, c, part, *a, **k)), \
                mock.patch.object(delta, "project_layer", side_effect=full_disk):
            with self.assertRaises(sqlite3.OperationalError):
                cycle()
        self.assertEqual([sorted(r) for r in reads[:1]], [["portal_api"]], reads)
        self.assertEqual(len(reads[0]["portal_api"]), 1, reads)
        self.assertIn("po-a", json.dumps(reads[0]), reads)
        with mock.patch.object(delta, "closure", side_effect=AssertionError("re-read")):
            resumed = cycle()
        self.assertEqual((resumed["resumed"], resumed["layer"]), ("mentions", "added"), resumed)
        self.assertEqual(resumed["postgres"]["applied_now"], 1, resumed)
        self.assertFalse(checkpoint.exists())
        again = cycle()   # its records closed: nothing left open
        self.assertEqual(again["layer"], "none: nothing new", again)
        full = fixture.full("gen2.sqlite")
        conn = sqlite3.connect(f"file:{full}?mode=ro", uri=True)
        self.addCleanup(conn.close)
        with sqlite3.connect(resumed["store"]) as layer:
            owned = {r[0] for r in layer.execute("SELECT object_id FROM delta_owned")}
        self.assertTrue(owned)
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            for oid in sorted(owned):
                for part in [*_BY_OBJECT, "object_values"]:
                    self.assertEqual(_sqlite_rows(conn, part, oid), _pg_rows(cur, part, oid),
                                     f"{part}:{oid}")

    def test_an_unreadable_or_foreign_checkpoint_is_removed_and_never_resumed(self):
        """A checkpoint that is torn, of another key or format, or names a record closed
        since is removed: the cycle reads its closure again."""
        from ontology import delta
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = delta.State(root / "state.sqlite")
            state.add("portal_api", [("r", "1")], {("r", "1"): "a"})
            read = {"portal_api": state.opened("portal_api", [("r", "1")])}
            path = root / delta.CHECKPOINT

            def save(key="k", stage="closure"):
                delta._save_checkpoint(root, key, stage, {"portal_api": [("r", "1")]},
                                       {"portal_api": {("r", "1")}}, read, {"closure": {}})
                return delta._resume(root, "k", state) if path.is_file() else "gone"
            self.assertEqual(save()["found"]["portal_api"], {("r", "1")})
            self.assertIsNone(save(key="other"))
            self.assertFalse(path.exists())
            self.assertIsNone(save(stage="projected"))
            path.write_bytes(b"\x1f\x8b torn")
            self.assertIsNone(delta._resume(root, "k", state))
            self.assertFalse(path.exists())
            save()
            state.close_over({}, {"portal_api": [("r", "1")]}, read)   # published since
            self.assertIsNone(delta._resume(root, "k", state))
            self.assertFalse(path.exists())

    def test_a_record_changed_again_during_a_cycle_stays_open_for_the_next(self):
        """A cycle closes its records as it read them: one changed again while it ran
        (reopened under a newer mark) stays open, so the next cycle follows the change."""
        from ontology import delta
        with tempfile.TemporaryDirectory() as tmp:
            state = delta.State(Path(tmp) / "state.sqlite")
            state.add("portal_api", [("r", "1"), ("r", "2")], {("r", "1"): "a", ("r", "2"): "a"})
            read = {"portal_api": state.opened("portal_api", [("r", "1"), ("r", "2")])}
            state.add("portal_api", [("r", "2")], {("r", "2"): "b"})   # changed meanwhile
            state.close_over({}, {"portal_api": [("r", "1"), ("r", "2")]}, read)
            self.assertEqual(state.changed("portal_api", open_only=True), [("r", "2")])

    def test_a_postgres_base_never_builds_over_a_layer_it_lacks_and_a_trim_is_said(self):
        """Over the Postgres base: a published layer Postgres has not applied is applied
        before the closure reads it, and while it cannot be no layer is built (the changes
        stay open); a pointer that dropped a layer says ``partial``; back on the SQLite
        base the next layer is cumulative again (whole)."""
        from datetime import timedelta
        from unittest import mock
        from ontology import delta
        from ontology.tests.test_delta import DeltaEquivalenceTests, normalizer
        from ontology.tests.test_projection import T1, T2
        from operating.pg import ontology_delta, ontology_load
        import psycopg

        fixture = DeltaEquivalenceTests("test_changed_records_equal_a_full_build_and_nothing_"
                                        "else_is_owned")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.capture(T1)
        base = fixture.full("gen1.sqlite")
        ontology_load.load(base, self.dsn, workers=1, replace=True, log=io.StringIO())
        environ = {"PLX_DELTA_BASE_PG": "1", "PLX_PG_DSN": self.dsn}
        pointer = fixture.root / "delta" / delta.POINTER

        def cycle(env=environ, **options):
            return delta.build_delta(fixture.state, fixture.mirror_path, fixture.root / "delta",
                                     base_path=base, schema=fixture.schema,
                                     normalizer=normalizer, environ=env, **options)

        def published():
            return json.loads(pointer.read_text(encoding="utf-8"))

        refused = {"applied": False, "refused": ["generation_loading"], "layers": 1}
        fixture.fake.set("public.app_purchase_order", "po-a", status="RECEIVED")
        fixture.capture(T2)
        with mock.patch.object(ontology_delta, "apply_listed", return_value=refused):
            first = cycle()   # published; its apply refused
        self.assertEqual(first["layer"], "added", first)
        fixture.fake.set("public.app_purchase_order_line", "l1", purchase_order_id="po-b")
        fixture.capture(T2 + timedelta(hours=1))
        with mock.patch.object(ontology_delta, "apply_listed", return_value=refused):
            waiting = cycle()
        self.assertEqual((waiting["layer"], waiting["status"]),
                         ("none: Postgres behind the published layers", "postgres_behind"))
        self.assertEqual(waiting["postgres_behind"], [Path(first["store"]).name])
        self.assertEqual(published()["store"], first["store"])   # nothing published
        caught_up = cycle()   # the missing layer applied first, then this cycle's own
        self.assertEqual(caught_up["layer"], "added", caught_up)
        self.assertEqual(caught_up["postgres_before"]["applied_now"], 1)
        self.assertEqual(caught_up["followed"]["portal_api"], 1)   # the open change kept
        with psycopg.connect(self.dsn) as pg:
            applied = {r[0] for r in pg.execute(
                "SELECT substr(name, 7) FROM ontology.load_state WHERE name LIKE 'delta:%'")}
        self.assertTrue({Path(first["store"]).name, Path(caught_up["store"]).name} <= applied)
        self.assertEqual((published()["base"], published()["partial"]), ("postgres", False))
        # one layer kept past the ones Postgres applied: the oldest leaves, the pointer
        # says it no longer holds every change
        fixture.fake.set("public.app_purchase_order", "po-a", status="CLOSED")
        fixture.capture(T2 + timedelta(hours=2))
        with mock.patch.object(delta, "PG_KEPT_LAYERS", 1):
            trimmed = cycle()
        body = published()
        self.assertEqual([Path(s).name for s in [item["store"] for item in body["layers"]]],
                         [Path(trimmed["store"]).name, Path(caught_up["store"]).name])
        self.assertTrue(body["partial"])
        # back on the SQLite base (the rollback): the next layer holds every change again
        fixture.fake.set("public.app_purchase_order_line", "l1", purchase_order_id="po-a")
        fixture.capture(T2 + timedelta(hours=3))
        back = cycle(env={"PLX_PG_DSN": self.dsn})
        self.assertEqual(back["layer"], "compacted", back)
        self.assertEqual((published()["base"], published()["partial"]), ("sqlite", False))


    def test_link_degree_decides_the_backlink_cap_alike_on_both_bases_in_one_read(self):
        """``Base.degree``: whether an object gaining a link holds more than the cap is
        decided as an exact count would, on the SQLite generation and on Postgres, counted
        no further than the cap + 1 per end and in one read per 500 objects (11-11: a
        per-object full count took 6-9 s on a 160k-link hub)."""
        from ontology import delta
        from ontology.tests.test_delta import DeltaEquivalenceTests
        from ontology.tests.test_projection import T1
        from operating.pg import ontology_load

        fixture = DeltaEquivalenceTests("test_changed_records_equal_a_full_build_and_nothing_"
                                        "else_is_owned")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.capture(T1)
        store = fixture.full("gen1.sqlite")
        ontology_load.load(store, self.dsn, workers=1, replace=True, log=io.StringIO())
        with sqlite3.connect(store) as conn:
            exact = dict(conn.execute(
                "SELECT o, COUNT(*) FROM (SELECT from_object AS o FROM link_rows UNION ALL "
                "SELECT to_object FROM link_rows) GROUP BY o"))
            ids = [r[0] for r in conn.execute("SELECT object_id FROM objects")]
        ids.append("no-such-object")
        top = max(exact.values())
        self.assertGreaterEqual(top, 3)   # the fixture holds an object over a small cap
        bases = [delta.Base(store), delta.Base(store, postgres=self.dsn)]
        for base in bases:
            self.addCleanup(base.close)

        class Counted:
            def __init__(self, conn):
                self.conn, self.reads = conn, 0

            def execute(self, *args):
                self.reads += 1
                return self.conn.execute(*args)

        for base in bases:
            for cap in (0, 1, top - 1, top):
                real, base.conn = base.conn, Counted(base.conn)
                try:
                    got = base.degree(ids * 2, cap)   # duplicates name one object once
                    reads = base.conn.reads
                finally:
                    base.conn = real
                self.assertEqual(reads, 1, (base.postgres, cap))
                self.assertEqual(set(got), set(ids))
                for oid in ids:
                    held = exact.get(oid, 0)
                    self.assertEqual(got[oid] <= cap, held <= cap, (base.postgres, cap, oid))
                    if held <= cap:
                        self.assertEqual(got[oid], held, (base.postgres, cap, oid))

    def test_identifier_holders_are_read_per_kind_alike_on_both_bases(self):
        """``Base.holders``: the objects holding each (kind, value), of one binding or any,
        a value held by more than the cap counted and not followed, as one lookup per value
        did; read once per kind and 500 values, on SQLite and on Postgres."""
        from ontology import delta
        from ontology.tests.test_delta import DeltaEquivalenceTests
        from ontology.tests.test_projection import T1
        from operating.pg import ontology_load

        fixture = DeltaEquivalenceTests("test_changed_records_equal_a_full_build_and_nothing_"
                                        "else_is_owned")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.capture(T1)
        store = fixture.full("gen1.sqlite")
        ontology_load.load(store, self.dsn, workers=1, replace=True, log=io.StringIO())
        with sqlite3.connect(store) as conn:
            held: dict[tuple[str, str], set[tuple[str, str]]] = {}
            for kind, value, oid, binding in conn.execute(
                    "SELECT i.identifier_kind, i.normalized, i.object_id, o.binding_id FROM "
                    "identifiers i JOIN objects o ON o.object_id = i.object_id"):
                held.setdefault((kind, value), set()).add((oid, binding))
        keys = sorted(held) + [(sorted(held)[0][0], "no-such-value")]
        bindings = sorted({b for pairs in held.values() for _, b in pairs})
        top = max(len(v) for v in held.values())
        self.assertGreaterEqual(top, 2)   # a value over a small cap
        kinds = len({k for k, _ in keys})

        class Counted:
            def __init__(self, conn):
                self.conn, self.reads = conn, 0

            def execute(self, *args):
                self.reads += 1
                return self.conn.execute(*args)

        bases = [delta.Base(store), delta.Base(store, postgres=self.dsn)]
        for base in bases:
            self.addCleanup(base.close)
        for cap in (1, top - 1, top):
            for binding in (None, bindings[0]):
                want, over = set(), 0
                for key in keys:
                    oids = {o for o, b in held.get(key, ()) if binding in (None, b)}
                    if len(oids) > cap:
                        over += 1
                    else:
                        want |= oids
                for base in bases:
                    real, base.conn = base.conn, Counted(base.conn)
                    try:
                        with mock.patch.object(delta, "LOOKUP_CAP", cap):
                            got = base.holders(keys + keys[:3], binding)
                        reads = base.conn.reads
                    finally:
                        base.conn = real
                    self.assertEqual(got, (want, over), (base.postgres, cap, binding))
                    self.assertEqual(reads, kinds, (base.postgres, cap, binding))

    def test_lineage_links_of_the_asked_types_are_the_links_rows_of_those_types(self):
        """``Base.typed_links`` (the closure's lineage chains): the links of the asked types
        at either end of the objects, every one ``links_of`` returns of those types and no
        other, in one read per 500 objects, on SQLite and on Postgres."""
        from collections import Counter
        from ontology import delta
        from ontology.tests.test_delta import DeltaEquivalenceTests
        from ontology.tests.test_projection import T1
        from operating.pg import ontology_load

        fixture = DeltaEquivalenceTests("test_changed_records_equal_a_full_build_and_nothing_"
                                        "else_is_owned")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.capture(T1)
        store = fixture.full("gen1.sqlite")
        ontology_load.load(store, self.dsn, workers=1, replace=True, log=io.StringIO())
        with sqlite3.connect(store) as conn:
            ids = sorted(r[0] for r in conn.execute("SELECT object_id FROM objects"))
            types = sorted({r[0] for r in conn.execute("SELECT link_type FROM link_rows")})
        self.assertGreaterEqual(len(types), 2)
        bases = [delta.Base(store), delta.Base(store, postgres=self.dsn)]
        for base in bases:
            self.addCleanup(base.close)
        for base in bases:
            every = Counter((r["link_type"], r["from_object"], r["to_object"])
                            for r in base.links_of(ids))
            for asked in ([types[0]], types[1:], types, ["no-such-type"]):
                want = Counter({k: n for k, n in every.items() if k[0] in asked})
                reads = []
                real = base.conn

                class Counted:
                    def execute(self, *args):
                        reads.append(args[0])
                        return real.execute(*args)

                base.conn = Counted()
                try:
                    got = Counter((r["link_type"], r["from_object"], r["to_object"])
                                  for r in base.typed_links(ids, asked))
                finally:
                    base.conn = real
                self.assertEqual(got, want, (base.postgres, asked))
                self.assertEqual(len(reads), 1, (base.postgres, asked))

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
            # the steps' per-object reads keep their partial indexes (migration 0013)
            held = {r[0] for r in pg.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'ontology' "
                "AND tablename = 'link_rows'")}
            self.assertLessEqual({"link_rows_lead_from", "link_rows_contra_from"}, held)
            # and the big tables their autovacuum options (migration 0014)
            options = dict(pg.execute(
                "SELECT relname, reloptions FROM pg_class WHERE relnamespace = "
                "'ontology'::regnamespace AND relname IN ('link_rows', 'value_cells')"))
            for table in ("link_rows", "value_cells"):
                self.assertIn("autovacuum_vacuum_insert_scale_factor=0.01",
                              options[table] or [], table)


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
