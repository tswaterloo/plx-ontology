"""The front door (operating/front_door.py): a warm session reads each question once; a
reading that adds a subject, is not the JSON asked for or does not come is None (the
question routes as typed). isolated-fixture: a scripted session, no model."""

from __future__ import annotations

import json
import time
import unittest

from operating import front_door as fd


class Scripted:
    """A session answering its turns from a list (the rules first)."""
    replies: list = []

    def __init__(self, argv, env):
        self.turns = 0
        self.closed = False
        self.sent = []

    def turn(self, text, seconds):
        self.turns += 1
        self.sent.append(text)
        if text == fd.FrontDoor().primer():   # the rules, the record kinds named
            return '{"ready": true}'
        return Scripted.replies.pop(0) if Scripted.replies else None

    def close(self):
        self.closed = True


def door(*replies):
    Scripted.replies = list(replies)
    front = fd.FrontDoor(spawn=Scripted, pool=1)
    front.warm()
    deadline = time.monotonic() + 5
    while front._free.qsize() < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    return front


def reply(rewrite, route="lookup", status="Looking up late purchase orders", recipe=None):
    return json.dumps({"rewrite": rewrite, "route": route, "status": status,
                       "recipe": recipe})


class FrontDoorReads(unittest.TestCase):
    def test_a_messy_question_is_read_into_a_clear_one(self):
        front = door(reply("Which purchase orders are late?"))
        got = front.read("which p o r late")
        self.assertEqual((got.rewrite, got.route, got.status),
                         ("Which purchase orders are late?", "lookup",
                          "Looking up late purchase orders"))
        self.assertTrue(got.changed)
        self.assertEqual(got.question, "which p o r late")

    def test_a_reading_that_adds_a_record_or_a_name_is_refused(self):
        front = door(reply("Where is the rest of BZ-DEO-21F?", "investigate"),
                     reply("Which purchase orders from Acme are late?"))
        self.assertIsNone(front.read("where is the rest of deo21"))   # code completed
        self.assertIsNone(front.read("which pos r late"))             # supplier added

    def test_codes_and_earlier_questions_ground_a_reading(self):
        front = door(reply("What is the status of PO100044?"),
                     reply("Where is the rest of BZ-DEO-21F?", "investigate",
                           "Tracing BZ-DEO-21F"))
        self.assertEqual(front.read("and that one?", ["status of po100044 pls"]).rewrite,
                         "What is the status of PO100044?")
        got = front.read("bz-deo-21f wheres the rest")
        self.assertEqual((got.route, got.status), ("investigate", "Tracing BZ-DEO-21F"))
        # a system's shorthand spelled out grounds; a company added never does
        spelled = door(reply("Is Business Central invoice S-ORD900001 paid?"),
                       reply("Is Business Central invoice S-ORD900001 from Acme paid?"))
        self.assertEqual(spelled.read("is bc invoice S-ORD900001 paid?").rewrite,
                         "Is Business Central invoice S-ORD900001 paid?")
        self.assertIsNone(spelled.read("is bc invoice S-ORD900001 paid?"))

    def test_no_reply_or_not_the_json_asked_for_is_none(self):
        front = door("I think you mean late POs", None)
        self.assertIsNone(front.read("which p o r late"))
        front = door(None)
        self.assertIsNone(front.read("which p o r late"))

    def test_the_pool_never_grows_past_its_size(self):
        started = []

        class Counted(Scripted):
            def __init__(self, argv, env):
                super().__init__(argv, env)
                started.append(self)
        Scripted.replies = [reply("Which purchase orders are late?")] * 3
        front = fd.FrontDoor(spawn=Counted, pool=1)
        front.warm()
        deadline = time.monotonic() + 5
        while front._free.qsize() < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        for _ in range(3):
            self.assertIsNotNone(front.read("which p o r late"))
        self.assertEqual(len(started), 1)

    def test_the_model_picks_a_ready_answer_and_the_phrase_rules_stay_the_fallback(self):
        from operator_api import backend
        from operator_api import models as m
        front = door(reply("Which purchase orders are late?", recipe="late_list"),
                     reply("Why is PO100044 late?", "investigate", recipe="no_such_recipe"))
        late = front.read("which p o r late")
        self.assertEqual(late.recipe, "late_list")
        self.assertIsNone(front.read("y is po100044 late").recipe)   # unknown: none
        asked = m.AskRequest(question="which p o r late")
        self.assertEqual(backend.read_shortcut(asked, late), "late_list")
        on_a_record = m.AskRequest(question="which p o r late",
                                   object_id='PurchaseOrder:bc.purchase_orders:{"id":"x"}')
        self.assertIsNone(backend.read_shortcut(on_a_record, late))

    def test_the_scope_fails_closed_to_the_records(self):
        """The model's scope routes a question; anything else it says, no reading, a
        record page, an open document or a pick is the evidence path, and a "general"
        question naming a record still has its record answered from the evidence."""
        from operating.general_answer import scope_of
        general = json.loads(reply("Explain landed cost."))
        front = door(json.dumps({**general, "scope": "general"}),
                     json.dumps({**general, "scope": "anything"}), reply("Explain landed cost."))
        self.assertEqual(front.read("explain landed cost").scope, "general")
        self.assertEqual(front.read("explain landed cost").scope, "company")
        read = front.read("explain landed cost")
        self.assertEqual(read.scope, "company")
        said = fd.Reading("q", "q", "lookup", "s", 0.1, None, "general")
        self.assertEqual(scope_of(said, "explain landed cost"), "general")
        self.assertEqual(scope_of(said, "is the landed cost on PO 4501 reasonable?"), "mixed")
        self.assertEqual(scope_of(said, "write a reminder about lot L2024-17"), "mixed")
        for kwargs in ({"object_id": "x"}, {"source": "original://x"}, {"picked": True}):
            self.assertEqual(scope_of(said, "explain landed cost", **kwargs), "company")
        self.assertEqual(scope_of(None, "explain landed cost"), "company")

    def test_off_switch(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {fd.SWITCH: "off"}):
            self.assertFalse(fd.FrontDoor.enabled())


if __name__ == "__main__":
    unittest.main()
