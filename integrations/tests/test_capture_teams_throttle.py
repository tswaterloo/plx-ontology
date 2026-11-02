"""Throttled Teams capture, per conversation (coverage audit 2026-10-30: 959 chats paused
on HTTP 429 in one cycle). A
conversation Graph throttles past the client's bounded Retry-After retries waits its own
Retry-After while every other conversation due is read, at any cardinality and with one or
several workers; only reads throttled in a row (Graph throttling the application) stop the
run; and the CLI waits the source's Retry-After (bounded, within the budget) before it
reads the waiting one. Fixtures: the fake Graph of ``test_capture_teams``. Evidence label:
injected-transport."""

from __future__ import annotations

import argparse
import re
import tempfile
import time
import unittest
from urllib.parse import unquote, urlsplit

from integrations import capture_teams, graph_read
from integrations.capture_teams import TeamsCapture
from integrations.live_source_guard import BoundedBackoff
from integrations.tests.test_capture_teams import CHAT, ENV, CAPTURE, scope
from integrations.tests import test_capture_teams as tct
from integrations.tests.test_capture_teams import confirm


class ThrottledScheduleTests(tct.CaptureCase):
    def channels(self, threads: list[str]) -> None:
        self.graph.channel_types = dict.fromkeys(threads, "standard")
        for thread in threads:
            self.graph.roots.setdefault(thread, {})
            self.graph.changed.setdefault(thread, {})

    def capture_with(self, workers: int = 1, sleep=lambda s: None) -> TeamsCapture:
        api, _transport = graph_read.build_graph(
            ENV, sender=self.graph, retry=BoundedBackoff(sleep=lambda s: None,
                                                         jitter=lambda: 0.0))
        return TeamsCapture(self.store, api, self.audience, CAPTURE, workers=workers,
                            sleep=sleep)

    def throttle(self, pattern: str | None) -> None:
        self.graph.failing = {} if pattern is None else {pattern: 429}

    def test_one_throttled_conversation_waits_its_retry_after_while_the_others_are_read(self):
        for total in (1, 10, 60):
            for workers in (1, 4):
                with self.subTest(total=total, workers=workers):
                    self.tearDown()
                    self.setUp()
                    threads = [f"19:thr{n:03d}@thread.tacv2" for n in range(total)]
                    self.channels(threads)
                    capture = self.capture_with(workers)
                    victim = threads[min(2, total - 1)]
                    self.throttle(rf"/channels/{re.escape(victim)}/messages")
                    run = capture.run(scope(refresh_hours=12))
                    status = {c["unit"]: c["status"] for c in run["conversations"]}
                    every = {self.unit(t)[1] for t in threads} | {self.unit(CHAT)[1]}
                    victim_unit = self.unit(victim)[1]
                    # Every conversation is read in this run; only the victim waits.
                    self.assertEqual(set(status), every)
                    self.assertEqual({u for u, st in status.items() if st != "complete"},
                                     {victim_unit})
                    self.assertFalse(run["throttled"])
                    self.assertIn(capture_teams.DEFERRED, run["discovery"]["reasons"])
                    self.assertEqual((run["complete"], run["discovery"]["unvisited"]),
                                     (False, 1))
                    self.assertGreater(run["next_due_seconds"], 0)
                    # Before its Retry-After ends it is not read again, nothing else is due.
                    self.throttle(None)
                    run = capture.run(scope(refresh_hours=12))
                    self.assertEqual((run["conversations"], run["discovery"]["deferred"]),
                                     ([], 1))
                    # After it, it is read and the sweep completes.
                    self.retry_passed()
                    run = capture.run(scope(refresh_hours=12))
                    self.assertEqual([c["unit"] for c in run["conversations"]], [victim_unit])
                    self.assertTrue(run["complete"])
                    self.assertEqual(run["discovery"]["unvisited"], 0)

    def test_reads_throttled_in_a_row_hold_every_lane_then_the_run_goes_on(self):
        threads = [f"19:thr{n:03d}@thread.tacv2" for n in range(12)]
        for workers, holds in ((1, None), (4, None), (1, 1)):
            with self.subTest(workers=workers, holds=holds):
                self.tearDown()
                self.setUp()
                self.channels(threads)
                held: list[float] = []
                capture = self.capture_with(workers, sleep=held.append)
                self.graph.failing = {r"/messages": 429}   # Graph throttles every read
                every = {self.unit(t)[1] for t in threads} | {self.unit(CHAT)[1]}
                streak = max(capture_teams.TENANT_THROTTLE_STREAK, workers)
                original = capture_teams.MAX_TENANT_HOLDS
                capture_teams.MAX_TENANT_HOLDS = holds if holds is not None else original
                try:
                    run = capture.run(scope(refresh_hours=12))
                finally:
                    capture_teams.MAX_TENANT_HOLDS = original
                read = {c["unit"] for c in run["conversations"]}
                self.assertFalse(run["complete"])
                if holds is None:
                    self.assertEqual(read, every)
                    self.assertFalse(run["throttled"])
                    self.assertGreaterEqual(run["tenant_holds"], 1)
                    self.assertTrue(held)
                    self.assertTrue(all(0 < h <= capture_teams.THROTTLE_DEFAULT_SECONDS
                                        for h in held), held)
                else:
                    # Past the holds a run allows, it starts no further conversation.
                    self.assertTrue(run["throttled"])
                    self.assertIn(capture_teams.THROTTLED, run["discovery"]["reasons"])
                    self.assertEqual(len(read), 2 * streak)
                # Once Graph answers again every conversation is read.
                self.graph.failing = {}
                self.retry_passed()
                run = capture.run(scope(refresh_hours=12))
                self.assertTrue(run["complete"])
                self.assertEqual(read | {c["unit"] for c in run["conversations"]}, every)


class ThrottledCliTests(unittest.TestCase):
    SPEC = tct.ProbeTests.SPEC
    argv, many, fixture = tct.ProbeTests.argv, tct.ProbeTests.many, tct.ProbeTests.fixture

    def test_the_cli_reads_the_others_then_waits_the_retry_after_for_the_throttled_one(self):
        graph = self.fixture()
        threads = [f"19:many{n:03d}@thread.tacv2" for n in range(12)]
        self.many(graph, threads)
        victim = threads[3]
        left = {"n": 3}   # the throttled read's attempts (first try and two retries)

        def throttling(method, url, *rest):
            path = unquote(urlsplit(url).path)
            if re.search(rf"/channels/{re.escape(victim)}/messages", path) and left["n"]:
                left["n"] -= 1
                return 429, {"Retry-After": "1"}, b'{"error": {"code": "TooManyRequests"}}'
            return graph(method, url, *rest)
        parser = argparse.ArgumentParser()
        capture_teams.add_cli_arguments(parser)
        slept: list[float] = []

        def sleep(seconds: float) -> None:   # the CLI's wait passes (the store's clock)
            slept.append(seconds)
            if seconds < 1.5:
                time.sleep(seconds)
        with tempfile.TemporaryDirectory() as host:
            confirm(host, list(graph.channel_types))
            args = parser.parse_args(self.argv(host)[1:] + ["--workers", "1"])
            report = capture_teams.run_cli(args, environ={**ENV, "XDG_STATE_HOME": host},
                                           sender=throttling, sleep=sleep,
                                           clock=lambda: 0.0)
        first, second = report["runs"][0], report["runs"][1]
        self.assertFalse(first["throttled"])   # one conversation, not the application
        self.assertEqual(first["retry_after_seconds"], 1.0)
        self.assertIn(capture_teams.DEFERRED, first["discovery"]["reasons"])
        waiting = [c for c in first["conversations"] if c["status"] != "complete"]
        self.assertEqual(len(waiting), 1)
        self.assertIn("http_429", " ".join(waiting[0]["reasons"]))
        # Run 1 read every conversation (2 channels, 12 more, the chat); run 2 the one.
        self.assertEqual((len(report["runs"]), len(first["conversations"])), (2, 15))
        wait = report["throttle_waits"][-1]   # the source's own wait, less the time passed
        self.assertTrue(0.0 < wait <= 1.0, wait)
        self.assertIn(wait, [round(s, 1) for s in slept])
        last = report["runs"][-1]
        self.assertEqual([c["status"] for c in last["conversations"]], ["complete"])
        self.assertTrue(report["pass"]["last_run_complete"])
        self.assertFalse(second["throttled"])


if __name__ == "__main__":
    unittest.main()
