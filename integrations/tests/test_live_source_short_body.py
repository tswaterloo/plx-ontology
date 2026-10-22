"""Bodies cut short in transit: whole-body reads and one version-checked retry.

Live observation (main, 2026-10-01): 1 of 5 Graph content runs failed with
"downloaded bytes differ from item size" while direct downloads returned the
full ``Content-Length`` 6/6. ``urllib_sender`` did one ``read(max_bytes + 1)``
and ``http.client`` returns a short body without raising when the connection
ends early. No network: the real ``urllib_sender`` runs against a fake opener
and response. Evidence label: ``injected-transport``.
"""

from __future__ import annotations

from http.client import IncompleteRead
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from integrations import live_source_guard
from integrations.http import SourceUnavailable
from integrations.live_source_guard import (DownloadIncomplete, GuardedTransport,
                                            urllib_sender)
from integrations.tests import test_live_source_adapters as adapters
from integrations.tests.test_live_source_adapters import (ACTOR, DRIVE_BASE, SP_HOST,
                                                          TEMPAUTH, drive_item)


class FakeResponse:
    """An ``http.client``-like response: ``read(n)`` may return fewer bytes."""

    def __init__(self, body: bytes, *, headers: dict[str, str] | None = None,
                 piece: int = 3, raise_at_end: Exception | None = None,
                 endless: bool = False) -> None:
        self._body = body
        self.headers = dict(headers if headers is not None
                            else {"Content-Length": str(len(body))})
        self.status = 200
        self._piece = piece
        self._raise_at_end = raise_at_end
        self._endless = endless
        self.reads: list[int | None] = []

    def read(self, amt=None):
        self.reads.append(amt)
        if self._endless:
            return b"x" * min(self._piece, amt)
        if not self._body:
            if self._raise_at_end is not None:
                raise self._raise_at_end
            return b""
        size = min(self._piece, amt if amt is not None else len(self._body))
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request.get_method(), request.full_url, timeout))
        return self.response


def send_with(response: FakeResponse, *, max_bytes: int = 1000, url: str | None = None):
    opener = FakeOpener(response)
    with patch.object(live_source_guard, "build_opener", return_value=opener):
        result = urllib_sender("GET", url or f"https://{SP_HOST}/x", {}, None, 5.0, max_bytes)
    return result, opener


class SenderWholeBodyTests(unittest.TestCase):
    BODY = b"%PDF-1.7 the whole original document body"

    def test_reads_in_a_loop_until_eof(self):
        response = FakeResponse(self.BODY, piece=4)
        (status, headers, raw), opener = send_with(response)
        self.assertEqual((status, raw), (200, self.BODY))
        self.assertEqual(headers["content-length"], str(len(self.BODY)))
        self.assertGreater(len(response.reads), len(self.BODY) // 4)
        self.assertEqual(opener.requests[0][0], "GET")

    def test_chunked_body_without_length_is_read_to_eof(self):
        response = FakeResponse(self.BODY, headers={"Transfer-Encoding": "chunked"}, piece=5)
        (_status, _headers, raw), _opener = send_with(response)
        self.assertEqual(raw, self.BODY)

    def test_short_body_against_content_length_is_download_incomplete(self):
        response = FakeResponse(self.BODY[:10],
                                headers={"Content-Length": str(len(self.BODY))})
        with self.assertRaises(DownloadIncomplete) as caught:
            send_with(response)
        self.assertEqual(str(caught.exception), "download incomplete")
        self.assertIsInstance(caught.exception, SourceUnavailable)

    def test_incomplete_read_from_http_client_is_download_incomplete(self):
        response = FakeResponse(self.BODY[:10], headers={},
                                raise_at_end=IncompleteRead(b"", 32))
        with self.assertRaisesRegex(DownloadIncomplete, "^download incomplete$"):
            send_with(response)

    def test_invalid_length_header_is_refused(self):
        for value in ("abc", "-1"):
            response = FakeResponse(self.BODY, headers={"Content-Length": value})
            with self.assertRaises(DownloadIncomplete):
                send_with(response)

    def test_read_stays_bounded_and_oversize_is_refused_by_the_transport(self):
        response = FakeResponse(b"", headers={}, piece=7, endless=True)
        (_status, _headers, raw), _opener = send_with(response, max_bytes=50)
        self.assertEqual(len(raw), 51)
        self.assertTrue(all(amt is not None and amt <= 51 for amt in response.reads))
        # A declared length larger than the bound is the bound check's refusal.
        response = FakeResponse(b"", headers={"Content-Length": "10000000"}, piece=7,
                                endless=True)
        opener = FakeOpener(response)
        transport = GuardedTransport(urllib_sender, environ={}, sharepoint_host=SP_HOST)
        with patch.object(live_source_guard, "build_opener", return_value=opener):
            with self.assertRaisesRegex(SourceUnavailable, "exceeds the 50-byte bound"):
                transport.download_preauthenticated(f"https://{SP_HOST}/x", max_bytes=50)

    def test_transport_keeps_the_distinct_message_and_never_leaks_the_url(self):
        url = f"https://{SP_HOST}/sites/PLXCUSTOMERS/_layouts/15/download.aspx?{TEMPAUTH}"
        response = FakeResponse(self.BODY[:5], headers={"Content-Length": str(len(self.BODY))})
        transport = GuardedTransport(urllib_sender, environ={}, sharepoint_host=SP_HOST)
        with patch.object(live_source_guard, "build_opener",
                          return_value=FakeOpener(response)):
            with self.assertRaises(DownloadIncomplete) as caught:
                transport.download_preauthenticated(url, max_bytes=1000)
        rendered = str(caught.exception) + repr(caught.exception)
        self.assertEqual(str(caught.exception), "download incomplete")
        self.assertNotIn(TEMPAUTH, rendered)
        self.assertIsNone(caught.exception.__cause__)

    def test_guarded_json_request_with_cut_body_fails_closed(self):
        body = b'{"value": [{"id": "1"}]}'
        response = FakeResponse(body[:8], headers={"Content-Length": str(len(body))})
        transport = GuardedTransport(urllib_sender, environ={})
        with patch.object(live_source_guard, "build_opener",
                          return_value=FakeOpener(response)):
            with self.assertRaises(DownloadIncomplete):
                transport.request("GET", "https://graph.microsoft.com/v1.0/users", {})


class ContentRetryTests(unittest.TestCase):
    """``SharePointReader.get_content`` with the real sender on the download leg."""

    # Reuse the adapter suite's fixture without re-collecting its tests here.
    setUp = adapters.SharePointTests.setUp
    reader = adapters.SharePointTests.reader

    def _download_route(self, cuts: list[int | None]):
        """Each download answers the next cut (bytes delivered; None = whole)."""
        full = self.content
        served = {"n": 0}

        def route(url):
            index = served["n"]
            served["n"] += 1
            cut = cuts[index] if index < len(cuts) else None
            body = full if cut is None else full[:cut]
            response = FakeResponse(body, headers={"Content-Length": str(len(full))})
            with patch.object(live_source_guard, "build_opener",
                              return_value=FakeOpener(response)):
                return urllib_sender("GET", url, {}, None, 5.0, len(full))
        self.routes[f"{SP_HOST}/sites/PLXCUSTOMERS/_layouts/15/download.aspx"] = route
        return served

    def _downloads(self):
        return [c for c in self.sender.calls if SP_HOST in urlsplit(c[1]).netloc]

    def _url_reads(self):
        return [c[1] for c in self.sender.api_calls()
                if "/items/F1?" in c[1] and "content.downloadUrl" in c[1]]

    def test_incomplete_then_whole_body_succeeds_after_one_retry(self):
        self._download_route([7, None])
        read = self.reader().get_content(ACTOR, "F1")
        self.assertEqual(read["content_status"], "read")
        self.assertEqual(read["content"], self.content)
        self.assertEqual(len(self._downloads()), 2)
        # The retry asked for a fresh download URL (and re-checked the eTag).
        self.assertEqual(len(self._url_reads()), 2)
        for download in self._downloads():
            self.assertNotIn("Authorization", download[2])
            self.assertEqual(download[0], "GET")
        self.assertLessEqual(len(self.transport._secrets), 2)

    def test_incomplete_twice_is_source_unavailable_after_one_bounded_retry(self):
        self._download_route([7, 3, None])
        with self.assertRaises(SourceUnavailable) as caught:
            self.reader().get_content(ACTOR, "F1")
        self.assertEqual(str(caught.exception), "download incomplete")
        self.assertNotIn(TEMPAUTH, str(caught.exception))
        self.assertEqual(len(self._downloads()), 2)
        self.assertLessEqual(len(self.transport._secrets), 2)

    def test_etag_change_before_retry_refuses_and_never_mixes_versions(self):
        served = self._download_route([7, None])
        original = self.routes[f"{DRIVE_BASE}/items/F1"]

        def moving(url):
            if served["n"] >= 1:
                self.file = drive_item("F1", "AO-26005B.pdf", etag="\"{E1},2\"",
                                       content=self.content, parent_id="FOLDER-CUST")
            return original(url)
        self.routes[f"{DRIVE_BASE}/items/F1"] = moving
        with self.assertRaisesRegex(SourceUnavailable, "changed during content read"):
            self.reader().get_content(ACTOR, "F1")
        self.assertEqual(len(self._downloads()), 1)

    def test_other_download_failures_are_not_retried(self):
        self.routes[f"{SP_HOST}/sites/PLXCUSTOMERS/_layouts/15/download.aspx"] = (
            503, {}, b"")
        with self.assertRaisesRegex(SourceUnavailable, "HTTP 503"):
            self.reader().get_content(ACTOR, "F1")
        self.assertEqual(len(self._downloads()), 1)

    def test_whole_body_of_the_wrong_size_stays_fail_closed(self):
        self.routes[f"{SP_HOST}/sites/PLXCUSTOMERS/_layouts/15/download.aspx"] = (
            200, {}, self.content[:-1])
        with self.assertRaisesRegex(SourceUnavailable, "differ from item size"):
            self.reader().get_content(ACTOR, "F1")
        self.assertEqual(len(self._downloads()), 1)


class DownloadBoundTests(unittest.TestCase):
    """The pre-authenticated download bound is the guard's 100 MB limit, no more."""

    URL = f"https://{SP_HOST}/sites/PLXCUSTOMERS/_layouts/15/download.aspx"

    def test_bound_above_the_limit_is_refused_before_any_request(self):
        sent = []
        transport = GuardedTransport(lambda *a: sent.append(a), environ={},
                                     sharepoint_host=SP_HOST)
        with self.assertRaisesRegex(ValueError, "content bound invalid"):
            transport.download_preauthenticated(
                self.URL, max_bytes=live_source_guard.MAX_CONTENT_BYTES + 1)
        self.assertEqual(sent, [])

    def test_body_longer_than_the_bound_is_refused_and_one_at_the_bound_is_kept(self):
        limit = live_source_guard.MAX_CONTENT_BYTES

        class Sized(bytes):  # reports a length without allocating 100 MB
            def __new__(cls, length: int):
                obj = super().__new__(cls)
                obj._length = length
                return obj

            def __len__(self) -> int:
                return self._length

        def sender_of(length: int):
            return lambda *args: (200, {}, Sized(length))

        over = GuardedTransport(sender_of(limit + 1), environ={}, sharepoint_host=SP_HOST)
        with self.assertRaisesRegex(SourceUnavailable, "exceeds the 100000000-byte bound"):
            over.download_preauthenticated(self.URL, max_bytes=limit)
        at = GuardedTransport(sender_of(limit), environ={}, sharepoint_host=SP_HOST)
        self.assertIsInstance(at.download_preauthenticated(self.URL, max_bytes=limit), bytes)


if __name__ == "__main__":
    unittest.main()
