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
