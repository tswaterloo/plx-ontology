"""The covering-read pass (operator_api.ask_cover)."""
import json
import unittest

from operator_api import ask_cover as ac, ask_patch


def sql_event(rows):
    result = {"result": "sql://" + "a" * 20, "scope": {"untrusted_text": "reads x; where a = 1; 2 of 2 rows shown"},
              "columns": ["a"], "rows": [{"cite": "c", "cells": ["SECRET-ROW-CELL"]}] * rows,
              "profile": {"rows": rows, "limit_hit": True,
                          "columns": [{"column": "a", "values": [{"cell": "x", "n": 2}]}]}}
    return {"type": "tool_call", "name": "plx.sql", "arguments": {"sql": "SELECT a FROM x WHERE a = 1 LIMIT 2"},
            "result": json.dumps(result)}


class CoverTests(unittest.TestCase):
    def test_the_check_sees_each_reads_scope_and_counts_never_its_rows(self):
        text = ac.task("q?", "Only two exist.", [sql_event(2)])
        self.assertIn("scope: reads x; where a = 1", text)
        self.assertIn("limit reached: True", text)
        self.assertIn('"x" x2', text)
        self.assertNotIn("SECRET-ROW-CELL", text)

    def test_the_prompt_is_the_probes_and_a_flag_of_another_kind_is_refused(self):
        self.assertIn("Find only these three kinds of statement", ac.PROMPT)
        with self.assertRaises(ac.Unreadable):
            ac.parse('{"flags": [{"kind": "D", "sentence": "s"}]}')
        with self.assertRaises(ac.Unreadable):
            ac.parse("no json")
        self.assertEqual(ac.parse('{"flags": []}'), [])

    def test_a_failed_call_is_not_checked_and_says_why(self):
        got = ac.check(object(), "q", "a", [sql_event(1)], call=lambda p: None)
        self.assertEqual((got.status, got.why), ("not_checked", "no reply"))
        got = ac.check(object(), "q", "a", [sql_event(1)], call=lambda p: "garbage")
        self.assertEqual((got.status, got.why), ("not_checked", "could not read its reply"))

    def test_a_flag_goes_back_as_one_turn_that_asks_for_the_whole_answer_again(self):
        printed = "Intro here. We have 7 open orders.\nTail."
        cover = ac.Cover("checked", [{"kind": "C", "sentence": "We have 7 open orders.",
                                      "why": "the read leaves out cancelled", "would_settle": "count without it"},
                                     {"kind": "A", "sentence": "Not in the answer.", "why": "x", "would_settle": ""}])
        flags = ac.sentences(printed, cover)
        # a quoted sentence the answer does not hold is never flagged on a guess
        self.assertEqual([f["sentence"] for f in flags], ["We have 7 open orders."])
        said = ac.message(flags)
        self.assertIn("write the complete answer again", said)
        self.assertIn("single word stands", said)
        self.assertIn("no mention of this check", said)

    def test_the_reply_is_the_answer_never_spliced_into_the_old_text(self):
        from dataclasses import dataclass, field
        from operator_api import simple_ask as sa

        @dataclass
        class Ran:
            final_text: str
            events: list = field(default_factory=list)
            usage: dict = field(default_factory=dict)

        first = Ran("Old answer. Both lines have no price.")
        # h17 (11-24): a reply that began "stands." was spliced into a bullet; now it IS the
        # whole reply or nothing
        kept = sa.covered(first, Ran("stands. Both of the lines have no price, so it holds."))
        self.assertEqual(kept.final_text, "Old answer. Both lines have no price.")
        self.assertEqual(sa.covered(first, Ran("stands")).final_text, first.final_text)
        self.assertEqual(sa.covered(first, Ran("")).final_text, first.final_text)
        new = sa.covered(first, Ran("New whole answer. Two lines have no price."))
        self.assertEqual(new.final_text, "New whole answer. Two lines have no price.")
        self.assertEqual(new.usage["cover_repair"], "rewritten")

    def test_a_patch_reply_that_starts_with_stands_keeps_the_sentence(self):
        written = "One. Two sentence here. Three."
        got = [ask_patch.Patch(5, 22, "Two sentence here.", "why")]
        done = ask_patch.apply(written, got, "1. stands. Both of the lines hold, so the sentence stands as written.")
        self.assertEqual((done.text, done.stood, done.replaced), (written, 1, 0))
        done = ask_patch.apply(written, got, "1. Standing orders are two.")
        self.assertEqual(done.replaced, 1)

    def test_message_requires_standalone_reply_without_revision_or_process_narration(self):
        flags = [{"kind": "A", "sentence": "None found.", "why": "limit reached",
                  "would_settle": "count all rows"}]
        said = ac.message(flags)
        self.assertIn("standalone reply", said)
        self.assertIn("for the first time", said)
        self.assertIn("never frame it as a revision, update, or diff", said)
        self.assertIn("search passes, cursors, filtering steps", said)

    def test_meta_framing_detects_revision_openings_and_ignores_clean_answers(self):
        for bad in (
            "I've rewritten the one-page Word memo. Three things changed:\n- item",
            "Short update: I've read last week's mail and Teams.",
            "**Updated answer:** Here are the open orders.",
            "Here is the corrected summary of open POs.",
            "Three things changed:\n1. First",
            "What changed: one figure was updated.",
        ):
            self.assertTrue(ac.meta_framing(bad), bad)
        for clean in (
            "",
            "**3 open orders remain.** All are priced in CAD.",
            "The invoice was updated on 21 Nov 2026 to 1,200.00 CAD.",
            "No matching sales order was found in the system.",
        ):
            self.assertFalse(ac.meta_framing(clean), clean)

    def test_covered_retry_replaces_only_when_non_empty_and_not_stands(self):
        from dataclasses import dataclass, field
        from operator_api import simple_ask as sa

        @dataclass
        class Ran:
            final_text: str
            events: list = field(default_factory=list)
            usage: dict = field(default_factory=dict)

        prev = Ran("I've rewritten the memo. Three things changed:\n- A",
                   usage={"cover_repair": "rewritten"})
        cleaned = sa.covered_retry(prev, Ran("Clean standalone answer."))
        self.assertEqual(cleaned.final_text, "Clean standalone answer.")
        self.assertEqual(cleaned.usage["cover_retry"], "rewritten")
        self.assertEqual(sa.covered_retry(prev, Ran("")).final_text, prev.final_text)
        self.assertEqual(sa.covered_retry(prev, Ran("stands")).final_text, prev.final_text)
        self.assertEqual(sa.covered_retry(prev, Ran("stands")).usage["cover_retry"], "kept")

    def test_warm_turn_retries_once_when_cover_rewrite_leaks_meta_framing(self):
        from dataclasses import dataclass, field
        from types import SimpleNamespace
        from unittest import mock
        from operator_api import simple_ask as sa

        @dataclass
        class FakeRun:
            final_text: str
            events: list = field(default_factory=list)
            usage: dict = field(default_factory=dict)

        class FakeSession:
            alive = True

            def __init__(self, replies):
                self.replies = list(replies)
                self.prompts = []

            def turn(self, prompt, on_line, cancel):
                self.prompts.append(prompt)
                return FakeRun(self.replies.pop(0))

            def close(self):
                self.alive = False

        def run_with(replies):
            warm = sa.WarmSessions()
            sess = FakeSession(replies)
            host = SimpleNamespace(close=lambda: None)
            asker = SimpleNamespace(
                _reader_key=lambda s: "r1",
                _scope_digest=lambda s: "d1",
                _start_digest=lambda s: "s1",
                _host_for=lambda s: host,
                _agent_for=lambda on: SimpleNamespace(open_session=lambda h: sess),
            )
            with mock.patch.object(sa, "spares_per_reader", return_value=0):
                ran = warm.turn(
                    asker, object(), "c1", "q?", (), lambda line: None, None,
                    check=lambda r, h: ("cover msg", sa.covered),
                )
            warm.close_all()
            return ran, sess.prompts

        ran, prompts = run_with([
            "Initial answer.",
            "I've rewritten the memo. Three things changed:\n- One",
            "Clean standalone answer.",
        ])
        self.assertEqual(ran.final_text, "Clean standalone answer.")
        self.assertEqual(ran.usage["cover_repair"], "rewritten")
        self.assertEqual(ran.usage["cover_retry"], "rewritten")
        self.assertEqual(len(prompts), 3)
        self.assertEqual(prompts[2], ac.STANDALONE_RETRY)

        # clean cover rewrite does not trigger a retry turn
        ran2, prompts2 = run_with(["Initial answer.", "Clean revised answer."])
        self.assertEqual(ran2.final_text, "Clean revised answer.")
        self.assertNotIn("cover_retry", ran2.usage)
        self.assertEqual(len(prompts2), 2)


if __name__ == "__main__":
    unittest.main()
