"""Contract C1: the value envelope, ``cut`` and ``fail`` (operating/value.py). The TypeScript
side (app/src/lib/value.ts) refuses the same shapes (value.test.ts)."""
import json
import logging
import unittest

from operating import value as v


class EnvelopeTest(unittest.TestCase):
    def test_unknown_or_missing_status_is_refused_never_ok(self):
        for raw in ({"value": 3, "status": "fine"}, {"value": 3}, [], "ok", None,
                    {"value": 3, "status": "ok", "extra": 1}):
            with self.assertRaises(ValueError, msg=raw):
                v.parse(raw)
        with self.assertRaises(ValueError):
            v.Value(3, "done")

    def test_not_ok_without_reason_is_refused(self):
        for status in v.VALUE_STATUSES:
            if status == "ok":
                continue
            with self.assertRaises(ValueError, msg=status):
                v.Value(None, status)
        self.assertEqual(v.Value(None, "stale", as_of="2026-11-23T19:00:00+00:00").status,
                         "stale")

    def test_round_trip_through_json(self):
        got = v.Value({"n": 4}, "partial", 4, "2026-11-23T19:00:00+00:00", "BC not read", "job")
        self.assertEqual(v.parse(json.loads(json.dumps(got.to_json()))), got)

    def test_cut_says_exact_total_reason_and_next(self):
        rows = list(range(81))
        got = v.cut(rows, 12, "page size")
        self.assertEqual(got.status, "cut")
        self.assertEqual(got.total, 81)
        self.assertEqual(got.value, {"items": rows[:12], "total": 81, "next": 12})
        self.assertIn("12 of 81", got.why)
        self.assertIn("page size", got.why)
        whole = v.cut(rows, 100, "page size")
        self.assertEqual((whole.status, whole.total, whole.value["next"]), ("ok", 81, None))
        for n in (1, 10, 60):     # every row kept or counted, none lost or doubled
            got = v.cut(rows[:n], 10, "page size")
            self.assertEqual(len(got.value["items"]) + (n - min(n, 10)), n)
            self.assertEqual(got.total, n)

    def test_listed_refuses_a_silent_cut(self):
        with self.assertRaises(ValueError):
            v.listed([1, 2], total=5, next=2)            # no reason
        with self.assertRaises(ValueError):
            v.listed([1, 2], total=5, why="page size")   # no way to the rest
        with self.assertRaises(ValueError):
            v.listed([1, 2, 3], total=2)                 # fewer than shown

    def test_fail_logs_and_returns_a_typed_failure(self):
        with self.assertLogs("operating.value", logging.WARNING) as logs:
            got = v.fail("Business Central could not be read", RuntimeError("boom\nmore"),
                          source_job="bc-sync")
        self.assertEqual((got.status, got.why, got.value), (
            "failed", "Business Central could not be read", None))
        self.assertIn("RuntimeError: boom", logs.output[0])
        self.assertNotIn("boom", got.why)
        with self.assertRaises(ValueError):
            v.fail("")


if __name__ == "__main__":
    unittest.main()
