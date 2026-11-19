"""One shared Graph throttle, per-operation request counts and permission reads only when
due (evidence: injected-transport; no network, no credentials)."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

from integrations import capture_sharepoint_lists as csl
from integrations import graph_read
from integrations.capture_sharepoint_lists import ListCapture
from integrations.graph_throttle import (MAX_WAIT_SECONDS, ThrottleGate, ThrottleHeld,
                                         scope_of)
from integrations.live_source_guard import GuardedTransport
from integrations.tests.test_capture_sharepoint_lists import (
    CAPTURE, ENV, LIST, OTHER, READER, SITE, CaptureCase, FakeListGraph, audience, scope)
from operating.ingest_forever import summarize_sharepoint

SP = f"https://graph.microsoft.com/v1.0/sites/{SITE}/lists/{LIST}"


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def transport(path: Path, clock: Clock, sender) -> GuardedTransport:
    return GuardedTransport(sender, environ=ENV, throttle=ThrottleGate(
        path, sleep=clock.sleep, now=clock.now))


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "throttle.json"
        self.clock = Clock()
        self.sent: list[str] = []

    def sender(self, answers):
        def send(method, url, headers, body, timeout, max_bytes):
            self.sent.append(url)
            status, headers = answers(len(self.sent))
            return status, headers, b"{}"
        return send

    def test_a_429_on_one_transport_holds_the_others_for_its_retry_after(self):
        one = transport(self.path, self.clock, self.sender(
            lambda n: (429, {"Retry-After": "20"}) if n == 1 else (200, {})))
        other = transport(self.path, self.clock, self.sender(lambda n: (200, {})))
        self.assertEqual(one.request("GET", SP, {})[0], 429)
        before = self.clock.t
        self.assertEqual(other.request("GET", SP, {})[0], 200)   # another process
        self.assertGreaterEqual(self.clock.t - before, 20)
        self.assertEqual(sum(self.clock.slept), self.clock.t - before)
        self.assertEqual(other.throttle.waited_seconds, 20)
        # held no longer: the next request waits for nothing
        slept = len(self.clock.slept)
        other.request("GET", SP, {})
        self.assertEqual(len(self.clock.slept), slept)

    def test_a_hold_past_the_wait_bound_refuses_the_request_without_sending_it(self):
        one = transport(self.path, self.clock, self.sender(
            lambda n: (429, {"Retry-After": str(int(MAX_WAIT_SECONDS) + 60)})))
        one.request("GET", SP, {})
        sent = len(self.sent)
        with self.assertRaises(ThrottleHeld) as held:
            one.request("GET", SP, {})
        self.assertEqual(len(self.sent), sent, "a refused request is never sent")
        self.assertEqual((held.exception.http_status, held.exception.error_code),
                         (429, "throttle_hold"))
        self.assertGreater(held.exception.retry_after_seconds, MAX_WAIT_SECONDS)
        self.clock.t += MAX_WAIT_SECONDS + 61     # the hold ends: reads go on
        self.assertEqual(one.request("GET", SP, {})[0], 429)

    def test_a_429_without_retry_after_backs_off_and_doubles_while_it_lasts(self):
        t = transport(self.path, self.clock, self.sender(lambda n: (429, {})))
        left = []
        for _ in range(3):
            self.clock.t += 10_000
            t.request("GET", SP, {})
            left.append(t.throttle.left("sharepoint"))
        self.assertEqual(left, [30.0, 60.0, 120.0])

    def test_only_sharepoint_scope_reads_share_a_hold_and_the_token_never_does(self):
        self.assertEqual(scope_of(SP), "sharepoint")
        self.assertEqual(scope_of("https://graph.microsoft.com/v1.0/drives/x/items/y"),
                         "sharepoint")
        self.assertIsNone(scope_of("https://graph.microsoft.com/v1.0/users/a/messages"))
        t = transport(self.path, self.clock, self.sender(lambda n: (429, {"Retry-After": "500"})))
        t.request("GET", SP, {})
        sent = len(self.sent)
        t.request("GET", "https://graph.microsoft.com/v1.0/users/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", {})
        self.assertEqual(GuardedTransport._classify(
            "POST", "https://login.microsoftonline.com/t/oauth2/v2.0/token"), ("token", None))
        self.assertEqual(len(self.sent), sent + 1)

    def test_a_corrupt_or_unwritable_state_file_means_no_hold_never_a_failure(self):
        self.path.write_text("{not json")
        t = transport(self.path, self.clock, self.sender(lambda n: (200, {})))
        self.assertEqual(t.request("GET", SP, {})[0], 200)
        t = transport(self.path / "no" / "dir", self.clock, self.sender(lambda n: (429, {})))
        self.assertEqual(t.request("GET", SP, {})[0], 429)

    def test_a_drain_request_ends_the_wait_at_once(self):
        from integrations import drain
        t = transport(self.path, self.clock, self.sender(lambda n: (429, {"Retry-After": "60"})))
        t.request("GET", SP, {})
        drain.request()
        self.addCleanup(drain.reset)
        before = len(self.clock.slept)
        with self.assertRaises(ThrottleHeld):
            t.request("GET", SP, {})
        self.assertEqual(len(self.clock.slept), before)

    def test_requests_are_counted_by_operation_and_throttle(self):
        t = transport(self.path, self.clock, self.sender(
            lambda n: (429, {"Retry-After": "1"}) if n == 2 else (200, {})))
        for _ in range(3):
            t.request("GET", f"{SP}/items/1/permissions", {})
        t.request("GET", SP, {})
        mix = t.request_mix()
        self.assertEqual(mix["graph.list_item_permissions"], {"sent": 3, "throttled": 1})
        self.assertEqual(mix["graph.site_list"], {"sent": 1, "throttled": 0})
        self.assertEqual(mix["_gate"]["holds_seen"], 1)
        self.assertNotIn(SITE, json.dumps(mix))


class Listing(FakeListGraph):
    """The fake list Graph with a throttle that starts after ``throttle_after`` item
    permission reads and answers every read 429 with a long Retry-After."""

    def __init__(self, items: int = 5) -> None:
        super().__init__(items)
        self.throttle_after: int | None = None
        self.permission_reads = 0

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        if re.search(r"/permissions$", unquote(urlsplit(url).path)):
            self.permission_reads += 1
            if self.throttle_after is not None and self.permission_reads > self.throttle_after:
                self.calls.append((method, url))
                return 429, {"Retry-After": "3000"}, b"{}"
        if (self.throttle_after is not None
                and self.permission_reads > self.throttle_after):
            self.calls.append((method, url))
            return 429, {"Retry-After": "3000"}, b"{}"
        return super().__call__(method, url, headers, body, timeout, max_bytes)


class ListPermissionTests(CaptureCase):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.clock = Clock()
        self.graph = Listing()
        gate = ThrottleGate(Path(tmp.name) / "t.json", sleep=self.clock.sleep,
                            now=self.clock.now)
        from integrations.live_source_guard import BoundedBackoff
        api, self.transport = graph_read.build_graph(
            ENV, sender=self.graph, retry=BoundedBackoff(sleep=lambda s: None))
        self.transport.throttle = gate
        self.capture = ListCapture(self.store, api, self.audience, CAPTURE)

    def permission_gets(self) -> int:
        return sum(1 for p in self.graph.gets() if p.endswith("/permissions"))

    def test_unchanged_items_reuse_their_permissions_and_cost_no_request(self):
        for n in (1, 10, 60):
            with self.subTest(items=n):
                self.graph.lists[LIST]["items"] = {}
                for i in range(1, n + 1):
                    self.graph.add(i, version=n)   # a new eTag per cardinality: read once
                self.graph.calls.clear()
                first = self.capture.run(scope(page_size=7))["lists"][0]
                self.assertEqual((first["status"], first["counts"]["acl_reused"]),
                                 ("complete", 0))
                self.assertEqual(self.permission_gets(), n)
                self.graph.calls.clear()
                second = self.capture.run(scope(page_size=7))["lists"][0]
                self.assertEqual(second["status"], "complete", second)
                self.assertEqual(self.permission_gets(), 0)
                self.assertEqual((second["counts"]["acl_reused"], second["counts"]["acl_read"],
                                  second["counts"]["versions_new"],
                                  second["counts"]["unchanged"]), (n, n, 0, n))
                rows = self.store.records(READER, csl.LIST_KIND.name, self.unit,
                                          limit=1000)["items"]
                self.assertEqual(sorted(int(r["key"]["id"]) for r in rows),
                                 list(range(1, n + 1)))

    def test_a_changed_etag_an_unread_acl_and_a_come_round_slot_are_read_again(self):
        self.capture.run(scope())
        self.graph.add(2, version=2)                       # eTag changed
        self.graph.calls.clear()
        self.capture.run(scope())
        self.assertEqual(self.permission_gets(), 1)
        import datetime as dt
        real = self.store._clock
        start = real()
        self.addCleanup(setattr, self.store, "_clock", real)
        self.store._clock = lambda: start + dt.timedelta(seconds=csl.ACL_REFRESH_SECONDS)
        self.graph.calls.clear()
        self.capture.run(scope())                          # every slot came round
        self.assertEqual(self.permission_gets(), 5)
        # permissions that failed to read are saved unreadable and never reused
        self.store._clock = lambda: start + dt.timedelta(seconds=2 * csl.ACL_REFRESH_SECONDS)
        self.graph.v1_permissions = 500
        run = self.capture.run(scope())["lists"][0]
        self.assertGreaterEqual(run["counts"]["acl_unreadable"], 3)
        self.graph.v1_permissions = None
        self.graph.calls.clear()
        run = self.capture.run(scope())["lists"][0]
        self.assertGreaterEqual(self.permission_gets(), 3)
        self.assertEqual(run["counts"]["acl_reused"] + self.permission_gets(), 5)

    def test_a_throttle_hold_stops_the_run_saves_nothing_unreadable_and_the_next_run_resumes(self):
        self.graph.lists[LIST]["items"] = {}
        for i in range(1, 31):
            self.graph.add(i)
        self.graph.throttle_after = 12
        self.graph.calls.clear()
        run = self.capture.run(scope(page_size=10))
        result = run["lists"][0]
        self.assertEqual((result["status"], result["reasons"]),
                         ("incomplete", [csl.THROTTLE_REASON]), result)
        self.assertEqual(result["counts"]["acl_unreadable"], 0, "nothing saved unreadable")
        sent_when_held = len(self.graph.calls)
        self.assertLess(sent_when_held, 30, self.graph.calls)
        self.assertEqual(self.transport.request_mix()["_gate"]["refused"], 1)
        # a second pass while held sends nothing
        again = self.capture.run(scope(page_size=10))["lists"][0]
        self.assertEqual(again["reasons"], [csl.THROTTLE_REASON])
        self.assertLessEqual(len(self.graph.calls), sent_when_held + 1)
        # the hold ends; the run resumes from its cursor and completes
        self.graph.throttle_after = None
        self.clock.t += 4000
        done = self.capture.run(scope(page_size=10))["lists"][0]
        self.assertEqual((done["status"], done["resumed_from"]), ("complete", "cursor"), done)
        rows = self.store.records(READER, csl.LIST_KIND.name, self.unit, limit=1000)["items"]
        self.assertEqual(len(rows), 30)

    def test_the_run_report_names_the_request_mix(self):
        self.capture.run(scope())
        mix = self.transport.request_mix()
        self.assertEqual(mix["graph.list_item_permissions"]["sent"], 5)
        self.assertEqual(mix["graph.list_items"]["sent"], 1)


class HostReasonTests(unittest.TestCase):
    def test_a_held_estate_run_is_reported_as_the_throttle_not_as_complete(self):
        report = {"runs": [{"targets": [{"roots": [{"complete": True, "reason": None}]}]}],
                  "throttle_hold": True}
        summary = summarize_sharepoint(report)
        self.assertEqual([u["reason"] for u in summary["units"]], [None, "throttle_hold"])
        self.assertEqual([u["complete"] for u in summary["units"]], [True, False])


if __name__ == "__main__":
    unittest.main()
