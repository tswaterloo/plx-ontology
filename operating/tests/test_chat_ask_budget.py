"""One triage call fits the model's context (``chat_ask_model.pack``): a big batch is split
into calls under ``PROMPT_CHARS``, a message too big alone is cut and says what was left
out, none is lost or doubled. Evidence: isolated-fixture."""
from __future__ import annotations

import unittest

from operating import chat_ask_model as cam


def message(i: int, chars: int, *, files: int = 0) -> dict:
    return {"reader": "r", "text": f"m{i} " + "x" * chars, "full_text": f"m{i} " + "x" * chars,
            "attachments": [f"f{n}: " + "y" * 20000 for n in range(files)],
            "earlier": [f"e{n} " + "z" * 12000 for n in range(40)] if files else []}


class Budget(unittest.TestCase):
    def test_cardinality_every_message_in_exactly_one_call_each_call_within_budget(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                batch = [message(i, 11000) for i in range(n)]
                calls = cam.pack(batch, 100_000)
                seen = [i for idx, _m in calls for i in idx]
                self.assertEqual(seen, list(range(n)))
                for idx, msgs in calls:
                    self.assertEqual(len(idx), len(msgs))
                    self.assertLessEqual(len(cam.prompt_of(msgs)), 100_000 + 2_000)
                if n > 10:
                    self.assertGreater(len(calls), 1)

    def test_one_huge_message_is_cut_and_says_what_was_left_out(self):
        calls = cam.pack([message(0, 11000, files=30)], 100_000)
        (_idx, (got,)), = calls
        self.assertTrue(got["left_out"])
        self.assertIn("left_out", cam.prompt_of([got]))
        self.assertLessEqual(len(cam.prompt_of([got])), 100_000 + 2_000)

    def test_a_fitting_message_is_untouched(self):
        (_i, (got,)), = cam.pack([message(0, 500)])
        self.assertNotIn("left_out", got)


if __name__ == "__main__":
    unittest.main()
