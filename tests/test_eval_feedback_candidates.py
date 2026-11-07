"""Eval candidates are made offline from the stored feedback, and a candidate matching a
held-out item is dropped before anything is written."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evals import partition
from operator_api import feedback as fb
from scripts import eval_feedback_candidates as tool


def answer(store: fb.FeedbackStore, question: str, expected: str, handle: str) -> str:
    return store.add({
        "kind": "answer", "reader": "plx:staff:test", "rating": -1, "question": question,
        "expected": expected, "context": {"flagged": [], "claims": [
            {"i": 0, "text": "made-up claim", "citations": [
                {"system": "BC", "handle": handle, "label": "PO"}]}]}})["id"]


class OfflineCandidates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.dir = self.state / "feedback"
        store = fb.FeedbackStore(self.dir)
        self.seen = answer(store, "when is PO 70001 due", "2026-12-01",
                           "bc://plx/purchaseOrders/70001")
        self.held = answer(store, "what did PO 70002 cost", "$1,234.50",
                           "bc://plx/purchaseOrders/70002")
        store.close()

    def test_a_held_out_match_is_dropped_before_writing(self):
        built = tool.build(tool.read_feedback(self.dir))
        held = next(c for c in built if c["feedback_id"] == self.held)
        self.assertEqual((held["key"]["facts"][0]["kind"], held["key"]["facts"][0]["value"]),
                         ("number", "1234.50"))
        digests = Path(self.tmp.name) / "held.json"
        digests.write_text(json.dumps({"key_sha256": sorted(
            partition.key_digests(tool.as_item(held)))}), "utf-8")
        done = tool.run(self.state, digest_files=[digests])
        self.assertEqual((done["written"], done["held_out_dropped"]), (1, 1))
        path = self.dir / "eval-candidates.json"
        written = path.read_text("utf-8")
        self.assertNotIn("70002", written)
        self.assertEqual([c["feedback_id"] for c in json.loads(written)["candidates"]],
                         [self.seen])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_no_held_out_set_writes_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in ("PLX_EVAL_HELD_OUT_DIR", "PLX_EVAL_CREDENTIALED"):
                os.environ.pop(name, None)
            with mock.patch("evals.runner.held_out_root",
                            side_effect=RuntimeError("not credentialed")):
                self.assertEqual(tool.main(["--state-dir", str(self.state)]), 2)
        self.assertFalse((self.dir / "eval-candidates.json").exists())


if __name__ == "__main__":
    unittest.main()
