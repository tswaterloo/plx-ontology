"""The page event stream (SPEED-SPECS 10.9 #7): server-sent events, no route thread held
while idle, one watcher for every subscriber, cycle/mine/door/release marks only."""

import asyncio
import json
from types import SimpleNamespace
import unittest

from fastapi.testclient import TestClient

from operator_api.app import create_app
from operator_api.auth import DevAuth, fixture_identity
from operator_api.events import EventCenter
from operator_api.mock import MockBackend
from operator_api.page_store import code_version


class Backend(MockBackend):
    def __init__(self):
        super().__init__()
        self.mark = "cycle-a"

    def events_mark(self):
        return self.mark


class CenterTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_watcher_broadcasts_a_moved_cycle_and_mine_only_to_its_person(self):
        backend = Backend()
        center = EventCenter(backend, every=0.01)
        a = center.subscribe("person-a")
        b = center.subscribe("person-b")
        await asyncio.sleep(0.03)   # cycle-a is the baseline, not news
        backend.mark = "cycle-b"
        got = await asyncio.wait_for(a.get(), 0.3)
        self.assertEqual(got, ("cycle", {"cycle": "cycle-b"}))
        self.assertEqual(await asyncio.wait_for(b.get(), 0.3), got)
        center.publish_mine("person-a")
        self.assertEqual(await asyncio.wait_for(a.get(), 0.3), ("mine", {"mine": 1}))
        self.assertTrue(b.empty())
        center.unsubscribe(a)
        center.unsubscribe(b)
        await asyncio.sleep(0.03)

    async def test_a_slow_subscriber_drops_its_oldest_event_never_the_server(self):
        center = EventCenter(Backend(), every=10, queue_size=1)
        q = center.subscribe("person")
        for n in range(4):
            center._put(("mine", {"mine": n}), "person")
        self.assertEqual(await asyncio.wait_for(q.get(), 0.2), ("mine", {"mine": 3}))
        center.unsubscribe(q)


class RouteTests(unittest.TestCase):
    def client(self):
        backend = Backend()
        backend.doors = SimpleNamespace(stamp=lambda: 3180, access_mark=lambda who: "mark-1", decide=lambda who: True)
        app = create_app(backend, DevAuth(fixture_identity("plx", ["staff:demo"]), mode="mock"),
                         secure_cookie=False)
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/api/auth/dev-login", json={}).status_code == 200
        return c, backend

    def test_the_route_is_async_and_sends_release_and_door_at_once(self):
        c, _ = self.client()
        route = next(r for r in c.app.routes if getattr(r, "path", None) == "/api/events")
        self.assertTrue(asyncio.iscoroutinefunction(route.endpoint))
        with c.stream("GET", "/api/events") as r:
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.headers["content-type"].startswith("text/event-stream"))
            lines = []
            for line in r.iter_lines():
                lines.append(line)
                if len([x for x in lines if x]) >= 4:
                    break
        text = "\n".join(lines)
        self.assertIn("event: release", text)
        self.assertIn(json.dumps({"release": code_version()}), text)
        self.assertIn("event: door", text)

    def test_the_stream_needs_a_reader(self):
        app = create_app(MockBackend(), DevAuth(fixture_identity("plx", ["staff:demo"]), mode="mock"),
                         secure_cookie=False)
        c = TestClient(app, base_url="http://testserver")
        with c.stream("GET", "/api/events") as r:
            self.assertEqual(r.status_code, 401)

    def test_moving_access_mark_emits_door_event(self):
        c, backend = self.client()
        marks = iter(["mark-1", None, "mark-2"])
        backend.doors.access_mark = lambda who: next(marks, "mark-2")
        from unittest import mock
        with mock.patch("operator_api.app.EVENTS_POLL_SECONDS", 0.02):
            with c.stream("GET", "/api/events") as r:
                self.assertEqual(r.status_code, 200)
                lines = []
                door_count = 0
                for line in r.iter_lines():
                    lines.append(line)
                    if line == "event: door":
                        door_count += 1
                    elif door_count >= 2 and line.startswith("data:"):
                        break
        text = "\n".join(lines)
        self.assertEqual(text.count("event: door"), 2)


if __name__ == "__main__":
    unittest.main()
