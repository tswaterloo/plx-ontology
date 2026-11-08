"""Meeting follow-ups through the app's routes (``/api/meetings/follow-up/{id}``,
``/api/meetings/action-items``) as different signed-in people: an attendee sees the page with
its prepared work, a stranger gets the same not-found as a missing meeting, one person's
items are shown only to them and the organiser, and nothing is sent or written.
Evidence label: ``isolated-fixture`` (mock backend, synthetic meetings)."""

from __future__ import annotations

import json
import tempfile
import unittest

from fastapi.testclient import TestClient

from evals import meeting_followup_eval as ev
from operating import meeting_followup as mf
from operating.session_identity import CHANNEL_CONSOLE, fixture_session
from operator_api.app import create_app
from operator_api.auth import DevAuth
from operator_api.mock import MockBackend

DANA = ("dana.price@plx.example", "11111111-1111-4111-8111-111111111111")
PRIYA = ("priya.nair@plx.example", "22222222-2222-4222-8222-222222222222")
SAM = ("sam.okafor@plx.example", "33333333-3333-4333-8333-333333333333")
STRANGER = ("x@plx.example", "55555555-5555-4555-8555-555555555555")


class MeetingsApi(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = mf.FollowUps(tmp.name, ev.Guard())
        for name in ("ops-2026-11-02.granola.md", "hr-2026-11-04.granola.md"):
            raw = (ev.HERE / name).read_text()
            self.assertEqual(store.ingest(raw, ev.Guard(), ev.World(),
                                          source="granola")["status"], "kept")
        self.ids = {r["meeting"]["title"]: r["meeting"]["meeting_id"] for r in store.all()}
        self.backend = MockBackend()
        self.backend.state_dir = tmp.name
        self.backend.meeting_people = ev.Guard()

    def client(self, who) -> TestClient:
        session = fixture_session("plx", ["staff:demo"], channel=CHANNEL_CONSOLE,
                                  subject_email=who[0], subject_oid=who[1])
        client = TestClient(create_app(self.backend, DevAuth(lambda: session, mode="mock"),
                                       secure_cookie=False), base_url="http://testserver")
        self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        return client

    def test_follow_up_page_and_action_items(self) -> None:
        ops = self.ids["Ops weekly"]
        page = self.client(DANA).get(f"/api/meetings/follow-up/{ops}")
        self.assertEqual(page.status_code, 200, page.text)
        body = page.json()["follow_up"]
        self.assertEqual((body["sent"], body["writes"]), (False, 0))
        self.assertTrue(body["decisions"] and body["actions"])
        message = next(a["prepared"] for a in body["actions"]
                       if a["prepared"]["kind"] == "message")
        self.assertEqual(message["card_id"], "meeting:" + message["card"]["id"][8:])
        self.assertIn("send_email", [o["kind"] for o in message["offers"]])
        missing = self.client(STRANGER).get(f"/api/meetings/follow-up/{ops}")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json(), self.client(STRANGER).get(
            "/api/meetings/follow-up/no-such-meeting").json())
        # the HR 1:1 is hidden from its own attendee without the hr marking
        hr = self.ids["1:1 Sam / Hannah"]
        self.assertEqual(self.client(SAM).get(f"/api/meetings/follow-up/{hr}").status_code, 404)
        mine = self.client(PRIYA).get("/api/meetings/action-items").json()["items"]
        self.assertTrue(mine and all(a["owner"]["address"] == PRIYA[0] for a in mine))
        organiser = self.client(DANA).get("/api/meetings/action-items",
                                          params={"person": PRIYA[0]}).json()["items"]
        self.assertEqual([a["id"] for a in organiser], [a["id"] for a in mine])
        peer = self.client(SAM).get("/api/meetings/action-items",
                                    params={"person": PRIYA[0]}).json()["items"]
        self.assertEqual(peer, [])
        self.assertNotIn("Hannah", json.dumps(self.client(SAM).get(
            "/api/meetings/action-items").json()))


if __name__ == "__main__":
    unittest.main()
