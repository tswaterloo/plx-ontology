"""Kept pages of the other screens (``OtherScreens``), split from ``test_last_good_page``."""
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
from operator_api.tests.test_last_good_page import (  # noqa: F401
    Reads, busy, page, reader)
from operator_api.tests import test_last_good_page as lgp


def work(*titles: str, cited: str = "graph-mail://box/msg-1@g-1") -> m.MyWork:
    """My work with one item per title, each citing ``cited`` in its list and naming it in
    its prepared work (a dict), as the autopilot queue keeps them."""
    return m.MyWork(status="read", sent=0, writes=0, digest=m.MyWorkDigest(
        **ap.digest([], datetime.now(timezone.utc))), items=[m.MyWorkItem(
            item_id=f"i-{n}", autopilot="mail", label="Mail", title=t, mine=True,
            happened={}, prepared={"reply_to": cited}, citations=[cited], due_at="2026-11-12T12:00:00Z",
            created_at="2026-11-11T12:00:00Z") for n, t in enumerate(titles)])


class OtherScreens(unittest.TestCase):
    """Owner 2026-11-11: My work, Autopilot, Meetings, Team and the QC pack served as the
    main screens are. Each is current only while its own inputs (the autopilot queue and
    write state, the grants, the follow-up store, the hour) are the same; a current copy
    says when it was read (``read_at``: other people's decisions on it are as of then);
    every handle it shows, in a list or a dict, still opens for the reader; the access
    check runs on its own worker, never behind a long read."""

    def setUp(self):
        lgp.SnapshotFirst.setUp(self)
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

    def test_homes_prepared_list_is_kept_and_a_queue_change_is_read_again(self):
        """``/api/autopilot/drafts`` served as the main screens are (read cold it held
        Home's first load 32 s): kept per reader, current while its queue is the same."""
        from operator_api.autopilot_area import AutopilotAreaMixin
        who, other, reads = reader("grp-a"), reader("grp-b"), []

        def read(backend, r):
            reads.append(r.key)
            return m.AutopilotDrafts(items=[], never_senders=[str(len(reads))])
        with mock.patch.object(AutopilotAreaMixin, "_prepared_read", read):
            first = self.backend.autopilot_prepared_list(who)
            self.assertEqual(self.backend.autopilot_prepared_list(who).never_senders,
                             first.never_senders)
            self.assertEqual(reads, [who.key])
            self.backend.autopilot_prepared_list(other)   # never another reader's copy
            self.assertEqual(reads, [who.key, other.key])
            time.sleep(0.01)
            (self.dir / "autopilot" / "queue.sqlite").write_text("q2 longer")
            got = self.backend.autopilot_prepared_list(who)
            self.assertIsNotNone(got.stale_since)
            self.background_reads_over()
            self.assertEqual(reads.count(who.key), 2)

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
        qc = m.Brief(kind="weekly", title="Weekly review", as_of="2026-11-11", sections=[],
                     basis="the reader's calls")
        packs = []
        with mock.patch.object(be, "SCREENS_AHEAD", ("my_work", ("weekly_review", None))), \
                mock.patch.object(be.BriefsMixin, "weekly_review",
                                  lambda backend, w, week, **k: packs.append(week) or qc):
            self.assertTrue(self.backend.warm(who, force=True))
            for _ in range(200):
                if (who.key, who.mode, ("weekly", None)) in self.backend._good:
                    break
                time.sleep(0.05)
        self.assertIn((who.key, who.mode, ("my_work",)), self.backend._good)
        self.assertIn((who.key, who.mode, ("weekly", None)), self.backend._good)   # the weekly review
        self.assertEqual(packs, [None])
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

    def test_screens_ahead_read_with_a_background_budget_session_readers_first(self):
        """11-21 live: the warm-up's screens were cut at a request's 60-90 s limit (165 in
        6 h) while they waited behind other reads, and a session reader's screens queued
        behind a whole directory round. A screen read ahead runs with the background
        budget; a session reader's go before directory employees' queued earlier, and a
        screen already queued for a reader is not queued twice."""
        from operator_api.backend_warm import ScreensPool
        from operator_api.serving import BACKGROUND_READ_BUDGET, READ_BUDGET
        who = reader("grp-a")
        budgets: list[int] = []
        self.backend.probe_screen = lambda r: budgets.append(READ_BUDGET.get())
        self.assertEqual(self.backend._screens_ahead(who, ("probe_screen",)), 1)
        self.assertEqual((budgets, READ_BUDGET.get()), ([BACKGROUND_READ_BUDGET], 1))

        pool, gate, order = ScreensPool(1, name="t-screens"), threading.Event(), []
        pool.submit(gate.wait, 10)                 # the one worker busy
        time.sleep(0.05)
        first = pool.submit(order.append, "d1", later=True, key="d1")
        again = pool.submit(order.append, "d1", later=True, key="d1")
        last = pool.submit(order.append, "d2", later=True, key="d2")
        pool.submit(order.append, "s1", key="s1")
        self.assertIs(first, again)
        gate.set()
        last.result(timeout=5)
        self.assertEqual(order, ["s1", "d1", "d2"])

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
        self.reads.run = lambda w, job, timeout=None, **_kw: job(SimpleNamespace())
        self.backend._home = lambda w: read.append("home") or m.Home.model_construct()
        self.backend._briefing_cached = lambda view, w, since_visit: (
            read.append(("briefing", since_visit)) or m.Briefing.model_construct())
        self.backend._suggestions_cached = lambda view, alerts=None: (
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

    def test_with_the_sql_alerts_the_warm_up_never_reads_the_old_engines_today(self):
        """Home 11-18 (50-90 s cold): with PLX_ALERTS_SQL=1 Home, the briefing and the late
        list read the SQL alerts, so the read-ahead never runs the old engine's reads of
        them (60-110 s each on the read workers live requests wait for); the screens it
        still reads ahead read Home through the SQL alerts, not the old Home."""
        who = reader("grp-a")
        self.backend.yield_to_live, self.backend.warm_enabled = False, True
        self.backend.warm_process = self.backend.warmer = None
        self.backend._warmed, self.backend._pages_seen = {}, {}
        old, read = [], []
        self.backend._attention_cached = lambda view: old.append("home") or ({}, {}, {})
        self.backend._briefing_cached = lambda view, w, since_visit: old.append("briefing")
        self.backend._late_held = lambda view: old.append("late") or ({}, [], {}, {})
        self.backend._home_served = lambda w: old.append("home served")
        # the suggestion cards are made from the SQL alerts: read ahead as the screen
        # (``suggestions``), never as the old engine's job (``_suggestions_cached``)
        self.backend._suggestions_cached = lambda view, *a: old.append("suggestions")
        self.backend.suggestions = lambda w: (
            read.append("suggestions") or m.Suggestions.model_construct())
        self.backend._upcoming_cached = lambda view, w: read.append("meetings")
        self.backend._home_alerts = lambda w: read.append("sql home") or m.Home.model_construct()
        today = tuple(k for k in be.SCREENS_AHEAD
                      if (k if isinstance(k, str) else k[0]) in ("_home_served",
                                                                 "suggestions"))
        with mock.patch.dict("os.environ", {"PLX_ALERTS_SQL": "1"}), \
                mock.patch.object(be, "SCREENS_AHEAD", today), \
                mock.patch("operator_api.simple_ask.ready", lambda *a: False):
            self.assertTrue(self.backend.warm(who, force=True))
            for _ in range(200):
                if "sql home" in read and "suggestions" in read:
                    break
                time.sleep(0.05)
        self.assertEqual(old, [])
        self.assertIn("suggestions", read)
        self.assertIn("sql home", read)

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


if __name__ == "__main__":
    unittest.main()
