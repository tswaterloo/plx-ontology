"""A main screen that times out serves that reader's last good page, marked with its age,
never another reader's; with none, the busy refusal stands. Dates a person reads are
Eastern; the briefing's date is the business day."""
from __future__ import annotations

import contextlib
from collections import namedtuple
import os
import threading
import time
import unittest
from concurrent.futures import Future
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest import mock

from operator_api import backend as be
from operator_api import errors
from operator_api import models as m


def reader(*principals: str, mode: str = "dev"):
    return SimpleNamespace(key=",".join(sorted(principals)), mode=mode,
                           display_name="Test Reader")


def page(late_total: int = 1) -> m.LateList:
    return m.LateList(total=late_total, offset=0, items=[], more=False, next_offset=0)


def policy(*markings: str, unrestricted: bool = False):
    """A fresh read policy (a new object, as each request reads one)."""
    return SimpleNamespace(tenant="t", audiences={}, units={}, unrestricted=unrestricted,
                           markings={mark: ["grp-a"] for mark in markings})


Gated = namedtuple("Gated", "tenant principals clearance")


class Reads:
    """The read worker as ``_serving`` asks it for the reader's access state: the job
    runs on a view holding the policy now; ``stuck`` never runs it (a worker held by a
    long read)."""

    def __init__(self):
        self.policy, self.stuck, self.denied = policy("finance"), False, set()
        self.decided = []

    def submit(self, who, job, *, priority=0, deadline=None):
        future = Future()
        if self.stuck:
            return future
        gated = Gated("t", frozenset(who.key.split(",")), None)
        view = SimpleNamespace(_request=contextlib.nullcontext, access=SimpleNamespace(
            reader_policy=lambda: (gated, self.policy), decide=self.decide))
        try:
            future.set_result(job(view))
        except Exception as error:  # noqa: BLE001
            future.set_exception(error)
        return future

    def decide(self, handles):
        self.decided.append(list(handles))
        return {h: ({"reason": "not_permitted"} if h in self.denied else None)
                for h in handles}


def busy():
    raise TimeoutError("workbench_busy")


class LastGoodPage(unittest.TestCase):
    def setUp(self):
        self.backend = be.WorkspaceBackend.__new__(be.WorkspaceBackend)
        self.backend._good, self.backend._refreshing = {}, set()
        self.backend._lock = threading.RLock()
        self.backend.reads = self.reads = Reads()
        self.backend._after = lambda step: step()   # keep at once, to test what is kept
        env = mock.patch.dict(os.environ, {"PLX_EXCLUDED_PEOPLE_OIDS": ""})
        env.start()
        self.addCleanup(env.stop)

    def serve(self, who, read):
        return self.backend._serving(who, ("late", 0, 25), read)

    def test_a_timeout_serves_the_readers_last_page_with_its_age(self):
        who = reader("grp-a")
        self.assertIsNone(self.serve(who, lambda: page(7)).stale_since)
        calls = []

        def slow():
            calls.append(1)
            raise TimeoutError("workbench_busy")

        got = self.serve(who, slow)
        self.assertEqual(got.total, 7)
        self.assertRegex(got.stale_since, r"^\d{4}-\d\d-\d\dT[\d:]+Z$")
        # the stale page is not kept as a fresh one: a later read-through still sees it
        self.assertIsNone(self.backend._good[(who.key, who.mode, ("late", 0, 25))][1].stale_since)

    def test_never_another_readers_page(self):
        self.serve(reader("grp-a"), lambda: page(7))

        def slow():
            raise TimeoutError("workbench_busy")

        for other in (reader("grp-b"), reader("grp-a", "grp-c"), reader("grp-a", mode="entra")):
            with self.assertRaises(TimeoutError):
                self.serve(other, slow)

    def test_no_page_kept_keeps_the_refusal_and_other_errors_pass_through(self):
        def busy():
            raise errors.BUSY

        with self.assertRaises(errors.UserFacing):
            self.serve(reader("grp-a"), busy)
        self.serve(reader("grp-a"), lambda: page())

        def refused():
            raise errors.ASK_OFF

        with self.assertRaises(errors.UserFacing) as raised:
            self.serve(reader("grp-a"), refused)
        self.assertEqual(raised.exception.code, "ask_not_served")

    def test_a_page_older_than_the_limit_is_not_served(self):
        who = reader("grp-a")
        self.serve(who, lambda: page())
        key = (who.key, who.mode, ("late", 0, 25))
        at, value, access = self.backend._good[key]
        self.backend._good[key] = (at - be.LAST_GOOD_SECONDS - 1, value, access)

        def slow():
            raise TimeoutError("workbench_busy")

        with self.assertRaises(TimeoutError):
            self.serve(who, slow)

    def test_a_served_stale_page_is_refreshed_in_the_background(self):
        who = reader("grp-a")
        self.serve(who, lambda: page(1))
        done = threading.Event()
        state = {"n": 0}

        def read():
            state["n"] += 1
            if state["n"] == 1:
                raise TimeoutError("workbench_busy")
            done.set()
            return page(9)

        self.assertEqual(self.serve(who, read).total, 1)
        self.assertTrue(done.wait(5))
        for _ in range(100):
            if not self.backend._refreshing:
                break
            threading.Event().wait(0.05)
        self.assertEqual(self.serve(who, lambda: page(9)).total, 9)

    def test_unchanged_access_still_serves_the_kept_page(self):
        who = reader("grp-a")
        self.serve(who, lambda: page(5))
        self.reads.policy = policy("finance")   # a new policy object, the same grants
        self.assertEqual(self.serve(who, busy).total, 5)

    def test_a_revoked_marking_with_the_same_principals_refuses_the_kept_page(self):
        who = reader("grp-a")
        self.serve(who, lambda: page(5))
        self.reads.policy = policy()            # finance revoked; principals unchanged
        with self.assertRaises(TimeoutError):
            self.serve(who, busy)

    def test_an_added_exclusion_refuses_the_kept_page(self):
        who = reader("grp-a")
        self.serve(who, lambda: page(5))
        with mock.patch.dict(os.environ, {
                "PLX_EXCLUDED_PEOPLE_OIDS": "22222222-2222-4222-8222-222222222222"}):
            with self.assertRaises(TimeoutError):
                self.serve(who, busy)
        self.assertEqual(self.serve(who, busy).total, 5)   # the exclusion lifted again

    def test_an_access_state_that_cannot_be_read_keeps_the_refusal(self):
        who = reader("grp-a")
        self.serve(who, lambda: page(5))
        self.reads.stuck = True
        with mock.patch.object(be, "ACCESS_WAIT_SECONDS", 0.05):
            with self.assertRaises(TimeoutError):
                self.serve(who, busy)
            # a page read while the state could not be read is never kept to serve stale
            self.serve(who, lambda: page(6))
        self.reads.stuck = False
        with self.assertRaises(TimeoutError):
            self.serve(who, busy)

    def test_a_page_read_across_an_access_change_is_not_kept(self):
        who = reader("grp-a")

        def read_while_revoked():
            self.reads.policy = policy()
            return page(5)

        self.serve(who, read_while_revoked)
        with self.assertRaises(TimeoutError):
            self.serve(who, busy)

    def test_a_kept_page_citing_a_source_now_denied_is_refused(self):
        who = reader("grp-a")
        cited = "graph-mail://box/msg-1@g-1"
        shown = page(5).model_copy(update={"items": [m.LatePo(
            object_id="po-1", label="PO 1", reason="late", open_url="/objects/po-1", sources=[
                m.Source(system="Email", handle=cited, label="Supplier mail")])]})
        self.assertEqual(be.page_handles(shown), {cited})
        self.serve(who, lambda: shown)
        self.assertEqual(self.serve(who, busy).total, 5)       # the source still opens
        self.assertIn([cited], self.reads.decided)
        self.reads.denied.add(cited)                           # the mailbox is revoked
        with self.assertRaises(TimeoutError):
            self.serve(who, busy)

    def test_a_fresh_page_is_returned_without_waiting_to_be_kept(self):
        del self.backend._after                                # the real background keep
        self.reads.stuck = True
        with mock.patch.object(be, "ACCESS_WAIT_SECONDS", 5):
            started = time.monotonic()
            self.assertEqual(self.serve(reader("grp-a"), lambda: page(5)).total, 5)
            self.assertLess(time.monotonic() - started, 1)


class PlainWordsAndBusinessDay(unittest.TestCase):
    def test_times_read_in_eastern(self):
        self.assertEqual(be.eastern_words(datetime(2026, 11, 7, 2, 8, tzinfo=timezone.utc)),
                         "6 Nov 2026 9:08 PM ET")
        self.assertEqual(be.eastern_words(datetime(2026, 7, 1, 16, 0, tzinfo=timezone.utc)),
                         "1 Jul 2026 12:00 PM ET")

    def test_the_briefing_is_dated_the_business_day_it_is_counted_to(self):
        aged = be.briefing_aged({"as_of": "2026-11-06", "items": [
            {"event_type": "po_late", "facts": {"days": 3}}]}, date(2026, 11, 7))
        self.assertEqual(aged["as_of"], "2026-11-07")
        self.assertEqual(aged["items"][0]["facts"]["days"], 4)


if __name__ == "__main__":
    unittest.main()
