"""Chained fast answers (``operating.fast_chain``) at their output boundary over stubbed
tool results: what is said is cited, "nothing is late" only when every order's verdict
proves it, a proposal never acts, and what the reads leave unsure declines or is said."""

from __future__ import annotations

import re
import unittest

from operating import fast_answer as fa
from operating import fast_chain as fc
from operating.tests.test_fast_answer import StubReads, _cell

READ_TOOLS = {"plx.find", "plx.object", "plx.read", "plx.around", "plx.process",
              "plx.aggregate", "plx.documents"}
VENDOR_BC = 'Supplier:bc.vendors:{"id":"v1"}'
BC_PO = "bc://production/c1/purchaseOrders/bc1@g-1"


class ChainUnderstandTests(unittest.TestCase):
    def test_the_owners_chained_questions_name_their_intent_and_record(self):
        cases = {
            "PO#4820 is late - what should we do next and who do we chase? dont send "
            "anything, just tell me": ("po_followup", "PO#4820", True),
            "next step on late PO#4820? who to call": ("po_followup", "PO#4820", True),
            "PO#4820: who is the vendor, how late is it, and what did they last email us":
                ("po_followup", "PO#4820", False),
            "give me the full story on PO#4820 - vendor, due date, last email":
                ("po_followup", "PO#4820", False),
            "did the vendor confirm PO#4846? when": ("po_confirmed", "PO#4846", False),
            "did we get an order confirmation on PO#4846": ("po_confirmed", "PO#4846", False),
            # whether they got back to us at all is what the confirmation answer says
            "has the supplier gotten back to us at all on PO#4846? any confirmation":
                ("po_confirmed", "PO#4846", False),
            "how do we know about PO#4820? show me the original email":
                ("po_first_mail", "PO#4820", False),
            "source email for PO#4831?": ("po_first_mail", "PO#4831", False),
            "anything late from Acme Labels, Inc.?": ("vendor_late", "Acme Labels, Inc", False),
            "Acme (PACKAGING) - any POs overdue?": ("vendor_late", "Acme (PACKAGING)", False),
            # a name holding a negation is the name's, not the question's
            "anything late from Acme Labels (DO NOT USE)?":
                ("vendor_late", "Acme Labels (DO NOT USE)", False),
        }
        for question, (intent, mention, asks_next) in cases.items():
            ask = fa.understand(question)
            self.assertIsNotNone(ask, question)
            self.assertEqual((ask.intent, ask.mention, ask.asks_next),
                             (intent, mention, asks_next), question)
            self.assertTrue(fa.serves(question), question)
        # an ETA question stays po_status's; a frame, a negation, a count or a record
        # number is not a vendor's lateness
        self.assertEqual(fa.understand("when will PO#4820 arrive, has the vendor confirmed "
                                       "the eta").intent, "po_status")
        for question in ("anything late from Acme this week?", "anything not late from Acme?",
                         "anything late?", "how many POs late from Acme"):
            ask = fa.understand(question)
            self.assertFalse(ask is not None and ask.intent == "vendor_late", question)
        self.assertNotEqual(getattr(fa.understand("anything late from PO#4820?"), "intent",
                                    None), "vendor_late")


def _verdict(late, state, due=None, gap=None, reasons=()):
    out = {"stage": "receipt", "late": late, "state": state, "reasons": list(reasons),
           "governing_date": due, "measured_against": "today" if late is not None else None,
           "values": [], "gaps": [], "gap": gap}
    if due:
        out["values"] = [{"property": "requested_date", "value": due,
                          "citation": {"l1_handle": f"bc://lines/{due}",
                                       "field_path": "row.expectedReceiptDate"}}]
    return out


class VendorLateTests(unittest.TestCase):
    """'Anything late from <vendor>': each open order judged by its own verdict."""

    def answers(self, verdicts, *, truncated=False):
        orders = [{"object_id": f'PurchaseOrder:bc.purchase_orders:{{"id":"{n}"}}',
                   "object_type": "PurchaseOrder",
                   "display": _cell(n, f"bc://po/{n}", "row.number")} for n in verdicts]
        card = {"object": {"object_id": VENDOR_BC, "object_type": "Supplier",
                           "display": _cell("Acme Labels", "bc://v/1", "row.displayName")},
                "matched": {"raw": _cell("Acme Labels", "bc://v/1", "row.displayName")}}

        def answers(name, args):
            if name == "plx.find":
                return {"candidates": [card]}
            if name == "plx.object":
                return {"object": card["object"]}
            if name == "plx.around" and args["from"] == VENDOR_BC:
                return {"set": "set:1", "objects": orders, "completeness": {
                    "complete": not truncated, "truncated": truncated,
                    "next_cursor": "c" if truncated else None}}
            if name == "plx.around":
                return {"objects": [], "edges": []}    # no identity twins
            if name == "plx.process":
                number = re.search(r'"id":"([^"]+)"', args["from"]).group(1)
                verdict = verdicts[number]
                return {"lateness": [verdict] if verdict is not None else []}
            return None
        return answers

    def run_vendor(self, verdicts, **kw):
        return _run_ask(fc.vendor_late, self.answers(verdicts, **kw),
                        fa.Ask("vendor_late", "Acme Labels", None))

    def test_nothing_late_is_said_only_when_every_open_orders_verdict_proves_it(self):
        draft, reads = self.run_vendor({
            "PO1": _verdict(False, "not_due", "2026-12-01"),
            "PO2": None,                                        # not open: never late
            "PO3": _verdict(None, "undetermined", gap={"reason": "no_due_date"})})
        said = " ".join(draft.sentences)
        self.assertIn("Nothing is late from Acme Labels", said)
        self.assertIn("PO1 (due 2026-12-01)", said)
        self.assertIn("no requested-by date on record", said)     # said apart, cited
        self.assertIn("PO3", [v["value"] for v in draft.values])
        self.assertEqual({n for n, _a in reads.calls} - READ_TOOLS, set())
        # one order the records leave unsettled: never "nothing late", the order named
        draft, _ = self.run_vendor({
            "PO1": _verdict(False, "not_due", "2026-12-01"),
            "PO4": _verdict(None, "undetermined", gap={"reason": "due_dates_disagree"})})
        said = " ".join(draft.sentences)
        self.assertNotIn("Nothing is late", said)
        self.assertNotRegex(said.lower(), r"\bnot late\b|\bnothing\b.*\blate\b|\bis late\b")
        self.assertIn("PO4", said)
        self.assertEqual(draft.answerability, "partial")

    def test_a_late_order_is_named_with_its_due_date_and_a_cut_listing_declines(self):
        draft, _ = self.run_vendor({"PO1": _verdict(True, "late", "2026-07-14"),
                                    "PO2": _verdict(False, "not_due", "2026-12-01")})
        said = " ".join(draft.sentences)
        self.assertIn("this purchase order is overdue: PO1, due 2026-07-14", said)
        self.assertNotIn("Nothing is late", said)
        with self.assertRaises(fa.Unanswerable):
            self.run_vendor({"PO1": _verdict(False, "not_due", "2026-12-01")},
                            truncated=True)


PO_ROOT = 'PurchaseOrder:portal.app_purchase_order:{"id":"p1"}'
FM_ROOT = 'PurchaseOrder:portal.fm_generated_purchase_order:{"id":7}'


class PoChainTests(unittest.TestCase):
    def answers(self, mail, *, late=True):
        po_cell = _cell("PO#4820", "api://po/p1", "row.po_number")
        rows = {BC_PO: {"row.vendorName": "Acme Labels Ltd"},
                "api://fm/7": {"row.contactrep": "Pat Rep", "row.emailrep": "pat@acme.com",
                               "row.phonerep": "416-555-0100", "row.emailcsr": None}}
        rows.update({d["citation"]: {"row.subject": d["subject"], "row.received_at": d["date"]}
                     for d in mail})

        def answers(name, args):
            if name == "plx.find":
                return {"candidates": [{"object": {"object_id": PO_ROOT,
                                                   "object_type": "PurchaseOrder",
                                                   "display": po_cell},
                                        "tier": "verified",
                                        "matched": {"raw": po_cell, "normalized": "PO4820"}}]}
            if name == "plx.process":
                return {"lateness": [_verdict(True, "late", "2026-07-14") if late else
                                     _verdict(False, "not_due", "2026-12-01")]}
            if name == "plx.object":
                return {"object": {"object_id": PO_ROOT}, "values": [
                    {"property": "status", "value": "B.Order",
                     "citation": {"l1_handle": "api://po/p1", "field_path": "row.status"}},
                    {"property": "bc_status", "value": "Open",
                     "citation": {"l1_handle": BC_PO, "field_path": "row.status"}}]}
            if name == "plx.around" and args["path"][0]["link_type"].endswith("ordered_from"):
                return {"objects": [{"object_id": VENDOR_BC, "object_type": "Supplier",
                                     "display": _cell("Acme", "bc://v/1", "row.displayName")}]}
            if name == "plx.around":
                return {"objects": [{"object_id": FM_ROOT, "object_type": "PurchaseOrder",
                                     "display": _cell("7", "api://fm/7", "row.id")}]}
            if name == "plx.read":
                return {"status": "resolved", "citation": args["citation"],
                        "fields": rows[args["citation"]]}
            if name == "plx.documents":
                self.assertEqual(args["participant"], "@acme.com")   # the rep's domain
                return {"documents": [{k: v for k, v in d.items() if k != "subject"}
                                      for d in mail]}
            return None
        return answers

    def mail(self, *subjects):
        return [{"citation": f"graph-mail://m/{at}", "kind": "email", "direction": "from_them",
                 "date": f"2026-0{at + 1}-10T15:00:00Z", "subject": subject,
                 "title": _cell(subject, f"graph-mail://m/{at}", "row.subject")}
                for at, subject in enumerate(subjects)]

    def test_the_next_step_is_a_proposal_from_cited_facts_and_never_an_action(self):
        mail = self.mail("Order Confirmation for PO 4820", "RE: PO#4820 delay")
        ask = fa.understand("PO#4820 is late - what should we do next and who do we chase? "
                            "dont send anything, just tell me")
        draft, reads = _run_ask(fc.po_followup, self.answers(mail), ask)
        said = " ".join(draft.sentences)
        self.assertIn("PO#4820 is overdue: it was due 2026-07-14", said)
        self.assertIn("The vendor is Acme Labels Ltd.", said)
        self.assertIn("Pat Rep, pat@acme.com, 416-555-0100", said)
        self.assertIn('2026-02-10, subject "RE: PO#4820 delay"', said)
        self.assertIn("Proposed next step (nothing has been sent): ask Pat Rep", said)
        values = {v["name"]: v for v in draft.values}
        for name, handle in (("vendor", BC_PO), ("vendor_contact_email", "api://fm/7"),
                             ("last_message_date", "graph-mail://m/1")):
            self.assertEqual(values[name]["citations"][0]["l1_handle"], handle)
        self.assertEqual({n for n, _a in reads.calls} - READ_TOOLS, set())   # reads only
        # the story question asks no next step: none is proposed
        draft, _ = _run_ask(fc.po_followup, self.answers(mail), fa.understand(
            "PO#4820: who is the vendor, how late is it, and what did they last email us"))
        self.assertNotIn("Proposed next step", " ".join(draft.sentences))

    def test_a_confirmation_is_the_vendors_first_one_and_none_found_declines(self):
        ask = fa.understand("did the vendor confirm PO#4820? when")
        mail = self.mail("Order Confirmation for PO 4820", "Order Confirmation for PO 4820 rev",
                         "Please confirm PO 4820")
        draft, _ = _run_ask(fc.po_confirmed, self.answers(mail), ask)
        said = " ".join(draft.sentences)
        self.assertIn("Yes, the vendor confirmed PO#4820", said)
        self.assertIn('2026-01-10, subject "Order Confirmation for PO 4820"', said)
        # a request to confirm is no confirmation; a search never proves there is none
        with self.assertRaises(fa.Unanswerable):
            _run_ask(fc.po_confirmed, self.answers(self.mail("Please confirm PO 4820")), ask)


class _VendorMail:
    """Stubbed reads of one late order, its FileMaker rep and its vendor mail pages."""

    def answers(self, pages, emailrep="pat@acme.com", needles=None, linked=()):
        po_cell = _cell("PO#4820", "api://po/p1", "row.po_number")
        rows = {BC_PO: {"row.vendorName": "Acme Labels Ltd"},
                "api://fm/7": {"row.contactrep": "Pat Rep", "row.emailrep": emailrep,
                               "row.phonerep": None, "row.emailcsr": None}}
        for page in pages:
            for d in page:
                rows[d["citation"]] = {"row.subject": d["subject"],
                                       "row.received_at": d["date"],
                                       "row.body_preview": d.get("preview", ""),
                                       "row.body_text": d.get("body", "")}
        searched = needles if needles is not None else []

        def answers(name, args):
            if name == "plx.find":
                return {"candidates": [{"object": {"object_id": PO_ROOT,
                                                   "object_type": "PurchaseOrder",
                                                   "display": po_cell},
                                        "tier": "verified",
                                        "matched": {"raw": po_cell, "normalized": "PO4820"}}]}
            if name == "plx.process":
                return {"lateness": [_verdict(True, "late", "2026-07-14")]}
            if name == "plx.object":
                return {"object": {"object_id": PO_ROOT}, "values": [
                    {"property": "bc_status", "value": "Open",
                     "citation": {"l1_handle": BC_PO, "field_path": "row.status"}}]}
            if name == "plx.around" and args["path"][0]["link_type"].endswith("ordered_from"):
                return {"objects": [{"object_id": VENDOR_BC, "object_type": "Supplier",
                                     "display": _cell("Acme", "bc://v/1", "row.displayName")}]}
            if name == "plx.around":
                return {"objects": [{"object_id": FM_ROOT, "object_type": "PurchaseOrder",
                                     "display": _cell("7", "api://fm/7", "row.id")}]}
            if name == "plx.read":
                return {"status": "resolved", "citation": args["citation"],
                        "fields": rows[args["citation"]]}
            if name == "plx.documents" and "object_id" in args:   # the order's linked mail
                return {"documents": list(linked), "completeness": {}}
            if name == "plx.documents":
                searched.append(args["participant"])
                at = int(args.get("cursor") or 0)
                listed = [{k: v for k, v in d.items() if k not in ("subject", "preview",
                                                                   "body")}
                          for d in pages[at]] if at < len(pages) else []
                more = at + 1 < len(pages)
                return {"documents": listed, "completeness": {
                    "next_cursor": str(at + 1) if more else None, "truncated": more}}
            return None
        return answers

    def doc(self, n, day, subject, direction="from_them", preview="", body="",
            mentions=()):
        handle = f"graph-mail://m/{n}"
        return {"citation": handle, "kind": "email", "direction": direction,
                "date": f"{day}T15:00:00Z", "subject": subject, "preview": preview,
                "body": body, "title": _cell(subject, handle, "row.subject"),
                "mentions": [{"normalized": m} for m in mentions]}

class LatestVendorMailTests(_VendorMail, unittest.TestCase):
    """The vendor's latest message about an order (the follow-up's, and "what's the last
    thing the vendor said"): mail from the rep's domains, newest first, read page by page
    until the newest one naming the order in its subject or opening is settled. In
    production the first page often held only messages naming the order further down
    (a payment statement) or none from the vendor, and the answer declined
    (``mail_pages_cut``) or named the statement."""

    def test_the_latest_names_the_order_up_front_and_is_read_across_pages(self):
        ask = fa.understand("PO#4820: who is the vendor, how late is it, and what did "
                            "they last email us")
        statement = self.doc(1, "2026-03-01", "Payment inquiry - account 101905",
                             preview="Please see the open items below.",
                             body="... PO#4820 $1,200 ...", mentions=("PO4820",))
        pages = [[statement,
                  self.doc(2, "2026-02-20", "RE: PO#4820", direction="to_them"),
                  self.doc(3, "2026-02-10", "Invoice 200184820")],   # a digit run, no PO
                 [self.doc(4, "2026-01-15", "RE: [External] RE: PO#4820 ETA"),
                  self.doc(5, "2026-01-02", "PO 4820")]]
        draft, reads = _run_ask(fc.po_followup, self.answers(pages), ask)
        said = " ".join(draft.sentences)
        self.assertIn('2026-01-15, subject "RE: [External] RE: PO#4820 ETA"', said)
        values = {v["name"]: v for v in draft.values}
        self.assertEqual(values["last_message_date"]["citations"][0]["l1_handle"],
                         "graph-mail://m/4")
        self.assertEqual([n for n, _a in reads.calls].count("plx.documents"), 2)
        # the same reads answer "what's the last thing the vendor said": the same message
        draft, _ = _run_ask(fc.RECIPES["vendor_last_mail"], self.answers(pages),
                            fa.understand("whats the last thing the vendor said about "
                                          "PO#4820"))
        self.assertEqual({v["name"]: v for v in draft.values}["message_date"]["value"],
                         "2026-01-15")

    def test_a_page_that_settles_the_latest_ends_the_search(self):
        pages = [[self.doc(1, "2026-03-01", "Re: PO#4820 shipping next week"),
                  self.doc(2, "2026-02-01", "Fwd: something else")],
                 [self.doc(3, "2026-01-01", "PO#4820")]]
        draft, reads = _run_ask(fc.po_followup, self.answers(pages), fa.understand(
            "next step on late PO#4820? who to call"))
        self.assertIn('2026-03-01, subject "Re: PO#4820 shipping next week"',
                      " ".join(draft.sentences))
        self.assertEqual([n for n, _a in reads.calls].count("plx.documents"), 1)

    def test_a_latest_past_every_page_read_declines(self):
        pages = [[self.doc(i, f"2026-0{9 - i}-01", f"Statement {i}")]
                 for i in range(fc.MAX_MAIL_PAGES + 1)]
        ask = fa.understand("whats the last thing the vendor said about PO#4820")
        with self.assertRaises(fa.Unanswerable):
            _run_ask(fc.RECIPES["vendor_last_mail"], self.answers(pages), ask)
        # every page read and none names the order: the last-mail answer declines (the
        # agent searches wider); the follow-up says it found none, partial
        pages = [[self.doc(1, "2026-03-01", "Statement", body="PO#4820")]]
        with self.assertRaises(fa.Unanswerable):
            _run_ask(fc.RECIPES["vendor_last_mail"], self.answers(pages), ask)
        draft, _ = _run_ask(fc.po_followup, self.answers(pages), fa.understand(
            "next step on late PO#4820? who to call"))
        self.assertIn("I found no message from the vendor naming PO#4820", " ".join(
            draft.sentences))
        self.assertEqual(draft.answerability, "partial")

    def test_a_rep_field_of_several_addresses_is_said_as_typed_and_each_domain_searched(self):
        needles = []
        pages = [[self.doc(1, "2026-03-01", "PO#4820 update")]]
        draft, _ = _run_ask(fc.po_followup, self.answers(
            pages, emailrep="pat@acme.com (Pat); ops@acme-labs.com", needles=needles),
            fa.understand("next step on late PO#4820? who to call"))
        values = {v["name"]: v for v in draft.values}
        self.assertEqual(values["vendor_contact_email"]["value"],
                         "pat@acme.com (Pat); ops@acme-labs.com")
        self.assertEqual(needles, ["@acme.com", "@acme-labs.com"])
        # free text holding an address is no contact address: nothing said, the supplier's
        # own addresses are searched instead
        needles = []
        draft, _ = _run_ask(fc.po_followup, self.answers(
            pages, emailrep="call Pat at pat@acme.com", needles=needles),
            fa.understand("next step on late PO#4820? who to call"))
        self.assertNotIn("vendor_contact_email", {v["name"] for v in draft.values})
        self.assertEqual(needles, [VENDOR_BC])


class NoVendorMailTests(_VendorMail, unittest.TestCase):
    """"Has the supplier gotten back to us at all on PO#1? any confirmation": with no
    message naming the order anywhere the reader can see, that is the answer (partial);
    in production it went to the agent, which abstained after 28 s."""

    def test_no_message_naming_the_order_is_said_and_any_other_miss_declines(self):
        ask = fa.understand("has the supplier gotten back to us at all on PO#4820? any "
                            "confirmation")
        # their one message the search matched names another number ("Invoice 200184820")
        draft, _ = _run_ask(fc.po_confirmed, self.answers(
            [[self.doc(1, "2026-02-01", "Invoice 200184820")]]), ask)
        said = " ".join(draft.sentences)
        self.assertIn("I found no message naming PO#4820 in the mail you can see", said)
        self.assertIn("no vendor confirmation is on record", said)
        self.assertIn("not linked to records yet", said)
        self.assertEqual(draft.answerability, "partial")
        # mail linked to the order, or vendor mail naming it with no confirmation subject:
        # a confirmation may be in it, so the agent reads it
        linked = [self.doc(9, "2026-02-01", "RE: PO#4820", direction="to_them")]
        with self.assertRaises(fa.Unanswerable):
            _run_ask(fc.po_confirmed, self.answers([[]], linked=linked), ask)
        with self.assertRaises(fa.Unanswerable):
            _run_ask(fc.po_confirmed, self.answers([[self.doc(1, "2026-02-01",
                                                              "RE: PO#4820 dates")]]), ask)


class FirstMailTests(unittest.TestCase):
    def run_first(self, docs, rows):
        po_cell = _cell("PO#4820", "api://po/p1", "row.po_number")
        rows = dict(rows, **{"api://fm/7": {"row.emailrep": "pat@acme.com"}})
        windows = []

        def answers(name, args):
            if name == "plx.find":
                return {"candidates": [{"object": {"object_id": PO_ROOT,
                                                   "object_type": "PurchaseOrder",
                                                   "display": po_cell}, "tier": "verified",
                                        "matched": {"raw": po_cell, "normalized": "PO4820"}}]}
            if name == "plx.object":
                return {"object": {"object_id": PO_ROOT}, "values": [
                    {"property": "portal_created_at", "value": "2025-10-31T11:51:57",
                     "citation": {"l1_handle": "api://po/p1", "field_path": "row.created_at"}}]}
            if name == "plx.around":
                return {"objects": [{"object_id": FM_ROOT, "object_type": "PurchaseOrder",
                                     "display": _cell("7", "api://fm/7", "row.id")}]}
            if name == "plx.documents":
                # the order's linked mail, never a text search of its number
                self.assertEqual((args["object_id"], args.get("query")), (PO_ROOT, None))
                windows.append((args["since"], args.get("until")))
                return {"documents": [
                    {"citation": h, "kind": "email", "date": d,
                     "title": _cell(subject, h, "row.subject"), "mentions": m}
                    for h, d, subject, m, _f in docs
                    if args["since"] <= d[:10] <= (args.get("until") or "9999")]}
            if name == "plx.read":
                return {"status": "resolved", "citation": args["citation"],
                        "fields": rows[args["citation"]]}
            return None
        draft, _ = _run_ask(fc.po_first_mail, answers,
                            fa.understand("how do we know about PO#4820? show me the "
                                          "original email"))
        return draft, windows

    def test_the_original_is_the_first_received_message_that_names_the_order(self):
        names = [{"normalized": "PO4820"}]
        docs = [  # oldest first: a sent copy, a digit match naming no order, a third party,
                  # one naming it only below its opening,
                  # a message linked through its attachment only, then the vendor's own
                  # message, listed with its mentions trimmed off (named in its subject)
            ("graph-mail://m/0", "2025-11-01T10:00:00Z", "RE: PO#4820", names,
             {"row.from": "us@ours.com", "row.mailbox_upn": "us@ours.com"}),
            ("graph-mail://m/1", "2025-11-02T10:00:00Z", "Project 14820 review", [],
             {"row.from": "us@ours.com", "row.mailbox_upn": "us@ours.com",
              "row.received_at": "2025-11-02T10:00:00Z"}),
            ("graph-mail://m/2", "2025-11-03T10:00:00Z", "Fwd: PO 4820", names,
             {"row.from": "x@broker.com", "row.mailbox_upn": "us@ours.com",
              "row.received_at": "2025-11-03T10:00:00Z"}),
            # another order's confirmation thread naming it only further down its text
            ("graph-mail://m/5", "2025-11-03T11:00:00Z", "RE: Order C777 Confirmation", names,
             {"row.from": "us@ours.com", "row.mailbox_upn": "us@ours.com",
              "row.received_at": "2025-11-03T11:00:00Z", "row.body_preview": "Thanks, see below",
              "row.body_text": "Thanks, see below\n> also add to PO#4820"}),
            ("graph-mail://m/4", "2025-11-03T12:00:00Z", "Year end count", [],
             {"row.from": "us@ours.com", "row.mailbox_upn": "us@ours.com",
              "row.received_at": "2025-11-03T12:00:00Z", "row.body_text": "see attached"}),
            ("graph-mail://m/3", "2025-11-04T10:00:00Z", "Order Confirmation PO 4820", [],
             {"row.from": "pat@acme.com", "row.mailbox_upn": "us@ours.com",
              "row.received_at": "2025-11-04T10:00:00Z"})]
        rows = {h: dict(fields, **{"row.subject": subject}) for h, _d, subject, _m, fields
                in docs}
        draft, _ = self.run_first(docs, rows)
        said = " ".join(draft.sentences)
        self.assertIn('sent on 2025-11-04, subject "Order Confirmation PO 4820"', said)
        # mail captured since the generation was built is not linked yet: said, partial
        self.assertIn("not linked to records yet and was not searched", said)
        self.assertEqual(draft.answerability, "partial")
        self.assertIn("newer_mail_cut", [g["reason"] for g in draft.gaps])

    def test_windows_follow_one_another_until_one_holds_the_first_message(self):
        docs = [("graph-mail://m/9", "2025-12-20T10:00:00Z", "PO 4820 delivery", [],
                 {"row.from": "pat@acme.com", "row.mailbox_upn": "us@ours.com",
                  "row.received_at": "2025-12-20T10:00:00Z"})]
        rows = {h: dict(fields, **{"row.subject": subject}) for h, _d, subject, _m, fields
                in docs}
        draft, windows = self.run_first(docs, rows)
        self.assertIn("2025-12-20", " ".join(draft.sentences))
        self.assertEqual(windows[0][0], "2025-10-31")
        for (_s, until), (since, _u) in zip(windows, windows[1:]):   # no day read twice
            self.assertEqual(since, (fc.date.fromisoformat(until)
                                     + fc.timedelta(days=1)).isoformat())
        self.assertLessEqual(windows[-1][0], "2025-12-20")
        self.assertGreaterEqual(windows[-1][1], "2025-12-20")


def _run_ask(recipe, answers, ask):
    reads, draft = StubReads(answers), fa.Draft()
    recipe(reads, ask, draft)
    return draft, reads


if __name__ == "__main__":
    unittest.main()
