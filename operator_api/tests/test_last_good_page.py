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
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote

from operator_api import backend as be
from operator_api import errors
from operator_api import models as m
from operating import autopilot as ap


def reader(*principals: str, mode: str = "dev"):
    return SimpleNamespace(key=",".join(sorted(principals)), mode=mode,
                           display_name="Test Reader", session=SimpleNamespace())


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
        self.decided, self.hidden, self.looked_up = [], set(), []

    def submit(self, who, job, *, priority=0, deadline=None):
        future = Future()
        if self.stuck:
            return future
        gated = Gated("t", frozenset(who.key.split(",")), None)
        view = SimpleNamespace(_request=contextlib.nullcontext, access=SimpleNamespace(
            reader_policy=lambda: (gated, self.policy), decide=self.decide,
            visible_objects=self.visible_objects))
        try:
            future.set_result(job(view))
        except Exception as error:  # noqa: BLE001
            future.set_exception(error)
        return future

    def visible_objects(self, ids):
        """The records the reader may read now, of ``ids``, in one batch (as the case
        host decides them for ``plx.object``)."""
        self.looked_up.append(sorted(ids))
        return frozenset(i for i in ids if i not in self.hidden)

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

    def test_an_unreadable_page_is_never_kept_and_the_kept_pages_stay_bounded(self):
        """API-012: a page whose status says it could not be read is not kept as last
        good. API-002: pages and their inputs stamps are evicted together, bounded."""
        state = ("s",)
        for status in ("unavailable", "error", "failed"):
            bad = page(3).model_copy()
            object.__setattr__(bad, "status", status)
            self.backend._keep_good(("k", status), bad, state, state)
            self.assertNotIn(("k", status), self.backend._good)
        # a record page's status is the record's own state (an ObjectStatus, unhashable),
        # not a read outcome: such a page is kept, even when its state reads "unavailable"
        for state_word in ("late", "unavailable"):
            record = page(3).model_copy()
            object.__setattr__(record, "status", m.ObjectStatus(
                state=state_word, text="Late by 12 days"))
            self.backend._keep_good(("obj", state_word), record, state, state)
            self.assertIn(("obj", state_word), self.backend._good)
        with mock.patch.object(be, "LAST_GOOD_KEPT", 3):
            for n in range(10):
                self.backend._keep_good(("k", n), page(n), state, state, inputs=("in", n))
            self.assertEqual(list(self.backend._good), [("k", 7), ("k", 8), ("k", 9)])
            self.assertEqual(set(self.backend._snap_inputs()), set(self.backend._good))

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

    def wait_over(self):
        for _ in range(200):
            if not self.backend._refreshing:
                return
            threading.Event().wait(0.05)
        self.fail("the background read did not end")

    def test_a_first_read_cut_at_the_request_limit_goes_on_with_more_time_and_the_retry_finds_it(self):
        """SP-05: a page never read before whose read outran the request limit answered
        busy, and its work was lost with it: a page needing longer than the limit could
        never be served, however often the app retried (12 % CPU: three 150 s refusals).
        The read now goes on off the request with BACKGROUND_READ_BUDGET times the limit,
        keeps its page, and the retry is served that page without a third read."""
        from operator_api import serving
        who = reader("grp-a")
        budgets, calls = [], []

        def read():
            calls.append(1)
            budgets.append(serving.READ_BUDGET.get())
            if len(calls) == 1:
                raise TimeoutError("workbench_busy")   # the request's read, cut at its limit
            return page(11)

        with self.assertRaises(TimeoutError):
            self.serve(who, read)
        self.wait_over()
        self.assertEqual(budgets, [1, serving.BACKGROUND_READ_BUDGET])
        got = self.serve(who, read)
        self.assertEqual((got.total, got.stale_since), (11, None))
        # never another reader's page
        with self.assertRaises(TimeoutError):
            self.serve(reader("grp-b"), busy)

    def test_a_retry_during_the_first_read_waits_for_it_and_starts_no_second(self):
        who = reader("grp-a")
        release, started, calls = threading.Event(), threading.Event(), []

        def read():
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError("workbench_busy")
            started.set()
            release.wait(10)
            return page(12)

        with self.assertRaises(TimeoutError):
            self.serve(who, read)
        self.assertTrue(started.wait(5))      # the background read is under way
        got = []
        retry = threading.Thread(target=lambda: got.append(self.serve(who, read)))
        retry.start()
        threading.Event().wait(0.4)
        self.assertEqual(len(calls), 2)       # the retry waits: no read beside the background's
        self.assertEqual(got, [])
        release.set()
        retry.join(10)
        self.assertEqual(got[0].total, 12)

    def test_a_retry_that_outwaits_the_first_read_is_refused_busy_not_read_beside_it(self):
        from unittest import mock
        from operator_api import backend as be
        who = reader("grp-a")
        release, calls = threading.Event(), []

        def read():
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError("workbench_busy")
            release.wait(10)
            return page(14)

        with self.assertRaises(TimeoutError):
            self.serve(who, read)
        with mock.patch.object(be, "PAGE_TIMEOUT_SECONDS", 0.2):
            with self.assertRaises(TimeoutError):
                self.serve(who, read)
        self.assertEqual(len(calls), 2)   # still no read beside the background's
        release.set()
        self.wait_over()
        self.assertEqual(self.serve(who, read).total, 14)

    def test_a_retry_whose_first_read_kept_nothing_reads_for_itself(self):
        who = reader("grp-a")
        calls = []

        def read():
            calls.append(1)
            if len(calls) <= 2:
                raise TimeoutError("workbench_busy")   # the request's, then the background's
            return page(13)

        with self.assertRaises(TimeoutError):
            self.serve(who, read)
        self.wait_over()
        self.assertEqual(self.serve(who, read).total, 13)

    def test_unchanged_access_still_serves_the_kept_page(self):
        who = reader("grp-a")
        self.serve(who, lambda: page(5))
        self.reads.policy = policy("finance")   # a new policy object, the same grants
        self.assertEqual(self.serve(who, busy).total, 5)

    def test_a_full_readers_new_source_keys_keep_the_page_a_staff_readers_refuse_it(self):
        full = reader("grp-a")
        self.reads.policy = policy("finance", unrestricted=True)
        self.serve(full, lambda: page(5))
        moved = policy("finance", unrestricted=True)
        moved.audiences = {"mail:new-box": ["grp-a"]}     # a delta named a new mailbox
        moved.units = {"mail": ["mail:new-box"]}
        self.reads.policy = moved
        self.assertEqual(self.serve(full, busy).total, 5)
        self.reads.policy = policy(unrestricted=True)     # a marking revoked: refused
        with self.assertRaises(TimeoutError):
            self.serve(full, busy)

    def test_a_staff_readers_changed_audiences_refuse_the_kept_page(self):
        staff = reader("grp-a")
        self.serve(staff, lambda: page(5))
        moved = policy("finance")
        moved.audiences = {"mail:new-box": ["grp-a"]}
        self.reads.policy = moved
        with self.assertRaises(TimeoutError):
            self.serve(staff, busy)
        full = policy("finance", unrestricted=True)       # became a full reader: refused
        self.reads.policy = full
        with self.assertRaises(TimeoutError):
            self.serve(staff, busy)

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
        # waiting for the stuck keep would take the whole access wait (30 s); the bound
        # leaves room for a loaded machine without letting that wait through
        with mock.patch.object(be, "ACCESS_WAIT_SECONDS", 30):
            started = time.monotonic()
            self.assertEqual(self.serve(reader("grp-a"), lambda: page(5)).total, 5)
            self.assertLess(time.monotonic() - started, 10)


def slow(value, seconds=0.6, started=None):
    """A read that takes ``seconds`` (past the fast budget the tests set)."""
    def read():
        if started is not None:
            started.set()
        time.sleep(seconds)
        return value
    return read


class NeverServedCold(LastGoodPage.__base__):
    """A screen or record page whose read is not done within the fast budget (a delta
    layer landed, nothing kept yet) serves the reader's last good page at once and keeps
    the fresh one when the read ends; every access rule of the busy path still holds."""

    def setUp(self):
        LastGoodPage.setUp(self)
        fast = mock.patch.object(be, "SERVE_FAST_SECONDS", 0.1)
        fast.start()
        self.addCleanup(fast.stop)
        self.marks = None
        marks = mock.patch.object(be, "_marks_stamp", lambda view: self.marks)
        marks.start()
        self.addCleanup(marks.stop)

    serve = LastGoodPage.serve

    def wait_kept(self, who, total):
        key = (who.key, who.mode, ("late", 0, 25))
        for _ in range(400):
            held = self.backend._good.get(key)
            if held is not None and held[1].total == total and not self.backend._refreshing:
                return
            time.sleep(0.05)
        self.fail("fresh page not kept")

    def test_a_slow_read_serves_the_last_page_at_once_and_keeps_the_fresh_one(self):
        who = reader("grp-a")
        self.serve(who, lambda: page(3))
        started = time.monotonic()
        got = self.serve(who, slow(page(8), seconds=4))
        self.assertLess(time.monotonic() - started, 3)       # never the 4 s read
        self.assertEqual(got.total, 3)
        self.assertIsNotNone(got.stale_since)
        self.wait_kept(who, 8)
        fresh = self.serve(who, lambda: page(8))
        self.assertEqual(fresh.total, 8)
        self.assertIsNone(fresh.stale_since)

    def test_while_a_read_is_under_way_the_last_page_is_served_without_another(self):
        who = reader("grp-a")
        self.serve(who, lambda: page(3))
        self.serve(who, slow(page(8)))
        calls = []
        got = self.serve(who, lambda: calls.append(1) or page(9))
        self.assertEqual((got.total, calls), (3, []))
        self.wait_kept(who, 8)

    def test_with_no_kept_page_or_changed_access_the_slow_read_is_waited_for(self):
        who = reader("grp-a")
        self.assertEqual(self.serve(who, slow(page(4), 0.3)).total, 4)   # nothing kept
        self.reads.policy = policy()                                   # finance revoked
        got = self.serve(who, slow(page(5), 0.3))
        self.assertEqual((got.total, got.stale_since), (5, None))

    def test_a_document_re_marked_beyond_the_reader_refuses_the_kept_page(self):
        who = reader("grp-a")
        self.serve(who, lambda: page(3))
        self.marks = "re-marked"
        got = self.serve(who, slow(page(6), 0.3))
        self.assertEqual((got.total, got.stale_since), (6, None))
        self.marks = None   # the page kept under the mark is not this state's either
        with self.assertRaises(TimeoutError):
            self.serve(who, busy)

    def test_the_last_page_is_kept_on_disk_for_a_restart_under_the_same_rules(self):
        import tempfile
        from operator_api.page_store import PageStore
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.backend.page_cache = SimpleNamespace(store=PageStore(Path(folder.name), "c1"))
        who = reader("grp-a")
        self.serve(who, lambda: page(3))
        self.backend._good.clear()                 # a restart: nothing in memory
        got = self.serve(who, slow(page(8)))
        self.assertEqual((got.total, bool(got.stale_since)), (3, True))
        self.wait_kept(who, 8)
        self.backend._good.clear()
        for other in (reader("grp-b"), reader("grp-a", mode="entra")):
            self.assertEqual(self.serve(other, slow(page(1), 0.3)).total, 1)
        self.backend._good.clear()
        self.reads.policy = policy()               # finance revoked since it was kept
        self.assertEqual(self.serve(who, slow(page(2), 0.3)).stale_since, None)

    def test_alert_rules_that_stopped_drop_the_kept_page_from_memory_and_disk(self):
        """The late POs' rules not running (``alerts_stale``) must say unavailable, never
        the old list marked stale for the 12 hours it is held (11-19 review blocker)."""
        import tempfile
        from operator_api.page_store import PageStore
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = PageStore(Path(folder.name), "c1")
        self.backend.page_cache = SimpleNamespace(store=store)
        who = reader("grp-a")
        key = (who.key, who.mode, ("late", 0, 25))
        stopped = errors.UserFacing("alerts_stale", "Late purchase orders are unavailable")

        def rules_stopped():
            raise stopped

        def slow_stopped():
            time.sleep(0.5)
            raise stopped

        for read in (rules_stopped, slow_stopped):
            self.serve(who, lambda: page(3))
            self.assertIn(key, self.backend._good)
            try:
                self.serve(who, read)          # fast read: refused; slow: old page once
            except errors.UserFacing as error:
                self.assertEqual(error.code, "alerts_stale")
            self.wait_finished()
            self.assertNotIn(key, self.backend._good)
            self.backend._good.clear()         # a restart: only the disk copy remains
            with self.assertRaises(errors.UserFacing) as raised:
                self.serve(who, rules_stopped)
            self.assertEqual(raised.exception.code, "alerts_stale")
            self.assertNotIn(key, self.backend._good)
            # the disk copy is gone too: a read that works is a fresh page, not the old
            self.assertEqual(self.serve(who, lambda: page(1)).total, 1)

    def test_the_alerts_home_briefing_and_items_are_kept_screens_not_a_door_read_each_visit(self):
        """Under the SQL alerts Home, the briefing and a type's items go through
        ``_serving`` (kept per reader, served at once after a restart), as the other
        screens do; a rule that is not current still says unavailable (raised through)."""
        from operator_api import page_sql
        b = self.backend
        b._recent, b.freshness = {}, lambda: m.Freshness()
        b._screen_inputs = lambda **kw: (lambda: "inputs")
        who = reader("grp-a")
        who.display_name = "Test Reader"
        seen: list[tuple] = []
        served = b._serving

        def spy(reader_, name, read, *a, **kw):
            seen.append(name)
            return served(reader_, name, read, *a, **kw)

        calls: list[str] = []

        def alert_home(*a, **kw):
            calls.append("home")
            return 2, [], "2026-11-19", [], {}

        stopped = errors.UserFacing("alerts_stale", "Late purchase orders are unavailable")

        def items(*a, **kw):
            raise stopped

        with mock.patch.object(b, "_serving", spy), \
                mock.patch.object(page_sql, "alerts_on", lambda: True), \
                mock.patch.object(page_sql, "alert_home", alert_home), \
                mock.patch.object(page_sql, "alert_briefing", lambda *a, **k: m.Briefing(
                    status="read", count=0, new_since_visit=0, since_visit=False,
                    other_teams=0, rank_basis="", routing_basis="", complete=True,
                    digest_preview="")), \
                mock.patch.object(page_sql, "alert_briefing_items", items):
            home = b._home_alerts(who)
            self.assertEqual((home.needs_attention.status, home.needs_attention.late_total),
                             ("read", 2))
            self.assertEqual(b.briefing(who).status, "read")
            with mock.patch.object(page_sql, "alert_home", items):   # rules not running
                other = reader("grp-b")
                other.display_name = "Other Reader"
                down = b._home_alerts(other)
            self.assertEqual(down.needs_attention.status, "unavailable")
            with self.assertRaises(errors.UserFacing) as raised:
                b.briefing_items(who, "po_late", offset=0, limit=25)
            self.assertEqual(raised.exception.code, "alerts_stale")
        self.assertEqual(seen, [("home", "alerts"), ("briefing", False),
                                ("home", "alerts"), ("briefing_items", "po_late", 0, 25)])

    def test_a_record_page_through_the_door_is_a_kept_screen_whose_refusals_stand(self):
        from operator_api import page_sql
        b = self.backend
        b._screen_inputs = lambda **kw: (lambda: "inputs")
        who = reader("grp-a")
        seen = []
        served = b._serving

        def spy(reader_, name, read, *a, **kw):
            seen.append(name)
            return served(reader_, name, read, *a, **kw)

        def missing(*a, **k):
            raise page_sql.NotFound()

        with mock.patch.object(b, "_serving", spy), \
                mock.patch.object(page_sql, "on", lambda kind=None: True), \
                mock.patch.object(page_sql, "order", missing):
            with self.assertRaises(errors.UserFacing) as raised:
                b.biz_page(who, "purchase_order", "bc://x/1")
        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(seen, [("biz_page", "purchase_order", "bc://x/1", "", "")])

    def revoked_mid_read(self, value):
        """A read that outlasts the fast budget, with finance revoked once it started."""
        def read():
            self.reads.policy = policy()
            time.sleep(0.6)
            return value
        return read

    def wait_finished(self):
        for _ in range(100):
            if not self.backend._refreshing:
                return
            time.sleep(0.05)
        self.fail("read under way never finished")

    def test_a_page_read_while_a_grant_is_revoked_is_not_kept_under_the_new_access(self):
        who = reader("grp-a")
        self.serve(who, lambda: page(3))
        self.serve(who, self.revoked_mid_read(page(99)))
        self.wait_finished()
        self.assertNotIn((who.key, who.mode, ("late", 0, 25)), self.backend._good)
        with self.assertRaises(TimeoutError):   # neither the old page nor the one read across
            self.serve(who, busy)

    def test_after_a_restart_no_disk_copy_read_across_a_revoke_is_served(self):
        import tempfile
        from operator_api.page_store import PageStore, kept_code
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = PageStore(Path(folder.name), "c1")
        self.backend.page_cache = SimpleNamespace(store=store)
        who = reader("grp-a")
        key = ("last-good", who.key, who.mode, ("late", 0, 25))
        self.serve(who, lambda: page(3))
        self.assertIsNotNone(store.get(key, code=kept_code()))
        self.backend._good.clear()                 # a restart: the disk copy only
        self.serve(who, self.revoked_mid_read(page(99)))
        self.wait_finished()
        self.assertIsNone(store.get(key, code=kept_code()))   # the copy no longer matching: gone
        self.backend._good.clear()                 # another restart
        with self.assertRaises(TimeoutError):
            self.serve(who, busy)

    def test_after_a_deploy_the_readers_last_page_is_served_and_a_misfit_is_not(self):
        """A deploy (a new code version) serves the reader's last good page from disk, marked
        stale while it is read again, under the same access checks; a kept page that no
        longer fits the new code's model is never served."""
        import pickle
        import sqlite3
        import tempfile
        from operator_api.page_store import PageStore, disk_key, kept_code
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.backend.page_cache = SimpleNamespace(store=PageStore(Path(folder.name), "c1"))
        who = reader("grp-a")
        self.serve(who, lambda: page(3))
        self.backend._good.clear()                 # a deploy: new code, the disk copy only
        self.backend.page_cache = SimpleNamespace(store=PageStore(Path(folder.name), "c2"))
        got = self.serve(who, busy)
        self.assertEqual((got.total, got.stale_since is not None), (3, True))
        # revoked since: the copy kept before the deploy is not served either
        self.backend._good.clear()
        self.serve(who, self.revoked_mid_read(page(99)))
        self.wait_finished()
        self.backend._good.clear()
        with self.assertRaises(TimeoutError):
            self.serve(who, busy)
        # a page kept by code whose model had another shape: a miss, never a broken page
        self.reads.policy = policy()
        self.serve(who, lambda: page(4))
        self.backend._good.clear()
        key = disk_key(("last-good", who.key, who.mode, ("late", 0, 25)), kept_code())
        with sqlite3.connect(Path(folder.name) / "page-cache.sqlite") as conn:
            kept = pickle.loads(conn.execute("SELECT value FROM pages WHERE key=?",
                                             (key,)).fetchone()[0])
            kept = (kept[0], ("model", "LateList", {"total": "four"}), kept[2])
            conn.execute("UPDATE pages SET value=? WHERE key=?", (pickle.dumps(kept), key))
        with self.assertRaises(TimeoutError):
            self.serve(who, busy)
        # kept and served across c1 -> c2 (the records it shows re-decided: none here)
        self.serve(who, lambda: page(5))
        self.backend._good.clear()
        self.assertEqual(self.serve(who, busy).total, 5)

    def deployed(self, value, who=None, *, hidden=()):
        """``value`` read and kept by code "c1"; then a deploy (code "c2", nothing in
        memory) whose code reads ``hidden`` as no longer readable by the reader: what a
        request that times out is served (None: the refusal)."""
        import tempfile
        from operator_api.page_store import PageStore
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        who = who or reader("grp-a")
        self.backend.page_cache = SimpleNamespace(store=PageStore(Path(folder.name), "c1"))
        self.serve(who, lambda: value)
        self.backend._good.clear()
        self.backend.page_cache = SimpleNamespace(store=PageStore(Path(folder.name), "c2"))
        self.reads.hidden, self.reads.looked_up = set(hidden), []
        try:
            return self.serve(who, busy)
        except TimeoutError:
            return None

    def late_page(self, *ids, extra=None):
        items = [m.LatePo(object_id=oid, label=oid, reason="late",
                          open_url=f"/object/PurchaseOrder/{quote(oid, safe='')}")
                 for oid in ids]
        if extra is not None:
            items[0] = items[0].model_copy(update={"sources": [extra]})
        return m.LateList(total=len(items), offset=0, items=items, more=False,
                          next_offset=0)

    def test_after_a_deploy_a_record_the_reader_lost_under_the_new_code_is_not_served(self):
        """The new code (any module: no list of them decides it) reads a record the kept
        page shows as no longer the reader's: the page is never served, not even stale;
        with every record still readable it is, after one batched check of all of them."""
        ids = ("PurchaseOrder:bc:PO-1", "PurchaseOrder:bc:PO-2", "PurchaseOrder:bc:PO 3/x")
        got = self.deployed(self.late_page(*ids))
        self.assertEqual((got.total, got.stale_since is not None), (3, True))
        self.assertEqual(self.reads.looked_up, [sorted(ids)])   # one batch, every record
        self.assertIsNone(self.deployed(self.late_page(*ids), hidden={ids[1]}))
        for n in (1, 10, 60):   # every record, asked once, whatever the count
            many = [f"PurchaseOrder:bc:{i}" for i in range(n)]
            self.assertEqual(self.deployed(self.late_page(*many)).total, n)
            self.assertEqual(self.reads.looked_up, [sorted(many)])
            self.assertIsNone(self.deployed(self.late_page(*many), hidden={many[-1]}))
        # in memory after the deploy, until a fresh read replaces it: still re-decided
        got = self.deployed(self.late_page(*ids))
        self.reads.hidden = {ids[2]}
        with self.assertRaises(TimeoutError):
            self.serve(reader("grp-a"), busy)

    def test_the_access_worker_runs_each_known_readers_kept_page_checks_before_it_serves(self):
        """Speed 11-12: after a deploy the first visit's check of each kept screen (every
        record re-decided) ran cold on the one access worker, past its 3 s wait at high
        load: no kept page was served and the reader waited the full read. The access
        worker now runs those checks once before it serves, for every known reader at any
        count. It decides nothing for the visit: a record hidden after it is still not
        served, and a reader whose check fails leaves the others' checks done."""
        import tempfile
        from operator_api.page_store import PageStore
        late = ("late", 0, 100)
        for n in (1, 10, 60):
            with self.subTest(n=n):
                folder = tempfile.TemporaryDirectory()
                self.addCleanup(folder.cleanup)
                self.backend.page_cache = SimpleNamespace(
                    store=PageStore(Path(folder.name), "c1"))
                readers = {f"0000000{i % 10}-0000-4000-8000-{i:012d}": reader(f"grp-{n}-{i}")
                           for i in range(n)}
                shown = {who.key: f"PurchaseOrder:bc:PO-{who.key}" for who in readers.values()}
                for who in readers.values():
                    self.backend._serving(who, late, lambda w=who: self.late_page(shown[w.key]))
                self.backend._good.clear()        # a deploy: new code, the disk copies only
                self.backend.page_cache = SimpleNamespace(
                    store=PageStore(Path(folder.name), "c2"))
                broken = next(iter(readers.values())).key
                self.reads.looked_up = []

                def visible(ids):
                    if broken in ids[0] and n > 1:
                        raise OSError("store unreadable for this one")
                    return Reads.visible_objects(self.reads, ids)

                def view_of(session):
                    who = SimpleNamespace(key=",".join(sorted(session.principals)))
                    gated = Gated("t", frozenset(session.principals), None)
                    return SimpleNamespace(_request=contextlib.nullcontext,
                                           access=SimpleNamespace(
                        reader_policy=lambda: (gated, self.reads.policy),
                        decide=self.reads.decide, visible_objects=visible), who=who)
                self.backend.known_readers = lambda: {
                    oid: {"ref": "r" * 64, "exp": int(time.time()) + 600} for oid in readers}
                self.backend.identity_for = lambda oid, exp: SimpleNamespace(
                    principals=readers[oid].key.split(","))
                self.backend.revoked = lambda ref: False
                self.backend.reader_mode = "dev"
                worker = SimpleNamespace(_workspace=view_of)
                started = time.monotonic()
                with mock.patch.object(be, "page_key", lambda view, what: ("k",)), \
                        mock.patch.object(be, "inbox_stamp", lambda view: None), \
                        mock.patch.object(be, "watcher_stamp", lambda view: None):
                    self.assertEqual(self.backend.prewarm_policies(worker=worker), n)
                self.assertLess(time.monotonic() - started, 0.05 * n + 1)
                checked = sorted(i for batch in self.reads.looked_up for i in batch)
                self.assertEqual(checked, sorted(v for k, v in shown.items()
                                                 if n == 1 or k != broken))
                # nothing decided for the visit: a record hidden since is not served
                last = list(readers.values())[-1]
                self.reads.hidden = {shown[last.key]}
                with self.assertRaises(TimeoutError):
                    self.backend._serving(last, late, busy)
                self.reads.hidden = set()

    def test_a_check_of_the_readers_state_alone_never_waits_behind_record_checks(self):
        """11-20: a check naming no record runs in ~0.01 s but queued 1.45 s median behind
        the record-naming checks (0.85 s each) on the check workers. It goes to the state
        worker; a check naming records stays on the check workers; with no state worker up
        both go to the check worker."""
        sent = []

        def worker(name):
            return SimpleNamespace(stopped=SimpleNamespace(is_set=lambda: False), jobs=None,
                                   submit=lambda who, job, **k: sent.append(name) or name)
        self.backend._access_job = lambda handles=(), screen=None, objects=None, who=None: (
            lambda v: (("state",), True))
        who = reader("grp-a")
        self.backend.check_worker, self.backend.access_worker = worker("check"), None
        self.backend.state_worker = worker("state")
        self.backend._access_later(who)
        self.backend._access_later(who, ("mail:1",))
        self.backend._access_later(who, objects=("PurchaseOrder:1",))
        self.backend.state_worker = None
        self.backend._access_later(who)
        self.assertEqual(sent, ["state", "check", "check", "check"])

    def test_the_api_starts_two_check_workers_on_one_queue_by_default(self):
        """A Home visit asks ~30 access checks at once: one check worker ran them in turn
        and the last waited past the page's 3 s (11-19 cold round)."""
        import argparse
        from operator_api import cli
        parser = argparse.ArgumentParser()
        cli.add_arguments(parser)
        self.assertEqual(parser.parse_args([]).check_workers, 2)
        self.assertEqual(parser.parse_args(["--check-workers", "1"]).check_workers, 1)

    def test_access_workers_share_one_queue_so_one_slow_read_never_holds_the_next(self):
        """11-18 walk: one access worker ran every reader's kept-page checks and door reads
        one after another (Home 2.3-4 s warm, 7-13 s cold; 40-60 jobs queued). Each
        access worker added takes jobs from one queue: a door read held up on one worker
        never holds up the next reader's on another; the first one up serves at once, and
        only it watches the inputs (one watcher whatever the count). A read that fails
        on a worker is that read's error only."""
        import queue as q
        from operator_api import serving as sv
        self.backend.access_worker = None
        self.backend.access_jobs = q.PriorityQueue()
        self.backend._access_started = False
        self.backend.prewarm_policies = lambda worker=None: 0
        watchers = []
        self.backend._keep_access_open = lambda worker: watchers.append(worker)
        stopped = threading.Event()
        self.backend.reads = SimpleNamespace(stopped=stopped, jobs=q.PriorityQueue())
        self.addCleanup(stopped.set)
        view = SimpleNamespace(bind=lambda session: None, close=lambda: None)
        from operator_api import backend_warm as bw
        with mock.patch.object(bw, "prewarm_shared_reads", lambda: None):
            for n in (1, 2, 3):
                with self.subTest(workers=n):
                    self.backend.access_worker = None
                    self.backend.access_jobs = q.PriorityQueue()
                    self.backend._access_started = False
                    watchers.clear()
                    made = [self.backend.add_access_worker(lambda s: view) for _ in range(n)]
                    loops = [threading.Thread(target=w.loop, daemon=True) for w in made]
                    for t in loops:
                        t.start()
                    self.assertIs(self.backend.access_worker, made[0])
                    self.assertEqual(watchers, [made[0]])
                    self.assertEqual(sv.peers(self.backend.access_jobs), made)
                    held, done = threading.Event(), []
                    who = reader("grp-a")
                    who.session = SimpleNamespace(principals=["grp-a"])

                    def slow(v):
                        held.wait(5)
                        return "slow"

                    first = self.backend.access_worker.submit(who, slow, priority=-1)
                    time.sleep(0.05)   # taken by a worker, held there
                    started = time.monotonic()
                    for i in range(3):
                        fut = self.backend.access_worker.submit(who, lambda v, i=i: i,
                                                                priority=-1)
                        if n > 1:   # another worker serves it while the slow one waits
                            done.append(fut.result(timeout=2))
                    if n > 1:
                        self.assertEqual(done, [0, 1, 2])
                        self.assertLess(time.monotonic() - started, 2)
                        bad = self.backend.access_worker.submit(
                            who, lambda v: (_ for _ in ()).throw(OSError("this read")))
                        with self.assertRaises(OSError):
                            bad.result(timeout=2)
                        self.assertEqual(self.backend.access_worker.submit(
                            who, lambda v: "after").result(timeout=2), "after")
                    self.assertFalse(first.done())
                    held.set()
                    self.assertEqual(first.result(timeout=2), "slow")
                    for w in made:
                        w.stop()
                    for t in loops:
                        t.join(2)
                    stopped.clear()

    def test_a_kept_pages_check_never_waits_behind_the_doors_reads(self):
        """11-19 after a restart: every access worker held by the door's reads (Home, the
        late list, the briefing groups: seconds each cold), the kept pages' checks queued
        behind them were not decided within ACCESS_WAIT_SECONDS, so Autopilot, My work and
        the suggestions were read whole (14 s). The check worker takes the checks alone:
        with every access worker held, a check (``_access_later``) is decided at once,
        and a door read never runs on the check worker."""
        import queue as q
        self.backend.access_worker = None
        self.backend.check_worker = None
        self.backend.access_jobs = q.PriorityQueue()
        self.backend.check_jobs = q.PriorityQueue()
        self.backend._access_started = False
        self.backend.prewarm_policies = lambda worker=None: 0
        self.backend._keep_access_open = lambda worker: None
        stopped = threading.Event()
        self.backend.reads = SimpleNamespace(stopped=stopped, jobs=q.PriorityQueue())
        self.addCleanup(stopped.set)
        ran_on = []
        view = SimpleNamespace(bind=lambda session: None, close=lambda: None)
        from operator_api import backend_warm as bw
        with mock.patch.object(bw, "prewarm_shared_reads", lambda: None):
            door = [self.backend.add_access_worker(lambda s: view) for _ in range(2)]
            check = self.backend.add_access_worker(lambda s: view, checks=True)
        self.assertIs(self.backend.access_worker, door[0])
        self.assertIs(self.backend.check_worker, check)
        loops = [threading.Thread(target=w.loop, daemon=True) for w in door + [check]]
        for t in loops:
            t.start()
        held = threading.Event()
        who = reader("grp-a")
        who.session = SimpleNamespace(principals=["grp-a"])

        def door_read(v):
            ran_on.append(threading.current_thread())
            held.wait(5)
            return "rows"

        reads = [self.backend.access_worker.submit(who, door_read) for _ in range(4)]
        time.sleep(0.05)   # both access workers held, two door reads queued
        self.backend._access_job = lambda handles=(), screen=None, objects=None, who=None: (
            lambda v: (("state",), True))
        started = time.monotonic()
        decided = self.backend._access_later(who, (), screen=("home",))
        self.assertEqual(decided.result(timeout=2), (("state",), True))
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(any(r.done() for r in reads))
        held.set()
        self.assertEqual([r.result(timeout=2) for r in reads], ["rows"] * 4)
        self.assertEqual(len(ran_on), 4)
        self.assertNotIn(loops[2], ran_on)   # no door read on the check worker
        for w in door + [check]:
            w.stop()
        for t in loops:
            t.join(2)

    def test_one_readers_checks_decide_each_handle_and_record_once_and_never_for_another(self):
        a, b = reader("grp-a"), reader("grp-b")
        for who in (a, b):
            who.session = SimpleNamespace(principals=[who.key])
        self.reads.hidden = {"o2"}
        self.reads.denied = {"h-no"}
        wide = [f"h{i}" for i in range(14)] + ["h-no"]
        first = self.backend._access_later(a, wide, screen=("home",), objects=["o1", "o2"])
        again = self.backend._access_later(a, wide[:5], screen=("briefing",), objects=["o1", "o2", "o3"])
        other = self.backend._access_later(b, wide[:5], screen=("home",), objects=["o1"])
        got = [f.result(timeout=2) for f in (first, again, other)]
        # h-no is refused and o2 is hidden for a, in every check that names them
        self.assertEqual([opens for _state, opens in got], [False, False, True])
        # a's second check reused a's first (handles and records); b's are b's own
        decided = [h for call in self.reads.decided for h in call]
        self.assertEqual(sorted(decided), sorted(wide + wide[:5]))
        self.assertEqual(sorted(i for call in self.reads.looked_up for i in call),
                         ["o1", "o1", "o2", "o3"])

    def test_one_readers_waiting_checks_are_decided_in_one_request_scope(self):
        """A Home visit asked ~30 checks that name no record, each queued behind the
        others (2 s waits for 0.05 s jobs). The ones of one reader still waiting are one
        job in one request scope; each is decided after it was asked, by its own job; a
        check naming records (a slow decision must not hold the rest), another reader's
        and one asked after the job started are jobs of their own; a cancelled one is not
        run."""
        import queue as q
        self.backend.access_worker = None
        self.backend.check_worker = None
        self.backend.access_jobs = q.PriorityQueue()
        self.backend.check_jobs = q.PriorityQueue()
        self.backend._access_started = False
        self.backend.prewarm_policies = lambda worker=None: 0
        self.backend._keep_access_open = lambda worker: None
        stopped = threading.Event()
        self.backend.reads = SimpleNamespace(stopped=stopped, jobs=q.PriorityQueue())
        self.addCleanup(stopped.set)
        scopes = []

        @contextlib.contextmanager
        def request():
            scopes.append(threading.get_ident())
            yield

        view = SimpleNamespace(bind=lambda session: None, close=lambda: None, _request=request)
        from operator_api import backend_warm as bw
        with mock.patch.object(bw, "prewarm_shared_reads", lambda: None):
            self.backend.add_access_worker(lambda s: view)
            check = self.backend.add_access_worker(lambda s: view, checks=True)
        loop = threading.Thread(target=check.loop, daemon=True)
        loop.start()
        self.addCleanup(lambda: (check.stop(), loop.join(2)))
        a, b = reader("grp-a"), reader("grp-b")
        for who in (a, b):
            who.session = SimpleNamespace(principals=[who.key])
        held = threading.Event()
        gate = check.submit(a, lambda v: held.wait(5), priority=-2)
        time.sleep(0.05)   # the one check worker is held
        self.backend._access_job = lambda handles=(), screen=None, objects=None, who=None: (
            lambda v: ((screen[0] if screen else None, tuple(handles)), True))
        mine = [self.backend._access_later(a, (), screen=(name,)) for name in ("home", "team", "my_work")]
        theirs = self.backend._access_later(b, (), screen=("home",))
        named = self.backend._access_later(a, ("h1",), screen=("home",))
        cancelled = self.backend._access_later(a, (), screen=("autopilot",))
        self.assertTrue(cancelled.cancel())
        held.set()
        gate.result(timeout=2)
        self.assertEqual([f.result(timeout=2) for f in mine],
                         [((n, ()), True) for n in ("home", "team", "my_work")])
        self.assertEqual(theirs.result(timeout=2), (("home", ()), True))
        self.assertEqual(named.result(timeout=2), (("home", ("h1",)), True))
        self.assertEqual(len(scopes), 2)   # reader a's three at once, reader b's (the named one is its own job)
        # one asked after that job started is decided by a job of its own
        later = self.backend._access_later(a, (), screen=("insights",))
        self.assertEqual(later.result(timeout=2), (("insights", ()), True))
        self.assertEqual(len(scopes), 3)
        # a check that fails fails alone
        self.backend._access_job = lambda handles=(), screen=None, objects=None, who=None: (
            lambda v: 1 / 0 if screen == ("bad",) else "ok")
        bad = self.backend._access_later(a, (), screen=("bad",))
        good = self.backend._access_later(a, (), screen=("good",))
        with self.assertRaises(ZeroDivisionError):
            bad.result(timeout=2)
        self.assertEqual(good.result(timeout=2), "ok")

    def test_marking_the_briefing_seen_runs_on_the_check_worker_not_behind_door_reads(self):
        """The visit mark is a short write a Home visit waits on: it ran on an access
        worker (1 s behind the door's reads); it runs on the check worker, else the access
        worker, else the read worker."""
        ran = []

        def worker(name, stopped=False):
            def run(who, job, timeout=0, priority=0):
                ran.append(name)
                return job(SimpleNamespace(briefing_seen=lambda: "2026-01-01T00:00:00Z"))
            return SimpleNamespace(run=run, stopped=SimpleNamespace(is_set=lambda: stopped))

        self.backend.warm_briefing = lambda who: True
        who = reader("grp-a")
        for check, access, expect in ((worker("check"), worker("access"), "check"),
                                      (worker("check", True), worker("access"), "access"),
                                      (None, None, "reads")):
            self.backend.check_worker, self.backend.access_worker = check, access
            self.backend.reads = worker("reads")
            ran.clear()
            self.assertTrue(self.backend.briefing_seen(who).ok)
            self.assertEqual(ran, [expect])

    def test_the_seen_mark_is_written_without_a_view_when_the_store_is_named(self):
        """A reader's first visit made a view on the check worker only to write one row
        (7-9 s under load, ahead of their access checks): with the store named the row is
        written at once, under the reader key the view's own write uses; an inbox the
        engine never wrote is still refused."""
        import tempfile
        from operating import proactive_engine as pe
        from operating import proactive_engine_inbox as pei
        from operating.session_identity import Actor
        tmp = Path(tempfile.mkdtemp(prefix="seen-direct-"))
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        store = tmp / "ontology.sqlite"
        self.backend.store_path = store
        self.backend.warm_briefing = lambda who: True
        ran = []
        self.backend.check_worker = SimpleNamespace(
            run=lambda *a, **k: ran.append("check"),
            stopped=SimpleNamespace(is_set=lambda: False))
        actor = Actor("t1", frozenset({"grp-a", "employee:x"}), "internal")
        who = SimpleNamespace(key="employee:x,grp-a", mode="dev",
                              session=SimpleNamespace(actor=actor, principals=actor.principals))
        self.assertFalse(self.backend.briefing_seen(who).ok)   # no inbox yet: refused
        pei._open_inbox(pe.inbox_path(store)).close()
        seen = self.backend.briefing_seen(who)
        self.assertTrue(seen.ok)
        self.assertEqual(ran, [])
        self.assertEqual(pei.last_visit(pe.inbox_path(store), pe.reader_key(actor)), seen.at)

    def test_a_new_layer_opens_each_known_readers_scope_on_the_access_worker(self):
        """After a delta layer (any inputs change) the access worker opens each known,
        unrevoked reader's request scope once, behind every access check, and decides
        nothing for any page; with no change nothing is queued."""
        stamps = [("s1",)]
        self.backend.inputs_stamp = lambda: stamps[-1]
        known = {"11111111-0000-4000-8000-000000000001": "grp-a",
                 "11111111-0000-4000-8000-000000000002": "grp-b",
                 "11111111-0000-4000-8000-000000000003": "grp-gone"}
        self.backend.known_readers = lambda: {o: {"ref": k, "exp": 1} for o, k in known.items()}
        self.backend.revoked = lambda ref: ref == "grp-gone"
        self.backend.identity_for = lambda oid, exp: SimpleNamespace(principals=[known[oid]])
        self.backend.reader_mode = "dev"
        queued, opened = [], []

        @contextlib.contextmanager
        def scope():
            opened.append(1)
            yield

        def submit(who, job, *, priority=0, deadline=None):
            queued.append((who.key, priority))
            job(SimpleNamespace(_request=scope, access=SimpleNamespace(
                decide=lambda h: self.fail("a decision"),
                visible_objects=lambda i: self.fail("a decision"))))
        worker = SimpleNamespace(submit=submit, stopped=threading.Event())
        with mock.patch.object(be, "GENERATION_POLL_SECONDS", 0.01):
            watch = threading.Thread(target=self.backend._keep_access_open, args=(worker,))
            watch.start()
            time.sleep(0.1)
            self.assertEqual(queued, [])                      # nothing changed: nothing
            stamps.append(("s2",))
            for _ in range(200):
                if len(queued) >= 2:
                    break
                time.sleep(0.01)
            worker.stopped.set()
            watch.join(2)
        self.assertEqual(sorted(queued), [("grp-a", 1), ("grp-b", 1)])
        self.assertEqual(len(opened), 2)

    def test_a_recent_readers_page_door_is_decided_again_before_it_ages_out(self):
        """While pages read through the door, each known reader seen within the last hours
        gets one cheap door read queued behind live jobs (priority 1) every
        DOOR_KEEP_SECONDS, inside the door's own age limit; one seen long ago, or revoked,
        gets none, and with the door off nothing is queued."""
        from operating import ontology_tools_sql as ots
        from operator_api import page_sql, serving
        self.assertLess(serving.DOOR_KEEP_SECONDS, ots.DOOR_MAX_AGE_SECONDS)
        now = time.time()
        known = {"11111111-0000-4000-8000-000000000001": ("grp-a", now - 60),
                 "11111111-0000-4000-8000-000000000002": ("grp-old", now - 9 * 3600),
                 "11111111-0000-4000-8000-000000000003": ("grp-gone", now - 60)}
        self.backend.known_readers = lambda: {o: {"ref": k, "exp": 1, "seen": t}
                                              for o, (k, t) in known.items()}
        self.backend.revoked = lambda ref: ref == "grp-gone"
        self.backend.identity_for = lambda oid, exp: SimpleNamespace(principals=[known[oid][0]])
        self.backend.reader_mode = "dev"
        queued, called = [], []

        def submit(who, job, *, priority=0, deadline=None):
            queued.append((who.key, priority))
            job(SimpleNamespace(_call=lambda tool, args: called.append((tool, args))))
        worker = SimpleNamespace(submit=submit)
        with mock.patch.dict(os.environ, {page_sql.ALERTS_SWITCH: "", page_sql.SWITCH: ""}):
            self.assertEqual(self.backend._keep_doors(worker), 0)
        with mock.patch.dict(os.environ, {page_sql.ALERTS_SWITCH: "1"}):
            self.assertEqual(self.backend._keep_doors(worker), 1)
        self.assertEqual(queued, [("grp-a", 1)])
        self.assertEqual(called, [(page_sql.TOOL, {"reads": [{"name": "alert_runs"}]})])

    def test_after_a_deploy_an_excluded_persons_record_is_never_served(self):
        """A page the old code kept showing a record of a person the exclusion names (the
        exclusion unchanged: the old code missed the trim) is not served once the new code
        decides that record withheld, wherever on the page it is named."""
        person = "Employee:entra:33333333-3333-4333-8333-333333333333"
        with mock.patch.dict(os.environ, {"PLX_EXCLUDED_PEOPLE_OIDS":
                                          "33333333-3333-4333-8333-333333333333"}):
            for shown in (m.Source(handle="portal://po/1", system="Portal", label="PO",
                                   open_url=f"/object/Employee/{quote(person, safe='')}"),
                          m.Source(handle="portal://po/1", system="Portal", label=person,
                                   open_url="/source?handle=portal%3A%2F%2Fpo%2F1")):
                page = self.late_page("PurchaseOrder:bc:PO-1", extra=shown)
                self.assertIsNotNone(self.deployed(page))
                self.assertIn(person, self.reads.looked_up[0])
                self.assertIsNone(self.deployed(page, hidden={person}))

    def test_after_a_deploy_a_page_whose_records_cannot_all_be_named_is_a_miss(self):
        """A record link that does not parse: the page cannot be re-decided record by
        record, so it is never served across a deploy (the same code still serves it)."""
        page = self.late_page("PurchaseOrder:bc:PO-1").model_copy(deep=True)
        page.items[0].open_url = "/object/PurchaseOrder/"
        self.assertIsNone(self.deployed(page))
        self.assertEqual(self.reads.looked_up, [])
        from operator_api.page_parts import shown_objects
        self.assertIsNone(shown_objects({"rows": [{"object_id": 5}]}))
        self.assertIsNone(shown_objects({"record_ids": "PurchaseOrder:x"}))
        self.assertIsNone(shown_objects([object()]))
        self.assertEqual(shown_objects({"a": [{"record_id": "SalesOrder:1", "n": 2.5}],
                                        "b": "/object/X/Item%3A9?tab=1", "c": None}),
                         frozenset({"SalesOrder:1", "Item:9"}))

    def test_a_round_trip_through_the_disk_keeps_every_handle_and_record(self):
        """The page rebuilt from its kept JSON names the same sources and records as the
        page read, so the checks before serving it see all of them."""
        from operator_api import backend_pages
        from operator_api.page_parts import shown_handles
        page = self.late_page("PurchaseOrder:bc:PO-1", "PurchaseOrder:bc:PO-2", extra=m.Source(
            handle="graph-mail://m/1", system="Email", label="mail", open_url="/source?x"))
        back = backend_pages._kept_good_model((1.0, backend_pages._portable_model(page), (), "c1"))[1]
        self.assertEqual(shown_handles(back), shown_handles(page))
        from operator_api.page_parts import shown_objects
        self.assertEqual(shown_objects(back), shown_objects(page))


class FirstReadAnswersReading(LastGoodPage.__base__):
    """Today's lists asked for by the app with no page kept for the reader (11-13 walk:
    cold Home 38 s, the briefing 54 s, the suggestions refused busy at 61 s): the request
    waits ``first_wait`` at most, is answered ``reading`` (nothing of the page in it) while
    the read goes on with the background's time, and the next request is served the page
    the read kept. Every other caller waits for the read, as before."""

    def setUp(self):
        LastGoodPage.setUp(self)
        self.backend.first_wait = 0.2
        self.backend._first_failed = {}

    serve = LastGoodPage.serve
    wait_over = LastGoodPage.wait_over

    def ask(self, who, read):
        """As the app's route asks (``app.first_partial``)."""
        from operator_api.app import first_partial
        with first_partial():
            return self.backend._serving(who, ("late", 0, 25), read, reading=lambda: m.LateList(
                total=0, offset=0, reading=True))

    def test_a_slow_first_read_answers_reading_at_once_and_the_next_request_its_page(self):
        from operator_api import serving
        self.backend._inputs_of = lambda *a, **k: (("served", 1), 0)   # inputs unchanged
        who = reader("grp-a")
        release, budgets = threading.Event(), []

        def read():
            budgets.append(serving.READ_BUDGET.get())
            release.wait(10)
            return page(21)

        started = time.monotonic()
        got = self.ask(who, read)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual((got.reading, got.total, got.items), (True, 0, []))
        # asked again while the read goes on: still reading, and no second read beside it
        self.assertTrue(self.ask(who, read).reading)
        self.assertEqual(budgets, [serving.BACKGROUND_READ_BUDGET])   # never a request's limit
        # never kept, so never served to anyone as a page
        self.assertNotIn((who.key, who.mode, ("late", 0, 25)), self.backend._good)
        release.set()
        self.wait_over()
        got = self.ask(who, read)
        self.assertEqual((got.reading, got.total, got.stale_since), (False, 21, None))
        self.assertEqual(len(budgets), 1)   # served the kept page: no read again
        # another reader has no page of this one's: their own first read, reading
        hold = threading.Event()
        other = self.ask(reader("grp-b"), lambda: hold.wait(10) and page(99))
        self.assertEqual((other.reading, other.total), (True, 0))
        hold.set()
        self.wait_over()

    def test_a_first_read_off_the_request_waits_the_backgrounds_time_for_the_access_state(self):
        """11-13 W7 (load 80-100): the access state not read within 3 s left most first
        reads unkept, so the warm visit read every Today list again (21-26 s); with 12 s,
        a reader's first policy decided cold (29 s) still left Home unkept and its next
        visit read it again (109 s). A read off the request waits as long as a page read
        may take for it; a request's own wait is unchanged."""
        from operator_api import serving
        waits = []
        real = self.backend._access_result
        self.backend._access_result = lambda future, wait: (waits.append(wait),
                                                            real(future, wait))[1]
        hold = threading.Event()
        self.assertTrue(self.ask(reader("grp-a"), lambda: hold.wait(10) and page(5)).reading)
        hold.set()
        self.wait_over()
        background = [w for w in waits if w > be.ACCESS_WAIT_SECONDS]
        self.assertEqual(background, [max(be.ACCESS_WAIT_SECONDS * serving.BACKGROUND_READ_BUDGET,
                                          be.PAGE_TIMEOUT_SECONDS)] * 2)
        waits.clear()
        self.backend._access_now(reader("grp-a"))
        self.assertEqual(waits, [be.ACCESS_WAIT_SECONDS])

    def test_a_kept_page_whose_access_is_not_decided_yet_answers_reading_not_a_blank_wait(self):
        """A fresh API after a deploy (or a new session) holds the reader's kept page but
        has not decided its access yet, so it may not serve it: the read gets the rest of
        ``first_wait``, then goes on off the request and the screen answers ``reading``
        (11-21 7:13 AM ET switch: Home, approvals, replies 9-15 s blank); the next request
        is served the page that read kept. Without the route's mark it is waited for."""
        from concurrent.futures import Future
        self.backend._inputs_of = lambda *a, **k: (("served", 1), 0)
        who = reader("grp-a")
        self.assertEqual(self.ask(who, lambda: page(5)).total, 5)   # kept
        self.wait_over()
        real = self.backend._access_later
        undecided = Future()                                       # never answers
        self.backend._access_later = lambda *a, **k: undecided
        release = threading.Event()

        def slow():
            release.wait(10)
            return page(7)

        started = time.monotonic()
        got = self.ask(who, slow)
        self.assertLess(time.monotonic() - started, be.ACCESS_WAIT_SECONDS + 2)
        self.assertEqual((got.reading, got.total, got.items), (True, 0, []))
        self.backend._access_later = real
        release.set()
        self.wait_over()
        got = self.ask(who, slow)
        self.assertEqual((got.reading, got.total), (False, 7))   # the page that read kept
        # an action's card (no route mark) still waits for the read itself
        self.backend._access_later = lambda *a, **k: undecided
        got = self.backend._serving(who, ("late", 0, 25), lambda: page(9),
                                    reading=lambda: m.LateList(total=0, offset=0, reading=True))
        self.assertEqual((got.reading, got.total), (False, 9))
        self.backend._access_later = real

    def test_a_quick_first_read_is_the_answer(self):
        got = self.ask(reader("grp-a"), lambda: page(5))
        self.assertEqual((got.reading, got.total), (False, 5))

    def test_every_other_caller_waits_for_the_read(self):
        """An action's card (``_card`` reads the suggestions) or the read-ahead never gets
        an empty page: without the route's mark the read is waited for."""
        def slow():
            threading.Event().wait(0.6)
            return page(8)

        got = self.backend._serving(reader("grp-a"), ("late", 0, 25), slow,
                                    reading=lambda: m.LateList(total=0, offset=0, reading=True))
        self.assertEqual((got.reading, got.total), (False, 8))
        self.backend.first_wait = None   # and off unless ``serve`` set it
        got = self.ask(reader("grp-b"), slow)
        self.assertEqual((got.reading, got.total), (False, 8))

    def test_a_first_read_that_fails_is_answered_by_its_error_not_reading_again(self):
        who = reader("grp-a")
        calls = []

        def refused():
            calls.append(1)
            threading.Event().wait(0.4)
            raise errors.UserFacing("forbidden", "Not yours.", 403)

        self.assertTrue(self.ask(who, refused).reading)
        self.wait_over()
        with self.assertRaises(errors.UserFacing) as caught:
            self.ask(who, refused)
        self.assertEqual(caught.exception.code, "forbidden")
        self.assertEqual(len(calls), 2)   # the next request read for itself
        with self.assertRaises(TimeoutError):   # a busy refusal stands the same way
            self.ask(reader("grp-c"), lambda: (calls.append(1), threading.Event().wait(0.4),
                                               busy()))
            self.wait_over()
            self.ask(reader("grp-c"), busy)

    def test_a_slow_first_read_that_keeps_nothing_is_the_next_requests_answer_once(self):
        """A screen whose slow read ends in a page never kept (a briefing that could not
        be read: ``unavailable``; or, on a busy server, the reader's access state
        confirmed only once, as the 11-13 walk's polls met: 90-300 s read on the request)
        is no "reading" loop and no second read: the reader's next request is answered
        with that page, once, as a request reading it itself would have been; only under
        the same inputs (no write of theirs since) and the same access state."""
        stamp = [(("served", 1), 0)]
        self.backend._inputs_of = lambda *a, **k: stamp[0]
        who = reader("grp-a")
        calls = []
        # each read below holds until the test lets it end (over), so the request's
        # first_wait always ends before it on a loaded machine (0.4 s sleeps raced it)
        gate = threading.Event()

        def first(who, read):
            gate.clear()
            return self.ask(who, read)

        def over():
            gate.set()
            self.wait_over()

        def unreadable():
            calls.append(1)
            gate.wait(10)
            return m.Suggestions(status="unavailable", total=0, note="not read")

        self.assertTrue(first(who, unreadable).reading)
        over()
        got = self.ask(who, unreadable)
        self.assertEqual((got.reading, got.status, len(calls)), (False, "unavailable", 1))
        # once: the next request reads again (and another reader never had it)
        self.assertTrue(first(who, unreadable).reading)
        self.assertTrue(first(reader("grp-b"), unreadable).reading)
        over()

        def slow(total):
            return lambda: (gate.wait(10), page(total))[1]

        real = self.backend._access_now

        def before_unconfirmed():
            """The reader's access state not read in time before the read, read after."""
            calls = []
            return lambda *a, **k: None if not calls.append(1) and len(calls) == 1 \
                else real(*a, **k)

        # the access state not confirmed before the read, only after: not kept, handed
        self.backend._access_now = before_unconfirmed()
        self.assertTrue(first(reader("grp-c"), slow(31)).reading)
        over()
        self.backend._access_now = real
        self.assertNotIn((reader("grp-c").key, "dev", ("late", 0, 25)), self.backend._good)
        got = self.ask(reader("grp-c"), busy)
        self.assertEqual((got.reading, got.total), (False, 31))
        # never confirmed at all: nothing handed, the next request reads for itself
        self.backend._access_now = lambda *a, **k: None
        self.assertTrue(first(reader("grp-e"), slow(51)).reading)
        over()
        self.backend._access_now = real
        with self.assertRaises(TimeoutError):
            self.ask(reader("grp-e"), busy)
        # a write of the reader's since the read (their inputs moved): not handed
        self.backend._access_now = before_unconfirmed()
        self.assertTrue(first(reader("grp-f"), slow(61)).reading)
        over()
        self.backend._access_now = real
        stamp[0] = (("served", 1), 1)
        self.assertTrue(first(reader("grp-f"), slow(62)).reading)
        over()
        # the access state moved since the read (a marking revoked): not handed
        self.backend._access_now = before_unconfirmed()
        self.assertTrue(first(reader("grp-g"), slow(71)).reading)
        over()
        self.backend._access_now = real
        self.reads.policy = policy()
        self.assertTrue(first(reader("grp-g"), slow(72)).reading)
        over()
        # past FIRST_HANDOFF_SECONDS it is gone
        self.backend._access_now = before_unconfirmed()
        self.assertTrue(first(reader("grp-d"), slow(41)).reading)
        over()
        self.backend._access_now = real
        with mock.patch.object(be, "FIRST_HANDOFF_SECONDS", -1):
            self.assertTrue(first(reader("grp-d"), slow(42)).reading)
        over()

    def test_each_today_screen_reading_shows_nothing_of_the_reader_data(self):
        """The five Today lists' ``reading`` answers: empty lists, zero counts, no source
        and no finding; Home only the reader's own name, the examples and their asked
        questions. Each a valid page of its screen."""
        backend = be.WorkspaceBackend.__new__(be.WorkspaceBackend)
        backend._lock, backend._recent = threading.RLock(), {}
        backend.warm = lambda *a, **k: False
        backend._screen_inputs = lambda **k: None
        backend._serving = lambda who, name, read, inputs_of=None, reading=None: reading()
        who = reader("grp-a")
        pages = [backend.home(who), backend.late_list(who, offset=25, limit=25),
                 backend.briefing(who), backend.suggestions(who), backend.reply_triage(who)]
        others = [backend.approvals_queue(who), backend.upcoming_meetings(who)]
        for got in others:   # the slower other screens: empty, zero, marked reading
            dumped = got.model_dump(mode="json")
            self.assertIs(dumped["reading"], True, type(got).__name__)
            self.assertFalse([k for k, v in dumped.items()
                              if isinstance(v, list) and v or k in ("total", "idle") and v])
            type(got).model_validate(dumped)
        for got in pages:
            dumped = got.model_dump(mode="json")
            self.assertIs(dumped["reading"], True, type(got).__name__)
            type(got).model_validate(dumped)
        home, late, brief, sugg, replies = pages
        self.assertEqual((home.greeting_name, home.needs_attention.status,
                          home.needs_attention.late_pos, home.needs_attention.late_total,
                          home.freshness.sources), ("Test", "unavailable", [], None, []))
        self.assertEqual((late.total, late.items, late.offset, late.more), (0, [], 25, False))
        self.assertEqual((brief.status, brief.count, brief.items, brief.groups),
                         ("unavailable", 0, [], []))
        self.assertEqual((sugg.status, sugg.total, sugg.items), ("unavailable", 0, []))
        self.assertEqual((replies.total, replies.lines), (0, []))
        # a record's page while its first read goes on: the id asked for and nothing else,
        # neither remembered as opened (the read-ahead) nor journalled
        backend._pages_seen, opened = {}, []
        backend.remember_reader = lambda who: opened.append("remembered")
        backend._labelled = lambda who, page, route: opened.append("journalled") or page
        rec = backend.object_page(who, "Customer", "Customer:portal.x:{\"id\":\"C1\"}")
        self.assertEqual((rec.reading, rec.label, rec.summary, rec.facts, rec.timeline,
                          rec.linked, rec.status.state, rec.complete, rec.customer),
                         (True, "", "", [], [], [], "reading", False, None))
        self.assertEqual((backend._pages_seen, opened), ({}, []))
        m.ObjectPage.model_validate(rec.model_dump(mode="json"))
        # a screen read that never comes back says "reading" is no last good page
        self.backend._keep_good(("k",), m.LateList(total=0, offset=0, reading=True),
                                ("s",), ("s",))
        self.assertNotIn(("k",), self.backend._good)


class RewarmOnDeltaLayer(unittest.TestCase):
    def test_a_new_delta_layer_pointer_reads_the_known_readers_ahead(self):
        import tempfile
        from operating import delta_overlay
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = Path(folder.name) / "ontology.sqlite"
        store.write_bytes(b"")
        backend = be.WorkspaceBackend.__new__(be.WorkspaceBackend)
        backend.store_path = store
        warmed, stop, done = [], threading.Event(), threading.Event()
        backend.warm_known = lambda: warmed.append(1) or done.set() or 0
        # Event-ordered, never timed: the watcher has read the unchanged state on a poll
        # before the layer lands, and the test waits for the read-ahead itself.
        polls, polled = [0], threading.Condition()
        real_state = backend._warm_state

        def state():
            found = real_state()
            with polled:
                polls[0] += 1
                polled.notify_all()
            return found
        backend._warm_state = state
        with mock.patch.dict(os.environ, {delta_overlay.DIR_ENV: ""}), \
                mock.patch.object(be, "GENERATION_POLL_SECONDS", 0.05):
            watcher = threading.Thread(target=backend.watch_generation, args=(stop,),
                                       kwargs={"rewarm_seconds": 3600}, daemon=True)
            watcher.start()
            with polled:   # the start's read and two polls of the same state
                self.assertTrue(polled.wait_for(lambda: polls[0] >= 3, 120))
            self.assertEqual(warmed, [])           # nothing changed: no read-ahead
            pointer = delta_overlay.pointer_path(store)
            pointer.parent.mkdir(parents=True, exist_ok=True)
            pointer.write_text("{}")               # a cycle lands a layer
            self.assertTrue(done.wait(120), "the new layer was never read ahead")
            stop.set()
            watcher.join(120)
        self.assertFalse(watcher.is_alive())
        self.assertEqual(warmed, [1])


    def test_a_cycle_written_to_postgres_reads_the_known_readers_ahead(self):
        """Over Postgres a delta cycle moves the load state, not a file: the known
        readers' Today is read again then (not only every REWARM_SECONDS), at most once
        per REWARM_FLOOR_SECONDS however often it moves."""
        backend = be.WorkspaceBackend.__new__(be.WorkspaceBackend)
        backend.store_path = None
        stamp, warmed, clock = [(3, "t1")], [], [0.0]
        backend._pg_load_stamp = lambda: stamp[0]
        backend.warm_known = lambda: warmed.append(stamp[0]) or 0
        # each poll: (the time then, the load state then); None: stopped
        plan = [(10, (3, "t1")), (20, (3, "t2")), (30, (4, "t3")), (100, (4, "t3")),
                (145, (4, "t3")), (150, (4, "t3"))]

        class Polls:
            def wait(self, seconds):
                if not plan:
                    return True
                clock[0], stamp[0] = plan.pop(0)
                return False

        with mock.patch.object(be, "time", SimpleNamespace(monotonic=lambda: clock[0])), \
                mock.patch.object(be, "REWARM_FLOOR_SECONDS", 120):
            backend.watch_generation(Polls(), rewarm_seconds=3600)
        # t2 at once; t3 (moved again 10 s later) once the floor has passed, once
        self.assertEqual(warmed, [(3, "t2"), (4, "t3")])


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



class ReadAgainAfterSeen(unittest.TestCase):
    """Home's seen mark makes the briefing and Home not current: the visit after it paid
    their whole read (briefing 1.25 s, Home 0.6 s on live data). The screens it changed are
    read again at once for the reader's own session; a service session is never the one
    remembered for them."""

    def test_only_the_changed_screens_of_that_reader_are_read_again(self):
        backend = be.WorkspaceBackend.__new__(be.WorkspaceBackend)
        backend._lock = threading.RLock()
        own, other = reader("grp-a"), reader("grp-b")
        own.session.live = other.session.live = True
        gone = reader("grp-c")
        gone.session.live = False
        names = (("briefing", False), ("briefing_items", "po_late", 0, 6, False), ("home",),
                 ("suggestions",), ("my_work",))
        for who in (own, other, gone):
            for name in names:
                backend._remember_read((who.key, "dev", name), who, lambda: None)
        backend._may_read_ahead = lambda who: who.session.live
        started = []
        backend._refresh_later = lambda who, key, read: started.append(key)
        screens = frozenset({"briefing", "briefing_items", "home"})
        self.assertEqual(backend.reread_after_write(own, screens), 3)
        self.assertEqual(sorted(k[2][0] for k in started), ["briefing", "briefing_items", "home"])
        self.assertEqual({k[0] for k in started}, {"grp-a"})
        started.clear()
        self.assertEqual(backend.reread_after_write(gone, screens), 0)   # expired: none
        self.assertEqual(backend.reread_after_write(own, None), 0)       # every screen: none
        self.assertEqual(started, [])
        # a service session (the Ask canary, the page measure) is never remembered: the
        # reader's own session stays the one their screens are read again for
        service = reader("grp-a")
        service.session.canary = True
        backend._remember_read(("grp-a", "dev", ("home",)), service, lambda: None)
        self.assertIs(backend._snap_reads()[("grp-a", "dev", ("home",))][0], own)


class FullReaderMovingParts(unittest.TestCase):
    def test_a_full_reader_skips_every_part_drawn_from_audiences_or_units(self):
        """A full reader's kept state leaves out audiences and units (new source keys move
        them on each delta); the finer audience parts move with them, so they are left out
        too, else every kept page of a full reader is refused when a key appears."""
        import collections
        from operating.pg import permissions as pp
        actor = collections.namedtuple("A", "tenant principals clearance")("t", frozenset({"u"}), 0)
        names = [n for n, _ in pp.policy_parts(SimpleNamespace(
            tenant="t", audiences={"mail:a": {"u"}}, markings={}, units={}), actor)]
        moving = [n for n in names if n.startswith(("audiences", "units"))]
        self.assertEqual(sorted(moving), ["audiences", "audiences_held", "audiences_keys", "units"])
        self.assertEqual(sorted(set(moving) - set(be.FULL_READER_MOVING_PARTS)), [])

if __name__ == "__main__":
    unittest.main()


class SnapshotFirst(LastGoodPage.__base__):
    """Owner 2026-11-11: every page under 1 s. A screen's last good page read under the
    inputs served now (the store, its layers, the inbox: ``inputs_stamp``) is served as it
    is with no read started; read under other inputs it is served at once marked
    ``stale_since`` and read again off the request; an inputs change reads the recent
    screens again. Every access rule of the kept page still holds on this path."""

    def setUp(self):
        LastGoodPage.setUp(self)
        self.inputs = ("gen-1",)
        stamp = mock.patch.object(be.WorkspaceBackend, "inputs_stamp",
                                  lambda backend: self.inputs)
        stamp.start()
        self.addCleanup(stamp.stop)
        self.marks = None
        marks = mock.patch.object(be, "_marks_stamp", lambda view: self.marks)
        marks.start()
        self.addCleanup(marks.stop)

    serve = LastGoodPage.serve
    wait_kept = NeverServedCold.wait_kept

    def counted(self, value):
        calls = []

        def read():
            calls.append(1)
            return value
        return read, calls

    def test_a_page_read_under_the_inputs_now_is_served_with_no_read(self):
        who = reader("grp-a")
        self.serve(who, lambda: page(3))
        read, calls = self.counted(page(9))
        started = time.monotonic()
        got = self.serve(who, read)
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertEqual((got.total, got.stale_since, calls), (3, None, []))
        # past the freshness limit it is no longer current: served stale, read again
        key = (who.key, who.mode, ("late", 0, 25))
        at, value, access = self.backend._good[key]
        self.backend._good[key] = (at - be.SNAPSHOT_FRESH_SECONDS - 1, value, access)
        got = self.serve(who, read)
        self.assertEqual(got.total, 3)
        self.assertIsNotNone(got.stale_since)
        self.wait_kept(who, 9)

    def test_the_sql_doors_late_list_is_kept_and_served_with_no_read(self):
        """The door's late list took 1.8-4.6 s on every visit (nothing kept it, so the
        warm-up read it for nothing): it is a kept screen as Home and the sidebar are."""
        from operator_api import page_sql
        who = reader("grp-a")
        reads = []

        def door(backend, w, offset, limit):
            reads.append((offset, limit))
            return 3, [], "2026-11-19T00:00:00+00:00"
        with mock.patch.object(page_sql, "alerts_on", lambda: True), \
                mock.patch.object(page_sql, "alert_late_pos", door), \
                mock.patch.object(page_sql, "runs_fresh", lambda backend: "fresh"), \
                mock.patch.object(be.WorkspaceBackend, "freshness",
                                  lambda backend, *a, **k: m.Freshness()):
            first = self.backend.late_list(who, offset=0, limit=100)
            key = (who.key, who.mode, ("late", 0, 100))
            for _ in range(400):   # kept off the request
                if key in self.backend._good and not self.backend._refreshing:
                    break
                time.sleep(0.05)
            again = self.backend.late_list(who, offset=0, limit=100)
            other = self.backend.late_list(reader("grp-b"), offset=0, limit=100)
        self.assertEqual((first.total, again.total, again.stale_since), (3, 3, None))
        self.assertEqual(reads.count((0, 100)), 2)   # the visit's, and grp-b's own: never shared
        self.assertEqual(other.total, 3)

    def test_the_kept_check_runs_again_for_each_known_reader_before_the_port_opens(self):
        """After the warm-up kept this code version's pages, each known reader's kept
        pages' checks run once more (the first visit's own were cold: 1-5 s); a reader
        refused or failing is skipped alone; none is run twice."""
        for n in (1, 10, 60):
            with self.subTest(n=n):
                ran, jobs = [], []
                worker = SimpleNamespace(stopped=threading.Event(), submit=lambda r, job, **kw: (
                    jobs.append(kw.get("deadline")), self._done(job(None)))[1])
                fake = SimpleNamespace(
                    identity_for=lambda oid, exp: (_ for _ in ()).throw(LookupError()) if oid == "bad"
                    else oid, revoked=lambda ref: ref == "r" * 63 + "x", reader_mode="dev",
                    known_readers=lambda: {**{f"u{i}": {"ref": "r" * 64, "exp": 9} for i in range(n)},
                                           "bad": {"ref": "r" * 64, "exp": 9},
                                           "gone": {"ref": "r" * 63 + "x", "exp": 9}},
                    check_worker=worker, access_worker=None, state_worker=None,
                    _prewarm_kept_checks=lambda view, r, until: ran.append(r.session) or 1)
                fake.prewarm_people = lambda: [(oid, int(v["exp"]), v["ref"])
                                               for oid, v in fake.known_readers().items()]
                started = time.monotonic()
                self.assertEqual(be.WorkspaceBackend.prewarm_checks_after_warm(fake, 5.0), n)
                self.assertLess(time.monotonic() - started, 2.0)
                self.assertEqual(sorted(ran), sorted(f"u{i}" for i in range(n)))
                state = []   # the state worker's runtime is warmed for the same readers
                fake.state_worker = SimpleNamespace(stopped=threading.Event(),
                                                    submit=lambda r, job, **kw: state.append(r.session))
                ran.clear()
                self.assertEqual(be.WorkspaceBackend.prewarm_checks_after_warm(fake, 5.0), n)
                self.assertEqual(sorted(state), sorted(f"u{i}" for i in range(n)))

    @staticmethod
    def _done(value):
        from concurrent.futures import Future
        f = Future()
        f.set_result(value)
        return f

    def test_new_inputs_serve_the_page_stale_at_once_and_keep_the_new_read(self):
        who = reader("grp-a")
        self.serve(who, lambda: page(3))
        self.inputs = ("gen-2",)
        got = self.serve(who, slow(page(8), 0.3))
        self.assertEqual(got.total, 3)
        self.assertIsNotNone(got.stale_since)
        self.wait_kept(who, 8)
        read, calls = self.counted(page(1))
        got = self.serve(who, read)
        self.assertEqual((got.total, got.stale_since, calls), (8, None, []))

    def test_the_readers_own_write_reads_the_screen_now_not_its_snapshot(self):
        """A reader who marks the briefing seen, asks, or acts sees it at once: their own
        write (any request that is not a read) leaves their kept pages not current, and
        the next visit waits for its read; another reader's snapshot is untouched."""
        who, other = reader("grp-a"), reader("grp-b")
        self.serve(who, lambda: page(3))
        self.serve(other, lambda: page(4))
        self.backend.reader_wrote(who)
        read, calls = self.counted(page(5))
        got = self.serve(who, read)
        self.assertEqual((got.total, got.stale_since, calls), (5, None, [1]))
        read, calls = self.counted(page(6))      # current again: kept under the write
        self.assertEqual((self.serve(who, read).total, calls), (5, []))
        read, calls = self.counted(page(7))
        self.assertEqual((self.serve(other, read).total, calls), (4, []))
        # a read under way across the write is never kept as current
        def read_across():
            self.backend.reader_wrote(who)
            return page(8)
        self.backend.reader_wrote(who)
        self.serve(who, read_across)
        read, calls = self.counted(page(9))
        self.assertEqual((self.serve(who, read).total, calls), (9, [1]))

    def test_no_inputs_stamp_reads_as_before(self):
        who = reader("grp-a")
        self.serve(who, lambda: page(3))
        self.inputs = None
        read, calls = self.counted(page(4))
        self.assertEqual((self.serve(who, read).total, calls), (4, [1]))

    def test_the_snapshot_passes_every_access_rule_of_a_kept_page(self):
        who = reader("grp-a")
        cited = "graph-mail://box/msg-1@g-1"
        shown = page(5).model_copy(update={"items": [m.LatePo(
            object_id="po-1", label="PO 1", reason="late", open_url="/objects/po-1", sources=[
                m.Source(system="Email", handle=cited, label="Supplier mail")])]})
        self.serve(who, lambda: shown)
        # a source the reader can no longer open: never served, the read decides
        self.reads.denied.add(cited)
        read, calls = self.counted(page(6))
        self.assertEqual((self.serve(who, read).total, calls), (6, [1]))
        self.reads.denied.clear()
        self.serve(who, lambda: shown)
        # a revoked marking with the same principals
        self.reads.policy = policy()
        read, calls = self.counted(page(7))
        self.assertEqual((self.serve(who, read).total, calls), (7, [1]))
        # an added exclusion
        self.reads.policy = policy("finance")
        self.serve(who, lambda: shown)
        with mock.patch.dict(os.environ, {
                "PLX_EXCLUDED_PEOPLE_OIDS": "22222222-2222-4222-8222-222222222222"}):
            read, calls = self.counted(page(8))
            self.assertEqual((self.serve(who, read).total, calls), (8, [1]))
        # a document re-marked beyond the reader
        self.serve(who, lambda: shown)
        self.marks = "re-marked"
        read, calls = self.counted(page(2))
        self.assertEqual((self.serve(who, read).total, calls), (2, [1]))
        # an access state that cannot be read: no snapshot either
        self.marks = None
        self.serve(who, lambda: shown)
        self.reads.stuck = True
        with mock.patch.object(be, "ACCESS_WAIT_SECONDS", 0.05):
            read, calls = self.counted(page(4))
            self.assertEqual((self.serve(who, read).total, calls), (4, [1]))
        # never another reader's
        self.reads.stuck = False
        other_read, other_calls = self.counted(page(1))
        self.assertEqual((self.serve(reader("grp-b"), other_read).total, other_calls),
                         (1, [1]))

    def test_a_disk_copy_is_never_current(self):
        who = reader("grp-a")
        self.serve(who, lambda: page(3))
        self.backend._snap_inputs().clear()      # as after a restart: read from disk
        read, calls = self.counted(page(5))
        got = self.serve(who, read)
        self.assertEqual(got.total, 3)
        self.assertIsNotNone(got.stale_since)
        self.wait_kept(who, 5)

    def test_an_inputs_change_reads_nothing_while_a_request_is_being_read(self):
        """The change refresh yields to live requests: each screen it reads again waits
        until the read workers are idle (``wait_idle``), so an object page asked for
        while the inputs moved is never queued behind a refresh."""
        order: list = []
        self.backend.yield_to_live = True
        self.backend.wait_idle = lambda: order.append("idle")
        self.backend.revoked = lambda ref: False
        self.inputs = ("gen", 1)
        readers = [reader(f"grp-{i}") for i in range(3)]
        for i, who in enumerate(readers):
            who.session = SimpleNamespace(session_ref=f"ref-{i}",
                                          expires_at=int(time.time()) + 3600)
            self.serve(who, lambda: page(1))
        self.inputs = ("gen", 2)
        for who in readers:
            def read(who=who):
                order.append(who.key)
                return page(2)
            self.backend._snap_reads()[(who.key, who.mode, ("late", 0, 25))] = (
                who, read, time.monotonic())
        self.assertEqual(self.backend._refresh_changed(), 3)
        self.assertEqual(order[0::2], ["idle"] * 3)        # every read after an idle wait
        self.assertEqual(sorted(order[1::2]), sorted(w.key for w in readers))

    def test_an_inputs_change_reads_each_recent_screen_again_once(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.backend._good.clear()
                self.backend._snap_inputs().clear()
                self.backend._snap_reads().clear()
                self.inputs = ("gen", n, 1)
                calls: dict[str, int] = {}
                readers = [reader(f"grp-{i}") for i in range(n)]
                for i, who in enumerate(readers):
                    who.session = SimpleNamespace(session_ref=f"ref-{i}",
                                                  expires_at=int(time.time()) + 3600)
                # one signed in with a revoked ticket, one expired: never read ahead
                revoked = {"ref-0"}
                self.backend.revoked = lambda ref: ref in revoked
                if n > 1:
                    readers[1].session.expires_at = int(time.time()) - 1
                skipped = {readers[0].key} | ({readers[1].key} if n > 1 else set())

                def read_of(who, total):
                    def read():
                        calls[who.key] = calls.get(who.key, 0) + 1
                        return page(total)
                    return read
                for who in readers:
                    self.serve(who, read_of(who, 1))
                calls.clear()
                self.inputs = ("gen", n, 2)
                for who in readers:   # the reads the change runs return the new page
                    self.backend._snap_reads()[(who.key, who.mode, ("late", 0, 25))] = (
                        who, read_of(who, 2), time.monotonic())
                done = self.backend._refresh_changed()
                # newest first: the skipped readers (served first) are oldest
                expected = min(n - len(skipped), be.SNAPSHOT_REFRESH_KEPT)
                self.assertEqual(done, expected)
                self.assertEqual(sorted(calls.values()), [1] * expected)
                self.assertFalse(skipped & set(calls))
                # the refreshed ones are current now: no read, the new page
                fresh = [w for w in readers if w.key in calls]
                for who in fresh:
                    read, got_calls = self.counted(page(9))
                    got = self.serve(who, read)
                    self.assertEqual((got.total, got.stale_since, got_calls), (2, None, []))
                # a second pass under the same inputs reads nothing again
                calls.clear()
                self.backend._refresh_changed()
                self.assertEqual(sorted(calls.values()), [1] * (n - len(skipped) - expected))
                self.assertFalse(skipped & set(calls))


class InboxDigestOffTheRequest(unittest.TestCase):
    """Every "seen" mark writes the gap inbox; its content digest reads all of it (2.7 s
    live). A request's inputs stamp never waits for it: a state not read yet is
    "pending" (equal to no digest) and is read once in the background; a seen mark still
    leaves the digest as it was."""

    def test_the_request_path_never_reads_the_inbox_and_a_seen_mark_changes_nothing(self):
        import sqlite3
        import tempfile
        from operator_api import page_parts as pp
        inbox = Path(tempfile.mkdtemp()) / "x.gap-inbox.sqlite"
        self.addCleanup(__import__("shutil").rmtree, inbox.parent, True)
        conn = sqlite3.connect(inbox)
        conn.executescript("CREATE TABLE gaps (id, kind); CREATE TABLE visits (who, at);"
                           "INSERT INTO gaps VALUES (1, 'po_late');")
        conn.commit()
        reads = []
        real = pp._inbox_digest
        gate = threading.Event()        # cleared: a digest read waits until set again
        gate.set()

        def slow(*args):
            reads.append(1)
            gate.wait(30)
            time.sleep(0.3)
            return real(*args)
        with mock.patch.object(pp, "_inbox_digest", slow):
            gate.clear()                # a request that waited for the digest takes 30 s
            started = time.monotonic()
            try:
                first = pp.inbox_state(inbox, wait=False)
                self.assertLess(time.monotonic() - started, 10)
                self.assertEqual(first[0], "pending")
                self.assertEqual(pp.inbox_state(inbox, wait=False)[0], "pending")  # one read
            finally:
                gate.set()
            digest = pp.inbox_state(inbox)          # a page key waits for that one read
            self.assertEqual((digest[0], len(reads)), ("content", 1))
            self.assertEqual(pp.inbox_state(inbox, wait=False), digest)
            conn.execute("INSERT INTO visits VALUES ('a', 1)")      # a "seen" mark
            conn.commit()
            self.assertEqual(pp.inbox_state(inbox, wait=False)[0], "pending")
            self.assertEqual(pp.inbox_state(inbox), digest)        # the same findings
            conn.execute("INSERT INTO gaps VALUES (2, 'po_late')")   # a new finding
            conn.commit()
            self.assertNotEqual(pp.inbox_state(inbox), digest)
            # the backend's inputs stamp, as a request reads it, does not wait either
            store = inbox.parent / "x"
            store.write_bytes(b"")
            conn.execute("INSERT INTO gaps VALUES (3, 'po_late')")
            conn.commit()
            backend = be.WorkspaceBackend.__new__(be.WorkspaceBackend)
            backend._lock, backend._inputs = threading.RLock(), None
            backend.store_path, backend.pg_dsn = store, None
            from operating import (ai_watcher, delta_overlay, ontology_host,  # noqa: F401
                                   proactive_engine)
            gate.clear()                # a stamp that waited for the digest takes 30 s
            started = time.monotonic()
            try:
                stamp = backend.inputs_stamp()
            finally:
                gate.set()
            self.assertLess(time.monotonic() - started, 10)
            self.assertEqual(stamp[0][2][0], "pending")
        conn.close()


class ChangeRefreshSwitch(unittest.TestCase):
    def test_the_change_refresh_starts_only_when_switched_on(self):
        """Off by default (the live inputs move every few minutes): reading the inputs
        stamp starts no refresh thread; PLX_SNAPSHOT_REFRESH=1 starts it once."""
        backend = be.WorkspaceBackend.__new__(be.WorkspaceBackend)
        backend._lock = threading.RLock()
        backend.store_path = Path("/nonexistent/store.sqlite")
        backend._served_state = lambda wait=True: ("store", 1)
        backend._pg_load_stamp = lambda: None
        started: list = []
        backend._refresh_on_change = lambda: started.append(1)
        self.assertEqual(be.WorkspaceBackend.inputs_stamp(backend), (("store", 1), None))
        self.assertIsNone(getattr(backend, "_inputs_watch", None))
        with mock.patch.object(be, "SNAPSHOT_REFRESH", True):
            backend._inputs = None
            be.WorkspaceBackend.inputs_stamp(backend)
            backend._inputs_watch.join(5)
        self.assertEqual(started, [1])


class MarksStamp(unittest.TestCase):
    """The page key's fingerprint of the documents marked beyond a reader: worked out once
    per withheld set (11-18 profile: ~11 s of CPU sorting and hashing a staff reader's ids
    on every access check of a 3-visit walk), and still new on every re-mark (a new set),
    every change to the PLX-issued documents, and for every other reader's set."""

    def test_one_digest_per_set_and_a_new_one_on_any_change(self):
        from operator_api import backend_pages as bp
        from operating.search import document_marks as dm
        view = SimpleNamespace(access=SimpleNamespace(grant=lambda: (None, ["m:a"])))
        sets = {frozenset({"m:a"}): (frozenset({"Doc:1", "Doc:2"}), frozenset({"h1"}))}
        own = [frozenset()]
        sorted_calls = []
        real_dumps = bp.json.dumps

        def dumps(value, *a, **k):
            sorted_calls.append(1)
            return real_dumps(value, *a, **k)
        with mock.patch.object(dm, "withheld_set", lambda held: sets[frozenset(held)]), \
                mock.patch.object(dm, "_own_issued", lambda: own[0]), \
                mock.patch.object(bp.json, "dumps", dumps), \
                mock.patch.dict(bp._MARK_DIGESTS, clear=True):
            first = bp._marks_stamp(view)
            self.assertEqual([bp._marks_stamp(view) for _ in range(50)], [first] * 50)
            self.assertEqual(len(sorted_calls), 1)
            self.assertEqual(first, bp.hashlib.sha256(real_dumps(
                [["Doc:1", "Doc:2"], ["h1"]]).encode()).hexdigest()[:16])
            sets[frozenset({"m:a"})] = (frozenset({"Doc:1"}), frozenset({"h1"}))   # re-mark
            remarked = bp._marks_stamp(view)
            self.assertNotEqual(remarked, first)
            own[0] = frozenset({"Doc:9"})                       # a PLX-issued doc found
            self.assertNotIn(bp._marks_stamp(view), (first, remarked))
            other = SimpleNamespace(access=SimpleNamespace(grant=lambda: (None, ["m:b"])))
            sets[frozenset({"m:b"})] = (frozenset({"Doc:3"}), frozenset())
            self.assertNotEqual(bp._marks_stamp(other), bp._marks_stamp(view))


class OwnIssuedStamp(unittest.TestCase):
    def test_an_equal_set_reloaded_keeps_its_digest_and_a_changed_one_moves_it(self):
        """The PLX-issued ids' digest is the same for an equal set loaded again (a new
        object) and moves when an id is added or removed."""
        from operator_api import backend_pages as bp
        first = frozenset({"Item:a", "Item:b"})
        stamp = bp._own_stamp(first)
        self.assertEqual(bp._own_stamp(first), stamp)
        self.assertEqual(bp._own_stamp(frozenset(set(first))), stamp)   # reloaded
        added = bp._own_stamp(first | {"Item:c"})
        self.assertNotEqual(added, stamp)
        self.assertNotEqual(bp._own_stamp(frozenset({"Item:a"})), stamp)
        self.assertEqual(bp._own_stamp(first), stamp)
        self.assertEqual(bp._own_stamp(frozenset()), "")
