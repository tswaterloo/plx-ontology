"""Row 24 (11-15): each check stores how many records it had to judge and judged, and the
briefing shows the judged share per alert type; it never says "complete" while a check
left records unjudged. The store-wide counts reach only a reader shown every record the run
scanned; any other reader sees its own unjudged count, never the store's population."""
from __future__ import annotations

import sqlite3
import unittest

from operating import briefing as bf
from operating import proactive_engine as pe
from operating import proactive_engine_findings
from operating.tests.test_proactive_engine import Engine, World

EVERYTHING = bf.Routing(basis="fixture", everything=True)


def _inbox(**rules) -> dict:
    """A read where every rule ran to the end over the served data, with ``rules``'
    coverage entries (``read_inbox``'s shape) for the named ones."""
    return {"status": "read", "gaps": [], "complete": True,
            "detectors": {name: "complete" for name in pe.DETECTORS},
            "rules": [{"rule": name, "status": "complete", "reasons": [],
                       "not_evaluated_by_reason": {}, **entry}
                      for name, entry in rules.items()]}


def _type(out: dict, name: str) -> dict:
    return next(t for t in out["types"] if t["event_type"] == name)


class BriefingJudgedShare(unittest.TestCase):
    def build(self, inbox: dict) -> dict:
        return bf.build(inbox, routing=EVERYTHING, hidden=None, last_visit=None)

    def test_a_check_that_left_records_unjudged_is_partial_with_its_share(self):
        out = self.build(_inbox(late_customer_order={
            "not_evaluated": 3, "checked": 10, "judged": 7}))
        late = _type(out, "customer_order_late")
        self.assertEqual(late["status"], "partial")
        self.assertIn("judged 7 of 10 (70%), 3 could not be judged", late["words"])
        self.assertEqual((late["checked"], late["judged"], late["judged_share"]), (10, 7, 0.7))
        self.assertFalse(out["complete"])
        # the emailed digest names the unfinished check too
        self.assertIn("judged 7 of 10 (70%)", bf.digest_text(out, first_name="Ann"))

    def test_without_the_counts_the_reader_still_never_reads_complete(self):
        # a reader not shown every record: its own unjudged count, no share
        out = self.build(_inbox(late_customer_order={"not_evaluated": 3, "checked": None,
                                                     "judged": None}))
        late = _type(out, "customer_order_late")
        self.assertEqual(late["status"], "partial")
        self.assertIn("checked; 3 records could not be judged", late["words"])
        self.assertIsNone(late["judged_share"])
        self.assertFalse(out["complete"])
        # an unknown skip count is never read as none skipped
        out = self.build(_inbox(late_customer_order={"not_evaluated": None}))
        self.assertEqual(_type(out, "customer_order_late")["status"], "partial")
        self.assertIn("how many is not known", _type(out, "customer_order_late")["words"])
        self.assertFalse(out["complete"])

    def test_every_record_judged_reads_complete_with_its_count(self):
        out = self.build(_inbox(late_customer_order={"not_evaluated": 0, "checked": 10,
                                                     "judged": 10}))
        late = _type(out, "customer_order_late")
        self.assertEqual(late["status"], "complete")
        self.assertIn("checked all 10", late["words"])
        self.assertEqual(late["judged_share"], 1.0)
        self.assertTrue(out["complete"])

    def test_one_left_of_many_never_rounds_to_all(self):
        out = self.build(_inbox(late_customer_order={"not_evaluated": 1, "checked": 1000,
                                                     "judged": 999}))
        late = _type(out, "customer_order_late")
        self.assertIn("judged 999 of 1,000 (99%)", late["words"])
        self.assertLess(late["judged_share"], 1.0)
        self.assertFalse(out["complete"])

    def test_counts_that_do_not_add_up_or_of_an_unknown_shape_give_no_share(self):
        for bad in ({"checked": 5, "judged": 9}, {"checked": "10", "judged": 7},
                    {"checked": True, "judged": 1}, {"checked": 10, "judged": -1}):
            with self.subTest(bad=bad):
                out = self.build(_inbox(late_customer_order={"not_evaluated": 2, **bad}))
                late = _type(out, "customer_order_late")
                self.assertIsNone(late["judged_share"])
                self.assertEqual(late["status"], "partial")
                self.assertFalse(out["complete"])
        # an unreadable skip count fails closed too
        out = self.build(_inbox(late_customer_order={"not_evaluated": "many"}))
        self.assertEqual(_type(out, "customer_order_late")["status"], "partial")

    def test_a_type_of_two_checks_shares_over_both_and_only_when_both_count(self):
        both = _inbox(po_receipt_quantity_mismatch={"not_evaluated": 0, "checked": 30,
                                                    "judged": 30},
                      invoice_quantity_mismatch={"not_evaluated": 10, "checked": 10,
                                                 "judged": 0})
        mismatch = _type(self.build(both), "quantity_mismatch")
        self.assertEqual((mismatch["checked"], mismatch["judged"], mismatch["judged_share"]),
                         (40, 30, 0.75))
        self.assertEqual(mismatch["status"], "partial")
        one = _inbox(po_receipt_quantity_mismatch={"not_evaluated": 0, "checked": 30,
                                                   "judged": 30})
        self.assertIsNone(_type(self.build(one), "quantity_mismatch")["judged_share"])


class EngineStoresWhatItChecked(unittest.TestCase):
    setUp = Engine.setUp
    run_engine = Engine.run_engine
    read = Engine.read

    def rule(self, name: str, **read) -> dict:
        return next(r for r in self.read(**read)["rules"] if r["rule"] == name)

    def test_a_run_stores_the_count_per_check_at_every_size(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                w = World()
                for i in range(n):
                    w.obj("ForwarderQuote", {"valid_until": "2026-01-31" if i % 2 else
                                             "2999-01-01"})
                self.run_engine(w)
                with sqlite3.connect(self.inbox) as conn:
                    self.assertEqual(conn.execute(
                        "SELECT checked, judged FROM detector_checked WHERE detector = "
                        "'expired_quote'").fetchone(), (n, n))
                rule = self.rule("expired_quote")
                self.assertEqual((rule["checked"], rule["judged"]), (n, n))
                # a check that does not count its records says so (NULL), never 0
                late = self.rule("late_or_unconfirmed_po")
                self.assertIsNone(late["checked"])

    def test_a_reader_not_shown_every_record_gets_no_store_wide_count(self):
        w = World()
        w.obj("ForwarderQuote", {"valid_until": "2026-01-31"})
        w.obj("ForwarderQuote", {"valid_until": "2026-01-31"}, audience="api:hidden")
        self.run_engine(w)
        self.assertEqual(self.rule("expired_quote")["checked"], 2)
        rule = self.rule("expired_quote", may_read=lambda key: key != "api:hidden")
        self.assertEqual((rule["checked"], rule["judged"]), (None, None))
        # a reader short of a marking the store holds is not shown every record either
        w2 = World()
        w2.obj("ForwarderQuote", {"valid_until": "2026-01-31"}, markings='["finance"]')
        self.setUp()
        self.run_engine(w2)
        self.assertIsNone(self.rule("expired_quote", held=frozenset())["checked"])
        self.assertEqual(self.rule("expired_quote")["checked"], 1)

    def test_a_count_below_what_was_left_unjudged_or_a_cut_short_run_is_not_kept(self):
        self.assertEqual(proactive_engine_findings._checked({"status": "complete", "checked": 2,
                                      "not_evaluated": {"x": 3}}), (None, None))
        self.assertEqual(proactive_engine_findings._checked({"status": "truncated", "checked": 9,
                                      "not_evaluated": {}}), (None, None))
        self.assertEqual(proactive_engine_findings._checked({"status": "complete", "checked": 9,
                                      "not_evaluated": {"x": 2}}), (9, 7))
        self.assertEqual(proactive_engine_findings._checked({"status": "complete", "not_evaluated": {"x": 1},
                                      "coverage": {"total": 9, "evaluated": 6}}), (9, 6))
        self.assertEqual(proactive_engine_findings._checked({"status": "complete", "not_evaluated": {"x": 4},
                                      "coverage": {"total": 9, "evaluated": 6}}), (None, None))


if __name__ == "__main__":
    unittest.main()
