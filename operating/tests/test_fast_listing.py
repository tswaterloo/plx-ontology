"""The newest-first list path (``operating.fast_listing``, intent ``recent_list``).

Evidence label: ``isolated-fixture``. What is tested is the contract at its boundary: which
questions the path reads (from the compiled schema) and which fall through to the agent
(names, negations, counts, two candidate types, an unread frame); that a restricted reader
learns nothing of a record, a party or a link it may not read; that one message's copies
in two mailboxes are read and said once; and that 1, 10 and 20 records are each said once,
in order. The real-gate cases run the fixture employees (A reads purchasing, B does not)
through ``fast_answer.answer`` and its answer gate.
"""

from __future__ import annotations

import json
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from operating import fast_answer
from operating import fast_listing
from operating.tests import test_agent_loop
from operating.tests.test_fast_answer import StubReads
from operating.tests.test_mcp_http_host import EmployeeDoorTestCase
from operating.tests.test_session_identity import OID_A, OID_B

NOW = datetime(2026, 11, 17, 17, 0, tzinfo=timezone.utc)


class UnderstandTests(unittest.TestCase):
    def ask(self, question):
        return fast_listing.understand(question, NOW)

    def test_the_noun_party_and_dating_come_from_the_schema(self):
        got = self.ask("latest emails from customers")
        self.assertEqual((got.intent, got.top, got.listing.object_type, got.listing.link,
                          got.listing.dating, got.listing.copy_key),
                         ("recent_list", fast_listing.DEFAULT_LISTED, "Email",
                          "Email.sent_by_customer", "received_at", "internet_message_id"))
        one = self.ask("most recent invoice from a supplier")
        self.assertEqual((one.top, one.listing.object_type, one.listing.link),
                         (1, "PurchaseInvoice", "PurchaseInvoice.billed_by"))
        po = self.ask("5 newest POs this week")
        self.assertEqual((po.top, po.listing.object_type, po.listing.link), (5, "PurchaseOrder",
                                                                             None))
        self.assertIsNotNone(po.window)
        self.assertEqual(self.ask("the newest customer emails").listing.link,
                         "Email.sent_by_customer")
        self.assertEqual(self.ask("last 2 purchase orders").top, 2)

    def test_what_the_path_does_not_read_falls_through_to_the_agent(self):
        for question in (
                "latest orders",                          # sales or purchase: two types
                "latest email from Unilever",             # a name
                "latest emails from One Wellness",        # a name holding "one"
                "latest messages from customers",         # no type ends in "message"
                "latest unpaid invoices",                 # a status
                "latest emails not from customers",       # a negation
                "how many latest emails",                 # a count
                "latest 50 emails",                       # past MAX_LISTED
                "latest emails from customers and suppliers",
                "latest supplier emails from customers",  # two parties
                "latest sales orders to customers",       # no "to" link on a sales order
                "open the second one",                    # no recency, no noun
                "emails from customers",                  # no recency word
                "latest emails from customers last fiscal quarter",  # an unread frame
                "x" * 250, None, 7):
            with self.subTest(question=question):
                self.assertIsNone(self.ask(question))

    def test_the_hook_in_fast_answer_reads_the_list_question(self):
        got = fast_answer.understand("latest emails from customers", NOW)
        self.assertEqual(got.intent, "recent_list")
        self.assertIn("recent_list", fast_answer._RECIPES)


# --------------------------------------------------------------- the recipe on stubs

EMAIL = fast_listing.Listing(object_type="Email", noun="emails", dating="received_at",
                             dating_type="timestamp", who="sender",
                             copy_key="internet_message_id", link="Email.sent_by_customer",
                             party="Customer", party_said="from customers")


def _stamp(n):
    return f"2026-11-17T{10 + n // 60:02d}:{n % 60:02d}:00Z"


def _card(oid, display, values):
    return {"object": {"object_id": oid, "display": {
                "untrusted_text": display, "origin": {"citation": f"h:{oid}",
                                                      "field_path": "row.subject"}}},
            "values": [{"property": p, "value": v,
                        "citation": {"l1_handle": f"h:{oid}", "field_path": f"row.{p}"}}
                       for p, v in values.items()]}


class World:
    """Candidate rows, the cards ``plx.object`` returns and what the reader may see."""

    def __init__(self, n, *, hidden=(), copies=None, marks=None):
        self.rows, self.cards, self.keys = [], {}, {}
        self.hidden = set(hidden)
        for i in range(n):
            oid, party = f"Email:m:{i}", f"Customer:c:{i % 3}"
            value = _stamp(n - i)                         # newest first
            self.rows.append((oid, party, json.dumps((marks or {}).get(oid, [])), value))
            self.cards[oid] = _card(oid, f"Subject {i}", {"received_at": value,
                                                          "sender": f"s{i}@x.test"})
            self.keys[oid] = (copies or {}).get(oid, f"<msg-{i}>")
        for c in range(3):
            self.cards[f"Customer:c:{c}"] = _card(f"Customer:c:{c}", f"Customer {c}", {})

    def answers(self, name, args):
        self.assert_visible(args["object_id"])
        return self.cards.get(args["object_id"]) if name == "plx.object" else None

    def assert_visible(self, oid):
        if oid in self.hidden:
            raise AssertionError(f"read a record the reader may not read: {oid}")

    def run(self, top):
        world = self

        def candidates(reads, listing, low, high, limit, offset):
            return world.rows[offset:offset + limit]

        def copy_keys(reads, listing, ids):
            return {i: world.keys[i] for i in ids if i in world.keys}

        ask = fast_answer.Ask("recent_list", "", None, top=top, listing=EMAIL)
        reads, draft = StubReads(self.answers), fast_answer.Draft()
        reads.now = lambda: NOW
        self.visible_asked = []

        def visible(reads, ids):
            self.visible_asked += list(ids)
            return frozenset(i for i in ids if i not in world.hidden)

        with mock.patch.object(fast_listing, "candidates", candidates), \
                mock.patch.object(fast_listing, "copy_keys", copy_keys), \
                mock.patch.object(fast_listing, "visible", visible), \
                mock.patch.object(fast_listing, "held_markings",
                                  lambda reads: frozenset({"held"})):
            fast_listing.recent_list(reads, ask, draft)
        return draft, reads


class RecipeTests(unittest.TestCase):
    def read_ids(self, reads):
        return [args["object_id"] for name, args in reads.calls if name == "plx.object"]

    def test_n_1_10_20_each_record_said_once_newest_first_and_on_budget(self):
        for n in (1, 10, 20):
            with self.subTest(n=n):
                world = World(n)
                started = time.monotonic()
                draft, reads = world.run(n)
                self.assertLess(time.monotonic() - started, 1.0)
                said = [r["object_id"] for r in draft.roots if r["object_type"] == "Email"]
                self.assertEqual(said, [f"Email:m:{i}" for i in range(n)])
                text = " ".join(draft.sentences)
                for i in range(n):
                    self.assertEqual(text.count(f'"Subject {i}"'), 1)
                # each record and each party read once
                ids = self.read_ids(reads)
                self.assertEqual(len(ids), len(set(ids)))
                self.assertEqual(draft.answerability, "answerable")

    def test_a_restricted_reader_learns_nothing_of_a_record_or_a_party_it_may_not_read(self):
        # record 1 hidden; record 2's party (Customer:c:2) hidden: both dropped unread
        world = World(6, hidden={"Email:m:1", "Customer:c:2"})
        draft, reads = world.run(10)
        text = " ".join(draft.sentences)
        said = [r["object_id"] for r in draft.roots if r["object_type"] == "Email"]
        self.assertEqual(said, ["Email:m:0", "Email:m:3", "Email:m:4"])
        for gone in ("Subject 1", "Subject 2", "Subject 5", "Customer 2"):
            self.assertNotIn(gone, text)
        self.assertNotIn("Email:m:1", self.read_ids(reads))
        self.assertNotIn("Customer:c:2", self.read_ids(reads))

    def test_a_link_whose_markings_the_reader_does_not_hold_is_never_checked_or_read(self):
        world = World(3, marks={"Email:m:0": ["finance"], "Email:m:1": ["held"],
                                "Email:m:2": "not-a-list"})
        draft, reads = world.run(10)
        self.assertEqual([r["object_id"] for r in draft.roots if r["object_type"] == "Email"],
                         ["Email:m:1"])
        self.assertNotIn("Email:m:0", world.visible_asked)
        self.assertNotIn("Email:m:0", self.read_ids(reads))

    def test_one_message_in_two_mailboxes_is_read_and_said_once(self):
        world = World(4, copies={"Email:m:0": "<same>", "Email:m:1": "<same>"})
        draft, reads = world.run(10)
        self.assertNotIn("Email:m:1", self.read_ids(reads))
        self.assertEqual([r["object_id"] for r in draft.roots if r["object_type"] == "Email"],
                         ["Email:m:0", "Email:m:2", "Email:m:3"])

    def test_nothing_visible_says_so_only_when_every_candidate_was_checked(self):
        draft, reads = World(3, hidden={f"Email:m:{i}" for i in range(3)}).run(5)
        self.assertEqual(draft.sentences, ["You can't see any emails from customers."])
        self.assertEqual(self.read_ids(reads), [])
        # more candidates than MAX_PAGES pages, none visible: no claim of "none"
        many = World(400, hidden={f"Email:m:{i}" for i in range(400)})
        with self.assertRaises(fast_answer.Unanswerable):
            many.run(5)

    def test_a_record_changed_since_the_list_read_it_declines(self):
        world = World(2)
        world.cards["Email:m:0"]["values"][0]["value"] = "2020-01-01T00:00:00Z"
        with self.assertRaises(fast_answer.Unanswerable):
            world.run(2)

    def test_the_timestamp_is_said_in_business_time(self):
        self.assertEqual(fast_listing._when("2026-11-17T15:56:37Z", "timestamp"),
                         "2026-11-17 10:56:37-05:00")
        self.assertEqual(fast_listing._when("2026-11-17", "date"), "2026-11-17")


# --------------------------------------------------------- the real gate, two readers

def _dating(declared):
    # the fixture's purchase orders carry no order_date: date them by expected_date
    names = [p.get("name") for p in fast_listing._properties(declared)]
    return ("expected_date", "date") if "expected_date" in names else None


class RealGateTests(EmployeeDoorTestCase):
    server = test_agent_loop.AgentLoopTests.server

    def answer(self, oid, question):
        why: list[str] = []
        with mock.patch.object(fast_listing, "_dating", _dating):
            return fast_answer.answer(question, fast_answer.Reads.of_server(
                self.server(oid)), why), why

    def test_purchasing_reader_gets_a_gated_list_and_a_restricted_reader_reads_nothing(self):
        ran, why = self.answer(OID_A, "latest purchase orders from suppliers")
        self.assertIsNotNone(ran, why)
        self.assertIn("The newest purchase orders from suppliers you can see", ran.final_text)
        self.assertIn("PO5448", ran.final_text)
        self.assertIn("Synthetic Vendor B", ran.final_text)
        roots = {r["object_type"] for r in ran.found["roots"]}
        self.assertEqual(roots, {"PurchaseOrder", "Supplier"})

        ran_b, why_b = self.answer(OID_B, "latest purchase orders from suppliers")
        if ran_b is not None:
            self.assertNotIn("PO54", ran_b.final_text)
            self.assertNotIn("Synthetic Vendor", ran_b.final_text)
            self.assertEqual([e for e in ran_b.events if e["name"] == "plx.object"], [])


if __name__ == "__main__":
    unittest.main()
