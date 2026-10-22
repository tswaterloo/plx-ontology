"""Keep-alive in the real guarded sender.

``urllib_sender`` runs whole (``build_opener``, the redirect and error processors, the
pool) against in-process fake servers: the HTTPS handler's ``https_open`` is pointed at a
real ``http.client.HTTPConnection`` subclass whose socket serves scripted HTTP/1.1 bytes,
so framing, ``Connection: close`` and end-of-body detection are http.client's own. No
network, no TLS. Evidence label: ``injected-transport``.
"""

from __future__ import annotations

import http.client
import io
import os
import threading
import unittest
from unittest import mock

from integrations import live_source_guard as guard
from integrations.live_source_guard import (DownloadIncomplete, GuardedTransport,
                                            ReadOnlyGuardError, urllib_sender)
from integrations.tests.test_live_source_adapters import DRIVE_BASE, SP_HOST

GRAPH = "https://graph.microsoft.com"
BC = "https://api.businesscentral.dynamics.com"
PROXY_VARS = ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy",
              "ALL_PROXY", "no_proxy", "NO_PROXY")


def reply(status: int = 200, body: bytes = b"{}", headers: dict[str, str] | None = None,
          *, length: int | None = None, reason: str = "OK") -> bytes:
    head = {"Content-Length": str(len(body) if length is None else length)}
    head.update(headers or {})
    lines = "".join(f"{k}: {v}\r\n" for k, v in head.items())
    return f"HTTP/1.1 {status} {reason}\r\n{lines}\r\n".encode() + body


class Server:
    """Scripted answers per host, in order; records which socket served each request."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.script: dict[str, list] = {}
        self.sockets: list[FakeSocket] = []
        self.served: list[tuple[int, str, str]] = []  # (socket id, host, request line)
        self.overlaps = 0
        self.gate: threading.Barrier | None = None

    def add(self, host: str, *answers) -> None:
        self.script.setdefault(host, []).extend(answers)

    def next_answer(self, host: str):
        with self.lock:
            return self.script[host].pop(0)


class Reader(io.BytesIO):
    def __init__(self, sock: "FakeSocket", data: bytes, *, timeout_after: bool = False):
        super().__init__(data)
        self.sock = sock
        self.timeout_after = timeout_after
        self.waited = False

    def _wait(self) -> None:
        if not self.waited and self.sock.server.gate is not None:
            self.waited = True
            self.sock.server.gate.wait(5)

    def _done(self) -> None:
        if self.tell() >= len(self.getbuffer()):
            self.sock.busy = False

    def readline(self, *args):
        self._wait()
        line = super().readline(*args)
        if not line and self.timeout_after:
            raise TimeoutError("timed out")
        self._done()
        return line

    def read(self, *args):
        data = super().read(*args)
        if not data and self.timeout_after:
            raise TimeoutError("timed out")
        self._done()
        return data

    def readinto(self, b):
        n = super().readinto(b)
        if not n and self.timeout_after:
            raise TimeoutError("timed out")
        self._done()
        return n


class FakeSocket:
    def __init__(self, server: Server, host: str) -> None:
        self.server = server
        self.host = host
        self.busy = False
        self.closed = False
        self.timeouts: list[float] = []
        self.pending: bytes | None = None
        self.timeout_after = False
        self.dead = False
        with server.lock:
            server.sockets.append(self)
            self.id = len(server.sockets)

    def settimeout(self, value):
        self.timeouts.append(value)

    def sendall(self, data: bytes):
        if self.dead:
            raise BrokenPipeError(32, "Broken pipe")
        if not data.split(b"\r\n", 1)[0].endswith(b" HTTP/1.1"):
            return  # a request body, sent after its head
        if self.busy:
            with self.server.lock:
                self.server.overlaps += 1
        self.busy = True
        line = data.split(b"\r\n", 1)[0].decode()
        with self.server.lock:
            self.server.served.append((self.id, self.host, line))
        answer = self.server.next_answer(self.host)
        if answer == "server-closed-idle":
            # The server had closed this kept-alive socket: nothing comes back.
            self.dead = True
            self.pending = b""
        elif isinstance(answer, tuple) and answer[0] == "timeout":
            self.pending, self.timeout_after = answer[1], True
        else:
            self.pending, self.timeout_after = answer, False

    def makefile(self, mode):
        data, self.pending = self.pending or b"", None
        return Reader(self, data, timeout_after=self.timeout_after)

    def close(self):
        self.closed = True


class FakeHTTPS(http.client.HTTPConnection):
    """``HTTPSConnection``'s constructor shape over a scripted socket (no TLS)."""

    server: Server

    def __init__(self, host, port=None, *, timeout=None, context=None, **kwargs):
        super().__init__(host, port, timeout=timeout, **kwargs)
        self.context = context
        self.connect_timeout = timeout
        self.tunnel = None

    def set_tunnel(self, host, port=None, headers=None):
        self.tunnel = (host, dict(headers or {}))

    def connect(self):
        target = self.tunnel[0] if self.tunnel else self.host
        self.sock = FakeSocket(self.server, target)


class KeepAliveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = Server()
        FakeHTTPS.server = self.server
        self.pool = guard._ConnectionPool()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in PROXY_VARS:
            os.environ.pop(name, None)
        pool = mock.patch.object(guard, "_POOL", self.pool)
        pool.start()
        self.addCleanup(pool.stop)
        self.addCleanup(self.pool.clear)
        https = mock.patch.object(
            guard._BoundedConnect, "https_open",
            lambda handler, req: handler.do_open(FakeHTTPS, req, context=handler._context))
        https.start()
        self.addCleanup(https.stop)

    def get(self, url: str, *, max_bytes: int = 1000, timeout: float = 5.0):
        return urllib_sender("GET", url, {"Accept": "application/json"}, None, timeout,
                             max_bytes)

    def sockets_used(self) -> list[int]:
        return [sock_id for sock_id, _host, _line in self.server.served]

    def test_second_get_to_the_same_host_reuses_the_connection(self):
        self.server.add("graph.microsoft.com", reply(body=b'{"a":1}'), reply(body=b'{"b":2}'))
        self.assertEqual(self.get(f"{GRAPH}/v1.0/a")[2], b'{"a":1}')
        self.assertEqual(self.get(f"{GRAPH}/v1.0/b")[2], b'{"b":2}')
        self.assertEqual(self.sockets_used(), [1, 1])
        self.assertEqual(len(self.server.sockets), 1)
        self.assertEqual(self.server.served[1][2], "GET /v1.0/b HTTP/1.1")
        # The connect is bounded apart from the read; each use waits its own timeout.
        sock = self.server.sockets[0]
        self.assertEqual(sock.timeouts, [5.0, 5.0])
        self.get_with_timeout_check()

    def get_with_timeout_check(self):
        self.server.add("graph.microsoft.com", reply())
        self.get(f"{GRAPH}/v1.0/c", timeout=40.0)
        self.assertEqual(self.server.sockets[0].timeouts[-1], 40.0)

    def test_connect_timeout_is_bounded_and_tls_context_is_the_shared_default(self):
        self.server.add("graph.microsoft.com", reply(), reply())
        seen = []
        original = FakeHTTPS.__init__

        def record(conn, *args, **kwargs):
            original(conn, *args, **kwargs)
            seen.append(conn)
        with mock.patch.object(FakeHTTPS, "__init__", record):
            self.get(f"{GRAPH}/v1.0/a", timeout=45.0)
            self.get(f"{GRAPH}/v1.0/a", timeout=45.0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].connect_timeout, guard.CONNECT_TIMEOUT_SECONDS)
        self.assertEqual(self.server.sockets[0].timeouts, [45.0, 45.0])
        context = guard._shared_tls_context()
        self.assertIs(seen[0].context, context)
        if context is not None:  # Python 3.12+: urllib builds it; settings untouched
            import ssl
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)

    def test_no_reuse_across_hosts(self):
        self.server.add("graph.microsoft.com", reply(), reply())
        self.server.add(SP_HOST, reply(body=b"PDF"))
        self.get(f"{GRAPH}/v1.0/a")
        self.get(f"https://{SP_HOST}/x")
        self.get(f"{GRAPH}/v1.0/b")
        self.assertEqual([(i, h) for i, h, _ in self.server.served],
                         [(1, "graph.microsoft.com"), (2, SP_HOST), (1, "graph.microsoft.com")])

    def test_refused_method_or_url_sends_nothing_even_with_a_pooled_connection(self):
        self.server.add("graph.microsoft.com", reply())
        self.get(f"{GRAPH}/v1.0/a")
        self.assertEqual(self.pool.idle_count(), 1)
        refused = [
            ("POST", f"{GRAPH}/v1.0/users/x/sendMail", b"{}"),
            ("DELETE", f"{GRAPH}/v1.0/a", None),
            ("PATCH", f"{GRAPH}/v1.0/a", b"{}"),
            ("GET", f"{GRAPH}/v1.0/a", b"body"),
            ("GET", "http://graph.microsoft.com/v1.0/a", None),
            ("GET", "https://graph.microsoft.com:8443/v1.0/a", None),
            ("GET", "https://user:pw@graph.microsoft.com/v1.0/a", None),
        ]
        for method, url, body in refused:
            with self.subTest(method=method, url=url), \
                    self.assertRaises(ReadOnlyGuardError):
                urllib_sender(method, url, {}, body, 5.0, 1000)
        self.assertEqual(len(self.server.served), 1)
        self.assertEqual(self.pool.idle_count(), 1)

    def test_redirect_refused_and_content_redirect_handed_back(self):
        location = f"https://{SP_HOST}/_layouts/15/download.aspx?tempauth=t"
        self.server.add("graph.microsoft.com",
                        reply(), reply(302, b"", {"Location": "https://evil.example/x"},
                                       reason="Found"),
                        reply(302, b"", {"Location": location, "Set-Cookie": "s=1"},
                              reason="Found"),
                        reply())
        self.get(f"{GRAPH}/v1.0/a")
        with self.assertRaises(ReadOnlyGuardError):
            self.get(f"{GRAPH}/v1.0/a")
        status, headers, raw = self.get(f"{GRAPH}{DRIVE_BASE}/items/F1/content")
        self.assertEqual((status, headers, raw), (302, {"location": location}, b""))
        # Neither redirect's connection was pooled: the last GET opens a new one.
        self.get(f"{GRAPH}/v1.0/a")
        self.assertEqual(self.sockets_used(), [1, 1, 2, 3])
        self.assertEqual(len(self.server.served), 4)

    def test_no_reuse_after_error_timeout_or_partial_read(self):
        cases = {
            "error status": (reply(503, b'{"error":{"code":"x"}}',
                                   {"Retry-After": "2", "X-Other": "y"},
                                   reason="Unavailable"), None),
            "partial body": (reply(body=b"abc", length=10), DownloadIncomplete),
            "read timeout": (("timeout", reply(body=b"abc", length=10)), TimeoutError),
            # No status within the timeout on a reused connection: a timeout, never
            # mistaken for a server-closed idle socket and never sent again.
            "status timeout": (("timeout", b""), TimeoutError),
            "oversized": (reply(body=b"x" * 50), None),
        }
        for name, (answer, error) in cases.items():
            with self.subTest(name):
                self.server.add("graph.microsoft.com", reply(), answer, reply())
                self.get(f"{GRAPH}/v1.0/a")
                first = self.sockets_used()[-1]
                if error is None:
                    status, headers, raw = self.get(f"{GRAPH}/v1.0/b", max_bytes=10)
                    if status == 503:
                        self.assertEqual(headers, {"retry-after": "2"})
                    else:
                        self.assertEqual(len(raw), 11)  # transport refuses the bound
                else:
                    with self.assertRaises(error):
                        self.get(f"{GRAPH}/v1.0/b")
                self.assertEqual(self.sockets_used()[-1], first)  # used the pooled one
                self.assertEqual(self.server.script["graph.microsoft.com"], [reply()])
                self.assertTrue(self.server.sockets[first - 1].closed)
                self.assertEqual(self.pool.idle_count(), 0)
                self.get(f"{GRAPH}/v1.0/c")
                self.assertNotEqual(self.sockets_used()[-1], first)
                self.pool.clear()

    def test_connection_close_response_drops_the_connection(self):
        self.server.add("graph.microsoft.com", reply(headers={"Connection": "close"}),
                        reply())
        self.get(f"{GRAPH}/v1.0/a")
        self.assertEqual(self.pool.idle_count(), 0)
        self.get(f"{GRAPH}/v1.0/b")
        self.assertEqual(self.sockets_used(), [1, 2])

    def test_stale_pooled_connection_resends_a_get_once_on_a_new_one(self):
        self.server.add("graph.microsoft.com", reply(), "server-closed-idle",
                        reply(body=b'{"ok":1}'))
        self.get(f"{GRAPH}/v1.0/a")
        self.assertEqual(self.get(f"{GRAPH}/v1.0/b")[2], b'{"ok":1}')
        self.assertEqual(self.sockets_used(), [1, 1, 2])
        self.assertTrue(self.server.sockets[0].closed)

    def test_non_get_never_takes_or_leaves_a_pooled_connection(self):
        token = ("https://login.microsoftonline.com/00000000-0000-0000-0000-000000000000"
                 "/oauth2/v2.0/token")
        form = (b"grant_type=client_credentials&scope=https%3A%2F%2Fgraph.microsoft.com"
                b"%2F.default&client_id=c&client_secret=s")
        self.server.add("login.microsoftonline.com", reply(), reply(), reply())
        self.get(f"https://login.microsoftonline.com/x")
        urllib_sender("POST", token, {}, form, 5.0, 1000)
        self.assertEqual(self.sockets_used(), [1, 2])
        self.assertEqual(self.server.served[1][2].split(" ")[0], "POST")
        self.get(f"https://login.microsoftonline.com/x")
        self.assertEqual(self.sockets_used(), [1, 2, 1])

    def test_concurrent_senders_never_share_a_connection_mid_request(self):
        threads = 6
        self.server.add("graph.microsoft.com", *[reply(body=b'{"n":1}')] * (threads * 3))
        self.server.gate = threading.Barrier(threads)
        errors = []

        def worker():
            try:
                self.get(f"{GRAPH}/v1.0/a")
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
        pool = [threading.Thread(target=worker) for _ in range(threads)]
        for thread in pool:
            thread.start()
        for thread in pool:
            thread.join(10)
        self.assertEqual(errors, [])
        # All six were in flight at once (the barrier): six distinct connections.
        self.assertEqual(sorted(self.sockets_used()), list(range(1, threads + 1)))
        self.assertEqual(self.server.overlaps, 0)
        self.assertEqual(self.pool.idle_count(), guard.KEEPALIVE_PER_HOST)  # bounded
        self.server.gate = None
        rounds = [threading.Thread(target=worker) for _ in range(threads * 2)]
        for thread in rounds:
            thread.start()
        for thread in rounds:
            thread.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(self.server.overlaps, 0)
        self.assertEqual(len(self.server.served), threads * 3)
        self.assertLessEqual(self.pool.idle_count(), guard.KEEPALIVE_PER_HOST)

    def test_proxy_tunnel_connections_pool_by_target_host(self):
        os.environ["https_proxy"] = "http://u:p@proxy.local:3128"
        self.server.add("graph.microsoft.com", reply(), reply())
        self.server.add(SP_HOST, reply())
        self.get(f"{GRAPH}/v1.0/a")
        self.get(f"https://{SP_HOST}/x")
        self.get(f"{GRAPH}/v1.0/b")
        self.assertEqual([(i, h) for i, h, _ in self.server.served],
                         [(1, "graph.microsoft.com"), (2, SP_HOST), (1, "graph.microsoft.com")])

    def test_idle_connection_past_its_age_is_closed_not_reused(self):
        now = [100.0]
        self.pool._clock = lambda: now[0]
        self.server.add("graph.microsoft.com", reply(), reply())
        self.get(f"{GRAPH}/v1.0/a")
        now[0] += guard.KEEPALIVE_IDLE_SECONDS + 1
        self.get(f"{GRAPH}/v1.0/b")
        self.assertEqual(self.sockets_used(), [1, 2])
        self.assertTrue(self.server.sockets[0].closed)

    def test_guarded_transport_path_reuses_through_the_real_sender(self):
        self.server.add(SP_HOST, reply(body=b"one"), reply(body=b"two"))
        transport = GuardedTransport(urllib_sender, environ={}, sharepoint_host=SP_HOST)
        self.assertFalse(transport.sender_injected)
        url = f"https://{SP_HOST}/_layouts/15/download.aspx?UniqueId=x&tempauth=t"
        for expected in (b"one", b"two"):
            self.assertEqual(transport.download_preauthenticated(url, max_bytes=100),
                             expected)
        self.assertEqual(self.sockets_used(), [1, 1])


if __name__ == "__main__":
    unittest.main()
