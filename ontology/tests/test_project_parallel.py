"""Parallel cold projection (``ontology/project_parallel.py``).

What must hold: a cold generation projected by worker processes, and digested by them,
equals the serial one (every row of every table, the build report and the content digest); a build whose
sources a worker cannot open again, or whose worker reads other bytes than the build
pinned, is projected serially and still equals it; a worker count that is not a positive
integer is refused. Tasks: a sharded binding's shards run as several tasks (each unit in
exactly one), the bindings over one derived relation of a source are one task, whose
worker reads the relation once (a spilled pass yields what the source yields; a pass
that did not finish, or a record that does not pickle, leaves no spill).
"""

from __future__ import annotations

from contextlib import closing
import dataclasses
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
from ontology import finish_digest, store
from ontology.generator.fixture import SyntheticCaptures, sources
from ontology.project import ProjectionError
from ontology.project import Reopenable, build_store
from ontology.tests.test_project_incremental import _Mailbox, incremental_path, tables

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
        cut = par.tasks(the_plan, par.pieces(3))
        self.assertEqual(sorted(u for task in cut for u in task), sorted(the_plan.units))
        self.assertEqual(set(record["seconds"]), {task[0] for task in cut})
        self.assertSameAsSerial(generation)
        # The same schema, every index included (the merge builds some at its end).
        self.assertEqual(schema_of(generation.store), schema_of(self.serial.store))
        # No part, working file or worker directory is left beside the generation.
        self.assertEqual(sorted(p.name for p in generation.store.parent.iterdir()),
                         ["store.sqlite", "timings.json", "units.json"])

    def test_a_same_record_link_is_projected_with_both_of_its_ends(self):
        the_plan = inc.plan(self.schema)
        where = {u: task for task in par.tasks(the_plan) for u in task}
        same = [(u, row) for u, row in the_plan.rows.items()
                if row[0] == "links" and row[4] == "same_record"]
        self.assertTrue(same)
        for unit, row in same:
            self.assertIn(f"b:{row[2]}", where[unit])
            self.assertIn(f"b:{row[3]}", where[unit])

    def test_shards_run_as_tasks_and_a_derived_relation_is_one_task(self):
        the_plan = inc.plan(self.schema)
        derived = {}
        for bid, (_, b) in the_plan.bindings.items():
            if b["relation"] in par.DERIVED:
                derived.setdefault((b["source"], b["relation"]), set()).add(f"b:{bid}")
        self.assertTrue(any(len(units) > 1 for units in derived.values()))
        plain = next(bid for bid in sorted(the_plan.isolated)
                     if the_plan.bindings[bid][1]["relation"] not in par.DERIVED)
        mail = next(bid for bid in sorted(the_plan.isolated)
                    if the_plan.bindings[bid][1]["relation"] in par.DERIVED)
        sharded = inc.with_shards(the_plan, {plain: 16, mail: 4})
        for pieces in (1, 2, 6, 32):
            with self.subTest(pieces=pieces):
                cut = par.tasks(sharded, pieces)
                every = [u for task in cut for u in task]
                self.assertEqual(sorted(every), sorted(sharded.units))
                where = {u: task for task in cut for u in task}
                # The plain binding: min(pieces, 16) tasks of its own units, the first
                # with its gaps unit; each of consecutive shards.
                own = sorted({where[f"b:{plain}#{k}"] for k in range(16)})
                self.assertEqual(len(own), min(pieces, 16))
                for task in own:
                    self.assertTrue(all(u == f"b:{plain}" or u.startswith(f"b:{plain}#")
                                        for u in task), task)
                self.assertIn(f"b:{plain}", where[f"b:{plain}#0"])
                # Every binding over one derived relation of a source: one task, the
                # shards of a sharded one with it.
                for units in derived.values():
                    self.assertEqual(len({where[u] for u in units}), 1, units)
                self.assertTrue(all(where[f"b:{mail}#{k}"] == where[f"b:{mail}"]
                                    for k in range(4)))
                # Each task's derived relations with several readers are read once.
                scope = set().union(*(sharded.units[u] for u in where[f"b:{mail}"]))
                b = the_plan.bindings[mail][1]
                self.assertIn((b["source"], b["relation"]), par._spilled(sharded, scope))

    def test_a_sharded_parallel_build_equals_the_serial_one(self):
        # Every isolated binding of a few records cut into shards (shard_count), and
        # its shards projected as several tasks: the generation the serial one's.
        with mock.patch.object(inc, "SHARD_RECORDS", 1):
            serial = inc.build_generation(self.root / "serial-sharded", self.schema,
                                          sources(self.schema, self.ROWS), clock=CLOCK,
                                          workers=1)
            generation, record = self.build("sharded", sources(self.schema, self.ROWS))
        units = json.loads((generation.store.parent / "units.json").read_text())
        self.assertTrue(units["shards"])
        self.assertNotIn("unavailable", record)
        self.assertTrue(any("#" in task for task in record["seconds"]), record["seconds"])
        self.assertEqual(generation.content_digest, self.serial.content_digest)
        self.assertEqual(generation.content_digest, serial.content_digest)
        self.assertEqual(generation.report, {**serial.report,
                                             "store": str(generation.store)})
        self.assertEqual(tables(generation.store), self.serial_tables)

    def test_a_derived_relation_is_read_once_and_replayed_as_read(self):
        class Source:
            reads = 0

            def __init__(self, records):
                self.records = records

            def __iter__(self):
                Source.reads += 1
                yield from self.records

        records = [{"n": n, "row": {"k": [n, str(n)]}} for n in range(600)]
        with tempfile.TemporaryDirectory() as tmp:
            spill = par._Spill(Source(records), Path(tmp) / "spill.bin")
            for _ in range(3):
                self.assertEqual(list(spill), records)
            self.assertEqual(Source.reads, 1)
            self.assertEqual(os.stat(spill.path).st_mode & 0o777, 0o600)
            # A pass that stops early leaves no spill: the next reads the source.
            Source.reads = 0
            spill = par._Spill(Source(records), Path(tmp) / "partial.bin")
            it = iter(spill)
            self.assertEqual(next(it), records[0])
            it.close()
            self.assertFalse(spill.path.exists())
            self.assertEqual(list(spill), records)
            self.assertEqual(list(spill), records)
            self.assertEqual(Source.reads, 2)
            # A record that does not pickle: every pass reads the source.
            Source.reads = 0
            odd = [*records[:3], {"n": lambda: None}]
            spill = par._Spill(Source(odd), Path(tmp) / "odd.bin")
            self.assertEqual(list(spill), odd)
            self.assertEqual(list(spill), odd)
            self.assertEqual(Source.reads, 2)
            self.assertFalse(spill.path.exists())

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


class Edited(SyntheticCaptures):
    """Synthetic captures whose ``relation`` holds ``value`` at ``path`` in every record
    (a field compared across a same_record link, not one it is matched by), opened again
    by a worker from its arguments, which its watermark names."""

    def __init__(self, schema, source, rows, changed, relation, path, value):
        super().__init__(schema, source, rows, changed)
        self.edit = (relation, path, value)

    def _stream(self, relation):
        for rec in super()._stream(relation):
            if relation == self.edit[0] and self.edit[1] in rec.row:
                rec = dataclasses.replace(rec, row={**rec.row, self.edit[1]: self.edit[2]},
                                          digest="d" * 64)
            yield rec

    def watermark(self):
        return [*super().watermark(), list(self.edit)]


class ReopenedMailbox(Reopenable):
    """The synthetic mail capture with mail arriving (``_Mailbox``), opened again by a
    worker from its arguments, which its watermark names."""

    source = "graph_mail"

    def __init__(self, schema, rows, new=0):
        self.new = new
        self.box = _Mailbox(SyntheticCaptures(schema, "graph_mail", rows), new=new)

    def records(self, relations):
        return self.box.records(relations)

    def describe(self):
        return self.box.describe()

    def watermark(self):
        return [*self.box.inner.watermark(), self.new]


class IncrementalParallelTests(unittest.TestCase):
    """An incremental build's changed units projected by worker processes and merged into
    its seed: rows, report and content digest equal the serial incremental build's and a
    cold build's of the same captures."""

    ROWS = 8

    @classmethod
    def setUpClass(cls):
        from ontology.schema import load_schema
        cls.schema = load_schema()

    @staticmethod
    def units(generation):
        return json.loads((generation.store.parent / "units.json").read_text())

    def test_changed_units_projected_by_workers_equal_the_serial_and_cold_builds(self):
        # A Portal header (its lines, its BC posting), a packed sharded Sage book, mail,
        # and every Sage customer's name: its same_record link to the unchanged Portal
        # customer now holds a property conflict, read from the values the seed stores
        # for that end, so it is projected after the merge in the build's own process.
        changed = ["public.app_purchase_order", "sage.ap_pjd", "messages"]

        def captures():
            built = sources(self.schema, self.ROWS, changed)
            built["portal_api"] = Edited(self.schema, "portal_api", self.ROWS, changed,
                                         "sage.ar_customer", "namecust", "Other name")
            return built
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(inc, "SHARD_RECORDS", 1):
            root = Path(tmp)
            built = {}
            for name, workers in (("serial", 1), ("parallel", 3)):
                inc.build_generation(root / name, self.schema, sources(self.schema, self.ROWS),
                                     clock=CLOCK, workers=1)
                # The parallel build's digest of the segments it wrote made by its workers
                # too, every segment cut into many ranges (a few rows each).
                with mock.patch.multiple(store, DIGEST_PARALLEL_MIN_BYTES=0, _CHUNK_ROWS=5,
                                         _CHUNK_VALUE_OBJECTS=2), \
                        mock.patch.object(finish_digest, "parallel_segment_parts",
                                          wraps=finish_digest.parallel_segment_parts
                                          ) as digested:
                    built[name] = inc.build_generation(
                        root / name, self.schema, captures(), clock=CLOCK, workers=workers,
                        space=incremental_path)
                self.assertEqual(digested.call_count, 1 if workers > 1 else 0)
            cold = build_store(root / "cold.sqlite", self.schema, captures(), clock=CLOCK)
            with closing(sqlite3.connect(cold["store"])) as conn:
                conflicts = conn.execute(
                    "SELECT COUNT(*) FROM links WHERE link_type = 'Customer.same_record_as' "
                    "AND why_not LIKE '%property_conflict%'").fetchone()[0]
            self.assertGreater(conflicts, 0)
            serial, parallel = built["serial"], built["parallel"]
            self.assertEqual((serial.mode, parallel.mode), ("incremental", "incremental"))
            self.assertEqual(parallel.units_rebuilt, serial.units_rebuilt)
            record = self.units(parallel)["parallel"]
            self.assertEqual(record["workers"], 3)
            self.assertNotIn("unavailable", record)
            # Some units by the workers, the same_record one in the build's own process.
            self.assertIn("project.parallel", parallel.seconds)
            self.assertIn("project.serial", parallel.seconds)
            self.assertNotIn("project.parallel", serial.seconds)
            self.assertEqual(tables(parallel.store), tables(serial.store))
            self.assertEqual(parallel.report, {**serial.report, "store": str(parallel.store)})
            self.assertEqual(parallel.content_digest, serial.content_digest)
            self.assertEqual(parallel.content_digest, cold["content_digest"])
            self.assertEqual(tables(parallel.store), tables(cold["store"]))
            self.assertEqual(sorted(p.name for p in parallel.store.parent.iterdir()),
                             ["store.sqlite", "timings.json", "units.json"])
            self.assertEqual(parallel.digest["mode"], "segments")
            # Every step's start, end and seconds kept beside the generation as it ran.
            timings = json.loads((parallel.store.parent / "timings.json").read_text())
            steps = {entry["step"]: entry for entry in timings["steps"]}
            for step in ("project.seed", "project.parallel", "project.counts",
                         "finish.check", "finish.digest", "finish"):
                self.assertIn(step, steps)
                self.assertIn("started_at", steps[step])
                self.assertEqual(steps[step]["seconds"], parallel.seconds[step])
            self.assertEqual(timings["running"], {})

    def test_mail_rewritten_object_by_object_by_workers_equals_cold(self):
        """One, ten and sixty new messages: the workers write only the changed objects
        of the mail bindings and hand on their report parts, so each build (and the next,
        which reads them) equals a cold build."""
        def mail(new):
            inner = sources(self.schema, 64, only=["graph_mail", "sharepoint"])
            return {**inner, "graph_mail": ReopenedMailbox(self.schema, 64, new)}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(inc, "OBJECT_RECORDS", 4):
            root = Path(tmp)
            first = inc.build_generation(root / "state", self.schema, mail(0), clock=CLOCK,
                                         workers=1)
            self.assertEqual(self.units(first)["objects"]["graph_mail.messages"]["mode"],
                             "whole")
            for new, changed in ((1, 1), (10, 9), (60, 50)):
                with self.subTest(new=new):
                    built = inc.build_generation(root / "state", self.schema, mail(new),
                                                 clock=CLOCK, workers=3,
                                                 space=incremental_path)
                    cold = build_store(root / f"cold{new}.sqlite", self.schema, mail(new),
                                       clock=CLOCK)
                    units = self.units(built)
                    self.assertEqual(built.mode, "incremental")
                    self.assertEqual(units["parallel"]["workers"], 3)
                    self.assertNotIn("unavailable", units["parallel"])
                    self.assertEqual(units["objects"]["graph_mail.messages"]["mode"],
                                     "delta")
                    self.assertEqual(units["objects"]["graph_mail.messages"]["changed"],
                                     changed)
                    self.assertEqual(tables(built.store), tables(cold["store"]))
                    self.assertEqual(built.content_digest, cold["content_digest"])
                    self.assertEqual(built.report["gaps"], cold["gaps"])
                    self.assertEqual(built.report["counts"], cold["counts"])

    def test_a_build_failing_in_its_unjournaled_merge_is_discarded_and_the_next_equals_cold(self):
        """The seed's deletes and the merge write the build's file with no rollback
        journal (its foreign keys off): a failure there leaves the committed generation
        current and no build file, and the next build, digested by segments, equals a
        cold one."""
        merge, modes = par.merge_part, []

        def failing(builder, part, batch, **kwargs):
            modes.append(builder.connection.execute("PRAGMA journal_mode").fetchone()[0])
            if len(modes) == 2:
                raise OSError("disk_lost")
            return merge(builder, part, batch, **kwargs)
        changed = sources(self.schema, self.ROWS, ["public.app_purchase_order", "sage.ap_pjd",
                                                   "messages"])
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(inc, "SHARD_RECORDS", 1):
            state = Path(tmp) / "state"
            first = inc.build_generation(state, self.schema, sources(self.schema, self.ROWS),
                                         clock=CLOCK, workers=1)
            with mock.patch.object(par, "merge_part", failing), \
                    self.assertRaisesRegex(OSError, "disk_lost"):
                inc.build_generation(state, self.schema, changed, clock=CLOCK, workers=3,
                                     space=incremental_path)
            self.assertEqual(modes, ["off", "off"])
            self.assertEqual(inc.current_generation(state).id, first.id)
            self.assertEqual([p.name for p in (state / "generations").iterdir()], [first.id])
            built = inc.build_generation(state, self.schema, changed, clock=CLOCK, workers=3,
                                         space=incremental_path)
            cold = build_store(Path(tmp) / "cold.sqlite", self.schema, changed, clock=CLOCK)
            self.assertEqual((built.mode, built.digest["mode"]), ("incremental", "segments"))
            self.assertIn("project.parallel", built.seconds)
            with closing(sqlite3.connect(built.store)) as conn:
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
                self.assertEqual(store.content_digest(conn), built.content_digest)
            self.assertEqual(built.content_digest, cold["content_digest"])
            self.assertEqual(tables(built.store), tables(cold["store"]))

    def test_a_worker_reading_other_bytes_leaves_its_units_to_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inc.build_generation(root / "state", self.schema, sources(self.schema, self.ROWS),
                                 clock=CLOCK, workers=1)
            changed = sources(self.schema, self.ROWS, ["purchaseOrders", "sage.ap_pjd"])
            changed["bc"] = Drifting(self.schema, "bc", self.ROWS, ["purchaseOrders"])
            built = inc.build_generation(root / "state", self.schema, changed, clock=CLOCK,
                                         workers=3, space=incremental_path)
            cold = build_store(root / "cold.sqlite", self.schema, changed, clock=CLOCK)
            record = self.units(built)["parallel"]
            self.assertEqual(built.mode, "incremental")
            self.assertEqual(record["unavailable"], "source_changed:bc")
            self.assertIn("project.serial", built.seconds)
            self.assertEqual(tables(built.store), tables(cold["store"]))
            self.assertEqual(built.content_digest, cold["content_digest"])


if __name__ == "__main__":
    unittest.main()
