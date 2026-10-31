"""The real backend over the console's fixture world (isolated-fixture, scripted planner):
the ask payload from the gate's claims, a restricted reader through every route, hidden
items as counts only, conflict cards, the Postgres suggest permission filter, feedback,
and the dev-identity refusals. No network; Postgres only in a throwaway database."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
from queue import Queue
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from operating import objective_runner, proactive_engine
from operator_api import backend as be
from operator_api import models as m
from operator_api.server import main as serve_main
from operator_ui import case_console
from operator_ui import object_workspace as objects
from operator_ui.tests import case_fixtures
from operator_ui.tests.test_objective_box import _on_record

OPS = [case_fixtures.OPERATIONS, case_fixtures.PURCHASING, case_fixtures.QUALITY]


HOLD = threading.Event()
HOLD.set()
HELD = threading.Event()


def planner(request):
    if "HOLD" in request["question"] and not request["transcript"]:
        HELD.set()          # an Ask is now running in the pool...
        HOLD.wait(60)       # ...until the test lets it finish
    roots = [line.split(": ", 1)[1] for line in request["question"].splitlines()
             if line.startswith(objective_runner.ROOT_LINE)]
    if not request["transcript"]:
        return {"action": "call", "calls": [
            {"tool": "plx.object", "arguments": {"object_id": oid}} for oid in roots]
            or [{"tool": "plx.find", "arguments": {"query": case_fixtures.PO_NUMBER}}]}, None
    result = request["transcript"][0]["observations"][0]["result"]
    if "values" not in result:
        return {"action": "abstain", "reason": "nothing readable"}, None
    return {"action": "answer", "unknowns": ["not in the record: why it is late"],
            **_on_record(result["values"])}, None


def events(text):
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


class Served:
    """``python3 -m operator_api serve --dev`` over the fixture state, in a thread."""

    def __init__(self, state: Path, principals, data_dir: Path) -> None:
        got: Queue = Queue(maxsize=1)
        argv = ["--dev", "--insecure-cookie", "--state-dir", str(state), "--ask",
                "--pg-dsn", "", "--app-data-dir", str(data_dir),
                "--console-arg=--source-mirror-audience",
                f"--console-arg={state / case_fixtures.MIRROR_AUDIENCE_FILE}"]
        for principal in principals:
            argv += ["--dev-fixture-principal", principal]

        def run():
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    serve_main(argv, ready=got.put, fixtures=case_fixtures, planner=planner)
            except BaseException as error:  # surfaced to the test thread
                got.put(error)
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        server = got.get(timeout=120)
        if isinstance(server, BaseException):
            raise server
        self.app = server.config.app
        self.client = TestClient(self.app, base_url="http://testserver")
        assert self.client.post("/api/auth/dev-login", json={}).status_code == 200

    def stop(self) -> None:
        self.app.state.backend.stop()
        self.thread.join(timeout=30)


class FixtureBackend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="app-api-"))
        cls.state = cls.tmp / "state"
        case_fixtures.seed(cls.state, "plx")
        store = cls.state / case_console.ONTOLOGY_FILE
        objects.project_mirror(cls.state, store)
        proactive_engine.after_build(store)
        cls.ops = Served(cls.state, OPS, cls.tmp / "ops-app")
        cls.outsider = Served(cls.state, ["staff:outsider"], cls.tmp / "outsider-app")

    @classmethod
    def tearDownClass(cls):
        cls.ops.stop()
        cls.outsider.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def ask(self, served, question):
        response = served.client.post("/api/ask", json={"question": question})
        self.assertEqual(response.status_code, 200)
        return events(response.text)

    def test_ask_maps_the_gate_kept_claims_to_cited_facts(self):
        seen = self.ask(self.ops, f"is {case_fixtures.PO_NUMBER} late?")
        names = [n for n, _ in seen]
        self.assertEqual(names[0], "status")
        self.assertIn("understanding", names)
        self.assertEqual(names[-1], "answer")
        for name, data in seen:
            m.ASK_EVENTS[name].model_validate(data)
        answer = seen[-1][1]
        self.assertIn(answer["outcome"], ("answered", "partial"))
        self.assertTrue(answer["facts"])
        gated = [f for f in answer["facts"] if f["check"] == "passed"]
        self.assertTrue(gated, answer["facts"])
        source = gated[0]["sources"][0]
        self.assertEqual(source["system"], "Portal")
        self.assertTrue(source["fields"])           # the value the gate checked
        self.assertTrue(source["open_url"].startswith("/source?handle="))
        self.assertTrue(any("why it is late" in n["what"] for n in answer["not_found"]))
        self.assertEqual(answer["subjects"][0]["type"], "PurchaseOrder")
        # the cited source opens for this reader through /api/source
        handle = source["handle"]
        opened = self.ops.client.get("/api/source", params={"handle": handle})
        self.assertEqual(opened.status_code, 200, opened.text)
        self.assertEqual(opened.json()["handle"], handle)
        # feedback on one's own answer is logged; recent questions show it
        fb = self.ops.client.post("/api/feedback", json={
            "answer_id": answer["answer_id"], "rating": -1, "note": "wrong date"})
        self.assertEqual(fb.status_code, 200, fb.text)
        self.assertEqual(fb.json()["stored_in"], ["feedback_log"])
        logged = (self.tmp / "ops-app" / "feedback.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(logged[-1])["note"], "wrong date")
        self.assertEqual(oct((self.tmp / "ops-app" / "feedback.jsonl").stat().st_mode & 0o777),
                         "0o600")
        home = self.ops.client.get("/api/home").json()
        self.assertEqual(home["recent_questions"][0]["answer_id"], answer["answer_id"])
        # another reader cannot rate (or learn of) it
        other = self.outsider.client.post("/api/feedback", json={
            "answer_id": answer["answer_id"], "rating": 1})
        self.assertEqual(other.status_code, 404)

    def test_object_page_and_suggest_for_the_reader(self):
        found = self.ops.client.get("/api/suggest",
                                    params={"q": case_fixtures.PO_NUMBER}).json()
        self.assertEqual(found["suggestions"][0]["match"], "exact")
        typo = self.ops.client.get("/api/suggest", params={"q": "Synthetic Botle"}).json()
        self.assertTrue(any("Bottle" in s["label"] for s in typo["suggestions"]), typo)
        page = self.ops.client.get(f"/api/object/PurchaseOrder/{case_fixtures.PO_NUMBER}")
        self.assertEqual(page.status_code, 200, page.text)
        body = page.json()
        m.ObjectPage.model_validate(body)
        self.assertEqual(body["label"], case_fixtures.PO_NUMBER)
        self.assertTrue(body["facts"] and body["related"])
        whole = self.ops.client.get(found["suggestions"][0]["open_url"]
                                    .replace("/object/", "/api/object/", 1))
        self.assertEqual(whole.json()["object_id"], body["object_id"])

    def test_a_po_page_and_answer_carry_its_trail_each_step_opening_for_the_reader(self):
        body = self.ops.client.get(
            f"/api/object/PurchaseOrder/{case_fixtures.PO_NUMBER}").json()
        trail = body["trail"]
        self.assertEqual(trail["kind"], "purchase_order", trail)
        (po,) = trail["trails"]
        self.assertEqual(po["subject"]["object_id"], body["object_id"])
        self.assertEqual(po["steps"][-1]["kind"], "verdict", po["steps"])
        self.assertTrue(po["verdict"])
        # the fixture PO's status word is unmapped: lateness and promises are not read,
        # so nothing is said about a confirmation either way
        self.assertEqual((po["state"], po["eta"], po["next_action"]),
                         ("undetermined", None, None))
        for step in po["steps"]:
            if step["source"] is None:
                self.assertEqual((step["kind"], po["state"]), ("verdict", "undetermined"))
                continue
            opened = self.ops.client.get("/api/source",
                                         params={"handle": step["source"]["handle"]})
            self.assertEqual(opened.status_code, 200 if step["source"]["open_url"] else 404)
        answer = self.ask(self.ops, f"is {case_fixtures.PO_NUMBER} late?")[-1][1]
        self.assertEqual([t["subject"]["object_id"] for t in answer["trail"]["trails"]],
                         [body["object_id"]])
        self.assertEqual(answer["trail"]["trails"][0]["steps"], po["steps"])

    def test_a_restricted_reader_sees_nothing_through_any_route(self):
        ops = self.ops.client
        po = ops.get(f"/api/object/PurchaseOrder/{case_fixtures.PO_NUMBER}").json()
        handle = po["facts"][0]["sources"][0]["handle"]
        # what the outsider never typed: the supplier's name, record ids, the handle
        secrets_ = {"Synthetic Bottle", "po-synth", handle, po["object_id"]}
        out = self.outsider.client
        bodies = [json.dumps(self.ask(self.outsider, f"is {case_fixtures.PO_NUMBER} late and who "
                                      "supplies it?"))]
        for path, params in (("/api/suggest", {"q": case_fixtures.PO_NUMBER}),
                             ("/api/suggest", {"q": "Synthetic Botle"}),
                             ("/api/home", None),
                             (f"/api/object/PurchaseOrder/{case_fixtures.PO_NUMBER}", None),
                             ("/api/object/PurchaseOrder/" + po["object_id"], None),
                             ("/api/source", {"handle": handle})):
            response = out.get(path, params=params)
            self.assertIn(response.status_code, (200, 404), (path, response.text))
            bodies.append(response.text)
        self.assertEqual(out.get("/api/source", params={"handle": handle}).status_code, 404)
        for body in bodies:
            for secret in secrets_:
                self.assertNotIn(json.dumps(secret)[1:-1], body, body[:300])
                self.assertNotIn(secret, body, body[:300])
        answer = json.loads(bodies[0])[-1][1]
        self.assertEqual(answer["facts"], [])

    def test_an_ask_in_progress_never_holds_up_other_routes(self):
        routes = (("/api/home", None), ("/api/suggest", {"q": case_fixtures.PO_NUMBER}),
                  (f"/api/object/PurchaseOrder/{case_fixtures.PO_NUMBER}", None),
                  ("/api/me", None))
        for client in (self.ops.client, self.outsider.client):
            for path, params in routes:   # cold costs are not what this measures
                client.get(path, params=params)
        HOLD.clear()
        streamed: list = []
        asker = threading.Thread(target=lambda: streamed.append(self.ops.client.post(
            "/api/ask", json={"question": f"HOLD is {case_fixtures.PO_NUMBER} late?"})),
            daemon=True)
        asker.start()
        self.assertTrue(HELD.wait(60), "the Ask never reached its planner")
        try:
            po = self.outsider.client  # another reader, and the same reader, both answer
            for client in (self.ops.client, po):
                for path, params in routes:
                    started = time.perf_counter()
                    response = client.get(path, params=params)
                    took = time.perf_counter() - started
                    self.assertIn(response.status_code, (200, 404), path)
                    self.assertLess(took, 1.0, (path, took))
        finally:
            HOLD.set()
            asker.join(60)
        self.assertEqual(events(streamed[0].text)[-1][0], "answer")

    def test_late_pos_on_home_are_longest_late_first(self):
        home = self.ops.client.get("/api/home").json()
        m.Home.model_validate(home)
        days = [p["days_late"] for p in home["needs_attention"]["late_pos"]]
        self.assertEqual(days, sorted(days, reverse=True))


class _View:
    """The workspace parts the mapping reads: ``decide`` withholding chosen handles, and
    a policy that grants nothing."""

    def __init__(self, closed):
        self.closed = set(closed)
        view = self

        class Policy:
            def may_read(self, actor, key):
                return False

        class Access:
            def decide(self, handles):
                return {h: ({"reason": "not_granted"} if h in view.closed else None)
                        for h in handles}

            def reader_policy(self):
                return object(), Policy()
        self.access = Access()

    def _can_open(self, handle):
        return True


OID_OK = "11111111-1111-4111-8111-111111111111"
OID_EXCLUDED = "22222222-2222-4222-8222-222222222222"
UNITS = {"api://public.app_purchase_order/x@g-1": "api:public.app_purchase_order",
         "graph-mail://a/SECRET-1@g-1": f"mail:{OID_OK}",
         "graph-mail://b/SECRET-2@g-1": f"mail:{OID_OK}",
         "graph-mail://c/SECRET-3@g-1": f"mail:{OID_EXCLUDED}",
         "sharepoint://d/SECRET-4": None}


class Mapping(unittest.TestCase):
    def backend(self):
        return be.WorkspaceBackend(lambda s: None, data_label="test")

    def hidden(self, exclusion):
        from integrations.people_exclusion import PeopleExclusion  # noqa: F401
        shown = {"text": "visible", "citations": ["api://public.app_po/1@g-1"]}
        secret = [{"text": f"SECRET subject line {h}", "citations": [h]} for h in UNITS]
        ran = {"claims": [shown], "withheld": len(secret),
               "loop": {"claims": [shown, *secret]}}
        with mock.patch("operator_ui.object_workspace.audience_unit", UNITS.get), \
                mock.patch("operating.pg.permissions.load_exclusion", return_value=exclusion):
            return self.backend()._hidden(_View(set(UNITS)), ran)

    def test_hidden_claims_follow_the_search_rule_and_show_nothing(self):
        from integrations.people_exclusion import PeopleExclusion
        hidden = self.hidden(PeopleExclusion([OID_EXCLUDED]))
        text = hidden.model_dump_json()
        # Portal (shared, named) 1 + two private mails (one unnamed bucket); the excluded
        # person's mail and the unit-less file are never counted
        self.assertEqual(hidden.count, 3)
        self.assertEqual([(o.owner_or_team, o.count, o.named) for o in hidden.by_owner],
                         [("Portal", 1, True),
                          ("Other people's mail, chats and files you can't open", 2, False)])
        for leak in ("SECRET", "graph-mail", OID_OK, OID_EXCLUDED, "sharepoint"):
            self.assertNotIn(leak, text)
        self.assertIn("don't have access", hidden.message)
        unloaded = self.hidden(PeopleExclusion(unavailable="excluded_people_oids_absent"))
        self.assertIsNone(unloaded.count)
        self.assertEqual(unloaded.by_owner, [])

    def test_landed_cost_comes_from_the_plans_computed_entries_not_prose(self):
        line = "bc://c/purchaseOrderLines/1@g-1"
        ran = {"investigation": {"answerability": {"state": "answerable"}, "answer": [
            {"name": "unit_price", "kind": "property", "value": "0.84",
             "citation": {"l1_handle": line, "field_path": "row.directUnitCost"}},
            {"name": "landed_cost_line_1", "kind": "computed", "value": "10080.00",
             "unit": "USD", "inputs": ["unit_price"]},
            {"name": "landed_cost_line_2", "kind": "computed", "value": "2265.675",
             "unit": "USD", "inputs": []},
            {"name": "landed_cost", "kind": "computed", "value": "12345.675", "unit": "USD",
             "inputs": ["landed_cost_line_1", "landed_cost_line_2"]},
            # decoys: a rate, a quantity, a label with capitals and digits
            {"name": "landed_cost_line_3", "kind": "computed", "value": "0.84",
             "unit": "USD/ea"},
            {"name": "landed_cost_line_4", "kind": "computed", "value": "4471",
             "unit": "SKU", "currency": "SKU1"},
            {"name": "goods_qty", "kind": "computed", "value": "12000", "unit": "EA"}]},
            "verdict": {"state": "answered",
                        "text": "Landed cost 12345.68 USD",
                        "facts": [{"text": "Item: 4471 SKU (BC line)", "citation": line,
                                   "openable": True}], "follow_ups": []}}
        cost = be.landed_cost_of(self.backend(), _View(()), ran)
        self.assertEqual(cost.status, "computed")
        self.assertEqual((cost.total.amount, cost.total.currency), ("12345.675", "USD"))
        self.assertEqual([(l.label, l.amount.amount, l.amount.currency) for l in cost.lines],
                         [("Line 1", "10080.00", "USD"), ("Line 2", "2265.675", "USD")])
        self.assertEqual([s.handle for s in cost.lines[0].sources], [line])
        self.assertNotIn("4471", cost.model_dump_json())
        prose_only = {"investigation": {"answer": []}, "verdict": ran["verdict"]}
        nothing = be.landed_cost_of(self.backend(), _View(()), prose_only)
        self.assertEqual((nothing.status, nothing.total, nothing.lines),
                         ("not_established", None, []))

    def test_people_are_named_with_their_roles_and_only_when_openable(self):
        cite = "graph-mail://box/messages/1@g-1"
        closed = "graph-mail://box/messages/2@g-1"
        ran = {"investigation": {"answer": [
            {"name": "person_1", "kind": "computed", "value": {
                "untrusted_text": "Dana Ortiz", "origin": {"citation": cite}},
             "steps": ["identity verified", "roles: owner on 2 record(s), sender on 1 "
                                            "record(s)"]},
            {"name": "person_2", "kind": "computed", "value": {
                "untrusted_text": "Hidden Person", "origin": {"citation": closed}},
             "steps": ["identity verified", "roles: approver on 1 record(s)"]},
            {"name": "person", "kind": "count", "value": "2"}]}}
        people = be.people_of(self.backend(), _View({closed}), ran)
        self.assertEqual([(p.name, p.role) for p in people], [("Dana Ortiz", "owner, sender")])
        self.assertIsNone(be.people_of(self.backend(), _View(()), {"investigation": {
            "answer": []}}))

    def conflict(self, ranks):
        return {"kind": "value_disagreement", "subject": "PurchaseOrder.expected_date",
                "values": [{"property": "expected_date", "value": v,
                            "citation": {"l1_handle": h}, "authority_rank": r}
                           for v, h, r in zip(("2026-10-17", "2026-10-24"),
                                              ("bc://c/purchaseOrders/1", "api://p/po/1"),
                                              ranks)]}

    def test_conflict_cards_trust_the_declared_authority_and_say_why(self):
        [card] = self.backend()._conflicts(_View(()), [self.conflict((1, 2))], "PO 1")
        self.assertEqual([v.trusted for v in card.values], [True, False])
        self.assertIn("BC is the system of record", card.values[0].why)
        self.assertEqual(card.field, "expected date")
        [tie] = self.backend()._conflicts(_View(()), [self.conflict((None, None))], "PO 1")
        self.assertEqual([v.trusted for v in tie.values], [False, False])
        self.assertIn("both are shown", tie.values[0].why)
        hidden_side = self.backend()._conflicts(_View({"bc://c/purchaseOrders/1"}),
                                                [self.conflict((1, 2))], "PO 1")
        self.assertEqual(hidden_side, [])

    def test_a_cached_page_is_decided_again_for_its_sources(self):
        handle = "sharepoint://site/drive/item-1@g-1"
        view = _View(())
        built = []

        class Policy:
            def may_read(self, actor, key):
                return True

        view.access.reader_policy = lambda: (object(), Policy())
        view.access.store_meta = lambda: {"content_digest": "sha256:x"}
        view._request = contextlib.nullcontext
        view.investigate = lambda family, oid: {}

        def workspace(oid):
            built.append(oid)
            return {"object_id": oid, "status": "read", "card": {
                "object": {"object_type": "PurchaseOrder", "display": "PO 1"},
                "values": [{"property": "status", "value": "open",
                            "citation": {"l1_handle": handle}}]}}
        view.workspace = workspace
        backend = self.backend()
        with mock.patch("operating.pg.permissions._fingerprint", lambda p, a: "fp"):
            be.object_page(backend, view, "PurchaseOrder", "PurchaseOrder:demo:1")
            be.object_page(backend, view, "PurchaseOrder", "PurchaseOrder:demo:1")
            self.assertEqual(len(built), 1)                 # served from the cache
            view.closed.add(handle)                          # its item ACL changed
            be.object_page(backend, view, "PurchaseOrder", "PurchaseOrder:demo:1")
            self.assertEqual(len(built), 2)                 # never served stale

    def test_ask_switched_off_says_so_plainly(self):
        from fastapi.testclient import TestClient as Client
        from operator_api.app import create_app
        from operator_api.auth import DevAuth, fixture_identity
        backend = be.WorkspaceBackend(lambda s: None, data_label="test", ask_served=False)
        client = Client(create_app(backend, DevAuth(fixture_identity("plx", ["staff:x"])),
                                   secure_cookie=False))
        client.post("/api/auth/dev-login", json={})
        last = events(client.post("/api/ask", json={"question": "late?"}).text)[-1]
        self.assertEqual(last, ("error", {"code": "ask_not_served",
                                          "message": "Ask isn't switched on on this server."}))

    def test_dev_identity_refusals(self):
        parser = argparse.ArgumentParser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                be.run(parser, argparse.Namespace(dev_ticket_login=False,
                                                  dev_employee_oid=None,
                                                  dev_fixture_principal=[]), "dev",
                       lambda *a: 0)
            tmp = Path(tempfile.mkdtemp(prefix="app-api-live-"))
            self.addCleanup(shutil.rmtree, tmp, True)
            (tmp / "session-state-label.json").write_text(json.dumps(
                {"schema": "plx-session-state-label/v1", "label": "live"}))
            args = argparse.Namespace(dev_fixture_principal=[], dev_employee_oid="x",
                                      dev_ticket_login=False)
            with self.assertRaises(SystemExit):
                be.make_auth(parser, args, "dev", {"resolver": object(), "session": None,
                                                   "state_dir": tmp})


PG = shutil.which("createdb") is not None and os.environ.get("PLX_TEST_NO_PG") != "1"


@unittest.skipUnless(PG, "no local Postgres")
class SuggestPermissionFilter(unittest.TestCase):
    """``search.entities`` through the reader scope, on the own migrations in a
    throwaway database."""

    def setUp(self):
        self.db = f"plx_test_appapi_{os.getpid()}"
        if subprocess.run(["createdb", self.db], capture_output=True).returncode != 0:
            self.skipTest("createdb refused")
        self.addCleanup(subprocess.run, ["dropdb", "--if-exists", "--force", self.db],
                        capture_output=True)
        self.dsn = f"postgresql:///{self.db}"
        import psycopg
        from operating.pg.migrate import migrate
        with psycopg.connect(self.dsn) as conn:
            conn.autocommit = True
            migrate(conn)
            for i, (name, key, marks) in enumerate((
                    ("Maple Leaf Soapworks", "api:public.app_supplier", []),
                    ("Maple Grove Secret Holdings", "api:public.app_finance_only", []),
                    ("Maple Street Payroll", "api:public.app_supplier", ["pii"]),
                    ("Northwind Packaging", "api:public.app_supplier", []))):
                conn.execute("INSERT INTO search.entities (entity_id, object_id, type, name, "
                             "normalized, audience_key, markings) VALUES (%s,%s,%s,%s,%s,%s,"
                             "%s)", (f"e{i}", f"Supplier:demo:{i}", "Supplier", name,
                                     be.normalize(name), key, marks))
        self.backend = be.WorkspaceBackend(lambda s: None, data_label="test", pg_dsn=self.dsn)
        self.addCleanup(self.backend.stop)
        from integrations.people_exclusion import PeopleExclusion
        patcher = mock.patch("operating.pg.permissions.load_exclusion",
                             return_value=PeopleExclusion([OID_EXCLUDED]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def grant(self, *keys):
        from ontology.types import Actor
        from operating.ontology_tools import ReadPolicy
        actor = Actor("plx", frozenset({"staff:x"}), "internal")
        policy = ReadPolicy("plx", {key: frozenset({"staff:x"}) for key in keys})
        self.backend._reader_policy = lambda reader: (actor, policy)

    def test_only_granted_audiences_and_held_markings_and_typos_match(self):
        reader = type("R", (), {"key": "staff:x"})()
        self.grant("api:public.app_supplier")
        self.backend._pg_suggest(reader, "warm", 8)   # pool and scope listing warmed
        started = time.perf_counter()
        got = self.backend._pg_suggest(reader, "mapel leaf", 8)
        took = (time.perf_counter() - started) * 1000
        names = [s.label for s in got]
        self.assertIn("Maple Leaf Soapworks", names)              # a typo still matches
        self.assertNotIn("Maple Grove Secret Holdings", names)    # audience not granted
        self.assertNotIn("Maple Street Payroll", names)           # marking not held
        self.assertLess(took, 200)
        # a revoked grant applies on the very next suggestion (no cached grant)
        self.grant()
        self.assertEqual(self.backend._pg_suggest(reader, "maple leaf", 8), [])


if __name__ == "__main__":
    unittest.main()
