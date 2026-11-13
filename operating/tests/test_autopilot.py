"""The autopilot engine (``operating.autopilot``): typed events from changed records, the
registry's autopilots preparing work per owner, the per-person queue and its safety rules.

SYNTHETIC fixtures only (``World`` stores, synthetic handles and addresses); evidence label:
isolated-fixture.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from operating import autopilot as ap
from operating.tests.test_proactive_engine import EXCLUDED_OID, OURS_OID, World
from operating.tests.test_proactive_threads import (APPROVER, APPROVER_OID, BOSS, OURS,
                                                    OUTSIDE, mail, people)
from operating import proactive_engine as pe

NOW = datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc)
SENT = NOW.date() - timedelta(days=1)
READER = f"synth-tenant:employee:{OURS_OID}"
OTHER = f"synth-tenant:employee:{APPROVER_OID}"


def everyone(_aud: str) -> bool:
    return True


def opens_all(handles):
    return {h: None for h in handles}


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.w = World()
        self.customer = self.w.obj("Customer", {"name": "Synth customer", "code": "C100",
                                                "erp_code": "C100", "email": OUTSIDE})
        self.env: dict[str, str] = {}
        self.named_order: str | None = None

    def order(self, po: str, *, on_hand: str = "50", need: str = "10") -> str:
        w = self.w
        order = w.obj("SalesOrder", {"order_number": "SO-7001", "customer_po": po,
                                     "status": "Open", "required_date": "2026-10-30"})
        w.link("SalesOrder.ordered_by", order, self.customer)
        item = w.obj("Product", {"code": "SYN-1", "quantity_on_hand": (on_hand, "EA")})
        line = w.obj("SalesOrderLine", {"outstanding_quantity": need, "uom": "EA"})
        w.link(ap.SO_LINE_OF, line, order)
        w.link(ap.SO_LINE_ITEM, line, item)
        pol = w.obj("PurchaseOrderLine", {"ordered_quantity": ("100", "EA"),
                                          "received_quantity": "0",
                                          "bc_expected_receipt_date": "2026-10-25"})
        w.link(ap.PO_LINE_ITEM, pol, item)
        return order

    def availability(self, order: str) -> dict:
        """``ap.availability`` of ``order`` over the built store, as a handler reads it."""
        path = self.dir / "atp.sqlite"
        self.w.build(path)
        st = pe._Store(path, threads=1)
        self.addCleanup(st.close)
        ctx = ap.Ctx(st, NOW.date(), time.monotonic() + 60, ())
        return ap.availability(ctx, order)

    def message(self, i: int, body: str, *, subject: str = "PO", box: str = OURS_OID,
                to=(OURS,), cc=(), sender: str = OUTSIDE, customer: bool = True) -> str:
        made = mail(self.w, SENT, sender=sender, to=to, cc=cc, body=body, subject=subject,
                    mailbox=box, conversation=f"conv-{i}", n=30_000 + i)
        if customer:
            self.w.link("Email.sent_by_customer", made, self.customer)
        if self.named_order is None:
            self.named_order = self.w.obj("SalesOrder", {"order_number": "SO-1", "status": "Open"})
        self.w.link("Email.mentions_order", made, self.named_order)   # the order it names
        return made

    def cycle(self, ids, *, guard=None, registry=None, now=NOW, name="s.sqlite", **kw):
        path = self.dir / name
        if not path.exists():
            self.w.build(path)
        st = pe._Store(path, threads=1)
        q = ap.Queue(self.dir / "q" / ap.QUEUE_FILE)
        try:
            return ap.run_cycle(st, q, ids, people=people() if guard is None else guard,
                                registry=registry, environ=self.env, now=now,
                                landed_at=ap._iso(now - timedelta(minutes=2)), **kw)
        finally:
            st.close()
            q.close()

    def queue(self) -> ap.Queue:
        q = ap.Queue(self.dir / "q" / ap.QUEUE_FILE)
        self.addCleanup(q.close)
        return q

    def items_known(self, *codes: str) -> None:
        """The identifier table rows the store builder derives for item numbers."""
        self.items = {c: self.w.obj("Product", {"code": c}, binding="bc.items") for c in codes}

    def build(self, *, events=(), extra_ids=()) -> None:
        path = self.dir / "s.sqlite"
        self.w.build(path)
        import sqlite3
        with sqlite3.connect(path) as c:
            for code, oid in getattr(self, "items", {}).items():
                c.execute("INSERT INTO identifiers (identifier_kind, normalized, raw, role, "
                          "object_id, property, l1_handle, field_path, lineage, recorded_at) "
                          "VALUES ('item_code', ?, ?, 'primary', ?, 'code', 'h', 'row.code', "
                          "'workbench', '2026-01-01T00:00:00Z')", (code.upper(), code, oid))
            for n, (kind, oid, at) in enumerate(events):
                c.execute("INSERT INTO events (event_id, event_type, time_basis, time, "
                          "l1_handle, field_path, digest, lineage, markings, recorded_at) "
                          "VALUES (?, ?, 'source_recorded', ?, 'h', 'row.x', 'd', "
                          "'workbench', '[]', '2026-01-01T00:00:00Z')", (f"e{n}", kind, at))
                c.execute("INSERT INTO event_objects VALUES (?, ?, 'subject')", (f"e{n}", oid))


class Classify(unittest.TestCase):
    def test_intents_from_the_message_own_words(self):
        for text, want in (
                ("Please find attached our PO 4512 for 200 cases.", {"customer_po"}),
                ("Could you send the COA for lot 22?", {"document_request"}),
                ("What is the status of our order 70012? When will it ship?",
                 {"status_request"}),
                ("Our supplier advises the resin is on backorder; revised ship date Nov 20.",
                 {"supplier_delay"}),
                ("There is no delay on this shipment.", set()),
                ("Sorry for the delay, the COA is attached.", set()),
                ("Has invoice 9921 been paid? Our records show a balance.",
                 {"payment_question"}),
                ("Thanks, have a good weekend.", set())):
            with self.subTest(text=text):
                self.assertEqual(set(ap.intents_of("", text)) - {"approval_request"}, want)

    def test_an_unknown_registry_entry_refuses_the_whole_registry(self):
        good = ap.load_registry()
        for bad in ({"handler": "send_now"}, {"on": "mail.anything"}, {"owner": "everyone"},
                    {"when": {"sender_is": "x"}}, {"done_when": "never"}, {"due_minutes": 0}):
            raw = json.loads(json.dumps(good))
            raw["autopilots"][0].update(bad)
            with self.subTest(bad=bad), self.assertRaises(ap.RegistryInvalid):
                ap.validate_registry(raw)


class Engine(Fixture):
    def test_customer_po_prepares_a_confirmation_with_the_promise_date_once(self):
        order = self.order("PO-4512", on_hand="5", need="10")
        msg = self.message(1, "Please find attached our PO PO-4512 for 10 cases.",
                           subject="New PO PO-4512")
        first = self.cycle([msg])
        second = self.cycle([msg])                      # the same record taken in again
        self.assertEqual(first["prepared"], {"customer_po": 1})
        self.assertEqual((second["events_new"], second["prepared"]), (0, {}))
        (item,) = self.queue().items()
        work = item["body"]["prepared"]
        self.assertEqual(item["owner"], OURS_OID)
        self.assertEqual(work["order"]["object_id"], order)
        # 5 on hand, 100 inbound on a PO line expected 2026-10-25: promise that date
        self.assertEqual(work["availability"]["date"], "2026-10-25")
        self.assertIn("Sun 25 Oct", work["draft"]["body"])      # the copy gate's date form
        self.assertFalse(work["draft"]["body"].rstrip().endswith(","))   # no sign-off
        self.assertEqual(work["draft"]["to"], [{"address": OUTSIDE}])
        self.assertTrue(item["citations"])
        self.assertEqual((first["sent"], first["writes"]), (0, 0))
        card = ap.card_of(item)
        self.assertEqual(card["id"], ap.CARD_PREFIX + item["item_id"])

    def test_a_machine_report_prepares_nothing_a_burst_from_one_person_still_does(self):
        # triage's automated-mail test (``inbox_triage.automated_mail``) by form: a JSON
        # report is skipped. A cycle is no window (minutes live, a day in a replay), so 35
        # templated messages from one sender in one cycle are not judged a feed: the same
        # mail prepares the same work whichever way the records arrive
        report = self.message(200, 'Monitor\n{\n  "observed_at": "2026-10-18T07:00:00Z",\n'
                                   '  "mode": "stream-C",\n  "reason": "missing"\n}',
                              subject="Monitor", sender="rail@synth-customer.example")
        burst = [self.message(100 + i, f"Could you send the COA for lot {i}?",
                              subject=f"COA {i}") for i in range(35)]
        out = self.cycle([report, *burst])
        self.assertEqual(out["skipped"]["automated"], 1)
        self.assertEqual(out["prepared"], {"customer_request": 30})   # 30 an hour, rest deferred

    def test_a_new_po_prepares_an_order_entry_intent_only_when_every_line_resolves(self):
        self.items_known("SYN-10", "SYN-20")
        whole = self.message(2, "Please ship:\n1. SYN-10 Gallon soap: 6 cases\n"
                                "2. SYN-20 Hand wash: 2 cases", subject="New PO 88123")
        part = self.message(3, "Please ship:\n1. SYN-10 Gallon soap: 6 cases\n"
                               "2. ZZ-404 Unknown thing: 2 cases", subject="New PO 88124")
        self.build()
        self.cycle([whole, part])
        by = {i["body"]["prepared"]["po_numbers"][0]: i for i in self.queue().items()}
        spec = ap.write_spec(by["88123"])
        self.assertEqual(spec["operation"], "sales_order_create")
        self.assertEqual(spec["payload"]["customerNumber"], "C100")
        self.assertEqual(spec["payload"]["externalDocumentNumber"], "88123")
        self.assertEqual([(ln["lineObjectNumber"], ln["quantity"])
                          for ln in spec["payload"]["salesOrderLines"]],
                         [("SYN-10", "6"), ("SYN-20", "2")])
        lines = by["88123"]["body"]["prepared"]["lines"]
        # no on-hand figure in the records: the ship date says so, never a guess
        self.assertTrue(all(ln["available"]["date"] is None and "not checked" in
                            ln["available"]["basis"] for ln in lines))
        self.assertIn("88123", by["88123"]["body"]["prepared"]["draft"]["body"])
        # one line names no item we hold: no intent, the line is shown unresolved
        self.assertIsNone(ap.write_spec(by["88124"]))
        self.assertIn("lines_not_resolved", by["88124"]["body"]["prepared"]["gaps"])

    def test_a_po_from_a_domain_two_customers_share_is_no_customer(self):
        self.items_known("SYN-10")
        self.w.obj("Customer", {"name": "Other synth", "erp_code": "C200",
                                "email": "ap@synth-customer.example"})
        msg = self.message(4, "Please ship SYN-10: 6 cases", subject="New PO 88125",
                           customer=False, sender="orders@synth-customer.example")
        self.build()
        self.cycle([msg])
        (item,) = self.queue().items()
        work = item["body"]["prepared"]
        self.assertIsNone(ap.write_spec(item))
        self.assertTrue(any(g.startswith("customer_not_resolved") for g in work["gaps"]))

    def test_cardinality_one_ten_sixty_no_row_lost_or_doubled(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                ids = [self.message(i, f"Could you send the COA for lot {i}?",
                                    subject=f"COA {i}") for i in range(n)]
                began = time.monotonic()
                summary = self.cycle(ids)
                # the per-owner hourly limit (30) defers the rest to the next hour, lost none
                later = self.cycle([], now=NOW + timedelta(hours=1))
                self.assertLess(time.monotonic() - began, 30)
                self.assertEqual(summary["prepared"], {"customer_request": min(n, 30)})
                self.assertEqual(later["prepared"].get("customer_request", 0), max(0, n - 30))
                items = self.queue().items()
                self.assertEqual(len({i["event_id"] for i in items}), n)
                self.assertEqual(len(items), n)

    def test_the_sender_side_comes_from_the_party_records_address(self):
        msg = self.message(60, "Could you send the COA for lot 3?", customer=False)
        self.assertEqual(self.cycle([msg])["prepared"], {"customer_request": 1})

    def test_people_exclusion_fails_closed(self):
        excluded_box = self.message(3, "Please send the COA.", box=EXCLUDED_OID, to=(BOSS,))
        cc_boss = self.message(4, "Please send the COA.", cc=(BOSS,))
        summary = self.cycle([excluded_box, cc_boss])
        self.assertEqual(summary["prepared"], {})
        self.assertEqual(summary["skipped"].get("mailbox_not_cleared"), 1)
        self.assertEqual(summary["skipped"].get("participants_not_cleared"), 1)
        blocked = self.cycle([cc_boss], guard=people(unavailable=True))
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(self.queue().items(), [])

    def test_mail_is_prepared_only_for_the_mailbox_owner_on_to(self):
        cc_only = self.message(5, "Please send the COA.", to=(APPROVER,), cc=(OURS,))
        summary = self.cycle([cc_only])
        self.assertEqual(summary["prepared"], {})
        self.assertEqual(summary["skipped"].get("customer_request:owner_not_addressed"), 1)

    def test_kill_switch_reads_and_prepares_nothing(self):
        msg = self.message(6, "Please send the COA.")
        self.env[ap.KILL_ENV] = "1"
        self.assertEqual(self.cycle([msg])["status"], "off")
        self.env.clear()
        (self.dir / "q").mkdir(exist_ok=True)
        (self.dir / "q" / ap.KILL_FILE).write_text("")
        self.assertEqual(self.cycle([msg])["status"], "off")
        self.assertEqual(self.queue().conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                         0)

    def test_rate_limit_per_owner_defers_the_rest_to_the_next_hour(self):
        reg = ap.load_registry()
        reg["limits"]["per_owner_per_hour"] = 3
        ids = [self.message(10 + i, f"Please send the COA for lot {i}.") for i in range(5)]
        self.assertEqual(self.cycle(ids, registry=reg)["prepared"], {"customer_request": 3})
        later = self.cycle([], registry=reg, now=NOW + timedelta(hours=1))
        self.assertEqual(later["prepared"], {"customer_request": 2})
        self.assertEqual(len(self.queue().items()), 5)

    def test_a_slow_handler_is_stopped_and_the_cycle_goes_on(self):
        reg = ap.load_registry()
        reg["limits"]["handler_seconds"] = 0.05

        def slow(ctx, event):
            time.sleep(0.1)
            ctx.check()
            return {}
        msg = self.message(20, "Please send the COA.")
        with mock.patch.dict(ap.HANDLERS, {"answer_request": slow}):
            summary = self.cycle([msg], registry=reg)
        self.assertEqual(summary["skipped"].get("customer_request:handler_timeout"), 1)
        self.assertEqual(summary["status"], "complete")

    def test_nothing_sends_or_writes_and_the_store_is_untouched(self):
        self.order("PO-1")
        ids = [self.message(30, "Please find attached our PO PO-1."),
               self.message(31, "Has invoice 4410 been paid? It shows a balance.")]
        path = self.dir / "s.sqlite"
        self.w.build(path)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        import socket
        with mock.patch.object(socket, "create_connection",
                               side_effect=AssertionError("network")), \
                mock.patch.object(socket.socket, "connect",
                                  side_effect=AssertionError("network")):
            summary = self.cycle(ids)
        self.assertEqual((summary["sent"], summary["writes"]), (0, 0))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_settles_when_the_owner_replied_in_the_conversation(self):
        msg = self.message(40, "Please send the COA for lot 7.")
        mail(self.w, SENT, sender=OURS, to=(OUTSIDE,), body="Attached.", subject="RE: PO",
             mailbox=OURS_OID, conversation="conv-40", n=39_999, hour=16)
        self.cycle([msg])
        (item,) = self.queue().items(states=("done",))
        self.assertEqual(item["done_reason"], "owner_replied")


    def test_teams_replies_and_meeting_follow_ups_settle_from_the_owners_own_messages(self):
        """AUTOMATION-009: a Teams item settles on the owner's later message in that chat,
        a meeting follow-up on the organizer's later mail to an outside attendee; neither
        on an earlier one, another chat, a non-attendee or a colleague who attended."""
        path = self.dir / "s.sqlite"
        self.w.build(path)
        st = pe._Store(path, threads=1)
        self.addCleanup(st.close)
        q = self.queue()
        ended = "2026-10-20T15:00:00Z"
        events = {
            "teams": {"event_type": "message.received", "object_id": "TeamsChatMessage:t",
                      "object_type": "TeamsChatMessage", "channel": "teams",
                      "at": "2026-10-20T14:00:00Z", "conversation_id": "teams:chat:c1"},
            "meeting": {"event_type": "meeting.ended", "object_id": "Meeting:m",
                        "object_type": "Meeting", "at": ended,
                        "attendees": ["buyer@client.example", OURS, BOSS]}}
        with q.conn:
            for name, (rule, body) in {"teams": ("owner_replied", events["teams"]),
                                       "meeting": ("owner_mailed_attendees",
                                                   events["meeting"])}.items():
                q.add_event({**body, "event_id": "e-" + name}, ended)
                q.add_item({"item_id": name, "event_id": "e-" + name, "autopilot": name,
                            "owner": OURS_OID, "title": name,
                            "body": {"happened": {"object_id": body["object_id"],
                                                  "channel": body.get("channel")},
                                     "done_when": rule},
                            "audiences": [], "markings": [], "citations": [],
                            "due_at": ended, "created_at": ended})
        settle = lambda: ap.settle(st, q, {}, people(), now=NOW)  # noqa: E731
        # earlier messages, another chat, a mail to nobody who attended: nothing settles
        with q.conn:
            q.note_sent([{"owner": OURS_OID, "conversation": "teams:chat:c1",
                          "at": "2026-10-20T13:00:00Z"},
                         {"owner": OURS_OID, "conversation": "teams:chat:other",
                          "at": "2026-10-20T16:00:00Z"},
                         {"owner": OURS_OID, "conversation": "x", "to": ["buyer@client.example"],
                          "at": "2026-10-20T14:30:00Z"},
                         {"owner": OURS_OID, "conversation": "y", "to": ["else@client.example"],
                          "at": "2026-10-20T16:00:00Z"},
                         {"owner": OURS_OID, "conversation": "w", "to": [BOSS],
                          "at": "2026-10-20T16:00:00Z"}])   # a colleague who attended
        self.assertEqual(settle(), Counter())
        with q.conn:
            q.note_sent([{"owner": OURS_OID, "conversation": "teams:chat:c1",
                          "at": "2026-10-20T14:05:00Z"},
                         {"owner": OURS_OID, "conversation": "z", "to": ["buyer@client.example"],
                          "at": "2026-10-20T15:30:00Z"}])
        self.assertEqual(settle(), Counter({"owner_replied": 1, "owner_mailed_attendees": 1}))
        self.assertEqual({i["item_id"] for i in q.items(states=("done",))}, {"teams", "meeting"})


class Readers(Fixture):
    def prepared(self) -> ap.Queue:
        ids = [self.message(50, "Please send the COA for lot 9."),
               self.message(51, "Where is order SO-2? When will it ship?")]
        billed = self.w.obj("SalesOrder", {"order_number": "SO-2", "status": "Open"})
        self.w.link("Email.mentions_order", ids[1], billed)
        inv = self.w.obj("SalesInvoice", {"invoice_number": "4410", "total_amount": "120.00",
                                          "remaining_amount": "120.00", "currency": "CAD"},
                         markings='["finance"]', audience="api:fin")
        self.w.link("SalesInvoice.bills", inv, billed)     # its invoice is finance-marked
        self.cycle(ids)
        return self.queue()

    def test_owner_sees_own_items_and_others_see_nothing(self):
        q = self.prepared()
        own = ap.visible(q, reader=READER, may_read=everyone, held=["finance"],
                         decide=opens_all)
        self.assertEqual(sorted(i["autopilot"] for i in own),
                         ["customer_request", "customer_request"])
        self.assertEqual(ap.visible(q, reader=OTHER, may_read=everyone, held=["finance"],
                                    decide=opens_all), [])
        self.assertEqual(ap.visible(q, reader="synth-tenant:admin", may_read=everyone,
                                    held=["finance"], decide=opens_all), [])

    def test_restricted_owner_and_granted_manager(self):
        q = self.prepared()
        no_finance = ap.visible(q, reader=READER, may_read=everyone, held=[],
                                decide=opens_all)
        self.assertEqual([i["autopilot"] for i in no_finance], ["customer_request"])
        self.assertEqual(ap.visible(q, reader=READER, may_read=lambda a: False,
                                    held=["finance"], decide=opens_all), [])
        refused = ap.visible(q, reader=READER, may_read=everyone, held=["finance"],
                             decide=lambda hs: {h: {"reason": "x"} for h in hs})
        self.assertEqual(refused, [])
        grants = {"teams": {OURS_OID: "OPS"}, "managers": {APPROVER_OID: ["OPS"]}}
        managed = ap.visible(q, reader=OTHER, may_read=everyone, held=["finance"],
                             decide=opens_all, grants=grants)
        self.assertEqual(len(managed), 2)
        self.assertFalse(any(i["mine"] for i in managed))
        other_team = {"teams": {OURS_OID: "OPS"}, "managers": {APPROVER_OID: ["QA"]}}
        self.assertEqual(ap.visible(q, reader=OTHER, may_read=everyone, held=["finance"],
                                    decide=opens_all, grants=other_team), [])

    def test_every_citation_of_an_item_is_checked_fail_closed(self):
        q = self.prepared()
        (item,) = [i for i in q.items([OURS_OID], ("open",))][:1]
        many = [f"api://synth.table/row-{n}" for n in range(60)]
        with q.conn:
            q.conn.execute("UPDATE items SET citations = ?", (json.dumps(many),))
        last = many[-1]
        seen = lambda decide: ap.visible(q, reader=READER, may_read=everyone,  # noqa: E731
                                         held=["finance"], decide=decide)
        self.assertEqual(len(seen(opens_all)), 2)
        # the 60th citation is refused: the item is not shown
        self.assertEqual(seen(lambda hs: {h: ({"reason": "x"} if h == last else None)
                                          for h in hs}), [])
        # a decision that leaves a citation out, or is not a mapping: not shown
        self.assertEqual(seen(lambda hs: {h: None for h in list(hs)[:40]}), [])
        self.assertEqual(seen(lambda hs: None), [])
        # a handle only the prepared work names is checked too
        body = dict(item["body"], prepared={"refs": [{"source": "bc://synth/hidden"}]})
        with q.conn:
            q.conn.execute("UPDATE items SET body = ?, citations = '[]' WHERE item_id = ?",
                           (json.dumps(body), item["item_id"]))
        shown = seen(lambda hs: {h: ({"reason": "x"} if h.startswith("bc://") else None)
                                 for h in hs})
        self.assertNotIn(item["item_id"], [i["item_id"] for i in shown])

    def test_digest_nudges_items_past_due(self):
        q = self.prepared()
        items = ap.visible(q, reader=READER, may_read=everyone, held=["finance"],
                           decide=opens_all)
        day = ap.digest(items, NOW + timedelta(hours=5))
        self.assertEqual(day["open"], 2)
        self.assertEqual(len(day["nudges"]), 2)


class Kinds(Fixture):
    def test_a_meeting_read_before_its_end_is_followed_up_once_after_it(self):
        # AUTOMATION-002: whether a meeting ended is judged at the cycle's time, and the
        # event read before its end never swallows the follow-up after it
        w = self.w
        meeting = w.obj("Meeting", {"mailbox": OURS_OID, "subject": "Review",
                                    "organizer": json.dumps({"address": OURS}),
                                    "attendees": json.dumps([{"address": OUTSIDE}]),
                                    "end": ap._iso(NOW + timedelta(hours=1))})
        w.link("Meeting.with_customer", meeting, self.customer)
        before = self.cycle([meeting], now=NOW)
        self.assertNotIn("meeting_followup", before["prepared"])
        after = self.cycle([meeting], now=NOW + timedelta(hours=2))
        self.assertEqual(after["prepared"].get("meeting_followup"), 1, after)
        again = self.cycle([meeting], now=NOW + timedelta(hours=3))
        self.assertNotIn("meeting_followup", again["prepared"])
        self.assertEqual(sum(1 for i in self.queue().items()
                             if i["autopilot"] == "meeting_followup"), 1)

    def test_a_handler_failure_is_tried_again_and_then_left_failed(self):
        # AUTOMATION-001: a handler that fails (a locked store) defers its event: the next
        # cycle prepares it once; one that keeps failing is left failed after
        # HANDLER_TRIES, never processed silently
        import sqlite3
        w = self.w
        meetings = [w.obj("Meeting", {"mailbox": OURS_OID, "subject": f"Review {n}",
                                      "organizer": json.dumps({"address": OURS}),
                                      "attendees": json.dumps([{"address": OUTSIDE}]),
                                      "end": "2026-10-18T15:00:00Z"}) for n in (1, 2)]
        for meeting in meetings:
            w.link("Meeting.with_customer", meeting, self.customer)
        real = ap.HANDLERS["meeting_followup"]
        broken = {meetings[1]}
        calls = []

        def flaky(ctx, event):
            calls.append(event["object_id"])
            if event["object_id"] in broken or calls.count(event["object_id"]) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real(ctx, event)

        def states():
            q = self.queue()
            return {r[0]: r[1] for r in q.conn.execute(
                "SELECT object_id, state FROM events WHERE event_type = 'meeting.ended'")}
        with mock.patch.dict(ap.HANDLERS, {"meeting_followup": flaky}):
            first = self.cycle(meetings)
            self.assertNotIn("meeting_followup", first["prepared"])
            self.assertEqual(states(), dict.fromkeys(meetings, "deferred"))
            second = self.cycle(meetings, now=NOW + timedelta(minutes=10))
            self.assertEqual(second["prepared"].get("meeting_followup"), 1, second)
            self.assertEqual(states(), {meetings[0]: "processed", meetings[1]: "deferred"})
            self.cycle(meetings, now=NOW + timedelta(minutes=20))
            self.assertEqual(states()[meetings[1]], "failed")
            self.cycle(meetings, now=NOW + timedelta(minutes=30))
        self.assertEqual(calls.count(meetings[1]), ap.HANDLER_TRIES)
        self.assertEqual(sum(1 for i in self.queue().items()
                             if i["autopilot"] == "meeting_followup"), 1)

    def test_task_qc_meeting_and_supplier_eta_events(self):
        w = self.w
        task = w.obj("PlannerTask", {"title": "Check order SO-7001 for the customer",
                                     "assignees": json.dumps([OURS_OID])})
        bare = w.obj("PlannerTask", {"title": "Tidy the release notes",
                                     "assignees": json.dumps([OURS_OID])})
        order = self.order("PO-9")
        lot = w.obj("Batch", {"lot_number": "L-1", "quality_status": "Rejected",
                              "order_list": "SO-7001", "tested_by": "QA"})
        meeting = w.obj("Meeting", {"mailbox": OURS_OID, "subject": "Review",
                                    "organizer": json.dumps({"address": OURS}),
                                    "attendees": json.dumps([{"address": OUTSIDE}]),
                                    "end": "2026-10-18T15:00:00Z"})
        w.link("Meeting.with_customer", meeting, self.customer)
        supplier = w.obj("Supplier", {"name": "Synth supplier"})
        delay = mail(w, SENT, sender="rep@synth-supplier.example", to=(OURS,),
                     body="Our resin is on backorder; the revised ship date is Nov 20.",
                     subject="Delay", mailbox=OURS_OID, conversation="conv-d", n=31_000)
        w.link("Email.sent_by_supplier", delay, supplier)
        po = w.obj("PurchaseOrder", {"po_number": "P-77", "expected_date": "2026-10-20",
                                     "status": "Released"})
        w.link("Email.mentions_po", delay, po)
        # the PO's item is on the open customer order required 30 Oct: the slip hits it
        pol = w.obj("PurchaseOrderLine", {"ordered_quantity": "5"})
        w.link(ap.PO_LINE_OF, pol, po)
        item = [r["to_object"] for r in w.rows["links"]
                if r["link_type"] == ap.SO_LINE_ITEM][0]
        w.link(ap.PO_LINE_ITEM, pol, item)
        pay = mail(w, SENT, sender="rep@synth-supplier.example", to=(OURS,),
                   body="Has invoice 9921 been paid? Our records show a balance.",
                   subject="Payment", mailbox=OURS_OID, conversation="conv-p", n=31_001)
        w.link("Email.sent_by_supplier", pay, supplier)
        self.env[ap.ROLES_ENV] = str(self.dir / "roles.json")
        (self.dir / "roles.json").write_text(json.dumps({"roles": {"quality": [OURS_OID]}}))
        summary = self.cycle([task, bare, lot, meeting, delay, pay])
        # supplier delay and payment questions are off until payables data exists; a task
        # naming no record makes no item (it would only restate itself)
        self.assertEqual(summary["prepared"], {"task_assigned": 1, "qc_hold": 1,
                                               "meeting_followup": 1, "supplier_eta": 1})
        items = {i["autopilot"]: i["body"]["prepared"] for i in self.queue().items()}
        self.assertEqual([a["order_number"] for a in items["qc_hold"]["affected"]],
                         ["SO-7001"])
        self.assertTrue(items["task_assigned"]["answer"])
        eta = items["supplier_eta"]
        self.assertEqual((eta["eta"]["stated"], eta["eta"]["slip_days"]), ("2026-11-20", 31))
        self.assertEqual([x["object_id"] for x in eta["impacted"]], [order])


class SupplierFollowUp(Fixture):
    def po(self, i: int, *, ordered: str = "2026-10-14", **extra) -> str:
        w = self.w
        sup = w.obj("Supplier", {"name": f"Synth supplier {i}",
                                 "csr_email": f"anna.reed{i}@synth-supplier.example"})
        po = w.obj("PurchaseOrder", {"po_number": f"P-{500 + i}", "order_date": ordered,
                                     "status": "Released", "vendor_name": f"Synth {i}",
                                     **extra})
        w.link("PurchaseOrder.ordered_from", po, sup)
        line = w.obj("PurchaseOrderLine", {"item_code": f"SYN-{i}", "ordered_quantity": "40",
                                           "uom": "CS"})
        w.link(ap.PO_LINE_OF, line, po)
        sent = mail(w, SENT - timedelta(days=4), sender=OURS,
                    to=(f"anna.reed{i}@synth-supplier.example",), body=f"PO P-{500 + i}",
                    subject=f"PO P-{500 + i}", mailbox=OURS_OID, conversation=f"po-{i}",
                    n=40_000 + i)
        w.link("Email.mentions_po", sent, po)
        return po

    def test_open_pos_without_confirmation_get_one_follow_up_each(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                pos = [self.po(i) for i in range(n)]
                confirmed = self.po(900)
                self.w.link("SupplierConfirmation.confirms",
                            self.w.obj("SupplierConfirmation", {"tier": "mail"}), confirmed)
                replied = self.po(901)
                answer = mail(self.w, SENT, sender="anna.reed901@synth-supplier.example",
                              to=(OURS,), body="Confirmed.", subject="RE: PO P-1401",
                              mailbox=OURS_OID, conversation="po-901", n=49_999)
                self.w.link("Email.mentions_po", answer, replied)
                closed = self.po(902, status="Closed")
                self.build(events=[("PurchaseOrder.created", p, "2026-10-14T10:00:00Z")
                                   for p in [*pos, confirmed, replied, closed]])
                reg = ap.load_registry()
                reg["limits"]["per_owner_per_hour"] = 1000
                first = self.cycle([], registry=reg)
                again = self.cycle([], registry=reg)
                self.assertEqual(first["prepared"], {"po_followup": n})
                self.assertEqual(again["prepared"], {})          # one follow-up per PO
                items = self.queue().items()
                self.assertEqual(sorted(i["body"]["happened"]["object_id"] for i in items),
                                 sorted(pos))
                work = items[0]["body"]["prepared"]
                body = work["draft"]["body"]
                self.assertTrue(body.startswith("Hi Anna,"))
                self.assertIn(work["purchase_order"]["number"], body)
                self.assertIn("x 40 CS", body)
                self.assertEqual(items[0]["owner"], OURS_OID)    # who sent the PO
                self.assertEqual(ap.visible(self.queue(), reader=OTHER, may_read=everyone,
                                            held=[], decide=opens_all), [])
                self.assertEqual((first["sent"], first["writes"]), (0, 0))


class OwnersDesk(Fixture):
    def test_the_owner_gets_answers_and_decision_cards_never_a_restated_ask(self):
        self.env[ap.ROLES_ENV] = str(self.dir / "roles.json")
        (self.dir / "roles.json").write_text(json.dumps({"roles": {"owner": [OURS_OID]}}))
        shipped = self.order("PO-31")
        self.w.rows["object_values"].append({**[v for v in self.w.rows["object_values"]
                                                if v["object_id"] == shipped][0],
                                             "property": "order_date", "value": "2026-10-10",
                                             "value_type": "date"})
        old = self.w.obj("SalesOrder", {"order_number": "SO-2019", "order_date": "2019-08-08",
                                        "status": "Closed"})
        stale = self.message(73, "Where is our order SO-2019? When will it ship?")
        self.w.link("Email.mentions_order", stale, old)
        ship = self.w.obj("Shipment", {"shipment_number": "SH-1", "ship_date": "2026-10-16",
                                       "carrier": "Synth Freight", "tracking_number": "TRK123"})
        self.w.link("Shipment.ships", ship, shipped)
        ask = self.message(70, "Where is our order SO-7001? When will it ship?")
        self.w.link("Email.mentions_order", ask, shipped)
        approve = self.message(71, "Please approve the attached quote for $4,200.",
                               subject="Approval")
        chat = self.message(72, "How was your weekend? Anything new?", subject="Hello")
        self.cycle([ask, approve, chat, stale])
        items = {i["body"]["happened"]["object_id"]: i for i in self.queue().items()}
        # the chat and an ask naming only a years-old order make no item
        self.assertEqual(set(items), {ask, approve})
        self.assertEqual({i["autopilot"] for i in items.values()}, {"desk"})
        answer = items[ask]["body"]["prepared"]
        self.assertIn("TRK123", answer["draft"]["body"])
        self.assertIn("Fri 16 Oct", answer["draft"]["body"])
        card = items[approve]["body"]["prepared"]
        self.assertEqual(card["kind"], "decision")
        self.assertIn(card["recommendation"], ("approve", "review"))
        self.assertTrue(card["reasons"])
        self.assertNotIn("draft", card)
        day = ap.digest(list(items.values()), NOW)
        self.assertEqual(day["brief"]["counts"]["decisions"], 1)
        self.assertEqual(day["brief"]["counts"]["drafts"], 1)


class Availability(Fixture):
    """The promise date: one running allocation per item, one stated unit, supply only
    from open purchase orders, every line read (AUTOMATION-003..006)."""

    def lines(self, order: str, item: str, *needs: tuple) -> None:
        for need in needs:
            line = self.w.obj("SalesOrderLine", {"outstanding_quantity": need[0],
                                                 "uom": need[1] if len(need) > 1 else "EA"})
            self.w.link(ap.SO_LINE_OF, line, order)
            self.w.link(ap.SO_LINE_ITEM, line, item)

    def bare_order(self, on_hand: tuple) -> tuple[str, str]:
        order = self.w.obj("SalesOrder", {"order_number": "SO-8001", "status": "Open"})
        item = self.w.obj("Product", {"code": "SYN-2", "quantity_on_hand": on_hand})
        return order, item

    def test_splitting_one_demand_into_lines_never_creates_supply(self):
        order, item = self.bare_order(("10", "EA"))
        self.lines(order, item, ("6",), ("6",))
        got = self.availability(order)
        self.assertIsNone(got["date"])                      # 12 under 10 on hand
        self.assertEqual([ln["available"] for ln in got["lines"]],
                         [NOW.date().isoformat(), None])
        single, item2 = self.bare_order(("10", "EA"))
        self.lines(single, item2, ("12",))
        self.assertEqual(self.availability(single)["date"], self.availability(order)["date"])
        # a PO line covers the rest once, for both lines together
        pol = self.w.obj("PurchaseOrderLine", {"ordered_quantity": ("2", "EA"),
                                               "bc_expected_receipt_date": "2026-10-28"})
        self.w.link(ap.PO_LINE_ITEM, pol, item)
        self.assertEqual(self.availability(order)["date"], "2026-10-28")

    def test_no_split_of_a_demand_creates_supply_at_any_cardinality(self):
        # invariant: 60 needed against 50 on hand and 6 on order, split into 1, 2, 10 or 60
        # lines (even and uneven), always waits for the order and is never covered sooner
        # than the one line holding the same need
        whole, item = self.bare_order(("50", "EA"))
        self.lines(whole, item, ("60",))
        for n in (1, 2, 10, 60):
            for uneven in (False, True):
                with self.subTest(lines=n, uneven=uneven):
                    order, split_item = self.bare_order(("50", "EA"))
                    if uneven and n > 1:
                        needs = [str(60 - (n - 1))] + ["1"] * (n - 1)
                    else:
                        needs = [str(60 // n)] * n
                    self.lines(order, split_item, *[(q,) for q in needs])
                    self.assertIsNone(self.availability(order)["date"])
                    pol = self.w.obj("PurchaseOrderLine", {
                        "ordered_quantity": ("6", "EA"),
                        "bc_expected_receipt_date": "2026-10-28"})
                    self.w.link(ap.PO_LINE_ITEM, pol, split_item)
                    self.assertIsNone(self.availability(order)["date"])   # 56 < 60
                    pol2 = self.w.obj("PurchaseOrderLine", {
                        "ordered_quantity": ("4", "EA"),
                        "bc_expected_receipt_date": "2026-10-30"})
                    self.w.link(ap.PO_LINE_ITEM, pol2, split_item)
                    got = self.availability(order)
                    if n > ap.MAX_LINES:   # lines one check cannot read: no promise at all
                        self.assertIsNone(got["date"])
                        self.assertIn("lines_not_read", got["unknown_reasons"])
                    else:
                        self.assertEqual(got["date"], "2026-10-30")

    def test_quantities_in_two_units_are_never_compared(self):
        for stock, line in ((("50", "KG"), "EA"), (("50", "EA"), "CASE"), (("50", None), "EA")):
            with self.subTest(stock=stock, line=line):
                order, item = self.bare_order(stock if stock[1] else stock[0])
                self.lines(order, item, ("10", line))
                got = self.availability(order)
                self.assertIsNone(got["date"])
                self.assertEqual(set(got["unknown_reasons"]),
                                 {"unit_unknown" if stock[1] is None else "unit_mismatch"})
        order, item = self.bare_order(("50", "each"))       # a spelling, not a unit
        self.lines(order, item, ("10", "EA"))
        self.assertEqual(self.availability(order)["date"], NOW.date().isoformat())

    def test_a_cancelled_purchase_order_supplies_nothing_whatever_its_line_says(self):
        for line_date, head_date in (("2026-10-25", None), (None, "2026-10-25"),
                                     ("2026-10-25", "2026-10-26"), (None, None)):
            with self.subTest(line=line_date, head=head_date):
                order, item = self.bare_order(("0", "EA"))
                self.lines(order, item, ("10",))
                po = self.w.obj("PurchaseOrder", {"status": "Cancelled", **(
                    {"expected_date": head_date} if head_date else {})})
                pol = self.w.obj("PurchaseOrderLine", {"ordered_quantity": ("100", "EA"), **(
                    {"bc_expected_receipt_date": line_date} if line_date else {})})
                self.w.link(ap.PO_LINE_ITEM, pol, item)
                self.w.link(ap.PO_LINE_OF, pol, po)
                self.assertIsNone(self.availability(order)["date"])

    def test_an_order_with_more_lines_than_one_check_reads_is_never_promised(self):
        order, item = self.bare_order(("1000", "EA"))
        self.lines(order, item, *[("1",)] * ap.MAX_LINES)
        other = self.w.obj("Product", {"code": "SYN-3", "quantity_on_hand": ("0", "EA")})
        self.lines(order, other, ("5",))                    # the 51st line: unfulfillable
        got = self.availability(order)
        self.assertIsNone(got["date"])
        self.assertEqual((got["lines_read"], got["lines_total"]),
                         (ap.MAX_LINES, ap.MAX_LINES + 1))
        self.assertEqual(got["unknown_reasons"]["lines_not_read"], 1)


class PreparedClaims(Fixture):
    """What a prepared reply or recommendation says holds for what is sent or decided
    (AUTOMATION-007/010/011)."""

    def test_a_document_reply_never_says_attached_when_the_send_carries_none(self):
        order = self.order("PO-77")
        msg = self.message(80, "Could you send the COA for our order SO-7001?",
                           subject="COA please")
        self.w.link("Email.mentions_order", msg, order)
        self.cycle([msg])
        (item,) = self.queue().items()
        work = item["body"]["prepared"]
        self.assertNotIn("attached", work["draft"]["body"].lower())
        self.assertIn("docs_not_attached", work["gaps"])
        self.assertTrue(work["links"])                     # the pack the reviewer attaches

    def test_a_last_price_is_reused_only_in_the_lines_unit_and_the_customers_currency(self):
        from operating.tests.test_proactive_business import money
        for currency, per, ordered, reused in (("USD", "CASE", "6 kg", False),
                                               ("USD", "CASE", "6 cases", False),
                                               ("CAD", "KG", "6 cases", False),
                                               ("CAD", "CASE", "6 cases", True)):
            with self.subTest(currency=currency, per=per, ordered=ordered):
                self.setUp()
                self.w.rows["object_values"].append({
                    **[v for v in self.w.rows["object_values"]
                       if v["object_id"] == self.customer][0],
                    "property": "currency", "value": "CAD", "value_type": "text"})
                self.items_known("SYN-10")
                inv = self.w.obj("SalesInvoice", {"invoice_number": "INV-9",
                                                  "invoice_date": "2026-09-01",
                                                  "currency": currency})
                self.w.link("SalesInvoice.billed_to", inv, self.customer)
                line = self.w.obj("SalesInvoiceLine", {"unit_price": "4.00",
                                                       "invoiced_quantity": ("10", per)})
                money(self.w, line, "unit_price", currency, per=per)
                self.w.link("SalesInvoiceLine.line_of", line, inv)
                self.w.link("SalesInvoiceLine.for_item", line, self.items["SYN-10"])
                msg = self.message(95, f"Please ship:\n1. SYN-10 Gallon soap: {ordered}",
                                   subject="New PO 88130")
                self.build()
                self.cycle([msg])
                (item,) = [i for i in self.queue().items()
                           if i["body"]["prepared"].get("po_numbers") == ["88130"]]
                work = item["body"]["prepared"]
                (ln,) = work["lines"]
                self.assertEqual(ln["last_price"]["price"], "4.00")
                if reused:
                    self.assertEqual(ln["unit_price"], "4.00")
                    self.assertEqual(ap.write_spec(item)["payload"]["salesOrderLines"][0]
                                     ["unitPrice"], "4.00")
                else:
                    self.assertIsNone(ln["unit_price"])
                    self.assertIn("price_unresolved", work["gaps"])
                    self.assertIsNone(ap.write_spec(item))   # never a BC write with it

    def approval(self, totals: dict[str, tuple[str, str]], prior: list[tuple[str, str]]):
        # the owner's desk prepares decision cards (``h_desk``)
        self.env[ap.ROLES_ENV] = str(self.dir / "roles.json")
        (self.dir / "roles.json").write_text(json.dumps({"roles": {"owner": [OURS_OID]}}))
        sup = self.w.obj("Supplier", {"name": "Synth supplier"})
        for i, (amount, currency) in enumerate(prior):
            old = self.w.obj("PurchaseOrder", {"po_number": f"OLD{i}", "total_cost": amount,
                                               "currency": currency})
            self.w.link("PurchaseOrder.ordered_from", old, sup)
        for number, (amount, currency) in totals.items():
            po = self.w.obj("PurchaseOrder", {"po_number": number, "total_cost": amount,
                                              "currency": currency, "status": "Open"})
            self.w.link("PurchaseOrder.ordered_from", po, sup)
        names = " and ".join(f"PO {n}" for n in totals)
        msg = self.message(90, f"Please approve {names} for $4,200.", subject="Approval")
        path = self.dir / "s.sqlite"
        self.w.build(path)
        import sqlite3
        with sqlite3.connect(path) as c:      # the identifier table rows of the PO numbers
            for r in self.w.rows["object_values"]:
                if r["property"] == "po_number" and r["value"] in totals:
                    c.execute("INSERT INTO identifiers (identifier_kind, normalized, raw, "
                              "role, object_id, property, l1_handle, field_path, lineage, "
                              "recorded_at) VALUES ('po_number', ?, ?, 'primary', ?, "
                              "'po_number', 'h', 'row.po_number', 'workbench', "
                              "'2026-01-01T00:00:00Z')", (r["value"], r["value"],
                                                          r["object_id"]))
        got = self.cycle([msg])
        items = [i for i in self.queue().items() if i["body"]["prepared"].get("kind") ==
                 "decision"]
        self.assertTrue(items, (got, [i["body"]["prepared"] for i in self.queue().items()]))
        return items[0]["body"]["prepared"]

    def test_one_po_within_its_suppliers_range_never_approves_the_others(self):
        prior = [("1000", "CAD")] * 5
        for second in (("9000", "CAD"), ("1000", None), ("1000", "USD")):
            with self.subTest(second=second):
                self.setUp()
                card = self.approval({"106501": ("1000", "CAD"), "106502": second}, prior)
                self.assertEqual(card["recommendation"], "review", card["reasons"])
        self.setUp()
        card = self.approval({"106501": ("1000", "CAD"), "106502": ("1100", "CAD")}, prior)
        self.assertEqual(card["recommendation"], "approve", card["reasons"])


class DocumentLines(unittest.TestCase):
    def test_lines_read_from_a_pdf_text_layer_a_body_list_and_a_sheet(self):
        from operating import document_lines as dl
        pdf = ("ITEM DESCRIPTION UOM QTY. UNIT PRICE Ext Price\n"
               "SYN-10 Soap, Gallon (4 pcs/cs) 13x12x12 37 lbs,\n48 cs/plt\n"
               "CASE 240.00 19.800 4,752.00\n"
               "SYN-20 Wash Gallon CASE 336.00 19.800 6,652.80\n"
               "SYN-30 Misprinted CASE 10.00 19.800 6,652.80\n"   # figures disagree: unread
               "PO Total: 11,404.80\nPhone 604-555-0101")
        got = dl.read_lines(pdf)
        self.assertEqual([(ln["tokens"][0], ln["quantity"], ln["unit_price"]) for ln in got],
                         [("SYN-10", "240.00", "19.800"), ("SYN-20", "336.00", "19.800")])
        body = "Hi,\n1. SYN-10: Soap: 6 cases\n2. SYN-20 wash 2 cases\nThanks\nT: 604 555 0101"
        self.assertEqual([(ln["tokens"][0], ln["quantity"]) for ln in dl.read_lines(body)],
                         [("SYN-10", "6"), ("SYN-20", "2")])
        sheet = "Item,Description,Qty,Unit Price,Amount\nSYN-10,Soap,6,10.00,60.00\n" \
                "SYN-20,Wash,0,10.00,0\nTotal,,6,,60.00"
        self.assertEqual([(ln["tokens"][0], ln["quantity"], ln["cite"])
                          for ln in dl.read_lines(sheet, "csv")], [("SYN-10", "6", "C2")])
        self.assertEqual(dl.po_numbers("Order No.: PO-008875\nDate"), ["PO-008875"])
        self.assertEqual(dl.read_lines("Thanks for the call, see you at 10."), [])


if __name__ == "__main__":
    unittest.main()


class OverlappingChunks(unittest.TestCase):
    """AUTOMATION-008: the text index stores a long body as chunks sharing
    ``CHUNK_OVERLAP`` characters; both readers (a mail body, a Teams message) used to
    concatenate them, so a PO line in the overlap was read twice."""

    def test_a_line_in_the_overlap_is_read_once(self):
        import sqlite3
        import zlib
        from types import SimpleNamespace
        from ontology import text_index as ti
        from operating import autopilot_work as work

        line = "PO 40123 line 2: 500 x widget"
        filler = "word " * (ti.CHUNK_CHARS // 5)
        # the PO line sits where the second chunk starts inside the first one's tail
        text = filler[:ti.CHUNK_CHARS - 60] + " " + line + " " + filler
        pieces = ti.chunks(text)
        self.assertGreater(len(pieces), 1)
        self.assertLess(pieces[1][0], ti.CHUNK_CHARS - 30)   # the line is in both chunks
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            "CREATE TABLE object_versions (object_id TEXT, l1_handle TEXT, superseded_at TEXT);"
            "CREATE TABLE text_fields (field_id INTEGER PRIMARY KEY, citation TEXT, "
            "field_path TEXT);"
            "CREATE TABLE text_chunks (field_id INTEGER, start INTEGER, body BLOB);")
        conn.execute("INSERT INTO object_versions VALUES ('Email:1', 'h1', NULL)")
        conn.execute("INSERT INTO text_fields VALUES (1, 'h1', 'row.body_text')")
        conn.executemany("INSERT INTO text_chunks VALUES (1, ?, ?)",
                         [(at, zlib.compress(part.encode())) for at, part in pieces])
        ctx = SimpleNamespace(st=SimpleNamespace(conn=conn))
        mail = work.body_text(ctx, {"object_id": "Email:1"})
        teams = ap.teams_bodies(SimpleNamespace(conn=conn), ["h1"])["h1"]
        for read in (mail, teams):
            self.assertEqual(read, text)
            self.assertEqual(read.count(line), 1)


class CurrentOrders(unittest.TestCase):
    """AUTOMATION-012: an order a message names is current when ordered within 120 days,
    or stated open whatever its age; the filter used to drop a dated open order older
    than 120 days."""

    def test_old_open_kept_old_closed_or_unstated_dropped_undated_by_status(self):
        from datetime import date
        from types import SimpleNamespace
        from operating import autopilot_work as work

        cell = lambda v: {"value": v}  # noqa: E731
        rows = {"SalesOrder:old-open": {"order_date": cell("2026-01-05"), "status": cell("Open")},
                "SalesOrder:old-closed": {"order_date": cell("2026-01-05"),
                                          "status": cell("Closed")},
                "SalesOrder:old-unstated": {"order_date": cell("2026-01-05")},
                "SalesOrder:recent-closed": {"order_date": cell("2026-10-20"),
                                             "status": cell("Shipped")},
                "SalesOrder:undated-open": {"status": cell("Released")},
                "SalesOrder:undated-closed": {"status": cell("Invoiced")},
                "SalesOrder:undated-unstated": {}}
        ctx = SimpleNamespace(as_of=date(2026, 11, 12),
                              read=lambda ids, props: {o: rows[o] for o in ids})
        self.assertEqual(work._current(ctx, list(rows)),
                         ["SalesOrder:old-open", "SalesOrder:recent-closed",
                          "SalesOrder:undated-open", "SalesOrder:undated-unstated"])
