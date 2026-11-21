"""The Insights tiles' stamps (``insights_cache.door_stamp``, ``insights_fast.Fast.stamp``)
are read once for the tiles that ask at the same moment, and never answered from a read
that started before the caller asked (a door or exclusion change made before the ask is
always seen). Evidence label: ``isolated-fixture`` (stand-in connections, no server)."""

from __future__ import annotations

import threading
import time
import unittest
from contextlib import contextmanager
from unittest import mock

from operator_api import insights_cache, insights_fast
from operator_api.shared_read import SharedRead


class SharedReadTests(unittest.TestCase):
    def test_a_caller_never_gets_a_read_that_started_before_it_asked(self):
        value = {"v": 0}
        started = threading.Event()
        release = threading.Event()
        reads = []

        def read():
            seen = value["v"]   # what the store holds when the read starts
            reads.append(seen)
            if len(reads) == 1:
                started.set()
                release.wait(5)
            return seen
        shared = SharedRead(read)
        got = {}
        first = threading.Thread(target=lambda: got.__setitem__("first", shared()))
        first.start()
        self.assertTrue(started.wait(5))
        value["v"] = 1   # changed while the first read is under way, before the next ask
        late = [threading.Thread(target=lambda i=i: got.__setitem__(i, shared()))
                for i in range(10)]
        for t in late:
            t.start()
        time.sleep(0.05)
        release.set()
        first.join(5)
        for t in late:
            t.join(5)
        self.assertEqual(got["first"], 0)
        self.assertEqual({got[i] for i in range(10)}, {1})   # all saw the change
        self.assertEqual(reads, [0, 1])   # ten late callers: one read between them

    def test_a_failed_read_fails_every_caller_it_served_and_the_next_ask_reads_again(self):
        calls = []

        def read():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("unreadable")
            return 7
        shared = SharedRead(read)
        with self.assertRaises(OSError):
            shared()
        self.assertEqual(shared(), 7)
        self.assertEqual(len(calls), 2)


class _Conn:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TileStampTests(unittest.TestCase):
    def _burst(self, stamp, n):
        out, gate = [], threading.Barrier(n)

        def one():
            gate.wait(5)
            out.append(stamp())
        threads = [threading.Thread(target=one) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        return out

    def test_a_visits_tiles_share_the_door_stamp_read(self):
        log = []

        def check(pg, schema):
            log.append(schema)
            time.sleep(0.02)
            return (41, True)
        with mock.patch("operating.pg.door.check", check):
            stamp = insights_cache.door_stamp(lambda: _Conn(log))
            for n in (1, 10, 60):
                log.clear()
                got = self._burst(stamp, n)
                self.assertEqual(got, [41] * n)            # no tile lost or answered wrong
                self.assertLessEqual(len(log), 2 if n > 1 else 1)

    def test_a_door_that_does_not_serve_serves_nothing_kept_to_any_tile(self):
        with mock.patch("operating.pg.door.check", lambda pg, schema: (41, False)):
            stamp = insights_cache.door_stamp(lambda: _Conn([]))
            self.assertEqual(self._burst(stamp, 10), [None] * 10)

    def test_the_exclusion_stamp_is_shared_and_an_unreadable_one_is_unserved(self):
        reads = []

        def exclusion(pg):
            reads.append(1)
            time.sleep(0.02)
            return ("x", 3)
        fast = insights_fast.Fast(lambda: _Conn([]), lambda: [])
        with mock.patch.object(insights_fast, "exclusion_stamp", exclusion), \
                mock.patch.object(insights_fast, "hidden_json", lambda: "[]"):
            got = self._burst(fast.stamp, 22)
            self.assertEqual(len(set(got)), 1)
            self.assertEqual(len(got), 22)
            self.assertLessEqual(len(reads), 2)
        with mock.patch.object(insights_fast, "exclusion_stamp",
                               mock.Mock(side_effect=OSError("gone"))):
            with self.assertRaises(insights_fast.Unserved):
                fast.stamp()
        with mock.patch.object(insights_fast, "exclusion_stamp", lambda pg: None):
            with self.assertRaises(insights_fast.Unserved):
                fast.stamp()

    def test_the_hidden_digest_follows_the_set(self):
        a, b = '["o1"]', '["o1","o2"]'
        self.assertEqual(insights_fast.hidden_digest(a), insights_fast.hidden_digest(a))
        self.assertNotEqual(insights_fast.hidden_digest(a), insights_fast.hidden_digest(b))
        same_text_new_object = "".join(['["o1"', "]"])
        self.assertEqual(insights_fast.hidden_digest(same_text_new_object),
                         insights_fast.hidden_digest(a))


if __name__ == "__main__":
    unittest.main()
