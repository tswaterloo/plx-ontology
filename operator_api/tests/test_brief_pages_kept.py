"""Briefs the Meetings page reads, kept so a warm visit is quick and never wider than the
reader:

* a finished week's review is read whole (no time budget, owner 11-23) once and served from
  the archive after; this week (never kept) is read whole too, and a page that reports
  itself cut is never kept;
* the upcoming meetings are kept per reader and hour, and read again when a source they
  show no longer opens for the reader."""

from __future__ import annotations

import contextlib
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from operating import briefs as br
from operator_api import briefs, models as m
from operator_api.serving import ReaderCache


class View:
    def __init__(self, held=frozenset({"finance"}), refused=()):
        self.refused = set(refused)
        self.access = SimpleNamespace(
            grant=lambda: (None, held),
            reader_policy=lambda: ("actor", SimpleNamespace(unrestricted=False)),
            decide=lambda hs: {h: ({"reason": "item_refused"} if h in self.refused
                                   else None) for h in hs},
            store_meta=lambda: {"content_digest": "d1"})

    def _request(self):
        return contextlib.nullcontext()

    def _call(self, name, args):
        raise AssertionError(name)

    def gap_inbox(self, **kw):
        return {"status": "read", "gaps": []}


def review(budget_seconds, stopped=False, **kw):
    return {"kind": "weekly", "sections": [{"rows": [], "figures": [
        {"label": "Orders booked", "value": "3", "sources": [{"handle": "bc:so#1"}]}]}],
        "budget": {"stopped": stopped, "seconds": budget_seconds}}


class WeeklyKeptTests(unittest.TestCase):

    def run_week(self, archive, week, today, stopped=False):
        budgets = []

        def weekly(call, held, alerts, *, budget_seconds, **kw):
            budgets.append(budget_seconds)
            return review(budget_seconds, stopped)
        with mock.patch.object(br, "weekly_review", weekly), \
                mock.patch.object(briefs, "_grant_key", lambda view, held: "g1"):
            got = briefs.weekly_review_of(View(), week, archive=archive, reader="r1",
                                          today=today)
        return got, budgets

    def test_a_finished_week_is_read_whole_once_then_served_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = br.WeeklyArchive(Path(tmp) / "w.sqlite")
            got, budgets = self.run_week(archive, None, date(2026, 11, 10))
            self.assertEqual(budgets, [None])
            again, budgets = self.run_week(archive, None, date(2026, 11, 10))
            self.assertEqual(budgets, [])                 # from the archive
            self.assertEqual(again["sections"], got["sections"])

    def test_the_read_ahead_reads_last_week_whole_and_keeps_it(self):
        budgets = []

        def weekly(call, held, alerts, *, budget_seconds, **kw):
            budgets.append(budget_seconds)
            return review(budget_seconds)
        with tempfile.TemporaryDirectory() as tmp:
            backend = SimpleNamespace(data_dir=Path(tmp))
            backend._weekly_archive = lambda: briefs.BriefsMixin._weekly_archive(backend)
            backend._weekly_times = lambda: briefs.BriefsMixin._weekly_times(backend)
            backend._weekly_took = lambda s: briefs.BriefsMixin._weekly_took(backend, s)
            job = briefs.BriefsMixin._weekly_ahead(backend, SimpleNamespace(key="r1"))
            with mock.patch.object(br, "weekly_review", weekly), \
                    mock.patch.object(briefs, "_grant_key", lambda view, held: "g1"):
                job(View())
                job(View())
            self.assertEqual(budgets, [None])
            self.assertEqual(len(backend._weekly_archive().weeks("r1", "g1")), 1)

    def test_this_week_and_a_finished_week_are_read_with_no_budget_and_a_cut_page_is_not_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = br.WeeklyArchive(Path(tmp) / "w.sqlite")
            _, budgets = self.run_week(archive, date(2026, 11, 9), date(2026, 11, 10))
            self.assertEqual(budgets, [None])
            _, budgets = self.run_week(archive, date(2026, 11, 2), date(2026, 11, 10),
                                       stopped=True)
            self.assertEqual(budgets, [None])
            self.assertEqual(archive.weeks("r1", "g1"), [])          # a cut page is not kept
            _, budgets2 = self.run_week(archive, date(2026, 11, 2), date(2026, 11, 10))
            self.assertEqual(budgets2, [None])
            self.assertEqual(len(archive.weeks("r1", "g1")), 1)

    def test_the_read_ahead_has_no_cut_mark_and_reads_the_week_again_when_it_was_not_kept(self):
        budgets = []

        def weekly(call, held, alerts, *, budget_seconds, **kw):
            budgets.append(budget_seconds)
            return review(budget_seconds, stopped=True)
        with tempfile.TemporaryDirectory() as tmp:
            backend = SimpleNamespace(data_dir=Path(tmp))
            backend._weekly_archive = lambda: briefs.BriefsMixin._weekly_archive(backend)
            backend._weekly_times = lambda: briefs.BriefsMixin._weekly_times(backend)
            backend._weekly_took = lambda s: briefs.BriefsMixin._weekly_took(backend, s)
            job = briefs.BriefsMixin._weekly_ahead(backend, SimpleNamespace(key="r1"))
            with mock.patch.object(br, "weekly_review", weekly), \
                    mock.patch.object(briefs, "_grant_key", lambda view, held: "g1"):
                job(View())
                job(View())
            self.assertEqual(budgets, [None, None])
            self.assertFalse((Path(tmp) / "weekly-cut.json").exists())

    def test_no_archive_or_no_grant_reads_with_no_budget(self):
        budgets = []

        def weekly(call, held, alerts, *, budget_seconds, **kw):
            budgets.append(budget_seconds)
            return review(budget_seconds)
        with mock.patch.object(br, "weekly_review", weekly):
            briefs.weekly_review_of(View(), None, today=date(2026, 11, 10))
            with tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(briefs, "_grant_key", lambda view, held: None):
                briefs.weekly_review_of(View(), None, archive=br.WeeklyArchive(
                    Path(tmp) / "w.sqlite"), reader="r1", today=date(2026, 11, 10))
        self.assertEqual(budgets, [None, None])


class UpcomingKeptTests(unittest.TestCase):
    EVENT = "CalendarEvent:graph.events:{\"id\":\"e1\"}"

    def test_kept_per_reader_and_read_again_when_a_source_is_refused(self):
        reads = []

        def upcoming(view, who=None, state_dir=None):
            reads.append((view, getattr(who, "key", None)))
            return [{"object_id": self.EVENT, "label": "Ops weekly",
                     "start": "2026-11-11T14:00:00Z",
                     "sources": [{"handle": "graph:event#1", "system": "Outlook",
                                  "label": "Ops weekly"}]}]
        backend = SimpleNamespace(page_cache=ReaderCache())
        one, other = SimpleNamespace(key="reader-1"), SimpleNamespace(key="reader-2")
        cached = lambda view, who=one: briefs.BriefsMixin._upcoming_cached(  # noqa: E731
            backend, view, who)
        with mock.patch.object(briefs, "upcoming_of", upcoming), \
                mock.patch("operating.pg.permissions._fingerprint",
                           lambda policy, gated: "fp-" + str(gated)):
            wide = View()
            first = cached(wide)
            self.assertIsInstance(first, m.UpcomingMeetings)
            self.assertEqual(len(first.meetings), 1)
            self.assertEqual(cached(wide), first)
            self.assertEqual(len(reads), 1)               # kept
            wide.refused.add(first.meetings[0].sources[0].handle)
            cached(wide)
            self.assertEqual(len(reads), 2)               # a refused source: read again
            # another reader under the same policy: their own open items, never this one's
            cached(wide, other)
            self.assertEqual([r[1] for r in reads], ["reader-1", "reader-1", "reader-2"])

    def test_a_new_action_item_is_a_new_page(self):
        reads = []

        def upcoming(view, who=None, state_dir=None):
            reads.append(state_dir)
            return []
        from operating import meeting_followup as mf
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(briefs, "upcoming_of", upcoming), \
                mock.patch("operating.pg.permissions._fingerprint",
                           lambda policy, gated: "fp-" + str(gated)):
            backend = SimpleNamespace(page_cache=ReaderCache(), state_dir=tmp)
            who = SimpleNamespace(key="reader-1")
            view = View()
            briefs.BriefsMixin._upcoming_cached(backend, view, who)
            briefs.BriefsMixin._upcoming_cached(backend, view, who)
            self.assertEqual(len(reads), 1)               # kept
            store = Path(tmp) / mf.STORE_DIR
            store.mkdir(parents=True, exist_ok=True)
            (store / "m1.json").write_text("{}")
            briefs.BriefsMixin._upcoming_cached(backend, view, who)
            self.assertEqual(len(reads), 2)               # the store changed: read again


if __name__ == "__main__":
    unittest.main()


class WeeklyLatePos(unittest.TestCase):
    """The weekly review's late-PO section is Home's Late list; while that list's first read
    is still going on (``reading``) the section says so, never "nothing late" or a 0."""

    def review(self) -> m.Brief:
        return m.Brief(kind="weekly", title="Weekly review", as_of="2026-11-21", basis="records",
                       sections=[
            m.BriefSection(key="summary", title="Summary", state="shown"),
            m.BriefSection(key="late_pos", title="Late purchase orders (open now)",
                           state="empty")])

    def test_still_reading_is_said_and_a_read_list_is_listed(self):
        reading = m.LateList(total=0, offset=0, items=[], reading=True)
        got = {s.key: s for s in briefs.late_from_home(self.review(), lambda: reading).sections}
        late = got["late_pos"]
        self.assertEqual((late.state, late.rows, late.figures), ("unread", [], []))
        self.assertIn("Still reading", late.note)
        done = m.LateList(total=0, offset=0, items=[])
        self.assertEqual({s.key: s for s in briefs.late_from_home(
            self.review(), lambda: done).sections}["late_pos"].state, "empty")

        def broken():
            raise RuntimeError("down")
        self.assertEqual(briefs.late_from_home(self.review(), broken), self.review())

    def test_the_review_lists_a_page_of_late_orders_and_opens_every_one(self):
        """Audit #10: the review listed 10 late orders and said "N more not listed" with no
        way to them; it lists the late list's page and links the whole list."""
        asked = []

        def late():
            asked.append(briefs.LATE_ROWS)
            items = [m.LatePo(object_id=f"PurchaseOrder:{i}", label=f"PO {i}", open_url=f"/po/{i}",
                              days_late=i, reason="past its promised date") for i in range(briefs.LATE_ROWS)]
            return m.LateList(total=250, offset=0, items=items)
        got = {s.key: s for s in briefs.late_from_home(self.review(), late).sections}["late_pos"]
        self.assertEqual((len(got.rows), got.more, got.more_href), (100, 150, "/late"))
        self.assertGreaterEqual(briefs.LATE_ROWS, 100)


class WeeklyNoTimeoutTests(unittest.TestCase):
    """The weekly review has no time limit (a reader's week takes 424-714 s): it is run
    with no timeout, and while the first build goes on the page is told so, with an estimate
    from the builds measured here."""

    def backend(self, tmp):
        b = SimpleNamespace(data_dir=Path(tmp), calls=[])
        for name in ("_weekly_archive", "_weekly_times", "_weekly_seconds", "_weekly_took",
                     "_brief_run"):
            setattr(b, name, getattr(briefs.BriefsMixin, name).__get__(b))
        b.late_list = lambda reader, limit=None: m.LateList(total=0, offset=0, items=[])

        def run(reader, job):
            b.calls.append(("weekly queue", job is not None))
            return None
        b.weekly_run = run
        return b

    def test_the_route_runs_the_week_on_the_weekly_workers_with_no_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self.backend(tmp)
            with self.assertRaises(Exception):   # no signed-in reader in this fake: 404
                briefs.BriefsMixin.weekly_review(b, SimpleNamespace(key="r1"), None)
            self.assertEqual(b.calls, [("weekly queue", True)])

    def test_the_reading_page_estimates_from_measured_builds_and_none_before_any(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self.backend(tmp)
            b.weekly_reading = briefs.BriefsMixin.weekly_reading.__get__(b)
            first = b.weekly_reading(SimpleNamespace(key="r1"))
            self.assertEqual((first.reading, first.ready_about, first.sections), (True, None, []))
            for seconds in (424.0, 600.0, 714.0):
                b._weekly_took(seconds)
            got = b.weekly_reading(SimpleNamespace(key="r1"))
            self.assertTrue(got.reading and got.ready_about)
            self.assertEqual(b._weekly_seconds(), 600.0)
