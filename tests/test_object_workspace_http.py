"""Object workspace routes of the operator console over the generic ontology tools.

The case console's fixture world (``operator_ui.tests.case_fixtures.seed``: synthetic rows read
through the real read-only clients over in-process fakes) is projected into an ontology
store (``ontology.project.build_store`` over its source mirror) and served through
``plx.find``/``plx.object``/``plx.around``/``plx.timeline``/``plx.process`` for one
host-configured reader, as ``python3 -m operator_ui.tests.case_fixtures --seed-fixtures`` does.
Citations open through the same case runtime. Evidence label: isolated-fixture /
injected-transport; no network, nothing live.
"""

from __future__ import annotations

import contextlib
import dataclasses
from datetime import datetime, timedelta, timezone
import hashlib
from http.client import HTTPConnection
import importlib.util
import io
import json
import os
from pathlib import Path
from queue import Queue
import re
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import quote

from ontology.core import Actor
from ontology.reviews import STORE_FILE as REVIEW_STORE_FILE, ReviewStore
from operating.action_path import FIXTURE_CAPABILITY, ActionPath, FixtureSystem
from operating.case_runtime import CaseRuntime
from operating.employee_permissions import (EmployeePermissionPolicy, EmployeePermissionResolver,
                                            PortalAccessSnapshot)
from operating.cases import PROPOSAL_STATUS, CaseStore
from operator_ui import case_console
from operator_ui.tests import case_fixtures
from operator_ui import object_workspace as objects
from operating import explanations
from operating import goal2_mc_approval as g2
from operating import ontology_tools as ot
from operating import ontology_tools_investigate as oi
from operating import ontology_tools_traverse as tt
from operating import session_identity
from operator_ui.case_workspace import CaseWorkspace
from operating.host import BC_DIR, BC_FILE
from operator_ui.server import make_server
from operator_ui.session_journal import SessionJournal, ask_counts, investigate_counts
from tests import test_investigate_landed_cost as landed
from types import SimpleNamespace

OPS = frozenset({case_fixtures.OPERATIONS, case_fixtures.QUALITY, case_fixtures.PURCHASING})
LIMITED = "staff:limited"
OUTSIDER = frozenset({"staff:finance"})
HIDDEN_RELATION = "public.app_purchase_order_line"


def _ids_and_handles(value, out):
    """Every object id and L1 handle anywhere in a response."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("object_id", "from", "to") and isinstance(item, str) and ":" in item \
                    and not item.startswith("set:"):
                out.add(item)
            elif isinstance(item, str) and item.startswith(objects.L1_SCHEMES):
                out.add(item)
            else:
                _ids_and_handles(item, out)
    elif isinstance(value, list):
        for item in value:
            _ids_and_handles(item, out)
    return out


def _leaves(value, out):
    """Every string leaf of a tool result (what a planner could restate)."""
    if isinstance(value, dict):
        for item in value.values():
            _leaves(item, out)
    elif isinstance(value, list):
        for item in value:
            _leaves(item, out)
    elif isinstance(value, str):
        out.append(value)
    return out


class ObjectWorkspaceHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="object-workspace-"))
        cls.state = cls.tmp / "state"
        case_fixtures.seed(cls.state, "plx")
        cls.store = cls.state / case_console.ONTOLOGY_FILE
        cls.report = objects.project_mirror(cls.state, cls.store)
        # A narrower reader: every fixture relation except the PO lines.
        audience = json.loads((cls.state / case_fixtures.MIRROR_AUDIENCE_FILE).read_text("utf-8"))
        for relation, principals in audience["audiences"].items():
            if relation != HIDDEN_RELATION:
                principals.append(LIMITED)
        cls.limited_audience = cls.tmp / "limited-audience.json"
        cls.limited_audience.write_text(json.dumps(audience), "utf-8")
        with sqlite3.connect(cls.store) as conn:
            cls.po_handle, cls.supplier_id = (conn.execute(
                "SELECT l1_handle FROM object_versions WHERE object_id LIKE ?",
                ("PurchaseOrder:portal.app_purchase_order:%",)).fetchone()[0],
                conn.execute("SELECT object_id FROM objects WHERE object_type = 'Supplier' "
                             "ORDER BY object_id").fetchone()[0])
        conn.close()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def serve(self, principals, *, audience=None, configured=True, actions=True,
              catalog_intent=False, live=False, planner=None, reports=False):
        """Console for one reader; runtime and tools are built in the serving thread.

        ``catalog_intent``: the action store is a fresh one holding one intent of a
        catalog action type (``fixture.record.set_field``) whose proposal cites the
        fixture PO's record and an isolated owner's target row. ``live``: the fixture's
        in-process Graph readers, as ``case_console --seed-fixtures`` serves them (the
        attached SharePoint originals and the mailbox, each behind its live audience).
        ``planner``: the Ask control's agent-loop planner (a scripted one here).
        ``reports``: the seeded playbook report store, as the console serves it."""
        ready: Queue = Queue(maxsize=1)
        audience = audience or self.state / case_fixtures.MIRROR_AUDIENCE_FILE

        def run():
            try:
                readers = (case_fixtures.live_readers(
                    case_fixtures.build_world(), self.state / case_fixtures.LIVE_AUDIENCE_FILE)
                    if live else None)
                runtime = CaseRuntime(self.state, client=None, audience=audience, tenant="plx",
                                      live=readers)
                view = None
                session = session_identity.fixture_session(
                    "plx", principals, channel=session_identity.CHANNEL_CONSOLE)
                actor = session.actor
                cases = None
                if configured:
                    path = (ActionPath(self.state / case_fixtures.ACTIONS_FILE, runtime.cases)
                            if actions else None)
                    if catalog_intent:
                        cases, path = self.register_catalog_intent(runtime, actor)
                    view = case_console.object_workspace_for(
                        runtime, self.store, actor, state_dir=self.state,
                        action_path=path, intents_from=cases, planner=planner,
                        evidence=case_console.evidence_for(case_fixtures.marker(self.state)),
                        report_store=case_console.report_store_for(
                            runtime, self.state / case_fixtures.REPORTS_FILE)
                        if reports else None)
                server = (make_server(case_workspace=CaseWorkspace(
                    runtime, actor), session=session, object_workspace=view)
                          if configured else make_server())
                ready.put(server)
                server.serve_forever(poll_interval=0.05)
                server.server_close()
                if view is not None:
                    view.close()
                runtime.close()
            except BaseException as error:  # surfaced to the test thread
                ready.put(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        server = ready.get(timeout=60)
        if isinstance(server, BaseException):
            raise server
        self.addCleanup(lambda: (server.shutdown(), thread.join(timeout=10)))
        port, token = server.server_address[1], server.operator_access_token

        def request(path, *, auth=True, body=None):
            """GET ``path``; with ``body``, POST it as JSON with the session's CSRF token."""
            headers = {"Authorization": "Bearer " + token} if auth and token else {}
            data = None
            if body is not None:
                if not csrf:
                    csrf.append(request("/api/session")[1]["csrf_token"])
                headers.update({"Content-Type": "application/json",
                                "X-Operator-Token": csrf[0]})
                data = json.dumps(body).encode("utf-8")
            conn = HTTPConnection("127.0.0.1", port, timeout=60)
            conn.request("GET" if body is None else "POST", path, body=data, headers=headers)
            response = conn.getresponse()
            body = json.loads(response.read())
            conn.close()
            return response.status, body
        csrf: list = []
        return request

    def register_catalog_intent(self, runtime, actor):
        owner = FixtureSystem()
        target = owner.seed("fixture.record", {"id": 1}, {"id": 1, "po_reference": None})

        def resolve(handle, reader):
            if handle.startswith("fixture://"):
                return owner.resolve(handle, reader)
            return runtime.resolve_citation(handle, reader)
        work = Path(tempfile.mkdtemp(dir=self.tmp))
        cases = CaseStore(work / "cases.db", citation_resolver=resolve)
        path = ActionPath(work / "actions.sqlite", cases)
        # The employee's console session names the action's subjects (decision g2-1): it
        # opens the case, records the proposal (through the action path) and registers it.
        session = session_identity.fixture_session(
            actor.tenant, actor.principals, channel=session_identity.CHANNEL_CONSOLE,
            clearance=actor.clearance, subject_email="employee@isolated-fixture.example")
        # OPS is a human principal: the objective is a person's words.
        case_id = path.open_case(actor, "Set the reference on isolated record 1 to REF-77?",
                                 objective_author_kind="human", session=session)
        proposal = path.record_proposal(
            actor, case_id, FIXTURE_CAPABILITY, {"field": "po_reference", "value": "REF-77"},
            [{"check": "field_empty", "citation": target, "field": "po_reference"},
             "a person confirms the reference"],
            citations=[target, self.po_handle], session=session,
            expected_effect={"relation": "fixture.record", "key": {"id": 1},
                             "fields": ["po_reference"], "equals": "REF-77"})
        self.registered = path.register(actor, case_id, proposal["proposal_id"],
                                        session=session)
        return cases, path

    def get(self, request, path):
        status, body = request(path)
        self.assertEqual(status, 200, body)
        return body

    def find_one(self, request, query, object_type):
        found = self.get(request, "/api/objects?q=" + quote(query, safe=""))["find"]
        self.assertIn(found["status"], ("found", "ambiguous"), found)
        ids = [c["object"]["object_id"] for c in found["candidates"]
               if c["object"]["object_type"] == object_type]
        self.assertTrue(ids, found)
        return ids[0]

    def open(self, request, object_id):
        return self.get(request, "/api/object?id=" + quote(object_id, safe=""))

    # ------------------------------------------------------------------ tests

    def test_find_then_open_joins_card_map_timeline_process_gaps_and_sources(self):
        request = self.serve(OPS)
        po = self.find_one(request, case_fixtures.PO_NUMBER, "PurchaseOrder")
        view = self.open(request, po)
        self.assertEqual(view["status"], "read")
        self.assertTrue(view["read_only"])
        self.assertEqual(view["business_system_writes"], 0)
        self.assertTrue(view["evidence"]["fixture"])
        self.assertTrue(view["sources_openable"])
        card = view["card"]
        self.assertEqual(card["object"]["object_id"], po)
        self.assertTrue(card["values"])
        for value in card["values"]:  # every value carries its L1 citation
            self.assertTrue(value["citation"]["l1_handle"].startswith("api://"), value)
            self.assertTrue(value["citation"]["field_path"])
            self.assertTrue(value["citation"]["digest"])
        hops = {(h["link_type"], h["direction"]) for h in view["around"]["hops"]}
        self.assertEqual(hops, {(item["link_type"], item["direction"])
                                for item in card["linked_counts"]
                                if item["count"] != 0 or item.get("tiers")})
        edges = [edge for hop in view["around"]["hops"] for edge in hop["result"]["edges"]]
        self.assertTrue(edges)
        for edge in edges:
            self.assertIn(po, (edge["from"], edge["to"]))
            self.assertTrue(edge["tier"])
        process = view["process"]
        self.assertEqual(process["status"], "read")
        self.assertTrue(process["result"]["stages"])
        # A staging root never yields a decided stage: every one is undecided with a gap.
        for stage in process["result"]["stages"]:
            if stage["state"] == "missing":
                self.assertIn("reason", stage["missing"])
        self.assertTrue(any(gap["from"].startswith("plx.process") for gap in view["gaps"]["items"]))
        self.assertEqual(view["gaps"]["total"], len(view["gaps"]["items"]))
        self.assertIn("events", view["timeline"])
        self.assertTrue(view["originals"]["items"])
        # Originals: every capture any answer cites (values, link reasons, events,
        # process, conflicts), each with its version, SHA-256 digests and its citers.
        cited = set()
        objects._handles({key: view[key] for key in ("card", "around", "timeline",
                                                     "process", "conflicts")}, cited)
        self.assertEqual({o["l1_handle"] for o in view["originals"]["items"]}, cited)
        self.assertEqual((view["originals"]["total"], view["originals"]["truncated"]),
                         (len(cited), False))
        self.assertTrue({v["citation"]["l1_handle"] for v in card["values"]} <= cited)
        for original in view["originals"]["items"]:
            self.assertEqual(original["source"], objects.SOURCE_SYSTEMS[original["scheme"]])
            self.assertEqual(original["record"], objects.record_of(original["l1_handle"]))
            self.assertEqual(original["version"], objects.version_of(original["l1_handle"]))
            self.assertTrue(original["version"])
            # A capture cited by a value, link, event or process carries its digest; one
            # named only as a display text's origin has none, and shows no invented one.
            if not all(by.startswith("display of") for by in original["cited_by"]):
                self.assertTrue(original["sha256"], original)
            for digest in original["sha256"]:
                self.assertRegex(digest, r"\A[0-9a-f]{64}\Z")
            self.assertTrue(original["cited_by"])
            self.assertTrue(original["openable"])  # api:// and a source reader
            self.assertNotIn("withheld", original)
        # Mail and documents: the case host's plx.documents for this reader; a listed
        # document opens only when the per-item gate admits the reader.
        self.assertEqual(view["documents"]["status"], "read")
        for document in view["documents"]["result"]["documents"]:
            self.assertEqual(document["openable"], "not_openable_reason" not in document)
        # The answer names the session reader, never a default identity.
        self.assertEqual(view["actor"], {"tenant": "plx", "principals": sorted(OPS)})

    def test_citation_opens_the_cited_source_row_for_the_reader(self):
        request = self.serve(OPS)
        po = self.find_one(request, case_fixtures.PO_NUMBER, "PurchaseOrder")
        value = self.open(request, po)["card"]["values"][0]
        handle = value["citation"]["l1_handle"]
        source = self.get(request, "/api/object-source?handle=" + quote(handle, safe=""))
        self.assertEqual(source["kind"], "row")
        self.assertEqual(source["citation"], handle)
        self.assertTrue(source["status"]["ok"])
        self.assertIn(value["citation"]["field_path"].split(".")[1], source["row"])
        status, body = request("/api/object-source?handle=" + quote("file:///etc/passwd", safe=""))
        self.assertEqual((status, body["error"]), (400, "invalid_request"))
        # A mail record the host's item gate does not admit reads as not found (the
        # same answer as a record that does not exist), never as its content.
        status, body = request("/api/object-source?handle=" + quote(
            "graph-mail://box@example.test/m1@ck", safe=""))
        self.assertEqual((status, body), (404, {"error": "not_found"}))
        status, _ = request("/api/object-source?handle=" + quote(handle + "0", safe=""))
        self.assertEqual(status, 404)

    def test_registered_intent_is_shown_on_the_object_whose_record_it_cites(self):
        request = self.serve(OPS, catalog_intent=True)
        po = self.find_one(request, case_fixtures.PO_NUMBER, "PurchaseOrder")
        view = self.open(request, po)
        intents = view["intents"]
        self.assertEqual(intents["status"], "read")
        self.assertEqual(intents["unmatchable"]["chain_broken"], 0)
        self.assertEqual(intents["unmatchable"]["withheld"], 0)
        self.assertEqual(len(intents["intents"]), 1, intents)
        [intent] = intents["intents"]
        self.assertEqual(intent["intent_id"], self.registered["intent_id"])
        self.assertEqual(intent["capability"], FIXTURE_CAPABILITY)
        self.assertEqual(intent["cited_records"], [self.po_handle])
        self.assertEqual(intent["state"], "proposed")
        self.assertFalse(intent["call_approved"])
        self.assertEqual(intent["dispatches"], [])
        self.assertTrue(intent["human_preconditions"])
        self.assertEqual(intent["checked_preconditions"], 1)
        self.assertEqual(intent["next_safe_step"]["step"], "prepare_gate")
        # Its approval state: nothing requested yet; decided in MC, never here.
        self.assertEqual(intent["approval"]["stage"], "not_requested")
        self.assertEqual(intent["approval"]["where"], "Mission Control")
        self.assertFalse(intent["approval"]["approved_here"])
        self.assertEqual(intent["decisions"], [])
        # The same case does not concern an object whose records it does not cite.
        other = self.open(request, self.supplier_id)
        self.assertEqual(other["intents"]["status"], "read")
        self.assertEqual(other["intents"]["intents"], [])
        self.assertEqual(other["intents"]["cases_read"], 1)
        # The seeded AO proposal's type is not in the action catalog: nothing registered.
        seeded = self.serve(OPS)
        found = self.get(seeded, "/api/objects?q=" + quote(case_fixtures.AO_CODES[0], safe=""))
        for candidate in found["find"]["candidates"]:
            self.assertEqual(self.open(seeded, candidate["object"]["object_id"])
                             ["intents"]["intents"], [])
        without = self.serve(OPS, actions=False)
        view = self.open(without, po)
        self.assertEqual(view["intents"], {"status": "not_configured"})

    def test_each_case_citing_the_object_shows_its_evaluation_current_stale_or_withheld(self):
        # A copy of the seeded state: this test appends an event to the evaluated case.
        self.state = Path(tempfile.mkdtemp(dir=self.tmp)) / "state"
        shutil.copytree(type(self).state, self.state)
        self.store = self.state / case_console.ONTOLOGY_FILE
        code = case_fixtures.AO_CODES[0]
        case_id = case_fixtures.marker(self.state)["cases"]["aos"][code]
        # The fixture's SharePoint readers too: the case cites an attached original.
        full = self.serve(OPS, reports=True, live=True)
        ao = self.find_one(full, code, "AssemblyOrder")
        panel = self.open(full, ao)["cases"]
        # Every seeded case is read (earlier tests of this class may have opened more).
        self.assertEqual((panel["status"], panel["cases_listing_full"]), ("read", False))
        self.assertGreaterEqual(panel["cases_read"], len(case_fixtures.AO_CODES) + 1)
        # Only the case citing this AO's record; its evaluation is the case workspace's.
        [case] = panel["cases"]
        self.assertEqual(case["case_id"], case_id)
        self.assertEqual([record.split("/")[2] for record in case["cited_records"]],
                         ["public.app_manufacturing_order"])
        self.assertEqual((case["evaluation_status"], case["evaluation"]["status"],
                          case["evaluation"]["freshness"]), ("current", "stored", "current"))
        runtime = CaseRuntime(self.state, client=None,
                              audience=self.state / case_fixtures.MIRROR_AUDIENCE_FILE,
                              tenant="plx", live=case_fixtures.live_readers(
                                  case_fixtures.build_world(),
                                  self.state / case_fixtures.LIVE_AUDIENCE_FILE))
        self.addCleanup(runtime.close)
        shown = CaseWorkspace(runtime, Actor("plx", OPS), report_store=(
            case_console.report_store_for(runtime, self.state / case_fixtures.REPORTS_FILE))
        ).case(case_id)
        self.assertEqual(case["evaluation"], shown["evaluation"])
        self.assertEqual(panel["evaluation_counts"]["current"], 1)
        self.assertEqual(sum(panel["evaluation_counts"].values()), 1)
        report_sha = case["evaluation"]["report_sha256"]
        # A case workspace built for another (wider) reader lists and evaluates nothing.
        wider = Actor("plx", frozenset({case_fixtures.OPERATIONS, case_fixtures.QUALITY}))
        crossed = case_console.object_workspace_for(runtime, self.store, Actor("plx", OPS),
                                                    state_dir=self.state)
        self.addCleanup(crossed.close)
        crossed.sources = CaseWorkspace(runtime, wider, report_store=(
            case_console.report_store_for(runtime, self.state / case_fixtures.REPORTS_FILE)))
        self.assertEqual(crossed.workspace(ao)["cases"], {"status": "not_configured"})
        # The same case member, granted every cited source but one (the one quality item
        # row the case cites): withheld, with nothing of the stored evaluation shown.
        audience = json.loads((self.state / case_fixtures.MIRROR_AUDIENCE_FILE)
                              .read_text("utf-8"))
        audience["audiences"]["silver.assembly_order_items"] = []
        one_less = self.state.parent / "one-source-less.json"
        one_less.write_text(json.dumps(audience), "utf-8")
        narrow = self.open(self.serve(OPS, audience=one_less, reports=True, live=True), ao)
        [case] = narrow["cases"]["cases"]
        self.assertEqual(case["case_id"], case_id)
        self.assertEqual(case["evaluation_status"], "withheld")
        self.assertEqual(set(case["evaluation"]), {"status", "reason"})
        self.assertEqual(narrow["cases"]["evaluation_counts"]["withheld"], 1)
        for leaked in (report_sha, "passed_all", "report_id", "silver."):
            self.assertNotIn(leaked, json.dumps(narrow["cases"]))
        # An outsider to the case sees no case at all.
        outsider = self.open(self.serve({LIMITED}, audience=self.limited_audience), ao)
        self.assertEqual(outsider["cases"]["cases"], [])
        # Any later case event: the stored report scored an earlier case.
        quality = Actor("plx", frozenset({case_fixtures.OPERATIONS, case_fixtures.QUALITY}))
        runtime.cases.gap(quality, case_id, "Signed AO countersignature", "not yet seen",
                          ["Quality"], "unknown")
        [case] = self.open(full, ao)["cases"]["cases"]
        self.assertEqual((case["evaluation_status"], case["evaluation"]["freshness"]),
                         ("stale", "stale"))
        # Without a report store: not configured; past the read bound: said, never silent.
        [case] = self.open(self.serve(OPS, live=True), ao)["cases"]["cases"]
        self.assertEqual(case["evaluation"], {"status": "not_configured"})
        with mock.patch.object(objects, "MAX_OBJECT_CASES", 1):
            capped = self.open(full, ao)["cases"]
        self.assertEqual((capped["cases_read"], capped["cases_listing_full"]), (1, True))

    def test_revoking_the_reader_refuses_find_card_and_source_on_the_next_request(self):
        audience_path = self.tmp / "revocable-audience.json"
        shutil.copyfile(self.state / case_fixtures.MIRROR_AUDIENCE_FILE, audience_path)
        request = self.serve(OPS, audience=audience_path)
        po = self.find_one(request, case_fixtures.PO_NUMBER, "PurchaseOrder")
        handle = self.open(request, po)["card"]["values"][0]["citation"]["l1_handle"]
        self.get(request, "/api/object-source?handle=" + quote(handle, safe=""))
        audience = json.loads(audience_path.read_text("utf-8"))
        audience["audiences"] = {relation: [p for p in principals if p not in OPS]
                                 for relation, principals in audience["audiences"].items()}
        audience_path.write_text(json.dumps(audience), "utf-8")
        found = self.get(request, "/api/objects?q=" + quote(case_fixtures.PO_NUMBER, safe=""))
        self.assertEqual(found["find"]["status"], "not_found")
        self.assertEqual(found["find"]["candidates"], [])
        status, body = request("/api/object?id=" + quote(po, safe=""))
        self.assertEqual((status, body), (404, {"error": "not_found"}))
        status, _ = request("/api/object-source?handle=" + quote(handle, safe=""))
        self.assertEqual(status, 404)
        # Granting again is honoured on the next request too.
        shutil.copyfile(self.state / case_fixtures.MIRROR_AUDIENCE_FILE, audience_path)
        self.assertEqual(self.open(request, po)["object_id"], po)

    def test_store_status_is_shown_and_a_foreign_store_is_refused(self):
        request = self.serve(OPS)
        po = self.find_one(request, case_fixtures.PO_NUMBER, "PurchaseOrder")
        view = self.open(request, po)
        self.assertEqual(view["store"]["state"], "current", view["store"])
        self.assertTrue(view["store"]["built_at"])
        self.assertEqual(view["store"]["mirror_generation"],
                         view["store"]["mirror_generation_now"])
        # No BC reader, mail, SharePoint, Teams, Mission Control or knowledge capture is
        # configured on the fixture console: the reader's policy grants none of those
        # families, and the answer says so.
        self.assertEqual([item["source"] for item in view["withheld_sources"]],
                         ["bc", "graph_mail", "sharepoint", "teams", "mission_control",
                          "brain"])
        self.assertTrue(all(item["reason"] == objects.NOT_GRANTED
                            for item in view["withheld_sources"]))

        def altered(name, key, change):
            copy = self.tmp / name
            shutil.copyfile(self.store, copy)
            with sqlite3.connect(copy) as conn:
                value = json.loads(conn.execute("SELECT value FROM store_meta WHERE key=?",
                                                (key,)).fetchone()[0])
                change(value)
                conn.execute("UPDATE store_meta SET value=? WHERE key=?",
                             (json.dumps(value), key))
            conn.close()
            return copy

        def older(value):
            relation = sorted(value["portal_api"])[0]
            value["portal_api"][relation]["last_capture_id"] = -1

        stale = objects.StoreStatus(altered("stale.sqlite", "source_freshness", older),
                                    self.state).read()
        self.assertEqual(stale["state"], "stale")
        self.assertEqual(len(stale["newer_relations"]), 1)

        def other(value):
            value["portal_api"]["generation"] = "0" * 16
        foreign = altered("foreign.sqlite", "l1_generations", other)
        self.assertEqual(objects.StoreStatus(foreign, self.state).read()["state"], "foreign")
        runtime = CaseRuntime(self.state, client=None,
                              audience=self.state / case_fixtures.MIRROR_AUDIENCE_FILE,
                              tenant="plx")
        try:
            with self.assertRaises(ValueError):
                case_console.object_workspace_for(runtime, foreign, Actor("plx", OPS),
                                                  state_dir=self.state)
        finally:
            runtime.close()

    def test_narrower_reader_sees_a_subset_and_honest_withheld_markers(self):
        full = self.serve(OPS)
        limited = self.serve({LIMITED}, audience=self.limited_audience)
        po_full = self.find_one(full, case_fixtures.PO_NUMBER, "PurchaseOrder")
        po_limited = self.find_one(limited, case_fixtures.PO_NUMBER, "PurchaseOrder")
        self.assertEqual(po_full, po_limited)
        wide = self.open(full, po_full)
        narrow = self.open(limited, po_limited)
        seen_wide = _ids_and_handles(wide, set())
        seen_narrow = _ids_and_handles(narrow, set())
        hidden = {item for item in seen_wide if HIDDEN_RELATION in item
                  or item.startswith("PurchaseOrderLine:")}
        self.assertTrue(hidden, "the full reader sees the PO lines")
        self.assertFalse(seen_narrow & hidden, sorted(seen_narrow & hidden)[:3])
        self.assertTrue(seen_narrow <= seen_wide, sorted(seen_narrow - seen_wide)[:3])
        # Monotone process: a decided stage or comparison for the narrow reader is the
        # full reader's decision, never a different one.
        wide_stages = {s["stage"]: s["state"] for s in wide["process"]["result"]["stages"]}
        for stage in narrow["process"]["result"]["stages"]:
            if stage["state"] != "missing" or stage["missing"]["state"] != "unknown":
                self.assertEqual(stage["state"], wide_stages[stage["stage"]])
        for check in narrow["process"]["result"].get("quantity_checks") or []:
            self.assertIsNone(check["agree"])
        # The PO lines' source row is not readable for the narrow reader either.
        line = next(item for item in sorted(hidden) if item.startswith("PurchaseOrderLine:"))
        line_handle = self.open(full, line)["card"]["values"][0]["citation"]["l1_handle"]
        self.assertTrue(line_handle.startswith("api://" + HIDDEN_RELATION))
        status, _ = limited("/api/object?id=" + quote(line, safe=""))
        self.assertEqual(status, 404)
        status, _ = limited("/api/object-source?handle=" + quote(line_handle, safe=""))
        self.assertEqual(status, 404)
        # The withheld state is stated, never silently dropped.
        self.assertIn("withheld", narrow["card"])

    def test_outsider_finds_nothing_and_an_unreadable_object_is_not_found(self):
        insider = self.serve(OPS)
        po = self.find_one(insider, case_fixtures.PO_NUMBER, "PurchaseOrder")
        outsider = self.serve(OUTSIDER)
        found = self.get(outsider, "/api/objects?q=" + quote(case_fixtures.PO_NUMBER, safe=""))
        self.assertEqual(found["find"]["status"], "not_found")
        self.assertEqual(found["find"]["candidates"], [])
        status, body = outsider("/api/object?id=" + quote(po, safe=""))
        self.assertEqual((status, body), (404, {"error": "not_found"}))
        status, body = insider("/api/object?id=" + quote(po + "x", safe=""))
        self.assertEqual((status, body), (404, {"error": "not_found"}))

    def test_an_unparsed_identifier_is_a_typed_gap_on_its_card_and_on_its_find(self):
        # Each record whose own identifier the projection could not parse (the build
        # report's parse_identifier_unparsed samples) shows why on its card, and a find
        # by the raw value the employee reads in its source answers a typed gap.
        from ontology.schema import load_schema
        with sqlite3.connect(self.store) as conn:
            report = json.loads(conn.execute("SELECT value FROM store_meta WHERE key = "
                                             "'build_report'").fetchone()[0])
            roots = [(gap["where"], conn.execute(
                "SELECT object_id FROM object_versions WHERE l1_handle = ?",
                (handle,)).fetchone()[0], handle) for gap in report["gaps"]
                if gap["code"] == "parse_identifier_unparsed" for handle in gap["samples"]]
        conn.close()
        self.assertTrue(roots, report["gaps"])
        types = load_schema().object_types
        full, outsider = self.serve(OPS), self.serve(OUTSIDER)
        for where, object_id, handle in roots:
            carrier, _, binding = where.partition("@")
            object_type, _, name = carrier.partition(".")
            card = self.open(full, object_id)["card"]
            self.assertNotIn(name, {v["property"] for v in card["values"]})
            gaps = [g for g in card["gaps"] if g["subject"] == carrier]
            self.assertEqual([g["reason"] for g in gaps], ["value_unparsed"], card["gaps"])
            self.assertEqual(gaps[0]["state"], "unknown")
            self.assertIn("identifier_unparsed", gaps[0]["detail"])
            path = next(source["path"] for prop in types[object_type]["properties"]
                        if prop["name"] == name for source in prop["sources"]
                        if source["binding"] == binding)
            source = self.get(full, "/api/object-source?handle=" + quote(handle, safe=""))
            raw = source["row"][path]
            found = self.get(full, "/api/objects?q=" + quote(raw, safe=""))["find"]
            self.assertNotIn(object_id, [c["object"]["object_id"] for c in found["candidates"]])
            typed = [g for g in found["gaps"] if g["reason"] == "identifier_unparsed"]
            self.assertEqual(len(typed), 1, found["gaps"])
            self.assertEqual(typed[0]["state"], "unknown")
            self.assertIn(carrier, typed[0]["detail"])
            # A reader with no grant learns nothing about what failed to parse.
            denied = self.get(outsider, "/api/objects?q=" + quote(raw, safe=""))["find"]
            self.assertEqual(denied["candidates"], [])
            self.assertNotIn("identifier_unparsed", json.dumps(denied))

    def test_a_live_lookup_that_finds_nothing_keeps_the_unparsed_identifier_gap(self):
        # A find the projection answers not_found falls back to the live readers; when
        # they read records (a throw-away store) and still find no exact match, the
        # served projection's unparsed identifiers are still why a value goes unfound.
        from ontology import resolve
        with sqlite3.connect(self.store) as conn:
            queries = sorted({raw for (raw,) in conn.execute(
                "SELECT DISTINCT raw FROM identifiers")})
        conn.close()
        built: list = []
        discard = resolve.Resolution.discard

        def recorded(resolution):
            built.append(resolution.store_path is not None)
            return discard(resolution)
        full, outsider = self.serve(OPS), self.serve(OUTSIDER)
        empty = 0
        with mock.patch.object(resolve.Resolution, "discard", recorded):
            for query in queries:
                built.clear()
                found = self.get(full, "/api/objects?q=" + quote(query, safe=""))["find"]
                if found["status"] != "not_found" or not any(built):
                    continue
                empty += 1
                typed = [g for g in found["gaps"] if g["reason"] == "identifier_unparsed"]
                self.assertEqual([g["state"] for g in typed], ["unknown"], (query, found))
                # The reader with no grant learns nothing about what failed to parse.
                denied = self.get(outsider, "/api/objects?q=" + quote(query, safe=""))["find"]
                self.assertEqual(denied["candidates"], [])
                self.assertNotIn("identifier_unparsed", json.dumps(denied))
        self.assertGreater(empty, 0, "no live lookup built a store and found nothing")

    def test_an_unparsed_identifier_carrier_needs_the_propertys_markings(self):
        # A reader granted the type but not the property's markings learns nothing of what
        # failed to parse there; the same reader holding them is told.
        from operating import ontology_host
        with sqlite3.connect(self.store) as conn:
            report = json.loads(conn.execute("SELECT value FROM store_meta WHERE key = "
                                             "'build_report'").fetchone()[0])
        conn.close()
        carriers = {gap["where"].partition("@")[0] for gap in report["gaps"]
                    if gap["code"] == "parse_identifier_unparsed"}
        self.assertTrue(carriers, report["gaps"])
        schema = ontology_host.default_schema()
        types = {name: dict(spec) for name, spec in schema.object_types.items()}
        for carrier in carriers:
            object_type, _, name = carrier.partition(".")
            types[object_type]["properties"] = [
                dict(prop, markings=[*prop.get("markings", ()), "pii"])
                if prop.get("name") == name else prop
                for prop in types[object_type]["properties"]]
        marked = dataclasses.replace(schema, object_types=types)
        runtime = CaseRuntime(self.state, client=None,
                              audience=self.state / case_fixtures.MIRROR_AUDIENCE_FILE,
                              tenant="plx")
        self.addCleanup(runtime.close)
        with mock.patch.object(ontology_host, "default_schema", lambda: marked):
            for markings in ((), ("pii",)):
                view = case_console.object_workspace_for(
                    runtime, self.store, Actor("plx", OPS), state_dir=self.state,
                    markings=markings)
                try:
                    found = view.find("ZZ-NO-SUCH-VALUE-0000")["find"]
                finally:
                    view.close()
                typed = [g for g in found["gaps"] if g["reason"] == "identifier_unparsed"]
                if markings:
                    self.assertEqual(len(typed), 1, found["gaps"])
                    for carrier in carriers:
                        self.assertIn(carrier, typed[0]["detail"])
                else:
                    self.assertNotIn("identifier_unparsed", json.dumps(found))

    def test_routes_need_the_token_and_validate_their_queries(self):
        request = self.serve(OPS)
        status, body = request("/api/objects?q=x", auth=False)
        self.assertEqual((status, body["error"]), (401, "access_token_required"))
        self.assertEqual(request("/api/objects")[1]["available"], True)
        for path in ("/api/objects?q=" + "x" * 201, "/api/objects?q=a&q=b",
                     "/api/objects?q=a&other=1", "/api/object", "/api/object?id=a&x=1",
                     "/api/object-source", "/api/object-source?handle=x&text_offset=-1"):
            status, body = request(path)
            self.assertEqual(status, 400, (path, body))
        unconfigured = self.serve(OPS, configured=False)
        self.assertEqual(unconfigured("/api/objects?q=x"), (200, {"available": False}))
        status, body = unconfigured("/api/object?id=x")
        self.assertEqual((status, body), (501, {"error": "capability_unavailable"}))

    def test_console_flags_build_and_serve_the_projection(self):
        """--seed-fixtures projects the fixture mirror and serves the object workspace."""
        from unittest import mock

        state = self.tmp / "console"
        served = {}

        def fake_serve(**kwargs):
            served.update(kwargs)
        with mock.patch.object(case_console, "serve", fake_serve):
            self.assertEqual(case_console.main([
                "--state-dir", str(state), "--fixture", "--seed-fixtures",
                "--principal", case_fixtures.OPERATIONS], fixtures=case_fixtures), 0)
        self.assertTrue((state / case_console.ONTOLOGY_FILE).is_file())
        view = served["object_workspace"]
        self.assertIsInstance(view, objects.ObjectWorkspace)
        self.assertTrue(view.evidence["fixture"])
        self.assertIsNotNone(served["case_workspace"])
        self.assertEqual(view.actor, served["session"].actor)
        self.assertTrue(served["session"].fixture)

    def test_object_routes_follow_the_session_identity(self):
        """Live: each authenticated request's own object workspace; fixture: the
        fixture session's, behind its token; nothing else is accepted."""
        runtime = CaseRuntime(self.state, client=None,
                              audience=self.state / case_fixtures.MIRROR_AUDIENCE_FILE,
                              tenant="plx")
        self.addCleanup(runtime.close)
        session = session_identity.fixture_session("plx", OPS,
                                                   channel=session_identity.CHANNEL_CONSOLE)
        view = case_console.object_workspace_for(runtime, self.store, session.actor,
                                                 state_dir=self.state)
        cases = CaseWorkspace(runtime, session.actor)
        other = CaseWorkspace(runtime, Actor("plx", OUTSIDER))
        class Tickets:
            """Two employees: one with an object workspace, one without."""
            def authenticate(self, ticket):
                if ticket not in ("t-a", "t-b"):
                    raise PermissionError("session_refused")
                return ticket, cases

            def object_workspace(self, who):
                return view if who == "t-a" else None
        for kwargs, code in (
                ({"object_workspace": view}, "console_object_workspace_requires_session_identity"),
                ({"case_workspace": other, "session": session_identity.fixture_session(
                    "plx", OUTSIDER, channel=session_identity.CHANNEL_CONSOLE),
                  "object_workspace": view}, "console_workspace_actor_must_be_the_session_identity"),
                ({"ticket_sessions": Tickets(), "object_workspace": view},
                 "console_session_source_ambiguous")):
            with self.subTest(code=code), self.assertRaises(ValueError) as caught:
                make_server(**kwargs)
            self.assertEqual(str(caught.exception), code)

        server = make_server(ticket_sessions=Tickets())
        self.assertIsNone(server.operator_access_token)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                                  daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), thread.join(timeout=10), server.server_close()))

        def get(path, ticket=None):
            conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=60)
            conn.request("GET", path, headers={"Authorization": "Bearer " + ticket}
                         if ticket else {})
            response = conn.getresponse()
            body = json.loads(response.read())
            conn.close()
            return response.status, body
        query = "/api/objects?q=" + quote(case_fixtures.PO_NUMBER, safe="")
        self.assertEqual(get(query)[0], 401)
        self.assertEqual(get(query, "t-x")[0], 401)
        status, body = get(query, "t-a")
        self.assertEqual(status, 200)
        self.assertEqual(body["find"]["status"], "found")
        self.assertEqual(get(query, "t-b"), (200, {"available": False}))
        self.assertEqual(get("/api/object?id=x", "t-b"),
                         (501, {"error": "capability_unavailable"}))


    def test_one_answer_decides_the_readers_grants_once(self):
        """The case host decides the reader's policy once per request: every tool call,
        item decision and read of one answer uses that grant (none mixes grants from
        before and after a change), and the next request decides again."""
        from operating.case_tools_host import CaptureCaseHostTools

        runtime = CaseRuntime(self.state, client=None,
                              audience=self.state / case_fixtures.MIRROR_AUDIENCE_FILE,
                              tenant="plx")
        self.addCleanup(runtime.close)
        view = case_console.object_workspace_for(runtime, self.store, Actor("plx", OPS),
                                                 state_dir=self.state)
        self.addCleanup(view.close)
        decided = []
        original = CaptureCaseHostTools.policy_now

        def counting(host, reader):
            decided.append(reader)
            return original(host, reader)
        with mock.patch.object(CaptureCaseHostTools, "policy_now", counting):
            found = view.find(case_fixtures.PO_NUMBER)
            self.assertEqual(len(decided), 1)
            po = next(c["object"]["object_id"] for c in found["find"]["candidates"]
                      if c["object"]["object_type"] == "PurchaseOrder")
            opened = view.workspace(po)
            self.assertEqual(len(decided), 2)
        self.assertEqual(opened["status"], "read")
        self.assertGreater(len(opened["around"]["hops"]) + len(opened["timeline"]["calls"]), 2)

    def test_each_answer_reads_one_generation_and_the_next_reads_the_newest(self):
        """The host renames a new projection generation over the store path: an answer
        in flight stays wholly on the generation it began on (status, digest and
        freshness), and the cached workspace's next answer reads the new one."""
        from operating.case_tools_host import CaptureCaseHostTools

        served = Path(tempfile.mkdtemp(dir=self.tmp)) / "ontology.sqlite"
        shutil.copyfile(self.store, served)
        newer = served.with_name("next.sqlite")
        shutil.copyfile(self.store, newer)
        with sqlite3.connect(newer) as conn:
            meta = dict(conn.execute("SELECT key, value FROM store_meta"))
            projected = json.loads(meta["source_freshness"])
            projected["portal_api"][sorted(projected["portal_api"])[0]]["last_capture_id"] = -1
            freshness = json.loads(meta["freshness"])
            for entry in freshness:
                entry["stale"] = True
            for key, value in (("source_freshness", json.dumps(projected)),
                               ("freshness", json.dumps(freshness)),
                               ("content_digest", "f" * 64)):
                conn.execute("UPDATE store_meta SET value=? WHERE key=?", (value, key))
        conn.close()
        runtime = CaseRuntime(self.state, client=None,
                              audience=self.state / case_fixtures.MIRROR_AUDIENCE_FILE,
                              tenant="plx")
        self.addCleanup(runtime.close)
        view = case_console.object_workspace_for(runtime, served, Actor("plx", OPS),
                                                 state_dir=self.state)
        self.addCleanup(view.close)

        def generation(answer):
            fresh = [f for f in answer["find"]["meta"]["freshness"]
                     if f["source"] == "portal_api"]
            return (answer["store"]["state"], answer["store"]["content_digest"],
                    [f["stale"] for f in fresh])

        first = generation(view.find(case_fixtures.PO_NUMBER))
        self.assertEqual(first[0], "current")
        self.assertEqual(first[2], [False])
        original = CaptureCaseHostTools.policy_now

        def swap_in_flight(host, reader):   # the host publishes mid-answer
            if newer.exists():
                newer.replace(served)
            return original(host, reader)
        with mock.patch.object(CaptureCaseHostTools, "policy_now", swap_in_flight):
            in_flight = view.find(case_fixtures.PO_NUMBER)
        self.assertEqual(generation(in_flight), first)
        self.assertTrue(in_flight["find"]["candidates"])
        self.assertEqual(generation(view.find(case_fixtures.PO_NUMBER)),
                         ("stale", "f" * 64, [True]))

    def test_a_question_family_runs_from_a_found_object_for_the_reader(self):
        request = self.serve(OPS)
        question = f"Is {case_fixtures.PO_NUMBER} fully received and invoiced?"
        found = self.get(request, "/api/objects?q=" + quote(question, safe=""))
        po = next(c["object"]["object_id"] for c in found["find"]["candidates"]
                  if c["object"]["object_type"] == "PurchaseOrder")
        self.assertIn("three_way_match",
                      {plan["family"] for plan in found["plans"]["PurchaseOrder"]})
        path = "/api/investigate?family=three_way_match&root=" + quote(po, safe="")
        answer = self.get(request, path)
        self.assertTrue(answer["read_only"])
        result = answer["investigation"]
        self.assertTrue(result["procedure"].startswith("procedure:three_way_match@"))
        self.assertEqual(result["root"]["object_id"], po)
        cited = [entry for entry in result["answer"] if entry.get("citation")]
        self.assertTrue(cited)
        self.assertTrue(all(entry["citation"]["l1_handle"].startswith("api://")
                            for entry in cited))
        counts = investigate_counts(answer)
        self.assertEqual((counts["claims"], counts["citations"]),
                         (len(result["answer"]), len(cited)))
        self.assertEqual(counts["checks_passed"], sum(e.get("check") == "passed"
                                                      for e in result["answer"]))
        # A reader who may read none of it gets no value and no record of the chain.
        blocked = self.get(self.serve(OUTSIDER), path)["investigation"]
        self.assertEqual(blocked["answerability"]["state"], "blocked")
        self.assertEqual((blocked["chain"]["objects"], blocked["values"]), ([], []))
        for bad in ("/api/investigate?family=three_way_match",
                    "/api/investigate?family=not%20a%20family&root=x",
                    "/api/investigate?family=chain&root=x&extra=1"):
            status, body = request(bad)
            self.assertEqual(status, 400, (bad, body))

    def test_ask_cites_only_resolved_evidence_and_shows_the_loop_claim_gate_verdict(self):
        invented = self.po_handle.rsplit("@", 1)[0] + "@g0-999999"
        asked = []

        def planner(request):
            # A model's moves: given the card of the object the question names (its
            # transcript's step 0), answer by reference to a cell that card returned: once
            # with the value its record holds, once with a number it does not hold, once
            # stating no value, once citing an invented handle.
            asked.append(request)
            card = request["transcript"][0]["observations"][0]["result"]
            handle = card["values"][0]["citation"]["l1_handle"]
            cell = {"citation": handle, "field": "row.po_number"}
            return {"action": "answer", "unknowns": ["the confirmed ship date"],
                    "text": "Its number is {po}. It orders {units}.", "claims": [
                        {"id": "po", "value": "PO#5447", **cell},
                        {"id": "units", "value": "12500", **cell},
                        {"text": "The purchase order is on record in the Portal.",
                         "citations": [handle]},
                        {"id": "mail", "value": "PO#5447", "citation": invented,
                         "field": "row.po_number"}]}, None

        request = self.serve(OPS, planner=planner)
        po = self.find_one(request, case_fixtures.PO_NUMBER, "PurchaseOrder")
        view = self.open(request, po)
        self.assertEqual(view["ask"], {"served": True})
        status, body = request("/api/object-ask", body={"object_id": po,
                                                        "question": "Is it confirmed?"})
        self.assertEqual(status, 200, body)
        # Rooted at the object in view, for the session's reader, writing nothing.
        self.assertTrue(asked[0]["question"].startswith("Is it confirmed?"))
        self.assertTrue(asked[0]["question"].endswith(po))
        self.assertEqual((body["actor"]["principals"], body["business_system_writes"]),
                         (sorted(OPS), 0))
        answer = body["answer"]
        self.assertEqual((answer["outcome"], answer["unknowns"]),
                         ("answered", ["the confirmed ship date"]))
        # The planner starts from the card in view, made for it as its step 0 (a
        # PurchaseOrder has several plans and the Ask named none: no plan runs).
        self.assertEqual((asked[0]["transcript"][0]["step"], asked[0]["transcript"][0]["seeded"],
                          [o["tool"] for o in asked[0]["transcript"][0]["observations"]]),
                         (0, True, ["plx.object"]))
        self.assertEqual(answer["tool_calls"], [{"step": 0, "tool": "plx.object",
                                                 "is_error": False}])
        # Each kept claim cites a record the card returned, typed as a handle, that this
        # reader opens, and passed the loop's claim gate (the one gate: its verdict here).
        card_handles = {v["citation"]["l1_handle"] for v in view["card"]["values"]}
        texts = [(c["text"], c["check"]["status"]) for c in answer["claims"]]
        self.assertEqual(texts, [("PO number: PO#5447", "passed")])
        # The text is code's: the prose's sentence naming the checked claim, its value.
        self.assertEqual(answer["text"], "Its number is PO#5447.")
        self.assertFalse(answer["claim_checks"]["passed"])
        self.assertEqual(answer["claim_checks"]["counts"]["passed"], 1)
        # The session journal's counts of this answer (no text, no handle).
        self.assertEqual(ask_counts(body), {"claims": 1, "citations": 1, "checks_passed": 1,
                                            "checks_failed": 1})
        for claim in answer["claims"]:
            for cite in claim["citations"]:
                self.assertIn(cite["l1_handle"], card_handles)
                self.assertTrue(cite["openable"])
                self.assertEqual(self.get(request, "/api/object-source?handle="
                                          + quote(cite["l1_handle"], safe=""))["citation"],
                                 cite["l1_handle"])
        # The number the record does not hold, the claim stating no value and the invented
        # citation: each claim is shown as unsupported with its typed reason, the handle
        # never.
        self.assertEqual(answer["unsupported"], [
            {"text": "The purchase order is on record in the Portal.",
             "reason": "claim_states_no_value"},
            {"text": "mail: PO#5447", "reason": "cites_what_this_run_did_not_return"},
            {"text": "units: 12500", "reason": "claim_check_failed",
             "failures": [{"code": "claim_value_mismatch", "value": "12500",
                           "detail": "the cited row.po_number holds another value"}]}])
        self.assertEqual(answer["dropped_claims"], 3)
        self.assertNotIn(invented, json.dumps(body))
        # Named a case, the Ask keeps its session there; the case route lists it and the
        # replay route runs it again for the session's reader: nothing changed, identical.
        actor = session_identity.fixture_session(
            "plx", OPS, channel=session_identity.CHANNEL_CONSOLE).actor
        runtime = CaseRuntime(self.state, client=None, tenant="plx",
                              audience=self.state / case_fixtures.MIRROR_AUDIENCE_FILE)
        self.addCleanup(runtime.close)
        case_id = runtime.cases.open(actor, "Is the purchase order confirmed?")
        status, body = request("/api/object-ask", body={"object_id": po, "case_id": case_id,
                                                        "question": "Is it confirmed?"})
        self.assertEqual((status, body["session"]),
                         (200, {"case_id": case_id, "recorded": True, "session_id": "S1"}))
        (kept,) = self.get(request, "/api/object-case?case_id=" + case_id)["sessions"]
        self.assertEqual((kept["session_id"], kept["outcome"], kept["call_count"]),
                         ("S1", "answered", 1))
        status, body = request("/api/object-replay", body={"case_id": case_id,
                                                           "session_id": "S1"})
        self.assertEqual((status, body["replay"]["replayed"], body["replay"]["identical"],
                          body["replay"]["divergences"]), (200, True, True, []))
        self.assertEqual(request("/api/object-replay", body={"case_id": case_id})[0], 400)
        self.assertEqual(self.serve(OUTSIDER)("/api/object-replay", body={
            "case_id": case_id, "session_id": "S1"}), (404, {"error": "not_found"}))
        # A named question family starts the planner from its plan's output too; one
        # that is no plan for this object's type refuses before any planner turn.
        asked.clear()
        status, body = request("/api/object-ask", body={"object_id": po, "question": "?",
                                                        "family": "three_way_match"})
        self.assertEqual(status, 200, body)
        seeded = asked[0]["transcript"][0]["observations"]
        self.assertEqual([(o["tool"], o["arguments"]) for o in seeded],
                         [("plx.object", {"object_id": po}),
                          ("plx.investigate", {"family": "three_way_match", "root": po})])
        self.assertIn("answerability", seeded[1]["result"].get("shown", seeded[1]["result"]))
        self.assertEqual([c["tool"] for c in body["answer"]["tool_calls"]],
                         ["plx.object", "plx.investigate"])
        asked.clear()
        for family in ("order_margin", "no such family", 7, ""):
            self.assertEqual(request("/api/object-ask", body={
                "object_id": po, "question": "?", "family": family}),
                (400, {"error": "invalid_request", "detail": "ask_family_invalid"}), family)
        self.assertEqual(asked, [])
        # Unknown fields, the CSRF token, an unreadable root and an unserved Ask refuse.
        self.assertEqual(request("/api/object-ask", body={"object_id": po, "question": "?",
                                                          "principal": "x"})[0], 400)
        self.assertEqual(request("/api/object-ask", body={"object_id": po + "x",
                                                          "question": "?"})[0], 404)
        unserved = self.serve(OPS)
        self.assertEqual(self.open(unserved, po)["ask"], {"served": False})
        self.assertEqual(unserved("/api/object-ask", body={"object_id": po, "question": "?"}),
                         (501, {"error": "ask_not_served"}))

    def test_ask_for_a_narrower_reader_holds_nothing_of_the_records_withheld_from_them(self):
        full = self.serve(OPS)
        po = self.find_one(full, case_fixtures.PO_NUMBER, "PurchaseOrder")
        full_view = self.open(full, po)
        hop = next(h for h in full_view["around"]["hops"] if any(
            o["object_id"].startswith("PurchaseOrderLine:") for o in h["result"]["objects"]))
        hidden = {item for item in _ids_and_handles(full_view, set())
                  if HIDDEN_RELATION in item or item.startswith("PurchaseOrderLine:")}
        line_card = self.open(full, sorted(h for h in hidden
                                           if h.startswith("PurchaseOrderLine:"))[0])["card"]
        hidden |= _ids_and_handles(line_card, set())
        line = line_card["values"][0]["citation"]["l1_handle"]
        self.assertTrue(line.startswith("api://" + HIDDEN_RELATION))
        observed, stated, shown = [], [], []

        def planner(request):
            # It "remembers" a PO line handle from another reader's session, reads it and
            # the lines, then states what the first (full) reader's session read of the
            # line, citing the line and the PO record (step 0: the card made for it).
            if len(request["transcript"]) < 2:
                return {"action": "call", "calls": [
                    {"tool": "plx.read", "arguments": {"citation": line}},
                    {"tool": "plx.around", "arguments": {"from": po, "path": [
                        {"link_type": hop["link_type"], "direction": hop["direction"]}]}}]}, None
            card, read, around = (o["result"] for step in request["transcript"]
                                  for o in step["observations"])
            observed.append(read.get("status"))
            stated.append(_leaves([read.get("fields"), read.get("content"),
                                   around.get("objects")], []))
            said = "; ".join(stated[0])
            value = card["values"][0]
            plain = value["value"]
            plain = plain["untrusted_text"] if isinstance(plain, dict) else plain
            shown.append((value["citation"]["field_path"], plain))
            return {"action": "answer", "text": "The PO is on record as {po}.", "claims": [
                {"id": "po", "value": shown[-1][1],
                 "citation": value["citation"]["l1_handle"], "field": shown[-1][0]},
                {"text": "Its line reads " + said[:600], "citations": [line]}]}, None

        answers = {}
        for name, principals, audience in (("full", OPS, None),
                                           ("narrow", {LIMITED}, self.limited_audience)):
            status, body = self.serve(principals, audience=audience, planner=planner)(
                "/api/object-ask", body={"object_id": po, "question": "What does it order?"})
            self.assertEqual(status, 200, body)
            answers[name] = body["answer"]
        # The full reader read the line resolved: its claim cites evidence and is shown
        # (unsupported only by the claim gate: it states no value as a claim), so the
        # probe is not empty.
        self.assertEqual(observed[0], "resolved")
        full_line = [c for c in answers["full"]["claims"] + answers["full"]["unsupported"]
                     if (c["text"] or "").startswith("Its line reads")]
        self.assertEqual([c.get("reason", "kept") for c in full_line], ["claim_states_no_value"])
        # What the full reader's claim states that the narrower reader may not read.
        narrow_view = json.dumps(self.open(self.serve({LIMITED}, audience=self.limited_audience),
                                           po))
        line_values = {value for value in stated[0] if len(value) >= 4
                       and value in full_line[0]["text"] and value not in narrow_view}
        self.assertTrue(line_values)
        # The narrower reader's read was not resolved, so nothing of the line is evidence
        # and no withheld id, handle or value appears in their answer.
        self.assertNotEqual(observed[1], "resolved")
        narrow = answers["narrow"]
        field, value = shown[1]
        self.assertEqual([c["text"] for c in narrow["claims"]],
                         [f"{field.removeprefix('row.')}: {value}"])   # a label, no path
        self.assertEqual(narrow["text"], f"The PO is on record as {value}.")
        self.assertEqual([u["reason"] for u in narrow["unsupported"]],
                         ["cites_what_this_run_did_not_return"])
        # Its unsupported claim cites a record withheld from this reader: its text, which
        # holds what the planner remembers of the line, is not shown.
        self.assertEqual((narrow["unsupported"][0]["text"],
                          narrow["unsupported"][0]["text_withheld"]),
                         (None, "cites_a_record_withheld_from_the_reader"))
        text = json.dumps(narrow)
        leaked = sorted(item for item in hidden | line_values if item in text)
        self.assertFalse(leaked, leaked[:3])
        # An outsider who cannot read the PO gets no answer at all (no oracle).
        status, body = self.serve(OUTSIDER, planner=planner)(
            "/api/object-ask", body={"object_id": po, "question": "What does it order?"})
        self.assertEqual((status, body), (404, {"error": "not_found"}))

    def test_a_restricted_reader_differs_from_a_full_reader_only_by_withheld_markers(self):
        full = self.serve(OPS, live=True)
        limited = self.serve({case_fixtures.OPERATIONS}, live=True)
        ao = self.find_one(full, case_fixtures.AO_CODES[0], "AssemblyOrder")
        self.assertEqual(ao, self.find_one(limited, case_fixtures.AO_CODES[0], "AssemblyOrder"))
        wide, narrow = self.open(full, ao), self.open(limited, ao)
        self.assertEqual(set(wide), set(narrow))
        # Nothing the restricted reader sees is hidden from the full reader.
        seen_wide, seen_narrow = _ids_and_handles(wide, set()), _ids_and_handles(narrow, set())
        self.assertTrue(seen_narrow <= seen_wide, sorted(seen_narrow - seen_wide)[:3])
        # Every value the restricted reader sees is the full reader's value; fewer
        # values come with the card's withheld marker.
        wide_values = {(v["property"], v["citation"]["l1_handle"]): v["value"]
                       for v in wide["card"]["values"]}
        for value in narrow["card"]["values"]:
            self.assertEqual(value["value"],
                             wide_values[(value["property"], value["citation"]["l1_handle"])])
        if len(narrow["card"]["values"]) < len(wide["card"]["values"]):
            self.assertIn("withheld", narrow["card"])
        # A capture both list is the same capture; one the restricted reader cannot
        # open says why.
        wide_originals = {o["l1_handle"]: o for o in wide["originals"]["items"]}
        for item in narrow["originals"]["items"]:
            other = wide_originals[item["l1_handle"]]
            self.assertEqual((item["record"], item["version"], item["sha256"]),
                             (other["record"], other["version"], other["sha256"]))
            if not item["openable"] and other["openable"]:
                self.assertIn("withheld", item)
        # The attached AO packet (a SharePoint original) is listed and opens for the
        # full reader; the restricted reader's documents carry the withheld marker in
        # its place, and opening it reads as not found.
        wide_docs = {d["citation"]: d for d in wide["documents"]["result"]["documents"]}
        narrow_docs = {d["citation"]: d for d in narrow["documents"]["result"]["documents"]}
        packets = sorted(h for h in wide_docs if h.startswith("original://"))
        self.assertTrue(packets, wide["documents"])
        self.assertTrue(wide_docs[packets[0]]["openable"])
        self.assertTrue(set(narrow_docs) <= set(wide_docs))
        self.assertNotIn(packets[0], narrow_docs)
        self.assertIsNotNone(narrow["documents"]["result"].get("withheld"))
        opened = self.get(full, "/api/object-source?handle=" + quote(packets[0], safe=""))
        self.assertEqual(opened["kind"], "original")
        status, body = limited("/api/object-source?handle=" + quote(packets[0], safe=""))
        self.assertEqual((status, body), (404, {"error": "not_found"}))
        # The explanation block (WS-14) for both readers of the same object: why, the
        # evidence each may open, the gaps and the capture it reflects.
        for answer in (wide, narrow):
            explanation = answer["explanation"]
            self.assertTrue(any(item["kind"] == "identity" for item in explanation["why"]))
            # The server sends one identity candidate per pair and the object labels the
            # console renders; the console merges and words nothing itself.
            pairs = {(e["link_type"], frozenset((e["from"], e["to"])))
                     for e in answer["card"]["identity"]}
            sent = explanation["identity"]
            self.assertEqual(len(sent), len(pairs))
            self.assertEqual({(e["link_type"], frozenset((e["from"], e["to"]))) for e in sent}, pairs)
            for edge in sent:
                for end in (edge["from"], edge["to"]):
                    self.assertEqual(explanation["labels"][end], explanations.subject_words(end))
                    self.assertNotIn(":", explanation["labels"][end].split(" ")[0])
            known = explanation["as_known_at"]
            self.assertEqual((known["state"], known["generation"]),
                             ("current", answer["store"]["mirror_generation"]))
            self.assertTrue(known["captured_at"] and known["label"].startswith("As known at"))
        wide_evidence = {item["handle"]: item for item in wide["explanation"]["evidence"]}
        self.assertTrue(wide_evidence[packets[0]]["openable"])
        # The restricted reader's block never names the packet; the documents' empty
        # answer says something may be withheld for them, never "not found" alone.
        self.assertNotIn(packets[0], json.dumps(narrow["explanation"]))
        documents_gaps = [gap for gap in narrow["explanation"]["gaps"]
                          if "mail and documents" in gap["where"]]
        self.assertTrue(any(gap["text"].endswith("may be " + explanations.WITHHELD)
                            for gap in documents_gaps if gap["class"] == "missing"))
        self.assertTrue(all(explanations.WITHHELD in gap["text"] for gap in documents_gaps
                            if gap["class"] == "missing"), documents_gaps)
        # Evidence offered to the restricted reader opens for them through the source route.
        for item in narrow["explanation"]["evidence"]:
            self.assertIn(item["handle"], wide_evidence)
            if item["openable"]:
                status, _ = limited("/api/object-source?handle=" + quote(item["handle"], safe=""))
                self.assertEqual(status, 200, item)


MC_OPERATOR = "workbench.operator@isolated-fixture.example"
FOLLOW_UP_BUCKET = "BKT-ISOLATED-FOLLOW-UPS"
ALIAS_A = "ricardo.alias@petralabx.com"   # another address of the same Entra user


class PersonControlsTest(unittest.TestCase):
    """The workspace's two controls over HTTP: an employee confirms a link (``plx.review``
    into the Workbench review store, surviving a console restart) and proposes a
    follow-up (an MC follow-up task intent whose exact call waits for another person's
    approval; nothing is sent). Seeded fixture mirror, in-process consoles on 127.0.0.1,
    signed test tickets; evidence: isolated-fixture."""

    @classmethod
    def setUpClass(cls) -> None:
        from operating.tests import test_session_identity as ids

        cls.ids = ids
        cls.tmp = Path(tempfile.mkdtemp(prefix="object-controls-"))
        cls.state = cls.tmp / "state"
        case_fixtures.seed(cls.state, "plx")
        cls.store = cls.state / case_console.ONTOLOGY_FILE
        objects.project_mirror(cls.state, cls.store)
        cls.policy, cls.portal = ids.identity_files(cls.tmp / "identity", {
            case_fixtures.OPERATIONS: ("VIEW_OPERATIONS", "operations.sales", "ops"),
            "review:identity": ("VIEW_OPERATIONS", "operations.sales", "ops")})

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def console(self, *, live, audience=None, ticket=None, planner=None, principals=OPS):
        """A console as ``case_console`` serves it: live, every request's employee from
        its signed ticket (``TicketSessions``; ``ticket``, default a fresh one); else the
        fixture operations session. ``audience``: the follow-up bucket's MC audience
        (``case_console`` takes the committed file's, ``load_bucket_audiences``)."""
        from operating.employee_permissions import (EmployeePermissionPolicy,
                                                    EmployeePermissionResolver,
                                                    PortalAccessSnapshot)
        ready: Queue = Queue(maxsize=1)

        def run():
            try:
                resolver = (EmployeePermissionResolver(
                    EmployeePermissionPolicy.from_file(self.policy),
                    PortalAccessSnapshot(self.portal)) if live else None)
                runtime = CaseRuntime(self.state, client=None, tenant="plx",
                                      audience=self.state / case_fixtures.MIRROR_AUDIENCE_FILE,
                                      employee_permissions=resolver, require_employee=live)

                path = ActionPath(self.state / case_fixtures.ACTIONS_FILE, runtime.cases,
                                  mc_operator_email=MC_OPERATOR,
                                  mc_follow_up_bucket=FOLLOW_UP_BUCKET,
                                  mc_follow_up_audience=audience)

                def objects_for(session):
                    return case_console.object_workspace_for(
                        runtime, self.store, session.actor, state_dir=self.state,
                        session=session, action_path=path, planner=planner)
                fixture = tickets = None
                if live:
                    tickets = case_console.TicketSessions(
                        resolver, self.ids.SECRET,
                        lambda session: CaseWorkspace(runtime, session.actor),
                        build_objects=objects_for,
                        replay_guard=session_identity.replay_guard_for(self.state))
                    server = make_server(ticket_sessions=tickets,
                                         journal=SessionJournal.for_state_dir(self.state))
                else:
                    session = session_identity.fixture_session(
                        "plx", principals, channel=session_identity.CHANNEL_CONSOLE)
                    fixture = objects_for(session)
                    server = make_server(session=session,
                                         case_workspace=CaseWorkspace(runtime, session.actor),
                                         object_workspace=fixture)
                ready.put(server)
                server.serve_forever(poll_interval=0.05)
                server.server_close()
                for view in (fixture, tickets):
                    if view is not None:
                        view.close()
                runtime.close()
            except BaseException as error:  # surfaced to the test thread
                ready.put(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        server = ready.get(timeout=60)
        if isinstance(server, BaseException):
            raise server
        stop = lambda: (server.shutdown(), thread.join(timeout=10))  # noqa: E731
        self.addCleanup(stop)
        bearer = ((ticket or self.ids.Fixture.ticket()) if live
                  else server.operator_access_token)

        def call(method, path, body=None, *, csrf=True):
            conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=60)
            headers = {"Authorization": "Bearer " + bearer}
            data = None
            if body is not None:
                data = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
                if csrf:
                    headers["X-Operator-Token"] = call.csrf
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            value = response.status, json.loads(response.read())
            conn.close()
            return value
        status, session = call("GET", "/api/session")
        self.assertEqual(status, 200, session)
        call.csrf, call.stop = session["csrf_token"], stop
        if live:   # the journal's key for this session (SessionIdentity.session_ref)
            call.session_ref = hashlib.sha256(session_identity.verify_session_ticket(
                bearer, self.ids.SECRET)["jti"].encode()).hexdigest()
        return call

    def open_po(self, call):
        status, found = call("GET", "/api/objects?q=" + quote(case_fixtures.PO_NUMBER, safe=""))
        self.assertEqual(status, 200, found)
        po = next(c["object"]["object_id"] for c in found["find"]["candidates"]
                  if c["object"]["object_type"] == "PurchaseOrder")
        status, view = call("GET", "/api/object?id=" + quote(po, safe=""))
        self.assertEqual(status, 200, view)
        return po, view

    def test_an_employee_confirms_a_link_and_the_decision_survives_a_restart(self):
        call = self.console(live=True)
        po, view = self.open_po(call)
        self.assertEqual(view["reviews"], {"status": "read", "reviews": [], "withheld": 0,
                                           "unread_subjects": 0})
        edge = next(e for hop in view["around"]["hops"] for e in hop["result"]["edges"]
                    if e["from"] == po)
        shown = view["card"]["values"][0]["citation"]
        decision = {"decision": "accept_link", "subject": edge["from"], "target": edge["to"],
                    "link_type": edge["link_type"],
                    "evidence": [{key: shown[key] for key in ("l1_handle", "digest",
                                                              "field_path")}]}
        # The CSRF token is required; the author is never a body field.
        self.assertEqual(call("POST", "/api/object-review", decision, csrf=False)[0], 403)
        self.assertEqual(call("POST", "/api/object-review",
                              dict(decision, author="someone-else"))[0], 400)
        # A version the person was not shown is refused, nothing recorded.
        status, refused = call("POST", "/api/object-review", dict(decision, evidence=[
            dict(decision["evidence"][0], digest="0" * 64)]))
        self.assertEqual((status, refused["error"]), (409, "evidence_changed"))
        status, recorded = call("POST", "/api/object-review", decision)
        self.assertEqual(status, 200, recorded)
        review = recorded["review"]["review"]
        self.assertEqual((review["decision"], review["state"], review["author"]["principal"],
                          recorded["business_system_writes"]),
                         ("accept_link", "effective", "employee:" + self.ids.OID_A, 0))
        call.stop()
        # The session journal recorded what the employee opened and decided, as counts:
        # the panels the object view showed, each review attempt by its answer.
        recorded = SessionJournal.for_state_dir(self.state).summary(
            call.session_ref)["sessions"][call.session_ref]
        self.assertEqual(recorded["objects_opened"], {"PurchaseOrder": 1})
        self.assertEqual({name: recorded["panel_items"][name] for name in (
            "timeline", "originals", "gaps", "conflicts", "reviews")},
            {"timeline": view["timeline"]["total"], "originals": view["originals"]["total"],
             "gaps": view["gaps"]["total"], "conflicts": len(view["conflicts"]),
             "reviews": 0})
        self.assertTrue(any(recorded["panel_items"].values()), recorded)   # not vacuous
        self.assertEqual({key: count for key, count in recorded["by_route"].items()
                          if "/api/object-review" in key},
                         {"POST /api/object-review 403": 1, "POST /api/object-review 400": 1,
                          "POST /api/object-review 409": 1, "POST /api/object-review 200": 1})
        dumped = json.dumps(recorded)
        for value in (po, edge["to"], case_fixtures.PO_NUMBER, self.ids.OID_A):
            self.assertNotIn(value, dumped)

        restarted = self.console(live=True)
        _, again = self.open_po(restarted)
        self.assertEqual([(r["review_id"], r["decision"], r["target"], r["link_type"],
                           r["state"]) for r in again["reviews"]["reviews"]],
                         [(review["review_id"], "accept_link", edge["to"],
                           edge["link_type"], "effective")])

    def test_ask_and_investigate_over_http_reach_the_session_journal_as_counts(self):
        def planner(request):
            card = request["transcript"][0]["observations"][0]["result"]
            cell = next(v for v in card["values"]
                        if v.get("citation") and v.get("value") is not None)
            value = cell["value"]
            return {"action": "answer", "unknowns": [], "text": "It is on record as {v}.",
                    "claims": [{"id": "v", "citation": cell["citation"]["l1_handle"],
                                "field": cell["citation"]["field_path"],
                                "value": value["untrusted_text"] if isinstance(value, dict)
                                else value}]}, None

        call = self.console(live=True, planner=planner)
        po, _ = self.open_po(call)
        status, asked = call("POST", "/api/object-ask",
                             {"object_id": po, "question": "Is it confirmed?"})
        self.assertEqual(status, 200, asked)
        status, investigated = call("GET", "/api/investigate?family=three_way_match&root="
                                    + quote(po, safe=""))
        self.assertEqual(status, 200, investigated)
        call.stop()
        recorded = SessionJournal.for_state_dir(self.state).summary(
            call.session_ref)["sessions"][call.session_ref]
        ask, investigation = ask_counts(asked), investigate_counts(investigated)
        self.assertGreater(ask["citations"], 0)           # not vacuous
        self.assertGreater(investigation["citations"], 0)
        self.assertEqual({key: count for key, count in recorded["by_route"].items()
                          if "ask" in key or "investigate" in key},
                         {"POST /api/object-ask 200": 1, "GET /api/investigate 200": 1})
        self.assertEqual(recorded["panel_items"]["citations"],
                         ask["citations"] + investigation["citations"])
        self.assertEqual(recorded["panel_items"]["claims"],
                         ask["claims"] + investigation["claims"])
        dumped = json.dumps(recorded)
        for value in (po, case_fixtures.PO_NUMBER, self.ids.OID_A):
            self.assertNotIn(value, dumped)

    def test_an_employee_corrects_a_value_and_it_shows_beside_the_source_after_a_restart(self):
        # Its own state directory: the link test reads every review of the PO's lines.
        self.state = Path(tempfile.mkdtemp(dir=self.tmp)) / "state"
        case_fixtures.seed(self.state, "plx")
        self.store = self.state / case_console.ONTOLOGY_FILE
        objects.project_mirror(self.state, self.store)
        call = self.console(live=True)
        po, view = self.open_po(call)
        line = next(e["from"] for hop in view["around"]["hops"] for e in hop["result"]["edges"]
                    if e["to"] == po and e["from"].startswith("PurchaseOrderLine:"))
        status, opened = call("GET", "/api/object?id=" + quote(line, safe=""))
        self.assertEqual(status, 200, opened)
        shown = next(v for v in opened["card"]["values"] if v["property"] == "ordered_quantity")
        cited = {key: shown["citation"][key] for key in ("l1_handle", "digest", "field_path")}
        decision = {"decision": "correct_value", "subject": line,
                    "value": {"property": "ordered_quantity", "value": "12000",
                              "unit": shown["unit"]},
                    "evidence": [dict(cited, start=0, end=len(shown["value"]))]}
        # A correction that cites no page or span of what was read is refused.
        status, refused = call("POST", "/api/object-review", dict(decision, evidence=[cited]))
        self.assertEqual((status, refused["error"]), (409, "citation_required"))
        status, recorded = call("POST", "/api/object-review", decision)
        self.assertEqual(status, 200, recorded)
        review = recorded["review"]["review"]
        self.assertEqual((review["decision"], review["state"], review["author"]["principal"],
                          recorded["review"]["projection"], recorded["business_system_writes"]),
                         ("correct_value", "effective", "employee:" + self.ids.OID_A,
                          "plx_object_now", 0))
        call.stop()

        restarted = self.console(live=True)
        status, again = restarted("GET", "/api/object?id=" + quote(line, safe=""))
        self.assertEqual(status, 200, again)
        value = next(v for v in again["card"]["values"] if v["property"] == "ordered_quantity")
        self.assertEqual((value["value"], value["review"]["value"], value["review"]["unit"],
                          value["review"]["tier"], value["review"]["review_id"]),
                         (shown["value"], "12000", shown["unit"], "reviewed",
                          review["review_id"]))

    def add_packets(self):
        """Two synthetic AO packets in this store, each one Portal-cited quantity: P1
        reaches the fixture's sales order and a second one over the many-to-many
        ``AssemblyOrderPacket.for_sales_order``; P2 reaches the fixture's order and one
        whose audience no reader holds. Rows added to the projected store as a build would
        write them (synthetic identifiers, isolated-fixture)."""
        conn = sqlite3.connect(self.store)
        self.addCleanup(conn.close)
        so, at = conn.execute("SELECT object_id, recorded_at FROM object_versions WHERE "
                              "object_id LIKE 'SalesOrder:%'").fetchone()

        def obj(object_id, relation, native):
            handle = f"api://{relation}/%7B%22id%22%3A{native}%7D@synthetic-1"
            conn.execute("INSERT INTO objects VALUES (?,?,?,?,?,?,?)",
                         (object_id, object_id.split(":")[0], object_id.split(":")[1],
                          json.dumps({"id": native}), "api:" + relation, at, None))
            conn.execute("INSERT INTO object_versions VALUES (?,?,?,?,?,?,?)",
                         (object_id, handle, "%064x" % native, "fm_bronze", "[]", at, None))
            return handle

        packets, others = [], []
        for native, relation in ((99001, "public.order_entry"),
                                 (99002, "public.isolated_withheld_orders")):
            packet = f'AssemblyOrderPacket:sharepoint.ao_packets:{{"id":{native}}}'
            other = f'SalesOrder:portal.fm_order_entry:{{"id":{native + 10}}}'
            handle = obj(packet, "public.assembly_order", native)
            obj(other, relation, native + 10)
            conn.execute("INSERT INTO object_values VALUES "
                         "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (packet, "produced_quantity", "quantity", "1920", "each", "produced",
                          None, None, None, handle, "row.produced_quantity", "%064x" % native,
                          "fm_bronze", "sharepoint.ao_packets", "decimal_text", None, "[]",
                          at, None))
            for order in (so, other):
                conn.execute(
                    "INSERT INTO links(link_id, link_type, from_object, to_object, tier, "
                    "binding_kind, reason, why_not, evidence_handles, markings, recorded_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (f"lnk-{native}-{order}", "AssemblyOrderPacket.for_sales_order", packet,
                     order, "corroborated", "mention",
                     json.dumps([{"feature": "identifier_mention", "agrees": True}]), "[]",
                     json.dumps([handle]), "[]", at))
            packets.append(packet)
            others.append(other)
        conn.commit()
        return packets, so, others

    def test_an_employee_allocates_a_shared_packet_through_plx_review(self):
        """ER-11 at the console: a packet reaching two orders is offered for allocation
        (plx.aggregate's allocation_review_required, with its own value and unit); a share
        over that value is refused (allocation_conflict); a share that fits is the very
        record plx.review allocate records; a link the reader may not see is a withheld
        group, never named and never offered."""
        self.state = Path(tempfile.mkdtemp(dir=self.tmp)) / "state"
        case_fixtures.seed(self.state, "plx")
        self.store = self.state / case_console.ONTOLOGY_FILE
        objects.project_mirror(self.state, self.store)
        (shared, hidden), so, (so2, withheld) = self.add_packets()
        ticket = self.ids.Fixture.ticket()
        call = self.console(live=True, ticket=ticket)
        status, view = call("GET", "/api/object?id=" + quote(shared, safe=""))
        self.assertEqual(status, 200, view)
        [item] = view["allocations"]["items"]
        self.assertEqual((item["link_type"], item["property"], item["state"], item["reached"],
                          [(v["value"], v["unit"]) for v in item["values"]]),
                         ("AssemblyOrderPacket.for_sales_order", "produced_quantity",
                          "allocation_review_required", [so, so2], [("1920", "each")]))
        cited = {key: item["values"][0]["citation"][key]
                 for key in ("l1_handle", "digest", "field_path")}
        decision = {"decision": "allocate", "subject": shared, "target": so,
                    "link_type": item["link_type"], "evidence": [cited],
                    "allocation": {"property": "produced_quantity", "quantity": "1000",
                                   "unit": "each"}}
        status, refused = call("POST", "/api/object-review", dict(decision, allocation=dict(
            decision["allocation"], quantity="1921")))
        self.assertEqual((status, refused["error"]), (409, "allocation_conflict"))
        status, recorded = call("POST", "/api/object-review", decision)
        self.assertEqual(status, 200, recorded)
        self.assertEqual((recorded["review"]["review"]["state"], recorded["review"]["created"],
                          recorded["business_system_writes"]), ("effective", True, 0))
        # plx.review allocate itself, called through the case host in the same session,
        # answers exactly that record: the store holds one allocation, recorded once.
        resolver = EmployeePermissionResolver(EmployeePermissionPolicy.from_file(self.policy),
                                              PortalAccessSnapshot(self.portal))
        runtime = CaseRuntime(self.state, client=None, tenant="plx",
                              audience=self.state / case_fixtures.MIRROR_AUDIENCE_FILE,
                              employee_permissions=resolver, require_employee=True)
        self.addCleanup(runtime.close)
        session, _ = case_console.TicketSessions(
            resolver, self.ids.SECRET, lambda s: None).authenticate(ticket)
        access = objects.host_access(runtime, self.store, session.actor, session=session,
                                     reviews=ReviewStore(self.state / REVIEW_STORE_FILE))
        self.addCleanup(access.close)
        with access.request():
            direct = access.tools()["plx.review"]["handler"](
                {"mode": "record", **decision}, session.actor)
        self.assertEqual((direct["created"], direct["review"]),
                         (False, recorded["review"]["review"]))
        self.assertEqual([r["decision"] for r in ReviewStore(
            self.state / REVIEW_STORE_FILE).history("plx", shared)], ["allocate"])
        # The packet whose second order this reader may not see: withheld, not offered.
        status, view = call("GET", "/api/object?id=" + quote(hidden, safe=""))
        self.assertEqual(status, 200, view)
        self.assertEqual([(i["state"], i["reached"]) for i in view["allocations"]["items"]],
                         [("group_link_withheld", [so])])
        self.assertNotIn(withheld, json.dumps(view))

    def test_a_follow_up_waits_for_another_persons_approval_of_the_exact_call(self):
        """The employee's follow-up is opened and recorded with them as its author, its
        MC call is prepared exactly and nothing is sent; a double submit answers the same
        case; the employee themself (by email or by an alias of their Entra oid) can never
        approve it."""
        call = self.console(live=True)
        po, view = self.open_po(call)
        body = {"object_id": po, "missing": "The supplier's confirmed ship date.",
                "why": "Production waits on the receipt.", "who_can_answer": ["purchasing"]}
        status, proposed = call("POST", "/api/object-follow-up", body)
        self.assertEqual(status, 200, proposed)
        proposal = proposed["proposal"]
        self.assertEqual((proposal["capability"], proposal["state"], proposal["executed"],
                          proposal["approved"], proposed["business_system_writes"]),
                         ("mc.task.create_follow_up", "call_approval_required", False, False,
                          0))
        request = proposal["prepared_request"]
        key = request["arguments"]["idempotencyKey"]
        description = request["arguments"]["description"]
        self.assertEqual(request, {"tool": "mc_create_task", "sent": False, "arguments": {
            "title": body["missing"], "bucket": FOLLOW_UP_BUCKET, "priority": "medium",
            "reporter": MC_OPERATOR, "idempotencyKey": key, "description": description}})
        # Why, then each citation, then the reference
        # that carries the key. This console knows no audience for its bucket, so no
        # source text goes.
        self.assertTrue(description.startswith(f"{body['why']}\n\nCited sources ("))
        self.assertTrue(description.endswith(
            f"\n\nPLX Workbench case {proposal['case_id']}, action {proposal['intent_id']}, "
            f"key {key}."))
        self.assertTrue(proposal["cited"])
        for index, handle in enumerate(proposal["cited"], 1):
            self.assertIn(f"\n\n[{index}] {handle}\n{g2.WITHHELD_UNRESOLVED}\n\n", description)
        self.assertIn(view["card"]["values"][0]["citation"]["l1_handle"], proposal["cited"])
        # A double submit opens nothing new.
        status, again = call("POST", "/api/object-follow-up", body)
        self.assertEqual(status, 200, again)
        self.assertEqual([again["proposal"][k] for k in ("case_id", "proposal_id", "intent_id",
                                                         "duplicate")],
                         [proposal["case_id"], proposal["proposal_id"], proposal["intent_id"],
                          True])
        runtime = CaseRuntime(self.state, client=None, tenant="plx",
                              audience=self.state / case_fixtures.MIRROR_AUDIENCE_FILE)
        self.addCleanup(runtime.close)
        reader = Actor("plx", OPS)
        self.assertEqual(sum(1 for case in runtime.cases.list(reader, limit=200)
                             if case["question"] == body["missing"]), 1)
        # The author can never approve: by their email, or by another address of their oid.
        from operating.action_path import ActionPathError, CALL_APPROVAL_SCHEMA
        from operating.goal2_mc_approval import ApproverRoster
        path = ActionPath(self.state / case_fixtures.ACTIONS_FILE, runtime.cases,
                          call_approvers={self.ids.EMAIL_A: self.ids.OID_A,
                                          ALIAS_A: self.ids.OID_A},
                          mc_operator_email=MC_OPERATOR, approver_roster=ApproverRoster(),
                          mc_follow_up_bucket=FOLLOW_UP_BUCKET)
        state = path.get(reader, proposal["intent_id"])
        session = session_identity.fixture_session("plx", OPS,
                                                   channel=session_identity.CHANNEL_CONSOLE)
        now = datetime.now(timezone.utc)
        for approver in (self.ids.EMAIL_A, ALIAS_A):
            with self.subTest(approver=approver), self.assertRaises(ActionPathError) as caught:
                path.approve_call(reader, proposal["intent_id"], None, {
                    "schema": CALL_APPROVAL_SCHEMA, "tool": "mc_create_task",
                    "arguments": state["prepared"]["arguments"], "approved_by": approver,
                    "approved_at": (now - timedelta(minutes=1)).isoformat(),
                    "expires_at": (now + timedelta(hours=1)).isoformat(),
                    "evidence_ref": "test: the author approving their own follow-up"},
                    session=session)
            self.assertEqual(caught.exception.code, "call_approver_is_subject")
        self.assertEqual(path.get(reader, proposal["intent_id"])["state"],
                         "call_approval_required")

    def test_a_follow_up_quotes_a_source_only_when_every_audience_employee_reads_it(self):
        """The bucket's audience is read through the console's own employee gate: with
        only the author (who reads the PO) the cited text goes; with an employee the
        Portal snapshot does not know, it is withheld with the reason."""
        members = [Actor("plx", frozenset({"employee:" + self.ids.OID_A}))]
        call = self.console(live=True, audience=lambda bucket: g2.McBucketAudience(
            bucket, tuple(members), "test: restricted MC project members"))
        po, _view = self.open_po(call)
        for missing, member, reason in (
                ("The supplier's confirmed ship date (audience A).", None, None),
                ("The supplier's confirmed ship date (audience A, B).",
                 Actor("plx", frozenset({"employee:" + self.ids.OID_B})),
                 g2.WITHHELD_NOT_READABLE)):
            if member is not None:
                members.append(member)
            status, proposed = call("POST", "/api/object-follow-up", {
                "object_id": po, "missing": missing, "why": "Production waits.",
                "who_can_answer": ["purchasing"]})
            self.assertEqual(status, 200, proposed)
            proposal = proposed["proposal"]
            description = proposal["prepared_request"]["arguments"]["description"]
            self.assertTrue(proposal["cited"])
            for index, handle in enumerate(proposal["cited"], 1):
                line = f"\n\n[{index}] {handle}\n"
                self.assertIn(line, description)
                shown = description.split(line, 1)[1].split("\n\n", 1)[0]
                if reason is None:   # the row's fields, as the author reads them
                    self.assertIn("\nstatus: ", shown)
                    self.assertNotIn("text withheld", shown)
                else:
                    self.assertEqual(shown, reason)

    def test_an_employee_records_a_question_familys_follow_ups_on_a_case_as_themself(self):
        """Record on case on the live console: the employee's ticket session is the recorded
        subject of every follow-up, through the console's action path and the case host's
        tools; a second press records nothing twice; nothing is proposed."""
        call = self.console(live=True)
        po, _view = self.open_po(call)
        body = {"family": "landed_cost", "root": po}
        status, first = call("POST", "/api/investigate-record", body)
        self.assertEqual(status, 200, first)
        self.assertTrue(first["recorded"])
        self.assertEqual({d["outcome"] for d in first["recorded"]}, {"recorded"})
        self.assertEqual(len(first["case"]["follow_ups"]), len(first["recorded"]))
        runtime = CaseRuntime(self.state, client=None, tenant="plx",
                              audience=self.state / case_fixtures.MIRROR_AUDIENCE_FILE,
                              employee_permissions=EmployeePermissionResolver(
                                  EmployeePermissionPolicy.from_file(self.policy),
                                  PortalAccessSnapshot(self.portal)), require_employee=True)
        self.addCleanup(runtime.close)
        path = ActionPath(self.state / case_fixtures.ACTIONS_FILE, runtime.cases)
        employee = Actor("plx", frozenset({"employee:" + self.ids.OID_A}))
        attempts = path.internal_actions(employee, first["case_id"])
        self.assertEqual(len(attempts), len(first["recorded"]))
        for attempt in attempts:
            self.assertEqual((attempt["sub"]["kind"], attempt["sub"]["principal"],
                              attempt["act"][0]["channel"]),
                             ("employee", "employee:" + self.ids.OID_A,
                              session_identity.CHANNEL_CONSOLE))
        self.assertEqual(runtime.cases.get(employee, first["case_id"])["proposals"], [])
        status, again = call("POST", "/api/investigate-record", body)
        self.assertEqual((status, again["case_id"], {d["outcome"] for d in again["recorded"]}),
                         (200, first["case_id"], {"exists"}))

    def test_a_fixture_follow_up_has_no_subject_and_a_fixture_person_reviews_with_the_grant(self):
        call = self.console(live=False)
        po, view = self.open_po(call)
        status, proposed = call("POST", "/api/object-follow-up", {
            "object_id": po, "missing": "The supplier's confirmed ship date for PO5447.",
            "why": "Production waits on the receipt.", "who_can_answer": ["purchasing"]})
        self.assertEqual(status, 200, proposed)
        proposal = proposed["proposal"]
        self.assertEqual((proposal["capability"], proposal["executed"], proposal["approved"],
                          proposed["business_system_writes"]),
                         ("mc.task.create_follow_up", False, False, 0))
        self.assertIn(view["card"]["values"][0]["citation"]["l1_handle"], proposal["cited"])
        runtime = CaseRuntime(self.state, client=None, tenant="plx",
                              audience=self.state / case_fixtures.MIRROR_AUDIENCE_FILE)
        self.addCleanup(runtime.close)
        actor = Actor("plx", OPS)
        case = runtime.cases.get(actor, proposal["case_id"])
        self.assertEqual([(p["id"], p["status"]) for p in case["proposals"]],
                         [(proposal["proposal_id"], PROPOSAL_STATUS)])
        # A fixture session names no employee: the intent is registered but never gets a
        # call to approve (no approval of it could count).
        self.assertEqual((proposal["state"], proposal["not_prepared"],
                          proposal["prepared_request"]),
                         ("proposed", "subject_unknown", None))
        # Proposed from inside the open case citing the PO, it attaches to that case; a
        # repeat answers the same proposal, and a case the reader cannot see is not found.
        inside = next(c for c in view["cases"]["cases"] if c["status"] == "open")
        body = {"object_id": po, "case_id": inside["case_id"],
                "missing": "Who signed the PO5447 receipt.", "why": "Quality asks.",
                "who_can_answer": ["operations"]}
        status, attached = call("POST", "/api/object-follow-up", body)
        self.assertEqual(status, 200, attached)
        self.assertEqual((attached["proposal"]["case_id"], attached["proposal"]["duplicate"]),
                         (inside["case_id"], False))
        again = call("POST", "/api/object-follow-up", body)[1]["proposal"]
        self.assertEqual((again["proposal_id"], again["duplicate"]),
                         (attached["proposal"]["proposal_id"], True))
        on_case = runtime.cases.get(actor, inside["case_id"])
        self.assertIn(attached["proposal"]["proposal_id"], [p["id"] for p in on_case["proposals"]])
        self.assertEqual(call("POST", "/api/object-follow-up",
                              dict(body, case_id="case_" + "0" * 24))[0], 404)
        # The fixture console is a person (author human), not an agent session: without the
        # identity-review grant the decision is refused for the grant, nothing written.
        edge = next(e for hop in view["around"]["hops"] for e in hop["result"]["edges"])
        shown = view["card"]["values"][0]["citation"]
        decision = {
            "decision": "reject_link", "subject": edge["from"], "target": edge["to"],
            "link_type": edge["link_type"],
            "evidence": [{"l1_handle": shown["l1_handle"], "digest": shown["digest"]}]}
        status, refused = call("POST", "/api/object-review", decision)
        self.assertEqual((status, refused["error"]), (409, "identity_review_required"))
        self.assertEqual(self.open_po(call)[1]["reviews"]["reviews"], [])
        # With the grant the same person records it (a copy of the state: the other tests
        # read this world's reviews), authored as a fixture name, never an employee.
        copy = Path(tempfile.mkdtemp(dir=self.tmp)) / "state"
        shutil.copytree(self.state, copy)
        self.state, self.store = copy, copy / case_console.ONTOLOGY_FILE
        granted = self.console(live=False, principals=OPS | {"review:identity"})
        status, recorded = granted("POST", "/api/object-review", decision)
        self.assertEqual(status, 200, recorded)
        review = recorded["review"]["review"]
        self.assertEqual((review["decision"], review["state"], recorded["business_system_writes"]),
                         ("reject_link", "effective", 0))
        self.assertTrue(review["author"]["principal"].startswith("fixture:"), review)
        _, again = self.open_po(granted)
        self.assertEqual([(r["review_id"], r["decision"]) for r in again["reviews"]["reviews"]],
                         [(review["review_id"], "reject_link")])


@unittest.skipUnless(importlib.util.find_spec("pypdf") and importlib.util.find_spec("reportlab"),
                     "forwarder quotes are read from PDFs: needs pypdf and reportlab")
class RecordOnCaseTest(unittest.TestCase):
    """LOG-07 at the console: an investigate answer recorded on a case through the one path
    (``ontology_tools_investigate.record_follow_ups``) over the session's action path, and the
    case's follow-ups and drafts listed per reader. The landed_cost forwarder-quote world
    (``tests.test_investigate_landed_cost.ForwarderQuoteTests``: SYNTHETIC quote PDFs captured
    from an injected SharePoint drive, linked to the purchase order they name, projected with
    Portal rows by the one projector; the quote files admit staff:quality by their own item
    permissions), served by fixture consoles; the case store resolves each citation for its
    reader through the same rows. Evidence: isolated-fixture."""

    RECORDER = Actor("plx", frozenset({"staff:quality", "staff:sales"}), "restricted")
    OUTSIDER = Actor("plx", frozenset({"staff:sales"}), "restricted")  # on the case only
    STRANGER = Actor("plx", frozenset({"staff:finance"}), "restricted")  # not on the case

    @classmethod
    def setUpClass(cls) -> None:
        landed.ForwarderQuoteTests.setUpClass.__func__(cls)
        cls.addClassCleanup(landed.ForwarderQuoteTests.tearDownClass.__func__, cls)

    def rows(self, handle, actor):
        return landed.ForwarderQuoteTests.rows(self, handle, actor)

    def setUp(self) -> None:
        self.actions, _, _ = landed._case_path(self, self.rows, self.RECORDER)

    def serve(self, actor):
        """A fixture console for ``actor``: the world's tools, recording through
        ``self.actions`` for its console session. Returns ``call(method, path, body)``: the
        console answers on this thread (the world's capture store is bound to it), the
        client asks from another."""
        session = session_identity.fixture_session(
            "plx", actor.principals, channel=session_identity.CHANNEL_CONSOLE,
            clearance=actor.clearance)
        tools = landed.ForwarderQuoteTests.base(self, session.actor)
        table: dict = {}
        ot.register(table, tools)
        tt.register(table, tt.TraverseTools(tools))
        oi.register(table, oi.InvestigateTools(table, rows=self.rows, scope=oi.read_scope(tools)))
        # Portal rows open (a resolver is configured); without a case host the console
        # grants no other family (ApiOnlyAccess), so a quote it does not open is not offered.
        view = objects.ObjectWorkspace(table, session.actor, sources=object(),
                                       follow_ups=objects.FollowUps(self.actions, session))
        # The case routes are not used here: the console only checks their reader.
        server = make_server(session=session, case_workspace=SimpleNamespace(
            actor=session.actor), object_workspace=view)
        server.timeout = 60
        self.addCleanup(server.server_close)

        def call(method, path, body=None):
            answer: Queue = Queue(maxsize=1)

            def client():
                try:
                    conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=120)
                    headers = {"Authorization": "Bearer " + server.operator_access_token}
                    if body is not None:
                        headers.update({"Content-Type": "application/json",
                                        "X-Operator-Token": call.csrf})
                    conn.request(method, path, None if body is None else json.dumps(body),
                                 headers)
                    response = conn.getresponse()
                    answer.put((response.status, json.loads(response.read())))
                    conn.close()
                except BaseException as error:  # surfaced to the test thread
                    answer.put(error)
            thread = threading.Thread(target=client, daemon=True)
            thread.start()
            server.handle_request()
            thread.join(timeout=120)
            value = answer.get(timeout=1)
            if isinstance(value, BaseException):
                raise value
            return value
        call.csrf = None
        call.csrf = call("GET", "/api/session")[1]["csrf_token"]
        call.session = session
        return call

    def test_an_answer_recorded_on_a_case_lists_its_follow_ups_and_drafts_per_reader(self):
        call = self.serve(self.RECORDER)
        status, found = call("GET", "/api/objects?q=PO100961")
        self.assertEqual(status, 200, found)
        po = next(c["object"]["object_id"] for c in found["find"]["candidates"]
                  if c["object"]["object_type"] == "PurchaseOrder")
        body = {"family": "landed_cost", "root": po}
        with mock.patch.object(oi, "record_follow_ups", wraps=oi.record_follow_ups) as spy:
            status, first = call("POST", "/api/investigate-record", body)
        self.assertEqual(status, 200, first)
        case_id = first["case_id"]
        # One call of the one path, with the console's action path, reader and session.
        spy.assert_called_once()
        self.assertEqual(spy.call_args.args[:3], (self.actions, call.session.actor, case_id))
        self.assertIs(spy.call_args.kwargs["session"], call.session)
        recorded = first["recorded"]
        ups = [d for d in recorded if d["action"] == oi.FOLLOW_UP_ACTION]
        drafts = [d for d in recorded if d["action"] == oi.DRAFT_ACTION]
        self.assertTrue(ups and drafts)
        self.assertEqual({(d["outcome"], d["read_back"]) for d in recorded},
                         {("recorded", "verified")})
        self.assertEqual((first["read_only"], first["business_system_writes"],
                          first["answer_pages"]["complete"]), (False, 0, True))
        self.assertGreater(first["answer_pages"]["read"], 1)  # every page of the answer
        # The case lists exactly what was recorded, as the recorder reads it now.
        listed = first["case"]
        self.assertEqual(call("GET", "/api/object-case?case_id=" + case_id), (200, {
            **{k: v for k, v in first.items() if k in ("read_only", "note", "evidence",
                                                       "business_system_writes", "actor",
                                                       "sources_openable", "store",
                                                       "withheld_sources")},
            "read_only": True, "note": objects.READ_ONLY_NOTE, "case": listed}))
        self.assertEqual([f["record_id"] for f in listed["follow_ups"]],
                         [d["record_id"] for d in ups])
        self.assertEqual([f["record_id"] for f in listed["drafts"]],
                         [d["record_id"] for d in drafts])
        for item in listed["follow_ups"]:
            self.assertTrue(item["missing"] and item["who_can_answer"] and item["state"], item)
        quote_requests = [d for d in listed["drafts"]
                          if d["statement"].startswith("Draft freight quote request to ")]
        self.assertTrue(quote_requests)
        self.assertTrue(all("(not sent)" in d["statement"] and d["status"] == "unknown"
                            for d in listed["drafts"]))
        # Each cites what the answer showed this reader: the quotes it read among them.
        quoted = [d for d in listed["drafts"]
                  if any(c["l1_handle"].startswith("sharepoint://") for c in d["citations"])]
        self.assertTrue(quoted)
        cited = [c for item in listed["drafts"] + listed["follow_ups"] for c in item["citations"]]
        self.assertEqual({c["status"] for c in cited}, {"resolved"})
        self.assertEqual({(c["l1_handle"].split("://")[0], c["openable"]) for c in cited},
                         {("api", True), ("sharepoint", False)})
        # Nothing is proposed, registered or sent: internal records only.
        self.assertEqual(self.actions.cases.get(self.RECORDER, case_id)["proposals"], [])
        self.assertEqual(self.actions.list_for_case(self.RECORDER, case_id)["intents"], [])
        attempts = self.actions.internal_actions(self.RECORDER, case_id)
        self.assertEqual(len(attempts), len(recorded))

        # A second press records nothing twice: the same case, every item exists.
        status, again = call("POST", "/api/investigate-record", body)
        self.assertEqual(status, 200, again)
        self.assertEqual(again["case_id"], case_id)
        self.assertEqual({d["outcome"] for d in again["recorded"]}, {"exists"})
        self.assertEqual(len(again["recorded"]), len(recorded))
        status, named = call("POST", "/api/investigate-record", {**body, "case_id": case_id})
        self.assertEqual((status, {d["outcome"] for d in named["recorded"]}), (200, {"exists"}))

        # A reader the case does not admit is refused before the plan runs or anything is
        # written; an unknown field or a missing case id is refused too.
        stranger = self.serve(self.STRANGER)
        with mock.patch.object(oi, "record_follow_ups", wraps=oi.record_follow_ups) as spy:
            for who, method, path, payload, code in (
                    (stranger, "POST", "/api/investigate-record", {**body, "case_id": case_id},
                     404),
                    (stranger, "GET", "/api/object-case?case_id=" + case_id, None, 404),
                    (call, "POST", "/api/investigate-record", {**body, "author": "x"}, 400),
                    (call, "GET", "/api/object-case", None, 400)):
                with self.subTest(path=path, payload=payload):
                    self.assertEqual(who(method, path, payload)[0], code)
            spy.assert_not_called()
        self.assertEqual(len(self.actions.internal_actions(self.RECORDER, case_id)),
                         len(attempts))

        # A reader on the case but outside the quotes' item permissions sees the drafts and
        # follow-ups citing a quote withheld (no text, no handle), the others as recorded.
        outside = self.serve(self.OUTSIDER)
        status, seen = outside("GET", "/api/object-case?case_id=" + case_id)
        self.assertEqual(status, 200, seen)
        self.assertNotIn("sharepoint://", json.dumps(seen))
        shown = {item["record_id"]: item for item in seen["case"]["drafts"]
                 + seen["case"]["follow_ups"]}
        self.assertEqual(set(shown), {item["record_id"] for item in listed["drafts"]
                                      + listed["follow_ups"]})
        for item in listed["drafts"] + listed["follow_ups"]:
            outsider = shown[item["record_id"]]
            if any(c["l1_handle"].startswith("sharepoint://") for c in item["citations"]):
                self.assertEqual((outsider.get("statement"), outsider.get("missing"),
                                  outsider.get("who_can_answer"), outsider["content"]),
                                 (None, None, None, "withheld_citation_unavailable"), outsider)
            else:
                self.assertEqual(outsider, item)
        self.assertTrue(any(shown[d["record_id"]].get("content") for d in quoted))
        # A withheld draft says whether it is superseded, as the case store records it.
        replaced = quoted[0]["record_id"]
        self.actions.cases.supersede(self.RECORDER, case_id, replaced, "test: a newer quote",
                                     author_kind=session_identity.AUTHOR_HUMAN)
        drafts = outside("GET", "/api/object-case?case_id=" + case_id)[1]["case"]["drafts"]
        self.assertEqual({d["record_id"]: d["superseded"] for d in drafts},
                         {d["record_id"]: d["record_id"] == replaced for d in listed["drafts"]})
        self.assertEqual(next(d for d in drafts if d["record_id"] == replaced)["content"],
                         "withheld_citation_unavailable")

        # Without a case id an answer that is not a plx.investigate answer opens no case.
        with mock.patch.object(objects.ObjectWorkspace, "_whole_answer",
                               lambda view, family, root, first: (
                                   dict(first, procedure="not a procedure"),
                                   {"read": 1, "complete": True})), \
                mock.patch.object(self.actions, "open_case",
                                  wraps=self.actions.open_case) as opened:
            status, refused = outside("POST", "/api/investigate-record", body)
        self.assertEqual((status, refused.get("detail")), (400, "not_an_investigate_answer"))
        opened.assert_not_called()

        # A case another session claims is refused, named or answered by a repeat, before
        # the recorder runs or any attempt is audited.
        holder = {"session": hashlib.sha256(b"another session").hexdigest(),
                  "subject": "fixture:another-session"}
        self.actions.cases.claim(self.RECORDER, case_id, holder, 600)
        with mock.patch.object(oi, "record_follow_ups", wraps=oi.record_follow_ups) as spy:
            for payload in ({**body, "case_id": case_id}, body):
                with self.subTest(payload=payload):
                    status, refused = call("POST", "/api/investigate-record", payload)
                    self.assertEqual((status, refused.get("detail")), (403, "case_claimed"))
            spy.assert_not_called()
        self.assertEqual(len(self.actions.internal_actions(self.RECORDER, case_id)),
                         len(attempts))
        self.actions.cases.claim(self.RECORDER, case_id, holder, 0)   # released

        # A closed case takes no more records when named; an unnamed press opens a fresh case.
        self.actions.cases.close(self.RECORDER, case_id, "test: the answer was followed up")
        with mock.patch.object(oi, "record_follow_ups", wraps=oi.record_follow_ups) as spy:
            status, refused = call("POST", "/api/investigate-record", {**body, "case_id": case_id})
            self.assertEqual((status, refused.get("detail")), (403, "case_closed"))
            spy.assert_not_called()
        # Read to its first page only, the answer is typed partial: page 1 holds no quote, so
        # a gap no read entry matches, and a draft from it, is not recorded (no citation would
        # gate its text; never the purchase order's in its place) and is named as not recorded.
        with mock.patch.object(objects, "MAX_RECORD_PAGES", 1):
            status, fresh = call("POST", "/api/investigate-record", body)
        self.assertEqual(status, 200, fresh)
        self.assertNotEqual(fresh["case_id"], case_id)
        self.assertEqual(self.actions.cases.get(self.RECORDER, fresh["case_id"])["status"],
                         "open")
        self.assertEqual(fresh["answer_pages"],
                         {"read": 1, "complete": False, "partial": "answer_pages_unread"})
        skipped = [d for d in fresh["recorded"] if d["outcome"] == "not_recorded"]
        self.assertEqual({(d["action"], d["partial"], "record_id" in d) for d in skipped},
                         {(oi.FOLLOW_UP_ACTION, "answer_pages_unread", False),
                          (oi.DRAFT_ACTION, "answer_pages_unread", False)})
        self.assertEqual({d["outcome"] for d in fresh["recorded"]} - {"not_recorded"},
                         {"recorded"})
        stored = fresh["case"]["follow_ups"] + fresh["case"]["drafts"]
        self.assertEqual(len(stored), len(fresh["recorded"]) - len(skipped))
        self.assertTrue(all(item["citations"] for item in stored), stored)
        # A case reader without the quotes reads none of the text the quotes gate.
        quoted_text = {item.get("missing") or item["statement"]
                       for item in listed["drafts"] + listed["follow_ups"]
                       if any(c["l1_handle"].startswith("sharepoint://")
                              for c in item["citations"])}
        self.assertTrue(quoted_text)
        status, seen = outside("GET", "/api/object-case?case_id=" + fresh["case_id"])
        self.assertEqual(status, 200, seen)
        shown_text = {value for item in seen["case"]["follow_ups"] + seen["case"]["drafts"]
                      for value in item.values() if isinstance(value, str)}
        self.assertEqual(quoted_text & shown_text, set())


class GrantSnapshotTest(unittest.TestCase):
    """Every tool call of one answer runs inside one grant request; a missing mirror
    fails closed."""

    def test_workspace_calls_run_inside_one_grant_request(self):
        entered = []

        class Grants(objects.ApiOnlyAccess):
            def request(self):
                from contextlib import contextmanager

                @contextmanager
                def scope():
                    entered.append("in")
                    yield
                    entered.append("out")
                return scope()

        def handler(arguments, actor):
            self.assertEqual(entered[-1], "in")
            return {"status": "not_found", "candidates": [], "gaps": []}
        table = {name: {"handler": handler} for name in objects.TOOLS}
        view = objects.ObjectWorkspace(table, Actor("plx", frozenset({"p"})), access=Grants())
        view.find("anything")
        self.assertEqual(entered, ["in", "out"])

    def test_an_unreadable_mirror_fails_closed(self):
        tmp = Path(tempfile.mkdtemp(prefix="object-workspace-unknown-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        state = tmp / "state"
        case_fixtures.seed(state, "plx")
        store = state / case_console.ONTOLOGY_FILE
        objects.project_mirror(state, store)
        status = objects.StoreStatus(store, tmp / "no-such-state")
        self.assertEqual(status.read()["state"], "unknown")
        table = {name: {"handler": lambda arguments, actor: self.fail("tool called")}
                 for name in objects.TOOLS}
        view = objects.ObjectWorkspace(table, Actor("plx", OPS), store_status=status)
        for call in (lambda: view.find("x"), lambda: view.workspace("A:b.c:1")):
            with self.assertRaises(PermissionError):
                call()


class StoreProvenanceTest(unittest.TestCase):
    """A store is a state directory's only when
    it records a Portal mirror or BC capture generation that is that source's capture
    there now; a source the store did not project is labelled not_projected, never
    refused (a store built during a Portal outage). The status is per source, so only a projected
    source of another generation refuses, naming that source and no other."""

    @classmethod
    def setUpClass(cls) -> None:
        from integrations.capture_mail import MailCaptureStore
        from integrations.capture_store import CaptureStore
        from integrations.source_mirror import AUDIENCE_SCHEMA, SourceMirror
        from ontology import project
        from ontology.project_bc import BcCaptures
        from ontology.schema import load_schema

        cls.tmp = Path(tempfile.mkdtemp(prefix="store-provenance-"))
        audience = cls.tmp / "mirror-audience.json"
        audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                        "audiences": {}}), "utf-8")
        bc_audience = SimpleNamespace(tenant="plx", allows=lambda *a, **k: True)

        def state(name, *, bc=True, mail=False):
            """A host state directory with a fresh empty mirror and, as asked, an empty
            BC capture (its own random generation) and mail capture."""
            where = cls.tmp / name
            objects.mirror_path(where).parent.mkdir(parents=True)
            SourceMirror(objects.mirror_path(where), None, audience, tenant="plx").close()
            if bc:
                (where / BC_DIR).mkdir()
                CaptureStore(where / BC_DIR / BC_FILE, bc_audience).close()
            if mail:
                MailCaptureStore(where).db.close()
            return where
        cls.state, cls.other, cls.no_bc = (state("state", mail=True), state("other"),
                                           state("no-bc", bc=False))
        schema = load_schema()
        cls.bc_only, cls.mail_only = cls.tmp / "bc-only.sqlite", cls.tmp / "mail-only.sqlite"
        cls.bc_mail = cls.tmp / "bc-mail.sqlite"
        with BcCaptures(cls.state / BC_DIR / BC_FILE) as bc, \
                project.MailCaptures(cls.state) as mail:
            for target, sources in ((cls.bc_only, {"bc": bc}),
                                    (cls.mail_only, {"graph_mail": mail}),
                                    (cls.bc_mail, {"bc": bc, "graph_mail": mail})):
                project.build_store(target, schema, sources)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def refused(self, store, state):
        """The store state ``checked_store_status`` refuses ``store`` with in ``state``."""
        with self.assertRaises(ValueError) as refusal:
            case_console.checked_store_status(store, state)
        return re.match(r"ontology store refused \((\w+)\)", str(refusal.exception)).group(1)

    def with_generations(self, generations):
        """A copy of the BC-only store whose ``l1_generations`` is ``generations``."""
        copy = self.tmp / f"altered-{len(list(self.tmp.glob('altered-*')))}.sqlite"
        shutil.copyfile(self.bc_only, copy)
        with sqlite3.connect(copy) as conn:
            conn.execute("UPDATE store_meta SET value = ? WHERE key = 'l1_generations'",
                         (json.dumps(generations),))
        conn.close()
        return copy

    def test_a_bc_only_store_is_served_with_the_portal_not_projected(self):
        status = case_console.checked_store_status(self.bc_only, self.state)
        table = {name: {"handler": lambda arguments, actor: {
            "status": "not_found", "candidates": [], "gaps": []}} for name in objects.TOOLS}
        view = objects.ObjectWorkspace(table, Actor("plx", OPS), store_status=status)
        store = view.find("anything")["store"]
        self.assertEqual((store["state"], store["projected"], store["not_projected"]),
                         ("current", ["bc"], ["graph_mail", "portal_api"]), store)
        # A capture the store did not project is labelled from its file, never opened.
        self.assertEqual((store["mirror_generation"], store["mirror_generation_now"]),
                         (None, None))
        self.assertEqual(store["sources"]["portal_api"],
                         {"state": "not_projected", "label": "Portal"})
        # The mail capture it did not project is the one a BC and mail store matches.
        self.assertEqual(objects.StoreStatus(self.bc_mail, self.state).read()["state"],
                         "current")

    def test_another_bc_capture_is_foreign_and_none_is_unknown_both_refused(self):
        self.assertEqual(self.refused(self.bc_only, self.other), "foreign")
        self.assertEqual(self.refused(self.bc_only, self.no_bc), "unknown")
        # Beside the same BC capture, a recorded mail capture missing here is unknown.
        alone = self.tmp / "bc-alone"
        for part in (objects.mirror_path(self.state).parent.name, BC_DIR):
            shutil.copytree(self.state / part, alone / part)
        self.assertEqual(case_console.checked_store_status(self.bc_only, alone).read()["state"],
                         "current")

    def state_copy(self, name, *, source=None, teams=None):
        """A copy of the mirror and BC capture of ``source`` (default the BC and mail
        state) without its mail capture, with a Teams capture file holding ``teams``."""
        where = self.tmp / name
        for part in (objects.mirror_path(self.state).parent.name, BC_DIR):
            shutil.copytree((source or self.state) / part, where / part)
        if teams is not None:
            from integrations import capture_teams
            (where / capture_teams.STORE_DIR).mkdir()
            (where / capture_teams.STORE_DIR / capture_teams.DB_FILE).write_bytes(teams)
        return where

    def answer(self, status):
        """``plx.find``'s ``store`` as the workspace serves it over ``status``."""
        table = {name: {"handler": lambda arguments, actor: {
            "status": "not_found", "candidates": [], "gaps": []}} for name in objects.TOOLS}
        view = objects.ObjectWorkspace(table, Actor("plx", OPS), store_status=status)
        return view.find("anything")["store"]

    def test_an_unprojected_unreadable_capture_never_refuses_or_is_blamed_on_bc(self):
        garbage = self.state_copy("garbage-teams", teams=b"not a capture store " * 64)
        store = self.answer(case_console.checked_store_status(self.bc_only, garbage))
        self.assertEqual((store["state"], store["not_projected"]),
                         ("current", ["portal_api", "teams"]), store)
        self.assertEqual(store["sources"]["teams"], {"state": "not_projected",
                                                     "label": "Teams"})
        self.assertEqual(store["sources"]["bc"]["state"], "current")

    def test_a_removed_capture_is_labelled_and_another_generation_names_only_its_source(self):
        # The mail capture the store projected was deleted: mail is removed, the store
        # still answers, labelled stale.
        alone = self.state_copy("mail-removed")
        store = self.answer(case_console.checked_store_status(self.bc_mail, alone))
        self.assertEqual((store["state"], store["removed"], store["sources"]["graph_mail"]),
                         ("stale", ["graph_mail"], {"state": "removed", "label": "mail"}),
                         store)
        self.assertIn("mail capture it was projected from is gone", store["detail"])
        # Another BC generation (beside a removed mail capture and an unreadable Teams
        # file the store never projected) refuses, naming bc and nothing else.
        foreign = self.state_copy("foreign-bc", source=self.other, teams=b"garbage")
        with self.assertRaises(PermissionError) as refusal:
            self.answer(objects.StoreStatus(self.bc_mail, foreign))
        self.assertEqual(str(refusal.exception),
                         "ontology_store_from_another_capture_generation:bc")
        self.assertIn("another bc capture generation", objects.StoreStatus(
            self.bc_mail, foreign).read()["detail"])

    def test_one_store_generation_over_unchanged_captures_opens_them_once(self):
        opened, plain = [], objects.StoreStatus(self.bc_mail, self.state)
        status = objects.StoreStatus(self.bc_mail, self.state, opener=lambda source: (
            opened.append(source) or plain.open_capture(source)))
        first = self.answer(status)
        self.assertEqual((first["state"], sorted(opened)), ("current", ["bc", "graph_mail"]))
        self.assertEqual(self.answer(status), first)
        self.assertEqual(sorted(opened), ["bc", "graph_mail"])   # no capture opened again
        # A capture file that changes is read again on the next answer.
        bc_file = self.state / BC_DIR / BC_FILE
        stat = bc_file.stat()
        os.utime(bc_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        self.assertEqual(self.answer(status)["state"], "current")
        self.assertEqual(sorted(opened), ["bc", "bc", "graph_mail", "graph_mail"])

    def test_a_bc_capture_newer_than_the_store_reads_stale(self):
        from integrations.tests import test_bc_capture as tb
        from ontology import project
        from ontology.project_bc import BcCaptures
        from ontology.schema import load_schema

        where = self.tmp / "bc-newer"
        (where / BC_DIR).mkdir(parents=True)
        fake = tb.FakeBc({"purchaseOrders": 1})
        capture = tb.Harness(self, fake, db=where / BC_DIR / BC_FILE)
        capture.run(entity_sets=("purchaseOrders",))
        store = self.tmp / "bc-newer.sqlite"
        with BcCaptures(where / BC_DIR / BC_FILE) as bc:
            project.build_store(store, load_schema(), {"bc": bc})
        status = case_console.checked_store_status(store, where)
        self.assertEqual(self.answer(status)["state"], "current")
        fake.data[(tb.CO[0], "purchaseOrders")][0].update(
            {"@odata.etag": 'W/"changed"', "number": "N-CHANGED"})
        capture.run(entity_sets=("purchaseOrders",))
        self.assertIn("bc/purchaseOrders", status.read()["newer_relations"])
        # A reader BC is withheld from (no host grant) sees BC stale, not which of its
        # relations changed.
        stale = self.answer(status)
        self.assertEqual(stale["state"], "stale", stale)
        self.assertEqual(stale["sources"]["bc"], {
            "state": "stale", "generation": status.read()["sources"]["bc"]["generation"],
            "label": "Business Central"})
        self.assertEqual(stale["newer_relations"], [])
        self.assertIn("Business Central capture is newer than this store", stale["detail"])

    def test_teams_and_mc_relations_newer_than_the_store_show_only_to_their_grantees(self):
        # Gap gap-d2a0aa06aafc: every grantable capture family is withheld from a reader
        # the host does not grant it, not only BC, mail and SharePoint.
        from integrations import capture_teams, mc_sources
        from operating.ontology_tools import ReadPolicy

        where = self.state_copy("teams-mc-stale", teams=b"")
        mc_sources.store_path(where).parent.mkdir(parents=True)
        mc_sources.store_path(where).write_bytes(b"")
        self.assertTrue((where / capture_teams.STORE_DIR / capture_teams.DB_FILE).is_file())
        newer = {"teams": "channelMessages", "mission_control": "tasks"}
        with sqlite3.connect(self.bc_only) as conn:
            meta = dict(conn.execute("SELECT key, value FROM store_meta"))
        conn.close()
        generations = {**json.loads(meta["l1_generations"]),
                       **{source: {"generation": source + "-1"} for source in newer}}
        freshness = {**json.loads(meta.get("source_freshness") or "{}"),
                     **{source: {relation: {"last_capture_id": 1}}
                        for source, relation in newer.items()}}
        meta.update(l1_generations=json.dumps(generations),
                    source_freshness=json.dumps(freshness))
        plain = objects.StoreStatus(self.bc_only, where)

        @contextlib.contextmanager
        def opener(source):
            if source not in newer:
                with plain.open_capture(source) as capture:
                    yield capture
                return
            yield SimpleNamespace(describe=lambda: {
                "generation": source + "-1",
                "relations": {newer[source]: {"last_capture_id": 2}}})
        status = objects.StoreStatus(self.bc_only, where, opener=opener)
        base = {"api:*": OPS, "bc:*": OPS}
        table = {name: {"handler": lambda arguments, actor: {
            "status": "not_found", "candidates": [], "gaps": []}} for name in objects.TOOLS}

        def answer(audiences):
            """``plx.find``'s answer to an OPS employee over the case-host access whose
            policy grants ``audiences``."""
            host = SimpleNamespace(
                policy_now=lambda reader: ReadPolicy("plx", audiences),
                request=contextlib.nullcontext, store_meta=lambda: dict(meta))
            access = objects.HostAccess(host, SimpleNamespace(employee_gate=lambda a: a),
                                        Actor("plx", OPS))
            return objects.ObjectWorkspace(table, Actor("plx", OPS), store_status=status,
                                           access=access).find("anything")

        ungranted = answer(base)
        store = ungranted["store"]
        self.assertEqual(store["state"], "stale", store)
        for source, label in (("teams", "Teams"), ("mission_control", "Mission Control")):
            self.assertEqual(store["sources"][source], {
                "state": "stale", "generation": source + "-1", "label": label})
        self.assertEqual(store["newer_relations"], [])
        self.assertNotIn("channelMessages", json.dumps(ungranted))
        self.assertNotIn("tasks", json.dumps(store))
        self.assertIn("Mission Control, Teams capture is newer", store["detail"])
        self.assertTrue({"teams", "mission_control"} <= {
            item["source"] for item in ungranted["withheld_sources"]})

        granted = answer({**base, "teams:*": OPS, "mc:*": OPS})
        self.assertEqual(granted["store"]["newer_relations"],
                         ["mission_control/tasks", "teams/channelMessages"])
        self.assertEqual(granted["store"]["sources"]["teams"]["newer_relations"],
                         ["channelMessages"])
        self.assertFalse({"teams", "mission_control"} & {
            item["source"] for item in granted["withheld_sources"]})

        class ApiOnly(objects.ApiOnlyAccess):
            def store_meta(self):
                return dict(meta)
        # An API-only reader (no case host) is granted the Portal alone: its object view
        # withholds every other grantable family, Teams and MC included, for want of a
        # host grant, and names none of their relations.
        api_only = objects.ObjectWorkspace(
            table, Actor("plx", OPS), store_status=status,
            access=ApiOnly()).workspace('Header:portal.sample_header:{"id":7}')
        self.assertEqual(api_only["status"], "read")
        withheld = [item["source"] for item in api_only["withheld_sources"]]
        self.assertEqual(withheld, [source for source, _ in objects.grantable_sources()
                                    if source not in objects.GRANTED_FAMILIES])
        self.assertTrue({"bc", "graph_mail", "sharepoint", "teams", "mission_control"}
                        <= set(withheld))
        self.assertTrue(all(item["reason"] == objects.NO_HOST_GRANT
                            for item in api_only["withheld_sources"]))
        self.assertEqual(api_only["store"]["newer_relations"], [])
        for source, label in (("teams", "Teams"), ("mission_control", "Mission Control")):
            self.assertEqual(api_only["store"]["sources"][source], {
                "state": "stale", "generation": source + "-1", "label": label})
        self.assertNotIn("channelMessages", json.dumps(api_only))
        self.assertNotIn("tasks", json.dumps(api_only["store"]))
        # A family no host grant names is never shown its relation names (fails closed).
        unknown = objects._store_for({"newer_relations": ["future/x"], "sources": {
            "future": {"state": "stale", "newer_relations": ["x"]}}}, set())
        self.assertEqual(unknown, {"newer_relations": [], "sources": {
            "future": {"state": "stale"}}})

    def meta(self, store):
        with sqlite3.connect(store) as conn:
            value = conn.execute("SELECT value FROM store_meta WHERE key = "
                                 "'l1_generations'").fetchone()[0]
        conn.close()
        return value

    def test_a_store_with_no_portal_or_bc_generation_is_refused(self):
        # Mail's generation is its capture schema version, the same in every copy: the
        # mail-only store matches this state's mail capture and is still not its own.
        self.assertEqual(json.loads(self.meta(self.mail_only)),
                         {"graph_mail": json.loads(self.meta(self.bc_mail))["graph_mail"]})
        self.assertEqual(self.refused(self.mail_only, self.state), "unknown")
        for generations in ({}, {"portal_api": {"generation": None}},
                            {"bc": "bc-capture/0"}, ["bc"]):
            with self.subTest(generations=generations):
                self.assertEqual(self.refused(self.with_generations(generations),
                                              self.state), "unknown")


def _cite(handle, field, digest="a"):
    return {"l1_handle": handle, "field_path": field, "digest": digest * 64,
            "lineage": "gold_etl_from_fm"}


class JoinedPanelsTest(unittest.TestCase):
    """The join over contract-shaped tool answers (stub tool table, no store)."""

    ROOT = 'Header:portal.sample_header:{"id":7}'
    OTHER = 'Party:bc.sample_party:{"id":3}'
    H1 = "api://public.sample_header/%7B%22id%22%3A7%7D@g1-3"
    H1B = "api://public.sample_header/%7B%22id%22%3A7%7D@g1-9"
    MAIL = "graph-mail://box@example.test/m1@c4"

    def table(self, documents=None):
        calls = []

        def tool(name, answer):
            def handler(arguments, actor):
                calls.append((name, arguments, actor))
                return answer(arguments) if callable(answer) else answer
            return {"handler": handler}
        card = {"object": {"object_id": self.ROOT, "object_type": "Header", "display": None,
                           "lineage": "gold_etl_from_fm"},
                "values": [{"property": "number", "value_type": "identifier", "value": "H-7",
                            "citation": _cite(self.H1, "row.number"), "markings": []}],
                "conflicts": [{"kind": "value_disagreement", "subject": "due", "values": [
                    {"property": "due", "value_type": "date", "value": "2099-01-05",
                     "citation": _cite(self.H1, "row.due"), "markings": []},
                    {"property": "due", "value_type": "date", "value": "2099-01-09",
                     "citation": _cite(self.H1B, "row.due", "b"), "markings": []}]}],
                "identity": [], "gaps": [],
                "linked_counts": [{"link_type": "Header.ordered_from", "direction": "forward",
                                   "count": 1}],
                "withheld": {"properties": 1, "objects": None, "reason": "marking"},
                "completeness": {"complete": True, "truncated": False, "returned": 1},
                "meta": {}}
        around = {"set": "set:abcdefgh1", "hops": [],
                  "objects": [{"object_id": self.OTHER, "object_type": "Party",
                               "display": None, "lineage": "bc_sample"}],
                  "edges": [{"link_id": "L1", "link_type": "Header.ordered_from",
                             "from": self.ROOT, "to": self.OTHER, "tier": "lead",
                             "reason": [{"feature": "pair", "agrees": True,
                                         "citations": [_cite(self.MAIL, "body", "c")]}],
                             "why_not": []}],
                  "gaps": [], "completeness": {"complete": True, "truncated": False,
                                               "returned": 1}}

        def timeline(arguments):
            if arguments["from"] == self.ROOT:
                return {"events": [{"event_id": "e1", "event_type": "Header.created",
                                    "time_basis": "source_business_date", "time": "2099-01-02",
                                    "objects": [{"object_id": self.ROOT,
                                                 "qualifier": "subject"}],
                                    "citation": _cite(self.H1, "row.created")}],
                        "gaps": [], "completeness": {"complete": True, "truncated": False,
                                                     "returned": 1}}
            return {"events": [], "gaps": [],
                    "withheld": {"properties": None, "objects": 2, "reason": "audience"},
                    "completeness": {"complete": True, "truncated": False, "returned": 0}}
        table = {"plx.find": tool("plx.find", {}), "plx.object": tool("plx.object", card),
                 "plx.around": tool("plx.around", around),
                 "plx.timeline": tool("plx.timeline", timeline)}
        if documents is not None:
            table[objects.DOCUMENTS_TOOL] = tool(objects.DOCUMENTS_TOOL, documents)
        return table, calls

    def open(self, **kwargs):
        table, calls = self.table(**kwargs)
        view = objects.ObjectWorkspace(table, Actor("plx", frozenset({"employee:oid-1"})),
                                       sources=object())
        return view.workspace(self.ROOT), calls

    def test_originals_list_every_cited_capture_and_withhold_an_ungranted_scheme(self):
        data, _ = self.open()
        by_handle = {item["l1_handle"]: item for item in data["originals"]["items"]}
        self.assertEqual(set(by_handle), {self.H1, self.H1B, self.MAIL})
        self.assertEqual({key: data["originals"][key] for key in ("total", "truncated")},
                         {"total": 3, "truncated": False})
        first = by_handle[self.H1]
        self.assertEqual((first["record"], first["version"]),
                         (objects.record_of(self.H1), "g1-3"))
        self.assertEqual(first["sha256"], ["a" * 64])
        self.assertEqual(first["cited_by"], ["value number", "event Header.created",
                                             "conflict due"])
        self.assertEqual(first["fields"], ["row.created", "row.due", "row.number"])
        self.assertEqual(first["properties"], ["number"])
        self.assertEqual((first["source"], first["lineages"]),
                         ("Portal API", ["gold_etl_from_fm"]))
        self.assertTrue(first["openable"])
        # The same record at a later capture is its own version, never merged away.
        self.assertEqual(by_handle[self.H1B]["record"], first["record"])
        self.assertEqual(by_handle[self.H1B]["version"], "g1-9")
        # A link reason citing mail: listed, with its digest, withheld with the reason.
        mail = by_handle[self.MAIL]
        self.assertEqual(mail["cited_by"], ["link Header.ordered_from"])
        self.assertFalse(mail["openable"])
        self.assertEqual(mail["withheld"], {"state": "withheld",
                                            "reason": objects.NO_HOST_GRANT})
        self.assertEqual(mail["sha256"], ["c" * 64])
        self.assertEqual(mail["source"], "mail")

    def test_source_system_comes_from_the_scheme_not_the_first_citation(self):
        # A neighbour display origin (no lineage) is the first citation of the capture;
        # a later link reason states the lineage. Neither decides the source system.
        table, _ = self.table()
        around = table["plx.around"]["handler"]({}, None)
        around["objects"][0]["display"] = {"untrusted_text": "Vendor",
                                           "origin": {"citation": self.MAIL,
                                                      "field_path": "subject"}}
        around["edges"][0]["reason"][0]["citations"][0]["lineage"] = "graph_mail_capture"
        table["plx.around"] = {"handler": lambda arguments, actor: around}
        view = objects.ObjectWorkspace(table, Actor("plx", frozenset({"employee:oid-1"})))
        [mail] = [item for item in view.workspace(self.ROOT)["originals"]["items"]
                  if item["l1_handle"] == self.MAIL]
        self.assertEqual(mail["cited_by"], ["display of linked Party",
                                            "link Header.ordered_from"])
        self.assertEqual((mail["source"], mail["lineages"]), ("mail", ["graph_mail_capture"]))

    def many_events(self, n):
        """A timeline answer citing ``n`` distinct api captures plus one bc capture."""
        events = [{"event_id": f"e{i}", "event_type": "Line.received",
                   "time_basis": "source_business_date", "time": "2099-01-02",
                   "objects": [{"object_id": self.ROOT, "qualifier": "subject"}],
                   "citation": _cite(f"api://public.sample_line/{i:04d}@g1-1", "row.qty")}
                  for i in range(n)]
        events.append({"event_id": "eb", "event_type": "Bc.posted",
                       "time_basis": "source_business_date", "time": "2099-01-03",
                       "objects": [{"object_id": self.ROOT, "qualifier": "subject"}],
                       "citation": _cite("bc://sample/entry/1@c1", "row.amount")})
        return {"events": events, "gaps": [],
                "completeness": {"complete": True, "truncated": False,
                                 "returned": len(events)}}

    def test_originals_past_the_cap_say_so_and_keep_every_withheld_capture(self):
        table, _ = self.table()
        # The timeline keeps MAX_EVENTS events, so card values carry the rest.
        answer = self.many_events(190)
        table["plx.timeline"] = {"handler": lambda arguments, actor: answer}
        card = table["plx.object"]["handler"]({}, None)
        card["values"] += [{"property": f"line{i}", "value_type": "quantity", "value": i,
                            "citation": _cite(f"api://public.sample_extra/{i:04d}@g1-1",
                                              "row.qty"), "markings": []}
                           for i in range(60)]
        table["plx.object"] = {"handler": lambda arguments, actor: card}
        view = objects.ObjectWorkspace(table, Actor("plx", frozenset({"employee:oid-1"})))
        originals = view.workspace(self.ROOT)["originals"]
        items = {item["l1_handle"]: item for item in originals["items"]}
        # 190 event + 60 value captures, 1 bc, H1, H1B (card) and MAIL (link reason).
        self.assertEqual((originals["total"], originals["truncated"], len(items)),
                         (254, True, objects.MAX_ORIGINALS))
        # The withheld captures sort after api:// and would be the first cut; they stay.
        self.assertIn(self.MAIL, items)
        self.assertIn("bc://sample/entry/1@c1", items)
        self.assertEqual(originals["by_scheme"],
                         {"api": {"total": 252, "shown": 198},
                          "bc": {"total": 1, "shown": 1},
                          "graph-mail": {"total": 1, "shown": 1}})
        self.assertEqual(originals["withheld"], {"total": 2, "shown": 2})
        self.assertEqual(sum(c["shown"] for c in originals["by_scheme"].values()), len(items))

    def test_many_withheld_captures_are_bounded_and_never_crowd_out_openable_ones(self):
        table, _ = self.table()
        around = table["plx.around"]["handler"]({}, None)
        around["edges"][0]["reason"][0]["citations"] += [
            _cite(f"bc://sample/entry/{i:04d}@c1", "row.amount") for i in range(400)]
        table["plx.around"] = {"handler": lambda arguments, actor: around}
        view = objects.ObjectWorkspace(table, Actor("plx", frozenset({"employee:oid-1"})),
                                       sources=object())
        originals = view.workspace(self.ROOT)["originals"]
        items = {item["l1_handle"]: item for item in originals["items"]}
        # 400 bc + MAIL withheld; H1 (card value) and H1B (conflict) openable.
        self.assertEqual((originals["total"], originals["truncated"], len(items)),
                         (403, True, objects.MAX_ORIGINALS))
        self.assertTrue(items[self.H1]["openable"] and items[self.H1B]["openable"])
        self.assertEqual(originals["withheld"], {"total": 401, "shown": 198})
        self.assertEqual(originals["by_scheme"]["bc"]["shown"]
                         + originals["by_scheme"]["graph-mail"]["shown"], 198)
        # Permission is unchanged: a listed withheld capture never opens.
        held = [item for item in items.values() if not item["l1_handle"].startswith("api://")]
        self.assertTrue(held and all(not item["openable"] and item["withheld"]
                                     == {"state": "withheld", "reason": objects.NO_HOST_GRANT}
                                     for item in held))

    def test_the_cards_own_captures_survive_the_cap_before_timeline_captures(self):
        table, _ = self.table()
        events = [{"event_id": f"e{i}", "event_type": "Line.received",
                   "time_basis": "source_business_date", "time": "2099-01-02",
                   "objects": [{"object_id": self.ROOT, "qualifier": "subject"}],
                   "citation": _cite(f"api://public.aaa_line/{i:04d}@g1-1", "row.qty")}
                  for i in range(199)]
        answer = {"events": events, "gaps": [],
                  "completeness": {"complete": True, "truncated": False, "returned": 199}}
        table["plx.timeline"] = {"handler": lambda arguments, actor: answer}
        card = table["plx.object"]["handler"]({}, None)
        own = "api://public.zzz_header/7@g1-3"
        card["values"].append({"property": "total", "value_type": "money", "value": "5.00",
                               "citation": _cite(own, "row.total"), "markings": []})
        table["plx.object"] = {"handler": lambda arguments, actor: card}
        view = objects.ObjectWorkspace(table, Actor("plx", frozenset({"employee:oid-1"})),
                                       sources=object())
        originals = view.workspace(self.ROOT)["originals"]
        items = {item["l1_handle"]: item for item in originals["items"]}
        self.assertEqual((originals["total"], originals["truncated"], len(items)),
                         (203, True, objects.MAX_ORIGINALS))
        # Card values, then the conflict's capture, then the timeline's, in that order.
        self.assertIn(own, items)
        self.assertEqual(items[own]["properties"], ["total"])
        self.assertIn(self.H1, items)
        self.assertIn(self.H1B, items)
        self.assertIn(self.MAIL, items)
        self.assertEqual(originals["by_scheme"]["api"], {"total": 202, "shown": 199})
        self.assertEqual(sum(h.startswith("api://public.aaa_line/") for h in items), 196)

    def test_originals_share_the_cap_round_robin_across_granted_families(self):
        # With bc granted too, a long api list cannot crowd bc out (or the reverse).
        handles = ([f"api://public.sample_line/{i:04d}@g1-1" for i in range(300)]
                   + [f"bc://sample/entry/{i:04d}@c1" for i in range(300)]
                   + ["graph-mail://box@example.test/m1@c4"])
        cited = {handle: {"cited_by": ["event x"]} for handle in handles}
        mail = {"graph-mail://box@example.test/m1@c4"}
        kept = objects._cap_originals(cited, objects.MAX_ORIGINALS, mail)
        self.assertEqual(len(kept), objects.MAX_ORIGINALS)
        self.assertIn("graph-mail://box@example.test/m1@c4", kept)
        self.assertEqual(sum(h.startswith("api://") for h in kept), 100)
        self.assertEqual(sum(h.startswith("bc://") for h in kept), 99)
        # A short family is exhausted and its slots go to the others.
        kept = objects._cap_originals(
            {h: cited[h] for h in handles[:300] + handles[300:305]}, 50, mail)
        self.assertEqual(sum(h.startswith("bc://") for h in kept), 5)
        self.assertEqual(len(kept), 50)
        self.assertEqual(objects._cap_originals({h: cited[h] for h in handles[:3]}, 50),
                         sorted(handles[:3]))

    def test_explanation_never_shows_a_withheld_capture_and_labels_an_outage_stale(self):
        table, _ = self.table()
        card = table["plx.object"]["handler"]
        label = ("source-mirror captures as of 2099-01-01T00:00:00Z, live source unavailable "
                 "(500, internal)")

        def held_card(arguments, actor):
            return {**card(arguments, actor), "meta": {"as_known_at": "2099-01-03T00:00:00Z",
                    "freshness": [{"source": "portal_api", "captured_at": "2099-01-01T00:00:00Z",
                                   "completeness": "partial", "stale": True,
                                   "served_from": "persisted_capture", "label": label}]}}
        table["plx.object"] = {"handler": held_card}
        view = objects.ObjectWorkspace(table, Actor("plx", frozenset({"employee:oid-1"})),
                                       sources=object())
        explanation = view.workspace(self.ROOT)["explanation"]
        # The link reason cites mail this console does not grant: the link's why stays,
        # the capture is a counted withheld gap, never a handle or an absence.
        self.assertNotIn(self.MAIL, json.dumps(explanation))
        self.assertEqual({item["handle"] for item in explanation["evidence"]}, {self.H1, self.H1B})
        gaps = {(gap["class"], gap["text"]): gap for gap in explanation["gaps"]}
        self.assertEqual(gaps[("withheld", "Mail records cited here: " + explanations.WITHHELD)][
            "count"], 1)
        self.assertIn(("withheld", "1 value " + explanations.WITHHELD), gaps)
        self.assertIn(("withheld", "2 records " + explanations.WITHHELD), gaps)
        self.assertEqual(gaps[("conflicting", "Sources disagree: value disagreement")]["about"],
                         ["due"])
        self.assertTrue(any(item["kind"] == "link" and "lead" in item["text"]
                            for item in explanation["why"]))
        known = explanation["as_known_at"]
        self.assertEqual((known["state"], known["captured_at"]),
                         ("outage", "2099-01-01T00:00:00Z"))
        self.assertTrue(known["label"].startswith("STALE") and label in known["label"])

    def test_withheld_parts_of_each_call_stay_in_the_answer(self):
        data, calls = self.open()
        self.assertEqual(data["card"]["withheld"]["properties"], 1)
        [neighbour] = [call for call in data["timeline"]["calls"]
                       if call["root"] != "object"]
        self.assertEqual(neighbour["result"]["withheld"],
                         {"properties": None, "objects": 2, "reason": "audience"})
        # Every tool call was made for the session reader.
        self.assertTrue(calls)
        self.assertTrue(all(actor.principals == frozenset({"employee:oid-1"})
                            for _, _, actor in calls))

    def test_documents_join_when_served_and_say_so_when_not(self):
        data, calls = self.open()
        self.assertEqual(data["documents"]["status"], "not_configured")
        self.assertNotIn(objects.DOCUMENTS_TOOL, [name for name, _, _ in calls])
        answer = {"documents": [{"citation": self.MAIL, "kind": "email",
                                 "title": {"untrusted_text": "PO H-7"}, "date": "2099-01-03",
                                 "snippet": None, "mentions": []},
                                {"citation": self.H1, "kind": "original", "title": None,
                                 "date": None, "snippet": None, "mentions": []}],
                  "searched": {"sources": ["graph_mail"]},
                  "gaps": [{"state": "inaccessible", "reason": "not_served",
                            "subject": "sharepoint"}],
                  "withheld": {"properties": None, "objects": None, "reason": "audience"},
                  "completeness": {"complete": True, "truncated": False, "returned": 2}}
        data, calls = self.open(documents=answer)
        [(_, arguments, _)] = [call for call in calls if call[0] == objects.DOCUMENTS_TOOL]
        self.assertEqual(arguments, {"object_id": self.ROOT, "limit": objects.MAX_DOCUMENTS})
        self.assertEqual(data["documents"]["status"], "read")
        mail, row = data["documents"]["result"]["documents"]
        self.assertEqual((mail["openable"], mail["not_openable_reason"]),
                         (False, objects.NO_HOST_GRANT))
        self.assertTrue(row["openable"])
        self.assertEqual(data["documents"]["result"]["withheld"]["reason"], "audience")
        self.assertIn({"from": "plx.documents", "state": "inaccessible",
                       "reason": "not_served", "subject": "sharepoint"},
                      data["gaps"]["items"])
        data, _ = self.open(documents={"error": {"code": "internal", "message": "x"}})
        self.assertEqual(data["documents"], {"status": "error",
                                             "error": {"code": "internal", "message": "x"}})

    def test_every_action_path_state_has_an_approval_stage(self):
        from operating import action_path
        self.assertEqual(set(objects.APPROVAL_STAGES), set(action_path.STATES))
        for state in action_path.STATES:
            approval = objects.approval_of(state)
            self.assertFalse(approval["approved_here"])
            self.assertEqual(approval["where"], "Mission Control")
        # Only a state after an authorizing gate decision reads approved.
        for state in ("proposed", "gate_prepared", "call_approved", "gate_requested",
                      "gate_rejected", "approval_expired", "gate_request_unknown"):
            self.assertNotEqual(objects.approval_of(state)["stage"], "approved", state)
        self.assertEqual(objects.approval_of("gate_authorized")["stage"], "approved")
        self.assertEqual(objects.approval_of("no_such_state")["stage"], "unknown")


class SharePointItemGateTest(unittest.TestCase):
    """SharePoint evidence through the case host's per-item gate.

    A versioned SharePoint capture (integrations/capture_sharepoint.py over the capture
    tests' in-process FakeDrive) projected with a Portal purchase order and mention
    links, served to two readers who both hold the folder: quality (Ricardo's identity)
    and sales, whose identity is on only some files' item permissions. Evidence label:
    injected-transport; no network, no credentials.
    """

    def setUp(self):
        import datetime as dt

        from integrations import graph_read
        from integrations.capture_sharepoint import (SharePointCapture,
                                                     SharePointCaptureStore, version_handle)
        from integrations.graph_read import SharePointReader
        from integrations.live_source_guard import BoundedBackoff
        from integrations.source_mirror import AUDIENCE_SCHEMA
        from integrations.tests import test_capture_sharepoint as tc
        from ontology import mentions
        from ontology import schema as schema_loader
        from ontology.project import SharePointCaptures, build_store
        from ontology.tests.test_mail_documents import StaticPortal, po

        self.tc, self.version_handle = tc, version_handle
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.capture_dir = self.root / "sharepoint"
        drive = tc.seeded_drive()
        # Quality-only: a lot note naming the purchase order (Ricardo alone on its ACL).
        drive.file("01FILED", "lot-note.txt", "01LOTS", b"Quality hold on PO100234 lot 7.",
                   mime="text/plain", acl=(tc.RICARDO,))
        store = SharePointCaptureStore(self.capture_dir, clock=tc.Clock())
        self.addCleanup(store.close)
        api, transport = graph_read.build_graph(tc.ENV, sender=drive, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        run = SharePointCapture(store, lambda d: SharePointReader(
            api, tc.capture_audience(), d, transport=transport, read_permissions=True),
            tc.CAPTURE).run(tc.scope())
        self.assertTrue(run["complete"], run)
        self.captured = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.timezone.utc)
        self.projection = self.root / "ontology.sqlite"
        portal = StaticPortal([po("public.app_purchase_order", {"id": "p1"},
                                  {"id": "p1", "po_number": "PO100234"})])
        with SharePointCaptures(self.capture_dir) as files:
            build_store(self.projection, schema_loader.load_schema(),
                        {"portal_api": portal, "sharepoint": files},
                        clock=lambda: self.captured + dt.timedelta(days=1),
                        post_project={"mentions": mentions.store_mention_links})
        self.mirror_audience = self.root / "mirror-audience.json"
        self.mirror_audience.write_text(json.dumps({
            "schema": AUDIENCE_SCHEMA, "tenant": "plx",
            "audiences": {"public.app_purchase_order": ["staff:quality", "staff:sales"]}}),
            "utf-8")

    def handle(self, item_id):
        return self.version_handle(self.tc.DRIVE, item_id, f'"{{{item_id}}},1"')

    def build(self, actor):
        """``(runtime, view)`` for one reader, in the calling thread (SQLite affinity)."""
        import datetime as dt

        from operating.case_runtime import LiveReaders
        runtime = CaseRuntime(self.root / "case-state", client=None,
                              audience=self.mirror_audience, tenant="plx",
                              live=LiveReaders(audience=self.tc.reader_audience()))
        soon = self.captured + dt.timedelta(hours=1)  # within the host's max capture age
        access = objects.host_access(runtime, self.projection, actor,
                                     sharepoint_capture_dir=self.capture_dir,
                                     clock=lambda: soon)
        return runtime, objects.ObjectWorkspace(access.tools(), actor,
                                                sources=CaseWorkspace(runtime, actor),
                                                access=access)

    def workspace(self, actor):
        runtime, view = self.build(actor)
        self.addCleanup(runtime.close)
        self.addCleanup(view.close)
        return view

    def served(self, actor):
        """``GET`` on a console serving ``actor``'s workspace for its fixture session
        (runtime and tools built in the serving thread)."""
        ready: Queue = Queue(maxsize=1)

        def run():
            try:
                runtime, view = self.build(actor)
                session = session_identity.fixture_session(
                    "plx", actor.principals, clearance=actor.clearance,
                    channel=session_identity.CHANNEL_CONSOLE)
                server = make_server(session=session, object_workspace=view,
                                     case_workspace=CaseWorkspace(runtime, session.actor))
                ready.put(server)
                server.serve_forever(poll_interval=0.05)
                server.server_close()
                view.close()
                runtime.close()
            except BaseException as error:  # surfaced to the test thread
                ready.put(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        server = ready.get(timeout=60)
        if isinstance(server, BaseException):
            raise server
        self.addCleanup(lambda: (server.shutdown(), thread.join(timeout=10)))

        def get(path):
            conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=60)
            conn.request("GET", path, headers={
                "Authorization": "Bearer " + server.operator_access_token})
            response = conn.getresponse()
            value = response.status, json.loads(response.read())
            conn.close()
            return value
        return get

    def test_a_file_shows_and_opens_for_its_reader_and_is_withheld_from_another(self):
        quality, sales = self.workspace(self.tc.QUALITY), self.workspace(self.tc.EMPLOYEE)
        found = quality.find("PO100234")["find"]
        po = next(c["object"]["object_id"] for c in found["candidates"]
                  if c["object"]["object_type"] == "PurchaseOrder")
        # Through the served console: each reader's own session and grant.
        get_quality, get_sales = self.served(self.tc.QUALITY), self.served(self.tc.EMPLOYEE)
        opened = "/api/object?id=" + quote(po, safe="")
        (_, wide), (_, narrow) = get_quality(opened), get_sales(opened)
        note, release = self.handle("01FILED"), self.handle("01FILEB")
        wide_docs = {d["citation"]: d for d in wide["documents"]["result"]["documents"]}
        narrow_docs = {d["citation"]: d for d in narrow["documents"]["result"]["documents"]}
        # Quality sees both files that name the order, each openable here.
        self.assertTrue({note, release} <= set(wide_docs), sorted(wide_docs))
        self.assertTrue(wide_docs[note]["openable"] and wide_docs[release]["openable"])
        # Files served from the capture in normal operation (the host's own as_of_capture
        # labels) explain as "as of capture", never as an unavailable live source.
        self.assertIn("as_of_capture", {g["reason"] for g in wide["documents"]["result"]["gaps"]})
        explained = wide["explanation"]
        self.assertNotEqual(explained["as_known_at"]["state"], "outage")
        self.assertNotIn("unavailable", explained["as_known_at"]["label"])
        self.assertNotIn("upstream_outage", {g["class"] for g in explained["gaps"]})
        [captured] = [g for g in explained["gaps"] if "as of capture" in g["text"]]
        self.assertEqual(captured["class"], "incomplete_source")
        self.assertTrue(any(d.startswith("as of capture 2") for d in captured["details"]))
        # Sales sees the file its identity is on; the quality-only note is withheld:
        # a marker in the documents answer, never its name, path or text.
        self.assertTrue(set(narrow_docs) <= set(wide_docs))
        self.assertIn(release, narrow_docs)
        self.assertTrue(narrow_docs[release]["openable"])
        self.assertNotIn(note, narrow_docs)
        self.assertIsNotNone(narrow["documents"]["result"].get("withheld"))
        self.assertNotIn("lot-note", json.dumps(narrow))
        self.assertNotIn("Quality hold", json.dumps(narrow))
        # Opening goes through the host's plx.read: the captured file, labelled as of
        # capture, for quality; not found for sales (the same as a missing file).
        source = "/api/object-source?handle="
        status, read = get_quality(source + quote(note, safe=""))
        self.assertEqual((status, read["kind"], read["status"]),
                         (200, "source_read", "resolved"))
        self.assertIn("Quality hold on PO100234", read["content"]["untrusted_text"])
        self.assertIn("as_of_capture", {gap["reason"] for gap in read["gaps"]})
        self.assertEqual(get_sales(source + quote(note, safe="")), (404, {"error": "not_found"}))
        status, read = get_sales(source + quote(release, safe=""))
        self.assertEqual((status, read["status"]), (200, "resolved"))
        # The decision is per item, not per family.
        with sales.access.request():
            decided = sales.access.decide([note, release])
        self.assertEqual(decided, {note: {"state": "withheld", "reason": objects.ITEM_REFUSED},
                                   release: None})


class ObjectViewBuildOnceTest(unittest.TestCase):
    """The workspace probe builds once: the rebuild and the claim sample are the projection
    check's, and a budget stop inside the build keeps the build's allowlisted partial
    counts (the fake clock and fixture wiring of ``tests/test_ontology_traverse_probe.py``)."""

    def test_a_build_over_half_the_budget_still_opens_views_without_a_rebuild(self):
        from scripts import object_workspace_probe as probe
        from tests.test_ontology_traverse_probe import consumer_run
        with tempfile.TemporaryDirectory() as tmp:
            code, document, built = consumer_run(probe.main, tmp, "--sample", "4",
                                                 before=600.0)
        self.assertEqual(built, [f"{tmp}/ontology.sqlite"])   # no rebuild
        self.assertNotIn("status", document)
        self.assertGreater(document["roots_opened"], 0)
        self.assertFalse(document["budget_exhausted"])

    def test_a_budget_stop_passes_the_builds_partial_counts_through(self):
        from scripts import object_workspace_probe as probe
        from tests.test_ontology_traverse_probe import (assert_build_partial_passed,
                                                        consumer_run)
        with tempfile.TemporaryDirectory() as tmp:
            code, document, built = consumer_run(probe.main, tmp, after=1000.0, stray=True)
        self.assertEqual(len(built), 1)
        assert_build_partial_passed(self, code, document)


# the SharePoint scope workspace.object_view captures (ontology.document_links' own)
SHAREPOINT_SCOPE = "examples/sharepoint_capture_scope.qcqa_aos.live-check.json"
# What workspace.object_view's expect may miss on the fixture world only: its live size
# floors and the live-read labels of injected transports.
FIXTURE_MISSES = frozenset({"roots_opened < 10", "denied_probed < 10",
                            "bc_capture.evidence_label != expected",
                            "sharepoint_capture.evidence_label != expected"})
# A view no captured SharePoint document reached: its SharePoint input is unproven.
NO_SHAREPOINT_INPUT = frozenset({"documents_linked_roots_sharepoint < 1",
                                 "sharepoint_withheld_read < 1"})


def sharepoint_scope_drive(files):
    """A FakeDrive (integrations.tests.test_capture_sharepoint) holding the folder
    ``SHAREPOINT_SCOPE`` captures with one synthetic text file per ``files``, and the
    scope's drive id; serve it with ``tc.SITE_DRIVE`` patched to that id."""
    from tests import test_live_wiring as wiring
    from integrations.tests import test_capture_sharepoint as tc

    target = json.loads((wiring.ROOT / SHAREPOINT_SCOPE).read_text("utf-8"))["targets"][0]
    drive_id = target["drive_id"]
    with mock.patch.object(tc, "SITE_DRIVE", drive_id):   # FakeDrive serves it
        drive = tc.FakeDrive()
        parent = tc.ROOT_ID
        for number, name in enumerate(target["folders"][0].strip("/").split("/"), 1):
            drive.folder(f"01DIR{number}", name, parent=parent, drive=drive_id)
            parent = f"01DIR{number}"
        for number, text in enumerate(files, 1):
            drive.file(f"01DOC{number}", f"packet-{number}.txt", parent,
                       text.encode("utf-8"), mime="text/plain", drive=drive_id)
    return drive, drive_id


def sharepoint_request(url: str) -> bool:
    """Whether a Graph request is the SharePoint capture's (its drive, site or download
    host), not the mail capture's."""
    from urllib.parse import urlsplit

    from integrations.tests import test_capture_sharepoint as tc

    parts = urlsplit(url)
    return parts.hostname == tc.SP_HOST or parts.path.startswith(
        ("/v1.0/drives/", "/v1.0/sites/"))


class ObjectViewLiveCheckTest(unittest.TestCase):
    """live check ``workspace.object_view``: the spec's own argv over the
    fixture world with the Portal, BC and Graph transports injected,
    its output checked by the verifier's own
    ``check_expectations``. Evidence: injected-transport."""

    # the second names a customer PO the spec's sample opens, so its related-mail panel
    # opens one
    MAIL_BODIES = ("Re: customer PO FXCPO-001, please confirm.",
                   "Customer PO FXCPO-SHARED-1 ships Friday.")
    # one synthetic text file per entry, filed in the scope's folder; naming no record
    FILES = ("Assembly packet scan, page 1.",)

    @classmethod
    @contextlib.contextmanager
    def fixture_sources(cls, spec_doc, *, bodies=MAIL_BODIES, lookup_refused=False,
                        files=FILES):
        """The fixture Portal, Graph mail and SharePoint transports, run from the repo
        root (the spec names its scope files relative to it); yields the environment to
        pass. The allowlisted mailbox holds one message per ``bodies``; ``lookup_refused``:
        every mailbox's directory lookup answers 403 (an app without User.Read.All). The
        SharePoint scope's drive holds its folder with one text file per ``files``."""
        import os

        from tests import test_live_wiring as wiring
        from integrations import graph_read
        from integrations.live_source_guard import BoundedBackoff
        from integrations.tests import test_capture_sharepoint as tc
        from integrations.tests.test_capture_mail import BOX_A, FakeGraph

        drive, drive_id = sharepoint_scope_drive(files)

        command = spec_doc["command"]
        scope = json.loads((wiring.ROOT / command[command.index("--mail-capture-scope") + 1])
                           .read_text("utf-8"))
        address = scope["mailboxes"][0]["address"]
        graph = FakeGraph()   # mailbox A serves as the spec's allowlisted mailbox
        graph.users[BOX_A].update(userPrincipalName=address, mail=address,
                                  proxyAddresses=[f"SMTP:{address}"])
        graph.folders[BOX_A]["sentitems"] = "AAMkFOLDER-SENT="
        graph.received = (datetime.now(timezone.utc) - timedelta(days=3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        for number, body in enumerate(bodies, 1):
            graph.put(BOX_A, f"MSG-{number}", f"CK-{number}", body=body)
        if lookup_refused:
            graph.directory_forbidden = {"*"}
        real_build = graph_read.build_graph

        def sender(method, url, headers, body, timeout, max_bytes):
            return (drive if sharepoint_request(url) else graph)(
                method, url, headers, body, timeout, max_bytes)

        def build(environ=None, **_kw):
            # The served host reads offline: no Graph credential (its people exclusion only).
            if environ is not None and not environ.get("MICROSOFT_GRAPH_CLIENT_ID"):
                return real_build(environ)
            return real_build(tc.ENV, sender=sender,
                              retry=BoundedBackoff(sleep=lambda s: None, jitter=lambda: 0.0))

        cwd = os.getcwd()
        with wiring.fixture_portal(), mock.patch.object(graph_read, "build_graph", build), \
                mock.patch.object(tc, "SITE_DRIVE", drive_id):
            os.chdir(wiring.ROOT)
            try:
                yield {**wiring.PORTAL_ENV, **wiring.BC_ENV, **tc.ENV}
            finally:
                os.chdir(cwd)

    def test_spec_runs_end_to_end_and_records_panel_denominators_only(self):
        from verify import live_verify as lv
        from tests import test_live_wiring as wiring
        from scripts import object_workspace_probe as probe

        spec_doc = wiring.spec("workspace.object_view")
        fake, books = wiring.fixture_bc()
        with tempfile.TemporaryDirectory() as tmp, self.fixture_sources(spec_doc) as environ:
            code, document = wiring.run_main(probe.main, wiring.argv_of(spec_doc, tmp),
                                             environ=environ, bc_sender=fake, bc_books=books)
            # A served row whose content no longer hashes to its citation is caught.
            from scripts import ontology_projection_probe as projection
            store = f"{tmp}/ontology.sqlite"
            with sqlite3.connect(objects.mirror_path(projection.served_state_path(store))) as conn:
                conn.execute("UPDATE row_current SET row_json = json_set(row_json, "
                             "'$.updated_at', 'tampered')")
            conn.close()
            # A run whose roots yield no identifier probes nobody for Find: never a pass.
            with mock.patch.object(probe, "identifier_queries", lambda *a: []):
                tampered = probe.check_views(projection.served_host(store, environ), sample=40,
                                             per_type=4, max_opens=5, budget_seconds=None)
            # A "no-grant" reader holding the capture principal finds every root (counted
            # leaks), and a next step with no why or no citation is counted unexplained.
            host = projection.served_host(store, environ)
            offered = {"status": "read", "intents": [
                {"capability": objects.FOLLOW_UP_CAPABILITY, "cited_records": ["r"],
                 "next_safe_step": {"detail": "a person approves the exact call"}},
                {"capability": "x.y", "cited_records": ["r"], "next_safe_step": {}},
                {"capability": "x.y", "cited_records": [],
                 "next_safe_step": {"detail": "a person approves"}}]}
            listed = {"status": "read", "cases_listing_full": False, "cases": [
                {"evaluation_status": "current"}, {"evaluation_status": "withheld"}]}
            with mock.patch.object(probe, "DENIED_PRINCIPAL", host.config.principal), \
                    mock.patch.object(objects.ObjectWorkspace, "_intents",
                                      lambda self, records: offered), \
                    mock.patch.object(objects.ObjectWorkspace, "_cases",
                                      lambda self, records: listed):
                leaky = probe.check_views(host, sample=40, per_type=4, max_opens=0,
                                          budget_seconds=None)
        self.assertEqual(code, 0, document)
        # Beside the live size floors and labels, only the SharePoint input may miss: the
        # mail names a record and the captured file none, so no SharePoint-linked root and
        # the run fails, never passes on the mail-linked root alone.
        failures = lv.check_expectations(spec_doc, code, document)
        self.assertLessEqual(set(failures), FIXTURE_MISSES | NO_SHAREPOINT_INPUT, failures)
        self.assertLessEqual(NO_SHAREPOINT_INPUT, set(failures), failures)
        self.assertEqual(document["sources"], ["bc", "graph_mail", "portal_api", "sharepoint"])
        self.assertGreaterEqual(document["documents_linked_roots"], 1)
        self.assertEqual((document["documents_linked_roots_sharepoint"],
                          document["sharepoint_withheld_probed"]), (0, 0))
        self.assertGreater(document["roots_opened"], 1)
        self.assertEqual(document["denied_not_found"], document["denied_probed"])
        self.assertEqual(document["denied_probed"], document["roots_opened"])
        # Every panel was measured, and an opened Portal row re-hashes to its citation.
        originals = document["originals"]
        self.assertGreater(originals["opened"], 0)
        self.assertEqual(originals["rehash_mismatch"], 0)
        self.assertGreater(originals["rehash_matched"], 0)
        self.assertEqual(tampered["originals"]["rehash_matched"], 0)
        self.assertEqual(tampered["originals"]["rehash_mismatch"], originals["rehash_matched"])
        self.assertEqual((tampered["find_denied_probed"], tampered["find_denied_leaks"]), (0, 0))
        self.assertIn("find_denied_probed < 1", lv.check_expectations(
            spec_doc, 0, {**document, **tampered}))
        # A Portal outage drops the find floor for Portal-bound roots only: with a BC root
        # searched as the no-grant reader it is blocked (its parts named), not fail; with
        # only Portal-bound roots searched the floor over the sources still up fails.
        by_source = document["find_denied_probed_by_source"]
        self.assertEqual(sum(by_source.values()), document["find_denied_probed"])
        self.assertEqual(document["find_denied_probed_unblocked"],
                         document["find_denied_probed"])
        self.assertTrue(by_source.get("portal_api") and by_source.get("bc"), by_source)
        portal_down = [{"part": "portal_sync", "source": "portal-workbench-api",
                        "http_status": 503, "error_code": "unavailable"}]
        for searched, failures in (({"bc": 1}, []), (
                {"portal_api": by_source["portal_api"]}, ["find_denied_probed_unblocked < 1"])):
            outage = {**document, "find_by_type": tampered["find_by_type"],
                      "find_denied_probed": sum(searched.values()),
                      "find_denied_probed_by_source": searched, "blocked_upstream": portal_down}
            outage["find_denied_probed_unblocked"] = probe.denied_probed_unblocked(outage)
            self.assertEqual([p["cause"] for p in lv.blocked_parts(outage)],
                             ["upstream_outage"])
            self.assertEqual(lv.check_expectations(spec_doc, 0, outage,
                                                   spec_doc["expect_blocked"]), failures)
        # A part naming a source the probe does not project from drops no root's floor.
        self.assertEqual(probe.denied_probed_unblocked(
            {"find_denied_probed_by_source": {"portal_api": 2, "unknown": 1},
             "blocked_upstream": [{"source": "elsewhere"}, "portal-workbench-api"]}), 3)
        for path in ("timeline.events", "relationships.edges", "values.quantity",
                     "next_steps.stages"):
            self.assertGreater(lv.resolve(document, path)[0], 0, path)
        self.assertIn("panel_read", document["documents"])
        # The spec asserts each WS panel non-empty on a root, and the record keeps them.
        asserted = {path.split(".")[1] for path in spec_doc["expect"]["json_min"]
                    if path.startswith("panels.")}
        self.assertEqual(asserted, set(probe.PANEL_COUNTS) - {
            "markers_conflicts", "markers_leads", "markers_withheld"})
        self.assertEqual(set(document["panels"]), set(probe.PANEL_COUNTS))
        for panel in sorted(asserted):
            self.assertGreater(document["panels"][panel]["roots"], 0, panel)
            self.assertIn(f"panels.{panel}.roots < 1", lv.check_expectations(
                spec_doc, 0, {**document, "panels": {
                    **document["panels"], panel: {"items": 0, "roots": 0}}}))
        self.assertEqual(lv._recorded(spec_doc, document)["panels"], document["panels"])
        # The capped record keeps every asserted path; the per-type find counts it may not
        # fit are summed in find_totals, which it keeps.
        self.assertEqual(document["find_totals"], {key: sum(
            t[key] for t in document["find_by_type"].values()) for key in probe.FIND_COUNTS})
        recorded = lv._recorded(spec_doc, document)
        kept = lv._cap({"fields": recorded}, spec_doc)["fields"]
        self.assertGreater(len(json.dumps(recorded)), lv.MAX_DETAIL_CHARS)
        expect = spec_doc["expect"]
        for path in expect["json_fields"]:
            self.assertTrue(lv.resolve(kept, path), path)
        for path in [*expect["json_min"], *expect["json_equals"]]:
            self.assertEqual(lv.resolve(kept, path), lv.resolve(recorded, path), path)
        # Find & open: a root is found at rank 1 by its own identifier, and the reader with
        # no grant gets no candidate for any root's identifiers.
        found = document["find_by_type"]
        self.assertGreater(sum(t["found_top1"] for t in found.values()), 0, found)
        self.assertEqual(sum(t["searched"] for t in found.values()),
                         document["find_denied_probed"])
        self.assertGreater(document["find_denied_probed"], 0)
        self.assertEqual(document["find_denied_leaks"], 0)
        # A root with no identifier to search by is explained: its type declares no own
        # identifier property, its card gaps the value as not a typed identifier of its
        # kind, as a value the projection could not parse (unparsed) or left out for a
        # cause it cannot tell (unread), or the record holds no value (no_value).
        own = probe.own_identifiers()
        for object_type, counts in found.items():
            self.assertEqual(sum(counts[k] for k in (
                "searched", "no_identifier_property", "identifier_invalid", "unparsed",
                "unread", "no_value")),
                             document["roots_by_type"][object_type], object_type)
            self.assertEqual(counts["no_identifier_property"] > 0, not own[object_type],
                             object_type)
        for key in ("identifier_invalid", "unparsed", "no_value"):
            self.assertGreater(sum(t[key] for t in found.values()), 0, (key, found))
        # With no action store the next steps are unmeasured, never counted as 0 explained.
        self.assertEqual(document["next_steps_explained"], {
            "status": "unmeasured", "reason": "intents_not_configured",
            "roots": document["roots_opened"]})
        self.assertGreater(leaky["find_denied_probed"], 0)
        self.assertEqual(leaky["find_denied_leaks"], leaky["find_denied_probed"])
        opened = leaky["roots_opened"]
        self.assertEqual(leaky["next_steps_explained"], {
            "status": "measured", "follow_ups_offered": opened, "follow_ups_explained": opened,
            "follow_ups_unexplained": 0, "next_steps_offered": 2 * opened,
            "next_steps_explained": 0, "next_steps_unexplained": 2 * opened})
        # Each open's case panel is read; every listed case counts under one evaluation.
        evaluations = document["case_evaluations"]
        self.assertEqual(sum(evaluations[state] for state in objects.EVALUATION_STATES),
                         evaluations["cases"], evaluations)
        self.assertFalse([key for key in evaluations if key.startswith("panel_")])
        self.assertEqual(leaky["case_evaluations"], {
            **dict.fromkeys(objects.EVALUATION_STATES, 0), "cases": 2 * opened,
            "roots_with_cases": opened, "listing_full": 0, "current": opened,
            "withheld": opened})
        # Counts and names only: nothing the verifier records names a record.
        fields = lv._recorded(spec_doc, document)
        self.assertEqual(fields["next_steps_explained"]["reason"], "intents_not_configured")
        # The verifier requires and records the evaluation status counts.
        self.assertEqual(fields["case_evaluations"], evaluations)
        self.assertIn("missing case_evaluations", lv.check_expectations(
            spec_doc, 0, {key: value for key, value in document.items()
                          if key != "case_evaluations"}))
        recorded = json.dumps(fields)
        for value in ("PO92", "PO#9", "FXBC", "FXPI", "FXCPO", "fx-po-", "api://", "bc://",
                      "graph-mail://", "petrasoap", ":portal."):
            self.assertNotIn(value, recorded)

    def test_a_captured_sharepoint_file_naming_a_record_fills_the_documents_panel(self):
        """The spec captures the bounded SharePoint scope ontology.document_links reads and
        projects it with the same document steps, laid out in the served state: with mail
        naming no record, a captured file naming a customer PO links it, the sampled root
        opens its documents panel and the run passes expect (no blocked part). The no-grant
        reader is served none of the files (its capture read-back and every open)."""
        from verify import live_verify as lv
        from tests import test_live_wiring as wiring
        from integrations import capture_sharepoint
        from scripts import object_workspace_probe as probe
        from scripts import ontology_projection_probe as projection

        spec_doc = wiring.spec("workspace.object_view")
        fake, books = wiring.fixture_bc()
        with tempfile.TemporaryDirectory() as tmp, self.fixture_sources(
                spec_doc, bodies=("Thanks, see you on Friday.",),
                files=("Customer PO FXCPO-SHARED-1 ships Friday.",)) as environ:
            code, document = wiring.run_main(probe.main, wiring.argv_of(spec_doc, tmp),
                                             environ=environ, bc_sender=fake, bc_books=books)
            served = projection.served_state_path(f"{tmp}/ontology.sqlite")
            held = capture_sharepoint.store_exists(served)
            # A "records only" reader that holds the folder (the capture principal) is
            # named and opened the SharePoint documents: counted leaks.
            host = projection.served_host(f"{tmp}/ontology.sqlite", environ)
            with mock.patch.object(projection, "MAIL_WITHHELD_PRINCIPAL",
                                   host.config.principal):
                leaky = probe.check_views(host, sample=40, per_type=4, max_opens=5,
                                          budget_seconds=None)
        self.assertEqual(code, 0, document)
        self.assertGreaterEqual(document["mention_links"], 1)   # the file's, not the mail's
        self.assertGreaterEqual(document["documents_linked_roots"], 1, document)
        # the mail names no record: every linked root is SharePoint's
        self.assertEqual(document["documents_linked_roots_sharepoint"],
                         document["documents_linked_roots"])
        self.assertGreaterEqual(document["panels"]["documents_opened"]["roots"], 1)
        self.assertEqual(lv.blocked_parts(document), [])
        self.assertNotIn("partial_upstream", document)   # a complete capture
        # timing the first live run measures: the capture's and the build's seconds
        self.assertIsInstance(document["sharepoint_capture"]["seconds"], float)
        self.assertIsInstance(document["build_seconds"], float)
        failures = lv.check_expectations(spec_doc, code, document)
        self.assertLessEqual(set(failures), FIXTURE_MISSES, failures)
        # A reader granted the records (BC by the live audience, the Portal by the served
        # mirror audience) but not the SharePoint folder reads each SharePoint-linked root
        # and is named or opened none of its SharePoint documents.
        self.assertEqual(document["sharepoint_withheld_probed"],
                         document["documents_linked_roots_sharepoint"])
        self.assertEqual((document["sharepoint_withheld_read"],
                          document["sharepoint_withheld_leaks"]),
                         (document["sharepoint_withheld_probed"], 0), document)
        self.assertGreater(leaky["sharepoint_withheld_leaks"], 0, leaky)
        self.assertIn("sharepoint_withheld_leaks != expected",
                      lv.check_expectations(spec_doc, 0, {**document, **leaky}))
        self.assertTrue(held)   # where the served host and the probe's views read it
        self.assertEqual(document["sources"], ["bc", "graph_mail", "portal_api", "sharepoint"])
        capture = document["sharepoint_capture"]
        self.assertEqual((capture["evidence_label"], capture["complete"],
                          capture["files_current"], capture["read_back_leaks"]),
                         ("injected-transport", True, 1, 0), capture)
        self.assertEqual(document["denied_leaks"], 0)
        self.assertEqual(document["denied_not_found"], document["denied_probed"])
        self.assertNotIn("FXCPO", json.dumps(lv._recorded(spec_doc, document)))

    def test_sharepoint_capture_fails_closed_and_is_never_served_stale(self):
        """Each SharePoint capture outcome over the spec's own argv: not configured (no
        SHAREPOINT_SITE_ID) reads nothing; one that raises is a part naming its class, its
        message never printed; incomplete with no file is a part that drops no mail or
        record source's find floor; a read-back leak exits 1 whatever expect says;
        incomplete holding files is served and marked partial_upstream, never blocking; an
        entry at served-state/sharepoint-capture the probe did not link is refused and
        left alone; and a later run without SharePoint serves no earlier run's capture."""
        from verify import live_verify as lv
        from tests import test_live_wiring as wiring
        from integrations import capture_sharepoint
        from scripts import object_workspace_probe as probe
        from scripts import ontology_projection_probe as projection

        spec_doc = wiring.spec("workspace.object_view")
        real_cli = capture_sharepoint.run_cli
        secret = "synthetic-capture-message-7f3a"

        def edited(edit):
            def run_cli(*args, **kwargs):
                report = real_cli(*args, **kwargs)
                edit(report)
                return report
            return run_cli

        def incomplete(report, files=None):
            report["runs"][-1].update(complete=False, incomplete_reasons=["time_budget"])
            if files is not None:
                report["summary"]["files_current"] = files

        def raises(*_args, **_kwargs):
            raise RuntimeError(secret)

        def leaked(report):
            report["leak_check"] = {**report["leak_check"], "synthetic": 1}

        def strip_sharepoint(argv):
            kept, skip = [], False
            for arg in argv:
                if skip:
                    skip = False
                elif arg.startswith("--sharepoint-"):
                    skip = True
                else:
                    kept.append(arg)
            return kept

        def run(*, no_site=False, cli=None, occupied=False, again_without=False):
            fake, books = wiring.fixture_bc()
            calls = mock.Mock(wraps=cli or real_cli)
            with tempfile.TemporaryDirectory() as tmp, \
                    self.fixture_sources(spec_doc) as environ, \
                    mock.patch.object(capture_sharepoint, "run_cli", calls):
                if no_site:
                    environ = {k: v for k, v in environ.items() if k != "SHAREPOINT_SITE_ID"}
                served = projection.served_state_path(f"{tmp}/ontology.sqlite")
                entry = Path(served, capture_sharepoint.STORE_DIR)
                if occupied:   # a directory nobody linked: never served, never removed
                    entry.mkdir(parents=True)
                    (entry / "kept.txt").write_text("synthetic", "utf-8")
                argv = wiring.argv_of(spec_doc, tmp)
                code, document = wiring.run_main(probe.main, argv, environ=environ,
                                                 bc_sender=fake, bc_books=books)
                if again_without:
                    code, document = wiring.run_main(
                        probe.main, strip_sharepoint(argv), environ=environ,
                        bc_sender=fake, bc_books=books)
                left = (entry.is_dir() and not entry.is_symlink()
                        and (entry / "kept.txt").is_file())
                return (code, document, calls.call_count, left,
                        capture_sharepoint.store_exists(served))

        def sharepoint_parts(document, key="blocked_upstream"):
            return [part for part in document.get(key) or ()
                    if part.get("part") == "sharepoint_capture"]

        def floors_kept(document):
            return (document["find_denied_probed"] >= 1 and
                    document["find_denied_probed_unblocked"]
                    == document["find_denied_probed"])

        with self.subTest("no SHAREPOINT_SITE_ID: a not_configured part, nothing read"):
            code, document, calls, _, held = run(no_site=True)
            self.assertEqual(code, 0, document)
            self.assertEqual(calls, 0)
            self.assertEqual([p.get("error_code") for p in sharepoint_parts(document)],
                             ["not_configured"])
            self.assertNotIn("sharepoint", document["sources"])
            self.assertTrue(floors_kept(document), document)
            self.assertFalse(held)
        with self.subTest("a capture that raises: a part, its message never printed"):
            code, document, calls, _, held = run(cli=raises)
            self.assertEqual(code, 0, document)
            self.assertEqual(calls, 1)
            self.assertEqual(len(sharepoint_parts(document)), 1, document)
            self.assertNotIn(secret, json.dumps(document))
            self.assertNotIn("sharepoint", document["sources"])
            self.assertTrue(floors_kept(document), document)
            self.assertFalse(held)
        with self.subTest("incomplete with no file: a part, the floors kept"):
            code, document, _, _, held = run(cli=edited(lambda r: incomplete(r, files=0)))
            self.assertEqual(code, 0, document)
            self.assertEqual([p.get("error_code") for p in sharepoint_parts(document)],
                             ["time_budget"])
            self.assertNotIn("sharepoint", document["sources"])
            self.assertTrue(floors_kept(document), document)
            self.assertFalse(held)
        with self.subTest("a read-back leak exits 1"):
            code, document, _, _, _ = run(cli=edited(leaked))
            self.assertEqual(document["sharepoint_capture"]["read_back_leaks"], 1)
            self.assertEqual(code, 1, document)
        with self.subTest("incomplete holding files: served, marked, not blocking"):
            code, document, _, _, held = run(cli=edited(incomplete))
            self.assertEqual(code, 0, document)
            self.assertTrue(held)
            self.assertIn("sharepoint", document["sources"])
            self.assertEqual(sharepoint_parts(document), [])
            self.assertEqual(lv.blocked_parts(document), [])
            self.assertEqual([p.get("error_code") for p in
                              sharepoint_parts(document, "partial_upstream")],
                             ["time_budget"], document)
            failures = lv.check_expectations(spec_doc, code, document)
            # served, but its file names no record: the SharePoint input stays unproven
            self.assertLessEqual(set(failures), FIXTURE_MISSES | NO_SHAREPOINT_INPUT,
                                 failures)
            self.assertIn("partial_upstream", lv._recorded(spec_doc, document))
        with self.subTest("an entry the probe did not link: refused, typed, left alone"):
            code, document, _, left, _ = run(occupied=True)
            self.assertEqual(code, 1, document)
            self.assertEqual((document.get("error"), document.get("build_error"),
                              document.get("code")),
                             ("projection_build_failed", "ServedCaptureConflict",
                              "served_capture_not_a_link"), document)
            self.assertTrue(left)
        with self.subTest("a later run without SharePoint removes the served link"):
            code, document, calls, _, held = run(again_without=True)
            self.assertEqual(code, 0, document)
            self.assertEqual(calls, 1)   # the first run's capture only
            self.assertNotIn("sharepoint", document["sources"])
            self.assertFalse(held)

    def test_each_marker_kind_counts_apart_and_withheld_alone_never_satisfies_the_gap_floor(self):
        """gap-7942ca70a8df: gaps, conflicts, leads and withheld markers are four panels;
        a root showing only a withheld marker leaves markers_gaps empty, so the spec's
        panels.markers_gaps.roots >= 1 fails on it."""
        from verify import live_verify as lv
        from tests import test_live_wiring as wiring
        from scripts import object_workspace_probe as probe

        spec_doc = wiring.spec("workspace.object_view")

        def measure(view):
            counts = {"open_errors": {}, "panels": {
                panel: {"items": 0, "roots": 0} for panel in probe.PANEL_COUNTS}}
            probe.measure_panels(view, mock.Mock(), counts, 5, 0)
            return counts["panels"]
        edge = lambda tier: {"tier": tier}   # noqa: E731
        withheld_only = measure({
            "card": {"withheld": {"total": 1}},
            "originals": {"withheld": {"total": 2}},
            "documents": {"result": {"documents": [{"not_openable_reason": "x"}]}}})
        self.assertEqual({k: v["items"] for k, v in withheld_only.items()
                          if k.startswith("markers_")},
                         {"markers_gaps": 0, "markers_conflicts": 0, "markers_leads": 0,
                          "markers_withheld": 4})
        document = {"panels": withheld_only}
        self.assertIn("panels.markers_gaps.roots < 1",
                      lv.check_expectations(spec_doc, 0, document))
        each = measure({
            "gaps": {"total": 3}, "conflicts": [{}, {}],
            "around": {"hops": [{"result": {"edges": [
                edge("verified"), edge("corroborated"), edge("stored_by_text_join"),
                edge("lead"), edge("contradicted")]}}]}})
        self.assertEqual({k: v["items"] for k, v in each.items() if k.startswith("markers_")},
                         {"markers_gaps": 3, "markers_conflicts": 2, "markers_leads": 2,
                          "markers_withheld": 0})
        self.assertNotIn("panels.markers_gaps.roots < 1", lv.check_expectations(
            spec_doc, 0, {"panels": each}))
        # A link on two hops counts once; two links between the same pair count twice.
        link = lambda link_id, link_type: {   # noqa: E731
            "link_id": link_id, "link_type": link_type, "from": "a", "to": "b", "tier": "lead"}
        hops = measure({"around": {"hops": [
            {"result": {"edges": [link("l1", "billed_to"), link("l2", "same_record_as")]}},
            {"result": {"edges": [link("l1", "billed_to")]}}]}})
        self.assertEqual((hops["markers_leads"]["items"], hops["relationships_tiered"]["items"]),
                         (2, 2))
        self.assertNotIn("markers_shown", probe.PANEL_COUNTS)
        self.assertNotIn("panels.markers_shown.roots", spec_doc["expect"]["json_min"])

    def test_each_panel_counts_on_the_root_that_shows_it_and_a_withheld_root_adds_none(self):
        from tests import test_live_wiring as wiring
        from operating.host import projection_path
        from scripts import object_workspace_probe as probe
        from scripts import ontology_projection_probe as projection

        spec_doc = wiring.spec("workspace.object_view")
        fake, books = wiring.fixture_bc()
        with tempfile.TemporaryDirectory() as tmp, self.fixture_sources(spec_doc) as environ:
            code, document = wiring.run_main(probe.main, wiring.argv_of(spec_doc, tmp),
                                             environ=environ, bc_sender=fake, bc_books=books)
            store = f"{tmp}/ontology.sqlite"
            served = projection_path(projection.served_host(store, environ).state_dir)
            roots = probe.sample_roots(str(served), sample=40, per_type=4)

            def views(picked):
                with mock.patch.object(probe, "sample_roots", lambda *a, **k: picked):
                    return probe.check_views(projection.served_host(store, environ), sample=40,
                                             per_type=4, max_opens=5, budget_seconds=None)
            # ten roots one at a time, and each customer PO (where the related mail is)
            ten = roots[:10]
            alone = {root: views([root])["panels"] for root in dict.fromkeys(
                [*ten, *(root for root in roots if root[0] == "CustomerPurchaseOrder")])}
            together = views(ten)["panels"]
            one = probe.check_views(projection.served_host(store, environ), sample=1, per_type=1,
                                    max_opens=5, budget_seconds=None)
            linked = probe.document_linked_roots(str(served))
            documented = [root for root, panels in alone.items()
                          if panels["documents_opened"]["roots"]]
            hidden = documented[0]
            with sqlite3.connect(served) as conn:
                conn.execute("UPDATE objects SET audience_key = 'not-granted:panel-probe' "
                             "WHERE object_id = ?", (hidden[1],))
            conn.close()
            withheld, rest = views([hidden]), views(roots)
        self.assertEqual(code, 0, document)
        panels = document["panels"]
        asserted_panels = {path.split(".")[1] for path in spec_doc["expect"]["json_min"]
                           if path.startswith("panels.")}
        self.assertEqual(set(panels), set(probe.PANEL_COUNTS))
        # Each panel is non-empty on some root; ten roots one at a time and at once count
        # the same items and roots (none lost or counted twice), and 40 never more roots
        # than opened.
        for panel in probe.PANEL_COUNTS:
            if panel in asserted_panels or panel.startswith("markers_"):
                self.assertGreater(panels[panel]["roots"], 0, panel)
            self.assertLessEqual(panels[panel]["roots"], document["roots_opened"])
            for key in ("items", "roots"):
                self.assertEqual(sum(alone[root][panel][key] for root in ten),
                                 together[panel][key], (panel, key))
        self.assertEqual(alone[hidden]["documents_opened"], {"items": 1, "roots": 1})
        # gap-07d4b4d2292c: the roots a mail document is linked to are sampled first, each
        # once, and exactly those show the panel; a one-root sample (the hash order over
        # types alone takes a root no document names) still takes one and opens its mail.
        self.assertEqual(roots[:len(linked)], linked)
        self.assertEqual(len(set(roots)), len(roots))
        self.assertEqual(set(documented), set(linked))
        self.assertEqual(document["documents_linked_roots"], len(linked))
        self.assertEqual(panels["documents_opened"]["roots"], len(linked))
        self.assertEqual((one["roots_sampled"], one["documents_linked_roots"]), (1, 1))
        self.assertEqual(one["panels"]["documents_opened"], {"items": 1, "roots": 1})
        # Withheld, that root shows no panel and adds nothing: its mail is opened no more.
        self.assertEqual((withheld["withheld_roots"], withheld["roots_opened"]), (1, 0))
        self.assertEqual(withheld["panels"], {panel: {"items": 0, "roots": 0}
                                              for panel in probe.PANEL_COUNTS})
        others = len(linked) - 1
        self.assertEqual(rest["panels"]["documents_opened"], {"items": others, "roots": others})
        self.assertEqual((rest["withheld_roots"], rest["workspace_errors"]), (1, 0))

    def test_a_bc_outage_still_needs_a_portal_root_searched_as_the_no_grant_reader(self):
        from verify import live_verify as lv
        from tests import test_live_wiring as wiring
        from scripts import object_workspace_probe as probe

        def bc_down(*_):   # every BC request, the token's included, is refused
            raise ConnectionRefusedError("BC is down")
        spec_doc = wiring.spec("workspace.object_view")
        runs = []
        for queries in (probe.identifier_queries, lambda *a: []):
            with tempfile.TemporaryDirectory() as tmp, self.fixture_sources(spec_doc) as env, \
                    mock.patch.object(probe, "identifier_queries", queries):
                runs.append(wiring.run_main(probe.main, wiring.argv_of(spec_doc, tmp),
                                            environ=env, bc_sender=bc_down))
        (code, searched), (_, unsearched) = runs
        self.assertEqual(code, 0, searched)
        self.assertEqual([(p["part"], p["cause"]) for p in lv.blocked_parts(searched)],
                         [("bc_capture", "upstream_outage")])
        self.assertNotIn("bc", searched["sources"])
        self.assertGreater(searched["find_denied_probed_by_source"].get("portal_api", 0), 0)
        self.assertEqual(lv.check_expectations(spec_doc, 0, searched,
                                               spec_doc["expect_blocked"]), [])
        # No Portal root searched as the no-grant reader: the BC outage keeps that floor.
        self.assertEqual(unsearched["find_denied_probed_unblocked"], 0)
        self.assertEqual(lv.check_expectations(spec_doc, 0, unsearched,
                                               spec_doc["expect_blocked"]),
                         ["find_denied_probed_unblocked < 1"])

    def test_a_portal_outage_is_blocked_and_a_fixture_host_refuses_the_bc_only_store(self):
        """With the Portal not configured, or answering 503, the
        store is projected from BC and mail beside a fresh empty mirror, served with
        portal_api not projected, and the run is blocked by the outage (expect_blocked
        passes), never a fail. A fixture host still refuses that live store."""
        from verify import live_verify as lv
        from tests import test_live_wiring as wiring
        from integrations import workbench_read_api
        from integrations.http import SourceHttpError
        from operating import mcp_server
        from scripts import object_workspace_probe as probe

        real = workbench_read_api.WorkbenchReadApiClient

        def unavailable(url, key, **kwargs):
            def fetch(url, headers, **_):
                raise SourceHttpError("read-only source GET failed: HTTPError", status=503,
                                      error_code="unavailable")
            return real(url, key, fetch=fetch, **kwargs)
        spec_doc = wiring.spec("workspace.object_view")
        for configured in (False, True):
            fake, books = wiring.fixture_bc()
            with self.subTest(portal="503" if configured else "not configured"), \
                    tempfile.TemporaryDirectory() as tmp, \
                    self.fixture_sources(spec_doc) as environ, \
                    mock.patch.object(workbench_read_api, "WorkbenchReadApiClient",
                                      unavailable):
                if not configured:
                    environ = {k: v for k, v in environ.items() if k not in wiring.PORTAL_ENV}
                code, document = wiring.run_main(probe.main, wiring.argv_of(spec_doc, tmp),
                                                 environ=environ, bc_sender=fake,
                                                 bc_books=books)
                self.assertEqual(code, 0, document)
                parts = [p for p in document["blocked_upstream"] if p["part"] == "portal_mirror"]
                self.assertEqual(len(parts), 1, document["blocked_upstream"])
                if configured:
                    self.assertEqual((parts[0]["http_status"], parts[0]["error_code"]),
                                     (503, "unavailable"))
                    self.assertIn({"part": "portal_mirror", "cause": "upstream_outage"},
                                  [{k: p[k] for k in ("part", "cause")}
                                   for p in lv.blocked_parts(document)])
                self.assertEqual(document["sources"], ["bc", "graph_mail", "sharepoint"])
                self.assertEqual(document["workspace_errors"], 0)
                self.assertGreater(document["roots_opened"], 0)
                self.assertEqual(lv.check_expectations(spec_doc, 0, document,
                                                       spec_doc["expect_blocked"]), [])
                # A fixture host (its own state: no BC capture) refuses the live store.
                errors = io.StringIO()
                with self.assertRaises(SystemExit) as exited, \
                        contextlib.redirect_stderr(errors):
                    mcp_server.session_server([
                        "--state-dir", f"{tmp}/fixture-host", "--source-mirror-audience",
                        str(wiring.ROOT / "examples/source_mirror_audience.example.json"),
                        "--fixture", "--principal", "staff:purchasing",
                        "--ontology-store", f"{tmp}/ontology.sqlite"])
                self.assertEqual(exited.exception.code, 2)
                self.assertIn("ontology store refused: unknown", errors.getvalue())

    def test_no_document_link_never_passes_and_a_refused_mailbox_lookup_reads_blocked(self):
        """gap-d2abdf27e7f4, gap-07d4b4d2292c: the documents panel is asserted only over a
        sampled root a document is linked to. With mail captured but naming no record, no
        root is document-linked: a typed source_gap part, never a pass. With every
        mailbox's directory lookup refused the build's mail.mailbox_lookup part reaches
        the record and the run passes expect_blocked (blocked, never pass)."""
        from verify import live_verify as lv
        from tests import test_live_wiring as wiring
        from scripts import object_workspace_probe as probe

        spec_doc = wiring.spec("workspace.object_view")
        runs = {}
        for name, options in (("unlinked", {"bodies": ("Thanks, see you on Friday.",)}),
                              ("refused", {"lookup_refused": True})):
            fake, books = wiring.fixture_bc()
            with tempfile.TemporaryDirectory() as tmp, \
                    self.fixture_sources(spec_doc, **options) as environ:
                runs[name] = wiring.run_main(probe.main, wiring.argv_of(spec_doc, tmp),
                                             environ=environ, bc_sender=fake, bc_books=books)
        for name, (code, document) in runs.items():
            with self.subTest(name):
                self.assertEqual(code, 0, document)
                self.assertEqual(document["documents_linked_roots"], 0)
                self.assertEqual(document["panels"]["documents_opened"]["roots"], 0)
                parts = lv.blocked_parts(document)
                self.assertTrue(parts, "no document link must never read pass")
                self.assertEqual(lv.check_expectations(spec_doc, code, document,
                                                       spec_doc["expect_blocked"]), [])
                # the full expect names why, not only the empty panel
                self.assertIn("documents_linked_roots < 1",
                              lv.check_expectations(spec_doc, code, document))
                recorded = lv._recorded(spec_doc, document)
                self.assertEqual(recorded["documents_linked_roots"], 0)
                self.assertIn("mention_links", recorded)
        unlinked, refused = runs["unlinked"][1], runs["refused"][1]
        self.assertEqual([(p["part"], p["source"], p["cause"])
                          for p in lv.blocked_parts(unlinked)],
                         [("documents.linked_roots", "microsoft-graph", "source_gap")])
        self.assertGreater(unlinked["mail_messages_seen"], 0)
        self.assertEqual(unlinked["mention_links"], 0)
        self.assertEqual([(p["part"], p["http_status"], p["cause"])
                          for p in lv.blocked_parts(refused)],
                         [("mail.mailbox_lookup", 403, "host_misconfiguration")])
        self.assertEqual(refused["mail_messages_seen"], 0)
        self.assertIn("forbidden", refused["mail_capture_reasons"])
        # the refusal is no outage of the record sources: their find floor still holds
        self.assertEqual(refused["find_denied_probed_unblocked"],
                         refused["find_denied_probed"])

    def test_an_ambiguous_first_rank_is_recall_at_five_and_counted_apart_from_top1(self):
        from scripts import object_workspace_probe as probe

        class Finder:
            def __init__(self, answers):
                self.answers = answers

            def find(self, query):
                status, ids = self.answers.get(query, ("not_found", []))
                return {"find": {"status": status, "candidates": [
                    {"object": {"object_id": i}} for i in ids]}}

        denied = Finder({})
        report = {"find_by_type": {}, "find_denied_probed": 0, "find_denied_leaks": 0,
                  "workspace_errors": 0}
        granted = Finder({"A": ("ambiguous", ["root-1", "other"]),
                          "B": ("found", ["root-2"]),
                          "C": ("ambiguous", ["root-2", "other"])})
        for root, queries in (("root-1", ["A"]), ("root-2", ["C", "B"])):
            probe.measure_find("T", root, queries, granted, denied, report, {})
        counts = report["find_by_type"]["T"]
        # root-2's unambiguous answer wins over its ambiguous first rank.
        self.assertEqual({k: counts[k] for k in ("searched", "found_top1",
                                                 "found_top1_ambiguous", "found_top5",
                                                 "ambiguous")},
                         {"searched": 2, "found_top1": 1, "found_top1_ambiguous": 1,
                          "found_top5": 2, "ambiguous": 1})
        self.assertEqual(report["find_denied_probed"], 2)


class SessionReplayLiveCheckTest(unittest.TestCase):
    """live check ``session.replay``: the spec's own argv over the fixture
    world with the Portal, BC and Graph transports injected (``ObjectViewLiveCheckTest``'s
    wiring), its printed summary checked by the verifier's own ``check_expectations``; then,
    over the same built state, a cited row changed between record and replay, and a
    "withheld" reader who holds the recorder's grant, each fail it. Evidence:
    injected-transport (the planner is the probe's deterministic one, no model)."""

    @staticmethod
    @contextlib.contextmanager
    def rewriting_reextraction():
        """Re-extraction that leaves every earlier citation resolving to other content."""
        from operating import case_originals

        reextract, resolve = (case_originals.OriginalStore.reextract,
                              case_originals.OriginalStore.resolve)
        done = []

        def reextract_then_rewrite(store, *args, **kwargs):
            done.append(True)
            return reextract(store, *args, **kwargs)

        def resolve_rewritten(store, *args, **kwargs):
            view = resolve(store, *args, **kwargs)
            return {**view, "text": "rewritten"} if done and "text" in view else view

        with mock.patch.object(case_originals.OriginalStore, "reextract",
                               reextract_then_rewrite), \
                mock.patch.object(case_originals.OriginalStore, "resolve", resolve_rewritten):
            yield

    def test_spec_keeps_and_replays_sessions_and_withholds_them_from_the_case_reader(self):
        from verify import live_verify as lv
        from tests import test_live_wiring as wiring
        from scripts import object_workspace_probe as probe
        from scripts import ontology_projection_probe as projection

        spec_doc = wiring.spec("session.replay")
        fake, books = wiring.fixture_bc()
        with tempfile.TemporaryDirectory() as tmp, \
                ObjectViewLiveCheckTest.fixture_sources(spec_doc) as environ:
            code, document = wiring.run_main(probe.main, wiring.argv_of(spec_doc, tmp),
                                             environ=environ, bc_sender=fake, bc_books=books)
            self.assertEqual(code, 0, document)
            self.assertEqual(lv.check_expectations(spec_doc, code, document), [])
            replayed = document["session_replay"]
            self.assertEqual((replayed["sessions_recorded"], replayed["replay_identical"],
                              replayed["withheld_reader_markers"]), (3, 3, 3))
            self.assertGreater(replayed["citations"], 0)
            self.assertNotIn("api://", json.dumps(document))   # counts only
            store = f"{tmp}/ontology.sqlite"
            mirror = objects.mirror_path(projection.served_state_path(store))
            replay = objects.ObjectWorkspace.replay

            def changed_first(workspace, request):
                # Every captured row changes in place (same handles) before the replay.
                with sqlite3.connect(mirror) as db:
                    for table in ("row_version", "row_current"):
                        db.execute(f"UPDATE {table} SET row_json = json_set(row_json, "
                                   "'$.notes', 'changed after the session')")
                db.close()
                return replay(workspace, request)

            runs = {}
            for name, patch in (
                    ("changed", mock.patch.object(objects.ObjectWorkspace, "replay",
                                                  changed_first)),
                    ("rewritten", self.rewriting_reextraction()),
                    ("granted", mock.patch.object(probe, "REPLAY_PRINCIPAL",
                                                  projection.served_host(store, environ)
                                                  .config.principal))):
                with patch:
                    runs[name] = probe.check_session_replay(
                        projection.served_host(store, environ), f"{tmp}/cases-{name}", sessions=3,
                        sample=40, per_type=4, budget_seconds=None)
        changed, granted = runs["changed"], runs["granted"]
        # DOC-08: re-extraction made one new version and every earlier citation resolved to
        # its original content; one that resolves to other content afterwards is counted.
        kept = runs["granted"]["reextraction"]
        self.assertEqual((kept["attached"], kept["new_version"], kept["earlier_citations"] > 0,
                          kept["changed_earlier"], kept["bytes_changed"], kept["refused"]),
                         (1, 1, True, 0, 0, 0))
        rewritten = runs["rewritten"]["reextraction"]
        self.assertGreater(rewritten["changed_earlier"], 0)
        self.assertFalse(probe.session_replay_passed(runs["rewritten"]))
        self.assertIn("session_replay.reextraction.changed_earlier != expected",
                      lv.check_expectations(spec_doc, 0, {
                          **document, "session_replay": runs["rewritten"]}))
        self.assertGreater(changed["divergences_by_kind"].get("citation_content_changed", 0),
                           0)
        self.assertLess(changed["replay_identical"], changed["sessions_recorded"])
        self.assertIn("session_replay.divergences != expected",
                      lv.check_expectations(spec_doc, 1, {**document,
                                                          "session_replay": changed}))
        self.assertFalse(probe.session_replay_passed(changed))
        # A "withheld" reader holding the recorder's grant sees every session: counted.
        self.assertEqual(granted["withheld_reader_shown"], granted["sessions_recorded"])
        self.assertFalse(probe.session_replay_passed(granted))


# The probe's typed causes of ObjectViewFailedOpenTest's four failed roots that are no
# permission withhold.
OPEN_ERRORS = {"workspace_KeyError": 1, "workspace_ValueError": 1,
               "not_found_no_current_version": 1, "not_found_gate_error": 1}


class ObjectViewFailedOpenTest(unittest.TestCase):
    """The workspace probe samples the store whatever the reader's grant, so a failed root
    open is classified by the host's own access decision. ``plx.object`` answers one
    ``object_not_found`` for a withheld root and a broken one, so the host types it
    (``CaseHostTools.not_found_reason``) and only a permission reason counts as withheld.
    Six sampled roots fail to open over the fixture world: ``hidden`` lies outside the
    reader's audience and ``marked`` carries a marking the reader lacks on its latest
    version (withheld); ``stale`` has a ``recorded_at`` that does not parse (no current
    version) and ``gated`` is an original whose item gate raises (broken); ``broken`` is
    readable and a panel raises the very ``KeyError("object_not_found")`` an invisible card
    raises; ``undecided`` is readable, a panel raises and the host's decision call itself
    fails. Evidence: injected-transport."""

    @classmethod
    def setUpClass(cls):
        from operating import ontology_host
        from operating.host import projection_path
        from scripts import object_workspace_probe as probe
        from scripts import ontology_projection_probe as projection
        from tests.test_ontology_traverse_probe import consumer_run

        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        code, _, _ = consumer_run(probe.main, tmp.name, "--sample", "1")
        assert code == 0, code
        store = f"{tmp.name}/ontology.sqlite"
        host = projection.served_host(store)
        served = projection_path(host.state_dir)
        cls.roots = probe.sample_roots(str(served), sample=40, per_type=4)
        hidden, broken, undecided, stale, marked, gated = (oid for _, oid in cls.roots[:6])
        cls.named = dict(hidden=hidden, broken=broken, stale=stale, marked=marked,
                         gated=gated, absent="Nothing:none:absent")
        gate_handle = "original://object-view-probe-gate"
        with sqlite3.connect(served) as conn:
            conn.execute("UPDATE objects SET audience_key = 'not-granted:object-view-probe' "
                         "WHERE object_id = ?", (hidden,))
            conn.execute("UPDATE object_versions SET markings = '[\"finance\"]' "
                         "WHERE object_id = ?", (marked,))
            conn.execute("UPDATE object_versions SET recorded_at = 'not-a-time', "
                         "superseded_at = NULL WHERE object_id = ?", (stale,))
            conn.execute("UPDATE object_versions SET l1_handle = ? WHERE object_id = ?",
                         (gate_handle, gated))
        conn.close()
        process, card = objects.ObjectWorkspace._process, ot.OntologyTools._object
        runtime_gate = ontology_host.item_gate_from_case_runtime

        def raising_process(self, object_id):
            if object_id == broken:
                raise KeyError("object_not_found")
            if object_id == undecided:
                raise ValueError("panel_failed")
            return process(self, object_id)

        def card_or_outage(self, arguments, actor):
            # the decision call (no detailed card) on ``undecided`` gets no answer
            if arguments["object_id"] == undecided and "response_format" not in arguments:
                raise ot.ToolError("store_unavailable", "no answer")
            return card(self, arguments, actor)

        def raising_gate(runtime):
            fallback = runtime_gate(runtime)

            def gate(actor, handle):
                if handle == gate_handle:
                    raise RuntimeError("item_gate_unreachable")
                return fallback(actor, handle)
            return gate

        with mock.patch.object(objects.ObjectWorkspace, "_process", raising_process), \
                mock.patch.object(ot.OntologyTools, "_object", card_or_outage), \
                mock.patch.object(ontology_host, "item_gate_from_case_runtime", raising_gate):
            cls.report = probe.check_views(projection.served_host(store), sample=40,
                                           per_type=4, max_opens=0, budget_seconds=None)
            # The reader's own answers and the host's typed reasons, through the console's
            # workspace as the probe opens it.
            host = projection.served_host(store)
            reader = projection.reader_actor(host)
            with host.served_tools(reader) as tools:
                workspace = case_console.object_workspace_for(tools.runtime, served, reader,
                                                              state_dir=host.state_dir)
                try:
                    with workspace.access.request():
                        cls.answers = {name: workspace.tools["plx.object"]["handler"](
                            {"object_id": oid}, reader) for name, oid in cls.named.items()}
                        gated_reader = tools.runtime.employee_gate(reader)
                        cls.reasons = {name: workspace.access.host.not_found_reason(
                            oid, gated_reader) for name, oid in cls.named.items()}
                finally:
                    workspace.close()

    def test_a_withheld_root_is_no_error_and_a_broken_or_readable_one_keeps_its_cause(self):
        report = self.report
        self.assertGreaterEqual(len(self.roots), 7)   # at least one root opens
        self.assertEqual(report["roots_sampled"], len(self.roots))
        self.assertEqual(report["withheld_roots"], 2)
        self.assertEqual(report["workspace_errors"], 4)
        self.assertEqual(report["open_errors"], OPEN_ERRORS)
        self.assertEqual(report["roots_opened"], len(self.roots) - 6)
        self.assertEqual(report["denied_leaks"], 0)
        self.assertEqual(report["denied_not_found"], report["denied_probed"])

    def test_the_host_types_not_found_and_the_reader_sees_one_answer(self):
        self.assertEqual(self.reasons, {
            "hidden": "withheld", "marked": "withheld", "stale": "no_current_version",
            "gated": "gate_error", "absent": "absent", "broken": None})
        # Withheld, broken and absent read alike to the reader: no reason reaches it.
        not_found = {"code": "object_not_found",
                     "message": "no such object is visible to this reader",
                     "hint": "resolve the identifier with plx.find first"}
        for name, answer in self.answers.items():
            if name != "broken":
                self.assertEqual(answer["error"], not_found, name)
        self.assertEqual(self.answers["broken"]["object"]["object_id"], self.named["broken"])

    def test_the_spec_requires_and_keeps_the_typed_cause_under_the_detail_cap(self):
        from verify import live_verify as lv
        from tests import test_live_wiring as wiring
        from scripts import object_workspace_probe as probe

        spec = wiring.spec("workspace.object_view")
        document = {**self.report, "probe": probe.PROBE}
        for expect in (spec["expect"], spec["expect_blocked"]):
            failures = lv.check_expectations(spec, 0, document, expect)
            self.assertFalse([f for f in failures if "open_errors" in f
                              or "withheld_roots" in f], failures)
            failures = lv.check_expectations(spec, 0, {
                k: v for k, v in document.items() if k not in ("open_errors", "withheld_roots")},
                expect)
            self.assertIn("missing open_errors", failures)
            self.assertIn("missing withheld_roots", failures)
        # A live-sized answer (40 types found by) overflows the cap: the cause still stays.
        wide = {**document, "find_by_type": {f"Type{n}": dict.fromkeys(probe.FIND_COUNTS, 1)
                                             for n in range(40)}}
        detail = lv._cap({"fields": lv._recorded(spec, wide)})
        self.assertIn("fields_truncated", detail)
        self.assertEqual(detail["fields"]["open_errors"], OPEN_ERRORS)
        self.assertEqual((detail["fields"]["workspace_errors"],
                          detail["fields"]["withheld_roots"]), (4, 2))
        self.assertNotIn("not-granted", json.dumps(detail))



class RecordOfTest(unittest.TestCase):
    def test_version_is_the_pinned_capture(self):
        self.assertEqual(objects.version_of("api://public.x/%7B%7D@g1-2#row.a"), "g1-2")
        self.assertIsNone(objects.version_of("original://abc#page=2"))
        self.assertEqual(objects.version_of("graph-mail://a@b.example/m1@ck"), "ck")
        self.assertIsNone(objects.version_of("graph-mail://a@b.example/m1"))

    def test_capture_and_fragment_are_dropped_but_mailbox_addresses_kept(self):
        self.assertEqual(objects.record_of("api://public.x/%7B%7D@g1-2#row.a"), "api://public.x/%7B%7D")
        self.assertEqual(objects.record_of("original://abc#page=2"), "original://abc")
        self.assertEqual(objects.record_of("graph-mail://a@b.example/m1@ck"),
                         "graph-mail://a@b.example/m1")
        self.assertEqual(objects.record_of("graph-mail://a@b.example/m1"),
                         "graph-mail://a@b.example/m1")



class SessionBcReuseTest(unittest.TestCase):
    """A session consumer reuses the producer's complete BC units (gap-fbb6c2dba861).

    ``workspace.object_view`` and ``ontology.mentions`` read the session state, but when the
    session projection (``ontology.session_projection``) captured BC and then ran out of budget
    in its projection before it committed, each consumer captured BC cold into its own served
    state: ``units_reused`` 0 and some 1,250 s of its 2,600 s budget gone before its
    projection. Now the consumer starts from the producer's capture and reads
    only what the session did not complete.

    Evidence label: ``injected-transport``. The verifier session (the live verifier
    ``SessionState``) runs the specs' own argv over the fixture Portal, the BC fake and the
    Graph fake; nothing live (both specs stay live-unverified)."""

    def run_session(self, *, fresh_shift: timedelta = timedelta(0), corrupt: bool = False):
        """One verifier session: the session mirror, the session projection producer (its
        budget runs out in its projection, after its BC capture), then workspace.object_view
        by its own argv with a fresh BC fake. Returns (producer report, consumer exit
        code, consumer report, the consumer's BC requests by path)."""
        from verify import live_verify as lv
        from tests import test_live_wiring as wiring
        from integrations import workbench_read_api
        from ontology import project_incremental
        from scripts import object_workspace_probe as workspace
        from scripts import ontology_projection_probe as projection
        from scripts.mirror_measure import ordered_full_coverage

        specs = lv.valid_specs(lv.load_specs(lv.ROOT / lv.SPECS_DIR_NAME))
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp, wiring.fixture_bound(), \
                ObjectViewLiveCheckTest.fixture_sources(specs["workspace.object_view"]) as env:
            os.chdir(lv.ROOT)
            session = lv.SessionState(lv.ROOT)
            try:
                mirror = specs["mirror.full_coverage"]
                plan = session.plan(mirror, specs)
                ordered_full_coverage(workbench_read_api.WorkbenchReadApiClient(
                    wiring.FIXTURE_URL, wiring.PORTAL_ENV["PLX_WORKBENCH_API_KEY"]),
                    session.bases(mirror, plan)["/mirror.sqlite"] + "/mirror.sqlite",
                    resolve_sample=0)
                session.record(mirror, plan, {"id": "mirror.full_coverage", "result": "pass",
                                              "exit_code": 0,
                                              "detail": {"fields": {"relation_count": 1}}})
                since = datetime.strptime(session.started_at, "%Y-%m-%dT%H:%M:%S.%fZ"
                                          ).replace(tzinfo=timezone.utc) + fresh_shift
                environ = {**env, lv.CAPTURE_FRESH_SINCE_ENV:
                           since.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}

                def run(spec_id, main, fake, books):
                    spec_doc = specs[spec_id]
                    step = session.plan(spec_doc, specs)
                    argv = lv._expand(spec_doc["command"][3:], f"{tmp}/{spec_id}", None,
                                      session.bases(spec_doc, step))
                    code, document = wiring.run_main(main, argv, environ=environ,
                                                     bc_sender=fake, bc_books=books)
                    session.record(spec_doc, step, {
                        "id": spec_id, "result": "pass" if code == 0 else "fail",
                        "exit_code": code, "detail": {"fields": document}})
                    return code, document, session.bases(spec_doc, step)

                fake, books = wiring.fixture_bc()
                # The producer's budget runs out in its projection, after its BC capture
                # (a filled store the verifier shares, never committed).
                with mock.patch.object(project_incremental, "build_generation",
                                       side_effect=projection._Budget.Spent("projection")):
                    _, produced, bases = run("ontology.session_projection", projection.main,
                                             fake, books)
                seed = Path(bases["/projection"], "projection", projection.SESSION_CAPTURES,
                            projection.SESSION_BC)
                self.assertTrue(seed.is_file())
                if corrupt:
                    seed.write_bytes(b"not a database" * 100)
                consumer, books = wiring.fixture_bc()
                code, document, _ = run("workspace.object_view", workspace.main,
                                        consumer, books)
            finally:
                os.chdir(cwd)
                session.close()
        reads = [call[1] for call in consumer.calls
                 if "login.microsoftonline.com" not in call[1]]
        return produced, code, document, reads

    def test_a_consumer_reuses_every_unit_the_session_producer_completed(self):
        produced, code, document, reads = self.run_session()
        # The producer captured BC in this session and did not commit its projection.
        self.assertEqual((produced["status"], produced["phase"]),
                         ("budget_exhausted", "projection"))
        made = produced["bc_capture"]
        self.assertGreater(made["units_complete"], 0, made)
        captured = document["bc_capture"]
        # Every unit the producer completed is reused but each company's own record,
        # which every call takes from the company listing it reads anyway (no request).
        self.assertEqual(captured["units_reused"] + captured["companies_captured"],
                         made["units_complete"], captured)
        self.assertEqual(captured["units_complete"], made["units_complete"])
        self.assertTrue(captured["session_seeded"])
        # Every unit was complete: the consumer read only the company listing, no unit.
        self.assertEqual(made["units_complete"], made["units_total"], made)
        self.assertTrue(reads)
        self.assertTrue(all(url.split("?")[0].endswith("/companies") for url in reads), reads)
        self.assertEqual(document["session_projection"]["mode"], "cold")
        self.assertIn("bc", document["sources"])
        # The reused capture is served to readers as before: nothing reaches no grant.
        self.assertEqual((code, document["denied_leaks"], document["find_denied_leaks"],
                          document["workspace_errors"]), (0, 0, 0, 0), document)
        self.assertGreater(document["roots_opened"], 0)
        # The spec holds on this output (only the labels tell the fake from a live read,
        # and the fixture's SharePoint file names no record) and its record keeps the
        # reuse counts.
        from verify import live_verify as lv
        from tests import test_live_wiring as wiring
        spec_doc = wiring.spec("workspace.object_view")
        self.assertEqual(set(lv.check_expectations(spec_doc, code, document)),
                         {"bc_capture.evidence_label != expected",
                          "sharepoint_capture.evidence_label != expected"}
                         | NO_SHAREPOINT_INPUT)
        recorded = lv._recorded(spec_doc, document)["bc_capture"]
        self.assertEqual((recorded["units_reused"], recorded["session_seeded"]),
                         (captured["units_reused"], True), recorded)

    def test_a_capture_outside_the_freshness_window_or_unreadable_is_read_again(self):
        # Session start after the producer's runs began: copied, but nothing reused.
        _, code, late, reads = self.run_session(fresh_shift=timedelta(hours=1))
        self.assertEqual(code, 0, late)
        self.assertTrue(late["bc_capture"]["session_seeded"])
        self.assertEqual(late["bc_capture"]["units_reused"], 0)
        self.assertEqual(late["bc_capture"]["units_complete"], late["bc_capture"]["units_total"])
        self.assertTrue(any(not url.split("?")[0].endswith("/companies") for url in reads))
        # A producer file SQLite refuses: not copied, the capture runs cold, never an error.
        _, code, cold, _ = self.run_session(corrupt=True)
        self.assertEqual(code, 0, cold)
        self.assertFalse(cold["bc_capture"]["session_seeded"])
        self.assertEqual(cold["bc_capture"]["units_reused"], 0)
        self.assertEqual(cold["bc_capture"]["units_complete"], cold["bc_capture"]["units_total"])



class IdentityExplanationTest(unittest.TestCase):
    """The object view's "why" names one identity candidate per pair of records: a link
    stored in both directions is one candidate, never listed twice."""

    PO = 'PurchaseOrder:portal.app_purchase_order:{"id":"po-synth-5447"}'
    FM = 'PurchaseOrder:portal.fm_generated_purchase_order:{"id":5447}'

    def identity_why(self, *edges):
        out = explanations.explain_object({"object_id": self.PO, "card": {
            "object": {"object_id": self.PO}, "values": [], "identity": list(edges)}})
        return [w for w in out["why"] if w["kind"] == "identity"]

    def edge(self, a, b, tier="verified"):
        return {"link_type": "PurchaseOrder.same_record_as", "from": a, "to": b, "tier": tier,
                "reason": [{"feature": "pair", "agrees": True}], "why_not": []}

    def test_a_link_stored_both_ways_is_one_candidate_with_each_tier_counted(self):
        both = self.identity_why(self.edge(self.FM, self.PO), self.edge(self.PO, self.FM))
        self.assertEqual(len(both), 1)
        self.assertEqual(both[0]["tiers"], {"verified": 1})
        differing = self.identity_why(self.edge(self.FM, self.PO), self.edge(self.PO, self.FM, "lead"))
        self.assertEqual(len(differing), 1)
        self.assertEqual(differing[0]["tiers"], {"verified": 1, "lead": 1})
        other = 'PurchaseOrder:portal.app_purchase_order:{"id":"po-synth-9"}'
        self.assertEqual(len(self.identity_why(self.edge(self.FM, self.PO), self.edge(other, self.FM))), 2)

if __name__ == "__main__":
    unittest.main()
