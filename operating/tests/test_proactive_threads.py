"""Approvals waiting and chases escalating (``operating.proactive_threads``) through the
engine and the per-reader inbox, the approvals queue, and the record document pack
(``operating.document_pack``).

SYNTHETIC fixtures only (``World`` stores, synthetic handles and addresses); evidence label:
isolated-fixture.
"""

from __future__ import annotations

from datetime import date, timedelta
from operating import proactive_engine as pe
import json
import time
import unittest
from unittest import mock

from operating import document_pack as dp
from operating import proactive_threads as pt
from operating.tests.test_proactive_engine import (AS_OF, EXCLUDED_OID, OURS_OID, Engine,
                                                   World)

RULES = list(pt.RULES)
OURS = "ops@synth-tenant.example"
APPROVER = "lead@synth-tenant.example"
APPROVER_OID = "00000000-0000-4000-8000-0000000000a2"
BOSS = "boss@synth-tenant.example"            # the excluded person
OUTSIDE = "buyer@synth-customer.example"
SECOND_BOX = APPROVER_OID          # the approver's own mailbox (cleared)
ASK = "Hi, please approve the attached price change for the next order. Internal: margin 41%."


def people(*, unavailable: bool = False):
    from integrations.people_exclusion import PeopleExclusion
    from operating.chunking.permissions import Exclusion

    guard = (PeopleExclusion(unavailable="excluded_people_oids_absent") if unavailable
             else PeopleExclusion([EXCLUDED_OID]))
    # the chats' members, as the Teams capture's directory gives them (c3 holds the
    # excluded person: never cleared)
    chats = {"c1": [OURS_OID, APPROVER_OID], "c2": [OURS_OID, APPROVER_OID],
             "c3": [OURS_OID, EXCLUDED_OID]}
    return Exclusion(guard, directory={OURS: OURS_OID, APPROVER: APPROVER_OID,
                                       BOSS: EXCLUDED_OID},
                     internal_domains=["synth-tenant.example"], chat_members=chats)


def day(offset: int) -> date:
    return AS_OF - timedelta(days=offset)


def mail(w: World, sent: date, *, sender: str = OURS, to=(APPROVER,), cc=(), body: str = ASK,
         subject: str = "Price change", mailbox: str = OURS_OID, conversation: str = "conv-1",
         n: int | None = None, hour: int = 15) -> str:
    return w.obj("Email", {"subject": subject, "sender": sender,
                           "to_addresses": json.dumps(list(to)),
                           "cc_addresses": json.dumps(list(cc)),
                           "sent_at": f"{sent.isoformat()}T{hour:02d}:00:00Z",
                           "body_preview": body, "conversation_id": conversation,
                           "mailbox": mailbox,
                           "internet_message_id": f"<t{n if n is not None else w.n}@synth>"},
                 binding="graph_mail.messages", audience=f"mail:{mailbox}")


def chase_thread(w: World, i: int, *, chases: int = 3, answered: bool = False) -> None:
    conv = f"chase-{i}"
    mail(w, day(20), sender=OURS, to=(OUTSIDE,), body="Your order is confirmed.",
         subject=f"Order {i}", conversation=conv, n=10_000 + i * 10)
    for k in range(chases):
        mail(w, day(12 - 3 * k), sender=OUTSIDE, to=(OURS,),
             body=(f"Following up on my last email: any update on the delivery date of {i}?"
                   if k % 2 == 0 else f"Gentle reminder, we are still waiting for {i}."),
             subject=f"Re: Order {i}", conversation=conv, n=10_000 + i * 10 + 1 + k)
    if answered:
        mail(w, day(1), sender=OURS, to=(OUTSIDE,), body="It ships Friday.",
             subject=f"Re: Order {i}", conversation=conv, n=10_000 + i * 10 + 9)


class Classifiers(unittest.TestCase):

    def test_negations_questions_promises_and_automated_mail_are_no_ask(self):
        for text, want in (("Please approve the attached PO.", True),
                           ("Can you approve this today?", True),
                           ("Attached is the artwork for your approval.", True),
                           ("No approval needed for this one.", False),
                           ("It was already approved, thanks.", False),
                           ("Does this need approval?", False),
                           ("I will need the approval forms by Friday.", False),
                           ("We will need the approval from head office to pay it.", False),
                           ("This invoice will be passed through for approval shortly.",
                            False),
                           ("Thanks for your approval, we will run the cartons.", False),
                           ("We will make a sample to send for your approval.", False),
                           ("> Please approve the PO\nThanks, done.", False)):
            with self.subTest(text=text):
                self.assertIs(pt.approval_ask(pt._own(text)), want)
        for text, want in (("Following up on my last email.", True),
                           ("Is there any update on this?", True),
                           ("Gentle reminder: please send the COA.", True),
                           ("Should I follow up with them?", False),
                           ("I will follow up tomorrow.", False),
                           ("Following up on our call today, here are the notes.", False),
                           ("No need to follow up.", False),
                           ("Meeting reminder: weekly review", False)):
            with self.subTest(text=text):
                self.assertIs(pt.chase_phrase(text), want)
        self.assertTrue(pt.automated("no-reply@x.example", "Please approve"))
        self.assertTrue(pt.automated("ap@x.example", "Please approve", "Automatic reply: PO"))
        self.assertTrue(pt.automated("a@x.example", "Thank you for your email. I am out of "
                                                    "the office until Monday."))
        self.assertTrue(pt.automated("a@x.example", "Please approve. To unsubscribe click"))
        self.assertFalse(pt.automated("a@x.example", "Please approve the PO.", "PO 12"))


class Threads(Engine):

    def setUp(self) -> None:
        super().setUp()
        patched = mock.patch.dict("os.environ", {pt.CHASE_FLAG: "1"})
        patched.start()
        self.addCleanup(patched.stop)

    def test_the_chase_rule_is_off_unless_turned_on(self):
        from operating import proactive_engine as pe
        with mock.patch.dict("os.environ", {pt.CHASE_FLAG: ""}):
            out = pt.chase_escalation(None, AS_OF, gap=pe._gap, cites=pe._cites,
                                      result=pe._result, skips=pe._Skips, people=people())
        self.assertEqual((out["gaps"], out["reasons"]), ([], ["off_by_default"]))


    def run_rules(self, w: World, **options) -> dict:
        options.setdefault("people", people())
        return self.run_engine(w, only=RULES, **options)

    def test_an_off_alert_type_reaches_no_reader_unless_they_switch_it_on(self):
        # approval_waiting is off in the registry (27% wrong when last measured): the rule
        # runs and stores its gaps, but a reader's inbox read (briefing, digest, agent
        # tools, alerts) returns none of them and counts none as hidden
        w, _ = self.approval_world()
        self.run_rules(w)
        self.assertTrue(self.kinds(pt.APPROVAL_KIND))                       # the rule ran
        self.assertEqual(self.read(kinds=[pt.APPROVAL_KIND], include_off=False)["gaps"], [])
        self.assertFalse(any(g["kind"] == pt.APPROVAL_KIND
                             for g in self.read(include_off=False, limit=1000)["gaps"]))
        switched = self.read(kinds=[pt.APPROVAL_KIND], include_off=False,
                             enabled=["approval_waiting"])["gaps"]
        self.assertTrue(switched)
        hidden = pe.hidden_counts(self.inbox, self.store, kinds=[pt.APPROVAL_KIND],
                                  may_read=lambda key: False, held=(), decide=lambda c: {})
        self.assertEqual(hidden.get(pt.APPROVAL_KIND, 0), 0)

    def kinds(self, *kinds: str, **read) -> list[dict]:
        return self.read(kinds=list(kinds), limit=1000, **read)["gaps"]

    def approval_world(self) -> tuple[World, str]:
        w = World()
        asked = mail(w, day(6), n=1)                                     # waiting
        mail(w, day(6), mailbox=SECOND_BOX, conversation="conv-1b", n=1)  # its copy
        mail(w, day(6), conversation="conv-2", n=2)                      # answered
        mail(w, day(5), sender=APPROVER, to=(OURS,), body="Approved.", conversation="conv-2",
             n=3)
        mail(w, day(6), sender="no-reply@synth-tool.example", conversation="conv-3", n=4)
        mail(w, day(6), cc=(BOSS,), conversation="conv-4", n=5)          # excluded person
        mail(w, day(6), mailbox=EXCLUDED_OID, conversation="conv-5", n=6)  # excluded box
        mail(w, day(6), body="No approval needed, FYI.", conversation="conv-6", n=7)
        mail(w, day(0), conversation="conv-7", n=8)                      # not waited yet
        return w, asked

    def test_an_unanswered_approval_is_raised_once_who_waits_on_whom_without_its_body(self):
        w, asked = self.approval_world()
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][pt.APPROVALS], "complete")
        (gap,) = self.kinds(pt.APPROVAL_KIND)
        event = gap["event"]
        self.assertEqual((event["asked_by"], event["waiting_on"], event["family"],
                          event["direction"], event["channel"]),
                         (OURS, [APPROVER], "price", "internal", "mail"))
        self.assertIn(gap["object_id"], {asked, *event["messages"]})
        self.assertNotIn("margin", json.dumps(gap))
        self.assertNotIn("please approve", json.dumps(gap).lower())
        skipped = summary["not_evaluated"][pt.APPROVALS]
        self.assertEqual(skipped.get("participants_not_cleared"), 1)

    def test_the_people_exclusion_unavailable_blocks_both_rules(self):
        w, _ = self.approval_world()
        chase_thread(w, 1)
        summary = self.run_rules(w, people=people(unavailable=True))
        for rule in RULES:
            self.assertEqual(summary["detectors"][rule], "blocked")
        self.assertEqual(self.kinds(pt.APPROVAL_KIND, pt.CHASE_KIND), [])

    def test_a_reader_without_every_copys_mailbox_sees_no_waiting_alert(self):
        w, _ = self.approval_world()
        self.run_rules(w)
        self.assertEqual(len(self.kinds(pt.APPROVAL_KIND)), 1)
        # the alert rests on no reply in either mailbox: a reader of one mailbox only sees
        # nothing (it might be answered where they may not look)
        one_box = self.kinds(pt.APPROVAL_KIND,
                             may_read=lambda key: key != f"mail:{SECOND_BOX}")
        self.assertEqual(one_box, [])
        nothing = self.kinds(pt.APPROVAL_KIND, may_read=lambda key: False)
        self.assertEqual(nothing, [])

    def test_chases_escalate_at_one_ten_and_sixty_threads_within_budget(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World()
                for i in range(n):
                    chase_thread(w, i)
                chase_thread(w, 900, answered=True)       # answered after the chases
                chase_thread(w, 901, chases=2)             # two chases only
                started = time.monotonic()
                summary = self.run_rules(w)
                self.assertLess(time.monotonic() - started, 60)
                self.assertEqual(summary["detectors"][pt.CHASES], "complete")
                gaps = self.kinds(pt.CHASE_KIND)
                self.assertEqual(len(gaps), n)
                self.assertEqual(len({g["object_id"] for g in gaps}), n)
                event = gaps[0]["event"]
                self.assertEqual((event["chases"], event["direction"], event["engaged"],
                                  event["party"]), (3, "inbound", True, "@synth-customer.example"))
                self.assertEqual(gaps[0]["follow_up"]["priority"], "high")

    def test_outreach_nobody_answered_is_no_chase_loop_and_a_mailing_is_no_approval_ask(self):
        w = World()
        chase_thread(w, 1)                                   # answered before: a loop
        cold = "rep@synth-vendor.example"                    # never heard from us
        for k in range(3):
            mail(w, day(12 - 3 * k), sender=cold, to=(OURS,), conversation="cold",
                 body="Just following up on my previous email about our services.",
                 subject="Re: Freight capacity", n=500 + k)
        for k in range(3):                                   # a sales pursuit, answered once
            mail(w, day(12 - 3 * k), sender=OUTSIDE, to=(OURS,), conversation="pursuit",
                 body="Following up: can we schedule a call about the opportunity?",
                 subject="Re: Order 1", n=700 + k)
        for k in range(3):                                   # one template, three times
            mail(w, day(12 - 3 * k), sender=OUTSIDE, to=(OURS,), conversation="template",
                 body=f"Checking in on supplies #{k}: anything we can help with?",
                 subject="Re: Order 1", n=800 + k)
        for c in range(3):                                   # our newsletter, re-sent
            for k in range(3):
                mail(w, day(12 - 3 * k), sender=OURS, to=(OUTSIDE,), conversation=f"news{c}",
                     body=f"Checking in on supplies today. Our monthly note #{k}: what's new.",
                     subject=f"News {c}", n=900 + c * 10 + k)
        many = tuple(f"p{i}@synth-tenant.example" for i in range(8))
        mail(w, day(6), to=many, conversation="mailing", n=600)
        summary = self.run_rules(w)
        gaps = self.kinds(pt.CHASE_KIND)
        self.assertEqual([g["event"]["party"] for g in gaps], ["@synth-customer.example"])
        self.assertNotIn("party_key", json.dumps(gaps))
        self.assertEqual(summary["detectors"][pt.CHASES], "complete")
        self.assertEqual(self.kinds(pt.APPROVAL_KIND), [])

    def test_a_teams_approval_ask_with_no_answer_from_who_it_mentions_is_raised(self):
        w = World()

        def teams(chat, sender, text, offset, mentions=()):
            return w.obj("TeamsChatMessage", {
                "created_at": f"{day(offset).isoformat()}T15:00:00Z", "chat_id": chat,
                "from": json.dumps({"id": sender, "kind": "user"}), "subject": text,
                "mentions": json.dumps([{"kind": "user", "target_id": m} for m in mentions])},
                binding="gen.teams.chat_messages", audience=f"teams:chat:{chat}")
        asked = teams("c1", OURS_OID, "Can you approve the label budget today?", 6,
                      (APPROVER_OID,))
        teams("c1", OURS_OID, "Adding the quote here.", 5)            # the asker again
        teams("c2", OURS_OID, "Can you approve the label budget today?", 6, (APPROVER_OID,))
        teams("c2", APPROVER_OID, "Approved.", 5)                      # answered
        teams("c3", OURS_OID, "Can you approve the budget?", 6, (EXCLUDED_OID,))  # excluded
        self.run_rules(w)
        (gap,) = [g for g in self.kinds(pt.APPROVAL_KIND) if g["event"]["channel"] == "teams"]
        self.assertEqual((gap["object_id"], gap["event"]["waiting_on"]), (asked, [APPROVER_OID]))
        self.assertEqual(self.kinds(pt.APPROVAL_KIND, may_read=lambda k: k != "teams:chat:c1"),
                         [])

    def test_the_word_index_and_the_whole_read_find_the_same_alerts(self):
        w, _ = self.approval_world()
        for i in range(3):
            chase_thread(w, i)
        self.run_rules(w)
        indexed = sorted(g["id"] for g in self.kinds(pt.APPROVAL_KIND, pt.CHASE_KIND))
        with mock.patch.object(pt, "_word_ids", return_value=None):
            self.run_rules(w)
        whole = sorted(g["id"] for g in self.kinds(pt.APPROVAL_KIND, pt.CHASE_KIND))
        self.assertEqual(len(indexed), 4)
        self.assertEqual(indexed, whole)

    def test_the_approvals_queue_groups_by_approver_and_only_drafts_nudges(self):
        w, _ = self.approval_world()
        self.run_rules(w)
        gaps = self.kinds(pt.APPROVAL_KIND, "mc_approval_pending")
        queue = pt.approvals_queue(gaps, AS_OF)
        self.assertEqual((queue["total"], queue["sent"], queue["writes"]), (1, 0, 0))
        (group,) = queue["groups"]
        self.assertEqual(group["waiting_on"], APPROVER)
        (item,) = group["items"]
        self.assertEqual(item["aging"], "3-5 business days")
        self.assertEqual((item["nudge"]["status"], item["nudge"]["sent"]),
                         ("draft_not_sent", False))
        self.assertNotIn("margin", item["nudge"]["body"])
        self.assertEqual(pt.approvals_queue([], AS_OF)["total"], 0)


# --------------------------------------------------------------------------- the pack

SO = "SalesOrder:bc.sales_orders:synth-so-1"
INV = "SalesInvoice:bc.sales_invoices:synth-inv-1"
CUSTOMER = "Customer:bc.customers:synth-cus-1"
OTHER_INV = "SalesInvoice:bc.sales_invoices:synth-inv-other"   # another order's invoice
SUP_INV = "SupplierInvoice:bc.purchase_invoices:synth-pinv-1"   # our cost for the order
PO = "PurchaseOrder:bc.purchase_orders:synth-po-1"
SALES_INV = "SalesInvoice:bc.sales_invoices:synth-inv-9"
LOT = "Lot:portal.lots:synth-lot-1"


def cited(text: str, handle: str) -> dict:
    return {"untrusted_text": text, "origin": {"citation": handle}}


class FakeReader:
    """The primitives as one reader gets them (synthetic): records, settled links and the
    documents around each record."""

    def __init__(self, docs: dict[str, list[dict]], *, address: bool = True) -> None:
        self.docs, self.address = docs, address
        self.paths: list = []

    def __call__(self, tool: str, args: dict) -> dict:
        if tool == "plx.object":
            oid = args["object_id"]
            values = []
            if oid == CUSTOMER and self.address:
                values = [{"property": "email", "value": "ap@synth-customer.example",
                           "citation": {"l1_handle": "synth://cus#email"}, "markings": []}]
            return {"object": {"display": cited(oid.rsplit("-", 1)[-1].upper(),
                                                f"synth://{oid}")}, "values": values}
        if tool == "plx.around":
            self.paths.append(args.get("path"))
            if not args.get("path"):   # every neighbour, the customer and its orders too
                return {"edges": [{"from": INV, "to": SO, "tier": "verified"},
                                  {"from": SO, "to": CUSTOMER, "tier": "verified"},
                                  {"from": OTHER_INV, "to": CUSTOMER, "tier": "verified"}]}
            link = args["path"][0]["link_type"]
            edges = {(SO, "SalesOrder.ordered_by"): [(SO, CUSTOMER, "verified")],
                     (SO, "SalesInvoice.bills"): [(INV, SO, "verified")],
                     (SO, "Shipment.ships"): [("Shipment:bc.shipments:lead", SO, "lead")],
                     # the supplier side of the same business: never a customer's pack
                     (SO, "SupplierInvoice.for_sales_order"): [(SUP_INV, SO, "verified")],
                     (SO, "SupplierConfirmation.for_sales_order"): [(SUP_INV, SO,
                                                                     "verified")]}
            if args["from"] == PO:   # whatever link is asked, a sales invoice comes back
                return {"edges": [{"from": SALES_INV, "to": PO, "tier": "verified",
                                   "link_type": link}]}
            return {"edges": [{"from": a, "to": b, "tier": t, "link_type": link}
                              for a, b, t in edges.get((args["from"], link), [])]}
        if tool == "plx.documents":
            return {"documents": self.docs.get(args["object_id"], [])}
        raise AssertionError(tool)


def doc(title: str, handle: str, kind: str = "attachment") -> dict:
    return {"citation": handle, "kind": kind, "title": {"untrusted_text": title},
            "date": "2026-11-01"}


class DocumentPack(unittest.TestCase):

    def docs(self) -> dict[str, list[dict]]:
        return {SO: [doc("COA lot 77.pdf", "h-coa"), doc("Packing slip 1.pdf", "h-ps"),
                     doc("Secret BOL.pdf", "h-bol-hidden"), doc("photo.jpg", "h-photo")],
                INV: [doc("Invoice 1001.pdf", "h-inv")],
                # the customer's and another order's documents: never this order's pack
                CUSTOMER: [doc("Statement October.pdf", "h-statement"),
                           doc("Invoice 2002.pdf", "h-other-inv")],
                OTHER_INV: [doc("Invoice 2002.pdf", "h-other-inv"),
                            doc("BOL 2002.pdf", "h-other-bol")],
                SUP_INV: [doc("Supplier invoice 7007 cost.pdf", "h-sup-inv")],
                SALES_INV: [doc("Invoice 9009.pdf", "h-sales-inv")],
                PO: [doc("PO confirmation 1.pdf", "h-po-conf")],
                LOT: [doc("COA lot 1.pdf", "h-lot-coa"),
                      dict(doc("Spec sheet other.pdf", "h-spec-other"),
                           mentions=[{"object_id": "Customer:bc.customers:synth-cus-2",
                                      "tier": "verified"}]),
                      dict(doc("Packing slip 55.pdf", "h-lot-ps"),
                           mentions=[{"object_id": LOT, "tier": "lead"}])]}

    def test_the_pack_lists_cited_documents_the_reader_may_open_and_drafts_without_sending(
            self):
        call = FakeReader(self.docs())
        decide = lambda hs: {h: (None if h != "h-bol-hidden" else {"reason": "item"})
                             for h in hs}
        pack = dp.build(call, frozenset(), SO, decide=decide)
        families = [d["family"] for d in pack["documents"]]
        # the invoice document found around the linked invoice, and the invoice record itself
        self.assertEqual(families, ["invoice", "invoice", "packing_slip", "coa"])
        self.assertEqual({d["citation"] for d in pack["documents"] if d["kind"] != "record"},
                         {"h-coa", "h-ps", "h-inv"})
        self.assertNotIn("Secret", json.dumps(pack))
        # only the order's own transaction links are walked: never its customer, never
        # another order's invoice or BOL, whatever the customer holds
        for absent in ("2002", "Statement", "h-other", "h-statement"):
            self.assertNotIn(absent, json.dumps(pack))
        walked = {p[0]["link_type"] for p in call.paths if p}
        self.assertNotIn(None, [p for p in call.paths])
        self.assertTrue(walked >= {"SalesInvoice.bills", "Shipment.ships"})
        self.assertFalse({w for w in walked if w.endswith((".ordered_by", ".billed_to"))}
                         - {"SalesOrder.ordered_by"})
        self.assertIn("bol", pack["missing"])
        self.assertEqual(pack["draft"]["to"]["address"], "ap@synth-customer.example")
        self.assertEqual((pack["draft"]["status"], pack["sent"], pack["writes"]),
                         ("draft_not_sent", False, 0))
        self.assertIn("To follow: Order confirmation, Bill of lading", pack["draft"]["body"])
        self.assertEqual(pack["card"]["id"], "pack:" + SO)
        self.assertEqual(pack["card"]["draft"]["channel"], "email")
        self.assertEqual(sorted(pack["draft"]["attach"]), ["h-coa", "h-inv", "h-ps"])

    def test_an_undecidable_reader_gets_no_document_and_a_lead_link_is_not_followed(self):
        call = FakeReader(self.docs(), address=False)

        def broken(_handles):
            raise RuntimeError("policy unreadable")
        pack = dp.build(call, frozenset(), SO, decide=broken)
        self.assertEqual([d for d in pack["documents"] if d["kind"] != "record"], [])
        self.assertIn("item_access_unchecked", {g["code"] for g in pack["gaps"]})
        self.assertNotIn("lead", json.dumps([d["via"] for d in pack["documents"]]))
        self.assertIsNone(pack["draft"]["to"])
        self.assertIn("no customer address you may read is recorded", pack["draft_missing"])
        # a decision that leaves a handle out refuses it (fail closed)
        pack = dp.build(FakeReader(self.docs()), frozenset(), SO, decide=lambda hs: {})
        self.assertEqual([d for d in pack["documents"] if d["kind"] != "record"], [])

    def test_each_side_reads_only_its_own_side_and_a_lot_only_its_certificates(self):
        admit_all = lambda hs: {h: None for h in hs}
        call = FakeReader(self.docs())
        pack = dp.build(call, frozenset(), SO, decide=admit_all)
        self.assertNotIn("h-sup-inv", json.dumps(pack))           # our cost, never sent
        self.assertNotIn("Supplier", json.dumps(pack))
        self.assertFalse([p for p in call.paths if p and "Supplier" in p[0]["link_type"]])
        pack = dp.build(FakeReader(self.docs()), frozenset(), PO, decide=admit_all)
        self.assertNotIn("h-sales-inv", json.dumps(pack))         # a customer's invoice
        self.assertEqual([d["citation"] for d in pack["documents"]], ["h-po-conf"])
        pack = dp.build(FakeReader(self.docs()), frozenset(), LOT, decide=admit_all)
        # its COA only: another customer's spec sheet and a packing slip a lead names are not
        self.assertEqual([d["citation"] for d in pack["documents"]], ["h-lot-coa"])
        self.assertIsNone(pack["draft"]["party"])

    def test_unknown_record_types_and_unreadable_records_are_refused(self):
        with self.assertRaises(dp.PackRefused):
            dp.build(FakeReader({}), frozenset(), "Person:x:1", decide=None)
        refusing = lambda tool, args: {"error": {"code": "not_found"}}
        with self.assertRaises(dp.PackRefused):
            dp.build(refusing, frozenset(), SO, decide=None)


if __name__ == "__main__":
    unittest.main()
