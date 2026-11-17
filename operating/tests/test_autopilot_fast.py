"""The autopilot's fast feed (``operating.autopilot_fast``): a Teams message the capture just
stored becomes the same event the regular feed would type, within one cycle, only for a
conversation the people exclusion clears, never twice, and no record is lost or read twice
across cycles.

SYNTHETIC fixtures only (the Teams capture's fake Graph, synthetic ids and addresses);
evidence label: isolated-fixture.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from integrations.capture_store import NEW_SIGNAL
from integrations.people_exclusion import PeopleExclusion
from integrations.tests import test_capture_teams as tct
from ontology import project
from ontology.schema import load_schema
from operating import autopilot as ap
from operating import autopilot_events
from operating import autopilot_queue
from operating import autopilot_fast as af
from operating import proactive_engine as pe
from operating.chunking.permissions import Exclusion

EXCLUDED = "0e0e0e0e-0000-4000-8000-00000000e0e1"


def people(chat_members=None, *, unavailable: bool = False) -> Exclusion:
    guard = (PeopleExclusion(unavailable="excluded_people_oids_absent") if unavailable
             else PeopleExclusion([EXCLUDED]))
    return Exclusion(guard, directory={tct.COS: tct.U_COS, tct.OTHER: tct.U_OTHER},
                     internal_domains=["petrasoap.com"],
                     chat_members={tct.CHAT: [tct.U_COS, tct.U_OTHER]}
                     if chat_members is None else chat_members)


class FastFeed(tct.CaptureCase):
    def setUp(self) -> None:
        super().setUp()
        self.state = Path(self.tmp.name)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.graph.epoch = self.now - timedelta(hours=2)   # the fixture's messages are recent
        self.assertTrue(self.capture.run(tct.scope())["complete"])
        self.queue = ap.Queue(self.state / "autopilot" / ap.QUEUE_FILE)
        self.addCleanup(self.queue.close)

    def post(self, *ids: str) -> None:
        for message_id in ids:
            self.graph.post_chat(message_id, f"Can you send the COA for lot {message_id}?")
        self.capture.run(tct.scope())

    def marks(self) -> dict:
        return {s: [g, n] for s, (g, _m, n) in af.waiting(self.state, {}).items()}

    def projected(self, name: str, keys=None) -> pe._Store:
        """The fast feed's scratch store of ``keys``, else the whole capture projected (what
        the regular feed reads once the delta layer holds it)."""
        work = self.state / name
        work.mkdir()
        if keys is not None:
            path = af.project(self.state, work, keys, {})
        else:
            path = work / "whole.sqlite"
            with project.capture_source("teams", self.store.path) as src:
                project.build_store(path, load_schema(), {"teams": src})
        st = pe._Store(path, threads=1, file_only=True)
        self.addCleanup(st.close)
        return st

    def test_a_new_message_is_the_regular_feeds_event_only_where_cleared(self) -> None:
        # the capture signals a stored message; a first read starts at the newest version
        self.assertTrue(Path(f"{self.store.path}{NEW_SIGNAL}").is_file())
        marks = self.marks()
        keys, _, counts = af.take(self.state, marks, people(), horizon=self.now - timedelta(days=3))
        self.assertEqual((keys, counts["versions"]), ({"teams": [], "mail": []}, 0))
        self.post("3003")
        keys, moved, counts = af.take(self.state, marks, people(),
                                      horizon=self.now - timedelta(days=3))
        self.assertEqual(len(keys["teams"]), 1, counts)
        self.assertGreater(moved["teams"][1], marks["teams"][1])
        fast = self.projected("fast", keys)
        ids = af.message_ids(fast.path)
        whole = self.projected("whole")
        registry = ap.load_registry()
        got, _ = ap.discover(fast, ids, registry, people(), base=whole, now=self.now)
        want, _ = ap.discover(whole, ids, registry, people(), now=self.now)
        self.assertEqual(len(got), 1)
        self.assertEqual([e["event_id"] for e in got], [e["event_id"] for e in want])
        self.assertEqual((got[0]["mailbox"], got[0]["sender"]), (tct.U_OTHER, tct.U_COS))
        # a chat whose members the exclusion does not clear (one excluded, or none
        # recorded) is never projected, nor anything of it read
        for chats in ({tct.CHAT: [tct.U_COS, EXCLUDED]}, {}):
            keys, _, counts = af.take(self.state, marks, people(chats),
                                      horizon=self.now - timedelta(days=3))
            self.assertEqual((keys["teams"], counts["not_cleared"]), ([], 1))

    def test_a_cycle_prepares_once_and_the_regular_feed_does_not_again(self) -> None:
        with self.queue.conn:
            self.queue.set_meta(af.MARKS, self.marks())
        self.post("3003")
        whole = self.projected("whole")
        gate = lambda *a, **k: True   # noqa: E731 - every workflow passed (gate tested apart)
        blocked = af.cycle(self.state, whole, self.queue, people(unavailable=True),
                           ap.load_registry(), gate=gate, now=self.now, environ={})
        self.assertEqual(blocked["status"], "blocked")       # fails closed, marks kept
        self.assertTrue(any(m < n for _g, m, n in af.waiting(
            self.state, self.queue.meta(af.MARKS)).values()))
        out = af.cycle(self.state, whole, self.queue, people(), ap.load_registry(), gate=gate,
                       now=self.now, environ={})
        self.assertEqual((out["status"], out["events_new"]), ("complete", 1), out)
        self.assertFalse((self.queue.path.parent / af.WORK_DIR).exists())
        (row,) = self.queue.conn.execute("SELECT object_id, state FROM events").fetchall()
        self.assertNotEqual(row["state"], "new")
        again = ap.run_cycle(whole, self.queue, [row["object_id"]], people=people(),
                             gate=gate, now=self.now, environ={}, due=False)
        self.assertEqual(again["events_new"], 0)
        self.assertEqual(af.cycle(self.state, whole, self.queue, people(), ap.load_registry(),
                                  gate=gate, now=self.now, environ={})["taken"]["teams"], 0)

    def test_n_messages_are_each_handled_once_and_timed_from_capture_to_ready(self) -> None:
        """AUDIT3-04 on fixtures: 1, 10 and 60 messages, each one event, one latency sample,
        all in one start of the feed; the measured seconds are printed (fixture timing)."""
        gate = lambda *a, **k: True   # noqa: E731
        with self.queue.conn:
            self.queue.set_meta(af.MARKS, self.marks())
        handled = 0
        for n in (1, 10, 60):
            self.post(*[f"9{n}{i:03d}" for i in range(n)])
            whole = self.projected(f"whole-{n}")
            started = datetime.now(timezone.utc)
            out = af.run(self.state, whole, self.queue, people(), ap.load_registry(),
                         gate=gate, environ={}, clock=lambda: datetime.now(timezone.utc))
            took = (datetime.now(timezone.utc) - started).total_seconds()
            self.assertEqual((out["status"], out["events_new"]), ("complete", n), out)
            handled += n
            self.assertEqual(self.queue.conn.execute(
                "SELECT COUNT(*) FROM events").fetchone()[0], handled)
            found = af.latency(self.queue.meta(af.LATENCY), datetime.now(timezone.utc))
            self.assertEqual(found["messages"], handled)
            self.assertLess(took, af.ROUNDS_SECONDS)
            print(f"\nfast feed, {n} messages: one start {took:.1f} s; captured->ready "
                  f"p50 {found['all']['captured_to_ready_p50_s']} s, "
                  f"p95 {found['all']['captured_to_ready_p95_s']} s")
        self.assertEqual(af.alarm_checks(self.state, datetime.now(timezone.utc))[1]["state"],
                         "ok")

    def test_no_message_lost_or_read_twice_across_cycles(self) -> None:
        for n in (1, 10, 60):
            marks = self.marks()
            ids = [f"{n}{i:03d}" for i in range(n)]
            self.post(*ids)
            seen: list = []
            with mock.patch.object(af, "MAX_RECORDS", 25):
                for _ in range(10):
                    keys, marks, _c = af.take(self.state, marks, people(),
                                              horizon=self.now - timedelta(days=3))
                    if not keys["teams"]:
                        break
                    seen += keys["teams"]
            paths = [p for _k, p in seen]
            self.assertEqual(len(paths), n)
            self.assertEqual(len(set(paths)), n)
            self.assertEqual({p.rsplit("/", 1)[1] for p in paths}, set(ids))

    def test_a_failing_batch_is_retried_then_left_to_the_regular_feed(self) -> None:
        with self.queue.conn:
            self.queue.set_meta(af.MARKS, self.marks())
        before = self.queue.meta(af.MARKS)
        self.post("3003")
        gate = lambda *a, **k: True   # noqa: E731
        with mock.patch.object(af, "project", side_effect=sqlite3.OperationalError("x")):
            outs = [af.cycle(self.state, None, self.queue, people(), ap.load_registry(),
                             gate=gate, now=self.now, environ={}) for _ in range(af.TRIES)]
        self.assertEqual([o["status"] for o in outs], ["failed"] * af.TRIES)
        self.assertEqual([o["left_to_follow"] for o in outs], [False] * (af.TRIES - 1) + [True])
        self.assertFalse((self.queue.path.parent / af.WORK_DIR).exists())
        after = self.queue.meta(af.MARKS)
        self.assertGreater(after["teams"][1], before["teams"][1])   # moved on, not stalled

    def test_mail_is_read_only_of_a_mailbox_the_exclusion_clears(self) -> None:
        db = self.state / "mail-capture" / "capture.db"
        db.parent.mkdir()
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE messages (version_id INTEGER PRIMARY KEY, "
                         "mailbox_id TEXT, message_id TEXT, "
                         "recorded_at TEXT DEFAULT '2026-11-13T11:58:00+00:00', "
                         "record TEXT DEFAULT '{\"receivedDateTime\": \"2026-11-13T11:57:00Z\"}')")
            conn.execute("INSERT INTO messages (version_id, mailbox_id, message_id) "
                         "VALUES (1, ?, 'm0')", (tct.U_COS,))
        marks = self.marks()
        with sqlite3.connect(db) as conn:
            conn.executemany("INSERT INTO messages (version_id, mailbox_id, message_id) "
                             "VALUES (?, ?, ?)", [
                (2, tct.U_COS, "m1"), (3, EXCLUDED, "m2"), (4, "someone@petrasoap.com", "m3")])
        horizon = self.now - timedelta(days=3)
        keys, moved, counts = af.take(self.state, marks, people(), horizon=horizon)
        self.assertEqual(keys["mail"], [(tct.U_COS, "m1")])
        self.assertEqual((counts["not_cleared"], moved["mail"][1]), (2, 4))
        keys, _m, counts = af.take(self.state, marks, people(unavailable=True), horizon=horizon)
        self.assertEqual((keys["mail"], counts["not_cleared"]), ([], 3))


# ----------------------------------------------------------------------------- edges
# Synthetic capture stores (only the columns the fast feed reads) and a stub exclusion, so
# each boundary is set exactly; the cycle's projection and handling are stubbed where the
# test is about what the cycle does around them.

GEN = "gen-1"


class _Clears:
    """A stub exclusion: clears every unit except those named."""

    unavailable = None

    def __init__(self, *blocked: str, error: Exception | None = None) -> None:
        self.blocked, self.error, self.asked = set(blocked), error, []

    def unit_clear(self, unit: str) -> bool:
        self.asked.append(unit)
        if self.error is not None:
            raise self.error
        return unit not in self.blocked


def _row(created: str | None = "2026-11-13T10:00:00Z", kind: str = "chat_message") -> str:
    row = {"kind": kind}
    if created is not None:
        row["created_at"] = created
    return json.dumps({"row": row})


class FastFeedEdges(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.teams, self.mail = self.state / "teams.db", self.state / "mail.db"
        with sqlite3.connect(self.teams) as conn:
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO meta VALUES ('generation', ?)", (GEN,))
            conn.execute("CREATE TABLE versions (capture_id INTEGER PRIMARY KEY, kind TEXT, "
                         "path TEXT, unit TEXT, payload TEXT, "
                         "recorded_at TEXT DEFAULT '2026-11-13T11:58:00+00:00')")
        conn.close()
        with sqlite3.connect(self.mail) as conn:
            conn.execute("CREATE TABLE messages (version_id INTEGER PRIMARY KEY, "
                         "mailbox_id TEXT, message_id TEXT, "
                         "recorded_at TEXT DEFAULT '2026-11-13T11:58:00+00:00', "
                         "record TEXT DEFAULT '{\"receivedDateTime\": \"2026-11-13T11:57:00Z\"}')")
        conn.close()
        patcher = mock.patch.object(af, "stores", lambda _s: {"teams": self.teams,
                                                              "mail": self.mail})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.horizon = datetime(2026, 11, 12, tzinfo=timezone.utc)
        self.queue = ap.Queue(self.state / "autopilot" / ap.QUEUE_FILE)
        self.addCleanup(self.queue.close)

    def versions(self, *rows: tuple) -> None:
        with sqlite3.connect(self.teams) as conn:
            conn.executemany("INSERT INTO versions (capture_id, kind, path, unit, payload) "
                             "VALUES (?, ?, ?, ?, ?)", rows)
        conn.close()

    def messages(self, *rows: tuple) -> None:
        with sqlite3.connect(self.mail) as conn:
            conn.executemany("INSERT INTO messages (version_id, mailbox_id, message_id) "
                             "VALUES (?, ?, ?)", rows)
        conn.close()

    def take(self, marks, people=None):
        return af.take(self.state, marks, people or _Clears(), horizon=self.horizon)

    # -- reading ------------------------------------------------------------------------

    def test_a_mark_of_another_generation_or_past_the_newest_restarts_at_the_newest(self):
        self.versions((5, "teams_chat", "c/1", "u", _row()))
        self.messages((7, "mbx", "m"))
        good = {"teams": [GEN, 3], "mail": ["", 4]}
        self.assertEqual(af.waiting(self.state, good),
                         {"teams": (GEN, 3, 5), "mail": ("", 4, 7)})
        for bad in (["other-gen", 3], [GEN, 9], [GEN, "3"], [GEN, 3, 0], "3", None, 3):
            got = af.waiting(self.state, {"teams": bad})
            self.assertEqual(got["teams"], (GEN, 5, 5), bad)   # nothing before it re-read
        self.mail.unlink()
        self.assertNotIn("mail", af.waiting(self.state, good))   # no store: not read

    def test_unreadable_non_message_and_old_versions_are_passed_never_taken(self):
        self.versions(
            (1, "teams_chat", "c/1", "chat:a", "{not json"),
            (2, "teams_chat", "c/2", "chat:a", json.dumps(["a list"])),
            (3, "teams_chat", "c/3", "chat:a", _row(kind="chat_member")),
            (4, "teams_chat", "c/4", "chat:a", _row(created=None)),
            (5, "teams_chat", "c/5", "chat:a", _row(created="2026-11-11T23:59:59Z")),
            (6, "teams_chat", "c/6", "chat:a", _row(created="2026-11-12T00:00:00Z")),
            (7, "teams_chat", "c/7", "chat:x", _row()),
            (8, "teams_channel", "t/8", "chan:b", _row(kind="channel_message")),
            (9, "teams_member", "m/9", "chat:a", _row()))
        keys, marks, counts = self.take({"teams": [GEN, 0]}, _Clears("chat:x"))
        # the version exactly at the horizon is inside it
        self.assertEqual(keys["teams"], [("teams_chat", "c/6"), ("teams_channel", "t/8")])
        self.assertEqual(counts, {"versions": 5, "not_cleared": 1, "before_horizon": 2})
        self.assertEqual(marks["teams"], [GEN, 9])   # read past all of them, none again

    def test_an_excluded_conversation_is_never_taken_and_a_failing_exclusion_stops_the_read(
            self):
        self.versions((1, "teams_chat", "c/1", "chat:x", _row()),
                      (2, "teams_chat", "c/2", "chat:a", _row()))
        self.messages((1, "excluded-oid", "m1"), (2, "cleared-oid", "m2"))
        people = _Clears("chat:x", "mail:excluded-oid")
        keys, _m, _c = self.take({"teams": [GEN, 0], "mail": ["", 0]}, people)
        self.assertEqual(keys, {"teams": [("teams_chat", "c/2")],
                                "mail": [("cleared-oid", "m2")]})
        self.assertIn("mail:excluded-oid", people.asked)
        with self.assertRaises(RuntimeError):
            self.take({"teams": [GEN, 0]}, _Clears(error=RuntimeError("list unreadable")))

    def test_versions_of_one_message_are_taken_once(self):
        self.versions((1, "teams_chat", "c/1", "chat:a", _row()),
                      (2, "teams_chat", "c/1", "chat:a", _row()),    # the message edited
                      (3, "teams_chat", "c/2", "chat:a", _row()))
        self.messages((1, "mbx", "m1"), (2, "mbx", "m1"))
        keys, _m, counts = self.take({"teams": [GEN, 0], "mail": ["", 0]})
        self.assertEqual(keys["teams"], [("teams_chat", "c/1"), ("teams_chat", "c/2")])
        self.assertEqual(keys["mail"], [("mbx", "m1")])
        self.assertEqual(counts["versions"], 5)

    def test_a_full_scan_or_page_stops_the_mark_at_its_last_row(self):
        self.versions(*[(i, "teams_chat", f"c/{i}", "chat:a", _row(kind="chat_member"))
                        for i in range(1, 6)])
        self.messages(*[(i, "mbx", f"m{i}") for i in range(1, 6)])
        with mock.patch.object(af, "SCAN", 3), mock.patch.object(af, "MAX_RECORDS", 2):
            keys, marks, _c = self.take({"teams": [GEN, 0], "mail": ["", 0]})
            self.assertEqual((marks["teams"], marks["mail"]), ([GEN, 3], ["", 2]))
            taken = list(keys["mail"])
            for _ in range(5):
                keys, marks, _c = self.take(marks)
                taken += keys["mail"]
        self.assertEqual(marks, {"teams": [GEN, 5], "mail": ["", 5]})
        self.assertEqual([m for _b, m in taken], ["m1", "m2", "m3", "m4", "m5"])

    # -- cycle ----------------------------------------------------------------------------

    def cycle(self, people=None, *, environ=None, registry=None, run_cycle=None,
              project=None):
        registry = registry if registry is not None else {"limits": {"cycle_seconds": 900}}
        run_cycle = run_cycle or mock.Mock(return_value={"status": "complete",
                                                         "events_new": 1})
        with mock.patch.object(af, "project", project or mock.Mock(
                return_value=self.state / "x.sqlite")), \
                mock.patch.object(af, "message_ids", return_value=["obj-1"]), \
                mock.patch.object(pe, "_Store") as store, \
                mock.patch.object(ap, "run_cycle", run_cycle):
            out = af.cycle(self.state, None, self.queue, people or _Clears(), registry,
                           gate=lambda *a, **k: True,
                           now=datetime(2026, 11, 13, 12, tzinfo=timezone.utc),
                           environ=environ or {})
        return out, store, run_cycle

    def waiting_mark(self, mark=0):
        with self.queue.conn:
            self.queue.set_meta(af.MARKS, {"teams": [GEN, mark]})
        self.versions((1, "teams_chat", "c/1", "chat:a", _row()))

    def test_the_kill_switch_or_no_exclusion_reads_nothing_and_keeps_the_marks(self):
        self.waiting_mark()
        for kwargs, status in (({"environ": {autopilot_queue.KILL_ENV: "1"}}, "off"),
                               ({"people": mock.Mock(unavailable="absent")}, "blocked")):
            with mock.patch.object(af, "take") as take:
                out, _s, run_cycle = self.cycle(**kwargs)
            self.assertEqual(out["status"], status)
            take.assert_not_called()
            run_cycle.assert_not_called()
            self.assertEqual(self.queue.meta(af.MARKS), {"teams": [GEN, 0]})
        with mock.patch.object(af, "take") as take:   # no exclusion object at all
            out = af.cycle(self.state, None, self.queue, None, {"limits": {}},
                           gate=lambda *a, **k: True, now=datetime.now(timezone.utc),
                           environ={})
        self.assertEqual(out["status"], "blocked")
        take.assert_not_called()

    def test_an_exclusion_failing_mid_read_projects_nothing_and_keeps_the_marks(self):
        self.waiting_mark()
        project = mock.Mock()
        with self.assertRaises(RuntimeError):
            self.cycle(_Clears(error=RuntimeError("list unreadable")), project=project)
        project.assert_not_called()
        self.assertEqual(self.queue.meta(af.MARKS), {"teams": [GEN, 0]})

    def test_nothing_cleared_moves_the_marks_without_projecting(self):
        self.waiting_mark()
        project = mock.Mock()
        out, _s, run_cycle = self.cycle(_Clears("chat:a"), project=project)
        self.assertEqual((out["status"], out["events_new"]), ("complete", 0))
        project.assert_not_called()
        run_cycle.assert_not_called()
        self.assertFalse((self.queue.path.parent / af.WORK_DIR).exists())
        self.assertEqual(self.queue.meta(af.MARKS)["teams"], [GEN, 1])

    def test_handling_gets_the_short_budget_without_changing_the_shared_registry(self):
        self.waiting_mark()
        registry = {"limits": {"cycle_seconds": 900, "per_hour": 5}, "workflows": {}}
        before = json.loads(json.dumps(registry))
        out, store, run_cycle = self.cycle(registry=registry)
        self.assertEqual(out["status"], "complete")
        kwargs = run_cycle.call_args.kwargs
        self.assertEqual(kwargs["registry"]["limits"],
                         {"cycle_seconds": af.CYCLE_SECONDS, "per_hour": 5})
        self.assertEqual((kwargs["due"], kwargs["settle_items"], kwargs["event_queue"]),
                         (False, False, True))
        self.assertIs(kwargs["discover_store"], store.return_value)
        self.assertEqual(registry, before)   # a regular cycle sharing it keeps its budget
        store.return_value.close.assert_called_once()
        self.assertEqual(self.queue.meta(af.MARKS)["teams"], [GEN, 1])

    def test_only_a_handled_batch_moves_the_marks(self):
        for n, (status, moved) in enumerate((("partial", True), ("complete", True),
                                             ("busy", False), ("blocked", False),
                                             ("failed", False)), start=1):
            with self.subTest(status=status):
                with self.queue.conn:
                    self.queue.set_meta(af.MARKS, {"teams": [GEN, n - 1]})
                    self.queue.set_meta(af.FAILED, {"marks": {"x": 1}, "tries": 2})
                self.versions((n, "teams_chat", f"c/{n}", "chat:a", _row()))
                out, _s, _r = self.cycle(run_cycle=mock.Mock(
                    return_value={"status": status, "events_new": 0}))
                self.assertEqual(out["status"], status)
                self.assertEqual(self.queue.meta(af.MARKS)["teams"][1] == n, moved)
                self.assertEqual(self.queue.meta(af.FAILED) == {}, moved)

    def test_a_failure_says_its_kind_only_and_a_new_batch_counts_its_tries_afresh(self):
        self.waiting_mark()
        boom = mock.Mock(side_effect=ValueError("Can you send lot 4471 to jane@example.com"))
        out, store, _r = self.cycle(run_cycle=boom)
        self.assertEqual((out["status"], out["reason"], out["tries"]),
                         ("failed", "ValueError", 1))
        self.assertNotIn("4471", json.dumps(out))
        self.assertNotIn("4471", json.dumps(self.queue.meta(af.FAILED)))
        store.return_value.close.assert_called_once()   # closed though handling raised
        self.assertFalse((self.queue.path.parent / af.WORK_DIR).exists())
        self.assertEqual(self.cycle(run_cycle=boom)[0]["tries"], 2)
        # the marks moved on elsewhere (another batch): its failure starts at one try
        with self.queue.conn:
            self.queue.set_meta(af.MARKS, {"teams": [GEN, 0], "mail": ["", 0]})
        out, _s, _r = self.cycle(run_cycle=boom)
        self.assertEqual((out["tries"], out["left_to_follow"]), (1, False))
        self.assertEqual(self.queue.meta(af.MARKS), {"teams": [GEN, 0], "mail": ["", 0]})
        out, _s, _r = self.cycle()      # handled at last: the failure record is cleared
        self.assertEqual(out["status"], "complete")
        self.assertEqual(self.queue.meta(af.FAILED), {})

    # -- run ------------------------------------------------------------------------------

    def run_rounds(self, outs, left):
        cycle = mock.Mock(side_effect=outs)
        with mock.patch.object(af, "cycle", cycle), \
                mock.patch.object(af, "waiting", side_effect=left):
            out = af.run(self.state, None, self.queue, _Clears(), {}, gate=None, environ={},
                         clock=lambda: datetime.now(timezone.utc))
        return out, cycle

    def test_a_start_runs_while_versions_wait_and_stops_on_a_failed_or_drained_cycle(self):
        done = {"status": "complete", "events_new": 2}
        waits, drained = {"teams": (GEN, 1, 5)}, {"teams": (GEN, 5, 5)}
        out, cycle = self.run_rounds([done, done, done], [waits, waits, drained])
        self.assertEqual((cycle.call_count, out["events_new"], len(out["cycles"])), (3, 6, 3))
        failed = {"status": "failed", "reason": "OperationalError"}
        out, cycle = self.run_rounds([done, failed] + [done] * af.ROUNDS, [waits] * af.ROUNDS)
        self.assertEqual((cycle.call_count, out["status"], out["events_new"]),
                         (2, "failed", 2))
        out, cycle = self.run_rounds([done] * (af.ROUNDS + 5), [waits] * (af.ROUNDS + 5))
        self.assertEqual(cycle.call_count, af.ROUNDS)

    # -- latency and its alarms (AUDIT3-04) -----------------------------------------------

    def test_each_handled_message_leaves_one_latency_sample_of_times_only(self):
        self.waiting_mark()
        self.messages((1, "mbx", "m1"))
        with self.queue.conn:
            self.queue.set_meta(af.MARKS, {"teams": [GEN, 0], "mail": ["", 0]})
        out, _s, _r = self.cycle()
        self.assertEqual(out["latency"]["messages"], 2, out)
        samples = self.queue.meta(af.LATENCY)
        self.assertEqual(sorted(s[1] for s in samples), ["mail", "teams"])
        # times only: ended, source, sent, captured; no id, path, mailbox or text
        self.assertNotIn("mbx", json.dumps(samples))
        self.assertNotIn("c/1", json.dumps(samples))
        teams = next(s for s in samples if s[1] == "teams")
        self.assertEqual(teams[2:], ["2026-11-13T10:00:00Z", "2026-11-13T11:58:00+00:00"])
        found = out["latency"]["all"]
        self.assertEqual(found["messages"], 2)
        self.assertLess(found["captured_to_ready_p95_s"], 200)
        # a batch not handled leaves no sample; the kept samples are capped
        with mock.patch.object(af, "LATENCY_KEEP", 3):
            for n in range(2, 6):
                self.versions((n, "teams_chat", f"c/{n}", "chat:a", _row()))
                self.cycle()
        self.assertEqual(len(self.queue.meta(af.LATENCY)), 3)
        self.versions((9, "teams_chat", "c/9", "chat:a", _row()))
        self.cycle(run_cycle=mock.Mock(return_value={"status": "busy", "events_new": 0}))
        self.assertEqual(len(self.queue.meta(af.LATENCY)), 3)

    def test_latency_percentiles_by_source_and_window(self):
        now = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
        at = lambda m: (now - timedelta(minutes=m)).isoformat()  # noqa: E731
        samples = [[at(0), "teams", at(m + 1), at(1)] for m in range(19)]   # 1..19 min
        samples += [[at(0), "mail", None, at(2)], [at(300), "teams", at(400), at(300)],
                    ["junk"], [at(0), "fax", at(1), at(1)]]
        found = af.latency(samples, now)
        self.assertEqual((found["messages"], found["unreadable"]), (20, 2))
        self.assertEqual(found["teams"]["messages"], 19)
        self.assertEqual(found["teams"]["sent_to_ready_p50_s"], 600)       # 10 min
        self.assertEqual(found["teams"]["sent_to_ready_p95_s"], 1140)      # 19 min
        self.assertEqual(found["mail"]["sent_to_ready_p95_s"], None)       # sent unknown
        self.assertEqual(found["mail"]["captured_to_ready_p95_s"], 120)
        self.assertEqual(af.latency([], now), {"messages": 0, "approvals": 0, "unreadable": 0})

    def alarms(self, now, *, ran=True, samples=(), marks=None):
        with self.queue.conn:
            if ran:
                self.queue.set_meta(af.RAN, (now - timedelta(minutes=1)).isoformat())
            self.queue.set_meta(af.LATENCY, list(samples))
            if marks is not None:
                self.queue.set_meta(af.MARKS, marks)
        return {c["name"]: c for c in af.alarm_checks(self.state, now)}

    def test_the_alarms_page_a_slow_reply_or_a_backlog_and_stay_quiet_while_off(self):
        now = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
        at = lambda m: (now - timedelta(minutes=m)).isoformat()  # noqa: E731
        self.assertEqual(af.alarm_checks(self.state / "none", now), [])    # never set up
        self.assertEqual(self.alarms(now, ran=False), {})                  # never ran
        with self.queue.conn:
            self.queue.set_meta(af.RAN, at(2 * 1440))
        self.assertEqual({c["name"] for c in af.alarm_checks(self.state, now)}, set())
        quick = [[at(0), "teams", at(3), at(1)] for _ in range(10)]
        found = self.alarms(now, samples=quick, marks={})
        self.assertEqual(found["autopilot_reply_latency"]["state"], "ok")
        self.assertEqual(found["autopilot_reply_latency"]["value"], 3.0)
        self.assertEqual(found["autopilot_fast_waiting"]["state"], "ok")
        slow = quick[:5] + [[at(0), "mail", at(25), at(1)] for _ in range(5)]
        found = self.alarms(now, samples=slow)
        self.assertEqual(found["autopilot_reply_latency"]["state"], "breach")
        self.assertIn("p95 25.0 min", found["autopilot_reply_latency"]["detail"])
        # a message the capture stored 15 min ago still waits past the feed's mark
        self.versions((1, "teams_chat", "c/1", "chat:a", _row()))
        with sqlite3.connect(self.teams) as conn:
            conn.execute("UPDATE versions SET recorded_at = ?", (at(15),))
        conn.close()
        found = self.alarms(now, samples=quick, marks={"teams": [GEN, 0]})
        self.assertEqual((found["autopilot_fast_waiting"]["state"],
                          found["autopilot_fast_waiting"]["value"]), ("breach", 15.0))
        # an unreadable queue is unknown, never a quiet ok
        self.queue.close()
        (self.state / "autopilot" / ap.QUEUE_FILE).write_bytes(b"not a database" * 100)
        found = {c["name"]: c for c in af.alarm_checks(self.state, now)}
        self.assertEqual(found["autopilot_reply_latency"]["state"], "unknown")


class _Ctx:
    def __init__(self, log: list, stale_after: int | None = None) -> None:
        self.log, self.stale_after, self.runs, self.closed = log, stale_after, 0, False

    def run(self, clock):
        self.runs += 1
        self.log.append(("run", clock()))
        return {"status": "complete", "events_new": 1}

    def stale(self) -> bool:
        return self.stale_after is not None and self.runs >= self.stale_after

    def close(self) -> None:
        self.closed = True


class Serve(unittest.TestCase):
    """AUDIT3-10: one continuous consumer; a capture's signal wakes a cycle within a poll,
    no signal waits for the backstop, path/timer starts stand down while it runs."""

    setUp = FastFeedEdges.setUp

    T = 1_800_000_000.0

    def signal(self, at: float) -> None:
        path = Path(f"{self.teams}{NEW_SIGNAL}")
        path.write_text("x")
        os.utime(path, (at, at))

    def serve(self, *, touches=(), until=200.0, ctxs=None, idle=None, **kw):
        mono, out, made = [0.0], [], []
        pending = sorted(touches)

        def sleep(s):
            mono[0] += s
            while pending and pending[0][0] <= mono[0]:
                self.signal(pending.pop(0)[1])

        def load():
            ctx = ctxs[len(made)] if ctxs and len(made) < len(ctxs) else None
            ctx = ctx or _Ctx([])
            made.append(ctx)
            return ctx

        with mock.patch.object(af, "idle", return_value=idle):
            ran = af.serve(self.state, self.queue, load, environ={},
                           clock=lambda: datetime.fromtimestamp(self.T + 0.7, timezone.utc),
                           out=out.append, stop=lambda: mono[0] > until, sleep=sleep,
                           monotonic=lambda: mono[0], **kw)
        return ran, out, made

    def test_a_signal_wakes_a_cycle_at_once_else_only_the_backstop_runs(self):
        ran, out, made = self.serve(touches=[(5.0, self.T)])
        # the start, the signal at 5 s, then the backstop at 65, 125 and 185 s
        self.assertEqual(ran, 5)
        self.assertEqual(len(made), 1)                       # loaded once
        self.assertEqual(out[1]["wake_seconds"], 0.7)        # signal -> cycle start
        self.assertNotIn("wake_seconds", out[2])             # a backstop cycle
        self.assertTrue(all(o["serve"] for o in out))
        self.assertTrue(made[0].closed)                      # closed when stopped

    def test_a_new_generation_or_the_reload_time_loads_afresh_and_closes_the_old(self):
        first = _Ctx([], stale_after=1)
        ran, _out, made = self.serve(ctxs=[first], until=130.0)
        self.assertEqual((ran, len(made)), (3, 2))
        self.assertTrue(first.closed and made[1].closed)
        ran, _out, made = self.serve(until=130.0, reload_s=100.0)
        self.assertEqual((ran, len(made)), (3, 2))           # the 120 s cycle reloaded

    def test_nothing_waiting_opens_no_store(self):
        ran, out, made = self.serve(touches=[(5.0, self.T)], idle="idle")
        self.assertEqual((ran, out, made), (0, [], []))

    def test_one_consumer_holds_the_lock_and_a_path_or_timer_start_stands_down(self):
        qdir = self.queue.path.parent
        with af.consumer_lock(qdir, wait=False) as held:
            self.assertTrue(held)
            with af.consumer_lock(qdir, wait=False) as again:
                self.assertFalse(again)
            (self.state / "projection").mkdir()
            (self.state / "projection" / "CURRENT").write_text('{"generation": 1}')
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                code = ap.main(["fast", "--state-dir", str(self.state)])
            self.assertEqual((code, json.loads(printed.getvalue())["status"]), (0, "served"))
        with af.consumer_lock(qdir, wait=False) as held:
            self.assertTrue(held)                            # released on exit

    def test_a_new_release_ends_the_consumer_for_its_unit_to_restart(self):
        a, b, link = self.state / "r1", self.state / "r2", self.state / "current"
        a.mkdir(), b.mkdir(), link.symlink_to(a)
        moved = af.release_moved({af.RELEASE_ENV: str(link)})
        self.assertFalse(moved())
        link.unlink(), link.symlink_to(b)
        self.assertTrue(moved())
        self.assertFalse(af.release_moved({})())

    # AUDIT3-10 re-check: the unit's proactive.env reaches the consumer only at its start
    def env(self, text: str) -> Path:
        path = self.state / "proactive.env"
        path.write_text(text)
        path.chmod(0o600)
        return path

    def unit_env(self, path: Path) -> dict:
        """What systemd gives the restarted consumer from its EnvironmentFile."""
        from integrations.people_exclusion import _ENV_LINE
        found = {}
        for line in path.read_text().splitlines():
            m = _ENV_LINE.match(line)
            if m and not line.lstrip().startswith("#"):
                found[m.group(1)] = m.group(2).strip("\"'")
        return found

    def serve_with_env(self, path: Path, edit: str, at: float = 30.0):
        """Serve under ``env_moved(path)``; ``edit`` is written to the file at ``at`` s."""
        mono, log = [0.0], []
        moved = af.env_moved(path, started=time.time() + 5)

        def sleep(s):
            before = mono[0]
            mono[0] += s
            if before < at <= mono[0]:
                path.write_text(edit)
                st = path.stat()
                os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1))

        stopped = []

        def stop():
            if moved():
                stopped.append(mono[0])
                return True
            return mono[0] > 300

        with mock.patch.object(af, "idle", return_value=None):
            ran = af.serve(self.state, self.queue, lambda: _Ctx(log), environ={},
                           clock=lambda: datetime.fromtimestamp(self.T, timezone.utc),
                           out=lambda _s: None, stop=stop, sleep=sleep,
                           monotonic=lambda: mono[0])
        return ran, stopped

    def test_an_exclusion_added_mid_run_stops_that_persons_processing_within_one_cycle(self):
        from integrations.people_exclusion import ENV_IDS, PeopleExclusion
        other = "0e0e0e0e-0000-4000-8000-00000000e0e2"
        path = self.env(f"{ENV_IDS}={EXCLUDED}\n")
        self.assertFalse(PeopleExclusion.from_env(self.unit_env(path)).is_excluded(other))
        ran, stopped = self.serve_with_env(path, f"{ENV_IDS}={EXCLUDED},{other}\n")
        # the start's cycle ran; the edit at 30 s ended it before the 60 s backstop cycle
        self.assertEqual(ran, 1)
        self.assertEqual(len(stopped), 1)
        self.assertLessEqual(stopped[0], 30.0 + af.POLL_SECONDS)
        # the unit's restart reads the file afresh: that person is now excluded
        self.assertTrue(PeopleExclusion.from_env(self.unit_env(path)).is_excluded(other))

    def test_the_env_off_switch_takes_effect_within_one_cycle(self):
        path = self.env("PLX_AUTOPILOT_OFF=0\n")
        self.assertNotEqual(af.idle(self.state, self.queue, self.unit_env(path)), "off")
        ran, stopped = self.serve_with_env(path, "PLX_AUTOPILOT_OFF=1\n")
        self.assertEqual((ran, len(stopped)), (1, 1))
        self.assertLessEqual(stopped[0], 30.0 + af.POLL_SECONDS)
        self.assertEqual(af.idle(self.state, self.queue, self.unit_env(path)), "off")

    def test_the_env_watch_ignores_a_touch_and_fails_closed(self):
        path = self.env("A=1\n")
        moved = af.env_moved(path, started=time.time() + 5)
        os.utime(path, (time.time() + 1, time.time() + 1))   # same content
        self.assertFalse(moved())
        path.unlink()
        self.assertTrue(moved())                              # removed
        path = self.env("A=1\n")
        # written after the process started (it waited for the lock): ends at once
        self.assertTrue(af.env_moved(path, started=time.time() - 60)())
        self.assertFalse(af.env_moved(self.state / "none.env", started=0.0)())
        self.assertEqual(af.env_file({af.ENV_FILE_ENV: str(path)}), path)

    def test_a_failing_consumer_backs_off_and_pages_after_three_in_a_row(self):
        from operating import ops_alarms
        qdir = self.queue.path.parent
        now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
        self.assertEqual((af.backoff_seconds(qdir, now), af.restart_check(self.state)), (0.0, []))
        waits = []
        for _ in range(3):
            af.serve_failed(qdir, RuntimeError("secret detail"), now)
            waits.append(af.backoff_seconds(qdir, now))
        self.assertEqual(waits, [15.0, 30.0, 60.0])
        self.assertEqual(af.backoff_seconds(qdir, now + timedelta(seconds=50)), 10.0)
        for _ in range(10):
            af.serve_failed(qdir, RuntimeError("x"), now)
        self.assertEqual(af.backoff_seconds(qdir, now), af.BACKOFF_MAX_SECONDS)
        (check,) = af.restart_check(self.state)
        self.assertEqual((check["name"], check["state"], check["value"]),
                         ("autopilot_fast_restarts", "breach", 13))
        self.assertNotIn("secret", check["detail"])           # its kind only
        self.assertIn("autopilot_fast_restarts", ops_alarms.LABELS)
        self.assertIn("autopilot_fast_restarts",
                      [c["name"] for c in af.alarm_checks(self.state, now)])
        af.serve_ok(qdir)
        self.assertEqual(af.restart_check(self.state)[0]["state"], "ok")
        self.assertEqual(af.backoff_seconds(qdir, now), 0.0)

    def test_the_consumer_records_a_failed_load_and_ends_on_an_env_change(self):
        (self.state / "projection").mkdir(exist_ok=True)
        (self.state / "projection" / "CURRENT").write_text('{"generation": 1}')
        import argparse
        a = argparse.Namespace(state_dir=str(self.state), queue=str(self.queue.path), store=None)
        path = self.env("A=1\n")
        os.utime(path, (0, 0))
        env = {af.ENV_FILE_ENV: str(path)}
        with mock.patch.dict(os.environ, env), mock.patch("signal.signal"), \
                mock.patch.object(af, "idle", return_value=None), \
                mock.patch.object(ap, "_FastStart", side_effect=RuntimeError("load")):
            with self.assertRaises(RuntimeError):
                ap._serve_fast(a, {}, None)
        self.assertEqual(af._serve_state(self.queue.path.parent)["failures"], 1)
        path.write_text("A=2\n")      # written after this process started: ends at once
        printed = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch("signal.signal"), \
                mock.patch.object(af, "idle", return_value=None), \
                mock.patch.object(ap, "_FastStart") as start, \
                contextlib.redirect_stdout(printed):
            self.assertEqual(ap._serve_fast(a, {}, None), 0)
        start.assert_not_called()                             # no cycle under the old env
        self.assertEqual(json.loads(printed.getvalue())["reason"], "env_changed")

    def test_serve_is_only_for_the_fast_feed(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            ap.main(["follow", "--serve", "--store", "s", "--queue", "q"])


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------------------ MC approvals (AUDIT3-10)

GATE_EXCLUDED = "ex@petrasoap.com"   # the excluded person's address (directory)


def mc_people(*, unavailable: bool = False) -> Exclusion:
    guard = (PeopleExclusion(unavailable="excluded_people_oids_absent") if unavailable
             else PeopleExclusion([EXCLUDED]))
    return Exclusion(guard, directory={tct.COS: tct.U_COS, tct.VINCE: tct.U_VINCE,
                                       GATE_EXCLUDED: EXCLUDED},
                     internal_domains=["petrasoap.com"], chat_members={})


class FastFeedApprovals(unittest.TestCase):
    """An MC approval gate the capture stores as requested is the regular feed's
    ``approval.requested`` event on the fast path, only when every person it names clears."""

    def setUp(self) -> None:
        from integrations import mc_sources
        from integrations.capture_store import CaptureStore
        from integrations.tests import test_mc_sources as tms
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.mc = mc_sources
        path = mc_sources.store_path(self.state)
        path.parent.mkdir(parents=True)
        self.store = CaptureStore(path, tms.audience())
        self.addCleanup(self.store.close)
        self.signal = Path(f"{path}{NEW_SIGNAL}")
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.task(assignee="vince")
        self.gate("apg_old", status="approved", decided=True)
        self.queue = ap.Queue(self.state / "autopilot" / ap.QUEUE_FILE)
        self.addCleanup(self.queue.close)

    def save(self, collection: str, record_id: str, row: dict, *more: tuple[str, dict]
             ) -> None:
        """One complete run of the collection's unit holding these records (``more``:
        further (record id, row) of the same run)."""
        from integrations.capture_store import CapturedRecord
        unit = self.mc.unit_of("ops", collection)
        run = self.store.begin_run(self.mc.KIND, unit,
                                   self.mc.MC_KIND.resources(("ops", collection, "")))
        records = [CapturedRecord(path=("ops", collection, rid),
                                  payload={"row": {**one, "scope": "ops"},
                                           "meta": {"project": "PRJ-OPS"}},
                                  source_version=f"v-{json.dumps(one, sort_keys=True)}",
                                  lineage=self.mc.LINEAGE)
                   for rid, one in ((record_id, row), *more)]
        self.store.save_page(run["run_id"], records, None)
        self.store.finish_run(run["run_id"], "complete")

    def task(self, **fields) -> None:
        self.save("tasks", "TASK-2", {"id": "TASK-2", "title": "Ship the lot", "stage":
                                      "review", "accountableOwner": "cos", **fields})

    def gate(self, gate_id: str, *, status: str = "pending", decided: bool = False,
             by: str = tct.COS, ago: timedelta = timedelta(minutes=5),
             also: dict | None = None, **fields) -> None:
        """Save a gate of TASK-2 (``also``: another gate's id -> its ``gate`` arguments,
        saved in the same run)."""
        def row_of(gid: str, status: str = status, decided: bool = decided, by: str = by,
                   ago: timedelta = ago, **fields) -> dict:
            at = (self.now - ago).isoformat().replace("+00:00", "Z")
            row = {"id": gid, "task_id": "TASK-2", "status": status, "requestedBy": by,
                   "requestedAt": at, "reason": "Merge the release PR", **fields}
            if decided:
                row.update(decidedBy=tct.VINCE, decidedAt=at)
            return row
        self.save("approvals", gate_id, row_of(gate_id, **fields),
                  *((gid, row_of(gid, **{"status": "pending", "decided": False,
                                         "by": tct.COS, **kw}))
                    for gid, kw in (also or {}).items()))

    def marks(self) -> dict:
        return {s: [g, n] for s, (g, _m, n) in af.waiting(self.state, {}).items()}

    def take(self, marks, people=None, horizon=timedelta(days=3)):
        return af.take(self.state, marks, people or mc_people(), horizon=self.now - horizon)

    def projected(self, name: str, keys=None) -> pe._Store:
        work = self.state / name
        work.mkdir()
        if keys is not None:
            path = af.project(self.state, work, keys, {})
        else:
            path = work / "whole.sqlite"
            with project.capture_source("mission_control", self.store.path) as src:
                project.build_store(path, load_schema(), {"mission_control": src})
        st = pe._Store(path, threads=1, file_only=True)
        self.addCleanup(st.close)
        return st

    def test_a_requested_gate_is_the_regular_feeds_approval_requested_event(self) -> None:
        marks = self.marks()                       # the store's past is follow's
        self.assertEqual(self.take(marks)[0]["mission_control"], [])
        self.signal.unlink(missing_ok=True)
        self.gate("apg_new")
        self.assertTrue(self.signal.is_file())     # a requested gate signals the fast feed
        keys, moved, counts = self.take(marks)
        self.assertEqual(keys["mission_control"],
                         [("mission_control", "ops/approvals/apg_new")], counts)
        self.assertGreater(moved["mission_control"][1], marks["mission_control"][1])
        self.assertEqual(sorted(keys[af.CLOSURE]),          # its task and its other gate
                         [("mission_control", "ops/approvals/apg_old"),
                          ("mission_control", "ops/tasks/TASK-2")])
        fast = self.projected("fast", keys)
        ids = af.message_ids(fast.path, keys)
        self.assertEqual(len(ids), 1)                      # the closure is read, never typed
        whole = self.projected("whole")
        registry = ap.load_registry()
        got, _ = ap.discover(fast, ids, registry, mc_people(), base=whole, now=self.now)
        want, _ = ap.discover(whole, ids, registry, mc_people(), now=self.now)
        self.assertEqual([(e["event_type"], e["status"]) for e in got],
                         [("approval.requested", "pending")])
        self.assertEqual([e["event_id"] for e in got], [e["event_id"] for e in want])

    def test_a_decided_old_or_other_version_is_never_taken_nor_signalled(self) -> None:
        marks = self.marks()
        self.signal.unlink(missing_ok=True)
        self.gate("apg_done", status="approved", decided=True)
        self.gate("apg_rejected", status="rejected")       # a status other than pending
        self.task(title="Ship the lot today")              # a task version
        self.assertFalse(self.signal.exists())
        self.gate("apg_stale", ago=timedelta(days=5))
        keys, moved, counts = self.take(marks)
        self.assertEqual((keys["mission_control"], counts["before_horizon"]), ([], 1))
        self.assertEqual(moved["mission_control"][1], self.marks()["mission_control"][1])

    def test_an_approval_naming_an_excluded_or_undecided_person_is_never_taken(self) -> None:
        marks = self.marks()
        cases = {"requester excluded": dict(by=GATE_EXCLUDED),
                 "requester unknown of ours": dict(by="nobody@petrasoap.com"),
                 "requester missing": dict(by=""),
                 "requester an @handle": dict(by="@ex"),
                 "requester a handle at a dotless host": dict(by="ex@petrasoap"),
                 "requester a bare @": dict(by="@"),
                 "address in its text": dict(note=f"cc {GATE_EXCLUDED.upper()}"),
                 "task assignee excluded": "ex",
                 "task owner a handle no one has": "nobody"}
        for name, case in cases.items():
            with self.subTest(name):
                if isinstance(case, str):
                    self.task(assignee=case)
                    self.gate("apg_case", note=name)
                else:
                    self.task(assignee="vince")
                    self.gate("apg_case", **case)
                keys, moved, counts = self.take(marks)
                self.assertEqual((keys["mission_control"], counts["not_cleared"]), ([], 1))
                marks = moved                                  # read past, never again
        # the same gate once every person clears is read
        self.task(assignee="vince", coassignees=[tct.COS])
        self.gate("apg_case", note="cleared")
        self.assertEqual(len(self.take(marks)[0]["mission_control"]), 1)
        # an exclusion that cannot decide withholds it, a check that raises stops the read,
        # and one that is unavailable reads nothing
        with mock.patch.object(PeopleExclusion, "is_excluded",
                               side_effect=RuntimeError("list unreadable")):
            keys, _m, counts = self.take(marks)
        self.assertEqual((keys["mission_control"], counts["not_cleared"]), ([], 1))
        with mock.patch.object(Exclusion, "identity_clear",
                               side_effect=RuntimeError("list unreadable")), \
                self.assertRaises(RuntimeError):
            self.take(marks)
        self.assertEqual(self.take(marks, mc_people(unavailable=True))[0]["mission_control"],
                         [])
        out = af.cycle(self.state, None, self.queue, mc_people(unavailable=True),
                       ap.load_registry(), gate=lambda *a, **k: True, now=self.now,
                       environ={})
        self.assertEqual(out["status"], "blocked")

    def test_a_cycle_prepares_the_approval_once_and_follow_does_not_again(self) -> None:
        served = self.projected("served")                  # before the request was stored
        self.assertEqual(af.idle(self.state, self.queue, {}), "idle")
        self.assertIn("mission_control", self.queue.meta(af.MARKS))  # marked at its newest
        self.gate("apg_new")
        self.assertIsNone(af.idle(self.state, self.queue, {}))       # waits for a cycle
        gate = lambda *a, **k: True   # noqa: E731 - every workflow passed (gate tested apart)
        out = af.cycle(self.state, served, self.queue, mc_people(), ap.load_registry(),
                       gate=gate, now=self.now, environ={})
        self.assertEqual((out["status"], out["events_new"]), ("complete", 1), out)
        (row,) = self.queue.conn.execute("SELECT object_id, event_type, state FROM events"
                                         ).fetchall()
        self.assertEqual(row["event_type"], "approval.requested")
        # prepared for the approver other than the asker, over a served store that does
        # not hold the gate yet; prepared only (nothing approves)
        items = self.queue.conn.execute("SELECT autopilot, owner FROM items").fetchall()
        self.assertEqual([(i["autopilot"], i["owner"]) for i in items],
                         [("approval_request", tct.U_VINCE)])
        self.assertEqual(out["latency"]["approvals"], 1)
        self.assertEqual(out["latency"]["messages"], 0)
        whole = self.projected("whole")
        again = ap.run_cycle(whole, self.queue, [row["object_id"]], people=mc_people(),
                             gate=gate, now=self.now, environ={}, due=False)
        self.assertEqual(again["events_new"], 0)
        self.assertEqual(af.cycle(self.state, served, self.queue, mc_people(),
                                  ap.load_registry(), gate=gate, now=self.now,
                                  environ={})["taken"].get("mission_control"), 0)

    def items(self, queue) -> list[dict]:
        return [dict(r) for r in queue.conn.execute(
            "SELECT item_id, event_id, autopilot, owner, title, body, audiences, markings, "
            "citations, due_at FROM items ORDER BY item_id")]

    def test_the_fast_item_is_follows_item_for_the_same_gate(self) -> None:
        """Review ee8cf2a71f: the fast feed's item for a new gate states what follow's does
        once the delta holds it, though the served store holds neither the new gate nor the
        decision of the gate before it."""
        self.gate("apg_old", ago=timedelta(hours=2))       # requested 2 h ago, pending
        served = self.projected("served")                  # the delta's last read
        self.gate("apg_old", status="approved", decided=True, ago=timedelta(hours=2))
        self.assertEqual(af.idle(self.state, self.queue, {}), "idle")
        self.gate("apg_new")
        gate = lambda *a, **k: True   # noqa: E731 - every workflow passed (gate tested apart)
        out = af.cycle(self.state, served, self.queue, mc_people(), ap.load_registry(),
                       gate=gate, now=self.now, environ={})
        self.assertEqual((out["status"], out["events_new"]), ("complete", 1), out)
        fast = self.items(self.queue)
        whole = self.projected("whole")
        (oid,) = [r[0] for r in self.queue.conn.execute("SELECT object_id FROM events")]
        follow = ap.Queue(self.state / "follow" / ap.QUEUE_FILE)
        self.addCleanup(follow.close)
        ap.run_cycle(whole, follow, [oid], people=mc_people(), gate=gate, now=self.now,
                     environ={}, due=False)
        want = self.items(follow)
        self.assertEqual([json.loads(i["body"])["prepared"]["recommendation"] for i in want],
                         ["approve"])
        self.assertIn("an earlier approval of this task was granted",
                      json.loads(want[0]["body"])["prepared"]["reasons"][0])
        self.assertEqual(fast, want)

    def test_a_gate_whose_other_gate_names_an_excluded_person_is_left_to_follow(self) -> None:
        self.gate("apg_other", by=GATE_EXCLUDED, ago=timedelta(hours=1))
        marks = self.marks()
        self.gate("apg_new")
        keys, moved, counts = self.take(marks)
        self.assertEqual((keys["mission_control"], counts["left_to_follow"]), ([], 1))
        self.assertNotIn(af.CLOSURE, keys)                 # nothing of it projected
        self.assertGreater(moved["mission_control"][1], marks["mission_control"][1])

    def test_an_approvals_latency_is_its_own_never_the_reply_alarms(self) -> None:
        end = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
        samples = [[end.isoformat(), "mission_control", "2026-11-13T11:00:00Z",
                    "2026-11-13T11:59:00Z"],
                   [end.isoformat(), "teams", "2026-11-13T11:58:00Z", "2026-11-13T11:59:00Z"]]
        got = af.latency(samples, end)
        self.assertEqual((got["messages"], got["approvals"], got["unreadable"]), (1, 1, 0))
        self.assertEqual(got["all"]["sent_to_ready_p95_s"], 120.0)
        self.assertEqual(got["mission_control"]["sent_to_ready_p95_s"], 3600.0)

    # ---- the regular feed (``follow``) applies the same rule (``ap.approvals_cleared``)

    def follow(self, name: str, people=None) -> tuple[set, dict]:
        """The gate ids of the regular feed's approval events over the whole projected
        store (with the MC capture reader live follow opens), and what it skipped."""
        from operating import autopilot_work as aw
        st = self.projected(name)
        st.autopilot_mc = aw.MCQuestions(self.state)
        self.addCleanup(st.autopilot_mc.conn.close)
        events, skipped = ap.discover(st, af.message_ids(st.path), ap.load_registry(),
                                      people or mc_people(), now=self.now)
        return ({e["fields"].get("gate_id") for e in events
                 if e["object_type"] == "MCApproval"}, skipped)

    def test_follow_never_makes_an_event_of_an_approval_naming_an_excluded_person(self) -> None:
        at = self.now.isoformat().replace("+00:00", "Z")
        gate_cases = {"requester excluded": dict(by=GATE_EXCLUDED),
                      "requester excluded, a bare handle": dict(by="ex"),
                      "requester unknown of ours": dict(by="nobody@petrasoap.com"),
                      "requester a handle no one has": dict(by="nobody"),
                      "requester missing": dict(by=""),
                      "requester excluded, an @handle": dict(by="@ex"),
                      "decider excluded": dict(status="approved", decidedBy=GATE_EXCLUDED,
                                               decidedAt=at),
                      "address in its reason": dict(reason=f"cc {GATE_EXCLUDED.upper()}"),
                      "address in its note": dict(note=f"loop in {GATE_EXCLUDED}")}
        task_cases = {"task assignee excluded": dict(assignee="ex"),
                      "task assignee a handle no one has": dict(assignee="nobody"),
                      "task accountable owner excluded": dict(accountableOwner=GATE_EXCLUDED),
                      "task reporter excluded": dict(reporter="ex"),
                      "task reporter unknown of ours": dict(reporter="nobody@petrasoap.com"),
                      "address in the task's text": dict(description=f"ask {GATE_EXCLUDED}")}
        for i, (name, case) in enumerate({**gate_cases, **task_cases}.items()):
            with self.subTest(name):
                gate_id = f"apg_case{i}"
                if name in task_cases:
                    self.task(**{"assignee": "vince", **case})
                    self.gate(gate_id)
                else:
                    self.task(assignee="vince")
                    self.gate(gate_id, **case)
                got, skipped = self.follow(f"case{i}")    # (a save is the unit's whole run:
                self.assertEqual(got, set())                # the store holds this gate only)
                self.assertEqual(skipped["approval_not_cleared"], 1, skipped)
        # every person clears (a bare handle the directory holds is its address): the
        # event is made (a task of its own: the cases' gates are TASK-2's other gates)
        self.save("tasks", "TASK-3", {"id": "TASK-3", "title": "Pack the lot", "stage":
                                      "review", "accountableOwner": "cos", "assignee": "vince",
                                      "reporter": "cos"})
        self.gate("apg_clear", by="vince", task_id="TASK-3")
        got, skipped = self.follow("clear")
        self.assertEqual(got, {"apg_clear"})
        self.assertEqual(skipped["approval_not_cleared"], 0, skipped)

    def test_both_feeds_read_the_people_only_the_capture_rows_hold(self) -> None:
        """Review c99babe87d: a task's co-assignees, its activity and comments and a gate's
        proposal are not projected; follow reads them from the capture rows as the fast
        feed does, and both withhold the approval."""
        marks = self.marks()
        cases = {"task co-assignee excluded": ("task", dict(coassignees=[GATE_EXCLUDED])),
                 "excluded address in a task comment": ("task", dict(comments=[
                     {"author": "cos", "body": f"ask {GATE_EXCLUDED} first"}])),
                 "excluded address in the task's activity": ("task", dict(activity=[
                     {"actor": tct.COS, "text": f"handed to {GATE_EXCLUDED}"}])),
                 "excluded address in the gate's proposal": ("gate", dict(proposal={
                     "action": "merge", "detail": f"reviewed by {GATE_EXCLUDED}"}))}
        for i, (name, (where, case)) in enumerate(cases.items()):
            with self.subTest(name):
                gate_id = f"apg_raw{i}"
                self.task(assignee="vince", **(case if where == "task" else {}))
                self.gate(gate_id, **(case if where == "gate" else {}))
                got, skipped = self.follow(f"raw{i}")
                self.assertEqual((got, skipped["approval_not_cleared"]), (set(), 1), skipped)
                keys, marks, counts = self.take(marks)
                self.assertEqual((keys["mission_control"], counts["not_cleared"]), ([], 1))
        # every person clears: the event is made (a task of its own: the cases' gates are
        # the other gates of TASK-2, and one naming an excluded person leaves it to follow)
        self.save("tasks", "TASK-3", {"id": "TASK-3", "title": "Pack the lot", "stage":
                                      "review", "accountableOwner": "cos", "assignee": "vince",
                                      "coassignees": [tct.COS], "comments": [{"body": "ok"}]})
        self.gate("apg_raw_clear", task_id="TASK-3")
        self.assertEqual(self.follow("raw_clear")[0], {"apg_raw_clear"})
        self.assertEqual(len(self.take(marks)[0]["mission_control"]), 1)

    def test_neither_feed_prepares_an_approval_whose_other_gate_names_an_excluded_person(
            self) -> None:
        """The sibling-gate rule holds on both
        feeds, fail closed. Another gate of the same task naming an excluded person: the
        fast feed makes no event, and follow (with the MC capture reader live follow opens,
        and over the projected store alone) stores and prepares nothing."""
        from operating import autopilot_work as aw
        gate = lambda *a, **k: True   # noqa: E731 - every workflow passed (gate tested apart)
        served = self.projected("served")
        self.assertEqual(af.idle(self.state, self.queue, {}), "idle")
        cases = {"requester": dict(by=GATE_EXCLUDED),
                 "an address in its reason": dict(reason=f"cc {GATE_EXCLUDED}")}
        for i, (name, case) in enumerate(cases.items()):
            with self.subTest(f"sibling's {name}"):
                self.gate(f"apg_new{i}", also={f"apg_other{i}": dict(ago=timedelta(hours=1),
                                                                    **case)})
                out = af.cycle(self.state, served, self.queue, mc_people(),
                               ap.load_registry(), gate=gate, now=self.now, environ={})
                self.assertEqual(out["events_new"], 0, out)
                for reader in (True, False):
                    st = self.projected(f"follow{i}{reader}")
                    if reader:
                        st.autopilot_mc = aw.MCQuestions(self.state)
                        self.addCleanup(st.autopilot_mc.conn.close)
                    queue = ap.Queue(st.path.parent / ap.QUEUE_FILE)
                    self.addCleanup(queue.close)
                    got = ap.run_cycle(st, queue, af.message_ids(st.path), people=mc_people(),
                                       gate=gate, now=self.now, environ={}, due=False)
                    self.assertEqual(got["events_new"], 0, (reader, got))
                    self.assertEqual(queue.conn.execute("SELECT COUNT(*) FROM items"
                                                        ).fetchone()[0], 0)
                self.assertEqual(self.queue.conn.execute("SELECT COUNT(*) FROM items"
                                                         ).fetchone()[0], 0)
        # every gate of the task clears (a task of its own: the cases' gates stay TASK-2's
        # in both stores): follow makes both events, with and without the capture reader
        self.save("tasks", "TASK-3", {"id": "TASK-3", "title": "Pack the lot", "stage":
                                      "review", "accountableOwner": "cos", "assignee": "vince"})
        self.gate("apg_new_ok", task_id="TASK-3",
                  also={"apg_other_ok": dict(ago=timedelta(hours=1), task_id="TASK-3")})
        st = self.projected("ok")
        events, _skipped = ap.discover(st, af.message_ids(st.path), ap.load_registry(),
                                       mc_people(), now=self.now)
        self.assertEqual({e["fields"].get("gate_id") for e in events
                          if e["object_type"] == "MCApproval"}, {"apg_new_ok", "apg_other_ok"})
        self.assertEqual(self.follow("ok_capture")[0], {"apg_new_ok", "apg_other_ok"})

    def test_follow_withholds_every_approval_when_the_exclusion_cannot_decide(self) -> None:
        self.gate("apg_new")
        self.assertIn("apg_new", self.follow("ok")[0])
        with mock.patch.object(PeopleExclusion, "is_excluded",
                               side_effect=RuntimeError("list unreadable")):
            got, skipped = self.follow("undecided")
        self.assertEqual(got, set())
        with mock.patch.object(Exclusion, "identity_clear",
                               side_effect=RuntimeError("list unreadable")):
            got, skipped = self.follow("raises")              # never raised past discover
        self.assertEqual(got, set())
        self.assertEqual(skipped["approval_not_cleared"], 1, skipped)
        out = ap.run_cycle(self.projected("blocked"), self.queue, [], environ={},
                           people=mc_people(unavailable=True), now=self.now, due=False)
        self.assertEqual(out["status"], "blocked")

    def test_follow_never_stores_or_prepares_a_withheld_approval(self) -> None:
        self.task(assignee="vince", accountableOwner=GATE_EXCLUDED)
        self.gate("apg_new")
        st = self.projected("whole")
        out = ap.run_cycle(st, self.queue, af.message_ids(st.path), people=mc_people(),
                           gate=lambda *a, **k: True, now=self.now, environ={}, due=False)
        self.assertEqual(out["events_new"], 0, out)
        self.assertEqual(self.queue.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        self.assertEqual(self.queue.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)

    def test_follow_reads_every_property_the_schema_gives_a_gate_and_its_task(self) -> None:
        schema = load_schema()
        for otype, read in (("MCApproval", autopilot_events.MC_GATE_READ), ("MCTask", autopilot_events.MC_TASK_READ)):
            props = {p["name"] for p in schema.object_types[otype]["properties"]}
            with self.subTest(otype):
                self.assertTrue(props)
                self.assertLessEqual(props, set(read))
