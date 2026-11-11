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


class Guard:
    """A people exclusion naming one object id and one address (``unavailable`` set: the
    list is not loaded)."""
    unavailable = None

    def is_excluded(self, value):
        return str(value).lower() in (OID, EXCLUDED)

    def decides(self, address):
        return address.endswith("@plx.example")

    def known_domains(self):
        return {"plx.example"}


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
        self.guard.unavailable = "excluded_people_oids_absent"
        ask(self.ana, "which orders are late")          # not written
        page = self.log(self.ana)
        self.assertEqual((page["total"], page["events"]), (None, []))
        answer = ask(self.ana, "what did I ask today")
        self.assertEqual(answer["outcome"], "refused")
        self.guard.unavailable = None
        self.assertEqual(self.log(self.ana)["total"], 1)
        # an excluded person's own work is never written
        excluded = client_for(self.backend, f"staff:{OID}")
        ask(excluded, "where is PO 90412, is it late")
        self.assertEqual(self.log(self.owner, everyone=True)["total"], 1)

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

    def test_waiting_then_answered_by_the_autopilots_inbound_tracking(self):
        self.send(["buyer@vendor.example"])
        self.assertEqual(self.loop()["state"], "waiting")
        queue = ap.Queue(self.queue_path)
        queue.note_inbound([{"mailbox": "ana@plx.example", "conversation_id": "other",
                             "sender": "buyer@vendor.example", "at": "2026-11-11T09:00:00Z",
                             "event_type": "message.received"}])
        queue.conn.commit()
        queue.close()
        self.assertEqual(self.loop()["state"], "answered")
        self.assertEqual(self.loop()["answered_at"], "2026-11-11T09:00:00Z")

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
        self.guard.is_excluded = lambda value: str(value).lower() in (
            OID, EXCLUDED, "colleague@plx.example")
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
        return type("R", (), {"key": f"plx:staff:{who}", "display_name": who.title(),
                              "email": f"{who}@plx.example",
                              "session": type("S", (), {"identities": frozenset(),
                                                        "subject_oid": None})()})()

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
                         [("action", "messaged"), ("write", "executed")])  # logged, as work
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
        self.guard.is_excluded = lambda value: str(value).lower() in (
            OID, EXCLUDED, "colleague@plx.example")
        after = wl.time_saved(self.store, reader="plx:staff:ana")["people"][0]
        self.assertEqual(before["weeks"][-1]["minutes"] - after["weeks"][-1]["minutes"], 6)
        self.guard.unavailable = "not loaded"
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
        self.guard.unavailable = "not loaded"
        closed = ana.get("/api/time-saved").json()
        self.assertEqual((closed["available"], closed["people"]), (False, []))


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
        self.guard.unavailable = "not loaded"
        self.assertEqual(self.review(), {"withheld": "people exclusion not loaded"})
        with mock.patch("sys.stdout"):
            self.assertEqual(ur.main(["--state-dir", str(self.state)]), 3)
        self.guard.unavailable = None
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
        self.guard.is_excluded = lambda v: str(v).lower() in (OID, EXCLUDED, later)
        self.assertEqual(self.review()["people"].get("X"), None)


if __name__ == "__main__":
    unittest.main()
