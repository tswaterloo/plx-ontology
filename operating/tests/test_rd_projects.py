"""R&D and sample projects (``operating.rd_projects``) through the engine and the per-reader
inbox: grouping per customer, status, owner, idle alert, per-item access, the people
exclusion and cardinality. SYNTHETIC fixtures only; evidence label: isolated-fixture."""

from __future__ import annotations

from datetime import date, timedelta
import json
import time
import unittest
from unittest import mock

from operating import rd_projects as rd
from operating.tests.test_proactive_engine import AS_OF, OURS_OID, Engine, World
from operating.tests.test_proactive_threads import BOSS, OURS, OUTSIDE, mail, people

OTHER = "buyer@other-customer.example"


def day(offset: int) -> date:
    return AS_OF - timedelta(days=offset)


class Classify(unittest.TestCase):
    def test_kinds_and_non_rd_text(self):
        for text, want in (("Could we get a sample of the new blend?", "sample_request"),
                           ("Please send the COA for the lot.", "coa"),
                           ("We want to change the formulation.", "formulation_change"),
                           ("Stability testing results are due.", "stability_test"),
                           ("Our sampling plan for incoming lots", None),
                           ("Try our free sample today!", None),
                           ("Where is my order?", None), ("", None), (None, None)):
            with self.subTest(text=text):
                self.assertEqual(rd.classify(text), want)

    def test_idle_days_is_bounded(self):
        for raw, want in (("9", 9), ("0", 5), ("61", 5), ("x", 5), (None, 5)):
            self.assertEqual(rd.idle_days({} if raw is None else {rd.IDLE_ENV: raw}), want)


class Rule(Engine):
    def run_rules(self, w: World, **options) -> dict:
        options.setdefault("people", people())
        patcher = mock.patch.dict("os.environ", {rd.ALERT_ENV: "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.run_engine(w, only=[rd.RULE], **options)

    def customer(self, w: World, name: str = "a") -> str:
        cache = w.__dict__.setdefault("_cust", {})
        if name not in cache:
            cache[name] = w.obj("Customer", {"name": f"Synth customer {name}"})
        return cache[name]

    def thread(self, w: World, i: int, *, last: int, customer: str = "a", sender: str = OUTSIDE,
               ours_last: bool = False, body: str = "Could you send a sample of the new blend?",
               n_msgs: int = 2, link: bool = True, box: str = OURS_OID,
               first_ours: bool = False) -> list[str]:
        """The customer asks, we answer, alternating; the last message is ours or theirs."""
        roles = ["us" if (k % 2 == 0) == first_ours else "them" for k in range(n_msgs)]
        if (roles[-1] == "us") != ours_last:
            roles.append("us" if ours_last else "them")
        made = []
        for k, role in enumerate(roles):
            ours = role == "us"
            made.append(mail(
                w, day(last + (len(roles) - 1 - k)), sender=OURS if ours else sender,
                to=(sender,) if ours else (OURS,), subject=f"Sample {i}",
                body="Samples are on their way." if ours and k else body,
                conversation=f"rd-{i}", n=60_000 + i * 100 + k, mailbox=box))
        if link:
            for m_ in made:
                w.link("Email.sent_by_customer", m_, self.customer(w, customer))
        return made

    def lines(self, **read):
        return self.read(kinds=[rd.LINE], limit=5000, **read)["gaps"]

    def test_grouping_status_owner_idle_and_access(self):
        w = World()
        self.thread(w, 1, last=12)                                 # customer asked, idle
        self.thread(w, 2, last=2, n_msgs=2, ours_last=True)        # we wrote last, fresh
        self.thread(w, 3, last=12, customer="b", sender=OTHER)     # another customer
        self.thread(w, 4, last=12, link=False)                     # no link: its one domain
        self.thread(w, 6, last=12, link=False, sender="someone@gmail.com")   # personal mail
        self.thread(w, 7, last=12, first_ours=True)                # we asked: a supplier's
        mail(w, day(12), sender="news@mail.synth-shop.example", to=(OURS,),
             subject="Free sample", body="Try our free sample", conversation="rd-5", n=61_000)
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][rd.RULE], "complete")
        lines = self.lines()
        self.assertEqual(len(lines), 4)                             # 1, 2, 3, 4 (not 6 or 7)
        states = sorted(g["event"]["state"] for g in lines)
        self.assertEqual(states, ["waiting_on_customer", "waiting_on_us", "waiting_on_us",
                                  "waiting_on_us"])
        keys = {g["event"]["project"] for g in lines}
        self.assertEqual(len(keys), 4)                              # one per customer/thread
        idle = self.read(kinds=[rd.IDLE], limit=5000)["gaps"]
        self.assertEqual(len(idle), 3)                              # threads 1, 3 and 4
        party = [g["event"] for g in lines if g["event"]["customer"] is None]
        self.assertEqual([e["party"] for e in party], ["@synth-customer.example"])
        self.assertTrue(all(e["owner"] for e in (g["event"] for g in lines)))
        text = json.dumps(lines + idle)
        self.assertNotIn("new blend", text)                         # never a body
        self.assertNotIn("Try our free sample", text)
        # per-item access: a reader without the mailbox sees no line and no idle alert
        deny = lambda key: key != f"mail:{OURS_OID}"                # noqa: E731
        self.assertEqual(self.lines(may_read=deny), [])
        self.assertEqual(self.read(kinds=[rd.IDLE], limit=50, may_read=deny)["gaps"], [])

    def test_idle_alert_is_off_by_default(self):
        w = World()
        self.thread(w, 1, last=12)
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop(rd.ALERT_ENV, None)
            self.run_engine(w, only=[rd.RULE], people=people())
        self.assertEqual(len(self.lines()), 1)
        self.assertEqual(self.read(kinds=[rd.IDLE], limit=50)["gaps"], [])

    def test_threads_of_one_customer_and_item_merge_and_two_customers_do_not(self):
        w = World()
        item = w.obj("Product", {"code": "SYN-9"})
        for i in (1, 2):
            for m_ in self.thread(w, i, last=3 + i):
                w.link("Email.mentions_product", m_, item)
        a = self.thread(w, 3, last=3, customer="a")
        for m_ in a:
            w.link("Email.sent_by_customer", m_, self.customer(w, "b"))   # two customers
        self.run_rules(w)
        keys = [g["event"]["project"] for g in self.lines()]
        self.assertEqual(len(keys), 2)                  # thread 3 has two customers: dropped
        self.assertEqual(len(set(keys)), 1)             # threads 1 and 2: one project

    def test_excluded_person_thread_is_not_judged_and_guard_down_blocks(self):
        w = World()
        self.thread(w, 1, last=12)
        mail(w, day(11), sender=OUTSIDE, to=(OURS,), cc=(BOSS,), subject="Sample 2",
             body="Please send a sample.", conversation="rd-2", n=62_000)
        mail(w, day(10), sender=OURS, to=(OUTSIDE,), subject="Sample 2", body="On its way.",
             conversation="rd-2", n=62_001)
        summary = self.run_rules(w)
        self.assertEqual(summary["not_evaluated"][rd.RULE].get("participants_not_cleared"), 1)
        self.assertEqual(len(self.lines()), 1)
        down = self.run_rules(w, people=people(unavailable=True))
        self.assertNotEqual(down["detectors"][rd.RULE], "complete")

    def test_cardinality_one_ten_sixty(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World()
                for i in range(n):
                    self.thread(w, i, last=12, customer=f"c{i}")
                t0 = time.monotonic()
                self.run_rules(w)
                self.assertEqual(len(self.lines()), n)
                self.assertEqual(len({g["event"]["project"] for g in self.lines()}), n)
                self.assertEqual(len(self.read(kinds=[rd.IDLE], limit=5000)["gaps"]), n)
                self.assertLess(time.monotonic() - t0, 120)


class Board(unittest.TestCase):
    """``board`` on synthetic trimmed alert lists."""

    def line(self, i, *, project="p1", customer="Customer:1", items=(), state="waiting_on_us",
             last="2026-11-01", days=5, owner="oid-1"):
        return {"id": f"g{i}", "kind": rd.LINE, "state": "open", "citations": [f"l1:{i}"],
                "event": {"project": project, "kind": "sample_request", "state": state,
                          "days": days, "last_on": last, "messages_n": 2, "customer": customer,
                          "items": list(items), "owner": owner, "contact": "buyer@x.example",
                          "messages": [{"id": f"Email:{i}", "subject": "Sample"}]}}

    def allow(self, *open_):
        return lambda handles: {h: (None if h in open_ else {"reason": "no"}) for h in handles}

    def test_record_checks_per_item_fail_closed_and_no_hidden_count(self):
        gaps = [self.line(1), self.line(2, items=["Product:9"], last="2026-11-05", days=1)]
        full = rd.board(gaps, date(2026, 11, 8), decide=self.allow("Customer:1", "Product:9"))
        self.assertEqual(full["projects"][0]["last_on"], "2026-11-05")
        partial = rd.board(gaps, date(2026, 11, 8), decide=self.allow("Customer:1"))
        (project,) = partial["projects"]
        # the hidden line moves no figure: status, last activity and days from line 1 alone
        self.assertEqual((project["last_on"], project["days"], len(project["lines"])),
                         ("2026-11-01", 5, 1))
        self.assertNotIn("Product:9", json.dumps(partial))
        self.assertNotIn("withheld", json.dumps(partial))
        none = rd.board(gaps, date(2026, 11, 8), decide=self.allow())
        self.assertEqual((none["projects"], none["total"]), ([], 0))
        broken = rd.board(gaps, date(2026, 11, 8), decide=mock.Mock(side_effect=RuntimeError))
        self.assertEqual(broken["projects"], [])

    def test_unknown_shapes_and_resolved_lines_are_not_shown(self):
        bad = self.line(1)
        bad["event"]["state"] = "mystery"
        gone = self.line(2)
        gone["state"] = "resolved"
        other = {**self.line(3), "kind": "something_else"}
        out = rd.board([bad, gone, other, {"kind": rd.LINE, "event": "x"}], date(2026, 11, 8),
                       decide=self.allow("Customer:1"))
        self.assertEqual(out["projects"], [])

    def test_idle_waiting_on_and_owner_and_sorting(self):
        gaps = [self.line(1, project="a", days=2), self.line(2, project="b", days=9),
                self.line(3, project="c", days=9, state="closed"),
                self.line(4, project="d", days=9, state="waiting_on_customer", owner="oid-2")]
        out = rd.board(gaps, date(2026, 11, 8), decide=self.allow("Customer:1"),
                       me=["OID-2"], idle_after=5)
        by = {p["id"]: p for p in out["projects"]}
        self.assertEqual((by["a"]["idle"], by["b"]["idle"], by["c"]["idle"]),
                         (False, True, False))
        self.assertEqual((by["b"]["waiting_on"], by["d"]["waiting_on"], by["c"]["waiting_on"]),
                         ("us", "customer", None))
        self.assertTrue(by["d"]["owner_is_you"])
        self.assertFalse(by["b"]["owner_is_you"])
        self.assertEqual(out["idle"], 2)
        self.assertEqual(out["projects"][-1]["id"], "c")      # closed last

    def test_status_draft_carries_no_commercial_terms_and_is_not_sent(self):
        project = {"kind": "sample_request", "status": "waiting_on_us", "last_on": "2026-11-01",
                   "contact": "buyer@x.example"}
        draft = rd.status_draft(project, date(2026, 11, 8))
        self.assertFalse(draft["sent"])
        self.assertEqual(draft["to"], [{"address": "buyer@x.example"}])
        text = draft["subject"] + draft["body"]
        for marker in ("$", "USD", "price", "quote", "discount", "PO"):
            self.assertNotIn(marker, text)
        self.assertEqual(rd.status_draft({**project, "contact": None}, date(2026, 11, 8))["to"],
                         [])


if __name__ == "__main__":
    unittest.main()
