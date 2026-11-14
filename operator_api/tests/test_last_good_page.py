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
        from operator_api import page_store
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
        from operator_api.page_parts import shown_handles
        page = self.late_page("PurchaseOrder:bc:PO-1", "PurchaseOrder:bc:PO-2", extra=m.Source(
            handle="graph-mail://m/1", system="Email", label="mail", open_url="/source?x"))
        back = be._kept_good_model((1.0, be._portable_model(page), (), "c1"))[1]
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
        others = [backend.approvals_queue(who), backend.upcoming_meetings(who),
                  backend.rd_board(who)]
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
        warmed, stop = [], threading.Event()
        backend.warm_known = lambda: warmed.append(1) or 0
        with mock.patch.dict(os.environ, {delta_overlay.DIR_ENV: ""}), \
                mock.patch.object(be, "GENERATION_POLL_SECONDS", 0.05):
            watcher = threading.Thread(target=backend.watch_generation, args=(stop,),
                                       kwargs={"rewarm_seconds": 3600}, daemon=True)
            watcher.start()
            time.sleep(0.3)
            self.assertEqual(warmed, [])           # nothing changed: no read-ahead
            pointer = delta_overlay.pointer_path(store)
            pointer.parent.mkdir(parents=True, exist_ok=True)
            pointer.write_text("{}")               # a cycle lands a layer
            for _ in range(60):
                if warmed:
                    break
                time.sleep(0.05)
            stop.set()
            watcher.join(2)
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


def work(*titles: str, cited: str = "graph-mail://box/msg-1@g-1") -> m.MyWork:
    """My work with one item per title, each citing ``cited`` in its list and naming it in
    its prepared work (a dict), as the autopilot queue keeps them."""
    return m.MyWork(status="read", sent=0, writes=0, digest=m.MyWorkDigest(
        **ap.digest([], datetime.now(timezone.utc))), items=[m.MyWorkItem(
            item_id=f"i-{n}", autopilot="mail", label="Mail", title=t, mine=True,
            happened={}, prepared={"reply_to": cited}, citations=[cited], due_at="2026-11-12T12:00:00Z",
            created_at="2026-11-11T12:00:00Z") for n, t in enumerate(titles)])


class OtherScreens(LastGoodPage.__base__):
    """Owner 2026-11-11: My work, Autopilot, Meetings, Team and the QC pack served as the
    main screens are. Each is current only while its own inputs (the autopilot queue and
    write state, the grants, the follow-up store, the hour) are the same; a current copy
    says when it was read (``read_at``: other people's decisions on it are as of then);
    every handle it shows, in a list or a dict, still opens for the reader; the access
    check runs on its own worker, never behind a long read."""

    def setUp(self):
        SnapshotFirst.setUp(self)
        import tempfile
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)
        (self.dir / "autopilot").mkdir()
        (self.dir / "autopilot" / "queue.sqlite").write_text("q1")
        self.backend.autopilot_queue = self.dir / "autopilot" / "queue.sqlite"
        self.backend.autopilot_write_state = self.dir / "autopilot-write"
        self.backend.writes_environ = {}
        self.backend.data_dir = self.dir
        self.titles = ["first"]
        self.calls = []

        def read(backend, who):
            if backend is self.backend:    # never an earlier test's read still running
                self.calls.append(who.key)
            return work(*self.titles)
        patch = mock.patch.object(be.AutopilotMixin, "my_work", read)
        patch.start()
        self.addCleanup(patch.stop)

    def background_reads_over(self):
        """Wait for every background read of this backend (``_refresh_later``) to end."""
        for _ in range(400):
            with self.backend._lock:
                events = list(self.backend.__dict__.get("_refresh_events", {}).values())
            if not events:
                return
            for event in events:
                event.wait(0.05)
        self.fail("a background read never ended")

    def visit(self, who):
        self.calls.clear()
        got = self.backend.my_work(who)
        return got, list(self.calls)

    def test_my_work_is_served_from_its_snapshot_and_says_when_it_was_read(self):
        who = reader("grp-a")
        got, calls = self.visit(who)
        self.assertEqual((got.read_at, got.stale_since, calls), (None, None, [who.key]))
        self.titles = ["second"]
        got, calls = self.visit(who)
        self.assertEqual(([i.title for i in got.items], calls), (["first"], []))
        self.assertRegex(got.read_at, r"^\d{4}-\d\d-\d\dT[\d:]+Z$")
        self.assertIsNone(got.stale_since)

    def test_a_home_visit_reads_ahead_also_when_a_kept_page_answers_it(self):
        """A snapshot runs no read, so the read-ahead must not live inside the read
        (polish-8 review): Home asks for it itself, once per visit, for that reader."""
        who = reader("grp-a")
        warmed = []
        self.backend.warm = lambda r, **kw: warmed.append(r.key)
        kept = object()
        self.backend._serving = lambda r, key, read, *a, **kw: kept   # a kept page, no read
        self.assertIs(self.backend.home(who), kept)
        self.assertEqual(warmed, [who.key])

    def test_another_persons_decision_in_the_queue_is_not_hidden_behind_the_snapshot(self):
        """Someone else decided an item (the queue file moved, not this reader's write):
        the kept page is served at once marked stale and read again."""
        who = reader("grp-a")
        self.visit(who)
        self.titles = ["decided elsewhere"]
        time.sleep(0.01)
        (self.dir / "autopilot" / "queue.sqlite").write_text("q2 longer")
        got, _ = self.visit(who)
        self.assertEqual([i.title for i in got.items], ["first"])
        self.assertIsNotNone(got.stale_since)
        for _ in range(200):     # the read again off the request, kept when done
            if not self.backend._refreshing and [i.title for i in self.visit(who)[0].items] \
                    == ["decided elsewhere"] and not self.backend._refreshing:
                break
            time.sleep(0.05)
        got, calls = self.visit(who)
        self.assertEqual(([i.title for i in got.items], got.stale_since, calls),
                         (["decided elsewhere"], None, []))
        # the write state is an input too
        touched = self.dir / "autopilot-write" / "decisions.jsonl"
        touched.parent.mkdir(exist_ok=True)
        touched.write_text(str(time.time()))
        got, _ = self.visit(who)
        self.assertIsNotNone(got.stale_since, touched.name)

    def test_a_revoked_team_grant_serves_no_kept_row(self):
        """The owner's workload grants decide whose people and items a manager sees: a
        grant changed is an access change, so the copy kept under the old grants is
        never served, not marked stale and not when the read is then refused busy."""
        grants = self.dir / "workload-grants.json"
        grants.write_text('{"teams": {"sales-person": "sales"}, "managers": {"grp-a": ["sales"]}}')
        who = reader("grp-a")
        for n in (1, 10):
            with self.subTest(n=n):
                self.backend._good.clear()
                self.backend._refreshing.clear()
                self.titles = ["sales-person"]
                self.visit(who)
                self.assertEqual(self.visit(who)[1], [])          # kept, served as kept
                time.sleep(0.01)
                grants.write_text('{"teams": {}, "managers": {}}' + " " * n)   # revoked
                self.titles = ["mine only"]
                got, calls = self.visit(who)
                self.assertEqual(([i.title for i in got.items], got.stale_since, calls),
                                 (["mine only"], None, [who.key]))
                # revoked again while the read is refused busy: the refusal, never the copy
                time.sleep(0.01)
                grants.write_text('{"teams": {"sales-person": "sales"}, "managers": {}}')

                def busy(backend, w):
                    raise errors.BUSY
                with mock.patch.object(be.AutopilotMixin, "my_work", busy):
                    with self.assertRaises(errors.UserFacing):
                        self.backend.my_work(who)
                    # the refusal reads again in the background: let that read end (still
                    # refused busy) so it never lands in the next round's counted reads
                    self.background_reads_over()
        # a grants file that cannot be read: nothing kept is served
        self.titles = ["fresh"]
        self.visit(who)
        with mock.patch.object(be, "files_stamp", side_effect=OSError("unreadable")), \
                mock.patch.object(be.AutopilotMixin, "my_work", busy):
            with self.assertRaises(errors.UserFacing):
                self.backend.my_work(who)

    def test_a_briefing_marked_seen_leaves_my_work_current_but_not_the_briefing(self):
        who = reader("grp-a")
        self.visit(who)
        self.backend._serving(who, ("briefing", True), lambda: page(3))
        self.backend.reader_wrote(who, frozenset({"briefing", "home"}))
        self.assertEqual(self.visit(who)[1], [])            # My work: still its snapshot
        calls: list = []
        got = self.backend._serving(who, ("briefing", True),
                                    lambda: calls.append(1) or page(4))
        self.assertEqual((got.total, calls), (4, [1]))      # the briefing: read now
        self.backend.reader_wrote(who)                      # any other write: every screen
        self.assertEqual(self.visit(who)[1], [who.key])

    def test_unreadable_screen_inputs_are_never_current(self):
        who = reader("grp-a")
        self.visit(who)
        with mock.patch.object(be, "files_stamp", side_effect=OSError("unlistable")):
            self.titles = ["now"]
            got, calls = self.visit(who)
        self.assertEqual(calls and [i.title for i in got.items], ["now"])
        self.assertIsNone(got.read_at)
        # a follow-up store that cannot be listed: the meetings list is read every visit
        with self.assertRaises(OSError):
            self.backend._screen_inputs(extra=lambda: "unreadable")()

    def test_a_screen_directory_that_cannot_be_listed_stamps_nothing(self):
        """A directory of a screen's inputs (the autopilot write state) with a part that
        cannot be listed raises, so nothing under it is current; never an empty stamp."""
        from operator_api.serving import files_stamp
        state = self.dir / "autopilot-write"
        (state / "held").mkdir(parents=True)
        (state / "held" / "w1.json").write_text("{}")
        before = files_stamp([state])
        os.chmod(state / "held", 0)
        self.addCleanup(os.chmod, state / "held", 0o700)
        if os.access(state / "held", os.R_OK):
            self.skipTest("a reader that ignores permissions (root)")
        with self.assertRaises(OSError):
            files_stamp([state])
        os.chmod(state / "held", 0o700)
        self.assertEqual(files_stamp([state]), before)

    def test_a_handle_inside_an_items_lists_or_dicts_is_rechecked_on_every_visit(self):
        who = reader("grp-a")
        mailed = "graph-mail://box/msg-9@g-9"
        self.titles = ["cited"]
        with mock.patch.object(be.AutopilotMixin, "my_work",
                               lambda backend, w: work("cited", cited=mailed)):
            self.backend.my_work(who)
        self.reads.denied.add(mailed)
        got, calls = self.visit(who)
        self.assertEqual(calls, [who.key])     # not served: read again for the reader
        self.assertIn(mailed, [h for d in self.reads.decided for h in d])
        # a dict-only handle (an action item's) is checked as well
        from operator_api.page_parts import shown_handles
        self.assertEqual(shown_handles(m.MyActionItems(items=[{"sources": [
            {"handle": mailed}]}, {"note": "plain words"}])), {mailed})

    def test_each_reader_gets_their_own_screen_at_any_count(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                readers = [reader(f"grp-{n}-{i}") for i in range(n)]
                for who in readers:
                    self.titles = [who.key]
                    self.visit(who)
                self.titles = ["changed"]
                started = time.monotonic()
                for who in readers:
                    got, calls = self.visit(who)
                    self.assertEqual(([i.title for i in got.items], calls), ([who.key], []))
                self.assertLess(time.monotonic() - started, 0.02 * n + 0.5)

    def test_the_warm_up_process_keeps_the_other_screens_for_a_first_visit(self):
        """After a restart a reader's first My work opens at once from the copy the
        warm-up process kept (marked stale, read again), never from another reader's."""
        who, other = reader("grp-a"), reader("grp-b")
        self.backend.yield_to_live, self.backend.warm_enabled = False, True
        self.backend.warm_process = self.backend.warmer = None
        self.backend._warmed, self.backend._pages_seen = {}, {}
        qc = m.Brief(kind="qc_daily", title="QC today", as_of="2026-11-11", sections=[],
                     basis="the reader's QC calls")
        packs = []
        with mock.patch.object(be, "SCREENS_AHEAD", ("my_work", ("qc_pack", "daily"))), \
                mock.patch.object(be.BriefsMixin, "qc_pack",
                                  lambda backend, w, kind: packs.append(kind) or qc):
            self.assertTrue(self.backend.warm(who, force=True))
            for _ in range(200):
                if (who.key, who.mode, ("qc", "daily")) in self.backend._good:
                    break
                time.sleep(0.05)
        self.assertIn((who.key, who.mode, ("my_work",)), self.backend._good)
        self.assertIn((who.key, who.mode, ("qc", "daily")), self.backend._good)   # the QC pack
        self.assertEqual(packs, ["daily"])
        self.backend._snap_inputs().clear()      # the live process: found as kept, not current
        self.titles = ["fresh"]
        got, _ = self.visit(who)
        self.assertEqual(([i.title for i in got.items], bool(got.stale_since)),
                         (["first"], True))
        for _ in range(200):     # the stale copy's read again, off the request, done
            if not self.backend._refreshing:
                break
            time.sleep(0.05)
        self.assertEqual(self.visit(other)[1], [other.key])   # nothing kept for another

    def test_the_warm_up_process_keeps_todays_screens_under_the_keys_the_app_asks(self):
        """Speed 11-12: after a restart the briefing's first read never finished (104 s,
        503) because the warm-up read Today only through its inner caches and kept no page
        for ``_serving``. The warm-up process now reads Home, the briefing, the
        suggestions and the late list's first page of 100 (Home's and "show all"'s), and
        the Team screen, through ``_serving``: each is kept for that reader only."""
        who, other = reader("grp-a"), reader("grp-b")
        self.backend.yield_to_live, self.backend.warm_enabled = False, True
        self.backend.warm_process = self.backend.warmer = None
        self.backend._warmed, self.backend._pages_seen = {}, {}
        read = []
        self.reads.run = lambda w, job, timeout=None: job(SimpleNamespace())
        self.backend._home = lambda w: read.append("home") or m.Home.model_construct()
        self.backend._briefing_cached = lambda view, w, since_visit: (
            read.append(("briefing", since_visit)) or m.Briefing.model_construct())
        self.backend._suggestions_cached = lambda view: (
            read.append("suggestions") or m.Suggestions.model_construct())
        self.backend._late_list = lambda w, offset, limit: (
            read.append(("late", offset, limit)) or page())
        team = m.TeamView(scope="self", state="ready", note="n")
        named = {"_home_served", "briefing", "suggestions", "late_list", "team_view"}
        today = [k for k in be.SCREENS_AHEAD if (k if isinstance(k, str) else k[0]) in named]
        self.assertEqual(len(today), 5)
        wanted = [("home",), ("briefing", False), ("suggestions",), ("late", 0, 100), ("team",)]
        with mock.patch.object(be, "SCREENS_AHEAD", tuple(today)), \
                mock.patch.object(be.TeamViewMixin, "team_view",
                                  lambda backend, w: read.append("team") or team):
            self.assertTrue(self.backend.warm(who, force=True))
            for _ in range(200):
                if all((who.key, who.mode, k) in self.backend._good for k in wanted):
                    break
                time.sleep(0.05)
        for k in wanted:
            self.assertIn((who.key, who.mode, k), self.backend._good, k)
            self.assertNotIn((other.key, other.mode, k), self.backend._good, k)
        self.assertEqual(read.count("home"), 1)      # once: the warm-up's own Home read
        self.assertIn(("late", 0, 100), read)

    def test_a_reader_no_longer_named_for_the_team_screen_is_never_served_its_copy(self):
        """The Team screen names everyone only for the owner and the Team readers named in
        the environment: kept now (served at once on the next visit), but a reader taken
        off that list, a person hidden (named, or an address the team snapshot now
        resolves) or an owner learned is an access change: the copy
        kept under it is never served, not marked stale and not when the read is refused."""
        from operator_api import team_view as tv
        who = reader("grp-a")
        calls = []

        def team(backend, w):
            calls.append(w.key)
            named = w.key in os.environ.get(tv.READERS_ENV, "").split(",")
            return m.TeamView(scope="everyone" if named else "self", state="ready", note="n")

        patch = mock.patch.object(be.TeamViewMixin, "team_view", team)
        patch.start()
        self.addCleanup(patch.stop)
        hidden = "0000000a-0000-4000-8000-00000000000a"
        # a hidden address resolves to a person only once the team snapshot names it
        people = {"none": {}}
        snap = mock.patch.object(tv, "snapshot", lambda path: {"people": dict(people)})
        snap.start()
        self.addCleanup(snap.stop)
        for change in ({tv.READERS_ENV: "grp-b"}, {tv.READERS_ENV: "grp-a",
                                                    tv.HIDDEN_ENV: hidden}, "owners",
                       "address"):
            with self.subTest(change=change), mock.patch.dict(
                    os.environ, {tv.READERS_ENV: "grp-a", tv.HIDDEN_ENV: "x@example.com"}):
                people.clear()
                self.backend._good.clear()
                self.backend._refreshing.clear()
                (self.dir / tv.OWNER_IDS_FILE).unlink(missing_ok=True)
                self.assertEqual(self.backend.team_view(who).scope, "everyone")
                calls.clear()
                self.assertEqual(self.backend.team_view(who).scope, "everyone")
                self.assertEqual(calls, [])                      # kept, served as kept
                time.sleep(0.01)
                if change == "owners":
                    (self.dir / tv.OWNER_IDS_FILE).write_text(f'["{hidden}"]')
                elif change == "address":
                    people[hidden] = {"addresses": ["x@example.com"]}
                else:
                    os.environ.update(change)
                got = self.backend.team_view(who)
                self.assertEqual((got.stale_since, calls), (None, [who.key]))
                self.backend._good.clear()
                self.assertEqual(self.backend.team_view(who).scope, got.scope)
                if change == "owners":
                    (self.dir / tv.OWNER_IDS_FILE).unlink()
                elif change == "address":
                    people.clear()
                else:
                    os.environ.update({tv.READERS_ENV: "grp-a", tv.HIDDEN_ENV: "x@example.com"})
                with mock.patch.object(be.TeamViewMixin, "team_view",
                                       lambda b, w: busy()), self.assertRaises(TimeoutError):
                    self.backend.team_view(who)                  # the refusal, never the copy
                self.background_reads_over()

    def test_the_access_check_runs_on_its_own_worker_not_behind_a_long_read(self):
        who = reader("grp-a")
        self.visit(who)
        own = Reads()
        self.backend.access_worker = SimpleNamespace(submit=own.submit,
                                                     stopped=threading.Event())
        self.reads.stuck = True      # every read worker held by a long read
        with mock.patch.object(be, "ACCESS_WAIT_SECONDS", 0.2):
            started = time.monotonic()
            got, calls = self.visit(who)
        self.assertLess(time.monotonic() - started, 0.15)
        self.assertEqual((got.items[0].title, calls), ("first", []))
        # and it decides as the read worker would: a denied handle still refuses
        own.denied.add("graph-mail://box/msg-1@g-1")
        self.reads.stuck = False
        self.assertEqual(self.visit(who)[1], [who.key])


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
