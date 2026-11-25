"""Clarify and decline (Genie plan v2 item 3): the question the Ask asks back and the gap it
declares reach the person as the answer's ``clarify`` and ``not_found`` (kept in the work
log with the answer), the person's pick continues the same question, and a block the app
cannot read whole is removed without asking or claiming anything (fail closed).
Evidence: isolated-fixture."""
from __future__ import annotations

import json
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from operator_api import ask_blocks as ab
from operator_api import models as m
from operator_api import simple_ask as sa
from operator_api.backend_ask import AskMixin


def block(kind: str, body) -> str:
    return f"```{kind}\n{body if isinstance(body, str) else json.dumps(body)}\n```\n"


ASK = {"question": "Which Croda account?", "options": ["Croda Inc (US)", "Croda Canada"]}
GAP = {"missing": "freight cost per shipment", "would_live": "carrier invoices"}


class Blocks(unittest.TestCase):
    def test_the_question_back_and_the_gap_are_taken_out_of_the_text(self):
        text = "Two accounts match.\n" + block("clarify", ASK) + "Freight is not kept.\n" \
            + block("GAP", GAP) + block("gap", GAP)
        shown, asked, gaps = ab.take(text)
        self.assertEqual(shown, "Two accounts match.\nFreight is not kept.")
        self.assertEqual(asked, ASK)
        self.assertEqual(gaps, [{"what": "freight cost per shipment",
                                 "where_it_would_live": "carrier invoices"}])   # once

    def test_a_block_not_read_whole_is_removed_and_nothing_is_taken_from_it(self):
        bad = [block("clarify", "not json"), block("clarify", {"question": "q"}),
               block("clarify", {**ASK, "options": ["one"]}),
               block("clarify", {**ASK, "options": ["a", "b", "c", "d", "e"]}),
               block("clarify", {**ASK, "options": ["a", "a"]}),
               block("clarify", {**ASK, "options": ["a", 3]}),
               block("clarify", {**ASK, "extra": 1}),
               block("clarify", {**ASK, "question": "x" * (ab.MAX_QUESTION + 1)}),
               block("clarify", ["a"]), block("gap", {"would_live": "x"}),
               block("gap", {**GAP, "missing": ""}), block("gap", {**GAP, "why": "x"}),
               block("gap", {**GAP, "would_live": 5})]
        for raw in bad:
            with self.subTest(raw=raw):
                shown, asked, gaps = ab.take("Answer.\n" + raw)
                self.assertEqual((shown, asked, gaps), ("Answer.", None, []))
        many = "".join(block("gap", {"missing": f"m{i}"}) for i in range(10))
        self.assertEqual(len(ab.take(many)[2]), ab.MAX_GAPS)
        self.assertEqual(ab.take(many)[2][0]["where_it_would_live"],
                         "a source the Workbench does not read")

    def test_every_session_is_told_the_two_blocks(self):
        self.assertIn(ab.PROMPT_LINE, sa.system_prompt())


def run(text: str, **kw) -> sa.SimpleRun:
    return sa.SimpleRun(text=text, agent_text=text, unverified=[], events=[], seconds=1.0,
                        stopped=False, failure=None, **kw)


class Answers(unittest.TestCase):
    def test_a_question_back_answers_nothing_yet_and_a_gap_is_not_on_record(self):
        asked = sa.answer_payload(run("Two accounts match.", clarify=ASK), "q", "c", 1, "a" * 24)
        self.assertEqual(asked.outcome, "abstained")
        self.assertEqual((asked.clarify.question, [o.label for o in asked.clarify.options]),
                         (ASK["question"], ASK["options"]))
        gap = {"what": "freight cost", "where_it_would_live": "carrier invoices"}
        declined = sa.answer_payload(run("Not kept.", gaps=[gap]), "q", "c", 1, "b" * 24)
        self.assertEqual(declined.outcome, "not_found")
        self.assertEqual([n.model_dump() for n in declined.not_found], [gap])
        plain = sa.answer_payload(run("It was 5."), "q", "c", 1, "c" * 24)
        self.assertEqual((plain.outcome, plain.clarify, plain.not_found), ("answered", None, []))
        # a session that failed before it spoke claims no question back and no gap
        failed = sa.SimpleRun(text="", agent_text="", unverified=[], events=[], seconds=1.0,
                              stopped=False, failure="sign_in", clarify=ASK, gaps=[gap])
        out = sa.answer_payload(failed, "q", "c", 1, "d" * 24)
        self.assertEqual((out.clarify, out.not_found), (None, []))

    def test_the_items_reach_the_answer_payload_and_every_session_is_told_the_block(self):
        groups = [{"title": "Reply", "items": [{"source": "Mail", "who": "Dana", "text": "Ship date?",
                                                "open_url": "/source?handle=x"}]}]
        got = sa.answer_payload(run("Two things.", work_items=groups), "q", "c", 1, "e" * 24)
        self.assertEqual(got.work_items[0].items[0].open_url, "/source?handle=x")
        self.assertEqual(sa.answer_payload(run("Two."), "q", "c", 1, "f" * 24).work_items, [])
        self.assertIn(ab.ITEMS_LINE, sa.system_prompt())

    def test_the_person_s_pick_continues_the_same_question(self):
        backend = SimpleNamespace(_lock=threading.Lock(), _answers={}, _recent={},
                                  journal_full_read=lambda *a: None,
                                  reader_wrote=lambda reader: None, memory=None)
        reader = SimpleNamespace(session=SimpleNamespace(), key="plx:staff:ana")
        request = m.AskRequest(question="how much did croda buy this year")
        answer = sa.answer_payload(run("Two accounts match.", clarify=ASK), request.question,
                                   "c", 1, "e" * 24)
        with mock.patch("operator_api.serving.access_label", lambda session: None), \
                mock.patch("operator_api.feedback.served_markings", lambda b, r: []):
            list(sa.finish(backend, reader, request, answer, "c", lambda: 5, False))
        pick = m.AskRequest(question="Croda Canada", clarified=m.ClarifyReply(
            answer_id=answer.answer_id, option=1))
        got = AskMixin._clarified(backend, reader, pick)
        self.assertIn("how much did croda buy this year", got.question)
        self.assertIn("Croda Canada", got.question)
        # another reader's pick on the same answer id is answered as its own words
        other = SimpleNamespace(session=SimpleNamespace(), key="plx:staff:ben")
        self.assertEqual(AskMixin._clarified(backend, other, pick).question, "Croda Canada")


class ItemsBlock(unittest.TestCase):
    HANDLE = "graph-mail://" + "ab12" * 20

    def block(self, groups):
        import json
        return "Two things need you.\n```items\n" + json.dumps({"groups": groups}) + "\n```\nBye."

    def test_the_block_leaves_the_text_and_its_groups_are_taken_whole(self):
        text, groups = ab.take_items(self.block([
            {"title": "Reply", "items": [{"source": "Mail", "who": "Dana", "when": "9:12 AM",
                                          "ref": "x://y", "text": "Asks for the ship date."}]},
            {"title": "Decide", "items": [{"text": "Approve the quote."}]}]))
        self.assertEqual(text, "Two things need you.\nBye.")
        self.assertEqual([g["title"] for g in groups], ["Reply", "Decide"])
        self.assertEqual(groups[1]["items"][0]["text"], "Approve the quote.")

    def test_a_block_that_is_not_the_shape_stays_in_the_text_nothing_lost(self):
        for bad in ("```items\nnot json\n```", '```items\n{"groups": []}\n```',
                    '```items\n{"groups": [{"title": "A", "items": [{"who": "x"}]}]}\n```'):
            text, groups = ab.take_items("Intro\n" + bad)
            self.assertEqual(groups, [])
            self.assertIn("```items", text)

    def test_a_link_only_for_a_ref_a_result_of_the_conversation_showed(self):
        from operating import open_refs
        ref = open_refs.short(self.HANDLE)
        events = [{"type": "tool_call", "name": "plx.search", "result": __import__("json").dumps(
            {"documents": [{"citation": self.HANDLE, "ref": ref}]})}]
        groups = [{"title": "Reply", "items": [
            {"source": "Whatever", "who": "Dana", "when": None, "ref": ref, "text": "a"},
            {"source": "Teams", "who": None, "when": None,
             "ref": "graph-mail://r.zzzzzzzzzz", "text": "b"},   # invented: no link
            {"source": "Planner", "who": None, "when": None, "ref": None, "text": "c"}]}]
        got = ab.link_items(groups, events)[0]["items"]
        self.assertIn("/source?handle=graph-mail%3A%2F%2F", got[0]["open_url"])
        self.assertEqual(got[0]["source"], "Mail")      # the handle's own scheme names the source
        self.assertNotIn("open_url", got[1])
        self.assertEqual(got[1]["source"], "Teams")      # the answer's own word stays
        self.assertNotIn("open_url", got[2])

    def test_no_limit_on_how_many_items_and_none_lost_at_1_10_60(self):
        for n in (1, 10, 60):
            items = [{"text": f"item {i}"} for i in range(n)]
            _, groups = ab.take_items(self.block([{"title": "Reply", "items": items}]))
            self.assertEqual([i["text"] for i in groups[0]["items"]], [f"item {i}" for i in range(n)])


if __name__ == "__main__":
    unittest.main()
