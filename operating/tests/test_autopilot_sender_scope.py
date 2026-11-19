"""Autopilot replies that look a record up by a number in inbound mail and state it back to
the sender name only the sender's own customer's records (``autopilot_handlers.senders_own``).

A PO, order or invoice number is no key across customers: C100 mails about PO 45160 and only
C200's SO-9999 carries 45160; no draft to C100 may name SO-9999, its status or dates. With
the sender's customer unresolved and a record matched, the handler holds (no draft). Each
path runs on the Postgres store shape (``_PostgresStore``: the identifier table answers)."""
from __future__ import annotations

import json
import sqlite3
import time
import unittest

from operating import autopilot_handlers as ah
from operating import autopilot_work as aw
from operating import proactive_engine as pe
from operating.tests import test_autopilot as ta
from operating import autopilot_events as ae
from operating.tests.test_proactive_engine import EXCLUDED_OID
from operating.tests.test_proactive_threads import APPROVER, APPROVER_OID, OURS, OURS_OID, OUTSIDE

THEIRS = "SO-9999"
TENANT = "synth-tenant.example"
GUEST_OID = "00000000-0000-4000-8000-0000000000b7"


class SenderScope(ta.Fixture):

    def setUp(self) -> None:
        super().setUp()
        self.ids: list[tuple[str, str, str, str]] = []   # (kind, number, record, property)

    # ---------------------------------------------------------------- world

    def new_customer(self, code: str) -> str:
        return self.w.obj("Customer", {"name": f"Synth buyer {code}", "erp_code": code,
                                       "email": f"buyer@{code.lower()}.example"})

    def so(self, number: str, po: str, customer: str | None) -> str:
        order = self.w.obj("SalesOrder", {"order_number": number, "customer_po": po,
                                          "status": "Open", "required_date": "2026-10-30",
                                          "order_date": "2026-10-10"})
        if customer:
            self.w.link("SalesOrder.ordered_by", order, customer)
        cpo = self.w.obj("CustomerPurchaseOrder", {"po_number": po})
        self.w.link("SalesOrder.references_customer_po", order, cpo)
        self.ids += [("customer_po", po, cpo, "po_number"),
                     ("order_number", number, order, "order_number")]
        return order

    def invoice(self, number: str, customer: str, order: str | None = None) -> str:
        inv = self.w.obj("SalesInvoice", {"invoice_number": number, "total_amount": "100.00",
                                          "remaining_amount": "40.00", "currency": "CAD",
                                          "due_date": "2026-11-30"})
        self.w.link("SalesInvoice.billed_to", inv, customer)
        if order:
            self.w.link("SalesInvoice.bills", inv, order)
        self.ids.append(("invoice_number", number, inv, "invoice_number"))
        return inv

    def theirs(self, po: str = "45160") -> str:
        """C200's order SO-9999 under customer PO ``po``."""
        self.c200 = self.new_customer("C200")
        return self.so(THEIRS, po, self.c200)

    def mail(self, body: str, *, subject: str = "Status", sender: str = OUTSIDE,
             customer: bool = True) -> str:
        return self.message(1, body, subject=subject, sender=sender, customer=customer)

    def ctx(self, domains=()) -> ah.Ctx:
        path = self.dir / "s.sqlite"
        self.w.build(path)
        with sqlite3.connect(path) as c:
            for kind, number, oid, prop in self.ids:
                c.execute("INSERT INTO identifiers (identifier_kind, normalized, raw, role, "
                          "object_id, property, l1_handle, field_path, lineage, recorded_at) "
                          "VALUES (?, ?, ?, 'primary', ?, ?, 'h', ?, 'workbench', "
                          "'2026-01-01T00:00:00Z')",
                          (kind, number.upper(), number, oid, prop, f"row.{prop}"))
        st = pe._Store(path, threads=1)
        self.addCleanup(st.close)
        return ah.Ctx(ta._PostgresStore(st), ta.NOW.date(), time.monotonic() + 60,
                      tuple(domains))

    def event(self, mail: str, text: str, *, subject: str = "Status", sender: str = OUTSIDE,
              intents=("status_request",), **more) -> dict:
        return {"object_id": mail, "subject": subject, "text": text, "sender": sender,
                "records": [], "intents": list(intents), "channel": "mail", **more}

    def run_paths(self, text: str, **kw) -> dict[str, dict]:
        """Every status-style reply path on one mail: the customer-request answer, the
        owner's desk answer and the reply prep."""
        sender = kw.pop("sender", OUTSIDE)
        msg = self.mail(text, sender=sender, customer=kw.pop("customer", True))
        ctx = self.ctx(kw.pop("domains", ()))
        ev = self.event(msg, text, sender=sender, **kw)
        return {"answer_request": ah.h_answer_request(ctx, ev),
                "desk": aw.h_desk(ctx, ev),
                "reply_prep": aw.h_reply_prep(ctx, ev)}

    def no_draft_names(self, out: dict, *words: str) -> None:
        for name, got in out.items():
            drafts = json.dumps({k: got.get(k) for k in ("draft", "teams_draft")})
            for word in words:
                self.assertNotIn(word, drafts, name)

    # ---------------------------------------------------------------- status replies

    def test_another_customers_order_under_the_same_po_is_never_named_to_the_sender(self):
        """C100 asks about PO 45160; only C200's SO-9999 carries it (the review's probe)."""
        self.theirs("45160")
        out = self.run_paths("Hello, what is the status of PO 45160?")
        self.no_draft_names(out, THEIRS, "45160", "2026-10-30")
        self.assertEqual(out["answer_request"], {"skip": "no_own_record_named"})
        self.assertEqual(out["desk"], {"skip": "no_own_record_named"})
        self.assertNotIn("draft", out["reply_prep"])

    def test_another_customers_order_named_by_its_order_number_is_never_stated(self):
        self.theirs("45160")
        out = self.run_paths(f"Any update on order {THEIRS}?")
        self.no_draft_names(out, THEIRS, "2026-10-30")
        self.assertIn("skip", out["answer_request"])

    def test_the_senders_own_order_is_answered_beside_another_customers(self):
        self.theirs("45160")
        mine = self.so("SO-7001", "45160", self.customer)
        out = self.run_paths("Hello, what is the status of PO 45160?")
        self.no_draft_names(out, THEIRS)
        self.assertEqual([o["object_id"] for o in out["answer_request"]["orders"]], [mine])
        for name in out:
            self.assertIn("SO-7001", out[name]["draft"]["body"], name)

    def test_an_unresolved_sender_holds_and_names_nothing(self):
        self.theirs("45160")
        out = self.run_paths("What is the status of PO 45160?", customer=False,
                             sender="someone@unknown-buyer.example")
        self.no_draft_names(out, THEIRS, "45160")
        self.assertEqual(out["answer_request"], {"skip": "status_customer_ambiguous"})
        self.assertEqual(out["desk"], {"skip": "status_customer_ambiguous"})
        self.assertEqual(out["reply_prep"]["gaps"], ["sender_customer_unresolved"])

    def test_an_order_with_no_customer_is_never_named_to_an_outside_sender(self):
        self.so(THEIRS, "45160", None)
        out = self.run_paths("Status of PO 45160 please?")
        self.no_draft_names(out, THEIRS)

    def test_an_outside_address_copied_on_an_internal_ask_scopes_the_answer(self):
        """An internal sender with a customer copied: the reply reaches the customer, so the
        same scope holds (the internal sender resolves to no customer: hold)."""
        self.theirs("45160")
        out = self.run_paths("Status of PO 45160?", sender=OURS, customer=False,
                             domains=("synth-tenant.example",),
                             reply_all={"to": [], "cc": [OUTSIDE]})
        self.no_draft_names(out, THEIRS)

    def test_an_all_internal_thread_is_answered_unscoped(self):
        """Colleagues asking each other see every customer's order (no outside reader)."""
        order = self.theirs("45160")
        out = self.run_paths("Status of PO 45160?", sender=OURS, customer=False,
                             domains=("synth-tenant.example",))
        self.assertEqual([o["object_id"] for o in out["answer_request"]["orders"]], [order])

    def test_an_outside_mail_marked_internal_is_still_scoped(self):
        """Only a Teams chat among directory people is internal by its side; a mail's
        side (a party link) never widens what an outside address is told."""
        self.theirs("45160")
        out = self.run_paths("Status of PO 45160?", side="internal")
        self.no_draft_names(out, THEIRS)

    def test_a_shipment_or_invoice_number_of_another_customers_order_is_never_followed(self):
        """A shipment or invoice record handed in with the mail leads to its order: that
        order is the other customer's, so nothing is named."""
        order = self.theirs("45160")
        inv = self.invoice("70011", self.c200, order)
        msg = self.mail("Where is my delivery?")
        ctx = self.ctx()
        got = ah.h_answer_request(ctx, self.event(msg, "Where is my delivery?",
                                                  records=[inv]))
        self.assertNotIn(THEIRS, json.dumps(got))

    # ---------------------------------------------------------------- invoice / payment

    def test_another_customers_invoice_balance_is_never_stated(self):
        other = self.new_customer("C200")
        self.invoice("70011", other)
        msg = self.mail("Has invoice 70011 been paid?")
        got = ah.h_ledger(self.ctx(), self.event(msg, "Has invoice 70011 been paid?",
                                                 intents=("payment_question",)))
        self.assertEqual(got, {"skip": "no_own_invoice_named"})

    def test_the_senders_own_invoice_is_answered_beside_another_customers(self):
        self.invoice("70011", self.new_customer("C200"))
        mine = self.invoice("70011", self.customer)
        msg = self.mail("Has invoice 70011 been paid?")
        got = ah.h_ledger(self.ctx(), self.event(msg, "Has invoice 70011 been paid?"))
        self.assertEqual([x["object_id"] for x in got["ledger"]], [mine])
        self.assertIn("40.00 CAD", got["draft"]["body"])

    def test_an_invoice_asked_by_an_unresolved_sender_holds(self):
        self.invoice("70011", self.new_customer("C200"))
        msg = self.mail("Has invoice 70011 been paid?", customer=False,
                        sender="ap@unknown-buyer.example")
        got = ah.h_ledger(self.ctx(), self.event(msg, "Has invoice 70011 been paid?",
                                                 sender="ap@unknown-buyer.example"))
        self.assertEqual(got, {"skip": "invoice_customer_ambiguous"})

    def test_an_invoice_billing_another_customers_order_is_never_stated(self):
        """Billed to C100 on paper but for C200's order: the two disagree, so it holds."""
        order = self.theirs("45160")
        self.invoice("70011", self.customer, order)
        msg = self.mail("Has invoice 70011 been paid?")
        got = ah.h_ledger(self.ctx(), self.event(msg, "Has invoice 70011 been paid?"))
        self.assertEqual(got, {"skip": "no_own_invoice_named"})

    # ---------------------------------------------------------------- ship-to / bill-to

    def ship_to(self, code: str, *, linked: bool) -> str:
        """A ship-to card of C100 (``bill_to_erp_code`` C100); ``linked``: the payer link
        ``Customer.billed_to`` is declared in the data."""
        card = self.w.obj("Customer", {"name": f"Synth ship-to {code}", "erp_code": code,
                                       "bill_to_erp_code": "C100",
                                       "email": f"dock@{code.lower()}.example"})
        if linked:
            self.w.link("Customer.billed_to", card, self.customer)
        return card

    def test_a_ship_tos_order_is_answered_to_its_declared_bill_to(self):
        order = self.so("SO-7101", "45170", self.ship_to("C100-S1", linked=True))
        out = self.run_paths("Status of PO 45170?")
        self.assertEqual([o["object_id"] for o in out["answer_request"]["orders"]], [order])
        self.assertIn("SO-7101", out["desk"]["draft"]["body"])

    def test_a_ship_tos_order_without_a_declared_link_holds(self):
        """``bill_to_erp_code`` alone is no relationship: nothing is named."""
        self.so("SO-7101", "45170", self.ship_to("C100-S1", linked=False))
        out = self.run_paths("Status of PO 45170?")
        self.no_draft_names(out, "SO-7101", "45170")
        self.assertEqual(out["answer_request"], {"skip": "no_own_record_named"})

    def test_a_ship_to_never_reads_its_bill_tos_or_a_siblings_orders(self):
        """The link reads ship-to -> bill-to only: the ship-to asking is told neither the
        payer's order nor another ship-to's."""
        mine = self.ship_to("C100-S1", linked=True)
        self.so("SO-7201", "45180", self.customer)
        self.so("SO-7202", "45180", self.ship_to("C100-S2", linked=True))
        own = self.so("SO-7203", "45180", mine)
        sender = "dock@c100-s1.example"
        out = self.run_paths("Status of PO 45180?", sender=sender, customer=False)
        self.no_draft_names(out, "SO-7201", "SO-7202")
        self.assertEqual([o["object_id"] for o in out["answer_request"]["orders"]], [own])

    # ---------------------------------------------------------------- meeting follow-up

    def meeting(self, attendees, domains=("synth-tenant.example",)) -> dict:
        """C100 (SO-7001) and C200 (SO-9999) are both parties of the meeting; returns the
        follow-up ``h_meeting`` prepares for ``attendees``."""
        self.theirs("45160")
        self.so("SO-7001", "45161", self.customer)
        meeting = self.w.obj("Meeting", {"subject": "Quarterly review"})
        ctx = self.ctx(domains)
        return ah.h_meeting(ctx, {"object_id": meeting, "fields": {"subject": "Quarterly review"},
                                  "parties": [self.customer, self.c200],
                                  "attendees": list(attendees)})

    @staticmethod
    def drafts(got: dict) -> list[dict]:
        return [d for d in [got.get("draft"), *(got.get("customer_notices") or ())] if d]

    def test_a_meeting_with_two_customers_splits_the_follow_up_by_customer(self):
        got = self.meeting([OURS, OUTSIDE, "buyer@c200.example"])
        self.assertNotIn("draft", got)            # never one draft listing both to both
        self.assertIn("attendees_span_customers", got["gaps"])
        by_to = {tuple(t["address"] for t in d["to"]): d["body"]
                 for d in got["customer_notices"]}
        self.assertEqual(set(by_to), {(OUTSIDE,), ("buyer@c200.example",)})
        self.assertIn("SO-7001", by_to[(OUTSIDE,)])
        self.assertNotIn(THEIRS, by_to[(OUTSIDE,)])
        self.assertIn(THEIRS, by_to[("buyer@c200.example",)])
        self.assertNotIn("SO-7001", by_to[("buyer@c200.example",)])

    def test_a_meeting_with_one_customer_names_only_that_customers_orders(self):
        """Both customers are parties; only C100 attended from outside."""
        got = self.meeting([OURS, OUTSIDE])
        self.assertEqual([t["address"] for t in got["draft"]["to"]], [OUTSIDE])
        self.assertIn("SO-7001", got["draft"]["body"])
        self.assertNotIn(THEIRS, json.dumps(got))
        self.assertNotIn("customer_notices", got)

    def test_an_internal_only_meeting_lists_every_partys_open_orders(self):
        got = self.meeting([OURS])
        self.assertEqual(got["draft"]["to"], [])
        self.assertIn("SO-7001", got["draft"]["body"])
        self.assertIn(THEIRS, got["draft"]["body"])
        self.assertEqual(got["gaps"], ["no_external_attendee"])

    def test_an_attendee_no_customer_states_is_told_no_order(self):
        alone = self.meeting([OURS, "guest@unknown-firm.example"])
        self.assertNotIn("SO-7001", alone["draft"]["body"])
        self.assertNotIn(THEIRS, alone["draft"]["body"])
        self.assertIn("attendee_customer_unknown", alone["gaps"])
        self.setUp()
        mixed = self.meeting([OUTSIDE, "guest@unknown-firm.example"])
        self.assertNotIn("draft", mixed)
        for d in mixed["customer_notices"]:
            if d["to"][0]["address"] != OUTSIDE:
                self.assertNotIn("SO-7001", d["body"])
                self.assertNotIn(THEIRS, d["body"])

    def test_a_supplier_attendee_is_told_no_customers_order(self):
        self.w.obj("Supplier", {"name": "Synth vendor", "email": "rep@vendor.example"})
        got = self.meeting([OURS, "rep@vendor.example"])
        for d in self.drafts(got):
            self.assertNotIn("SO-7001", d["body"])
            self.assertNotIn(THEIRS, d["body"])

    # ---------------------------------------------------------------- Teams chats

    def chat_event(self, members: list[str], text: str, *, sender: str = GUEST_OID,
                   audience: str = "teams:chat:g1") -> dict:
        """The one event ``_teams_events`` makes of a message ``sender`` posts in a chat of
        ``members`` @mentioning the owner, with the capture's people (the review's probe:
        every chat event was typed internal whoever its members were)."""
        from integrations.people_exclusion import PeopleExclusion
        from operating.chunking.permissions import Exclusion
        msg = self.w.obj("TeamsChatMessage", {
            "created_at": "2026-10-20T14:00:00Z", "subject": text,
            "from": json.dumps({"display_name": "Synth person", "id": sender}),
            "mentions": json.dumps([{"id": OURS_OID}])},
            audience=audience, binding="graph_teams.messages")
        ctx = self.ctx((TENANT,))
        people = Exclusion(PeopleExclusion([EXCLUDED_OID]), directory={OURS: OURS_OID, APPROVER: APPROVER_OID},
                           internal_domains=[TENANT],
                           chat_members={audience.split(":", 2)[2]: members})
        directory, _a, _d = ae._people_maps(people)
        skipped = __import__("collections").Counter()
        events = [e for e in ae._teams_events(ctx.st, [msg], {msg: ("TeamsChatMessage", audience)},
                                               people, directory, skipped)
                  if e["mailbox"] == OURS_OID]
        self.assertEqual(len(events), 1, skipped)
        self._chat_ctx = ctx
        return events[0]

    def chat_reply(self, members: list[str], text: str = "What is the status of PO 45160?",
                   **kw) -> tuple[dict, dict]:
        ev = self.chat_event(members, text, **kw)
        return ev, aw.h_reply_prep(self._chat_ctx, ev)

    def test_a_guest_in_a_chat_never_reads_another_customers_order(self):
        """The review's finding: a C100 buyer is a guest in the chat (the capture lists
        their id and address); C200's SO-9999 under the shared PO 45160 is never put in
        the chat draft."""
        theirs = self.theirs("45160")
        ev, got = self.chat_reply([OURS_OID, APPROVER_OID, GUEST_OID, OUTSIDE])
        self.assertEqual(ev["outside"], sorted([GUEST_OID, OUTSIDE]))
        self.assertNotIn("teams_draft", got)
        self.no_draft_names({"reply_prep": got}, THEIRS, "2026-10-30")
        # the guest's own order under the same number is answered, the other's still not
        mine = self.so("SO-7001", "45160", self.customer)
        ev, got = self.chat_reply([OURS_OID, APPROVER_OID, GUEST_OID, OUTSIDE])
        self.assertIn("SO-7001", got["teams_draft"]["body"])
        self.no_draft_names({"reply_prep": got}, THEIRS)
        # a guest's #EXT# UPN reads as their own address
        upn = {**ev, "outside": [GUEST_OID, "buyer_synth-customer.example#EXT#@" + TENANT]}
        self.assertEqual(ah.senders_own(self._chat_ctx, upn, [theirs, mine]), [mine])

    def test_an_unknown_member_or_a_channel_holds_and_a_directory_chat_is_unscoped(self):
        order = self.theirs("45160")
        # a member id with no address held (a guest listed by id only): hold
        ev, got = self.chat_reply([OURS_OID, APPROVER_OID, GUEST_OID])
        self.assertEqual(ev["outside"], [GUEST_OID])
        self.assertIsNone(ah.senders_own(self._chat_ctx, ev, [order]))
        self.assertNotIn("teams_draft", got)
        self.assertEqual(got["gaps"], ["sender_customer_unresolved"])
        # guests of two customers in one chat: neither's order is named
        ev, _got = self.chat_reply([OURS_OID, GUEST_OID, OUTSIDE, "x@c200.example",
                                    "00000000-0000-4000-8000-0000000000b8"])
        self.assertEqual(ah.senders_own(self._chat_ctx, ev, [order]), [])
        # a channel's members are not captured: hold; an event without membership: hold
        ev = self.chat_event([], "Status of PO 45160?", sender=APPROVER_OID,
                             audience="teams:channel:t1:19:x@thread.tacv2")
        self.assertIsNone(ev["outside"])
        self.assertIsNone(ah.senders_own(self._chat_ctx, ev, [order]))
        self.assertIsNone(ah.senders_own(self._chat_ctx, {"channel": "teams", "side": "internal",
                                                          "object_id": "x"}, [order]))
        # every member a directory person: colleagues see every customer's order
        ev, got = self.chat_reply([OURS_OID, APPROVER_OID], sender=APPROVER_OID)
        self.assertEqual(ev["outside"], [])
        self.assertIn(THEIRS, got["teams_draft"]["body"])

    def test_a_multi_address_or_guest_string_is_no_internal_reader(self):
        order = self.theirs("45160")
        msg = self.mail("Status of PO 45160?", sender=OURS, customer=False)
        ctx = self.ctx((TENANT,))
        for raw in (f"x@evil.example {OURS}", f"buyer_evil.example#EXT#@{TENANT}"):
            with self.subTest(raw=raw):
                ev = self.event(msg, "Status of PO 45160?", sender=OURS,
                                reply_all={"to": [], "cc": [raw]})
                self.assertIsNone(ah.senders_own(ctx, ev, [order]))
        self.assertEqual(ah.senders_own(ctx, self.event(msg, "Status?", sender=OURS), [order]),
                         [order])

    # ---------------------------------------------------------------- cardinality

    def test_scope_keeps_every_own_order_and_drops_every_other_at_1_10_60(self):
        for n in (1, 10, 60):
            with self.subTest(orders=n):
                self.setUp()
                other = self.new_customer("C200")
                mine = [self.so(f"SO-1{i:03d}", f"PO-{i}", self.customer) for i in range(n)]
                theirs = [self.so(f"SO-2{i:03d}", f"PO-{i}", other) for i in range(n)]
                msg = self.mail("Status?")
                ctx = self.ctx()
                started = time.monotonic()
                got = ah.senders_own(ctx, self.event(msg, "Status?"), [*theirs, *mine])
                self.assertLess(time.monotonic() - started, 5)
                self.assertEqual(got, mine)


    def test_meeting_split_keeps_every_attendee_once_at_1_10_60(self):
        for n in (1, 10, 60):
            with self.subTest(attendees=n):
                self.setUp()
                ours = [f"p{i}@synth-customer.example" for i in range(n)]
                theirs = [f"p{i}@c200.example" for i in range(n)]
                started = time.monotonic()
                got = self.meeting([OURS, *ours, *theirs])
                self.assertLess(time.monotonic() - started, 5)
                by_to = {d["customer"]: [t["address"] for t in d["to"]]
                         for d in got["customer_notices"]}
                self.assertEqual(by_to, {"C100": ours, "C200": theirs})

if __name__ == "__main__":
    unittest.main()
