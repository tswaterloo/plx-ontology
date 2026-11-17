"""The delta's Postgres lookups on several connections read what one connection reads.

``ontology.delta.Base`` over Postgres runs its chunked lookups (the closure's links,
identifiers, holders, handles, lineage, the mention reads' holders) ``workers`` statements
at a time (``PLX_DELTA_LOOKUP_WORKERS``). Checked at the boundary: every lookup answers
the same over 1, 10 and 60 chunks with 1 and 4 connections, and a whole cycle over a
Postgres base makes the same layer (rows, digest, closure, owned set) either way.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

from operating.tests import pg_testdb

def _migrate(dsn: str) -> None:
    import psycopg
    from operating.tests.test_pg_ontology_delta import MIGRATIONS
    with psycopg.connect(dsn, autocommit=True) as pg:
        for migration in MIGRATIONS:
            pg.execute(migration.read_text(encoding="utf-8"))


def _fixture():
    from ontology.tests.test_delta import DeltaEquivalenceTests
    fixture = DeltaEquivalenceTests("test_changed_records_equal_a_full_build_and_nothing_"
                                    "else_is_owned")
    fixture.setUp()
    return fixture


def _rows(path: Path) -> dict[str, list[tuple]]:
    """Every row of every content table of a store, sorted (``store_meta`` without the
    build's own time; the word indexes are virtual tables a plain read cannot scan)."""
    out: dict[str, list[tuple]] = {}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE "
            "'sqlite_%' AND COALESCE(sql, '') NOT LIKE 'CREATE VIRTUAL TABLE%' AND name "
            "NOT LIKE '%\\_data' ESCAPE '\\' AND name NOT LIKE "
            "'%\\_idx' ESCAPE '\\' AND name NOT LIKE '%\\_config' ESCAPE '\\' AND name NOT "
            "LIKE '%\\_docsize' ESCAPE '\\' AND name NOT LIKE '%\\_content' ESCAPE '\\'")]
        for table in sorted(tables):
            rows = [tuple(r) for r in conn.execute(f'SELECT * FROM "{table}"')]
            if table == "store_meta":
                rows = [r for r in rows if r[0] not in ("delta_built_at", "built_at")]
            out[table] = sorted(rows, key=repr)
    return out


class ParallelLookupTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from ontology.tests.test_projection import T1
        from operating.pg import ontology_load
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)
        _migrate(cls.dsn)
        cls.fixture = _fixture()
        cls.addClassCleanup(cls.fixture.doCleanups)
        cls.fixture.capture(T1)
        cls.base = cls.fixture.full("gen1.sqlite")
        ontology_load.load(cls.base, cls.dsn, workers=1, replace=True, log=io.StringIO())

    def _base(self, workers: int):
        from ontology import delta
        base = delta.Base(self.base, postgres=self.dsn, workers=workers)
        self.addCleanup(base.close)
        return base

    def _answers(self, base) -> dict[str, object]:
        objects = [r[0] for r in base.conn.execute("SELECT object_id FROM objects")]
        objects = sorted(objects) + ["not:an:object"]
        keys = base.identifiers_of(objects)
        types = {r[0] for r in base.conn.execute("SELECT DISTINCT link_type FROM link_rows")}
        handles = base.handles_of(objects)
        from ontology.delta import record_of_handle
        records = sorted({found[1] for found in map(record_of_handle, handles.values())
                          if found is not None and found[0] == "portal_api"}, key=repr)
        return {
            # the order rows come back within one statement is the server's: compared as a
            # multiset (every caller folds them into sets)
            "links_of": sorted(tuple(r) for r in base.links_of(objects)),
            "typed_links": sorted(tuple(r) for r in base.typed_links(objects, types)),
            "handles_of": handles,
            "exists": base.exists(objects),
            "identifiers_of": keys,
            "holders": base.holders(sorted(keys)),
            "degree": base.degree(objects, 2),
            "objects_of": base.objects_of("portal_api", records),
            "records": len(records),
        }

    def test_every_lookup_answers_the_same_on_one_or_four_connections_n_1_10_60(self):
        from ontology import delta
        serial = self._base(1)
        objects = sum(1 for _ in serial.conn.execute("SELECT object_id FROM objects")) + 1
        self.assertGreater(objects, 10)
        chunks = delta._chunks
        for parts in (1, 10, 60):
            size = max(1, -(-objects // parts))   # about ``parts`` statements per lookup
            with self.subTest(parts=parts), mock.patch.object(
                    delta, "_chunks", lambda items, size_=size: chunks(items, size_)):
                want = self._answers(serial)
                parallel = self._base(4)
                got = self._answers(parallel)
                self.assertEqual(got, want)
                self.assertTrue(want["links_of"] and want["handles_of"] and want["exists"]
                                and want["objects_of"] and want["identifiers_of"])
                if parts > 1:   # the statements did run on the lookup threads
                    self.assertIsNotNone(parallel._pool)
                    self.assertGreater(len(parallel._extra), 1)

    def test_parts_come_back_in_the_order_asked_whatever_finishes_first(self):
        base = self._base(4)
        # later parts finish first: each sleeps less than the one before
        params = [((60 - n) / 2000.0, json.dumps([n])) for n in range(60)]
        got = [[row[0] for row in rows] for rows in base._parts(
            "SELECT value, pg_sleep(?) FROM json_each(?)", params)]
        # ``json_each`` values come back as text over Postgres
        self.assertEqual(got, [[str(n)] for n in range(60)])

    def test_a_failed_statement_on_a_lookup_thread_fails_the_lookup(self):
        base = self._base(4)
        with self.assertRaises(Exception):
            list(base._parts("SELECT * FROM no_such_table WHERE ? = ?",
                             [(n, n) for n in range(10)]))

    def test_the_worker_count_is_read_from_the_environment_and_refused_when_unreadable(self):
        from ontology import delta
        self.assertEqual(delta.lookup_workers({"PLX_DELTA_LOOKUP_WORKERS": "1"}), 1)
        self.assertEqual(delta.lookup_workers({"PLX_DELTA_LOOKUP_WORKERS": " 6 "}), 6)
        self.assertGreaterEqual(delta.lookup_workers({}), 1)
        self.assertLessEqual(delta.lookup_workers({}), delta.DEFAULT_LOOKUP_WORKERS)
        for bad in ("0", "-2", "x", "1.5", "65"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                delta.lookup_workers({"PLX_DELTA_LOOKUP_WORKERS": bad})
        # a SQLite base reads on its one connection whatever is asked
        sqlite_base = delta.Base(self.base, workers=8)
        self.addCleanup(sqlite_base.close)
        self.assertEqual(sqlite_base.workers, 1)


class ParallelCycleTests(unittest.TestCase):

    def _cycle(self, workers: str) -> tuple[dict, dict]:
        from datetime import timedelta
        from ontology import delta
        from ontology.tests.test_delta import normalizer
        from ontology.tests.test_projection import T1, T2
        from operating.pg import ontology_load
        name, dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, name)
        _migrate(dsn)
        fixture = _fixture()
        self.addCleanup(fixture.doCleanups)
        fixture.capture(T1)
        base = fixture.full("gen1.sqlite")
        ontology_load.load(base, dsn, workers=1, replace=True, log=io.StringIO())
        fixture.fake.set("public.app_purchase_order", "po-a", status="RECEIVED")
        fixture.fake.set("public.app_purchase_order_line", "l1", purchase_order_id="po-b")
        fixture.capture(T2)
        fixture.fake.set("public.app_purchase_order", "po-b", status="CANCELLED")
        fixture.capture(T2 + timedelta(hours=1))
        chunks = delta._chunks
        # one id a statement: every lookup of the cycle is many statements
        with mock.patch.object(delta, "_chunks", lambda items, size=500: chunks(items, 1)):
            report = delta.build_delta(
                fixture.state, fixture.mirror_path, fixture.root / "delta", base_path=base,
                schema=fixture.schema, normalizer=normalizer, publish=False,
                environ={"PLX_DELTA_BASE_PG": "1", "PLX_PG_DSN": dsn,
                         "PLX_DELTA_LOOKUP_WORKERS": workers})
        return report, _rows(Path(report["store"]))

    def test_a_cycle_over_postgres_makes_the_same_layer_on_one_or_four_connections(self):
        serial, serial_rows = self._cycle("1")
        parallel, parallel_rows = self._cycle("4")
        self.assertGreater(serial["owned"], 0)
        for key in ("owned", "records", "projected", "changed"):
            self.assertEqual(parallel.get(key), serial.get(key), key)
        closure = {k: v for k, v in serial["closure"].items() if k != "seconds"}
        self.assertEqual({k: v for k, v in parallel["closure"].items() if k != "seconds"},
                         closure)
        self.assertGreater(closure["changed_objects_in_base"], 0)
        self.assertEqual(parallel_rows.keys(), serial_rows.keys())
        # each run captures its own fixture: handles, events and surrogate ids differ
        # between two serial runs too, so those tables compare by size; the projected
        # objects are the same rows, and the links the same ends, whatever the run
        for table in serial_rows:
            if table == "objects":
                self.assertEqual(parallel_rows[table], serial_rows[table], table)
            else:
                self.assertEqual(len(parallel_rows[table]), len(serial_rows[table]), table)
        ends = lambda rows: sorted(row[1:6] for row in rows)   # type, from, to, tier, kind
        self.assertEqual(ends(parallel_rows["link_rows"]), ends(serial_rows["link_rows"]))
        self.assertTrue(serial_rows["objects"] and serial_rows["link_rows"])


if __name__ == "__main__":
    unittest.main()
