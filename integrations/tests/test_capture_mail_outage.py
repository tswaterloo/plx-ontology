"""A Graph outage on every mailbox read ends the mail run early and typed, never by its
budget with nothing written (2026-11-12: Graph answered every mailFolders, messages
and calendar read HTTP 502 after 21 s or 503/504 for the whole tenant, from both
applications; each mailbox listing took about a minute of retries, 166 of them outlived
the run's two-hour budget, the ingest runner killed the child and nothing was stored or
reported for a day). Over an injected Graph, no network and no credentials. Evidence
label: ``injected-transport``."""

from __future__ import annotations

import unittest
from urllib.parse import unquote, urlsplit

from integrations import capture_mail, graph_read
from integrations.capture_mail import (OUTAGE_MAILBOXES, SOURCE_UNAVAILABLE, MailCapture)
from integrations.graph_read import GraphMailReader
from integrations.live_source_guard import BoundedBackoff
from integrations.tests.test_capture_mail import CAPTURE, ENV, j
from integrations.tests import test_capture_mail_throughput as throughput


def mail_read(url: str) -> bool:
    path = unquote(urlsplit(url).path)
    return "/mailFolders" in path or "/messages" in path


class OutageTests(throughput.ThroughputCase):
    add_mailbox = throughput.LanesAndPriorityTests.add_mailbox

    def setUp(self) -> None:
        super().setUp()
        self.down: set[str] | None = None   # mailbox ids whose mail reads fail; empty: all

    def answer(self, method, url, headers, body, timeout, max_bytes):
        path = unquote(urlsplit(url).path)
        if self.down is not None and mail_read(url) and (
                not self.down or any(f"/users/{box}/" in path for box in self.down)):
            self.graph.calls.append((method, url, dict(headers)))
            return j(502, {"error": {"code": "UnknownError", "message": ""}})
        return self.graph(method, url, headers, body, timeout, max_bytes)

    def boxes(self, n: int) -> list[tuple[str, str]]:
        made = [self.add_mailbox(k) for k in range(1, n + 1)]
        for box, _upn in made:
            self.graph.put(box, f"M-{box[-4:]}", "CK-1")
        return made

    def run_once(self, upns, **kwargs) -> dict:
        api, _transport = graph_read.build_graph(
            ENV, sender=self.answer,
            retry=BoundedBackoff(sleep=self.sleeps.append, jitter=lambda: 0.0))
        audience = capture_mail.LiveSourceAudience(
            {f"mail:{u}": ["svc:mail-capture"] for u in upns}, tenant="plx",
            classifications={"mail:*": "restricted"})
        capture = MailCapture(self.store, GraphMailReader(api, audience), CAPTURE,
                              sleep=self.sleeps.append, page_size=2,
                              **{"budget_seconds": 7200, **kwargs})
        return capture.run(throughput.lane_scope(upns))

    def mail_requests(self) -> int:
        return sum(1 for _m, url, _h in self.graph.calls if mail_read(url))

    def held(self) -> list[str]:
        return sorted(m for m, _f in self.stored())

    def test_an_outage_on_every_mailbox_stops_the_run_typed_and_the_next_run_captures(self):
        for n in (1, 10, 60):
            with self.subTest(mailboxes=n):
                self.tearDown()
                self.setUp()
                made = self.boxes(n)
                upns = [upn for _box, upn in made]
                self.down = set()   # every mailbox read fails
                run = self.run_once(upns)
                failing = min(n, OUTAGE_MAILBOXES)
                # Each failing mailbox: its listing and its fallback folders' first
                # reads, each retried; past the fifth nothing more is requested.
                self.assertLessEqual(self.mail_requests(), failing * 3 * 3,
                                     self.mail_requests())
                self.assertFalse(run["complete"])
                self.assertEqual(run["source_down"], n >= OUTAGE_MAILBOXES)
                self.assertEqual(SOURCE_UNAVAILABLE in run["incomplete_reasons"],
                                 n >= OUTAGE_MAILBOXES, run["incomplete_reasons"])
                self.assertEqual(self.held(), [])
                # Graph answers again: the next run reads every mailbox, none lost or
                # doubled.
                self.down = None
                again = self.run_once(upns)
                self.assertFalse(again["source_down"])
                self.assertNotIn(SOURCE_UNAVAILABLE, again["incomplete_reasons"])
                self.assertEqual(self.held(), sorted(f"M-{b[-4:]}" for b, _u in made))

    def test_one_mailbox_failing_never_stops_the_others(self):
        made = self.boxes(OUTAGE_MAILBOXES + 3)
        upns = [upn for _box, upn in made]
        self.down = {made[0][0]}
        run = self.run_once(upns)
        self.assertFalse(run["source_down"])
        self.assertNotIn(SOURCE_UNAVAILABLE, run["incomplete_reasons"])
        self.assertEqual(self.held(), sorted(f"M-{b[-4:]}" for b, _u in made[1:]))

    def test_a_spent_budget_lists_no_more_folders(self):
        made = self.boxes(10)
        upns = [upn for _box, upn in made]

        def listings() -> int:   # the clock: one second per folder listing sent
            return sum(1 for _m, url, _h in self.graph.calls
                       if unquote(urlsplit(url).path).endswith("/mailFolders"))
        run = self.run_once(upns, budget_seconds=3, monotonic=listings)
        self.assertEqual(listings(), 3)
        self.assertFalse(run["source_down"])
        self.assertIn("time_budget", run["incomplete_reasons"])
        self.assertIn("folder_listing_incomplete", run["incomplete_reasons"])

    def test_throttling_is_never_an_outage(self):
        self.assertNotIn("http_429", capture_mail._OUTAGE)
        self.assertTrue({"http_502", "http_503", "http_504", "timeout"}
                        <= capture_mail._OUTAGE)


if __name__ == "__main__":
    unittest.main()
