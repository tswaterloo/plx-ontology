"""The HTTP API as a non-owner with the real access layer: the reader's policy is the one the
object workspace's host decides (``HostAccess.reader_policy``: the fixture world's source
audiences, per request), not a stand-in. ``/api/suggest`` reads a scratch Postgres
(``search.entities``) through that policy; ``/api/object`` and ``/api/ask`` read the same
world's ontology store through it. A reader outside a source's audience gets no suggestion,
page, fact or cited value of it, whatever the request claims, and an excluded person's
record is never suggested. Evidence: isolated fixture + scratch Postgres; no network."""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from operating.tests import pg_testdb

try:
    import psycopg
    from fastapi.testclient import TestClient
    from operating import session_identity
    from operating.case_runtime import CaseRuntime
    from operating.pg import migrate
    from operator_api import backend as be
    from operator_api.app import create_app
    from operator_api.auth import DevAuth, fixture_identity
    from operator_api.tests import case_fixtures
    from operator_api.workspace import case_console, object_workspace as objects
except ImportError:  # pragma: no cover
    psycopg = None

OPS, QUALITY, OUTSIDER = "staff:operations", "staff:quality", "staff:finance"
GONE = "33333333-3333-4333-8333-333333333333"   # an excluded person
# (entity id, name, audience key): keys are source audiences of the fixture world
ENTITIES = [
    ("e1", "Zed Operations Supplier", "api:public.app_supplier"),
    ("e2", "Zed Operations Order", "api:public.app_purchase_order"),
    ("e3", "Zed Quality Task", "api:silver.production_tasks"),
    ("e4", "Zed Excluded Person Contact", f"mail:{GONE}"),
]


# What the planner (the model) was shown: every tool result of every step, in order. A
# record outside the reader's audience must never be in it: a model shown it could say it,
# whatever later gate drops the answer's facts.
SHOWN: list = []


def planner_for(po_number):
    """The Ask planner: finds the purchase order by its number, reads it, and answers
    only from what that read returned (a model reads what it was shown)."""
    def shown(result):
        return result["shown"] if isinstance(result, dict) and "omitted" in result \
            and isinstance(result.get("shown"), dict) else result

    def planner(request):
        done = request["transcript"]
        if done:
            SHOWN.extend(o.get("result") for o in done[-1].get("observations") or ())
        if not done:
            return {"action": "call", "calls": [{"tool": "plx.find", "arguments": {
                "query": po_number, "identifier_kind": "petra_po"}}]}, None
        found = shown(done[0]["observations"][0]["result"])
        cands = found.get("candidates") or []
        if not cands:
            return {"action": "answer", "text": "Nothing was found for that order.",
                    "claims": [], "unknowns": ["the order is not available to you"]}, None
        if len(done) == 1:
            return {"action": "call", "calls": [{"tool": "plx.object", "arguments": {
                "object_id": cands[0]["object"]["object_id"]}}]}, None
        values = shown(done[1]["observations"][0]["result"]).get("values") or []
        value = next((v for v in values if v.get("citation") and v.get("value") is not None),
                     None)
        if value is None:
            return {"action": "answer", "text": "Nothing could be read.", "claims": [],
                    "unknowns": ["no value"]}, None
        plain = value["value"]
        plain = plain["untrusted_text"] if isinstance(plain, dict) else plain
        return {"action": "answer", "text": "The order is on record as {po}.",
                "claims": [{"id": "po", "value": plain,
                            "citation": value["citation"]["l1_handle"],
                            "field": value["citation"]["field_path"]}],
                "unknowns": []}, None
    return planner


@unittest.skipIf(psycopg is None, "psycopg or fastapi not installed")
class RealAccessOverHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="pg-real-access-"))
        cls.state = cls.tmp / "state"
        case_fixtures.seed(cls.state, "plx")
        cls.store = cls.state / case_console.ONTOLOGY_FILE
        objects.project_mirror(cls.state, cls.store)
        with sqlite3.connect(cls.store) as conn:
            cls.po_id = conn.execute(
                "SELECT object_id FROM objects WHERE object_id LIKE "
                "'PurchaseOrder:portal.app_purchase_order:%' ORDER BY object_id").fetchone()[0]
        cls.db, cls.dsn = pg_testdb.create_db()
        with psycopg.connect(cls.dsn) as conn:
            migrate.migrate(conn, values={"embed_model": "m", "embed_dim": 8})
            for eid, name, audience in ENTITIES:
                conn.execute(
                    "INSERT INTO search.entities (entity_id, object_id, type, name, normalized,"
                    " audience_key, markings, alias) VALUES (%s, %s, 'supplier', %s, %s, %s,"
                    " '{}', '{}')", (eid, f'Supplier:bc.vendors:{{"id":"{eid}"}}', name,
                                    name.lower(), audience))
            conn.commit()
        cls.runtimes: list = []

        def objects_for(session):
            runtime = CaseRuntime(cls.state, client=None, tenant="plx",
                                  audience=cls.state / case_fixtures.MIRROR_AUDIENCE_FILE)
            cls.runtimes.append(runtime)
            return case_console.object_workspace_for(
                runtime, cls.store, session.actor, state_dir=cls.state,
                planner=planner_for(case_fixtures.PO_NUMBER), session=session,
                evidence=case_console.evidence_for(case_fixtures.marker(cls.state)))
        cls.backend = be.WorkspaceBackend(objects_for, data_label="t", pg_dsn=cls.dsn,
                                          store_path=cls.store, state_dir=cls.state)
        # the owner threads: each builds its runtime on its own thread and serves its queue
        import threading
        cls.threads = [threading.Thread(target=cls.backend.reads.loop, daemon=True),
                       threading.Thread(target=cls.backend.add_ask_worker(objects_for).loop,
                                        daemon=True)]
        for thread in cls.threads:
            thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.backend.stop()
        for thread in cls.threads:
            thread.join(timeout=10)
        pg_testdb.drop_db(cls.db)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        patch = mock.patch.dict(os.environ, {"PLX_EXCLUDED_PEOPLE_OIDS": GONE})
        patch.start()
        self.addCleanup(patch.stop)

    def client(self, principal: str, login: bool = True) -> TestClient:
        session = fixture_identity("plx", [principal])()
        client = TestClient(create_app(self.backend, DevAuth(lambda: session),
                                       secure_cookie=False), base_url="http://testserver")
        if login:
            self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        return client

    # ---- search (Postgres) --------------------------------------------------------------

    def suggested(self, principal: str, q: str = "zed") -> tuple[set[str], str]:
        found = self.client(principal).get("/api/suggest", params={"q": q, "limit": 20})
        self.assertEqual(found.status_code, 200, found.text)
        self.assertEqual(found.json()["source"], "search_entities")
        return {s["label"] for s in found.json()["suggestions"]}, found.text

    def test_search_follows_the_real_source_audiences(self) -> None:
        ops, text = self.suggested(OPS)
        self.assertEqual(ops, {"Zed Operations Supplier", "Zed Operations Order"})
        for hidden in ("Zed Quality Task", "Zed Excluded Person Contact", GONE):
            self.assertNotIn(hidden, text)
        quality, text = self.suggested(QUALITY)
        self.assertEqual(quality, {"Zed Quality Task"})
        self.assertNotIn("Zed Operations", text)
        nobody, text = self.suggested(OUTSIDER)
        self.assertEqual(nobody, set())
        self.assertNotIn("Zed", text)

    # ---- the record page ----------------------------------------------------------------

    def page(self, principal: str, **kw):
        return self.client(principal).get(f"/api/object/PurchaseOrder/{quote(self.po_id, safe='')}", **kw)

    def test_the_record_page_opens_only_for_a_reader_of_its_source(self) -> None:
        self.assertEqual(self.page(OPS).status_code, 200)
        for principal in (QUALITY, OUTSIDER):
            for kw in ({}, {"params": {"as": "owner", "owner": "1"}},
                       {"headers": {"X-Owner": "1", "X-Forwarded-User": "owner"}}):
                found = self.page(principal, **kw)
                self.assertEqual(found.status_code, 404, (principal, found.text[:200]))
                self.assertNotIn(case_fixtures.PO_NUMBER, found.text)
        self.assertEqual(self.client(OPS, login=False).get(
            f"/api/object/PurchaseOrder/{quote(self.po_id, safe='')}").status_code, 401)

    # ---- Ask ----------------------------------------------------------------------------

    def asked(self, principal: str) -> tuple[dict, str]:
        """The answer payload and the whole stream of a question that names no record (the
        planner looks the order up itself, as the reader); ``SHOWN`` holds what the
        planner was shown for it."""
        SHOWN.clear()
        with self.client(principal).stream("POST", "/api/ask", json={
                "question": "Which purchase order should I look at?"}) as response:
            self.assertEqual(response.status_code, 200)
            text = "".join(response.iter_text())
        answers = [json.loads(line[6:]) for block in text.split("\n\n")
                   for line in block.split("\n") if line.startswith("data: ")
                   and block.startswith("event: answer")]
        self.assertEqual(len(answers), 1, text[:600])
        return answers[0], text

    def test_ask_answers_from_the_readers_own_sources_only(self) -> None:
        mine, _text = self.asked(OPS)
        self.assertTrue(mine["facts"], mine)                      # the reader's own source
        self.assertIn(case_fixtures.PO_NUMBER, json.dumps(mine))
        # the planner found and read the order for its reader (the gate's other side)
        self.assertIn("po-synth", json.dumps(SHOWN, default=str))
        for principal in (QUALITY, OUTSIDER):
            answer, text = self.asked(principal)
            self.assertEqual(answer["facts"], [], principal)
            self.assertNotEqual(answer["outcome"], "answered", principal)
            for secret in (case_fixtures.PO_NUMBER, self.po_id):
                self.assertNotIn(secret, text, principal)
            # nothing of the order reached the model: no candidate, no read, no value
            self.assertTrue(SHOWN, principal)   # the planner did look it up
            seen = json.dumps(SHOWN, default=str)
            self.assertNotIn(self.po_id, seen, principal)
            self.assertNotIn("po-synth", seen, principal)


if __name__ == "__main__":
    unittest.main()
