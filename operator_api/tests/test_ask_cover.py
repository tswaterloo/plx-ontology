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

    def test_a_flag_becomes_a_patch_on_the_written_sentence_by_its_place(self):
        written = "Intro here. We have {{count(sql://x)}} open orders.\nTail."
        printed = "Intro here. We have 7 open orders.\nTail."
        cover = ac.Cover("checked", [{"kind": "C", "sentence": "We have 7 open orders.",
                                      "why": "read 0 drops <>", "would_settle": "count without it"}])
        got = ac.patches(printed, written, cover)
        self.assertEqual([p.sentence for p in got], ["We have {{count(sql://x)}} open orders."])
        done = ask_patch.apply(written, got, "1. stands")
        self.assertEqual((done.text, done.stood), (written, 1))
        # text that does not split alike is never swapped on a guess
        self.assertEqual(ac.patches(printed + " Extra.", written, cover), [])


if __name__ == "__main__":
    unittest.main()
