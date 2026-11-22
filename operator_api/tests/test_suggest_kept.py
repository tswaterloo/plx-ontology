"""The person's search box keeps a business-table answer for a few seconds, per reader, query
and limit, and only while the reader's access state and the served inputs are those it was
read under. isolated-fixture."""
from __future__ import annotations

import threading
import time
import unittest

from operator_api import models as m
from operator_api.backend_serving import ServingMixin


def answer(label="Croda", source="business") -> m.SuggestResponse:
    return m.SuggestResponse(query="croda", source=source, took_ms=300, suggestions=[
        m.Suggestion(object_id="h1", type="Customer", label=label, match="exact",
                     score=1.0, open_url="/account/customer?h=h1")])


class Host(ServingMixin):
    def __init__(self):
        self._lock = threading.RLock()
        self.reads, self.access, self.inputs = 0, ("p1", False, "e1", "m1"), ("i1",)
        self.source, self.label = "business", "Croda"

    def _access_now(self, reader, screen=None):
        if isinstance(self.access, Exception):
            raise self.access
        return self.access

    def inputs_stamp(self):
        return self.inputs

    def _suggest(self, reader, q, limit, *, person=False):
        self.reads += 1
        return answer(self.label, self.source)


def who(key="employee:a", view_as=False):
    session = type("S", (), {"view_as": view_as})()
    return type("R", (), {"key": key, "mode": "m", "session": session})()


class SuggestKept(unittest.TestCase):
    def test_a_repeat_is_answered_from_the_copy_with_its_own_time(self):
        h, a = Host(), who()
        first = h.suggest(a, "croda", 20, person=True)
        again = h.suggest(a, "croda", 20, person=True)
        self.assertEqual(h.reads, 1)
        self.assertEqual(again.suggestions, first.suggestions)
        self.assertLess(again.took_ms, 300)

    def test_another_reader_query_or_limit_reads_itself(self):
        h = Host()
        h.suggest(who("employee:a"), "croda", 20, person=True)
        h.suggest(who("employee:b"), "croda", 20, person=True)
        h.suggest(who("employee:a"), "croda", 8, person=True)
        h.suggest(who("employee:a"), "croda x", 20, person=True)
        self.assertEqual(h.reads, 4)

    def test_a_moved_access_state_or_input_reads_again_and_an_unreadable_one_never_keeps(self):
        for moved in (("p2", False, "e1", "m1"), ("p1", False, "e2", "m1"), ("p1", False, "e1", "m2")):
            with self.subTest(access=moved):
                h, a = Host(), who()
                h.suggest(a, "croda", 20, person=True)
                h.access = moved
                h.suggest(a, "croda", 20, person=True)
                self.assertEqual(h.reads, 2)
        h, a = Host(), who()
        h.suggest(a, "croda", 20, person=True)
        h.inputs = ("i2",)
        h.suggest(a, "croda", 20, person=True)
        self.assertEqual(h.reads, 2)
        for bad in (None, RuntimeError("undecided")):
            h, a = Host(), who()
            h.access = bad
            if isinstance(bad, Exception):
                with self.assertRaises(RuntimeError):
                    h.suggest(a, "croda", 20, person=True)
            else:
                h.suggest(a, "croda", 20, person=True)
                h.access = ("p1", False, "e1", "m1")
                h.suggest(a, "croda", 20, person=True)
                self.assertEqual(h.reads, 2)   # nothing was kept for the unread state

    def test_a_state_that_moved_during_the_read_keeps_nothing(self):
        h, a = Host(), who()
        real = h._suggest

        def moving(*args, **kw):
            out = real(*args, **kw)
            h.access = ("p2", False, "e1", "m1")   # a grant revoked while it was read
            return out
        h._suggest = moving
        h.suggest(a, "croda", 20, person=True)
        h.suggest(a, "croda", 20, person=True)
        self.assertEqual(h.reads, 2)

    def test_only_a_business_answer_for_a_person_is_kept_and_a_viewing_as_session_nothing(self):
        h, a = Host(), who()
        h.source = "search_entities"   # item-level rows: decided per call
        h.suggest(a, "croda", 20, person=True)
        h.suggest(a, "croda", 20, person=True)
        self.assertEqual(h.reads, 2)
        h = Host()
        h.suggest(a, "croda", 20)   # not the person's box (a model's lookup)
        h.suggest(a, "croda", 20)
        v = who(view_as=True)
        h.suggest(v, "croda", 20, person=True)
        h.suggest(v, "croda", 20, person=True)
        self.assertEqual(h.reads, 4)

    def test_old_copies_expire_and_the_store_stays_small(self):
        h, a = Host(), who()
        h.SUGGEST_KEPT_SECONDS = 0.05
        h.suggest(a, "croda", 20, person=True)
        time.sleep(0.08)
        h.suggest(a, "croda", 20, person=True)
        self.assertEqual(h.reads, 2)
        h.SUGGEST_KEPT_SECONDS = 20.0
        for i in range(600):
            h.suggest(a, f"q{i}", 20, person=True)
        self.assertLessEqual(len(h._suggest_kept), h.SUGGEST_KEPT_MOST)


if __name__ == "__main__":
    unittest.main()
