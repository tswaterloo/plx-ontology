"""Teams chats, group chats and sender-answered replies in the per-role replay (split from
test_autopilot_replay for its size cap).

SYNTHETIC fixtures only; evidence label: isolated-fixture.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from operating import autopilot as ap
from operating import autopilot_engine
from operating import autopilot_events
from operating import autopilot_queue
from operating import autopilot_reading
from operating import autopilot_replay as rp
from operating import autopilot_roster as roster
from operating import proactive_engine as pe
from operating import workflows as wf
from operating.tests.test_autopilot import (NOW, OTHER, READER, SENT, Fixture, everyone,
                                            opens_all)
from operating.tests.test_proactive_engine import EXCLUDED_OID, OURS_OID
from operating.tests.test_proactive_threads import (APPROVER, APPROVER_OID, BOSS, OURS, OUTSIDE,
                                                    mail, people)
from operating.tests.test_autopilot_replay import DAY, REP, SHIPPED_REGISTRY, StubAsks, World, at, Replay


class TeamsChats(World):
    """One-to-one Teams chats feed the autopilot by their time (no event index), and the
    person's own chat messages are their actions. These tests are about which messages a
    chat owes and how they score."""

    def chat(self, chat: str, sender: str, hour: int, text: str, day: int = 0) -> str:
        # the capture's shape: {"display_name", "id"}
        return self.w.obj("TeamsChatMessage", {"created_at": at(hour, day), "from": json.dumps(
                              {"display_name": "Synth person", "id": sender}), "subject": text},
                          audience=f"teams:chat:{chat}", binding="graph_teams.messages")

    def test_cardinality_a_chat_waiting_on_the_owner_is_prepared_and_their_answer_scores(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                self.chat("c1", OURS_OID, 9, "Morning, can you check the run sheet?", day=-1)
                for i in range(n):
                    self.chat("c1", APPROVER_OID, 10, f"Did batch {i} pass the check?")
                self.chat("c1", OURS_OID, 15, "Yes, all passed.")
                # the chat holding the excluded person is never read
                self.chat("c3", OURS_OID, 9, "Hi", day=-1)
                self.chat("c3", EXCLUDED_OID, 10, "Can you call me?")
                self.chat("c1", APPROVER_OID, 16, "Thanks. And the next one?")
                self.store()
                out = Replay.replay(self, role=f"person:{OURS}")
                # a burst before the owner answers is one item; a message after it, another
                self.assertEqual(out["prepared_by_autopilot"], {"teams_reply": 2})
                self.assertEqual(out["skipped"].get("teams_reply:thread_item_open", 0), n - 1)
                judged = sum(v["judged"] for v in out["workflows"].values())
                matched = sum(v["matched"] for v in out["workflows"].values())
                self.assertEqual((judged, matched), (2, 1))
                self.assertEqual(sum(v["actions"] for v in out["workflows"].values()), 1)
                self.assertNotIn("c3", (self.dir / "out" / autopilot_engine.GATE_FILE).read_text())

    def _items(self):
        q = ap.Queue(self.dir / "out" / f"person_{OURS.split('@')[0]}_synth_tenant_"
                     "example" / ap.QUEUE_FILE)
        self.addCleanup(q.close)
        return q, q.items(None, ("open", "done", "settled", "dismissed"))

    def test_a_teams_item_names_its_sender_and_quotes_the_message(self):
        """The live defect (11-16): every Teams reply item read "Someone is waiting on a
        reply: (Teams message)". The item names the sender as the message states
        (``from.display_name``) and quotes the start of its text; a message with no text
        says so; the stored sender stays the id (the exclusion's input) and no id is shown."""
        self.chat("c1", OURS_OID, 9, "Morning, can you check the run sheet?", day=-1)
        self.chat("c1", APPROVER_OID, 10, "Did batch 7 pass the check? password: Zx9!long-secret")
        self.chat("c2", OURS_OID, 9, "Hi", day=-1)
        self.w.obj("TeamsChatMessage", {"created_at": at(11), "from": json.dumps(
            {"display_name": "", "id": APPROVER_OID})}, audience="teams:chat:c2",
            binding="graph_teams.messages")
        st = self.store()
        Replay.replay(self, role=f"person:{OURS}")
        q, items = self._items()
        by_chat = {i["audiences"][0]: i for i in items if i["autopilot"] == "teams_reply"}
        named = by_chat["teams:chat:c1"]
        happened = named["body"]["happened"]
        self.assertEqual(happened["sender"], APPROVER_OID.lower())
        self.assertEqual(happened["sender_name"], "Synth person")
        self.assertTrue(happened["snippet"].startswith("Did batch 7 pass the check?"))
        self.assertNotIn("Zx9!long-secret", json.dumps(named))
        self.assertIn("Synth person is waiting on a reply: \u201cDid batch 7 pass the check?",
                      named["title"])
        shown = autopilot_engine.shown_item(named)
        self.assertEqual(shown["body"]["happened"]["sender"], "Synth person")
        self.assertEqual(shown["body"]["happened"]["message"], happened["snippet"])
        self.assertNotIn(APPROVER_OID.lower(), json.dumps(shown["body"]["happened"]["sender"]))
        # no name stated: their address from the directory; no text: not read yet, then
        # (the next cycle read the served store) the reason
        bare = autopilot_engine.shown_item(by_chat["teams:chat:c2"])
        self.assertEqual(bare["body"]["happened"]["sender"], APPROVER)
        self.assertEqual(bare["body"]["happened"]["message"], autopilot_engine.NOT_READ)
        self.assertIn("(Teams message with no text)", bare["title"])
        self.assertNotIn(APPROVER_OID.lower(), bare["title"].lower())
        with q.conn:
            q.conn.execute("UPDATE items SET state = 'open'")
        self.assertGreaterEqual(ap.name_teams_items(st, q, {}), 1)
        again = {i["audiences"][0]: i for i in q.items(None) if i["autopilot"] == "teams_reply"}
        self.assertEqual(again["teams:chat:c1"]["body"], named["body"])   # done once
        bare = autopilot_engine.shown_item(again["teams:chat:c2"])
        self.assertEqual(bare["body"]["happened"]["message"], autopilot_engine.NO_TEXT)
        self.assertNotIn(APPROVER_OID.lower(), bare["title"].lower())

    def test_cardinality_items_prepared_before_the_names_are_named_once(self):
        """Open Teams items stored before the item carried the name and text (the live
        queue's) are named from the stored message and their event on the next cycle, each
        once, none lost or doubled, a reply's title written again; a sender with no stated
        name and no address still reads "Someone", never the id."""
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                msgs = [self.chat(f"c{i}", APPROVER_OID, 10, f"Is order {i} ready?")
                        for i in range(n)]
                anon = self.w.obj("TeamsChatMessage", {"created_at": at(10), "from": json.dumps(
                    {"id": EXCLUDED_OID})}, audience="teams:chat:cx",
                    binding="graph_teams.messages")
                st = self.store()
                q = ap.Queue(self.dir / "q" / ap.QUEUE_FILE)
                self.addCleanup(q.close)
                old = "Someone is waiting on a reply: (Teams message)"
                with q.conn:
                    for k, oid in enumerate([*msgs, anon]):
                        sender = (APPROVER_OID if oid != anon else EXCLUDED_OID).lower()
                        event = {"event_id": f"e{k}", "event_type": "message.received",
                                 "object_id": oid, "object_type": "TeamsChatMessage",
                                 "channel": "teams", "sender": sender, "at": at(10),
                                 "text": "" if oid == anon else f"Is order {k} ready?"}
                        q.add_event(event, at(10))
                        q.add_item({
                            "item_id": f"i{k}", "event_id": f"e{k}", "autopilot": "teams_reply",
                            "owner": OURS_OID, "title": "Someone is waiting on your reply in "
                            "Teams: " + old, "body": {
                                "label": "Someone is waiting on your reply in Teams",
                                "happened": {k2: event[k2] for k2 in (
                                    "event_type", "object_id", "object_type", "channel",
                                    "sender", "at")},
                                "prepared": {"kind": "reply", "asked": [], "summary": old}},
                            "audiences": ["teams:chat:c"], "markings": [], "citations": [],
                            "due_at": at(14), "created_at": at(10)})
                # a malformed stored row (item body or its event) is skipped and counted,
                # never failing the cycle or the other items
                with q.conn:
                    q.conn.execute("INSERT INTO events (event_id, event_type, object_id, "
                                   "object_type, seen_at, body) VALUES ('ebad', 'message.received', "
                                   "'x', 'TeamsChatMessage', ?, '{not json')", (at(10),))
                    for bad_id, bad_event, bad_body in (("ibad1", "e0", "{not json"),
                                                        ("ibad2", "ebad", json.dumps({
                                                            "happened": {
                                                                "channel": "teams",
                                                                "event_type":
                                                                "message.received"}}))):
                        q.conn.execute(
                            "INSERT INTO items (item_id, event_id, autopilot, owner, title, "
                            "body, audiences, markings, citations, due_at, created_at, state) "
                            "VALUES (?, ?, 'teams_reply', ?, 't', ?, '[]', '[]', '[]', ?, ?, "
                            "'open')", (bad_id, bad_event, OURS_OID, bad_body, at(14), at(10)))
                from collections import Counter
                skipped = Counter()
                self.assertEqual(ap.name_teams_items(st, q, {}, skipped=skipped), n + 1)
                self.assertEqual(skipped["teams_item_unreadable"], 2)
                self.assertEqual(ap.name_teams_items(st, q, {}), 0)      # once
                with q.conn:
                    q.conn.execute("DELETE FROM items WHERE item_id LIKE 'ibad%'")
                after = {i["item_id"]: i for i in q.items(None)}
                self.assertEqual(sorted(after), sorted(f"i{k}" for k in range(n + 1)))
                for k in range(n):
                    item = after[f"i{k}"]
                    self.assertEqual(item["body"]["happened"]["sender_name"], "Synth person")
                    self.assertEqual(item["title"], "Someone is waiting on your reply in "
                                     f"Teams: Synth person is waiting on a reply: \u201cIs "
                                     f"order {k} ready?\u201d")
                bare = autopilot_engine.shown_item(after[f"i{n}"])
                self.assertEqual(bare["body"]["happened"]["sender"], "Someone")
                self.assertEqual(bare["body"]["happened"]["message"], autopilot_engine.NO_TEXT)
                self.assertNotIn(EXCLUDED_OID.lower(), json.dumps(bare).lower().replace(
                    json.dumps(bare["body"]["happened"]["object_id"]).lower(), ""))

    def test_old_workflow_labels_are_retitled_on_open_items_only(self):
        """The registry no longer says "Someone is waiting on your reply": an open item
        whose title starts with the old label gets the new one (title and body label), the
        rest of the title kept; done again it changes nothing; a closed item, an item of
        another title and the batch bound are respected."""
        registry = ap.load_registry()
        labels = {a["id"]: a["label"] for a in registry["autopilots"]}
        self.assertEqual(labels["teams_reply"], "Reply waiting in Teams")
        for a in registry["autopilots"]:
            self.assertNotIn("Someone", a["label"])
        q = ap.Queue(self.dir / "q" / ap.QUEUE_FILE)
        self.addCleanup(q.close)
        old = "Someone is waiting on your reply in Teams"
        rows = [("o1", "open", old + ": Ana Ruiz is waiting on a reply: x"),
                ("o2", "open", old + ": Bo is waiting on a reply: y"),
                ("c1", "done", old + ": Cy is waiting on a reply: z"),
                ("n1", "open", "Reply waiting in Teams: Di is waiting on a reply: w"),
                ("n2", "open", old + " and more")]
        with q.conn:
            for k, (iid, state, title) in enumerate(rows):
                q.add_event({"event_id": f"e{k}", "event_type": "message.received",
                             "object_id": f"o{k}", "object_type": "TeamsChatMessage"}, at(10))
                q.add_item({"item_id": iid, "event_id": f"e{k}", "autopilot": "teams_reply",
                            "owner": OURS_OID, "title": title, "body": {"label": old},
                            "audiences": [], "markings": [], "citations": [],
                            "due_at": at(14), "created_at": at(10)})
                q.conn.execute("UPDATE items SET state = ? WHERE item_id = ?", (state, iid))
        self.assertEqual(ap.retitle_old_labels(q, registry, limit=1), 1)
        self.assertEqual(ap.retitle_old_labels(q, registry), 1)
        self.assertEqual(ap.retitle_old_labels(q, registry), 0)      # idempotent
        got = {i["item_id"]: i for i in q.items(None, ("open", "done"))}
        self.assertEqual(got["o1"]["title"],
                         "Reply waiting in Teams: Ana Ruiz is waiting on a reply: x")
        self.assertEqual(got["o2"]["title"],
                         "Reply waiting in Teams: Bo is waiting on a reply: y")
        self.assertEqual(got["o1"]["body"]["label"], "Reply waiting in Teams")
        self.assertTrue(got["c1"]["title"].startswith(old))          # closed untouched
        self.assertEqual(got["c1"]["body"]["label"], old)
        self.assertEqual(got["n2"]["title"], old + " and more")

    def test_cardinality_a_reply_sent_as_several_messages_is_anticipated_whole(self):
        """One answer typed as n messages, each six minutes after the one before, is
        what the prepared reply replaces: all n are anticipated, the item matches once.
        A message 20 minutes after the last one, or in another chat, is not."""
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                self.chat("c1", OURS_OID, 9, "Morning, can you check the run sheet?", day=-1)
                self.chat("c2", OURS_OID, 9, "Hi", day=-1)
                self.chat("c1", APPROVER_OID, 10, "Did the batch pass the check?")
                for i in range(n):
                    self.chat("c1", OURS_OID, 12 + i * 6 / 60, f"Part {i} of the answer.")
                last = 12 + (n - 1) * 6 / 60
                self.chat("c2", OURS_OID, last + 5 / 60, "Unrelated note.")
                self.chat("c1", OURS_OID, last + 29 / 60, "One more thing, later.")
                self.store()
                out = Replay.replay(self, role=f"person:{OURS}")
                flow = out["workflows"]["teams_chat"]
                self.assertEqual((flow["judged"], flow["matched"]), (1, 1))
                self.assertEqual((flow["actions"], flow["anticipated"]), (n + 2, n))

    def test_a_message_after_the_other_side_wrote_is_not_part_of_the_reply(self):
        """Reproduced on Vince's DEV days: the owner answers in two messages, the other
        person writes ("Thanks!", owed nothing), and the owner writes again three minutes
        later. That is a new exchange, not the rest of the reply: only the two messages
        of the answer are anticipated."""
        self.chat("c1", OURS_OID, 9, "Morning, can you check the run sheet?", day=-1)
        self.chat("c1", APPROVER_OID, 10, "Did the batch pass the check?")
        self.chat("c1", OURS_OID, 12, "Yes, it passed.")
        self.chat("c1", OURS_OID, 12 + 3 / 60, "All three lots.")
        self.chat("c1", APPROVER_OID, 12 + 5 / 60, "Thanks!")
        self.chat("c1", OURS_OID, 12 + 8 / 60, "Also the label run starts at two.")
        self.store()
        out = Replay.replay(self, role=f"person:{OURS}")
        flow = out["workflows"]["teams_chat"]
        self.assertEqual((flow["judged"], flow["matched"]), (1, 1))
        self.assertEqual((flow["actions"], flow["anticipated"]), (3, 2))

    def test_an_action_without_the_other_side_s_time_gets_no_burst_credit(self):
        """An action list read without ``other_at`` (an older cache) or with an
        unreadable one credits the matched message only."""
        t0 = rp.ap._instant("2026-10-21T12:00:00Z")
        hit = {"kind": "teams_sent", "conversation": "c1", "at": "2026-10-21T12:00:00Z",
               "other_at": None}
        for extra in ({}, {"other_at": "not a time"}, {"other_at": None}):
            with self.subTest(extra=extra):
                nxt = {"kind": "teams_sent", "conversation": "c1",
                       "at": "2026-10-21T12:03:00Z", **extra}
                seen: set = set()
                rp._credit_burst(hit, [(t0, hit), (t0 + timedelta(minutes=3), nxt)], seen)
                self.assertEqual(id(nxt) in seen, extra == {"other_at": None})

    def texts(self, bodies: dict) -> None:
        """A message's body as the capture's text index holds it (not a property)."""
        import sqlite3, zlib
        conn = sqlite3.connect(self.dir / "s.sqlite")
        with conn:
            for oid, body in bodies.items():
                handle = conn.execute("SELECT l1_handle FROM object_versions WHERE "
                                      "object_id = ?", (oid,)).fetchone()[0]
                cur = conn.execute(
                    "INSERT INTO text_fields (object_id, version_handle, citation, field_path,"
                    " document_kind, sender_class, markings, text_digest) VALUES "
                    "(?, ?, ?, 'row.body_text', 'teams_message', 'unknown', '[]', 'd')",
                    (oid, handle, handle))
                conn.execute("INSERT INTO text_chunks (field_id, start, body) VALUES "
                             "(?, 0, ?)", (cur.lastrowid, zlib.compress(body.encode())))
        conn.close()

    def test_cardinality_a_chat_note_closing_its_loop_after_a_casual_lead_in_is_not_owed(self):
        """Reproduced on dev days: "lol okay thanks", "Sure will do!", "Sorry bout that" in
        a chat were prepared as owed and went unanswered. A chat message that asks or
        answers is still prepared."""
        notes = ("lol okay thanks", "Sure will do!", "Sorry bout that", "yep will do")
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                texts = {}
                for i in range(n):
                    chat = f"c{i}"
                    self.chat(chat, OURS_OID, 9, "", day=-1)
                    texts[self.chat(chat, APPROVER_OID, 10, "")] = notes[i % len(notes)]
                self.chat("q", OURS_OID, 9, "", day=-1)
                texts[self.chat("q", APPROVER_OID, 10, "")] = "sure, can you send it?"
                self.store()
                self.texts(texts)
                from integrations.people_exclusion import PeopleExclusion
                from operating.chunking.permissions import Exclusion
                guard = Exclusion(PeopleExclusion([EXCLUDED_OID]),
                                  directory={OURS: OURS_OID, APPROVER: APPROVER_OID},
                                  internal_domains=["synth-tenant.example"],
                                  chat_members={c: [OURS_OID, APPROVER_OID] for c in
                                                [f"c{i}" for i in range(n)] + ["q"]})
                out = Replay.replay(self, role=f"person:{OURS}", guard=guard)
                self.assertEqual(out["status"], "complete")
                self.assertEqual(out["skipped"].get("teams_not_chat_or_not_cleared", 0), 0)
                self.assertEqual(out["prepared_by_autopilot"], {"teams_reply": 1})

    NOTES = ("Yeah send a note too just so people are aware", "haha", "no worries",
             "Hi all, the truck is at the dock now", "FYI the dock closes at 3 today",
             "How are we doing buddy", "Is this you or your bot lol",
             "Let me know how it goes", "will work on that next week", "\U0001F44D\U0001F44D",
             "Did it on the weekend", "can call you back after")
    # Real asks with no "?" a phrase list dropped
    ASKS = ("need the COA by Friday", "can't find the invoice", "pls send invoice copy",
            "lemme know the eta", "Update me on the PO", "we are missing 2 drums on the order",
            "Hi Dana, the label is incorrect on batch 7", "Is the pallet getting picked up today",
            "Need you to reassign this one", "You free?", "Call me when you have a sec")

    def asks_world(self, n: int, extra=(), barred=(), more=(), asks=None):
        """``n`` chats whose last message asks nothing, one chat per ask (and per ``extra``
        text), each a chat the owner wrote in the day before; a chat per ``barred`` text
        holding the excluded person; ``more``: chat ids the caller writes in (members: the
        owner and the approver). The texts by message id and the exclusion."""
        texts, chats = {}, list(more)
        members = {}
        for k, text in enumerate(barred):
            self.chat(f"x{k}", OURS_OID, 9, "", day=-1)
            texts[self.chat(f"x{k}", EXCLUDED_OID, 10, "")] = text
            members[f"x{k}"] = [OURS_OID, EXCLUDED_OID]
        for i in range(n):
            chats.append(f"n{i}")
            self.chat(f"n{i}", OURS_OID, 9, "", day=-1)
            texts[self.chat(f"n{i}", APPROVER_OID, 10, "")] = self.NOTES[i % len(self.NOTES)]
        for k, ask in enumerate((*(self.ASKS if asks is None else asks), *extra)):
            chats.append(f"a{k}")
            self.chat(f"a{k}", OURS_OID, 9, "", day=-1)
            texts[self.chat(f"a{k}", APPROVER_OID, 10, "")] = ask
        self.store()
        self.texts(texts)
        from integrations.people_exclusion import PeopleExclusion
        from operating.chunking.permissions import Exclusion
        return texts, Exclusion(PeopleExclusion([EXCLUDED_OID]),
                                directory={OURS: OURS_OID, APPROVER: APPROVER_OID,
                                           BOSS: EXCLUDED_OID},
                                internal_domains=["synth-tenant.example"],
                                chat_members={**{c: [OURS_OID, APPROVER_OID] for c in chats},
                                              **members})

    def test_cardinality_the_model_reads_which_chat_messages_ask_and_only_asks_are_owed(self):
        """Owner walk 11-17: items for "send a note too so people are aware", a joke, a
        pleasantry were never used; the regex filter after it dropped real asks with no "?".
        The cheap certain rules (no words, a note to all) decide
        without a call; every other message the small model reads, batched, and only an
        ask is prepared. Every ask survives at N=1/10/60 notes; calls stay batched."""
        from operating import chat_ask_model as cam
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                _texts, guard = self.asks_world(n)
                model = StubAsks(yes=self.ASKS)
                judge = cam.Judge(model, batch=25)
                out = Replay.replay(self, role=f"person:{OURS}", guard=guard, ask_judge=judge)
                self.assertEqual(out["status"], "complete")
                self.assertEqual(out["prepared_by_autopilot"], {"teams_reply": len(self.ASKS)})
                sent = [t for b in model.batches for t in b]
                # no words and the note to all are decided in code: never sent
                self.assertNotIn("\U0001F44D\U0001F44D", sent)
                self.assertNotIn("Hi all, the truck is at the dock now", sent)
                self.assertEqual(len(sent), len(set(sent)) if n <= len(self.NOTES) else
                                 len(sent))    # each message read once
                self.assertTrue(all(len(b) <= 25 for b in model.batches))
                self.assertLessEqual(len(model.batches), -(-len(sent) // 25) + 1)
                # the reader is the owner, by first name, never an address
                self.assertTrue(all(m["reader"] == "Ops" for b in model.sent for m in b))
                self.assertFalse(any("@" in json.dumps(m) for b in model.sent for m in b))

    def test_cardinality_open_reply_items_made_unread_are_read_once_and_junk_closed(self):
        """Live 11-21: 6 of the 10 open Teams reply items were closers or a bare address,
        made before their message had a reading. A cycle with the model reads each open
        item's message once (``recheck_open_replies``): one read as fyi is closed
        (``triaged_fyi``), a real ask stays, nothing is read twice; a failed reading keeps
        every item. N=1/10/60."""
        from operating import chat_ask_model as cam
        from operating.tests.test_autopilot_triage import StubTriage
        registry = ap.load_registry()

        def recheck(classify):
            q, _items = self._items()
            return q, ap.recheck_open_replies(q, registry, cam.Judge(classify, batch=25),
                                              guard, {}, "2026-11-21T12:00:00Z")
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                _texts, guard = self.asks_world(n)
                first = Replay.replay(self, role=f"person:{OURS}", guard=guard)
                made = first["prepared_by_autopilot"].get("teams_reply", 0)
                notes = made - len(self.ASKS)
                self.assertGreater(notes, 0)
                # a mixed queue: one note's item the person already answered stays as it was
                q, _items = self._items()
                answered = next(i for i in q.items(None, ("open",))
                                if i["autopilot"] == "teams_reply" and not any(
                                    a in json.dumps(i) for a in self.ASKS))
                with q.conn:
                    q.settle_item(answered["item_id"], "done", "owner_replied",
                                  "2026-11-21T11:00:00Z")
                model = StubTriage({a: ("reply", "") for a in self.ASKS})
                q, got = recheck(model)
                self.assertEqual(got, {"read": made - 1, "closed": notes - 1})
                kept = q.item(answered["item_id"])
                self.assertEqual((kept["state"], kept["done_reason"]), ("done", "owner_replied"))
                items = q.items(None, ("open", "done", "settled", "dismissed"))
                reasons = Counter(i["done_reason"] for i in items
                                  if i["autopilot"] == "teams_reply")
                self.assertEqual(reasons["triaged_fyi"], notes - 1)
                self.assertEqual(sum(1 for i in items if i["autopilot"] == "teams_reply"
                                     and i["state"] == "open"), len(self.ASKS))
                again = StubTriage({})
                self.assertEqual(recheck(again)[1], {"read": 0, "closed": 0})
                self.assertEqual(again.batches, [])     # read already: no second call
        self.setUp()
        _texts, guard = self.asks_world(3)
        Replay.replay(self, role=f"person:{OURS}", guard=guard)
        for fail in ("raise", "none"):
            _q, got = recheck(StubAsks(fail=fail))
            self.assertEqual(got["closed"], 0)
        _q, items = self._items()
        self.assertFalse([i for i in items if str(i["done_reason"]).startswith("triaged")])

    def test_any_model_failure_keeps_every_message_and_nothing_is_cached(self):
        """A timeout, an error, a reply that does not parse or skips a message: the
        message is kept (shown), never dropped, and asked again on a later cycle."""
        from operating import chat_ask_model as cam
        for fail in ("raise", "none", "short"):
            with self.subTest(fail=fail):
                self.setUp()
                _t, guard = self.asks_world(3)
                model = StubAsks(yes=(), fail=fail)        # read: no ask anywhere
                out = Replay.replay(self, role=f"person:{OURS}", guard=guard,
                                    ask_judge=cam.Judge(model))
                sent = [t for batch in model.batches for t in batch]
                self.assertGreater(len(sent), len(self.ASKS))
                kept = out["prepared_by_autopilot"].get("teams_reply", 0)
                q, _items = self._items()
                cached = q.conn.execute("SELECT count(*) FROM chat_asks").fetchone()[0]
                if fail == "short":         # the one verdict missing: that message kept
                    self.assertEqual((kept, cached), (len(model.batches), len(sent) - kept))
                else:
                    self.assertEqual((kept, cached), (len(sent), 0))

    def test_excluded_and_pay_messages_never_reach_the_model(self):
        """The people exclusion bars a chat, and
        pay or HR matter is never read: none of them is in any batch the model is sent."""
        from operating import chat_ask_model as cam
        extra = ("can you check the gross pay, net pay and YTD on my pay stub")
        _texts, guard = self.asks_world(2, extra=extra,
                                        barred=("need the signed form from you today",))
        model = StubAsks(yes=self.ASKS)
        Replay.replay(self, role=f"person:{OURS}", guard=guard, ask_judge=cam.Judge(model))
        sent = [t for b in model.batches for t in b]
        self.assertTrue(sent)
        for text in (*extra, "need the signed form from you today"):
            self.assertNotIn(text, sent)

    def test_a_cached_verdict_is_read_once_per_message(self):
        """The model reads a message once: the same message judged again (a later cycle,
        the text-read pass) gets the queue's cached verdict, no call."""
        from operating import chat_ask_model as cam
        _texts, guard = self.asks_world(4)
        first = StubAsks(yes=self.ASKS)
        Replay.replay(self, role=f"person:{OURS}", guard=guard, ask_judge=cam.Judge(first))
        sent = [t for batch in first.batches for t in batch]
        q, _items = self._items()
        cached = q.conn.execute("SELECT count(*), sum(asks) FROM chat_asks").fetchone()
        self.assertEqual(tuple(cached), (len(sent), len(self.ASKS)))
        events = [json.loads(b) for (b,) in q.conn.execute(
            "SELECT body FROM events WHERE body LIKE '%\"ask_check\": \"model\"%'")]
        self.assertEqual(len(events), len(sent))
        again = StubAsks(yes=())
        judge = cam.Judge(again)
        got = judge.ask(q, [(e["event_id"], e, e["mailbox"], "Ops") for e in events],
                        guard, "2026-10-21T12:00:00Z")
        self.assertEqual(again.batches, [])
        self.assertEqual(judge.stats["cached"], len(events))
        self.assertEqual(sum(got.values()), len(self.ASKS))

    def test_an_item_prepared_before_its_text_was_read_closes_when_it_asks_nothing(self):
        """A Teams message typed before the store's text index held its body has no text:
        its ask is unknown, so it is prepared (never lost); the cycle that reads the text
        closes the item when the model reads that it asks nothing (done, "no_ask") and keeps
        one that asks; a model failure keeps it. An autopilot whose rule needs no ask is
        left as it was."""
        from operating import chat_ask_model as cam
        self.chat("c1", OURS_OID, 9, "Morning, can you check the run sheet?", day=-1)
        self.chat("c2", OURS_OID, 9, "Morning, can you check the run sheet?", day=-1)
        self.chat("c4", OURS_OID, 9, "Morning, can you check the run sheet?", day=-1)
        noted = self.chat("c1", APPROVER_OID, 10, "")
        asked = self.chat("c2", APPROVER_OID, 10, "")
        unknown = self.chat("c4", APPROVER_OID, 10, "")
        _texts, guard = self.asks_world(0, more=("c1", "c2", "c4"), asks=())
        st = pe._Store(self.dir / "s.sqlite", threads=1)
        self.addCleanup(st.close)
        Replay.replay(self, role=f"person:{OURS}", guard=guard)
        q, items = self._items()
        by_chat = {i["audiences"][0]: i for i in items if i["autopilot"] == "teams_reply"}
        self.assertEqual(set(by_chat), {"teams:chat:c1", "teams:chat:c2", "teams:chat:c4"})
        self.texts({noted: "Yeah send a note too just so people are aware",
                    asked: "need the COA by Friday", unknown: "the label on batch 7"})
        with q.conn:
            q.conn.execute("UPDATE items SET state = 'open', done_reason = NULL")
        stamp = "2026-10-21T12:00:00Z"
        model = StubAsks(yes=("need the COA by Friday",),
                         no=("Yeah send a note too just so people are aware",))
        # a rule that needs no ask: named, never closed, nothing read by the model
        self.assertEqual(ap.name_teams_items(st, q, {}, ask_rules=(), now=stamp,
                                             judge=cam.Judge(model), people=guard), 3)
        self.assertEqual({i["state"] for i in q.items(None, ("open", "done"))}, {"open"})
        self.assertEqual(model.batches, [])
        with q.conn:
            q.conn.execute("UPDATE items SET body = json_remove(body, '$.happened.snippet', "
                           "'$.happened.snippet_missing')")
        skipped = Counter()
        self.assertEqual(ap.name_teams_items(st, q, {}, ask_rules={"teams_reply"}, now=stamp,
                                             skipped=skipped, judge=cam.Judge(model),
                                             people=guard), 3)
        got = {i["audiences"][0]: (i["state"], i["done_reason"])
               for i in q.items(None, ("open", "done"))}
        self.assertEqual(got, {"teams:chat:c1": ("done", "no_ask"),
                               "teams:chat:c2": ("open", None),
                               "teams:chat:c4": ("open", None)})      # not known: kept
        self.assertEqual(skipped["teams_item_no_ask"], 1)

class GroupChats(TeamsChats):
    """A group chat's message is addressed only to each member it @mentions; the other
    members get it as not addressed to them, which only a turn rule answers."""

    THIRD = "00000000-0000-4000-8000-0000000000a3"

    def msg(self, chat: str, sender: str, hour: int, text: str, mention: str | None = None,
            day: int = 0) -> str:
        oid = self.chat(chat, sender, hour, text, day=day)
        if mention:
            self.w.rows["object_values"].append({
                **self.w.rows["object_values"][-1], "property": "mentions",
                "value": json.dumps([{"id": 0, "target_id": mention, "kind": "user",
                                      "display_name": "Synth", "text": "Synth"}])})
        return oid

    def unaddressed(self) -> int:
        q = ap.Queue(next((self.dir / "out").glob("person_*")) / ap.QUEUE_FILE)
        try:
            return q.conn.execute("SELECT COUNT(*) FROM events WHERE json_extract(body, "
                                  "'$.unaddressed') AND NOT json_extract(body, '$.to_me') "
                                  "AND json_extract(body, '$.mailbox') = ?", (OURS_OID,)
                                  ).fetchone()[0]
        finally:
            q.close()

    def test_cardinality_only_a_mention_of_the_owner_prepares_in_a_group(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                guard = people()
                guard.chat_members["g1"] = [OURS_OID, APPROVER_OID, self.THIRD]
                guard.chat_members["g2"] = [OURS_OID, APPROVER_OID, EXCLUDED_OID]
                self.chat("g1", OURS_OID, 9, "Morning all", day=-1)     # engaged in it
                for i in range(n):
                    self.msg("g1", APPROVER_OID, 10, f"Status of run {i}?")    # no mention
                self.msg("g1", self.THIRD, 11, "Can you check the run sheet?", mention=OURS_OID)
                self.msg("g1", APPROVER_OID, 12, "Third, check this", mention=self.THIRD)
                self.msg("g1", OURS_OID, 15, "Checked, all fine.")
                self.msg("g2", APPROVER_OID, 10, "Can you call?", mention=OURS_OID)
                self.store()
                out = Replay.replay(self, role=f"person:{OURS}", guard=guard)
                self.assertEqual(out["prepared_by_autopilot"], {"teams_reply": 1})
                # the rest reach the owner as not addressed to them (no turn: their own
                # message was the day before): no item
                self.assertEqual(self.unaddressed(), n + 1)
                judged = sum(v["judged"] for v in out["workflows"].values())
                matched = sum(v["matched"] for v in out["workflows"].values())
                self.assertEqual((judged, matched), (1, 1))
                self.assertNotIn("g2", (self.dir / "out" / autopilot_engine.GATE_FILE).read_text())

    def test_cardinality_the_first_answer_to_the_owners_own_message_is_their_turn(self):
        """``teams_group_turn``: in a group chat the owner keeps writing in (10 messages in
        14 days), the first message after their own one within the hour is theirs to answer
        though nobody @mentions them: one item for the burst, scored as ``teams_group``.
        Not: an hour after their message, a chat they wrote in nine times, a chat holding an
        excluded person; a message not addressed to them never counts in a sender's
        answered share, and no other autopilot answers it."""
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                guard = people()
                for chat in ("g1", "g3"):
                    guard.chat_members[chat] = [OURS_OID, APPROVER_OID, self.THIRD]
                guard.chat_members["g2"] = [OURS_OID, APPROVER_OID, EXCLUDED_OID]
                for d in range(-10, 0):
                    for chat in ("g1", "g2"):
                        self.msg(chat, OURS_OID, 9, f"Run sheet {d} is up", day=d)
                    if d > -9:
                        self.msg("g3", OURS_OID, 9, f"Run sheet {d} is up", day=d)
                for chat in ("g1", "g2", "g3"):
                    self.msg(chat, OURS_OID, 10, "Line 2 is down until noon.")
                    for i in range(n):
                        self.msg(chat, APPROVER_OID, 10 + (20 + i % 30) / 60, f"Noted {i}, why?")
                self.msg("g1", OURS_OID, 11, "Bearing failed, swapped now.")
                self.msg("g1", self.THIRD, 13, "Can someone check line 4?")   # 2 h on: no
                self.store()
                out = Replay.replay(self, role=f"person:{OURS}", guard=guard)
                self.assertEqual(out["prepared_by_autopilot"], {"teams_group_turn": 1})
                self.assertEqual(out["skipped"].get("teams_group_turn:thread_item_open", 0),
                                 n - 1)
                flow = out["workflows"]["teams_group"]
                self.assertEqual((flow["judged"], flow["matched"]), (1, 1))
                q = ap.Queue(next((self.dir / "out").glob("person_*")) / ap.QUEUE_FILE)
                self.addCleanup(q.close)
                self.assertEqual(q.conn.execute("SELECT COUNT(*) FROM inbound").fetchone()[0], 0)
                # a member who never writes in a chat never gets its messages as events
                self.assertEqual(q.conn.execute(
                    "SELECT COUNT(*) FROM events WHERE json_extract(body, '$.mailbox') = ? "
                    "AND json_extract(body, '$.audience') = 'teams:chat:g3'",
                    (self.THIRD,)).fetchone()[0], 0)
                self.assertNotIn("g2", (self.dir / "out" / autopilot_engine.GATE_FILE).read_text())

    def test_cardinality_a_question_no_one_mentioned_is_the_usual_answerers(self):
        """``teams_group_turn``'s second branch (grade 2026-11-18: a group "How do I ...?"
        the person answered in four minutes had no item): a question nobody @mentioned is
        the owner's when they were the first to write after most of the sender's messages
        there in the last 14 days; one item for the burst. Not: a chat where another member
        answers that sender first, a closing message, a sender with no answered history."""
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                guard = people()
                for chat in ("g1", "g3", "g4"):
                    guard.chat_members[chat] = [OURS_OID, APPROVER_OID, self.THIRD]
                for d in range(-5, 0):
                    self.msg("g1", APPROVER_OID, 9, f"Is batch {d} labelled?", day=d)
                    self.msg("g1", OURS_OID, 9.5, f"Yes, batch {d} is done.", day=d)
                    self.msg("g3", APPROVER_OID, 9, f"Is pallet {d} wrapped?", day=d)
                    self.msg("g3", self.THIRD, 9.2, f"Wrapped {d}.", day=d)
                    self.msg("g3", OURS_OID, 9.5, f"Thanks {d}.", day=d)
                    self.msg("g4", OURS_OID, 9.5, f"Shift {d} notes are up.", day=d)
                for i in range(n):
                    self.msg("g1", APPROVER_OID, 10 + (i % 30) / 60, f"How do I make invoice {i}?")
                    self.msg("g3", APPROVER_OID, 10 + (i % 30) / 60, f"How do I book pallet {i}?")
                    self.msg("g4", self.THIRD, 10 + (i % 30) / 60, f"Where is form {i}?")
                self.msg("g1", APPROVER_OID, 13, "Thanks all, sorted.")
                self.store()
                out = Replay.replay(self, role=f"person:{OURS}", guard=guard)
                self.assertEqual(out["prepared_by_autopilot"], {"teams_group_turn": 1})
                self.assertEqual(out["skipped"].get("teams_group_turn:thread_item_open", 0),
                                 n - 1)

    def test_a_third_member_writing_in_between_ends_the_reply(self):
        """In a group the owner answers a mention; another member (not the asker) writes;
        the owner's next message five minutes on answers that, not the mention."""
        guard = people()
        guard.chat_members["g1"] = [OURS_OID, APPROVER_OID, self.THIRD]
        self.chat("g1", OURS_OID, 9, "Morning all", day=-1)
        self.msg("g1", self.THIRD, 11, "Can you check the run sheet?", mention=OURS_OID)
        self.msg("g1", OURS_OID, 12, "Checked, all fine.")
        self.msg("g1", APPROVER_OID, 12 + 2 / 60, "Is the press free after lunch?")
        self.msg("g1", OURS_OID, 12 + 5 / 60, "Yes from one.")
        self.store()
        out = Replay.replay(self, role=f"person:{OURS}", guard=guard)
        flow = out["workflows"]["teams_chat"]
        self.assertEqual((flow["judged"], flow["matched"]), (1, 1))
        self.assertEqual((flow["actions"], flow["anticipated"]), (2, 1))


class CaptureShapeChats(TeamsChats):
    """The Teams capture lists a chat's members as object ids AND addresses, and names a
    one-to-one chat ``19:<id>_<id>@unq.gbl.spaces``: before, such a chat read as a group of
    four, so no one-to-one message ever reached its other member (reproduced bug)."""

    PAIR = f"19:{OURS_OID}_{APPROVER_OID}@unq.gbl.spaces"
    BARRED = f"19:{EXCLUDED_OID}_{OURS_OID}@unq.gbl.spaces"
    UNKNOWN = f"19:{APPROVER_OID}_{OURS_OID}@unq.gbl.spaces"   # members never captured
    THIRD = GroupChats.THIRD

    def guard(self):
        guard = people()
        guard.chat_members[self.PAIR] = [OURS_OID, APPROVER_OID, OURS, APPROVER]
        guard.chat_members[self.BARRED] = [EXCLUDED_OID, OURS_OID, BOSS, OURS]
        guard.chat_members["g9"] = [OURS_OID, APPROVER_OID, self.THIRD, OURS, APPROVER]
        return guard

    def test_cardinality_a_one_to_one_chat_reaches_its_other_member_both_ways(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                self.chat(self.PAIR, OURS_OID, 9, "Morning, the run sheet is up.", day=-1)
                for i in range(n):
                    self.chat(self.PAIR, APPROVER_OID, 10, f"Did batch {i} pass the check?")
                self.chat(self.PAIR, OURS_OID, 15, "Yes, all passed.")
                self.chat(self.BARRED, EXCLUDED_OID, 10, "Can you call me?")
                self.chat(self.UNKNOWN, APPROVER_OID, 10, "Are you in today?")
                # a group whose members appear by id and by address: a group, not a
                # one-to-one chat (the owner never writes there: not even unaddressed)
                self.chat("g9", APPROVER_OID, 11, "Status of the run?")
                self.store()
                out = Replay.replay(self, role=f"person:{OURS}", guard=self.guard())
                self.assertEqual(out["prepared_by_autopilot"], {"teams_reply": 1})
                self.assertEqual(out["skipped"].get("teams_reply:thread_item_open", 0), n - 1)
                self.assertEqual(GroupChats.unaddressed(self), 0)
                judged = sum(v["judged"] for v in out["workflows"].values())
                matched = sum(v["matched"] for v in out["workflows"].values())
                self.assertEqual((judged, matched), (1, 1))
                text = (self.dir / "out" / autopilot_engine.GATE_FILE).read_text()
                self.assertNotIn(self.BARRED, text)
                self.assertNotIn(self.UNKNOWN, text)

    def test_chat_people_reads_one_person_once_and_unknown_chats_as_no_one(self):
        directory = {OURS: OURS_OID, APPROVER: APPROVER_OID}
        members = self.guard().chat_members
        self.assertEqual(autopilot_events.chat_people(self.PAIR, members, directory),
                         sorted([OURS_OID, APPROVER_OID]))
        self.assertEqual(autopilot_events.chat_people("g9", members, directory),
                         sorted([OURS_OID, APPROVER_OID, self.THIRD]))
        self.assertEqual(autopilot_events.chat_people(self.UNKNOWN, members, directory), [])


class TeamsSenderAnswered(World):
    """The shipped ``teams_reply`` rule (grade 2026-11-18): a question put to the person
    directly (a one-to-one chat, an @mention) is prepared whoever asks, however rarely or
    late the person answered that sender before; a group message that does not name them
    is not ``teams_reply``'s, even from a sender they always answer. The sender record
    (``answers_sender``) stays a registry condition, fail closed without its window."""

    chat = TeamsChats.chat
    FAST = "00000000-0000-4000-8000-0000000000b1"
    SLOW = "00000000-0000-4000-8000-0000000000b2"
    FEW = "00000000-0000-4000-8000-0000000000b3"

    def history(self, guard, sender: str, days: tuple[int, ...], answer_hours: float) -> None:
        # DAY is a Sunday: days -12..-10 are Tuesday..Thursday
        chat = f"h-{sender[-2:]}"
        guard.chat_members[chat] = [OURS_OID, sender]
        for d in days:
            self.chat(chat, sender, 10, "Can you check the batch sheet?", day=d)
            self.chat(chat, OURS_OID, 10 + answer_hours, "Checked.", day=d)

    def test_cardinality_a_direct_question_is_prepared_whoever_asks(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                guard = people()
                self.history(guard, self.FAST, (-12, -11, -10), 2)
                # answered two days on (49 h: past 24 working hours), and too short a record
                self.history(guard, self.SLOW, (-12, -11, -10), 49)
                self.history(guard, self.FEW, (-11, -10), 2)
                for i in range(n):
                    chat = f"w{i}"
                    guard.chat_members[chat] = [OURS_OID, self.FAST]
                    self.chat(chat, OURS_OID, 8, "Morning", day=-1)
                    self.chat(chat, self.FAST, 9 + i // 10, f"Did batch {i} pass?")
                    # the same sender in a group chat, the person not named: not this rule's
                    group = f"g{i}"
                    guard.chat_members[group] = [OURS_OID, self.FAST, APPROVER_OID]
                    self.chat(group, self.FAST, 9 + i // 10, f"Did lot {i} ship already?")
                for sender in (self.SLOW, self.FEW):
                    chat = f"w-{sender[-2:]}"
                    guard.chat_members[chat] = [OURS_OID, sender]
                    self.chat(chat, OURS_OID, 8, "Morning", day=-1)
                    self.chat(chat, sender, 9, "Did the batch pass?")
                self.store()
                out = Replay.replay(self, role=f"person:{OURS}", guard=guard)
                self.assertEqual(out["status"], "complete")
                self.assertEqual(out["prepared_by_autopilot"], {"teams_reply": n + 2})
                q = ap.Queue(next((self.dir / "out").glob("person_*")) / ap.QUEUE_FILE)
                self.addCleanup(q.close)
                items = q.items(None, ("open", "done"))
                self.assertEqual({i["body"]["happened"]["sender"] for i in items},
                                 {self.FAST, self.SLOW, self.FEW})
                self.assertFalse([i for i in items if "teams:chat:g" in json.dumps(i)])

    def test_an_unreadable_answer_window_prepares_nothing(self):
        """Fail closed: without the judged window the sender record is unknown, so no chat
        passes a rule needing ``answers_sender`` (never a guess at the window)."""
        reg = json.loads(SHIPPED_REGISTRY.read_text(encoding="utf-8"))
        entry = next(a for a in reg["autopilots"] if a["id"] == "teams_reply")
        entry["when"]["answers_sender"] = [3, 0.9]
        path = self.dir / "registry-sender-record.json"
        path.write_text(json.dumps(reg))
        guard = people()
        self.history(guard, self.FAST, (-12, -11, -10), 2)
        guard.chat_members["w0"] = [OURS_OID, self.FAST]
        self.chat("w0", OURS_OID, 8, "Morning", day=-1)
        self.chat("w0", self.FAST, 9, "Did batch 1 pass?")
        self.store()
        with mock.patch.object(ap, "REGISTRY_FILE", path):
            out = Replay.replay(self, role=f"person:{OURS}", guard=guard)
        self.assertEqual(out["prepared_by_autopilot"], {"teams_reply": 1})
        broken = self.dir / "replay-broken.json"
        broken.write_text("{not json")
        with mock.patch.object(ap, "REPLAY_CONFIG_FILE", broken), \
                mock.patch.object(ap, "REGISTRY_FILE", path):
            self.assertIsNone(ap.answer_window_hours())
            self.setUp()
            self.history(guard, self.FAST, (-12, -11, -10), 2)
            self.chat("w0", OURS_OID, 8, "Morning", day=-1)
            self.chat("w0", self.FAST, 9, "Did batch 1 pass?")
            self.store()
            out = Replay.replay(self, role=f"person:{OURS}", guard=guard)
        self.assertEqual(out["prepared_by_autopilot"], {})
        for bad in ({"horizon_hours": 0}, {"horizon_hours": True}, {"horizon_hours": "24"},
                    {"horizon_hours": 24 * 15}, []):
            broken.write_text(json.dumps(bad))
            with mock.patch.object(ap, "REPLAY_CONFIG_FILE", broken):
                self.assertIsNone(ap.answer_window_hours(), bad)

    def test_the_record_counts_working_hours_not_the_weekend(self):
        """A Friday afternoon message answered on Monday morning is answered within 24
        working hours; the same answer a day later is not."""
        q = ap.Queue(self.dir / "q.sqlite")
        self.addCleanup(q.close)
        friday = datetime(2026, 10, 16, 20, 0, tzinfo=timezone.utc)      # 4 PM New York
        q.note_inbound([{"mailbox": OURS_OID, "conversation_id": f"t{k}", "sender": "s",
                         "at": ap._iso(friday - timedelta(days=7 * k))} for k in range(3)])
        q.note_sent([{"owner": OURS_OID, "conversation": f"t{k}", "message_id": f"m{k}",
                      "to": ["s"], "at": ap._iso(friday - timedelta(days=7 * k)
                                                 + timedelta(days=3 + (k == 2), hours=-6))}
                     for k in range(3)])
        later = ap._iso(friday + timedelta(days=10))
        self.assertEqual(q.reply_history(OURS_OID, "s", later, horizon_hours=24,
                                         working=True), (3, 2))
        self.assertEqual(q.reply_history(OURS_OID, "s", later, horizon_hours=24), (3, 0))


if __name__ == "__main__":
    unittest.main()
