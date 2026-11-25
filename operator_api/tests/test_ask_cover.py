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


if __name__ == "__main__":
    unittest.main()
