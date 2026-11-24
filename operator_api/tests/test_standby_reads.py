"""The warm-up reads the hot standby only while it is the primary's replica and current;
anything else (no standby named, unreadable, behind, promoted) keeps the primary."""

import threading
import unittest

from operator_api import standby_reads as sr


class Conn:
    def __init__(self, answers, log):
        self.answers, self.log = answers, log

    def execute(self, sql):
        self.log.append(sql)
        got = self.answers(sql)
        if isinstance(got, Exception):
            raise got

        class Cur:
            def fetchone(self_inner):
                return got
        return Cur()

    def close(self):
        pass


def lag_for(standby_row, gap, clock=None, log=None):
    log = [] if log is None else log

    def connect(dsn):
        if dsn == "standby":
            return Conn(lambda sql: standby_row, log)
        return Conn(lambda sql: (gap,) if not isinstance(gap, Exception) else gap, log)
    return sr.Lag("primary", "standby", connect=connect,
                  clock=clock or (lambda: 0.0)), log


ENV = {sr.DSN_ENV: "standby"}


class Choose(unittest.TestCase):
    def test_the_standby_only_when_named_answering_in_recovery_and_not_stale(self):
        cases = [
            ({}, (True, "61F/A4E1E208"), 0, "primary"),                       # none named
            (ENV, (True, "61F/A4E1E208"), 1024, "standby"),                   # current
            # a write burst's minute behind (11-22: 0.3-1.1 GB): still the standby, each
            # read-ahead after the first pass waits for it (``wait_current``)
            (ENV, (True, "61F/A4E1E208"), sr.MAX_BEHIND_BYTES + 1, "standby"),
            (ENV, (True, "61F/A4E1E208"), 1100 * 1024 * 1024, "standby"),
            (ENV, (True, "61F/A4E1E208"), sr.STALE_BYTES + 1, "primary"),     # stuck
            (ENV, (False, "61F/A4E1E208"), 0, "primary"),                     # promoted copy
            (ENV, RuntimeError("no tunnel"), 0, "primary"),                   # unreadable
            (ENV, (True, "61F/A4E1E208"), RuntimeError("primary"), "primary"),
            (ENV, (True, "1/2'; DROP TABLE x; --"), 0, "primary"),             # not a position
        ]
        for env, row, gap, want in cases:
            lag, _ = lag_for(row, gap)
            dsn, held = sr.choose("primary", env, lag=lag)
            self.assertEqual(dsn, want, (env, row, gap))
            self.assertEqual(held is not None, want == "standby")

    def test_no_primary_or_the_same_dsn_reads_as_before(self):
        lag, log = lag_for((True, "1/2"), 0)
        self.assertEqual(sr.choose(None, ENV, lag=lag), (None, None))
        self.assertEqual(sr.choose("standby", ENV, lag=lag), ("standby", None))
        self.assertEqual(log, [])   # nothing asked

    def test_behind_is_read_at_most_every_poll(self):
        now = [0.0]
        lag, log = lag_for((True, "1/2"), 5, clock=lambda: now[0])
        lag.behind(); lag.behind()
        self.assertEqual(len(log), 2)          # one query on each side
        now[0] += sr.POLL_SECONDS
        lag.behind()
        self.assertEqual(len(log), 4)


class WaitCurrent(unittest.TestCase):
    def test_a_read_ahead_waits_while_behind_then_goes_on(self):
        gaps = iter([sr.MAX_BEHIND_BYTES * 2, sr.MAX_BEHIND_BYTES * 2, 10])

        class FakeLag:
            def current(self):
                return next(gaps) <= sr.MAX_BEHIND_BYTES
        now = [0.0]
        slept = []
        self.assertTrue(sr.wait_current(FakeLag(), seconds=60, sleep=lambda s: (
            slept.append(s), now.__setitem__(0, now[0] + s)), clock=lambda: now[0]))
        self.assertEqual(len(slept), 2)

    def test_the_wait_is_bounded_and_ends_on_stop(self):
        class Behind:
            def current(self):
                return False
        now = [0.0]
        tick = lambda s: now.__setitem__(0, now[0] + s)
        self.assertFalse(sr.wait_current(Behind(), seconds=10, sleep=tick,
                                         clock=lambda: now[0]))
        self.assertLessEqual(now[0], 10 + sr.POLL_SECONDS)
        stop = threading.Event()
        stop.set()
        self.assertFalse(sr.wait_current(Behind(), stop, seconds=600, sleep=tick,
                                         clock=lambda: now[0]))
        self.assertTrue(sr.wait_current(None))   # the primary: never waits


if __name__ == "__main__":
    unittest.main()
