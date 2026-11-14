"""The work log: every path appends to one log, each person reads only their own (the owner
everyone's), the people exclusion fails closed, "continue" reopens an Ask session, a sent
message's loop closes on a reply or nudges when overdue, and Ask answers questions about
the person's own work from it."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from operating import autopilot as ap
from operating import usage_review as ur
from operator_api import feedback as fb
from operator_api import work_log as wl
from operator_api.app import create_app
from operator_api.auth import DevAuth, fixture_identity
from operator_api.mock import MockBackend
from operating import autopilot_replay as rp
from operating import proactive_engine as pe
from operating.tests import test_autopilot as engine_fixture
from operating.tests import test_autopilot_replay as replay_fixture
from operating.tests.test_proactive_threads import OURS, OUTSIDE

OID = "11111111-2222-4333-8444-555555555555"
EXCLUDED = "excluded.person@plx.example"


# The tenant's directory as the mail capture records it: each colleague's address and
# object id (the excluded person's too), so the domain is known and these are decided.
PEOPLE = {EXCLUDED: OID, **{f"{name}@plx.example": f"{i:08d}-2222-4333-8444-555555555555"
                            for i, name in enumerate(("ana", "colleague", "colleague2",
                                                      "vince"), start=20)}}


def Guard(also=(), unavailable=False):
    """The real people exclusion (``PeopleExclusion``) naming ``OID`` and ``also`` (object
    ids or the directory's addresses), with the directory attached; ``unavailable``: the
    list not loaded. Never a stand-in: a fake guard's answers hid what the real one does
    with an address it cannot decide (CT-T-06)."""
    from integrations.people_exclusion import PeopleExclusion
    if unavailable:
        return PeopleExclusion(unavailable="excluded_people_oids_absent")
    guard = PeopleExclusion([OID, *(PEOPLE.get(v, v) for v in also)])
    guard.attach_directory([(oid, address, []) for address, oid in PEOPLE.items()])
    return guard


def client_for(backend, principal):
    app = create_app(backend, DevAuth(fixture_identity("plx", [principal]), mode="mock"),
                     secure_cookie=False)
    client = TestClient(app, base_url="http://testserver")
    assert client.post("/api/auth/dev-login", json={}).status_code == 200
    return client


def ask(client, question, conversation=None):
    body = {"question": question, **({"conversation_id": conversation} if conversation else {})}
    text = client.post("/api/ask", json=body).text
    last = [b for b in text.strip().split("\n\n") if b.startswith("event: answer")][-1]
    return json.loads(last.split("data: ", 1)[1])


class WorkLogApi(unittest.TestCase):
    def setUp(self):
        self.guard = Guard()
        self.backend = MockBackend()
        self.backend.work_log = wl.WorkLog(None, guard=lambda: self.guard)
        self.backend._is_owner = lambda who: "staff:owner" in who.key
        self.ana = client_for(self.backend, "staff:ana")
        self.ben = client_for(self.backend, "staff:ben")
        self.owner = client_for(self.backend, "staff:owner")

    def log(self, client, **params):
        return client.get("/api/work-log", params=params).json()

    def test_each_path_writes_one_log_and_each_person_reads_only_their_own(self):
        first = ask(self.ana, "where is PO 90412, is it late")
        self.assertEqual(self.ana.post("/api/feedback/note", json={
            "kind": "record", "note": "chased the supplier by phone", "object_type": "po",
            "object_id": "PO-90412", "label": "PO 90412"}).status_code, 200)
        self.assertEqual(self.ana.post("/api/work-log/saved", json={
            "saved": True, "kind": "object", "label": "PO 90412", "object_type": "po",
            "object_id": "PO-90412"}).status_code, 200)
        ask(self.ben, "which orders are late")
        mine = self.log(self.ana)
        self.assertEqual([(e["kind"], e["verb"]) for e in mine["events"]],
                         [("save", "saved"), ("note", "noted"), ("ask", "asked")])
        self.assertTrue(all(e["mine"] for e in mine["events"]))
        self.assertEqual(mine["events"][2]["thread"], f"ask:{first['conversation_id']}")
        self.assertEqual(mine["events"][2]["source_id"], first["answer_id"])
        # the record page's section and the search
        on_po = self.log(self.ana, record="PO-90412")["events"]
        self.assertEqual({e["kind"] for e in on_po}, {"save", "note"})
        self.assertEqual(self.log(self.ana, q="phone")["total"], 1)
        # nobody else reads it; only the owner reads everyone's
        self.assertEqual(self.log(self.ben)["total"], 1)
        self.assertEqual(self.log(self.ben, record="PO-90412")["total"], 0)
        self.assertEqual(self.ana.get("/api/work-log", params={"everyone": True}).status_code,
                         403)
        # /api/me tells the page who may ask for everyone's, by the same decision
        self.assertEqual((self.ana.get("/api/me").json()["owner"],
                          self.owner.get("/api/me").json()["owner"]), (False, True))
        every = self.log(self.owner, everyone=True)
        self.assertEqual(every["total"], 4)
        self.assertFalse(any(e["mine"] for e in every["events"]))
        # a thread is continued and organised by its own person only
        thread = f"ask:{first['conversation_id']}"
        self.assertEqual(self.ben.get(f"/api/work-log/threads/{thread}").status_code, 404)
        self.assertEqual(self.ben.post(f"/api/work-log/threads/{thread}",
                                       json={"pinned": True}).status_code, 404)

    def test_continue_reopens_the_ask_session_as_it_was_seen(self):
        first = ask(self.ana, "where is PO 90412, is it late")
        conversation = first["conversation_id"]
        second = ask(self.ana, "who is the supplier", conversation)
        thread = f"ask:{conversation}"
        out = self.ana.post(f"/api/work-log/threads/{thread}",
                            json={"name": "PO 90412 chase", "pinned": True,
                                  "case_id": "case-77"}).json()
        self.assertEqual(out["resume"]["href"], f"/ask?c={conversation}")
        self.assertEqual([t["answer"]["answer_id"] for t in out["resume"]["turns"]],
                         [first["answer_id"], second["answer_id"]])
        self.assertEqual(out["resume"]["turns"][0]["answer"]["direct_answer"],
                         first["direct_answer"])
        self.assertEqual((out["name"], out["pinned"], out["case_id"]),
                         ("PO 90412 chase", True, "case-77"))
        self.assertEqual(self.log(self.ana, pinned=True)["total"], 2)
        self.assertEqual(self.log(self.ana, case_id="case-77")["total"], 2)
        self.assertEqual(self.log(self.ana, q="chase")["total"], 2)

    def test_a_resumed_trail_only_answer_rechecks_the_trail_sources(self):
        """A partial answer with no facts and only a trail quoting
        records was resumed without asking the read policy. Every source the kept turn
        would show (trail steps, conflicts) is decided again; refused or undecidable,
        the turn is withheld."""
        from operator_api import models as m
        step = "bc://x/purchaseOrders/p9@1"
        other = "graph-mail://box/m1@1"
        original = self.backend.ask

        def trail_only(reader, request):
            for event, payload in original(reader, request):
                if event == "answer":
                    src = {"system": "BC", "handle": step, "label": "BC PO 4501",
                           "quote": "Promised 2026-12-01 by Northline", "fields": None}
                    payload = payload.model_copy(update={
                        "facts": [], "trail": m.EvidenceTrail.model_validate({
                            "kind": "claims", "title": "Records read",
                            "steps": [{"kind": "document", "title": "PO 4501 read",
                                       "source": src, "check": "read_from_record"}]}),
                        "conflicts": [m.Conflict.model_validate({
                            "field": "promised", "values": [
                                {"value": "2026-12-01", "source": src, "trusted": True,
                                 "why": "record"},
                                {"value": "2026-12-09", "source": {
                                    **src, "system": "Email", "handle": other,
                                    "quote": "Northline mail"},
                                 "trusted": False, "why": "mail"}]})]})
                yield event, payload

        asked = []

        def refuse(refused):
            def withheld(_reader, handles):
                asked.append(sorted(handles))
                return "withheld" if set(handles) & refused else None
            return withheld

        with mock.patch.object(self.backend, "ask", trail_only):
            first = ask(self.ana, "where is PO 4501")
        self.assertEqual(first["facts"], [])
        thread = f"/api/work-log/threads/ask:{first['conversation_id']}"
        with mock.patch.object(self.backend, "ask_withheld", refuse(set())):
            open_turn = self.ana.get(thread).json()["resume"]["turns"][0]
        self.assertEqual(asked, [sorted({step, other})])
        self.assertIn("Northline", json.dumps(open_turn["answer"]))
        for refused in ({step}, {other}):
            with mock.patch.object(self.backend, "ask_withheld", refuse(refused)):
                turn = self.ana.get(thread).json()["resume"]["turns"][0]
            self.assertIsNone(turn["answer"])
            self.assertTrue(turn["withheld"])
            self.assertNotIn("Northline", json.dumps(turn))

        def undecidable(_reader, _handles):
            raise TimeoutError("read policy busy")
        with mock.patch.object(self.backend, "ask_withheld", undecidable):
            turn = self.ana.get(thread).json()["resume"]["turns"][0]
        self.assertIsNone(turn["answer"])
        # a source-shaped part with no handle cannot be decided: withheld
        from operator_api.ask_drafts import cited_handles
        with self.assertRaises(ValueError):
            cited_handles({"facts": [], "trail": {"steps": [{"source": {
                "system": "BC", "label": "PO 4501", "quote": "Promised 2026-12-01",
                "handle": ""}}]}})

    def test_the_exclusion_fails_closed_when_writing_and_reading(self):
        ask(self.ana, "where is PO 90412, is it late")
        self.guard = Guard(unavailable=True)
        ask(self.ana, "which orders are late")          # not written
        page = self.log(self.ana)
        self.assertEqual((page["total"], page["events"]), (None, []))
        answer = ask(self.ana, "what did I ask today")
        self.assertEqual(answer["outcome"], "refused")
        self.guard = Guard()
        self.assertEqual(self.log(self.ana)["total"], 1)
        # an excluded person's own work is never written
        excluded = client_for(self.backend, f"staff:{OID}")
        ask(excluded, "where is PO 90412, is it late")
        self.assertEqual(self.log(self.owner, everyone=True)["total"], 1)

    def test_a_signed_in_reader_is_logged_under_the_exclusion_the_api_loads(self):
        """The guard the API builds (``load_exclusion``: object ids alone) decides no
        address. A signed-in reader (object id and address, as an Entra sign-in carries)
        was shut out for their undecidable address, so nobody's work was logged; the
        object id now decides: their work is logged, the excluded person's never is, and
        a session with an address alone stays out (fail closed). CT-T-06."""
        import dataclasses
        import os
        from operating.pg.permissions import load_exclusion
        with mock.patch.dict(os.environ, {"PLX_EXCLUDED_PEOPLE_OIDS": OID}):
            os.environ.pop("PLX_EXCLUDED_PEOPLE_FILE", None)
            self.guard = load_exclusion()
        self.assertEqual(repr(self.guard), "PeopleExclusion(ids_only, 1 ids)")

        def signed_in(principal, oid, email):
            session = dataclasses.replace(fixture_identity("plx", [principal])(),
                                          subject_oid=oid, email=email,
                                          identities=frozenset({oid} if oid else ()))
            app = create_app(self.backend, DevAuth(lambda: session, mode="mock"),
                             secure_cookie=False)
            client = TestClient(app, base_url="http://testserver")
            self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
            return client
        dee = signed_in("staff:dee", "77777777-2222-4333-8444-555555555555", "dee@plx.example")
        gone = signed_in("staff:gone", OID, EXCLUDED)
        bare = signed_in("staff:bare", None, "bare@plx.example")
        for client in (dee, gone, bare):
            ask(client, "where is PO 90412, is it late")
        self.assertEqual(self.log(dee)["total"], 1)
        everyone = self.log(self.owner, everyone=True)
        self.assertEqual(everyone["total"], 1, everyone)
        self.assertNotIn(EXCLUDED, json.dumps(everyone))

    def test_saving_a_record_whose_id_is_outside_the_thread_grammar_is_kept(self):
        """A projected record's id (``{"id":"..."}``) or a friendly path's ref with a
        space was refused (422) and the Save lost; it is kept with its record."""
        for object_id in ('PurchaseOrder:portal.app_purchase_order:{"id":"po-1"}',
                          "Ridgeway Labels"):
            sent = self.ana.post("/api/work-log/saved", json={
                "saved": True, "kind": "object", "label": "PO 1", "object_type": "Supplier",
                "object_id": object_id})
            self.assertEqual(sent.status_code, 200, sent.text)
        events = self.log(self.ana, kind="save")["events"]
        self.assertEqual(len(events), 2, events)
        self.assertIn('{\\"id\\":\\"po-1\\"}', json.dumps(events))

    def test_ask_about_my_own_work_answers_from_my_log_only(self):
        ask(self.ana, "where is PO 90412, is it late")
        ask(self.ben, "is the supplier for SO 5512 late")
        answer = ask(self.ana, "what did I ask today")
        self.assertEqual(answer["outcome"], "answered")
        self.assertIn("where is PO 90412", answer["direct_answer"])
        self.assertNotIn("5512", answer["direct_answer"])
        record = ask(self.ana, "where was I on 90412")
        self.assertIn("where is PO 90412", record["direct_answer"])
        # a business question is not routed to the log
        business = ask(self.ana, "what did I order from the supplier last month")
        self.assertFalse(business["direct_answer"].startswith("Your work log"))
        # nothing mailed from the Workbench: what the person mailed is asked of their mail
        mailed = ask(self.ben, "what did I send yesterday")
        self.assertFalse(mailed["direct_answer"].startswith("Your work log"))
        self.assertTrue(ask(self.ben, "what did I ask yesterday")["direct_answer"]
                        .startswith("Your work log has nothing"))
        # AP-11: an approval given outside the Workbench (Mission Control, Business
        # Central) is asked of the business systems too; a save is the log's alone
        approved = ask(self.ben, "what did I approve yesterday")
        self.assertFalse(approved["direct_answer"].startswith("Your work log"), approved)
        self.assertTrue(ask(self.ben, "what did I save yesterday")["direct_answer"]
                        .startswith("Your work log has nothing"))

    def test_a_confirm_logs_its_own_row_without_reading_the_journal_again(self):
        """AP-11: the confirm's log entry comes from the row the desk just wrote (its
        recipients and subject), never from a second read of the whole actions journal;
        the journal is read only under its lock, where the desk checks the chain."""
        import inspect
        from operating import card_actions as ca
        from operating.session_identity import CHANNEL_CONSOLE, fixture_session
        self.backend.actions_environ = {"PLX_EXCLUDED_PEOPLE_OIDS": OID}
        session = fixture_session("plx", ["staff:demo"], channel=CHANNEL_CONSOLE,
                                  subject_email="vince@plx.example",
                                  subject_oid="11111111-1111-4111-8111-111111111111")
        vince = TestClient(create_app(self.backend, DevAuth(lambda: session, mode="mock"),
                                      secure_cookie=False), base_url="http://testserver")
        self.assertEqual(vince.post("/api/auth/dev-login", json={}).status_code, 200)
        card = next(c for c in vince.get("/api/suggestions").json()["items"]
                    if c["kind"] == "vendor_chase" and c["draft"]["to"]["address"])
        ask_ = {"card_id": card["id"], "kind": "send_email"}
        found = vince.post("/api/actions/preview", json=ask_).json()
        readers = []
        real = ca.Journal.rows

        def rows(journal):
            readers.append(inspect.stack()[1].function)
            return real(journal)
        for n in range(3):
            with mock.patch.object(ca.Journal, "rows", rows):
                done = vince.post("/api/actions/confirm", json={
                    **ask_, "confirm": True, "idempotency_key": f"dialog-0123456789abcd{n:02d}",
                    "payload_hash": found["payload_hash"]}).json()
            self.assertEqual(done["status"], "dry_run")
            if n == 0:
                break
        self.assertTrue(readers)
        self.assertEqual(set(readers), {"__enter__"}, readers)
        [event] = [e for e in self.log(vince)["events"] if e["kind"] == "action"]
        self.assertEqual((event["verb"], event["title"], event["people"]),
                         ("prepared", found["payload"]["subject"], found["payload"]["to"]))


class Loops(unittest.TestCase):
    """A sent message opens a loop that a reply closes, the person closes, or that nudges
    once overdue."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.now = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)
        self.guard = Guard()
        self.store = wl.WorkLog(Path(tmp.name) / "wl", guard=lambda: self.guard,
                                clock=lambda: self.now)
        self.queue_path = Path(tmp.name) / "autopilot.sqlite"
        ap.Queue(self.queue_path).close()
        self.store.inbound_path = lambda: self.queue_path
        self.notes = wl.Notes(self.store, lambda who: "ana@plx.example")
        self.reader = type("R", (), {"key": "plx:staff:ana", "display_name": "Ana",
                                     "email": "ana@plx.example",
                                     "session": type("S", (), {"identities": frozenset(),
                                                               "subject_oid": None})()})()

    def send(self, to, card="card-1", seq=1):
        result = {"card_id": card, "kind": "send_email", "status": "sent", "text": "Sent",
                  "at": self.now.isoformat(), "seq": seq}
        row = {"to": to, "cc": [], "subject": "PO 90412 ship date",
               "in_reply_to": "msg-1", "card_label": "PO 90412"}
        self.notes.action(self.reader, result, row,
                          {"subject": {"object_id": "PO-90412", "type": "po", "label": "PO"}})

    def loop(self):
        events, _ = self.store.events(reader="plx:staff:ana", open_loops=True)
        return events[0]["loop"] if events else None

    def inbound(self, conversation, at, sender="buyer@vendor.example"):
        queue = ap.Queue(self.queue_path)
        queue.note_inbound([{"mailbox": "ana@plx.example", "conversation_id": conversation,
                             "sender": sender, "at": at, "event_type": "message.received"}])
        queue.conn.commit()
        queue.close()

    def test_only_a_reply_in_the_thread_answers_mail_from_them_elsewhere_is_a_maybe(self):
        """API-007: mail from a recipient outside the sent message's thread is only a
        possible reply (shown, never counted); a message in the thread answers it."""
        self.send(["buyer@vendor.example"])
        self.assertEqual(self.loop()["state"], "waiting")
        self.inbound("other", "2026-11-11T09:00:00Z")
        loop = self.loop()
        self.assertEqual((loop["state"], loop["answered_at"], loop["possible_reply_at"]),
                         ("waiting", None, "2026-11-11T09:00:00Z"))
        event = self.store.events(reader="plx:staff:ana")[0][0]
        self.assertIn("outside the thread", wl.say(event))
        self.now += timedelta(hours=wl.REPLY_HOURS + 1)   # overdue still says it
        self.assertEqual(self.loop()["state"], "overdue")
        self.assertEqual(self.loop()["possible_reply_at"], "2026-11-11T09:00:00Z")
        self.inbound("msg-1", "2026-11-12T10:00:00Z", sender="someone@vendor.example")
        loop = self.loop()
        self.assertEqual((loop["state"], loop["answered_at"]),
                         ("answered", "2026-11-12T10:00:00Z"))
        self.assertNotIn("outside the thread", wl.say(
            self.store.events(reader="plx:staff:ana")[0][0]))

    def test_without_a_known_conversation_mail_from_a_recipient_answers_it(self):
        """A new message has no conversation to match yet: the recipient's mail answers
        it (never 'No reply' over a real reply)."""
        self.notes.action(self.reader, {"card_id": "card-7", "kind": "send_email",
                                        "status": "sent", "text": "Sent",
                                        "at": self.now.isoformat(), "seq": 7},
                          {"to": ["buyer@vendor.example"], "subject": "PO 90412"}, None)
        self.inbound("any", "2026-11-11T09:00:00Z", sender="other@vendor.example")
        self.assertEqual(self.loop()["state"], "waiting")   # not a recipient
        self.inbound("any", "2026-11-11T10:00:00Z")
        self.now += timedelta(hours=wl.REPLY_HOURS + 30)
        loop = self.loop()
        self.assertEqual((loop["state"], loop["answered_at"], loop["nudge"]),
                         ("answered", "2026-11-11T10:00:00Z", None))

    def card_reply(self, conversation_of, kind="reply_in_thread"):
        """A card's reply as the desk records it: ``in_reply_to`` is the cited message's
        handle (``card_actions.thread_handle``), never its conversation id."""
        notes = wl.Notes(self.store, lambda who: "ana@plx.example", conversation_of)
        notes.action(self.reader, {"card_id": "card-8", "kind": kind, "status": "sent",
                                   "text": "Sent", "at": self.now.isoformat(), "seq": 8},
                     {"to": ["buyer@vendor.example"], "cc": [], "subject": "RE: PO 90412",
                      "in_reply_to": "graph-mail://m/AAMk-1@c-1", "card_label": "PO 90412"},
                     {"subject": {"object_id": "PO-90412", "type": "po", "label": "PO"}})

    def test_a_vendor_reply_in_the_card_replys_conversation_answers_it(self):
        """b795e24c05 repair: the card reply's handle is resolved to its conversation,
        the key inbound mail is tracked by, so the vendor's reply in that thread four
        days on answers it: no 'No reply in 4 days', no follow-up draft."""
        asked = []

        def conversation_of(reader, handle):
            asked.append((reader.key, handle))
            return "AAQk-conv-90412" if handle == "graph-mail://m/AAMk-1@c-1" else None

        for kind in ("reply_in_thread", "forward"):
            with self.subTest(kind=kind):
                self.setUp()
                asked.clear()
                self.card_reply(conversation_of, kind)
                self.assertEqual(asked, [("plx:staff:ana", "graph-mail://m/AAMk-1@c-1")])
                self.inbound("AAQk-conv-90412", "2026-11-14T09:00:00Z")
                self.now += timedelta(days=4)
                loop = self.loop()
                self.assertEqual((loop["state"], loop["answered_at"], loop["nudge"]),
                                 ("answered", "2026-11-14T09:00:00Z", None))
                self.assertNotIn("follow_up", loop)
                self.assertNotIn("No reply", wl.say(
                    self.store.events(reader="plx:staff:ana")[0][0]))

    def test_a_card_reply_whose_conversation_cannot_be_resolved_reads_recipient_mail(self):
        """The handle never stands in for a conversation id (it never matches inbound):
        unresolved, the vendor's mail answers it as before; resolved, mail of another
        conversation is only a possible reply."""
        for resolver in (None, lambda reader, handle: None,
                         lambda reader, handle: 1 / 0):
            with self.subTest(resolver=resolver):
                self.setUp()
                self.card_reply(resolver)
                self.inbound("AAQk-conv-90412", "2026-11-14T09:00:00Z")
                self.now += timedelta(days=4)
                self.assertEqual(self.loop()["state"], "answered")
        self.setUp()
        self.card_reply(lambda reader, handle: "AAQk-conv-90412")
        self.inbound("AAQk-other", "2026-11-14T09:00:00Z")
        self.now += timedelta(days=4)
        loop = self.loop()
        self.assertEqual((loop["state"], loop["possible_reply_at"]),
                         ("overdue", "2026-11-14T09:00:00Z"))

    def test_overdue_nudges_and_the_person_can_close_it(self):
        self.send(["buyer@vendor.example"])
        self.now += timedelta(hours=wl.REPLY_HOURS + 1)
        loop = self.loop()
        self.assertEqual(loop["state"], "overdue")
        self.assertIn("buyer@vendor.example", loop["nudge"])
        self.assertIn("follow-up", loop["follow_up"])
        thread = self.store.events(reader="plx:staff:ana")[0][0]["thread"]
        self.store.organise("plx:staff:ana", thread, closed=True)
        self.assertIsNone(self.loop())   # closed loops leave the waiting list

    def test_a_send_naming_an_excluded_person_is_never_logged_and_rows_are_rechecked(self):
        self.send([EXCLUDED])
        self.assertEqual(self.store.events(reader="plx:staff:ana")[1], 0)
        self.send(["colleague@plx.example"], card="card-2")
        self.assertEqual(self.store.events(reader="plx:staff:ana")[1], 1)
        # the colleague is excluded later: their row is hidden from then on
        self.guard = Guard(also=["colleague@plx.example"])
        self.assertEqual(self.store.events(reader="plx:staff:ana")[1], 0)

    def test_the_log_is_append_only_and_refuses_unknown_events(self):
        self.send(["buyer@vendor.example"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._conn.execute("UPDATE event SET title = 'x'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._conn.execute("DELETE FROM event")
        with self.assertRaises(ValueError):
            self.store.record(self.reader, kind="action", verb="deleted", title="x",
                              thread="card:x", source="actions_journal")
        with self.assertRaises(ValueError):
            self.store.record(self.reader, kind="action", verb="sent", title="x",
                              thread="nowhere", source="actions_journal")

class TimeSaved(unittest.TestCase):
    """O-03: per person and week, the minutes the Workbench saved, counting only work that
    was used, each accepted-and-sent item once, weeks from Monday in US Eastern; fail closed
    on the exclusion; a reader sees their own, the owner everyone's."""

    def setUp(self):
        self.now = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)   # a Wednesday
        self.guard = Guard()
        self.store = wl.WorkLog(None, guard=lambda: self.guard, clock=lambda: self.now)

    def reader(self, who):
        """A signed-in reader: their address and the object id their sign-in carries
        (the exclusion decides a reader by it; the address alone is undecidable)."""
        import uuid
        return type("R", (), {"key": f"plx:staff:{who}", "display_name": who.title(),
                              "email": f"{who}@plx.example",
                              "session": type("S", (), {"identities": frozenset(),
                                                        "subject_oid": str(uuid.uuid5(
                                                            uuid.NAMESPACE_DNS, who))})()})()

    def put(self, who, kind, verb, item="i1", at=None, sent=True, people=(), outcome=None):
        """``outcome``: the status Notes records on an action or write event."""
        self.store.clock = lambda: at or self.now
        self.store.record(self.reader(who), kind=kind, verb=verb, title="t",
                          thread=f"autopilot:{item}" if kind == "autopilot" else
                          f"ask:{item}" if kind == "ask" else f"card:{item}",
                          source="x", source_id=item, people=people,
                          detail={"status": outcome} if outcome else None,
                          awaiting={"due": "2026-11-20T00:00:00Z"} if sent else None)
        self.store.clock = lambda: self.now

    def test_only_used_work_counts_once_by_its_weight_in_its_eastern_week(self):
        mins = wl.saved_minutes()
        self.assertEqual(mins, {"mail_sent": 6, "record": 5, "teams_sent": 2})
        self.put("ana", "autopilot", "accepted", "a1")
        self.put("ana", "autopilot", "sent", "a1")            # one item: the message only
        self.put("ana", "autopilot", "accepted", "a2")        # accepted, no message
        self.put("ana", "autopilot", "prepared", "a3")        # prepared, never used
        self.put("ana", "autopilot", "rejected", "a4")
        self.put("ana", "action", "sent", "c1", outcome="sent")             # went out
        self.put("ana", "action", "sent", "c2", sent=False, outcome="dry_run")  # nothing went out
        self.put("ana", "action", "messaged", "c3", sent=False, outcome="dry_run")
        self.put("ana", "action", "messaged", "c6", sent=False, outcome="sent")  # a live Teams message
        self.put("ana", "action", "messaged", "c7", sent=False)  # says nothing of its outcome
        self.put("ana", "action", "dismissed", "c4", sent=False)
        self.put("ana", "ask", "asked", "q1")
        self.put("ana", "ask", "failed", "q2")
        self.put("ana", "alert", "dismissed", "c5", sent=False)
        self.put("ana", "write", "executed", "w1", outcome="written")
        self.put("ana", "write", "executed", "w2", outcome="would_write")  # a dry-run write
        self.put("ana", "write", "executed", "w3")                         # no outcome: nothing
        # Sunday 11:30 PM ET is last week; Monday 12:30 AM ET is this week
        self.put("ana", "ask", "asked", "q3", at=datetime(2026, 11, 9, 4, 30, tzinfo=timezone.utc))
        self.put("ana", "ask", "asked", "q4", at=datetime(2026, 11, 9, 5, 30, tzinfo=timezone.utc))
        self.put("ana", "ask", "asked", "q5", at=datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.put("ben", "ask", "asked", "q6")
        found = wl.time_saved(self.store, reader="plx:staff:ana")
        self.assertEqual(found["weeks"], ["2026-10-19", "2026-10-26", "2026-11-02",
                                          "2026-11-09"])
        [ana] = found["people"]
        self.assertEqual(ana["weeks"][-1]["items"], {"mail_sent": 2, "record": 4,
                                                     "teams_sent": 1})
        self.assertEqual(ana["weeks"][-1]["minutes"], 2 * 6 + 4 * 5 + 2)
        self.assertEqual(ana["weeks"][-2], {"start": "2026-11-02", "minutes": 5,
                                            "items": {"record": 1}})
        self.assertEqual([w["minutes"] for w in ana["weeks"][:2]], [0, 0])
        self.assertIn("estimate", found["method"])
        self.assertIn("6 min", found["method"])
        every = wl.time_saved(self.store, reader=None)
        self.assertEqual([p["name"] for p in every["people"]], ["Ana", "Ben"])

    def test_a_dry_run_teams_message_and_a_would_write_save_nothing(self):
        """Through the paths that log them (Notes.action, Notes.write): neither went out."""
        notes, ana = wl.Notes(self.store, lambda who: "ana@plx.example"), self.reader("ana")
        notes.action(ana, {"card_id": "card-1", "kind": "teams_message", "status": "dry_run",
                           "text": "Not sent", "at": self.now.isoformat(), "seq": 1},
                     {"to": ["dispatch@vendor.example"], "card_label": "PO 90412"}, None)
        notes.write(ana, "executed", {"intent": {"intent_id": "wi-1", "record_id": "PO-1"},
                                      "status": "would_write", "text": "Would set the date",
                                      "at": self.now.isoformat(), "replayed": False})
        events, _ = self.store.events(reader="plx:staff:ana")
        self.assertEqual(sorted((e["kind"], e["verb"]) for e in events),
                         [("action", "prepared"), ("write", "executed")])  # logged, as work
        found = wl.time_saved(self.store, reader="plx:staff:ana")
        self.assertEqual(sum(w["minutes"] for p in found["people"] for w in p["weeks"]), 0)
        # the same two, live, count 2 + 5 minutes
        notes.action(ana, {"card_id": "card-2", "kind": "teams_message", "status": "sent",
                           "text": "Sent", "at": self.now.isoformat(), "seq": 2},
                     {"to": ["dispatch@vendor.example"], "card_label": "PO 90412"}, None)
        notes.write(ana, "executed", {"intent": {"intent_id": "wi-2", "record_id": "PO-1"},
                                      "status": "written", "text": "Set the date",
                                      "at": self.now.isoformat(), "replayed": False})
        [person] = wl.time_saved(self.store, reader="plx:staff:ana")["people"]
        self.assertEqual(person["weeks"][-1]["items"], {"teams_sent": 1, "record": 1})
        self.assertEqual(person["weeks"][-1]["minutes"], 2 + 5)

    def test_a_dry_run_send_is_logged_and_recalled_as_prepared_never_sent(self):
        """API-005: a dry-run send (nothing went out) reads "Prepared (not sent)" in the log
        and in Ask's recall, opens no reply loop, also for a row logged before the fix
        (verb "sent", status dry_run); a live send still reads "Sent"."""
        notes, ana = wl.Notes(self.store, lambda who: "ana@plx.example"), self.reader("ana")
        for seq, (kind, status) in enumerate([("send_email", "dry_run"),
                                              ("reply_in_thread", "dry_run"),
                                              ("forward", "dry_run"),
                                              ("send_email", "sent")], 1):
            notes.action(ana, {"card_id": f"card-{seq}", "kind": kind, "status": status,
                               "text": "x", "at": self.now.isoformat(), "seq": seq},
                         {"to": ["buyer@vendor.example"], "subject": f"PO 9041{seq} date",
                          "in_reply_to": "msg-1"}, None)
        self.put("ana", "action", "sent", "old", sent=False, outcome="dry_run")  # legacy row
        events, total = self.store.events(reader="plx:staff:ana")
        self.assertEqual(total, 5)
        said = {e["title"]: (e["verb"], wl.say(e), e["loop"]) for e in events}
        for subject in ("PO 90411 date", "PO 90412 date", "PO 90413 date", "t"):
            verb, line, loop = said[subject]
            self.assertEqual(verb, "prepared")
            self.assertTrue(line.startswith("Prepared (not sent): "), line)
            self.assertIsNone(loop)
        verb, line, loop = said["PO 90414 date"]
        self.assertEqual((verb, line[:6]), ("sent", "Sent: "))
        self.assertIsNotNone(loop)
        answer = wl.recall_answer(wl.recall(self.store, "plx:staff:ana",
                                            "what did I send today"))
        self.assertEqual(answer.count("Prepared (not sent): "), 4, answer)
        self.assertEqual(answer.count("Sent: "), 1, answer)
        # an unknown action status is not logged as anything that happened
        notes.action(ana, {"card_id": "card-9", "kind": "send_email", "status": "unknown",
                           "text": "x", "at": self.now.isoformat(), "seq": 9},
                     {"to": ["buyer@vendor.example"], "subject": "PO 90419"}, None)
        self.assertEqual(self.store.events(reader="plx:staff:ana")[1], 5)

    def test_what_did_i_send_lists_every_path_that_sent_and_nothing_else(self):
        """API-006: an autopilot send is a send; a snooze, dismissal, assignment, mark-done
        or follow-up is not; a failed or unknown outcome is not; a dry run reads
        "Prepared (not sent)"."""
        ana = self.reader("ana")
        rows = [("autopilot", "sent", "autopilot reply to Acme", None),
                ("autopilot", "accepted", "autopilot accepted", None),
                ("action", "sent", "card email", "sent"),
                ("action", "replied", "card reply", "sent"),
                ("action", "messaged", "teams message", "sent"),
                ("action", "snoozed", "snoozed card", "snoozed"),
                ("action", "dismissed", "dismissed card", "dismissed"),
                ("action", "assigned", "assigned card", "assigned"),
                ("action", "done", "done card", "done"),
                ("action", "follow_up", "follow up card", "follow_up"),
                ("action", "prepared", "dry run email", "dry_run")]
        for i, (kind, verb, title, status) in enumerate(rows):
            self.store.record(ana, kind=kind, verb=verb, title=title,
                              thread=f"autopilot:i{i}" if kind == "autopilot" else
                              f"card:c{i}", source="x", source_id=str(i),
                              detail={"status": status} if status else None)
        self.store.record(ana, kind="action", verb="sent", title="legacy loop email",
                          thread="card:old", source="x", source_id="old",
                          awaiting={"due": "2026-11-20T00:00:00Z"})
        self.store.record(self.reader("ben"), kind="autopilot", verb="sent",
                          title="ben's send", thread="autopilot:b", source="x",
                          source_id="b")
        found = wl.recall(self.store, "plx:staff:ana", "what did I send today")
        self.assertEqual(sorted(e["title"] for e in found["events"]),
                         ["autopilot reply to Acme", "card email", "card reply",
                          "dry run email", "legacy loop email", "teams message"])
        self.assertEqual(found["total"], 6)
        answer = wl.recall_answer(found)
        self.assertIn("Prepared (not sent): dry run email", answer)
        self.assertNotIn("ben's send", answer)

    def test_a_long_log_is_read_in_bounded_batches_and_pages_stay_exact(self):
        """API-008: matches are read and exclusion-checked in batches of ``EVENT_BATCH``,
        never all at once; each page and the total stay exact, excluded rows skipped."""
        reads = []

        class Cursor:
            def __init__(self, cur, sql):
                self.cur, self.light = cur, "e.seq, e.reader, e.people" in sql

            def fetchall(self):
                rows = self.cur.fetchall()
                if self.light:
                    reads.append(("all", len(rows)))
                return rows

            def fetchmany(self, size):
                rows = self.cur.fetchmany(size)
                reads.append(("many", len(rows)))
                return rows

            def __getattr__(self, name):
                return getattr(self.cur, name)

        class Conn:
            def __init__(self, conn):
                self.conn = conn

            def execute(self, sql, *args):
                return Cursor(self.conn.execute(sql, *args), sql)

            def __getattr__(self, name):
                return getattr(self.conn, name)
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.store = wl.WorkLog(None, guard=lambda: self.guard, clock=lambda: self.now)
                for i in range(n):
                    self.put("ana", "action", "sent", f"c{i}", outcome="sent",
                             people=[EXCLUDED] if i % 7 == 3 else ())
                shown = [i for i in range(n) if i % 7 != 3]   # an excluded row is never logged
                self.store._conn = Conn(self.store._conn)
                reads.clear()
                with mock.patch.object(wl, "EVENT_BATCH", 4):
                    seen, offset = [], 0
                    while True:
                        events, total = self.store.events(reader="plx:staff:ana",
                                                          limit=5, offset=offset)
                        self.assertEqual(total, len(shown))
                        if not events:
                            break
                        seen += [e["source_id"] for e in events]
                        offset += 5
                self.assertEqual(seen, [f"c{i}" for i in reversed(shown)])
                self.assertNotIn("all", [k for k, _ in reads])
                self.assertTrue(reads and max(size for _, size in reads) <= 4, reads)
        # a row excluded after it was logged is skipped by the batched read too
        self.guard = Guard(also=["colleague@plx.example"])
        self.put("ana", "action", "sent", "late", outcome="sent",
                 people=["colleague2@plx.example"])
        before = self.store.events(reader="plx:staff:ana")[1]
        self.guard = Guard(also=["colleague2@plx.example"])
        self.assertEqual(self.store.events(reader="plx:staff:ana")[1], before - 1)

    def test_an_answer_too_large_to_keep_keeps_its_drafts_and_says_so(self):
        """API-010: an Ask answer over ``MAX_DETAIL`` keeps its question, instruction and
        drafts with every handle it cites (so a re-check stays complete), and a resumed
        session says the turn was not kept in full instead of dropping it silently."""
        ana = self.reader("ana")
        trail = "bc://x/purchaseOrders/p9@1"
        big = {"question": "draft the chase for PO 90412", "conversation_id": "c1",
               "answer_id": "a1", "direct_answer": "Here is a draft.",
               "instruction": {"status": "question", "text": "chase PO 90412"},
               "drafts": [{"id": "askdraft:a1:0", "subject": "PO 90412"}],
               "facts": [{"text": "x", "sources": [{"handle": "graph-mail://b/m1@1"}]}],
               "trail": {"steps": [{"source": {"system": "BC", "handle": trail,
                                               "quote": "q" * (wl.MAX_DETAIL + 10)}}]}}
        self.store.record(ana, kind="ask", verb="asked", title=big["question"],
                          thread="ask:c1", source="ask", source_id="a1", detail=big)
        kept = self.store.ask_detail("plx:staff:ana", "a1")
        self.assertEqual(kept["_kept"], "partial")
        self.assertEqual((kept["drafts"], kept["instruction"]),
                         (big["drafts"], big["instruction"]))
        from operator_api.ask_drafts import cited_handles
        self.assertEqual(set(cited_handles(kept)), {trail, "graph-mail://b/m1@1"})
        events, _ = self.store.events(reader="plx:staff:ana", thread="ask:c1")
        [turn] = wl.resume("ask:c1", events)["turns"]
        self.assertEqual((turn["question"], turn["answer"], turn["withheld"]),
                         (big["question"], None, wl.PARTIAL_TURN))
        # a source without a handle cannot be decided: the drafts are not kept
        bad = {**big, "answer_id": "a2",
               "conflicts": [{"system": "BC", "quote": "no handle"}]}
        self.store.record(ana, kind="ask", verb="asked", title="q", thread="ask:c2",
                          source="ask", source_id="a2", detail=bad)
        kept = self.store.ask_detail("plx:staff:ana", "a2")
        self.assertNotIn("drafts", kept)
        with self.assertRaises(ValueError):
            cited_handles(kept)   # its re-check withholds the turn

    def test_cardinality_and_the_exclusion(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.store = wl.WorkLog(None, guard=lambda: self.guard,
                                        clock=lambda: self.now)
                for i in range(n):
                    self.put("ana", "ask", "asked", f"q{i}")
                    self.put(f"p{i}", "autopilot", "sent", f"a{i}")
                every = wl.time_saved(self.store, reader=None)
                self.assertEqual(len(every["people"]), n + 1)
                by = {p["name"]: p["weeks"][-1]["minutes"] for p in every["people"]}
                self.assertEqual(by["Ana"], 5 * n)
                self.assertEqual({by[f"P{i}"] for i in range(n)}, {6})
                self.assertEqual(sum(p["weeks"][-1]["minutes"] for p in every["people"]),
                                 5 * n + 6 * n)
        # a row naming a person excluded later is no longer counted; no list, nothing
        self.put("ana", "action", "sent", "cx", people=["colleague@plx.example"], outcome="sent")
        before = wl.time_saved(self.store, reader="plx:staff:ana")["people"][0]
        self.guard = Guard(also=["colleague@plx.example"])
        after = wl.time_saved(self.store, reader="plx:staff:ana")["people"][0]
        self.assertEqual(before["weeks"][-1]["minutes"] - after["weeks"][-1]["minutes"], 6)
        self.guard = Guard(unavailable=True)
        self.assertIsNone(wl.time_saved(self.store, reader="plx:staff:ana"))

    def test_a_reader_sees_their_own_and_only_the_owner_everyones(self):
        backend = MockBackend()
        backend.work_log = self.store
        backend._is_owner = lambda who: "staff:owner" in who.key
        ana, owner = client_for(backend, "staff:ana"), client_for(backend, "staff:owner")
        ask(ana, "which orders are late")
        ask(owner, "which orders are late")
        self.store.clock = lambda: datetime.now(timezone.utc)
        mine = ana.get("/api/time-saved").json()
        self.assertTrue(mine["available"])
        self.assertEqual([(p["mine"], p["weeks"][-1]["minutes"]) for p in mine["people"]],
                         [(True, 5)])
        self.assertEqual(ana.get("/api/time-saved", params={"everyone": True}).status_code,
                         403)
        # the owner's own view is theirs; in everyone's the owner is never one of the people
        self.assertEqual([p["mine"] for p in owner.get("/api/time-saved").json()["people"]],
                         [True])
        every = owner.get("/api/time-saved", params={"everyone": True}).json()
        self.assertEqual([p["mine"] for p in every["people"]], [False])   # Ana alone
        self.assertEqual(ana.get("/api/time-saved", params={"weeks": 13}).status_code, 422)
        self.guard = Guard(unavailable=True)
        closed = ana.get("/api/time-saved").json()
        self.assertEqual((closed["available"], closed["people"]), (False, []))


    def test_the_checks_over_every_row_read_indexes_not_the_long_rows(self):
        """AP-11: a row's Ask answer (``detail``, up to 200 KB) sits before its people and
        loop; the page's people re-check over every matching row and the autopilot's
        answered-in-Workbench lookup read covering indexes, never those rows. A log made
        before the indexes gets them on its next open (declared step 2)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "work-log" / "work-log.sqlite"
            path.parent.mkdir()
            old = sqlite3.connect(path)
            from operating.declared_schema import work_log as declared
            old.executescript(declared._DDL)
            old.close()
            store = wl.WorkLog(path.parent, guard=lambda: self.guard, clock=lambda: self.now)
            notes, ana = wl.Notes(store, lambda who: "ana@plx.example"), self.reader("ana")
            for n in (1, 10, 60):
                for i in range(n):
                    store.record(ana, kind="ask", verb="asked", title=f"q{n}-{i}",
                                 thread=f"ask:q{n}-{i}", source="x", source_id=f"q{n}-{i}",
                                 detail={"answer": "x" * 150_000})
                    notes.action(ana, {"card_id": f"card-{n}-{i}", "kind": "send_email",
                                       "status": "sent", "text": "x",
                                       "at": self.now.isoformat(), "seq": i},
                                 {"to": ["buyer@vendor.example"], "subject": f"PO {n}-{i}",
                                  "in_reply_to": f"msg-{n}-{i}"}, None)
                seen: list[str] = []
                store._conn.set_trace_callback(seen.append)
                for filters in ({}, {"kind": "ask"}, {"thread": "card:card-1-0"},
                                {"since": self.now.isoformat()}):
                    store.events(reader="plx:staff:ana", **filters)
                store._conn.set_trace_callback(None)
                light = [q for q in seen if q.startswith("SELECT e.seq, e.reader, e.people")]
                self.assertEqual(len(light), 4)
                for query in light:
                    plan = " ".join(str(tuple(r)[-1]) for r in
                                    store._conn.execute("EXPLAIN QUERY PLAN " + query))
                    self.assertIn("COVERING INDEX event_light", plan, query)
                asked: list[str] = []
                real = ap.sqlite3.connect

                def connect(*args, **kwargs):
                    conn = real(*args, **kwargs)
                    conn.set_trace_callback(asked.append)
                    return conn
                queue = ap.Queue(Path(tmp) / f"queue-{n}.sqlite")
                queue.work_log_path = lambda: path
                with mock.patch.object(ap.sqlite3, "connect", connect):
                    self.assertTrue(queue.answered_in_workbench(
                        ["ana@plx.example"], {"event_type": "message.received",
                                              "at": ap._iso(self.now - timedelta(hours=1)),
                                              "conversation_id": f"msg-{n}-0"},
                        self.now + timedelta(hours=1)))
                queue.close()
                [lookup] = [q for q in asked if "FROM event" in q]
                plan = " ".join(str(tuple(r)[-1]) for r in
                                store._conn.execute("EXPLAIN QUERY PLAN " + lookup))
                self.assertIn("COVERING INDEX event_awaiting", plan, lookup)
            store.close()

class TheAutopilotReadsTheLog(engine_fixture.Fixture):
    """The autopilot engine reads the owner's own Workbench sends from the log: a message
    the owner already answered from the approvals desk gets no prepared item; one answered
    before it arrived, by someone else, or with no readable log still does."""

    def answered_from_the_desk(self, conversation, at):
        store = wl.WorkLog(self.dir / "work-log", guard=lambda: Guard(), clock=lambda: at)
        reader = type("R", (), {"key": "synth:ops", "display_name": "Ops", "email": OURS,
                                "session": type("S", (), {"identities": frozenset(),
                                                          "subject_oid": None})()})()
        wl.Notes(store, lambda who: OURS).action(
            reader, {"card_id": "card-9", "kind": "reply_in_thread", "status": "sent",
                     "seq": 1}, {"to": [OUTSIDE], "subject": "RE: COA",
                                 "in_reply_to": conversation}, None)
        store.close()

    def test_an_answered_message_gets_no_item_and_the_rest_do(self):
        answered = self.message(60, "Please send the COA for lot 60.")
        earlier = self.message(61, "Please send the COA for lot 61.")
        untouched = self.message(62, "Please send the COA for lot 62.")
        self.answered_from_the_desk("conv-60", engine_fixture.NOW - timedelta(hours=1))
        # a desk reply sent before message 61 arrived does not answer it
        self.answered_from_the_desk("conv-61", datetime(2026, 10, 1, tzinfo=timezone.utc))
        # only the live cycle opts in (``follow``); no other caller reads the log
        summary = self.cycle([answered, earlier, untouched], read_work_log=True)
        self.assertEqual(summary["skipped"].get("customer_request:answered_in_workbench"), 1)
        self.assertEqual(summary["prepared"], {"customer_request": 2})

    def test_an_accepted_item_answers_its_message_not_a_later_one_seen_late(self):
        from operating.tests.test_proactive_threads import mail
        first, later = (mail(self.w, engine_fixture.SENT, sender=OUTSIDE, to=(OURS,),
                             body=f"Please send the COA for lot {n}.", subject="COA",
                             mailbox=engine_fixture.OURS_OID, conversation="conv-70",
                             n=31_000 + n, hour=hour) for n, hour in ((70, 9), (71, 13)))
        order = self.w.obj("SalesOrder", {"order_number": "SO-70", "status": "Open"})
        for made in (first, later):
            self.w.link("Email.sent_by_customer", made, self.customer)
            self.w.link("Email.mentions_order", made, order)
        at = datetime.combine(engine_fixture.SENT, datetime.min.time(), timezone.utc)
        store = wl.WorkLog(self.dir / "work-log", guard=lambda: Guard(),
                           clock=lambda: at + timedelta(hours=14))
        reader = type("R", (), {"key": "synth:ops", "display_name": "Ops", "email": OURS,
                                "session": type("S", (), {"identities": frozenset(),
                                                          "subject_oid": None})()})()
        # accepted at 14:00 answering the 09:00 message; the 13:00 one reached the feed late
        wl.Notes(store, lambda who: OURS).autopilot_decided(reader, {
            "item_id": "ap_x", "title": "COA", "decision": {"decision": "accept", "sent": True},
            "happened": {"conversation_id": "conv-70", "object_id": first,
                         "at": ap._iso(at + timedelta(hours=9))},
            "prepared": {"draft": {"to": [{"address": OUTSIDE}], "subject": "RE: COA"}}})
        store.close()
        summary = self.cycle([first, later], read_work_log=True)
        self.assertEqual(summary["skipped"].get("customer_request:answered_in_workbench"), 1)
        self.assertEqual(summary["prepared"], {"customer_request": 1})
        prepared = [i["body"]["happened"]["object_id"] for i in self.queue().items(None)]
        self.assertEqual(prepared, [later])

    def test_a_log_that_cannot_be_read_prepares_as_before(self):
        msg = self.message(63, "Please send the COA for lot 63.")
        (self.dir / "work-log").mkdir()
        (self.dir / "work-log" / "work-log.sqlite").write_text("not a database")
        self.assertEqual(self.cycle([msg], read_work_log=True)["prepared"],
                         {"customer_request": 1})


class TheReplayNeverReadsTheLog(replay_fixture.World):
    """A replay judges past days with what was known then: neither its day cycles nor the
    catch-up that drains what a cycle deferred ever open the work log, even when
    ``PLX_WORK_LOG_DIR`` names a log holding the owner's answers to those very messages."""

    def test_cardinality_no_replay_cycle_or_catch_up_opens_the_log(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                log_dir = self.dir / "live-work-log"
                store = wl.WorkLog(log_dir, guard=lambda: Guard(),
                                   clock=lambda: replay_fixture.DAY + timedelta(hours=11))
                reader = type("R", (), {"key": "synth:ops", "display_name": "Ops",
                                        "email": OURS, "session": type("S", (), {
                                            "identities": frozenset(), "subject_oid": None})()})()
                for i in range(n):
                    po = self.po(i)
                    self.supplier_mail(i, f"PO P-{900 + i} will ship on Nov 20.", po=po)
                    # the owner answered every one of them from the desk that same day
                    wl.Notes(store, lambda who: OURS).action(
                        reader, {"card_id": f"card-{i}", "kind": "reply_in_thread",
                                 "status": "sent", "seq": 1},
                        {"to": [replay_fixture.REP], "subject": "RE: Shipping",
                         "in_reply_to": f"sup-{i}"}, None)
                store.close()
                self.store()
                registry = json.loads(json.dumps(ap.load_registry()))
                registry["limits"]["max_events_per_cycle"] = 1   # the rest wait for _drain
                opened = []
                real = ap.Queue.work_log_path

                def spy(queue):
                    opened.append(1)
                    return real(queue)

                st = pe._Store(self.dir / "s.sqlite", threads=1)
                try:
                    with mock.patch.dict("os.environ", {ap.WORK_LOG_ENV: str(log_dir)}), \
                            mock.patch.object(ap.Queue, "work_log_path", spy):
                        out = rp.replay(st, replay_fixture.people(), role=f"person:{OURS}",
                                        out=self.dir / "out", since=replay_fixture.DAY,
                                        until=replay_fixture.DAY + timedelta(days=1), roles={},
                                        config=rp.load_config(), registry=registry,
                                        environ={},
                                        end=replay_fixture.DAY + timedelta(days=10),
                                        probe=lambda: 64.0)
                finally:
                    st.close()
                self.assertEqual(out["status"], "complete")
                self.assertEqual(opened, [])
                flow = out["workflows"]["supplier_eta_receiving"]
                self.assertEqual(flow["prepared"], n)       # none suppressed
                days = rp.Progress(next((self.dir / "out").glob("person_*")) /
                                   "progress.sqlite")
                try:
                    summaries = days.summaries()
                finally:
                    days.close()
                drained = sum(sum(v for k, v in (d.get("drained") or {}).items()
                                  if not k.startswith("skipped:")) for d in summaries)
                self.assertEqual(drained, n - 1)     # the catch-up really ran
                self.assertNotIn("answered_in_workbench", json.dumps(summaries))
                # the same log does answer these messages for a live cycle (not vacuous)
                queue = ap.Queue(next((self.dir / "out").glob("person_*")) / ap.QUEUE_FILE)
                try:
                    body = json.loads(queue.conn.execute(
                        "SELECT body FROM events WHERE json_extract(body, '$.event_type') = "
                        "'message.received' LIMIT 1").fetchone()[0])
                    with mock.patch.dict("os.environ", {ap.WORK_LOG_ENV: str(log_dir)}):
                        self.assertTrue(queue.answered_in_workbench(
                            {OURS}, body, replay_fixture.DAY + timedelta(days=1)))
                finally:
                    queue.close()


class UsageReview(unittest.TestCase):
    """Every Ask answer as its reader saw it (sources,
    steps, time, outcome) with its thumbs, failed questions, "Give feedback" notes linked
    to their screen, slow pages and errors; the exclusion fails closed, pay/HR withheld."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.guard = Guard()
        patch = mock.patch.object(fb, "exclusion", lambda: self.guard)
        patch.start()
        self.addCleanup(patch.stop)
        env = mock.patch.dict("os.environ", {"PLX_FEEDBACK_DIR": "", "PLX_WORK_LOG_DIR": ""})
        env.start()
        self.addCleanup(env.stop)
        self.backend = MockBackend()
        self.backend.state_dir = self.state
        self.backend.work_log = wl.WorkLog(self.state / "work-log", guard=lambda: self.guard)
        self.backend.feedback_store = fb.FeedbackStore(self.state / "feedback")
        self.addCleanup(self.backend.feedback_store.close)
        self.ana = client_for(self.backend, "staff:ana")

    def review(self, **kw):
        return ur.review(self.state, since=datetime.now(timezone.utc) - timedelta(hours=1),
                         guard=self.guard, **kw)

    def test_answers_feedback_errors_and_slow_pages_in_one_read_only_digest(self):
        answered = ask(self.ana, "Where is PO 90412?")
        self.ana.post("/api/feedback", json={"answer_id": answered["answer_id"],
                                             "rating": -1, "note": "date is wrong"})
        self.ana.post("/api/feedback/note", json={"kind": "page", "note": "Too slow",
                                                  "page": "/purchasing"})
        from operator_api import models as m
        back = m.AnswerPayload(
            answer_id="a" * 24, question="Is the order late?", outcome="partial",
            direct_answer="Which purchase order?", hidden=m.Hidden(count=0),
            freshness=m.Freshness(), conversation_id="c-back", latency_ms=1,
            clarify=m.Clarify(question="Which purchase order?", options=[
                m.ClarifyOption(label="PO 1"), m.ClarifyOption(label="PO 2")]))
        with mock.patch.object(self.backend, "ask", return_value=iter([("answer", back)])):
            self.ana.post("/api/ask", json={"question": "Is the order late?"})
        with mock.patch.object(self.backend, "ask", side_effect=TimeoutError()):
            self.ana.post("/api/ask", json={"question": "Which lots expire this week?"})
        now = datetime.now(timezone.utc).isoformat()
        latency = self.state / "latency.log"
        latency.write_text("\n".join(json.dumps(r) for r in (
            {"at": now, "method": "GET", "route": "/api/home", "status": 200, "ms": 4200},
            {"at": now, "method": "GET", "route": "/api/home", "status": 200, "ms": 90},
            {"at": now, "method": "GET", "route": "/api/object", "status": 500, "ms": 30},
            {"method": "GET", "route": "/api/old", "status": 500, "ms": 9000})) + "\n")
        before = {p: p.stat().st_mtime_ns for p in self.state.rglob("*.sqlite")}
        found = self.review(latency_log=latency)
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in self.state.rglob("*.sqlite")})
        by_q = {a["question"]: a for a in found["answers"]}
        first = by_q["Where is PO 90412?"]
        self.assertEqual(first["answer"], answered["direct_answer"])
        self.assertEqual(first["outcome"], answered["outcome"])
        self.assertEqual(len(first["sources"]),
                         sum(len(f["sources"]) for f in answered["facts"]))
        self.assertTrue(first["steps"])
        self.assertEqual(first["feedback"][0]["rating"], -1)
        self.assertIn(first, found["flagged"])                # thumbs down
        # a question asked back is the person's turn, never a low answer
        asked_back = by_q["Is the order late?"]
        self.assertEqual(asked_back["outcome"], "asked_back")
        self.assertNotIn(asked_back, found["flagged"])
        failed = by_q["Which lots expire this week?"]
        self.assertEqual((failed["outcome"], failed["error"]), ("error", "busy"))
        note = next(f for f in found["feedback"] if f["kind"] == "page")
        self.assertEqual((note["page"], note["note"]), ("/purchasing", "Too slow"))
        self.assertEqual(found["latency"]["slow"], [{"route": "/api/home", "count": 1,
                                                     "max_ms": 4200.0}])
        self.assertEqual(found["latency"]["errors"], [{"route": "/api/object", "count": 1}])
        text = "\n".join(ur.digest(found))
        for words in ("Where is PO 90412?", "thumbs down", "error busy", "/api/home x1",
                      "Too slow"):
            self.assertIn(words, text)
        # the failed question's own thread reopens it in Ask, without the review's data
        thread = self.ana.get("/api/work-log", params={"kind": "ask"}).json()["events"]
        self.assertIn("failed", {e["verb"] for e in thread})

    def test_fails_closed_on_the_exclusion_and_withholds_pay_and_hr_text(self):
        ask(self.ana, "Is SIN 130 692 544 on file for the new hire?")
        self.guard = Guard(unavailable=True)
        self.assertEqual(self.review(), {"withheld": "people exclusion not loaded"})
        with mock.patch("sys.stdout"):
            self.assertEqual(ur.main(["--state-dir", str(self.state)]), 3)
        self.guard = Guard()
        texts = json.dumps(self.review()["answers"])
        self.assertNotIn("130 692 544", texts)
        self.assertIn(ur.WITHHELD_PAY_HR, texts)
        # a person excluded after their rows were written: their rows are left out
        later = "99999999-2222-4333-8444-555555555555"
        self.backend.work_log.record(
            type("R", (), {"key": f"plx:staff:{later}", "display_name": "X", "email": None,
                           "session": type("S", (), {"identities": frozenset(),
                                                     "subject_oid": None})()})(),
            kind="note", verb="noted", title="x", thread="record:note/general",
            source="feedback")
        self.assertEqual(self.review()["people"].get("X"), {"note": 1})
        self.guard = Guard(also=[later])
        self.assertEqual(self.review()["people"].get("X"), None)


if __name__ == "__main__":
    unittest.main()
