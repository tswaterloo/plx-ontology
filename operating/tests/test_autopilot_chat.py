"""A Teams chat draft reads the chat's earlier messages and the person's own recent replies
(``operating.autopilot_chat``; 2026-11-18 ceiling test: given only the one message the
drafter wrote 0/27 sendable, a session given these two inputs 4/27).

Boundaries: only the message's own chat and only before the message (never the real reply,
never another chat); examples only the person's own, from chats the exclusion clears, each
pair a screen refuses left out; a reply's figure never comes from an example; examples are
never kept on an item; anything unreadable gives nothing. No model is called."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from operating import autopilot_chat as chat
from operating import autopilot_drafter as ad
from operating.tests.test_autopilot_drafter import BIN, HOME, Model
from operating.tests.test_payroll_marking import PAYSTUB

ME = "11111111-1111-1111-1111-111111111111"
JO = "22222222-2222-2222-2222-222222222222"
CHAT = "teams:chat:19:a@thread.v2"
OTHER = "teams:chat:19:b@thread.v2"
BARRED_CHAT = "teams:chat:19:c@thread.v2"


class People:
    def __init__(self, barred_units=(), barred=()) -> None:
        self.barred_units, self.barred = set(barred_units), set(barred)

    def identity_clear(self, who: str) -> bool:
        return who not in self.barred

    def unit_clear(self, unit: str) -> bool:
        return unit not in self.barred_units


def _payload(at: str, sender: str, text: str, *, kind="chat_message", mid="") -> str:
    name = "Vince Sample" if sender == ME else "Jo Colleague"
    return json.dumps({"row": {"kind": kind, "id": mid or at, "created_at": at,
                               "from": {"user": {"id": sender, "display_name": name}},
                               "body_text": text, "message_type": "message"}})


class Store:
    def __init__(self) -> None:
        self.state = Path(tempfile.mkdtemp())
        (self.state / "teams-capture").mkdir()
        self.conn = sqlite3.connect(self.state / "teams-capture" / "capture.db")
        self.conn.execute("CREATE TABLE versions (kind TEXT, unit TEXT, path TEXT, "
                          "payload TEXT, superseded_at TEXT)")

    def add(self, unit: str, at: str, sender: str, text: str, **kw) -> None:
        self.conn.execute("INSERT INTO versions VALUES ('teams_chat', ?, ?, ?, NULL)",
                          (unit, at + text[:5], _payload(at, sender, text, **kw)))
        self.conn.commit()


def _at(i: int) -> str:
    return f"2026-11-17T{8 + i // 60:02d}:{i % 60:02d}:00Z"


EVENT = {"channel": "teams", "conversation_id": CHAT, "at": "2026-11-17T12:00:00Z",
         "text": "Can you approve the neck size?", "object_id": "TeamsChatMessage:x"}


class ChatThread(unittest.TestCase):

    def test_only_the_chats_earlier_messages_the_last_ones_in_order_never_the_reply_and_the_cut_said(self):
        for n in (1, 10, 60):
            s = Store()
            for i in range(n):
                s.add(CHAT, _at(i), ME if i % 3 == 0 else JO, f"earlier {i}")
            s.add(CHAT, "2026-11-17T12:05:00Z", ME, "THE REAL REPLY 4000 units")
            s.add(OTHER, _at(0), JO, "another chat's secret")
            got = chat.chat_thread(s.state, EVENT, ME, "Vince")
            want = list(range(n))[-chat.THREAD:]
            notes = [x for x in got if x.startswith("[") and x.endswith("not shown]")]
            self.assertEqual(notes, [f"[{n - chat.THREAD} earlier messages not shown]"] if n > chat.THREAD else [])
            got = [x for x in got if x not in notes]

            self.assertEqual([int(x.rsplit(" ", 1)[1]) for x in got], want, n)
            self.assertEqual(len(set(got)), len(got))
            joined = "\n".join(got)
            self.assertNotIn("REAL REPLY", joined)
            self.assertNotIn("secret", joined)
            # the person's own lines are marked theirs, the others by name
            for line, i in zip(got, want):
                self.assertIn("Vince (their own):" if i % 3 == 0 else "Jo Colleague:", line)

    def test_unreadable_or_not_a_chat_gives_nothing(self):
        s = Store()
        s.add(CHAT, _at(0), JO, "earlier")
        s.conn.execute("INSERT INTO versions VALUES ('teams_chat', ?, 'p', 'not json', NULL)",
                       (CHAT,))
        s.add(CHAT, _at(1), JO, "a system event", kind="system")
        s.conn.commit()
        self.assertEqual(len(chat.chat_thread(s.state, EVENT, ME)), 1)
        for event in ({**EVENT, "channel": "mail"},
                      {**EVENT, "conversation_id": "teams:channel:19:x|post"},
                      {**EVENT, "at": ""}):
            self.assertEqual(chat.chat_thread(s.state, event, ME), [], event)
        self.assertEqual(chat.chat_thread(Path(tempfile.mkdtemp()), EVENT, ME), [])
        self.assertEqual(chat.reply_examples(Path(tempfile.mkdtemp()), EVENT, ME, People()),
                         [])


class ReplyExamples(unittest.TestCase):

    def store(self) -> Store:
        s = Store()
        s.add(OTHER, "2026-11-10T09:00:00Z", JO, "Did the labels come in?")
        s.add(OTHER, "2026-11-10T09:01:00Z", ME, "yep lol")
        s.add(OTHER, "2026-11-11T09:00:00Z", JO, PAYSTUB)
        s.add(OTHER, "2026-11-11T09:01:00Z", ME, "ok")
        s.add(OTHER, "2026-11-12T09:00:00Z", JO, "loop in barred@ours.example?")
        s.add(OTHER, "2026-11-12T09:01:00Z", ME, "no")
        s.add(BARRED_CHAT, "2026-11-13T09:00:00Z", JO, "from a chat the exclusion bars")
        s.add(BARRED_CHAT, "2026-11-13T09:01:00Z", ME, "sure")
        s.add(OTHER, "2026-09-01T09:00:00Z", JO, "too old")
        s.add(OTHER, "2026-09-01T09:01:00Z", ME, "old reply")
        s.add(OTHER, "2026-11-17T12:30:00Z", JO, "after the message")
        s.add(OTHER, "2026-11-17T12:31:00Z", ME, "later reply")
        return s

    def test_own_replies_from_cleared_chats_each_refused_pair_left_out(self):
        s = self.store()
        people = People(barred_units={BARRED_CHAT}, barred={"barred@ours.example"})
        held: dict = {}
        got = chat.reply_examples(s.state, EVENT, ME, people, held)
        self.assertEqual(got, ["Jo Colleague wrote: Did the labels come in?\n  -> yep lol"])
        # read once an hour per person: a second draft reads the cache
        s.add(OTHER, "2026-11-14T09:00:00Z", JO, "new one")
        s.add(OTHER, "2026-11-14T09:01:00Z", ME, "new reply")
        self.assertEqual(chat.reply_examples(s.state, EVENT, ME, people, held), got)

    def test_a_figure_only_an_example_states_withholds_the_reply_and_examples_are_never_kept(self):
        m = ad.Material(reader="Vince", channel="teams", sender="Jo Colleague", subject="",
                        text="How many minis would they need?",
                        thread=["[2026-11-17 11:00] Jo Colleague: they approved the quote"],
                        examples=["Jo Colleague wrote: how many?\n  -> 4000 to start"])
        w = ad.Writer("opus", binary=BIN, home=HOME, seconds=5, per_cycle=4,
                      call=Model("They need 4000 to start"))
        event = {**EVENT, "cleared": True, "sender": JO}
        self.assertEqual(w.write(m, event, People(), ME), (None, "figure_not_in_material"))
        # the chat's own earlier message may carry the figure
        m2 = ad.Material(**{**m.__dict__, "thread": [*m.thread, "[2026-11-17 11:01] Jo "
                                                     "Colleague: they want 4000 to start"]})
        model = Model("They need 4000 to start")
        w = ad.Writer("opus", binary=BIN, home=HOME, seconds=5, per_cycle=4, call=model)
        got, why = w.write(m2, event, People(), ME)
        self.assertEqual((got.text, why), ("They need 4000 to start", ""))
        self.assertIn("they approved the quote", model.prompts[0])
        self.assertIn("-> 4000 to start", model.prompts[0])
        self.assertNotIn("examples", m2.kept())
        self.assertEqual(m2.kept()["thread"], m2.thread)
        with self.assertRaises(ValueError):
            ad.Material.of({**m2.kept(), "examples": ["x"]})

    def test_an_example_the_screen_refuses_never_reaches_the_model(self):
        # the writer screens everything the prompt holds: an example is in it
        m = ad.Material(reader="Vince", channel="teams", sender="Jo Colleague", subject="",
                        text="ok?", examples=["Jo wrote: " + PAYSTUB + "\n  -> ok"])
        model = Model("ok")
        w = ad.Writer("opus", binary=BIN, home=HOME, seconds=5, per_cycle=4, call=model)
        got, why = w.write(m, {**EVENT, "cleared": True, "sender": JO}, People(), ME)
        self.assertIsNone(got)
        self.assertEqual(why, "pay_or_hr")
        self.assertEqual(model.prompts, [])


class MessageText(unittest.TestCase):
    """11-18 live: the fast feed typed chat messages before the store's text index held
    them; 6 of 6 reached the writer empty and were answered nothing_useful."""

    def event(self, mid: str) -> dict:
        key = json.dumps({"chat_id": CHAT[len("teams:chat:"):], "id": mid}, separators=(",", ":"))
        return {**EVENT, "text": "", "object_id": "TeamsChatMessage:gen.teams.chat_messages:" + key}

    def test_an_untyped_chat_message_is_drafted_from_its_captured_text(self):
        from types import SimpleNamespace
        from operating import autopilot as ap

        class Writer:
            busy, seen = False, []

            def new_cycle(self):
                pass

            def pending(self):
                return set()

            def write(self, m, event, people, owner, **kw):
                self.seen.append(m)
                return None, "nothing_useful"

        s = Store()
        s.add(CHAT, "2026-11-17T11:00:00Z", JO, "earlier")
        s.add(CHAT, "2026-11-17T12:00:00Z", JO, "Can you approve the neck size?", mid="m1")
        s.add(OTHER, "2026-11-17T12:00:00Z", JO, "same id, another chat", mid="m2")
        queue = SimpleNamespace(path=s.state / "autopilot" / "autopilot.sqlite")
        for mid, want in (("m1", "Can you approve the neck size?"), ("m2", ""), ("nope", "")):
            writer = Writer()
            writer.seen = []
            held = {"writer": writer}
            ap._model_draft(queue, held, {}, self.event(mid), ME, {"kind": "reply"}, People(), {})
            self.assertEqual(writer.seen[0].text, want, mid)
        # not a chat message, or no id: nothing read
        self.assertEqual(chat.message_text(s.state, {**self.event("m1"), "channel": "mail"}), "")
        self.assertEqual(chat.message_text(s.state, {**self.event("m1"), "object_id": "x"}), "")


if __name__ == "__main__":
    unittest.main()
