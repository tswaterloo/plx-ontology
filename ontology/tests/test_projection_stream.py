"""Bounded-memory, batch-independent projection (decision projection-memory, option a).

The projector streams each binding through an on-disk working file and writes the
store a batch at a time. These tests pin the two properties that make that safe:
the result does not depend on the batch size, and Python memory does not grow with
the capture.
"""

from __future__ import annotations

import gc
import json
from pathlib import Path
import sqlite3
import tempfile
import tracemalloc
import unittest
from unittest import mock

from ontology import project
from ontology.project import Projector, build_store
from ontology.tests import synthetic_captures
from ontology.tests.test_projection import ProjectionCase, T2, compiled_schema, normalizer


def _summary(report):
    return json.dumps({k: report[k] for k in ("counts", "gaps")}, sort_keys=True)


class BatchIndependenceTests(ProjectionCase):
    """The fixture mirror world (two captures, every link kind, conflicts, events)."""

    def test_same_captures_same_digest_and_report_for_any_batch_size(self):
        for batch in (1, 2, 3, 7):
            path, report = self.build(f"batch/{batch}.sqlite", batch_size=batch)
            self.assertEqual(report["content_digest"], self.report["content_digest"], batch)
            self.assertEqual(_summary(report), _summary(self.report), batch)
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()),
                             sorted(f"{b}.sqlite" for b in (1, 2, 3, 7) if b <= batch))

    def test_cut_build_is_batch_independent_too(self):
        _, first = self.build("cut/a.sqlite", as_known_at="2026-09-29T12:00:00Z")
        _, second = self.build("cut/b.sqlite", as_known_at="2026-09-29T12:00:00Z",
                               batch_size=1)
        self.assertEqual(first["content_digest"], second["content_digest"])
        self.assertEqual(_summary(first), _summary(second))

    def test_invalid_batch_size_is_refused(self):
        with self.assertRaises(project.ProjectionError) as caught:
            Projector(self.schema, {}, normalizer=normalizer, batch_size=0)
        self.assertEqual(caught.exception.code, "batch_size_invalid")


class SyntheticWorldTests(unittest.TestCase):
    """The real curated schema over generated captures (every binding, link and event
    type the schema declares)."""

    @classmethod
    def setUpClass(cls):
        from ontology.schema import load_schema
        cls.schema = load_schema()

    def build(self, rows, **options):
        with tempfile.TemporaryDirectory() as tmp:
            report = build_store(Path(tmp) / "store.sqlite", self.schema,
                                 synthetic_captures.sources(self.schema, rows),
                                 clock=lambda: T2, **options)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["store.sqlite"])
        return report

    def test_curated_schema_digest_does_not_depend_on_batch_size(self):
        reference = self.build(6)
        counts = reference["counts"]
        self.assertGreater(counts["objects"], 500)
        self.assertGreater(counts["links"], 300)
        self.assertGreater(counts["events"], 50)
        self.assertGreater(counts["versions"], counts["objects"])
        for batch in (1, 5):
            report = self.build(6, batch_size=batch)
            self.assertEqual(report["content_digest"], reference["content_digest"], batch)
            self.assertEqual(_summary(report), _summary(reference), batch)

    def test_stored_key_links_reach_their_declared_ceiling(self):
        # DECISIONS/2026-10-02-stored-key-compare: no stored-key binding compares text, so
        # no projected link carries the free-text cap, and an uncontested link sits at the
        # ceiling its binding declares (incl. the links whose pairs were text before).
        ceilings: dict[tuple, set] = {}
        for lid, link in self.schema.link_types.items():
            for lb in link["bindings"]:
                ceilings.setdefault((lid, lb.get("from_binding"), lb.get("to_binding"),
                                     lb["kind"]), set()).add(lb["tier_ceiling"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.sqlite"
            build_store(path, self.schema, synthetic_captures.sources(self.schema, 6),
                        clock=lambda: T2)
            conn = sqlite3.connect(str(path))
            rows = conn.execute("SELECT link_type, from_object, to_object, tier, binding_kind,"
                                " why_not FROM links").fetchall()
            conn.close()
        seen = set()
        for link_type, from_object, to_object, tier, kind, why_not in rows:
            caps = [w.get("because") for w in json.loads(why_not) if w.get("feature") == "tier_cap"]
            self.assertNotIn("free_text_compare_is_not_a_stored_key", caps, link_type)
            key = (link_type, from_object.split(":")[1], to_object.split(":")[1], kind)
            if not json.loads(why_not) and len(ceilings[key]) == 1:
                self.assertEqual(tier, next(iter(ceilings[key])), key)
                seen.add(key)
        for key in (("Customer.same_record_as", "portal.app_customer", "portal.fm_customer",
                     "same_record"),
                    ("Receipt.receives", "portal.app_receipt", "portal.app_purchase_order", "fk"),
                    ("ReceiptLine.line_of", "portal.app_receipt_line", "portal.app_receipt",
                     "fk"),
                    ("PurchaseOrderLine.line_of", "portal.app_purchase_order_line",
                     "portal.app_purchase_order", "fk")):
            self.assertIn(key, seen)

    def test_failed_build_removes_its_working_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "store.sqlite"
            with mock.patch.object(Projector, "_project_events",
                                   side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    build_store(target, self.schema, synthetic_captures.sources(self.schema, 2),
                                clock=lambda: T2)
            self.assertEqual(list(Path(tmp).iterdir()), [])


class WorkingFileHygieneTests(unittest.TestCase):
    def test_working_file_is_created_exclusively_with_mode_0600(self):
        import os, stat
        from ontology.project import Projector
        with tempfile.TemporaryDirectory() as tmp:
            projector = Projector.__new__(Projector)
            projector.scratch_dir = Path(tmp)
            projector._conn = None
            projector._cache = {}
            projector._cites = {}
            projector._finalizer = None
            with mock.patch.object(project.secrets, "token_hex", return_value="fixed"):
                (Path(tmp) / ".plx-projection-fixed.work").write_bytes(b"planted")
                with self.assertRaises(FileExistsError):
                    projector._db
                (Path(tmp) / ".plx-projection-fixed.work").unlink()
                projector._db
                mode = stat.S_IMODE(os.stat(Path(tmp) / ".plx-projection-fixed.work").st_mode)
            self.assertEqual(mode, 0o600)
            projector.close()

    def test_sweep_removes_only_stale_unheld_files(self):
        import os, sqlite3, time
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            old = time.time() - 48 * 3600

            def make(name, age):
                path = d / name
                sqlite3.connect(str(path)).execute("CREATE TABLE t (x)").connection.close()
                os.utime(path, (age, age))
                return path

            stale_work = make(".plx-projection-aaaa.work", old)
            stale_build = make(".store.sqlite.build-bbbb", old)
            fresh = make(".plx-projection-cccc.work", time.time())
            other = make(".other.sqlite.build-dddd", old)
            live = make(".plx-projection-eeee.work", old)
            holder = sqlite3.connect(str(live), isolation_level=None)
            holder.execute("BEGIN IMMEDIATE")  # a build in progress holds the write lock
            os.utime(live, (old, old))
            removed = project.sweep_stale_build_files(d, "store.sqlite")
            holder.execute("ROLLBACK")
            holder.close()
            self.assertEqual(sorted(removed), sorted([stale_build.name, stale_work.name]))
            self.assertTrue(fresh.exists() and live.exists() and other.exists())
            self.assertFalse(stale_work.exists() or stale_build.exists())


class MemoryBoundTests(unittest.TestCase):
    """Python heap (tracemalloc) peak of a whole build at N and 10N objects.

    The in-memory build held every version, value and link: on this world its peak
    was 7.9 MiB at 600 objects and 75 MiB at 6,000 (linear). Streamed, the peak is set
    by the batch, the version cache and the report (about 1-2.5 MiB at both sizes), so
    10x the objects must not move it by more than a small margin. SQLite's own page
    caches are fixed by PRAGMA cache_size and are not on this heap."""

    BATCH = 100
    CACHE = 128

    def peak(self, rows):
        schema = compiled_schema()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(project, "_VERSION_CACHE", self.CACHE):
            sources = synthetic_captures.sources(schema, rows)
            gc.collect()
            tracemalloc.start()
            try:
                tracemalloc.reset_peak()
                report = build_store(Path(tmp) / "store.sqlite", schema, sources,
                                     normalizer=normalizer, clock=lambda: T2,
                                     batch_size=self.BATCH)
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
        return report["counts"], peak

    def test_peak_memory_stays_flat_when_objects_grow_tenfold(self):
        self.peak(20)   # warm-up: one-time allocations (imports, compiled patterns)
        small_counts, small = self.peak(100)
        large_counts, large = self.peak(1000)
        self.assertGreaterEqual(large_counts["objects"], 9 * small_counts["objects"])
        self.assertGreater(large_counts["links"], 9 * small_counts["links"])
        self.assertGreater(small_counts["objects"], 500)
        # Flat: 10x the objects adds at most half the small peak plus 1 MiB (garbage
        # collection timing), and stays under a fixed 6 MiB budget. The in-memory
        # build fails both by an order of magnitude.
        self.assertLess(large, 1.5 * small + 2 ** 20, (small, large))
        self.assertLess(large, 6 * 2 ** 20, (small, large))


if __name__ == "__main__":
    unittest.main()
