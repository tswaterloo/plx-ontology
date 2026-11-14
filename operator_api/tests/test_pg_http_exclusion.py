"""The people exclusion end to end over HTTP, with the ontology served from Postgres
(``PLX_ONTOLOGY_PG=only``) and search from the same scratch database.

A synthetic world: the fixture Portal mirror (PO5447) and two captured mailboxes (the
capture tests' FakeGraph: mailbox A and B). Mailbox A's owner writes about the order from
their own mailbox and to mailbox B; a supplier writes to B as well. All of it was captured
and projected while nobody was excluded. First, each mailbox's reader (no owner) reads
their own mailbox's mail on Postgres and not the other's. Then mailbox A's owner is
excluded the way an operator does it while the API runs (the list file rewritten; nothing
restarted or rebuilt). From then on no route a reader uses (suggest, the record page and
its history, explore, record pack and brief views, a mail record's or the person's own
page, Ask and what its planner was shown, Home, the briefing and late lists) returns any of
that person's mail or record, to the mailbox's own reader, to the other mailbox's reader,
or to the owner-equivalent reader granted every mailbox and relation; the supplier's mail
stays visible to its readers (the exclusion takes out exactly that person's records).

Found by this test (CT-T-06): a case runtime kept the list it started with, so after a
person was excluded the record page and Ask of a reader already served kept showing mail
the person had sent to another mailbox, while search no longer did.

Evidence: isolated fixture + injected transport (FakeGraph) + scratch Postgres; every id
and address is made up; no network, no credentials."""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from operating.tests import pg_testdb

try:
    import psycopg
    from fastapi.testclient import TestClient
    from integrations import graph_read
    from integrations.capture_mail import MailCapture, MailCaptureStore
    from integrations.graph_read import GraphMailReader
    from integrations.live_source_guard import BoundedBackoff
    from integrations.people_exclusion import PeopleExclusion
    from integrations.tests.test_capture_mail import (BOX_A, BOX_B, CAPTURE, ENV, SCOPE, UPN_A,
                                                       Clock, FakeGraph,
                                                       capture_audience, reader_audience)
    from ontology import mentions
    from ontology import project
    from ontology import schema as schema_loader
    from ontology.people_links import people_links_step
    from operating.case_mail_scope import MailScope
    from operating.case_runtime import CaseRuntime, LiveReaders
    from operating.pg import migrate, ontology_load
    from operator_api import backend as be
    from operator_api.app import create_app
    from operator_api.auth import DevAuth, fixture_identity
    from operator_api.tests import case_fixtures
    from operator_api.workspace import case_console, object_workspace as objects
except ImportError:  # pragma: no cover
    psycopg = None

ORDERS, FINANCE = "staff:orders", "staff:finance"   # mailbox A's and mailbox B's readers
OPS, QUALITY, PURCHASING = (case_fixtures.OPERATIONS, case_fixtures.QUALITY,
                            case_fixtures.PURCHASING)
READERS = {"mailbox A reader": (ORDERS, OPS), "mailbox B reader": (FINANCE, OPS),
           "owner": (ORDERS, FINANCE, OPS, QUALITY, PURCHASING)}
# Words only the excluded person's mail carries, and the person's own identifiers.
HIDDEN = ("ZEDHIDEAONE", "ZEDHIDEBONE", "MSG-A1", "MSG-B1", UPN_A, BOX_A)
KEPT = "ZEDKEEPBTWO"   # the supplier's mail to mailbox B
NOBODY = "0e0e0e0e-0000-4000-8000-00000000e0e1"   # the capture's own excluded id


SHOWN: list = []


def planner(request):
    """Ask's planner: searches the mail about the order, then answers from what it was
    shown (a model says what it read)."""
    done = request["transcript"]
    if done:
        SHOWN.extend(o.get("result") for o in done[-1].get("observations") or ())
    if not done:
        return {"action": "call", "calls": [
            {"tool": "plx.documents", "arguments": {"query": case_fixtures.PO_NUMBER,
                                                    "sources": ["graph_mail"]}},
            {"tool": "plx.find", "arguments": {"query": case_fixtures.PO_NUMBER,
                                               "identifier_kind": "petra_po"}}]}, None
    return {"action": "answer", "text": "Here is what the mail says.", "claims": [],
            "unknowns": ["what the mail says"]}, None


@unittest.skipIf(psycopg is None, "psycopg or fastapi not installed")
class ExclusionEndToEnd(unittest.TestCase):
    maxDiff = None   # every leak is listed
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="pg-http-exclusion-"))
        cls.state = cls.tmp / "state"
        case_fixtures.seed(cls.state, "plx")
        graph = FakeGraph()
        po = case_fixtures.PO_NUMBER
        graph.put(BOX_A, "MSG-A1", "CK-A1", subject=f"ZEDHIDEAONE {po} draft",
                  body=f"ZEDHIDEAONE note on {po} from the excluded person's own mailbox.")
        graph.put(BOX_B, "MSG-B1", "CK-B1", subject=f"ZEDHIDEBONE {po} question",
                  body=f"ZEDHIDEBONE the excluded person asks about {po}.")
        graph.messages[BOX_B]["MSG-B1"]["from"] = {"emailAddress": {"address": UPN_A}}
        graph.put(BOX_B, "MSG-B2", "CK-B2", subject=f"{KEPT} {po} confirmed",
                  body=f"{KEPT} the supplier confirms {po}.")
        store = MailCaptureStore(cls.state, clock=Clock())
        api, _ = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        run = MailCapture(store, GraphMailReader(api, capture_audience()), CAPTURE,
                          sleep=lambda s: None).run(SCOPE)
        assert run["complete"], run
        store.close()
        cls.store = cls.state / case_console.ONTOLOGY_FILE
        mirror = project.MirrorCaptures(objects.mirror_path(cls.state))
        try:
            with project.MailCaptures(cls.state, internal_domains=SCOPE.internal_domains,
                                      exclusion=PeopleExclusion.from_env(ENV)) as mail:
                project.build_store(cls.store, schema_loader.load_schema(),
                                    {mirror.source: mirror, "graph_mail": mail},
                                    post_project={"mentions": mentions.store_mention_links,
                                                  "with_people": people_links_step})
        finally:
            mirror.close()
        with sqlite3.connect(cls.store) as conn:
            cls.po_id = conn.execute(
                "SELECT object_id FROM objects WHERE object_id LIKE "
                "'PurchaseOrder:portal.app_purchase_order:%' ORDER BY object_id").fetchone()[0]
            cls.mail_objects = {row[0]: row[1] for row in conn.execute(
                "SELECT DISTINCT o.object_id, v.l1_handle FROM objects o JOIN object_versions"
                " v ON v.object_id = o.object_id WHERE v.l1_handle LIKE 'graph-mail://%'")}
            cls.people = [row[0] for row in conn.execute(
                "SELECT object_id FROM objects WHERE object_type = 'Person'")]
        cls.db, cls.dsn = pg_testdb.create_db()
        with psycopg.connect(cls.dsn) as conn:
            migrate.migrate(conn, values={"embed_model": "m", "embed_dim": 8})
            conn.commit()
        ontology_load.load(cls.store, cls.dsn, workers=1, replace=True, log=io.StringIO())
        # the store's facts read on one connection (a parallel read spawns a process per
        # worker: minutes of start-up for a fixture this small)
        # the list a running API re-reads: a file outside git (``excluding`` rewrites it)
        cls.listed = cls.tmp / "excluded-people.json"
        cls.write_list(NOBODY)
        cls.env = mock.patch.dict(os.environ, {"PLX_ONTOLOGY_PG": "only",
                                               "PLX_PG_DSN": cls.dsn,
                                               "PLX_FACTS_WORKERS": "1",
                                               "PLX_EXCLUDED_PEOPLE_OIDS": NOBODY,
                                               "PLX_EXCLUDED_PEOPLE_FILE": str(cls.listed)})
        cls.env.start()
        audience = cls.state / case_fixtures.MIRROR_AUDIENCE_FILE
        cls.runtimes: list = []

        def objects_for(session):
            runtime = CaseRuntime(cls.state, client=None, tenant="plx", audience=audience,
                                  live=LiveReaders(audience=reader_audience()),
                                  mail_scope=MailScope.unrestricted())
            cls.runtimes.append(runtime)
            return case_console.object_workspace_for(
                runtime, cls.store, session.actor, state_dir=cls.state, markings=("pii",),
                mail_capture_dir=cls.state, planner=planner, session=session,
                evidence=case_console.evidence_for(case_fixtures.marker(cls.state)))
        cls.backend = be.WorkspaceBackend(objects_for, data_label="t", pg_dsn=cls.dsn,
                                          store_path=cls.store, state_dir=cls.state)
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
        cls.env.stop()
        for runtime in cls.runtimes:
            with contextlib.suppress(Exception):
                runtime.close()
        pg_testdb.drop_db(cls.db)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def write_list(cls, oid: str) -> None:
        staged = cls.listed.with_suffix(".new")
        staged.write_text(json.dumps({"schema": "plx-people-exclusion/v1",
                                      "object_ids": [oid]}), "utf-8")
        os.replace(staged, cls.listed)   # a new file: every reader sees the change at once

    def excluding(self, oid: str) -> None:
        """Exclude ``oid`` (with the capture's own id) as an operator would while the API
        runs: by rewriting the list file; nothing is restarted or rebuilt."""
        self.write_list(oid)

    def client(self, principals) -> TestClient:
        session = dataclasses.replace(fixture_identity("plx", list(principals),
                                                       clearance="restricted")(),
                                      principals=frozenset(principals))
        client = TestClient(create_app(self.backend, DevAuth(lambda: session),
                                       secure_cookie=False), base_url="http://testserver")
        self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        return client

    @staticmethod
    def get(client, path: str, **kw):
        """The route's answer; a refusal for load (busy) is asked again (a loaded host
        refuses while a read is slow: it says nothing either way)."""
        for _ in range(4):
            got = client.get(path, **kw)
            if got.status_code != 503 and '"code":"busy"' not in got.text:
                return got
        return got

    def routes(self) -> list[tuple[str, str]]:
        po = quote(self.po_id, safe="")
        out = [("GET", f"/api/{view}/PurchaseOrder/{po}")
               for view in ("object", "history", "explore", "record-pack", "brief")]
        for object_id in [*self.mail_objects, *self.people]:
            kind = object_id.split(":", 1)[0]
            out.append(("GET", f"/api/object/{kind}/{quote(object_id, safe='')}"))
        out += [("GET", f"/api/suggest?q={q}") for q in ("zedhide", "zedkeep", "orders", "PO5447")]
        out += [("GET", path) for path in ("/api/home", "/api/briefing",
                                           "/api/briefing/items", "/api/late")]
        return out

    def everything(self, principals) -> dict[str, str]:
        client = self.client(principals)
        out = {}
        for method, path in self.routes():
            got = self.get(client, path)
            # a refusal for load would pass every check below while proving nothing
            self.assertLess(got.status_code, 500, (path, got.text[:200]))
            self.assertNotIn('"code":"busy"', got.text, path)
            out[path] = got.text
        SHOWN.clear()
        with client.stream("POST", "/api/ask", json={
                "question": "What does the mail say about the order?"}) as response:
            self.assertEqual(response.status_code, 200)
            out["/api/ask"] = "".join(response.iter_text())
        self.assertIn("event: answer", out["/api/ask"])
        out["/api/ask (shown to the planner)"] = json.dumps(SHOWN, default=str)
        return out

    def page(self, client, object_id: str):
        kind = object_id.split(":", 1)[0]
        return self.get(client, f"/api/object/{kind}/{quote(object_id, safe='')}")

    def mail_id(self, message: str) -> str:
        return next(o for o in self.mail_objects if f'"id":"{message}"' in o)

    def test_the_exclusion_takes_out_exactly_that_persons_records_on_every_route(self):
        # before: nobody this world holds is excluded; each mailbox's reader (no owner)
        # reads their own mailbox's mail on Postgres and never the other's
        self.excluding(NOBODY)
        a_reader, b_reader = (self.client(READERS[name]) for name in
                              ("mailbox A reader", "mailbox B reader"))
        mine = self.page(a_reader, self.mail_id("MSG-A1"))
        self.assertEqual(mine.status_code, 200, mine.text[:300])
        self.assertIn("ZEDHIDEAONE", mine.text)
        theirs = self.page(a_reader, self.mail_id("MSG-B2"))
        self.assertEqual(theirs.status_code, 404, theirs.text[:300])
        self.assertNotIn(KEPT, theirs.text)
        found = self.get(b_reader, "/api/suggest", params={"q": "zedkeep"}).text
        self.assertIn(KEPT, found)
        theirs = self.get(a_reader, "/api/suggest", params={"q": "zedkeep"})
        self.assertEqual(theirs.status_code, 200, theirs.text)
        self.assertNotIn(KEPT, theirs.text)
        self.assertEqual(self.page(b_reader, self.mail_id("MSG-B1")).status_code, 200)

        # mailbox A's owner is excluded, after the capture and the projection
        self.excluding(BOX_A)
        leaks = []
        for name, principals in READERS.items():
            got = self.everything(principals)
            leaks += [f"{name} {path}: {hidden}" for path, text in got.items()
                      for hidden in HIDDEN if hidden in text]
            if name == "mailbox A reader":
                continue
            # the supplier's mail stays: its page, its suggestion and what Ask read
            kept = [path for path, text in got.items() if KEPT in text]
            self.assertIn(f"/api/object/Email/{quote(self.mail_id('MSG-B2'), safe='')}",
                          kept, name)
            self.assertIn("/api/suggest?q=zedkeep", kept, name)
            self.assertIn("/api/ask (shown to the planner)", kept, name)
        self.assertEqual(leaks, [])
        # the excluded person's own records are not found at all, for any reader
        owner = self.client(READERS["owner"])
        for object_id in (self.mail_id("MSG-A1"), self.mail_id("MSG-B1"),
                          next(p for p in self.people if BOX_A in p)):
            self.assertEqual(self.page(owner, object_id).status_code, 404, object_id)


if __name__ == "__main__":
    unittest.main()
