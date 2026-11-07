"""A read job the route stopped waiting for ends at its next store statement
(operator_api/backend.py ``Worker.run``), so the worker serves the next reader's job.
Evidence label: ``isolated-fixture`` (an in-memory SQLite statement, made-up reader)."""

from __future__ import annotations

import sqlite3
import threading
import time
from types import SimpleNamespace
import unittest

from operating.call_limits import interruptible
from operator_api import backend as be

READER = SimpleNamespace(session=SimpleNamespace(principals=frozenset({"reader-x"})))
# A statement that runs for minutes unless interrupted.
SLOW = ("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n "
        "WHERE i < 4000000000) SELECT count(*) FROM n")


class View:
    def bind(self, session):
        pass

    def close(self):
        pass


class WorkerDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.worker = be.Worker(lambda session: View())
        self.thread = threading.Thread(target=self.worker.loop, daemon=True)
        self.thread.start()
        self.addCleanup(self.worker.stop)

    def test_a_timed_out_job_frees_the_worker_at_its_next_statement(self):
        ended = []

        def slow(view):
            conn = sqlite3.connect(":memory:")
            interruptible(conn)
            try:
                return conn.execute(SLOW).fetchone()
            except sqlite3.OperationalError as error:
                ended.append(str(error))
                raise
        with self.assertRaisesRegex(TimeoutError, "workbench_busy"):
            self.worker.run(READER, slow, timeout=0.3)
        began = time.monotonic()
        self.assertEqual(self.worker.run(READER, lambda view: "next", timeout=5), "next")
        self.assertLess(time.monotonic() - began, 2.0)
        self.assertEqual(ended, ["interrupted"])

    def test_a_job_without_a_deadline_and_one_in_time_answer_whole(self):
        def counted(view):
            conn = sqlite3.connect(":memory:")
            interruptible(conn)
            return conn.execute("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 "
                                "FROM n WHERE i < 200000) SELECT count(*) FROM n").fetchone()[0]
        self.assertEqual(self.worker.run(READER, counted, timeout=30), 200000)
        self.assertEqual(self.worker.submit(READER, counted).result(timeout=30), 200000)


if __name__ == "__main__":
    unittest.main()


class SessionSlotsTests(unittest.TestCase):
    """Agent sessions are admitted by the memory available at each admission
    (``backend.SessionSlots``), never by a count fixed at start-up: the first is always
    admitted, another only while memory holds it above the floor, and a session waiting
    for memory starts once memory frees, without any release."""

    def test_admission_follows_the_memory_available_now(self):
        free = [be.ASK_MEMORY_FLOOR_GB + 2.0]     # room for one session only
        slots = be.SessionSlots(4, available=lambda: free[0])
        slots.POLL_SECONDS = 0.02
        self.assertTrue(slots.acquire(blocking=False))     # the first: always
        self.assertFalse(slots.acquire(blocking=False))    # a second does not fit now
        started = time.monotonic()
        self.assertFalse(slots.acquire(timeout=0.1))       # waits, then gives up
        self.assertGreaterEqual(time.monotonic() - started, 0.09)
        threading.Timer(0.1, lambda: free.__setitem__(0, 40.0)).start()
        self.assertTrue(slots.acquire(timeout=5))          # memory freed: admitted
        self.assertTrue(slots.acquire(blocking=False))
        self.assertTrue(slots.acquire(blocking=False))
        self.assertFalse(slots.acquire(blocking=False))    # never past the most asked
        slots.release()
        self.assertTrue(slots.acquire(blocking=False))
        # unknown memory: the request stands
        unknown = be.SessionSlots(2, available=lambda: None)
        self.assertTrue(unknown.acquire(blocking=False) and unknown.acquire(blocking=False))
        self.assertFalse(unknown.acquire(blocking=False))
