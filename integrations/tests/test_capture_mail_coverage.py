"""Mail capture against Graph's own folder counts (AUD-09).

The live store held 3.27M of the 3.47M messages Graph counts (2026-11-14): 677 folders,
every one complete with its history read, short by 1% or more. A folder's delta and
history never list mail moved into it from before its window after its history was read.
After each run's rounds the capture compares every listed folder's ``totalItemCount``
with the current messages it holds, re-lists a short folder whole (bounded, resumable,
its own cursor) and records per folder what explains what is still missing. Over the
fake Graph of ``test_capture_mail_throughput``; evidence label ``injected-transport``.
"""

from __future__ import annotations

from datetime import timedelta
import json
from urllib.parse import unquote
from unittest import mock

from integrations import capture_mail
from integrations.capture_mail_scope import CaptureScope
from integrations.tests.test_capture_mail import (BOX_A, BOX_B, OUTSIDER, READER_A, SYNTHETIC,
                                                  UPN_A, UPN_B, reader_audience)
from integrations.tests.test_capture_mail_throughput import (CUSTOM, EXCLUDED,
                                                             EXCLUDED_MAIL, OLD, FolderGraph,
                                                             ThroughputCase)
from operating import ops_health


def scope(*upns: str) -> CaptureScope:
    return CaptureScope([{"address": u, "folders": "all"} for u in upns or (UPN_A, UPN_B)],
                        window_days=90, internal_domains=["example-plx.com"],
                        allowlist=SYNTHETIC, history="all")


class CountingGraph(FolderGraph):
    """Graph whose folder counts may include items its message listing never returns
    (``phantom``: non-mail items, items with no received time)."""

    def __init__(self) -> None:
        super().__init__()
        self.phantom: dict[tuple[str, str], int] = {}

    def _count(self, user, fid):
        return super()._count(user, fid) + self.phantom.get((user, fid), 0)

    def moved_in(self, user: str, ids: list[str], *, folder: str = CUSTOM,
                 sender: str | None = None) -> None:
        """Old mail moved into a folder after its history was read: Graph lists and counts
        it, but the folder's windowed delta never reports it."""
        for mid in ids:
            self.put(user, mid, "CK-1", folder=folder, received=OLD, sender=sender)
        self.log = [entry for entry in self.log if entry[2] not in ids]


class CoverageCase(ThroughputCase):
    def setUp(self) -> None:
        super().setUp()
        self.graph = CountingGraph()

    def later(self, hours: float = 7) -> None:
        """Past FOLDER_RELIST_SECONDS: the next run lists the mailbox's folders again."""
        self.clock.now += timedelta(hours=hours)

    def coverage(self, box: str = BOX_A, folder: str = CUSTOM) -> dict:
        row = self.store.db.execute("SELECT * FROM folder_coverage WHERE mailbox_id=? AND "
                                    "folder_id=?", (box, folder)).fetchone()
        return dict(row) if row else {}

    def relists(self, box: str, folder: str = CUSTOM) -> int:
        """Whole-folder listing pages sent for one folder: a re-list asks
        ``receivedDateTime lt`` its own start (October on the test clock), the history
        the window's start (July)."""
        return len([u for u in self.requested("receivedDateTime lt 2026-10")
                    if box in u and folder in u])

    def first_run(self) -> None:
        self.graph.put(BOX_A, "IN-1", "CK-1")
        self.graph.put(BOX_A, "CU-1", "CK-1", folder=CUSTOM)
        self.graph.put(BOX_B, "B-1", "CK-1", folder=CUSTOM)
        run = self.capture().run(scope())
        self.assertTrue(run["complete"], run["incomplete_reasons"])
        self.assertEqual(run["graph_coverage"]["short"], 0)
        self.assertEqual(self.coverage()["status"], "whole")


class ShortFolderTests(CoverageCase):
    def test_a_short_folder_is_relisted_and_what_is_still_missing_is_explained(self):
        self.first_run()
        moved = [f"OLD-{n}" for n in range(6)]
        self.graph.moved_in(BOX_A, moved)
        self.graph.moved_in(BOX_A, ["X-OLD"], sender=EXCLUDED_MAIL)   # never stored
        self.graph.phantom[(BOX_A, CUSTOM)] = 10                     # never listed
        self.later()
        capture = self.capture()
        cursors = {}
        real = capture._coverage

        def watched(boxes):
            cursors["before"] = self.folder_rows()
            out = real(boxes)
            cursors["after"] = self.folder_rows()
            return out
        capture._coverage = watched
        run = capture.run(scope())
        # The delta and history cursors, status and rounds of every folder: as the
        # rounds left them; the re-list keeps its own cursor.
        self.assertEqual(cursors["before"], cursors["after"])
        held = {m for m, f in self.stored(BOX_A) if f == CUSTOM}
        self.assertEqual(held, {"CU-1", *moved})
        self.assertFalse(self.store.db.execute(
            "SELECT 1 FROM messages WHERE message_id='X-OLD'").fetchall())
        row = self.coverage()
        detail = json.loads(row["detail"])
        self.assertEqual((row["total"], row["captured"], row["short"]), (18, 7, 11))
        self.assertEqual((row["status"], row["reason"]), ("explained", "not_listed_by_graph"))
        self.assertEqual((detail["recovered"], detail["people_excluded"],
                          detail["not_listed_by_graph"], detail["unexplained"]), (6, 1, 10, 0))
        self.assertIsNotNone(row["last_relist_at"])
        self.assertIsNone(row["relist_next_url"])
        self.assertEqual((run["graph_coverage"]["recovered"],
                          run["graph_coverage"]["relists_finished"]), (6, 1))
        # Mailbox B was whole: never re-listed.
        self.assertEqual(self.relists(BOX_B), 0)
        self.assertEqual(self.coverage(BOX_B)["status"], "whole")

        # The owner's status: per mailbox, for a reader who may see it; nothing else.
        (box_a,) = self.store.completeness(READER_A, reader_audience())["mailboxes"]
        self.assertEqual(box_a["mailbox"], BOX_A)
        cover = box_a["graph_coverage"]
        self.assertEqual((cover["graph_total"], cover["captured"], cover["short"]),
                         (19, 8, 11))
        (folder,) = cover["folders"]
        self.assertEqual((folder["status"], folder["short"], folder["relist"]["recovered"]),
                         ("explained", 11, 6))
        self.assertEqual(self.store.completeness(OUTSIDER, reader_audience())["mailboxes"],
                         [])
        check = ops_health.mail_coverage_check(self.state, self.clock.now,
                                               ops_health.targets({}))
        self.assertEqual((check["state"], check["value"]), ("ok", 11))
        self.assertIn("1 folder(s) explained", check["detail"])

        # Explained and not grown: the next listing does not re-list it again.
        sent = self.relists(BOX_A)
        self.later()
        self.capture().run(scope())
        self.assertEqual(self.relists(BOX_A), sent)
        self.assertEqual(self.coverage()["status"], "explained")
        # Grown past the bound again: re-listed.
        self.graph.moved_in(BOX_A, [f"MORE-{n}" for n in range(6)])
        self.later()
        self.capture().run(scope())
        self.assertGreater(self.relists(BOX_A), sent)
        self.assertEqual(self.coverage()["captured"], 13)

    def test_a_gap_nothing_explains_is_recorded_unexplained_and_breaches(self):
        self.first_run()
        self.graph.moved_in(BOX_A, [f"OLD-{n}" for n in range(8)])
        self.later()
        # The store keeps none of what the re-list reads (as if each read failed).
        with mock.patch.object(capture_mail.MailCapture, "_capture_page",
                               lambda self, *a, **k: True):
            self.capture().run(scope())
        row = self.coverage()
        self.assertEqual((row["status"], row["reason"], row["short"]),
                         ("unexplained", "unexplained", 8))
        check = ops_health.mail_coverage_check(self.state, self.clock.now,
                                               ops_health.targets({}))
        self.assertEqual(check["state"], "breach")

    def test_a_gap_under_the_bound_is_not_relisted(self):
        self.first_run()
        self.graph.phantom[(BOX_A, CUSTOM)] = 4   # under COVERAGE_SHORT_MIN
        self.later()
        self.capture().run(scope())
        self.assertEqual((self.coverage()["status"], self.coverage()["short"]), ("whole", 4))
        self.assertEqual(self.relists(BOX_A), 0)

    def folder_rows(self):
        return [tuple(r) for r in self.store.db.execute(
            "SELECT mailbox_id, folder_key, folder_id, next_url, delta_url, round_kind, "
            "round_started_at, status, reason, rounds_complete, backfill_next_url, "
            "backfill_before, backfill_status, received_since FROM folders "
            "ORDER BY mailbox_id, folder_key")]


class BoundTests(CoverageCase):
    def test_a_relist_is_bounded_per_run_and_resumes_without_counting_twice(self):
        for n in (1, 10, 60):
            with self.subTest(messages=n):
                self.tearDown()
                self.setUp()
                self.first_run()
                moved = [f"OLD-{n}-{i}" for i in range(n)]
                self.graph.moved_in(BOX_A, moved)
                self.graph.phantom[(BOX_A, CUSTOM)] = 5
                self.later()
                runs = 0
                with mock.patch.object(capture_mail, "RELIST_PAGES", 2):
                    while self.coverage().get("status") in (None, "whole", "short",
                                                            "relisting") and runs < 40:
                        before = len(self.requested("$orderby=receivedDateTime desc"))
                        run = self.capture().run(scope())
                        runs += 1
                        self.assertTrue(run["complete"], run["incomplete_reasons"])
                        # at most the bound's pages per run (page size 2)
                        self.assertLessEqual(
                            len(self.requested("$orderby=receivedDateTime desc")) - before, 2)
                held = [m for m, f in self.stored(BOX_A) if f == CUSTOM]
                self.assertEqual(sorted(held), sorted(["CU-1", *moved]))   # none lost/twice
                row = self.coverage()
                detail = json.loads(row["detail"])
                self.assertEqual(detail["listed"], n + 1)
                self.assertEqual((row["status"], detail["not_listed_by_graph"],
                                  detail["recovered"]), ("explained", 5, n))
                self.assertEqual(runs, max(1, -(-(n + 1) // 4)))

    def test_the_count_reads_at_most_its_bound_and_takes_the_rest_next_run(self):
        self.first_run()
        self.later()
        with mock.patch.object(capture_mail, "COVERAGE_COUNT_ROWS", 1):
            run = self.capture().run(scope())
        # the first folder due is always counted; the others wait for the next run
        self.assertEqual(run["graph_coverage"]["folders_counted"], 1)
        for _ in range(3):
            self.later(0.1)
            with mock.patch.object(capture_mail, "COVERAGE_COUNT_ROWS", 1):
                run = self.capture().run(scope())
            self.assertLessEqual(run["graph_coverage"]["folders_counted"], 1)
        # every listed folder compared by now, none left waiting for its count
        self.assertEqual(self.store.db.execute(
            "SELECT COUNT(*) FROM folder_coverage f JOIN mailbox_folders l USING "
            "(mailbox_id, folder_id) WHERE f.checked_at < l.listed_at").fetchone()[0], 0)

    def test_a_mailbox_out_of_the_run_is_never_relisted(self):
        self.first_run()
        self.graph.moved_in(BOX_B, [f"OLD-{n}" for n in range(8)])
        # An earlier short record of an excluded person's mailbox and of B.
        for box in (EXCLUDED, BOX_B):
            self.store.save_coverage(box, CUSTOM, status="short", total=50, captured=1,
                                     short=49, short_since=self.store.now())
        self.later()
        self.capture().run(scope(UPN_A))   # B not in this run's scope
        for box in (EXCLUDED, BOX_B):
            self.assertEqual(self.relists(box), 0, box)
        self.assertEqual(self.coverage(BOX_B)["status"], "short")

    def test_an_unreadable_coverage_store_never_fails_the_run(self):
        self.first_run()
        self.later()
        with mock.patch.object(capture_mail.MailCaptureStore, "coverage_rows",
                               side_effect=RuntimeError("boom")):
            run = self.capture().run(scope())
        self.assertTrue(run["complete"], run["incomplete_reasons"])
        self.assertEqual(run["graph_coverage"]["error"], "folder_error:RuntimeError")


class SlowGraph(CountingGraph):
    """A folder whose whole-folder listing pages each take longer than the re-list's
    own deadline (``stuck``: on the live store one folder's last page never finished
    inside RELIST_SECONDS, 2026-11-15)."""

    def __init__(self, ticks: dict) -> None:
        super().__init__()
        self.ticks = ticks
        self.stuck: tuple[str, str] | None = None

    def __call__(self, method, url, *args):
        plain = unquote(url.replace("+", " "))
        if (self.stuck is not None and "receivedDateTime lt 2026-10" in plain
                and self.stuck[0] in plain and self.stuck[1] in plain):
            self.ticks["now"] += capture_mail.RELIST_SECONDS + 1
        return super().__call__(method, url, *args)


class TurnTests(CoverageCase):
    """AUD-09 livelock (2026-11-15): one folder stuck mid re-list stood first every run
    and spent the re-list's time, so 671 short folders never had a turn."""

    def setUp(self) -> None:
        super().setUp()
        self.ticks = {"now": 0.0}
        self.graph = SlowGraph(self.ticks)

    def capture(self, **kwargs):
        kwargs.setdefault("monotonic", lambda: self.ticks["now"])
        return super().capture(**kwargs)

    def held(self, box: str) -> list[str]:
        return sorted(m for m, f in self.stored(box) if f == CUSTOM)

    def test_a_stuck_folder_never_holds_up_the_others_and_resumes_losing_nothing(self):
        self.first_run()
        stuck = [f"A-OLD-{n}" for n in range(10)]
        other = [f"B-OLD-{n}" for n in range(6)]
        self.graph.moved_in(BOX_A, stuck)
        self.graph.moved_in(BOX_A, ["X-OLD"], sender=EXCLUDED_MAIL)
        self.graph.moved_in(BOX_B, other)
        self.graph.stuck = (BOX_A, CUSTOM)   # the larger gap: first under the old order
        self.later()
        run = self.capture().run(scope())
        self.assertTrue(run["complete"], run["incomplete_reasons"])
        self.assertEqual(self.coverage(BOX_A)["status"], "relisting")
        # The next run: the folder never attempted takes its turn first and finishes.
        self.later(0.1)
        run = self.capture().run(scope())
        self.assertEqual(self.coverage(BOX_B)["status"], "whole")
        self.assertEqual(self.held(BOX_B), sorted(["B-1", *other]))
        self.assertGreaterEqual(run["graph_coverage"]["relists_finished"], 1)
        # The stuck folder had its turn too: its failed turn is recorded, its cursor kept.
        row = self.coverage(BOX_A)
        self.assertEqual((row["status"], row["reason"]), ("relisting", "relist_stalled"))
        self.assertIsNotNone(row["relist_next_url"])
        self.assertEqual(run["graph_coverage"]["relist_stalled"], 1)
        # Every run still gives it a turn; once Graph answers in time it resumes from its
        # own cursor: every message once, the excluded person's never.
        self.graph.stuck = None
        for _ in range(5):
            if self.coverage(BOX_A)["status"] != "relisting":
                break
            self.later(0.1)
            self.capture().run(scope())
        row = self.coverage(BOX_A)
        self.assertEqual((row["status"], row["reason"]), ("whole", "recovered"))
        self.assertEqual(self.held(BOX_A), sorted(["CU-1", *stuck]))
        self.assertEqual(len(self.stored(BOX_A)), len(set(self.stored(BOX_A))))
        self.assertFalse(self.store.db.execute(
            "SELECT 1 FROM messages WHERE message_id='X-OLD'").fetchall())
        detail = json.loads(row["detail"])
        self.assertEqual((detail["recovered"], detail["people_excluded"],
                          detail["unexplained"]), (10, 1, 0))

    def test_a_folder_whose_relist_fails_is_recorded_and_the_next_one_goes_on(self):
        self.first_run()
        self.graph.moved_in(BOX_A, [f"A-OLD-{n}" for n in range(10)])
        other = [f"B-OLD-{n}" for n in range(6)]
        self.graph.moved_in(BOX_B, other)
        self.later()
        real = capture_mail.MailCapture._relist

        def failing(capture, box, row, limit):
            if box == BOX_A:
                raise RuntimeError("unexpected")
            return real(capture, box, row, limit)
        with mock.patch.object(capture_mail.MailCapture, "_relist", failing):
            run = self.capture().run(scope())
        self.assertTrue(run["complete"], run["incomplete_reasons"])
        self.assertNotIn("error", run["graph_coverage"])
        self.assertEqual(run["graph_coverage"]["relist_failed"], 1)
        self.assertEqual((self.coverage(BOX_A)["status"], self.coverage(BOX_A)["reason"]),
                         ("short", "folder_error:RuntimeError"))
        self.assertEqual(self.coverage(BOX_B)["status"], "whole")
        self.assertEqual(self.held(BOX_B), sorted(["B-1", *other]))

    def test_relisting_never_extends_the_capture_budget(self):
        self.first_run()
        self.graph.moved_in(BOX_A, [f"A-OLD-{n}" for n in range(6)])
        self.later()
        read = []
        real_turns = capture_mail.MailCapture._turns

        def slow_turns(capture, ordered):
            self.ticks["now"] += 20
            return real_turns(capture, ordered)

        def watched(capture, box, row, limit):
            read.append(box)
            return 0, "cut"
        with mock.patch.object(capture_mail.MailCapture, "_turns", slow_turns), \
                mock.patch.object(capture_mail.MailCapture, "_relist", watched):
            self.capture(budget_seconds=10).run(scope())
        self.assertEqual(read, [])

    def test_the_folder_attempted_longest_ago_goes_first(self):
        self.first_run()
        self.graph.moved_in(BOX_A, [f"A-OLD-{n}" for n in range(10)])
        self.graph.moved_in(BOX_B, [f"B-OLD-{n}" for n in range(6)])
        self.later()
        order = []

        def watched(capture, box, row, limit):
            order.append(box)
            self.ticks["now"] += capture_mail.RELIST_SECONDS + 1   # spends the turn's time
            return 0, "cut"   # reads nothing: each folder stays short
        with mock.patch.object(capture_mail.MailCapture, "_relist", watched):
            for _ in range(4):
                self.capture().run(scope())
                self.later(0.1)
        # one folder a run: never attempted first (the larger gap first), then in turn
        self.assertEqual(order, [BOX_A, BOX_B, BOX_A, BOX_B])

    def test_an_unreadable_attempt_stamp_still_takes_a_turn(self):
        self.first_run()
        self.graph.moved_in(BOX_A, [f"A-OLD-{n}" for n in range(6)])
        self.later()
        self.capture().run(scope())   # compared: short (re-listed and done here)
        self.store.save_coverage(BOX_A, CUSTOM, status="short", relist_counts="[not json",
                                 short=6, reason="awaiting_relist")
        self.later(0.1)
        self.capture().run(scope())
        self.assertNotEqual(self.coverage(BOX_A)["status"], "short")


if __name__ == "__main__":
    import unittest
    unittest.main()
