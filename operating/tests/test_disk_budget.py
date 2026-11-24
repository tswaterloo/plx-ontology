"""The one disk budget (speed specs P3): grants while free - floor - reservations covers the
need, refuses with the numbers otherwise, drops expired reservations, and holds under
concurrent callers (flock)."""
from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import time
import unittest
from unittest import mock

from operating import disk_budget as db

GB = 1024 ** 3


def _grab(state_dir, job, q):
    env = {"PLX_STATE_DIR": state_dir}
    with mock.patch.object(db, "free_gb", return_value=db.FLOOR_GB + 10):
        try:
            db.reserve(job, 6, environ=env)
            q.put(("granted", job))
        except db.Refused:
            q.put(("refused", job))


class Budget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"PLX_STATE_DIR": self.tmp.name}
        free = mock.patch.object(db, "free_gb", return_value=db.FLOOR_GB + 10)
        free.start()
        self.addCleanup(free.stop)

    def test_reserve_refuse_release_and_one_floor(self):
        got = db.reserve("build:stock", 6, environ=self.env)
        self.assertEqual(got.gb, 6)
        with self.assertRaises(db.Refused) as err:
            db.reserve("build:emails", 5, environ=self.env)
        self.assertEqual((err.exception.need_gb, round(err.exception.available_gb, 2)), (5, 4))
        self.assertIn("needs 5.0 GB", str(err.exception))
        st = db.status(environ=self.env)
        self.assertEqual((st["floor_gb"], st["available_gb"]), (db.FLOOR_GB, 4.0))
        # a job's own reservation is replaced, not added
        db.reserve("build:stock", 7, environ=self.env)
        self.assertEqual(db.status(environ=self.env)["available_gb"], 3.0)
        self.assertTrue(db.release("build:stock", environ=self.env))
        self.assertTrue(db.check(10, environ=self.env))
        self.assertFalse(db.check(10.5, environ=self.env))
        # the floor is the shared definition (operating.disk_floor reads it)
        from operating import disk_floor
        self.assertEqual(disk_floor.DISK_FLOOR_GB["/data"], db.FLOOR_GB)

    def test_expired_reservations_are_dropped(self):
        db.reserve("old", 9, ttl_s=1, environ=self.env)
        with mock.patch.object(db.time, "time", return_value=time.time() + 5):
            self.assertEqual(db.status(environ=self.env)["reservations"], {})

    def test_two_processes_cannot_both_take_the_last_room(self):
        q = multiprocessing.get_context("fork").Queue()
        procs = [multiprocessing.get_context("fork").Process(
            target=_grab, args=(self.tmp.name, f"job{i}", q)) for i in range(2)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(30)
        got = sorted(q.get(timeout=5)[0] for _ in procs)
        self.assertEqual(got, ["granted", "refused"])

    def test_status_time_does_not_grow_with_reservations(self):
        took = {}
        for n in (1, 10, 60):
            with open(os.path.join(self.tmp.name, db.STATE_NAME), "w") as fh:
                json.dump({"reservations": {f"j{i}": {"gb": 0.01, "until": time.time() + 600}
                                            for i in range(n)}}, fh)
            t0 = time.monotonic()
            st = db.status(environ=self.env)
            took[n] = time.monotonic() - t0
            self.assertEqual(len(st["reservations"]), n)
        self.assertLess(took[60], 3 * took[1] + 0.05)

    def test_a_bad_floor_setting_is_refused(self):
        with self.assertRaises(RuntimeError):
            db.floor_gb({"PLX_DISK_FLOOR_GB_DATA": "lots"})
        self.assertEqual(db.floor_gb({"PLX_DISK_FLOOR_GB_DATA": "30"}), 30.0)


if __name__ == "__main__":
    unittest.main()
