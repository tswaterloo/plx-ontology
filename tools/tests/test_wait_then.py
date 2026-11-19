"""tools/wait_then.py: waits for exact conditions, checks the host has room, then runs.

Each case is a false start seen on 11-17 or a fail-closed rule: a quoted mention of the
marker, a PENDING/until line, the waiter's own rule line, a marker already in MASTER when
the wait began, free-text markers, an unknown unit (systemd calls it inactive), a unit's old
success, a failed unit, a host that cannot be reached, a Spark GPU that reports [N/A].
Evidence label: isolated-fixture (systemctl, ssh and exec injected).
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from tools import wait_then as w

M = "[[REBUILD-LIVE-DONE]]"


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.master = self.tmp / "MASTER.md"
        self.ran: list = []
        self.clock = Clock()

    def run_tool(self, argv, *, show=None, probe=None, on_sleep=None):
        def sleep(s):
            self.clock.sleep(s)
            if on_sleep:
                on_sleep(self.clock.t)
        try:
            return w.run(argv, show=show or (lambda u, user: {}), probe=probe or (
                lambda *a: self.fail("no probe expected")),
                execvp=lambda f, a: self.ran.append(a), sleep=sleep, clock=self.clock)
        except w.Stop as stop:
            return stop.code


class MarkerTests(Harness):
    def test_only_an_anchored_unquoted_done_line_counts(self):
        no = [
            f"- 6:36 AM ET aa1117: waiter armed for '{M}' (starts the measure)",   # quoted
            f'- 9:02 AM ET lane: rebuild "{M}" seen?',
            f"- 9:02 AM ET lane: rule: run after `{M}`",
            f"- 2:10 PM ET rebuild-1117: {M} PENDING (apply still running)",
            f"- 2:10 PM ET rebuild-1117: swap waits until {M}",
            f"- 2:50 PM ET me: gate starts once {M} is posted",                      # own rule
            f"- proposal: the rebuild lane should post {M}",
            f"  - 3:00 PM ET nested {M}",                                            # no "- " start
            f"3:00 PM ET rebuild-1117: {M}",
            f"- 3:00 PM ET rebuild-1117: [[REBUILD-LIVE-DONE]]X",
            f"- 3:00 PM ET rebuild-1117: {M} not yet",
        ]
        for line in no:
            self.assertFalse(w.marker_line(line, M), line)
        for line in (f"- {M} 4:12 PM ET rebuild-1117: rows equal, plan a7ceb18b, health 200",
                     f"- 4:12 PM ET rebuild-1117: REBUILD LIVE {M}."):
            self.assertTrue(w.marker_line(line, M), line)

    def test_free_text_markers_are_refused(self):
        for marker in ("REBUILD LIVE", "REBUILD-LIVE-DONE", "[[rebuild live]]", "[[X]]"):
            self.assertEqual(self.run_tool(["--master", str(self.master), "--marker", marker,
                                            "--", "true"]), 3, marker)
        self.assertEqual(self.ran, [])

    def test_a_marker_already_there_never_starts_and_a_new_one_does(self):
        self.master.write_text(f"- {M} 1:00 AM ET yesterday's run\n- other\n")

        def append(t):
            if t >= 90:   # a new done line, prepended by a tmp+mv rewrite
                text = self.master.read_text()
                self.master.write_text(f"- {M} 4:12 PM ET rebuild-1117: live\n" + text)
            elif t >= 30:  # today's false-start shapes arrive first
                self.master.write_text(self.master.read_text()
                                       + f"- 2:50 PM ET waiter armed for '{M}'\n"
                                       + f"- 2:51 PM ET rebuild-1117: {M} PENDING\n")
        code = self.run_tool(["--master", str(self.master), "--marker", M, "--interval", "30s",
                              "--", "echo", "go"], on_sleep=append)
        self.assertEqual((code, self.ran), (0, [["echo", "go"]]))
        self.assertEqual(self.clock.t, 90)

    def test_timeout_without_the_marker_never_runs(self):
        self.master.write_text(f"- {M} old\n")
        code = self.run_tool(["--master", str(self.master), "--marker", M, "--timeout", "5m",
                              "--", "true"])
        self.assertEqual((code, self.ran), (5, []))


class UnitTests(Harness):
    def shows(self, *states):
        states = list(states)

        def show(unit, user):
            return states.pop(0) if len(states) > 1 else states[0]
        return show

    def test_unknown_unit_refuses_instead_of_reading_as_inactive(self):
        show = self.shows({"LoadState": "not-found", "ActiveState": "inactive"})
        self.assertEqual(self.run_tool(["--unit", "rb1117-typo", "--", "true"], show=show), 3)
        self.assertEqual(self.ran, [])

    def test_an_old_success_does_not_start_and_the_next_run_does(self):
        old = {"LoadState": "loaded", "ActiveState": "inactive", "Result": "success",
               "InvocationID": "a"}
        running = dict(old, ActiveState="active", InvocationID="b")
        new = dict(old, InvocationID="b")
        show = self.shows(old, old, old, running, new)
        code = self.run_tool(["--unit", "rb1117-apply", "--", "true"], show=show)
        self.assertEqual((code, self.ran), (0, [["true"]]))

    def test_a_failed_unit_ends_the_wait_without_running(self):
        running = {"LoadState": "loaded", "ActiveState": "active", "InvocationID": "a"}
        failed = dict(running, ActiveState="failed", Result="exit-code")
        code = self.run_tool(["--unit", "rb1117-apply:success", "--", "true"],
                             show=self.shows(running, running, failed))
        self.assertEqual((code, self.ran), (7, []))


class FileTests(Harness):
    def test_a_file_already_there_does_not_count_until_it_changes(self):
        done = self.tmp / "DONE"
        done.write_text("old")

        def touch(t):
            if t >= 60:
                done.write_text("new run")
        code = self.run_tool(["--file", str(done), "--", "true"], on_sleep=touch)
        self.assertEqual((code, self.clock.t), (0, 60))


SPARK = ("MemAvailable:   37727124 kB\n"
         "/dev/nvme0n1p2 3936899620 931000000 2868000000  25% /\n"
         "[N/A]\n")


class PreflightTests(Harness):
    def test_spark_unified_memory_reads_mem_available_as_the_gpus(self):
        fig = w.parse_probe(SPARK)
        self.assertAlmostEqual(fig["gpu"], fig["ram"])
        self.assertAlmostEqual(fig["ram"], 35.98, places=1)
        self.assertAlmostEqual(w.parse_probe(SPARK.replace("[N/A]", "40000\n81000"))["gpu"],
                               39.06, places=1)   # discrete GPUs: the least free one

    def test_short_memory_never_launches(self):
        code = self.run_tool(["--now", "--host", "s7d3d", "--min-gpu-free-gb", "40",
                              "--timeout", "10m", "--", "vllm", "serve"],
                             probe=lambda *a: SPARK)
        self.assertEqual((code, self.ran), (4, []))

    def test_room_then_runs_and_an_unreachable_host_refuses(self):
        seen = []
        code = self.run_tool(["--now", "--host", "s7d3d", "--min-ram-gb", "25",
                              "--min-disk-gb", "100", "--", "go"],
                             probe=lambda host, disk, cfg: seen.append(host) or SPARK)
        self.assertEqual((code, self.ran, seen), (0, [["go"]], ["s7d3d"]))

        def down(*a):
            raise w.Stop(6, "host s7d3d unreachable")
        self.ran.clear()
        self.assertEqual(self.run_tool(["--now", "--host", "s7d3d", "--min-ram-gb", "1",
                                        "--", "go"], probe=down), 6)
        self.assertEqual(self.ran, [])
        with self.assertRaises(w.Stop):
            w.parse_probe("garbage\n")   # unreadable figures refuse


if __name__ == "__main__":
    unittest.main()
