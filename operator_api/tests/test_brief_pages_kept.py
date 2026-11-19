"""Briefs the Meetings page reads, kept so a warm visit is quick and never wider than the
reader:

* a finished week's review is read whole once with the kept budget and served from the
  archive after; this week (never kept) keeps a live brief's budget, and a budget-cut
  week is never kept;
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
            self.assertEqual(budgets, [briefs.KEPT_WEEK_BUDGET_SECONDS])
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
            job = briefs.BriefsMixin._weekly_ahead(backend, SimpleNamespace(key="r1"))
            with mock.patch.object(br, "weekly_review", weekly), \
                    mock.patch.object(briefs, "_grant_key", lambda view, held: "g1"):
                job(View())
                job(View())
            self.assertEqual(budgets, [briefs.WARM_WEEK_BUDGET_SECONDS])
            self.assertEqual(len(backend._weekly_archive().weeks("r1", "g1")), 1)

    def test_this_week_keeps_the_live_budget_and_a_cut_week_is_read_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = br.WeeklyArchive(Path(tmp) / "w.sqlite")
            _, budgets = self.run_week(archive, date(2026, 11, 9), date(2026, 11, 10))
            self.assertEqual(budgets, [br.BUDGET_SECONDS])
            _, budgets = self.run_week(archive, date(2026, 11, 2), date(2026, 11, 10),
                                       stopped=True)
            self.assertEqual(budgets, [briefs.KEPT_WEEK_BUDGET_SECONDS])
            _, budgets2 = self.run_week(archive, date(2026, 11, 2), date(2026, 11, 10))
            self.assertEqual(budgets2, [briefs.KEPT_WEEK_BUDGET_SECONDS])

    def test_no_archive_or_no_grant_keeps_the_live_budget(self):
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
        self.assertEqual(budgets, [br.BUDGET_SECONDS, br.BUDGET_SECONDS])


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
