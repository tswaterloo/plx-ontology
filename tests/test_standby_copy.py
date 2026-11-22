"""deploy/spark/standby_copy.py: a stream that moves no bytes is stopped, so it fails."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("standby_copy", ROOT / "deploy/spark/standby_copy.py")
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)


class Proc:
    def __init__(self, pid, ends_at=None):
        self.pid, self.ends_at, self.code, self.stopped = pid, ends_at, None, False

    def poll(self):
        return self.code

    def wait(self):
        return self.code

    def terminate(self):
        self.stopped, self.code = True, 143


class WatchTests(unittest.TestCase):
    def run_watch(self, procs, moves, stall=60):
        clock = {"t": 0.0}

        def sleep(s):
            clock["t"] += s
            for p in procs:
                if p.ends_at is not None and clock["t"] >= p.ends_at and p.code is None:
                    p.code = 0

        with mock.patch.object(sc.os, "killpg", side_effect=OSError):
            return sc.watch(procs, stall, bytes_of=lambda pid: moves(pid, clock["t"]),
                            now=lambda: clock["t"], sleep=sleep, every=30)

    def test_a_stream_that_stops_moving_is_stopped_and_fails(self):
        live, hung = Proc(1, ends_at=600), Proc(2)
        codes = self.run_watch([live, hung],
                               lambda pid, t: int(t) if pid == 1 else min(int(t), 90))
        self.assertEqual(codes, [0, 143])
        self.assertTrue(hung.stopped)
        self.assertFalse(live.stopped)

    def test_slow_but_moving_streams_all_finish(self):
        procs = [Proc(1, ends_at=300), Proc(2, ends_at=900)]
        self.assertEqual(self.run_watch(procs, lambda pid, t: int(t) // 10), [0, 0])
        self.assertFalse(any(p.stopped for p in procs))


class ResendTests(unittest.TestCase):
    """A failed stream is sent again whole before the copy is given up."""

    def test_a_failed_stream_is_sent_again_and_the_copy_goes_on(self):
        sent, rounds = [], iter([[0]])
        got = sc.resend(["p0", "p1", "p2"], [1], 2, 60, start=lambda p: sent.append(p) or p,
                        wait=lambda procs: next(rounds))
        self.assertEqual((got, sent), ([], ["p1"]))

    def test_a_stream_failing_every_retry_still_fails_and_others_are_not_resent(self):
        sent = []
        got = sc.resend(["p0", "p1", "p2"], [0, 2], 2, 60, start=lambda p: sent.append(p) or p,
                        wait=lambda procs: [143 if p == "p2" else 1 for p in procs])
        self.assertEqual(got, [2])            # tar's exit 1 (file changed) counts as sent
        self.assertEqual(sent, ["p0", "p2", "p2"])

    def test_no_retries_leaves_the_failures(self):
        self.assertEqual(sc.resend(["p0"], [0], 0, 60, start=None, wait=None), [0])


class FolderTests(unittest.TestCase):

    def test_slot_folders_are_not_made_but_the_wal_folders_are(self):
        self.assertFalse(sc.folder_made("pg_replslot/plx_sb4ec_archive"))
        self.assertTrue(sc.folder_made("pg_replslot"))
        self.assertTrue(sc.folder_made("pg_wal/archive_status"))
        self.assertTrue(sc.folder_made("base/16385"))


if __name__ == "__main__":
    unittest.main()
