"""Approve-to-write through the app's routes (``/api/writes*``): prepare, preview, the
two-step approve and the dry-run execute, as different signed-in people, with the product
wiring (no write transport) and every HTTP sender watched for a non-GET call.

Evidence label: ``isolated-fixture`` (mock backend, fake read-only current values).
"""

from __future__ import annotations

import http.client
import unittest
import urllib.request
from unittest import mock

from fastapi.testclient import TestClient

from integrations import live_source_guard
from operating import write_intents as wi
from operating.session_identity import CHANNEL_CONSOLE, fixture_session
from operator_api.app import create_app
from operator_api.auth import DevAuth
from operator_api.mock import MockBackend

COMPANY = "0f1e2d3c-4b5a-4968-8776-655443322110"
LINE = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
VINCE = ("vince@plx.example", "11111111-1111-4111-8111-111111111111")
ANA = ("ana@plx.example", "33333333-3333-4333-8333-333333333333")
EXCLUDED = ("gone@plx.example", "44444444-4444-4444-8444-444444444444")
STRANGER = ("sam@plx.example", "55555555-5555-4555-8555-555555555555")
SPEC = {"system": "bc", "operation": "purchase_order_line_update", "company_id": COMPANY,
        "record_id": LINE, "payload": {"quantity": "12", "directUnitCost": "2.40"},
        "precondition": {"etag": 'W/"l1"'}, "reason": "Vendor confirmed 12 at 2.40",
        "citations": ["mail://ana/msg-1"]}


class WritesApi(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = MockBackend()
        self.backend.writes_environ = {"PLX_EXCLUDED_PEOPLE_OIDS": EXCLUDED[1]}
        self.backend.write_grants = wi.WriteGrants({VINCE[0]: ["bc:purchaseOrderLines"],
                                                    ANA[0]: ["bc:purchaseOrderLines"],
                                                    EXCLUDED[0]: ["bc:purchaseOrderLines"]})
        self.etag = 'W/"l1"'
        self.backend.write_current = lambda env, company, entity_set, rid: {
            "etag": self.etag, "fields": {"id": rid, "quantity": 10, "directUnitCost": 2.5}}

    def client(self, who) -> TestClient:
        session = fixture_session("plx", ["staff:demo"], channel=CHANNEL_CONSOLE,
                                  subject_email=who[0], subject_oid=who[1])
        client = TestClient(create_app(self.backend, DevAuth(lambda: session, mode="mock"),
                                       secure_cookie=False), base_url="http://testserver")
        self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        return client

    def test_prepare_preview_approve_execute_without_any_non_get_call(self) -> None:
        sent: list[tuple[str, str]] = []

        def watch(method, url, *a, **k):
            sent.append((str(method).upper(), str(url)))
            raise AssertionError("no HTTP in this flow")

        def opened(request, *a, **k):
            method = request.get_method() if hasattr(request, "get_method") else "GET"
            return watch(method, getattr(request, "full_url", request))

        with mock.patch.object(live_source_guard, "urllib_sender", watch), \
                mock.patch.object(urllib.request, "urlopen", opened), \
                mock.patch.object(http.client.HTTPConnection, "request",
                                  lambda self, method, url, *a, **k: watch(method, url)):
            ana, vince = self.client(ANA), self.client(VINCE)
            made = ana.post("/api/writes", json=SPEC)
            self.assertEqual(made.status_code, 200, made.text)
            iid = made.json()["intent"]["intent_id"]
            self.assertEqual(ana.post("/api/writes", json=SPEC).json()["replayed"], True)
            # a reader with no grant neither prepares (the preview would show BC's current
            # values) nor sees another's entry
            sam = self.client(STRANGER)
            self.assertEqual(sam.post("/api/writes", json={**SPEC, "reason": "x"}).status_code,
                             403)
            self.assertEqual(sam.get(f"/api/writes/{iid}/preview").status_code, 404)
            self.assertEqual(sam.get("/api/writes").json()["items"], [])
            shown = vince.get(f"/api/writes/{iid}/preview").json()
            self.assertEqual([(c["field"], c["current"], c["proposed"]) for c in shown["changes"]],
                             [("directUnitCost", 2.5, "2.40"), ("quantity", 10, "12")])
            self.assertEqual(shown["problems"], [])
            self.assertIn("If-Match", shown["request"]["headers"])
            approve = {"confirm": True, "preview_hash": shown["preview_hash"],
                       "acknowledge_warnings": True}
            self.assertEqual(ana.post(f"/api/writes/{iid}/approve", json=approve).status_code,
                             403)       # four eyes: the preparer never approves
            gone = self.client(EXCLUDED)
            refused = gone.post(f"/api/writes/{iid}/approve", json=approve)
            self.assertEqual(refused.status_code, 403, refused.text)
            first_click = vince.post(f"/api/writes/{iid}/approve",
                                     json={**approve, "confirm": False})
            self.assertEqual(first_click.status_code, 422)
            ok = vince.post(f"/api/writes/{iid}/approve", json=approve)
            self.assertEqual(ok.json()["status"], "approved", ok.text)
            run = {"idempotency_key": "dialog-0123456789abcdef"}
            first, again = (vince.post(f"/api/writes/{iid}/execute", json=run)
                            for _ in range(2))
            self.assertEqual((first.json()["status"], again.json()["replayed"]),
                             ("would_write", True))
            self.assertIn(f"Writing is off ({wi.LIVE_FLAG}=1 not set)", first.json()["missing"])
            self.assertEqual(first.json()["request"]["method"], "PATCH")
            # product wiring: with no injected reader the live read needs BC credentials;
            # without them the record is unreadable and nothing is approved
            self.backend.write_current = None
            self.backend.writes_environ = {"PLX_EXCLUDED_PEOPLE_OIDS": EXCLUDED[1]}
            unread = vince.get(f"/api/writes/{iid}/preview").json()
            self.assertIn("could not be read", " ".join(unread["problems"]))
        self.assertEqual([s for s in sent if s[0] != "GET"], [])

    def test_a_moved_etag_refuses_the_approval(self) -> None:
        ana, vince = self.client(ANA), self.client(VINCE)
        iid = ana.post("/api/writes", json=SPEC).json()["intent"]["intent_id"]
        shown = vince.get(f"/api/writes/{iid}/preview").json()
        self.etag = 'W/"l2"'
        refused = vince.post(f"/api/writes/{iid}/approve",
                             json={"confirm": True, "preview_hash": shown["preview_hash"],
                                   "acknowledge_warnings": True})
        self.assertEqual(refused.status_code, 409)
        self.assertIn("etag moved", refused.json()["error"]["message"]
                      if "error" in refused.json() else refused.text)


if __name__ == "__main__":
    unittest.main()
