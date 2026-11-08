"""Customers chasing order status and quality holds aging (``operating.proactive_chasers``)
through the engine and the per-reader inbox.

SYNTHETIC fixtures only (``World`` stores, synthetic handles and addresses); evidence label:
isolated-fixture.
"""

from __future__ import annotations

from datetime import timedelta
import json
import sqlite3
import time
import unittest
import zlib

from operating import briefing
from operating import proactive_chasers as pc
from operating import proactive_engine as pe
from operating.tests.test_proactive_engine import (AS_OF, EXCLUDED_OID, OURS_OID, Engine,
                                                   World, handle, people)

RULES = [pc.CHASE, pc.HOLDS]
KINDS = (pc.CHASING, pc.TELL_FIRST, pc.HOLD_AGING, pc.REJECT_TREND)
CUSTOMER_MAIL = "buyer@synth-customer.example"
MAILBOX = f"mail:{OURS_OID}"
OTHER_BOX = "mail:00000000-0000-4000-8000-0000000000b2"
GUEST_OID = "00000000-0000-4000-8000-0000000000c3"   # outside the tenant's directory
BODY = "Hi, where is our order SYNTH-SO-1? Still waiting on it, secret pricing 777."


def day(offset: int) -> str:
    return (AS_OF - timedelta(days=offset)).isoformat()


def customer(w: World, address: str = CUSTOMER_MAIL) -> str:
    return w.obj("Customer", {"name": "SYNTH Customer", "email": address},
                 audience="api:customers")


def order(w: World, who: str, required: str, *, number: str = "SYNTH-SO-1",
          status: str = "Open", ordered: str | None = None) -> str:
    so = w.obj("SalesOrder", {"order_number": number, "status": status,
                              "required_date": required,
                              **({"order_date": ordered} if ordered else {})},
               binding="portal.app_sales_order", audience="api:orders")
    w.link("SalesOrder.ordered_by", so, who)
    item = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
    line = w.obj("SalesOrderLine", {"ordered_quantity": ("10", "ea"),
                                    "shipped_quantity": ("4", "ea"),
                                    "outstanding_quantity": ("6", "ea")},
                 audience="api:orders")
    w.link("SalesOrderLine.line_of", line, so)
    w.link("SalesOrderLine.for_item", line, item)
    return so


def mail(w: World, so: str, sent: str, *, sender: str = CUSTOMER_MAIL, body: str = BODY,
         subject: str = "Order SYNTH-SO-1 " + "x" * 80, mailbox: str = MAILBOX,
         tier: str = "verified", n: int | None = None) -> str:
    oid = w.obj("Email", {"subject": subject, "sender": sender,
                          "sent_at": f"{sent}T15:00:00Z", "body_preview": body,
                          "internet_message_id": f"<m{n if n is not None else w.n}@synth>"},
                binding="graph_mail.messages", audience=mailbox)
    w.link("Email.mentions_sales_order", oid, so, tier)
    return oid


class TextWorld(World):
    """A world whose Teams messages carry their body in the store's text index."""

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[tuple[str, str]] = []

    def teams(self, so: str, sent: str, chat: str, sender: str, body: str = BODY) -> str:
        oid = self.obj("TeamsChatMessage", {"created_at": f"{sent}T15:00:00Z",
                                            "from": json.dumps({"id": sender,
                                                                "kind": "user"})},
                       binding="gen.teams.chat_messages", audience=f"teams:chat:{chat}")
        self.link("TeamsChatMessage.mentions_sales_order", oid, so)
        self.texts.append((oid, body))
        return oid

    def build(self, path):
        built = super().build(path)
        conn = sqlite3.connect(built)
        with conn:
            for oid, body in self.texts:
                handle = conn.execute("SELECT l1_handle FROM object_versions WHERE "
                                      "object_id = ?", (oid,)).fetchone()[0]
                cur = conn.execute(
                    "INSERT INTO text_fields (object_id, version_handle, citation, field_path,"
                    " document_kind, sender_class, markings, text_digest) VALUES "
                    "(?, ?, ?, 'row.body_text', 'teams_message', 'unknown', '[]', 'd')",
                    (oid, handle, handle))
                conn.execute("INSERT INTO text_chunks (field_id, start, body) VALUES "
                             "(?, 0, ?)", (cur.lastrowid, zlib.compress(body.encode())))
        conn.close()
        return built


def chat_people(members: dict):
    from integrations.people_exclusion import PeopleExclusion
    from operating.chunking.permissions import Exclusion

    return Exclusion(PeopleExclusion([EXCLUDED_OID]),
                     directory={"ops@synth-tenant.example": OURS_OID},
                     internal_domains=["synth-tenant.example"], chat_members=members)


def held_batch(w: World, *, ordered: str | None, status: str = "Allocated",
               quality: str = "On-Hold", code: str = "BC-SYNTH-1") -> tuple[str, str | None]:
    batch = w.obj("Batch", {"quality_status": quality, "status": status, "batch_code": code,
                            "planned_quantity": ("600", "kg")},
                  binding="portal.fm_batch", audience="fm:batches")
    w.link("Batch.makes_bulk", batch, w.obj("Product", {"code": "SYNTH-BULK"},
                                            audience="api:items"))
    if ordered is None:
        return batch, None
    so = w.obj("SalesOrder", {"order_number": f"SO-{code}", "status": "Open",
                              "order_date": ordered}, binding="portal.app_sales_order",
               audience="api:orders")
    line = w.obj("SalesOrderLine", {"ordered_quantity": ("5", "ea")}, audience="api:orders")
    w.link("SalesOrderLine.line_of", line, so)
    alloc = w.obj("BatchAllocation", {}, audience="fm:batches")
    w.link("BatchAllocation.allocates", alloc, batch)
    w.link("BatchAllocation.for_order_line", alloc, line)
    return batch, so


def rejected_lot(w: World, supplier: str, item: str, received: str, number: str) -> str:
    lot = w.obj("Lot", {"qc_status": "Rejected", "lot_number": number}, audience="fm:lots")
    receipt = w.obj("Receipt", {"received_date": received}, audience="api:receipts")
    w.link("Lot.received_in", lot, receipt)
    w.link("Lot.of_item", lot, item)
    w.link("Receipt.supplied_by", receipt, supplier)
    return lot


class ChaseAndHolds(Engine):

    def run_rules(self, w: World, **options) -> dict:
        return self.run_engine(w, only=RULES, **options)

    def kinds(self, kind: str, **read) -> list[dict]:
        return self.read(kinds=[kind], limit=1000, **read)["gaps"]

    # ------------------------------------------------------------ customer chase

    def test_a_repeat_chase_is_raised_with_a_drafted_reply_and_no_body(self):
        w = World()
        who = customer(w)
        so = order(w, who, day(-10))
        first = mail(w, so, day(9), n=1)
        mail(w, so, day(9), mailbox="mail:00000000-0000-4000-8000-0000000000b2",
             n=1)                                           # another mailbox's copy
        second = mail(w, so, day(2))
        mail(w, so, day(40))                                # outside the window
        mail(w, so, day(3), sender="ops@synth-tenant.example")         # ours
        mail(w, so, day(3), sender="no-reply@synth-customer.example")  # automated
        mail(w, so, day(3), mailbox=f"mail:{EXCLUDED_OID}")            # excluded mailbox
        mail(w, so, day(3), body="Thanks for the invoice.", subject="Re: Order")  # no ask
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][pc.CHASE], "complete")
        (gap,) = self.kinds(pc.CHASING)
        self.assertEqual(gap["object_id"], so)
        self.assertEqual(gap["summary"], f"Customer chased 2 times between {day(9)} and "
                                         f"{day(2)} about order SYNTH-SO-1")
        event = gap["event"]
        self.assertEqual([m["id"] for m in event["messages"]], [first, second])
        self.assertTrue(all(len(m["subject"]) <= pc.SUBJECT_CHARS for m in event["messages"]))
        text = json.dumps(gap)
        self.assertNotIn("secret pricing", text)
        self.assertNotIn("Still waiting", text)
        draft = gap["follow_up"]["draft"]
        self.assertFalse(draft["sent"])
        self.assertIn("order SYNTH-SO-1", draft["body"])
        self.assertIn("SYNTH-ITEM: 10 ea ordered, 4 shipped, 6 still open.", draft["body"])
        self.assertIn("It has not shipped yet.", draft["body"])
        self.assertNotIn("777", draft["body"])
        self.assertEqual(gap["follow_up"]["notify"]["role"], "account_rep")
        self.assertEqual(summary["not_evaluated"][pc.CHASE].get("automated_sender"), 1)

    def test_a_late_order_chased_once_is_raised_and_an_on_time_one_is_not(self):
        w = World()
        who = customer(w)
        late = order(w, who, day(4), number="SYNTH-LATE")
        on_time = order(w, who, day(-20), number="SYNTH-OK")
        mail(w, late, day(1))
        mail(w, on_time, day(1))
        summary = self.run_rules(w)
        (gap,) = self.kinds(pc.CHASING)
        self.assertEqual(gap["object_id"], late)
        self.assertIn("4 days past its requested date", gap["summary"])
        self.assertEqual(summary["detectors"][pc.CHASE], "complete")

    def test_a_lead_mention_or_unattributed_sender_is_a_lead(self):
        w = World()
        so = order(w, customer(w), day(-10))
        mail(w, so, day(5), tier="lead")
        mail(w, so, day(3), sender="someone@elsewhere.example")
        self.run_rules(w)
        (gap,) = self.kinds(pc.CHASING)
        self.assertTrue(gap["summary"].startswith("Lead, not firm: "))
        self.assertEqual(gap["follow_up"]["priority"], "low")

    def test_without_the_people_exclusion_the_chase_rule_is_blocked(self):
        w = World()
        so = order(w, customer(w), day(4))
        mail(w, so, day(1))
        summary = self.run_rules(w, people=people(unavailable=True))
        self.assertEqual(summary["detectors"][pc.CHASE], "blocked")
        self.assertEqual(self.kinds(pc.CHASING), [])

    def test_a_restricted_reader_sees_no_chase_it_may_not_open(self):
        w = World()
        so = order(w, customer(w), day(4))
        mail(w, so, day(1))
        w.obj("Shipment", {"shipment_number": "SYNTH-SH"}, audience="bc:shipments")
        self.run_rules(w)
        self.assertEqual(len(self.kinds(pc.CHASING)), 1)
        for hidden in (MAILBOX, "bc:shipments", "api:orders"):
            with self.subTest(hidden=hidden):
                self.assertEqual(self.kinds(pc.CHASING, may_read=lambda a, h=hidden: a != h),
                                 [])
                self.assertEqual(self.kinds(pc.TELL_FIRST,
                                            may_read=lambda a, h=hidden: a != h), [])

    def test_teams_chases_count_only_in_chats_whose_members_are_cleared(self):
        w = TextWorld()
        so = order(w, customer(w), day(-10))
        w.teams(so, day(3), "19:cleared", GUEST_OID)
        w.teams(so, day(2), "19:cleared", GUEST_OID)
        w.teams(so, day(2), "19:cleared", OURS_OID)     # our own staff asking: never counted
        w.teams(so, day(1), "19:cleared", OURS_OID)
        w.teams(so, day(2), "19:mixed", GUEST_OID)
        w.teams(so, day(1), "19:mixed", GUEST_OID)
        members = {"19:cleared": (OURS_OID, GUEST_OID),
                   "19:mixed": (GUEST_OID, EXCLUDED_OID)}
        summary = self.run_rules(w, people=chat_people(members))
        (gap,) = self.kinds(pc.CHASING)
        self.assertEqual([m["channel"] for m in gap["event"]["messages"]], ["teams", "teams"])
        self.assertTrue(gap["summary"].startswith("Lead, not firm: "))   # no customer link
        self.assertEqual(summary["not_evaluated"][pc.CHASE]["message_not_cleared"], 2)
        self.assertNotIn("secret pricing", json.dumps(gap))
        # no chat membership known: every chat is withheld (fail closed)
        self.store.unlink()
        self.inbox.unlink()
        self.run_rules(w)
        self.assertEqual(self.kinds(pc.CHASING), [])

    def test_an_order_due_soon_for_a_customer_who_chased_before_is_listed(self):
        w = World()
        who = customer(w)
        past = order(w, who, day(70), number="SYNTH-OLD", status="Shipped")
        mail(w, past, day(60))
        due = order(w, who, day(-2), number="SYNTH-DUE")
        order(w, who, day(-1), number="SYNTH-READY", status="Ready to Ship")
        order(w, customer(w, "other@synth-other.example"), day(-2), number="SYNTH-QUIET")
        self.run_rules(w)
        (gap,) = self.kinds(pc.TELL_FIRST)
        self.assertEqual(gap["object_id"], due)
        self.assertEqual(gap["summary"], f"Order SYNTH-DUE is due {day(-2)} and not ready to "
                                         "ship; this customer chased 1 time(s) in the last "
                                         "180 days")
        self.assertIn("draft", gap["follow_up"])
        # a reader who may not open the earlier chase's mailbox never sees the list entry
        self.assertEqual(self.kinds(pc.TELL_FIRST, may_read=lambda a: a != MAILBOX), [])

    def test_tell_first_counts_only_chases_a_reader_may_open(self):
        w = World()
        who = customer(w)
        past = order(w, who, day(70), number="SYNTH-OLD", status="Shipped")
        mail(w, past, day(90), mailbox=OTHER_BOX)
        for offset in (60, 50, 40):
            mail(w, past, day(offset))
        order(w, who, day(-2), number="SYNTH-DUE")
        self.run_rules(w)
        (gap,) = self.kinds(pc.TELL_FIRST)
        self.assertIn("chased 4 time(s)", gap["summary"])
        self.assertEqual([m["sent"] for m in gap["event"]["messages"]],
                         [day(60), day(50), day(40)])        # the latest three, by date
        self.assertEqual(self.kinds(pc.TELL_FIRST, may_read=lambda a: a != OTHER_BOX), [])

    def test_chase_phrases_by_family(self):
        for text, family in (("Where is my PO 1234?", "where_is"),
                             ("Could you send the tracking number", "eta"),
                             ("Any update on this?", "status"),
                             ("Just following up on the delivery", "follow_up"),
                             ("Please find the invoice attached", None),
                             ("> where is my order\nThanks, received.", None)):
            with self.subTest(text=text):
                self.assertEqual(pc.chase_phrase(text), family)

    # ------------------------------------------------------------ quality holds

    def test_holds_past_their_days_worst_first_with_the_orders_waiting(self):
        w = World()
        waited, so = held_batch(w, ordered=day(40))
        held_batch(w, ordered=day(5), code="BC-SYNTH-NEW")              # under the days
        held_batch(w, ordered=day(40), status="Cancelled", code="BC-SYNTH-X")
        held_batch(w, ordered=None, code="BC-SYNTH-U")                  # no date
        lot = w.obj("Lot", {"qc_status": "Quarantine", "lot_number": "L-SYNTH-Q"},
                    audience="fm:lots")
        w.link("Lot.received_in", lot, w.obj("Receipt", {"received_date": day(20)},
                                             audience="api:receipts"))
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][pc.HOLDS], "complete")
        gaps = self.kinds(pc.HOLD_AGING)
        self.assertEqual(sorted(g["object_id"] for g in gaps), sorted([waited, lot]))
        by = {g["object_id"]: g for g in gaps}
        self.assertEqual(by[waited]["summary"],
                         "Batch BC-SYNTH-1 of SYNTH-BULK, 600 kg on On-Hold 40 days (counted "
                         "from its earliest allocated order); 1 open order(s) wait on it "
                         "(SO-BC-SYNTH-1)")
        self.assertEqual(by[waited]["follow_up"]["priority"], "high")
        self.assertEqual(by[waited]["event"]["orders_affected"], 1)
        self.assertEqual(summary["not_evaluated"][pc.HOLDS], {"hold_age_unknown": 1})
        # a reader who may not open the batches sees the lot's hold only
        self.assertEqual([g["object_id"] for g in self.kinds(
            pc.HOLD_AGING, may_read=lambda a: a != "fm:batches")], [lot])

    def test_a_suppliers_rising_rejects_carry_a_complaint_draft(self):
        w = World()
        sup = w.obj("Supplier", {"name": "SYNTH Supplier"}, audience="api:suppliers")
        item = w.obj("Product", {"code": "SYNTH-RAW"}, audience="api:items")
        rejected_lot(w, sup, item, day(10), "L-1")
        rejected_lot(w, sup, item, day(30), "L-2")
        old = rejected_lot(w, sup, item, day(120), "L-OLD")
        self.run_rules(w)
        (trend_s,) = [g for g in self.kinds(pc.REJECT_TREND) if g["object_id"] == sup]
        self.assertEqual(trend_s["summary"], "2 rejects for supplier SYNTH Supplier in the "
                                             "last 90 days, up from 1 in the 90 days before")
        # the earlier window's reject behind "up from 1" is cited too
        self.assertIn(handle(int(old.rsplit(":", 1)[1])), trend_s["citations"])
        draft = trend_s["follow_up"]["draft"]
        self.assertEqual((draft["to"], draft["sent"]), ("supplier", False))
        self.assertIn("L-1", draft["body"])
        self.assertEqual({g["object_id"] for g in self.kinds(pc.REJECT_TREND)}, {sup, item})
        self.assertEqual(self.kinds(pc.REJECT_TREND, may_read=lambda a: a != "fm:lots"), [])

    # ------------------------------------------------------------ briefing

    def test_every_new_kind_has_a_next_step_and_its_words(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                self.assertIn(kind, briefing.EVENT_TYPES)
                self.assertIn(kind, briefing.NEXT_ACTIONS)
                self.assertIn(kind, briefing.TODAY_WORDS)
        self.assertIn(pc.CHASE, pe.REQUIRES_GRANT)
        self.assertTrue(set(RULES) <= set(pe.DETECTORS) <= set(pe.DETECTOR_SECONDS)
                        | set(pe.DETECTORS))

    # ------------------------------------------------------------ cardinality

    def test_every_order_and_batch_is_judged_once_at_1_10_and_60(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World()
                chased, held = [], []
                for i in range(n):
                    address = f"buyer{i}@synth-c{i}.example"
                    so = order(w, customer(w, address), day(-5 - i % 20),
                               number=f"SYNTH-SO-{i}")
                    mail(w, so, day(3), sender=address)
                    mail(w, so, day(1), sender=address)
                    chased.append(so)
                    held.append(held_batch(w, ordered=day(20 + i % 30), code=f"BC-{i}")[0])
                started = time.monotonic()
                summary = self.run_rules(w)
                self.assertLess(time.monotonic() - started, 60)
                for rule in RULES:
                    self.assertEqual(summary["detectors"][rule], "complete")
                self.assertEqual(sorted(g["object_id"] for g in self.kinds(pc.CHASING)),
                                 sorted(chased))
                self.assertEqual(sorted(g["object_id"] for g in self.kinds(pc.HOLD_AGING)),
                                 sorted(held))
                self.inbox.unlink()
                self.store.unlink()


if __name__ == "__main__":
    unittest.main()
