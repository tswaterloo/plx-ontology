"""Assist eval scoring: facts present by how they were settled, wrong facts counted."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from evals import assist_eval


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


if __name__ == "__main__":
    unittest.main()
