"""Regression tests for the CORE-001..008 audit fixes (contract dialect, timers, actor, intervals)."""
from __future__ import annotations

import signal
import time
import unittest

from ontology import json_contract, store
from ontology.json_contract import SchemaError, validate
from ontology.types import Actor
from operating import call_limits


class ContractTests(unittest.TestCase):
    def test_oversized_unique_array_rejects_fast(self):
        schema = {"type": "array", "maxItems": 10, "uniqueItems": True}
        started = time.monotonic()
        errors = validate(list(range(6000)), schema)
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertTrue(any("more than 10" in e for e in errors))
        self.assertFalse(any("not unique" in e for e in errors))

    def test_unique_items_still_detects_duplicates(self):
        schema = {"type": "array", "uniqueItems": True}
        self.assertTrue(validate([1, 2, 1], schema))
        self.assertTrue(validate([{"a": [1, 2]}, {"a": [1, 2]}], schema))
        self.assertTrue(validate([1, 1.0], schema))
        self.assertFalse(validate([1, True, "1"], schema))
        self.assertFalse(validate([[True], [1]], schema))

    def test_bool_and_number_differ_at_every_depth(self):
        self.assertFalse(validate([True], {"const": [True]}))
        self.assertTrue(validate([1], {"const": [True]}))
        self.assertTrue(validate({"a": [False]}, {"enum": [{"a": [0]}]}))
        self.assertFalse(validate({"a": [0]}, {"enum": [{"a": [0.0]}]}))

    def test_pointer_keeps_empty_parts(self):
        schema = {"$defs": {"": {"type": "string"}, "a": {"": {"type": "integer"}}},
                  "properties": {"x": {"$ref": "#/$defs/"}, "y": {"$ref": "#/$defs/a/"}}}
        self.assertFalse(validate({"x": "s", "y": 3}, schema))
        self.assertTrue(validate({"x": 1}, schema))
        with self.assertRaises(SchemaError):
            json_contract._pointer(schema, "#/$defs//missing")
        with self.assertRaises(SchemaError):
            json_contract._pointer(schema, "#nope")


class TimerTests(unittest.TestCase):
    def setUp(self):
        self._old = signal.signal(signal.SIGALRM, lambda *_: None)

    def tearDown(self):
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self._old)

    def test_nested_bound_keeps_outer_timer(self):
        signal.setitimer(signal.ITIMER_REAL, 30)
        with call_limits.bounded(5):
            pass
        left, _ = signal.getitimer(signal.ITIMER_REAL)
        self.assertGreater(left, 25)
        self.assertLessEqual(left, 30)

    def test_no_outer_timer_leaves_none(self):
        with call_limits.bounded(5):
            pass
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)


class ActorTests(unittest.TestCase):
    def test_actor_does_not_keep_callers_set(self):
        mine = {"a", "b"}
        actor = Actor(tenant="t", principals=mine)
        mine.add("root")
        self.assertEqual(actor.principals, frozenset({"a", "b"}))
        self.assertIsInstance(actor.principals, frozenset)

    def test_generator_principals_survive(self):
        actor = Actor(tenant="t", principals=(p for p in ("a", "b")))
        self.assertEqual(actor.principals, frozenset({"a", "b"}))


class IntervalTests(unittest.TestCase):
    def test_equal_start_open_and_closed(self):
        out = store.held_stretches("2026-01-01", None,
                                   [("2026-02-01", None), ("2026-02-01", "2026-03-01")])
        self.assertEqual(out, [("2026-01-01", "2026-02-01")])
        out2 = store.held_stretches("2026-01-01", None,
                                    [("2026-02-01", "2026-03-01"), ("2026-02-01", None)])
        self.assertEqual(out2, out)

    def test_nested_and_adjacent(self):
        removed = [("2026-02-01", "2026-05-01"), ("2026-03-01", "2026-04-01"),
                   ("2026-05-01", "2026-06-01")]
        self.assertEqual(store.held_stretches("2026-01-01", "2026-09-01", removed),
                         [("2026-01-01", "2026-02-01"), ("2026-06-01", "2026-09-01")])

    def test_equal_start_closed_pair_then_open_later(self):
        removed = [("2026-02-01", "2026-03-01"), ("2026-02-01", "2026-04-01"),
                   ("2026-06-01", None)]
        self.assertEqual(store.held_stretches("2026-01-01", None, removed),
                         [("2026-01-01", "2026-02-01"), ("2026-04-01", "2026-06-01")])

    def test_small_years_are_four_wide(self):
        for year in (1, 999, 1000):
            self.assertTrue(store.instant(f"{year:04d}-05-06T00:00:00Z")
                            .startswith(f"{year:04d}-05-06T"), year)


if __name__ == "__main__":
    unittest.main()
