"""A build's fingerprints and shard prints computed by worker processes
(``ontology/build_prints.py``).

What must hold: every relation fingerprint, watermark entry and shard print (and their
order) equals the serial ``_pin`` / ``_shard_prints`` result, for plain relations, for
sharded bindings and for relations derived from captured documents (mail attachments,
SharePoint files); a source a worker reads other bytes of, or a worker that dies, leaves
its prints to the build, still equal; the code that computes prints keeps its digests
(held prints stay held); a parallel build keeps the serial build's unit keys.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from ontology import build_prints as bp
from ontology import project_incremental as inc
from ontology.generator.fixture import SyntheticCaptures, sources
from ontology.project import MailCaptures
from ontology.tests.test_project_parallel import Drifting

CLOCK = lambda: datetime(2026, 1, 3, tzinfo=timezone.utc)  # noqa: E731


def _die(*_args):
    os._exit(3)


def serial_prints(built, the_plan, held=None, held_shards=None):
    pinned, prints, marks = inc._pin(built, the_plan, held)
    return prints, marks, inc._shard_prints(the_plan, pinned, prints, held_shards)


def parallel_prints(built, the_plan, held=None, held_shards=None, workers=3):
    pool = bp.PrintPool(workers)
    try:
        ahead = bp.ahead_marks(built, the_plan)
        pinned, prints, marks = bp.pinned(inc._pin, built, the_plan, held, pool, ahead)
        for name, source in pinned.items():   # the build's own sources, pinned
            if isinstance(source, inc._Pinned):
                assert not isinstance(source._inner, bp._Ahead), name
        shards = bp.shard_prints(inc._shard_prints, the_plan, pinned, prints, held_shards,
                                 pool, marks)
        return (prints, marks, shards), pool
    finally:
        pool.close()


def forced(the_plan, shards=4):
    """A held entry per isolated binding of another relation print: every binding is read
    and cut into ``shards`` (``shard_count`` keeps a held count)."""
    return {bid: {"relation": "earlier", "prints": [], "shards": shards}
            for bid in the_plan.isolated}


class SyntheticPrintsTests(unittest.TestCase):
    ROWS = 40

    @classmethod
    def setUpClass(cls):
        from ontology.schema import load_schema
        cls.schema = load_schema()
        cls.plan = inc.plan(cls.schema)

    def test_parallel_prints_equal_the_serial_ones(self):
        held = forced(self.plan)
        expected = serial_prints(sources(self.schema, self.ROWS), self.plan, None, held)
        got, pool = parallel_prints(sources(self.schema, self.ROWS), self.plan, None, held)
        self.assertEqual(got, expected)
        self.assertEqual([list(part) for part in got], [list(part) for part in expected])
        self.assertTrue(expected[2])
        self.assertGreater(pool.tasks, 0)
        self.assertEqual((pool.fallback, pool.broken), (0, False))
        # Held prints of the same relation print are taken, not read, by both.
        again = {bid: {**entry, "relation": expected[0][bid]}
                 for bid, entry in expected[2].items()}
        self.assertEqual(parallel_prints(sources(self.schema, self.ROWS), self.plan,
                                         expected[1], again)[0][2],
                         serial_prints(sources(self.schema, self.ROWS), self.plan,
                                       expected[1], again)[2])

    def test_a_source_read_otherwise_or_a_dead_worker_leaves_its_prints_to_the_build(self):
        held = forced(self.plan)
        expected = serial_prints(sources(self.schema, self.ROWS), self.plan, None, held)
        drifting = sources(self.schema, self.ROWS)
        drifting["portal_api"] = Drifting(self.schema, "portal_api", self.ROWS)
        drift_expected = serial_prints(
            {**sources(self.schema, self.ROWS),
             "portal_api": Drifting(self.schema, "portal_api", self.ROWS)},
            self.plan, None, held)
        got, pool = parallel_prints(drifting, self.plan, None, held)
        self.assertEqual(got, drift_expected)
        self.assertGreater(pool.fallback, 0)
        with mock.patch.object(bp, "_relation_print", _die), \
                mock.patch.object(bp, "_shard_pass", _die):
            got, pool = parallel_prints(sources(self.schema, self.ROWS), self.plan, None,
                                        held)
        self.assertEqual(got, expected)
        self.assertTrue(pool.broken)

    def test_the_code_prints_are_computed_by_keeps_its_digests(self):
        # build_prints is reached by no fingerprint, reader or shard root: held
        # watermarks, relation prints and shard prints of an earlier build stay held.
        from ontology import build_keys
        reached = set(build_keys.code_parts(build_keys.SHARD_ROOTS))
        for name in sources(self.schema, 1).values():
            reached |= set(build_keys.reader_parts(name))
        reached |= set(build_keys.code_parts(build_keys.ROWS_CODE_ROOTS,
                                             build_keys.ROWS_CODE_STOPS))
        self.assertFalse({key for key in reached if key[0] == "ontology.build_prints"})

    def test_a_parallel_build_keeps_the_serial_builds_keys(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(inc, "SHARD_RECORDS", 4):
            units = {}
            for workers in (1, 3):
                built = inc.build_generation(Path(tmp) / str(workers), self.schema,
                                             sources(self.schema, self.ROWS), clock=CLOCK,
                                             workers=workers)
                units[workers] = json.loads((built.store.parent / "units.json").read_text())
        self.assertTrue(units[1]["shards"])
        for part in ("keys", "shards", "watermarks"):
            self.assertEqual(units[3][part], units[1][part], part)


class DocumentSourcePrintsTests(unittest.TestCase):
    """Mail and SharePoint captures with derived relations: their relation prints by
    workers, their documentFields passes by the build."""

    @classmethod
    def setUpClass(cls):
        from ontology.schema import load_schema
        cls.schema = load_schema()
        cls.plan = inc.plan(cls.schema)

    def check(self, opener):
        held = forced(self.plan)
        with opener() as one:
            expected = serial_prints({one.source: one}, self.plan, None, held)
        with opener() as one:
            got, pool = parallel_prints({one.source: one}, self.plan, None, held)
        self.assertEqual(got, expected)
        self.assertEqual([list(part) for part in got], [list(part) for part in expected])
        self.assertTrue(any(v for v in expected[0].values()))
        self.assertGreater(pool.tasks, 0)
        self.assertEqual((pool.fallback, pool.broken), (0, False))
        return expected

    def test_sharepoint_prints_equal_the_serial_ones(self):
        from integrations.tests.test_capture_sharepoint import CaptureCase, scope
        from ontology.project import SharePointCaptures

        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        case = CaptureCase("run")
        case.setUp()
        self.addCleanup(case.tearDown)
        self.assertTrue(case.capture().run(scope())["complete"])
        expected = self.check(lambda: SharePointCaptures(case.state))
        self.assertTrue(any(bid.startswith("sharepoint.") for bid in expected[2]))

    def test_mail_prints_equal_the_serial_ones(self):
        from ontology.project import MailCaptures
        from ontology.tests import test_mail_documents as md

        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        root = md.captured_mail_with_documents(self)
        expected = self.check(lambda: MailCaptures(
            root / "capture", internal_domains=md.SCOPE.internal_domains))
        self.assertTrue(any(bid.startswith("graph_mail.") for bid in expected[2]))


class SplitMailReadTests(unittest.TestCase):
    """The mail attachments read by mailbox ranges side by side
    (``MailCaptures._attached_rows``): every attachment row, derived record, relation
    fingerprint and shard print equals the one read's, and a reader that cannot open
    under the snapshot's file marks leaves it one read."""

    @classmethod
    def setUpClass(cls):
        from ontology.schema import load_schema
        cls.schema = load_schema()
        cls.plan = inc.plan(cls.schema)

    def store(self, extra=()):
        """The mail fixture with its mailbox copied under three more ids (one sorting
        before it), so the attachments span four mailboxes (and under ``extra``)."""
        import sqlite3
        from ontology.tests import test_mail_documents as md

        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        root = md.captured_mail_with_documents(self)
        conn = sqlite3.connect(str(root / "capture" / "mail-capture" / "capture.db"))
        try:
            (held,), = conn.execute("SELECT DISTINCT mailbox_id FROM attachments").fetchall()
            columns = {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
                       for t in ("messages", "attachments")}
            for other in ("00000000-0000-0000-0000-000000000001", held[:-1] + "b",
                          held[:-1] + "c", *(e(held) for e in extra)):
                for table, renamed in (("messages", ("l1_handle", "mailbox_id")),
                                       ("attachments", ("message_handle", "l1_handle",
                                                        "mailbox_id"))):
                    names = [c for c in columns[table] if c != "version_id"]
                    picked = [f"replace({c}, :held, :other)" if c in renamed else c
                              for c in names]
                    conn.execute(f"INSERT INTO {table} ({', '.join(names)}) SELECT "
                                 f"{', '.join(picked)} FROM {table} WHERE mailbox_id = :held",
                                 {"held": held, "other": other})
            conn.commit()
        finally:
            conn.close()
        return lambda: MailCaptures(root / "capture",
                                    internal_domains=md.SCOPE.internal_domains)

    def read(self, opener, readers):
        relations = ("attachments", "documentFields", "containerMembers")
        held = forced(self.plan)
        with mock.patch.object(MailCaptures, "SPLIT_READERS", readers), \
                mock.patch.object(MailCaptures, "SPLIT_ROWS", 1):
            with opener() as one:
                records = {r: list(map(inc._fingerprint_cells, recs))
                           for r, recs in one.records(relations).items()}
            with opener() as one:
                prints = serial_prints({one.source: one}, self.plan, None, held)
        return records, prints

    def test_mail_read_by_mailbox_ranges_equals_one_read(self):
        opener = self.store()
        with opener() as one, mock.patch.object(MailCaptures, "SPLIT_READERS", 3), \
                mock.patch.object(MailCaptures, "SPLIT_ROWS", 1):
            one.records(["attachments"])
            self.assertEqual(len(one._mailbox_ranges()), 3)
        one_read = self.read(opener, 1)
        split = self.read(opener, 3)
        self.assertEqual(split, one_read)
        self.assertGreaterEqual(len(one_read[0]["attachments"]), 32)
        self.assertGreater(len(one_read[0]["documentFields"]), 0)
        self.assertTrue(any(bid.startswith("graph_mail.") for bid in one_read[1][2]))

    def test_a_mailbox_id_whose_handles_sort_apart_leaves_one_read(self):
        # ``<id>.x``'s handles sort before ``<id>``'s ('.' < '/'), its mailbox after
        opener = self.store(extra=(lambda held: held + ".x",))
        with opener() as one, mock.patch.object(MailCaptures, "SPLIT_READERS", 3), \
                mock.patch.object(MailCaptures, "SPLIT_ROWS", 1):
            one.records(["attachments"])
            self.assertIsNone(one._mailbox_ranges())
        self.assertEqual(self.read(opener, 3), self.read(opener, 1))

    def test_a_reader_under_other_file_marks_leaves_one_read(self):
        from ontology import project

        opener = self.store()
        one_read = self.read(opener, 1)
        marks = project.begin_snapshot
        calls = []

        def drifted(conn):   # a reader's file marks moved since the snapshot
            found = marks(conn)
            if threading.current_thread() is threading.main_thread():
                return found
            calls.append(1)
            return found if len(calls) % 3 else [["moved"]]
        with mock.patch.object(project, "begin_snapshot", drifted):
            self.assertEqual(self.read(opener, 3), one_read)
        self.assertGreater(len(calls), 2)


class SpeedProbe(unittest.TestCase):
    """Not a gate: ``PLX_PRINTS_PROBE=1`` measures serial and parallel prints of the
    synthetic captures at a few sizes (stdout)."""

    @unittest.skipUnless(os.environ.get("PLX_PRINTS_PROBE"), "measurement only")
    def test_measure(self):
        from ontology.schema import load_schema
        schema = load_schema()
        the_plan = inc.plan(schema)
        for rows in (1, 10, 60, int(os.environ.get("PLX_PRINTS_ROWS", "3000"))):
            held = forced(the_plan)
            started = time.monotonic()
            expected = serial_prints(sources(schema, rows), the_plan, None, held)
            serial = time.monotonic() - started
            started = time.monotonic()
            got, _ = parallel_prints(sources(schema, rows), the_plan, None, held,
                                     workers=int(os.environ.get("PLX_PRINTS_WORKERS", "8")))
            parallel = time.monotonic() - started
            self.assertEqual(got, expected)
            print(f"rows={rows} serial={serial:.2f}s parallel={parallel:.2f}s")


if __name__ == "__main__":
    unittest.main()
