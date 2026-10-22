"""Workbench-checked links: contracts, the check, the case record, MCP.

Evidence label: ``injected-transport``. The AO chain rows are the 2026-09-30
live-trace values served by the in-process ``FakeWorkbench`` (the same fixture
as ``test_ao_26005b_case``); the Portal sales order, purchase order, FM PO and
receipt rows are synthetic. Nothing here is live data.
"""

from __future__ import annotations

import copy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from integrations.source_mirror import AUDIENCE_SCHEMA
from integrations.tests.test_source_mirror import BASE, KEY, CATALOG, Clock, FakeWorkbench
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology.core import Actor, ValidationError
from ontology.relation_contracts import FM_SOURCE_FK
from operating.tests.firewall_view import client_result
from operating.mcp_server import source_read
from operating import source_reads
from operating.case_links import BUILTIN_PATH, LinkContracts, SCHEMA
from operating.case_runtime import LIMITATIONS, CaseRuntime
from operating.cases import CaseStore
from operating.mcp_server import HOST_READS, MCPServer
from operating.tests.test_ao_26005b_case import AO_CATALOG, AO_TABLES

OPS_QUALITY = frozenset({"staff:operations", "staff:quality"})
OPS = frozenset({"staff:operations"})
GRANTED = {"public.*": ["staff:operations"], "silver.*": ["staff:quality"]}


def _relation(columns, domain="purchasing"):
    return {"columns": columns, "key": ["id"], "domain": domain, "since": None}


EXTRA_CATALOG = {
    "public.app_sales_order": _relation(["id", "order_number", "po_reference",
                                         "custom_fields"], "orders"),
    "public.app_purchase_order": _relation(["id", "po_number", "custom_fields"]),
    "public.app_receipt": _relation(["id", "receipt_number", "purchase_order_id",
                                     "po_reference"]),
    "public.generated_purchase_order": _relation(["id", "ponumber"]),
    "public.generated_purchase_order_item": _relation(["id", "idpogen", "idproductsupp"]),
    "public.receipt_packaging": _relation(["id", "idpogenitem", "ponoqc"]),
}

EXTRA_TABLES = {
    "public.app_sales_order": [
        # FM-ensured order: fmId as a JSON number inside a JSON-text column.
        {"id": "so-synth-1", "order_number": "SO100151",
         "po_reference": "PO12172025",
         "custom_fields": json.dumps({"fmId": 351, "source": "fm-oe-ensure"})},
        # fmId as a digit string inside an object column, pointing elsewhere.
        {"id": "so-synth-2", "order_number": "SO100152", "po_reference": None,
         "custom_fields": {"fmId": "352", "source": "fm-oe-ensure"}},
        # Portal-entered, never bridged: no fmId at all.
        {"id": "so-synth-3", "order_number": "SO-2026-0001", "po_reference": None,
         "custom_fields": {"source": "portal"}}],
    "public.app_purchase_order": [
        {"id": "po-synth-5447", "po_number": "PO5447",
         "custom_fields": {"fmGeneratedPoId": "9001.0", "fmPoNumber": "5447"}},
        {"id": "po-synth-5448", "po_number": "PO5448", "custom_fields": "{not json"}],
    "public.app_receipt": [
        {"id": "rcpt-synth-1", "receipt_number": "RP0000001",
         "purchase_order_id": "po-synth-5447", "po_reference": "PO5447"},
        {"id": "rcpt-synth-2", "receipt_number": "RP0000002", "purchase_order_id": None,
         "po_reference": " PO5447 "},
        {"id": "rcpt-synth-3", "receipt_number": "RP0000003", "purchase_order_id": None,
         "po_reference": "po5447"},
        {"id": "rcpt-synth-4", "receipt_number": "RP0000004",
         "purchase_order_id": "po-synth-5448", "po_reference": "PO5447"}],
    "public.generated_purchase_order": [{"id": 9001, "ponumber": "5447"}],
    "public.generated_purchase_order_item": [
        {"id": 77001, "idpogen": 9001, "idproductsupp": 5}],
    "public.receipt_packaging": [{"id": 66001, "idpogenitem": 77001, "ponoqc": "PO5447"}],
}

# A second order and line so a link row can point at the wrong order.
WRONG_ORDER_ROWS = {
    "public.order_entry_item": {"id": 1141, "idoe": 352, "idfp": 1534, "idbulk": 1091,
                                "qty": "100.0", "status_c": "Open"},
    "public.order_entry": {"id": 352, "idcust": 90, "custpocode": "PO-OTHER",
                           "erpcode": "OR1000151999", "reference": None, "datepo": None,
                           "datereq": None, "status": "Open"},
}


def link_mode(case_id, contract, source, target) -> dict:
    """plx.case_add_finding arguments in link mode (the retired plx.case_link)."""
    return {"case_id": case_id, "link": {"contract_id": contract, "from_citation": source,
                                         "to_citation": target}}


class LinkHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audience_path = self.root / "audience.json"
        self.audience_path.write_text(json.dumps({
            "schema": AUDIENCE_SCHEMA, "tenant": "plx", "audiences": GRANTED}), "utf-8")
        self.fake = FakeWorkbench()
        self.fake.catalog = copy.deepcopy(CATALOG)
        self.fake.catalog.update(copy.deepcopy(AO_CATALOG))
        self.fake.catalog.update(copy.deepcopy(EXTRA_CATALOG))
        for tables in (AO_TABLES, EXTRA_TABLES):
            for name, rows in tables.items():
                self.fake.tables[name] = [copy.deepcopy(row) for row in rows]
        for name, row in WRONG_ORDER_ROWS.items():
            self.fake.tables[name].append(dict(row))
        self.request_id = 100

    def runtime(self) -> CaseRuntime:
        client = WorkbenchReadApiClient(BASE, KEY, fetch=self.fake)
        runtime = CaseRuntime(self.root / "state", client=client, audience=self.audience_path,
                              tenant="plx", mirror_options={"clock": Clock(),
                                                            "retry_delay_seconds": 0})
        self.addCleanup(runtime.close)
        return runtime

    def server(self, principals, runtime) -> MCPServer:
        server = MCPServer(case_runtime=runtime, actor=Actor("plx", frozenset(principals)))
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                  "clientInfo": {"name": "link-test", "version": "1"}}})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    def call(self, server, name, arguments) -> dict:
        self.request_id += 1
        host = name in HOST_READS  # the drivers' raw citation read is host code
        response = server.handle({"jsonrpc": "2.0", "id": self.request_id,
                                  "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments}},
                                 host_code=host)
        self.assertNotIn("error", response, response)
        if host:
            return response["result"]
        return client_result(name, response["result"])  # through the output boundary

    def ok(self, server, name, arguments) -> dict:
        if name in source_reads.READS:  # a driver's direct read, not a host tool
            return source_read(server, name, arguments or {})
        result = self.call(server, name, arguments)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def refused(self, server, name, arguments) -> str:
        result = self.call(server, name, arguments)
        self.assertTrue(result["isError"], result)
        return result["content"][0]["text"]

    def cite(self, server, relation, row_id) -> str:
        rows = source_reads.read(server.case_runtime, server.actor, "mirror.rows", {
            "relation": relation, "where": [["id", "eq", row_id]]})["rows"]
        self.assertEqual(len(rows), 1, (relation, row_id))
        return rows[0]["citation"]

    def link(self, server, case_id, contract, source, target) -> dict:
        return self.ok(server, "plx.case_add_finding",
                       link_mode(case_id, contract, source, target))["link"]

    def open(self, server) -> str:
        return self.ok(server, "plx.case_open", {"question": "Which order was AO-26005B for?",
                                                 "roots": ["AO-26005B"]})["case_id"]


class ContractFileTests(unittest.TestCase):
    def document(self) -> dict:
        return json.loads(BUILTIN_PATH.read_text(encoding="utf-8"))

    def test_builtin_contracts_cover_the_named_links_with_evidence(self):
        contracts = LinkContracts.builtin()
        for contract_id, match in (
                ("fm-ao-link-to-assembly-order", "fk"), ("fm-ao-link-to-order-line", "fk"),
                ("fm-order-line-to-order", "fk"),
                ("portal-so-to-fm-order-by-fmid", "json_path"),
                ("portal-po-to-fm-generated-po", "json_path"),
                ("fm-packaging-receipt-to-po-item", "fk"),
                ("portal-receipt-to-po-by-number-text", "text_match"),
                ("bc-invoice-to-portal-order-by-doc-number", "text_match")):
            contract = contracts.get(contract_id)
            self.assertEqual(contract["match"], match, contract_id)
            self.assertEqual(contract["strength"],
                             "lead" if match == "text_match" else "verified")
            self.assertTrue(contract["evidence"])
        # Per the pack, only the Portal receipt's stored PO id was set by a text join.
        self.assertEqual({cid for cid in contracts.ids() if contracts.get(cid)["provenance"]},
                         {"portal-receipt-to-po-by-id"})
        self.assertEqual(contracts.get("portal-receipt-to-po-by-id")["strength"],
                         "stored_by_text_join")
        for contract_id in contracts.ids():
            contract = contracts.get(contract_id)
            if contract["fm_source_fk"]:
                self.assertEqual(contract["pairs"][0]["from"],
                                 FM_SOURCE_FK[contract["fm_source_fk"]][0])
            # Every Portal behaviour is cited at a commit (a base commit, or the
            # file's verify/upstream-pins.json pin: tests/test_customer_po_scope.py).
            for item in contract["evidence"]:
                if item.startswith("Portal"):
                    self.assertRegex(item, r"^Portal [0-9a-f]{12}\b", contract_id)

    def test_invalid_contracts_are_refused(self):
        base = self.document()
        first = base["contracts"][0]

        def refuse(change, code):
            document = copy.deepcopy(base)
            change(document["contracts"][0])
            with self.assertRaises(ValidationError) as caught:
                LinkContracts(document)
            self.assertIn(code, str(caught.exception))

        refuse(lambda c: c["pairs"][0].update(compare="text"),
               "link_contract_compare_does_not_fit_match")
        refuse(lambda c: c.update(match="text_match"),
               "link_contract_compare_does_not_fit_match")
        refuse(lambda c: c["pairs"][0].update({"from": "idoeitem"}),
               "link_contract_fm_source_fk_mismatch")
        refuse(lambda c: c.update(match="json_path"),
               "link_contract_json_path_without_json_key")
        refuse(lambda c: c["pairs"][0].update({"from": "custom_fields.x"}),
               "link_contract_fk_reads_json_key")
        refuse(lambda c: c.update(evidence=[]), "link_contract_evidence_required")
        refuse(lambda c: c.update(surprise=1), "link_contract_unknown_key")
        refuse(lambda c: c.update(provenance="source_set_by_text_join"),
               "link_contract_provenance_needs_note")
        refuse(lambda c: c.update(provenance="guess", provenance_note="n"),
               "link_contract_invalid_provenance")
        duplicated = copy.deepcopy(base)
        duplicated["contracts"].append(copy.deepcopy(first))
        with self.assertRaises(ValidationError):
            LinkContracts(duplicated)
        with self.assertRaises(ValidationError):
            LinkContracts({**base, "schema": "other"})
        self.assertEqual(base["schema"], SCHEMA)


class CheckTests(unittest.TestCase):
    """The pure check over resolved views."""

    def setUp(self) -> None:
        self.contracts = LinkContracts.builtin()

    @staticmethod
    def view(relation, row, key=None):
        return {"relation": relation, "key": key or {"id": row.get("id")}, "row": row}

    def check(self, contract, source, target):
        return self.contracts.check(contract, "api://from", source, "api://to", target)

    def test_integer_ids_compare_by_value_across_spellings(self):
        ao = self.view("public.assembly_order", {"id": 10718})
        for value in (10718, "10718", "10718.0", 10718.0, " 10718 ", Decimal("10718"),
                      Decimal("10718.000")):
            link = self.view("public.order_entry_item_assembly",
                             {"id": 1245, "idassembly": value})
            self.assertEqual(self.check("fm-ao-link-to-assembly-order", link, ao)["outcome"],
                             "verified", value)
        for value, reason in ((10719, None), ("10719", None)):
            link = self.view("public.order_entry_item_assembly",
                             {"id": 1245, "idassembly": value})
            self.assertEqual(self.check("fm-ao-link-to-assembly-order", link, ao)["outcome"],
                             "contradicted")
        for value, reason in ((None, "from_value_absent"), ("", "from_value_absent"),
                              (True, "from_value_not_integer_id"),
                              ("10718.5", "from_value_not_integer_id"),
                              (10718.5, "from_value_not_integer_id"),
                              (-1, "from_value_not_integer_id"),
                              ("AO-26005B", "from_value_not_integer_id"),
                              (Decimal("10718.5"), "from_value_not_integer_id"),
                              (Decimal("NaN"), "from_value_not_integer_id"),
                              (Decimal("-1"), "from_value_not_integer_id")):
            link = self.view("public.order_entry_item_assembly",
                             {"id": 1245, "idassembly": value})
            checked = self.check("fm-ao-link-to-assembly-order", link, ao)
            self.assertEqual(checked["outcome"], "unknown", value)
            self.assertTrue(checked["reason"].startswith(reason), checked["reason"])
        missing = self.view("public.order_entry_item_assembly", {"id": 1245})
        self.assertIn("field_not_in_cited_row",
                      self.check("fm-ao-link-to-assembly-order", missing, ao)["reason"])

    def test_text_ids_are_exact_and_text_matches_are_trimmed_leads(self):
        po = self.view("public.app_purchase_order", {"id": "po-1", "po_number": "PO5447"})
        receipt = self.view("public.app_receipt", {"id": "r", "purchase_order_id": "PO-1"})
        self.assertEqual(self.check("portal-receipt-to-po-by-id", receipt, po)["outcome"],
                         "contradicted")  # text ids are case-sensitive
        receipt = self.view("public.app_receipt", {"id": "r", "po_reference": "  PO5447  "})
        self.assertEqual(self.check("portal-receipt-to-po-by-number-text", receipt, po)
                         ["outcome"], "lead")
        # Postgres TRIM removes spaces only: a trailing NBSP or tab is not trimmed.
        for text in ("PO5447\u00a0", "PO5447\t"):
            receipt = self.view("public.app_receipt", {"id": "r", "po_reference": text})
            self.assertEqual(self.check("portal-receipt-to-po-by-number-text", receipt, po)
                             ["outcome"], "no_match", repr(text))
        receipt = self.view("public.app_receipt", {"id": "r", "po_reference": "po5447"})
        checked = self.check("portal-receipt-to-po-by-number-text", receipt, po)
        self.assertEqual((checked["outcome"], checked["reason"]),
                         ("no_match", "text_differs_no_lead"))

    def test_json_path_reads_one_key_of_an_object_column(self):
        gpo = self.view("public.generated_purchase_order", {"id": 9001})
        for fields, outcome in (({"fmGeneratedPoId": 9001}, "verified"),
                                (json.dumps({"fmGeneratedPoId": "9001"}), "verified"),
                                ({"fmGeneratedPoId": 9002}, "contradicted"),
                                ({}, "unknown"), ("{bad", "unknown"), ("[1]", "unknown"),
                                (None, "unknown")):
            po = self.view("public.app_purchase_order", {"id": "po", "custom_fields": fields})
            self.assertEqual(self.check("portal-po-to-fm-generated-po", po, gpo)["outcome"],
                             outcome, fields)

    def test_wrong_relations_non_rows_and_unknown_contracts_are_refused(self):
        ao = self.view("public.assembly_order", {"id": 10718})
        line = self.view("public.order_entry_item", {"id": 1140, "idoe": 351})
        with self.assertRaises(ValidationError) as caught:
            self.check("fm-ao-link-to-assembly-order", line, ao)
        self.assertIn("link_citation_relation_is_not_contract_from", str(caught.exception))
        with self.assertRaises(ValidationError):
            self.contracts.check("fm-ao-link-to-assembly-order", "original://x", line,
                                 "api://to", ao)
        with self.assertRaises(ValidationError):
            self.check("no-such-contract", line, ao)
        several = {"relation": "public.order_entry_item_assembly", "key": {"id": 1},
                   "rows": [{"idassembly": 10718}, {"idassembly": 10719}]}
        checked = self.check("fm-ao-link-to-assembly-order", several, ao)
        self.assertEqual(checked["outcome"], "unknown")
        self.assertIn("key_not_unique_or_not_a_row", checked["reason"])


class AoChainTests(LinkHarness):
    def chain(self, server):
        return {"ao": self.cite(server, "public.assembly_order", 10718),
                "link": self.cite(server, "public.order_entry_item_assembly", 1245),
                "line": self.cite(server, "public.order_entry_item", 1140),
                "order": self.cite(server, "public.order_entry", 351),
                "item": self.cite(server, "silver.assembly_order_items", 58383),
                "task": self.cite(server, "silver.production_tasks", 14355),
                "damage": self.cite(server, "silver.assembly_order_damage", 5444)}

    def test_ao_chain_is_verified_hop_by_hop_and_supports_a_relation_claim(self):
        runtime = self.runtime()
        server = self.server(OPS_QUALITY, runtime)
        case_id = self.open(server)
        cite = self.chain(server)
        hops = [("fm-ao-link-to-assembly-order", cite["link"], cite["ao"]),
                ("fm-ao-link-to-order-line", cite["link"], cite["line"]),
                ("fm-order-line-to-order", cite["line"], cite["order"]),
                ("fm-ao-item-to-assembly-order", cite["item"], cite["ao"]),
                ("fm-production-task-to-assembly-order", cite["task"], cite["ao"]),
                ("fm-damage-to-assembly-order", cite["damage"], cite["ao"])]
        links = [self.link(server, case_id, *hop) for hop in hops]
        self.assertEqual([link["outcome"] for link in links], ["verified"] * 6)
        self.assertEqual([link["link_id"] for link in links],
                         ["L1", "L2", "L3", "L4", "L5", "L6"])
        self.assertEqual(links[0]["pairs"], [{
            "from_field": "idassembly", "to_field": "id", "compare": "integer_id",
            "from_value": 10718, "to_value": 10718, "equal": True}])
        self.assertIn("idassembly=10718 equals public.assembly_order row {\"id\":10718} "
                      "id=10718", links[0]["statement"])
        actor = Actor("plx", OPS_QUALITY)
        # Six links and six findings with mirror status exceed one response: the
        # MCP read is a summary listing the links section (paged below).
        mcp_view = self.ok(server, "plx.case_get", {"case_id": case_id})
        self.assertEqual(mcp_view["view"], "summary")
        self.assertIn("links", mcp_view["sections"])
        view = runtime.cases.get(actor, case_id)
        supported = view["findings"]["supported"]
        self.assertEqual(len(supported), 6)
        self.assertTrue(all(item["claim"]["statement_by"] == "workbench" for item in supported))
        self.assertEqual(supported[2]["claim"]["contracts"], ["fm-order-line-to-order"])
        self.assertEqual({cite_view["handle"] for cite_view in supported[2]["citations"]},
                         {cite["line"], cite["order"]})
        self.assertEqual([(item["id"], item["outcome"], item["contract"]["id"])
                          for item in view["links"]][:3],
                         [("L1", "verified", "fm-ao-link-to-assembly-order"),
                          ("L2", "verified", "fm-ao-link-to-order-line"),
                          ("L3", "verified", "fm-order-line-to-order")])
        self.assertTrue(all(item["citations_status"] == "resolved" for item in view["links"]))
        # Repeating a recorded check on the same content records nothing new.
        again = self.link(server, case_id, *hops[0])
        self.assertEqual((again["link_id"], again["duplicate"], again["finding_id"]),
                         ("L1", True, links[0]["finding_id"]))
        self.assertEqual(runtime.cases.get(actor, case_id)["integrity"]["events"],
                         view["integrity"]["events"])
        # The multi-hop relationship claim rests on the three verified links.
        receipt = runtime.cases.finding(
            actor, case_id, "AO-26005B (AO 10718) is linked by order line 1140 to sales "
            "order 351 (OR1000151885).", "fact", "supported",
            relation_links=["L1", "L2", "L3"])
        self.assertEqual(receipt["claim"]["link_outcomes"], ["verified"] * 3)
        claim = next(item for item in runtime.cases.get(actor, case_id)
                     ["findings"]["supported"] if item["id"] == receipt["finding_id"])
        self.assertEqual({item["handle"] for item in claim["citations"]},
                         {cite["link"], cite["ao"], cite["line"], cite["order"]})
        self.assertEqual(claim["claim"]["statement_by"], "caller")
        # Paged reads carry the links section too.
        page = self.ok(server, "plx.case_get", {"case_id": case_id, "section": "links",
                                                "limit": 200})
        self.assertEqual(page["total"], 6)
        self.assertEqual([item["id"] for item in page["items"]],
                         [item["id"] for item in view["links"]])
        summary = self.ok(server, "plx.case_get", {"case_id": case_id, "section": "summary"})
        self.assertEqual(summary["counts"]["links"], 6)

    def test_a_link_row_pointing_at_another_order_is_contradicted_and_cannot_be_supported(self):
        self.fake.tables["public.order_entry_item_assembly"][0]["idoeitem"] = 1141
        runtime = self.runtime()
        server = self.server(OPS_QUALITY, runtime)
        case_id = self.open(server)
        cite = self.chain(server)
        first = self.link(server, case_id, "fm-ao-link-to-assembly-order",
                          cite["link"], cite["ao"])
        wrong = self.link(server, case_id, "fm-ao-link-to-order-line",
                          cite["link"], cite["line"])
        self.assertEqual((first["outcome"], wrong["outcome"]), ("verified", "contradicted"))
        self.assertEqual((wrong["pairs"][0]["from_value"], wrong["pairs"][0]["to_value"]),
                         (1141, 1140))
        self.assertIn("the values differ", wrong["statement"])
        view = self.ok(server, "plx.case_get", {"case_id": case_id})
        contradicted = view["findings"]["contradicted"]
        self.assertEqual([item["id"] for item in contradicted], [wrong["finding_id"]])
        self.assertEqual(contradicted[0]["claim"]["link_outcomes"], ["contradicted"])
        actor = Actor("plx", OPS_QUALITY)
        with self.assertRaises(ValidationError) as caught:
            runtime.cases.finding(actor, case_id, "AO-26005B is for OR1000151885.", "fact",
                                  "supported", relation_links=["L1", "L2"])
        self.assertIn("relation_claim_supported_requires_verified_links:L2=contradicted",
                      str(caught.exception))
        with self.assertRaises(ValidationError):
            runtime.cases.finding(actor, case_id, "AO-26005B is not for OR1000151885.",
                                  "fact", "contradicted", relation_links=["L1"])
        receipt = runtime.cases.finding(actor, case_id, "AO-26005B is for OR1000151885.",
                                        "fact", "contradicted", relation_links=["L1", "L2"])
        self.assertEqual(receipt["claim"]["link_outcomes"], ["verified", "contradicted"])
        for bad, code in ((["L9"], "relation_link_not_found:L9"), ([], "relation_links_empty")):
            with self.assertRaises(ValidationError) as caught:
                runtime.cases.finding(actor, case_id, "x", "fact", "unknown",
                                      relation_links=bad)
            self.assertIn(code, str(caught.exception))
        with self.assertRaises(ValidationError):
            runtime.cases.finding(actor, case_id, "x", "interpretation", "unknown",
                                  relation_links=["L1"])
        # The real line of this link is 1141, which belongs to another order.
        other = self.cite(server, "public.order_entry_item", 1141)
        self.assertEqual(self.link(server, case_id, "fm-ao-link-to-order-line",
                                   cite["link"], other)["outcome"], "verified")
        self.assertEqual(self.link(server, case_id, "fm-order-line-to-order", other,
                                   cite["order"])["outcome"], "contradicted")

    def test_unknown_values_record_nothing_and_say_why(self):
        self.fake.tables["public.order_entry_item_assembly"][0]["idassembly"] = None
        server = self.server(OPS_QUALITY, self.runtime())
        case_id = self.open(server)
        cite = self.chain(server)
        before = self.ok(server, "plx.case_get", {"case_id": case_id})["integrity"]["events"]
        result = self.ok(server, "plx.case_add_finding", link_mode(
            case_id, "fm-ao-link-to-assembly-order", cite["link"], cite["ao"]))
        self.assertEqual((result["link"]["outcome"], result["link"]["recorded"],
                          result["link"]["reason"]),
                         ("unknown", False, "from_value_absent:idassembly"))
        self.assertEqual(result["next_tools"], ["plx.case_record_gap"])
        after = self.ok(server, "plx.case_get", {"case_id": case_id})
        self.assertEqual(after["integrity"]["events"], before)
        self.assertNotIn("links", after)

    def test_refusals_wrong_relation_unknown_contract_original_and_closed_case(self):
        server = self.server(OPS_QUALITY, self.runtime())
        case_id = self.open(server)
        cite = self.chain(server)
        add = "plx.case_add_finding"
        text = self.refused(server, add, link_mode(case_id, "fm-ao-link-to-assembly-order",
                                                   cite["line"], cite["ao"]))
        self.assertIn("link_citation_relation_is_not_contract_from", text)
        text = self.refused(server, add, link_mode(case_id, "ao-to-anything", cite["link"],
                                                   cite["ao"]))
        self.assertIn("unknown_link_contract", text)
        self.assertIn("fm-order-line-to-order", text)
        self.assertIn("citation_scheme_not_supported", self.refused(server, add, link_mode(
            case_id, "fm-ao-link-to-assembly-order", "https://example.test/x", cite["ao"])))
        self.assertIn("link_needs_two_different_citations", self.refused(
            server, add, link_mode(case_id, "fm-ao-link-to-assembly-order", cite["ao"],
                                   cite["ao"])))
        self.ok(server, "plx.case_close", {"case_id": case_id, "outcome": "done"})
        self.assertIn("case_closed", self.refused(server, add, link_mode(
            case_id, "fm-ao-link-to-assembly-order", cite["link"], cite["ao"])))

    def test_a_reader_without_the_silver_audience_sees_silver_links_withheld(self):
        runtime = self.runtime()
        writer = self.server(OPS_QUALITY, runtime)
        case_id = self.ok(writer, "plx.case_open", {
            "question": "AO items", "audience": ["staff:operations", "staff:quality"]}
                          )["case_id"]
        cite = self.chain(writer)
        self.link(writer, case_id, "fm-ao-link-to-assembly-order", cite["link"], cite["ao"])
        self.link(writer, case_id, "fm-ao-item-to-assembly-order", cite["item"], cite["ao"])
        reader = self.server(OPS, runtime)
        # The ops-only reader shares the case audience but cannot read silver.*.
        view = self.ok(reader, "plx.case_get", {"case_id": case_id})
        public, silver = view["links"]
        self.assertEqual(public["outcome"], "verified")
        self.assertEqual((silver["content"], silver["outcome"], silver["pairs"],
                          silver["statement"]),
                         ("withheld_citation_unavailable", None, None, None))
        self.assertNotIn("handle", json.dumps(silver["citations"]))
        self.assertEqual(len(view["findings"]["withheld"]), 1)
        self.assertEqual(len(view["findings"]["supported"]), 1)
        # Nor can that reader link a row it cannot resolve.
        self.assertIn("citation_unresolvable_for_actor", self.refused(
            reader, "plx.case_add_finding", link_mode(
                case_id, "fm-ao-item-to-assembly-order", cite["item"], cite["ao"])))


class PortalLinkTests(LinkHarness):
    def test_sales_order_to_fm_order_by_fmid_and_po_to_fm_po_by_custom_fields(self):
        server = self.server(OPS_QUALITY, self.runtime())
        case_id = self.open(server)
        order = self.cite(server, "public.order_entry", 351)
        outcomes = [self.link(server, case_id, "portal-so-to-fm-order-by-fmid",
                              self.cite(server, "public.app_sales_order", so_id), order)
                    for so_id in ("so-synth-1", "so-synth-2", "so-synth-3")]
        self.assertEqual([item["outcome"] for item in outcomes],
                         ["verified", "contradicted", "unknown"])
        self.assertEqual(outcomes[2]["reason"], "from_value_absent:custom_fields.fmId")
        gpo = self.cite(server, "public.generated_purchase_order", 9001)
        good = self.link(server, case_id, "portal-po-to-fm-generated-po",
                         self.cite(server, "public.app_purchase_order", "po-synth-5447"), gpo)
        self.assertEqual((good["outcome"], good["pairs"][0]["from_value"]),
                         ("verified", "9001.0"))
        broken = self.link(server, case_id, "portal-po-to-fm-generated-po",
                           self.cite(server, "public.app_purchase_order", "po-synth-5448"), gpo)
        self.assertEqual((broken["outcome"], broken["reason"]),
                         ("unknown", "from_json_column_unparseable:custom_fields.fmGeneratedPoId"))
        item = self.cite(server, "public.generated_purchase_order_item", 77001)
        receipt = self.cite(server, "public.receipt_packaging", 66001)
        self.assertEqual(self.link(server, case_id, "fm-packaging-receipt-to-po-item",
                                   receipt, item)["outcome"], "verified")
        self.assertEqual(self.link(server, case_id, "fm-po-item-to-fm-po", item, gpo)
                         ["outcome"], "verified")

    def test_receipt_links_are_text_join_qualified_or_leads_never_supported_claims(self):
        runtime = self.runtime()
        server = self.server(OPS_QUALITY, runtime)
        case_id = self.open(server)
        po = self.cite(server, "public.app_purchase_order", "po-synth-5447")
        # Portal stores the receipt's PO id but set it by a PO-number text join
        # (a weak link): recorded, qualified, never verified.
        by_id = self.link(server, case_id, "portal-receipt-to-po-by-id",
                          self.cite(server, "public.app_receipt", "rcpt-synth-1"), po)
        self.assertEqual((by_id["outcome"], by_id["recorded"], by_id["finding_id"]),
                         ("stored_by_text_join", True, None))
        self.assertEqual(by_id["contract"]["provenance"], "source_set_by_text_join")
        self.assertIn("Portal stores this PO id; Portal set it by PO-number text match",
                      by_id["statement"])
        self.assertIn("not a verified link", by_id["statement"])
        self.assertNotIn("the cited rows are linked", by_id["statement"])
        # A stored id that differs still contradicts.
        other = self.link(server, case_id, "portal-receipt-to-po-by-id",
                          self.cite(server, "public.app_receipt", "rcpt-synth-4"), po)
        self.assertEqual(other["outcome"], "contradicted")
        self.assertIsNotNone(other["finding_id"])
        lead = self.link(server, case_id, "portal-receipt-to-po-by-number-text",
                         self.cite(server, "public.app_receipt", "rcpt-synth-2"), po)
        self.assertEqual((lead["outcome"], lead["recorded"], lead["finding_id"]),
                         ("lead", True, None))
        self.assertIn("a lead from matching text, not a verified link", lead["statement"])
        self.assertEqual(lead["pairs"][0]["from_value"], " PO5447 ")
        none = self.link(server, case_id, "portal-receipt-to-po-by-number-text",
                         self.cite(server, "public.app_receipt", "rcpt-synth-3"), po)
        self.assertEqual((none["outcome"], none["recorded"]), ("no_match", False))
        # A receipt with no stored PO id is unknown under the id contract, not contradicted.
        self.assertEqual(self.link(server, case_id, "portal-receipt-to-po-by-id",
                                   self.cite(server, "public.app_receipt", "rcpt-synth-2"),
                                   po)["outcome"], "unknown")
        view = self.ok(server, "plx.case_get", {"case_id": case_id})
        self.assertEqual([item["outcome"] for item in view["links"]],
                         ["stored_by_text_join", "contradicted", "lead"])
        self.assertEqual(view["links"][0]["contract"]["provenance"], "source_set_by_text_join")
        self.assertEqual(view["findings"]["supported"], [])
        self.assertEqual([item["id"] for item in view["findings"]["contradicted"]],
                         [other["finding_id"]])
        self.assertTrue(any("verified link" in line and "never says the source value is "
                            "correct" in line for line in view["limitations"]))
        self.assertEqual(view["limitations"], list(LIMITATIONS))
        actor = Actor("plx", OPS_QUALITY)
        with self.assertRaises(ValidationError) as caught:
            runtime.cases.finding(actor, case_id, "Receipt RP0000001 is for PO5447.", "fact",
                                  "supported", relation_links=[by_id["link_id"]])
        self.assertIn(f"{by_id['link_id']}=stored_by_text_join", str(caught.exception))
        with self.assertRaises(ValidationError) as caught:
            runtime.cases.finding(actor, case_id, "Receipt RP0000002 is for PO5447.", "fact",
                                  "supported", relation_links=[lead["link_id"]])
        self.assertIn(f"{lead['link_id']}=lead", str(caught.exception))
        receipt = runtime.cases.finding(actor, case_id, "Receipt RP0000002 may be for PO5447.",
                                        "fact", "unknown", relation_links=[lead["link_id"]])
        self.assertEqual(receipt["claim"]["link_outcomes"], ["lead"])


class StoreLevelTests(unittest.TestCase):
    """Relation claims against rows whose content changed after the link was checked."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rows = {
            "api://public.order_entry_item/1140@g-1": {
                "relation": "public.order_entry_item", "key": {"id": 1140},
                "row": {"id": 1140, "idoe": 351}},
            "api://public.order_entry/351@g-2": {
                "relation": "public.order_entry", "key": {"id": 351}, "row": {"id": 351}}}
        self.store = CaseStore(Path(self.temp.name) / "cases.db",
                               citation_resolver=lambda handle, actor: copy.deepcopy(
                                   self.rows[handle]),
                               link_contracts=LinkContracts.builtin())
        self.actor = Actor("plx", OPS)

    def test_changed_content_withdraws_support_for_a_relation_claim(self):
        case_id = self.store.open(self.actor, "q")
        line, order = list(self.rows)
        link = self.store.link(self.actor, case_id, line, order, "fm-order-line-to-order")
        self.assertEqual(link["outcome"], "verified")
        self.rows[line]["row"]["idoe"] = 352
        with self.assertRaises(ValidationError) as caught:
            self.store.finding(self.actor, case_id, "line 1140 is on order 351", "fact",
                               "supported", relation_links=[link["link_id"]])
        self.assertIn("relation_link_content_changed_since_checked:L1", str(caught.exception))
        view = self.store.get(self.actor, case_id)
        self.assertEqual(view["links"][0]["citations_status"], "citation_content_changed")
        self.assertEqual(view["findings"]["supported"], [])
        self.assertEqual(len(view["findings"]["citation_content_changed"]), 1)
        # A fresh check of the new content is a new, contradicted link.
        again = self.store.link(self.actor, case_id, line, order, "fm-order-line-to-order")
        self.assertEqual((again["link_id"], again["outcome"]), ("L2", "contradicted"))

    def test_store_without_link_contracts_refuses_links(self):
        store = CaseStore(Path(self.temp.name) / "plain.db",
                          citation_resolver=lambda handle, actor: self.rows[handle])
        case_id = store.open(self.actor, "q")
        with self.assertRaises(ValidationError):
            store.link(self.actor, case_id, *self.rows, "fm-order-line-to-order")
        # Findings without relation links are unchanged.
        receipt = store.finding(self.actor, case_id, "a fact", "fact", "supported",
                                list(self.rows)[:1])
        self.assertNotIn("claim", receipt)
        self.assertNotIn("links", store.get(self.actor, case_id))


if __name__ == "__main__":
    unittest.main()
