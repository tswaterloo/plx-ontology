"""``serving.fan_out``: per-record reads side by side on a worker pool, the caller reading
too. Every task is run once, results come back in the tasks' order, a busy pool never
holds the caller, the time budget stops new tasks only, and a task's failure is raised."""

from __future__ import annotations

import queue
import threading
import time
from types import SimpleNamespace
import unittest

from operator_api.serving import Worker, fan_out


class FanOutTests(unittest.TestCase):
    def pool(self, width: int, *, start: bool = True):
        jobs, stopped = queue.PriorityQueue(), threading.Event()
        views = []

        def objects_for(session):
            view = SimpleNamespace(_call=lambda n, a: ("helper", session.name, n),
                                   bind=lambda _s: None, close=lambda: None)
            views.append(session.name)
            return view
        threads = []
        for _ in range(width - 1 if start else 0):
            worker = Worker(objects_for, jobs)
            worker.stopped = stopped
            threads.append(threading.Thread(target=worker.loop, daemon=True))
            threads[-1].start()

        def stop():
            stopped.set()
            for thread in threads:
                thread.join(5)
        self.addCleanup(stop)
        return jobs, stopped, views

    reader = SimpleNamespace(session=SimpleNamespace(name="ann", principals={"ann"}))

    def test_every_task_once_in_order_at_each_size(self):
        jobs, stopped, views = self.pool(4)
        for n in (1, 10, 60):
            with self.subTest(n=n):
                ran: list[int] = []

                def task(i):
                    def run(call):
                        time.sleep(0.002)
                        ran.append(i)
                        return (i, call("plx.object", {}))
                    return run
                started = time.monotonic()
                got = fan_out(jobs, stopped, self.reader, lambda n_, a: ("caller", "ann", n_),
                              [task(i) for i in range(n)], lambda: False, width=4)
                self.assertEqual([g[0] for g in got], list(range(n)))
                self.assertEqual(sorted(ran), list(range(n)))
                # each read on the caller's runtime or a helper's workspace of the reader
                self.assertTrue({g[1][1] for g in got} <= {"ann"})
                self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(set(views), {"ann"})

    def test_a_busy_pool_never_holds_the_caller(self):
        jobs, stopped, _views = self.pool(4, start=False)   # no worker takes a job
        got = fan_out(jobs, stopped, self.reader, lambda n, a: "caller",
                      [lambda call, i=i: (i, call("x", {})) for i in range(10)],
                      lambda: False, width=4)
        self.assertEqual(got, [(i, "caller") for i in range(10)])
        cancelled = []
        while not jobs.empty():
            cancelled.append(jobs.get()[2][0].cancelled())
        self.assertEqual(cancelled, [True, True, True])

    def test_the_budget_stops_new_tasks_and_keeps_a_prefix(self):
        jobs, stopped, _views = self.pool(4)
        checks = iter(range(1000))
        got = fan_out(jobs, stopped, self.reader, lambda n, a: None,
                      [lambda call, i=i: i for i in range(30)],
                      lambda: next(checks) >= 7, width=4)
        self.assertEqual(got, list(range(7)))

    def test_a_failing_task_is_raised_after_the_rest_end(self):
        jobs, stopped, _views = self.pool(3)

        def task(i):
            def run(call):
                if i == 4:
                    raise RuntimeError("read failed")
                return i
            return run
        with self.assertRaises(RuntimeError):
            fan_out(jobs, stopped, self.reader, lambda n, a: None,
                    [task(i) for i in range(20)], lambda: False, width=3)


if __name__ == "__main__":
    unittest.main()
