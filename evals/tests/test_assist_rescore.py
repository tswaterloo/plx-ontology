"""The Assist re-score's judge stage: what the model answers is read fail closed, and the
planted known-wrong pairs measure the judge in every batch."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from evals import assist_rescore as rs


def _row(i: int) -> dict:
    return {"pair": f"p{i}", "item_id": f"i{i}", "action": f"a{i}", "role": "r",
            "autopilot": "reply_prep", "prepared": f"label: reply\nsummary: answer question {i}",
            "screened": "pay_or_hr" if i == 3 else None,
            "trigger": {"channel": "mail", "text": f"question {i}?", "conversation": f"c{i}"},
            "sent": {"kind": "mail_sent", "text": f"answer {i}", "conversation": f"c{i}"}}


class JudgeTest(unittest.TestCase):
    def test_answers_other_than_yes_or_no_are_errors_that_cover_nothing(self):
        got = rs.parse_verdicts('x [{"id": "a", "agrees": "Yes"}, {"id": "b", "agrees": "maybe"}]',
                                ["a", "b", "c"])
        self.assertEqual(got["a"]["agrees"], "yes")
        self.assertEqual(got["b"], {"error": "bad_answer"})
        self.assertEqual(got["c"], {"error": "unanswered"})
        self.assertTrue(all(v["error"] == "unanswered" for v in rs.parse_verdicts("no json", ["a", "b"]).values()))

    def test_planted_wrong_pairs_are_counted_and_never_cover(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / "pairs.jsonl").write_text("".join(json.dumps(_row(i)) + "\n" for i in range(6)))
            asked = []

            def yes_to_all(prompt: str, model: str):
                ids = [json.loads(l)["id"] for l in prompt.splitlines() if l.startswith('{"id"')]
                asked.append(ids)
                return json.dumps([{"id": i, "agrees": "yes"} for i in ids]), 0.01
            rs.cmd_judge(SimpleNamespace(out=str(out), batch=10, probes=4, model="m"), call=yes_to_all)
            rows = [json.loads(l) for l in open(out / "judged.jsonl")]
            planted = [r for r in rows if r.get("probe")]
            self.assertEqual(len(planted), 4)
            self.assertTrue(all(r["expect"] == "no" for r in planted))
            # the screened pair never reaches the judge; planted pairs never count
            self.assertNotIn("p3", asked[0])
            self.assertEqual(rs.agreed_pairs(out), {"p0", "p1", "p2", "p4", "p5"})
            self.assertEqual(os.stat(out / "judged.jsonl").st_mode & 0o777, 0o600)
            courtesy = [r for r in rows if r.get("probe") == "courtesy"]
            self.assertTrue(courtesy)

    def test_prepared_text_keeps_the_items_words_not_its_references(self):
        item = {"body": {"label": "Work a message left you", "prepared": {
            "summary": "Send invoice IN1 to Luna", "links": [{"href": "/object/x", "label": "Open"}],
            "todos": [{"what": "send invoice IN1", "record": "Email:graph_mail.messages:{\"id\": 1}"}],
            "draft": {"body": "Hi, attached.", "recipients": {"source": {"a@b.c": "api://x"}}}}}}
        text = rs.prepared_text(item)
        self.assertIn("summary: Send invoice IN1 to Luna", text)
        self.assertIn("what: send invoice IN1", text)
        self.assertIn("body: Hi, attached.", text)
        self.assertNotIn("api://", text)
        self.assertNotIn("/object/", text)


if __name__ == "__main__":
    unittest.main()
