"""CT-D-08: a SharePoint list item that states a receipt's document date (DocDate) projects as an ExpenseReceipt with
typed columns (vendor, amount in its stated currency as Decimal text, document date,
category, status, tax year, who paid), finance and personal on the whole record; an item
that states an invoice date (InvoiceDate) and no DocDate projects as a VendorInvoiceEntry
(invoice number, dates, vendor, amount in the currency it states or unknown, the CAD
amount with its rate and source, month, file link, the commissions list's tax and owing
amounts), finance on the whole record; every other item stays the generated
SharePointListItem, so one record is one object, and every list item's text keeps its
mention links under its own type. The rule reads the item's own columns, never a list
named in code; a curated binding taking only some rows leaves the rest to the generated
type (``rest_to_generated``) and needs one negatable row test, plus at most the
exclusions that keep it disjoint from its sibling bindings."""

from __future__ import annotations

from contextlib import ExitStack
import copy
import json
from pathlib import Path
import sqlite3
import unittest

from integrations.tests import test_capture_sharepoint_lists as tsl
from operating import host

EXPENSE_COLUMNS = ["Title", "Vendor", "Amount", "Currency", "DocDate", "Category",
                   "Status", "Year", "PaidBy", "InvoiceId", "InvoiceDate", "AmountCAD",
                   "FXRate", "FXSource", "Month", "FileLink", "Notes", "Description",
                   "Currency0", "Status0", "EmailFrom", "InvoiceNumber", "DueDate",
                   "PreTaxAmount", "HSTAmount", "TotalWithHST", "InterestAccrued",
                   "TotalOwing", "PetraERPStatus"]


def _scope():
    from integrations.capture_sharepoint_lists import ListScope
    entry = {**tsl.SHIPPED, "columns": sorted(set(tsl.SHIPPED["columns"]) | set(EXPENSE_COLUMNS))}
    return ListScope({"schema": "plx-sharepoint-list-capture-scope/v1", "lists": [entry]})


class ListProjectionCase(tsl.CaptureCase):
    """One list capture and one projection build per test."""

    def project(self):
        state = Path(self.tmp.name)
        with ExitStack() as stack:
            sources = host.state_captures(state, stack)
            host.build_projection(state, sources)
        conn = sqlite3.connect(host.projection_path(state))
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        return conn

    def read(self, sql):
        """Rows of one query over a fresh projection, the connection closed before the
        next build (an open reader would hold the store the build rewrites)."""
        conn = self.project()
        try:
            return [dict(r) for r in conn.execute(sql)]
        finally:
            conn.close()

    def split(self, n):
        """``n`` items: every third an expense row (it states DocDate), every third a
        vendor-invoice row (InvoiceDate, no DocDate), the rest plain list items naming the
        invoice before them; one build."""
        self.graph.lists[tsl.LIST]["items"] = {}
        kinds = {}
        for i in range(1, n + 1):
            if i % 3 == 1:
                kinds[i] = "ExpenseReceipt"
                self.graph.add(i, fields={
                    "Title": f"Receipt {i}", "Vendor": "Test Coffee Shop",
                    "Amount": "__12.50__" if i % 2 else None, "Currency": "CAD",
                    "DocDate": "2026-03-04T08:00:00Z", "Category": "Meals",
                    "Status": "Review", "Year": "2026", "PaidBy": "Test Person"})
            elif i % 3 == 2:
                kinds[i] = "VendorInvoiceEntry"
                self.graph.add(i, fields=_invoice_fields(i))
            else:
                kinds[i] = "SharePointListItem"
                self.graph.add(i, fields={"Title": f"Paid against invoice no. INV-{i - 1}"})
        self.assertTrue(self.capture.run(_scope())["complete"])
        rows = self.read("SELECT object_type, native_key FROM objects WHERE object_type IN "
                         "('ExpenseReceipt', 'VendorInvoiceEntry', 'SharePointListItem')")
        got = {}
        for r in rows:
            got[r["object_type"]] = got.get(r["object_type"], 0) + 1
        want = {}
        for k in kinds.values():
            want[k] = want.get(k, 0) + 1
        self.assertEqual(got, want)
        keys = [r["native_key"] for r in rows]
        self.assertEqual(len(keys), len(set(keys)))   # one record, one object
        return self.values("ExpenseReceipt")

    def values(self, object_type):
        values = {}
        for row in self.read(
                "SELECT o.object_id, v.property, v.value, v.currency, v.tax_basis, v.markings "
                "FROM objects o JOIN object_values v USING (object_id) WHERE o.object_type = "
                f"'{object_type}' AND v.superseded_at IS NULL"):
            values.setdefault(row["object_id"], {})[row["property"]] = row
        return values


class ExpenseReceiptProjectionTests(ListProjectionCase):

    def test_cardinality_1(self):
        values = self.split(1)
        (first,) = values.values()
        self.assertEqual(first["amount"]["value"], "12.50")
        self.assertEqual(first["amount"]["currency"], "CAD")
        self.assertEqual(first["vendor"]["value"], "Test Coffee Shop")
        self.assertEqual(first["doc_date"]["value"], "2026-03-04T08:00:00.000000Z")
        self.assertEqual(first["tax_year"]["value"], "2026")
        self.assertEqual(first["paid_by"]["value"], "Test Person")

    def test_cardinality_10_and_every_value_is_finance_and_personal(self):
        values = self.split(10)
        self.assertEqual(len(values), 4)
        blank = next(v for v in values.values() if v["title"]["value"] == "Receipt 4")
        self.assertIsNone(blank["amount"]["value"])   # a blank amount stays blank
        for props in values.values():
            for name, row in props.items():
                marks = set(json.loads(row["markings"]))
                self.assertLessEqual({"finance", "pii"}, marks, name)

    def test_cardinality_60(self):
        self.assertEqual(len(self.split(60)), 20)

    def test_a_generic_item_keeps_its_fields_and_none_of_the_typed_ones(self):
        self.assertTrue(self.capture.run(_scope())["complete"])
        rows = self.read("SELECT object_type FROM objects WHERE object_type IN "
                         "('ExpenseReceipt', 'SharePointListItem')")
        self.assertEqual([r["object_type"] for r in rows], ["SharePointListItem"] * 5)


def _invoice_fields(i):
    """A vendor-invoice row in one of the three shapes the captured lists use: Currency
    with its CAD amount and rate, Currency0 (the list's own Currency name was taken), or
    the commissions-owing list with no currency column at all."""
    shape = (i // 3) % 3
    base = {"Title": f"Invoice {i}", "InvoiceDate": "2026-08-20T07:00:00Z"}
    if shape == 0:
        return {**base, "Vendor": "Test Supplier", "Amount": "100.0", "Currency": "USD",
                "AmountCAD": "137.39", "FXRate": "1.3739",
                "FXSource": "BoC Valet FXUSDCAD noon 2026-08-20", "Month": "2026-08",
                "InvoiceId": f"INV-{i}", "Category": "Test API", "Notes": "monthly",
                "FileLink": {"Url": f"https://example.invalid/inv-{i}.pdf",
                             "Description": f"inv-{i}.pdf"}}
    if shape == 1:
        return {**base, "Vendor": "Test Tool", "Amount": "56.5", "Currency0": "CAD",
                "Status0": "Pending Review", "EmailFrom": "billing@example.invalid",
                "InvoiceId": f"INV-{i}", "Category": "Other"}
    return {**base, "InvoiceNumber": f"INV-{i}", "DueDate": "2026-09-30T00:00:00Z",
            "PreTaxAmount": "10000.0", "HSTAmount": "1300.0", "TotalWithHST": "11300.0",
            "InterestAccrued": "717.81", "TotalOwing": "23317.81", "PetraERPStatus": "Paid"}


class VendorInvoiceEntryProjectionTests(ListProjectionCase):
    """The vendor-invoice rows of the same captures (made-up values in the real shapes)."""

    def invoices(self, n):
        self.split(n)
        return {props["title"]["value"]: props
                for props in self.values("VendorInvoiceEntry").values()}

    def test_each_shape_types_its_columns_and_currency(self):
        got = self.invoices(9)    # rows 2, 5, 8: one of each shape
        usd, cad0, owing = got["Invoice 2"], got["Invoice 5"], got["Invoice 8"]
        self.assertEqual((usd["amount"]["value"], usd["amount"]["currency"]), ("100.0", "USD"))
        self.assertEqual((usd["amount_cad"]["value"], usd["amount_cad"]["currency"]),
                         ("137.39", "CAD"))
        self.assertEqual(usd["fx_rate"]["value"], "1.3739")
        self.assertEqual(usd["invoice_id"]["value"], "INV-2")
        self.assertEqual(usd["invoice_date"]["value"], "2026-08-20T07:00:00.000000Z")
        self.assertEqual(usd["month"]["value"], "2026-08")
        self.assertEqual(usd["file_link"]["value"], "https://example.invalid/inv-2.pdf")
        self.assertEqual(usd["currency"]["value"], "USD")
        # Currency0 is the currency where the list's own Currency name was taken
        self.assertEqual((cad0["amount"]["value"], cad0["amount"]["currency"]), ("56.5", "CAD"))
        self.assertEqual(cad0["currency"]["value"], "CAD")
        self.assertEqual(cad0["status"]["value"], "Pending Review")
        self.assertIn("pii", json.loads(cad0["email_from"]["markings"]))
        # no currency column: the amounts stay, the currency is never assumed
        for name, basis in (("pre_tax_amount", "excl_tax"), ("hst_amount", "tax_only"),
                            ("total_with_hst", "incl_tax"), ("total_owing", "unknown")):
            self.assertIsNone(owing[name]["currency"], name)
            self.assertEqual(owing[name]["tax_basis"], basis, name)
        self.assertEqual(owing["total_owing"]["value"], "23317.81")
        self.assertEqual(owing["invoice_number"]["value"], "INV-8")
        self.assertEqual(owing["erp_status"]["value"], "Paid")
        for props in got.values():
            for name, row in props.items():
                self.assertIn("finance", set(json.loads(row["markings"])), name)

    def test_cardinality_10_one_object_per_invoice_row(self):
        self.assertEqual(len(self.invoices(10)), 3)

    def test_cardinality_60_one_object_per_invoice_row(self):
        got = self.invoices(60)
        self.assertEqual(len(got), 20)
        self.assertEqual(sum(1 for v in got.values() if "total_owing" in v), 6)

    def test_an_invoice_number_is_text_and_no_mention_link_type_targets_the_entry(self):
        # kept text until in-place migration can add a new identifier kind: an identifier
        # would make every mention document (mail, files, chats) a new link type into this
        # one, rebuilding the drive_items unit and its post-projection properties
        got = self.invoices(10)
        self.assertTrue(any(v.get("invoice_id", {}).get("value") == "INV-2"
                            for v in got.values()), got)
        from ontology.schema import load_schema
        schema = load_schema()
        self.assertEqual([lt for lt in schema.link_types
                          if lt.endswith(".mentions_vendor_invoice_entry")], [])
        self.assertEqual(self.read(
            "SELECT count(*) AS n FROM links WHERE link_type LIKE "
            "'%.mentions_vendor_invoice_entry'")[0]["n"], 0)

    def test_a_reader_without_the_finance_marking_reads_no_invoice_value(self):
        from integrations.source_mirror import AudienceMap
        from operating.case_runtime import CaseRuntime, LiveReaders
        from operating.ontology_host import CaseHostTools
        self.split(3)
        state = Path(self.tmp.name)
        runtime = CaseRuntime(self.tmp.name, client=None,
                              audience=AudienceMap({}, tenant="plx"), tenant="plx",
                              live=LiveReaders(self.audience))
        self.addCleanup(runtime.close)
        find = {"object_type": "VendorInvoiceEntry", "limit": 50,
                "filters": [{"property": "invoice_date", "op": "present"}]}
        seen = {}
        for grants in (None, {"finance": ["staff:list-reader"]}):
            tools = CaseHostTools(runtime, tsl.READER, store_path=host.projection_path(state),
                                  marking_grants=grants)
            self.addCleanup(tools.close)
            found = tools.call("plx.find", find, tsl.READER)
            seen[grants is not None] = found
        # without the marking a filter on a finance value finds nothing (never a count)
        self.assertEqual(seen[False]["candidates"], [])
        for value in ("Test Supplier", "137.39", "INV-2", "1.3739"):
            self.assertNotIn(value, json.dumps(seen[False]), value)
        (hit,) = seen[True]["candidates"]   # the grant is what opens it
        oid = hit["object"]["object_id"]
        opened = {}
        for grants in (None, {"finance": ["staff:list-reader"]}):
            tools = CaseHostTools(runtime, tsl.READER, store_path=host.projection_path(state),
                                  marking_grants=grants)
            self.addCleanup(tools.close)
            opened[grants is not None] = json.dumps(
                tools.call("plx.object", {"object_id": oid}, tsl.READER))
        for value in ("Test Supplier", "137.39", "INV-2", "1.3739"):
            self.assertNotIn(value, opened[False], value)
            self.assertIn(value, opened[True], value)

    def test_an_item_stating_both_dates_is_one_expense_receipt(self):
        self.graph.lists[tsl.LIST]["items"] = {}
        self.graph.add(1, fields={**_invoice_fields(2), "DocDate": "2026-03-04T08:00:00Z"})
        self.assertTrue(self.capture.run(_scope())["complete"])
        rows = self.read("SELECT object_type FROM objects WHERE object_type IN "
                         "('ExpenseReceipt', 'VendorInvoiceEntry', 'SharePointListItem')")
        self.assertEqual([r["object_type"] for r in rows], ["ExpenseReceipt"])


class RestToGeneratedRuleTests(unittest.TestCase):

    def test_the_generated_binding_takes_the_complement(self):
        from ontology.schema import load_schema
        schema = load_schema()
        generic = schema.bindings["gen.sharepoint.list_items"][1]
        self.assertIn({"path": "fields.DocDate", "op": "absent"}, generic.get("where", []))
        self.assertIn({"path": "fields.InvoiceDate", "op": "absent"}, generic.get("where", []))
        self.assertIn("SharePointListItem", schema.object_types)   # not shadowed
        expense = schema.bindings["sharepoint.expense_receipts"][1]
        self.assertEqual(expense["where"], [{"path": "fields.DocDate", "op": "present"}])

    def test_negation_is_exact_or_refused(self):
        from ontology.schema import _negated
        self.assertEqual(_negated({"path": "a", "op": "eq", "value": "x"}),
                         {"path": "a", "op": "not_in", "value": ["x"]})
        self.assertEqual(_negated({"path": "a", "op": "in", "value": ["x", "y"]}),
                         {"path": "a", "op": "not_in", "value": ["x", "y"]})
        self.assertIsNone(_negated({"path": "a", "op": "gte", "value": 3}))
        # a blank value: eq/in "" and not_in "" both hold on a blank cell, so refused
        self.assertIsNone(_negated({"path": "a", "op": "eq", "value": ""}))
        self.assertIsNone(_negated({"path": "a", "op": "in", "value": ["x", ""]}))

    def test_typed_list_items_keep_their_mention_link_types(self):
        from ontology import mentions
        from ontology.schema import load_schema
        pairs = mentions.mention_link_types(load_schema().link_types)
        for source in ("SharePointListItem", "ExpenseReceipt", "VendorInvoiceEntry"):
            self.assertIn((source, "PurchaseOrder"), pairs, source)
        self.assertEqual(pairs[("VendorInvoiceEntry", "PurchaseOrder")],
                         "VendorInvoiceEntry.mentions_purchase_order")

    def _compile_with(self, edit):
        from ontology import schema as sch
        root = Path(sch.__file__).parent / "schema"
        docs = []
        for path in sorted((root / "domains").glob("*.json")) + sorted(
                (root / "generated").glob("*.json")):
            d = json.loads(path.read_text())
            if path.name == "sharepoint_lists.json":
                d = copy.deepcopy(d)
                edit(d)
            docs.append((path.name, d))
        with self.assertRaises(sch.SchemaLoadError) as raised:
            sch.compile_documents(docs, check_format=False)
        return str(raised.exception)

    def test_two_rest_bindings_that_may_take_one_row_are_an_error(self):
        def overlap(doc):
            invoices = next(t for t in doc["object_types"] if t["id"] == "VendorInvoiceEntry")
            invoices["bindings"][0]["where"] = invoices["bindings"][0]["where"][:1]
        self.assertIn("may take the same row", self._compile_with(overlap))

    def test_a_rest_binding_without_one_negatable_test_is_an_error(self):
        from ontology import schema as sch
        doc = json.loads((Path(sch.__file__).parent / "schema" / "domains" /
                          "sharepoint_lists.json").read_text())
        bad = copy.deepcopy(doc)
        bad["object_types"][0]["bindings"][0]["where"].append(
            {"path": "fields.Vendor", "op": "present"})
        root = Path(sch.__file__).parent / "schema"
        docs = []
        for path in sorted((root / "domains").glob("*.json")) + sorted(
                (root / "generated").glob("*.json")):
            d = json.loads(path.read_text())
            docs.append((path.name, bad if path.name == "sharepoint_lists.json" else d))
        with self.assertRaises(sch.SchemaLoadError) as raised:
            sch.compile_documents(docs, check_format=False)
        self.assertIn("rest_to_generated needs one where predicate", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
