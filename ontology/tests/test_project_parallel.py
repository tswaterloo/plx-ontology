"""Parallel cold projection (``ontology/project_parallel.py``).

What must hold: a cold generation projected by worker processes, and digested by them,
equals the serial one (every row of every table, the build report and the content digest); a build whose
sources a worker cannot open again, or whose worker reads other bytes than the build
pinned, is projected serially and still equals it; a worker count that is not a positive
integer is refused.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from ontology import project_incremental as inc
from ontology import project_parallel as par
from ontology import store
from ontology.generator.fixture import SyntheticCaptures, sources
from ontology.project import ProjectionError
from ontology.tests.test_project_incremental import tables

def schema_of(path):
    conn = sqlite3.connect(str(path))
    try:
        return sorted(conn.execute("SELECT type, name, sql FROM sqlite_master"))
    finally:
        conn.close()


CLOCK = lambda: datetime(2026, 1, 3, tzinfo=timezone.utc)  # noqa: E731


class Drifting(SyntheticCaptures):
    """Synthetic captures whose watermark names the process reading them: a worker's
    differs from the build's, as a store written after the build pinned it would."""

    def watermark(self):
        return [*super().watermark(), os.getpid()]


class Unreopenable:
    """A source that does not remember how it was opened (an in-memory capture)."""

    def __init__(self, inner):
        self.inner, self.source = inner, inner.source

    def records(self, relations):
        return self.inner.records(relations)

    def describe(self):
        return self.inner.describe()

    def watermark(self):
        return self.inner.watermark()


class ParallelBuildTests(unittest.TestCase):
    ROWS = 6

    @classmethod
    def setUpClass(cls):
        from ontology.schema import load_schema
        cls.schema = load_schema()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.serial = inc.build_generation(cls.root / "serial", cls.schema,
                                          sources(cls.schema, cls.ROWS), clock=CLOCK,
                                          workers=1)
        cls.serial_tables = tables(cls.serial.store)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def build(self, name, built, workers=3):
        generation = inc.build_generation(self.root / name, self.schema, built,
                                          clock=CLOCK, workers=workers)
        record = json.loads((generation.store.parent / "units.json").read_text())
        return generation, record["parallel"]

    def assertSameAsSerial(self, generation):
        self.assertEqual(generation.content_digest, self.serial.content_digest)
        self.assertEqual(generation.report, {**self.serial.report,
                                             "store": str(generation.store)})
        self.assertEqual(tables(generation.store), self.serial_tables)

    def test_a_parallel_cold_build_equals_the_serial_one(self):
        # Its content digest made by worker processes too, every table cut into many
        # ranges (a few rows each), whatever the store's size.
        with mock.patch.multiple(store, DIGEST_PARALLEL_MIN_BYTES=0, _CHUNK_ROWS=5,
                                 _CHUNK_VALUE_OBJECTS=2), \
                mock.patch.object(store, "_parallel_digest_parts",
                                  wraps=store._parallel_digest_parts) as digested:
            generation, record = self.build("parallel", sources(self.schema, self.ROWS))
            conn = sqlite3.connect(str(generation.store))
            ranges = {table: len(store._chunk_bounds(conn, table)) - 1
                      for table in store._CHUNK_KEYS}
            conn.close()
        self.assertEqual(digested.call_count, 1)
        # (the fixture removes no object: that table is one empty range)
        self.assertTrue(all(count > 2 for table, count in ranges.items()
                            if table != "object_removals"), ranges)
        self.assertEqual(record["workers"], 3)
        self.assertNotIn("unavailable", record)
        # Every task was measured (the next build starts the longest first), and the
        # tasks cover every unit once.
        the_plan = inc.plan(self.schema)
        cut = par.tasks(the_plan)
        self.assertEqual(sorted(u for task in cut for u in task), sorted(the_plan.units))
        self.assertEqual(set(record["seconds"]), {task[0] for task in cut})
        self.assertSameAsSerial(generation)
        # The same schema, every index included (the merge builds some at its end).
        self.assertEqual(schema_of(generation.store), schema_of(self.serial.store))
        # No part, working file or worker directory is left beside the generation.
        self.assertEqual(sorted(p.name for p in generation.store.parent.iterdir()),
                         ["store.sqlite", "units.json"])

    def test_a_same_record_link_is_projected_with_both_of_its_ends(self):
        the_plan = inc.plan(self.schema)
        where = {u: task for task in par.tasks(the_plan) for u in task}
        same = [(u, row) for u, row in the_plan.rows.items()
                if row[0] == "links" and row[4] == "same_record"]
        self.assertTrue(same)
        for unit, row in same:
            self.assertIn(f"b:{row[2]}", where[unit])
            self.assertIn(f"b:{row[3]}", where[unit])

    def test_a_source_a_worker_cannot_open_again_makes_the_build_serial(self):
        built = sources(self.schema, self.ROWS)
        built["bc"] = Unreopenable(built["bc"])
        generation, record = self.build("unreopenable", built)
        self.assertEqual(record, {"unavailable": "source_not_reopenable:bc"})
        self.assertSameAsSerial(generation)

    def test_a_worker_reading_other_bytes_than_the_build_makes_it_serial(self):
        built = sources(self.schema, self.ROWS)
        built["bc"] = Drifting(self.schema, "bc", self.ROWS)
        generation, record = self.build("drifting", built)
        self.assertEqual(record, {"unavailable": "source_changed:bc"})
        self.assertSameAsSerial(generation)

    def test_a_worker_count_that_is_not_a_positive_integer_is_refused(self):
        for raw in ("0", "-2", "many"):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {par.WORKERS_ENV: raw}):
                with self.assertRaises(ProjectionError) as caught:
                    par.workers()
                self.assertEqual(caught.exception.code, "build_workers_invalid")
        with mock.patch.dict(os.environ, {par.WORKERS_ENV: "1"}):
            self.assertEqual(par.workers(), 1)


if __name__ == "__main__":
    unittest.main()
