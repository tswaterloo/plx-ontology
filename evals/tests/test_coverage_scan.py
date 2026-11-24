"""PB7 coverage scan (evals/coverage_scan.py): every stored answer carries its parts and
what read each; tag and verdict words in an answer's text are counted."""
import json
import tempfile
import unittest
from pathlib import Path

from evals import coverage_scan as cs


class Scan(unittest.TestCase):
    def test_answers_without_coverage_or_with_verdict_words_fail(self):
        with tempfile.TemporaryDirectory() as root:
            held = Path(root, "runs", "held")
            held.mkdir(parents=True)
            part = {"part": "late POs", "sources": ["purchase_orders"], "reads": 0, "found": 0}
            (held / "a.json").write_text(json.dumps({"text": "Two late.", "coverage": [part]}))
            (held / "b.json").write_text(json.dumps({"text": "One (unverified)."}))
            (held / "c.json").write_text(json.dumps({"text": "BC is the system of record.",
                                                     "coverage": []}))
            failures, n, covered, worded = cs.scan([root], words=True)
        self.assertEqual((n, covered, worded), (3, 2, 2))
        self.assertTrue(any("b: no coverage" in f for f in failures))


if __name__ == "__main__":
    unittest.main()
