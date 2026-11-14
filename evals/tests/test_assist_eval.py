"""Assist eval scoring: facts present by how they were settled, wrong facts counted."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from evals import assist_eval
from operating import assist


def _mail(handle: str, body: str) -> dict:
    return {"role": "sales", "sender": "s", "handle": handle, "at": "2026-10-10T12:00:00Z",
            "to": ["a@example.com"], "cc": [], "subject": "Order", "body": body,
            "intent": "email A: order", "outside": True}


def _draft(handle: str, settled: list[str], text: str) -> dict:
    return {"handle": handle, "role": "sales", "settled": settled,
            "agent_calls": int("agent" in settled),
            "draft": {"to": ["a@example.com"], "body": text, "questions": ["status of X"],
                      "facts": [{"text": text}]}}


class ScoreTest(unittest.TestCase):
    def test_fast_share_excludes_agent_answers_and_conflicts_count_wrong(self):
        mails = [_mail("h1", "Order SO-1001 shipped."), _mail("h2", "Order SO-1002 shipped."),
                 _mail("h3", "Order SO-1003 is on hold.")]
        drafts = [_draft("h1", ["fast"], "Order SO-1001 shipped."),
                  _draft("h2", ["agent"], "Order SO-1002 shipped."),
                  _draft("h3", ["fast"], "Order SO-1003 was released.")]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "sample.jsonl").write_text("".join(json.dumps(m) + "\n" for m in mails))
            (out / "drafts.jsonl").write_text("".join(json.dumps(d) + "\n" for d in drafts))
            (out / "sample-counts.json").write_text(json.dumps({"sales": {"self_started": 3}}))
            role = assist_eval.score(out)["roles"]["sales"]
        self.assertEqual((role["facts_present"]["k"], role["facts_present"]["n"]), (3, 3))
        self.assertEqual(role["facts_present_fast"]["k"], 2)
        self.assertEqual(role["drafts_with_conflict"], 1)
        self.assertEqual(role["facts_wrong"]["k"], 1)
        self.assertEqual(role["usable"]["k"], 2)



class RecipientsOutcomeTest(unittest.TestCase):
    """Each recipient line is right (filled in, right), asked (left to a question,
    nothing wrong filled in) or wrong (a wrong address filled in, or one silently
    missing)."""

    MAIL = {"to": ["a@x.com", "b@x.com"], "cc": ["c@plx.test"]}

    def judge(self, draft, after=None):
        return assist_eval.recipients_outcome(self.MAIL, draft, after)

    def test_right_asked_wrong(self):
        right = self.judge({"to": ["a@x.com", "b@x.com"], "cc": ["c@plx.test"]})
        self.assertEqual((right["primary"], right["to"], right["cc"]),
                         ("right", "right", "right"))
        # b silently missing on To, c silently missing on Cc: wrong, though a is right
        missing = self.judge({"to": ["a@x.com"], "cc": []})
        self.assertEqual((missing["primary"], missing["to"], missing["cc"]),
                         ("right", "wrong", "wrong"))
        # asked about b and c: asked, with the real lines among the candidates
        asked = self.judge({"to": ["a@x.com"], "cc": [], "ask": [
            {"field": "to", "pick": "any", "candidates": ["b@x.com"]},
            {"field": "cc", "pick": "any", "candidates": ["z@plx.test"]}]})
        self.assertEqual((asked["to"], asked["to_hit"], asked["cc"], asked["cc_hit"]),
                         ("asked", True, "asked", False))
        # a wrong address filled in is wrong whatever else is asked
        wrong = self.judge({"to": ["z@x.com"], "ask": [
            {"field": "to", "pick": "one", "candidates": ["a@x.com"]}]})
        self.assertEqual((wrong["primary"], wrong["to"]), ("wrong", "wrong"))
        # nobody found and nothing offered: the draft says so, asked, never a hit
        none = self.judge({"to": [], "unresolved": ["X"]})
        self.assertEqual((none["primary"], none["primary_hit"]), ("asked", False))

    def test_the_lines_after_the_pick_are_judged_to_at_best_asked(self):
        draft = {"to": [], "ask": [{"field": "to", "pick": "one",
                                    "candidates": ["a@x.com", "q@x.com"]}]}
        after = {"to": ["a@x.com", "b@x.com"], "cc": ["c@plx.test"]}
        got = self.judge(draft, after)
        self.assertEqual((got["primary"], got["primary_hit"], got["to"], got["to_hit"],
                          got["cc"]), ("asked", True, "asked", True, "right"))
        got = self.judge(draft, {"to": ["a@x.com"], "cc": []})
        self.assertEqual((got["to"], got["cc"]), ("wrong", "wrong"))
        # a wrong Cc filled in before the pick stays wrong whatever the pick brings
        got = self.judge(dict(draft, cc=["z@plx.test"]), after)
        self.assertEqual(got["cc"], "wrong")

    def test_a_block_that_is_not_an_object_says_nothing_found(self):
        class Ran:
            found = {}
            agent_text = "x\n```json\n[1, 2]\n```"
        self.assertIsNone(assist.found_of(Ran(), []))
        self.assertEqual(assist.found_of(None, ["not_resolved"]), "none")


if __name__ == "__main__":
    unittest.main()
