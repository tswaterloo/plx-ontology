"""ROUTES-A (speed plan 9.3): the ops summary read, the home bundle's per-part timing.
SYNTHETIC alarm status and readers. Evidence label: ``isolated-fixture``."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from operating import ops_alarms
from operating.session_identity import CHANNEL_CONSOLE, fixture_session
from operator_api.app import create_app
from operator_api.auth import DevAuth
from operator_api.mock import MockBackend

OWNER = ("ana@plx.example", "33333333-3333-4333-8333-333333333333")
OTHER = ("sam@plx.example", "55555555-5555-4555-8555-555555555555")


class OpsSummary(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        (self.state / ops_alarms.ALARM_DIR).mkdir()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.status = {"schema": ops_alarms.STATUS_SCHEMA, "checked_at": now,
                       "open": [{"key": "delta_last_cycle", "line": "late", "opened_at": now},
                                {"key": "capture_failing:mail", "line": "x", "opened_at": now}],
                       "recovered": [{"key": "old", "line": "y"}],
                       "checks": [{"key": "c%d" % i} for i in range(50)]}
        (self.state / ops_alarms.ALARM_DIR / ops_alarms.STATUS_FILE).write_text(
            json.dumps(self.status))
        b = self.backend = MockBackend()
        b.state_dir = self.state
        b._is_owner = lambda reader: reader.session.subject_oid == OWNER[1]
        env = mock.patch.dict(os.environ, {"PLX_TEAM_VIEW_READERS": ""})
        env.start()
        self.addCleanup(env.stop)

    def client(self, who) -> TestClient:
        session = fixture_session("plx", ["staff:demo"], channel=CHANNEL_CONSOLE,
                                  subject_email=who[0], subject_oid=who[1])
        client = TestClient(create_app(self.backend, DevAuth(lambda: session, mode="mock"),
                                       secure_cookie=False), base_url="http://testserver")
        self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        return client

    def test_summary_is_the_same_pass_with_the_same_open_incidents(self):
        owner = self.client(OWNER)
        full = owner.get("/api/ops/status").json()
        short = owner.get("/api/ops/status?summary=1").json()
        self.assertEqual(short["open"], full["open"])
        self.assertEqual(short["open_count"], 2)
        self.assertEqual((short["configured"], short["running"], short["checked_at"]),
                         (full["configured"], full["running"], full["checked_at"]))
        for left_out in ("checks", "recovered", "queue"):
            self.assertNotIn(left_out, short)
        self.assertLess(len(json.dumps(short)), len(json.dumps(full)))

    def test_summary_is_for_the_same_readers_only(self):
        other = self.client(OTHER)
        self.assertEqual(other.get("/api/ops/status").status_code, 403)
        self.assertEqual(other.get("/api/ops/status?summary=1").status_code, 403)

    def test_summary_with_no_status_file_says_nothing_is_running(self):
        (self.state / ops_alarms.ALARM_DIR / ops_alarms.STATUS_FILE).unlink()
        short = self.client(OWNER).get("/api/ops/status?summary=1").json()
        self.assertEqual((short["running"], short["open"], short["open_count"]),
                         (False, [], 0))


class HomeBundleTiming(unittest.TestCase):
    def test_every_part_is_timed_in_server_timing_and_the_body_is_unchanged(self):
        session = fixture_session("plx", ["staff:demo"], channel=CHANNEL_CONSOLE,
                                  subject_email=OWNER[0], subject_oid=OWNER[1])
        client = TestClient(create_app(MockBackend(), DevAuth(lambda: session, mode="mock"),
                                       secure_cookie=False), base_url="http://testserver")
        client.post("/api/auth/dev-login", json={})
        got = client.get("/api/home/bundle")
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(set(got.json()), {"parts"})
        timed = {p.split(";")[0] for p in got.headers["server-timing"].split(", ")}
        self.assertEqual(timed, set(got.json()["parts"]))
        self.assertEqual(len(timed), 7)


if __name__ == "__main__":
    unittest.main()
