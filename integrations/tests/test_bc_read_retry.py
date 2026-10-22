"""Bounded retry of BC GETs (injected transport, injected sleep/jitter/clock).

No network and no credentials: every request goes to an in-process sender that
fails on a script and otherwise answers from the ``test_bc_read_entities``
fixture routes. Evidence label: ``injected-transport``. Live behaviour is
checked by ``verify/live-checks/bc.case_entities.json`` on reviewed main.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.client import RemoteDisconnected
import socket
import threading
import unittest
from urllib.error import URLError
from urllib.parse import unquote, urlsplit

from integrations import bc_read, graph_read
from integrations.live_source_guard import (BACKOFF_CAP_SECONDS, MAX_RETRIES, BoundedBackoff,
                                            NotVisible, SourceUnavailable, is_transport_failure)
from integrations.plx_hub_guard import HubTransport
from integrations.tests.test_bc_read_entities import BASE, FINANCE, Fixture
from integrations.tests.test_live_source_adapters import COMPANY, ENV, j

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
BC = "api.businesscentral.dynamics.com"
LOGIN = "login.microsoftonline.com"


class Recorder:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def backoff(jitter: float = 1.0, **kwargs) -> tuple[BoundedBackoff, Recorder]:
    sleeps = Recorder()
    return BoundedBackoff(sleep=sleeps, jitter=lambda: jitter, clock=lambda: NOW,
                          **kwargs), sleeps


def probe_backoff(**kwargs) -> tuple[bc_read.ProbeBackoff, Recorder]:
    sleeps = Recorder()
    return bc_read.ProbeBackoff(sleep=sleeps, timer=lambda: 0.0, **kwargs), sleeps


class Answer:
    """Answers the GETs of one BC path from ``script`` (an exception is raised, a response
    returned) until it runs out, then from ``inner``; ``gets`` counts that path's GETs."""

    def __init__(self, inner, path: str, script) -> None:
        self.inner, self.path, self.script, self.gets = inner, path, list(script), 0

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        if method == "GET" and unquote(urlsplit(url).path) == self.path:
            self.gets += 1
            if self.script:
                outcome = self.script.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome
        return self.inner(method, url, headers, body, timeout, max_bytes)


class Flaky:
    """Answers the scripted outcomes for the first matching requests, then the routes.

    An outcome is an exception instance (raised by the sender, as urllib would)
    or a ``(status, headers, body)`` response."""

    def __init__(self, inner, script, *, host: str = BC, every: int | None = None,
                 skip: int = 0) -> None:
        self.inner = inner
        self.skip = skip
        self.script = list(script)
        self.host = host
        self.every = every
        self.calls: list[tuple[str, str]] = []
        self.threads: set[int] = set()
        self._seen = 0

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append((method, url))
        self.threads.add(threading.get_ident())
        if f"//{self.host}/" in url:
            self._seen += 1
            fail = (self.script and self._seen > self.skip
                    and (self.every is None or self._seen % self.every == 0))
            if fail:
                outcome = self.script.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome
        return self.inner(method, url, headers, body, timeout, max_bytes)

    def to(self, host: str) -> list[tuple[str, str]]:
        return [c for c in self.calls if f"//{host}/" in c[1]]


def reader_with(script, *, retry: BoundedBackoff | None, fixture: Fixture | None = None,
                host: str = BC):
    fixture = fixture or Fixture()
    sender = Flaky(fixture.sender, script, host=host)
    api, _transport, tenant, environment = bc_read.build_bc(ENV, sender=sender, retry=retry)
    reader = bc_read.BusinessCentralReader(api, fixture.audience, tenant_id=tenant,
                                           environment=environment)
    return reader, sender


def read_po(reader):
    return reader.list_entities(FINANCE, COMPANY, "purchaseOrders", page_size=5, max_pages=1,
                                max_items=5)


class RetryableClassTests(unittest.TestCase):
    def test_each_transient_class_is_retried_once_then_succeeds(self):
        cases = {"URLError": URLError("temporary failure in name resolution"),
                 "socket.timeout": socket.timeout("timed out"),
                 "TimeoutError": TimeoutError("read timed out"),
                 "ConnectionResetError": ConnectionResetError(104, "reset"),
                 "RemoteDisconnected": RemoteDisconnected("closed"),
                 "429": j(429, {"error": {"code": "TooManyRequests"}}),
                 "502": j(502, {}), "503": j(503, {}), "504": j(504, {})}
        for label, outcome in cases.items():
            with self.subTest(label):
                retry, sleeps = backoff()
                reader, sender = reader_with([outcome], retry=retry)
                listing = read_po(reader)
                self.assertEqual(len(listing["items"]), 1)
                bc_calls = sender.to(BC)
                self.assertEqual(len(bc_calls), 2)
                self.assertEqual(bc_calls[0], bc_calls[1])  # the identical GET, resent
                self.assertTrue(all(method == "GET" for method, _ in bc_calls))
                self.assertEqual(len(sender.to(LOGIN)), 1)  # token fetched once, not resent
                self.assertEqual(sleeps.sleeps, [0.5])     # base 0.5 s, full jitter factor
                self.assertEqual(retry.retries, 1)

    def test_backoff_is_exponential_jittered_and_capped(self):
        retry, _ = backoff(jitter=1.0)
        self.assertEqual([retry.delay(a) for a in range(5)], [0.5, 1.0, 2.0, 4.0, 4.0])
        retry, _ = backoff(jitter=0.0)
        self.assertEqual([retry.delay(a) for a in range(4)], [0.25, 0.5, 1.0, 2.0])
        self.assertEqual(MAX_RETRIES, 2)
        with self.assertRaises(ValueError):
            BoundedBackoff(max_retries=3)

    def test_transport_failures_keep_the_source_unavailable_label(self):
        reader, _ = reader_with([URLError("x")], retry=backoff(max_retries=0)[0])
        with self.assertRaises(SourceUnavailable) as raised:
            read_po(reader)
        self.assertIs(type(raised.exception), SourceUnavailable)
        self.assertTrue(is_transport_failure(raised.exception))
        self.assertEqual(str(raised.exception), f"GET to {BC} failed: URLError")


class NotRetriedTests(unittest.TestCase):
    def test_client_errors_and_500_are_not_retried(self):
        for status in (400, 401, 403, 404, 500):
            with self.subTest(status):
                retry, sleeps = backoff()
                reader, sender = reader_with([j(status, {"error": {"code": "x"}})],
                                             retry=retry)
                with self.assertRaises(SourceUnavailable) as raised:
                    read_po(reader)
                self.assertEqual(str(raised.exception),
                                 f"business-central GET returned HTTP {status}")
                self.assertEqual(len(sender.to(BC)), 1)
                self.assertEqual((sleeps.sleeps, retry.retries), ([], 0))

    def test_get_by_id_404_is_not_visible_without_retry(self):
        retry, sleeps = backoff()
        fixture = Fixture()
        reader, sender = reader_with([j(404, {})], retry=retry, fixture=fixture)
        with self.assertRaises(NotVisible):
            reader.get_entity(FINANCE, COMPANY, "purchaseOrders",
                              fixture.po["id"])
        self.assertEqual((len(sender.to(BC)), sleeps.sleeps), (1, []))

    def test_token_post_is_sent_once_and_never_retried(self):
        retry, sleeps = backoff()
        reader, sender = reader_with([URLError("down")], retry=retry, host=LOGIN)
        with self.assertRaises(SourceUnavailable) as raised:
            read_po(reader)
        self.assertEqual(str(raised.exception), f"POST to {LOGIN} failed: URLError")
        self.assertEqual(len(sender.to(LOGIN)), 1)
        self.assertEqual(sender.to(BC), [])  # no GET without a token
        self.assertEqual(sleeps.sleeps, [])

    def test_graph_reads_keep_no_retry(self):
        """Only BC opted in; the Graph SourceApi still fails on the first 503."""
        fixture = Fixture()
        sender = Flaky(fixture.sender, [j(503, {})], host="graph.microsoft.com")
        api, _ = graph_read.build_graph(ENV, sender=sender)
        self.assertIsNone(api.retry)
        with self.assertRaises(SourceUnavailable):
            api.get_json("https://graph.microsoft.com/v1.0/users/x@petralabx.com")
        self.assertEqual(len(sender.to("graph.microsoft.com")), 1)


class GiveUpTests(unittest.TestCase):
    def test_gives_up_after_the_bound_with_the_original_error_labelled(self):
        retry, sleeps = backoff()
        reader, sender = reader_with([URLError("a"), URLError("b"), URLError("c"),
                                      URLError("d")], retry=retry)
        with self.assertRaises(SourceUnavailable) as raised:
            read_po(reader)
        self.assertEqual(str(raised.exception),
                         f"GET to {BC} failed: URLError (gave up after 3 attempts)")
        self.assertTrue(is_transport_failure(raised.exception))
        self.assertEqual(len(sender.to(BC)), 1 + MAX_RETRIES)
        self.assertEqual(sleeps.sleeps, [0.5, 1.0])

    def test_status_gives_up_after_the_bound_with_the_last_status(self):
        retry, sleeps = backoff()
        reader, sender = reader_with([j(503, {}), j(502, {}), j(504, {})], retry=retry)
        with self.assertRaises(SourceUnavailable) as raised:
            read_po(reader)
        self.assertEqual(str(raised.exception),
                         "business-central GET returned HTTP 504 (gave up after 3 attempts)")
        self.assertEqual((len(sender.to(BC)), len(sleeps.sleeps)), (3, 2))

    def test_mixed_failure_then_success_within_the_bound(self):
        retry, sleeps = backoff()
        reader, sender = reader_with([TimeoutError(), j(429, {})], retry=retry)
        self.assertEqual(len(read_po(reader)["items"]), 1)
        self.assertEqual((len(sender.to(BC)), sleeps.sleeps), (3, [0.5, 1.0]))


class RetryAfterTests(unittest.TestCase):
    def test_retry_after_is_honoured_and_capped(self):
        date_soon = format_datetime(NOW + timedelta(seconds=3), usegmt=True)
        date_late = format_datetime(NOW + timedelta(hours=1), usegmt=True)
        date_past = format_datetime(NOW - timedelta(seconds=30), usegmt=True)
        cases = [("3", 3.0), ("100", BACKOFF_CAP_SECONDS), (date_soon, 3.0),
                 (date_late, BACKOFF_CAP_SECONDS), (date_past, 0.25), ("garbage", 0.25),
                 ("0", 0.25)]
        for value, expected in cases:
            with self.subTest(value):
                retry, sleeps = backoff(jitter=0.0)
                reader, _ = reader_with([(429, {"Retry-After": value}, b"{}")], retry=retry)
                read_po(reader)
                self.assertEqual(sleeps.sleeps, [expected])

    def test_retry_after_header_name_is_case_insensitive(self):
        retry, sleeps = backoff(jitter=0.0)
        reader, _ = reader_with([(503, {"retry-after": "2"}, b"{}")], retry=retry)
        read_po(reader)
        self.assertEqual(sleeps.sleeps, [2.0])


class SleepBoundTests(unittest.TestCase):
    def test_one_get_never_sleeps_more_than_the_retries_times_the_cap(self):
        retry, sleeps = backoff()
        reader, _ = reader_with([(429, {"Retry-After": "86400"}, b"{}")] * 3, retry=retry)
        with self.assertRaises(SourceUnavailable):
            read_po(reader)
        self.assertEqual(sleeps.sleeps, [BACKOFF_CAP_SECONDS, BACKOFF_CAP_SECONDS])
        self.assertLessEqual(sum(sleeps.sleeps), MAX_RETRIES * BACKOFF_CAP_SECONDS)

    def test_shared_budget_stops_retrying_across_a_burst(self):
        retry, sleeps = backoff(budget_seconds=5.0)
        fixture = Fixture()
        sender = Flaky(fixture.sender, [URLError("x")] * 40)
        api, _t, tenant, environment = bc_read.build_bc(ENV, sender=sender, retry=retry)
        reader = bc_read.BusinessCentralReader(api, fixture.audience, tenant_id=tenant,
                                               environment=environment)
        failures = 0
        for _ in range(10):
            try:
                read_po(reader)
            except SourceUnavailable:
                failures += 1
        self.assertEqual(failures, 10)
        self.assertLessEqual(sum(sleeps.sleeps), 5.0)
        self.assertEqual(retry.slept_seconds, sum(sleeps.sleeps))
        # 0.5 + 1.0 per GET fits three GETs into 5 s (4.5 s), then a partial step,
        # then no further retries: the burst stops stalling.
        self.assertLess(len(sender.to(BC)), 10 * (1 + MAX_RETRIES))
        self.assertEqual(sleeps.sleeps[:6], [0.5, 1.0, 0.5, 1.0, 0.5, 1.0])

    def test_build_bc_default_has_a_budget(self):
        api, *_ = bc_read.build_bc(ENV, sender=Fixture().sender)
        self.assertIsInstance(api.retry, BoundedBackoff)
        self.assertEqual(api.retry.budget_seconds, bc_read.RETRY_BUDGET_SECONDS)
        self.assertEqual(api.retry.max_retries, MAX_RETRIES)


class ProbeTests(unittest.TestCase):
    VENDORS = f"{BASE}/companies({COMPANY})/vendors"

    def vendors(self, report: dict) -> dict | None:
        return next((e for e in report["case_entity_errors"]
                     if (e["company_index"], e["entity_set"]) == (0, "vendors")), None)

    def test_case_entities_probe_survives_intermittent_failures_sequentially(self):
        baseline = bc_read.probe(ENV, sender=Fixture().sender, case_entities=True,
                                 retry=probe_backoff()[0])
        self.assertEqual((baseline["get_retries"], baseline["retry_sleep_seconds"]), (0, 0.0))
        retry, sleeps = probe_backoff()
        fixture = Fixture()
        script = [URLError("x"), j(503, {}), TimeoutError(), j(429, {}), URLError("y")]
        sender = Flaky(fixture.sender, script, every=7)
        report = bc_read.probe(ENV, sender=sender, case_entities=True, retry=retry)
        # Identical to the clean run (the fixture's own 404 gaps included): no
        # entity or line error comes from the injected transient failures.
        self.assertEqual(report["case_entities"], baseline["case_entities"])
        text = repr(report["case_entities"])
        for transient in ("URLError", "TimeoutError", "HTTP 503", "HTTP 429"):
            self.assertNotIn(transient, text)
        self.assertEqual(report["get_retries"], 5)
        self.assertEqual(sleeps.sleeps, [bc_read.PROBE_UNIT_PAUSES[0]] * 5)
        self.assertEqual(report["retry_sleep_seconds"], sum(sleeps.sleeps))
        self.assertEqual(report["requests_sent"], baseline["requests_sent"] + 5)
        self.assertEqual(sender.threads, {threading.get_ident()})  # one thread, sequential

    def test_one_unit_under_repeated_transport_failure_sends_at_most_three_gets(self):
        """One retry layer: ``ProbeRetry`` read a unit again
        around ``SourceApi``'s own retry, up to 9 GETs per read; now a probe GET is sent
        at most 1 + MAX_RETRIES times, after the probe's 5 s and 15 s pauses."""
        retry, sleeps = probe_backoff()
        sender = Answer(Fixture().sender, self.VENDORS, [URLError("down")] * 100)
        report = bc_read.probe(ENV, sender=sender, case_entities=True, retry=retry)
        self.assertEqual(sender.gets, 1 + MAX_RETRIES)
        self.assertEqual(sleeps.sleeps, list(bc_read.PROBE_UNIT_PAUSES))
        self.assertEqual((self.vendors(report)["http_status"], self.vendors(report)["detail"]),
                         (None, "retried.urlerror"))
        self.assertEqual((report["unit_retries"], report["unit_retry_sleep_seconds"],
                          report["get_retries"], report["retry_sleep_seconds"]),
                         (2, 20.0, 2, 20.0))
        self.assertIsNone(report["stopped_reason"])  # the retry bound, not the wall

    def test_a_retry_the_wall_cap_alone_refuses_fails_typed_wall_cap(self):
        """bc-probe-followups review: a read whose GET ``SourceApi`` would have sent again
        but for the wall kept the GET's own error and ``stopped_reason`` null; it is a
        ``wall_cap`` stop like a read the wall keeps from starting."""
        # Every read can start (one attempt fits) but no pause and second attempt does.
        wall = bc_read.PROBE_ATTEMPT_SECONDS + bc_read.PROBE_UNIT_PAUSES[0] - 1
        for label, outcome in (("transport failure", URLError("down")),
                               ("HTTP 503", j(503, {}))):
            with self.subTest(label):
                retry, sleeps = probe_backoff(wall_seconds=wall)
                sender = Answer(Fixture().sender, self.VENDORS, [outcome] * 10)
                report = bc_read.probe(ENV, sender=sender, case_entities=True, retry=retry)
                self.assertEqual((sender.gets, sleeps.sleeps), (1, []))
                self.assertEqual((self.vendors(report)["error_class"],
                                  self.vendors(report)["http_status"],
                                  self.vendors(report)["detail"]),
                                 ("SourceUnavailable", None, "wall_cap"))
                self.assertEqual(report["stopped_reason"], "wall_cap")
                others = [e for e in report["case_entity_errors"]
                          if e["detail"] == "wall_cap" and e != self.vendors(report)]
                self.assertEqual(others, [])  # only the read that was cut

    def test_a_read_is_typed_wall_cap_by_its_last_request_only(self):
        """bc-expected-refusal-company review: the cut flag lived per read, so a read whose
        first GET the wall cut (its failure absorbed) and whose second GET failed on its
        own was typed ``wall_cap``; it carries the second GET's own error."""
        wall = bc_read.PROBE_ATTEMPT_SECONDS + bc_read.PROBE_UNIT_PAUSES[0] - 1
        retry, sleeps = probe_backoff(wall_seconds=wall)
        reader, sender = reader_with([URLError("down"), j(500, {})], retry=retry)

        def two_requests():
            with self.assertRaises(SourceUnavailable):
                read_po(reader)  # cut by the wall: no retry
            return read_po(reader)  # HTTP 500: never retried, the wall plays no part

        with self.assertRaises(SourceUnavailable) as raised:
            retry.read(two_requests)
        self.assertIn("HTTP 500", str(raised.exception))
        self.assertEqual((retry.wall_capped, sleeps.sleeps, len(sender.to(BC))), (0, [], 2))
        # The cut request itself still types its read.
        reader, _sender = reader_with([URLError("down")], retry=retry)
        with self.assertRaisesRegex(SourceUnavailable, "wall cap"):
            retry.read(lambda: read_po(reader))
        self.assertEqual(retry.wall_capped, 1)

    def test_a_429_honours_retry_after_up_to_the_longest_pause_and_stops_beyond_it(self):
        longest = bc_read.PROBE_UNIT_PAUSES[-1]
        for wait, gets, slept in ((str(int(longest)), 2, [longest]), ("3", 2, [3.0]),
                                  (str(int(longest) + 1), 1, []), ("3600", 1, [])):
            with self.subTest(retry_after=wait):
                retry, sleeps = probe_backoff()
                throttled = (429, {"Retry-After": wait},
                             b'{"error":{"code":"TooManyRequests"}}')
                sender = Answer(Fixture().sender, self.VENDORS, [throttled])
                report = bc_read.probe(ENV, sender=sender, case_entities=True, retry=retry)
                self.assertEqual((sender.gets, sleeps.sleeps), (gets, slept))
                failure = self.vendors(report)
                if slept:
                    self.assertIsNone(failure)
                else:
                    self.assertEqual((failure["http_status"], failure["error_code"]),
                                     (429, "too_many_requests"))
                self.assertEqual((report["unit_retries"], report["unit_retry_sleep_seconds"]),
                                 (len(slept), sum(slept)))

    def test_probe_entity_error_label_names_the_original_failure(self):
        retry, sleeps = probe_backoff()
        # The company listing, the first entity read and its citation succeed;
        # every later BC GET fails at the transport.
        sender = Flaky(Fixture().sender, [URLError("x")] * 1000, skip=3)
        report = bc_read.probe(ENV, sender=sender, case_entities=True, retry=retry)
        labels = {e["error"] for c in report["case_entities"] for e in c["entities"]
                  if "error" in e}
        self.assertTrue(labels)
        self.assertTrue(all(label.startswith(f"SourceUnavailable: GET to {BC} failed: "
                                             "URLError") for label in labels), labels)
        self.assertIn(f"SourceUnavailable: GET to {BC} failed: URLError "
                      "(gave up after 3 attempts)", labels)
        self.assertLessEqual(sum(sleeps.sleeps), bc_read.PROBE_RETRY_BUDGET_SECONDS)
        self.assertEqual(report["retry_sleep_seconds"], round(sum(sleeps.sleeps), 3))


class SharedHelperTests(unittest.TestCase):
    def test_hub_transport_uses_the_same_backoff(self):
        sleeps = Recorder()
        hub = HubTransport(lambda *a: (200, {}, b"{}"), sleep=sleeps, jitter=lambda: 1.0,
                           clock=lambda: NOW)
        self.assertIsInstance(hub._backoff_policy, BoundedBackoff)
        hub.pause(0, {"Retry-After": "3"})
        hub.pause(1)
        self.assertEqual(sleeps.sleeps, [3.0, 1.0])
        self.assertEqual(hub.slept_seconds, 4.0)

    def test_budget_and_type_validation(self):
        for bad in (-1, True, "5"):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    BoundedBackoff(budget_seconds=bad)
        fixture = Fixture()
        with self.assertRaises(ValueError):
            bc_read.SourceApi("business-central", None, fixture.transport, retry=object())
        # A probe without its wall cap is refused before any request.
        with self.assertRaises(ValueError):
            bc_read.probe(ENV, sender=fixture.sender, retry=BoundedBackoff())
        self.assertEqual(fixture.sender.calls, [])


if __name__ == "__main__":
    unittest.main()
