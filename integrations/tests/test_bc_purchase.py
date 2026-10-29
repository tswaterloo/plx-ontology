"""BC purchase reader and PO case view (injected transport, recorded BC API v2.0 shapes).

No network and no credentials: every request goes to the in-process ``FakeSender``
(``test_live_source_adapters``). Responses follow the API v2.0 JSON shape (an
``@odata.context``, ``value`` rows with ``@odata.etag``, JSON numbers for money and
quantities, ``0001-01-01`` for an empty date) and honour ``$filter`` (one equality),
``$select`` (projection), ``$top`` and ``@odata.nextLink`` paging. Evidence label:
``injected-transport``; the adapter is live-unverified until
``devloop/live-checks/bc.purchase_reader.json`` has a passing result.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from integrations import bc_purchase, bc_read, live_source_grammar as grammar
from integrations.bc_purchase import read_purchase_evidence
from integrations.bc_read import BusinessCentralReader
from integrations.live_source_guard import LiveSourceAudience
from integrations.tests.test_live_source_adapters import (CLIENT_SECRET, ACCESS_TOKEN, COMPANY,
                                                          ENV, TENANT_ID, FakeSender, j)
from ontology import identifiers, store_ddl
from ontology.core import Actor
from ontology.json_contract import validate
from ontology.project import CapturedRecord, build_store, object_id_of
from ontology.schema import load_schema
from operating import ontology_tools as ot
from operating.bc_purchase_case import build_purchase_case, portal_po_link
from operating.ontology_tools_process import ProcessTools


BASE = f"/v2.0/{TENANT_ID}/Production/api/v2.0"
HOST = "https://api.businesscentral.dynamics.com"
COMPANY2 = "0f0e0d0c-0b0a-0908-0706-0504030201ff"
OTHER = "0f0e0d0c-0b0a-0908-0706-0504030203ff"
C1 = f"{BASE}/companies({COMPANY})"
C2 = f"{BASE}/companies({COMPANY2})"
BUYER = Actor("plx", frozenset({"staff:buyer"}), "restricted")
RECEIVER = Actor("plx", frozenset({"staff:receiver"}), "restricted")
PO = "PO100250"
# every company of the fixtures, pinned: the probe reads pinned companies only
PINS = {"production": {COMPANY: "manufacturing", COMPANY2: "manufacturing", OTHER: "manufacturing",
                         "0f0e0d0c-0b0a-0908-0706-0504030202ff": "manufacturing"}}


def gid(n: int) -> str:
    return f"cccccccc-0000-0000-0000-{n:012d}"


def header(n: int, **fields: Any) -> dict:
    return {"@odata.etag": f'W/"JzE5OzEyMzQ1{n}Jw=="', "id": gid(n),
            "lastModifiedDateTime": "2026-09-30T14:02:11.103Z", **fields}


def line(n: int, document: int, **fields: Any) -> dict:
    return {"@odata.etag": f'W/"JzE5O{n}Jw=="', "id": gid(500 + n), "documentId": gid(document),
            **fields}


def collection(rows: list[dict], *, page: int = 0) -> Callable[[str], Any]:
    """A BC collection: $filter (one equality, BC compares Code fields ignoring case),
    $select projection, $top, and server paging of ``page`` rows by $skiptoken."""
    def route(url: str):
        query = parse_qs(urlsplit(url).query)
        result = rows
        if "$filter" in query:
            field, _, literal = query["$filter"][0].partition(" eq ")
            value = literal.strip("'")
            result = [r for r in result if str(r.get(field, "")).casefold() == value.casefold()]
        if "$top" in query:
            result = result[:int(query["$top"][0])]
        if "$select" in query:
            names = set(query["$select"][0].split(","))
            result = [{k: v for k, v in r.items() if k in names or k.startswith("@")}
                      for r in result]
        start = int(query.get("$skiptoken", ["0"])[0])
        body: dict[str, Any] = {"@odata.context": f"{HOST}{BASE}/$metadata#x"}
        if page and start + page < len(result):
            body["value"] = result[start:start + page]
            body["@odata.nextLink"] = (url.split("&%24skiptoken=")[0]
                                       + f"&%24skiptoken={start + page}")
        else:
            body["value"] = result[start:start + page] if page else result
        return 200, {}, json.dumps(body).encode()
    return route


class Fixture:
    """Company 1 holds PO100250: two item lines and a comment, two posted receipts
    (partial: 1,000 + 800 of 3,000 for P-0821; 0 of 500 for R-0433) and one
    invoice whose orderNumber is blank (found by vendor invoice No. INV-9) with a
    freight G/L line."""

    def __init__(self) -> None:
        self.order = header(1, number=PO, orderDate="2026-09-01", postingDate="2026-09-01",
                            requestedReceiptDate="0001-01-01", vendorId=gid(90),
                            vendorNumber="V0042", vendorName="Bottle Supplier Co",
                            currencyCode="USD", pricesIncludeTax=False, status="Open",
                            fullyReceived=False, purchaser="",
                            totalAmountExcludingTax=2380.0, totalTaxAmount=0,
                            totalAmountIncludingTax=2380.0, shipToName="not selected")
        self.order_lines = [
            line(1, 1, sequence=10000, itemId=gid(70), lineType="Item",
                 lineObjectNumber="P-0821", description="Bottle 250ml", unitOfMeasureCode="PCS",
                 quantity=3000, directUnitCost=0.51, discountPercent=0,
                 amountExcludingTax=1530.0, expectedReceiptDate="2026-10-15",
                 receivedQuantity=1800, invoicedQuantity=1800),
            line(2, 1, sequence=20000, itemId=gid(71), lineType="Item",
                 lineObjectNumber="R-0433", description="Resin", unitOfMeasureCode="KG",
                 quantity=500, directUnitCost=1.70, discountPercent=0, amountExcludingTax=850.0,
                 expectedReceiptDate="0001-01-01", receivedQuantity=0, invoicedQuantity=0),
            line(3, 1, sequence=30000, lineType="Comment", lineObjectNumber="",
                 description="Ship by air", unitOfMeasureCode="", quantity=0,
                 directUnitCost=0, discountPercent=0, amountExcludingTax=0,
                 expectedReceiptDate="0001-01-01", receivedQuantity=0, invoicedQuantity=0)]
        self.receipts = [
            header(2, number="PRC-1", orderNumber=PO, postingDate="2026-09-20",
                   vendorNumber="V0042", vendorName="Bottle Supplier Co", currencyCode="USD"),
            header(3, number="PRC-2", orderNumber=PO, postingDate="2026-09-27",
                   vendorNumber="V0042", vendorName="Bottle Supplier Co", currencyCode="USD")]
        self.receipt_lines = {
            2: [line(10, 2, sequence=10000, lineType="Item", lineObjectNumber="P-0821",
                     description="Bottle 250ml", unitOfMeasureCode="PCS", quantity=1000,
                     unitCost=0.51, expectedReceiptDate="2026-10-15"),
                line(11, 2, sequence=20000, lineType="Item", lineObjectNumber="R-0433",
                     description="Resin", unitOfMeasureCode="KG", quantity=0, unitCost=1.7,
                     expectedReceiptDate="0001-01-01")],
            3: [line(12, 3, sequence=10000, lineType="Item", lineObjectNumber="P-0821",
                     description="Bottle 250ml", unitOfMeasureCode="PCS", quantity=800,
                     unitCost=0.51, expectedReceiptDate="2026-10-15")]}
        self.invoices = [header(4, number="PPI-1007", vendorInvoiceNumber="INV-9", orderId=None,
                                orderNumber="", invoiceDate="2026-09-28",
                                postingDate="2026-09-28", dueDate="2026-10-28",
                                vendorId=gid(90), vendorNumber="V0042",
                                vendorName="Bottle Supplier Co", currencyCode="USD",
                                pricesIncludeTax=False, status="Open",
                                totalAmountExcludingTax=1074.0, totalTaxAmount=0,
                                totalAmountIncludingTax=1074.0)]
        self.invoice_lines = {4: [
            line(20, 4, sequence=10000, itemId=gid(70), lineType="Item",
                 lineObjectNumber="P-0821", description="Bottle 250ml",
                 unitOfMeasureCode="PCS", quantity=1800, unitCost=0.53, discountPercent=0,
                 amountExcludingTax=954.0),
            line(21, 4, sequence=20000, lineType="Account", lineObjectNumber="41130",
                 description="Freight", unitOfMeasureCode="", quantity=1, unitCost=120,
                 discountPercent=0, amountExcludingTax=120.0)]}
        self.items = [header(70, number="P-0821", displayName="Bottle 250ml", type="Inventory",
                             baseUnitOfMeasureCode="PCS", blocked=False),
                      header(71, number="R-0433", displayName="Resin", type="Inventory",
                             baseUnitOfMeasureCode="KG", blocked=False)]
        self.routes: dict[str, Any] = {
            f"{BASE}/companies": j(200, {"value": [
                {"id": COMPANY, "name": "My Company", "@odata.etag": 'W/"1"'}]}),
            f"{C1}/purchaseOrders": collection([self.order]),
            f"{C1}/purchaseOrders({gid(1)})/purchaseOrderLines":
                collection(self.order_lines, page=2),
            f"{C1}/purchaseReceipts": collection(self.receipts),
            f"{C1}/purchaseInvoices": collection(self.invoices),
            f"{C1}/items": collection(self.items),
        }
        for number, rows in self.receipt_lines.items():
            self.routes[f"{C1}/purchaseReceipts({gid(number)})/purchaseReceiptLines"] = \
                collection(rows)
        for number, rows in self.invoice_lines.items():
            self.routes[f"{C1}/purchaseInvoices({gid(number)})/purchaseInvoiceLines"] = \
                collection(rows)
        self.sender = FakeSender(self.routes)
        self.audience = LiveSourceAudience({
            "bc:production:companies": ["staff:buyer", "staff:receiver"],
            f"bc:production:{COMPANY}:*": ["staff:buyer"],
            f"bc:production:{COMPANY2}:*": ["staff:buyer"],
            f"bc:production:{COMPANY}:purchaseorders": ["staff:buyer", "staff:receiver"],
            f"bc:production:{COMPANY}:purchaseorderlines": ["staff:buyer", "staff:receiver"],
            f"bc:production:{COMPANY}:purchasereceipts": ["staff:buyer", "staff:receiver"],
            f"bc:production:{COMPANY}:purchaseinvoices": ["staff:buyer", "staff:receiver"]}, tenant="plx")
        api, _transport, tenant, environment = bc_read.build_bc(ENV, sender=self.sender)
        self.reader = BusinessCentralReader(api, self.audience, tenant_id=tenant,
                                            environment=environment)

    def evidence(self, actor: Actor = BUYER, **kwargs: Any) -> dict:
        kwargs.setdefault("vendor_invoice_numbers", ["INV-9"])
        return read_purchase_evidence(self.reader, actor, PO, **kwargs)

    def case(self, actor: Actor = BUYER, portal_po: dict | None = None, **kwargs: Any) -> dict:
        return build_purchase_case(self.evidence(actor, **kwargs),
                                   evidence_label="injected-transport", portal_po=portal_po)

    def api_calls(self) -> list[tuple[str, str]]:
        return [(c[0], c[1]) for c in self.sender.api_calls()]


def codes(case: dict, kind: str) -> list[str]:
    return [issue["code"] for issue in case[kind]]


def only_line(order: dict, item: str) -> dict:
    [found] = [ln for ln in order["lines"] if ln["item_number"] == item]
    return found


class PurchaseEvidenceTests(unittest.TestCase):
    def test_partial_receipt_keeps_ordered_received_invoiced_distinct_and_cited(self):
        f = Fixture()
        case = f.case()
        self.assertEqual(case["status"], "single_order")
        self.assertTrue(case["complete"])
        self.assertEqual(case["evidence_label"], "injected-transport")
        self.assertEqual(case["adapter_status"], "live-unverified")
        [order] = case["orders"]
        self.assertEqual(order["number"], PO)
        self.assertTrue(order["citation"].startswith(f"bc://production/{COMPANY}/purchaseOrders/"))
        self.assertEqual(order["currency_code"], "USD")
        self.assertIsNone(order["dates"]["requestedReceiptDate"])  # 0001-01-01 is absent
        self.assertEqual(order["dates"]["orderDate"], "2026-09-01")
        bottle = only_line(order, "P-0821")
        self.assertEqual((bottle["ordered"]["role"], bottle["ordered"]["quantity_type"],
                          bottle["ordered"]["field"], bottle["ordered"]["value"]),
                         ("ordered", "ordered", "quantity", "3000"))
        self.assertEqual((bottle["received_per_order_line"]["quantity_type"],
                          bottle["received_per_order_line"]["field"],
                          bottle["received_per_order_line"]["value"]),
                         ("received", "receivedQuantity", "1800"))
        self.assertEqual(bottle["received_per_receipts"]["value"], "1800")
        self.assertEqual(len(bottle["received_per_receipts"]["citations"]), 2)
        self.assertTrue(all(c.startswith(f"bc://production/{COMPANY}/purchaseReceiptLines/")
                            for c in bottle["received_per_receipts"]["citations"]))
        self.assertEqual(bottle["receipt_status"],
                         {"per_order_line": "partially_received",
                          "per_receipts": "partially_received", "comparison_only": True})
        self.assertNotIn("outstanding", json.dumps(bottle))  # no cross-role arithmetic
        self.assertEqual(bottle["invoiced_per_order_line"]["quantity_type"], None)
        self.assertEqual(bottle["invoiced_per_invoices"]["value"], "1800")
        # unit costs: order directUnitCost, invoice line unitCost (not directUnitCost)
        self.assertEqual((bottle["unit_cost"]["order"]["field"],
                          bottle["unit_cost"]["order"]["value"],
                          bottle["unit_cost"]["order"]["currency_code"]),
                         ("directUnitCost", "0.51", "USD"))
        self.assertEqual([c["unit_cost"] for c in bottle["unit_cost"]["invoices"]], ["0.53"])
        self.assertIn("BC_INVOICE_UNIT_COST_DIFFERS", codes(case, "conflicts"))
        resin = only_line(order, "R-0433")
        self.assertEqual(resin["receipt_status"]["per_order_line"], "none_received")
        self.assertEqual(resin["received_per_receipts"]["value"], "0")
        self.assertIsNone(resin["expected_receipt_date"])
        comment = [ln for ln in order["lines"] if ln["line_type"] == "Comment"][0]
        self.assertEqual(comment["received_per_receipts"]["status"], "not_an_item_line")
        # the freight G/L line is a charge, never item cost
        [charge] = order["invoice_charges"]
        self.assertEqual((charge["account_or_charge_number"], charge["amount_excluding_tax"]),
                         ("41130", "120.0"))
        self.assertEqual(case["gaps"], [])
        self.assertFalse(case["identity_asserted"])

    def test_found_by_vendor_invoice_number_when_order_number_is_blank(self):
        f = Fixture()
        evidence = f.evidence()
        [company] = evidence["companies"]
        [invoice] = company["invoices"]
        self.assertEqual([m["field"] for m in invoice["matched_by"]], ["vendorInvoiceNumber"])
        statuses = {(r["entity_set"], r["field"]): r["status"] for r in company["reads"]}
        self.assertEqual(statuses[("purchaseInvoices", "orderNumber")], "none_found")
        self.assertEqual(statuses[("purchaseInvoices", "vendorInvoiceNumber")],
                         "single_candidate")
        without = build_purchase_case(f.evidence(vendor_invoice_numbers=[]),
                                      evidence_label="injected-transport")
        self.assertIn("BC_INVOICES_NOT_FOUND", codes(without, "gaps"))
        bottle = only_line(without["orders"][0], "P-0821")
        self.assertEqual(bottle["invoiced_per_invoices"]["status"], "no_document_found")
        self.assertIsNone(bottle["invoiced_per_invoices"]["value"])

    def test_paging_selects_and_get_only(self):
        f = Fixture()
        evidence = f.evidence()
        order = evidence["companies"][0]["orders"][0]
        self.assertEqual(order["lines"]["completeness"]["pages_read"], 2)
        self.assertEqual(len(order["lines"]["items"]), 3)
        self.assertNotIn("shipToName", order["header"])  # $select keeps addresses out
        calls = f.api_calls()
        self.assertTrue(calls)
        self.assertTrue(all(method == "GET" for method, _ in calls))
        receipt_query = [parse_qs(urlsplit(url).query) for _m, url in calls
                         if urlsplit(url).path.endswith("/purchaseReceipts")][0]
        self.assertEqual(receipt_query["$filter"], [f"orderNumber eq '{PO}'"])
        self.assertIn("orderNumber", receipt_query["$select"][0].split(","))
        self.assertTrue(any("%24skiptoken=2" in url for _m, url in calls))
        blob = json.dumps([c[2] for c in f.sender.api_calls()]) + json.dumps(evidence)
        self.assertNotIn(CLIENT_SECRET, blob)
        self.assertNotIn(ACCESS_TOKEN, json.dumps(evidence))

    def test_missing_receipts_are_a_gap_not_zero(self):
        f = Fixture()
        f.routes[f"{C1}/purchaseReceipts"] = collection([])
        case = f.case()
        bottle = only_line(case["orders"][0], "P-0821")
        self.assertEqual(bottle["received_per_receipts"]["status"], "no_document_found")
        self.assertIsNone(bottle["received_per_receipts"]["value"])
        self.assertEqual(bottle["receipt_status"]["per_receipts"], "unknown")
        self.assertIn("BC_RECEIPTS_NOT_FOUND", codes(case, "gaps"))
        self.assertIn("BC_RECEIVED_WITHOUT_RECEIPT_DOCUMENT", codes(case, "conflicts"))

    def test_absent_received_field_is_unknown_not_zero(self):
        f = Fixture()
        for row in f.order_lines:
            row.pop("receivedQuantity")
        bottle = only_line(f.case()["orders"][0], "P-0821")
        self.assertEqual(bottle["received_per_order_line"]["status"], "absent")
        self.assertIsNone(bottle["received_per_order_line"]["value"])
        self.assertEqual(bottle["receipt_status"]["per_order_line"], "unknown")

    def test_receipt_read_failure_is_incomplete_never_a_partial_figure(self):
        f = Fixture()
        f.routes[f"{C1}/purchaseReceipts"] = j(400, {"error": {
            "code": "BadRequest_PropertyNotFound", "message": "orderNumber"}})
        case = f.case()
        self.assertFalse(case["complete"])
        self.assertEqual(case["status"], "single_order_incomplete")
        bottle = only_line(case["orders"][0], "P-0821")
        self.assertEqual(bottle["received_per_receipts"]["status"], "incomplete_read")
        self.assertIsNone(bottle["received_per_receipts"]["value"])
        self.assertIn("BC_RECEIPTS_READ_INCOMPLETE", codes(case, "gaps"))
        self.assertIn("BC_READ_INCOMPLETE", codes(case, "gaps"))
        self.assertNotIn("BC_RECEIPTS_NOT_FOUND", codes(case, "gaps"))

    def test_truncated_receipt_lines_never_sum_partially(self):
        f = Fixture()
        f.routes[f"{C1}/purchaseReceipts({gid(2)})/purchaseReceiptLines"] = \
            collection(f.receipt_lines[2], page=1)
        evidence = read_purchase_evidence(f.reader, BUYER, PO, vendor_invoice_numbers=["INV-9"],
                                          line_max_pages=1)
        case = build_purchase_case(evidence, evidence_label="injected-transport")
        self.assertFalse(case["complete"])
        bottle = only_line(case["orders"][0], "P-0821")
        self.assertEqual(bottle["received_per_receipts"]["status"], "incomplete_read")
        self.assertIsNone(bottle["received_per_receipts"]["value"])
        self.assertIn("BC_ORDER_LINES_INCOMPLETE", codes(case, "gaps"))

    def test_currency_blank_and_differing(self):
        f = Fixture()
        f.receipts[0]["currencyCode"] = ""
        f.invoices[0]["currencyCode"] = "CAD"
        case = f.case()
        self.assertIn("BC_CURRENCY_BLANK", codes(case, "gaps"))
        self.assertIn("BC_CURRENCY_DIFFERS", codes(case, "conflicts"))
        # a cost in another currency is not compared with the order's
        self.assertNotIn("BC_INVOICE_UNIT_COST_DIFFERS", codes(case, "conflicts"))
        [receipt] = [r for r in case["receipts"] if r["number"] == "PRC-1"]
        self.assertIsNone(receipt["currency_code"])

    def test_order_deleted_but_documents_exist(self):
        f = Fixture()
        f.routes[f"{C1}/purchaseOrders"] = collection([])
        case = f.case()
        self.assertEqual(case["status"], "documents_without_order")
        self.assertIn("BC_ORDER_NOT_FOUND_DOCUMENTS_EXIST", codes(case, "gaps"))
        self.assertEqual(len(case["receipts"]), 2)
        self.assertEqual(case["orders"], [])

    def test_nothing_found_is_not_proof_of_no_purchase(self):
        f = Fixture()
        for path in ("purchaseOrders", "purchaseReceipts", "purchaseInvoices"):
            f.routes[f"{C1}/{path}"] = collection([])
        case = f.case()
        self.assertEqual(case["status"], "no_bc_documents")
        self.assertIn("BC_ORDER_NOT_FOUND", codes(case, "gaps"))

    def test_same_number_in_two_companies_is_ambiguous(self):
        f = Fixture()
        f.routes[f"{BASE}/companies"] = j(200, {"value": [
            {"id": COMPANY, "name": "My Company", "@odata.etag": 'W/"1"'},
            {"id": COMPANY2, "name": "PLX Distribution", "@odata.etag": 'W/"2"'}]})
        other = header(40, number=PO, vendorNumber="V9", currencyCode="CAD")
        f.routes[f"{C2}/purchaseOrders"] = collection([other])
        f.routes[f"{C2}/purchaseOrders({gid(40)})/purchaseOrderLines"] = collection([])
        f.routes[f"{C2}/purchaseReceipts"] = collection([])
        f.routes[f"{C2}/purchaseInvoices"] = collection([])
        case = f.case()
        self.assertEqual(case["status"], "ambiguous_orders")
        self.assertEqual({o["company_id"] for o in case["orders"]}, {COMPANY, COMPANY2})
        self.assertIn("BC_SEVERAL_ORDERS_FOR_NUMBER", codes(case, "gaps"))

    def test_audience_refusal_is_not_permitted_without_io(self):
        f = Fixture()
        evidence = f.evidence(actor=RECEIVER)
        [company] = evidence["companies"]
        statuses = {(r["entity_set"], r["field"]): r["status"] for r in company["reads"]}
        self.assertEqual(statuses[("purchaseOrders", "number")], "single_candidate")
        [receipt] = company["receipts"][:1]
        self.assertEqual(receipt["lines"]["status"], "not_permitted")
        self.assertFalse(evidence["complete"])
        self.assertFalse(any("purchaseReceiptLines" in url for _m, url in f.api_calls()))
        bottle = only_line(build_purchase_case(evidence, evidence_label="injected-transport")
                           ["orders"][0], "P-0821")
        self.assertEqual(bottle["received_per_receipts"]["status"], "incomplete_read")

    def test_invoice_of_another_vendor_is_not_reconciled(self):
        f = Fixture()
        f.invoices[0]["vendorNumber"] = "V7777"
        case = f.case()
        self.assertIn("BC_INVOICE_VENDOR_DIFFERS", codes(case, "conflicts"))
        bottle = only_line(case["orders"][0], "P-0821")
        self.assertEqual(bottle["invoiced_per_invoices"]["status"], "no_document_found")

    def test_draft_and_in_review_invoices_are_shown_never_summed(self):
        for status in ("Draft", "In Review"):
            f = Fixture()
            f.invoices[0]["status"] = status
            case = f.case()
            order = case["orders"][0]
            bottle = only_line(order, "P-0821")
            self.assertEqual(bottle["invoiced_per_invoices"]["status"], "no_document_found")
            self.assertIsNone(bottle["invoiced_per_invoices"]["value"])
            self.assertEqual(len(order["draft_invoices"]), 1)
            self.assertEqual(order["invoices"], [])
            self.assertIn("BC_INVOICE_DRAFT_NOT_COUNTED", codes(case, "gaps"))
            self.assertNotIn("BC_INVOICE_UNIT_COST_DIFFERS", codes(case, "conflicts"))
            self.assertEqual(order["invoice_charges"], [])
            self.assertEqual(case["invoices"][0]["invoice_class"], "draft")

    def test_canceled_invoice_is_dropped(self):
        f = Fixture()
        f.invoices[0]["status"] = "Canceled"
        order = f.case()["orders"][0]
        bottle = only_line(order, "P-0821")
        self.assertIsNone(bottle["invoiced_per_invoices"]["value"])
        self.assertEqual(len(order["canceled_invoices"]), 1)
        self.assertEqual(order["invoices"], [])

    def test_blank_or_unknown_invoice_status_is_a_gap(self):
        for status in ("", "Corrective"):
            f = Fixture()
            f.invoices[0]["status"] = status
            case = f.case()
            bottle = only_line(case["orders"][0], "P-0821")
            self.assertEqual(bottle["invoiced_per_invoices"]["status"], "invoice_status_unknown")
            self.assertIsNone(bottle["invoiced_per_invoices"]["value"])
            self.assertIn("BC_INVOICE_STATUS_UNKNOWN", codes(case, "gaps"))
            self.assertNotIn("BC_INVOICES_NOT_FOUND", codes(case, "gaps"))

    def test_paid_invoice_is_counted(self):
        f = Fixture()
        f.invoices[0]["status"] = "Paid"
        bottle = only_line(f.case()["orders"][0], "P-0821")
        self.assertEqual(bottle["invoiced_per_invoices"]["value"], "1800")

    def test_vin_invoice_naming_another_order_is_not_attributed(self):
        f = Fixture()
        f.invoices[0]["orderNumber"] = "PO999999"
        case = f.case()
        bottle = only_line(case["orders"][0], "P-0821")
        self.assertEqual(bottle["invoiced_per_invoices"]["status"], "no_document_found")
        self.assertIn("BC_INVOICE_NAMES_OTHER_ORDER", codes(case, "conflicts"))
        self.assertNotIn("BC_INVOICE_UNIT_COST_DIFFERS", codes(case, "conflicts"))
        # the same order number in another case is this order
        g = Fixture()
        g.invoices[0]["orderNumber"] = PO.lower()
        self.assertEqual(only_line(g.case()["orders"][0], "P-0821")
                         ["invoiced_per_invoices"]["value"], "1800")

    def test_vin_invoice_with_blank_vendor_is_not_attributed(self):
        for target in ("invoice", "order"):
            f = Fixture()
            (f.invoices[0] if target == "invoice" else f.order)["vendorNumber"] = ""
            case = f.case()
            bottle = only_line(case["orders"][0], "P-0821")
            self.assertEqual(bottle["invoiced_per_invoices"]["status"], "no_document_found")
            self.assertIn("BC_INVOICE_VENDOR_UNVERIFIABLE", codes(case, "gaps"))

    def test_receipt_line_not_on_order_and_shared_item(self):
        f = Fixture()
        f.receipt_lines[3].append(line(13, 3, sequence=20000, lineType="Item",
                                       lineObjectNumber="X-1", unitOfMeasureCode="PCS",
                                       quantity=5, unitCost=1))
        f.order_lines.append(line(4, 1, sequence=40000, lineType="Item",
                                  lineObjectNumber="P-0821", unitOfMeasureCode="PCS",
                                  quantity=10, directUnitCost=0.51, receivedQuantity=0,
                                  invoicedQuantity=0))
        case = f.case()
        self.assertIn("BC_RECEIPT_LINE_NOT_ON_ORDER", codes(case, "conflicts"))
        self.assertIn("BC_ITEM_ON_SEVERAL_ORDER_LINES", codes(case, "gaps"))
        bottles = [ln for ln in case["orders"][0]["lines"] if ln["item_number"] == "P-0821"]
        self.assertEqual({b["received_per_receipts"]["status"] for b in bottles},
                         {"item_on_several_order_lines"})

    def test_invalid_inputs_refused_before_io(self):
        f = Fixture()
        with self.assertRaises(ValueError):
            read_purchase_evidence(f.reader, BUYER, "PO' or 1 eq 1")
        with self.assertRaises(ValueError):
            read_purchase_evidence(f.reader, BUYER, PO, vendor_invoice_numbers="INV-9")
        self.assertEqual(f.api_calls(), [])
        evidence = f.evidence(vendor_invoice_numbers=["bad'vin"])
        self.assertEqual(evidence["vendor_invoice_numbers"],
                         [{"value": "bad'vin", "searched": False, "reason": "not_searchable"}])
        with self.assertRaises(ValueError):
            build_purchase_case(evidence, evidence_label="live-verified")


def scoped_world(f: Fixture) -> None:
    """One order line of 100.5 PCS, one receipt line of 100 and one posted invoice line of
    100, the invoice naming the order by orderNumber and orderId."""
    f.order_lines[:] = [line(1, 1, sequence=10000, itemId=gid(70), lineType="Item",
                             lineObjectNumber="P-0821", description="Bottle 250ml",
                             unitOfMeasureCode="PCS", quantity=100.5, directUnitCost=0.51,
                             discountPercent=0, amountExcludingTax=51.26,
                             expectedReceiptDate="2026-10-15", receivedQuantity=100,
                             invoicedQuantity=100)]
    del f.receipts[1:]
    f.receipt_lines[2][:] = [line(10, 2, sequence=10000, lineType="Item",
                                  lineObjectNumber="P-0821", description="Bottle 250ml",
                                  unitOfMeasureCode="PCS", quantity=100, unitCost=0.51,
                                  expectedReceiptDate="2026-10-15")]
    f.invoices[0].update(orderId=gid(1), orderNumber=PO)
    f.invoice_lines[4][:] = [line(20, 4, sequence=10000, itemId=gid(70), lineType="Item",
                                  lineObjectNumber="P-0821", description="Bottle 250ml",
                                  unitOfMeasureCode="PCS", quantity=100, unitCost=0.51,
                                  discountPercent=0, amountExcludingTax=51.0)]


class EvidenceCapture:
    """The ``bc`` L1 capture of one read as a capture source lists it: its documents,
    lines and items, with ``scope_claims`` as each entity set's ``scopes`` and never a
    full traversal (one PO's records are not a whole set)."""

    source = "bc"

    def __init__(self, evidence: dict) -> None:
        self.rows: dict[str, list] = {}

        def put(company: str, record: dict, row: dict) -> None:
            self.rows.setdefault(record["entity_set"], []).append(CapturedRecord(
                source="bc", relation=record["entity_set"],
                record_key=f"{company}/{record['id']}", row=row, l1_handle=record["citation"],
                field_prefix="row", digest=record["content_sha256"], recorded_at=AT,
                superseded_at=None, last_seen_at=AT,
                audience_key=f"bc:{company}/{record['entity_set']}",
                capture_lineage="bc_manufacturing"))

        for company in evidence["companies"]:
            cid = company["company_id"]
            for bucket in ("orders", "receipts", "invoices", "items"):
                for document in company[bucket]:
                    put(cid, document, {"id": document["id"], "number": document["number"],
                                        **document["header"], **document["dates"]})
                    for item in (document["lines"] or {}).get("items", ()):
                        row = {"id": item["id"], "documentId": document["id"],
                               "lineType": item["line_type"],
                               "lineObjectNumber": item["item_number"],
                               "itemId": item["item_id"],
                               "unitOfMeasureCode": item["unit_of_measure_code"]}
                        row.update({e["field"]: e["value"] for e in (
                            *item["quantities"], item["unit_cost"],
                            item["amount_excluding_tax"])})
                        put(cid, item, row)
        self.claims = bc_purchase.scope_claims(evidence)

    def records(self, relations):
        return {r: list(self.rows.get(r, ())) for r in relations}

    def describe(self) -> dict:
        return {"generation": "bc-evidence", "relations": {
            r: {"last_refresh_at": AT, "last_capture_id": None, "last_full_traversal_at": None,
                "scopes": self.claims.get(r, [])} for r in sorted({*self.rows, *self.claims})}}


AT = "2026-10-04T09:00:00Z"


class ScopedCompletenessTests(unittest.TestCase):
    """A per-PO read reports scoped completeness claims, and ``plx.process`` decides the
    three-way sides it covers over a BC capture of that read alone, Portal not captured
    (injected transport; devloop/DECISIONS/2026-10-05-bc-only-portal-hops.md)."""

    def answer(self, evidence: dict) -> tuple[dict, dict]:
        schema = ot.default_schema()
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        path = Path(scratch.name) / "ontology.sqlite"
        build_store(path, load_schema(), {"bc": EvidenceCapture(evidence)},
                    normalizer=identifiers.normalize)
        reader = ot.StoreReader.open(path)
        self.addCleanup(reader.conn.close)
        policy = ot.ReadPolicy(tenant="plx", audiences={"bc:*": {"r"}, "api:*": {"r"}},
                               markings={m: {"r"} for m in store_ddl.MARKINGS})
        tools = ProcessTools(ot.OntologyTools(reader, policy, schema=schema,
                                              grammar=ot.default_grammar()))
        root = object_id_of("PurchaseOrder", "bc.purchase_orders", {"id": gid(1)})
        result = tools.call("plx.process", {"from": root, "process": "purchase_to_invoice"},
                            Actor("plx", frozenset({"r"})))
        contracts = ot.load_contracts()
        self.assertEqual(validate(result, contracts["tools"]["plx.process"]["output"],
                                  root=contracts, limit=3), [])
        return result, dict(reader.conn.execute("SELECT key, value FROM store_meta"))

    def test_a_fully_paged_po_decides_both_sides_from_bc_alone_and_says_so(self):
        f = Fixture()
        scoped_world(f)
        evidence = f.evidence(scoped_completeness=True,
                              clock=lambda: datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc))
        self.assertTrue(evidence["complete"])
        self.assertIn(("GET", f"orderId eq {gid(1)}"),
                      [(m, parse_qs(urlsplit(u).query).get("$filter", [""])[0])
                       for m, u in f.api_calls() if urlsplit(u).path.endswith("/purchaseInvoices")])
        claims = bc_purchase.scope_claims(evidence)
        self.assertEqual(claims["purchaseInvoices"], [{
            "scope": f"po={PO}", "company_id": COMPANY, "anchors": [gid(1)],
            "by": ["orderNumber", "orderId"], "complete": True, "read_at": AT}])
        self.assertEqual(claims["items"][0]["anchors"],
                         sorted([gid(501), gid(510), gid(520)]))
        result, meta = self.answer(evidence)
        checks = {tuple(c["roles"]): c for c in result["quantity_checks"]}
        short = checks[("ordered", "received")]
        self.assertIs(short["agree"], False)
        self.assertEqual({(v["quantity_role"], v["value"]) for v in short["values"]},
                         {("ordered", "100.5"), ("received", "100")})
        self.assertTrue(all(v["citation"]["l1_handle"].startswith(
            f"bc://production/{COMPANY}/") for v in short["values"]))
        self.assertIs(checks[("received", "invoiced")]["agree"], True)
        # Decided from BC alone: the Portal twin was not captured, so the answer says the
        # cross-system check did not run and is partial, never complete.
        unchecked = {g["subject"]: g["state"] for g in result["gaps"]
                     if g["reason"] == "cross_system_unchecked"}
        self.assertEqual(unchecked, {r: "not_ingested" for r in ("ordered", "received",
                                                                 "invoiced")})
        self.assertEqual((result["completeness"]["complete"], result["completeness"]["reason"]),
                         (False, "cross_system_unchecked"))
        # A per-PO read never becomes a source-wide claim: the bc family stays partial,
        # no entity set is fully traversed and every claim names its records.
        self.assertIn({"source": "bc", "completeness": "partial"},
                      [{k: f[k] for k in ("source", "completeness")}
                       for f in json.loads(meta["freshness"])])
        relations = json.loads(meta["source_freshness"])["bc"]
        self.assertEqual({r["last_full_traversal_at"] for r in relations.values()}, {None})
        self.assertTrue(all(c["anchors"] for r in relations.values() for c in r["scopes"]))

    def test_a_capped_read_keeps_its_side_undecided_and_says_why(self):
        f = Fixture()
        scoped_world(f)
        f.receipts.append(header(3, number="PRC-2", orderNumber=PO, postingDate="2026-09-27",
                                 vendorNumber="V0042", vendorName="Bottle Supplier Co",
                                 currencyCode="USD"))
        evidence = f.evidence(scoped_completeness=True, max_documents=1)
        self.assertEqual([c["complete"] for c in
                          bc_purchase.scope_claims(evidence)["purchaseReceipts"]], [False])
        result, _ = self.answer(evidence)
        received = [c for c in result["quantity_checks"] if "received" in c["roles"]]
        self.assertTrue(received)
        self.assertEqual({c["agree"] for c in received}, {None})
        self.assertIn(("received", "source_capture_incomplete"),
                      [(g["subject"], g["reason"]) for g in result["gaps"]])


class PortalLinkTests(unittest.TestCase):
    ORDER = {"id": gid(1), "number": PO}

    def test_identity_only_from_entity_map_naming_this_document(self):
        portal = {"id": "po-uuid-1", "po_number": PO, "custom_fields": {}, "bc_entity_map": {
            "entity_type": "purchaseOrder", "source_system": "portal",
            "source_id": "po-uuid-1", "status": "LINKED", "bc_system_id": gid(1).upper()}}
        link = portal_po_link(self.ORDER, portal, PO)
        self.assertEqual((link["link"], link["identity_asserted"]), ("identity", True))
        portal["bc_entity_map"]["bc_system_id"] = gid(2)
        link = portal_po_link(self.ORDER, portal, PO)
        self.assertEqual((link["link"], link["identity_asserted"]), ("conflict", False))

    def test_numbers_are_references(self):
        portal = {"id": "po-uuid-1", "po_number": PO, "custom_fields": json.dumps(
            {"fmPoNumber": "12345", "renumberedTo": PO, "renumberSource": "bc-drain-align"})}
        link = portal_po_link(self.ORDER, portal, PO)
        self.assertEqual(link["link"], "reference")
        self.assertFalse(link["identity_asserted"])
        self.assertEqual(link["basis"], ["portal_po_number_equals_bc_number",
                                         "portal_recorded_renumber_to_bc_number"])
        fm = portal_po_link(self.ORDER, {"id": "x", "po_number": "PO-ABC",
                                         "custom_fields": {"fmPoNumber": "12345"}}, "12345")
        self.assertIn("searched_fm_po_number", fm["basis"])
        self.assertFalse(fm["identity_asserted"])
        self.assertEqual(portal_po_link(self.ORDER, None, PO)["link"], "reference")
        none = portal_po_link(self.ORDER, {"id": "x", "po_number": "PO-OTHER"}, PO)
        self.assertEqual((none["link"], none["basis"]), ("none", ["no_link_evidence"]))

    def test_case_carries_portal_link(self):
        f = Fixture()
        case = f.case(portal_po={"id": "po-uuid-1", "po_number": PO})
        self.assertEqual(case["orders"][0]["portal_link"]["basis"],
                         ["portal_po_number_equals_bc_number"])
        self.assertFalse(case["identity_asserted"])


class ProbeTests(unittest.TestCase):
    def test_probe_reports_shapes_only(self):
        f = Fixture()
        f.invoices[0]["orderNumber"] = PO   # the probe finds the invoice by orderNumber
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = bc_purchase.main(["probe"], environ=ENV, sender=f.sender, books=PINS)
        self.assertEqual(code, 0)
        text = out.getvalue()
        report = json.loads(text)
        for secret in (PO, "V0042", "Bottle", "P-0821", "INV-9", "0.51", "0.53", "PRC-1",
                       COMPANY, CLIENT_SECRET, ACCESS_TOKEN):
            self.assertNotIn(secret, text)
        [company] = report["companies"]
        self.assertEqual(company["seed_from"], "receipt")
        self.assertTrue(company["receipt_order_number_present"])
        self.assertEqual(company["documents"], {"orders": 1, "receipts": 2, "invoices": 1})
        # PO directUnitCost 0.51 vs invoice unitCost 0.53 on P-0821 (PCS): one pair differs;
        # the freight Account line is no item line. Counts only, recorded by the live check.
        self.assertEqual(company["price_pairs"], {"invoice_lines": 1, "paired": 1, "equal": 0,
                                                  "differ": 1, "unpaired": 0,
                                                  "cost_absent": 0})
        from devloop import live_verify as lv
        spec = json.loads((Path(lv.ROOT) / lv.SPECS_DIR_NAME / "bc.purchase_reader.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(lv.check_expectations(spec, 0, report), [])
        self.assertEqual(lv._recorded(spec, report)["companies[*].price_pairs.differ"], [1])
        self.assertEqual(company["unavailable_reads"], [])
        self.assertIn("purchaseOrderLines.received", company["quantity_roles_read"])
        self.assertIn("purchaseReceiptLines.received", company["quantity_roles_read"])
        self.assertEqual(company["line_fields_absent"], [])
        self.assertEqual(report["adapter_status"], "live-unverified")
        self.assertTrue(all(m == "GET" for m, _ in f.api_calls()))

    def test_probe_reads_only_the_pinned_company_and_counts_the_rest(self):
        f = Fixture()
        f.routes[f"{BASE}/companies"] = j(200, {"value": [
            {"id": COMPANY2, "name": "CRONUS Test", "@odata.etag": 'W/"2"'},
            {"id": COMPANY, "name": "My Company", "@odata.etag": 'W/"1"'},
            {"id": OTHER, "name": "PLX Consol", "@odata.etag": 'W/"3"'}]})
        books = {"production": {COMPANY: "manufacturing"}}
        names = {"production": {COMPANY: "My Company"}}
        report = bc_purchase.probe(ENV, sender=f.sender, books=books, names=names)
        self.assertEqual((report["companies_listed"], report["companies_count"],
                          report["out_of_scope_companies"]), (3, 1, 2))
        self.assertFalse([u for _m, u in f.api_calls() if COMPANY2 in u or OTHER in u])
        self.assertEqual(len(report["companies"]), 1)
        # a pinned GUID listed under another name is not read at all
        f2 = Fixture()
        f2.routes[f"{BASE}/companies"] = j(200, {"value": [
            {"id": COMPANY, "name": "CRONUS Test", "@odata.etag": 'W/"1"'}]})
        report = bc_purchase.probe(ENV, sender=f2.sender, books=books, names=names)
        self.assertEqual((report["companies_count"], report["pinned_name_mismatch"]), (0, 1))
        self.assertFalse([u for _m, u in f2.api_calls() if "purchase" in u])

    def test_probe_names_an_unsupported_receipt_filter(self):
        f = Fixture()
        f.routes[f"{C1}/purchaseReceipts"] = j(400, {"error": {"code": "BadRequest"}})
        report = bc_purchase.probe(ENV, sender=f.sender, books=PINS)
        [company] = report["companies"]
        self.assertEqual(company["seed_from"], "purchase_order")
        self.assertEqual(company["unavailable_reads"], ["purchaseReceipts.orderNumber"])

    def test_live_check_spec_passes_on_injected_probe_and_fails_on_receipt_400(self):
        from pathlib import Path
        from devloop import live_verify as lv
        path = Path(lv.ROOT) / lv.SPECS_DIR_NAME / "bc.purchase_reader.json"
        spec = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(lv.validate_spec(spec, stem=path.stem), [])
        self.assertEqual(lv.check_expectations(spec, 0, bc_purchase.probe(
            ENV, sender=Fixture().sender, books=PINS)), [])
        f = Fixture()
        f.routes[f"{C1}/purchaseReceipts"] = j(400, {"error": {"code": "BadRequest"}})
        failures = lv.check_expectations(spec, 0, bc_purchase.probe(ENV, sender=f.sender, books=PINS))
        self.assertIn("companies[*].unavailable_reads not empty", failures)

    def _live_shape(self, receipts_route) -> tuple[Fixture, dict]:
        """The 82dbb71 live shape: company 1 seeds from a receipt; company 2 answers the
        seed receipt read with ``receipts_route`` and lists no purchase orders; company
        3 is an empty book (no receipts, no purchase orders)."""
        f = Fixture()
        company3 = "0f0e0d0c-0b0a-0908-0706-0504030202ff"
        c3 = f"{BASE}/companies({company3})"
        f.routes[f"{BASE}/companies"] = j(200, {"value": [
            {"id": cid, "name": name, "@odata.etag": 'W/"1"'}
            for cid, name in ((COMPANY, "manufacturing"), (COMPANY2, "consol"),
                              (company3, "distribution"))]})
        f.routes.update({f"{C2}/purchaseReceipts": receipts_route,
                         f"{C2}/purchaseOrders": collection([]),
                         f"{c3}/purchaseReceipts": collection([]),
                         f"{c3}/purchaseOrders": collection([])})
        return f, bc_purchase.probe(ENV, sender=f.sender, books=PINS)

    def test_failed_seed_read_is_diagnosed_and_an_empty_book_is_not_an_error(self):
        from pathlib import Path
        from devloop import live_verify as lv
        spec = json.loads((Path(lv.ROOT) / lv.SPECS_DIR_NAME / "bc.purchase_reader.json")
                          .read_text(encoding="utf-8"))

        def refuse_select(url: str):
            if "$select" in parse_qs(urlsplit(url).query):
                return j(400, {"error": {"code": "BadRequest", "message": f"no {PO}"}})
            return collection([])(url)

        f, report = self._live_shape(refuse_select)
        good, failed, empty = report["companies"]
        self.assertEqual(good["seed_from"], "receipt")
        self.assertEqual(failed["seed_from"], "none")
        self.assertEqual(failed["seed_reason"], "read_failed")
        self.assertNotIn("receipt_order_number_present", failed)
        self.assertEqual(failed["receipt_seed_error"], {
            "read": "purchaseReceipts", "select": ["number", "orderNumber"],
            "error_class": "SourceUnavailable", "http_status": 400,
            "error_code": "bad_request", "detail": "http_400",
            "without_select": {"status": "read"}})
        self.assertEqual(empty["seed_from"], "none")
        self.assertEqual(empty["seed_reason"], "no_purchase_documents")
        self.assertFalse({"error", "receipt_seed_error", "order_seed_error"} & set(empty))
        self.assertNotIn(PO, json.dumps(report))
        self.assertTrue(all(m == "GET" for m, _ in f.api_calls()))
        # three pinned companies: the Production spec's companies_count == 1 also fails
        self.assertEqual(lv.check_expectations(spec, 0, report),
                         ["companies_count != expected", "companies[*].receipt_seed_error present"])
        recorded = lv._recorded(spec, report)
        self.assertEqual(recorded["companies[*].seed_reason"],
                         ["read_failed", "no_purchase_documents"])
        self.assertEqual(recorded["companies[*].receipt_seed_error.http_status"], [400])
        self.assertEqual(recorded["companies[*].receipt_seed_error.without_select.status"],
                         ["read"])

        _f, healthy = self._live_shape(collection([]))
        self.assertEqual([c.get("seed_reason") for c in healthy["companies"]],
                         [None, "no_purchase_documents", "no_purchase_documents"])
        self.assertEqual(lv.check_expectations(spec, 0, healthy), ["companies_count != expected"])

    def test_outage_on_both_seed_reads_stays_a_visible_failure(self):
        from pathlib import Path
        from devloop import live_verify as lv
        spec = json.loads((Path(lv.ROOT) / lv.SPECS_DIR_NAME / "bc.purchase_reader.json")
                          .read_text(encoding="utf-8"))
        f = Fixture()
        f.routes[f"{C1}/purchaseReceipts"] = j(500, {"error": {"code": "Internal_ServerError"}})
        f.routes[f"{C1}/purchaseOrders"] = j(500, {"error": {"code": "Internal_ServerError"}})
        report = bc_purchase.probe(ENV, sender=f.sender, books=PINS)
        [company] = report["companies"]
        self.assertEqual(company["seed_from"], "none")
        self.assertEqual(company["seed_reason"], "read_failed")
        for key, entity_set in (("receipt_seed_error", "purchaseReceipts"),
                                ("order_seed_error", "purchaseOrders")):
            self.assertEqual(company[key]["read"], entity_set)
            self.assertEqual(company[key]["http_status"], 500)
            self.assertEqual(company[key]["without_select"]["status"], "unavailable")
        failures = lv.check_expectations(spec, 0, report)
        self.assertIn("companies[*].receipt_seed_error present", failures)
        self.assertIn("companies[*].order_seed_error present", failures)

    def test_grammar_allows_receipt_order_number(self):
        self.assertEqual(grammar.bc_query("purchaseReceipts", equals=("orderNumber", PO)),
                         {"$filter": f"orderNumber eq '{PO}'"})
        for entity_set, names in bc_purchase.SELECT.items():
            self.assertTrue(set(names) <= grammar.BC_QUERY_SPECS[entity_set].select, entity_set)
        self.assertEqual(bc_purchase.date_value("0001-01-01"), None)


if __name__ == "__main__":
    unittest.main()
