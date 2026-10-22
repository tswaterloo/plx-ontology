"""AO-26005B end to end through the case MCP tools (injected transport + isolated fixture).

Since task allowlist-3 the investigation under test is the reference investigation of
``operating/tests/ao_reference_investigation.py`` (moved out of product code; product
cases open through the generic tools, ``operating.case_workspace_live``). These tests keep
guarding what that realistic case exercises in the case tools: cited computations with
quantity roles, per-component damage, evidence-conflict gaps, paging, originals and
per-reader withholding. The same scenarios on the generic path are
``ontology/tests/test_quality_packets.py`` and ``operating/tests/test_case_workspace_live.py``.

Evidence label: ``injected-transport`` for the rows and ``isolated-fixture`` for
the scan. The rows are the values of the 2026-09-30 live trace
(``EVIDENCE/live/2026-09-30-ao-26005b-live-trace.json``: FM AssemblyOrder
10718 -> link 1245 -> SalesOrderLine 1140 -> SalesOrder 351 / OR1000151885),
served by the in-process ``FakeWorkbench`` under the FM column names the trace
records; that the live reporting API exposes the
same columns is what the live checks verify. The scan is a
synthetic image-only PDF (``fixtures/ao-26005b-scan-fixture.pdf``, marked
"SYNTHETIC TEST FIXTURE"), not Ricardo's original. Nothing here is live data.

The reference investigation is what an MCP client (Claude) does for Ricardo's question: open a case,
resolve and cite the rows, derive planned and produced finished goods from the
component consumed one per unit, compute planned / produced / filled and ordered
vs produced with the case compute tool, keep damage per component (never one
"pack" total), record the gaps and the evidence conflicts (FM lateness, paper vs
FM), and report the paper figures as unverified leads outside the case. The test
then attaches the scan through the quality inbox, cites its OCR page, and reads
the same case back from a fresh runtime (a later session).
"""

from __future__ import annotations

from decimal import Decimal
import copy
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

from integrations.source_mirror import AUDIENCE_SCHEMA
from integrations.tests.test_source_mirror import BASE, KEY, CATALOG, Clock, FakeWorkbench
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology.core import Actor
from operating.mcp_server import source_read
from operating import source_reads
from operating.tests import ao_reference_investigation as ao_reference
from operating.tests.ao_reference_investigation import MissingData, load_paper_leads
from operating.case_runtime import CaseRuntime
from operating.mcp_server import HOST_READS, MAX_RESULT_BYTES, MCPServer
from operating.tests.firewall_view import client_result
from operating.tests.test_case_originals import FIXTURE, local_ocr_available

# AO-26005B's expectations: the 2026-09-30 live trace
# (EVIDENCE/live/2026-09-30-ao-26005b-live-trace.json). They
# belong to this test and to the evaluation data (evals/), never to product
# code; every computed value is read from a cited row and only compared with these.
AO_CODE, ORDER_CODE = "AO-26005B", "OR1000151885"
EXPECTED = {"planned_minus_produced": "6", "produced_minus_filled": "1",
            "ordered_minus_produced": "417"}
# Damage per component, keyed by the component's packaging code (public.packaging.packcode,
# the code the paper packet prints), never by an AO item id.
EXPECTED_DAMAGE = {"P-0121": "7", "P-0156": "3", "P-01274": "20"}
EXPECTED_PRODUCED = "1503"
# AO 10718 -> link -> line 1140 -> sales order 351 (ids of the trace, compared only).
EXPECTED_CHAIN = {"link_ao": "10718", "link_line": "1140", "line_id": "1140",
                  "line_order": "351", "ao_id": "10718", "item_ao": "10718",
                  "order_id": "351"}
PAPER_LEADS_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / \
    "ao_26005b_paper_leads.json"


def investigate(call, paper=None, originals=None, **kwargs):
    """The product investigation of AO-26005B (the AO these fixtures are)."""
    kwargs.setdefault("ao_code", AO_CODE)
    return ao_reference.investigate(call, paper, originals, **kwargs)


def chain_text(out) -> dict:
    return {name: None if value is None else str(value) for name, value in out["chain"].items()}


OPS_QUALITY = frozenset({"staff:operations", "staff:quality"})
OPS = frozenset({"staff:operations"})
GRANTED = {"public.*": ["staff:operations"], "silver.*": ["staff:quality"]}
SCAN = "AO-26005B scan.pdf"
PAPER = load_paper_leads(PAPER_LEADS_EXAMPLE)  # [PAPER] unverified leads (example file)


def _relation(columns, domain="manufacturing"):
    return {"columns": columns, "key": ["id"], "domain": domain, "since": None}


AO_CATALOG = {
    "public.assembly_order": _relation(
        ["id", "nameamo_c", "idfinprod", "idcustomerorderassembly", "qtytotal", "status",
         "datemanuf", "finalqty", "units"]),
    "public.order_entry_item_assembly": _relation(
        ["id", "idoeitem", "idassembly", "qtyplan_c", "qtymfg_c", "qtyplan", "qtymfg"]),
    "public.order_entry_item": _relation(
        ["id", "idoe", "idfp", "idbulk", "qty", "status_c"], "orders"),
    "public.order_entry": _relation(
        ["id", "idcust", "custpocode", "erpcode", "reference", "datepo", "datereq", "status"],
        "orders"),
    "silver.assembly_order_items": _relation(
        ["id", "assembly_order_id", "packaging_id", "bulk_product_id", "finished_product_id",
         "quantity", "actual_amount", "qty_per_batch", "qty_per_batch_box"]),
    "silver.production_tasks": _relation(
        ["id", "mo_id", "category", "task_type", "date_start", "time_start", "time_finish",
         "hours_total", "qty", "uom"]),
    "public.app_manufacturing_order": _relation(
        ["id", "mo_code", "quantity", "status", "qty_sample", "qty_damaged", "size_pieces",
         "lot_number"]),
    "silver.assembly_order_damage": _relation(
        ["id", "assembly_order_id", "assembly_item_id", "raw_or_pack", "damage_type", "status",
         "qty_damaged", "qty_total", "date_entered"]),
    "public.packaging": _relation(["id", "packcode", "name", "status"], "products"),
}

# Values from the live trace; the AO header quantities were empty live. The Portal
# manufacturing order row is NOT from the trace (the trace did not read that relation):
# its values are fixture assumptions, checked by the live checks.
AO_TABLES = {
    # Packaging master rows of the live trace (code = the code the paper packet prints).
    "public.packaging": [
        {"id": 162, "packcode": "P-0156", "name": "Gallon Jug US 3.78L (Round)",
         "status": "Active"},
        {"id": 122, "packcode": "P-0121", "name": "Petra, Cap, for Gallon with seal",
         "status": "Active"},
        {"id": 2047, "packcode": "P-01872", "name": "DVS, Label", "status": "Active"},
        {"id": 1415, "packcode": "P-01274", "name": "Petra, Box for Round Gallons",
         "status": "Active"},
        {"id": 152, "packcode": "P-0147", "name": "Petra, Label, 4X4 White",
         "status": "Active"}],
    "public.app_manufacturing_order": [
        {"id": "mo-10718", "mo_code": "AO-26005B", "quantity": "1509", "status": "Realized",
         "qty_sample": "1", "qty_damaged": "0", "size_pieces": "1509",
         "lot_number": "26005B"}],
    "public.assembly_order": [
        {"id": 10718, "nameamo_c": "AO-26005B", "idfinprod": 1534,
         "idcustomerorderassembly": 89, "qtytotal": None, "status": "Realized",
         "datemanuf": "2026-01-05", "finalqty": None, "units": "EA"},
        {"id": 10719, "nameamo_c": "AO-26006", "idfinprod": 1600,
         "idcustomerorderassembly": 90, "qtytotal": "800.0", "status": "Planned",
         "datemanuf": None, "finalqty": None, "units": "EA"}],
    "public.order_entry_item_assembly": [
        {"id": 1245, "idoeitem": 1140, "idassembly": 10718, "qtyplan_c": "0.0",
         "qtymfg_c": "1503.0", "qtyplan": None, "qtymfg": None}],
    "public.order_entry_item": [
        {"id": 1140, "idoe": 351, "idfp": 1534, "idbulk": 1091, "qty": "1920.0",
         "status_c": "Completed"}],
    "public.order_entry": [
        {"id": 351, "idcust": 89, "custpocode": "PO12172025", "erpcode": "OR1000151885",
         "reference": None, "datepo": "2025-12-17", "datereq": "2026-01-01",
         "status": "Closed"}],
    "silver.assembly_order_items": [
        {"id": 58382, "assembly_order_id": 10718, "packaging_id": None,
         "bulk_product_id": 1091, "finished_product_id": 1534, "quantity": "5998.275",
         "actual_amount": "5974.425", "qty_per_batch": "3.975", "qty_per_batch_box": "15.9"},
        {"id": 58383, "assembly_order_id": 10718, "packaging_id": 162, "bulk_product_id": None,
         "finished_product_id": 1534, "quantity": "1509.0", "actual_amount": "1503.0",
         "qty_per_batch": "1.0", "qty_per_batch_box": "4.0"},
        {"id": 58384, "assembly_order_id": 10718, "packaging_id": 122, "bulk_product_id": None,
         "finished_product_id": 1534, "quantity": "1509.0", "actual_amount": "1503.0",
         "qty_per_batch": "1.0", "qty_per_batch_box": "4.0"},
        {"id": 58385, "assembly_order_id": 10718, "packaging_id": 2047, "bulk_product_id": None,
         "finished_product_id": 1534, "quantity": "1509.0", "actual_amount": "1503.0",
         "qty_per_batch": "1.0", "qty_per_batch_box": "4.0"},
        {"id": 58386, "assembly_order_id": 10718, "packaging_id": 1415, "bulk_product_id": None,
         "finished_product_id": 1534, "quantity": "378.0", "actual_amount": "375.75",
         "qty_per_batch": "0.25", "qty_per_batch_box": "1.0"},
        {"id": 58387, "assembly_order_id": 10718, "packaging_id": 152, "bulk_product_id": None,
         "finished_product_id": 1534, "quantity": "755.0", "actual_amount": "751.5",
         "qty_per_batch": "0.5", "qty_per_batch_box": "2.0"}],
    "silver.production_tasks": [
        {"id": 14353, "mo_id": 10718, "category": "AO", "task_type": "Setup",
         "date_start": "2026-01-07", "time_start": "16:30:00", "time_finish": "17:20:00",
         "hours_total": "3.333333333333333", "qty": "0.0", "uom": None},
        {"id": 14354, "mo_id": 10718, "category": "AO", "task_type": "Line clearance",
         "date_start": "2026-01-07", "time_start": "17:20:00", "time_finish": "17:25:00",
         "hours_total": "0.25", "qty": "1.0", "uom": None},
        {"id": 14355, "mo_id": 10718, "category": "AO", "task_type": "Filling",
         "date_start": "2026-01-07", "time_start": "17:25:00", "time_finish": "21:45:00",
         "hours_total": "17.333333333333332", "qty": "1502.0", "uom": None},
        {"id": 14356, "mo_id": 10718, "category": "AO", "task_type": "Cleaning",
         "date_start": "2026-01-05", "time_start": "21:45:00", "time_finish": "22:30:00",
         "hours_total": "3.0", "qty": "0.0", "uom": None}],
    "silver.assembly_order_damage": [
        {"id": 5444, "assembly_order_id": 10718, "assembly_item_id": 58386,
         "raw_or_pack": "Pack", "damage_type": "Pack", "status": "Generated",
         "qty_damaged": "20.0", "qty_total": "180627.35", "date_entered": "2026-01-08"},
        {"id": 5445, "assembly_order_id": 10718, "assembly_item_id": 58383,
         "raw_or_pack": "Pack", "damage_type": "Pack", "status": "Generated",
         "qty_damaged": "3.0", "qty_total": "180627.35", "date_entered": "2026-01-08"},
        {"id": 5446, "assembly_order_id": 10718, "assembly_item_id": 58384,
         "raw_or_pack": "Pack", "damage_type": "Pack", "status": "Generated",
         "qty_damaged": "7.0", "qty_total": "180627.35", "date_entered": "2026-01-08"}],
}


class Ao26005bCaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.audience_path = self.root / "audience.json"
        self.audience_path.write_text(json.dumps({
            "schema": AUDIENCE_SCHEMA, "tenant": "plx", "audiences": GRANTED}), "utf-8")
        self.fake = FakeWorkbench()
        self.fake.catalog = copy.deepcopy(CATALOG)
        self.fake.catalog.update(copy.deepcopy(AO_CATALOG))
        for name, rows in AO_TABLES.items():
            self.fake.tables[name] = [dict(row) for row in rows]
        self.clock = Clock()
        self.request_id = 100

    def runtime(self) -> CaseRuntime:
        client = WorkbenchReadApiClient(BASE, KEY, fetch=self.fake)
        runtime = CaseRuntime(self.state, client=client, audience=self.audience_path,
                              tenant="plx", mirror_options={
                                  "clock": self.clock, "retry_delay_seconds": 0},
                              originals_options={"local_ocr": True})
        self.addCleanup(runtime.close)
        return runtime

    def server(self, principals, runtime) -> MCPServer:
        # A case-only session: the case tools never need UnifiedPlatform.
        server = MCPServer(case_runtime=runtime, actor=Actor("plx", frozenset(principals)))
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                  "clientInfo": {"name": "ao-case", "version": "1"}}})
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
        size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())
        self.assertLessEqual(size, MAX_RESULT_BYTES)
        return result["structuredContent"]

    def caller(self, server):
        """The investigation's tool caller: host code, as ``local_client`` is (results as
        the tools built them; ``ok`` reads them through the output boundary)."""
        def call(name, arguments):
            if name in source_reads.READS:  # a driver's direct read, not a host tool
                return source_read(server, name, arguments or {})
            self.request_id += 1
            result = server.handle({"jsonrpc": "2.0", "id": self.request_id,
                                    "method": "tools/call",
                                    "params": {"name": name, "arguments": arguments}},
                                   host_code=True)["result"]
            self.assertFalse(result["isError"], result)
            return result["structuredContent"]
        return call

    def case_view(self, server, case_id) -> dict:
        """Read a whole case the way a client must: the full view when it fits
        one response, else the summary plus every section paged to the end.
        Returns the case without the response-shape keys, so both paths
        compare equal."""
        view = self.ok(server, "plx.case_get", {"case_id": case_id})
        if view["view"] == "full":
            self.assertTrue(view["complete"])
            return {key: value for key, value in view.items() if key not in ("view", "complete")}
        self.assertEqual((view["view"], view["complete"]), ("summary", False))
        whole = {key: value for key, value in view.items() if key not in (
            "view", "complete", "counts", "reason", "sections", "paging", "header_truncated")}
        if view.get("header_truncated"):
            header = self.ok(server, "plx.case_get", {"case_id": case_id, "section": "header"})
            self.assertEqual(header["view"], "header")
            whole.update({key: value for key, value in header.items()
                          if key not in ("view", "complete")})
        whole["findings"] = {bucket: [] for bucket in view["counts"]["findings"]}
        for section in ("findings", "calculations", "gaps", "proposals", "verifications",
                        "timeline"):
            items, offset = [], 0
            while offset is not None:
                page = self.ok(server, "plx.case_get", {"case_id": case_id, "section": section,
                                                        "offset": offset, "limit": 200})
                self.assertEqual((page["view"], page["section"]), ("section", section))
                self.assertEqual(page["offset"] if "offset" in page else offset, offset)
                self.assertTrue(page["items"] or page["next_offset"] is None)
                items.extend(page["items"])
                offset = page["next_offset"]
            # A shrunk or oversized item is not the case's content.
            self.assertFalse([item for item in items
                              if item.get("mirror_status_omitted") or item.get("oversized")])
            if section == "findings":
                for item in items:
                    whole["findings"][item.pop("bucket")].append(item)
                self.assertEqual({bucket: len(found) for bucket, found in
                                  whole["findings"].items()}, view["counts"]["findings"])
            else:
                whole[section] = items
                self.assertEqual(len(items), view["counts"][section])
        return whole

    BOOLEAN_CHECKS = ("chain_consistent", "order_link_found", "fg_basis_found",
                      "component_actuals_consistent", "link_qtymfg_matches_derived",
                      "ao_header_quantities_empty", "fm_dates_after_required_date",
                      "dates_readable", "mo_found")

    def assert_live_trace(self, out) -> None:
        """What the 2026-09-30 live trace showed for AO-26005B (compared, never used)."""
        self.assertEqual(chain_text(out), EXPECTED_CHAIN)
        self.assertEqual(out["results"], EXPECTED)
        self.assertEqual(out["damaged_by_code"], EXPECTED_DAMAGE)
        self.assertEqual(out["produced_fg"], EXPECTED_PRODUCED)

    def gap_titles(self, view) -> list:
        return [(gap["state"], gap["missing"]) for gap in view["gaps"]]

    def test_ao_26005b_is_investigated_cited_computed_and_recorded(self):
        runtime = self.runtime()
        server = self.server(OPS_QUALITY, runtime)
        out = investigate(self.caller(server), PAPER)
        case_id = out["case_id"]
        self.assert_live_trace(out)
        self.assertEqual({name: out["checks"][name] for name in self.BOOLEAN_CHECKS},
                         {name: True for name in self.BOOLEAN_CHECKS})
        self.assertEqual((out["checks"]["damage_rows"], out["checks"]["related_aos"],
                          out["checks"]["paper_damage_conflicts"]), (3, 0, 1))
        view = self.case_view(server, case_id)
        self.assertTrue(view["integrity"]["chain_ok"])
        supported = [finding["statement"] for finding in view["findings"]["supported"]]
        self.assertEqual(len(supported), 6)
        self.assertIn("derived from its component row 58383, consumed one per finished unit "
                      "(qty_per_batch 1): planned 1509 and produced 1503", supported[1])
        self.assertIn("qtymfg_c (1503), an FM link-table calculation no Portal code reads",
                      supported[2])
        # The link's qtyplan_c (0 live) is labelled an FM calc, never the planned figure.
        self.assertIn("Its qtyplan_c is likewise an FM link-table calculation no Portal code "
                      "reads, not the planned figure.", supported[2])
        # The unfilled unit is the cited Line clearance task's quantity (FM side of QC retain).
        self.assertIn("minus the Filling task's 1502 equals what its Line clearance task "
                      "row(s) record (1)", supported[3])
        self.assertEqual(view["findings"]["supported"][3]["citations"][2]["handle"],
                         out["cite"]["clearance"][0])
        # (c) The unfilled unit is the AO's retained sample: the Portal manufacturing
        # order's qty_sample equals it, and that row is cited beside the FM tasks.
        self.assertIn("The Portal manufacturing order AO-26005B records the same number of "
                      "samples (1), so the unfilled unit is the AO's retained sample (a QC "
                      "retain sample), not a loss.", supported[3])
        self.assertEqual(view["findings"]["supported"][3]["citations"][3]["handle"],
                         out["cite"]["mo"])
        self.assertTrue(out["qc_retain"])
        self.assertIn("AO item 58383: 3; AO item 58384: 7; AO item 58386: 20", supported[4])
        self.assertIn("manufacturing order AO-26005B records qty_sample 1, qty_damaged 0, "
                      "size_pieces 1509", supported[5])
        # Planned finished goods corroborated by the Portal's size in pieces (served on the
        # manufacturing order; the FM AO's sizepieces is not in this fixture's catalog).
        self.assertIn("The planned figure equals the Portal manufacturing order's size_pieces, "
                      "the Portal's planned production quantity", supported[1])
        self.assertEqual([cite["handle"] for cite in
                          view["findings"]["supported"][1]["citations"]],
                         [out["cite"]["ao"], out["cite"]["item"], out["cite"]["mo"]])
        self.assertEqual(out["planned_fg"], {"derived": "1509", "sources": {
            "fm_sizepieces": "unread", "mo_size_pieces": "1509"}})
        # The Portal's produced-for-line columns are not served here: visible, not zero.
        self.assertEqual(out["line_produced"]["state"], "unread")
        # Lateness is a recorded conflict, never a supported fact.
        self.assertFalse([text for text in supported if "late" in text.lower()])
        self.assertEqual(view["findings"]["contradicted"], [])
        self.assertEqual(self.gap_titles(view), [
            # (a) The Portal's produced/accepted columns are not served: unread, not absent.
            ("inaccessible", "AO-26005B produced and accepted finished goods as the Portal "
                             "reads them (totalprodqty_c, qtyacc_c): unread"),
            ("unknown", "Whether the rest of order line 1140 was produced by other assembly "
                        "orders"),
            ("unknown", "Whether AO-26005B was late against the governing required date: "
                        "evidence conflict"),
            ("absent_in_source", "AO-26005B header quantities (qtytotal, finalqty)"),
            ("not_ingested", "The paper AO-26005B packet original (scan) is not attached")])
        self.assertIn("not evidence that they are empty", view["gaps"][0]["why"])
        self.assertEqual(out["fg_header"]["unread"], ["totalprodqty_c", "qtyacc_c"])
        self.assertEqual(out["produced_fg_basis"], "component row actual_amount / qty_per_batch")
        view["gaps"] = view["gaps"][1:]
        late = view["gaps"][1]["why"]
        for text in ("date manufactured 2026-01-05", "Filling task 2026-01-07",
                     "requires 2026-01-01", "customer PO PO12172025",
                     "not its Filling task's date, so it may be a plan date",
                     "printed plan date versus FM's date manufactured: agrees",
                     "production and QC release date versus FM's date manufactured: disagrees",
                     "required-date and PO fields are blank"):
            self.assertIn(text, late)
        self.assertIn("not a proven shortfall", view["gaps"][0]["why"])
        self.assertIn("qtyaomfg_c plus qtybcmfg_c, its orders view) was not read: the cited "
                      "line row does not serve those columns.", view["gaps"][0]["why"])
        # The paper wording is built from the leads: what disagrees and what agrees.
        self.assertIn("the paper leads disagree on: shipper box damage; production and QC "
                      "release date versus FM's date manufactured. They agree on: gallon jug "
                      "damage; cap damage; filled count; units produced but not filled",
                      view["gaps"][3]["why"])
        # Planned, actual, filled, ordered and damaged stay distinct roles; finished goods
        # are derived from the component row by qty_per_batch, every unit is declared.
        calcs = view["calculations"]
        self.assertEqual([(calc["expression"], calc["unit"], calc["result"], calc["result_role"])
                          for calc in calcs], [
            ("planned / per_unit - produced / per_unit", "EA", "6", "planned_not_produced"),
            ("produced / per_unit - filled", "EA", "1", "produced_not_filled"),
            ("d0", "item58383", "3", "damaged"), ("d0", "item58384", "7", "damaged"),
            ("d0", "item58386", "20", "damaged"),
            ("ordered - produced / per_unit", "EA", "417", "ordered_not_produced_by_this_ao")])
        self.assertEqual({spec["citation"]["handle"] for spec in calcs[0]["inputs"].values()},
                         {out["cite"]["item"]})
        self.assertEqual({(name, spec["unit"], spec.get("role"), spec.get("role_basis"))
                          for calc in calcs[:2] for name, spec in calc["inputs"].items()},
                         {("planned", "pc", "planned", "source_contract"),
                          ("produced", "pc", "actual", "source_contract"),
                          ("per_unit", "pc/EA", None, None),
                          # A Filling task's qty is pinned "filled" by a conditional source
                          # contract (silver-production-tasks-qty-filled-on-filling), so the
                          # role is no longer only the caller's declaration.
                          ("filled", "EA", "filled", "source_contract")})
        self.assertEqual(view["proposals"], [])

    def test_damage_of_different_components_is_never_one_total(self):
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        box, jug = out["cite"]["damage"][0], out["cite"]["damage"][1]
        refused = self.call(server, "plx.case_compute", {
            "case_id": out["case_id"], "expression": "box + jug", "unit": "item58386",
            "inputs": {"box": {"citation": box, "field": "row.qty_damaged", "unit": "item58386"},
                       "jug": {"citation": jug, "field": "row.qty_damaged",
                               "unit": "item58383"}}})
        self.assertTrue(refused["isError"])
        self.assertIn("unit_mismatch", refused["content"][0]["text"])
        self.assertNotIn("30", json.dumps(out["damaged"]))
        # A second damage row of the same component is summed in that component's unit.
        self.setUp()
        self.fake.tables["silver.assembly_order_damage"].append(
            {"id": 5447, "assembly_order_id": 10718, "assembly_item_id": 58386,
             "raw_or_pack": "Pack", "damage_type": "Pack", "status": "Generated",
             "qty_damaged": "2.0", "qty_total": "180627.35", "date_entered": "2026-01-09"})
        again = investigate(self.caller(self.server(OPS_QUALITY, self.runtime())))
        self.assertEqual(again["damaged_by_code"], {**EXPECTED_DAMAGE, "P-01274": "22"})

    def test_paper_leads_are_compared_with_the_cited_fm_rows_and_kept_out_of_the_case(self):
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server), PAPER)
        leads = {lead["id"]: lead for lead in out["paper_leads"]}
        self.assertTrue(all(lead["status"] == "unverified_lead" and
                            "[PAPER]" in lead["paper_source"] for lead in leads.values()))
        self.assertEqual({name: (lead["fm"], lead["paper"], lead["agrees"])
                          for name, lead in leads.items()}, {
            "damage_box": ("20", "11", False), "damage_jug": ("3", "3", True),
            "damage_cap": ("7", "7", True), "filled": ("1502", "1502", True),
            "qc_retain": ("1", "1", True),
            "production_date": ("2026-01-05", "2026-01-07", False),
            "plan_date": ("2026-01-05", "2026-01-05", True)})
        box = leads["damage_box"]
        self.assertEqual(box["fm_citations"], [out["cite"]["damage"][0]])
        # The paper figures are leads in the report; the case names the conflict in words.
        view = self.case_view(server, out["case_id"])
        [paper] = [gap for gap in view["gaps"] if gap["state"] == "not_ingested"]
        self.assertIn("disagree on: shipper box damage", paper["why"])
        self.assertIn("QC retain", paper["why"])
        # A corrected FM box row flips the wording: no stale conflict is asserted.
        self.setUp()
        self.fake.tables["silver.assembly_order_damage"][0]["qty_damaged"] = "11.0"
        fixed_server = self.server(OPS_QUALITY, self.runtime())
        fixed = investigate(self.caller(fixed_server), PAPER)
        self.assertEqual(fixed["checks"]["paper_damage_conflicts"], 0)
        [paper] = [gap for gap in self.case_view(fixed_server, fixed["case_id"])["gaps"]
                   if gap["state"] == "not_ingested"]
        self.assertIn("They agree on: shipper box damage", paper["why"])
        self.assertNotIn("disagree on: shipper box damage", paper["why"])
        # Without a leads file nothing paper-derived is claimed.
        self.setUp()
        bare_server = self.server(OPS_QUALITY, self.runtime())
        bare = investigate(self.caller(bare_server))
        self.assertEqual(bare["paper_leads"], [])
        self.assertNotIn("paper_damage_conflicts", bare["checks"])
        bare_view = self.case_view(bare_server, bare["case_id"])
        self.assertIn("No paper leads were supplied", bare_view["gaps"][-1]["why"])
        self.assertNotIn("required-date and PO fields", json.dumps(bare_view["gaps"]))
        statements = " ".join([finding["statement"] for finding in view["findings"]["supported"]]
                              + [gap["missing"] + " " + gap["why"] for gap in view["gaps"]])
        self.assertNotRegex(statements, r"\b(11|21|30)\b")

    def test_the_numbers_come_from_the_cited_rows_not_the_expectations(self):
        for item in self.fake.tables["silver.assembly_order_items"][1:4]:
            item["actual_amount"] = "1500.0"  # every unit-ratio component agrees on 1500
        self.fake.tables["public.assembly_order"][0]["qtytotal"] = "1509.0"
        out = investigate(self.caller(self.server(OPS_QUALITY, self.runtime())))
        self.assertEqual(out["results"]["planned_minus_produced"], "9")
        self.assertEqual(out["produced_fg"], "1500")
        self.assertNotEqual(out["results"], EXPECTED)
        self.assertFalse(out["checks"]["ao_header_quantities_empty"])
        # The box and labels still say 1503 x their ratio, the link still 1503.
        self.assertFalse(out["checks"]["component_actuals_consistent"])
        self.assertFalse(out["checks"]["link_qtymfg_matches_derived"])

    def test_components_implying_another_produced_figure_are_a_recorded_conflict(self):
        self.fake.tables["silver.assembly_order_items"][4]["actual_amount"] = "376.5"  # box
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        self.assertFalse(out["checks"]["component_actuals_consistent"])
        view = self.case_view(server, out["case_id"])
        [gap] = [gap for gap in view["gaps"] if "every component" in gap["missing"]]
        self.assertEqual(gap["state"], "unknown")
        self.assertEqual([cite["handle"] for cite in gap["citations"]],
                         [out["cite"]["item"],
                          *[row["citation"] for row in source_reads.read(server.case_runtime, server.actor, "mirror.rows", {
                              "relation": "silver.assembly_order_items",
                              "where": [["id", "eq", 58386]]})["rows"]]])

    def test_an_unaccounted_unfilled_unit_is_a_gap_and_nothing_unread_is_claimed(self):
        tasks = self.fake.tables["silver.production_tasks"]
        tasks[:] = [task for task in tasks if task["task_type"] != "Line clearance"]
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        self.assertEqual(out["cite"]["clearance"], [])
        view = self.case_view(server, out["case_id"])
        [gap] = [gap for gap in view["gaps"] if "produced but not filled" in gap["missing"]]
        self.assertNotIn("read, not cited", gap["why"])
        self.assertFalse([f for f in view["findings"]["supported"]
                          if "Line clearance" in f["statement"]])

    def serve_fg_columns(self, total, accepted, sample="1.0"):
        self.fake.catalog["public.assembly_order"]["columns"] += [
            "totalprodqty_c", "qtyacc_c", "qtysample"]
        self.fake.tables["public.assembly_order"][0].update(
            totalprodqty_c=total, qtyacc_c=accepted, qtysample=sample)

    def test_served_portal_fg_columns_are_the_produced_figure_and_checked(self):
        # (a) When the API serves them, FG produced is totalprodqty_c (incl. samples) and
        # accepted is qtyacc_c (produced - samples), cited from the AO row.
        self.serve_fg_columns("1503.0", "1502.0")
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        self.assertEqual((out["produced_fg"], out["produced_fg_basis"]),
                         ("1503", "assembly_order.totalprodqty_c"))
        self.assertEqual(out["fg_header"]["unread"], [])
        view = self.case_view(server, out["case_id"])
        [header] = [f for f in view["findings"]["supported"]
                    if "totalprodqty_c" in f["statement"]]
        self.assertIn("records totalprodqty_c 1503 (produced finished goods including samples), "
                      "qtyacc_c 1502 (accepted finished goods (produced minus samples))",
                      header["statement"])
        self.assertEqual([c["handle"] for c in header["citations"]], [out["cite"]["ao"]])
        self.assertFalse([g for g in view["gaps"] if g["state"] == "inaccessible"])
        self.assertFalse([g for g in view["gaps"] if "totalprodqty_c says" in g["missing"]])
        # A header that disagrees with the component rows is a conflict citing both.
        self.setUp()
        self.serve_fg_columns("1500.0", "1499.0")
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        self.assertEqual(out["produced_fg"], "1500")
        [gap] = [g for g in self.case_view(server, out["case_id"])["gaps"]
                 if "totalprodqty_c says" in g["missing"]]
        self.assertEqual((gap["state"], [c["handle"] for c in gap["citations"]]),
                         ("unknown", [out["cite"]["ao"], out["cite"]["item"]]))
        # Served but empty is absent in the source (not unread).
        self.setUp()
        self.serve_fg_columns(None, None)
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        self.assertEqual(out["fg_header"]["empty"], ["totalprodqty_c", "qtyacc_c"])
        titles = self.gap_titles(self.case_view(server, out["case_id"]))
        self.assertIn(("absent_in_source", "AO-26005B totalprodqty_c, qtyacc_c"), titles)
        self.assertFalse([title for title in titles if title[0] == "inaccessible"])

    def test_a_box_row_floored_by_the_portal_cascade_implies_the_same_production(self):
        # (a) cascadeAssemblyActualQty floors box-type packaging rows to whole boxes:
        # 0.25 x 1503 = 375.75 -> 375 is the same production, not a conflict.
        self.fake.tables["silver.assembly_order_items"][4]["actual_amount"] = "375.0"
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        self.assertTrue(out["checks"]["component_actuals_consistent"])
        self.assertFalse([g for g in self.case_view(server, out["case_id"])["gaps"]
                          if "every component" in g["missing"]])
        # A bulk row (kg) is never floored: 5974 kg is another production.
        self.setUp()
        self.fake.tables["silver.assembly_order_items"][0]["actual_amount"] = "5974.0"
        out = investigate(self.caller(self.server(OPS_QUALITY, self.runtime())))
        self.assertFalse(out["checks"]["component_actuals_consistent"])

    def test_no_ao_item_id_is_hardcoded_and_damage_is_keyed_by_component_code(self):
        # (a) Renumbered item rows give the same results: nothing is keyed by item id.
        for item in self.fake.tables["silver.assembly_order_items"]:
            item["id"] += 1000
        for row in self.fake.tables["silver.assembly_order_damage"]:
            row["assembly_item_id"] += 1000
        out = investigate(self.caller(self.server(OPS_QUALITY, self.runtime())))
        self.assertEqual(out["results"], EXPECTED)
        self.assertEqual(out["damaged_by_code"], EXPECTED_DAMAGE)
        self.assertEqual(sorted(out["damaged"]), ["item59383", "item59384", "item59386"])
        # Each component keeps its own unit and cites its packaging row in the report.
        box = [entry for entry in out["damaged_by_component"] if entry["code"] == "P-01274"]
        self.assertEqual([(entry["unit"], entry["damaged"]) for entry in box],
                         [("item59386", "20")])
        self.assertTrue(box[0]["packaging_citation"].startswith("api://public.packaging/"))

    def test_two_items_sharing_a_packaging_code_keep_both_damage_entries(self):
        # AO-26013C lists P-0147 twice: neither item's damage may overwrite the other's.
        self.fake.tables["silver.assembly_order_items"][2]["packaging_id"] = 162
        out = investigate(self.caller(self.server(OPS_QUALITY, self.runtime())))
        shared = [entry for entry in out["damaged_by_component"] if entry["code"] == "P-0156"]
        self.assertEqual(sorted((entry["unit"], entry["damaged"]) for entry in shared),
                         [("item58383", "3"), ("item58384", "7")])
        self.assertEqual(sorted(out["damaged_by_code"].values()), ["20", "3", "7"])
        self.assertNotIn("P-0156", out["damaged_by_code"])
        self.assertEqual(len(out["damaged_by_code"]), 3)

    def test_component_actual_rounding_half_up_like_postgres(self):
        # Exact value 1.00005 (qty_per_batch 1.00005, produced 1): Postgres round(.,4)
        # gives 1.0001 (half away from zero); half-even would give 1.0000.
        row = {"qty_per_batch": "1.00005", "actual_amount": "1.0001",
               "packaging_id": 162, "bulk_product_id": None}
        self.assertTrue(ao_reference.component_actual_agrees(row, Decimal("1")))
        row["actual_amount"] = "1.0000"
        self.assertFalse(ao_reference.component_actual_agrees(row, Decimal("1")))

    def test_paper_damage_is_per_component_and_has_no_cross_component_sum(self):
        # (b) Paper box damage 11 (FM 20): no sum of boxes, jugs and caps is reported.
        out = investigate(self.caller(self.server(OPS_QUALITY, self.runtime())), PAPER)
        damage = out["paper_damage"]
        self.assertEqual({code: (entry["paper"], entry["fm"], entry["agrees"])
                          for code, entry in damage["by_component"].items()},
                         {"P-01274": ("11", "20", False), "P-0156": ("3", "3", True),
                          "P-0121": ("7", "7", True)})
        self.assertNotIn("paper_sum_mixed_units", damage)
        self.assertNotIn("fm_sum_mixed_units", damage)
        self.assertNotRegex(json.dumps(damage), r"\b(21|30)\b")
        self.assertEqual(damage["status"], "unverified_lead")
        self.assertIsNone(investigate(self.caller(self.server(
            OPS_QUALITY, self.runtime())))["paper_damage"])

    def test_an_unfilled_unit_without_a_matching_sample_count_is_not_called_a_retain(self):
        # (c) Only a sample count equal to produced - filled names the unit a QC retain.
        self.fake.tables["public.app_manufacturing_order"][0]["qty_sample"] = "0"
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        self.assertFalse(out["qc_retain"])
        self.assertFalse(out["checks"]["qc_retain_sample"])
        [cleared] = [f for f in self.case_view(server, out["case_id"])["findings"]["supported"]
                     if "Line clearance" in f["statement"]]
        self.assertNotIn("retain", cleared["statement"])
        self.assertEqual(len(cleared["citations"]), 3)
        # Without a Portal row, the FM AO row's qtysample is the sample count (cited).
        self.setUp()
        self.fake.tables["public.app_manufacturing_order"] = []
        self.fake.catalog["public.assembly_order"]["columns"].append("qtysample")
        self.fake.tables["public.assembly_order"][0]["qtysample"] = "1.0"
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        self.assertTrue(out["qc_retain"])
        [cleared] = [f for f in self.case_view(server, out["case_id"])["findings"]["supported"]
                     if "Line clearance" in f["statement"]]
        self.assertIn("The FM AO row of AO-26005B records the same number of samples (1)",
                      cleared["statement"])
        self.assertEqual(cleared["citations"][-1]["handle"], out["cite"]["ao"])

    def test_a_missing_portal_manufacturing_order_is_a_gap(self):
        self.fake.tables["public.app_manufacturing_order"] = []
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        self.assertFalse(out["mo"]["found"])
        self.assertIsNone(out["cite"]["mo"])
        view = self.case_view(server, out["case_id"])
        [gap] = [gap for gap in view["gaps"]
                 if gap["missing"].startswith("The Portal manufacturing order")]
        self.assertEqual(gap["state"], "unknown")
        # A found row with an empty column records that column as absent.
        self.setUp()
        self.fake.tables["public.app_manufacturing_order"][0]["qty_sample"] = None
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        self.assertEqual(out["mo"]["absent"], ["qty_sample"])

    def test_paper_lead_files_must_carry_provenance_and_digit_free_labels(self):
        source = json.loads(PAPER_LEADS_EXAMPLE.read_text())
        for mutate, code in ((lambda d: d["leads"][0].update(label="P-01274 box"), "digits"),
                             (lambda d: d.update(provenance="pack"), "provenance"),
                             (lambda d: d["notes"]["lateness"].append("blank on 1/5"), "digits"),
                             (lambda d: d.update(status="verified"), "schema"),
                             (lambda d: d["leads"][1].update(id="damage_box"), "unique"),
                             (lambda d: d["leads"][0].update(unit_qty="a quarter"),
                              "unit_qty")):
            with self.subTest(code=code):
                data = copy.deepcopy(source)
                mutate(data)
                path = self.root / "leads.json"
                path.write_text(json.dumps(data), "utf-8")
                with self.assertRaisesRegex(ValueError, code):
                    load_paper_leads(path)

    def test_a_labelled_figure_is_one_number_after_its_code_never_a_compound(self):
        lead = {"kind": "component_damage", "code": "P-01274", "paper": "11"}
        located = ao_reference._figure_in_labelled_field
        for text in ("DAMAGES P-01274 11 RM", "P-01274 = 11", "P-01274: 11", "P-01274 11"):
            with self.subTest(text=text):
                self.assertTrue(located(lead, text))
        # Leading zeros as printed on the sheet (P-0121 = 07) are the same figure.
        self.assertTrue(located({**lead, "code": "P-0121", "paper": "7"}, "P-0121 = 07"))
        for text in ("P-01274 11/20", "P-01274 11-2", "P-01274 11,5", "P-01274 11.5",
                     "P-01274 11 20", "P-01274 110", "P-012745 11", "XP-01274 11",
                     "P-01274 Shipper 11", "P-0121 11 P-01274 20"):
            with self.subTest(text=text):
                self.assertFalse(located(lead, text))
        # A date lead is never "in its labelled field": it is only seen on the page.
        date = {"kind": "date_manufactured", "paper": "2026-01-07"}
        self.assertFalse(located(date, "released 2026-01-07"))
        self.assertTrue(ao_reference._date_on_page(date, "released 2026-01-07"))

    def paper_with(self, *extra):
        data = copy.deepcopy(json.loads(PAPER_LEADS_EXAMPLE.read_text()))
        data["leads"].extend(extra)
        path = self.root / "leads-extra.json"
        path.write_text(json.dumps(data), "utf-8")
        return load_paper_leads(path)

    def test_a_repeated_packaging_code_is_keyed_by_component_and_never_summed(self):
        # AO-26013C prints P-0147 twice (unit qty 1.00 and 0.25). Here two AO items use
        # packaging 152 with different qty_per_batch, each with its own damage row.
        items = self.fake.tables["silver.assembly_order_items"]
        items.append({**items[-1], "id": 58390, "qty_per_batch": "0.25", "quantity": "378.0",
                      "actual_amount": "375"})
        damage = self.fake.tables["silver.assembly_order_damage"]
        damage.append({**damage[0], "id": 5450, "assembly_item_id": 58387, "qty_damaged": "2.0"})
        damage.append({**damage[0], "id": 5451, "assembly_item_id": 58390, "qty_damaged": "5.0"})
        paper = self.paper_with(
            {"id": "label_each", "page": 3, "kind": "component_damage", "packaging_id": "152",
             "code": "P-0147", "unit_qty": "0.5", "label": "case label damage", "paper": "2"},
            {"id": "label_quarter", "page": 3, "kind": "component_damage",
             "packaging_id": "152", "code": "P-0147", "unit_qty": "0.25",
             "label": "quarter case label damage", "paper": "4"})
        out = investigate(self.caller(self.server(OPS_QUALITY, self.runtime())), paper)
        by_component = out["paper_damage"]["by_component"]
        self.assertEqual({key: (entry["paper"], entry["fm"], entry["agrees"])
                          for key, entry in by_component.items() if key.startswith("P-0147")},
                         {"P-0147 (label_each)": ("2", "2", True),
                          "P-0147 (label_quarter)": ("4", "5", False)})
        self.assertEqual(by_component["P-01274"]["fm"], "20")  # unique codes stay plain
        # Without the paper's unit qty the lead matches two AO items: not compared, not summed.
        paper = self.paper_with(
            {"id": "label_any", "page": 3, "kind": "component_damage", "packaging_id": "152",
             "code": "P-0147", "label": "case label damage", "paper": "7"})
        out = investigate(self.caller(self.server(OPS_QUALITY, self.runtime())), paper)
        entry = out["paper_damage"]["by_component"]["P-0147"]
        self.assertEqual((entry["fm"], entry["agrees"]), (None, None))
        self.assertIn("58387, 58390", entry["fm_ambiguous"])
        self.assertNotIn("7", [lead["fm"] for lead in out["paper_leads"]
                               if lead["id"] == "label_any"])

    def test_portal_planned_pieces_corroborate_or_conflict_never_by_default(self):
        catalog = self.fake.catalog["public.assembly_order"]["columns"]
        catalog.append("sizepieces")
        self.fake.tables["public.assembly_order"][0]["sizepieces"] = "1509.0"
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        self.assertEqual(out["planned_fg"]["sources"],
                         {"fm_sizepieces": "1509", "mo_size_pieces": "1509"})
        basis = self.case_view(server, out["case_id"])["findings"]["supported"][1]
        self.assertIn("equals the FM AO row's sizepieces and the Portal manufacturing order's "
                      "size_pieces", basis["statement"])
        # A different planned size is an evidence conflict citing both rows.
        self.setUp()
        self.fake.catalog["public.assembly_order"]["columns"].append("sizepieces")
        self.fake.tables["public.assembly_order"][0]["sizepieces"] = "1600"
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        view = self.case_view(server, out["case_id"])
        [gap] = [gap for gap in view["gaps"] if "planned finished-good figure" in gap["missing"]]
        self.assertEqual(gap["state"], "unknown")
        self.assertEqual([cite["handle"] for cite in gap["citations"]],
                         [out["cite"]["item"], out["cite"]["ao"]])
        self.assertIn("equals the Portal manufacturing order's size_pieces",
                      view["findings"]["supported"][1]["statement"])
        self.assertNotIn("FM AO row's sizepieces", view["findings"]["supported"][1]["statement"])
        # Served but empty everywhere: reported as empty, never as agreement.
        self.setUp()
        self.fake.tables["public.app_manufacturing_order"][0]["size_pieces"] = None
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        self.assertEqual(out["planned_fg"]["sources"],
                         {"fm_sizepieces": "unread", "mo_size_pieces": "empty"})
        self.assertNotIn("The planned figure equals", json.dumps(
            self.case_view(server, out["case_id"])["findings"]))

    def test_the_portal_produced_figure_of_the_order_line_is_read_when_served(self):
        self.fake.catalog["public.order_entry_item"]["columns"] += ["qtyaomfg_c", "qtybcmfg_c"]
        self.fake.tables["public.order_entry_item"][0].update(qtyaomfg_c="1503.0",
                                                              qtybcmfg_c=None)
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        self.assertEqual(out["line_produced"], {
            "state": "read", "values": {"qtyaomfg_c": "1503", "qtybcmfg_c": None},
            "qty_produced": "1503"})
        [gap] = [gap for gap in self.case_view(server, out["case_id"])["gaps"]
                 if gap["missing"].startswith("Whether the rest of order line")]
        self.assertIn("is on the cited line row and is reported", gap["why"])

    def test_the_finished_good_basis_is_chosen_by_rule_never_by_item_id(self):
        items = self.fake.tables["silver.assembly_order_items"]
        del items[1]  # 58383, the jug of the live trace: the cap carries the same FG units
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        self.assertTrue(out["cite"]["item"].startswith(
            "api://silver.assembly_order_items/%7B%22id%22%3A58384%7D"))
        self.assertEqual(out["results"], EXPECTED)
        self.assertEqual(out["chain"]["item_ao"], 10718)

    def test_disagreeing_or_missing_unit_ratio_components_derive_nothing(self):
        for mutate, why in (
                (lambda items: items[2].update(actual_amount="1499.0"), "disagree"),
                (lambda items: [item.update(qty_per_batch="0.5") for item in items[1:4]],
                 "no packaging row")):
            with self.subTest(why=why):
                self.setUp()
                mutate(self.fake.tables["silver.assembly_order_items"])
                server = self.server(OPS_QUALITY, self.runtime())
                out = investigate(self.caller(server))
                self.assertIsNone(out["cite"]["item"])
                self.assertFalse(out["checks"]["fg_basis_found"])
                self.assertEqual(out["results"], {})  # no planned/produced/ordered figures
                self.assertEqual(out["damaged_by_code"], EXPECTED_DAMAGE)
                view = self.case_view(server, out["case_id"])
                [gap] = [gap for gap in view["gaps"]
                         if gap["missing"].startswith("The finished-good quantities")]
                self.assertEqual(gap["state"], "unknown")
                self.assertIn(why, gap["why"])

    def test_lateness_is_judged_on_calendar_dates_and_never_recorded_as_a_fact(self):
        # On time by FM dates: no conflict gap either.
        self.fake.tables["public.order_entry"][0]["datereq"] = "2026-01-10T00:00:00+00:00"
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        self.assertIs(out["fm_late"], False)
        self.assertFalse(out["checks"]["fm_dates_after_required_date"])
        view = self.case_view(server, out["case_id"])
        self.assertEqual(len(view["findings"]["supported"]), 6)
        self.assertNotIn("governing required date", json.dumps(view["gaps"]))

    def test_unreadable_dates_become_an_unknown_gap_never_a_fact(self):
        # US-format dates would compare wrongly as strings ("01/01/2026" < "12/28/2025").
        self.fake.tables["public.order_entry"][0]["datereq"] = "01/01/2026"
        self.fake.tables["public.assembly_order"][0]["datemanuf"] = "12/28/2025"
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        self.assertIsNone(out["fm_late"])
        self.assertFalse(out["checks"]["dates_readable"])
        self.assertFalse(out["checks"]["fm_dates_after_required_date"])
        view = self.case_view(server, out["case_id"])
        [gap] = [gap for gap in view["gaps"] if "by the required date" in gap["missing"]]
        self.assertEqual(gap["state"], "unknown")
        self.assertIn("manufactured, required", gap["why"])
        self.assertNotIn("governing required date", json.dumps(view["gaps"]))

    def assert_no_order_relationship_is_supported(self, server, out):
        self.assertFalse(out["checks"]["chain_consistent"])
        self.assertNotEqual(chain_text(out), EXPECTED_CHAIN)
        self.assertFalse(out["checks"]["fm_dates_after_required_date"])
        self.assertIsNone(out["fm_late"])
        self.assertNotIn("ordered_minus_produced", out["results"])
        view = self.case_view(server, out["case_id"])
        self.assertFalse([finding for finding in view["findings"]["supported"]
                          if "sales order" in finding["statement"]])
        [gap] = [gap for gap in view["gaps"] if gap["missing"].startswith("Which sales order")]
        self.assertEqual(gap["state"], "unknown")
        self.assertNotIn("governing required date", json.dumps(view["gaps"]))
        self.assertNotIn("Whether the rest of order line", json.dumps(view["gaps"]))
        return view

    def test_a_link_to_another_order_line_is_contradicted_never_supported(self):
        # The AO's link row now points at a line of a different sales order.
        self.fake.tables["public.order_entry_item"].append(
            {"id": 9999, "idoe": 352, "idfp": 1534, "idbulk": 1091, "qty": "1920.0",
             "status_c": "Completed"})
        self.fake.tables["public.order_entry_item_assembly"][0]["idoeitem"] = 9999
        server = self.server(OPS_QUALITY, self.runtime())
        # Without --order the line's own sales order (352) is followed: it is not served.
        with self.assertRaises(MissingData) as missing:
            investigate(self.caller(server))
        self.assertEqual(missing.exception.code, "public.order_entry:order_of_line:found_0")
        # Named, OR1000151885 resolves to 351: the chain is visibly broken.
        out = investigate(self.caller(server), order_code=ORDER_CODE)
        self.assertEqual((out["chain"]["link_line"], out["chain"]["line_order"]), (9999, 352))
        view = self.assert_no_order_relationship_is_supported(server, out)
        [finding] = view["findings"]["contradicted"]
        self.assertIn("order line 9999, which belongs to sales order 352", finding["statement"])
        self.assertEqual(len(finding["citations"]), 4)
        # AO-internal quantities are still computed from the AO's own rows.
        self.assertEqual(out["results"]["planned_minus_produced"], "6")

    def test_a_wrongly_resolved_order_is_contradicted_never_supported(self):
        # OR1000151885 resolves to a different order row than the AO's line.
        self.fake.tables["public.order_entry"][0]["id"] = 9999
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server), order_code=ORDER_CODE)
        self.assertEqual((out["chain"]["line_order"], out["chain"]["order_id"]), (351, 9999))
        view = self.assert_no_order_relationship_is_supported(server, out)
        self.assertEqual(len(view["findings"]["contradicted"]), 1)

    def test_items_of_another_ao_are_never_read_as_this_aos(self):
        for item in self.fake.tables["silver.assembly_order_items"]:
            item["assembly_order_id"] = 10719
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        self.assertIsNone(out["cite"]["item"])
        self.assertTrue(out["checks"]["chain_consistent"])
        self.assertEqual(out["results"], {})
        view = self.case_view(server, out["case_id"])
        self.assertIn("no packaging row", json.dumps(view["gaps"]))

    def test_other_aos_on_the_same_order_line_are_related_never_merged(self):
        self.fake.tables["public.order_entry_item_assembly"].append(
            {"id": 1246, "idoeitem": 1140, "idassembly": 10719, "qtyplan_c": "0.0",
             "qtymfg_c": "417.0", "qtyplan": None, "qtymfg": None})
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        self.assertEqual(out["results"], EXPECTED)  # this AO against the line, unchanged
        [related] = out["related"]
        self.assertEqual((related["ao_id"], related["ao_code"], related["link_qtymfg_c"]),
                         (10719, "AO-26006", "417.0"))
        self.assertEqual(out["checks"]["related_aos"], 1)
        view = self.case_view(server, out["case_id"])
        [gap] = [gap for gap in view["gaps"] if gap["missing"].startswith("Whether the rest")]
        self.assertIn("reported as related and not merged: AO-26006.", gap["why"])
        # The related AO's rows are not cited by the case (they are not this AO's evidence).
        self.assertNotIn(related["link_citation"], json.dumps(view))

    def test_an_ao_serving_several_order_lines_uses_the_line_of_this_order(self):
        self.fake.tables["public.order_entry_item"].append(
            {"id": 2000, "idoe": 400, "idfp": 1534, "idbulk": 1091, "qty": "300.0",
             "status_c": "Completed"})
        self.fake.tables["public.order_entry_item_assembly"].insert(0, {
            "id": 1200, "idoeitem": 2000, "idassembly": 10718, "qtyplan_c": "0.0",
            "qtymfg_c": "300.0", "qtyplan": None, "qtymfg": None})
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server), order_code=ORDER_CODE)
        self.assertEqual(chain_text(out), EXPECTED_CHAIN)
        self.assertEqual(out["results"], EXPECTED)
        # Without naming the order, two linked lines are refused, never one picked.
        with self.assertRaises(MissingData) as missing:
            investigate(self.caller(server))
        self.assertEqual(missing.exception.code,
                         "public.order_entry_item_assembly:link_of_ao:found_2")

    def test_hops_follow_the_cited_rows_not_the_trace_ids(self):
        # Same chain under other ids: nothing is read by the historical constants.
        tables = self.fake.tables
        tables["public.order_entry_item_assembly"][0]["idoeitem"] = 7000
        tables["public.order_entry_item"][0].update({"id": 7000, "idoe": 8000})
        tables["public.order_entry"][0]["id"] = 8000
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        self.assertTrue(out["checks"]["chain_consistent"])
        self.assertNotEqual(chain_text(out), EXPECTED_CHAIN)  # differs from the live trace
        self.assertEqual(out["results"], EXPECTED)
        view = self.case_view(server, out["case_id"])
        self.assertIn("order line 7000 to sales order 8000",
                      view["findings"]["supported"][0]["statement"])

    def test_a_case_too_large_for_one_response_reads_back_whole_by_paging(self):
        server = self.server(OPS_QUALITY, self.runtime())
        case_id = investigate(self.caller(server))["case_id"]
        # Reference: the full view under an envelope large enough for any case.
        with patch("operating.mcp_server.MAX_RESULT_BYTES", 10_000_000):
            reference = self.call(server, "plx.case_get", {"case_id": case_id})
        reference = reference["structuredContent"]
        self.assertEqual(reference["view"], "full")
        reference = {key: value for key, value in reference.items()
                     if key not in ("view", "complete")}
        self.assertEqual(self.case_view(server, case_id), reference)
        # Envelopes this case no longer fits but each item still does (a
        # smaller one shrinks an item, which case_view refuses): the
        # client-side read reassembles the case exactly. (14 000: every string but a
        # typed field now leaves as an untrusted_text envelope, so the largest item
        # needs more than the 10 000 it fitted before the output boundary.)
        for envelope in (16_000, 14_000):
            with self.subTest(envelope=envelope), \
                    patch("operating.mcp_server.MAX_RESULT_BYTES", envelope):
                summary = self.ok(server, "plx.case_get", {"case_id": case_id})
                self.assertEqual(summary["view"], "summary")
                self.assertEqual(self.case_view(server, case_id), reference)
                # Pages reference the limitations the summary gives in full.
                page = self.ok(server, "plx.case_get", {"case_id": case_id,
                                                        "section": "findings"})
                self.assertNotIn("limitations", page)
                self.assertEqual(page["limitations_ref"]["count"],
                                 len(summary["limitations"]))
                self.assertEqual(page["limitations_ref"]["sha256"], hashlib.sha256(json.dumps(
                    summary["limitations"], ensure_ascii=False).encode()).hexdigest())

    def add_sibling_ao(self, code="AO-26005C", offset=10_000, line_id=1141):
        """A second AO on the same sales order (its own line), cloned from AO-26005B's rows
        under other ids; no Portal manufacturing order."""
        tables, ao_id = self.fake.tables, 10718 + offset
        tables["public.assembly_order"].append(dict(
            tables["public.assembly_order"][0], id=ao_id, nameamo_c=code))
        tables["public.order_entry_item"].append(dict(
            tables["public.order_entry_item"][0], id=line_id))
        tables["public.order_entry_item_assembly"].append(dict(
            tables["public.order_entry_item_assembly"][0], id=1245 + offset, idoeitem=line_id,
            idassembly=ao_id))
        for name, ao_column in (("silver.assembly_order_items", "assembly_order_id"),
                                ("silver.production_tasks", "mo_id"),
                                ("silver.assembly_order_damage", "assembly_order_id")):
            for row in [dict(row) for row in tables[name] if row[ao_column] == 10718]:
                row.update({"id": row["id"] + offset, ao_column: ao_id})
                if "assembly_item_id" in row:
                    row["assembly_item_id"] += offset
                tables[name].append(row)
        return ao_id

    def test_another_ao_is_investigated_by_its_code_alone(self):
        ao_id = self.add_sibling_ao()
        leads = load_paper_leads(Path(__file__).parents[2] / "examples" /
                                 "ao_26005c_paper_leads.json")
        server = self.server(OPS_QUALITY, self.runtime())
        out = ao_reference.investigate(self.caller(server), leads, ao_code="AO-26005C")
        self.assertEqual(chain_text(out), {**EXPECTED_CHAIN, "link_ao": str(ao_id),
                                           "link_line": "1141", "line_id": "1141",
                                           "ao_id": str(ao_id), "item_ao": str(ao_id)})
        self.assertTrue(out["checks"]["chain_consistent"])
        self.assertEqual(out["results"], EXPECTED)  # the clone's rows carry the same figures
        self.assertEqual(out["damaged_by_code"], EXPECTED_DAMAGE)
        view = self.case_view(server, out["case_id"])
        self.assertEqual(view["roots"], ["AO-26005C", ORDER_CODE])
        self.assertIn("AO-26005C (FM AssemblyOrder 20718) is linked by order line 1141 to sales "
                      "order 351 (OR1000151885).", view["findings"]["supported"][0]["statement"])
        # Paper components named by their printed code meet the FM item of that code.
        by_id = {lead["id"]: lead for lead in out["paper_leads"]}
        self.assertEqual((by_id["damage_box"]["fm"], by_id["damage_box"]["paper"],
                          by_id["damage_box"]["agrees"]), ("20", "9", False))
        self.assertEqual((by_id["damage_jug"]["fm"], by_id["damage_jug"]["agrees"]),
                         ("3", False))
        self.assertEqual(len(by_id["damage_box"]["fm_citations"]), 1)
        self.assertEqual(out["checks"]["paper_damage_conflicts"], 2)
        # Leads of another AO are never applied.
        with self.assertRaises(ValueError):
            ao_reference.investigate(self.caller(server), PAPER, ao_code="AO-26005C")

    def test_an_ao_without_an_order_link_is_a_cited_report_with_a_named_gap(self):
        self.fake.tables["public.order_entry_item_assembly"].clear()
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        self.assertFalse(out["checks"]["order_link_found"])
        self.assertFalse(out["checks"]["chain_consistent"])
        self.assertEqual(out["results"], {"planned_minus_produced": "6",
                                          "produced_minus_filled": "1"})
        self.assertIn("order_link_absent", out["gap_codes"])
        view = self.case_view(server, out["case_id"])
        self.assertEqual(view["roots"], [AO_CODE])
        [gap] = [gap for gap in view["gaps"] if gap["missing"].startswith("Which sales order")]
        self.assertIn("found no order_entry_item_assembly row", gap["why"])
        self.assertFalse([finding for finding in view["findings"]["supported"]
                          if "sales order" in finding["statement"]])

    def test_missing_data_is_named_exactly_and_zero_damage_rows_are_a_gap(self):
        server = self.server(OPS_QUALITY, self.runtime())
        with self.assertRaises(MissingData) as missing:
            ao_reference.investigate(self.caller(server), ao_code="AO-29999Z")
        self.assertEqual(missing.exception.code, "public.assembly_order:code_match:found_0")
        with self.assertRaises(ValueError):
            ao_reference.investigate(self.caller(server), ao_code="26005B")
        self.setUp()
        self.fake.tables["silver.production_tasks"] = [
            row for row in self.fake.tables["silver.production_tasks"]
            if row["task_type"] != "Filling"]
        with self.assertRaises(MissingData) as missing:
            investigate(self.caller(self.server(OPS_QUALITY, self.runtime())))
        self.assertEqual(missing.exception.code, "silver.production_tasks:Filling:found_0")
        self.setUp()
        self.fake.tables["silver.assembly_order_damage"].clear()
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        self.assertEqual((out["damaged"], out["checks"]["damage_rows"]), ({}, 0))
        self.assertIn("damage_rows_absent", out["gap_codes"])
        [gap] = [gap for gap in self.case_view(server, out["case_id"])["gaps"]
                 if "recorded any component damage" in gap["missing"]]
        self.assertIn("not evidence of zero damage", gap["why"])

    def serve_lotno(self, own=None):
        """The FM AO relation as live: it also serves (and the mirror indexes) lotno."""
        self.fake.catalog["public.assembly_order"]["columns"].append("lotno")
        for row in self.fake.tables["public.assembly_order"]:
            row["lotno"] = None
        self.fake.tables["public.assembly_order"][0]["lotno"] = own

    def test_an_ao_code_that_is_another_aos_lot_number_is_never_that_ao(self):
        self.serve_lotno("26005B")
        other = self.add_sibling_ao("AO-26006C")
        [row] = [row for row in self.fake.tables["public.assembly_order"] if row["id"] == other]
        row["lotno"] = "AO-26005C"  # another AO's lot number spelled like the asked code
        server = self.server(OPS_QUALITY, self.runtime())
        with self.assertRaises(MissingData) as missing:
            ao_reference.investigate(self.caller(server), ao_code="AO-26005C")
        self.assertEqual(missing.exception.code, "public.assembly_order:code_match:found_0")
        # A lot number is no AO identifier: plx.find names no candidate for it either.
        found = self.caller(server)("plx.find", {"query": "AO-26005C"})
        self.assertEqual(found["candidates"], [])

    def test_plx_find_resolution_cites_the_same_rows_as_the_retired_mirror_resolve(self):
        # Pinned from the run before the identity layer replaced mirror.resolve: the
        # run id after "@" is per runtime and left out, every row and its handle stays.
        golden = [
            "api://public.app_manufacturing_order/%7B%22id%22%3A%22mo-10718%22%7D@RUN-1",
            "api://public.assembly_order/%7B%22id%22%3A10718%7D@RUN-4",
            "api://public.order_entry/%7B%22id%22%3A351%7D@RUN-5",
            "api://public.order_entry_item/%7B%22id%22%3A1140%7D@RUN-8",
            "api://public.order_entry_item_assembly/%7B%22id%22%3A1245%7D@RUN-7",
            "api://silver.assembly_order_damage/%7B%22id%22%3A5444%7D@RUN-11",
            "api://silver.assembly_order_damage/%7B%22id%22%3A5445%7D@RUN-11",
            "api://silver.assembly_order_damage/%7B%22id%22%3A5446%7D@RUN-11",
            "api://silver.assembly_order_items/%7B%22id%22%3A58383%7D@RUN-9",
            "api://silver.production_tasks/%7B%22id%22%3A14354%7D@RUN-10",
            "api://silver.production_tasks/%7B%22id%22%3A14355%7D@RUN-10"]
        for kwargs in ({}, {"order_code": ORDER_CODE}):
            self.setUp()
            server = self.server(OPS_QUALITY, self.runtime())
            out = investigate(self.caller(server), **kwargs)
            view = json.dumps(self.case_view(server, out["case_id"]))
            cited = sorted(set(re.findall(r'api://[^"\\\s]+', view)))
            self.assertEqual([re.sub(r"@[0-9a-f]{16}-", "@RUN-", c) for c in cited], golden)

    def test_case_host_tools_close_releases_the_reader_and_the_working_directory(self):
        import sqlite3
        from operating.ontology_host import CaseHostTools
        runtime = self.runtime()
        actor = Actor("plx", OPS_QUALITY)
        host = CaseHostTools(runtime, actor)
        host.call("plx.find", {"query": AO_CODE}, runtime.employee_gate(actor))
        reader, work = host._reader, Path(host._temp.name)
        self.assertTrue(work.exists())
        host.close()
        with self.assertRaises(sqlite3.ProgrammingError):  # the reader's connection is shut
            reader.conn.execute("SELECT 1")
        self.assertFalse(work.exists())
        self.assertIsNone(host._tools)
        host.call("plx.find", {"query": AO_CODE}, runtime.employee_gate(actor))  # rebuilt
        host.close()

    def test_find_pages_to_the_end_and_an_incomplete_answer_is_never_absence(self):
        pages = {None: {"candidates": [{"n": 1}], "completeness": {"complete": False,
                                                                   "next_cursor": "c1"}},
                 "c1": {"candidates": [{"n": 2}], "completeness": {"complete": True,
                                                                   "next_cursor": None}}}
        seen = []

        def call(name, arguments):
            seen.append(arguments)
            return pages[arguments.get("cursor")]
        self.assertEqual(ao_reference._find(call, AO_CODE, "public.assembly_order"),
                         [{"n": 1}, {"n": 2}])
        self.assertEqual(seen, [{"query": AO_CODE}, {"query": AO_CODE, "cursor": "c1"}])
        capped = {"candidates": [], "completeness": {"complete": False, "next_cursor": None,
                                                     "reason": "scan_cap"}}
        with self.assertRaises(MissingData) as missing:
            ao_reference._find(lambda name, arguments: capped, AO_CODE,
                               "public.assembly_order")
        self.assertEqual(missing.exception.code, "public.assembly_order:find_incomplete:found_0")

    def test_a_normalised_only_match_is_a_lead_never_the_record(self):
        server = self.server(OPS_QUALITY, self.runtime())
        candidates = self.caller(server)("plx.find", {"query": "ao-26005b"})["candidates"]
        with self.assertRaises(MissingData) as missing:
            ao_reference._found(candidates, "ao-26005b", ao_reference.AO_CODE_MATCH)
        self.assertEqual(missing.exception.code,
                         "public.assembly_order:code_match_not_exact:found_1")
        self.assertEqual(len(ao_reference._found(candidates, "AO-26005B",
                                                 ao_reference.AO_CODE_MATCH)), 1)

    def test_an_ambiguous_code_is_a_named_failure_never_a_picked_winner(self):
        self.fake.tables["public.assembly_order"].append(dict(
            self.fake.tables["public.assembly_order"][0], id=20718))  # same nameamo_c twice
        server = self.server(OPS_QUALITY, self.runtime())
        with self.assertRaises(MissingData) as missing:
            investigate(self.caller(server))
        self.assertEqual(missing.exception.code, "public.assembly_order:code_match:found_2")

    def test_an_ao_whose_lot_number_is_its_code_is_resolved_once(self):
        self.serve_lotno("AO-26005B")  # the same row matches on nameamo_c and on lotno
        out = investigate(self.caller(self.server(OPS_QUALITY, self.runtime())))
        self.assert_live_trace(out)

    def test_a_portal_mo_matched_only_by_lot_number_is_never_the_aos(self):
        mo = self.fake.tables["public.app_manufacturing_order"][0]
        mo.update(mo_code="AO-99999X", lot_number="AO-26005B", qty_sample="5",
                  qty_damaged="40")
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server))
        self.assertFalse(out["checks"]["mo_found"])
        self.assertIsNone(out["cite"]["mo"])
        self.assertIn("mo_not_found", out["gap_codes"])
        self.assertFalse(out["checks"]["qc_retain_sample"])  # FM's qtysample is not served
        view = self.case_view(server, out["case_id"])
        self.assertNotIn("qty_damaged 40", json.dumps(view))
        self.assertNotIn("app_manufacturing_order/", json.dumps(view["findings"]))

    def test_order_matches_only_the_order_number_and_a_missing_link_is_a_gap(self):
        server = self.server(OPS_QUALITY, self.runtime())
        # The customer PO of the same order is not its order number.
        with self.assertRaises(MissingData) as missing:
            investigate(self.caller(server), order_code="PO12172025")
        self.assertEqual(missing.exception.code, "public.order_entry:code_match:found_0")
        # No link with --order: the same cited report and gap as without it.
        self.setUp()
        self.fake.tables["public.order_entry_item_assembly"].clear()
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server), order_code=ORDER_CODE)
        self.assertFalse(out["checks"]["order_link_found"])
        self.assertIn("order_link_absent", out["gap_codes"])
        self.assertEqual(self.case_view(server, out["case_id"])["roots"], [AO_CODE, ORDER_CODE])

    def test_ao_codes_are_case_insensitive_and_malformed_ones_refused(self):
        server = self.server(OPS_QUALITY, self.runtime())
        out = ao_reference.investigate(self.caller(server), PAPER, ao_code=" ao-26005b ")
        self.assert_live_trace(out)
        view = self.case_view(server, out["case_id"])
        self.assertEqual(view["roots"][0], AO_CODE)  # as FM stores it
        for bad in ("AO-26005", "AO-2600B", "26005B", "AO_26005B", None):
            with self.subTest(code=bad), self.assertRaises(ValueError):
                ao_reference.investigate(self.caller(server), ao_code=bad)

    @unittest.skipUnless(local_ocr_available(), "no local OCR backend installed")
    def test_scan_is_attached_ocred_cited_and_survives_a_new_session(self):
        runtime = self.runtime()
        server = self.server(OPS_QUALITY, runtime)
        out = investigate(self.caller(server))
        case_id = out["case_id"]
        quality_inbox = runtime.originals.inbox / "staff:quality"
        quality_inbox.mkdir()
        shutil.copyfile(FIXTURE, quality_inbox / SCAN)
        # An operations-only member of the case can neither attach the quality scan
        # nor learn that it exists, even when naming its principal directory.
        ops_server = self.server(OPS, runtime)
        for arguments, code in (({}, "inbox_file_not_found"),
                                ({"inbox_principal": "staff:quality"},
                                 "inbox_principal_not_held")):
            refused = self.call(ops_server, "plx.case_attach_original",
                                {"case_id": case_id, "inbox_name": SCAN, **arguments})
            self.assertTrue(refused["isError"])
            self.assertIn(code, refused["content"][0]["text"])
        self.assertTrue((quality_inbox / SCAN).exists())

        attached = self.ok(server, "plx.case_attach_original",
                           {"case_id": case_id, "inbox_name": SCAN})
        original = attached["original"]
        self.assertEqual(original["audience"], ["staff:quality"])
        self.assertFalse((quality_inbox / SCAN).exists())  # consumed: attachable once
        self.assertEqual(attached["business_system_writes"], 0)
        self.assertEqual(original["extraction"]["status"], "extracted")
        self.assertEqual(original["extraction"]["ocr_pages"], [1])
        recorded = self.case_view(server, case_id)
        self.assertIn("machine-read", recorded["findings"]["supported"][-1]["statement"])
        [page] = original["page_citations"]
        # The OCR text is read the way a client reads it: through the MCP tool.
        page_view = self.ok(server, "plx.source_citation", {"citation": page})
        self.assertEqual((page_view["ok"], page_view["text_source"], page_view["sha256"],
                          page_view["text_complete"]),
                         (True, "ocr", original["sha256"], True))
        self.assertIn("machine-read", page_view["note"])
        self.assertEqual(page_view["text"], runtime.resolve_citation(
            page, Actor("plx", OPS_QUALITY))["text"])
        # An operations-only member of the case cannot read the quality scan's text.
        self.assertEqual(self.ok(ops_server, "plx.source_citation", {"citation": page}),
                         {"ok": False, "error": "original_not_visible", "citation": page})
        # The OCR numbers agree with the cited rows (read from the rows, not constants).
        rows = {name: self.ok(server, "plx.source_citation", {"citation": handle})["row"]
                for name, handle in (("item", out["cite"]["item"]),
                                     ("filling", out["cite"]["filling"]))}
        ocr = {label: int(value) for label, value in re.findall(
            r"(PLANNED|MANUFACTURED|FILLED|DAMAGED PACKS) (\d+)", page_view["text"])}
        self.assertEqual(ocr["PLANNED"], int(float(rows["item"]["quantity"])))
        self.assertEqual(ocr["MANUFACTURED"], int(float(rows["item"]["actual_amount"])))
        self.assertEqual(ocr["FILLED"], int(float(rows["filling"]["qty"])))
        # The synthetic scan prints one mixed "pack" total; the case keeps damage per
        # component and never records such a total.
        self.assertNotIn(str(ocr["DAMAGED PACKS"]), out["damaged"].values())
        self.ok(server, "plx.case_add_finding", {
            "case_id": case_id, "kind": "fact", "status": "supported",
            "statement": "The scan's OCR text (machine-read, to be confirmed by Quality) "
                         "reads PLANNED 1509, MANUFACTURED 1503 and FILLED 1502, matching the "
                         "cited AO item and filling rows.",
            "citations": [page, out["cite"]["item"], out["cite"]["filling"]]})
        # A new session: a fresh runtime and server on the same state directory.
        runtime.close()
        later_runtime = self.runtime()
        later = self.server(OPS_QUALITY, later_runtime)
        view = self.case_view(later, case_id)
        self.assertTrue(view["integrity"]["chain_ok"])
        self.assertEqual(len(view["findings"]["supported"]), 8)
        self.assertEqual(view["findings"]["withheld"], [])
        self.assertEqual(len(view["calculations"]), 6)
        cites = {cite["handle"]: cite for finding in view["findings"]["supported"]
                 for cite in finding["citations"]}
        self.assertEqual(cites[page]["status"], "resolved")
        self.assertEqual(cites[page]["mirror"]["text_source"], "ocr")
        self.assertIn("machine-read", cites[page]["mirror"]["note"])
        self.assertEqual(cites[original["citation"]]["status"], "resolved")
        # An operations-only reader of the same case sees no scan content and no
        # silver.* quantities (neither grant is theirs).
        ops_view = self.case_view(self.server(OPS, later_runtime), case_id)
        text = json.dumps(ops_view)
        self.assertNotIn("PLANNED 1509", text)
        self.assertNotIn(original["sha256"], text)
        # The order link and the Portal manufacturing order (public.* is theirs).
        self.assertEqual(len(ops_view["findings"]["supported"]), 2)
        # Tampering with the stored scan withdraws every finding citing it.
        blob = later_runtime.originals.store / original["sha256"]
        blob.write_bytes(blob.read_bytes() + b"\n%tampered")
        tampered = self.case_view(later, case_id)
        self.assertEqual(len(tampered["findings"]["supported"]), 6)
        self.assertNotIn("PLANNED 1509", json.dumps(tampered))


if __name__ == "__main__":
    unittest.main()
