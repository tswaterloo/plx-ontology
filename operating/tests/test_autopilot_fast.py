"""The autopilot's fast feed (``operating.autopilot_fast``): a Teams message the capture just
stored becomes the same event the regular feed would type, within one cycle, only for a
conversation the people exclusion clears, never twice, and no record is lost or read twice
across cycles.

SYNTHETIC fixtures only (the Teams capture's fake Graph, synthetic ids and addresses);
evidence label: isolated-fixture.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

from integrations.capture_store import NEW_SIGNAL
from integrations.people_exclusion import PeopleExclusion
from integrations.tests import test_capture_teams as tct
from ontology import project
from ontology.schema import load_schema
from operating import autopilot as ap
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
                         "mailbox_id TEXT, message_id TEXT)")
            conn.execute("INSERT INTO messages VALUES (1, ?, 'm0')", (tct.U_COS,))
        marks = self.marks()
        with sqlite3.connect(db) as conn:
            conn.executemany("INSERT INTO messages VALUES (?, ?, ?)", [
                (2, tct.U_COS, "m1"), (3, EXCLUDED, "m2"), (4, "someone@petrasoap.com", "m3")])
        horizon = self.now - timedelta(days=3)
        keys, moved, counts = af.take(self.state, marks, people(), horizon=horizon)
        self.assertEqual(keys["mail"], [(tct.U_COS, "m1")])
        self.assertEqual((counts["not_cleared"], moved["mail"][1]), (2, 4))
        keys, _m, counts = af.take(self.state, marks, people(unavailable=True), horizon=horizon)
        self.assertEqual((keys["mail"], counts["not_cleared"]), ([], 3))


if __name__ == "__main__":
    unittest.main()
