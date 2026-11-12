"""CC-P-10: every model call is counted in the ledger (feature, seconds, turns, tokens,
reuse; never the prompt or the answer), and an identical recent ask with no tools is
answered once. Evidence label ``isolated-fixture`` (stand-in model processes)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from operating import general_answer, model_ledger
from operating import query_understanding as qu

SECRET_QUESTION = "what did ledgertestcustomer pay us"


class LedgerTests(unittest.TestCase):
    def setUp(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, folder, True)
        self.ledger = folder / "calls.jsonl"
        patch = mock.patch.dict(os.environ, {model_ledger.LEDGER_ENV: str(self.ledger)})
        patch.start()
        self.addCleanup(patch.stop)
        model_ledger._KEPT.clear()
        self.addCleanup(model_ledger._KEPT.clear)

    def lines(self):
        return [json.loads(x) for x in self.ledger.read_text().splitlines()]

    def test_an_identical_ask_is_answered_once_and_every_call_is_counted(self):
        spawned = []

        def spawn(argv, env, timeout, prompt):
            spawned.append(prompt)
            return json.dumps({"result": '{"intent": "status"}', "num_turns": 1,
                               "usage": {"input_tokens": 10, "output_tokens": 5}})
        reader = qu.ClaudeUnderstander(model="haiku", spawn=spawn)
        first = reader(SECRET_QUESTION, {}, 1)
        second = reader(SECRET_QUESTION, {}, 1)
        self.assertEqual((first, len(spawned)), (second, 1))
        reader("another question", {}, 1)
        self.assertEqual(len(spawned), 2)
        lines = self.lines()
        self.assertEqual([(l["feature"], l["reused"], l["ok"]) for l in lines],
                         [("query_understanding", False, True),
                          ("query_understanding", True, True),
                          ("query_understanding", False, True)])
        self.assertEqual((lines[0]["turns"], lines[0]["tokens"]), (1, 15))
        self.assertEqual(lines[0]["prompt"], lines[1]["prompt"])
        self.assertNotIn("ledgertestcustomer", self.ledger.read_text())
        self.assertEqual(oct(self.ledger.stat().st_mode & 0o777), "0o600")
        got = model_ledger.summary(lines)["query_understanding"]
        self.assertEqual((got["calls"], got["reused"], got["failed"], got["repeats"]),
                         (3, 1, 0, 0))

    def test_a_failed_call_is_counted_and_never_reused(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(stdout=json.dumps({"is_error": True, "result": "x"}))
        with mock.patch.object(general_answer.subprocess, "run", side_effect=run):
            self.assertIsNone(general_answer.claude_call(SECRET_QUESTION))
            self.assertIsNone(general_answer.claude_call(SECRET_QUESTION))
        self.assertEqual(len(calls), 2)
        lines = self.lines()
        self.assertEqual([(l["feature"], l["ok"], l["reused"]) for l in lines],
                         [("general_answer", False, False)] * 2)
        # the same prompt asked twice without a reuse: a repeat the summary counts
        self.assertEqual(model_ledger.summary(lines)["general_answer"]["repeats"], 1)

    def test_without_a_ledger_file_nothing_is_written(self):
        with mock.patch.dict(os.environ, {model_ledger.LEDGER_ENV: ""}):
            model_ledger.record("x", seconds=1, ok=True, prompt="p")
        self.assertFalse(self.ledger.exists())


if __name__ == "__main__":
    unittest.main()
