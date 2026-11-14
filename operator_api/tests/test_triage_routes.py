"""The replies, workload and workload-grants routes through the app and the real backend
(``WorkspaceBackend``; its reader's view a stub returning a synthetic trimmed alert list),
with the real people exclusion (``integrations.people_exclusion.PeopleExclusion``).

SYNTHETIC fixtures only; evidence label: isolated-fixture.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from operating import proactive_engine as pe
from operator_api import backend as be
from operator_api.app import create_app
from operator_api.auth import DevAuth, fixture_identity

ME = "00000000-0000-4000-8000-0000000000a1"
B = "00000000-0000-4000-8000-0000000000b1"
C = "00000000-0000-4000-8000-0000000000b2"
EXCLUDED = "00000000-0000-4000-8000-0000000000e1"
GAP = {"id": "pe-1", "kind": "reply_owed", "object_id": "Email:synth-1", "summary": "Waiting",
       "citations": ["l1:synth"], "state": "open",
       "event": {"days": 2, "asked_by": "buyer@synth.example", "chases": 1, "rank": [0, 0, 1, 2],
                 "messages": [{"id": "Email:synth-1", "channel": "mail", "sent": "2026-11-03",
                               "subject": "Order 1"}]}}


class Backend(be.WorkspaceBackend):
    owner = False
    inbox_status = "read"

    def _brief_run(self, reader, job):
        view = SimpleNamespace(
            gap_inbox=lambda **kw: {"status": self.inbox_status, "gaps": [GAP]},
            access=SimpleNamespace(reader_policy=lambda: (
                None, SimpleNamespace(unrestricted=self.owner))),
            _request=lambda: mock.MagicMock())
        return job(view)


class Routes(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        store = self.dir / "store.sqlite"
        conn = sqlite3.connect(pe.inbox_path(store))
        conn.execute("CREATE TABLE gaps (kind TEXT, follow_up TEXT, state TEXT)")
        for owner in (ME, ME, B, C, EXCLUDED):
            conn.execute("INSERT INTO gaps VALUES ('reply_owed', ?, 'open')",
                         (json.dumps({"owner": owner}),))
        conn.commit()
        conn.close()
        self.backend = Backend(lambda s: None, data_label="t", store_path=store,
                               data_dir=self.dir)
        patcher = mock.patch.dict(os.environ, {"PLX_EXCLUDED_PEOPLE_OIDS": EXCLUDED})
        patcher.start()
        self.addCleanup(patcher.stop)

    def client(self, oid: str = ME) -> TestClient:
        # the employee principal an Entra login carries (a fixture session refuses to
        # declare one, so it is set on the session the dev door hands out)
        session = dataclasses.replace(fixture_identity("plx", ["staff:demo"])(),
                                      principals=frozenset({"staff:demo", f"employee:{oid}"}))
        client = TestClient(create_app(self.backend, DevAuth(lambda: session),
                                       secure_cookie=False), base_url="http://testserver")
        self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        return client

    def test_replies_list_the_readers_own_line_with_a_draft_card_never_sent(self):
        found = self.client().get("/api/replies")
        self.assertEqual(found.status_code, 200, found.text)
        body = found.json()
        (line,) = body["lines"]
        self.assertEqual((body["sent"], body["writes"], line["draft"]["sent"]), (0, 0, False))
        self.assertEqual(line["card_id"], "reply:pe-1")
        self.assertEqual(line["draft"]["to"], [{"address": "buyer@synth.example"}])

    def test_an_alert_list_not_read_yet_is_said_so_never_as_no_access(self):
        # UX walk 11-13: Team's approvals said "couldn't find that, or you don't have
        # access" while the alert run had not run on the server
        self.backend.inbox_status = "not_scheduled"
        client = self.client()
        for route in ("/api/replies", "/api/approvals"):
            got = client.get(route)
            self.assertEqual(got.status_code, 404, route)
            self.assertEqual(got.json()["error"]["code"], "alerts_not_read", route)

    def test_workload_answers_with_the_real_exclusion_and_suppresses_small_teams(self):
        client = self.client()
        found = client.get("/api/workload")
        self.assertEqual(found.status_code, 200, found.text)
        body = found.json()
        self.assertEqual([p["person"] for p in body["people"]], [ME])
        self.assertNotIn(EXCLUDED, found.text)
        self.assertNotIn(B, found.text)
        unassigned = {t["team"]: t for t in body["teams"]}["not_assigned"]
        self.assertEqual(unassigned["awaiting_reply"], 4)   # three cleared people
        with mock.patch.dict(os.environ, {"PLX_EXCLUDED_PEOPLE_OIDS": ""}), \
                mock.patch("operating.briefs.env_people", return_value=None):
            closed = client.get("/api/workload").json()
        self.assertEqual(closed["people"], [])
        self.assertFalse([t for t in closed["teams"] if t["awaiting_reply"]])

    def test_only_the_owner_assigns_grants_and_a_bad_shape_is_refused(self):
        grants = {"teams": {ME: "SALES", B: "SALES"}, "managers": {C: ["SALES"]}}
        client = self.client()
        self.assertEqual(client.post("/api/workload/grants", json=grants).status_code, 403)
        self.backend.owner = True
        made = client.post("/api/workload/grants", json=grants)
        self.assertEqual(made.status_code, 200, made.text)
        self.assertEqual(client.post("/api/workload/grants",
                                     json={"teams": {"x": "sales"}}).status_code, 422)
        # the manager sees the team's people and their own; the team of two shows no total
        body = self.client(C).get("/api/workload").json()
        self.assertEqual(sorted(p["person"] for p in body["people"]), sorted([ME, B, C]))
        sales = {t["team"]: t for t in body["teams"]}["SALES"]
        self.assertEqual((sales["awaiting_reply"], sales["suppressed"]), (None, True))


if __name__ == "__main__":
    unittest.main()
