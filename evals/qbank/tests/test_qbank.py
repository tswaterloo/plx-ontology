"""Question bank: grading rules, the TUNE/HOLDOUT split, stratified sampling, fail-closed
generation and the miner's question detection and reply-fact rule. No database: truth reads
are replaced by canned rows."""

from __future__ import annotations

import datetime as dt
import json
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evals.qbank import bank, db, mine, runner
from evals.qbank.core import Family, num, spec, split_of, stratified
from evals.qbank.grade import grade


def item(**g):
    return {"grading": spec("rule", **g)}


class GradeTest(unittest.TestCase):
    def test_numbers_dates_currency_names(self):
        it = item(numbers=[num("12345.67", "owed", currency="USD")])
        self.assertTrue(grade(it, {"text": "They owe US$12,345.67."})[0])
        self.assertTrue(grade(it, {"text": "About 12.3k USD is open."})[0])
        self.assertFalse(grade(it, {"text": "They owe 12,345.67 CAD."})[0], "a contradicting currency fails")
        self.assertFalse(grade(it, {"text": "They owe 11,000 USD."})[0])
        count = item(numbers=[num(15, "orders")])
        self.assertFalse(grade(count, {"text": "Orders placed Nov 9-15, 2026: several."})[0], "a date's digits are no figure")
        self.assertFalse(grade(count, {"text": "There are 16 orders."})[0], "small counts are exact")
        names = item(names=[["ACME-1", "Widget Blue"], "OR100"], min_names=1)
        self.assertTrue(grade(names, {"text": "It holds the widget blue item."})[0])
        self.assertFalse(grade(names, {"text": "It holds a red gadget."})[0])
        dates = item(dates=["2026-11-03"], date_tol_days=1)
        for said in ("It was on 2026-11-03.", "Placed November 4, 2026.", "3 Nov 2026", "on 11/03/2026"):
            self.assertTrue(grade(dates, {"text": said})[0], said)
        self.assertFalse(grade(dates, {"text": "Placed November 6, 2026."})[0])

    def test_cannot_zero_and_states(self):
        cant = item(cannot=True)
        self.assertTrue(grade(cant, {"text": "I couldn't find any order with that number."})[0])
        self.assertTrue(grade(cant, {"text": "...", "outcome": "not_found"})[0])
        self.assertFalse(grade(cant, {"text": "Order OR9 is closed and shipped to the customer."})[0])
        zero = {"grading": spec("none", numbers=[num(0, "owed")], zero_ok=True)}
        self.assertTrue(grade(zero, {"text": "They have no open balance."})[0])
        self.assertTrue(grade(zero, {"text": "I found no supplier invoices against that PO."})[0], "a zero truth: 'none found' is right")
        self.assertFalse(grade(zero, {"text": "They owe 4,210.00 USD."})[0])
        state = item(must_any=("closed", "shipped"))
        self.assertTrue(grade(state, {"text": "That order shipped last week."})[0])
        self.assertFalse(grade(state, {"text": "It is still being picked."})[0])
        self.assertFalse(grade(item(numbers=[num(3, "n")]), None)[0])


class SplitAndSampleTest(unittest.TestCase):
    def test_split_is_by_instance_and_holdout_is_guarded(self):
        ids = [f"fam:{i}" for i in range(4000)]
        share = sum(split_of(i) == "holdout" for i in ids) / len(ids)
        self.assertTrue(0.17 < share < 0.23, share)
        self.assertEqual([split_of(i) for i in ids[:50]], [split_of(i) for i in ids[:50]])
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"QBANK_HOLDOUT": ""}):
            with self.assertRaises(SystemExit):
                runner.eligible("holdout", Path(d))
            with self.assertRaises(SystemExit):
                runner.eligible("all", Path(d))
            with mock.patch.dict(os.environ, {"QBANK_REPLICA": ""}), self.assertRaises(SystemExit):
                runner.main(["run", "--n", "1", "--out", d, "--api-args", "x", "--mint", "x", "--link", "x", "--conc", "6"])

    def test_sample_covers_every_stratum_without_loss_or_duplicates(self):
        rng = random.Random(0)
        items = [{"id": f"i{k}", "system": rng.choice("abcdefg") if k > 5 else "rare", "kind": rng.choice(("lookup", "count", "trap"))}
                 for k in range(3000)]
        strata = {(i["system"], i["kind"]) for i in items}
        for n in (1, 10, 60, 300):
            got = runner.sample(items, n, seed=3)
            self.assertEqual(len(got), n)
            self.assertEqual(len({i["id"] for i in got}), len(got))
            if n >= len(strata):
                self.assertEqual({(i["system"], i["kind"]) for i in got}, strata, "a rare stratum is never dropped")
        self.assertEqual(len(runner.sample(items[:20], 300, 1)), 20)
        rows = [{"k": i, "w": i} for i in range(300)]
        picked = stratified(rows, 30, random.Random(1))
        self.assertEqual(len(picked), 30)
        self.assertGreaterEqual(sum(r["w"] < 100 for r in picked), 9, "the low-activity third is sampled")


class GenerateFailsClosedTest(unittest.TestCase):
    def fam(self, asks=("Status of {order_number}?",)):
        return Family("t_status", "sales", "SalesOrder", "lookup", "POOL", "TRUTH", asks,
                      lambda rows, p: spec("s", must_any=(rows[0]["status"],)) if rows else None)

    def run_generate(self, fam, truth):
        def query(sql, params=None):
            return [{"order_number": "SO1", "w": 1}, {"order_number": "SO2", "w": 2}] if sql == "POOL" else truth(params)
        with mock.patch.object(bank, "families", return_value=[fam]), mock.patch.object(db, "query", side_effect=query):
            return bank.generate(1, 1.0, None, 2, log=lambda *_: None)

    def test_items_truth_and_stale(self):
        items = self.run_generate(self.fam(), lambda p: [{"status": "open"}] if p["order_number"] == "SO1" else [])
        by = {i["params"]["order_number"]: i for i in items}
        self.assertEqual(by["SO1"]["grading"]["must_any"], ["open"])
        self.assertTrue(by["SO2"]["stale"], "a truth that does not grade is never sampled")
        self.assertNotIn("open", json.dumps(self.fam().asks))
        # a refresh reads the family's current truth SQL, and a grade that breaks marks the item stale
        with tempfile.TemporaryDirectory() as d:
            bank.save(items, Path(d))
            fam2 = Family("t_status", "sales", "SalesOrder", "lookup", "POOL", "TRUTH2", self.fam().asks, lambda rows, p: rows[0]["gone"])
            seen = []
            with mock.patch.object(bank, "families", return_value=[fam2]), \
                    mock.patch.object(db, "query", side_effect=lambda sql, p=None: seen.append(sql) or [{"status": "open"}]):
                bank.refresh(2, Path(d), log=lambda *_: None)
            self.assertEqual(set(seen), {"TRUTH2"})
            self.assertTrue(all(i["stale"] for i in bank.all_items(Path(d))))

    def test_broken_family_and_lost_server_fail_closed(self):
        with self.assertRaises(bank.FamilyErrors):
            self.run_generate(self.fam(("Status of {no_such_slot}?",)), lambda p: [{"status": "open"}])
        def lost(p):
            raise db.TruthError("connect: OperationalError: no slot")
        with self.assertRaises(bank.FamilyErrors):
            self.run_generate(self.fam(), lost)
        with self.assertRaises(SystemExit):
            bank.generate(1, 1.0, {"no_such_family"}, 1, log=lambda *_: None)


class MineTest(unittest.TestCase):
    def test_question_detection_by_form(self):
        for q in ("Has the order shipped yet?", "can you check the PO status", "When is it due", "Is there stock left"):
            self.assertTrue(mine.is_question(q), q)
        for s in ("Thanks, shipped it yesterday.", "I will check tomorrow", "We need 40 cases.", "ok?"):
            self.assertFalse(mine.is_question(s), s)

    def test_reply_fact_picks_the_field_never_the_reply_value(self):
        rec = {"status": "closed", "customer": "Northwind Traders", "order_date": dt.date(2026, 10, 2),
               "required_date": dt.date(2026, 10, 20), "value": 1250, "value_currency": "USD"}
        self.assertEqual(mine.reply_matches("order", rec, "It should arrive Oct 21st", False), {"required_date"})
        self.assertEqual(mine.reply_matches("order", rec, "It is $1,250 for Northwind Traders", False), {"value", "customer"})
        self.assertEqual(mine.reply_matches("order", rec, "it's closed", False), set(), "a state word counts only for a status question")
        self.assertEqual(mine.reply_matches("order", rec, "yes, closed and shipped", True), {"status"})
        self.assertIn("%(key)s", mine.truth_sql("order", "required_date"))
        self.assertNotIn("2026", mine.truth_sql("order", "required_date"))


if __name__ == "__main__":
    unittest.main()
