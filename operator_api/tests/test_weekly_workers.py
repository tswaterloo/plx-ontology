"""The weekly review's builds (7-12 min a reader) run on workers of their own: while two of
them are running, a Home long read on the shared read queue (its long slots) starts and
finishes at once. Evidence label: ``isolated-fixture`` (real Worker threads and queues, jobs
that block on an event, made-up readers)."""

from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace

from operator_api import backend_warm as bw
from operator_api import serving as sv

READER = SimpleNamespace(session=SimpleNamespace(principals=frozenset({"reader-x"})))


class View:
    def bind(self, session):
        pass

    def close(self):
        pass


class WeeklyWorkersTests(unittest.TestCase):
    def test_a_home_long_read_is_not_blocked_while_two_weekly_builds_run(self):
        # the API's read workers (3, two long slots) and two weekly workers, as served
        reads = sv.Worker(lambda session: View())
        helpers = [sv.Worker(lambda session: View(), reads.jobs) for _ in range(2)]
        weekly_jobs = __import__("queue").PriorityQueue()
        weekly = [sv.Worker(lambda session: View(), weekly_jobs) for _ in range(2)]
        for w in (reads, *helpers, *weekly):
            threading.Thread(target=w.loop, daemon=True).start()
            self.addCleanup(w.stop)
        backend = SimpleNamespace(reads=reads, weekly_jobs=weekly_jobs, weekly_workers=weekly,
                                  weekly_expected=2)
        release, started = threading.Event(), threading.Semaphore(0)

        def build(view):
            started.release()
            release.wait(30)
            return "built"
        self.addCleanup(release.set)
        results = []
        builders = [threading.Thread(target=lambda: results.append(
            bw.WarmMixin.weekly_run(backend, READER, build)), daemon=True) for _ in range(2)]
        for t in builders:
            t.start()
        for _ in range(2):
            self.assertTrue(started.acquire(timeout=5))   # both builds are running now
        # Home's long reads (alerts, approvals, replies) on the shared queue: all answer
        got = [reads.run(READER, lambda view, n=n: n, timeout=5, long=True) for n in range(4)]
        self.assertEqual(got, [0, 1, 2, 3])
        self.assertEqual(results, [])   # the builds are still going
        release.set()
        for t in builders:
            t.join(5)
        self.assertEqual(results, ["built", "built"])

    def test_control_the_same_builds_on_the_shared_long_slots_do_block_a_home_long_read(self):
        reads = sv.Worker(lambda session: View())
        helpers = [sv.Worker(lambda session: View(), reads.jobs) for _ in range(2)]
        for w in (reads, *helpers):
            threading.Thread(target=w.loop, daemon=True).start()
            self.addCleanup(w.stop)
        release, started = threading.Event(), threading.Semaphore(0)
        self.addCleanup(release.set)

        def build(view):
            started.release()
            release.wait(30)
        futures = [reads.submit(READER, build, long=True) for _ in range(2)]
        for _ in range(2):
            self.assertTrue(started.acquire(timeout=5))
        with self.assertRaisesRegex(TimeoutError, "workbench_busy"):
            reads.run(READER, lambda view: 1, timeout=0.5, long=True)
        release.set()
        for f in futures:
            f.result(timeout=5)

    def test_with_no_weekly_worker_expected_the_read_queue_runs_it_with_no_timeout(self):
        seen = []
        backend = SimpleNamespace(
            weekly_jobs=None, weekly_workers=[], weekly_expected=0,
            reads=SimpleNamespace(run=lambda r, job, timeout="x", long=False:
                                  seen.append((timeout, long)) or "ok"))
        self.assertEqual(bw.WarmMixin.weekly_run(backend, READER, lambda v: 1), "ok")
        self.assertEqual(seen, [(None, True)])


if __name__ == "__main__":
    unittest.main()
