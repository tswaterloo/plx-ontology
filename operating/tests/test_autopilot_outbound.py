"""What an Autopilot message may carry to a reader outside the company
(operating/autopilot_outbound.py), at the proposal boundary both the assistant and the job
path use (``autopilot_agent_io.outbound_proposals``) and end to end through a live cycle: a
proposed mail to an outside address that names another party's order or PO in any written
form, another party by the word that leads its name, or any long number the recipient has
not seen is held back; each outside recipient is checked on their own."""

from __future__ import annotations

import json
import time

from operating import autopilot_agent_io as io
from operating import autopilot_handlers as ah
from operating import proactive_engine as pe
from operating.tests import test_autopilot as ta
from operating.tests.test_autopilot_agent import OutsideReaders, Session, assistant, final

DOMAINS = (ta.OURS.split("@", 1)[1],)
SUPPLIER = "sales@vendor.example"


def send(to, body, subject="Your order"):
    return {"arguments": {"system": "mail", "operation": "send_email", "target": {},
                          "change": {"to": list(to), "subject": subject, "body": body},
                          "reason": "Reply."}}


class ProposalPath(ta.Fixture):
    def setUp(self) -> None:
        super().setUp()
        w = self.w
        other = w.obj("Customer", {"name": "Northwind Soapworks Inc", "code": "C200",
                                   "erp_code": "C200", "email": "ap@northwind.example"})
        theirs = w.obj("SalesOrder", {"order_number": "SO-9931", "status": "Open"})
        w.link("SalesOrder.ordered_by", theirs, other)
        own = w.obj("SalesOrder", {"order_number": "SO-4410", "status": "Open"})
        w.link("SalesOrder.ordered_by", own, self.customer)
        supplier = w.obj("Supplier", {"name": "Vendor Example Ltd", "rep_email": SUPPLIER})
        po = w.obj("PurchaseOrder", {"po_number": "PO100109", "status": "Open"})
        w.link("PurchaseOrder.ordered_from", po, w.obj("Supplier", {"name": "Other mill"}))
        mine = w.obj("PurchaseOrder", {"po_number": "PO100200", "status": "Open"})
        w.link("PurchaseOrder.ordered_from", mine, supplier)
        self.identified += [("so_number", "SO-9931", theirs, "order_number"),
                            ("so_number", "SO-4410", own, "order_number"),
                            ("petra_po", "PO100109", po, "po_number"),
                            ("petra_po", "PO100200", mine, "po_number")]
        self.build()
        st = pe._Store(self.dir / "s.sqlite", threads=1)
        self.addCleanup(st.close)
        self.ctx = ah.Ctx(st, ta.NOW.date(), time.monotonic() + 60, DOMAINS)

    def held(self, proposals, *, text="Where is my order?", account=(), cc=()):
        event = {"sender": ta.OUTSIDE, "to": [ta.OURS], "cc": list(cc), "subject": "Order",
                 "channel": "mail"}
        kept, back = io.outbound_proposals(proposals, event, {"me": [ta.OURS]}, "me", text,
                                           list(account), lambda: self.ctx)
        return [p["arguments"]["change"]["body"] for p in kept], [b["why"] for b in back]

    def test_another_partys_order_or_po_in_any_written_form_is_held(self):
        for body in ("SO9931 ships first.", "SO 9931 ships first.", "SO-9931 ships first.",
                     "Order 9931 ships first.", "order #9931 ships first.",
                     "9931 ships first.", "PO-100109 is late.", "PO 100109 is late.",
                     "PO#100109 is late.", "P.O. 100109 is late."):
            with self.subTest(body=body):
                kept, why = self.held([send([ta.OUTSIDE], body)])
                self.assertEqual(kept, [])
                self.assertIn(why[0], ("another_partys_record", "number_not_on_their_records"))
        # the stored record found through a normal form: named as another party's record
        for body in ("SO 9931 ships first.", "PO-100109 is late.", "PO 100109 is late."):
            with self.subTest(named=body):
                self.assertEqual(self.held([send([ta.OUTSIDE], body)])[1],
                                 ["another_partys_record"])

    def test_their_own_numbers_pass_in_any_written_form(self):
        account = ["Order SO-4410: open, ships Friday."]
        for body in ("SO 4410 ships Friday.", "Your order 4410 ships Friday.",
                     "SO4410 ships Friday."):
            with self.subTest(body=body):
                self.assertEqual(self.held([send([ta.OUTSIDE], body)], account=account),
                                 ([body], []))
        # a number they wrote themselves, however they wrote it
        self.assertEqual(self.held([send([ta.OUTSIDE], "Noted: PO 88123 ships Friday.")],
                                   text="Please confirm our PO#88123."),
                         (["Noted: PO 88123 ships Friday."], []))

    def test_any_long_number_they_have_not_seen_is_held_dates_and_quantities_are_not(self):
        self.assertEqual(self.held([send([ta.OUTSIDE], "Reference 557731 applies.")])[1],
                         ["number_not_on_their_records"])
        self.assertEqual(self.held([send([ta.OUTSIDE], "Batch 2026 is ready.")])[1],
                         ["number_not_on_their_records"])
        fine = ("We ship 1200 kg on 2026-12-03, as planned since May 2026.",
                "Can we talk on December 3, 2026?")
        for body in fine:
            with self.subTest(body=body):
                self.assertEqual(self.held([send([ta.OUTSIDE], body)]), ([body], []))

    def test_another_party_named_by_the_word_that_leads_its_name_is_held(self):
        self.assertEqual(self.held([send([ta.OUTSIDE], "Northwind takes the next lot.")])[1],
                         ["another_partys_name"])
        self.assertEqual(self.held([send([ta.OUTSIDE], "Northwind Soapworks first.")])[1],
                         ["another_partys_name"])
        # the recipient themselves, or a word they wrote, is not another party
        self.assertEqual(self.held([send(["ap@northwind.example"],
                                         "Northwind is next in line.")],
                                   cc=["ap@northwind.example"]),
                         (["Northwind is next in line."], []))

    def test_two_outside_parties_each_get_only_their_own_records(self):
        """The customer's party line names their order; the supplier copied on the thread
        may not be told it, the customer may."""
        account = ["Order SO-4410: open, ships Friday."]
        body = "SO-4410 needs your stock by Friday."
        self.assertEqual(self.held([send([SUPPLIER], body)], account=account, cc=[SUPPLIER])[1],
                         ["another_partys_record"])
        self.assertEqual(self.held([send([ta.OUTSIDE], body)], account=account,
                                   cc=[SUPPLIER]), ([body], []))
        # one message to both: held (the supplier may not read the customer's order)
        self.assertEqual(self.held([send([ta.OUTSIDE, SUPPLIER], body)], account=account,
                                   cc=[SUPPLIER])[1], ["another_partys_record"])
        # the supplier's own PO in their line: to the supplier yes, to the customer no
        po_line = ["PO100200: open, due Friday."]
        self.assertEqual(self.held([send([SUPPLIER], "PO 100200 is due Friday.")],
                                   account=po_line, cc=[SUPPLIER])[0],
                         ["PO 100200 is due Friday."])
        self.assertEqual(self.held([send([ta.OUTSIDE], "PO 100200 is due Friday.")],
                                   account=po_line, cc=[SUPPLIER])[1],
                         ["another_partys_record"])

    def test_cardinality_every_proposal_checked_none_lost(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                props = [send([ta.OUTSIDE], f"SO {9931 if i % 2 else 4410} update {i}.")
                         for i in range(n)]
                kept, why = self.held(props, account=["Order SO-4410: open."])
                self.assertEqual(len(kept) + len(why), n)
                self.assertEqual(len(why), n // 2)
                self.assertTrue(all("4410" in k for k in kept))


class LiveProposal(OutsideReaders):
    """The assistant on an inside thread proposes a mail to
    an outside address writing another customer's order as "SO 9931": held back."""

    def test_a_proposed_mail_with_another_customers_order_in_a_variant_form_is_held(self):
        for body in ("SO 9931 first.", "order 9931 first.", "SO9931 first."):
            with self.subTest(body=body):
                self.setUp()
                leak = {"system": "mail", "operation": "send_email", "target": {},
                        "change": {"to": ["sales@vendor.example"], "subject": "Stock",
                                   "body": body}, "reason": "Ask the supplier."}
                msg = self.message(95, "Please chase the supplier on stock.",
                                   sender=ta.APPROVER, customer=False)
                session = Session(calls=[("plx.prepare_write", leak)],
                                  answer=final(summary="Chase the supplier"))
                out, items = self.live([msg], assistant(session))
                self.assertFalse([i for i in items if body in json.dumps(json.loads(
                    i["body"])["prepared"].get("jobs", []))])
                self.assertEqual(out["skipped"].get("assistant:outbound_held"), 1, out)


if __name__ == "__main__":
    import unittest
    unittest.main()
