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

    def _is_owner(self, reader):    # the owner-code test (feedback.owner_open): ME, when on
        return self.owner and f"employee:{ME}" in (reader.session.principals or ())

    def _brief_run(self, reader, job, **_kw):
        view = SimpleNamespace(
            gap_inbox=lambda **kw: {"status": self.inbox_status,
                                    "gaps": getattr(self, "gaps", [GAP])},
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

    def test_a_kept_background_draft_replaces_the_holding_body_and_says_its_state(self):
        from operating import reply_drafts as rd
        self.assertEqual(self.client().get("/api/replies").json()["lines"][0]["draft_state"],
                         "queued")
        self.backend.state_dir = self.dir
        rd.Kept(rd.path_of(self.dir)).mark(ME, "Email:synth-1", "ready", kind="reply",
                                           body="Order 1 ships [day].", ask="Which day?",
                                           why="Check before sending: x")
        client = self.client()
        (line,) = client.get("/api/replies").json()["lines"]
        self.assertEqual((line["draft_state"], line["drafted_by"], line["draft"]["body"],
                          line["draft_note"], line["draft_warning"]),
                         ("ready", "model", "Order 1 ships [day].", "Which day?",
                          "Check before sending: x"))
        self.assertFalse(line["draft"]["sent"])
        # the send dialog's card is rebuilt with the same kept draft, never the holding body
        shown = client.post("/api/actions/preview", json={"card_id": line["card_id"],
                                                          "kind": "send_email"})
        self.assertEqual(shown.status_code, 200, shown.text)
        self.assertEqual(shown.json()["payload"]["body"], "Order 1 ships [day].")
        # an unfilled blank never goes out: the send refuses it until the person fills it
        self.assertIn("Fill in [day] before you send.", shown.json()["problems"])
        filled = client.post("/api/actions/preview", json={
            "card_id": line["card_id"], "kind": "send_email",
            "payload": {"body": "Order 1 ships after the check."}}).json()
        self.assertFalse([p for p in filled["problems"] if p.startswith("Fill in")],
                         filled["problems"])

    def test_a_message_the_triage_read_as_owed_nothing_leaves_the_list(self):
        """Home's "Needs your reply" follows the triage model's reading kept for the
        reader's own mailbox: automated, fyi or notice leaves the list; a reply reading, a
        message with no reading, an old yes/no verdict and another person's reading stay."""
        from operating import autopilot as ap

        def gap(n):
            g = json.loads(json.dumps(GAP))
            g.update(id=f"pe-{n}", object_id=f"Email:synth-{n}")
            g["event"]["messages"][0]["id"] = f"Email:synth-{n}"
            return g
        self.backend.gaps = [gap(n) for n in range(1, 7)]
        self.backend.state_dir = self.dir
        (self.dir / "autopilot").mkdir()
        q = ap.Queue(self.dir / "autopilot" / ap.QUEUE_FILE)
        with q.conn:
            q.note_chat_asks([
                {"message_id": "Email:synth-2", "reader": ME, "asks": False,
                 "reason": "automated:", "at": "2026-11-21T00:00:00Z"},
                {"message_id": "Email:synth-3", "reader": ME, "asks": False,
                 "reason": "fyi:book the pickup", "at": "2026-11-21T00:00:00Z"},
                {"message_id": "Email:synth-4", "reader": ME, "asks": True,
                 "reason": "reply:", "at": "2026-11-21T00:00:00Z"},
                {"message_id": "Email:synth-5", "reader": ME, "asks": False,
                 "reason": "note", "at": "2026-11-21T00:00:00Z"},
                {"message_id": "Email:synth-6", "reader": B, "asks": False,
                 "reason": "automated:", "at": "2026-11-21T00:00:00Z"}])
        q.close()
        body = self.client().get("/api/replies").json()
        self.assertEqual(sorted(x["object_id"] for x in body["lines"]),
                         ["Email:synth-1", "Email:synth-4", "Email:synth-5", "Email:synth-6"])
        self.assertEqual(body["total"], 4)

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
        # the manager sees the team's people and their own, never the owner (learned when
        # the owner signed in); the team of two shows no total
        body = self.client(C).get("/api/workload").json()
        self.assertEqual(sorted(p["person"] for p in body["people"]), sorted([B, C]))
        sales = {t["team"]: t for t in body["teams"]}["SALES"]
        self.assertEqual((sales["awaiting_reply"], sales["suppressed"]), (None, True))


if __name__ == "__main__":
    unittest.main()
