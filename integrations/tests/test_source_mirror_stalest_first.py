"""The Portal mirror syncs every relation the catalog serves, stalest first (coverage
audit 2026-10-30: 34 served relations had never been read while the cycle budget ran
out on the same alphabetical prefix). A relation never captured goes first, then the
one whose last capture is oldest; large fills still go last. So a cycle its budget cuts
is followed by one that starts where it stopped, and every served relation is read
within a few cycles at any cardinality. Fixtures: the timed fake Workbench API of
``test_source_mirror_batched_merge``. Evidence label: injected-transport."""

from __future__ import annotations

import unittest

from integrations.tests.test_source_mirror import OPS
from integrations.tests import test_source_mirror_batched_merge as merge
from integrations.tests.test_source_mirror_batched_merge import small_relation
from integrations.tests.test_source_mirror_sync import SyncTestCase


class StalestFirstTests(SyncTestCase):
    timed = merge.OrderingTests.timed

    def setUp(self):
        super().setUp()
        self.fake = merge.TimedFake()

    def test_a_cut_cycle_is_followed_by_one_starting_where_it_stopped(self):
        for total in (1, 10, 60):
            with self.subTest(relations=total):
                if total != 1:
                    self.doCleanups()
                    self.setUp()
                for n in range(total):
                    small_relation(self.fake, f"public.rel_{n:03d}")
                names = sorted(self.fake.catalog)   # every relation the catalog serves
                mirror = self.timed()
                seen: set[str] = set()
                cycles = 0
                while not set(names) <= seen:
                    cycles += 1
                    self.assertLessEqual(cycles, total + 2)
                    # Every served relation: no list named (the catalog is the list).
                    report = mirror.sync(OPS, budget_seconds=45)
                    order = [r["relation"] for r in report["runs"]]
                    unread = [n for n in order if n not in seen]
                    # Every relation not read yet goes before any one read before.
                    self.assertEqual(order[:len(unread)], unread)
                    seen |= {r["relation"] for r in report["runs"]
                             if r["outcome"] == "captured"}
                self.assertLessEqual(cycles, -(-len(names) // 4) + 1)

    def test_never_captured_relations_go_before_any_refresh(self):
        for name in ("public.rel_a", "public.rel_b"):
            small_relation(self.fake, name)
        mirror = self.timed()
        mirror.sync(OPS)
        small_relation(self.fake, "public.rel_z")   # newly served, sorts last by name
        report = mirror.sync(OPS)
        self.assertEqual(report["runs"][0]["relation"], "public.rel_z")


if __name__ == "__main__":
    unittest.main()
