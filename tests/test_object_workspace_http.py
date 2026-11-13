"""Object workspace routes of the operator console over the generic ontology tools.

The case console's fixture world (``operator_api.tests.case_fixtures.seed``: synthetic rows read
through the real read-only clients over in-process fakes) is projected into an ontology
store (``ontology.project.build_store`` over its source mirror) and served through
``plx.find``/``plx.object``/``plx.around``/``plx.timeline``/``plx.process`` for one
host-configured reader, as ``python3 -m operator_api.tests.case_fixtures --seed-fixtures`` does.
Citations open through the same case runtime. Evidence label: isolated-fixture /
injected-transport; no network, nothing live.
"""

from __future__ import annotations

import dataclasses
from http.client import HTTPConnection
import json
from pathlib import Path
from queue import Queue
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import quote

from ontology.core import Actor
from operating.action_path import FIXTURE_CAPABILITY, ActionPath, FixtureSystem
from operating.case_runtime import CaseRuntime
from operating.cases import CaseStore
from operator_api.workspace import case_console
from operator_api.tests import case_fixtures
from operator_api.workspace import object_workspace as objects
from operating import explanations
from operating import session_identity
from operator_api.workspace.case_workspace import CaseWorkspace
from operator_api.tests.console_harness import make_server
from operator_api.workspace.session_journal import ask_counts, investigate_counts

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

    def test_a_kept_reader_policy_is_decided_again_on_any_change_and_per_session(self):
        """The reader's policy is decided once per session while what it is decided from
        is unchanged (a page's five requests pay for it once), and again at once when a
        grant is revoked or regained, for another session, or once kept too long."""
        from operating.case_tools_host import CaptureCaseHostTools

        audience_path = self.tmp / "kept-policy-audience.json"
        shutil.copyfile(self.state / case_fixtures.MIRROR_AUDIENCE_FILE, audience_path)
        runtime = CaseRuntime(self.state, client=None, audience=audience_path, tenant="plx")
        self.addCleanup(runtime.close)
        drives = self.tmp / "kept-policy-sharepoint"
        access = objects.host_access(runtime, self.store, Actor("plx", OPS),
                                     sharepoint_capture_dir=drives)
        self.addCleanup(access.close)
        sessions = [session_identity.fixture_session(
            "plx", OPS, channel=session_identity.CHANNEL_CONSOLE) for _ in range(2)]
        self.assertNotEqual(sessions[0], sessions[1])
        relation = "api:public.app_purchase_order"
        decided = mock.patch.object(CaptureCaseHostTools, "policy_now", autospec=True,
                                    side_effect=CaptureCaseHostTools.policy_now)

        def grant(session):
            access.bind(session)
            with access.request():
                return access.grant()[0](relation)
        with decided as policy_now:
            self.assertTrue(grant(sessions[0]))
            self.assertTrue(grant(sessions[0]))
            self.assertEqual(policy_now.call_count, 1)   # kept for the session
            self.assertTrue(grant(sessions[1]))           # another session: its own
            self.assertEqual(policy_now.call_count, 2)
            # revoked in the mirror audience: the very next request reads without it
            audience = json.loads(audience_path.read_text("utf-8"))
            audience["audiences"] = {r: [p for p in principals if p not in OPS]
                                     for r, principals in audience["audiences"].items()}
            audience_path.write_text(json.dumps(audience), "utf-8")
            self.assertFalse(grant(sessions[0]))
            self.assertEqual(policy_now.call_count, 3)
            self.assertFalse(grant(sessions[0]))
            self.assertEqual(policy_now.call_count, 3)
            shutil.copyfile(self.state / case_fixtures.MIRROR_AUDIENCE_FILE, audience_path)
            self.assertTrue(grant(sessions[0]))           # regained: at once too
            # a capture store written since (its drives' or mailboxes' recorded ACLs): again
            store = drives / "sharepoint-capture" / "capture.db"
            store.parent.mkdir(parents=True)
            store.write_bytes(b"")
            before = policy_now.call_count
            grant(sessions[0])
            self.assertEqual(policy_now.call_count, before + 1)
            store.write_bytes(b"x")
            grant(sessions[0])
            self.assertEqual(policy_now.call_count, before + 2)
            # kept at most POLICY_KEPT_SECONDS, whatever it was decided from
            with mock.patch.object(objects, "POLICY_KEPT_SECONDS", -1.0):
                store.write_bytes(b"xy")
                grant(sessions[0])
                grant(sessions[0])
            self.assertEqual(policy_now.call_count, before + 4)

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
        before and after a change); the next request of the session keeps it while
        nothing it is decided from changed (a change decides it again:
        ``test_a_kept_reader_policy_is_decided_again_on_any_change_and_per_session``)."""
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
            self.assertEqual(len(decided), 1)
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
        original = CaptureCaseHostTools.policy_inputs

        def swap_in_flight(host, reader):   # the host publishes mid-answer
            if newer.exists():
                newer.replace(served)
            return original(host, reader)
        with mock.patch.object(CaptureCaseHostTools, "policy_inputs", swap_in_flight):
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
