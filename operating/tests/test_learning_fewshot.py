"""Few-shot drafting from a person's own past replies (``operating.learning_fewshot``), the
learned skips' minimum evidence, and the lessons' hand-over to memory
(``operator_api.learning_memory``).

SYNTHETIC fixtures only; evidence label: isolated-fixture.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
import json
import os
import unittest
from unittest import mock

from operating import learning as L
from operating import learning_fewshot as fs
from operating import learning_signals as ls
from operating.tests.test_autopilot import NOW
from operating.tests.test_learning import ANA, BEN, CLEAR, EXCLUDED, ignored, lessons_of

KEITH = "keith@synth-customer.example"
OTHER = "olga@other-customer.example"
DRAFT = "Hi Keith,\n\nThanks, PO 4471 is entered as our order SO-88.\nIt can ship Fri 16 Oct."
REPLY = ("Hi Keith,\n\nThank you for the update.\nWe are on it.\nAll as usual here.\n"
         "There is nothing else open.\nLet me know if you need anything else.\n\nBest,\nAna\n\nFrom: Keith\nSent: ...\nold text")


def replied(i: int, person: str = ANA, *, sender: str = KEITH, reply: str = REPLY,
            autopilot: str = "customer_po", conv: str = "conv-a") -> L.Signal:
    feats = L.edit_features("", reply)
    return L.Signal(kind="replied", person=person, at=L._iso(NOW - timedelta(days=i)),
                    ref=f"item:r-{person[:4]}-{sender[:3]}-{i}", people=(sender,),
                    data={"autopilot": autopilot, "channel": "mail", "side": "customer",
                          "sender": L.sender_key(sender), "item": f"ap_r{i}",
                          "thread": L.digest("thread", f"{conv}-{i}")[:16],
                          "reply": f"Email:r{i}", "topic": L.topic_keys("purchase order"),
                          **{k: feats[k] for k in ("greeting", "signoff", "signoff_text",
                                                   "words", "shape")}})


def event(sender: str = KEITH) -> dict:
    return {"sender": sender, "subject": "Purchase order", "text": "PO 4471 attached",
            "channel": "mail", "side": "customer", "conversation_id": "conv-new"}


def work() -> dict:
    return {"summary": "s", "draft": {"body": DRAFT, "to": [{"address": KEITH}]}}


class FewShotDraft(unittest.TestCase):
    def test_a_draft_takes_what_the_person_wrote_to_this_counterparty_and_keeps_every_fact(self):
        got = lessons_of([replied(i) for i in range(1, 4)])
        out = got.apply_draft(work(), ANA, event=event(), autopilot="customer_po")
        body = out["draft"]["body"]
        self.assertIn("Thanks, PO 4471 is entered as our order SO-88.", body)
        self.assertIn("It can ship Fri 16 Oct.", body)
        self.assertIn("Thank you for the update.", body)
        self.assertIn("Let me know if you need anything else.", body)
        self.assertTrue(body.rstrip().endswith("Best"))
        self.assertNotIn("Ana", body)                       # never their signature
        self.assertGreaterEqual(set(out["learned"]["few_shot"]), {"lead", "close"})
        self.assertEqual(set(out["learned"]["examples"][0]), {"item", "at", "match"})
        # the learned draft is closer to what they send
        reply = "Hi Keith,\n\nThank you for the update.\nLet me know if " \
                "you need anything else.\n\nBest,"
        self.assertNotIn("as usual", body)                  # a middle sentence: never kept
        self.assertLess(L.normalized_edit_distance(body, reply),
                        L.normalized_edit_distance(DRAFT, reply))

    def test_only_the_persons_own_earlier_repeated_writing_shapes_a_draft(self):
        got = lessons_of([replied(i) for i in range(1, 4)])
        w = work()
        self.assertEqual(got.apply_draft(w, BEN, event=event(), autopilot="customer_po"), w)
        # examples written after the draft's moment are never used (time-forward)
        early = got.apply_draft(w, ANA, event=event(), autopilot="customer_po",
                                before=L._iso(NOW - timedelta(days=10)))
        self.assertEqual(early["learned"]["few_shot"], [])
        self.assertNotIn("for the update", early["draft"]["body"])
        # a sentence written once may be that day's fact: never added
        once = lessons_of([replied(1)])
        body = once.apply_draft(w, ANA, event=event(), autopilot="customer_po")
        self.assertNotIn("for the update", (body.get("draft") or {}).get("body", ""))
        # another counterparty and another kind of item: not like this message
        far = lessons_of([replied(i, sender=OTHER, autopilot="reply_prep")
                          for i in range(1, 6)])
        self.assertNotIn("for the update", far.apply_draft(
            w, ANA, event=event(), autopilot="customer_po")["draft"]["body"])

    def test_a_repeated_claim_is_never_inserted_only_courtesy(self):
        reply = "Hi Keith,\n\nIt shipped yesterday.\nPlease see attached.\n\nBest,"
        got = lessons_of([replied(i, reply=reply) for i in range(1, 6)])
        body = got.apply_draft(work(), ANA, event=event(), autopilot="customer_po")[
            "draft"]["body"]
        self.assertNotIn("shipped yesterday", body)
        self.assertNotIn("attached", body)
        self.assertFalse(fs.courtesy("we cancelled it."))
        for claim in ("ok, it went out.", "ok, we can do that.", "thanks, we got it.",
                      "great, all set.", "noted, it is on the truck."):
            self.assertFalse(fs.courtesy(claim), claim)
        self.assertTrue(fs.courtesy("thank you for the update."))
        self.assertTrue(fs.courtesy("let me know if you need anything else."))

    def test_a_sentence_with_a_figure_or_name_is_never_learned_or_inserted(self):
        reply = "Hi Keith,\n\nPO 991 ships Monday.\nSandro will call you.\n\nBest,"
        got = lessons_of([replied(i, reply=reply) for i in range(1, 5)])
        body = got.apply_draft(work(), ANA, event=event(), autopilot="customer_po")[
            "draft"]["body"]
        self.assertNotIn("991", body)
        self.assertNotIn("Sandro", body)
        self.assertEqual(replied(1, reply=reply).data["shape"], ["#", "#"])
        self.assertIsNone(L.template("Maria will call you back today."))
        self.assertEqual(L.template("Please see attached."), "please see attached.")

    def test_an_excluded_person_has_no_examples_and_finance_replies_teach_nothing(self):
        kept, dropped = L.admit([replied(i, person=EXCLUDED) for i in range(1, 4)]
                                + [replied(i, sender="excluded@synth.example")
                                   for i in range(5, 8)], CLEAR)
        self.assertEqual(kept, [])
        self.assertEqual(dropped["person_not_cleared"], 3)
        money = "Hi Keith,\n\nThe invoice balance is past due, please remit payment.\n\nBest,"
        self.assertEqual(replied(1, reply=money).data["shape"], ["#"])


class DrafterPrompt(unittest.TestCase):
    def test_prompt_block_shows_own_pairs_and_rules_never_finance(self):
        sigs = [replied(i) for i in range(1, 4)]
        got = lessons_of(sigs)
        texts = {"ap_r1": (None, REPLY), "ap_r2": (None, "Hi Keith,\n\nThe invoice is past "
                                                         "due, please pay.\n\nBest,"),
                 "ap_r3": ("Hi Keith, draft", REPLY)}
        block = got.drafter_prompt(ANA, event(), "customer_po",
                                   lambda s: texts.get(s.data["item"]))
        self.assertIn("for the update", block)
        self.assertNotIn("invoice", block)
        self.assertNotIn("old text", block)                    # quoted thread cut
        self.assertIn("Prepared draft:", block)
        self.assertEqual(got.drafter_prompt(BEN, event(), "customer_po", texts.get), "")

    def test_model_draft_is_used_only_when_it_keeps_every_fact_and_adds_none(self):
        ev = event()
        good = "Hi Keith,\n\nThanks, PO 4471 is entered as our order SO-88. It can ship " \
               "Fri 16 Oct.\n\nBest,"
        self.assertEqual(fs.model_draft(DRAFT, ev, "", lambda p: good), good)
        lost = "Hi Keith,\n\nPO 4471 is entered.\n\nBest,"
        self.assertIsNone(fs.model_draft(DRAFT, ev, "", lambda p: lost))
        added = good.replace("Fri 16 Oct.", "Fri 16 Oct, tracking 1Z999.")
        self.assertIsNone(fs.model_draft(DRAFT, ev, "", lambda p: added))
        self.assertIsNone(fs.model_draft(DRAFT, ev, "", lambda p: None))
        self.assertIsNone(fs.model_draft(DRAFT, ev, "", lambda p: 1 / 0))
        self.assertIsNone(fs.model_draft(DRAFT, ev, "", lambda p: good.replace(
            "Best,", "Maria will call you Monday.\n\nBest,")))       # a name, a weekday
        pay = good.replace("Thanks,", "Your salary and bonus are attached. Thanks,")
        self.assertIsNone(fs.model_draft(DRAFT, ev, "", lambda p: pay))
        seen = []
        fs.model_draft(DRAFT, ev, "BLOCK", lambda p: seen.append(p) or good)
        self.assertIn("BLOCK", seen[0])
        self.assertIn("PREPARED DRAFT:\n" + DRAFT, seen[0])


class Signals(unittest.TestCase):
    def test_a_reply_to_an_item_without_a_draft_is_a_features_only_example(self):
        items = [{"item_id": "i9", "event_id": "e9", "owner": ANA, "autopilot": "customer_po",
                  "state": "done", "done_reason": "owner_replied",
                  "due_at": "2026-10-10T00:00:00Z", "created_at": "2026-10-09T00:00:00Z",
                  "body": {"prepared": {}, "happened": {}}},
                 {"item_id": "i8", "event_id": "e8", "owner": ANA, "autopilot": "approval",
                  "state": "done", "done_reason": "approval_decided",
                  "done_at": "2026-10-11T00:00:00Z", "due_at": "2026-10-10T00:00:00Z",
                  "created_at": "2026-10-09T00:00:00Z", "body": {"prepared": {}}}]
        events = {"e9": {**event(), "conversation_id": "c9"}}
        got = ls.item_signals(items, events, now=NOW, reply_of=lambda i, e: (
            "Email:9", "2026-10-09T12:00:00Z", REPLY))
        self.assertEqual([(s.kind, s.ref) for s in got],
                         [("replied", "item:i9"), ("item_acted", "item:i8")])
        data = got[0].data
        self.assertEqual(data["thread"], L.digest("thread", "c9")[:16])
        self.assertEqual(data["shape"], ["thank you for the update.", "we are on it.", "#",
                                         "there is nothing else open.",
                                         "let me know if you need anything else."])
        self.assertNotIn("Ana", json.dumps(data))
        self.assertNotIn("Keith", json.dumps(data))
        # a reply is an act on the item (never counted as a draft's added sentence)
        lessons = lessons_of(got)
        self.assertFalse([l for l in lessons.of(ANA) if l.key.startswith("add:")])


class LearnedSkips(unittest.TestCase):
    def test_a_counterparty_left_three_times_is_skipped_mixed_history_is_not(self):
        def left(i, sender, autopilot="customer_po"):
            s = ignored(i, autopilot=autopilot)
            return L.Signal(kind=s.kind, person=s.person, at=s.at, ref=s.ref + sender,
                            data={**s.data, "sender": L.sender_key(sender)})
        got = lessons_of([left(i, OTHER) for i in range(1, 5)]
                         + [left(i, KEITH) for i in range(1, 3)]
                         + [replied(i, sender=KEITH) for i in range(3, 5)])
        self.assertTrue(got.skip_reason(ANA, (), "customer_po", event(OTHER)))
        self.assertIsNone(got.skip_reason(ANA, (), "customer_po", event(KEITH)))
        self.assertIsNone(got.skip_reason(BEN, (), "customer_po", event(OTHER)))
        # two leaves are not enough evidence
        few = lessons_of([left(i, OTHER) for i in range(1, 3)])
        self.assertIsNone(few.skip_reason(ANA, (), "customer_po", event(OTHER)))


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, args):
        self.sql, self.args = sql, args
        return self

    def fetchall(self):
        return self.rows


class _Memory:
    def __init__(self, rows):
        self.conn = _Conn(rows)
        self.wrote = []

    @contextmanager
    def connect(self):
        yield self.conn

    def remember(self, reader, **item):
        self.wrote.append((reader.key, item))
        return len(self.wrote)


class MemoryHandOff(unittest.TestCase):
    def test_corrections_and_thumbs_reach_the_persons_own_memory_key(self):
        from operator_api import learning_memory as lm
        key = f"group:g1,synth-tenant:employee:{ANA}"
        mem = _Memory([(f"t:employee:{BEN}x", NOW), (key, NOW)])
        writer = lm.OfflineWriter(mem)
        signals = [L.Signal(kind="answer_corrected", person=ANA, at=L._iso(NOW - timedelta(
            days=i)), ref=f"ask:{i}", data={"question": "who carries SO-1",
                                            "value": "Synth Freight", "markings": []})
            for i in range(1, 4)]
        signals += [L.Signal(kind="answer_rated", person=ANA, at=L._iso(NOW - timedelta(
            days=i)), ref=f"fb:{i}", data={"rating": 1, "systems": ["bc"], "markings": []})
            for i in range(1, 4)]
        lessons = lessons_of(signals)
        with mock.patch.object(L, "MEMORY_FACTORY", lambda: writer), \
                mock.patch.dict("sys.modules", {"operator_api.memory": mock.Mock(
                    KeyReader=lambda k: mock.Mock(key=k))}):
            got = L._publish_to_memory(lessons)
        self.assertEqual({k for k, _i in mem.wrote}, {key})
        kinds = {(i["kind"], i["subject"].split(".")[1]) for _k, i in mem.wrote}
        self.assertEqual(kinds, {("fact", "fact"), ("preference", "source")})
        self.assertTrue(all(i["source"] == "learn" for _k, i in mem.wrote))
        self.assertIn("Synth Freight", json.dumps([i["text"] for _k, i in mem.wrote]))
        self.assertTrue(got)
        # a person who never used the Ask has no key: nothing is written for them
        none = lm.OfflineWriter(_Memory([]))
        self.assertIsNone(none.remember_for(BEN, kind="fact", text="x", source="learn"))
        self.assertEqual(none.skipped, {"no_reader_key": 1})
        # a key that only looks like it (another oid inside) is never taken
        self.assertIsNone(lm.OfflineWriter(_Memory([(f"employee:{ANA}0", NOW)]))
                          .remember_for(ANA, kind="fact", text="x", source="learn"))

    def test_no_dsn_no_memory_writer(self):
        from operator_api import learning_memory as lm
        self.assertIsNone(lm.factory({}))
        self.assertEqual(L._publish_to_memory(L.Lessons(())), "memory_not_available")


if __name__ == "__main__":
    unittest.main()
