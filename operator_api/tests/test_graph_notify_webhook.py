"""Unit tests for the public Microsoft Graph webhook route ``POST /graph/notify``.

Covers:
- validationToken handshake (200 text/plain; charset=utf-8, exact raw echo, no auth)
- valid mail and Teams change notifications (202 Accepted, status JSON + wake markers)
- wrong/missing clientState dropped (fail closed when secret unset/empty)
- wrong tenantId dropped (supports both AZURE_TENANT_ID and MICROSOFT_GRAPH_TENANT_ID)
- optional PLX_GRAPH_NOTIFY_SUBSCRIPTIONS allowlist
- lifecycle events (reauthorizationRequired, subscriptionRemoved, missed)
- oversized request body (> 256 KB) rejected with 413
- malformed JSON bodies handled gracefully without crashing
- no secret, token, ID, or raw content leaked into status or wake files
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from integrations import graph_notifications as gn
from operator_api.app import create_app
from operator_api.auth import DevAuth, fixture_identity
from operator_api.graph_notify import GRAPH_NOTIFY_BODY_MAX
from operator_api.mock import MockBackend

CLIENT_STATE = "webhook-secret-state-0123456789abcdef"
TENANT = "00000000-0000-4000-8000-0000000000aa"
SUB_TEAMS = "1b0c2f3e-0000-4000-8000-0000000000a2"
SUB_MAIL = "1b0c2f3e-0000-4000-8000-0000000000a1"


def _make_client(state_dir: Path) -> TestClient:
    backend = MockBackend()
    backend.state_dir = state_dir
    auth = DevAuth(fixture_identity("plx", ["staff:demo"]), mode="mock")
    return TestClient(create_app(backend, auth))


class GraphNotifyWebhookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = Path(tempfile.mkdtemp(prefix="graph-notify-webhook-"))
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        self.client = _make_client(self.state)
        self.base_env = {
            gn.ENV_CLIENT_STATE: CLIENT_STATE,
            gn.ENV_GRAPH_TENANT: TENANT,
            gn.ENV_SUBSCRIPTIONS: f"{SUB_MAIL},{SUB_TEAMS}",
        }

    def _note(self, **overrides):
        base = {
            "subscriptionId": SUB_TEAMS,
            "clientState": CLIENT_STATE,
            "tenantId": TENANT,
            "changeType": "created",
            "resource": "chats('19:chat-1@thread.v2')/messages('1700000000001')",
            "subscriptionExpirationDateTime": "2026-11-25T19:00:00.0000000Z",
        }
        base.update(overrides)
        return base

    def test_validation_token_returns_200_plain_text_exact_echo_without_auth(self) -> None:
        token = "Validation: Test+Token/12345 == ok"
        resp = self.client.post("/graph/notify", params={"validationToken": token})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "text/plain; charset=utf-8")
        self.assertEqual(resp.text, token)
        # Validation handshake does not write status or wake files
        self.assertFalse((self.state / gn.STATUS_FILE).exists())

    def test_valid_notifications_return_202_and_write_status_and_wakes(self) -> None:
        payload = {
            "value": [
                self._note(),
                self._note(
                    subscriptionId=SUB_MAIL,
                    resource="Users/11111111-0000-4000-8000-000000000001/Messages/AAMkMsg1=",
                ),
            ]
        }
        with mock.patch.dict(os.environ, self.base_env, clear=True):
            resp = self.client.post("/graph/notify", json=payload)
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.json(), {"accepted": 2, "dropped": 0})

        status = json.loads((self.state / gn.STATUS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(status["schema"], gn.STATUS_SCHEMA)
        self.assertEqual(status["counts"]["accepted"], 2)
        self.assertEqual(status["sources"]["teams"]["subscription_expires"],
                         "2026-11-25T19:00:00.0000000Z")
        self.assertIn("mail", status["sources"])

        # Both ingest Wakes and lightweight graph-notify.wake marker written
        wakes = gn.Wakes(self.state).latest(gn.SOURCES)
        self.assertEqual(set(wakes.keys()), {"mail", "teams"})
        marker = json.loads((self.state / gn.WEBHOOK_WAKE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(marker["sources"], ["mail", "teams"])
        self.assertIsInstance(marker["at"], float)

        # No secrets, IDs, or resource paths persisted anywhere under host/
        written = "".join(
            p.read_text(encoding="utf-8")
            for p in (self.state / "host").rglob("*")
            if p.is_file()
        )
        for secret in (CLIENT_STATE, TENANT, SUB_TEAMS, SUB_MAIL, "19:chat-1", "AAMkMsg1"):
            self.assertNotIn(secret, written)

    def test_wrong_or_missing_client_state_dropped_fail_closed(self) -> None:
        payload = {
            "value": [
                self._note(clientState="wrong-secret-0123456789abcdef"),
                self._note(clientState=None),
            ]
        }
        with mock.patch.dict(os.environ, self.base_env, clear=True):
            resp = self.client.post("/graph/notify", json=payload)
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.json(), {"accepted": 0, "dropped": 2})
        self.assertEqual(gn.Wakes(self.state).latest(gn.SOURCES), {})
        self.assertFalse((self.state / gn.WEBHOOK_WAKE_FILE).exists())

        # Unset/empty secret fails closed and drops even matching notifications
        env_no_secret = {gn.ENV_GRAPH_TENANT: TENANT}
        with mock.patch.dict(os.environ, env_no_secret, clear=True):
            resp_unset = self.client.post("/graph/notify", json={"value": [self._note()]})
        self.assertEqual(resp_unset.status_code, 202)
        self.assertEqual(resp_unset.json(), {"accepted": 0, "dropped": 1})

        env_empty_secret = {gn.ENV_CLIENT_STATE: "   ", gn.ENV_GRAPH_TENANT: TENANT}
        with mock.patch.dict(os.environ, env_empty_secret, clear=True):
            resp_empty = self.client.post("/graph/notify", json={"value": [self._note(clientState="")]})
        self.assertEqual(resp_empty.status_code, 202)
        self.assertEqual(resp_empty.json(), {"accepted": 0, "dropped": 1})

    def test_wrong_tenant_id_and_unknown_subscription_dropped(self) -> None:
        payload = {
            "value": [
                self._note(tenantId="99999999-0000-4000-8000-000000000099"),
                self._note(subscriptionId="33333333-0000-4000-8000-000000000003"),
                self._note(),  # valid
            ]
        }
        with mock.patch.dict(os.environ, self.base_env, clear=True):
            resp = self.client.post("/graph/notify", json=payload)
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.json(), {"accepted": 1, "dropped": 2})
        status = json.loads((self.state / gn.STATUS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(status["counts"]["tenant_mismatch"], 1)
        self.assertEqual(status["counts"]["subscription_unknown"], 1)
        self.assertEqual(status["counts"]["accepted"], 1)

    def test_lifecycle_events_recorded_and_trigger_markers(self) -> None:
        payload = {
            "value": [
                self._note(
                    resource="/chats/getAllMessages",
                    lifecycleEvent="reauthorizationRequired",
                ),
                self._note(
                    resource="/teams/getAllMessages",
                    lifecycleEvent="subscriptionRemoved",
                ),
                self._note(
                    resource="/chats/getAllMessages",
                    lifecycleEvent="missed",
                ),
            ]
        }
        with mock.patch.dict(os.environ, self.base_env, clear=True):
            resp = self.client.post("/graph/notify", json=payload)
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.json(), {"accepted": 3, "dropped": 0})

        status = json.loads((self.state / gn.STATUS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(status["counts"]["lifecycle_reauthorizationRequired"], 1)
        self.assertEqual(status["counts"]["lifecycle_subscriptionRemoved"], 1)
        self.assertEqual(status["counts"]["lifecycle_missed"], 1)
        self.assertEqual(status["sources"]["teams"]["last_lifecycle"], "lifecycle_missed")

        # Renewal wake marker written for reauthorizationRequired / subscriptionRemoved
        renew_marker = json.loads((self.state / gn.RENEW_WAKE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(
            renew_marker["events"],
            ["lifecycle_reauthorizationRequired", "lifecycle_subscriptionRemoved"],
        )
        # Missed lifecycle event wakes Teams ingest
        self.assertIn("teams", gn.Wakes(self.state).latest(gn.SOURCES))

    def test_oversized_body_rejected_with_413(self) -> None:
        oversized = b'{"value": ["' + (b"x" * (GRAPH_NOTIFY_BODY_MAX + 1024)) + b'"]}'
        with mock.patch.dict(os.environ, self.base_env, clear=True):
            resp = self.client.post(
                "/graph/notify",
                content=oversized,
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(resp.json()["error"]["code"], "request_too_large")

    def test_malformed_json_does_not_crash(self) -> None:
        with mock.patch.dict(os.environ, self.base_env, clear=True):
            resp = self.client.post(
                "/graph/notify",
                content=b"{not valid json",
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.json(), {"accepted": 0, "dropped": 1})
        status = json.loads((self.state / gn.STATUS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(status["counts"]["event_malformed"], 1)


if __name__ == "__main__":
    unittest.main()
