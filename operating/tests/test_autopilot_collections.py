"""Collection drafts (``operating.autopilot_collections``): a customer's past-due invoices
drafted as the payment reminder its receivables person would send, to the address the
customer's own payment mail uses, held when the mail or the books say not to chase; prepared
only, finance-marked, nothing sent. Synthetic records only (isolated-fixture)."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import time
import unittest
from unittest import mock

from operating import autopilot as ap
from operating import autopilot_handlers
from operating import autopilot_reading
from operating import autopilot_collections as ac
from operating import proactive_sheets as ps
from operating.tests.test_autopilot_replay import World
from operating.tests.test_proactive_engine import EXCLUDED_OID, OURS_OID
from operating.tests.test_proactive_threads import BOSS, OURS, mail, people

FIN = '["finance"]'
DAY = date(2026, 10, 21)
PAYER = "pat.jones@synth-customer.example"


def registry(**params: int) -> dict:
    reg = ap.load_registry()
    for a in reg["autopilots"]:
        if a["id"] == "collection_draft":
            a["enabled"] = True
            a["params"] = {**a["params"], **params}
    return reg


def spec_of(reg: dict) -> dict:
    return next(a for a in reg["autopilots"] if a["id"] == "collection_draft")


class Collections(World):
    def holder(self, code: str = "SYNTHCUST") -> str:
        return self.w.obj("Customer", {"erp_code": code, "name": f"Synth {code}"},
                          binding="portal.sage_ar_customers", audience="api:sage.ar_customers")

    def doc(self, number: str, party: str = "SYNTHCUST", *, due: date, balance: str = "500.00",
            currency: str = "CAD", kind: str = "1", paid_on: str = "",
            issued: date | None = None) -> str:
        issued = issued or due - timedelta(days=30)
        return self.w.obj("RelSageArOpenDocument", {
            "idcust": party, "idinvc": number, "dateinvc": issued.strftime("%Y%m%d"),
            "datedue": due.strftime("%Y%m%d"), "amtinvctc": balance, "amtduetc": balance,
            "codecurn": currency, "swpaid": "1" if paid_on else "0", "datepaid": paid_on,
            "trxtypetxt": kind}, binding="gen.sage.ar_open_document", audience="api:sage.ar",
            markings=FIN)

    def from_customer(self, cust: str, body: str, *, days_ago: int, sender: str = PAYER,
                      subject: str = "Remittance", n: int = 0, conv: str = "pay-1") -> str:
        made = mail(self.w, DAY - timedelta(days=days_ago), sender=sender, to=(OURS,),
                    body=body, subject=subject, conversation=conv, n=70_000 + n)
        self.w.link("Email.sent_by_customer", made, cust)
        return made

    def from_us(self, cust: str, body: str, *, days_ago: int, to=(PAYER,), cc=(),
                subject: str = "Past due invoices", n: int = 0, conv: str = "pay-1") -> str:
        made = mail(self.w, DAY - timedelta(days=days_ago), sender=OURS, to=to, cc=cc,
                    body=body, subject=subject, conversation=conv, n=80_000 + n)
        self.w.link("Email.mentions_customer", made, cust, tier="lead")
        return made

    def prepare(self, st, reg=None, *, who=None):
        reg = reg or registry()
        events = ac.collection_events(st, reg, DAY, who or people())
        work = {tuple(e["customer"]): ap._prepare(st, spec_of(reg), e, DAY, (), 30)
                for e in events}
        return events, work

    # ------------------------------------------------------------------ the draft

    def test_reminder_lists_each_past_due_invoice_cited_to_the_payment_contact(self):
        cust = self.holder()
        a = self.doc("SYN-IN-1001", due=DAY - timedelta(days=12), balance="1234.50")
        b = self.doc("SYN-IN-1002", due=DAY - timedelta(days=5), balance="200")
        self.doc("SYN-IN-1003", due=DAY - timedelta(days=2))            # inside the grace
        self.doc("SYN-IN-1004", due=DAY + timedelta(days=9))            # not due yet
        self.doc("SYN-IN-1005", due=DAY - timedelta(days=40), paid_on="20261001")  # paid
        self.doc("SYN-IN-1006", due=DAY - timedelta(days=40), balance="0.00")  # nothing owed
        self.from_customer(cust, "Please find our remittance for invoice SYN-IN-0901.",
                           days_ago=50)
        st = self.store()
        events, work = self.prepare(st)
        self.assertEqual([tuple(e["customer"]) for e in events], [("sage", "SYNTHCUST")])
        self.assertTrue(ap.matches(spec_of(registry())["when"], events[0]))
        prepared = work[("sage", "SYNTHCUST")]
        w = prepared["work"]
        self.assertEqual(w["tone"], "reminder")
        self.assertEqual([(i["invoice"], i["balance"], i["currency"], i["days_overdue"])
                          for i in w["invoices"]],
                         [("SYN-IN-1001", "1234.50", "CAD", 12),
                          ("SYN-IN-1002", "200.00", "CAD", 5)])
        self.assertEqual({i["object_id"] for i in w["invoices"]}, {a, b})
        self.assertEqual(w["totals"], {"CAD": "1434.50"})
        self.assertEqual(Decimal(w["totals"]["CAD"]),
                         sum(Decimal(i["balance"]) for i in w["invoices"]))
        msg = w["draft"]
        self.assertEqual(msg["to"], [{"address": PAYER}])
        body = msg["body"]
        self.assertTrue(body.startswith("Hi Pat,\n\n"), body)
        self.assertIn("- Invoice SYN-IN-1001, dated ", body)
        self.assertIn(": 1,234.50 CAD, 12 days past due", body)
        self.assertIn(": 200.00 CAD, 5 days past due", body)
        self.assertIn("- Total past due: 1,434.50 CAD", body)
        for gone in ("SYN-IN-1003", "SYN-IN-1004", "SYN-IN-1005", "SYN-IN-1006"):
            self.assertNotIn(gone, body)
        self.assertIsNone(msg["in_reply_to"])
        self.assertTrue(msg["copy_check"]["ok"])
        self.assertIn("SYN-IN-1001", msg["subject"])
        # finance-marked, cited, the mailbox's audience carried, nothing that writes or sends
        self.assertIn("finance", prepared["markings"])
        self.assertIn(f"mail:{OURS_OID}", prepared["audiences"])
        self.assertIn("api:sage.ar", prepared["audiences"])
        self.assertTrue(prepared["citations"])
        self.assertTrue(w["dry_run"])
        self.assertNotIn("write", w)
        self.assertIsNone(autopilot_reading.write_spec({"body": {"prepared": w},
                                         "citations": prepared["citations"]}))

    def test_totals_stay_per_currency(self):
        cust = self.holder()
        self.doc("SYN-IN-2001", due=DAY - timedelta(days=10), balance="100.10")
        self.doc("SYN-IN-2002", due=DAY - timedelta(days=10), balance="0.20", currency="USD")
        self.doc("SYN-IN-2003", due=DAY - timedelta(days=11), balance="0.10", currency="USD")
        self.from_customer(cust, "Invoice SYN-IN-1999 is paid in full.", days_ago=60)
        _e, work = self.prepare(self.store())
        w = work[("sage", "SYNTHCUST")]["work"]
        self.assertEqual(w["totals"], {"CAD": "100.10", "USD": "0.30"})
        self.assertIn("- Total past due: 0.30 USD", w["draft"]["body"])
        self.assertIn("- Total past due: 100.10 CAD", w["draft"]["body"])

    def test_tone_follows_age_and_our_earlier_chases(self):
        cases = {"SYNA": (45, None, "firm"), "SYNB": (70, None, "final"),
                 "SYNC": (12, 8, "firm"), "SYND": (10, 3, None)}
        for i, (code, (late, chased, _tone)) in enumerate(sorted(cases.items())):
            cust = self.holder(code)
            address = f"pat.{code.lower()}@synth-{code.lower()}.example"
            self.doc(f"SYN-{code}-1", code, due=DAY - timedelta(days=late))
            self.from_customer(cust, f"Can you send a copy of invoice SYN-{code}-0?",
                               days_ago=late + 20, sender=address, n=i, conv=f"c{i}")
            if chased:
                self.from_us(cust, f"Invoice SYN-{code}-1 is past due, please remit.",
                             days_ago=chased, to=(address,), n=i, conv=f"c{i}")
        _e, work = self.prepare(self.store())
        got = {k[1]: (w.get("work") or {}).get("tone") or w.get("skip")
               for k, w in work.items()}
        self.assertEqual(got, {"SYNA": "firm", "SYNB": "final", "SYNC": "firm",
                               "SYND": "chased_recently"})
        # firm by age alone: a new message under the tone's own subject, never "second"
        alone = work[("sage", "SYNA")]["work"]["draft"]
        self.assertIsNone(alone["in_reply_to"])
        self.assertEqual(alone["subject"], "Past due: invoice SYN-SYNA-1")
        final = work[("sage", "SYNB")]["work"]["draft"]
        self.assertIn("final notice", final["body"])
        self.assertIn("Please arrange payment by Wed 28 Oct.", final["body"])
        # a follow-up replies in the conversation it was chased in
        firm = work[("sage", "SYNC")]["work"]
        self.assertEqual(firm["draft"]["in_reply_to"], firm["chases"][-1]["object_id"])
        self.assertEqual(firm["draft"]["subject"], "RE: Past due invoices")

    # ------------------------------------------------------------------ holds

    def test_holds_from_the_thread_and_the_books(self):
        due = DAY - timedelta(days=20)
        bodies = {
            "SYNDIS": "We dispute the pricing on invoice SYN-SYNDIS-1.",
            "SYNPRO": "The payment will be sent on Friday.",
            "SYNPAY": "Payment was sent yesterday for invoice SYN-SYNPAY-1.",
            "SYNOLD": "We will pay on Friday.",                 # promised 15 days ago: lapsed
            "SYNCRN": "Could you issue a credit note for the damaged case?",
            "SYNNEG": "Invoice SYN-SYNNEG-1 is not disputed.",  # negated: no hold
        }
        days_ago = {"SYNOLD": 15}
        for i, (code, body) in enumerate(sorted(bodies.items())):
            cust = self.holder(code)
            self.doc(f"SYN-{code}-1", code, due=due)
            self.doc(f"SYN-{code}-2", code, due=due)
            self.from_customer(cust, body, days_ago=days_ago.get(code, 2),
                               sender=f"pat.{code.lower()}@synth-{code.lower()}.example",
                               n=i, conv=f"c{i}")
        open_credit = self.holder("SYNCRD")
        self.doc("SYN-SYNCRD-1", "SYNCRD", due=due)
        self.doc("SYN-SYNCRD-CN", "SYNCRD", due=due, balance="-50.00", kind="3")
        self.from_customer(open_credit, "Invoice SYN-SYNCRD-0 attached.", days_ago=40,
                           sender="pat.crd@synth-crd.example", n=90, conv="c90")
        self.holder("SYNNOM")                     # no mail at all: no address to use
        self.doc("SYN-SYNNOM-1", "SYNNOM", due=due)
        self.doc("SYN-SYNUNR-1", "SYNUNR", due=due)
        self.w.obj("RelSageArOpenDocument", {        # no due date: the total is unknown
            "idcust": "SYNUNR", "idinvc": "SYN-SYNUNR-2", "dateinvc": "20260901",
            "amtduetc": "10.00", "codecurn": "CAD", "swpaid": "0", "trxtypetxt": "1"},
            binding="gen.sage.ar_open_document", audience="api:sage.ar", markings=FIN)
        _e, work = self.prepare(self.store())
        skip = {k[1]: w.get("skip") for k, w in work.items()}
        self.assertEqual(skip["SYNDIS"], "dispute_in_thread")
        self.assertEqual(skip["SYNPRO"], "payment_promised")
        self.assertEqual(skip["SYNCRN"], "credit_in_thread")
        self.assertEqual(skip["SYNCRD"], "open_credit_note")
        self.assertEqual(skip["SYNNOM"], "recipient_unknown")
        self.assertEqual(skip["SYNUNR"], "invoice_unreadable")
        # payment reported for one invoice: only that one is left out of the letter
        paid = work[("sage", "SYNPAY")]["work"]
        self.assertEqual([i["invoice"] for i in paid["invoices"]], ["SYN-SYNPAY-2"])
        self.assertEqual([h["reason"] for h in paid["held"]], ["payment_reported"])
        # a lapsed promise: chased, firmly
        self.assertEqual(work[("sage", "SYNOLD")]["work"]["tone"], "firm")
        self.assertEqual(len(work[("sage", "SYNNEG")]["work"]["invoices"]), 2)

    def test_a_remittance_received_after_the_due_date_holds_its_invoice(self):
        cust = self.holder()
        self.doc("SYN-IN-3001", due=DAY - timedelta(days=20))
        self.doc("SYN-IN-3002", due=DAY - timedelta(days=20))
        self.from_customer(cust, "Statement attached for invoice SYN-IN-2999.", days_ago=30)
        cite = {"l1_handle": "synth://remit/1", "object_id": "Attachment:fx:1",
                "markings": "[]"}
        sheet = self.w.obj("Attachment", {"name": "remit.xlsx"}, binding="graph_mail.attachments",
                           audience="mail:remit-box")
        cite = {**cite, "object_id": sheet}
        line = ps.Reading("f1", 0, 0, "A0", {"ref": ["SYN-IN-3001"], "amount_paid": ["500"]},
                          {}, "remit.xlsx", DAY - timedelta(days=1), cite)
        early = ps.Reading("f2", 0, 0, "A0", {"ref": ["SYN-IN-3002"], "amount_paid": ["500"]},
                           {}, "remit.xlsx", DAY - timedelta(days=25), cite)  # before due
        from operating import autopilot_receivables as ar
        with mock.patch.object(ar, "remittances",
                               lambda _st, _p: {("f1", 0): [line], ("f2", 0): [early]}):
            _e, work = self.prepare(self.store())
        w = work[("sage", "SYNTHCUST")]["work"]
        self.assertEqual([i["invoice"] for i in w["invoices"]], ["SYN-IN-3002"])
        self.assertEqual((w["held"][0]["reason"], w["held"][0]["because"]),
                         ("remittance_received", [sheet]))
        # the remittance's own record narrows the item: a reader who cannot open it never
        # sees the item that says it was received
        self.assertIn("mail:remit-box", work[("sage", "SYNTHCUST")]["audiences"])

    def test_a_held_invoices_reason_narrows_who_sees_the_item(self):
        from operating.tests.test_autopilot import READER, opens_all
        from operating.tests.test_proactive_threads import APPROVER_OID
        cust = self.holder()
        self.doc("SYN-IN-6001", due=DAY - timedelta(days=20))
        self.doc("SYN-IN-6002", due=DAY - timedelta(days=20))
        self.from_us(cust, "Attached is the statement for your account.", days_ago=1, n=1,
                     subject="Account statement")
        # the customer says it paid one invoice, in another person's (cleared) mailbox
        told = mail(self.w, DAY - timedelta(days=2), sender=PAYER, to=(OURS,),
                    body="Payment was sent on Monday for invoice SYN-IN-6001.",
                    subject="Paid", mailbox=APPROVER_OID, conversation="p2", n=77)
        self.w.link("Email.sent_by_customer", told, cust)
        reg = registry()
        off = self.cycle([], registry=ap.load_registry(), as_of=DAY)
        q = self.queue()
        self.assertIsNone(q.meta("collections_due_day"))      # off: no day marked
        self.assertEqual(q.items(None, ("open",)), [])
        summary = self.cycle([], registry=reg, as_of=DAY)
        self.assertEqual(q.meta("collections_due_day"), DAY.isoformat())
        items = q.items(None, ("open",))
        self.assertEqual(len(items), 1, {k: summary.get(k) for k in
                                         ("skipped", "prepared", "events_seen", "errors")})
        [item] = items
        work = item["body"]["prepared"]
        self.assertEqual([i["invoice"] for i in work["invoices"]], ["SYN-IN-6002"])
        self.assertEqual(work["held"][0]["because"], [told])
        self.assertIn(f"mail:{APPROVER_OID}", item["audiences"])
        self.assertIn("finance", item["markings"])
        self.assertIsNotNone(autopilot_reading.card_of(item))
        cleared = ["finance"]
        self.assertEqual(len(autopilot_reading.visible(q, reader=READER, may_read=lambda a: True,
                                        held=cleared, decide=opens_all)), 1)
        # cleared for the invoices and the payment contact's mailbox, not the one the
        # reason came from: the item is not shown at all
        self.assertEqual(autopilot_reading.visible(q, reader=READER,
                                    may_read=lambda a: a != f"mail:{APPROVER_OID}",
                                    held=cleared, decide=opens_all), [])
        self.assertEqual(autopilot_reading.visible(q, reader=READER, may_read=lambda a: True, held=[],
                                    decide=opens_all), [])      # no finance: nothing
        # the same day again: read once, nothing new
        again = self.cycle([], registry=reg, as_of=DAY)
        self.assertEqual(len(q.items(None, ("open",))), 1)
        self.assertTrue(off is not None and again is not None)

    def test_at_most_one_draft_per_customer_a_week(self):
        cust = self.holder()
        self.doc("SYN-IN-7001", due=DAY - timedelta(days=20))
        self.doc("SYN-IN-7002", due=DAY - timedelta(days=3))   # past the grace only tomorrow
        self.from_customer(cust, "Remittance for invoice SYN-IN-6990 attached.", days_ago=40)
        st = self.store()
        reg = registry()
        (today,) = ac.collection_events(st, reg, DAY, people())
        (later,) = ac.collection_events(st, reg, DAY + timedelta(days=1), people())
        self.assertEqual(later["fields"]["invoices"], 2)
        self.assertEqual(today["event_id"], later["event_id"])   # the week's one draft
        (next_week,) = ac.collection_events(st, reg, DAY + timedelta(days=7), people())
        self.assertNotEqual(next_week["event_id"], today["event_id"])

    # ------------------------------------------------------------------ people, fail closed

    def test_the_excluded_person_and_an_uncleared_mailbox_are_never_read(self):
        cust = self.holder()
        self.doc("SYN-IN-4001", due=DAY - timedelta(days=20))
        # the newest payment mail copies the excluded person: dropped whole, so the address
        # it would give is never used
        self.from_us(cust, "Invoice SYN-IN-4001 is past due.", days_ago=12,
                     to=("other.person@synth-customer.example",), cc=(BOSS,), n=1)
        hidden = mail(self.w, DAY - timedelta(days=11), sender="ann.lee@synth-customer.example",
                      to=(OURS,), body="Invoice SYN-IN-4001 query.", subject="Invoice",
                      mailbox=EXCLUDED_OID, conversation="x", n=99)
        self.w.link("Email.sent_by_customer", hidden, cust)
        self.from_customer(cust, "Remittance for invoice SYN-IN-3990 attached.", days_ago=40)
        st = self.store()
        events, work = self.prepare(st)
        w = work[("sage", "SYNTHCUST")]["work"]
        self.assertEqual(w["draft"]["to"], [{"address": PAYER}])
        self.assertNotIn(f"mail:{EXCLUDED_OID}", work[("sage", "SYNTHCUST")]["audiences"])
        self.assertNotIn(EXCLUDED_OID, [e.get("owner_hint") for e in events])
        self.assertGreaterEqual(st.collection_counts["message_not_cleared"], 1)

    def test_no_people_exclusion_or_unknown_params_prepare_nothing(self):
        cust = self.holder()
        self.doc("SYN-IN-5001", due=DAY - timedelta(days=20))
        self.from_customer(cust, "Remittance for invoice SYN-IN-4990.", days_ago=40)
        st = self.store()
        self.assertEqual(ac.collection_events(st, registry(), DAY, people(unavailable=True)), [])
        self.assertEqual(ac.collection_events(st, registry(), DAY, None), [])
        self.assertEqual(ac.collection_events(st, ap.load_registry(), DAY, people()), [])
        for bad in ({"grace_days": "3"}, {"grace_days": 40}, {"bogus_days": 1},
                    {"firm_days": True}):
            reg = registry()
            spec_of(reg)["params"].update(bad)
            self.assertEqual(ac.collection_events(st, reg, DAY, people()), [], bad)
        (event,) = ac.collection_events(st, registry(), DAY, people())
        ctx = autopilot_handlers.Ctx(st, DAY, float("inf"), ())
        st.autopilot_people = None
        self.assertEqual(ac.h_collection_draft(ctx, event),
                         {"skip": "people_exclusion_unavailable"})
        st.autopilot_people = people()
        self.assertEqual(ac.h_collection_draft(ctx, {**event, "customer": None}),
                         {"skip": "customer_unknown"})
        self.assertEqual(ac.h_collection_draft(ctx, {**event, "params": {"x": 1}}),
                         {"skip": "params_invalid"})

    def test_registry_ships_it_off_and_no_other_autopilot_takes_its_event(self):
        reg = ap.load_registry()
        spec = spec_of(reg)
        self.assertIs(spec["enabled"], False)
        self.assertEqual((spec["owner"], spec["markings"]), ("hinted", ["finance"]))
        event = {"event_type": "record.due", "object_type": "SalesInvoice",
                 "status": ac.HANDLER}
        self.assertEqual([s["id"] for s in registry()["autopilots"]
                          if s["on"] == "record.due"
                          and ap.matches(s.get("when") or {}, event)], ["collection_draft"])

    # ------------------------------------------------------------------ cardinality

    def test_one_draft_per_customer_at_1_10_and_60(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                for i in range(n):
                    code = f"SYNC{i:03d}"
                    cust = self.holder(code)
                    self.doc(f"SYN-{code}-1", code, due=DAY - timedelta(days=15))
                    self.doc(f"SYN-{code}-2", code, due=DAY - timedelta(days=8))
                    self.from_customer(cust, f"Remittance for invoice SYN-{code}-0.",
                                       days_ago=30, sender=f"pat.c{i}@synth-c{i}.example",
                                       n=i, conv=f"c{i}")
                st = self.store()
                # CPU time, not wall clock: a busy test host never fails it, an N+1 read
                # path still does
                began = time.process_time()
                events, work = self.prepare(st)
                took = time.process_time() - began
                self.assertEqual(len(events), n)
                self.assertEqual(len({e["event_id"] for e in events}), n)
                drafted = [w["work"] for w in work.values() if "work" in w]
                self.assertEqual(len(drafted), n)
                self.assertEqual(sorted(i["invoice"] for w in drafted for i in w["invoices"]),
                                 sorted(f"SYN-SYNC{i:03d}-{k}" for i in range(n)
                                        for k in (1, 2)))
                self.assertEqual({w["draft"]["to"][0]["address"] for w in drafted},
                                 {f"pat.c{i}@synth-c{i}.example" for i in range(n)})
                self.assertLess(took, 60)


if __name__ == "__main__":
    unittest.main()
