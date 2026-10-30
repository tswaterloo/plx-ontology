"""Guard diagnostics: Retry-After on the real sender's error path and the fixed
vocabulary for ``SourceUnavailable`` (bc-retry minor).

Keyless: ``urllib_sender`` runs against a stub opener (``build_opener``
patched), every other request goes to the in-process BC fixture. Evidence
label: ``injected-transport``; nothing here is live.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.message import Message
from http.client import RemoteDisconnected
import io
import json
import socket
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

from integrations import bc_read, live_source_guard as guard
from integrations.live_source_guard import (BoundedBackoff, DownloadIncomplete,
                                            GuardedTransport, NotVisible, ReadOnlyGuardError,
                                            SourceNotFound, SourceUnavailable,
                                            transport_failure, unavailable_detail,
                                            urllib_sender)
from integrations.tests.test_bc_read_entities import FINANCE, Fixture
from integrations.tests.test_live_source_adapters import COMPANY, ENV

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
URL = ("https://api.businesscentral.dynamics.com/v2.0/00000000-0000-0000-0000-000000000000/"
       "Production/api/v2.0/companies")


def http_error(status: int, headers: dict[str, str], body: bytes = b"{}") -> HTTPError:
    message = Message()
    for key, value in headers.items():
        message[key] = value
    return HTTPError(URL, status, "error", message, io.BytesIO(body))


class StubOpener:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.opened = 0

    def open(self, request, timeout=None):
        self.opened += 1
        raise self.error


class UrllibSenderErrorHeaderTests(unittest.TestCase):
    def send(self, error: BaseException):
        opener = StubOpener(error)
        with mock.patch.object(guard, "build_opener", lambda *handlers: opener):
            return urllib_sender("GET", URL, {"Accept": "application/json"}, None, 5.0, 1000)

    def test_http_error_hands_back_retry_after_lower_cased_and_nothing_else(self):
        status, headers, raw = self.send(http_error(
            429, {"Retry-After": "3", "Set-Cookie": "session=abc", "WWW-Authenticate": "x",
                  "Content-Type": "application/json"}))
        self.assertEqual((status, raw), (429, b"{}"))
        self.assertEqual(headers, {"retry-after": "3"})

    def test_http_date_retry_after_passes_and_absent_header_gives_empty(self):
        _status, headers, _raw = self.send(http_error(
            503, {"retry-after": "Wed, 01 Oct 2026 12:00:02 GMT"}))
        self.assertEqual(headers, {"retry-after": "Wed, 01 Oct 2026 12:00:02 GMT"})
        self.assertEqual(self.send(http_error(503, {}))[1], {})
        self.assertEqual(self.send(HTTPError(URL, 503, "x", None, io.BytesIO(b"")))[1], {})

    def test_overlong_retry_after_is_dropped(self):
        self.assertEqual(self.send(http_error(429, {"Retry-After": "9" * 100}))[1], {})

    def test_redirect_error_still_refused(self):
        with self.assertRaises(ReadOnlyGuardError):
            self.send(http_error(302, {"Location": "https://evil.example/"}))

    def test_retry_after_is_honoured_through_the_real_sender_path(self):
        """A 429 from urllib (HTTPError) waits the server's Retry-After (3 s),
        not the 0.5 s base backoff, then the identical GET succeeds."""
        fixture = Fixture()
        opener = StubOpener(http_error(429, {"Retry-After": "3"}))
        bc_gets: list[str] = []

        def sender(method, url, headers, body, timeout, max_bytes):
            if method == "GET" and "//api.businesscentral.dynamics.com/" in url:
                bc_gets.append(url)
                if len(bc_gets) == 1:
                    with mock.patch.object(guard, "build_opener", lambda *handlers: opener):
                        return urllib_sender(method, url, headers, body, timeout, max_bytes)
            return fixture.sender(method, url, headers, body, timeout, max_bytes)

        sleeps: list[float] = []
        retry = BoundedBackoff(sleep=sleeps.append, jitter=lambda: 1.0, clock=lambda: NOW)
        api, _transport, tenant, environment = bc_read.build_bc(ENV, sender=sender, retry=retry)
        reader = bc_read.BusinessCentralReader(api, fixture.audience, tenant_id=tenant,
                                               environment=environment)
        listing = reader.list_entities(FINANCE, COMPANY, "purchaseOrders", page_size=5,
                                       max_pages=1, max_items=5)
        self.assertEqual(len(listing["items"]), 1)
        self.assertEqual(opener.opened, 1)
        self.assertEqual(len(bc_gets), 2)
        self.assertEqual(bc_gets[0], bc_gets[1])
        self.assertEqual(sleeps, [3.0])


class UnavailableDetailTests(unittest.TestCase):
    """The diagnosis token comes only from the fixed vocabulary."""

    def detail(self, exc):
        value = unavailable_detail(exc)
        token = value[len(guard.RETRIED_PREFIX):] if value.startswith(
            guard.RETRIED_PREFIX) else value
        self.assertIn(token, guard.DETAIL_TOKENS)  # closed vocabulary only
        return value

    def test_every_reachable_reason_fits_the_live_verifier_enum_bound(self):
        """A reason longer than live_verify's 64-character enum
        bound is hidden as ``<str len=N>`` in the live report. Every
        SourceUnavailable class in the codebase x every token x retried/not, as
        ``operating.ingestion._error_reason`` composes it, must fit and stay an
        enum there."""
        import importlib
        import pkgutil

        import integrations
        from verify.live_verify import ENUM_STRING
        from operating import ingestion

        for info in pkgutil.iter_modules(integrations.__path__):
            if not info.name.startswith("test"):
                importlib.import_module(f"integrations.{info.name}")
        classes, stack = set(), [SourceUnavailable]
        while stack:
            cls = stack.pop()
            classes.add(cls)
            stack.extend(cls.__subclasses__())
        classes = {c for c in classes if not c.__module__.split(".")[-1].startswith("test")}
        self.assertGreaterEqual({c.__name__ for c in classes},
                                {"SourceUnavailable", "DownloadIncomplete", "SourceNotFound",
                                 "UpstreamError"})
        self.assertTrue(all(len(t) <= guard.MAX_DETAIL_TOKEN for t in guard.DETAIL_TOKENS))
        checked = 0
        for cls in classes:
            for token in guard.DETAIL_TOKENS:
                for detail in (token, guard.RETRIED_PREFIX + token):
                    exc = cls.__new__(cls)
                    # _error_reason imports unavailable_detail from the guard at call time.
                    with mock.patch.object(guard, "unavailable_detail",
                                           lambda _e, d=detail: d):
                        reason = ingestion._error_reason(exc)
                    self.assertLessEqual(len(reason), 64, reason)
                    self.assertRegex(reason, ENUM_STRING)
                    self.assertTrue(reason.endswith(":" + detail), reason)
                    checked += 1
        self.assertGreater(checked, 100)

    def test_long_subclass_name_falls_back_to_the_base_class(self):
        from operating import ingestion

        class AVeryLongSourceUnavailableSubclassNameForTest(SourceUnavailable):
            pass

        reason = ingestion._error_reason(AVeryLongSourceUnavailableSubclassNameForTest(
            "GET to graph.microsoft.com failed: RemoteDisconnected (gave up after 3 attempts)"))
        self.assertEqual(reason, "source_error:SourceUnavailable:retried.remote_closed")

    def test_guard_messages_map_to_fixed_tokens(self):
        cases = {
            "BC GET returned HTTP 429": "http_429",
            "graph GET returned HTTP 503": "http_503",
            "BC GET returned HTTP 503 (gave up after 3 attempts)":
                "retried.http_503",
            "BC GET returned HTTP 418": "http_other",
            "download returned HTTP 403": "http_403",
            "graph nextLink escaped the collection": "nextlink_escaped",
            "BC pagination loop": "pagination_loop",
            "graph listing lacks value list": "listing_invalid",
            "BC GET returned non-JSON": "non_json",
            "response exceeds the 4000000-byte bound": "response_too_large",
            "token request refused: HTTP 401": "token_refused",
            "token response invalid": "token_invalid",
            "missing environment variables: BC_TENANT_ID": "missing_env",
            "BC read returned another identity": "identity_mismatch",
            "drive item changed during content read": "item_changed",
            "something nobody anticipated": "unclassified",
        }
        for message, token in cases.items():
            with self.subTest(message):
                self.assertEqual(self.detail(SourceUnavailable(message)), token)

    def test_transport_failures_name_the_failure_kind_not_the_host(self):
        for name, token in (("URLError", "urlerror"), ("TimeoutError", "timeout"),
                            ("timeout", "timeout"), ("ConnectionResetError", "conn_reset"),
                            ("RemoteDisconnected", "remote_closed"),
                            ("WeirdError", "transport_failure")):
            with self.subTest(name):
                exc = transport_failure(f"GET to graph.microsoft.com failed: {name}")
                self.assertEqual(self.detail(exc), token)
        gave_up = transport_failure("GET to api.businesscentral.dynamics.com failed: URLError "
                                    "(gave up after 3 attempts)")
        self.assertEqual(self.detail(gave_up), "retried.urlerror")

    def test_download_incomplete_and_not_found(self):
        self.assertEqual(self.detail(DownloadIncomplete()), "download_incomplete")
        self.assertEqual(self.detail(DownloadIncomplete("download length header invalid")),
                         "download_length_invalid")
        self.assertEqual(self.detail(SourceNotFound("BC GET returned HTTP 404")), "http_404")

    def test_real_transport_errors_through_guarded_transport(self):
        for error, token in ((URLError("name resolution for secret-host.example"), "urlerror"),
                             (socket.timeout("timed out"), "timeout"),
                             (RemoteDisconnected("closed"), "remote_closed")):
            def sender(*_args, error=error):
                raise error
            with self.subTest(token), self.assertRaises(SourceUnavailable) as raised:
                GuardedTransport(sender, environ={}).request("GET", URL, {})
            self.assertEqual(self.detail(raised.exception), token)

    def test_source_text_never_reaches_the_token(self):
        hostile = SourceUnavailable("GET returned HTTP 503 for vince@petrasoap.com "
                                    "https://graph.microsoft.com/v1.0/users/x AO-26005B")
        detail = self.detail(hostile)
        self.assertEqual(detail, "http_503")
        for leaked in ("vince", "petrasoap", "https", "AO-", "users"):
            self.assertNotIn(leaked, detail)

    def test_non_source_errors_have_no_detail(self):
        self.assertIsNone(unavailable_detail(ValueError("x")))
        self.assertIsNone(unavailable_detail(NotVisible()))
        self.assertIsNone(unavailable_detail(ReadOnlyGuardError("x")))


class UpstreamErrorCodeTests(unittest.TestCase):
    """Error-code class of an HTTP failure: fixed vocabulary."""

    def test_codes_map_to_snake_case_tokens_of_the_fixed_vocabulary(self):
        from verify.data_profile import http_error_code_ok

        def code(value) -> str | None:
            return guard.upstream_error_code(json.dumps(value).encode())
        self.assertEqual(code({"error": {"code": "itemNotFound"}}), "item_not_found")
        self.assertEqual(code({"error": {"code": "InvalidAuthenticationToken"}}),
                         "invalid_authentication_token")
        self.assertEqual(code({"error": {"code": "BadRequest_NotFound"}}),
                         "bad_request_not_found")
        self.assertEqual(code({"error": "invalid_client",
                               "error_description": "AADSTS7000215 secret"}),
                         "invalid_client")
        self.assertEqual(code({"error": {"code": "acme-secret-value"}}), "other")
        self.assertIsNone(code({"error": {"message": "no code"}}))
        self.assertIsNone(guard.upstream_error_code(b"<html>Forbidden</html>"))
        self.assertTrue(all(http_error_code_ok(t) for t in guard.ERROR_CODE_TOKENS))

    def test_token_refusal_carries_status_and_code_class_only(self):
        planted = "PLANTED-BODY-do-not-leak"

        def sender(method, url, headers, body, timeout, max_bytes):
            return 401, {}, json.dumps({"error": "invalid_client",
                                        "error_description": planted}).encode()
        api, _transport = bc_read.build_bc(ENV, sender=sender)[:2]
        with self.assertRaises(SourceUnavailable) as caught:
            api.get_json(URL)
        diagnosis = guard.failure_diagnostics(caught.exception)
        self.assertEqual(diagnosis, {"error_class": "SourceUnavailable", "http_status": 401,
                                     "error_code": "invalid_client", "detail": "token_refused"})
        self.assertNotIn(planted, json.dumps(diagnosis) + str(caught.exception))


if __name__ == "__main__":
    unittest.main()
