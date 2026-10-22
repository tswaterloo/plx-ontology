"""Ordinary MCP follow-up/gap semantic parity (injected transport and local stores).

No network or owning-system dispatch: the existing FakeWorkbench/action fixtures
provide the runtime. Citationless, propertyless source absence must be refused
through both entry points; an unknown follow-up remains a valid internal write.
"""

import dataclasses
import json
import unittest

from ontology.core import Actor
from operating import customer_po_coverage as cpo
from operating.action_catalog import (CATALOG_PATH, ActionCatalog, ActionRefused,
                                      CatalogError, _precondition)
from operating.action_path import ActionPath
from operating.mcp_server import MCPServer
from operating.ontology_host import CaseHostTools
from operating.session_identity import CHANNEL_MCP, fixture_session
from operating.ontology_tools import shared_schema
from operating.tests.test_mcp_action_tools import ActionToolsTestCase
from operating.tests.test_mcp_case_tools import OPS_FINANCE
from operating.tests.test_playbook_eval import CustomerPoEvalBase


class FollowupAbsenceGuardTests(ActionToolsTestCase):
    @staticmethod
    def parameters(state):
        return {"missing": "the customer's PO document",
                "why": "ask customer service to establish whether it is on file",
                "who_can_answer": ["customer service"], "state": state}

    def test_citationless_absence_is_refused_by_both_ordinary_mcp_routes(self):
        server = self.action_server(OPS_FINANCE)
        case_id = self.employee_case("Is the customer PO document on file?")
        parameters = self.parameters("absent_in_source")
        actor = Actor("plx", OPS_FINANCE)
        before = self.rt.cases.get(actor, case_id)["gaps"]

        refusal = self.refused(server, "plx.case_record_gap",
                               {"case_id": case_id, **parameters})
        self.assertIn("gap_guard_failed", refusal)
        self.assertEqual(self.rt.cases.get(actor, case_id)["gaps"], before)

        result = self.call(server, "plx.act", {
            "mode": "propose", "case_id": case_id,
            "action": "workbench.follow_up.record", "parameters": parameters,
        })
        self.assertEqual((self.mc.sender.calls, self.fx.calls), ([], 0))
        self.assertTrue(
            result["isError"],
            "plx.act must refuse propertyless, citationless absent_in_source just "
            "as plx.case_record_gap does; a stored/verified receipt is not absence evidence",
        )
        self.assertEqual(self.rt.cases.get(actor, case_id)["gaps"], before)

    def test_unknown_followup_is_still_recorded_without_source_absence_evidence(self):
        server = self.action_server(OPS_FINANCE)
        case_id = self.employee_case("Is the customer PO document on file?")
        result = self.ok(server, "plx.act", {
            "mode": "propose", "case_id": case_id,
            "action": "workbench.follow_up.record", "parameters": self.parameters("unknown"),
        })
        execution = result["execution"]
        self.assertEqual((result["kind"], execution["recorded"],
                          execution["read_back"]["outcome"], result["mc_calls"],
                          result["business_system_writes"]),
                         ("workbench_internal", True, "verified", 0, 0))
        self.assertEqual((self.mc.sender.calls, self.fx.calls), ([], 0))


class AbsenceParityTests(CustomerPoEvalBase, unittest.TestCase):
    """One absence prover on both write paths: plx.case_record_gap and plx.act's
    workbench.follow_up.record give the same verdict on the same cited Portal rows
    (injected-transport FakeWorkbench mirror)."""

    ACTION = "workbench.follow_up.record"

    def setUp(self) -> None:
        super().setUp()
        runtime = self.server.case_runtime
        actor = self.server.actor   # the host's session records who acted on every step
        self.server = MCPServer(case_runtime=runtime, actor=actor,
                                action_path=ActionPath(self.h.root / "actions.db", runtime.cases),
                                session=fixture_session(actor.tenant, actor.principals,
                                                        channel=CHANNEL_MCP))
        self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                       "clientInfo": {"name": "parity", "version": "1"}}})
        self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        rows = self.actual("mirror.rows", {"relation": cpo.RELATION, "limit": 50})["rows"]
        self.by_number = {row["row"]["order_number"]: row for row in rows}
        self.case_id = self.call("plx.case_open", {
            "question": "Missing customer POs", "roots": ["OR0001"]})["case_id"]

    def number(self, test):
        return next(n for n, r in sorted(self.by_number.items()) if test(r["row"]))

    def empty_order(self):
        return self.number(lambda row: "po_reference" in row and row["po_reference"] is None
                           and row.get("custom_fields") is None)

    def parameters(self, number, state="absent_in_source", citations=None):
        return {"missing": f"Customer PO of sales order {number}",
                "why": "The cited order shows no customer PO.",
                "who_can_answer": ["PLX order entry"], "state": state,
                "property": "SalesOrder.customer_po",
                "citations": [self.by_number[number]["citation"]]
                if citations is None else citations}

    def gaps(self):
        return self.call("plx.case_get", {"case_id": self.case_id})["gaps"]

    def act(self, parameters):
        return {"mode": "propose", "case_id": self.case_id, "action": self.ACTION,
                "parameters": parameters}

    def both_refuse(self, parameters, failure):
        route = self.h.refused(self.server, "plx.case_record_gap",
                               {"case_id": self.case_id, **parameters})
        act = self.h.refused(self.server, "plx.act", self.act(parameters))
        self.assertIn(f"gap_guard_failed:{failure}", route)
        self.assertIn(f"action_refused:gap_guard_failed:{failure}", act)
        self.assertEqual(self.gaps(), [])
        return route, act

    def test_citationless_absence_is_refused_on_both_paths(self):
        self.both_refuse(self.parameters(self.empty_order(), citations=[]),
                         "absence_needs_citation")
        parameters = self.parameters(self.empty_order())
        del parameters["property"]
        self.both_refuse(parameters, "absence_needs_property")

    def test_a_contradicting_populated_field_is_refused_on_both_paths(self):
        self.assertEqual(self.by_number["OR0001"]["row"]["po_reference"], "PO-5501")
        route, act = self.both_refuse(self.parameters("OR0001"),
                                      "gap_contradicted_by_cited_evidence")
        for text in (route, act):
            self.assertIn("po_reference", text)
            self.assertNotIn("PO-5501", text)  # the value is never echoed

    def test_an_unreadable_field_is_not_absence_on_either_path(self):
        number = self.number(lambda row: row.get("custom_fields") == "not json")
        self.both_refuse(self.parameters(number), "gap_property_unreadable")
        # Recorded as what it is: unknown, through either path.
        self.call("plx.case_record_gap",
                  {"case_id": self.case_id, **self.parameters(number, state="unknown")})
        result = self.call("plx.act", self.act(self.parameters(number, state="unknown")))
        self.assertEqual(result["execution"]["read_back"]["outcome"], "verified")
        self.assertEqual([gap["state"] for gap in self.gaps()], ["unknown", "unknown"])

    def test_a_supported_absence_is_recorded_on_both_paths(self):
        number = self.empty_order()
        parameters = self.parameters(number)
        route = self.call("plx.case_record_gap", {"case_id": self.case_id, **parameters})
        result = self.call("plx.act", self.act(parameters))
        execution = result["execution"]
        [shown] = [item for item in execution["preconditions"] if item["id"] == "absence_shown"]
        self.assertEqual(shown["checked"], route["gap_guard"]["checked"])
        self.assertEqual(shown["checked"][0]["fields"],
                         {"po_reference": "absent", "custom_fields.custPoCode": "absent"})
        self.assertEqual((execution["recorded"], execution["read_back"]["outcome"],
                          result["business_system_writes"]), (True, "verified", 0))
        self.assertEqual([(gap["state"], gap["property"]) for gap in self.gaps()],
                         [("absent_in_source", ["SalesOrder.customer_po"])] * 2)


class HostSchemaParityTests(CustomerPoEvalBase, unittest.TestCase):
    """plx.act's gap guard reads the host's own schema, as plx.case_record_gap on that
    host does, never the shared one: here the host binds SalesOrder.customer_po to
    po_reference only, so both paths check exactly that field."""

    ACTION = AbsenceParityTests.ACTION
    number, empty_order, parameters = (AbsenceParityTests.number,
                                       AbsenceParityTests.empty_order,
                                       AbsenceParityTests.parameters)
    gaps, act, both_refuse = (AbsenceParityTests.gaps, AbsenceParityTests.act,
                              AbsenceParityTests.both_refuse)

    def setUp(self) -> None:
        super().setUp()
        shared = shared_schema()
        order = shared.object_types["SalesOrder"]
        properties = [dict(item, sources=[source for source in item["sources"]
                                          if source["path"] == "po_reference"])
                      if item["name"] == "customer_po" else item
                      for item in order["properties"]]
        self.host_schema = dataclasses.replace(shared, object_types={
            **shared.object_types, "SalesOrder": {**order, "properties": properties}})
        self.assertNotEqual(self.host_schema, shared)
        runtime = self.server.case_runtime
        actor = self.server.actor   # the host's session records who acted on every step
        ontology = CaseHostTools(runtime, actor, schema=self.host_schema)
        self.server = MCPServer(case_runtime=runtime, actor=actor,
                                action_path=ActionPath(self.h.root / "actions.db",
                                                       runtime.cases),
                                ontology=ontology,
                                session=fixture_session(actor.tenant, actor.principals,
                                                        channel=CHANNEL_MCP))
        self.addCleanup(self.server.close)
        self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                       "clientInfo": {"name": "host-schema", "version": "1"}}})
        self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        rows = self.actual("mirror.rows", {"relation": cpo.RELATION, "limit": 50})["rows"]
        self.by_number = {row["row"]["order_number"]: row for row in rows}
        self.case_id = self.call("plx.case_open", {
            "question": "Missing customer POs", "roots": ["OR0001"]})["case_id"]

    def test_a_supported_absence_is_recorded_on_both_paths(self):
        number = self.empty_order()
        parameters = self.parameters(number)
        route = self.call("plx.case_record_gap", {"case_id": self.case_id, **parameters})
        execution = self.call("plx.act", self.act(parameters))["execution"]
        [shown] = [item for item in execution["preconditions"] if item["id"] == "absence_shown"]
        # The host binds one carrier: the shared schema's custom_fields.custPoCode is not
        # checked on either path.
        self.assertEqual(route["gap_guard"]["checked"][0]["fields"], {"po_reference": "absent"})
        self.assertEqual(shown["checked"], route["gap_guard"]["checked"])
        self.assertEqual([gap["state"] for gap in self.gaps()], ["absent_in_source"] * 2)

    def test_a_property_the_host_does_not_declare_is_refused_on_both_paths(self):
        order = self.host_schema.object_types["SalesOrder"]
        self.host_schema.object_types["SalesOrder"] = {**order, "properties": [
            item for item in order["properties"] if item["name"] != "customer_po"]}
        self.assertIn("customer_po", [item["name"] for item in shared_schema()
                                      .object_types["SalesOrder"]["properties"]])
        self.both_refuse(self.parameters(self.empty_order(), state="unknown"),
                         "gap_property_undeclared")


class GapGuardCatalogTests(unittest.TestCase):
    def document(self):
        document = json.loads(CATALOG_PATH.read_text())
        return document, document["actions"]["workbench.follow_up.record"]

    def test_a_catalog_gap_write_without_the_gap_guard_does_not_load(self):
        document, entry = self.document()
        entry["preconditions"] = [item for item in entry["preconditions"]
                                  if item["check"] != "gap_guard"]
        del entry["parameters"]["property"]
        del entry["executor"]["arguments"]["about_property"]
        with self.assertRaisesRegex(CatalogError, "catalog_gap_write_without_gap_guard"):
            ActionCatalog(document)

    def test_without_a_reader_an_absence_is_refused(self):
        _, entry = self.document()
        [guard] = [item for item in entry["preconditions"] if item["check"] == "gap_guard"]
        values = {"state": "absent_in_source", "property": "SalesOrder.customer_po",
                  "citations": ["api://workbench/public.app_sales_order/1"]}
        with self.assertRaises(ActionRefused) as caught:
            _precondition(guard, values, {})
        self.assertTrue(caught.exception.code.startswith(
            "gap_guard_failed:gap_property_undeclared"), caught.exception.code)
        with self.assertRaises(ActionRefused) as caught:
            _precondition(guard, values, {}, None, shared_schema())
        self.assertTrue(caught.exception.code.startswith(
            "gap_guard_failed:gap_evidence_unreadable"), caught.exception.code)
        # Not absence: no reader or schema is needed to record it.
        self.assertTrue(_precondition(guard, {**values, "state": "unknown", "property": None},
                                      {})["holds"])


if __name__ == "__main__":
    unittest.main()
