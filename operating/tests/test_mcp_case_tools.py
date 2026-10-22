"""MCP-level tests for the case runtime tool family (injected transport only).

Evidence label: ``injected-transport``. The real ``WorkbenchReadApiClient`` talks
to the in-process ``FakeWorkbench`` from the source-mirror tests; nothing touches
the network and no output here is live Portal data. Every tool is driven through
``MCPServer.handle`` (JSON-RPC ``tools/call``), the same path an MCP client uses.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from integrations.document_understanding import ExtractedDocument
from integrations.source_mirror import AUDIENCE_SCHEMA, MirrorError
from integrations.tests.test_source_mirror import BASE, KEY, Clock, FakeWorkbench
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology.core import Actor, ValidationError
from operating.case_runtime import CASE_RUNTIME_DIR, CaseRuntime, CaseRuntimeUnavailable
from operating import case_context_tools, source_reads
from operating.tests.firewall_view import as_text, client_result
from operating import agent_loop
from operating.tests import test_agent_loop as loop_tests
from operating.tests import test_mcp_http_host as door_tests
from operating.tests.test_session_identity import OID_A
from operating.cases import CitationUnresolvable
from operating import action_path as ap
from operating.session_identity import CHANNEL_MCP, fixture_session
from operating.claim_checks import returned_evidence
from operating.mcp_server import (CASE_ONLY_INSTRUCTIONS, CASE_RUNTIME_TOOLS,
                                  DEFAULT_ORIGINAL_TEXT_CHARS, HOST_READS, MAX_RESULT_BYTES,
                                  TOOLS, MCPServer)
from operating.tests.test_action_path import (EMPLOYEE, EMPLOYEE_OID, OPERATOR, ROSTER,
                                              STEPHEN, VINCE, VINCE_OID)


OPS = frozenset({"staff:operations"})
FINANCE = frozenset({"staff:finance"})
OPS_FINANCE = frozenset({"staff:operations", "staff:finance"})
OUTSIDER = frozenset({"staff:marketing"})
GRANTED = {"public.*": ["staff:operations"], "sage.*": ["staff:finance"]}
CASE_WRITE_TOOLS = {"plx.case_open", "plx.case_add_finding", "plx.case_compute",
                    "plx.case_record_gap", "plx.case_propose", "plx.case_close",
                    "plx.case_attach_original", "plx.act",
                    "plx.document_fields"}  # local request record, never a source write


class CaseToolsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.audience_path = self.root / "audience.json"
        self.write_audience(GRANTED)
        self.fake = FakeWorkbench()
        self.clock = Clock()
        self.request_id = 100

    def write_audience(self, audiences: dict) -> None:
        self.audience_path.write_text(json.dumps({
            "schema": AUDIENCE_SCHEMA, "tenant": "plx", "audiences": audiences}),
            encoding="utf-8")

    def runtime(self) -> CaseRuntime:
        client = WorkbenchReadApiClient(BASE, KEY, fetch=self.fake)
        runtime = CaseRuntime(self.state, client=client, audience=self.audience_path,
                              tenant="plx", mirror_options={
                                  "clock": self.clock, "retry_delay_seconds": 0})
        self.addCleanup(runtime.close)
        return runtime

    def server(self, principals: frozenset[str], runtime: CaseRuntime) -> MCPServer:
        # A test host: the Workbench mechanics are exercised with the test-only capability.
        server = MCPServer(case_runtime=runtime, actor=Actor("plx", principals),
                           test_capabilities=True)
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                  "clientInfo": {"name": "case-test", "version": "1"}}})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    def call(self, server: MCPServer, name: str, arguments: dict | None = None) -> dict:
        self.request_id += 1
        host = name in HOST_READS  # the drivers' raw citation read is host code
        response = server.handle({"jsonrpc": "2.0", "id": self.request_id,
                                  "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments or {}}},
                                 host_code=host)
        self.assertNotIn("error", response, response)
        if host:
            return response["result"]
        # Read through the output boundary (checked on every call), then as text.
        return client_result(name, response["result"])

    def ok(self, server: MCPServer, name: str, arguments: dict | None = None) -> dict:
        result = self.call(server, name, arguments)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def refused(self, server: MCPServer, name: str, arguments: dict | None = None) -> str:
        result = self.call(server, name, arguments)
        self.assertTrue(result["isError"], result)
        self.assertNotIn("structuredContent", result)
        return result["content"][0]["text"]

    def read(self, server: MCPServer, name: str, arguments: dict | None = None) -> dict:
        """A direct L1 read for the session actor (``operating.source_reads``), as the
        in-process drivers read; the case host lists no source-read tool."""
        return source_reads.read(server.case_runtime, server.actor, name, arguments or {})

    def read_refused(self, server: MCPServer, name: str,
                     arguments: dict | None = None) -> str:
        with self.assertRaises(MirrorError) as caught:
            self.read(server, name, arguments)
        return f"source_mirror_{caught.exception.code}: {caught.exception}"

    def add_coded_ao(self) -> None:
        """A second FM AO whose code the identifier grammar accepts (AO-26006 has no letter)."""
        rows = self.fake.tables["public.assembly_order"]
        rows.append(dict(rows[0], id=rows[0]["id"] + 1000, nameamo_c="AO-26006B"))

    def ao_citation(self, server: MCPServer) -> str:
        rows = self.read(server, "mirror.rows", {
            "relation": "public.assembly_order",
            "where": [["nameamo_c", "eq", "AO-26006"]]})
        self.assertEqual(rows["returned"], 1)
        return rows["rows"][0]["citation"]

    def open_case_with_fact(self, server: MCPServer) -> tuple[str, str]:
        citation = self.ao_citation(server)
        case_id = self.ok(server, "plx.case_open", {
            "question": "Why is AO-26006 short against plan?",
            "roots": ["AO-26006"]})["case_id"]
        finding = self.ok(server, "plx.case_add_finding", {
            "case_id": case_id, "statement": "AO-26006 total quantity is 1509.",
            "kind": "fact", "status": "supported", "citations": [citation]})
        self.assertEqual(finding["finding"]["finding_id"], "F1")
        return case_id, citation


class ToolListingTests(CaseToolsTestCase):
    def test_tool_family_is_listed_with_honest_annotations(self):
        server = self.server(OPS, self.runtime())
        tools = {tool["name"]: tool for tool in server.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
        self.assertLessEqual(CASE_RUNTIME_TOOLS, set(tools))
        for name in CASE_RUNTIME_TOOLS:
            self.assertEqual(tools[name]["annotations"]["readOnlyHint"],
                             name not in CASE_WRITE_TOOLS, name)
            if name in CASE_WRITE_TOOLS:
                self.assertFalse(tools[name]["annotations"]["destructiveHint"], name)
        propose = tools["plx.case_propose"]["description"]
        self.assertIn("never dispatched", propose)
        self.assertIn("writes no business system", propose)
        for name in ("plx.schema", "plx.find", "plx.object"):
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"], name)
        self.assertIn("Without case_id", tools["plx.case_get"]["description"])
        self.assertEqual(len({tool["name"] for tool in TOOLS}), len(TOOLS))


class CaseFlowTests(CaseToolsTestCase):
    def test_catalog_rows_resolve_then_case_cites_computes_and_proposes(self):
        self.add_coded_ao()
        server = self.server(OPS, self.runtime())
        catalog = self.read(server, "mirror.catalog")
        names = {item["name"] for item in catalog["relations"]}
        self.assertIn("public.assembly_order", names)
        self.assertNotIn("sage.po_header", names)  # finance-only family is not even counted
        self.assertEqual(catalog["relation_count"], len(names))
        self.assertTrue(server.case_runtime.client_configured)

        rows = self.read(server, "mirror.rows", {
            "relation": "public.assembly_order", "where": [["nameamo_c", "eq", "AO-26006"]]})
        self.assertEqual(rows["returned"], 1)
        citation = rows["rows"][0]["citation"]
        self.assertTrue(citation.startswith("api://public.assembly_order/"), citation)
        self.assertEqual(rows["rows"][0]["row"]["qtytotal"], "1509")
        self.assertFalse(rows["freshness"]["atomic_upstream_snapshot"])
        self.assertFalse(rows["freshness"]["deletions_proven"])

        # A suffixless code is no AO identifier (the grammar needs the letter): read as
        # a day prefix (grammar-2) it only leads to that day's lettered AOs, never to the
        # suffixless row and never as a typed match; a coded sibling AO is found, and
        # cited, by its code.
        leads = self.ok(server, "plx.find", {"query": "AO-26006"})["candidates"]
        self.assertEqual([(c["object"]["display"]["untrusted_text"], c["tier"]) for c in leads],
                         [("AO-26006B", "lead")])
        self.assertNotIn(leads[0]["reason"], ("exact_identifier", "alias_match"))
        found = self.ok(server, "plx.find", {"query": "AO-26006B"})["candidates"]
        self.assertEqual({c["matched"]["property"] for c in found}, {"ao_code"})
        self.assertEqual(self.ok(server, "plx.source_citation", {
            "citation": found[0]["matched"]["raw"]["origin"]["citation"]})["row"]["nameamo_c"],
            "AO-26006B")

        cited = self.ok(server, "plx.source_citation", {"citation": citation})
        self.assertTrue(cited["ok"])
        self.assertEqual(cited["status"], "current")
        self.assertEqual(cited["row"]["nameamo_c"], "AO-26006")

        case_id = self.ok(server, "plx.case_open", {
            "question": "Why is AO-26006 short against plan?", "roots": ["AO-26006"]})["case_id"]
        fact = self.ok(server, "plx.case_add_finding", {
            "case_id": case_id, "statement": "AO-26006 planned total is 1509 EA.",
            "kind": "fact", "status": "supported", "citations": [citation]})
        self.assertEqual(fact["finding"]["label"], "cited_fact")
        self.assertEqual(fact["business_system_writes"], 0)
        # A fact without a citation is refused and records nothing.
        self.assertIn("fact_requires_citation", self.refused(server, "plx.case_add_finding", {
            "case_id": case_id, "statement": "Uncited claim", "kind": "fact",
            "status": "supported"}))

        computed = self.ok(server, "plx.case_compute", {
            "case_id": case_id,
            "inputs": {"planned": {"citation": citation, "field": "row.qtytotal", "unit": "EA"},
                       "manufactured": {"literal": "1503", "unit": "EA"}},
            "expression": "planned - manufactured", "unit": "EA"})
        self.assertEqual(computed["calculation"]["result"], "6")
        self.assertEqual(computed["calculation"]["unit"], "EA")
        self.assertIn("unit_mismatch", self.refused(server, "plx.case_compute", {
            "case_id": case_id,
            "inputs": {"planned": {"citation": citation, "field": "row.qtytotal", "unit": "EA"},
                       "price": {"literal": "12.50", "unit": "USD"}},
            "expression": "planned + price", "unit": "EA"}))
        self.assertIn("float", self.refused(server, "plx.case_compute", {
            "case_id": case_id, "inputs": {"x": {"literal": 1.5, "unit": "EA"}},
            "expression": "x", "unit": "EA"}))

        gap = self.ok(server, "plx.case_record_gap", {
            "case_id": case_id, "missing": "Inspection count for AO-26006",
            "why": "No inspection relation is in the approved catalog",
            "who_can_answer": ["Quality lead"], "state": "not_ingested"})
        self.assertEqual(gap["gap"]["gap_id"], "G1")

        proposal = self.ok(server, "plx.case_propose", {
            "case_id": case_id, "capability": "test.record.set_field",
            "payload": {"ao": "AO-26006", "note": "short by 6 EA"},
            "preconditions": [{"citation": citation, "still_current": True}]})
        self.assertFalse(proposal["dispatched"])
        self.assertFalse(proposal["approved"])
        self.assertTrue(proposal["proposal"]["status"].startswith("proposed"))

        view = self.ok(server, "plx.case_get", {"case_id": case_id})
        supported = view["findings"]["supported"]
        self.assertEqual([item["id"] for item in supported], ["F1"])
        self.assertEqual(supported[0]["statement"], "AO-26006 planned total is 1509 EA.")
        cite = supported[0]["citations"][0]
        self.assertEqual((cite["handle"], cite["status"]), (citation, "resolved"))
        self.assertEqual(cite["mirror"]["status"], "current")
        self.assertFalse(cite["mirror"]["changed_since"])
        self.assertEqual(view["calculations"][0]["result"], "6")
        self.assertEqual(view["calculations"][0]["inputs"]["planned"]["citation"]["handle"],
                         citation)
        self.assertEqual(len(view["gaps"]), 1)
        self.assertEqual(len(view["proposals"]), 1)
        self.assertEqual(view["findings"]["withheld"], [])
        self.assertTrue(view["integrity"]["chain_ok"])
        self.assertEqual(view["integrity"]["events"], 5)
        self.assertTrue(any("never dispatched" in text for text in view["limitations"]))

        listed = self.ok(server, "plx.case_get", {"status": "open"})
        self.assertEqual([item["case_id"] for item in listed["cases"]], [case_id])
        self.assertEqual(self.ok(server, "plx.case_get", {"status": "closed"})["cases"], [])
        # The listing mode takes only status and limit; status needs the listing.
        self.assertIn("case_id_required_for:section", self.refused(
            server, "plx.case_get", {"section": "gaps"}))
        self.assertIn("status_filters_the_listing_only", self.refused(
            server, "plx.case_get", {"case_id": case_id, "status": "open"}))

        # Supersede instead of edit: F2 replaces F1, F1 stays in the record.
        replaced = self.ok(server, "plx.case_add_finding", {
            "case_id": case_id, "statement": "AO-26006 planned total is 1509 EA per the AO header.",
            "kind": "fact", "status": "supported", "citations": [citation],
            "supersedes": "F1", "supersede_reason": "More precise wording"})
        self.assertEqual(replaced["finding"]["finding_id"], "F2")
        self.assertEqual(replaced["superseded"]["finding_id"], "F1")
        self.assertIn("finding_already_superseded", self.refused(
            server, "plx.case_add_finding", {
                "case_id": case_id, "statement": "Again", "kind": "interpretation",
                "status": "unknown", "supersedes": "F1", "supersede_reason": "again"}))
        view = self.ok(server, "plx.case_get", {"case_id": case_id})
        self.assertEqual([item["id"] for item in view["findings"]["supported"]], ["F2"])
        superseded = view["findings"]["superseded"]
        self.assertEqual(superseded[0]["id"], "F1")
        self.assertEqual(superseded[0]["superseded"]["replaced_by"], "F2")

        closed = self.ok(server, "plx.case_close", {"case_id": case_id,
                                                    "outcome": "Short by 6 EA; cause open."})
        self.assertEqual(closed["closed"]["kind"], "case.closed")
        self.assertIn("case_closed", self.refused(server, "plx.case_record_gap", {
            "case_id": case_id, "missing": "x", "why": "y", "who_can_answer": ["z"],
            "state": "unknown"}))

    def test_a_proposal_can_carry_its_cited_evidence_finding_in_one_write(self):
        server = self.server(OPS, self.runtime())
        citation = self.ao_citation(server)
        case_id = self.ok(server, "plx.case_open", {"question": "Note AO-26006?"})["case_id"]
        receipt = self.ok(server, "plx.case_propose", {
            "case_id": case_id, "capability": "test.record.set_field",
            "payload": {"note": "short"}, "citations": [citation],
            "evidence": {"statement": "AO-26006 total quantity is 1509 EA.",
                         "value_claims": [{"value": "1509", "citation": citation,
                                           "field": "row.qtytotal", "unit": "EA"}]}})
        proposal = receipt["proposal"]
        self.assertEqual(proposal["evidence_finding_id"], "F1")
        view = self.ok(server, "plx.case_get", {"case_id": case_id})
        [finding] = view["findings"]["supported"]
        self.assertEqual((finding["id"], finding["kind"]), ("F1", "fact"))
        self.assertEqual([cite["handle"] for cite in finding["citations"]], [citation])
        self.assertEqual(view["proposals"][0]["evidence_finding_id"], "F1")
        self.assertTrue(self.ok(server, "plx.case_get", {"case_id": case_id,
                                                         "section": "claims"})["passed"])
        events = view["integrity"]["events"]
        for arguments, code in (
                ({"evidence": {"statement": "x", "kind": "fact"}}, "unknown_arguments"),
                ({"evidence": {"statement": "AO-26006 is short."}, "citations": []},
                 "fact_requires_citation")):
            self.assertIn(code, self.refused(server, "plx.case_propose", {
                "case_id": case_id, "capability": "test.record.set_field", "payload": {}, **arguments}))
        self.assertEqual(self.ok(server, "plx.case_get", {"case_id": case_id})
                         ["integrity"]["events"], events)  # refusals record nothing

    def test_case_survives_a_new_session_and_citation_reports_upstream_change(self):
        first = self.server(OPS, self.runtime())
        case_id, citation = self.open_case_with_fact(first)
        self.fake.update("public.assembly_order", 10719, qtytotal="1510",
                         audittimestampmodified="2026-09-29T11:30:00")
        self.clock.advance(3600)
        second = self.server(OPS, self.runtime())  # new session, same state directory
        refreshed = self.read(second, "mirror.rows", {
            "relation": "public.assembly_order", "where": [["nameamo_c", "eq", "AO-26006"]]})
        self.assertNotEqual(refreshed["rows"][0]["citation"], citation)
        view = self.ok(second, "plx.case_get", {"case_id": case_id})
        cite = view["findings"]["supported"][0]["citations"][0]
        self.assertEqual(cite["status"], "resolved")  # the cited content is retained
        self.assertEqual(cite["mirror"]["status"], "superseded")
        self.assertTrue(cite["mirror"]["changed_since"])
        self.assertEqual(cite["mirror"]["current_citation"], refreshed["rows"][0]["citation"])


class PermissionTests(CaseToolsTestCase):
    def test_actor_outside_audience_sees_no_rows_and_no_case(self):
        self.add_coded_ao()
        runtime = self.runtime()
        ops = self.server(OPS, runtime)
        case_id, citation = self.open_case_with_fact(ops)
        outsider = self.server(OUTSIDER, runtime)
        self.assertEqual(self.read(outsider, "mirror.catalog")["relation_count"], 0)
        self.assertIn("not_visible", self.read_refused(outsider, "mirror.rows", {
            "relation": "public.assembly_order"}))
        self.assertTrue(self.ok(ops, "plx.find", {"query": "AO-26006B"})["candidates"])
        self.assertEqual(self.ok(outsider, "plx.find", {"query": "AO-26006B"})["candidates"],
                         [])
        denied = self.ok(outsider, "plx.source_citation", {"citation": citation})
        self.assertEqual((denied["ok"], denied["error"]), (False, "not_visible"))
        self.assertNotIn("row", denied)
        self.assertEqual(self.refused(outsider, "plx.case_get", {"case_id": case_id}),
                         "case_not_found")
        self.assertEqual(self.refused(outsider, "plx.case_get", {"case_id": "case-missing"}),
                         "case_not_found")  # unknown and invisible are indistinguishable
        self.assertEqual(self.ok(outsider, "plx.case_get")["cases"], [])
        self.assertEqual(self.refused(outsider, "plx.case_add_finding", {
            "case_id": case_id, "statement": "x", "kind": "interpretation",
            "status": "unknown"}), "case_not_found")

    def test_case_member_without_source_audience_gets_findings_withheld(self):
        runtime = self.runtime()
        both = self.server(OPS_FINANCE, runtime)
        case_id, _ = self.open_case_with_fact(both)
        finance = self.server(FINANCE, runtime)
        view = self.ok(finance, "plx.case_get", {"case_id": case_id})
        self.assertEqual(view["findings"]["supported"], [])
        withheld = view["findings"]["withheld"]
        self.assertEqual([item["id"] for item in withheld], ["F1"])
        self.assertIsNone(withheld[0]["statement"])
        self.assertEqual(withheld[0]["content"], "withheld_citation_unavailable")
        self.assertNotIn("handle", withheld[0]["citations"][0])
        self.assertNotIn("AO-26006", json.dumps(view["findings"]))

    def test_a_member_cannot_supersede_a_finding_it_sees_only_as_withheld(self):
        runtime = self.runtime()
        both = self.server(OPS_FINANCE, runtime)
        case_id, _ = self.open_case_with_fact(both)
        finance = self.server(FINANCE, runtime)
        text = self.refused(finance, "plx.case_add_finding", {
            "case_id": case_id, "statement": "F1 is wrong", "kind": "interpretation",
            "status": "unknown", "supersedes": "F1", "supersede_reason": "doubt"})
        self.assertIn("finding_not_visible", text)
        # Refused before anything is appended: no replacement finding, F1 not superseded.
        full = self.ok(both, "plx.case_get", {"case_id": case_id})
        self.assertEqual(sorted(item["id"] for bucket in full["findings"].values()
                                for item in bucket), ["F1"])
        self.assertEqual(full["findings"]["superseded"], [])

    def test_case_text_reaches_the_client_as_case_data_with_its_author(self):
        runtime = self.runtime()
        both = self.server(OPS_FINANCE, runtime)
        case_id, _ = self.open_case_with_fact(both)
        response = both.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                "params": {"name": "plx.case_get",
                                           "arguments": {"case_id": case_id}}})
        view = response["result"]["structuredContent"]
        # What the agent wrote is the case's record of it, never Workbench text.
        self.assertEqual(view["findings"]["supported"][0]["statement"], {
            "untrusted_text": "AO-26006 total quantity is 1509.",
            "origin": {"citation": f"case://{case_id}#F1", "field_path": "statement",
                       "author_kind": "agent"}})
        self.assertEqual(view["question"]["origin"], {
            "citation": f"case://{case_id}", "field_path": "question", "author_kind": "agent"})
        self.assertNotIn("workbench://", json.dumps(view["findings"]))
        # Another actor's agent may not supersede it (only a person corrects another
        # author's finding): refused before anything is appended.
        ops = self.server(OPS, runtime)
        self.assertIn("supersede_needs_human_author", self.refused(ops, "plx.case_add_finding", {
            "case_id": case_id, "statement": "F1 is wrong", "kind": "interpretation",
            "status": "unknown", "supersedes": "F1", "supersede_reason": "doubt"}))
        after = self.ok(both, "plx.case_get", {"case_id": case_id})
        self.assertEqual(sorted(item["id"] for bucket in after["findings"].values()
                                for item in bucket), ["F1"])
        # A case whose opener named no question author reads as unknown, not an error.
        older = runtime.cases.open(Actor("plx", OPS), "Is AO-26006 short?")
        header = self.ok(ops, "plx.case_get", {"case_id": older, "section": "header"})
        self.assertEqual(header["objective_author_kind"], "unknown")

    def test_revoking_the_audience_withholds_the_finding_on_next_read(self):
        server = self.server(OPS, self.runtime())
        case_id, citation = self.open_case_with_fact(server)
        self.ok(server, "plx.case_compute", {
            "case_id": case_id,
            "inputs": {"planned": {"citation": citation, "field": "row.qtytotal", "unit": "EA"}},
            "expression": "planned", "unit": "EA"})
        self.write_audience({"sage.*": ["staff:finance"]})  # public.* revoked
        view = self.ok(server, "plx.case_get", {"case_id": case_id})
        self.assertEqual(view["findings"]["supported"], [])
        self.assertEqual([item["id"] for item in view["findings"]["withheld"]], ["F1"])
        self.assertEqual(view["calculations"][0]["content"], "withheld_citation_unavailable")
        self.assertIsNone(view["calculations"][0]["result"])
        self.assertIn("not_visible", self.read_refused(server, "mirror.rows", {
            "relation": "public.assembly_order"}))
        self.write_audience(GRANTED)  # restoring the grant restores the content
        view = self.ok(server, "plx.case_get", {"case_id": case_id})
        self.assertEqual([item["id"] for item in view["findings"]["supported"]], ["F1"])

    def test_header_relations_gate_the_whole_case_on_the_current_source_audience(self):
        """Regression: a case whose question names an identifier
        held by a source relation records that relation; revoking the grant hides the
        case header from every read (get in each view, list), restoring shows it."""
        runtime = self.runtime()
        server = self.server(OPS_FINANCE, runtime)
        self.ao_citation(server)  # the identifier was read from public.assembly_order
        gated = self.ok(server, "plx.case_open", {
            "question": "Why is AO-26006 short against plan?", "roots": ["AO-26006"],
            "header_relations": [["public.assembly_order"]]})["case_id"]
        plain = self.ok(server, "plx.case_open", {"question": "Unrelated question"})["case_id"]
        ops = self.server(OPS, runtime)

        def shown(reader: MCPServer) -> tuple[list[str], str]:
            texts = [self.ok(reader, "plx.case_get")]
            listed = [item["case_id"] for item in texts[0]["cases"]]
            for view in ({}, {"section": "header"}):
                result = self.call(reader, "plx.case_get", {"case_id": gated, **view})
                if result["isError"]:
                    self.assertEqual(result["content"][0]["text"], "case_not_found")
                else:
                    texts.append(result["structuredContent"])
            return listed, json.dumps(texts)

        listed, text = shown(ops)
        self.assertEqual(set(listed), {gated, plain})
        self.assertIn("AO-26006", text)
        self.write_audience({"sage.*": ["staff:finance"]})  # public.* revoked
        for reader in (ops, server):
            listed, text = shown(reader)
            self.assertEqual(listed, [plain])  # an ungated case is unaffected
            self.assertNotIn("AO-26006", text)
        self.write_audience(GRANTED)
        listed, text = shown(ops)
        self.assertEqual(set(listed), {gated, plain})
        self.assertEqual(self.ok(ops, "plx.case_get", {"case_id": gated, "section": "header"})
                         ["roots"], ["AO-26006"])
        # Provenance the opener cannot read now, or that is not a relation, is refused.
        finance = self.server(FINANCE, runtime)
        self.assertEqual(self.refused(finance, "plx.case_open", {
            "question": "AO-26006?", "header_relations": [["public.assembly_order"]]}),
            "case_header_not_readable_by_actor")
        for bad in ([[]], [["AO-26006"]], [["public.assembly_order", 3]]):
            self.assertTrue(self.call(server, "plx.case_open", {
                "question": "q", "header_relations": bad})["isError"], bad)

    def test_cited_gaps_and_proposals_are_withheld_from_readers_who_cannot_cite(self):
        runtime = self.runtime()
        both = self.server(OPS_FINANCE, runtime)
        case_id, citation = self.open_case_with_fact(both)
        self.ok(both, "plx.case_record_gap", {
            "case_id": case_id, "missing": "Inspection count for AO-26006",
            "why": "Planned 1509 but no inspection row", "who_can_answer": ["Quality lead"],
            "state": "unknown", "citations": [citation]})
        self.ok(both, "plx.case_propose", {
            "case_id": case_id, "capability": "test.record.set_field",
            "payload": {"ao": "AO-26006", "note": "short by 6 EA"}, "citations": [citation]})
        for tool, arguments in (("plx.case_record_gap", {
                "missing": "m", "why": "w", "who_can_answer": ["x"], "state": "unknown"}),
                ("plx.case_propose", {"capability": "test.record.set_field", "payload": {}})):
            self.assertIn("citation_scheme_not_supported", self.refused(both, tool, {
                "case_id": case_id, **arguments, "citations": ["file:///po.pdf"]}))
        full = self.ok(both, "plx.case_get", {"case_id": case_id})
        self.assertEqual(full["gaps"][0]["missing"], "Inspection count for AO-26006")
        self.assertEqual(full["proposals"][0]["citations"][0]["mirror"]["status"], "current")
        finance = self.server(FINANCE, runtime)
        for arguments in ({}, {"section": "gaps"}, {"section": "proposals"}):
            view = self.ok(finance, "plx.case_get", {"case_id": case_id, **arguments})
            items = view.get("items") or view.get("gaps", []) + view.get("proposals", [])
            self.assertTrue(items)
            for item in items:
                self.assertEqual(item["content"], "withheld_citation_unavailable")
            blob = json.dumps(items)
            for secret in ("AO-26006", "1509", "short by", citation, "assembly_order"):
                self.assertNotIn(secret, blob, secret)
        self.write_audience({"sage.*": ["staff:finance"]})  # public.* revoked for everyone
        view = self.ok(both, "plx.case_get", {"case_id": case_id})
        self.assertEqual(view["gaps"][0]["content"], "withheld_citation_unavailable")
        self.assertIsNone(view["proposals"][0]["payload"])

    def test_unreadable_audience_file_fails_closed(self):
        server = self.server(OPS, self.runtime())
        case_id, _ = self.open_case_with_fact(server)
        self.audience_path.unlink()
        self.assertIn("not_visible", self.read_refused(server, "mirror.rows", {
            "relation": "public.assembly_order"}))
        view = self.ok(server, "plx.case_get", {"case_id": case_id})
        self.assertEqual([item["id"] for item in view["findings"]["withheld"]], ["F1"])

    def test_unknown_citation_schemes_are_rejected_and_withheld(self):
        runtime = self.runtime()
        server = self.server(OPS, runtime)
        case_id, _ = self.open_case_with_fact(server)
        for handle in ("file:///etc/passwd", "sp://site/doc@1", "https://example.test/x"):
            self.assertIn("citation_scheme_not_supported", self.refused(
                server, "plx.case_add_finding", {
                    "case_id": case_id, "statement": "From elsewhere", "kind": "interpretation",
                    "status": "unknown", "citations": [handle]}))
            self.assertIn("citation_scheme_not_supported", self.refused(
                server, "plx.case_compute", {
                    "case_id": case_id,
                    "inputs": {"x": {"citation": handle, "field": "row.v", "unit": "EA"}},
                    "expression": "x", "unit": "EA"}))
            # The store's resolver fails closed too, below the MCP pre-check.
            with self.assertRaises(CitationUnresolvable):
                runtime.resolve_citation(handle, server.actor)
            with self.assertRaises(CitationUnresolvable):
                runtime.cases.finding(server.actor, case_id, "bypass",
                                      "interpretation", "unknown", [handle])
        self.assertEqual(self.ok(server, "plx.case_get", {"case_id": case_id})
                         ["integrity"]["events"], 2)
        # A well-formed api:// citation that names no mirrored row is also refused.
        self.assertEqual(self.refused(server, "plx.case_add_finding", {
            "case_id": case_id, "statement": "ghost", "kind": "fact", "status": "supported",
            "citations": ["api://public.assembly_order/%7B%22id%22%3A1%7D@1"]}),
            "citation_unresolvable_for_actor")


# Page 1: short OCR text; page 2: long text layer whose JSON escaping (control
# characters, quotes, backslashes) and multi-byte characters stress the bound.
PAGE_ONE = "AO-26005B\nPLANNED 1509"
PAGE_TWO = ("\x01\"\\é€𝄞 line\n" * 9000)[:90000]


def _two_page_extractor(content, mime_type, *, original_url, filename="",
                        local_pdf_ocr=False):
    text = PAGE_ONE + "\f" + PAGE_TWO
    pages = ({"page": 1, "start": 0, "end": len(PAGE_ONE)},
             {"page": 2, "start": len(PAGE_ONE) + 1, "end": len(text)})
    return ExtractedDocument("application/pdf", "x", "extracted", "stub-ocr", text, (), (),
                             (), pages, ocr_pages=(1,))


class OriginalReadTests(CaseToolsTestCase):
    """plx.source_citation reads original:// citations: bounded, audience-checked."""

    def runtime(self) -> CaseRuntime:
        runtime = CaseRuntime(self.state, client=WorkbenchReadApiClient(BASE, KEY, fetch=self.fake),
                              audience=self.audience_path, tenant="plx",
                              mirror_options={"clock": self.clock, "retry_delay_seconds": 0},
                              originals_options={"extractor": _two_page_extractor,
                                                 "local_ocr": True})
        self.addCleanup(runtime.close)
        return runtime

    def attach(self, server: MCPServer, runtime: CaseRuntime) -> tuple[str, dict]:
        case_id, _ = self.open_case_with_fact(server)
        inbox = runtime.originals.inbox / "staff:operations"
        inbox.mkdir()
        (inbox / "scan.pdf").write_bytes(b"%PDF-1.4\n% synthetic\n")
        attached = self.ok(server, "plx.case_attach_original",
                           {"case_id": case_id, "inbox_name": "scan.pdf"})
        return case_id, attached["original"]

    def bounded(self, server: MCPServer, arguments: dict) -> dict:
        result = self.call(server, "plx.source_citation", arguments)
        self.assertFalse(result["isError"], result)
        size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())
        self.assertLessEqual(size, MAX_RESULT_BYTES)
        return result["structuredContent"]

    def test_file_citation_lists_pages_and_page_citation_returns_labelled_text(self):
        runtime = self.runtime()
        server = self.server(OPS, runtime)
        _, original = self.attach(server, runtime)
        whole = self.bounded(server, {"citation": original["citation"]})
        self.assertEqual((whole["ok"], whole["status"], whole["sha256"], whole["page_citations"]),
                         (True, "current", original["sha256"], original["page_citations"]))
        self.assertNotIn("text", whole)
        first = self.bounded(server, {"citation": original["page_citations"][0]})
        self.assertEqual((first["page"], first["text"], first["text_source"], first["text_complete"],
                          first["next_text_offset"], first["page_text_chars"]),
                         (1, PAGE_ONE, "ocr", True, None, len(PAGE_ONE)))
        self.assertIn("machine-read", first["note"])
        self.assertEqual(first["page_text_sha256"],
                         hashlib.sha256(PAGE_ONE.encode()).hexdigest())
        # The digest a case records for these citations is unchanged by the read path.
        actor = server.actor
        self.assertNotIn("page_citations", runtime.resolve_citation(original["citation"], actor))

    def test_long_page_is_paged_to_the_end_within_the_result_bound(self):
        runtime = self.runtime()
        server = self.server(OPS, runtime)
        _, original = self.attach(server, runtime)
        page = original["page_citations"][1]
        parts, offset, calls = [], 0, 0
        while offset is not None:
            calls += 1
            self.assertLess(calls, 200)
            result = self.bounded(server, {"citation": page, "text_offset": offset,
                                           "max_text_chars": 40000})
            self.assertEqual(result["text_offset"], offset)
            self.assertEqual(result["text_chars"], len(result["text"]))
            self.assertGreater(result["text_chars"], 0)
            self.assertFalse(result["text_complete"])
            self.assertEqual(result["text_source"], "text_layer")
            self.assertNotIn("note", result)
            parts.append(result["text"])
            offset = result["next_text_offset"]
        self.assertGreater(calls, 3)  # the escaping forced slices below 40000 chars
        text = "".join(parts)
        self.assertEqual(text, PAGE_TWO)
        self.assertEqual(result["page_text_sha256"],
                         hashlib.sha256(text.encode("utf-8")).hexdigest())
        default = self.bounded(server, {"citation": page})
        self.assertLessEqual(default["text_chars"], DEFAULT_ORIGINAL_TEXT_CHARS)
        small = self.bounded(server, {"citation": page, "text_offset": 5, "max_text_chars": 3})
        self.assertEqual((small["text"], small["next_text_offset"]), (PAGE_TWO[5:8], 8))
        end = self.bounded(server, {"citation": page, "text_offset": len(PAGE_TWO)})
        self.assertEqual((end["text"], end["next_text_offset"], end["text_complete"]),
                         ("", None, False))
        self.assertIn("argument_out_of_bounds:text_offset", self.refused(
            server, "plx.source_citation", {"citation": page, "text_offset": len(PAGE_TWO) + 1}))

    def test_outsiders_cannot_read_or_probe_and_changed_bytes_fail_closed(self):
        runtime = self.runtime()
        server = self.server(OPS, runtime)
        _, original = self.attach(server, runtime)
        outsider = self.server(FINANCE, runtime)
        ghost = "original://orig-" + "0" * 32
        for handle in (original["citation"], original["page_citations"][0], ghost,
                       ghost + "#page=1"):
            denied = self.bounded(outsider, {"citation": handle})
            self.assertEqual(denied, {"ok": False, "error": "original_not_visible",
                                      "citation": handle})
        self.assertEqual(self.bounded(server, {"citation": ghost})["error"],
                         "original_not_visible")
        self.assertEqual(self.bounded(server, {"citation": original["citation"] + "#page=9"})
                         ["error"], "original_page_not_found")
        self.assertEqual(self.bounded(server, {"citation": "original://../../etc/passwd"})
                         ["error"], "invalid_original_citation")
        # Paging arguments apply only to a page citation.
        for handle in (original["citation"], self.ao_citation(server), ghost):
            self.assertIn("text_paging_requires_a_page_citation", self.refused(
                server, "plx.source_citation", {"citation": handle, "text_offset": 0}))
        blob = runtime.originals.store / original["sha256"]
        blob.write_bytes(blob.read_bytes() + b"%tampered")
        for handle in (original["citation"], original["page_citations"][0]):
            changed = self.bounded(server, {"citation": handle})
            self.assertEqual((changed["ok"], changed["error"]),
                             (False, "original_content_changed"))
            self.assertNotIn("text", changed)


def _no_ocr_extractor(content, mime_type, *, original_url, filename="", local_pdf_ocr=False):
    if not local_pdf_ocr:
        raise ValidationError("local_pdf_ocr_unavailable: OCR disabled on this host")
    return _two_page_extractor(content, mime_type, original_url=original_url,
                               filename=filename, local_pdf_ocr=local_pdf_ocr)


class ReextractToolTests(OriginalReadTests):
    """plx.case_attach_original with citation + reason (re-extraction): explicit,
    versioned, cited, audience-checked."""

    local_ocr = False

    def runtime(self) -> CaseRuntime:
        runtime = CaseRuntime(self.state, client=WorkbenchReadApiClient(BASE, KEY, fetch=self.fake),
                              audience=self.audience_path, tenant="plx",
                              mirror_options={"clock": self.clock, "retry_delay_seconds": 0},
                              originals_options={"extractor": _no_ocr_extractor,
                                                 "local_ocr": self.local_ocr})
        self.addCleanup(runtime.close)
        return runtime

    def test_reextraction_records_a_cited_finding_and_keeps_earlier_citations(self):
        runtime = self.runtime()
        server = self.server(OPS, runtime)
        case_id, original = self.attach(server, runtime)
        self.assertEqual((original["extraction"]["status"], original["page_citations"]),
                         ("failed", []))
        before = self.bounded(server, {"citation": original["citation"]})
        # Without OCR the repeat is identical: nothing is recorded.
        same = self.ok(server, "plx.case_attach_original", {
            "case_id": case_id, "citation": original["citation"], "reason": "retry"})
        self.assertEqual((same["duplicate"], same["finding"], same["extraction_version"]["version"]),
                         (True, None, 1))
        events = self.ok(server, "plx.case_get", {"case_id": case_id})["integrity"]["events"]
        self.local_ocr = True
        enabled_runtime = self.runtime()
        enabled = self.server(OPS, enabled_runtime)
        result = self.ok(enabled, "plx.case_attach_original", {
            "case_id": case_id, "citation": original["citation"],
            "reason": "OCR enabled on the host"})
        version = result["extraction_version"]
        self.assertEqual((result["duplicate"], version["version"], version["local_ocr_enabled"],
                          result["business_system_writes"]), (False, 2, True, 0))
        view = self.ok(enabled, "plx.case_get", {"case_id": case_id})
        self.assertEqual(view["integrity"]["events"], events + 1)
        finding = view["findings"]["supported"][-1]
        self.assertIn("extraction version 2", finding["statement"])
        self.assertIn("machine-read", finding["statement"])
        self.assertEqual({cite["handle"] for cite in finding["citations"]},
                         {original["citation"], version["citation"]})
        self.assertEqual(self.bounded(enabled, {"citation": original["citation"]})["extraction"],
                         before["extraction"])
        page = self.bounded(enabled, {"citation": version["page_citations"][0]})
        self.assertEqual((page["text"], page["text_source"], page["extraction_version"]),
                         (PAGE_ONE, "ocr", 2))
        self.ok(enabled, "plx.case_add_finding", {
            "case_id": case_id, "kind": "fact", "status": "supported",
            "statement": "OCR page 1 reads PLANNED 1509 (machine-read).",
            "citations": [version["page_citations"][0]]})
        # A case member outside the original's audience can neither re-extract
        # nor read the re-extraction finding.
        shared_case = self.ok(self.server(OPS_FINANCE, enabled_runtime), "plx.case_open", {
            "question": "Shared with finance"})["case_id"]
        finance = self.server(FINANCE, enabled_runtime)
        self.assertEqual(self.ok(finance, "plx.case_get", {"case_id": shared_case})["status"],
                         "open")
        for handle in (original["citation"], version["citation"]):
            self.assertIn("original_not_visible", self.refused(
                finance, "plx.case_attach_original",
                {"case_id": shared_case, "citation": handle, "reason": "x"}))
        self.assertIn("case_not_found", self.refused(finance, "plx.case_attach_original", {
            "case_id": case_id, "citation": original["citation"], "reason": "x"}))
        self.assertEqual(self.ok(finance, "plx.case_get", {"case_id": shared_case})
                         ["integrity"]["events"], 1)  # refusals record nothing
        for handle, code in (("api://public.assembly_order/%7B%7D@1", "citation_scheme_not_supported"),
                             (original["citation"] + "#extraction=9", "original_extraction_not_found")):
            self.assertIn(code, self.refused(enabled, "plx.case_attach_original", {
                "case_id": case_id, "citation": handle, "reason": "x"}))
        self.ok(enabled, "plx.case_close", {"case_id": case_id, "outcome": "done"})
        self.assertIn("case_closed", self.refused(enabled, "plx.case_attach_original", {
            "case_id": case_id, "citation": original["citation"], "reason": "x"}))

    def test_attach_and_reextract_modes_are_exclusive(self):
        runtime = self.runtime()
        server = self.server(OPS, runtime)
        case_id, original = self.attach(server, runtime)
        events = self.ok(server, "plx.case_get", {"case_id": case_id})["integrity"]["events"]
        for arguments, code in (
                ({"case_id": case_id}, "pass_exactly_one_of:inbox_name,drive_id,citation"),
                ({"case_id": case_id, "inbox_name": "x.pdf",
                  "citation": original["citation"], "reason": "x"},
                 "pass_exactly_one_of:inbox_name,drive_id,citation"),
                ({"case_id": case_id, "drive_id": "b!drive", "item_id": "AO1",
                  "inbox_name": "x.pdf"}, "pass_exactly_one_of:inbox_name,drive_id,citation"),
                ({"case_id": case_id, "inbox_name": "x.pdf", "reason": "x"},
                 "reason_does_not_apply_with_inbox_name"),
                ({"case_id": case_id, "inbox_name": "x.pdf", "item_id": "AO1"},
                 "item_id_does_not_apply_with_inbox_name"),
                ({"case_id": case_id, "drive_id": "b!drive", "item_id": "AO1", "reason": "x"},
                 "reason_does_not_apply_with_drive_id"),
                ({"case_id": case_id, "citation": original["citation"], "reason": "x",
                  "inbox_principal": "staff:operations"},
                 "inbox_principal_does_not_apply_with_citation"),
                ({"case_id": case_id, "citation": original["citation"]},
                 "missing_argument:reason")):
            self.assertIn(code, self.refused(server, "plx.case_attach_original", arguments))
        self.assertEqual(self.ok(server, "plx.case_get", {"case_id": case_id})
                         ["integrity"]["events"], events)  # refusals record nothing

    def test_a_version_already_held_is_recorded_on_a_case_that_lacks_it(self):
        self.local_ocr = True
        runtime = self.runtime()
        server = self.server(OPS, runtime)
        case_id, original = self.attach(server, runtime)  # attached with OCR: version 1
        # Version 2 made directly in the store, as if the case finding write had
        # failed after the version was stored (or another case re-extracted it).
        runtime.originals._extract = lambda *a, **k: ExtractedDocument(
            "application/pdf", "x", "extracted", "stub-ocr", "OTHER", (), (), (),
            ({"page": 1, "start": 0, "end": 5},), ocr_pages=(1,))
        runtime.originals.reextract(server.actor, original["citation"], "x")
        first = self.ok(server, "plx.case_attach_original", {
            "case_id": case_id, "citation": original["citation"], "reason": "retry"})
        self.assertEqual((first["duplicate"], first["extraction_version"]["version"]), (True, 2))
        self.assertIsNotNone(first["finding"])
        self.assertIn("extraction version 2", self.ok(server, "plx.case_get", {
            "case_id": case_id})["findings"]["supported"][-1]["statement"])
        again = self.ok(server, "plx.case_attach_original", {
            "case_id": case_id, "citation": original["citation"], "reason": "retry"})
        self.assertIsNone(again["finding"])

    def test_every_original_result_fits_at_the_worst_case_the_store_allows(self):
        """Regression matrix for the result-size family: attach, re-extract (new
        version and duplicate retry) and file/page reads, for version 1 and the
        last version, with 200 OCR pages, 20 long warnings, 1000-char reasons and
        the version cap. Every result must fit, keep its outcome readable and
        name every page by list or pattern."""
        from operating import case_originals
        self.local_ocr = True
        runtime = self.runtime()
        server = self.server(OPS, runtime)

        def heavy(tag):
            def extract(*args, **kwargs):
                parts = [f"{tag} page {n} " + "w" * 40 for n in range(1, 201)]
                text, spans, start = "\f".join(parts), [], 0
                for n, part in enumerate(parts, 1):
                    spans.append({"page": n, "start": start, "end": start + len(part)})
                    start += len(part) + 1
                return ExtractedDocument("application/pdf", "x", "extracted", "stub-ocr",
                                         text, (), (), tuple("w" * 200 for _ in range(20)),
                                         tuple(spans), ocr_pages=tuple(range(1, 201)))
            return extract

        runtime.originals._extract = heavy("v1")
        case_id, original = self.attach(server, runtime)  # attach: bounded
        self.assertEqual(original["extraction"]["page_count"], 200)
        results = []
        for number in range(2, case_originals.MAX_EXTRACTIONS + 1):
            runtime.originals._extract = heavy(f"v{number}")
            results.append(self.ok(server, "plx.case_attach_original", {
                "case_id": case_id, "citation": original["citation"], "reason": "r" * 1000}))
        retry = self.ok(server, "plx.case_attach_original", {
            "case_id": case_id, "citation": original["citation"], "reason": "r" * 1000})
        self.assertTrue(retry["duplicate"])
        for result in results + [retry]:
            version = result["extraction_version"]
            self.assertIn(version["version"], range(2, case_originals.MAX_EXTRACTIONS + 1))
            if "page_citations" not in version:
                self.assertTrue(version["page_citations_omitted"])
                self.assertEqual(version["page_citation_pattern"].replace("<n>", "7"),
                                 version["citation"] + "&page=7")
        view = self.ok(server, "plx.case_get", {"case_id": case_id})
        supported = (view["counts"]["findings"]["supported"] if view["view"] == "summary"
                     else len(view["findings"]["supported"]))
        self.assertEqual(supported, 1 + 1 + 19)  # AO fact, attach, 19 new versions
        latest = f"{original['citation']}#extraction={case_originals.MAX_EXTRACTIONS}"
        for handle in (original["citation"], latest):
            whole = self.bounded(server, {"citation": handle})
            self.assertEqual(len(whole["extractions"]), case_originals.MAX_EXTRACTIONS)
            self.assertEqual(whole["extraction"]["page_count"], 200)
            pages = whole.get("page_citations")
            if pages is None:
                self.assertTrue(whole["page_citations_omitted"])
                pages = [whole["page_citation_pattern"].replace("<n>", str(n))
                         for n in range(1, 201)]
            self.assertEqual(len(pages), 200)
            for handle_of_page in (pages[0], pages[-1]):
                self.assertTrue(self.bounded(server, {"citation": handle_of_page})["text"])
        self.assertEqual(self.bounded(server, {"citation": original["citation"] + "#page=1"})
                         ["text"], "v1 page 1 " + "w" * 40)

    # The read-path tests of the parent class need page text at attach time.
    test_file_citation_lists_pages_and_page_citation_returns_labelled_text = None
    test_long_page_is_paged_to_the_end_within_the_result_bound = None
    test_outsiders_cannot_read_or_probe_and_changed_bytes_fail_closed = None


class BooleanArgumentTests(CaseToolsTestCase):
    """CR-001: explicit true/false options used to fail inside _arg (len(bool))."""

    def test_true_false_and_omitted_booleans_work_through_handle(self):
        server = self.server(OPS, self.runtime())
        case_id, citation = self.open_case_with_fact(server)
        for value, expect_mirror in ((None, True), (True, True), (False, False)):
            arguments = {"case_id": case_id}
            if value is not None:
                arguments["include_citation_status"] = value
            cite = self.ok(server, "plx.case_get", arguments)[
                "findings"]["supported"][0]["citations"][0]
            self.assertEqual(cite["handle"], citation)
            self.assertEqual("mirror" in cite, expect_mirror, value)
        for arguments in ({"case_id": case_id, "include_citation_status": 0},
                          {"case_id": case_id, "include_citation_status": "yes"}):
            self.assertIn("invalid_argument_type",
                          self.refused(server, "plx.case_get", arguments))


class CitationGenerationTests(CaseToolsTestCase):
    """CR-003 / CR-004 through the MCP tools."""

    def remove_mirror(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = self.state / CASE_RUNTIME_DIR / ("source-mirror.sqlite" + suffix)
            if path.exists():
                path.unlink()

    def test_rebuilt_mirror_withholds_old_citations_instead_of_vouching_for_new_content(self):
        runtime = self.runtime()
        first = self.server(OPS, runtime)
        case_id, citation = self.open_case_with_fact(first)
        self.ok(first, "plx.case_compute", {
            "case_id": case_id,
            "inputs": {"planned": {"citation": citation, "field": "row.qtytotal", "unit": "EA"}},
            "expression": "planned", "unit": "EA"})
        runtime.close()
        self.remove_mirror()  # disposable mirror rebuilt; the case store is kept
        self.fake.update("public.assembly_order", 10719, qtytotal="9999")
        second = self.server(OPS, self.runtime())
        new_citation = self.ao_citation(second)
        self.assertNotEqual(new_citation, citation)  # capture id 1 again, new generation
        answer = self.ok(second, "plx.source_citation", {"citation": citation})
        self.assertEqual((answer["ok"], answer["error"], answer["reason"]),
                         (False, "capture_unknown", "mirror_generation_mismatch"))
        view = self.ok(second, "plx.case_get", {"case_id": case_id})
        self.assertEqual(view["findings"]["supported"], [])
        self.assertEqual([item["id"] for item in view["findings"]["withheld"]], ["F1"])
        self.assertEqual(view["calculations"][0]["content"], "withheld_citation_unavailable")
        self.assertNotIn("9999", json.dumps(view))
        self.assertTrue(any("new generation" in text for text in view["limitations"]))

    def test_citation_resolving_to_different_content_is_not_supported(self):
        runtime = self.runtime()
        server = self.server(OPS, runtime)
        case_id, citation = self.open_case_with_fact(server)
        # Defense in depth: corrupt the cited capture in place (no API path does
        # this); the read-time digest check must catch it.
        with sqlite3.connect(self.state / CASE_RUNTIME_DIR / "source-mirror.sqlite") as db:
            db.execute("UPDATE row_current SET row_json = replace(row_json, '1509', '9999')")
        view = self.ok(server, "plx.case_get", {"case_id": case_id})
        self.assertEqual(view["findings"]["supported"], [])
        changed = view["findings"]["citation_content_changed"]
        self.assertEqual([item["id"] for item in changed], ["F1"])
        self.assertEqual(changed[0]["recorded_status"], "supported")
        cite = changed[0]["citations"][0]
        self.assertEqual((cite["handle"], cite["status"]), (citation, "citation_content_changed"))
        self.assertIn("mirror", cite)

    def test_calculation_inputs_carry_mirror_status_and_result_stays_recorded(self):
        first = self.server(OPS, self.runtime())
        case_id, citation = self.open_case_with_fact(first)
        self.ok(first, "plx.case_compute", {
            "case_id": case_id,
            "inputs": {"planned": {"citation": citation, "field": "row.qtytotal", "unit": "EA"},
                       "made": {"literal": "1503", "unit": "EA"}},
            "expression": "planned - made", "unit": "EA"})
        view = self.ok(first, "plx.case_get", {"case_id": case_id})
        planned = view["calculations"][0]["inputs"]["planned"]
        self.assertEqual(planned["citation"]["status"], "resolved")
        self.assertEqual(planned["citation"]["mirror"]["status"], "current")
        self.assertNotIn("citation", view["calculations"][0]["inputs"]["made"])
        self.fake.update("public.assembly_order", 10719, qtytotal="1510",
                         audittimestampmodified="2026-09-29T11:30:00")
        self.clock.advance(3600)
        second = self.server(OPS, self.runtime())
        current = self.ao_citation(second)
        for arguments in ({"case_id": case_id},
                          {"case_id": case_id, "section": "calculations"}):
            result = self.ok(second, "plx.case_get", arguments)
            calc = (result["calculations"] if "calculations" in result else result["items"])[0]
            self.assertEqual((calc["result"], calc["inputs"]["planned"]["value"]), ("6", "1509"))
            self.assertEqual(calc["inputs_status"], "resolved")  # retained content matches
            mirror = calc["inputs"]["planned"]["citation"]["mirror"]
            self.assertEqual(mirror["status"], "superseded")
            self.assertTrue(mirror["changed_since"])
            self.assertEqual(mirror["current_citation"], current)

    def test_cited_json_number_with_a_fraction_is_computable_through_the_mirror(self):
        # A JSON number with a fraction reached plx.case_compute
        # as a float and was refused, so the cited field could not be computed.
        for row in self.fake.tables["public.assembly_order"]:
            if row["id"] == 10719:
                row["qtytotal"] = 1509.5
        server = self.server(OPS, self.runtime())
        case_id, citation = self.open_case_with_fact(server)
        computed = self.ok(server, "plx.case_compute", {
            "case_id": case_id,
            "inputs": {"planned": {"citation": citation, "field": "row.qtytotal", "unit": "EA"},
                       "made": {"literal": "1503", "unit": "EA"}},
            "expression": "planned - made", "unit": "EA"})
        self.assertEqual(computed["calculation"]["result"], "6.5")
        planned = self.ok(server, "plx.case_get",
                          {"case_id": case_id})["calculations"][0]["inputs"]["planned"]
        self.assertEqual(planned["value"], "1509.5")
        self.assertEqual(planned["source_number"]["raw"], "1509.5")
        self.assertEqual(planned["source_number"]["rule"], "binary64-shortest-repr")
        self.assertEqual(planned["citation"]["status"], "resolved")


class ResultBoundTests(CaseToolsTestCase):
    """CR-002: every case_get response fits the doubled envelope."""

    def call_bounded(self, server: MCPServer, name: str, arguments: dict) -> dict:
        result = self.call(server, name, arguments)
        self.assertFalse(result["isError"], result["content"][0]["text"][:200])
        size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())
        self.assertLessEqual(size, MAX_RESULT_BYTES)
        self.assertEqual(as_text(json.loads(result["content"][0]["text"])),
                         result["structuredContent"])
        return result["structuredContent"]

    def page_all(self, server: MCPServer, name: str, arguments: dict, key: str) -> list:
        items, offset, pages = [], 0, 0
        while offset is not None:
            page = self.call_bounded(server, name, {**arguments, "offset": offset})
            self.assertEqual(page["offset"], offset)
            self.assertEqual(page["returned"], len(page[key]))
            self.assertGreater(page["returned"], 0)
            items += page[key]
            offset = page["next_offset"]
            pages += 1
        return items, pages

    def test_large_case_is_summarized_and_every_event_is_retrievable_by_page(self):
        server = self.server(OPS, self.runtime())
        case_id, citation = self.open_case_with_fact(server)
        statements = {}
        for index in range(6):
            # Quotes, backslashes and non-ASCII are escaped twice in the envelope.
            text = (f'F{index + 2} qty "1509" \\ größe — ' * 200)[:3999] + "."
            self.assertEqual(len(text), 4000)
            receipt = self.ok(server, "plx.case_add_finding", {
                "case_id": case_id, "statement": text, "kind": "fact",
                "status": "supported", "citations": [citation]})
            statements[receipt["finding"]["finding_id"]] = text
        self.ok(server, "plx.case_compute", {
            "case_id": case_id,
            "inputs": {"planned": {"citation": citation, "field": "row.qtytotal", "unit": "EA"}},
            "expression": "planned", "unit": "EA"})
        self.ok(server, "plx.case_record_gap", {
            "case_id": case_id, "missing": "m" * 4000, "why": "w" * 4000,
            "who_can_answer": ["Quality lead"], "state": "unknown"})
        self.ok(server, "plx.case_propose", {
            "case_id": case_id, "capability": "test.record.set_field", "payload": {"note": "n" * 4000}})
        # Before the fix this whole view failed with result_too_large.
        summary = self.call_bounded(server, "plx.case_get", {"case_id": case_id})
        self.assertEqual((summary["view"], summary["complete"]), ("summary", False))
        self.assertIn("exceeds one response", summary["reason"])
        self.assertEqual(summary["counts"]["findings"]["supported"], 7)
        self.assertEqual(summary["counts"]["timeline"], 11)
        self.assertEqual(summary["integrity"], {"events": 11, "chain_ok": True})

        findings, pages = self.page_all(server, "plx.case_get",
                                        {"case_id": case_id, "section": "findings"}, "items")
        self.assertGreater(pages, 1)
        self.assertEqual([item["id"] for item in findings], [f"F{i}" for i in range(1, 8)])
        for item in findings:
            self.assertEqual(item["bucket"], "supported")
            self.assertEqual(item["citations"][0]["mirror"]["status"], "current")
            if item["id"] in statements:
                self.assertEqual(item["statement"], statements[item["id"]])
        timeline, _ = self.page_all(server, "plx.case_get",
                                    {"case_id": case_id, "section": "timeline", "limit": 4},
                                    "items")
        self.assertEqual([event["seq"] for event in timeline], list(range(1, 12)))
        for section, expected in (("calculations", ["C1"]), ("gaps", ["G1"]),
                                  ("proposals", ["P1"])):
            items, _ = self.page_all(server, "plx.case_get",
                                     {"case_id": case_id, "section": section}, "items")
            self.assertEqual([item["id"] for item in items], expected)
        self.assertEqual(len(items[0]["payload"]["note"]), 4000)
        header = self.call_bounded(server, "plx.case_get",
                                   {"case_id": case_id, "section": "header"})
        self.assertEqual(header["question"], "Why is AO-26006 short against plan?")
        self.assertIn("invalid_section", self.refused(
            server, "plx.case_get", {"case_id": case_id, "section": "everything"}))
        # A small case is still returned whole in one call.
        small = self.ok(server, "plx.case_open", {"question": "small"})["case_id"]
        self.assertEqual(self.call_bounded(server, "plx.case_get", {"case_id": small})["view"],
                         "full")

class ConfigurationTests(CaseToolsTestCase):
    def test_no_session_exists_without_a_configured_runtime(self):
        # The platform host that could run without a case runtime is retired
        # (deletion-6): a session without one cannot be built, so nothing is read.
        with self.assertRaisesRegex(ValueError, "case_only_session_requires_case_runtime"):
            MCPServer(case_runtime=None, actor=Actor("plx", OPS))
        self.assertEqual(self.fake.paths, [])

    def test_case_only_session_serves_exactly_the_case_tools(self):
        server = self.server(OPS, self.runtime())
        listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual({tool["name"] for tool in listed["result"]["tools"]},
                         set(CASE_RUNTIME_TOOLS))
        self.assertFalse(hasattr(server, "platform"))
        refused = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                 "params": {"name": "plx.search",
                                            "arguments": {"query": "AO-26006"}}})
        self.assertEqual(refused["error"]["message"], "Unknown tool")
        with self.assertRaises(TypeError):
            MCPServer(actor=Actor("plx", OPS))  # no runtime: nothing to serve
        # A session always names its actor and takes no platform (deletion-6).
        with self.assertRaises(TypeError):
            MCPServer(case_runtime=self.runtime())
        with self.assertRaisesRegex(ValueError, "case_session_requires_actor"):
            MCPServer(case_runtime=self.runtime(), actor=None)
        with self.assertRaises(TypeError):
            MCPServer(None, case_runtime=self.runtime())
        with self.assertRaises(TypeError):
            MCPServer(object(), case_runtime=self.runtime(), actor=Actor("plx", OPS))

    def test_stdio_host_with_only_an_audience_never_builds_the_platform(self):
        import subprocess
        import sys
        root = Path(__file__).resolve().parents[2]
        state = self.root / "host-state"
        command = [sys.executable, "-m", "operating.mcp_server", "--state-dir", str(state),
                   "--tenant", "plx", "--fixture", "--principal", "staff:operations",
                   "--source-mirror-audience", str(self.audience_path)]
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                "clientInfo": {"name": "host-test", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        done = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=60,
                              input="".join(json.dumps(item) + "\n" for item in messages))
        self.assertEqual(done.returncode, 0, done.stderr)
        replies = [json.loads(line) for line in done.stdout.splitlines()]
        self.assertEqual(replies[0]["result"]["instructions"], CASE_ONLY_INSTRUCTIONS)
        self.assertEqual({tool["name"] for tool in replies[1]["result"]["tools"]},
                         set(CASE_RUNTIME_TOOLS))
        # The case runtime and the host's fixture/live label (operating.session_identity).
        self.assertEqual(sorted(path.name for path in state.iterdir()),
                         [CASE_RUNTIME_DIR, "session-state-label.json"])
        loaded = subprocess.run([sys.executable, "-c", (
            "import sys, operating.mcp_server, operating.case_workspace_live\n"
            "print(sorted(m for m in sys.modules if m in {'operating.unified', "
            "'operating.objectives', 'operating.campaigns', 'operating.business_watch'}))")],
            cwd=root, capture_output=True, text=True, timeout=60)
        self.assertEqual(loaded.stdout.strip(), "[]", loaded.stderr)
        refused = subprocess.run(command[:-2] + ["--enable-workbench-read-api"], cwd=root,
                                 capture_output=True, text=True, timeout=60, input="")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("the following arguments are required: --source-mirror-audience",
                      refused.stderr)

    def test_runtime_requires_a_valid_audience_and_uses_the_state_directory(self):
        with self.assertRaises(CaseRuntimeUnavailable):
            CaseRuntime(self.state, client=None, audience=None, tenant="plx")
        bad = self.root / "bad.json"
        bad.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                   "audiences": {"public.*": ["*"]}}), encoding="utf-8")
        with self.assertRaises(ValueError):
            CaseRuntime(self.state, client=None, audience=bad, tenant="plx")
        runtime = self.runtime()
        self.assertTrue(Path(runtime.cases.path).is_relative_to(self.state / CASE_RUNTIME_DIR))
        self.assertTrue((self.state / CASE_RUNTIME_DIR / "source-mirror.sqlite").exists())

    def test_offline_runtime_answers_only_from_held_captures(self):
        online = self.server(OPS, self.runtime())
        case_id, citation = self.open_case_with_fact(online)
        offline_runtime = CaseRuntime(self.state, client=None, audience=self.audience_path,
                                      tenant="plx", mirror_options={"clock": self.clock})
        self.addCleanup(offline_runtime.close)
        offline = self.server(OPS, offline_runtime)
        self.assertFalse(offline_runtime.client_configured)
        view = self.ok(offline, "plx.case_get", {"case_id": case_id})
        self.assertEqual(view["findings"]["supported"][0]["citations"][0]["handle"], citation)
        self.assertIn("source_unavailable", self.read_refused(offline, "mirror.rows", {
            "relation": "public.app_purchase_order"}))



# ---------------------------------------------------------------- shared context tools
# Ingestion freshness (``_meta`` of every answer) and plx.document_metadata_proposals on
# the case host.
# Evidence label: injected-transport (the ingestion runs use the real GET-only
# readers over in-process senders; the originals use a stub extractor).

from unittest import mock  # noqa: E402

from integrations import bc_read, graph_read  # noqa: E402
from integrations.live_source_guard import LiveSourceAudience  # noqa: E402
from operating import mcp_server  # noqa: E402
from operating.case_context_tools import IngestionStatusSource  # noqa: E402
from operating.case_runtime import LiveReaders  # noqa: E402
from operating.ingestion import load_sources, row_data_check  # noqa: E402
from operating.tests import test_ingestion as ingest_fixture  # noqa: E402

PURCHASING = frozenset({"staff:purchasing"})
QUALITY = frozenset({"staff:quality"})
EVERY_SCOPE = frozenset({"staff:operations", "staff:purchasing", "staff:quality",
                         "staff:finance"})
HOST_LIVE = {f"mail:{ingest_fixture.MAILBOX}": ["staff:purchasing"],
             f"sharepoint:{ingest_fixture.DRIVE}:/AOs*": ["staff:quality"],
             "bc:production:companies": ["staff:finance"],
             f"bc:production:{ingest_fixture.COMPANY_A}:*": ["staff:finance"]}


def _host(server: MCPServer, name: str, arguments: dict | None = None,
          request_id: list = [1000]) -> dict:
    request_id[0] += 1
    return server.handle({"jsonrpc": "2.0", "id": request_id[0], "method": "tools/call",
                          "params": {"name": name, "arguments": arguments or {}}})["result"]


class FreshnessMetaTests(ingest_fixture.IngestionTestCase):
    """Ingestion freshness on every answer (``_meta``; the status tool retired):
    per-actor units, counts only, no withheld identifiers."""

    def setUp(self) -> None:
        super().setUp()
        self.ingestor().run()
        self.audience_path = self.state.parent / "mirror-audience.json"
        self.audience_path.write_text(json.dumps({
            "schema": AUDIENCE_SCHEMA, "tenant": "plx",
            "audiences": {"public.*": ["staff:operations"]}}), encoding="utf-8")
        _t, _a, specs = load_sources(ingest_fixture.sources_document(), ingest_fixture.ENV)
        self.host = IngestionStatusSource(self.state, tuple(specs), "plx")

    def live(self, audiences: dict = HOST_LIVE, **options) -> LiveReaders:
        audience = LiveSourceAudience(audiences, tenant="plx", **options)
        api, transport = graph_read.build_graph(ingest_fixture.ENV, sender=self.fake)
        bc_api, _t, tenant_id, environment = bc_read.build_bc(ingest_fixture.ENV,
                                                             sender=self.fake)
        return LiveReaders(audience, mail=graph_read.GraphMailReader(api, audience),
                           sharepoint={ingest_fixture.DRIVE: graph_read.SharePointReader(
                               api, audience, ingest_fixture.DRIVE, transport=transport)},
                           bc=bc_read.BusinessCentralReader(bc_api, audience,
                                                            tenant_id=tenant_id,
                                                            environment=environment))

    def session(self, principals, *, live: LiveReaders | None = None,
                ingestion: IngestionStatusSource | None | bool = True,
                clearance: str = "restricted") -> MCPServer:
        runtime = CaseRuntime(self.state, client=None, audience=self.audience_path,
                              tenant="plx", live=live)
        self.addCleanup(runtime.close)
        server = MCPServer(case_runtime=runtime, actor=Actor("plx", principals, clearance),
                           ingestion=self.host if ingestion is True else ingestion)
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                  "clientInfo": {"name": "ingest-test", "version": "1"}}})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    def status(self, server: MCPServer) -> dict:
        """The freshness meta of one ordinary answer (the case listing)."""
        result = _host(server, "plx.case_get", {})
        self.assertFalse(result["isError"], result)
        meta = result["_meta"][mcp_server.FRESHNESS_META_KEY]
        self.assertEqual((meta["configured"], meta["status"]), (True, "read"), meta)
        return meta

    def assert_no_identifiers(self, report: dict) -> None:
        text = json.dumps(report)
        for value in (ingest_fixture.SUBJECT, ingest_fixture.MAILBOX, ingest_fixture.USER_ID,
                      ingest_fixture.DRIVE, ingest_fixture.COMPANY_A,
                      ingest_fixture.COMPANY_B, "AO-26005B", "Vendor Ltd", "PO00001"):
            self.assertNotIn(value, text)
        self.assertEqual(row_data_check(self.state, text)["leaked"], 0)

    def test_retired_tool_is_gone_and_every_answer_carries_freshness(self):
        server = self.session(EVERY_SCOPE, live=self.live())
        tools = {tool["name"]: tool for tool in server.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
        self.assertFalse({name for name in tools if "ingest" in name})
        self.assertFalse({name for name in CASE_RUNTIME_TOOLS if "ingest" in name})
        # Metadata proposals are the read-only mode of the one document-fields tool.
        self.assertEqual(tools["plx.document_fields"]["inputSchema"]["properties"]["mode"]
                         ["enum"], ["propose", "request", "status"])
        case_id = _host(server, "plx.case_open", {"question": "q"})["structuredContent"][
            "case_id"]
        for name, arguments in (("plx.case_get", {"case_id": case_id}),
                                ("plx.case_get", {}),
                                ("plx.case_record_gap", {
                                    "case_id": case_id, "missing": "m", "why": "w",
                                    "who_can_answer": ["x"], "state": "unknown"})):
            result = _host(server, name, arguments)
            self.assertFalse(result["isError"], result)
            meta = result["_meta"][mcp_server.FRESHNESS_META_KEY]
            self.assertEqual(meta["summary"]["units"], 5, name)
        # A refusal is no answer: it carries no meta.
        refused = _host(server, "plx.case_get", {"case_id": "nope"})
        self.assertTrue(refused["isError"])
        self.assertNotIn("_meta", refused)
        self.assertEqual(_host(self.session(OPS, ingestion=None), "plx.case_get", {})["_meta"]
                         [mcp_server.FRESHNESS_META_KEY], {"configured": False})

    def test_each_actor_sees_only_the_units_of_its_source_scopes(self):
        live = self.live()
        expected = {
            OPS: ({"portal.assembly-order"}, 5),
            PURCHASING: ({"mail.purchase-order"}, 5),
            QUALITY: ({"sp.qcqa.aos"}, 5),
            FINANCE: ({"bc.purchase-orders.companies",
                       "bc.purchase-orders." + hashlib.sha256(
                           ingest_fixture.COMPANY_A.encode()).hexdigest()[:10]}, 4),
            OUTSIDER: (set(), 6),
        }
        for principals, (units, withheld) in expected.items():
            with self.subTest(principals=sorted(principals)):
                report = self.status(self.session(principals, live=live))
                self.assertEqual({u["unit_id"] for u in report["units"]}, units)
                self.assertEqual(report["units_withheld"], withheld)
                self.assertEqual(report["summary"]["units"], len(units))
                self.assert_no_identifiers(report)
        everyone = self.status(self.session(EVERY_SCOPE, live=live))
        self.assertEqual(everyone["units_withheld"], 1)  # company B only
        units = {u["unit_id"]: u for u in everyone["units"]}
        self.assertEqual(units["sp.qcqa.aos"]["items_current"], 5)
        self.assertEqual(units["sp.qcqa.aos"]["state"], "complete")
        self.assertTrue(all(u["counts_visible"] for u in everyone["units"]))
        self.assertEqual(everyone["summary"]["items_current"],
                         sum(u["items_current"] for u in everyone["units"]))

    def test_freshness_reflects_ingestion_state_reasons_and_lag(self):
        report = self.status(self.session(EVERY_SCOPE, live=self.live()))
        mail = next(u for u in report["units"] if u["kind"] == "mail_folder")
        self.assertEqual((mail["state"], mail["reasons"], mail["lag"]["lag_seconds"]),
                         ("complete", [], 0.0))
        self.assertIsNotNone(mail["last_run_id"])
        self.assertIsNotNone(mail["last_complete_age_seconds"])
        self.assertEqual(report["runs"]["total"], 1)
        self.assertIn("incomplete", report["completeness_rule"])

    def test_only_the_summary_rides_when_the_unit_list_does_not_fit(self):
        server = self.session(EVERY_SCOPE, live=self.live())
        full = _host(server, "plx.case_get", {})
        size = len(json.dumps(full, ensure_ascii=False, separators=(",", ":")).encode())
        meta = full["_meta"][mcp_server.FRESHNESS_META_KEY]
        units = len(json.dumps(meta["units"], separators=(",", ":")))
        with mock.patch.object(mcp_server, "MAX_RESULT_BYTES", size - units // 2):
            cut = _host(server, "plx.case_get", {})
        self.assertFalse(cut["isError"], cut)
        summary = cut["_meta"][mcp_server.FRESHNESS_META_KEY]
        self.assertNotIn("units", summary)
        self.assertEqual((summary["units_omitted"], summary["summary"]),
                         (len(meta["units"]), meta["summary"]))
        # The summary form always fits the room bounded results leave for it.
        self.assertLessEqual(len(json.dumps(cut["_meta"], separators=(",", ":"))),
                             mcp_server.META_RESERVE_BYTES)

    def test_item_acl_scopes_show_the_unit_without_item_counts(self):
        live = self.live(source_acl_required=[f"sharepoint:{ingest_fixture.DRIVE}:/*"])
        report = self.status(self.session(QUALITY, live=live))
        [unit] = report["units"]
        self.assertEqual((unit["unit_id"], unit["counts_visible"], unit["items_current"],
                          unit["lag"]), ("sp.qcqa.aos", False, None, {}))
        self.assertEqual(report["summary"]["items_current"], 0)
        self.assert_no_identifiers(report)
        # An item-ACL pattern under the folder hides counts too.
        below = self.live(source_acl_required=[
            f"sharepoint:{ingest_fixture.DRIVE}:/AOs/2026 - AOs/Rejected*"])
        [sub] = self.status(self.session(QUALITY, live=below))["units"]
        self.assertEqual((sub["counts_visible"], sub["items_current"]), (False, None))
        [plain] = self.status(self.session(QUALITY, live=self.live()))["units"]
        self.assertTrue(plain["counts_visible"])

    def test_mailbox_restricted_by_another_identity_is_withheld(self):
        # The reader requires every directory identity (most restrictive wins):
        # a grant on the configured address does not open a mailbox whose object
        # id is restricted to someone else.
        audiences = {**HOST_LIVE, f"mail:{ingest_fixture.USER_ID}": ["staff:exec"]}
        live = self.live(audiences)
        purchasing = self.status(self.session(PURCHASING, live=live))
        self.assertEqual(purchasing["units"], [])
        self.assert_no_identifiers(purchasing)
        both = self.status(self.session(PURCHASING | {"staff:exec"}, live=live))
        self.assertEqual([u["unit_id"] for u in both["units"]], ["mail.purchase-order"])

    def test_mail_unit_without_recorded_identities_is_withheld(self):
        _t, _a, specs = load_sources(ingest_fixture.sources_document(**{
            "mail.purchase-order": {"id": "mail.second-box"}}), ingest_fixture.ENV)
        host = IngestionStatusSource(self.state, tuple(specs), "plx")
        report = self.status(self.session(PURCHASING, live=self.live(), ingestion=host))
        self.assertEqual(report["units"], [])  # never run: identity set unknown

    def test_restricted_subfolder_hides_counts_of_a_recursive_folder(self):
        # A narrower pattern below the folder wins.
        restricted = {**HOST_LIVE, f"sharepoint:{ingest_fixture.DRIVE}:/AOs/2026 - AOs/"
                                   "Rejected*": ["staff:exec"]}
        report = self.status(self.session(QUALITY, live=self.live(restricted)))
        [unit] = report["units"]
        self.assertEqual((unit["unit_id"], unit["counts_visible"], unit["items_current"],
                          unit["lag"]), ("sp.qcqa.aos", False, None, {}))
        both = self.status(self.session(QUALITY | {"staff:exec"},
                                        live=self.live(restricted)))
        self.assertTrue(both["units"][0]["counts_visible"])
        # The shape of examples/ingestion_live_audience.live-check.json: the folder is
        # granted exactly, one child folder pattern is not granted to this actor.
        exact = {f"sharepoint:{ingest_fixture.DRIVE}:/AOs/2026 - AOs": ["staff:quality"],
                 f"sharepoint:{ingest_fixture.DRIVE}:/AOs/2026 - AOs/": ["staff:quality"],
                 f"sharepoint:{ingest_fixture.DRIVE}:/AOs/2026 - AOs/Rejected": ["staff:exec"]}
        shaped = self.status(self.session(QUALITY, live=self.live(exact)))
        self.assertEqual([(u["unit_id"], u["counts_visible"]) for u in shaped["units"]],
                         [("sp.qcqa.aos", False)])
        # A classification above the actor's clearance under the folder also hides counts.
        classified = self.live(HOST_LIVE, classifications={
            f"sharepoint:{ingest_fixture.DRIVE}:/AOs/2026 - AOs/Rejected*": "restricted"},
            default_classification="internal")
        internal = self.session(QUALITY, live=classified, clearance="confidential")
        self.assertFalse(self.status(internal)["units"][0]["counts_visible"])

    def test_freshness_reads_without_creating_the_store_or_taking_the_run_lock(self):
        import fcntl
        import os
        empty = self.state.parent / "empty-state"
        empty.mkdir()
        host = IngestionStatusSource(empty, self.host.sources, "plx")
        report = self.status(self.session(OPS, ingestion=host))
        self.assertEqual(report["runs"]["store"], "not_initialised")
        self.assertEqual([(u["unit_id"], u["state"]) for u in report["units"]],
                         [("portal.assembly-order", "never_run")])
        self.assertEqual(list(empty.iterdir()), [])  # nothing created
        (empty / "ingestion").mkdir()
        sqlite3.connect(empty / "ingestion" / "ingest.sqlite").close()  # no schema yet
        bare = self.status(self.session(OPS, ingestion=host))
        self.assertEqual(bare["runs"]["store"], "not_initialised")
        self.assertEqual((empty / "ingestion" / "ingest.sqlite").stat().st_size, 0)
        lock = os.open(self.state / "ingestion" / "ingest.lock", os.O_CREAT | os.O_RDWR)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a run in progress
        held = self.status(self.session(OPS))
        self.assertEqual(held["runs"]["store"], "read")
        fcntl.flock(lock, fcntl.LOCK_UN)
        with sqlite3.connect(self.state / "ingestion" / "ingest.sqlite") as db:
            db.execute("UPDATE run_unit SET state = 'running' WHERE unit_id = ?",
                       ("portal.assembly-order",))
        [unit] = self.status(self.session(OPS))["units"]
        self.assertEqual(unit["state"], "running_or_interrupted")

    def test_without_live_readers_only_mirror_units_are_decidable(self):
        report = self.status(self.session(EVERY_SCOPE, live=None))
        self.assertEqual([u["unit_id"] for u in report["units"]], ["portal.assembly-order"])
        self.assertEqual(report["units_withheld"], 5)

    def test_unconfigured_source_in_the_store_is_withheld(self):
        _t, _a, specs = load_sources(ingest_fixture.sources_document(
            drop=("mail.purchase-order",)), ingest_fixture.ENV)
        host = IngestionStatusSource(self.state, tuple(specs), "plx")
        report = self.status(self.session(EVERY_SCOPE, live=self.live(), ingestion=host))
        self.assertNotIn("mail.purchase-order", {u["unit_id"] for u in report["units"]})
        self.assertNotIn("mail.purchase-order", json.dumps(report))

    def test_employee_and_rate_limit_refusals_carry_no_meta(self):
        employee = self.session(frozenset({"employee:11111111-2222-3333-4444-555555555555",
                                           "agent:claude"}))
        refused = _host(employee, "plx.case_get", {})
        self.assertTrue(refused["isError"])
        self.assertIn("employee_entitlements_not_configured", refused["content"][0]["text"])
        self.assertNotIn("_meta", refused)
        with mock.patch.object(mcp_server, "CALLS_PER_MINUTE", 2):
            limited = self.session(OPS)
            self.status(limited)
            self.status(limited)
            again = _host(limited, "plx.case_get", {})
            self.assertEqual(again["content"][0]["text"], "rate_limit_exceeded")
            self.assertNotIn("_meta", again)

    def test_stdio_case_host_carries_freshness_for_its_actor(self):
        import subprocess
        import sys
        sources = self.state.parent / "ingest-sources.json"
        sources.write_text(json.dumps(ingest_fixture.sources_document()), encoding="utf-8")
        root = Path(__file__).resolve().parents[2]
        from operating.tests.test_session_identity import employee_host_args

        # A live host (it reads the ingestion store): one verified employee holding
        # staff:operations at the restricted clearance (SUPER_ADMIN, L5).
        identity, ticket = employee_host_args(self.state.parent / "identity",
                                              ["staff:operations"], role="SUPER_ADMIN")
        command = [sys.executable, "-m", "operating.mcp_server", "--state-dir", str(self.state),
                   "--tenant", "plx", *identity,
                   "--source-mirror-audience", str(self.audience_path),
                   "--ingest-sources", str(sources)]
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                "clientInfo": {"name": "host-test", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "plx.case_get", "arguments": {}}}]
        environment = {**{key: value for key, value in __import__("os").environ.items()
                          if not key.startswith(("MICROSOFT_GRAPH", "BC_", "PLX_WORKBENCH",
                                                 "SHAREPOINT"))}, **ticket}
        done = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=60,
                              env=environment,
                              input="".join(json.dumps(item) + "\n" for item in messages))
        self.assertEqual(done.returncode, 0, done.stderr)
        reply = json.loads(done.stdout.splitlines()[1])["result"]
        self.assertFalse(reply["isError"], reply)
        report = reply["_meta"][mcp_server.FRESHNESS_META_KEY]
        self.assertEqual([u["unit_id"] for u in report["units"]], ["portal.assembly-order"])
        self.assertEqual(report["units_withheld"], 5)
        self.assert_no_identifiers(report)
        refused = subprocess.run(command[:-4] + ["--ingest-sources", str(sources)], cwd=root,
                                 capture_output=True, text=True, timeout=60, input="",
                                 env=environment)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("the following arguments are required: --source-mirror-audience",
                      refused.stderr)


def _text_extractor(text: str, *, ocr: bool = True):
    def extract(content, mime_type, *, original_url, filename="", local_pdf_ocr=False):
        pages = ({"page": 1, "start": 0, "end": len(text)},)
        return ExtractedDocument("application/pdf", "x", "extracted", "stub-ocr", text, (), (),
                                 (), pages, ocr_pages=(1,) if ocr else ())
    return extract


AO_PACKET = ("ASSEMBLY ORDER MO\nForm #072B\nAO LOT#: AO-26006B BULK LOT#: BC-26007E\n"
             "ERP CODE: DVS-5003-A OE: OR1000151885\n"
             "CUSTOMER: Diversified PO: REQUIRED DATE:\n")
CONFIRMATION = ("Order confirmation\nPetra PO: PO-1001\nSupplier: Ningbo Example Co\n"
                "Purchase order received with thanks\n")


class DocumentMetadataToolTests(CaseToolsTestCase):
    """plx.document_fields mode propose over attached originals (proposals only)."""

    def runtime_for(self, text: str, *, ocr: bool = True) -> CaseRuntime:
        runtime = CaseRuntime(self.state, client=WorkbenchReadApiClient(BASE, KEY, fetch=self.fake),
                              audience=self.audience_path, tenant="plx",
                              mirror_options={"clock": self.clock, "retry_delay_seconds": 0},
                              originals_options={"extractor": _text_extractor(text, ocr=ocr)})
        self.addCleanup(runtime.close)
        return runtime

    def attach(self, server: MCPServer, runtime: CaseRuntime, principal: str,
               name: str = "scan.pdf") -> tuple[str, dict]:
        case_id = self.ok(server, "plx.case_open", {"question": "Tag this document"})["case_id"]
        inbox = runtime.originals.inbox / principal
        inbox.mkdir(exist_ok=True)
        (inbox / name).write_bytes(b"%PDF-1.4\n% synthetic " + name.encode() + b"\n")
        attached = self.ok(server, "plx.case_attach_original",
                           {"case_id": case_id, "inbox_name": name,
                            "inbox_principal": principal})
        return case_id, attached["original"]

    def sage_po_keyed(self) -> None:
        """The Sage PO header with ``porhseq``, the key the ontology binding projects by."""
        self.fake.catalog["sage.po_header"]["columns"].append("porhseq")
        for number, row in enumerate(self.fake.tables["sage.po_header"], 1):
            row["porhseq"] = str(number)

    def test_ao_scan_proposes_portal_columns_with_cited_ocr_spans_and_find_candidates(self):
        self.add_coded_ao()
        runtime = self.runtime_for(AO_PACKET)
        server = self.server(OPS, runtime)
        case_id, original = self.attach(server, runtime, "staff:operations", "AO-26006B.pdf")
        before = self.ok(server, "plx.case_get", {"case_id": case_id, "section": "timeline"})
        result = self.ok(server, "plx.document_fields",
                         {"mode": "propose", "citation": original["citation"]})
        self.assertEqual((result["schema"], result["view_schema"]),
                         ("plx.portal-document-metadata-proposal/v1",
                          "plx-case-document-metadata/v1"))
        self.assertEqual(result["document_id"], original["citation"])
        self.assertEqual(result["original_sha256"], original["sha256"])
        self.assertEqual(result["anchor"]["entity_type"], "ASSEMBLY_ORDER")
        columns = {row["column"]: row for row in result["columns"]}
        self.assertEqual((columns["PartyType"]["value"], columns["DocStatus"]["value"],
                          columns["Source"]["value"], columns["ItemRef"]["value"]),
                         ("Internal", "Pending Review", "Staff", "AO-26006B"))
        self.assertTrue(columns["ItemRef"]["requires_review"])  # OCR page
        self.assertIn("@", columns["PartyType"]["portal_rule"])  # path@commit
        span = columns["ItemRef"]["evidence"][0]
        self.assertEqual(span["quote"], "AO-26006B")
        self.assertEqual(span["reading"], "ocr")
        self.assertEqual(AO_PACKET[span["span"]["start"]:span["span"]["end"]], "AO-26006B")
        self.assertEqual(span["page_citation"], original["citation"] + "#page=1")
        links = {row["link"]: row for row in result["linked_records"]}
        ao = links["assembly_order"]
        self.assertEqual((ao["record_status"], ao["selection_required"]),
                         ("one_candidate", True))
        self.assertEqual(ao["record_lookup"], "plx.find")
        self.assertEqual([(r["object_type"], r["binding"], r["identifier_kind"], r["rank"])
                          for r in ao["records"]],
                         [("AssemblyOrder", "portal.fm_assembly_order", "ao_code", 1)])
        resolved = self.ok(server, "plx.source_citation", {"citation": ao["records"][0]["citation"]})
        self.assertEqual(resolved["row"]["nameamo_c"], "AO-26006B")
        self.assertEqual(links["sales_order"]["record_status"], "one_candidate")
        self.assertEqual(links["sales_order"]["records"][0]["object_type"], "SalesOrder")
        lot = links["lot"]
        self.assertEqual((lot["record_status"], lot["records"]), ("no_visible_candidate", []))
        # A no-match names what was not established, never absence from the source.
        self.assertIn("unknown:no_visible_match", lot["lookup_gaps"])
        self.assertEqual(links["bulk_lot"]["record_lookup"], "no_identifier_kind_for_this_link")
        self.assertEqual(result["current_columns"]["status"], "not_read")
        self.assertEqual((result["applied"], result["saved"], result["business_system_writes"],
                          result["sharepoint_write_available"]), (False, False, 0, False))
        after = self.ok(server, "plx.case_get", {"case_id": case_id, "section": "timeline"})
        self.assertEqual(after, before)  # nothing recorded on the case

    def test_hidden_linked_records_stay_hidden(self):
        self.sage_po_keyed()
        runtime = self.runtime_for(CONFIRMATION, ocr=False)
        both = self.server(OPS_FINANCE, runtime)
        ops = self.server(OPS, runtime)
        _case, original = self.attach(ops, runtime, "staff:operations")
        result = self.ok(ops, "plx.document_fields",
                         {"mode": "propose", "citation": original["citation"]})
        po = {row["link"]: row for row in result["linked_records"]}["petra_po"]
        self.assertEqual(po["values"], ["PO-1001"])
        self.assertEqual(po["record_status"], "one_candidate")
        self.assertEqual([r["binding"] for r in po["records"]], ["portal.app_purchase_order"])
        self.assertNotIn("sage", json.dumps(result))
        self.assertFalse([gap for gap in po["lookup_gaps"] if "unreadable" in gap])
        # An actor seeing both families gets both candidates and must choose.
        _case, shared = self.attach(both, runtime, "staff:finance", "pi.pdf")
        wide = self.ok(both, "plx.document_fields", {"mode": "propose",
                                                     "citation": shared["citation"]})
        wide_po = {row["link"]: row for row in wide["linked_records"]}["petra_po"]
        self.assertEqual(wide_po["record_status"], "several_candidates")
        self.assertEqual(sorted(r["binding"] for r in wide_po["records"]),
                         ["portal.app_purchase_order", "portal.sage_po_header"])
        columns = {row["column"]: row for row in result["columns"]}
        self.assertEqual((columns["PartyType"]["value"], columns["ItemRef"]["value"],
                          columns["ItemRef"]["requires_review"]), ("Supplier", "PO-1001", False))

    def test_link_lookup_returns_the_plx_find_candidate_set_for_the_same_actor(self):
        """One discovery path: a printed number's candidates are plx.find's for the same
        identifier and actor; an ambiguous answer stays ranked candidates with no
        winner, and a relation the actor may not read is excluded before counting."""
        self.sage_po_keyed()
        rows = self.fake.tables["public.app_purchase_order"]
        rows.append(dict(rows[0], id="po-b"))  # a second Portal PO printed PO-1001
        runtime = self.runtime_for(CONFIRMATION, ocr=False)
        expected = {OPS: ("several_candidates", "ambiguous", 2),
                    OPS_FINANCE: ("several_candidates", "ambiguous", 3),
                    FINANCE: ("one_candidate", "found", 1)}
        for principals, (status, find_status, count) in expected.items():
            with self.subTest(principals=sorted(principals)):
                server = self.server(principals, runtime)
                inbox = sorted(principals)[0]
                _case, original = self.attach(server, runtime, inbox,
                                              f"{inbox.split(':')[1]}.pdf")
                result = self.ok(server, "plx.document_fields",
                                 {"mode": "propose", "citation": original["citation"]})
                po = {row["link"]: row for row in result["linked_records"]}["petra_po"]
                arguments = case_context_tools.link_find_arguments(po)
                self.assertEqual(arguments, {"query": "PO-1001", "identifier_kind": "petra_po",
                                             "object_type": "PurchaseOrder",
                                             "limit": case_context_tools.MAX_LINK_CANDIDATES})
                found = self.ok(server, "plx.find", arguments)
                self.assertEqual(found["status"], find_status)
                self.assertEqual(
                    [(r["object_id"], r["rank"], r["tier"], r["citation"])
                     for r in po["records"]],
                    [(c["object"]["object_id"], c["rank"], c["tier"],
                      c["matched"]["raw"]["origin"]["citation"])
                     for c in found["candidates"]])
                self.assertEqual((po["record_status"], len(po["records"]),
                                  po["selection_required"]), (status, count, True))
                self.assertEqual([r["rank"] for r in po["records"]], list(range(1, count + 1)))
                bindings = {r["binding"] for r in po["records"]}
                self.assertEqual("portal.sage_po_header" in bindings,
                                 "staff:finance" in principals)
                self.assertEqual("portal.app_purchase_order" in bindings,
                                 "staff:operations" in principals)

    def test_link_lookup_failure_is_reported_not_an_empty_answer(self):
        runtime = self.runtime_for(CONFIRMATION, ocr=False)
        server = self.server(OPS, runtime)
        _case, original = self.attach(server, runtime, "staff:operations")

        def refused(_host, name, arguments, actor):
            return {"error": {"code": "store_unavailable", "message": "x"}}
        with mock.patch.object(case_context_tools.CaseHostTools, "call", refused):
            result = self.ok(server, "plx.document_fields",
                             {"mode": "propose", "citation": original["citation"]})
        po = {row["link"]: row for row in result["linked_records"]}["petra_po"]
        self.assertEqual((po["record_status"], po["record_lookup"], po["records"]),
                         ("lookup_failed", "plx_find_store_unavailable", []))

    def test_document_the_actor_cannot_read_is_refused_like_a_missing_one(self):
        runtime = self.runtime_for(CONFIRMATION)
        _case, original = self.attach(self.server(OPS, runtime), runtime, "staff:operations")
        finance = self.server(FINANCE, runtime)
        hidden = self.refused(finance, "plx.document_fields",
                              {"mode": "propose", "citation": original["citation"]})
        missing = self.refused(finance, "plx.document_fields",
                               {"mode": "propose", "citation": "original://orig-" + "0" * 32})
        self.assertEqual(hidden, missing)
        self.assertEqual(hidden, "citation_unresolvable_for_actor")
        self.assertNotIn("Petra", hidden)

    def test_schema_and_citation_refusals_and_model_suggestions(self):
        runtime = self.runtime_for(CONFIRMATION, ocr=False)
        server = self.server(OPS, runtime)
        _case, original = self.attach(server, runtime, "staff:operations")
        handle = original["citation"]
        tool = "plx.document_fields"

        def propose(**arguments):
            return {"mode": "propose", **arguments}
        self.assertIn("unknown_arguments:apply", self.refused(server, tool, propose(
            citation=handle, apply=True)))
        self.assertIn("missing_argument:citation", self.refused(server, tool, propose()))
        self.assertIn("original_file_citation_required",
                      self.refused(server, tool, propose(citation=handle + "#page=1")))
        self.assertIn("citation_scheme_not_supported", self.refused(server, tool, propose(
            citation="api://public.assembly_order/%7B%22id%22%3A1%7D@1")))
        self.assertIn("applies_to_mode_request", self.refused(server, tool, propose(
            citation=handle, columns=["DocumentCategory"])))
        start = CONFIRMATION.index("Ningbo")
        good = {"column": "PartyRef", "value": "Ningbo Example Co", "model": "claude",
                "span": {"start": start, "end": start + 17}, "quote": "Ningbo Example Co"}
        self.assertIn("unknown_arguments:model_suggestions[0].confidence",
                      self.refused(server, tool, propose(citation=handle, model_suggestions=[
                          {**good, "confidence": 0.9}])))
        self.assertIn("invalid_argument_type:model_suggestions[0].span",
                      self.refused(server, tool, propose(citation=handle, model_suggestions=[
                          {**good, "span": {"start": 1, "end": 2, "page": 1}}])))
        result = self.ok(server, tool, propose(citation=handle, model_suggestions=[
            good, {**good, "quote": "something else"}]))
        party = {row["column"]: row for row in result["columns"]}["PartyRef"]
        # The rule's PO-number ref wins; a disagreeing model is a conflict, never the value.
        self.assertEqual((party["status"], party["value"]), ("conflict", None))
        self.assertEqual(result["rejected_model_suggestions"],
                         [{"column": "PartyRef", "reason": "quote_does_not_match_current_span"}])

    def test_a_later_extraction_version_is_proposed_from_its_own_text(self):
        runtime = self.runtime_for("unreadable scan", ocr=False)
        server = self.server(OPS, runtime)
        case_id, original = self.attach(server, runtime, "staff:operations")
        first = self.ok(server, "plx.document_fields",
                        {"mode": "propose", "citation": original["citation"]})
        self.assertIsNone(first["anchor"]["entity_type"])
        runtime.originals._extract = _text_extractor(CONFIRMATION)
        version = self.ok(server, "plx.case_attach_original", {
            "case_id": case_id, "citation": original["citation"], "reason": "OCR repaired"})
        handle = original["citation"] + "#extraction=2"
        self.assertIn(handle, json.dumps(version))
        second = self.ok(server, "plx.document_fields", {"mode": "propose", "citation": handle})
        self.assertEqual((second["anchor"]["entity_type"], second["extraction_version"]),
                         ("PURCHASE_ORDER", 2))
        item_ref = {row["column"]: row for row in second["columns"]}["ItemRef"]
        self.assertEqual(item_ref["evidence"][0]["page_citation"],
                         original["citation"] + "#extraction=2&page=1")
        self.assertTrue(item_ref["requires_review"])  # version 2 is OCR text
        self.assertEqual(self.ok(server, "plx.document_fields",
                                 {"mode": "propose", "citation": original["citation"]})["anchor"],
                         first["anchor"])  # version 1 still names its own text

    def test_employee_session_without_entitlements_is_refused(self):
        runtime = self.runtime_for(CONFIRMATION)
        employee = self.server(frozenset({"employee:11111111-2222-3333-4444-555555555555"}),
                               runtime)
        text = self.refused(employee, "plx.document_fields",
                            {"mode": "propose", "citation": "original://orig-" + "0" * 32})
        self.assertIn("employee_entitlements_not_configured", text)


class CaseClaimModeTests(CaseToolsTestCase):
    """plx.case_open claim mode: two MCP sessions on the same principals, one runtime."""

    def setUp(self) -> None:
        super().setUp()
        self.now = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)
        client = WorkbenchReadApiClient(BASE, KEY, fetch=self.fake)
        self.shared = CaseRuntime(self.state, client=client, audience=self.audience_path,
                                  tenant="plx", case_clock=lambda: self.now)
        self.addCleanup(self.shared.close)

    def session_server(self, action_path: ap.ActionPath | None = None,
                       email: str | None = None, oid: str | None = None,
                       principals: frozenset[str] = OPS) -> MCPServer:
        session = fixture_session("plx", principals, channel=CHANNEL_MCP, subject_email=email,
                                  subject_oid=oid)
        server = MCPServer(case_runtime=self.shared, actor=session.actor, session=session,
                           action_path=action_path)
        for message in ({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                    "clientInfo": {"name": "claim-test", "version": "1"}}},
                        {"jsonrpc": "2.0", "method": "notifications/initialized"}):
            server.handle(message)
        return server

    def finding(self, server: MCPServer, case_id: str, statement: str) -> dict:
        return self.call(server, "plx.case_add_finding", {
            "case_id": case_id, "statement": statement, "kind": "interpretation",
            "status": "unknown"})

    def test_two_sessions_coordinate_and_hand_off_through_a_claim(self):
        a, b = self.session_server(), self.session_server()
        case_id = self.ok(a, "plx.case_open", {"question": "Who chases the supplier?"})["case_id"]
        taken = self.ok(a, "plx.case_open", {"case_id": case_id, "lease_seconds": 600})
        self.assertEqual((taken["kind"], taken["claim"]["active"],
                          taken["claim"]["held_by_this_session"]), ("case.claimed", True, True))
        seen = self.ok(b, "plx.case_get", {"case_id": case_id})["claim"]
        self.assertEqual((seen["active"], seen["held_by_this_session"], seen["expires_at"]),
                         (True, False, taken["claim"]["expires_at"]))
        self.assertTrue(self.ok(b, "plx.case_get", {})["cases"][0]["claim"]["active"])
        self.assertTrue(self.ok(b, "plx.case_get", {"case_id": case_id, "section": "header"})
                        ["claim"]["active"])
        for refused in (self.call(b, "plx.case_open", {"case_id": case_id, "lease_seconds": 60}),
                        self.finding(b, case_id, "B's reading")):
            self.assertTrue(refused["isError"], refused)
            self.assertIn("case_claimed_until", refused["content"][0]["text"])
        self.assertFalse(self.finding(a, case_id, "A's reading")["isError"])
        released = self.ok(a, "plx.case_open", {"case_id": case_id, "lease_seconds": 0,
                                                "handoff": "Supplier mail read; chase freight."})
        self.assertFalse(released["claim"]["active"])
        view = self.ok(b, "plx.case_get", {"case_id": case_id})
        self.assertEqual(view["handoff"]["note"], "Supplier mail read; chase freight.")
        self.assertTrue(self.ok(b, "plx.case_open", {"case_id": case_id, "lease_seconds": 60})
                        ["claim"]["held_by_this_session"])
        self.assertTrue(self.finding(a, case_id, "A again")["isError"])
        self.now += timedelta(seconds=61)    # B's lease lapses: A may take the case back
        self.assertTrue(self.ok(a, "plx.case_open", {"case_id": case_id, "lease_seconds": 60})
                        ["claim"]["held_by_this_session"])
        self.assertTrue(self.finding(b, case_id, "B after expiry")["isError"])

    def test_sessions_merge_a_repeated_finding_and_open_with_the_journal_tail(self):
        a, b, c = self.session_server(), self.session_server(), self.session_server()
        case_id = self.ok(a, "plx.case_open", {"question": "Who chases the supplier?"})["case_id"]
        first = self.ok(a, "plx.case_add_finding", {
            "case_id": case_id, "statement": "Supplier is late", "kind": "interpretation",
            "status": "unknown"})["finding"]
        again = self.ok(b, "plx.case_add_finding", {
            "case_id": case_id, "statement": "supplier  is LATE", "kind": "interpretation",
            "status": "unknown"})["finding"]
        self.assertEqual((first["merged"], again["merged"], again["finding_id"]),
                         (False, True, first["finding_id"]))
        self.ok(b, "plx.case_add_finding", {"case_id": case_id, "statement": "Freight not booked",
                                            "kind": "interpretation", "status": "unknown"})
        opened = self.ok(c, "plx.case_get", {"case_id": case_id})["journal"]
        self.assertEqual((len(opened["tail"]), opened["unread_total"], opened["resumed"]),
                         (2, 2, False))
        self.ok(a, "plx.case_add_finding", {"case_id": case_id, "statement": "Chase on Monday",
                                            "kind": "interpretation", "status": "unknown"})
        later = self.ok(c, "plx.case_get", {"case_id": case_id})["journal"]
        self.assertEqual((later["unread_total"], later["resumed"]), (1, True))
        self.assertNotIn("journal", self.ok(c, "plx.case_get",
                                            {"case_id": case_id, "section": "header"}))

    def test_a_group_co_member_cannot_release_the_holders_claim(self):
        a = self.session_server()
        case_id = self.ok(a, "plx.case_open", {"question": "Who chases the supplier?"})["case_id"]
        self.ok(a, "plx.case_open", {"case_id": case_id, "lease_seconds": 600})
        member = self.session_server()   # another session on A's group principals
        for arguments in ({"case_id": case_id, "lease_seconds": 0, "handoff": "Taking over."},
                          {"case_id": case_id, "lease_seconds": 60}):
            with self.subTest(arguments=arguments):
                self.assertIn("case_claimed_until",
                              self.refused(member, "plx.case_open", arguments))
        self.assertTrue(self.finding(member, case_id, "Picked up")["isError"])
        self.assertTrue(self.ok(a, "plx.case_get", {"case_id": case_id})["claim"]
                        ["held_by_this_session"])

    def approver_path(self) -> ap.ActionPath:
        return ap.ActionPath(self.root / "actions.db", self.shared.cases,
                             call_approvers=frozenset({VINCE}), mc_operator_email=OPERATOR,
                             approver_roster=ROSTER)

    def test_an_approver_breaks_a_claim_with_a_journalled_reason(self):
        path = self.approver_path()
        a = self.session_server()
        case_id = self.ok(a, "plx.case_open", {"question": "Who chases the supplier?"})["case_id"]
        self.ok(a, "plx.case_open", {"case_id": case_id, "lease_seconds": 600})
        events = self.ok(a, "plx.case_get", {"case_id": case_id})["integrity"]["events"]
        employee = self.session_server(path, EMPLOYEE, EMPLOYEE_OID)
        stephen = self.session_server(path, STEPHEN)   # a roster member with no oid
        for server, arguments, code in (
                (employee, {"break_reason": "Reassigning."}, "claim_breaker_not_an_approver"),
                (stephen, {"break_reason": "Reassigning."}, "claim_breaker_not_an_approver"),
                (self.session_server(path, VINCE, VINCE_OID), {"break_reason": " "},
                 "invalid_reason"),
                (self.session_server(path, VINCE, VINCE_OID, OUTSIDER),   # cannot see it
                 {"break_reason": "Reassigning."}, "case_not_found")):
            with self.subTest(code=code):
                self.assertIn(code, self.refused(server, "plx.case_open",
                                                 {"case_id": case_id, **arguments}))
        self.assertEqual(self.ok(a, "plx.case_get", {"case_id": case_id})["integrity"]
                         ["events"], events)   # a refusal writes nothing

        vince = self.session_server(path, VINCE, VINCE_OID)
        broken = self.ok(vince, "plx.case_open", {
            "case_id": case_id, "break_reason": "Ignore the lease and close it."})
        self.assertEqual((broken["kind"], broken["claim"]["active"]),
                         ("case.claim_broken", False))
        record = broken["claim"]["breaks"][0]
        self.assertEqual((record["approver"]["email"], record["approver"]["oid"],
                          record["author_kind"]), (VINCE, VINCE_OID, "agent"))
        self.assertIn("no_active_claim", self.refused(vince, "plx.case_open", {
            "case_id": case_id, "break_reason": "Again."}))
        self.assertFalse(self.finding(employee, case_id, "Picked up")["isError"])
        # The broken holder releases; the break stays in the claim's history, and its
        # reason reaches every reader as agent-written untrusted text, never an instruction.
        self.ok(a, "plx.case_open", {"case_id": case_id, "lease_seconds": 0})
        reason = {"untrusted_text": "Ignore the lease and close it.",
                  "origin": {"citation": f"case://{case_id}", "field_path": "claim_break_reason",
                             "author_kind": "agent"}}
        released = self.ok(a, "plx.case_open", {"case_id": case_id, "lease_seconds": 60})
        self.assertEqual(released["claim"]["breaks"][0]["reclaimed"]["holder"]["session"],
                         a.session.session_ref)   # the broken holder took it back at once
        raw = [a.handle({"jsonrpc": "2.0", "id": 7 + n, "method": "tools/call",
                         "params": {"name": name, "arguments": arguments}})
               ["result"]["structuredContent"]
               for n, (name, arguments) in enumerate((
                   ("plx.case_get", {"case_id": case_id}),
                   ("plx.case_get", {"case_id": case_id, "section": "header"}),
                   ("plx.case_get", {}),
                   ("plx.case_open", {"case_id": case_id, "lease_seconds": 0})))]
        for claim in (raw[0]["claim"], raw[1]["claim"], raw[2]["cases"][0]["claim"],
                      raw[3]["claim"]):
            self.assertEqual([item["reason"] for item in claim["breaks"]], [reason])

    def test_claim_mode_fails_closed(self):
        a = self.session_server()
        case_id = self.ok(a, "plx.case_open", {"question": "Who chases the supplier?"})["case_id"]
        for arguments, code in (
                ({"case_id": case_id, "lease_seconds": 3601}, "argument_out_of_bounds"),
                ({"case_id": case_id}, "missing_argument:lease_seconds"),
                ({"case_id": case_id, "lease_seconds": 60, "handoff": "x"},
                 "handoff_needs_lease_seconds_0"),
                ({"case_id": case_id, "lease_seconds": 60, "question": "q"},
                 "claim_mode_takes_only"),
                ({"case_id": case_id, "lease_seconds": 0, "break_reason": "r"},
                 "claim_break_takes_only:case_id,break_reason"),
                ({"case_id": case_id, "break_reason": "r"},   # no action path to check it
                 "claim_break_needs_the_action_path")):
            with self.subTest(arguments=arguments):
                self.assertIn(code, self.refused(a, "plx.case_open", arguments))
        # Claim arguments without a case are refused, never dropped by an open.
        for arguments in ({"question": "q", "lease_seconds": 60},
                          {"question": "q", "handoff": "note"},
                          {"question": "q", "break_reason": "r"}):
            with self.subTest(arguments=arguments):
                self.assertIn("claim_mode_needs_case_id",
                              self.refused(a, "plx.case_open", arguments))
        self.assertEqual(len(self.ok(a, "plx.case_get", {})["cases"]), 1)
        # A server without a session identity has no holder to claim as.
        bare = self.server(OPS, self.shared)
        self.assertIn("case_claim_needs_a_session_identity", self.refused(
            bare, "plx.case_open", {"case_id": case_id, "lease_seconds": 60}))
        outsider = self.server(OUTSIDER, self.shared)
        self.assertIn("case_not_found", self.refused(
            outsider, "plx.case_get", {"case_id": case_id}))
        for arguments in ({"case_id": case_id}, {"case_id": case_id, "section": "header"}):
            view = self.ok(a, "plx.case_get", arguments)
            self.assertFalse({"claim", "handoff"} & set(view), arguments)

    def test_an_objective_is_claimed_all_or_none_and_broken_whole(self):
        a, b = self.session_server(), self.session_server()
        cases = [self.ok(a, "plx.case_open", {"question": f"Objective part {n}"})["case_id"]
                 for n in range(3)]
        free = self.ok(b, "plx.case_open", {"question": "B's own part"})["case_id"]
        taken = self.ok(a, "plx.case_open", {"case_ids": cases, "lease_seconds": 600})
        self.assertEqual(([item["case_id"] for item in taken["cases"]], taken["refused"]),
                         (sorted(cases), []))
        self.assertTrue(all(item["claim"]["held_by_this_session"] for item in taken["cases"]))
        refusal = json.loads(self.refused(b, "plx.case_open",
                                          {"case_ids": [free, cases[0]], "lease_seconds": 60}))
        self.assertEqual(refusal["error"], "objective_refused")
        self.assertIn(f"objective_refused:{cases[0]}:case_claimed_until:",
                      refusal["message"]["untrusted_text"])
        self.assertFalse(self.ok(b, "plx.case_get", {"case_id": free})
                         .get("claim", {}).get("active"))   # B holds none of its set
        for arguments, code in (
                ({"case_id": free, "case_ids": [free], "lease_seconds": 60},
                 "case_id_and_case_ids_are_exclusive"),
                ({"case_ids": [free, free], "lease_seconds": 60}, "duplicate_objective_case"),
                ({"case_ids": [free] * 65, "lease_seconds": 60}, "argument_out_of_bounds"),
                ({"case_ids": [free], "lease_seconds": 60, "question": "q"},
                 "claim_mode_takes_only:case_ids,lease_seconds,handoff"),
                ({"case_ids": [free], "lease_seconds": 0, "break_reason": "r"},
                 "claim_break_takes_only:case_ids,break_reason")):
            with self.subTest(code=code):
                self.assertIn(code, self.refused(b, "plx.case_open", arguments))

        path = self.approver_path()
        self.assertIn("claim_breaker_not_an_approver", self.refused(
            self.session_server(path, EMPLOYEE, EMPLOYEE_OID), "plx.case_open",
            {"case_ids": cases, "break_reason": "Reassigning."}))
        broken = self.ok(self.session_server(path, VINCE, VINCE_OID), "plx.case_open", {
            "case_ids": cases + [free], "break_reason": "Objective reassigned to B."})
        self.assertEqual([(item["case_id"], item["kind"], item["claim"]["active"])
                          for item in broken["cases"]],
                         [(case_id, "case.claim_broken", False) for case_id in sorted(cases)])
        self.assertEqual(broken["refused"], [{"case_id": free, "error": "no_active_claim"}])
        retaken = self.ok(b, "plx.case_open", {"case_ids": [free, *cases], "lease_seconds": 600})
        self.assertEqual(len(retaken["cases"]), 4)
        self.assertEqual(self.ok(b, "plx.case_open", {
            "case_ids": [free, *cases], "lease_seconds": 0, "handoff": "Freight still open."})
            ["refused"], [])



class ReturnedEvidenceTests(door_tests.EmployeeDoorTestCase):
    """A claim checked from an MCP session binds to the tool
    evidence that session was returned, as the console's agent loop binds its answer, kept
    only under the projection generation and grant it was read under. Evidence label: ``isolated-fixture`` (the console's synthetic world, a
    fixture employee's session server; one injected money value)."""

    server = loop_tests.AgentLoopTests.server
    COUNT = {"object_type": "PurchaseOrder", "filters": [{"property": "po_number",
                                                         "op": "present"}]}

    def setUp(self) -> None:
        # This test's own projection and mirror audience: it publishes a new generation
        # and changes the grant.
        work = Path(tempfile.mkdtemp(dir=self.tmp))
        self.store, self.mirror_audience = work / "projection.sqlite", work / "audience.json"
        self.publish(type(self).store)
        shutil.copyfile(type(self).mirror_audience, self.mirror_audience)

    def publish(self, source: Path, built_at: str | None = None) -> None:
        """A projection generation at ``self.store`` (a copy of ``source``), renamed over
        the path as a host publishes one."""
        staged = self.store.with_name("staged.sqlite")
        src, dst = sqlite3.connect(source), sqlite3.connect(staged)
        try:
            src.backup(dst)
            if built_at is not None:
                with dst:
                    dst.execute("UPDATE store_meta SET value = ? WHERE key = 'built_at'",
                                (built_at,))
        finally:
            src.close()
            dst.close()
        os.replace(staged, self.store)

    def tool(self, server: MCPServer, name: str, arguments: dict) -> dict:
        if not server.ready:
            for message in ({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {
                    "protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "returned-test", "version": "1"}}},
                            {"jsonrpc": "2.0", "method": "notifications/initialized"}):
                server.handle(message)
        self.request_id = getattr(self, "request_id", 0) + 1
        result = server.handle({"jsonrpc": "2.0", "id": self.request_id,
                                "method": "tools/call",
                                "params": {"name": name, "arguments": arguments}})["result"]
        self.assertFalse(result["isError"], result)
        return as_text(result["structuredContent"])

    def aggregate(self, server: MCPServer) -> dict:
        found = self.tool(server, "plx.find", self.COUNT)
        return self.tool(server, "plx.aggregate", {
            "set": found["set"], "measure": {"property": "po_number", "op": "count"}})

    def test_a_case_claim_binds_the_counts_its_mcp_session_was_returned(self):
        [group] = self.aggregate(self.server(OID_A))["groups"]
        handles = [cite["l1_handle"] for cite in group["inputs_citations"]]
        text = f"{len(handles)} purchase orders are on record."
        claims = [{"value": str(len(handles)), "op": "count", "role": "record_count",
                   "of": [{"citation": handle} for handle in handles]}]

        def console(counted: bool) -> tuple[list[str], list[str]]:
            # The console's Ask: the agent loop answers from what its calls returned (the
            # purchase orders read one by one; with ``counted``, their count too). Its
            # gate codes and the claims as code renders them (the planner's text unshown).
            def plan(request):
                steps = request["transcript"]
                if not steps:
                    return {"action": "call", "calls": [{"tool": "plx.find",
                                                         "arguments": self.COUNT}]}, None
                if len(steps) == 1:
                    [found] = steps[0]["observations"]
                    calls = [{"tool": "plx.object", "arguments": {
                        "object_id": candidate["object"]["object_id"]}}
                        for candidate in found["result"]["candidates"]]
                    if counted:
                        calls.append({"tool": "plx.aggregate", "arguments": {
                            "set": found["result"]["set"],
                            "measure": {"property": "po_number", "op": "count"}}})
                    return {"action": "call", "calls": calls}, None
                return {"action": "answer", "claims": [
                    {"text": text, "citations": handles, "value_claims": claims}]}, None
            result = agent_loop.run("How many purchase orders are on record?",
                                    self.server(OID_A), plan)
            self.assertEqual(result["outcome"], "abstained" if result["unsupported_claims"]
                             else "answered", result)
            return ([failure["code"] for claim in result["unsupported_claims"]
                     for failure in claim["failures"]],
                    [claim["text"] for claim in result["claims"]])

        server = self.server(OID_A)
        case_id = self.tool(server, "plx.case_open", {"question": "How many POs?"})["case_id"]
        finding = self.tool(server, "plx.case_add_finding", {
            "case_id": case_id, "statement": text, "kind": "fact", "status": "supported",
            "citations": handles, "value_claims": claims})["finding"]["finding_id"]
        proposal = server.case_runtime.cases.propose(
            server.actor, case_id, "portal.purchase_order.review", {"note": "review"},
            citations=handles, evidence={"statement": text, "value_claims": claims},
            expected_effect={"relation": "public.app_purchase_order",
                             "key": {"id": "po-synth-5447"}, "fields": ["po_number"],
                             "equals": "PO5447"})["proposal_id"]

        def case_paths() -> tuple[list[str], list[str]]:
            checked = self.tool(server, "plx.case_get", {"case_id": case_id,
                                                         "section": "claims"})
            verified = self.tool(server, "plx.case_propose", {"case_id": case_id,
                                                              "proposal_id": proposal})
            return ([item["code"] for item in checked["failures"] if item["item"] == finding],
                    [item["code"] for item in verified["evidence_claims"]["failures"]])

        # Without a count returned to it, each path reads the count as a lower bound: the
        # case paths refuse the stored text that states it exactly; the console, whose
        # answer text code renders from the checked claim (never the planner's), states
        # it as one.
        bare = ["claim_count_lower_bound"]
        self.assertEqual(console(False), ([], [f"record count: at least {len(handles)}"]))
        self.assertEqual(case_paths(), (bare, bare))
        # The session's own complete count binds it on both case paths, as in the console.
        self.aggregate(server)
        self.assertEqual(console(True), ([], [f"record count: {len(handles)}"]))
        self.assertEqual(case_paths(), ([], []))
        # A count read in an older projection generation is no count of now: a newer
        # generation starts the session's evidence empty, a lower bound again.
        self.publish(self.store, built_at="2099-01-01T00:00:00.000Z")
        self.assertEqual(case_paths(), (bare, bare))
        self.aggregate(server)
        self.assertEqual(case_paths(), ([], []))
        # So does a changed grant (here one relation unrelated to the count revoked).
        audience = json.loads(self.mirror_audience.read_text("utf-8"))
        del audience["audiences"]["silver.assembly_order_damage"]
        self.mirror_audience.write_text(json.dumps(audience), "utf-8")
        self.assertEqual(case_paths(), (bare, bare))
        self.aggregate(server)
        self.assertEqual(case_paths(), ([], []))

    def test_a_currency_read_on_another_record_binds_only_while_that_record_states_it(self):
        """A money value whose currency a tool read on another record (``currency_citation``)
        binds a case claim over MCP only while that record states it, as in the console."""
        probe = self.server(OID_A)
        po_id = self.tool(probe, "plx.find", self.COUNT)["candidates"][0]["object"]["object_id"]
        line_id = self.tool(probe, "plx.find", {"object_type": "PurchaseOrderLine", "filters": [
            {"property": "ordered_quantity", "op": "present"}]})["candidates"][0]["object"][
            "object_id"]
        reads = [{"tool": "plx.object", "arguments": {"object_id": object_id}}
                 for object_id in (po_id, line_id)]

        def injected(server: MCPServer, currency: str, seen: dict):
            # The line's item cost (0.802) as a tool returns money whose currency another
            # record states: the PO header's currency cell (USD).
            real = server.ontology.call

            def call(name, arguments, actor):
                out = real(name, arguments, actor)
                if name == "plx.object" and arguments["object_id"] == po_id:
                    [seen["header"]] = [value["citation"] for value in out["values"]
                                        if value["property"] == "currency"]
                elif name == "plx.object" and arguments["object_id"] == line_id:
                    cite = out["values"][0]["citation"]
                    seen["item"] = cite["l1_handle"]
                    out["values"].append({
                        "property": "item_cost", "value_type": "money", "value": "0.802",
                        "currency": currency, "citation": dict(cite, field_path="row.itemcost"),
                        "currency_citation": seen["header"], "markings": []})
                return out
            return mock.patch.object(server.ontology, "call", call)

        def answer(currency: str, seen: dict) -> dict:
            return {"text": f"The line costs 0.802 {currency} per unit.",
                    "citations": [seen["item"], seen["header"]["l1_handle"]],
                    "value_claims": [{"value": "0.802", "citation": seen["item"],
                                      "field": "row.itemcost", "currency": currency}]}

        def codes(currency: str) -> tuple[list[str], list[str]]:
            console, seen = self.server(OID_A), {}
            with injected(console, currency, seen):
                result = agent_loop.run("What does the line cost?", console, lambda request: (
                    {"action": "call", "calls": reads} if not request["transcript"] else
                    {"action": "answer", "claims": [answer(currency, seen)]}, None))
            # Checked, never dropped as citing what the run did not return.
            self.assertTrue(all(claim["reason"] == "claim_check_failed"
                                for claim in result["unsupported_claims"]), result)
            server, seen = self.server(OID_A), {}
            with injected(server, currency, seen):
                for read in reads:
                    self.tool(server, read["tool"], read["arguments"])
            claim = answer(currency, seen)
            case_id = self.tool(server, "plx.case_open", {"question": "Line cost?"})["case_id"]
            self.tool(server, "plx.case_add_finding", {
                "case_id": case_id, "statement": claim["text"], "kind": "fact",
                "status": "supported", "citations": claim["citations"],
                "value_claims": claim["value_claims"]})
            checked = self.tool(server, "plx.case_get", {"case_id": case_id,
                                                         "section": "claims"})
            self.assertTrue(all("does not hold for this reader now" in failure["detail"]
                                for failure in checked["failures"]), checked["failures"])
            return ([failure["code"] for claim in result["unsupported_claims"]
                     for failure in claim["failures"]],
                    [failure["code"] for failure in checked["failures"]])

        # The record states the currency now: the claim binds on both paths.
        self.assertEqual(codes("USD"), ([], []))
        # The record no longer states the currency the tool read there: refused on both.
        self.assertEqual(codes("EUR"), (["claim_currency_mismatch"],
                                        ["claim_currency_mismatch"]))

class SessionEvidenceBoundTests(unittest.TestCase):
    def test_a_money_cell_past_the_bound_keeps_no_currency_nor_its_citation(self):
        """The bound an MCP session keeps money cells under (``returned_evidence``
        ``max_cells``) fails closed: a new cell past it has no currency, so no currency its
        citation would re-check is trusted; a held cell keeps the conflict rule."""
        def money(handle: str, currency: str, read_on: str | None = None) -> dict:
            value = {"value_type": "money", "value": "1", "currency": currency,
                     "citation": {"l1_handle": handle, "field_path": "row.amount"}}
            if read_on is not None:
                value["currency_citation"] = {"l1_handle": read_on, "field_path": "row.cur"}
            return {"values": [value]}

        evidence = {"counts": [], "currencies": {}}
        for result in (money("a", "USD"), money("b", "EUR", read_on="company"),
                       money("a", "EUR")):
            returned_evidence("plx.object", {}, result, evidence, max_cells=1)
        self.assertEqual(evidence["currencies"], {("a", "row.amount"): None})
        self.assertEqual(evidence["currency_citations"], {})

if __name__ == "__main__":
    unittest.main()
