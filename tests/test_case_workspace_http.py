"""Read-only case workspace routes of the operator console (injected transport + synthetic scan).

The AO-26005B case is investigated through the real MCP case tools over the in-process
``FakeWorkbench`` (``operating/tests/test_ao_26005b_case.py``), the synthetic scan is attached
through ``plx.case_attach_original``, a proposal is recorded with ``plx.case_propose`` and the
evaluation is stored in a ``PlaybookReportStore``. The console then reads the same state
directory from a fresh, offline ``CaseRuntime`` (a later session) for one host-configured
actor. Nothing here is live data.
"""

from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
from queue import Queue
import shutil
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import quote

from ontology.core import Actor
from operating.case_runtime import CaseRuntime
from operating import explanations
from operating.cases import actor_label
from operating.tests import test_ao_26005b_case as ao_tests
from operating.tests import test_ingestion as ingest_fixture
from operating.tests.test_case_originals import FIXTURE, local_ocr_available
from operating.tests.test_playbook_eval import AoEvalCase
from operator_ui import case_workspace as case_workspace_module
from operator_ui import case_console
from operator_ui import case_plain
from operator_ui.tests import case_fixtures
from operator_ui.case_console import report_store_for
from operator_ui.case_workspace import CaseWorkspace
from operator_ui.server import make_server
from operating.session_identity import CHANNEL_CONSOLE, fixture_session

OPS_QUALITY = ao_tests.OPS_QUALITY
OPS = ao_tests.OPS
OUTSIDER = frozenset({"staff:finance"})


class CaseWorkspaceHttpTest(AoEvalCase):
    def setUp(self) -> None:
        super().setUp()
        self.out = self.investigate()
        self.case_id = self.out["case_id"]
        runtime = self.server.case_runtime
        self.page = None
        if local_ocr_available():
            inbox = runtime.originals.inbox / "staff:quality"
            inbox.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(FIXTURE, inbox / ao_tests.SCAN)
            attached = self.call("plx.case_attach_original",
                                 {"case_id": self.case_id, "inbox_name": ao_tests.SCAN})
            self.original = attached["original"]
            [self.page] = self.original["page_citations"]
        ao_row = self.call("plx.source_citation", {"citation": self.out["cite"]["ao"]})
        self.call("plx.case_propose", {
            "case_id": self.case_id, "capability": "portal.assembly_order.set_header_quantities",
            "payload": {"assembly_order": "AO-26005B", "qtytotal": "1509", "evidence": {
                "qtytotal": {"citation": self.out["cite"]["item"], "field": "quantity"}}},
            "preconditions": ["Quality confirms the counts on the signed scan"],
            "citations": [self.out["cite"]["ao"], self.out["cite"]["item"]],
            "expected_effect": {"relation": ao_row["relation"], "key": ao_row["key"],
                                "values": {"qtytotal": "1509"}}})
        self.report = self.evaluate(self.case_id)
        recorder = Actor("plx", OPS_QUALITY)
        self.reports_path = self.h.state / "playbook-reports.sqlite"
        report_store_for(runtime, self.reports_path, [recorder]).record(
            self.report, evaluated_by=actor_label(recorder), transcript_source="none")
        self.events = self.server.case_runtime.cases.get(
            recorder, self.case_id)["integrity"]["events"]

    def serve(self, principals, *, workspace: bool = True):
        """Console for one actor; its runtime is built in the serving thread (SQLite affinity)."""
        ready: Queue = Queue(maxsize=1)

        def run():
            try:
                runtime = CaseRuntime(self.h.state, client=None, audience=self.h.audience_path,
                                      tenant="plx")
                case_workspace = session = None
                if workspace:
                    # Fixture console: an explicit fixture session identity.
                    session = fixture_session("plx", principals, channel=CHANNEL_CONSOLE)
                    case_workspace = CaseWorkspace(
                        runtime, session.actor,
                        report_store=report_store_for(runtime, self.reports_path))
                server = make_server(case_workspace=case_workspace,
                                     session=session)
                ready.put(server)
                server.serve_forever(poll_interval=0.05)
                server.server_close()
                runtime.close()
            except BaseException as error:  # surfaced to the test thread
                ready.put(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        server = ready.get(timeout=30)
        if isinstance(server, BaseException):
            raise server
        self.addCleanup(lambda: (server.shutdown(), thread.join(timeout=10)))
        port, token = server.server_address[1], server.operator_access_token

        def request(path, *, method="GET", auth=True, raw=False):
            conn = HTTPConnection("127.0.0.1", port, timeout=30)
            headers = {"Authorization": "Bearer " + token} if auth and token else {}
            if method == "POST":
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body="{}" if method == "POST" else None, headers=headers)
            response = conn.getresponse()
            body = response.read()
            value = ((response.status, dict(response.getheaders()), body) if raw
                     else (response.status, json.loads(body)))
            conn.close()
            return value
        return request

    @staticmethod
    def citation_path(handle):
        return "/api/case-citation?handle=" + quote(handle, safe="")

    def get(self, request, path):
        status, body = request(path)
        self.assertEqual(status, 200, body)
        return body

    def test_case_reader_sees_the_whole_workspace_read_only(self):
        request = self.serve(OPS_QUALITY)
        self.assertEqual(request("/api/cases", auth=False)[0], 401)
        self.assertEqual(request(f"/api/case/{self.case_id}", auth=False)[0], 401)
        self.assertTrue(self.get(request, "/api/session")["case_workspace"])
        listed = self.get(request, "/api/cases")
        self.assertEqual([item["case_id"] for item in listed["cases"]], [self.case_id])
        self.assertEqual(listed["business_system_writes"], 0)

        view = self.get(request, f"/api/case/{self.case_id}")
        self.assertEqual((view["read_only"], view["business_system_writes"]), (True, 0))
        self.assertEqual(view["roots"], ["AO-26005B", "OR1000151885"])
        self.assertTrue(view["integrity"]["chain_ok"])
        self.assertEqual(len(view["timeline"]), view["integrity"]["events"])
        self.assertEqual(view["findings"]["withheld"], [])
        supported = view["findings"]["supported"]
        self.assertTrue(supported)
        link_fact = supported[0]
        self.assertEqual({cite["status"] for cite in link_fact["citations"]}, {"resolved"})
        self.assertEqual({cite["mirror"]["status"] for cite in link_fact["citations"]},
                         {"current"})
        # Gaps carry their states, including the visible unknown.
        self.assertEqual(sorted(gap["state"] for gap in view["gaps"]),
                         ["absent_in_source", "not_ingested", "unknown"])
        # The proposal is a record that needs a person's approval in Mission Control.
        [proposal] = view["proposals"]
        self.assertEqual(proposal["capability"], "portal.assembly_order.set_header_quantities")
        self.assertEqual(proposal["approval"]["where"], "Mission Control")
        self.assertEqual((proposal["approval"]["approved_here"],
                          proposal["approval"]["dispatched"]), (False, False))
        # Cited records and the source dates read from them (not from constants).
        relations = {record["relation"] for record in view["records"]}
        self.assertIn("public.assembly_order", relations)
        self.assertIn("silver.assembly_order_items", relations)
        dates = {(item["relation"], item["column"]): item["at"] for item in view["source_dates"]}
        self.assertEqual(dates[("public.order_entry", "datereq")], "2026-01-01")
        self.assertEqual(dates[("public.assembly_order", "datemanuf")], "2026-01-05")
        self.assertEqual(dates[("silver.production_tasks", "date_start")], "2026-01-07")
        ao_record = next(record for record in view["records"]
                         if record["handle"] == self.out["cite"]["ao"])
        self.assertIn({"kind": "proposal", "id": proposal["id"]}, ao_record["cited_by"])
        # The stored evaluation, summarised as stored.
        evaluation = view["evaluation"]
        self.assertEqual(evaluation["status"], "stored")
        self.assertEqual(evaluation["report_sha256"], self.report["report_sha256"])
        self.assertEqual(evaluation["passed_all"], self.report["summary"]["passed_all"])
        self.assertEqual(evaluation["passed_all_clean"], False)
        self.assertEqual(evaluation["passed_all_qualification"], "authority_not_checked")
        self.assertEqual(evaluation["checks"], self.report["summary"]["checks"])
        self.assertNotIn("recorded_by", evaluation)
        self.assertNotIn("staff:", json.dumps(evaluation))  # never the recorder's principals
        self.assertEqual(view["truncated"], {})
        self.assertIsNone(view["paging"])

        # A record chip resolves through the citation route to the cited row.
        row = self.get(request, self.citation_path(self.out["cite"]["ao"]))
        self.assertEqual((row["kind"], row["relation"], row["row"]["nameamo_c"]),
                         ("row", "public.assembly_order", "AO-26005B"))
        self.assertEqual(row["status"]["status"], "current")
        if self.page is not None:
            [original] = view["originals"]
            self.assertEqual(original["sha256"], self.original["sha256"])
            self.assertEqual(original["pages"], [{"citation": self.page, "page": 1,
                                                  "text_source": "ocr"}])
            text = self.get(request, self.citation_path(self.page))
            self.assertEqual((text["kind"], text["text_source"]), ("original", "ocr"))
            self.assertIn("machine-read", text["note"])
            self.assertIn("PLANNED", text["text"])
        # Reading changed nothing: no case event was appended.
        again = self.get(request, f"/api/case/{self.case_id}")
        self.assertEqual(again["integrity"]["events"], self.events)

    def test_a_narrower_case_member_sees_only_what_its_audience_resolves(self):
        request = self.serve(OPS)
        self.assertEqual([item["case_id"] for item in self.get(request, "/api/cases")["cases"]],
                         [self.case_id])
        view = self.get(request, f"/api/case/{self.case_id}")
        self.assertEqual({record["relation"].split(".")[0] for record in view["records"]},
                         {"public"})
        self.assertFalse([item for item in view["source_dates"]
                          if item["relation"].startswith("silver.")])
        self.assertTrue(view["findings"]["withheld"])
        for item in view["findings"]["withheld"]:
            self.assertIsNone(item["statement"])
            self.assertFalse([cite for cite in item["citations"] if "handle" in cite])
        [proposal] = view["proposals"]
        self.assertIsNone(proposal["capability"])  # cites a silver row: withheld
        self.assertEqual(view["originals"], [])
        self.assertNotIn("silver.", json.dumps(view))
        # The stored evaluation was computed over rows this reader cannot resolve: nothing
        # about it (counts, check ids, limits, recorder) reaches the narrower member.
        self.assertEqual(set(view["evaluation"]), {"status", "reason"})
        self.assertEqual(view["evaluation"]["status"], "withheld")
        for leaked in ("item_cited", "damage_cited", "filling_cited", "originals_identity",
                       "passed_all", self.report["report_sha256"]):
            self.assertNotIn(leaked, json.dumps(view))
        self.assertNotIn("staff:", json.dumps(view["evaluation"]))
        for handle in [self.out["cite"]["item"]] + ([self.page] if self.page else []):
            status, body = request(self.citation_path(handle))
            self.assertEqual((status, body), (404, {"error": "not_found"}))

    def test_an_actor_outside_the_case_audience_is_denied_like_a_missing_case(self):
        request = self.serve(OUTSIDER)
        self.assertEqual(self.get(request, "/api/cases")["cases"], [])
        denied = request(f"/api/case/{self.case_id}")
        missing = request("/api/case/case-00000000000000000000000000000000")
        self.assertEqual(denied, (404, {"error": "not_found"}))
        self.assertEqual(denied, missing)
        self.assertEqual(request(self.citation_path(self.out["cite"]["ao"])),
                         (404, {"error": "not_found"}))

    def test_routes_are_read_only_and_validate_their_input(self):
        request = self.serve(OPS_QUALITY)
        for path in (f"/api/case/{self.case_id}", "/api/cases", "/api/case-citation"):
            status, _ = request(path, method="POST")
            self.assertNotEqual(status, 200)
        self.assertEqual(request("/api/cases?status=pending")[0], 400)
        self.assertEqual(request("/api/cases?limit=0")[0], 400)
        self.assertEqual(request("/api/cases?owner=x")[0], 400)
        self.assertEqual(request("/api/case-citation?handle=" + quote("https://x", safe=""))[0],
                         400)
        self.assertEqual(request("/api/case-citation")[0], 400)
        self.assertEqual(request(f"/api/case/{'x' * 65}")[0], 404)
        self.assertEqual(self.get(request, "/api/cases?status=closed")["cases"], [])
        view = self.get(request, f"/api/case/{self.case_id}")
        self.assertEqual(view["integrity"]["events"], self.events)

    def test_sections_page_every_list_and_large_lists_are_capped_with_flags(self):
        request = self.serve(OPS_QUALITY)
        whole = self.get(request, f"/api/case/{self.case_id}")
        timeline, offset = [], 0
        while offset is not None:
            page = self.get(request, f"/api/case/{self.case_id}?section=timeline"
                                     f"&offset={offset}&limit=4")
            self.assertEqual((page["view"], page["section"], page["total"]),
                             ("section", "timeline", whole["integrity"]["events"]))
            timeline.extend(page["items"])
            offset = page["next_offset"]
        self.assertEqual(timeline, whole["timeline"])
        findings = self.get(request, f"/api/case/{self.case_id}?section=findings")["items"]
        self.assertEqual(sorted(item["id"] for item in findings),
                         sorted(item["id"] for items in whole["findings"].values()
                                for item in items))
        self.assertEqual(request(f"/api/case/{self.case_id}?section=secrets")[0], 400)
        self.assertEqual(request(f"/api/case/{self.case_id}?offset=2")[0], 400)
        self.assertEqual(request(f"/api/case/{self.case_id}?section=gaps&limit=0")[0], 400)
        with mock.patch.object(case_workspace_module, "SECTION_LIMIT", 3):
            capped = self.get(request, f"/api/case/{self.case_id}")
        self.assertEqual(capped["truncated"]["timeline"],
                         {"shown": 3, "total": whole["integrity"]["events"]})
        self.assertEqual(capped["timeline"], whole["timeline"][-3:])
        self.assertEqual(sum(len(items) for items in capped["findings"].values()),
                         min(3, len(findings)))
        self.assertTrue(capped["paging"])
        # The evaluation is still judged on the whole view, not on the capped lists.
        self.assertEqual(capped["evaluation"]["status"], "stored")

    def test_a_stored_evaluation_of_an_earlier_case_is_labelled_stale(self):
        request = self.serve(OPS_QUALITY)
        evaluation = self.get(request, f"/api/case/{self.case_id}")["evaluation"]
        self.assertEqual((evaluation["freshness"], evaluation["evaluated_events"],
                          evaluation["current_events"]), ("current", self.events, self.events))
        # Any later case event (here a gap) means the report scored an earlier case.
        self.call("plx.case_record_gap", {
            "case_id": self.case_id, "missing": "Signed AO scan countersignature",
            "why": "not yet seen", "who_can_answer": ["Quality"], "state": "unknown"})
        evaluation = self.get(request, f"/api/case/{self.case_id}")["evaluation"]
        self.assertEqual(evaluation["status"], "stored")
        self.assertEqual((evaluation["freshness"], evaluation["evaluated_events"],
                          evaluation["current_events"]), ("stale", self.events, self.events + 1))
        self.assertIn("changed after this evaluation", evaluation["freshness_reason"])

    @unittest.skipUnless(local_ocr_available(), "no local OCR backend installed: the scan "
                         "fixture has no text layer, so it yields no page citation to refuse")
    def test_original_pdf_bytes_are_served_only_to_its_audience(self):
        runtime = self.server.case_runtime
        inbox = runtime.originals.inbox / "staff:quality"
        inbox.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURE, inbox / "scan-for-bytes.pdf")
        attached = self.call("plx.case_attach_original",
                             {"case_id": self.case_id, "inbox_name": "scan-for-bytes.pdf",
                              "inbox_principal": "staff:quality"})["original"]
        route = "/api/case-original-pdf?handle=" + quote(attached["citation"], safe="")
        request = self.serve(OPS_QUALITY)
        self.assertEqual(request(route, auth=False, raw=True)[0], 401)
        status, headers, body = request(route, raw=True)
        self.assertEqual(status, 200, body[:200])
        self.assertEqual(body, FIXTURE.read_bytes())
        self.assertEqual(headers["Content-Type"], "application/pdf")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")
        # A page citation or any other query is refused; nothing is written.
        for bad in ("/api/case-original-pdf?handle=" + quote(attached["page_citations"][0], safe=""),
                    "/api/case-original-pdf", route + "&x=1"):
            self.assertEqual(request(bad)[0], 400, bad)
        self.assertNotEqual(request(route, method="POST")[0], 200)

    def test_original_pdf_bytes_are_refused_outside_the_audience_like_a_missing_one(self):
        runtime = self.server.case_runtime
        inbox = runtime.originals.inbox / "staff:quality"
        inbox.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURE, inbox / "scan-for-bytes.pdf")
        attached = self.call("plx.case_attach_original",
                             {"case_id": self.case_id, "inbox_name": "scan-for-bytes.pdf",
                              "inbox_principal": "staff:quality"})["original"]
        route = "/api/case-original-pdf?handle=" + quote(attached["citation"], safe="")
        missing = "/api/case-original-pdf?handle=" + quote("original://orig-" + "0" * 32, safe="")
        for principals in (OPS, OUTSIDER):  # a case member without staff:quality; an outsider
            request = self.serve(principals)
            denied = request(route)
            self.assertEqual(denied, (404, {"error": "not_found"}))
            self.assertEqual(denied, request(missing))

    def test_a_console_without_a_case_workspace_says_so(self):
        request = self.serve(OPS_QUALITY, workspace=False)
        self.assertEqual(self.get(request, "/api/cases"), {"available": False, "cases": []})
        self.assertEqual(request(f"/api/case/{self.case_id}"),
                         (501, {"error": "capability_unavailable"}))
        self.assertFalse(self.get(request, "/api/session")["case_workspace"])


class CaseWorkspaceUnitTest(unittest.TestCase):
    def test_workspace_requires_a_real_actor(self):
        with self.assertRaises(ValueError):
            CaseWorkspace(object(), "staff:operations")  # type: ignore[arg-type]

    def test_a_broken_or_bounded_action_listing_never_reads_not_registered(self):
        class Listing:
            def __init__(self, listing):
                self.listing = listing

            def list_for_case(self, actor, case_id):
                return self.listing

        def approval(listing, proposal):
            workspace = CaseWorkspace.__new__(CaseWorkspace)
            workspace.action_path, workspace.actor = Listing(listing), Actor("plx", OPS)
            return CaseWorkspace._approval(proposal, workspace._intents("case-1"))

        broken = {"intents": [{"case_id": "case-1", "state": None,
                               "integrity": "action_event_chain_broken"}],
                  "total": 1, "truncated": False}
        self.assertEqual(approval(broken, {"id": "P1"})["state"], "unknown_chain_broken")
        self.assertIsNone(approval(broken, {"id": "P1"})["dispatched"])
        bounded = {"intents": [], "total": 3, "truncated": True}
        self.assertEqual(approval(bounded, {"id": "P1"})["state"], "unknown_list_truncated")
        withheld = {"intents": [{"case_id": "case-1", "proposal_id": "P1",
                                 "state": "dispatched_unknown", "withheld": True,
                                 "content": "withheld_citation_unavailable"}],
                    "total": 1, "truncated": False}
        state = approval(withheld, {"id": "P1", "content": "withheld_citation_unavailable"})
        self.assertEqual((state["withheld"], state["dispatched"]), (True, None))
        self.assertNotIn("intent_id", state)
        sent = {"intents": [{**withheld["intents"][0], "state": "verified"}],
                "total": 1, "truncated": False}
        self.assertTrue(approval(sent, {"id": "P1", "content": "x"})["dispatched"])
        self.assertEqual(approval({"intents": [], "total": 0, "truncated": False},
                                  {"id": "P1"})["state"], "not_registered")

    def test_any_withheld_or_changed_part_withholds_the_evaluation(self):
        complete = {"findings": {"supported": [{"id": "F1"}], "withheld": [],
                                 "citation_content_changed": []},
                    "calculations": [{"id": "C1", "inputs_status": "resolved"}],
                    "gaps": [{"id": "G1", "citations_status": "uncited"}],
                    "proposals": [{"id": "P1", "citations_status": "resolved"}],
                    "verifications": []}
        self.assertTrue(CaseWorkspace._view_complete(complete))
        hidden = "withheld_citation_unavailable"
        variants = [("findings", "withheld", [{"id": "F2"}]),
                    ("findings", "citation_content_changed", [{"id": "F3"}])]
        for bucket_owner, bucket, value in variants:
            view = json.loads(json.dumps(complete))
            view[bucket_owner][bucket] = value
            self.assertFalse(CaseWorkspace._view_complete(view), bucket)
        for name, item in (("calculations", {"id": "C2", "content": hidden}),
                           ("calculations", {"id": "C3",
                                             "inputs_status": "citation_content_changed"}),
                           ("gaps", {"id": "G2", "content": hidden}),
                           ("proposals", {"id": "P2",
                                          "citations_status": "citation_content_changed"}),
                           ("verifications", {"id": "V1", "content": hidden})):
            view = json.loads(json.dumps(complete))
            view[name].append(item)
            self.assertFalse(CaseWorkspace._view_complete(view), item)

    def test_a_superseded_finding_or_link_with_changed_citations_withholds_the_evaluation(self):
        resolved = {"handle": "api://r/1@g1-1", "status": "resolved", "digest": "a"}
        changed = {"handle": "api://r/1@g1-1", "status": "citation_content_changed",
                   "digest": "a", "current_digest": "b"}
        complete = {"findings": {"supported": [], "withheld": [], "citation_content_changed": [],
                                 "superseded": [{"id": "F1", "citations": [resolved],
                                                 "superseded": {"by": "F2"}}]},
                    "calculations": [], "gaps": [], "proposals": [], "verifications": [],
                    "links": [{"id": "L1", "citations": [resolved], "citations_status": "resolved"}]}
        self.assertTrue(CaseWorkspace._view_complete(complete))
        view = json.loads(json.dumps(complete))
        view["findings"]["superseded"][0]["citations"] = [resolved, changed]
        self.assertFalse(CaseWorkspace._view_complete(view))
        view = json.loads(json.dumps(complete))
        view["findings"]["superseded"][0]["citations"] = [{"index": 0, "status": "citation_unavailable"}]
        self.assertFalse(CaseWorkspace._view_complete(view))
        for link in ({"id": "L2", "content": "withheld_citation_unavailable"},
                     {"id": "L3", "citations_status": "citation_content_changed"}):
            view = json.loads(json.dumps(complete))
            view["links"].append(link)
            self.assertFalse(CaseWorkspace._view_complete(view), link)

    def test_freshness_compares_the_report_boundary_with_the_case_now(self):
        timeline = [{"seq": 1, "kind": "case.opened", "at": "t1"},
                    {"seq": 2, "kind": "finding.recorded", "at": "t2"}]
        view = {"integrity": {"events": 2}, "timeline": timeline}
        boundary = {"events": 2, "last_event": {"seq": 2, "kind": "finding.recorded", "at": "t2"}}
        self.assertEqual(CaseWorkspace._freshness(boundary, view)["freshness"], "current")
        later = {"integrity": {"events": 3},
                 "timeline": timeline + [{"seq": 3, "kind": "gap.recorded", "at": "t3"}]}
        stale = CaseWorkspace._freshness(boundary, later)
        self.assertEqual((stale["freshness"], stale["evaluated_events"], stale["current_events"]),
                         ("stale", 2, 3))
        # Same count, different last event (another case's report, or a rebuilt store).
        other = {**boundary, "last_event": {"seq": 2, "kind": "gap.recorded", "at": "t9"}}
        self.assertEqual(CaseWorkspace._freshness(other, view)["freshness"], "stale")
        for missing in (None, {}, {"events": "2"}):
            self.assertEqual(CaseWorkspace._freshness(missing, view)["freshness"], "unknown")

    def test_seeding_refuses_an_existing_state_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "case-runtime").mkdir()
            with self.assertRaises(ValueError):
                case_fixtures.seed(Path(temp))
            self.assertEqual(sorted(p.name for p in Path(temp).iterdir()), ["case-runtime"])
            self.assertEqual(list((Path(temp) / "case-runtime").iterdir()), [])


ALL = frozenset({"staff:operations", "staff:quality", "staff:purchasing"})


def _serve_workspace(test, build):
    """Serve ``build()`` (a CaseWorkspace, built in the serving thread) over HTTP."""
    ready: Queue = Queue(maxsize=1)

    def run():
        try:
            workspace = build()
            session = fixture_session(workspace.actor.tenant, workspace.actor.principals,
                                      channel=CHANNEL_CONSOLE,
                                      clearance=workspace.actor.clearance)
            server = make_server(case_workspace=workspace, session=session)
            ready.put(server)
            server.serve_forever(poll_interval=0.05)
            server.server_close()
            workspace.runtime.close()
        except BaseException as error:  # surfaced to the test thread
            ready.put(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    server = ready.get(timeout=60)
    if isinstance(server, BaseException):
        raise server
    test.addCleanup(lambda: (server.shutdown(), thread.join(timeout=10)))
    port, token = server.server_address[1], server.operator_access_token

    def request(path):
        conn = HTTPConnection("127.0.0.1", port, timeout=60)
        conn.request("GET", path, headers={"Authorization": "Bearer " + token})
        response = conn.getresponse()
        value = (response.status, json.loads(response.read()))
        conn.close()
        return value
    return request


class CaseJoinFixtureTest(unittest.TestCase):
    """The joined workspace over the console's fixture seed (six AO packets + PO5447).

    Evidence label: isolated-fixture / injected-transport (synthetic rows, packets and
    mail through the real read-only clients over in-process fakes). Nothing is live.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.temp.name) / "state"
        cls.written = case_fixtures.seed(cls.state)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def serve(self, principals, *, mutate=None, state=None):
        state = self.state if state is None else state

        def build():
            from operating.action_path import ActionPath
            world = case_fixtures.build_world()
            if mutate is not None:
                mutate(world)
            runtime = case_fixtures.runtime_for(
                state, world, state / case_fixtures.MIRROR_AUDIENCE_FILE,
                state / case_fixtures.LIVE_AUDIENCE_FILE, "plx", online=False)
            return CaseWorkspace(
                runtime, Actor("plx", frozenset(principals)),
                evidence=case_console.evidence_for(case_fixtures.marker(state)),
                action_path=ActionPath(state / case_fixtures.ACTIONS_FILE, runtime.cases))
        return _serve_workspace(self, build)

    def get(self, request, path):
        status, body = request(path)
        self.assertEqual(status, 200, body)
        return body

    def case(self, request, key):
        case_id = self.written["po"] if key == "po" else self.written["aos"][key]
        return self.get(request, f"/api/case/{case_id}")

    def test_every_seeded_case_reads_as_fixture_and_the_six_packets_are_attached(self):
        request = self.serve(ALL)
        listed = self.get(request, "/api/cases")
        self.assertEqual(len(listed["cases"]), 7)
        self.assertTrue(listed["evidence"]["fixture"])
        self.assertIn("FIXTURE", listed["evidence"]["note"])
        for code in case_fixtures.AO_CODES:
            view = self.case(request, code)
            self.assertTrue(view["evidence"]["fixture"], code)
            [original] = view["originals"]
            self.assertEqual(original["filename"], f"{code}.pdf")
            self.assertEqual(original["source"]["kind"], "sharepoint-drive")
            self.assertEqual(original["source"]["etag_status"], "current")
            self.assertEqual(original["source"]["etag_at_attach"], f'"{{{code}}},1"')
            self.assertTrue(original["integrity"]["sha256_rehashed_on_this_read"])
            self.assertEqual(original["pages_total"], 12)
            # No second join of its own: relationships are the ontology's.
            self.assertNotIn("relationship_map", view)

    def test_an_ao_case_shows_its_originals_timeline_and_actions(self):
        request = self.serve(ALL)
        view = self.case(request, "AO-26005B")
        self.assertEqual((view["read_only"], view["business_system_writes"]), (True, 0))
        # Timeline: case events plus dated source values, including the packet's item date.
        relations = {item["relation"] for item in view["source_dates"]}
        self.assertTrue({"public.order_entry", "original"} <= relations)
        # The proposal's action type is not in the typed action catalog: the action
        # path refused to register it, so it is shown as not registered, never dispatched.
        [proposal] = view["proposals"]
        approval = proposal["approval"]
        self.assertEqual((self.written["intent"], self.written["intent_refused"]),
                         (None, "action_type_registration_refused:"
                          "owner_write_surface_does_not_set_these_fields"))
        self.assertEqual((approval["state"], approval["dispatched"], approval["approved_here"]),
                         ("not_registered", False, False))
        self.assertNotIn("intent_id", approval)
        # The console decides no mail link of its own (the ontology's, plx.around).
        self.assertNotIn("mail_links", view)

    def test_the_po_case_shows_the_supplier_confirmation_mail_and_no_conflict_on_agreeing_rows(self):
        request = self.serve(ALL)
        view = self.case(request, "po")
        # The confirmation mail the case cites is re-read for the reader and dated.
        dates = {(item["relation"], item["column"]) for item in view["source_dates"]}
        self.assertIn(("mail", "received"), dates)
        # The Portal PO and its FM twin both read USD: the same value is no disagreement,
        # so no PurchaseOrder.currency conflict is raised (it once was, on equal values).
        self.assertEqual([c for c in view["conflicts"]
                          if "PurchaseOrder.currency" in c.get("question", "")], [],
                         view["conflicts"])

    def test_a_po_whose_fm_twin_reads_another_currency_shows_one_conflict_citing_both(self):
        def cad(world):
            for row in world.fake.tables["public.generated_purchase_order"]:
                if row.get("id") == 5447:
                    row["currency"] = "CAD"
        real_build = case_fixtures.build_world

        def build_disagreeing():
            world = real_build()
            cad(world)
            return world
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)  # runs after the server below has stopped
        state = Path(temp.name) / "state"
        with mock.patch.object(case_fixtures, "build_world", build_disagreeing):
            written = case_fixtures.seed(state)
        request = self.serve(ALL, mutate=cad, state=state)
        view = self.get(request, f"/api/case/{written['po']}")
        # One evidence conflict citing both records, never resolved.
        [conflict] = [c for c in view["conflicts"]
                      if "PurchaseOrder.currency" in c.get("question", "")]
        self.assertEqual((conflict["kind"], conflict["state"]),
                         ("evidence_conflict", "unknown"))
        [side] = conflict["sides"]
        self.assertEqual(len(side["citations"]), 2)
        relations = {self.get(request, "/api/case-citation?handle=" + quote(handle, safe=""))
                     ["relation"] for handle in side["citations"]}
        self.assertEqual(relations, {"public.app_purchase_order",
                                     "public.generated_purchase_order"})

    def test_a_reader_without_quality_or_the_mailbox_sees_no_withheld_identity(self):
        request = self.serve({"staff:operations"})
        ao = self.case(request, "AO-26005B")
        text = json.dumps({key: value for key, value in ao.items() if key != "limitations"})
        for secret in ("silver.", "AO-26005B.pdf", "original://orig", "sharepoint://b",
                       "ITEM-AO", "set_header_quantities", "Quality confirms", "act_"):
            self.assertNotIn(secret, text)
        # The withheld proposal shows its approval state, never what it proposes.
        [proposal] = ao["proposals"]
        self.assertEqual(proposal["content"], "withheld_citation_unavailable")
        # (Its action type is not in the catalog, so it was never registered.)
        self.assertEqual((proposal["approval"]["state"], proposal["approval"]["dispatched"]),
                         ("not_registered", False))
        self.assertNotIn("capability", proposal["approval"])
        self.assertEqual(ao["originals"], [])
        self.assertEqual(ao["conflicts"], [])  # every paper/FM conflict cites quality rows
        self.assertTrue(ao["evidence"]["fixture"])
        po = self.case(request, "po")
        text = json.dumps({key: value for key, value in po.items() if key != "limitations"})
        for secret in ("graph-mail://", "synthetic-vendor-a", case_fixtures.CONFIRMATION_ID,
                       "Order confirmation"):
            self.assertNotIn(secret, text)
        # The packet's metadata proposals are refused like a missing original.
        status, _body = request("/api/case-document-metadata?handle=" + quote(
            "original://orig-" + "0" * 32, safe=""))
        self.assertEqual(status, 404)

    def test_a_bounded_action_listing_with_nothing_registered_reads_not_registered(self):
        # Nothing on this case is registered (its proposal's action type is not in the
        # catalog), so a listing bound of zero truncates nothing: still not registered.
        # A truncated listing reading unknown is covered with a stub listing above.
        from operating import action_path
        with mock.patch.object(action_path, "MAX_LISTED_INTENTS", 0):
            request = self.serve(ALL)
            [proposal] = self.case(request, "AO-26005B")["proposals"]
        self.assertEqual((proposal["approval"]["state"], proposal["approval"]["dispatched"]),
                         ("not_registered", False))

    def test_intents_come_from_the_action_path_listing_for_the_reader(self):
        """The workspace reads ActionPath.list_for_case (permission-aware), never the
        store's tables; a withheld intent shows its state only."""
        from operating.action_path import ActionPath
        calls = []
        real = ActionPath.list_for_case

        def spy(path, actor, case_id):
            calls.append((sorted(actor.principals), case_id))
            return real(path, actor, case_id)

        with mock.patch.object(ActionPath, "list_for_case", spy):
            request = self.serve({"staff:operations"})
            [proposal] = self.case(request, "AO-26005B")["proposals"]
        self.assertEqual(calls, [(["staff:operations"], self.written["aos"]["AO-26005B"])])
        approval = proposal["approval"]
        # Nothing registered on this case (action type not in the catalog).
        self.assertEqual((approval["state"], approval["dispatched"]), ("not_registered", False))
        self.assertNotIn("intent_id", approval)
        self.assertNotIn("capability", approval)

    def test_a_sharepoint_item_changed_after_attach_reads_changed_since(self):
        def touch(world):
            world.folder.items["ITEM-AO-26005B"]["eTag"] = '"{AO-26005B},2"'
        request = self.serve(ALL, mutate=touch)
        [original] = self.case(request, "AO-26005B")["originals"]
        self.assertEqual(original["source"]["etag_status"], "changed_since")
        self.assertEqual(original["source"]["etag_at_attach"], '"{AO-26005B},1"')

    def test_document_metadata_proposals_are_served_read_only_for_a_readable_original(self):
        request = self.serve(ALL)
        [original] = self.case(request, "AO-26005B")["originals"]
        meta = self.get(request, "/api/case-document-metadata?handle="
                        + quote(original["citation"], safe=""))
        self.assertEqual((meta["proposals_only"], meta["read_only"],
                          meta["business_system_writes"], meta["saved"], meta["applied"]),
                         (True, True, 0, False, False))
        self.assertEqual(meta["anchor"]["entity_type"], "ASSEMBLY_ORDER")
        status, _ = request("/api/case-document-metadata?handle=api%3A%2F%2Fx")
        self.assertEqual(status, 400)


class CaseConsoleFixtureLabelTest(unittest.TestCase):
    """A seeded directory is labelled fixture on every start, with or without the flag."""

    def start(self, argv):
        captured = {}

        def fake_serve(*, port, case_workspace, session, object_workspace=None):
            self.assertTrue(session.fixture)
            self.assertEqual(case_workspace.actor, session.actor)
            captured["workspace"] = case_workspace
            captured["objects"] = object_workspace
            captured["listed"] = case_workspace.list_cases()
        with mock.patch.object(case_console, "serve", fake_serve), \
                mock.patch("builtins.print"):
            self.assertEqual(case_console.main(argv, fixtures=case_fixtures), 0)
        return captured

    def test_product_console_never_reaches_the_fixture_world(self):
        # Without the fixture entry point the console seeds nothing and refuses to serve
        # a directory a full seed wrote (it holds no fixture readers of its own).
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            argv = ["--state-dir", str(state), "--fixture", "--principal", "staff:operations"]
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                case_console.main([*argv, "--seed-fixtures"])
            self.assertFalse(state.exists())
            self.start([*argv, "--seed-fixtures"])
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"), \
                    mock.patch.object(case_console, "serve",
                                      side_effect=AssertionError("served")):
                case_console.main(argv)

    def test_seed_then_restart_without_the_flag_still_says_fixture(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            # Seeding is a fixture identity: it needs the explicit flag.
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                self.start(["--state-dir", str(state), "--seed-fixtures",
                            "--principal", "staff:operations"])
            self.assertFalse(state.exists())
            first = self.start(["--state-dir", str(state), "--fixture", "--seed-fixtures",
                                "--principal", "staff:operations", "--principal", "staff:quality"])
            self.assertTrue(first["listed"]["evidence"]["fixture"])
            self.assertEqual(len(first["listed"]["cases"]), 7)
            # The seed is projected and served by the object workspace, labelled fixture.
            self.assertTrue(first["objects"].evidence["fixture"])
            again = self.start(["--state-dir", str(state), "--fixture",
                                "--principal", "staff:operations"])
            self.assertTrue(again["listed"]["evidence"]["fixture"])
            self.assertTrue(again["objects"].evidence["fixture"])
            self.assertIsNotNone(again["workspace"].action_path)
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                self.start(["--state-dir", str(state), "--fixture", "--seed-fixtures",
                            "--principal", "staff:operations"])
            # Real readers are refused beside fixture rows, on the seeded directory or a new seed.
            for flags in (["--live-source-audience", "examples/ao_originals_live_audience.example.json"],
                          ["--ingest-sources", "x.json"], ["--open", "ao=AO-26005B"]):
                with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                    self.start(["--state-dir", str(state), "--fixture",
                                "--principal", "staff:operations", *flags])
            fresh = Path(temp) / "fresh"
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                self.start(["--state-dir", str(fresh), "--fixture", "--seed-fixtures",
                            "--principal",
                            "staff:operations", "--live-source-audience",
                            "examples/ao_originals_live_audience.example.json"])
            self.assertFalse(fresh.exists())

    def test_a_marker_without_the_seed_audience_is_fixture_but_gets_no_fixture_readers(self):
        # E.g. a directory the retired single-AO seed wrote: still labelled fixture, but no
        # seed's in-process readers or default audience are attached to it.
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / case_console.FIXTURE_MARKER).write_text(json.dumps({
                "schema": case_console.MARKER_SCHEMA, "world": "ao-26005b",
                "evidence_label": "injected-transport", "note": "older seed"}), "utf-8")
            argv = ["--state-dir", temp, "--fixture", "--principal", "staff:operations"]
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                self.start(argv)  # no seed audience: --source-mirror-audience is required
            with mock.patch.object(case_fixtures, "build_world",
                                   side_effect=AssertionError("fixture readers attached")):
                captured = self.start([*argv, "--source-mirror-audience",
                                       "examples/ao_case_audience.example.json"])
            self.assertEqual(captured["listed"]["evidence"],
                             {"label": case_console.EVIDENCE_LABEL, "fixture": True,
                              "note": case_console.EVIDENCE_NOTE})
            # Real readers stay refused beside it.
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                self.start([*argv, "--source-mirror-audience",
                            "examples/ao_case_audience.example.json", "--ingest-sources",
                            "x.json"])

    def test_a_fixture_session_labels_a_new_directory_fixture(self):
        with tempfile.TemporaryDirectory() as temp:
            argv = ["--state-dir", temp, "--source-mirror-audience",
                    "examples/ao_case_audience.example.json", "--principal", "staff:operations"]
            # Without the explicit flag a free-text principal is no identity.
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                self.start(argv)
            captured = self.start([*argv, "--fixture"])
            self.assertTrue(captured["listed"]["evidence"]["fixture"])
            self.assertEqual(captured["listed"]["evidence"]["label"], "isolated-fixture")
            self.assertIsNone(captured["objects"])  # no projection store: no object routes
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                self.start(["--state-dir", temp, "--fixture", "--principal", "staff:operations"])

    def test_unlabelled_case_state_is_live_and_refuses_a_fixture_session(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "case-runtime").mkdir()
            with self.assertRaises(SystemExit), mock.patch("sys.stderr") as err:
                self.start(["--state-dir", temp, "--source-mirror-audience",
                            "examples/ao_case_audience.example.json", "--fixture",
                            "--principal", "staff:operations"])
            self.assertIn("fixture_session_on_live_state",
                          "".join(str(c) for c in err.mock_calls))


class CaseWorkspaceCoverageTest(ingest_fixture.IngestionTestCase):
    """Ingestion coverage in the workspace: per-actor trimming (injected transport)."""

    def readers(self):
        from integrations import bc_read, graph_read
        from integrations.live_source_guard import LiveSourceAudience
        from operating.case_runtime import LiveReaders
        from operating.tests.test_mcp_case_tools import HOST_LIVE

        audience = LiveSourceAudience(HOST_LIVE, tenant="plx")
        api, transport = graph_read.build_graph(ingest_fixture.ENV, sender=self.fake)
        bc_api, _t, tenant_id, environment = bc_read.build_bc(ingest_fixture.ENV,
                                                             sender=self.fake)
        return LiveReaders(audience, mail=graph_read.GraphMailReader(api, audience),
                           sharepoint={ingest_fixture.DRIVE: graph_read.SharePointReader(
                               api, audience, ingest_fixture.DRIVE, transport=transport)},
                           bc=bc_read.BusinessCentralReader(bc_api, audience,
                                                            tenant_id=tenant_id,
                                                            environment=environment))

    def test_coverage_is_counts_only_and_trimmed_to_the_reader(self):
        from integrations.source_mirror import AUDIENCE_SCHEMA
        from operating.case_context_tools import IngestionStatusSource
        from operating.ingestion import load_sources
        from operating.tests.test_mcp_case_tools import EVERY_SCOPE

        self.ingestor().run()
        audience = self.state.parent / "mirror-audience.json"
        audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                        "audiences": {"public.*": ["staff:operations"]}}),
                            encoding="utf-8")
        _tenant, _actor, specs = load_sources(ingest_fixture.sources_document(),
                                              ingest_fixture.ENV)
        host = IngestionStatusSource(self.state, tuple(specs), "plx")
        runtime = CaseRuntime(self.state, client=None, audience=audience, tenant="plx",
                              live=self.readers())
        self.addCleanup(runtime.close)
        every = CaseWorkspace(runtime, Actor("plx", EVERY_SCOPE, "restricted"),
                              ingestion=host).source_coverage()
        ops = CaseWorkspace(runtime, Actor("plx", frozenset({"staff:operations"}), "restricted"),
                            ingestion=host).source_coverage()
        self.assertEqual((every["status"], ops["status"]), ("read", "read"))
        self.assertGreater(every["summary"]["units"], ops["summary"]["units"])
        # What operations may not read is withheld and only counted.
        self.assertEqual(ops["visibility"]["units_withheld"],
                         every["visibility"]["units_withheld"]
                         + every["summary"]["units"] - ops["summary"]["units"])
        text = json.dumps(ops)
        for value in (ingest_fixture.MAILBOX, ingest_fixture.DRIVE, ingest_fixture.SUBJECT):
            self.assertNotIn(value, text)
        self.assertEqual(ops["business_system_writes"], 0)
        self.assertEqual(CaseWorkspace(runtime, Actor("plx", EVERY_SCOPE)).source_coverage(),
                         {"status": "not_configured"})

class CaseConflictsTest(unittest.TestCase):
    """The conflicts the case records, and the supplier-mail conflicts for its POs."""

    @staticmethod
    def row(relation, ident):
        key = {"id": ident}
        handle = f"api://{relation}/{quote(json.dumps(key, separators=(',', ':')))}@g-1"
        return {"citation": handle, "relation": relation, "key": key}

    def test_a_contradiction_recorded_by_plx_case_link_stays_a_conflict(self):
        from operator_ui.case_workspace import conflicts
        po = self.row("public.app_purchase_order", "po-a")
        line = self.row("public.app_purchase_order_line", "l-b")
        view = {"findings": {}, "gaps": [], "links": [
            {"id": "L1", "outcome": "contradicted", "contract": {"id": "portal-po-line-to-po"},
             "citations": [{"handle": line["citation"], "status": "resolved"},
                           {"handle": po["citation"], "status": "resolved"}]},
            # Withheld from this reader: no citation, never listed.
            {"id": "L2", "outcome": "contradicted", "content": "withheld_citation_unavailable",
             "citations": [None, None]}]}
        [conflict] = conflicts(view)
        self.assertEqual((conflict["kind"], conflict["id"]),
                         ("link_contradicted", "portal-po-line-to-po"))
        self.assertEqual(conflict["sides"][0]["citations"], [line["citation"], po["citation"]])


if __name__ == "__main__":
    unittest.main()


class CaseSummaryCounterpartyTest(unittest.TestCase):
    """``case_summary`` names a party only as the engine served it (a typed or reviewed
    link recorded on the case, with its tier); a text-only match is a lead that names none.

    Evidence label: isolated-fixture (synthetic rows over the shared schema)."""

    PO_H = "api://public.app_purchase_order/%7B%22id%22%3A%22po-1%22%7D@g1-3"
    SUP_H = "api://public.app_supplier/%7B%22id%22%3A%22s-1%22%7D@g1-4"

    def rows(self, supplier_id="S-1", supplier_row_id="s-1"):
        return {self.PO_H: [{"id": "po-1", "po_number": "PO5447", "status": "open",
                             "supplier_id": supplier_id}],
                self.SUP_H: [{"id": supplier_row_id, "name": "Synthetic Bottle Vendor A"}]}

    def view(self, outcome=None):
        links = [] if outcome is None else [{
            "id": "L1", "outcome": outcome, "contract": {"id": "synthetic"},
            "citations": [{"handle": self.PO_H, "status": "resolved"},
                          {"handle": self.SUP_H, "status": "resolved"}]}]
        return {"roots": ["PO5447"], "gaps": [], "findings": {}, "links": links}

    def test_a_party_that_matches_only_by_normalised_text_is_a_lead_and_is_not_named(self):
        # "S-1" and "s-1" are one key only after lowercasing and dropping punctuation.
        summary = case_plain.case_summary(self.view(), self.rows())
        party = summary["counterparty"]
        self.assertIsNone(party["name"])
        self.assertEqual(party["tier"], "stored_by_text_join")
        self.assertEqual(party["tier_meaning"], explanations.TIER_MEANING["stored_by_text_join"])
        self.assertIn("S-1", party["reference"])
        self.assertNotIn("Synthetic Bottle Vendor A", json.dumps(summary))

    def test_a_typed_or_reviewed_link_names_the_party_with_its_tier(self):
        for outcome in ("verified", "reviewed"):
            # The records share no text: only the engine's link says they belong together.
            summary = case_plain.case_summary(self.view(outcome),
                                              self.rows(supplier_id="x", supplier_row_id="9"))
            party = summary["counterparty"]
            self.assertEqual((party["name"], party["tier"]), ("Synthetic Bottle Vendor A", outcome))
            self.assertEqual(party["tier_meaning"], explanations.TIER_MEANING[outcome])
            self.assertTrue(summary["title"].endswith("· Synthetic Bottle Vendor A"))

    def test_a_weak_or_contradicted_link_never_names_the_party(self):
        for outcome in ("lead", "stored_by_text_join", "contradicted"):
            party = case_plain.case_summary(self.view(outcome), self.rows())["counterparty"]
            self.assertIsNone(party["name"], outcome)

    def test_a_link_to_a_record_the_reader_cannot_resolve_names_nothing(self):
        rows = self.rows()
        del rows[self.SUP_H]
        party = case_plain.case_summary(self.view("verified"), rows)["counterparty"]
        self.assertIsNone(party["name"])


class ReasonWordsOnePathTest(unittest.TestCase):
    """One reason code reads the same in the object view and the case and home views."""

    CODES = ("no_match", "not_served", "not_applicable_to_source",
             "evidence_not_fully_visible")

    def test_a_reason_code_renders_the_same_words_in_the_object_view_and_the_case_view(self):
        for code in self.CODES:
            phrase = explanations._REASON_TEXT[code]
            object_gap = explanations._Gaps()
            object_gap.gap({"reason": code, "subject": "x"}, "this record")
            [item] = object_gap.out()
            plain = case_plain.plain_gap({"missing": "po_twin_of", "why": f"plx.investigate:{code}: x"})
            self.assertTrue(item["text"].endswith(phrase), item["text"])
            self.assertEqual(plain["text"].lower().rstrip("."), phrase.lower(), code)

    def test_the_case_view_keeps_no_wording_table_of_its_own(self):
        for name in ("REASONS", "type_label"):
            self.assertFalse(hasattr(case_plain, name), name)
        self.assertEqual(case_plain.plain_gap(
            {"missing": "x", "why": "value_disagreement"})["text"],
            explanations.reason_words("value_disagreement"))

    def test_home_words_a_reason_code_as_the_case_view_does(self):
        from operator_ui import home

        gap = {"id": "G1", "missing": "po_twin_of", "why": "plx.investigate:no_match: x"}

        class Cases:
            def get(self, actor, case_id):
                return {"gaps": [gap]}

        class Runtime:
            cases = Cases()

        class Workspace:
            actor, runtime = None, Runtime()

            def list_cases(self, **kwargs):
                return {"cases": [{"case_id": "c1", "question": "q", "opened_at": "t",
                                   "updated_at": "t", "events": 1}]}
        [case] = home._open_cases(Workspace())["cases"]
        self.assertEqual(case["gaps"][0]["plain"]["text"],
                         case_plain.plain_gap(gap)["text"])
        self.assertEqual(case["gaps"][0]["plain"]["text"].lower().rstrip("."),
                         explanations._REASON_TEXT["no_match"])


class IdentityCandidatesTest(unittest.TestCase):
    A = 'PurchaseOrder:portal.app_purchase_order:{"id":"po-1"}'
    B = 'PurchaseOrder:portal.fm_generated_purchase_order:{"id":5447}'

    def edge(self, a, b, tier, handle):
        return {"link_id": f"{a[-6:]}{b[-6:]}", "link_type": "PurchaseOrder.same_record_as",
                "from": a, "to": b, "tier": tier,
                "reason": [{"feature": "pair", "agrees": True,
                            "citations": [{"l1_handle": handle}, {"l1_handle": "shared"}]}],
                "why_not": []}

    def test_a_link_stored_both_ways_is_one_candidate_with_features_merged(self):
        edges = [self.edge(self.A, self.B, "verified", "h1"),
                 self.edge(self.B, self.A, "lead", "h2")]
        [one] = explanations.identity_candidates(edges)
        self.assertEqual(one["tiers_differ"], ["verified", "lead"])
        [feature] = one["reason"]
        self.assertEqual([c["l1_handle"] for c in feature["citations"]], ["h1", "shared", "h2"])
        self.assertEqual(len(one["directions"]), 2)
        self.assertEqual(edges[0]["reason"][0]["citations"], [{"l1_handle": "h1"},
                                                              {"l1_handle": "shared"}])
        self.assertEqual(explanations.identity_candidates([]), [])
        self.assertEqual(explanations.identity_candidates(None), [])
