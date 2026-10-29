"""BC captured and projected on its own (``ontology/project_bc.py``), injected transport.

Every BC read goes through the real GET-only ``BcCapture`` into a real capture store,
served by the in-process ``FakeBc`` of ``integrations/tests/test_bc_capture.py`` (no
network, no credentials). The capture store is then projected alone (source ``bc``, no
Portal mirror) under the curated schema, and the generic tools answer over it. Two
companies reuse the same document and item numbers (synthetic ids and numbers only).
Evidence label: ``injected-transport``; the live counterpart is
``devloop/live-checks/ontology.projection_bc.json`` (live-unverified).
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from integrations.bc_read import CAPTURE_ENTITY_SETS
from integrations.tests import test_bc_capture as tb
from ontology import identifiers, store_ddl
from ontology.json_contract import validate
from ontology.project import ProjectionError, build_store, capture_source
from ontology.project_bc import INCOMPLETE, NOT_CAPTURED, BcCaptures
from ontology.schema import load_schema
from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_aggregate as oa
from operating import ontology_tools_traverse as tt
from operating.ontology_host import case_host_policy, store_bc_units
from operating.ontology_tools_process import ProcessTools
from scripts import ontology_projection_bc_probe as probe

MAN, DIST = tb.CO[0], tb.CO[1]
READER = "staff:full"
FULL = Actor("plx", frozenset({READER}))


def gid(company: int, kind: int, n: int = 1) -> str:
    return f"{company:08x}-{kind:04x}-4000-8000-{n:012x}"


def record(record_id: str, version: str = "0", **fields) -> dict:
    return {"@odata.etag": f'W/"{record_id[-6:]}{version}"', "id": record_id,
            "lastModifiedDateTime": "2026-09-30T10:00:00Z", **fields}


def purchase_world() -> tb.FakeBc:
    """One PO -> receipt -> posted invoice per company, every number reused by both
    companies (BC numbers are unique only within a company); BC API v2.0 field names."""
    fake = tb.FakeBc({name: 0 for name in CAPTURE_ENTITY_SETS}, companies=tb.CO)
    for c, company in ((1, MAN), (2, DIST)):
        po, item, receipt, invoice, vendor = (gid(c, k) for k in (1, 2, 3, 4, 5))
        fake.data[(company, "purchaseOrders")] = [record(
            po, number="PO-1001", vendorNumber="V-1", status="Open", orderDate="2026-09-01",
            requestedReceiptDate="2026-09-20", currencyCode="USD",
            totalAmountIncludingTax="#1200.00", fullyReceived=True)]
        fake.lines[(company, "purchaseOrders", po)] = [record(
            gid(c, 6), documentId=po, sequence=10000, lineType="Item",
            lineObjectNumber="ITEM-A", quantity="#100", unitOfMeasureCode="EA",
            directUnitCost="#12.00", receivedQuantity="#100", invoicedQuantity="#100")]
        fake.data[(company, "items")] = [record(
            item, number="ITEM-A", displayName="Widget", baseUnitOfMeasureCode="EA",
            type="Inventory", unitCost="#12.00", blocked=False)]
        fake.data[(company, "vendors")] = [record(
            vendor, number="V-1", displayName="Vendor", currencyCode="USD", balance="#0")]
        fake.data[(company, "purchaseReceipts")] = [record(
            receipt, number="PR-1", orderNumber="PO-1001", vendorNumber="V-1",
            postingDate="2026-09-18")]
        fake.lines[(company, "purchaseReceipts", receipt)] = [record(
            gid(c, 7), documentId=receipt, sequence=10000, lineType="Item",
            lineObjectNumber="ITEM-A", quantity="#100", unitOfMeasureCode="EA",
            unitCost="#12.00")]
        fake.data[(company, "purchaseInvoices")] = [record(
            invoice, number="PI-1", orderId=po, orderNumber="PO-1001", vendorId=vendor,
            vendorNumber="V-1", vendorInvoiceNumber="VI-9", invoiceDate="2026-09-21",
            postingDate="2026-09-21", dueDate="2026-10-21", status="Open",
            totalAmountExcludingTax="#1200.00", totalTaxAmount="#0",
            totalAmountIncludingTax="#1200.00")]
        fake.lines[(company, "purchaseInvoices", invoice)] = [record(
            gid(c, 8), documentId=invoice, sequence=10000, lineType="Item",
            lineObjectNumber="ITEM-A", quantity="#100", unitOfMeasureCode="EA",
            unitCost="#12.00",
            amountExcludingTax="#1200.00", description="Widget")]
    return fake


def sales_credit_world() -> tb.FakeBc:
    """A posted sales invoice and a credit memo applied to it (BC API v2.0 salesCreditMemo
    field names), with one Item line, in the manufacturing company only."""
    fake = tb.FakeBc({name: 0 for name in CAPTURE_ENTITY_SETS}, companies=tb.CO)
    customer, invoice, memo, item = (gid(1, k) for k in (11, 12, 13, 14))
    fake.data[(MAN, "customers")] = [record(customer, number="C-1", displayName="Customer",
                                            currencyCode="USD")]
    fake.data[(MAN, "items")] = [record(item, number="ITEM-A", displayName="Widget",
                                        baseUnitOfMeasureCode="EA")]
    fake.data[(MAN, "salesInvoices")] = [record(
        invoice, number="SI-1", customerId=customer, customerNumber="C-1", status="Open",
        postingDate="2026-09-10", currencyCode="USD", totalAmountExcludingTax="#1000.00")]
    fake.data[(MAN, "salesCreditMemos")] = [record(
        memo, number="CM-1", customerId=customer, customerNumber="C-1", invoiceId=invoice,
        invoiceNumber="SI-1", creditMemoDate="2026-09-20", postingDate="2026-09-20",
        status="Open", currencyCode="USD", totalAmountExcludingTax="#125.50",
        totalTaxAmount="#0", totalAmountIncludingTax="#125.50")]
    fake.lines[(MAN, "salesCreditMemos", memo)] = [record(
        gid(1, 15), documentId=memo, sequence=10000, lineType="Item",
        lineObjectNumber="ITEM-A", quantity="#5", unitOfMeasureCode="EA",
        unitPrice="#25.10", amountExcludingTax="#125.50")]
    return fake


def credit_memo_number_world() -> tb.FakeBc:
    """Posted credit memos BC states no ``invoiceId`` for (blank or the nil Guid), naming
    their invoice by ``invoiceNumber`` only: one unique in its own company, one whose number
    exists in the other company only, one whose number names two invoices, and one naming an
    invoice no company holds. CM-1 keeps its ``invoiceId`` and names another invoice's number.
    """
    fake = tb.FakeBc({name: 0 for name in CAPTURE_ENTITY_SETS}, companies=tb.CO)
    customer, invoice, item = (gid(1, k) for k in (11, 12, 14))
    for company, c in ((MAN, 1), (DIST, 2)):
        fake.data[(company, "customers")] = [record(gid(c, 11), number="C-1",
                                                    displayName="Customer",
                                                    currencyCode="USD")]
        fake.data[(company, "items")] = [record(gid(c, 14), number="ITEM-A",
                                                displayName="Widget",
                                                baseUnitOfMeasureCode="EA")]

    def invoice_row(record_id: str, number: str, company_customer: str) -> dict:
        return record(record_id, number=number, customerId=company_customer,
                      customerNumber="C-1", status="Open", postingDate="2026-09-10",
                      currencyCode="USD", totalAmountExcludingTax="#1000.00")

    fake.data[(MAN, "salesInvoices")] = [
        invoice_row(invoice, "SI-1", customer),
        invoice_row(gid(1, 12, 2), "SI-2", customer),
        invoice_row(gid(1, 12, 3), "SI-TWICE", customer),
        invoice_row(gid(1, 12, 4), "SI-TWICE", customer)]
    # The same number in the other company: a cross-company match is never a link.
    fake.data[(DIST, "salesInvoices")] = [invoice_row(gid(2, 12), "SI-ELSEWHERE",
                                                      gid(2, 11))]

    def memo(n: int, number: str, invoice_number: str, invoice_id: str) -> dict:
        return record(gid(1, 13, n), number=number, customerId=customer, customerNumber="C-1",
                      invoiceId=invoice_id, invoiceNumber=invoice_number,
                      creditMemoDate="2026-09-20", postingDate="2026-09-20", status="Open",
                      currencyCode="USD", totalAmountExcludingTax="#125.50",
                      totalTaxAmount="#0", totalAmountIncludingTax="#125.50")

    nil = "00000000-0000-0000-0000-000000000000"
    fake.data[(MAN, "salesCreditMemos")] = [
        memo(1, "CM-1", "SI-2", invoice),             # invoiceId wins over the number
        memo(2, "CM-NUMBER", "SI-1", nil),            # nil Guid: the number resolves
        memo(3, "CM-BLANK", "SI-1", ""),              # blank: the number resolves
        memo(4, "CM-AMBIGUOUS", "SI-TWICE", nil),     # two invoices: unresolved
        memo(5, "CM-CROSS", "SI-ELSEWHERE", nil),     # another company's invoice
        memo(6, "CM-UNKNOWN", "SI-NONE", nil)]        # no such invoice
    return fake


def payment_world() -> tb.FakeBc:
    """One vendor's posted purchase invoices in the manufacturing company, one PO each (BC
    API v2.0 field names): PO-P1 paid, PO-P2 partially paid, PO-P3 unpaid in EUR, PO-P4
    reduced by an applied purchase credit memo; a second memo names no invoice BC holds.
    No ``remainingAmount``: live BC serves none on purchase invoices (pin 09a82605)."""
    fake = tb.FakeBc({name: 0 for name in CAPTURE_ENTITY_SETS}, companies=tb.CO)
    vendor = gid(1, 20)
    fake.data[(MAN, "vendors")] = [record(vendor, number="V-1", displayName="Vendor")]
    invoices = {1: ("Paid", "USD", "500.00", "2026-08-01"),
                2: ("Open", "USD", "1000.00", "2026-08-15"),
                3: ("Open", "EUR", "300.00", "2026-09-25"),
                4: ("Open", "USD", "1000.00", "2026-09-30")}
    fake.data[(MAN, "purchaseOrders")], fake.data[(MAN, "purchaseInvoices")] = [], []
    for n, (status, currency, total, due) in invoices.items():
        po, invoice = gid(1, 21, n), gid(1, 22, n)
        fake.data[(MAN, "purchaseOrders")].append(record(
            po, number=f"PO-P{n}", vendorNumber="V-1", status="Open", currencyCode=currency))
        fake.data[(MAN, "purchaseInvoices")].append(record(
            invoice, number=f"PI-P{n}", orderId=po, orderNumber=f"PO-P{n}", vendorId=vendor,
            vendorNumber="V-1", postingDate="2026-07-20", dueDate=due, status=status,
            currencyCode=currency, totalAmountIncludingTax=f"#{total}"))
        fake.lines[(MAN, "purchaseOrders", po)] = []
        fake.lines[(MAN, "purchaseInvoices", invoice)] = []
    nil = "00000000-0000-0000-0000-000000000000"
    memos = ((1, "PCM-1", gid(1, 22, 4), "PI-P4", "200.00"),
             (2, "PCM-LOOSE", nil, "PI-NONE", "50.00"))
    fake.data[(MAN, "purchaseCreditMemos")] = [record(
        gid(1, 23, n), number=number, vendorId=vendor, vendorNumber="V-1", invoiceId=invoice,
        invoiceNumber=invoice_number, postingDate="2026-09-01", status="Open",
        currencyCode="USD", totalAmountIncludingTax=f"#{total}")
        for n, number, invoice, invoice_number, total in memos]
    fake.lines[(MAN, "purchaseCreditMemos", gid(1, 23, 1))] = [record(
        gid(1, 24), documentId=gid(1, 23, 1), sequence=10000, lineType="Item",
        lineObjectNumber="ITEM-A", quantity="#4", unitOfMeasureCode="EA",
        amountExcludingTax="#200.00")]
    fake.lines[(MAN, "purchaseCreditMemos", gid(1, 23, 2))] = []
    # A vendor payment journal line applied to PI-P2 (unposted), and the vendor's aged row.
    journal = gid(1, 25)
    fake.data[(MAN, "vendorPaymentJournals")] = [record(journal, code="PAYMENT")]
    fake.lines[(MAN, "vendorPaymentJournals", journal)] = [record(
        gid(1, 26), journalId=journal, journalDisplayName="PAYMENT", vendorId=vendor,
        vendorNumber="V-1", postingDate="2026-10-06", documentNumber="PMT-1",
        amount="#600.00", appliesToInvoiceId=gid(1, 22, 2), appliesToInvoiceNumber="PI-P2")]
    fake.data[(MAN, "agedAccountsPayables")] = [{
        "vendorId": vendor, "vendorNumber": "V-1", "currencyCode": "USD",
        "balanceDue": "#1500.00", "currentAmount": "#300.00", "period1Amount": "#800.00",
        "period2Amount": "#400.00", "period3Amount": "#0", "agedAsOfDate": "2026-10-05",
        "periodLengthFilter": "30D"}]
    # The receivable side: a customer receipt line applied to a posted sales invoice by
    # its number (no appliesToInvoiceId), and the customer's aged row.
    customer, sales_invoice, receipts = gid(1, 27), gid(1, 28), gid(1, 29)
    fake.data[(MAN, "customers")] = [record(customer, number="C-1", displayName="Customer")]
    fake.data[(MAN, "salesInvoices")] = [record(
        sales_invoice, number="SI-P1", customerId=customer, customerNumber="C-1",
        status="Open", postingDate="2026-09-01", currencyCode="USD",
        totalAmountIncludingTax="#900.00")]
    fake.lines[(MAN, "salesInvoices", sales_invoice)] = []
    fake.data[(MAN, "customerPaymentJournals")] = [record(receipts, code="CASHRCPT")]
    fake.lines[(MAN, "customerPaymentJournals", receipts)] = [record(
        gid(1, 30), journalId=receipts, journalDisplayName="CASHRCPT", customerId=customer,
        customerNumber="C-1", postingDate="2026-10-06", documentNumber="RCPT-1",
        amount="#-900.00", appliesToInvoiceId=nil, appliesToInvoiceNumber="SI-P1")]
    fake.data[(MAN, "agedAccountsReceivables")] = [{
        "customerId": customer, "customerNumber": "C-1", "name": "Customer",
        "currencyCode": "USD", "balanceDue": "#900.00", "currentAmount": "#900.00",
        "period1Amount": "#0", "period2Amount": "#0", "period3Amount": "#0",
        "agedAsOfDate": "2026-10-05", "periodLengthFilter": "30D"}]
    return fake


def po_id(company: int) -> str:
    return f'PurchaseOrder:bc.purchase_orders:{{"id":"{gid(company, 1)}"}}'


class BcWorld:
    """Capture ``fake`` through the real BcCapture, project the store alone, open the
    tools as a full-grant reader (every ``bc`` and ``api`` unit, every marking)."""

    def __init__(self, test: unittest.TestCase, fake: tb.FakeBc | None = None, *,
                 entity_sets=CAPTURE_ENTITY_SETS) -> None:
        self.h = tb.Harness(test, fake or purchase_world())
        self.capture = self.h.run(entity_sets=entity_sets)
        scratch = tempfile.TemporaryDirectory()
        test.addCleanup(scratch.cleanup)
        self.store_path = Path(scratch.name) / "ontology.sqlite"
        with BcCaptures(self.h.db) as source:
            self.build = build_store(self.store_path, load_schema(), {"bc": source},
                                     normalizer=identifiers.normalize)
        self.reader = ot.StoreReader.open(self.store_path)
        test.addCleanup(self.reader.conn.close)
        self.schema = ot.default_schema()
        self.contracts = ot.load_contracts()
        self.test = test
        self.tools(ot.ReadPolicy(tenant="plx", audiences={"bc:*": {READER}, "api:*": {READER}},
                                 markings={m: {READER} for m in store_ddl.MARKINGS}))

    def tools(self, policy: ot.ReadPolicy) -> None:
        self.base = ot.OntologyTools(self.reader, policy, schema=self.schema,
                                     grammar=ot.default_grammar())
        self.traverse, self.process = tt.TraverseTools(self.base), ProcessTools(self.base)
        self.aggregate = oa.AggregateTools(self.base, self.traverse)

    def call(self, name: str, arguments: dict, actor: Actor = FULL) -> dict:
        tool = {"plx.process": self.process, "plx.find": self.base, "plx.object": self.base,
                "plx.aggregate": self.aggregate}.get(name, self.traverse)
        out = tool.call(name, arguments, actor)
        self.test.assertEqual(validate(out, self.contracts["tools"][name]["output"],
                                       root=self.contracts, limit=3), [], name)
        for check in out.get("quantity_checks", ()):  # a typed code exactly when undecided
            self.test.assertEqual("undecided_reason" in check, check["agree"] is None, check)
        return out

    def around(self, root: str, link: str, direction: str = "reverse",
               actor: Actor = FULL) -> dict:
        return self.call("plx.around", {"from": root, "path": [
            {"link_type": link, "direction": direction}]}, actor)

    def resolved(self, handle: str) -> dict:
        """A cited handle, resolved offline through the capture store."""
        result = self.h.store.resolve(handle, tb.SERVICE)
        self.test.assertTrue(result["ok"], handle)
        return result

    def gaps(self, code: str) -> set[str]:
        return {g["where"] for g in self.build["gaps"] if g["code"] == code}


def citations(value) -> list[str]:
    return list(probe._citations(value))


class BcOnlyProjectionTests(unittest.TestCase):
    def test_a_bc_rooted_po_answers_around_timeline_and_process_with_bc_citations(self):
        w = BcWorld(self)
        with capture_source("bc", w.h.db) as source:  # the registry opens the BC store
            self.assertIsInstance(source, BcCaptures)
        root = po_id(1)
        for link, target in (("PurchaseOrderLine.line_of", gid(1, 6)),
                             ("Receipt.receives", gid(1, 3)),
                             ("PurchaseInvoice.invoices", gid(1, 4))):
            out = w.around(root, link)
            self.assertEqual([o["object_id"].rsplit('"', 2)[1] for o in out["objects"]],
                             [target], link)
            self.assertEqual({o["lineage"] for o in out["objects"]}, {"bc_manufacturing"})
            self.assertEqual(out["gaps"], [])
        # A pair feature cites both compared cells at their capture handles.
        edge = w.around(root, "Receipt.receives")["edges"][0]
        cited = {c["field_path"]: w.resolved(c["l1_handle"])["row"]
                 for f in edge["reason"] for c in f.get("citations", ())}
        self.assertEqual((cited["row.orderNumber"]["orderNumber"], cited["row.number"]["number"]),
                         ("PO-1001", "PO-1001"))

        timeline = w.call("plx.timeline", {"from": root})
        posted = {e["event_type"]: e for e in timeline["events"]}
        self.assertLessEqual({"Receipt.posted", "PurchaseInvoice.posted",
                              "PurchaseOrder.bc_expected"}, set(posted))
        event = posted["Receipt.posted"]
        self.assertEqual((event["time"], w.resolved(event["citation"]["l1_handle"])["row"]
                          ["postingDate"]), ("2026-09-18", "2026-09-18"))

        answer = w.call("plx.process", {"from": root, "process": "purchase_to_invoice"})
        states = {s["stage"]: s for s in answer["stages"]}
        self.assertEqual([states[s]["state"] for s in ("purchase_order", "receipt",
                                                        "vendor_invoice")], ["present"] * 3)
        # The G/L capture is complete in every company and holds no entry for the invoice:
        # only then is the stage absent in the source.
        self.assertEqual(states["gl_posting"]["missing"]["state"], "absent_in_source")
        values = {(v["quantity_role"], v["value"], v["unit"]): v["citation"]
                  for check in answer["quantity_checks"] for v in check["values"]
                  if v["value_type"] == "quantity"}
        self.assertEqual(set(values), {("ordered", "100", "EA"), ("received", "100", "EA"),
                                       ("invoiced", "100", "EA")})
        for citation in values.values():
            self.assertEqual(w.resolved(citation["l1_handle"])["row"]["quantity"], "100")
            self.assertEqual(citation["lineage"], "bc_manufacturing")
        everything = [w.around(root, "PurchaseInvoice.invoices"), timeline, answer]
        self.assertTrue(citations(everything))
        self.assertTrue(all(h.startswith("bc://") for h in citations(everything)))

    def test_bc_blank_date_projects_as_no_date_never_a_due_date(self):
        """BC sends 0001-01-01 for an empty date (integrations.capture_store.bc_blank_date):
        the projector stores no planned receipt, so the PO's receipt has no due date (a
        gap), never a year-1 date that is long overdue or on time for every schedule."""
        fake = purchase_world()
        for company in (MAN, DIST):
            fake.data[(company, "purchaseOrders")][0]["requestedReceiptDate"] = "0001-01-01"
            fake.data[(company, "purchaseReceipts")] = []
        w = BcWorld(self, fake)
        root = po_id(1)
        timeline = w.call("plx.timeline", {"from": root})
        self.assertNotIn("PurchaseOrder.bc_expected",
                         {e["event_type"] for e in timeline["events"]})
        answer = w.call("plx.process", {"from": root, "process": "po_delivery_window"})
        (due,) = answer["due"]
        # no date read: undecided with a gap (a BC-only build cannot prove its Portal twin
        # dateless either), never overdue from a year-1 date
        self.assertIsNone(due["overdue"])
        self.assertNotIn("due_by", due)
        self.assertIn(due["gap"]["reason"], ("no_due_date", "due_date_unproven"))

    def test_three_way_price_compares_one_stated_tax_basis_in_a_known_currency(self):
        """BC line prices take their tax basis from the header's pricesIncludeTax and a
        blank currencyCode the company's local currency (companyInformation); the PO vs
        invoice price check compares only on one known basis and currency."""
        def world(headers: dict, local: dict) -> BcWorld:
            fake = purchase_world()
            for (c, company), (po, invoice) in zip(((1, MAN), (2, DIST)), headers):
                fake.data[(company, "purchaseOrders")][0].update(po)
                fake.data[(company, "purchaseInvoices")][0].update(invoice)
                if local.get(c):
                    fake.data[(company, "companyInformation")] = [record(
                        gid(c, 30), displayName="Company", currencyCode=local[c])]
            return BcWorld(self, fake)

        def price(w: BcWorld, company: int) -> tuple[dict, set]:
            answer = w.call("plx.process", {"from": po_id(company),
                                            "process": "purchase_to_invoice"})
            check = next(c for c in answer["quantity_checks"]
                         if c["roles"] == ["ordered", "invoiced"])
            return check, {g["reason"] for g in answer["gaps"]}

        excl = {"pricesIncludeTax": False}
        w = world(((excl, dict(excl, currencyCode="")),       # local currency USD
                   (excl, {"pricesIncludeTax": True, "currencyCode": "USD"})),
                  {1: "USD"})
        check, _ = price(w, 1)
        self.assertIs(check["agree"], True, check["note"])
        self.assertEqual({(v["property"], v["value"], v["currency"]) for v in check["values"]},
                         {("bc_direct_unit_cost", "12.00", "USD"),
                          ("unit_cost", "12.00", "USD")})
        stored = w.reader.conn.execute(
            "SELECT DISTINCT property, tax_basis FROM object_values WHERE lineage = ? AND "
            "property IN ('bc_direct_unit_cost', 'unit_cost')", ("bc_manufacturing",))
        self.assertEqual(set(map(tuple, stored)), {("bc_direct_unit_cost", "excl_tax"),
                                                  ("unit_cost", "excl_tax")})
        check, gaps = price(w, 2)   # equal prices, one with tax and one without
        self.assertIsNone(check["agree"])
        self.assertIn("tax_basis_differs", gaps)

        w = world(((excl, {"pricesIncludeTax": None, "currencyCode": "USD"}),
                   (excl, dict(excl, currencyCode=""))),   # no companyInformation here
                  {1: "CAD"})
        check, gaps = price(w, 1)
        self.assertIsNone(check["agree"])
        self.assertIn("tax_basis_unknown", gaps)
        self.assertIn("PurchaseInvoiceLine.unit_cost@bc.purchase_invoice_lines",
                      w.gaps("tax_basis_unresolved"))
        check, gaps = price(w, 2)
        self.assertIsNone(check["agree"])
        self.assertIn("currency_mismatch", gaps)
        self.assertEqual([v["currency_gap"]["reason"] for v in check["values"]
                          if v["property"] == "unit_cost"], ["currency_unknown"])

    def test_portal_hops_are_typed_gaps_never_absence(self):
        w = BcWorld(self)
        self.assertIn("portal.app_purchase_order", w.gaps(NOT_CAPTURED))
        out = w.around(po_id(1), "PurchaseOrder.same_record_as")
        self.assertEqual(out["objects"], [])
        self.assertEqual([(g["state"], g["reason"]) for g in out["gaps"]],
                         [("not_ingested", "no_match")])
        self.assertIn("portal.app_purchase_order was not captured", out["gaps"][0]["detail"])
        # The three-way match reads the twin and identity hops through Portal bindings.
        # Portal was not captured, so a BC-rooted side proven complete in BC is decided
        # from BC alone and says so (devloop/DECISIONS/2026-10-05-bc-only-portal-hops.md):
        # a typed cross_system_unchecked gap per side and a partial answer, never an
        # absence and never complete.
        answer = w.call("plx.process", {"from": po_id(1), "process": "purchase_to_invoice"})
        checks = {tuple(c["roles"]): c["agree"] for c in answer["quantity_checks"]}
        self.assertEqual((checks[("ordered", "received")], checks[("received", "invoiced")]),
                         (True, True))
        self.assertEqual({(g["state"], g["reason"]) for g in answer["gaps"]
                          if g["subject"] in ("ordered", "received", "invoiced")},
                         {("not_ingested", "cross_system_unchecked")})
        self.assertEqual((answer["completeness"]["complete"], answer["completeness"]["reason"]),
                         (False, "cross_system_unchecked"))
        # A reader the Portal is withheld from never gets that: it stays undecided.
        w.tools(ot.ReadPolicy(tenant="plx", audiences={"bc:*": {READER}},
                              markings={m: {READER} for m in store_ddl.MARKINGS}))
        answer = w.call("plx.process", {"from": po_id(1), "process": "purchase_to_invoice"})
        self.assertEqual({c["agree"] for c in answer["quantity_checks"]}, {None})
        self.assertEqual({(g["state"], g["reason"]) for g in answer["gaps"]
                          if g["subject"] in ("ordered", "received", "invoiced")},
                         {("unknown", "evidence_not_fully_visible")})
        # A grant of exact units (as a case host decides it) is enough for the PO's own
        # company: BC links never cross companies. Without one unit a side reads, that
        # side stays undecided; the other company's units never stand in for it.
        units = [k for k in w.reader.audience_keys() if k.startswith(f"bc:{MAN}/")]
        lines = f"bc:{MAN}/purchaseInvoiceLines"
        self.assertIn(lines, units)
        others = [k for k in w.reader.audience_keys() if k.startswith(f"bc:{DIST}/")]
        for granted, expected in ((units, (True, True)),
                                  ([k for k in units if k != lines] + others, (True, None))):
            w.tools(ot.ReadPolicy(tenant="plx", audiences={
                "api:*": {READER}, **{k: {READER} for k in granted}},
                markings={m: {READER} for m in store_ddl.MARKINGS}))
            answer = w.call("plx.process", {"from": po_id(1), "process": "purchase_to_invoice"})
            checks = {tuple(c["roles"]): c["agree"] for c in answer["quantity_checks"]}
            self.assertEqual((checks[("ordered", "received")],
                              checks[("received", "invoiced")]), expected, granted)
        self.assertIn(("unknown", "evidence_not_fully_visible"),
                      {(g["state"], g["reason"]) for g in answer["gaps"]
                       if g["subject"] == "invoiced"})

    def test_an_incomplete_bc_capture_is_a_gap_never_absence(self):
        fake = purchase_world()
        fake.fail(rf"{MAN}\)/generalLedgerEntries$", (403, {}, b'{"error":{"code":"Forbidden"}}'))
        w = BcWorld(self, fake)
        self.assertEqual(tb.Harness.unit(w.h, w.capture, MAN, "generalLedgerEntries")["state"],
                         "failed")
        self.assertIn("bc.general_ledger_entries", w.gaps(INCOMPLETE))
        with BcCaptures(w.h.db) as source:
            fresh = source.describe()["relations"]
        self.assertIsNone(fresh["generalLedgerEntries"]["last_full_traversal_at"])
        self.assertIsNotNone(fresh["purchaseOrders"]["last_full_traversal_at"])
        # A unit is one company's whole entity set: its runs prove a per-company claim.
        scopes = {c["company_id"]: c for c in fresh["generalLedgerEntries"]["scopes"]}
        self.assertEqual((scopes[MAN]["complete"], scopes[DIST]["complete"],
                          scopes[DIST]["scope"]), (False, True, f"company={DIST}"))
        answer = w.call("plx.process", {"from": po_id(1), "process": "purchase_to_invoice"})
        gl = next(s for s in answer["stages"] if s["stage"] == "gl_posting")
        self.assertNotEqual(gl["missing"]["state"], "absent_in_source")
        meta = answer["meta"]["freshness"]
        self.assertEqual([(f["source"], f["completeness"]) for f in meta], [("bc", "partial")])
        # The other company's G/L capture is complete: its PO's absence is proven, though
        # the family stays partial.
        answer = w.call("plx.process", {"from": po_id(2), "process": "purchase_to_invoice"})
        gl = next(s for s in answer["stages"] if s["stage"] == "gl_posting")
        self.assertEqual(gl["missing"]["state"], "absent_in_source")
        self.assertEqual([(f["source"], f["completeness"]) for f in answer["meta"]["freshness"]],
                         [("bc", "partial")])

    def test_an_entity_set_never_captured_is_reported_and_never_absent(self):
        w = BcWorld(self, entity_sets=tuple(s for s in CAPTURE_ENTITY_SETS
                                            if s != "generalLedgerEntries"))
        self.assertIn("bc.general_ledger_entries", w.gaps(NOT_CAPTURED))
        self.assertNotIn("bc.general_ledger_entries", w.gaps(INCOMPLETE))
        answer = w.call("plx.process", {"from": po_id(1), "process": "purchase_to_invoice"})
        gl = next(s for s in answer["stages"] if s["stage"] == "gl_posting")
        self.assertNotEqual(gl["missing"]["state"], "absent_in_source")

    def test_one_company_is_never_verified_linked_to_another(self):
        w = BcWorld(self)
        self.assertGreater(w.build["counts"]["links.cross_company_not_linked"], 0)
        for company, lineage in ((1, "bc_manufacturing"), (2, "bc_distribution")):
            out = w.around(po_id(company), "PurchaseInvoice.invoices")
            self.assertEqual([o["object_id"] for o in out["objects"]],
                             [f'PurchaseInvoice:bc.purchase_invoices:{{"id":"{gid(company, 4)}"}}'])
            self.assertEqual({o["lineage"] for o in out["objects"]}, {lineage})
        report = probe.check_bc_projection(w.reader.conn, w.schema, sample=5)
        self.assertEqual(report["cross_company_links"], 0)
        self.assertEqual(report["roots_checked"], 2)

    def test_the_probe_counts_each_undecided_three_way_comparison_by_its_typed_code(self):
        """A receipt and an invoice on one PO line decide the quantity comparisons from BC
        alone; every undecided one is counted by the code that holds it back, and only a
        missing-source code may leave nothing decided (``three_way.explained``)."""
        def three_way(fake: tb.FakeBc) -> dict:
            w = BcWorld(self, fake)
            incomplete = any(g["code"] == INCOMPLETE or g["code"] == NOT_CAPTURED
                             and g["where"].startswith("bc.") for g in w.build["gaps"])
            return probe.check_bc_projection(w.reader.conn, w.schema, sample=5,
                                             bc_capture_incomplete=incomplete)["three_way"]

        # The price check's PO side states no tax basis or local currency here.
        self.assertEqual(three_way(purchase_world()), {
            "decided": 4, "undecided": 2, "undecided_by": {"currency_mismatch": 2},
            "unexplained": 2, "explained": True})

        def uninvoiced(**receipt_line) -> tb.FakeBc:
            fake = purchase_world()
            for c, company in ((1, MAN), (2, DIST)):
                fake.data[(company, "purchaseInvoices")] = []
                # no invoice posted: BC's own invoiced figure is 0 (absent_by)
                for po_line in fake.lines[(company, "purchaseOrders", gid(c, 1))]:
                    po_line["invoicedQuantity"] = "#0"
                fake.lines[(company, "purchaseReceipts", gid(c, 3))][0].update(receipt_line)
            return fake

        # An unfinished items capture in both companies: nothing is decided, and every
        # comparison names a missing source (or a stage with no record yet).
        fake = uninvoiced()
        for company in (MAN, DIST):
            fake.fail(rf"{company}\)/items", *[(403, {}, b'{"error":{"code":"Forbidden"}}')] * 9)
        self.assertEqual(three_way(fake), {
            "decided": 0, "undecided": 6, "undecided_by": {
                "source_capture_incomplete": 2, "no_evidence_record": 4},
            "unexplained": 0, "explained": True})
        # A receipt line naming an item that was never captured: its lines have no single
        # item, and the PO line's received figure disagrees with the item's lines;
        # nothing is decided and that is unexplained, never passed as a missing source.
        self.assertEqual(three_way(uninvoiced(lineObjectNumber="ITEM-X")), {
            "decided": 0, "undecided": 8, "undecided_by": {
                # the uninvoiced stage is proven absent from the PO line's zero figure, so
                # its quantity checks name the received side's cause; its price checks
                # keep absent_in_bc
                "group_not_single": 4, "recorded_measure_conflict": 2, "absent_in_bc": 2},
            "unexplained": 6, "explained": False})

    def test_an_undecided_check_keeps_its_typed_code_when_the_gap_list_is_cut(self):
        """The code is the check item's own (``undecided_reason``), never looked up in the
        gap list: an answer cut to fit the response keeps it."""
        w = BcWorld(self)
        args = {"from": po_id(1), "process": "purchase_to_invoice"}
        full = w.call("plx.process", args)
        self.assertIn("currency_mismatch", {g["reason"] for g in full["gaps"]})
        w.base.limits = {**w.base.limits, "max_result_bytes": ot._envelope_bytes(full) - 50}
        cut = w.call("plx.process", args)
        self.assertEqual(cut["completeness"]["reason"], "result_size")
        self.assertNotIn("currency_mismatch", {g["reason"] for g in cut["gaps"]})
        self.assertEqual([probe.undecided_code(c) for c in cut["quantity_checks"]
                          if c["agree"] is None], ["currency_mismatch"])

    def test_every_retained_version_is_projected_and_cites_its_own_capture(self):
        fake = purchase_world()
        h = tb.Harness(self, fake)
        h.run(entity_sets=("purchaseOrders",))
        fake.data[(MAN, "purchaseOrders")][0].update(
            {"@odata.etag": 'W/"changed"', "status": "Released"})
        h.clock.now = h.clock.now.replace(hour=13)
        h.run(entity_sets=("purchaseOrders",))
        with BcCaptures(h.db) as source:
            versions = list(source.records(["purchaseOrders"])["purchaseOrders"])
        mine = [v for v in versions if v.record_key == f"{MAN}/{gid(1, 1)}"]
        self.assertEqual([v.row["status"] for v in mine], ["Open", "Released"])
        self.assertEqual(mine[0].superseded_at, mine[1].recorded_at)
        self.assertIsNone(mine[1].superseded_at)
        self.assertEqual({v.audience_key for v in mine}, {f"bc:{MAN}/purchaseOrders"})
        for version in mine:
            resolved = h.store.resolve(version.l1_handle, tb.SERVICE)
            self.assertEqual((resolved["row"]["status"], resolved["digest"], resolved["lineage"]),
                             (version.row["status"], version.digest, "bc_manufacturing"))

    def test_a_file_that_is_not_a_bc_capture_store_is_refused(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        other = Path(scratch.name) / "other.sqlite"
        sqlite3.connect(other).execute("CREATE TABLE meta (key TEXT, value TEXT)").connection.close()
        for path, code in ((Path(scratch.name) / "absent.sqlite", "bc_capture_missing"),
                           (other, "bc_capture_layout_unsupported")):
            with self.assertRaises(ProjectionError) as caught:
                BcCaptures(path)
            self.assertEqual(caught.exception.code, code)


class SalesCreditMemoTests(unittest.TestCase):
    def test_a_credit_memo_is_reached_from_its_customer_and_invoice_with_cited_decimal_money(self):
        w = BcWorld(self, sales_credit_world())

        def oid(object_type: str, binding: str, kind: int) -> str:
            return f'{object_type}:{binding}:{{"id":"{gid(1, kind)}"}}'

        memo = oid("SalesCreditMemo", "bc.sales_credit_memos", 13)
        line = oid("SalesCreditMemoLine", "bc.sales_credit_memo_lines", 15)
        customer = oid("Customer", "bc.customers", 11)
        invoice = oid("SalesInvoice", "bc.sales_invoices", 12)
        hops = [(customer, "SalesCreditMemo.credited_to", "reverse", memo),
                (invoice, "SalesCreditMemo.applies_to", "reverse", memo),
                (memo, "SalesCreditMemoLine.line_of", "reverse", line),
                (line, "SalesCreditMemoLine.for_item", "forward",
                 oid("Product", "bc.items", 14))]
        answers = []
        for root, link, direction, target in hops:
            out = w.around(root, link, direction)
            self.assertEqual([o["object_id"] for o in out["objects"]], [target], link)
            self.assertEqual({e["tier"] for e in out["edges"]}, {"verified"}, link)
            answers.append(out)
        found = w.call("plx.find", {"query": "CM-1"})
        self.assertEqual((found["status"], found["candidates"][0]["object"]["object_id"]),
                         ("found", memo))
        values = {v["property"]: v for v in w.call("plx.object", {"object_id": memo})["values"]}
        total = values["total_amount"]
        self.assertEqual((total["value"], total["currency"]), ("125.50", "USD"))
        self.assertEqual(w.resolved(total["citation"]["l1_handle"])["row"]
                         ["totalAmountIncludingTax"], "125.50")
        self.assertEqual(values["applies_to_invoice_number"]["value"], "SI-1")
        lines = {v["property"]: v for v in w.call("plx.object", {"object_id": line})["values"]}
        quantity, price = lines["credited_quantity"], lines["unit_price"]
        self.assertEqual((quantity["value"], quantity.get("unit"), price["value"],
                          price["currency"]),
                         ("5", "EA", "25.10", "USD"))
        cited = citations([*answers, found, values, lines])
        self.assertTrue(cited)
        self.assertTrue(all(h.startswith("bc://") for h in cited))

    def test_a_memo_without_an_invoice_id_applies_to_the_invoice_its_number_names(self):
        """``SalesCreditMemo.applies_to``'s fallback binding (invoiceNumber, schema ``where``
        + ``unique_target``): it fires only where BC states no invoiceId, never across
        companies, and never guesses an ambiguous or unknown number."""
        w = BcWorld(self, credit_memo_number_world())
        memos = {n: f'SalesCreditMemo:bc.sales_credit_memos:{{"id":"{gid(1, 13, n)}"}}'
                 for n in range(1, 7)}
        si_1 = f'SalesInvoice:bc.sales_invoices:{{"id":"{gid(1, 12)}"}}'
        si_2 = f'SalesInvoice:bc.sales_invoices:{{"id":"{gid(1, 12, 2)}"}}'
        applied = {}
        for n, memo in memos.items():
            out = w.around(memo, "SalesCreditMemo.applies_to", "forward")
            applied[n] = [(o["object_id"], e["tier"]) for o, e in
                          zip(out["objects"], out["edges"])]
        # CM-1 names SI-2 by number but carries SI-1's invoiceId: only the id is used.
        self.assertEqual(applied[1], [(si_1, "verified")])
        self.assertNotIn(si_2, [o for o, _ in applied[1]])
        # The nil Guid and a blank both let the number resolve, at the same tier.
        self.assertEqual(applied[2], [(si_1, "verified")])
        self.assertEqual(applied[3], [(si_1, "verified")])
        # Ambiguous, cross-company and unknown numbers stay unresolved: no link at all.
        self.assertEqual((applied[4], applied[5], applied[6]), ([], [], []))
        self.assertEqual(w.build["counts"].get("links.ambiguous_target_not_linked"), 1)
        self.assertGreater(w.build["counts"].get("links.cross_company_not_linked", 0), 0)
        # The reverse hop is the one the margin plan walks from a posted invoice.
        back = w.around(si_1, "SalesCreditMemo.applies_to", "reverse")
        self.assertEqual(sorted(o["object_id"] for o in back["objects"]),
                         sorted([memos[1], memos[2], memos[3]]))


class PaymentStateTests(unittest.TestCase):
    """Is a PO's invoice paid, what is outstanding per currency, aged how long, net of
    applied purchase credit memos: plx.process and plx.aggregate over BC captures."""

    VENDOR = f'Supplier:bc.vendors:{{"id":"{gid(1, 20)}"}}'
    NIL = "00000000-0000-0000-0000-000000000000"

    @staticmethod
    def po(n: int) -> str:
        return f'PurchaseOrder:bc.purchase_orders:{{"id":"{gid(1, 21, n)}"}}'

    @staticmethod
    def stages(w: BcWorld, n: int) -> dict:
        answer = w.call("plx.process", {"from": PaymentStateTests.po(n),
                                        "process": "purchase_to_invoice"})
        return {s["stage"]: s for s in answer["stages"]}

    def open_invoices(self, w: BcWorld, *filters: dict) -> str:
        out = w.call("plx.around", {"from": self.VENDOR, "path": [
            {"link_type": "PurchaseInvoice.billed_by", "direction": "reverse"}],
            "filters": [{"property": "status", "op": "eq", "value": "Open"}, *filters]})
        return out["set"]

    @staticmethod
    def per_currency(w: BcWorld, handle: str, measure: str) -> dict:
        out = w.call("plx.aggregate", {"set": handle, "measure": {"property": measure,
                                                                  "op": "sum"},
                                       "group_by": ["currency"]})
        w.test.assertEqual(out["gaps"], [])
        w.test.assertFalse(out["partial"])
        for group in out["groups"]:
            for citation in group["inputs_citations"]:
                w.test.assertTrue(citation["l1_handle"].startswith("bc://"))
                w.resolved(citation["l1_handle"])
        return {g["currency"]: (g["value"], g["inputs"]) for g in out["groups"]}

    def test_paid_partial_unpaid_and_credited_invoices_per_currency_with_bc_citations(self):
        w = BcWorld(self, payment_world())
        paid, partial, credited = (self.stages(w, n) for n in (1, 2, 4))
        self.assertEqual(paid["vendor_payment"]["state"], "present")
        status = paid["vendor_payment"]["citations"][0]
        self.assertEqual(w.resolved(status["l1_handle"])["row"]["status"], "Paid")
        for stages in (partial, credited):  # an Open invoice is proven not paid
            self.assertEqual((stages["vendor_payment"]["missing"]["state"],
                              stages["vendor_payment"]["missing"]["reason"]),
                             ("absent_in_source", "status_not_reached"))
        self.assertEqual(credited["vendor_credit"]["state"], "present")
        memo = credited["vendor_credit"]["citations"][0]["l1_handle"]
        self.assertEqual(w.resolved(memo)["row"]["number"], "PCM-1")
        self.assertEqual(partial["vendor_credit"]["missing"]["state"], "absent_in_source")

        # Open invoices per currency, never one total: USD 1000 + 1000, EUR 300 apart (what
        # is still owed is not served: test_an_unexposed_..._is_a_gap_never_zero).
        self.assertEqual(self.per_currency(w, self.open_invoices(w), "total_amount"),
                         {"USD": ("2000", 2), "EUR": ("300", 1)})
        # Aged: open more than 30 days past due on 2026-10-05.
        aged = self.open_invoices(w, {"property": "due_date", "op": "lt",
                                      "value": "2026-09-05"})
        self.assertEqual(self.per_currency(w, aged, "total_amount"), {"USD": ("1000", 1)})

        # Credit memos net on the purchase side: only the memo applied to the PO's
        # invoice (1000 invoiced, 200 credited).
        out = w.call("plx.around", {"from": self.po(4), "path": [
            {"link_type": "PurchaseInvoice.invoices", "direction": "reverse"},
            {"link_type": "PurchaseCreditMemo.applies_to", "direction": "reverse"}]})
        self.assertEqual(self.per_currency(w, out["set"], "total_amount"), {"USD": ("200", 1)})
        # A memo naming no invoice BC holds is netted into no PO: it is counted from its
        # vendor (as credit_memo_unattributed on the sales side), never attributed.
        loose = w.call("plx.around", {"from": self.VENDOR, "path": [
            {"link_type": "PurchaseCreditMemo.credited_by", "direction": "reverse"}],
            "filters": [{"property": "applies_to_invoice_id", "op": "in",
                         "value": ["", self.NIL]}]})
        (memo_obj,) = loose["objects"]
        self.assertEqual(memo_obj["object_id"],
                         f'PurchaseCreditMemo:bc.purchase_credit_memos:{{"id":"{gid(1, 23, 2)}"}}')
        self.assertEqual(w.around(memo_obj["object_id"], "PurchaseCreditMemo.applies_to",
                                  "forward")["objects"], [])

    def test_payment_journal_lines_and_aged_balances_are_projected_with_bc_citations(self):
        w = BcWorld(self, payment_world())
        invoice = f'PurchaseInvoice:bc.purchase_invoices:{{"id":"{gid(1, 22, 2)}"}}'
        (payment,) = w.around(invoice, "Payment.applies_to_purchase_invoice")["objects"]
        self.assertEqual(w.around(payment["object_id"], "Payment.paid_to", "forward")
                         ["objects"][0]["object_id"], self.VENDOR)
        values = {v["property"]: v for v in w.call("plx.object", {
            "object_id": payment["object_id"]})["values"]}
        self.assertEqual(values["amount"]["value"], "600.00")
        # The journal line states no currency: a typed gap, never a guessed currency.
        self.assertEqual((values["amount"]["currency"], values["amount"]["currency_gap"]
                          ["reason"]), (None, "currency_unknown"))
        row = w.resolved(values["amount"]["citation"]["l1_handle"])["row"]
        self.assertEqual((row["amount"], row["appliesToInvoiceNumber"]), ("600.00", "PI-P2"))
        (aged,) = w.around(self.VENDOR, "AgedPayable.of_supplier")["objects"]
        aged_values = {v["property"]: v for v in w.call("plx.object", {
            "object_id": aged["object_id"]})["values"]}
        self.assertEqual((aged_values["balance_due"]["value"],
                          aged_values["period1_amount"]["value"]), ("1500.00", "800.00"))
        self.assertTrue(aged_values["balance_due"]["citation"]["l1_handle"].startswith(
            "bc://production/"))
        # The receivable side: a receipt applied by invoice number, the customer's aged row.
        customer = f'Customer:bc.customers:{{"id":"{gid(1, 27)}"}}'
        sales_invoice = f'SalesInvoice:bc.sales_invoices:{{"id":"{gid(1, 28)}"}}'
        (receipt,) = w.around(sales_invoice, "Payment.applies_to_sales_invoice")["objects"]
        self.assertEqual(w.around(receipt["object_id"], "Payment.received_from", "forward")
                         ["objects"][0]["object_id"], customer)
        (receivable,) = w.around(customer, "AgedReceivable.of_customer")["objects"]
        balance = {v["property"]: v for v in w.call("plx.object", {
            "object_id": receivable["object_id"]})["values"]}["balance_due"]
        self.assertEqual(balance["value"], "900.00")
        self.assertEqual(w.resolved(balance["citation"]["l1_handle"])["row"]["customerId"],
                         gid(1, 27))
        # A reader without the finance marking reaches no payment or aged row at all.
        w.tools(ot.ReadPolicy(tenant="plx", audiences={"bc:*": {READER}},
                              markings={m: {READER} for m in store_ddl.MARKINGS
                                        if m != "finance"}))
        for root, link in ((customer, "AgedReceivable.of_customer"),
                           (customer, "Payment.received_from"),
                           (self.VENDOR, "AgedPayable.of_supplier"),
                           (invoice, "Payment.applies_to_purchase_invoice")):
            self.assertEqual(w.around(root, link)["objects"], [], link)
        for hidden in (receivable, payment, aged):
            self.assertEqual(w.call("plx.object", {"object_id": hidden["object_id"]})
                             ["error"]["code"], "object_not_found")

    def test_an_unexposed_credit_memo_set_or_remaining_amount_is_a_gap_never_zero(self):
        fake = payment_world()
        fake.fail(rf"{MAN}\)/purchaseCreditMemos(\?|$)",
                  (404, {}, b'{"error":{"code":"BadRequest_NotFound"}}'))
        w = BcWorld(self, fake)
        self.assertEqual(tb.Harness.unit(w.h, w.capture, MAN, "purchaseCreditMemos")["state"],
                         "failed")
        self.assertFalse(w.capture["all_complete"])
        self.assertLessEqual({"bc.purchase_credit_memos", "bc.purchase_credit_memo_lines"},
                             w.gaps(INCOMPLETE))
        stages = self.stages(w, 4)
        self.assertEqual((stages["vendor_credit"]["missing"]["state"],
                          stages["vendor_credit"]["missing"]["reason"]),
                         ("unknown", "absence_unproven"))
        # Status still decides payment; the amount owed is unknown, never 0.
        self.assertEqual(stages["vendor_payment"]["missing"]["reason"], "status_not_reached")
        out = w.call("plx.aggregate", {"set": self.open_invoices(w), "measure": {
            "property": "remaining_amount", "op": "sum"}, "group_by": ["currency"]})
        self.assertTrue(out["partial"])
        self.assertEqual({g["key"]["currency"]["untrusted_text"]: g["value"]
                          for g in out["groups"]}, {"USD": None, "EUR": None})
        self.assertEqual([(g["state"], g["reason"]) for g in out["gaps"]],
                         [("inaccessible", "not_served"), ("unknown", "measure_value_missing")])


class HostPolicyTests(unittest.TestCase):
    def test_store_held_bc_units_are_granted_by_the_live_audience_alone(self):
        w = BcWorld(self)
        units = store_bc_units(w.reader)
        self.assertEqual(units[f"bc:{MAN}/purchaseOrders"], "production")
        runtime = SimpleNamespace(
            mirror=SimpleNamespace(_audience=lambda: SimpleNamespace(allows=lambda a, r: False)),
            live=SimpleNamespace(audience=tb.audience(), bc=None), employee_gate=lambda a: a)

        def granted(actor: Actor) -> set[str]:
            policy = case_host_policy(runtime, actor, None, bc_units=units)
            return {k for k, principals in policy.audiences.items() if principals}

        self.assertEqual(granted(tb.BUYER), {f"bc:{MAN}/purchaseOrders",
                                             f"bc:{MAN}/purchaseOrderLines"})
        # A line set needs its header set too (as the capture store decides).
        self.assertEqual(granted(tb.HEADERS_ONLY), {f"bc:{MAN}/purchaseOrders"})
        self.assertEqual(granted(tb.NOBODY), set())
        self.assertEqual(case_host_policy(SimpleNamespace(**{**vars(runtime), "live": None}),
                                          tb.BUYER, None, bc_units=units).audiences, {})
        w.tools(case_host_policy(runtime, tb.HEADERS_ONLY, None, bc_units=units))
        out = w.around(po_id(1), "PurchaseOrderLine.line_of", actor=tb.HEADERS_ONLY)
        self.assertEqual(out["objects"], [])
        self.assertNotIn("absent_in_source", {g["state"] for g in out["gaps"]})
        w.tools(case_host_policy(runtime, tb.BUYER, None, bc_units=units))
        out = w.around(po_id(1), "PurchaseOrderLine.line_of", actor=tb.BUYER)
        self.assertEqual(len(out["objects"]), 1)


class ProbeTests(unittest.TestCase):
    def test_the_live_probe_runs_bc_alone_and_prints_counts_only(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = probe.main(["--db", f"{scratch.name}/capture.sqlite", "--store",
                               f"{scratch.name}/ontology.sqlite", "--sample", "5"],
                              environ=tb.ENV, sender=purchase_world(), books=tb.BOOKS)
        self.assertEqual(code, 0, out.getvalue())
        report = json.loads(out.getvalue())
        self.assertEqual((report["evidence_label"], report["companies_captured"],
                          report["units_complete"], report["units_reached"],
                          report["units_total"]),
                         ("injected-transport", 3, 108, 108, 108))
        self.assertEqual((report["roots_checked"], report["process_calls"],
                          report["contract_failures"], report["non_bc_citations"],
                          report["portal_hop_absent"], report["cross_company_links"]),
                         (2, 2, 0, 0, 0, 0))
        self.assertGreater(report["bc_citations"], 0)
        self.assertGreater(report["portal_hop_gaps"].get("not_ingested", 0), 0)
        self.assertEqual(report["bc_uncaptured_bindings"], 0)
        self.assertEqual(report["bc_incomplete_by_binding"], {})
        self.assertEqual(report["three_way"]["undecided_by"], {"currency_mismatch": 2})
        self.assertIs(report["three_way"]["explained"], True)
        self.assertGreater(report["portal_bindings_not_captured"], 0)
        text = out.getvalue()
        for secret in ("bc://", "PO-1001", "ITEM-A", "Widget", MAN, tb.NAMES[MAN]):
            self.assertNotIn(secret, text)

    def test_an_unproven_bc_side_beside_its_uncaptured_twin_is_not_a_missing_source(self):
        """Every BC binding captured in full, but each receipt's line is captured twice
        (``capture_versions_overlap``): the received side's own evidence is unproven, and
        its gap reads ``source_capture_incomplete`` for the uncaptured Portal twin. With
        nothing decided, that is unexplained: only an incomplete BC capture explains it."""
        fake = purchase_world()
        for c, company in ((1, MAN), (2, DIST)):
            fake.data[(company, "purchaseInvoices")] = []
            # no invoice posted: BC's own invoiced figure is 0 (absent_by)
            for po_line in fake.lines[(company, "purchaseOrders", gid(c, 1))]:
                po_line["invoicedQuantity"] = "#0"
            lines = fake.lines[(company, "purchaseReceipts", gid(c, 3))]
            lines.append(dict(lines[0], sequence=20000))
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            probe.main(["--db", f"{scratch.name}/capture.sqlite", "--store",
                        f"{scratch.name}/ontology.sqlite", "--sample", "5"],
                       environ=tb.ENV, sender=fake, books=tb.BOOKS)
        report = json.loads(out.getvalue())
        self.assertEqual((report["all_complete"], report["bc_incomplete_bindings"],
                          report["bc_uncaptured_bindings"], report["contract_failures"]),
                         (True, 0, 0, 0))
        self.assertEqual(report["three_way"], {
            "decided": 0, "undecided": 4, "undecided_by": {
                "source_capture_incomplete": 2, "absent_in_bc": 2},
            "unexplained": 2, "explained": False})


if __name__ == "__main__":
    unittest.main()
