"""Expedites with no answer, supplier shortage notices against open orders, and who chased
whom for payment (``operating.proactive_mail_pains``) through the engine and the
per-reader inbox.

SYNTHETIC fixtures only (``World`` stores, synthetic handles and addresses); evidence label:
isolated-fixture.
"""

from __future__ import annotations

from datetime import timedelta
from operating.copy_gate import say_date
import json
import time

from operating import briefing
from operating import proactive_engine as pe
from operating import proactive_mail_pains as mp
from operating.tests.test_proactive_engine import (AS_OF, EXCLUDED_OID, Engine, World,
                                                   people)

RULES = [*mp.RULES, "overdue_receivable"]
CUSTOMER = "buyer@synth-customer.example"
SUPPLIER = "sales@synth-supplier.example"
OURS = "ops@synth-tenant.example"
MAILBOX = "mail:00000000-0000-4000-8000-0000000000a1"
OTHER_BOX = "mail:00000000-0000-4000-8000-0000000000b2"
SECRET = "secret pricing 777"
NO_FINANCE = frozenset({"l5", "pii", "quality_controlled"})


def day(offset: int) -> str:
    return (AS_OF - timedelta(days=offset)).isoformat()


def item(w: World) -> str:
    return w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")


def sales_order(w: World, product: str, *, number: str = "SYNTH-SO-1",
                required: str | None = None) -> tuple[str, str]:
    who = w.obj("Customer", {"name": "SYNTH Customer", "email": CUSTOMER},
                audience="api:customers")
    so = w.obj("SalesOrder", {"order_number": number, "status": "Open",
                              "required_date": required or day(-30)},
               binding="portal.app_sales_order", audience="api:orders")
    w.link("SalesOrder.ordered_by", so, who)
    line = w.obj("SalesOrderLine", {"ordered_quantity": ("10", "ea"),
                                    "outstanding_quantity": ("10", "ea")},
                 audience="api:orders")
    w.link("SalesOrderLine.line_of", line, so)
    w.link("SalesOrderLine.for_item", line, product)
    return so, line


def purchase_order(w: World, product: str, *, number: str = "SYNTH-PO-1") -> tuple[str, str]:
    supplier = w.obj("Supplier", {"name": "SYNTH Supplier", "bc_email": SUPPLIER},
                     audience="api:suppliers")
    po = w.obj("PurchaseOrder", {"status": "Pending", "po_number": number,
                                 "expected_date": day(-20)}, audience="api:po")
    w.link("PurchaseOrder.ordered_from", po, supplier)
    line = w.obj("PurchaseOrderLine", {"ordered_quantity": ("10", "ea"),
                                       "received_quantity": ("0", "ea", "received")},
                 audience="api:po")
    w.link("PurchaseOrderLine.line_of", line, po)
    w.link("PurchaseOrderLine.for_item", line, product)
    return po, line


def mail(w: World, target: str, link: str, sent: str, body: str, *, sender: str = CUSTOMER,
         to: str = OURS, subject: str = "Order update " + "x" * 80, mailbox: str = MAILBOX,
         tier: str = "verified", n: int | None = None,
         conversation: str | None = None) -> str:
    oid = w.obj("Email", {**({"conversation_id": conversation} if conversation else {}),
                          "subject": subject, "sender": sender, "to_addresses": to,
                          "sent_at": f"{sent}T15:00:00Z", "body_preview": body,
                          "internet_message_id": f"<m{n if n is not None else w.n}@synth>"},
                binding="graph_mail.messages", audience=mailbox)
    w.link(link, oid, target, tier)
    return oid


def overdue_invoice(w: World) -> str:
    inv = w.obj("SalesInvoice", {"status": "Open", "invoice_number": "SYNTH-INV",
                                 "due_date": "2026-09-01", "remaining_amount": "1200.50"},
                binding="bc.sales_invoices", audience="bc:sales")
    from operating.tests.test_proactive_business import money
    money(w, inv, "remaining_amount", "CAD")
    w.link("SalesInvoice.billed_to", inv, w.obj("Customer", {"name": "SYNTH Customer"},
                                                audience="bc:customers"))
    return inv


class MailPains(Engine):

    def run_rules(self, w: World, **options) -> dict:
        return self.run_engine(w, only=RULES, **options)

    def kinds(self, kind: str, **read) -> list[dict]:
        return self.read(kinds=[kind], limit=1000, **read)["gaps"]

    # ------------------------------------------------------------------ phrases

    def test_phrases_skip_questions_and_conditionals_but_keep_direct_requests(self):
        cases = [
            ("Please expedite this order, we need it urgently.", mp.EXPEDITE_FAMILIES, "expedite"),
            ("Could you please expedite PO 55?", mp.EXPEDITE_FAMILIES, "expedite"),
            ("Do we need to expedite this?", mp.EXPEDITE_FAMILIES, None),
            ("If it is late we will need to expedite.", mp.EXPEDITE_FAMILIES, None),
            ("Ship it as soon as possible.", mp.EXPEDITE_FAMILIES, "urgent"),
            ("As soon as we get the PO we can rush it.", mp.EXPEDITE_FAMILIES, None),
            ("Attached is your order confirmation; for any discrepancies or changes, please "
             "email us as soon as possible.", mp.EXPEDITE_FAMILIES, None),
            ("Should you need it sooner, please let us know ASAP.", mp.EXPEDITE_FAMILIES, None),
            # a notice's clause is dropped only from its opener on: an ask before it stays
            ("Your account is 60 days past due; for any questions call me.",
             mp.PAYMENT_FAMILIES, "past_due"),
            ("This invoice is past due, should you have questions please call.",
             mp.PAYMENT_FAMILIES, "past_due"),
            ("Please expedite PO 4512, we need it for any orders shipping next week.",
             mp.EXPEDITE_FAMILIES, "expedite"),
            ("For any discrepancies, please email us as soon as possible.",
             mp.EXPEDITE_FAMILIES, None),
            ("In the event of changes - please reply ASAP.", mp.EXPEDITE_FAMILIES, None),
            ("Due to a raw material shortage, delivery is delayed.", mp.SHORTAGE_FAMILIES,
             "shortage"),
            ("Is there a shortage on this item?", mp.SHORTAGE_FAMILIES, None),
            ("Could you confirm there is no shortage?", mp.SHORTAGE_FAMILIES, None),
            ("Unless the port reopens, shipments are delayed.", mp.SHORTAGE_FAMILIES, None),
            ("This invoice is past due, please remit.", mp.PAYMENT_FAMILIES, "past_due"),
            ("Was the payment past due?", mp.PAYMENT_FAMILIES, None),
            ("Thanks for the order.", mp.PAYMENT_FAMILIES, None),
            ("> Please expedite\nThanks, received.", mp.EXPEDITE_FAMILIES, None),
            # negated: says the opposite of the family
            ("Your order is not delayed.", mp.SHORTAGE_FAMILIES, None),
            ("There is no shortage on this item.", mp.SHORTAGE_FAMILIES, None),
            ("Lead times are no longer extended.", mp.SHORTAGE_FAMILIES, None),
            ("This is not urgent.", mp.EXPEDITE_FAMILIES, None),
            ("We do not need to expedite.", mp.EXPEDITE_FAMILIES, None),
            ("We don't need it ASAP.", mp.EXPEDITE_FAMILIES, None),
            ("Your invoice is not overdue.", mp.PAYMENT_FAMILIES, None),
            ("Payment received for the overdue invoice.", mp.PAYMENT_FAMILIES, None),
            ("Thank you for your payment of the past due balance.", mp.PAYMENT_FAMILIES, None),
            # a negation elsewhere in the sentence does not hide the phrase
            ("Not a problem, but please expedite this order.", mp.EXPEDITE_FAMILIES,
             "expedite"),
            ("The invoice has not yet been paid and is overdue.", mp.PAYMENT_FAMILIES,
             "past_due"),
        ]
        for text, families, want in cases:
            with self.subTest(text=text):
                self.assertEqual(mp.phrase(text, families,
                                           requests=families is not mp.SHORTAGE_FAMILIES),
                                 want)

    # ------------------------------------------------------------------ expedite

    def test_an_unanswered_expedite_is_raised_once_and_answered_or_recent_ones_are_not(self):
        w = World()
        p = item(w)
        so, _ = sales_order(w, p)
        first = mail(w, so, "Email.mentions_sales_order", day(6),
                     f"Please expedite this order, we need it urgently. {SECRET}", n=1)
        mail(w, so, "Email.mentions_sales_order", day(6), "copy", n=1,
             mailbox=OTHER_BOX)   # another mailbox's copy
        mail(w, so, "Email.mentions_sales_order", day(5), "Do we need to expedite this?")
        mail(w, so, "Email.mentions_sales_order", day(4), "Please rush this order.",
             mailbox=f"mail:{EXCLUDED_OID}")                          # excluded mailbox
        answered, _ = sales_order(w, p, number="SYNTH-SO-2")
        mail(w, answered, "Email.mentions_sales_order", day(6), "Please expedite.")
        mail(w, answered, "Email.mentions_sales_order", day(5), "It ships Friday.",
             sender=OURS, to=CUSTOMER)
        # answered in the same conversation by a reply that names another order
        threaded, _ = sales_order(w, p, number="SYNTH-SO-4")
        mail(w, threaded, "Email.mentions_sales_order", day(6), "Please expedite.",
             conversation="conv-synth-1")
        mail(w, answered, "Email.mentions_sales_order", day(5), "It ships Friday.", sender=OURS,
             to=CUSTOMER, conversation="conv-synth-1", tier="lead")   # names another order
        recent, _ = sales_order(w, p, number="SYNTH-SO-3")
        mail(w, recent, "Email.mentions_sales_order", day(1), "Please expedite.")
        po, _ = purchase_order(w, p)
        ours = mail(w, po, "Email.mentions_purchase_order", day(8),
                    "Please expedite PO SYNTH-PO-1.", sender=OURS, to=SUPPLIER)
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][mp.EXPEDITE], "complete")
        gaps = {g["object_id"]: g for g in self.kinds(mp.EXPEDITE_KIND)}
        self.assertEqual(set(gaps), {so, po})
        self.assertEqual([m["id"] for m in gaps[so]["event"]["messages"]], [first])
        self.assertIn("SYNTH-SO-1: we were asked 1 time,", gaps[so]["summary"])
        self.assertIn("we asked 1 time,", gaps[po]["summary"])
        self.assertEqual(gaps[po]["event"]["messages"][0]["id"], ours)
        self.assertNotIn(SECRET, json.dumps(gaps))
        for hidden in (MAILBOX, "api:orders"):
            with self.subTest(hidden=hidden):
                seen = self.kinds(mp.EXPEDITE_KIND, may_read=lambda a, h=hidden: a != h)
                self.assertNotIn(so, [g["object_id"] for g in seen])

    def test_an_automatic_reply_or_a_note_to_colleagues_is_no_expedite_ask_or_answer(self):
        w = World()
        p = item(w)
        # our own out-of-office reply, sent to a colleague, naming the order
        away, _ = sales_order(w, p, number="SYNTH-SO-5")
        mail(w, away, "Email.mentions_sales_order", day(9),
             "Thank you for your email. I am out of office, please expedite with ops.",
             sender=OURS, to="colleague@synth-tenant.example",
             subject="Automatic reply: Order SYNTH-SO-5")
        # an away note under an ordinary subject, from outside
        note, _ = sales_order(w, p, number="SYNTH-SO-6")
        mail(w, note, "Email.mentions_sales_order", day(9),
             "I am currently out of the office. Please expedite urgent orders with sales.",
             subject="Re: SYNTH-SO-6")
        # ours asking a colleague to rush it: coordination, nobody outside could answer
        inside, _ = sales_order(w, p, number="SYNTH-SO-7")
        mail(w, inside, "Email.mentions_sales_order", day(9), "Please expedite this order.",
             sender=OURS, to="colleague@synth-tenant.example")
        # a real ask whose only later message is the other side's automatic reply
        asked, _ = sales_order(w, p, number="SYNTH-SO-8")
        mail(w, asked, "Email.mentions_sales_order", day(9), "Please expedite this order.",
             sender=OURS, to=CUSTOMER, conversation="conv-synth-8")
        mail(w, asked, "Email.mentions_sales_order", day(8), "I will be away until Monday.",
             sender=CUSTOMER, to=OURS, conversation="conv-synth-8",
             subject="Automatic reply: SYNTH-SO-8")
        # ours whose recipients were not captured: unknown, so still read as an ask
        unknown, _ = sales_order(w, p, number="SYNTH-SO-9")
        mail(w, unknown, "Email.mentions_sales_order", day(9), "Please expedite this order.",
             sender=OURS, to="")
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][mp.EXPEDITE], "complete")
        gaps = {g["object_id"]: g for g in self.kinds(mp.EXPEDITE_KIND)}
        self.assertEqual(set(gaps), {asked, unknown})
        self.assertIn("SYNTH-SO-8: we asked 1 time,", gaps[asked]["summary"])

    # ------------------------------------------------------------------ shortage

    def test_a_shortage_notice_lists_open_orders_by_item_or_by_pegging(self):
        w = World()
        p = item(w)
        so, so_line = sales_order(w, p, required=day(-5))
        sales_order(w, item(w), number="SYNTH-OTHER")                 # another item
        po, po_line = purchase_order(w, p)
        mail(w, po, "Email.mentions_purchase_order", day(3),
             f"Due to a raw material shortage, delivery is delayed. {SECRET}",
             sender=SUPPLIER)
        quiet_po, _ = purchase_order(w, item(w), number="SYNTH-PO-Q")
        mail(w, quiet_po, "Email.mentions_purchase_order", day(3),
             "Is there a shortage on this item?", sender=SUPPLIER)
        self.run_rules(w)
        (gap,) = self.kinds(mp.SHORTAGE_KIND)
        self.assertEqual(gap["object_id"], po)
        self.assertEqual(gap["event"]["orders_affected"], 1)
        self.assertEqual(gap["event"]["basis"], "item")
        self.assertEqual(gap["follow_up"]["priority"], "high")       # wanted within 14 days
        self.assertTrue(gap["summary"].startswith("Supplier notice (shortage)"))
        self.assertNotIn(SECRET, json.dumps(gap))
        for hidden in (MAILBOX, "api:orders", "api:po"):
            with self.subTest(hidden=hidden):
                self.assertEqual(self.kinds(mp.SHORTAGE_KIND,
                                            may_read=lambda a, h=hidden: a != h), [])
        # pegging, when the store pegs the order's lines, decides the dependent orders
        w.link("SalesOrderLine.supplied_by", so_line, po_line, "lead")
        self.store.unlink()
        self.inbox.unlink()
        self.run_rules(w)
        (gap,) = self.kinds(mp.SHORTAGE_KIND)
        self.assertEqual(gap["event"]["basis"], "pegging")

    # ------------------------------------------------------------------ payment chase

    def test_our_chases_fold_into_the_overdue_receivable_per_reader(self):
        w = World()
        inv = overdue_invoice(w)
        mail(w, inv, "Email.mentions_sales_invoice", day(10),
             f"This invoice is past due, please remit. {SECRET}", sender=OURS, to=CUSTOMER)
        mail(w, inv, "Email.mentions_sales_invoice", day(4), "Payment reminder for the "
             "invoice below.", sender=OURS, to=CUSTOMER, mailbox=OTHER_BOX)
        mail(w, inv, "Email.mentions_sales_invoice", day(3), "Is it past due?",
             sender=OURS, to=CUSTOMER)                                # a question: none
        mail(w, inv, "Email.mentions_sales_invoice", day(2), "Past due, please pay.",
             sender=OURS, to="ar@synth-tenant.example")               # to ourselves only
        self.run_rules(w)
        (gap,) = self.kinds("overdue_receivable")
        self.assertTrue(gap["summary"].endswith(f"; chased 2 times, last on {day(4)}"))
        self.assertEqual(gap["event"]["chases"]["count"], 2)
        self.assertNotIn("handle", json.dumps(gap["event"]["chases"]))
        self.assertNotIn(SECRET, json.dumps(gap))
        (narrow,) = self.kinds("overdue_receivable", may_read=lambda a: a != OTHER_BOX)
        self.assertTrue(narrow["summary"].endswith(f"; chased 1 time, last on {day(10)}"))
        (blind,) = self.kinds("overdue_receivable",
                              may_read=lambda a: not a.startswith("mail:"))
        self.assertNotIn("chased", blind["summary"])                  # still sees the invoice
        self.assertNotIn("chases", blind.get("event", {}))
        self.assertEqual(self.kinds("overdue_receivable", held=NO_FINANCE), [])

    def test_a_supplier_chasing_payment_is_finance_marked(self):
        w = World()
        bill = w.obj("PurchaseInvoice", {"status": "Open", "vendor_invoice_number": "SYNTH-AP",
                                         "remaining_amount": "50"}, audience="bc:ap")
        paid = w.obj("PurchaseInvoice", {"status": "Paid", "vendor_invoice_number": "SYNTH-AP2"},
                     audience="bc:ap")
        for target in (bill, paid):
            mail(w, target, "Email.mentions_purchase_invoice", day(5),
                 "Your account is past due, please remit.", sender=SUPPLIER)
        mail(w, bill, "Email.mentions_purchase_invoice", day(2), "Statement of account "
             "attached.", sender=SUPPLIER)
        self.run_rules(w)
        (gap,) = self.kinds(mp.PAYABLE_KIND)         # the paid one is not raised
        self.assertEqual(gap["object_id"], bill)
        self.assertIn("SYNTH-AP 2 times, last on " + say_date(day(2)), gap["summary"])
        self.assertEqual(self.kinds(mp.PAYABLE_KIND, held=NO_FINANCE), [])
        self.assertEqual(self.kinds(mp.PAYABLE_KIND, may_read=lambda a: a != MAILBOX), [])

    def sage_bill(self, w: World, vendor: str, number: str, *, issued: int,
                  paid: int | None = None, due: str = "50.00", kind: str = "1") -> str:
        def ymd(offset: int) -> str:
            return day(offset).replace("-", "")
        return w.obj("RelSageApOpenDocument", {
            "idvend": vendor + "  ", "idinvc": number, "dateinvc": ymd(issued),
            "dateinvcdu": ymd(issued - 30), "txttrxtype": kind,
            "swpaid": "1" if paid is not None else "0",
            "datepaid": ymd(paid) if paid is not None else "0",
            "amtduetc": "0" if paid is not None else due},
            binding="gen.sage.ap_open_document", audience="sage:ap")

    def test_a_supplier_chasing_payment_is_read_against_sage_ap_on_the_day(self):
        w = World()
        owed = w.obj("Supplier", {"name": "SYNTH Supplier", "code": "SYNTHV1"},
                     audience="api:suppliers")
        settled = w.obj("Supplier", {"name": "SYNTH Paid", "code": "SYNTHV2"},
                        audience="api:suppliers")
        named = self.sage_bill(w, "SYNTHV1", "SYNTH-AP-77", issued=60, paid=-5)  # paid later
        self.sage_bill(w, "SYNTHV1", "SYNTH-AP-78", issued=-2)            # issued after the day
        self.sage_bill(w, "SYNTHV2", "SYNTH-AP-90", issued=60, paid=10)   # paid before the day
        self.sage_bill(w, "SYNTHV2", "SYNTH-AP-91", issued=60, kind="3", due="40")  # a credit
        for n, (target, tier, text) in enumerate((
                (owed, "verified", "Invoice SYNTH-AP-77 is past due, please remit."),
                (owed, "verified", "Reminder: the balance is overdue, kindly remit."),
                (settled, "verified", "Your account is past due, please remit."),
                (owed, "lead", "Your account is past due, please remit."))):
            mail(w, target, "Email.sent_by_supplier", day(6 - n), text, sender=SUPPLIER,
                 to=json.dumps([OURS]), tier=tier, conversation=f"pay-{n}", n=700 + n)
        self.run_rules(w)
        (gap,) = self.kinds(mp.PAYABLE_KIND)
        self.assertEqual((gap["object_id"], gap["event"]["source"], gap["event"]["chases"],
                          gap["event"]["open_bills"], gap["event"]["named_invoice"]),
                         (named, "sage_ap", 2, 1, True))
        self.assertIn("they name invoice SYNTH-AP-77", gap["summary"])
        self.assertNotIn("kindly remit", json.dumps(gap))               # no body kept
        self.assertEqual(self.kinds(mp.PAYABLE_KIND, held=NO_FINANCE), [])
        self.assertEqual(self.kinds(mp.PAYABLE_KIND, may_read=lambda a: a != MAILBOX), [])
        self.assertEqual(self.kinds(mp.PAYABLE_KIND, may_read=lambda a: a != "sage:ap"), [])

    def test_a_payment_chase_without_a_sender_link_is_matched_by_the_suppliers_domain(self):
        w = World()
        w.obj("Supplier", {"name": "SYNTH Three", "code": "SYNTHV3",
                           "bc_email": "orders@synth-vendor3.example"}, audience="api:suppliers")
        w.obj("Supplier", {"name": "SYNTH Four", "code": "SYNTHV4",
                           "bc_email": "someone@gmail.com"}, audience="api:suppliers")
        bill = self.sage_bill(w, "SYNTHV3", "SYNTH-AP-31", issued=40)
        self.sage_bill(w, "SYNTHV4", "SYNTH-AP-41", issued=40)
        anchor = w.obj("Product", {"code": "SYNTH-X"}, audience="api:items")
        for n, sender in enumerate(("billing@synth-vendor3.example", "other@gmail.com")):
            mail(w, anchor, "Email.mentions_product", day(3), "Your account is past due, "
                 "please remit.", sender=sender, to=json.dumps([OURS]),
                 conversation=f"dom-{n}", n=800 + n)
        self.run_rules(w)
        (gap,) = self.kinds(mp.PAYABLE_KIND)      # a public mail domain names no supplier
        self.assertEqual(gap["object_id"], bill)
        self.assertIn("1 bill of theirs unpaid in Sage AP", gap["summary"])

    # ------------------------------------------------------------------ fail closed

    def test_without_the_people_exclusion_every_rule_is_blocked_and_nothing_is_folded(self):
        w = World()
        p = item(w)
        so, _ = sales_order(w, p)
        mail(w, so, "Email.mentions_sales_order", day(6), "Please expedite.")
        inv = overdue_invoice(w)
        mail(w, inv, "Email.mentions_sales_invoice", day(5), "Past due, please pay.",
             sender=OURS, to=CUSTOMER)
        summary = self.run_rules(w, people=people(unavailable=True))
        for rule in mp.RULES:
            self.assertEqual(summary["detectors"][rule], "blocked")
        for kind in mp.KINDS:
            self.assertEqual(self.kinds(kind), [])
        (gap,) = self.kinds("overdue_receivable")
        self.assertNotIn("chased", gap["summary"])

    def test_registered_with_a_next_step_and_one_line_words(self):
        for kind in mp.KINDS:
            with self.subTest(kind=kind):
                self.assertIn(kind, briefing.NEXT_ACTIONS)
                self.assertIn(kind, briefing.TODAY_WORDS)
        for rule in mp.RULES:
            self.assertIn(rule, pe.DETECTORS)
            self.assertLess(pe.DETECTOR_SECONDS[rule], 61)

    # ------------------------------------------------------------------ cardinality

    def test_n_orders_give_n_alerts_none_lost_or_doubled(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World()
                p = item(w)
                want = set()
                for i in range(n):
                    so, _ = sales_order(w, p, number=f"SYNTH-SO-{i}")
                    want.add(so)
                    mail(w, so, "Email.mentions_sales_order", day(5), "Please expedite.",
                         n=20_000 + i)
                    mail(w, so, "Email.mentions_sales_order", day(5), "Please expedite.",
                         n=20_000 + i, mailbox=OTHER_BOX)
                if self.store.exists():
                    self.store.unlink()
                    self.inbox.unlink()
                started = time.monotonic()
                self.run_rules(w)
                took = time.monotonic() - started
                found = [g["object_id"] for g in self.kinds(mp.EXPEDITE_KIND)]
                self.assertEqual(sorted(found), sorted(want))
                self.assertLess(took, 60)

    def test_over_the_message_cap_the_newest_are_judged_and_the_rest_counted(self):
        from unittest import mock

        w = World()
        p = item(w)
        old, _ = sales_order(w, p, number="SYNTH-OLD")
        new, _ = sales_order(w, p, number="SYNTH-NEW")
        mail(w, new, "Email.mentions_sales_order", day(4), "Please expedite.")
        mail(w, old, "Email.mentions_sales_order", day(20), "Please expedite.")
        with mock.patch.object(mp, "MAX_MESSAGES", 1):
            summary = self.run_rules(w)
        self.assertEqual([g["object_id"] for g in self.kinds(mp.EXPEDITE_KIND)], [new])
        self.assertEqual(summary["not_evaluated"][mp.EXPEDITE]["messages_over_cap"], 1)
