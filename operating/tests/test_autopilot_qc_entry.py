"""QC entries prepared from the batch events (``operating.autopilot_qc_entry``): a batch made
and not tested gets its test entry, a batch tested and not released its release check, each
for the person who entered the product's earlier results; nothing written. Synthetic records
only."""
from __future__ import annotations

from datetime import date
import unittest

from operating import autopilot as ap
from operating import autopilot_handlers
from operating import autopilot_qc_entry as qc
from operating.tests.test_autopilot_replay import World
from operating.tests.test_proactive_engine import OURS_OID
from operating.tests.test_proactive_threads import people

DAY = date(2026, 10, 21)


class QcEntry(World):
    def batch(self, code: str, product: str, made: str, *, tested: str | None = None,
              released: str | None = None, by: str = "", ph: str = "") -> str:
        w = self.w
        b = w.obj("Batch", {"batch_code": code, "realized_date": made, "tested_by": by,
                            "checked_by": by if released else "", "qc_ph_24h": ph,
                            "qc_viscosity_24h": ph and "1200", "qc_tpc_yeast": ""})
        w.link("Batch.makes_bulk", b, product)
        self.events.append(("Batch.manufactured", b, f"{made}T00:00:00Z"))
        if tested:
            self.events.append(("Batch.tested", b, f"{tested}T00:00:00Z"))
        if released:
            self.events.append(("Batch.released", b, f"{released}T00:00:00Z"))
        return b

    def test_made_not_tested_gets_a_test_entry_for_the_product_s_tester(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                prod = self.w.obj("Product", {"code": "SYN-BULK", "name": "Syn bulk"})
                self.batch("B-old", prod, "2026-10-10", tested="2026-10-11", by="Ops", ph="6.1")
                due = [self.batch(f"B-{i}", prod, "2026-10-20") for i in range(n)]
                self.batch("B-done", prod, "2026-10-20", tested="2026-10-21", by="Ops", ph="6.2")
                self.batch("B-stale", prod, "2026-09-01")            # outside the window
                st = self.store()
                events = qc.due_events(st, ap.load_registry(), DAY, people())
                tests = [e for e in events if e["fields"]["entry"] == "test"]
                self.assertEqual(sorted(e["object_id"] for e in tests), sorted(due))
                self.assertEqual(len({e["event_id"] for e in events}), len(events))
                self.assertTrue(all(e["owner_hint"] == OURS_OID for e in tests))
                ctx = autopilot_handlers.Ctx(st, DAY, float("inf"), ())
                work = qc.h_qc_entry(ctx, tests[0])
                self.assertTrue(work["dry_run"])
                self.assertEqual(work["entry"]["kind"], "qc_results")
                # only the tests this product's earlier batches have results for, newest first
                self.assertEqual(work["entry"]["tests"], [
                    {"test": "ph 24h", "recent": ["6.2", "6.1"]},
                    {"test": "viscosity 24h", "recent": ["1200", "1200"]}])

    def test_tested_not_released_gets_a_release_check_and_released_none(self):
        prod = self.w.obj("Product", {"code": "SYN-BULK", "name": "Syn bulk"})
        a = self.batch("B-a", prod, "2026-10-19", tested="2026-10-20", by="Ops", ph="6.3")
        self.batch("B-b", prod, "2026-10-19", tested="2026-10-20", released="2026-10-21", by="Ops", ph="6.4")
        st = self.store()
        events = qc.due_events(st, ap.load_registry(), DAY, people())
        rel = [e for e in events if e["fields"]["entry"] == "release"]
        self.assertEqual([e["object_id"] for e in rel], [a])
        work = qc.h_qc_entry(autopilot_handlers.Ctx(st, DAY, float("inf"), ()), rel[0])
        self.assertEqual(work["entry"]["kind"], "qc_release")
        self.assertEqual(work["entry"]["results"][0]["value"], "6.3")

    def test_a_product_with_no_earlier_results_has_no_known_tests(self):
        prod = self.w.obj("Product", {"code": "SYN-NEW", "name": "New"})
        b = self.batch("B-new", prod, "2026-10-20")
        st = self.store()
        (event,) = qc.due_events(st, ap.load_registry(), DAY, people())
        work = qc.h_qc_entry(autopilot_handlers.Ctx(st, DAY, float("inf"), ()), event)
        self.assertEqual(work, {"skip": "tests_unknown"})

    def test_result_columns_come_from_the_schema(self):
        cols = qc.result_columns()
        self.assertIn("qc_ph_24h", cols)
        self.assertTrue(all(c.startswith("qc_") for c in cols))

    def test_no_earlier_tester_or_an_unavailable_exclusion_leaves_no_owner(self):
        prod = self.w.obj("Product", {"code": "SYN-BULK", "name": "Syn bulk"})
        self.batch("B-1", prod, "2026-10-20")
        self.batch("B-old", prod, "2026-10-10", tested="2026-10-11", by="Ops", ph="6.1")
        st = self.store()
        got = qc.due_events(st, ap.load_registry(), DAY, people(unavailable=True))
        self.assertEqual([e["owner_hint"] for e in got], [None])
        self.setUp()
        prod = self.w.obj("Product", {"code": "SYN-BULK", "name": "Syn bulk"})
        self.batch("B-1", prod, "2026-10-20")
        got = qc.due_events(self.store(), ap.load_registry(), DAY, people())
        self.assertEqual([e["owner_hint"] for e in got], [None])


if __name__ == "__main__":
    unittest.main()
