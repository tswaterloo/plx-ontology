"""My work (``/api/my-work*``) through the app's routes: only the owner sees and approves
their prepared items, approve goes through the card and write-intent flows, the kill switch
stops approval, and nothing is sent or written.

Evidence label: ``isolated-fixture`` (mock backend, a synthetic queue file).
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from operating import autopilot as ap
from operating.session_identity import CHANNEL_CONSOLE, fixture_session
from operator_api.app import create_app
from operator_api.auth import DevAuth
from operator_api.mock import MockBackend

OWNER = ("ana@plx.example", "33333333-3333-4333-8333-333333333333")
STRANGER = ("sam@plx.example", "55555555-5555-4555-8555-555555555555")


def item(item_id: str, *, markings=(), write=False) -> dict:
    prepared = {"summary": "PO 88123 received",
                "draft": {"channel": "email", "to": [{"address": "buyer@synth.example"}],
                          "subject": "RE: PO 88123", "body": "Hello,\n\nReceived."}}
    if write:
        prepared["write"] = {"system": "bc", "operation": "sales_order_create",
                             "payload": {"customerNumber": "C100",
                                         "externalDocumentNumber": "88123"},
                             "reason": "Customer PO received", "precondition": {}}
    return {"item_id": item_id, "event_id": "e-" + item_id, "autopilot": "customer_po",
            "owner": OWNER[1], "title": "Customer PO received: PO 88123",
            "body": {"happened": {"object_id": "Email:x:1"}, "prepared": prepared,
                     "done_when": "owner_replied", "label": "Customer PO received"},
            "audiences": ["mail:" + OWNER[1]], "markings": list(markings),
            "citations": ["mail://synth/1"], "due_at": "2026-10-19T17:00:00Z",
            "created_at": "2026-10-19T15:00:00Z"}


class MyWorkApi(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / ap.QUEUE_FILE
        q = ap.Queue(path)
        with q.conn:
            q.add_item(item("ap_one", write=True))
            q.add_item(item("ap_fin", markings=["finance"]))
        q.close()
        self.backend = MockBackend()
        self.backend.writes_environ = {}
        self.backend.autopilot_queue = path
        self.backend.autopilot_grant = (lambda aud: True, frozenset(),
                                        lambda hs: {h: None for h in hs})

    def client(self, who) -> TestClient:
        session = fixture_session("plx", ["staff:demo"],
                                  channel=CHANNEL_CONSOLE, subject_email=who[0],
                                  subject_oid=who[1])
        client = TestClient(create_app(self.backend, DevAuth(lambda: session, mode="mock"),
                                       secure_cookie=False), base_url="http://testserver")
        self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        return client

    def test_owner_sees_and_approves_through_the_existing_flows_only(self) -> None:
        owner, stranger = self.client(OWNER), self.client(STRANGER)
        mine = owner.get("/api/my-work").json()
        # the finance-marked item stays hidden from a reader without the finance marking
        self.assertEqual([i["item_id"] for i in mine["items"]], ["ap_one"])
        self.assertEqual((mine["sent"], mine["writes"], mine["status"]), (0, 0, "read"))
        self.assertEqual(mine["items"][0]["card_id"], "autopilot:ap_one")
        self.assertTrue(mine["items"][0]["write_ready"])
        self.assertEqual(stranger.get("/api/my-work").json()["items"], [])
        self.assertEqual(stranger.post("/api/my-work/ap_one/write").status_code, 404)
        # the entry is refused until its lines are filled in: nothing is written
        refused = owner.post("/api/my-work/ap_one/write")
        self.assertEqual(refused.status_code, 422, refused.text)
        self.assertIn("lines", refused.text)
        # the card resolves for the owner only
        self.assertEqual(self.backend.my_work_card_of(_reader(owner_session()),
                                                      "autopilot:ap_one")["id"],
                         "autopilot:ap_one")
        self.assertIsNone(self.backend.my_work_card_of(_reader(owner_session(STRANGER)),
                                                       "autopilot:ap_one"))

    def test_kill_switch_stops_approval(self) -> None:
        self.backend.writes_environ = {ap.KILL_ENV: "1"}
        owner = self.client(OWNER)
        self.assertEqual(owner.get("/api/my-work").json()["status"], "off")
        self.assertEqual(owner.post("/api/my-work/ap_one/write").status_code, 409)


def owner_session(who=OWNER):
    return fixture_session("plx", ["staff:demo"],
                           channel=CHANNEL_CONSOLE, subject_email=who[0], subject_oid=who[1])


def _reader(session):
    from operator_api.auth import Reader
    return Reader(session=session, mode="mock", display_name="synth")


if __name__ == "__main__":
    unittest.main()
