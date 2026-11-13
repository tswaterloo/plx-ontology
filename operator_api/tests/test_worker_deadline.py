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
        clock = [1000.0]
        # room above the floor for one session growing to its peak, not two
        free = [be.ASK_SESSION_FLOOR_GB + 1.5 * be.ASK_SESSION_GB]
        slots = be.SessionSlots(4, available=lambda: free[0], clock=lambda: clock[0])
        slots.POLL_SECONDS = 0.02
        self.assertTrue(slots.acquire(blocking=False))     # the first: always
        self.assertFalse(slots.acquire(blocking=False))    # the first is still growing
        started = time.monotonic()
        self.assertFalse(slots.acquire(timeout=0.1))       # waits, then gives up
        self.assertGreaterEqual(time.monotonic() - started, 0.09)
        # the running session's memory is already out of MemAvailable: once it is past
        # its ramp, the same memory admits the next one (counted twice, it refused it)
        clock[0] += be.ASK_SESSION_RAMP_SECONDS
        self.assertTrue(slots.acquire(blocking=False))
        threading.Timer(0.1, lambda: free.__setitem__(0, 40.0)).start()
        self.assertTrue(slots.acquire(timeout=5))          # memory freed: admitted
        self.assertTrue(slots.acquire(blocking=False))
        self.assertFalse(slots.acquire(blocking=False))    # never past the most
        slots.release()
        self.assertTrue(slots.acquire(blocking=False))
        # under the floor nothing but a first session starts
        low = be.SessionSlots(4, available=lambda: be.ASK_SESSION_FLOOR_GB - 1)
        self.assertTrue(low.acquire(blocking=False))
        clock[0] += 1000
        self.assertFalse(low.acquire(blocking=False))
        # unknown memory: admitted up to the most
        unknown = be.SessionSlots(2, available=lambda: None)
        self.assertTrue(unknown.acquire(blocking=False) and unknown.acquire(blocking=False))
        self.assertFalse(unknown.acquire(blocking=False))

    def test_an_ended_session_reserves_no_memory_and_a_job_carries_its_deadline(self):
        """API-003: a released session's admission no longer counts against the next one
        within the ramp. API-001: a job's deadline is on its future before it is
        queued (a worker that takes it at once still sees it)."""
        import queue
        from operator_api import serving
        clock = [1000.0]
        free = [be.ASK_SESSION_FLOOR_GB + 1.5 * be.ASK_SESSION_GB]
        slots = be.SessionSlots(4, available=lambda: free[0], clock=lambda: clock[0])
        self.assertTrue(slots.acquire(blocking=False))
        slots.release()
        free[0] = be.ASK_SESSION_FLOOR_GB + 1.5 * be.ASK_SESSION_GB   # its memory freed
        self.assertTrue(slots.acquire(blocking=False))     # the first again: always
        other = []
        t = threading.Thread(target=lambda: other.append(slots.acquire(blocking=False)))
        t.start(); t.join()
        self.assertEqual(other, [False])                   # one still ramping: counted
        slots.release()
        self.assertEqual(slots._admitted, [])

        seen = []

        class Jobs(queue.PriorityQueue):
            def put(self, item, *a, **k):
                seen.append(getattr(item[2][0], "plx_deadline", None))
                super().put(item, *a, **k)
        serving.submit(Jobs(), threading.Event(), None, lambda view: None, 0, 123.0)
        self.assertEqual(seen, [123.0])

    def test_waiting_questions_queue_in_order_and_are_told_their_place(self):
        """2026-11-08 graded run: asks waiting for a session were refused as busy after
        their wait. They queue first come, first served, each told how many are ahead,
        and each starts when a session ends."""
        slots = be.SessionSlots(1, available=lambda: 64.0)
        slots.POLL_SECONDS = 0.02
        self.assertTrue(slots.acquire(blocking=False))
        order, told = [], {}

        def ask(name):
            def report(ahead, waited):
                told.setdefault(name, []).append(ahead)
            self.assertTrue(slots.acquire(timeout=10, report=report))
            order.append(name)

        first = threading.Thread(target=ask, args=("first",))
        first.start()
        while slots.waiting() < 1:
            time.sleep(0.01)
        second = threading.Thread(target=ask, args=("second",))
        second.start()
        while slots.waiting() < 2:
            time.sleep(0.01)
        self.assertEqual(told, {"first": [0], "second": [1]})
        slots.release()
        first.join(5)
        self.assertEqual(order, ["first"])
        slots.release()
        second.join(5)
        self.assertEqual(order, ["first", "second"])


class AskDrainCancelTests(unittest.TestCase):
    """API-011: an Ask whose stream ends before its answer (the reader left, or the Ask
    timed out) cancels its job and sets the session's cancel token; the agent session's
    stop reads that token on its next line."""

    def test_a_closed_or_timed_out_drain_cancels_the_session(self):
        import queue
        from concurrent.futures import Future
        from operator_api import errors
        events: "queue.Queue" = queue.Queue()
        events.put(("status", 1))
        future, cancel = Future(), threading.Event()
        stream = be.WorkspaceBackend._drain(None, events, future, 30, cancel)
        self.assertEqual(next(stream), ("status", 1))
        stream.close()                                   # the reader went away
        self.assertTrue(cancel.is_set())
        self.assertTrue(future.cancelled())
        future, cancel = Future(), threading.Event()
        with self.assertRaises(type(errors.ASK_TIMEOUT)):
            list(be.WorkspaceBackend._drain(None, queue.Queue(), future, 0.2, cancel))
        self.assertTrue(cancel.is_set())
        events = queue.Queue()
        events.put(("done", None))
        future, cancel = Future(), threading.Event()
        self.assertEqual(list(be.WorkspaceBackend._drain(None, events, future, 5, cancel)), [])
        self.assertFalse(cancel.is_set())                # an answered Ask cancels nothing

    def test_the_session_stop_reads_the_cancel_token(self):
        from operating import agent_session
        cancel = threading.Event()
        stop = agent_session._either(None, lambda _line: cancel.is_set())
        self.assertFalse(stop("line"))
        cancel.set()
        self.assertTrue(stop("line"))
