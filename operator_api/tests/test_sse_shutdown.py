"""Fast shutdown of long-lived ``GET /api/events`` SSE streams on SIGTERM / server stop.

During a deploy (``plx-release restart``), uvicorn waits up to ``GRACE_SECONDS`` (180 s)
for active requests to drain so in-flight Ask answers finish cleanly (ARCH-T6). Long-lived
``/api/events`` SSE connections must exit immediately when shutdown is signaled so they do
not hold ``plx-api`` open for the entire 180-second grace period.
"""

from __future__ import annotations

import asyncio
import http.client
import socket
import threading
import time
import unittest

import uvicorn
from fastapi.testclient import TestClient

from operator_api.app import create_app
from operator_api.auth import DevAuth, fixture_identity
from operator_api.events import EventCenter
from operator_api.mock import MockBackend
from operator_api.server import GRACE_SECONDS, _make_server


class _Backend(MockBackend):
    def events_mark(self):
        return "cycle-1"


class EventCenterShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_wakes_waiting_subscriber_and_cancels_watcher_promptly(self):
        center = EventCenter(_Backend(), every=30.0)
        q = center.subscribe("person-a")
        task = center._task
        self.assertIsNotNone(task)

        started = time.monotonic()
        center.shutdown()
        item = await asyncio.wait_for(q.get(), timeout=0.5)
        elapsed = time.monotonic() - started

        self.assertTrue(center.stopping)
        self.assertTrue(center.is_stop(item))
        self.assertLess(elapsed, 0.5)
        await asyncio.sleep(0.02)
        self.assertTrue(task.done())
        center.unsubscribe(q)

    async def test_shutdown_wakes_full_subscriber_queue(self):
        center = EventCenter(_Backend(), every=30.0, queue_size=1)
        q = center.subscribe("person-a")
        center.publish_mine("person-a")
        self.assertTrue(q.full())

        center.shutdown()
        item = await asyncio.wait_for(q.get(), timeout=0.5)
        self.assertTrue(center.is_stop(item))
        center.unsubscribe(q)

    async def test_subscribe_after_shutdown_returns_stop_sentinel_without_watcher(self):
        center = EventCenter(_Backend(), every=30.0)
        center.shutdown()
        q = center.subscribe("person-a")
        self.assertIsNone(center._task)
        self.assertTrue(center.is_stop(q.get_nowait()))


class RouteAndServerShutdownTests(unittest.TestCase):
    def _make_app(self):
        return create_app(
            _Backend(),
            DevAuth(fixture_identity("plx", ["staff:demo"]), mode="mock"),
            secure_cookie=False,
        )

    def test_cross_thread_shutdown_ends_active_events_stream_under_one_second(self):
        app = self._make_app()
        client = TestClient(app, base_url="http://testserver")
        assert client.post("/api/auth/dev-login", json={}).status_code == 200

        got_release = threading.Event()
        shutdown_at: list[float] = []

        def trigger_shutdown() -> None:
            if got_release.wait(timeout=3.0):
                shutdown_at.append(time.monotonic())
                app.state.event_center.shutdown()

        trigger = threading.Thread(target=trigger_shutdown, daemon=True)
        trigger.start()

        lines: list[str] = []
        with client.stream("GET", "/api/events") as response:
            self.assertEqual(response.status_code, 200)
            for line in response.iter_lines():
                lines.append(line)
                if line == "event: release":
                    got_release.set()

        ended = time.monotonic()
        trigger.join(timeout=1.0)
        self.assertTrue(shutdown_at, "shutdown was never triggered")
        self.assertIn("event: release", lines)
        self.assertLess(ended - shutdown_at[0], 1.0)

    def test_uvicorn_server_exits_promptly_with_open_events_sse_client(self):
        app = self._make_app()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
            timeout_graceful_shutdown=GRACE_SECONDS,
        )
        server = _make_server(config)
        thread = threading.Thread(target=server.run, name="test-uvicorn", daemon=True)
        thread.start()

        deadline = time.monotonic() + 5.0
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(server.started, "uvicorn failed to start")

        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/api/auth/dev-login", body="{}", headers={"Content-Type": "application/json"})
            login = conn.getresponse()
            login.read()
            self.assertEqual(login.status, 200)
            cookie = login.getheader("set-cookie").split(";", 1)[0]

            conn.request("GET", "/api/events", headers={"Cookie": cookie})
            sse_resp = conn.getresponse()
            self.assertEqual(sse_resp.status, 200)

            # Read the initial release event so we know the SSE generator is actively waiting.
            first_line = sse_resp.readline().decode("utf-8", errors="replace").strip()
            self.assertEqual(first_line, "event: release")
            _data_line = sse_resp.readline()
            _blank_line = sse_resp.readline()

            # Signal graceful shutdown (same as SIGTERM handler) and verify uvicorn exits fast.
            started = time.monotonic()
            server.should_exit = True
            rest = sse_resp.read()
            thread.join(timeout=3.0)
            elapsed = time.monotonic() - started

            self.assertFalse(thread.is_alive(), "uvicorn hung waiting for /api/events to close")
            self.assertLess(elapsed, 2.0)
            self.assertIsNotNone(rest)
            conn.close()
        finally:
            app.state.event_center.shutdown()
            server.should_exit = True
            thread.join(timeout=3.0)


if __name__ == "__main__":
    unittest.main()
