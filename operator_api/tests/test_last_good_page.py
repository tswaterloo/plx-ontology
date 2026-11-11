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
        for _ in range(100):
            held = self.backend._good.get(key)
            if held is not None and held[1].total == total and not self.backend._refreshing:
                return
            time.sleep(0.05)
        self.fail("fresh page not kept")

    def test_a_slow_read_serves_the_last_page_at_once_and_keeps_the_fresh_one(self):
        who = reader("grp-a")
        self.serve(who, lambda: page(3))
        started = time.monotonic()
        got = self.serve(who, slow(page(8)))
        self.assertLess(time.monotonic() - started, 0.5)
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
        from operator_api.page_store import PageStore
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = PageStore(Path(folder.name), "c1")
        self.backend.page_cache = SimpleNamespace(store=store)
        who = reader("grp-a")
        key = ("last-good", who.key, who.mode, ("late", 0, 25))
        self.serve(who, lambda: page(3))
        self.assertIsNotNone(store.get(key))
        self.backend._good.clear()                 # a restart: the disk copy only
        self.serve(who, self.revoked_mid_read(page(99)))
        self.wait_finished()
        self.assertIsNone(store.get(key))          # the copy that no longer matches is gone
        self.backend._good.clear()                 # another restart
        with self.assertRaises(TimeoutError):
            self.serve(who, busy)


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


class ChangeRefreshSwitch(unittest.TestCase):
    def test_the_change_refresh_starts_only_when_switched_on(self):
        """Off by default (the live inputs move every few minutes): reading the inputs
        stamp starts no refresh thread; PLX_SNAPSHOT_REFRESH=1 starts it once."""
        backend = be.WorkspaceBackend.__new__(be.WorkspaceBackend)
        backend._lock = threading.RLock()
        backend.store_path = Path("/nonexistent/store.sqlite")
        backend._served_state = lambda: ("store", 1)
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
