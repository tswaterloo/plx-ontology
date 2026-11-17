"""What became of each prepared autopilot item (``operating.autopilot_outcomes``): one
class per item, the classes sum to the items, an unknown decision is never an accept.
SYNTHETIC items. Evidence label: ``isolated-fixture``."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import time
import unittest

from operating import autopilot_outcomes as ao

NOW = datetime(2026, 11, 15, 21, 0, tzinfo=timezone.utc)


def item(i: int, *, state="open", due=NOW, reason=None, draft="Hello, the order ships Monday."):
    return {"item_id": f"ap_{i}", "autopilot": "teams_reply" if i % 2 else "desk",
            "owner": "o", "state": state, "done_reason": reason,
            "due_at": due.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "created_at": due.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "body": json.dumps({"prepared": {"draft": {"body": draft}}})}


class Classify(unittest.TestCase):
    def test_each_decision_and_state_maps_to_one_class(self):
        old = NOW - timedelta(hours=30)
        cases = [
            (item(1), {"decision": "accept"}, "accepted"),
            (item(2), {"decision": "accept", "text": " Hello,  the order ships Monday. "},
             "accepted"),                                       # whitespace only
            (item(3), {"decision": "accept", "text": "Ships Tuesday."}, "edited"),
            (item(4), {"decision": "edit", "text": "Ships Tuesday."}, "edited"),
            (item(5), {"decision": "reject"}, "rejected"),
            (item(6), {"decision": "snooze", "until": "2026-11-16T09:00:00Z"}, "waiting"),
            (item(7, due=old), {"decision": "snooze", "until": "2026-11-15T09:00:00Z"},
             "ignored"),                                        # snooze ran out, overdue
            (item(8, state="done", reason="owner_replied"), None, "ignored"),
            (item(9, due=old), None, "ignored"),
            (item(10), None, "waiting"),
        ]
        for it, dec, want in cases:
            with self.subTest(item=it["item_id"]):
                self.assertEqual(ao.classify(it, dec, NOW)[0], want)

    def test_unknown_input_fails_closed_never_an_accept(self):
        self.assertEqual(ao.classify(item(1), {"decision": "approve"}, NOW),
                         ("ignored", "unknown"))
        self.assertEqual(ao.classify(item(2, state="weird"), None, NOW), ("ignored", "unknown"))
        broken = {**item(3), "due_at": None, "created_at": "not a time"}
        self.assertEqual(ao.classify(broken, None, NOW), ("ignored", "unknown"))


class Measure(unittest.TestCase):
    def test_classes_sum_to_items_at_1_10_60_and_shares_over_decided(self):
        words = [None, {"decision": "accept"}, {"decision": "edit", "text": "x"},
                 {"decision": "reject"}]
        for n in (1, 10, 60):
            with self.subTest(n=n):
                items = [item(i, due=NOW - timedelta(hours=30 if i % 3 == 0 else 0))
                         for i in range(n)]
                decisions = {f"ap_{i}": words[i % 4] for i in range(n) if words[i % 4]}
                start = time.perf_counter()
                got = ao.measure(items, decisions, NOW)
                self.assertLess(time.perf_counter() - start, 0.5)
                self.assertEqual(got["items"], n)
                self.assertEqual(sum(got[c] for c in ao.CLASSES), n)
                self.assertEqual(got["decided"], n - got["waiting"])
                self.assertEqual(sum(v["items"] for v in got["by_autopilot"].values()), n)
                if got["decided"]:
                    total = sum(float(v) for v in got["shares"].values())
                    self.assertAlmostEqual(total, 100.0, delta=0.21)   # 4 shares rounded

    def test_no_items_has_no_shares(self):
        got = ao.measure([], {}, NOW)
        self.assertEqual(got["items"], 0)
        self.assertEqual(set(got["shares"].values()), {None})


if __name__ == "__main__":
    unittest.main()
