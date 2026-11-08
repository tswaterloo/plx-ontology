"""The owner-only per-person workload route and the R&D board route through the app and the
real backend (its reader's view a stub returning a synthetic trimmed alert list), with the real
people exclusion. SYNTHETIC fixtures only; evidence label: isolated-fixture."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
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


def rd_line(i: int, project: str, customer: str = "Customer:1", state: str = "waiting_on_us"):
    return {"id": f"rd-{i}", "kind": "rd_project_line", "state": "open",
            "citations": [f"l1:rd{i}"],
            "event": {"project": project, "kind": "sample_request", "state": state, "days": 6,
                      "last_on": "2026-11-02", "messages_n": 2, "customer": customer,
                      "items": [], "owner": B, "contact": f"buyer{i}@synth-customer.example",
                      "messages": [{"id": f"Email:{i}", "subject": "Sample"}]}}


class Backend(be.WorkspaceBackend):
    owner = False
    gaps: list = []
    refuse: set = set()

    def _brief_run(self, reader, job):
        view = SimpleNamespace(
            gap_inbox=lambda **kw: {"status": "read", "gaps": list(self.gaps)},
            access=SimpleNamespace(
                reader_policy=lambda: (None, SimpleNamespace(unrestricted=self.owner)),
                decide=lambda handles: {h: ({"reason": "no"} if h in self.refuse else None)
                                        for h in handles}),
            _request=lambda: mock.MagicMock())
        return job(view)


class Owner(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        store = self.dir / "store.sqlite"
        conn = sqlite3.connect(pe.inbox_path(store))
        conn.execute("CREATE TABLE gaps (kind TEXT, follow_up TEXT, state TEXT, "
                     "resolved_at TEXT)")
        rows = [("reply_owed", ME, "open", None), ("reply_owed", B, "open", None),
                ("mc_task_overdue", B, "open", None), ("price_below_cost", C, "open", None),
                ("reply_owed", EXCLUDED, "open", None),
                ("reply_owed", B, "resolved", "2999-01-01"),
                ("reply_owed", C, "resolved", "2000-01-01"),
                ("reply_owed", EXCLUDED, "resolved", "2999-01-01")]
        for kind, owner, state, at in rows:
            conn.execute("INSERT INTO gaps VALUES (?, ?, ?, ?)",
                         (kind, json.dumps({"owner": owner, "who_can_answer": []}), state, at))
        conn.commit()
        conn.close()
        self.backend = Backend(lambda s: None, data_label="t", store_path=store,
                               data_dir=self.dir)
        self.backend.owner, self.backend.gaps, self.backend.refuse = False, [], set()
        patcher = mock.patch.dict(os.environ, {"PLX_EXCLUDED_PEOPLE_OIDS": EXCLUDED})
        patcher.start()
        self.addCleanup(patcher.stop)

    def client(self, oid: str = ME, login: bool = True) -> TestClient:
        session = dataclasses.replace(fixture_identity("plx", ["staff:demo"])(),
                                      principals=frozenset({"staff:demo", f"employee:{oid}"}))
        client = TestClient(create_app(self.backend, DevAuth(lambda: session),
                                       secure_cookie=False), base_url="http://testserver")
        if login:
            self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        return client

    # ---- the owner view -------------------------------------------------------------------

    def test_only_the_owner_reads_it_and_nothing_in_the_request_changes_that(self):
        anon = self.client(login=False)
        self.assertEqual(anon.get("/api/owner/workload").status_code, 401)
        client = self.client()
        self.assertEqual(client.get("/api/owner/workload").status_code, 403)
        # forged claims: query, headers and a manager grant do not make a reader the owner
        for kw in ({"params": {"owner": "1", "as": ME}},
                   {"headers": {"X-Owner": "1", "X-Forwarded-User": "owner",
                                "Authorization": "Bearer owner"}}):
            self.assertEqual(client.get("/api/owner/workload", **kw).status_code, 403)
        (self.dir / "workload-grants.json").write_text(json.dumps(
            {"teams": {B: "SALES"}, "managers": {ME: ["SALES"]}}))
        found = client.get("/api/owner/workload")
        self.assertEqual(found.status_code, 403)
        self.assertNotIn(B, found.text)
        self.assertEqual(client.post("/api/owner/workload", json={}).status_code, 405)

    def test_owner_sees_each_cleared_person_counts_only_and_excluded_never(self):
        self.backend.owner = True
        found = self.client().get("/api/owner/workload")
        self.assertEqual(found.status_code, 200, found.text)
        body = found.json()
        by = {p["person"]: p for p in body["people"]}
        self.assertEqual(sorted(by), sorted([ME, B, C]))
        self.assertEqual((by[B]["awaiting_reply"], by[B]["tasks"], by[B]["alerts_closed"]),
                         (1, 1, 1))
        self.assertEqual((by[C]["alerts"], by[C]["alerts_closed"]), (1, 0))   # old close: out
        self.assertNotIn(EXCLUDED, found.text)
        self.assertIn("mail, Teams and tasks only", body["note"])
        self.assertIn("undercount", body["note"])
        text = found.text.lower()
        for word in ("subject", "body", "summary", "citation", "@"):
            self.assertNotIn(word, text)
        # the exclusion fails closed: guard missing, nobody is listed
        with mock.patch("operating.briefs.env_people", return_value=None):
            closed = self.client().get("/api/owner/workload").json()
        self.assertEqual(closed["people"], [])

    # ---- the R&D board --------------------------------------------------------------------

    def test_board_draft_card_is_never_sent_and_hides_addresses_elsewhere(self):
        self.backend.gaps = [rd_line(1, "p1")]
        found = self.client().get("/api/rd-projects")
        self.assertEqual(found.status_code, 200, found.text)
        body = found.json()
        (project,) = body["projects"]
        self.assertEqual((body["sent"], body["writes"], project["draft"]["sent"]), (0, 0, False))
        self.assertEqual(project["card_id"], "rd:p1")
        self.assertEqual(project["draft"]["to"], [{"address": "buyer1@synth-customer.example"}])
        self.assertEqual(found.text.count("synth-customer.example"), 1)   # only inside the draft
        self.assertNotIn("contact", project)
        self.assertEqual((project["waiting_on"], project["owner"]), ("us", B))

    def test_restricted_reader_loses_a_project_whose_record_it_cannot_open(self):
        self.backend.gaps = [rd_line(1, "p1", "Customer:1"), rd_line(2, "p2", "Customer:2")]
        self.backend.refuse = {"Customer:2"}
        found = self.client().get("/api/rd-projects")
        ids = [p["id"] for p in found.json()["projects"]]
        self.assertEqual(ids, ["p1"])
        self.assertNotIn("Customer:2", found.text)
        self.assertNotIn("buyer2@", found.text)
        # and its card cannot be rebuilt by guessing the id
        card = self.client().post("/api/actions/preview", json={
            "card_id": "rd:p2", "kind": "reply_in_thread", "payload": {}})
        self.assertEqual(card.status_code, 404)

    def test_board_cardinality_one_ten_sixty(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.backend.gaps = [rd_line(i, f"p{i}", f"Customer:{i}") for i in range(n)]
                t0 = time.monotonic()
                body = self.client().get("/api/rd-projects").json()
                self.assertEqual(len(body["projects"]), n)
                self.assertEqual(len({p["id"] for p in body["projects"]}), n)
                self.assertEqual(len({p["customer"] for p in body["projects"]}), n)
                self.assertLess(time.monotonic() - t0, 20)


if __name__ == "__main__":
    unittest.main()
