"""A stalled server never hangs a capture child (2026-11-14 2:28 AM ET: an MC capture child
sat 25 min in ``ssl.read`` and MC went 76 min without a record).

Every capture client goes through ``live_source_guard.urllib_sender`` (Graph, SharePoint,
BC, Mission Control, the brain) or ``integrations.http.get_json`` (the Portal reporting
API). Real sockets on loopback: one server accepts and never answers, one answers its
headers then trickles a byte at a time forever. Each request ends in bounded time.
"""

from __future__ import annotations

import http.client
import socket
import threading
import time
import unittest
from unittest import mock

from integrations import http as source_http
from integrations import live_source_guard as guard


class Stalled:
    """A loopback TCP server: ``mode`` silent (accept, read, never answer) or trickle
    (headers of a huge body, then one byte every 50 ms until the client goes)."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        self.sock.settimeout(0.2)
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            threading.Thread(target=self.answer, args=(conn,), daemon=True).start()

    def answer(self, conn: socket.socket) -> None:
        try:
            conn.recv(65536)
            if self.mode == "trickle":
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                             b"Content-Length: 100000000\r\n\r\n")
            while not self.stop.is_set():
                if self.mode == "trickle":
                    conn.sendall(b" ")
                time.sleep(0.05)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self) -> None:
        self.stop.set()
        self.sock.close()


def bounded(test: unittest.TestCase, call, seconds: float) -> BaseException | None:
    """``call()`` on a thread; fails the test if it is still running after ``seconds``."""
    outcome: list = []

    def run() -> None:
        try:
            outcome.append(call())
        except BaseException as exc:  # noqa: BLE001 - the outcome under test
            outcome.append(exc)
    worker = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(seconds)
    test.assertFalse(worker.is_alive(), f"still reading after {seconds} s")
    test.assertLess(time.monotonic() - started, seconds)
    return outcome[0] if outcome and isinstance(outcome[0], BaseException) else None


class StalledServerTests(unittest.TestCase):
    def server(self, mode: str) -> Stalled:
        server = Stalled(mode)
        self.addCleanup(server.close)
        return server

    def through_guard(self, server: Stalled):
        port = server.port

        class Loopback(http.client.HTTPConnection):   # HTTPSConnection's shape, no TLS
            def __init__(self, host, port_=None, *, timeout=None, context=None, **kw):
                super().__init__(host, port_, timeout=timeout, **kw)

            def connect(self):
                self.sock = socket.create_connection(("127.0.0.1", port), self.timeout)
        patch = mock.patch.object(
            guard._BoundedConnect, "https_open",
            lambda handler, req: handler.do_open(Loopback, req, context=handler._context))
        patch.start()
        self.addCleanup(patch.stop)
        pool = mock.patch.object(guard, "_POOL", guard._ConnectionPool())
        pool.start()
        self.addCleanup(pool.stop)
        return lambda: guard.urllib_sender(
            "GET", "https://graph.microsoft.com/v1.0/users", {"Accept": "application/json"},
            None, 0.5, 1000)

    def test_a_server_that_accepts_and_never_answers_times_out(self) -> None:
        error = bounded(self, self.through_guard(self.server("silent")), 10)
        self.assertIsInstance(error, OSError)

    def test_a_trickling_server_is_cut_at_the_requests_deadline(self) -> None:
        # Old: each socket read waited 0.5 s at most, so a byte every 50 ms held the
        # reader as long as the server liked (1000 bytes: 50 s; a stream: forever).
        self.assertLess(guard.request_seconds(0.5, 1000), 3)
        error = bounded(self, self.through_guard(self.server("trickle")), 10)
        self.assertIsInstance(error, guard.RequestTooSlow)
        self.assertIsInstance(error, TimeoutError)   # a transport failure: retried, bounded
        self.assertEqual(guard.SourceUnavailable.__name__, "SourceUnavailable")

    def test_the_portal_reader_is_cut_too(self) -> None:
        server = self.server("trickle")
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=0.5)
        self.addCleanup(conn.close)
        conn.request("GET", "/")
        response = conn.getresponse()
        error = bounded(self, lambda: source_http.read_bounded(response, 1001, 0.5), 10)
        self.assertIsInstance(error, TimeoutError)

    def test_the_deadline_scales_with_the_body_bound(self) -> None:
        self.assertEqual(guard.request_seconds(30, 0), 120)
        self.assertGreater(guard.request_seconds(30, 150 << 20), 2400)


if __name__ == "__main__":
    unittest.main()
