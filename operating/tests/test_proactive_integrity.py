"""Integrity alerts (``operating.proactive_integrity``) through the engine, the per-reader inbox
and its people exclusion: manual stock write-downs, open orders with blocked accounts,
ledger batches that failed to post, recurring tasks that missed a cycle.

SYNTHETIC fixtures only (``World`` stores, synthetic codes); evidence label: isolated-fixture.
"""

from __future__ import annotations

from datetime import timedelta
import json

from operating import proactive_integrity as pi
from operating.tests.test_proactive_engine import (AS_OF, EXCLUDED_OID, OURS_OID, Engine,
                                                   World, people)
from operating.tests.test_proactive_chasers import order as sales_order

FIN = '["finance"]'
LEDGER = "bc.item_ledger_entries"


def entry(w: World, item: str, qty: str, kind: str, document: str, *,
          audience: str = "bc:finance") -> str:
    """One BC item ledger entry, its enum word XML-escaped the way the API states it."""
    return w.obj("ItemLedgerEntry", {
        "entry_type": kind.replace(" ", "_x0020_").replace(".", "_x002E_"),
        "document_number": document, "item_number": item, "quantity": qty,
        "cost_amount_actual": "10"}, binding=LEDGER, audience=audience, markings=FIN)


def sage_day(offset: int) -> str:
    return (AS_OF - timedelta(days=offset)).strftime("%Y%m%d")


def day(offset: int) -> str:
    return (AS_OF - timedelta(days=offset)).isoformat() + "T12:00:00Z"


def task(w: World, title: str, due: int, done: int | None, *,
         assignee: str = OURS_OID, created: int | None = None) -> str:
    values = {"title": title, "plan_id": "SYNTH-PLAN", "plan_title": "Synthetic checks",
              "due_at": day(due), "assignees": json.dumps([assignee])}
    if done is not None:
        values["completed_at"] = day(done)
    if created is not None:
        values["created_at"] = day(created)
    return w.obj("PlannerTask", values, audience="meetings:plan:synth")


class IntegrityAlerts(Engine):

    def run_rules(self, w: World, **options) -> dict:
        return self.run_engine(w, only=list(pi.RULES), **options)

    def kinds(self, kind: str, **read) -> list[dict]:
        return self.read(kinds=[kind], limit=1000, **read)["gaps"]

    def test_manual_write_downs_are_raised_and_loads_and_conversions_are_not(self):
        w = World()
        for i in range(25):                       # the opening-balance load: never raised
            entry(w, f"SYNTH-I{i}", "100", "Positive Adjmt.", "SYN-OPEN-1")
        for doc in ("SYN-BAT-1", "SYN-BAT-2", "SYN-BAT-3"):   # production via the journal
            entry(w, "SYNTH-I1", "-5", "Negative Adjmt.", doc)
            entry(w, "SYNTH-I2", "4", "Positive Adjmt.", doc)
        for doc in ("SYN-ADJ-1", "SYN-ADJ-2", "SYN-ADJ-3"):   # repeated by hand
            entry(w, "SYNTH-I3", "-2", "Negative Adjmt.", doc)
        entry(w, "SYNTH-I4", "-60", "Negative Adjmt.", "SYN-ADJ-4")   # large: 60 of 100
        entry(w, "SYNTH-I5", "-1", "Negative Adjmt.", "SYN-ADJ-5")    # one small: no
        entry(w, "SYNTH-I5", "3", "Odd_x0020_Type", "SYN-ADJ-6")      # unknown type
        entry(w, "SYNTH-I6", "-80", "Negative Adjmt.", "SYN-ADJ-7")   # large, but reversed
        entry(w, "SYNTH-I6", "80", "Positive Adjmt.", "SYN-ADJ-8")
        entry(w, "SYNTH-I6", "100", "Purchase", "SYN-RCPT-1")
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][pi.ADJUST_RULE], "complete")
        gaps = {g["summary"].split()[1]: g for g in self.kinds(pi.ADJUST_RULE)}
        self.assertEqual(set(gaps), {"SYNTH-I3", "SYNTH-I4"})
        self.assertEqual(gaps["SYNTH-I3"]["summary"],
                         "Item SYNTH-I3 was adjusted down by hand 3 times on 3 documents (6 "
                         "in its base unit, the largest 2% of all that came in; 0 adjusted "
                         "up by hand)")
        self.assertEqual(gaps["SYNTH-I4"]["follow_up"]["priority"], "high")
        self.assertEqual(summary["not_evaluated"][pi.ADJUST_RULE],
                         {"entry_type_unmapped": 1})
        # every entry is finance-marked: a reader without finance sees none of it
        self.assertEqual(self.kinds(pi.ADJUST_RULE, held=frozenset({"l5", "pii"})), [])

    def test_one_ten_and_sixty_items_each_raise_exactly_once(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                w = World()
                for i in range(n):
                    for doc in range(3):
                        entry(w, f"SYNTH-N{i}", "-1", "Negative Adjmt.", f"SYN-{i}-{doc}")
                self.run_rules(w)
                found = [g["summary"].split()[1] for g in self.kinds(pi.ADJUST_RULE)]
                self.assertEqual(sorted(found), sorted(f"SYNTH-N{i}" for i in range(n)))

    def test_open_orders_of_a_blocked_account_are_raised_for_a_finance_reader_only(self):
        w = World()
        blocked = w.obj("Customer", {"name": "SYNTH Blocked", "bc_blocked": "All",
                                     "bc_balance_due": "250.00", "currency": "CAD"},
                        binding="bc.customers", audience="bc:sales", markings=FIN)
        sales_order(w, blocked, "2026-11-30")
        sales_order(w, blocked, "2026-10-01", number="SYNTH-SO-2", status="Closed")
        fine = w.obj("Customer", {"name": "SYNTH Fine", "bc_blocked": "_x0020_"},
                     binding="bc.customers", audience="bc:sales")
        sales_order(w, fine, "2026-11-30", number="SYNTH-SO-3")
        odd = w.obj("Customer", {"name": "SYNTH Odd", "bc_blocked": "Sometimes"},
                    binding="bc.customers", audience="bc:sales")
        sales_order(w, odd, "2026-11-30", number="SYNTH-SO-4")
        paying = w.obj("Supplier", {"name": "SYNTH Pay", "bc_blocked": "Payment"},
                       binding="bc.vendors", audience="bc:purchasing")
        po = w.obj("PurchaseOrder", {"status": "Pending", "expected_date": "2026-11-30"})
        w.link("PurchaseOrder.ordered_from", po, paying)
        summary = self.run_rules(w)
        (gap,) = self.kinds(pi.BLOCKED_RULE)
        self.assertEqual(gap["object_id"], blocked)
        self.assertEqual(gap["summary"],
                         "Blocked customer SYNTH Blocked (all) has 1 open customer order")
        self.assertEqual(gap["event"]["amount"]["currency"], "CAD")
        self.assertEqual(summary["not_evaluated"][pi.BLOCKED_RULE], {"blocked_unmapped": 1})
        self.assertEqual(self.kinds(pi.BLOCKED_RULE, held=frozenset({"l5"})), [])
        self.assertEqual(self.kinds(pi.BLOCKED_RULE,
                                    may_read=lambda a: a != "api:orders"), [])

    def test_an_open_error_batch_is_raised_and_posted_or_unknown_ones_are_not(self):
        w = World()

        def batch(bid: str, stat: str) -> str:
            return w.obj("RelSageGlBatch", {"batchid": bid, "batchstat": stat},
                         binding="gen.sage.gl_batch", audience="api:sage.gl", markings=FIN)

        def error(bid: str, err: str, offset: int, text: str) -> None:
            w.obj("RelSageGlPerr", {"batchnbr": bid, "errbatch": err, "audtdate": sage_day(
                offset), "errdesc": text}, binding="gen.sage.gl_perr",
                audience="api:sage.gl", markings=FIN)
        for bid in ("000100", "000200", "000300", "000400"):
            batch(bid, "4")
        still = batch("000101", "1")
        batch("000201", "4")
        batch("000301", "9")
        batch("000401", "1")
        for _ in range(3):
            error("000100", "000101", 5, "Cannot post to a future year.")
        error("000200", "000201", 5, "Account 4110 is not defined in General Ledger.")
        error("000300", "000301", 5, "Cannot post to a future year.")
        error("000400", "000401", 400, "Cannot post to a future year.")   # long open
        summary = self.run_rules(w)
        (gap,) = self.kinds(pi.GL_RULE)
        self.assertEqual(gap["object_id"], still)
        self.assertEqual(gap["summary"],
                         "1 general-ledger batch still open with 3 entries that failed to "
                         "post, mostly \"cannot post to a future year.\" (last "
                         f"{(AS_OF - timedelta(days=5)).isoformat()})")
        self.assertEqual(summary["not_evaluated"][pi.GL_RULE], {"status_unmapped": 1})
        self.assertEqual(self.kinds(pi.GL_RULE, held=frozenset({"l5", "pii"})), [])

    def test_a_cycle_missed_is_raised_a_late_but_in_cycle_one_is_not_and_people_fail_closed(
            self):
        w = World()
        # weekly; the one due 28 days ago was done after the next fell due (21 days ago)
        task(w, "Synthetic scale calibration", 35, 34)
        missed = task(w, "Synthetic scale calibration", 28, 18)
        task(w, "Synthetic scale calibration", 21, 16)        # late, within its week
        task(w, "Synthetic scale calibration - 05-10-2026", 14, 13)   # dated title
        # another series whose occurrences are all within their cycle
        for due in (35, 28, 21, 14):
            task(w, "Synthetic sanitation", due, due - 3)
        # an excluded person's series is never judged
        for due in (35, 28, 21):
            task(w, "Synthetic hidden check", due, None, assignee=EXCLUDED_OID)
        summary = self.run_rules(w)
        (gap,) = self.kinds(pi.CYCLE_RULE)
        self.assertEqual(gap["object_id"], missed)
        self.assertEqual(gap["summary"],
                         "Recurring task Synthetic scale calibration in Synthetic checks "
                         "(every 7 days) missed 1 cycle in the last 90 days")
        self.assertEqual(summary["not_evaluated"][pi.CYCLE_RULE], {"person_not_cleared": 3})
        self.setUp()
        blocked = self.run_rules(w, people=people(unavailable=True))
        self.assertEqual(blocked["detectors"][pi.CYCLE_RULE], "blocked")
        self.assertEqual(self.kinds(pi.CYCLE_RULE), [])


    def test_a_replayed_day_reads_the_tasks_as_that_day_saw_them(self):
        # weekly; the one due 14 days ago was done 3 days AFTER the run's day: on the day it
        # was open past its next due, so the day sees a missed cycle; a series made of
        # tasks created after the day did not exist yet
        w = World()
        for due in (35, 28, 21):
            task(w, "Synthetic filter change", due, due - 1)
        late = task(w, "Synthetic filter change", 14, -3)
        for due in (35, 28, 21, 14):
            task(w, "Synthetic future series", due, None, created=-2)
        summary = self.run_rules(w)
        (gap,) = self.kinds(pi.CYCLE_RULE)
        self.assertEqual(gap["object_id"], late)
        self.assertEqual(summary["detectors"][pi.CYCLE_RULE], "complete")

if __name__ == "__main__":
    import unittest
    unittest.main()
