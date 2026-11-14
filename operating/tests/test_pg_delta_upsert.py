"""A layer's rows replace the served rows of the same key (``ontology_delta._copy``).

A migration layer carries content-keyed rows (events, links, identifiers, text) the live
delta follower may already have written to the served store; its copy must not fail on the
key, the layer's version wins, and the tables numbered from sequences stay plain copies.
Own throwaway database (``pg_testdb``); the ``plx`` database is never touched.
"""
from __future__ import annotations

import io
import sqlite3
import unittest

from operating.tests import pg_testdb
from operating.tests.test_pg_schema_migrate import MIGRATIONS


class LayerRowsReplaceServedRowsTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import psycopg
        from ontology.tests.test_delta import DeltaEquivalenceTests
        from ontology.tests.test_projection import T1
        from operating.pg import ontology_load
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            for migration in MIGRATIONS:
                pg.execute(migration.read_text(encoding="utf-8"))
        fixture = DeltaEquivalenceTests("test_changed_records_equal_a_full_build_and_nothing_"
                                        "else_is_owned")
        fixture.setUp()
        cls.addClassCleanup(fixture.doCleanups)
        fixture.capture(T1)
        cls.layer = str(fixture.full("layer.sqlite"))
        ontology_load.load(cls.layer, cls.dsn, workers=1, replace=True, log=io.StringIO())

    def count(self, cur, table):
        cur.execute(f"SELECT count(*) FROM ontology.{table}")
        return cur.fetchone()[0]

    def test_a_migration_copy_over_rows_the_served_store_holds_lets_the_layer_win(self):
        import json
        import psycopg
        from operating.pg import ontology_delta
        upsert = ("objects", "events", "event_objects", "identifiers", "object_removals",
                  "link_rows", "text_fields", "text_chunks", "chunk_words")
        sequenced = ("object_versions", "value_sources", "value_cells", "packed_sources")
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur, \
                sqlite3.connect(f"file:{self.layer}?mode=ro", uri=True) as conn:
            ids = json.dumps([r[0] for r in conn.execute("SELECT object_id FROM objects")])
            link_id, tier = conn.execute("SELECT link_id, tier FROM links LIMIT 1").fetchone()
            self.assertGreater(self.count(cur, "events"), 0)
            other = "tier-set-by-a-delta"
            self.assertNotEqual(tier, other)
            cur.execute("UPDATE ontology.link_rows SET tier = %s WHERE link_id = %s",
                        (other, link_id))
            cur.execute("SELECT object_id, row_no FROM ontology.objects ORDER BY 1")
            row_nos = cur.fetchall()
            before = {t: self.count(cur, t) for t in (*upsert, *sequenced)}
            ontology_delta._insert(conn, self.layer, cur, ids, every_link=True)
            after = {t: self.count(cur, t) for t in (*upsert, *sequenced)}
            self.assertEqual({t: after[t] for t in upsert}, {t: before[t] for t in upsert})
            cur.execute("SELECT tier FROM ontology.link_rows WHERE link_id = %s", (link_id,))
            self.assertEqual(cur.fetchone()[0], tier)   # the layer's version won
            cur.execute("SELECT object_id, row_no FROM ontology.objects ORDER BY 1")
            self.assertEqual(cur.fetchall(), row_nos)   # the served row numbers stay
            # numbered from sequences: copied beside the served rows, never replaced
            self.assertGreater(after["object_versions"], before["object_versions"])
            self.assertGreater(after["value_sources"], before["value_sources"])
            pg.rollback()

    def test_a_layer_of_more_than_one_chunk_of_sources_gives_the_rows_of_a_single_pass(self):
        import json
        from unittest import mock
        import psycopg
        from operating.pg import ontology_delta

        def run(chunk):
            with psycopg.connect(self.dsn) as pg, pg.cursor() as cur, \
                    sqlite3.connect(f"file:{self.layer}?mode=ro", uri=True) as conn:
                ids = json.dumps([r[0] for r in conn.execute("SELECT object_id FROM objects")])
                with mock.patch.object(ontology_delta, "_CHUNK", chunk):
                    report = ontology_delta._insert(conn, self.layer, cur, ids,
                                                    every_link=True)
                cur.execute(
                    "SELECT v.object_id, v.l1_handle, s.binding_id, f.property, f.field_path, "
                    "c.value, c.value_num, c.currency, c.currency_citation, "
                    "k.kind IS NOT NULL, (p.src IS NOT NULL) FROM ontology.value_cells c "
                    "JOIN ontology.value_sources s ON s.src = c.src JOIN "
                    "ontology.object_versions v ON v.ver = s.ver JOIN ontology.value_fields f "
                    "ON f.field = c.field JOIN ontology.value_kinds k ON k.kind = c.kind "
                    "LEFT JOIN ontology.packed_sources p ON p.src = c.src")
                cells = sorted(map(repr, cur.fetchall()))
                cur.execute("SELECT count(*) FROM ontology.packed_sources")
                packed = cur.fetchone()[0]
                pg.rollback()
            return report, cells, packed

        whole = run(10 ** 9)
        self.assertGreater(whole[0]["copied.value_sources"], 2)
        chunked = run(2)   # several chunks of sources
        self.assertGreater(whole[0]["copied.value_cells"], 0)
        self.assertEqual(chunked, whole)

    def test_duplicate_keys_in_one_batch_keep_the_last_row_and_other_tables_still_fail(self):
        import psycopg
        from operating.pg import ontology_delta
        columns = ("event_id", "object_id", "qualifier")
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            cur.execute("SELECT column_name FROM information_schema.columns WHERE "
                        "table_schema = 'ontology' AND table_name = 'event_objects' "
                        "ORDER BY ordinal_position")
            self.assertEqual([r[0] for r in cur.fetchall()][:3], list(columns))
            keys: dict = {}
            rows = [("dup-ev", "o1", "q")] * 3 + [("dup-ev", "o2", "q")]
            self.assertEqual(ontology_delta._copy(cur, "event_objects", columns, rows,
                                                  keys=keys), 4)
            self.assertEqual(ontology_delta._copy(cur, "event_objects", columns, rows,
                                                  keys=keys), 4)   # again: over its own rows
            self.assertEqual(keys, {"event_objects": ["event_id", "object_id", "qualifier"]})
            cur.execute("SELECT object_id FROM ontology.event_objects WHERE event_id = "
                        "'dup-ev' ORDER BY 1")
            self.assertEqual([r[0] for r in cur.fetchall()], ["o1", "o2"])
            pg.rollback()
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            cur.execute("SELECT * FROM ontology.link_rows LIMIT 1")
            names = [d.name for d in cur.description]
            row = dict(zip(names, cur.fetchone()))
            cols = tuple(n for n in names if n != "row_no")
            # the last of equal keys wins on the columns that are not the key
            first = tuple(row[c] for c in cols)
            second = tuple("last" if c == "tier" else row[c] for c in cols)
            ontology_delta._copy(cur, "link_rows", cols, [first, second])
            cur.execute("SELECT tier FROM ontology.link_rows WHERE link_id = %s",
                        (row["link_id"],))
            self.assertEqual(cur.fetchone()[0], "last")
            # a table keyed by a number from a sequence is not upserted: a repeat fails
            cur.execute("SELECT src FROM ontology.packed_sources LIMIT 1")
            src = cur.fetchone()
            if src is None:
                cur.execute("INSERT INTO ontology.packed_sources VALUES (-1)")
                src = (-1,)
            with self.assertRaises(psycopg.errors.UniqueViolation):
                ontology_delta._copy(cur, "packed_sources", ("src",), [src])
            pg.rollback()

    def test_a_link_id_names_one_row_when_its_type_changes_on_the_partitioned_table(self):
        """Partitioned by type (migration 0017) the key is (link_id, link_type); a layer's
        row of a served id under another type still replaces it, and of one batch's rows
        of one id the last wins, as the link id's own key did."""
        import psycopg
        from operating.pg import ontology_delta
        with psycopg.connect(self.dsn) as pg, pg.cursor() as cur:
            cur.execute("SELECT relkind FROM pg_class WHERE oid = 'ontology.link_rows'::regclass")
            self.assertEqual(cur.fetchone()[0], "p")
            cur.execute("SELECT * FROM ontology.link_rows LIMIT 1")
            names = [d.name for d in cur.description]
            row = dict(zip(names, cur.fetchone()))
            cols = tuple(n for n in names if n != "row_no")
            moved = tuple("Other.type" if c == "link_type" else row[c] for c in cols)
            ontology_delta._copy(cur, "link_rows", cols, [moved])
            cur.execute("SELECT link_type FROM ontology.link_rows WHERE link_id = %s",
                        (row["link_id"],))
            self.assertEqual(cur.fetchall(), [("Other.type",)])
            third = tuple("Third.type" if c == "link_type" else
                          ("last" if c == "tier" else row[c]) for c in cols)
            ontology_delta._copy(cur, "link_rows", cols, [moved, third])
            cur.execute("SELECT link_type, tier FROM ontology.link_rows WHERE link_id = %s",
                        (row["link_id"],))
            self.assertEqual(cur.fetchall(), [("Third.type", "last")])
            # rows of other ids are untouched
            cur.execute("SELECT count(*) FROM ontology.link_rows")
            total = cur.fetchone()[0]
            ontology_delta._copy(cur, "link_rows", cols, [third])
            cur.execute("SELECT count(*) FROM ontology.link_rows")
            self.assertEqual(cur.fetchone()[0], total)
            pg.rollback()
