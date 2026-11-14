"""Entry assist (``operating.entry_assist``): a customer PO pre-fills the Sage order, every
field cited or ``check``, never a guessed value. SYNTHETIC fixtures; evidence label:
isolated-fixture."""

from __future__ import annotations

import time
import unittest

from operating import autopilot as ap
from operating import entry_assist as ea
from operating import proactive_engine as pe
from operating.tests.test_autopilot import NOW, Fixture


class OrderEntry(Fixture):

    def ctx(self):
        self.build()
        import sqlite3
        with sqlite3.connect(self.dir / "s.sqlite") as c:   # the builder's customer numbers
            for v in self.w.rows["object_values"]:
                if v["object_id"].startswith("Customer:") and v["property"] == "erp_code":
                    c.execute("INSERT INTO identifiers (identifier_kind, normalized, raw, "
                              "role, object_id, property, l1_handle, field_path, lineage, "
                              "recorded_at) VALUES ('customer_erp_code', ?, ?, 'primary', ?, "
                              "'erp_code', 'h', 'row.erp_code', 'workbench', "
                              "'2026-01-01T00:00:00Z')",
                              (v["value"].upper(), v["value"], v["object_id"]))
        st = pe._Store(self.dir / "s.sqlite", threads=1)
        self.addCleanup(st.close)
        return ap.Ctx(st, NOW.date(), time.monotonic() + 60, ())

    def test_a_po_mail_prefills_customer_po_and_lines_and_flags_what_it_cannot_read(self):
        self.items_known("SYN-10", "SYN-20")
        msg = self.message(2, "Customer #: C100\nPlease ship:\n1. SYN-10 Gallon soap: "
                              "6 cases\n2. ZZ-404 Unknown thing: 2 cases",
                           subject="New PO 88123")
        got = ea.order_entry(self.ctx(), email=msg)
        h = got["header"]
        self.assertTrue(got["dry_run"])
        self.assertEqual((h["customer"]["status"], h["customer"]["value"]), ("cited", "C100"))
        self.assertIn("customer number the PO states", h["customer"]["source"]["basis"])
        self.assertEqual((h["po_number"]["status"], h["po_number"]["value"]), ("cited", "88123"))
        self.assertEqual(h["ship_to"]["status"], "check")
        known, unknown = got["lines"]
        self.assertEqual((known["item"]["status"], known["item"]["value"]), ("cited", "SYN-10"))
        self.assertEqual(known["quantity"]["value"], "6")
        # no price anywhere: flagged, never filled
        self.assertEqual(known["unit_price"], {"value": None, "status": "check",
                                               "reason": known["unit_price"]["reason"]})
        # an item number the records do not hold is never invented
        self.assertEqual((unknown["item"]["status"], unknown["item"]["value"]), ("check", None))
        self.assertEqual(got["counts"]["check"] + got["counts"]["cited"],
                         got["counts"]["fields"])

    def test_the_sender_alone_never_fills_the_customer_it_is_only_suggested(self):
        # the mail is linked to C100 (its sender's record) but the PO names no customer:
        # a distributor or broker sends POs too, so the sender is no proof of the buyer
        msg = self.message(3, "Please ship 6 cases of SYN-10", subject="PO 88124")
        c = ea.order_entry(self.ctx(), email=msg)["header"]["customer"]
        self.assertEqual((c["status"], c["value"]), ("check", None))
        self.assertEqual([s["value"] for s in c["suggestions"]], ["C100"])
        self.assertIn("sender's customer record", c["suggestions"][0]["basis"])

    def test_a_po_naming_another_customer_than_the_senders_is_a_check_with_both(self):
        # a distributor (D200, the sender's record) sends its end customer's PO
        dist = self.w.obj("Customer", {"name": "Wholesale Distribution Inc", "code": "D200",
                                       "erp_code": "D200"})
        msg = self.message(4, "Ship to: Synth customer\nSYN-10: 6 cases", subject="PO 88125",
                           customer=False)
        self.w.link("Email.sent_by_customer", msg, dist)
        c = ea.order_entry(self.ctx(), email=msg)["header"]["customer"]
        self.assertEqual((c["status"], c["value"]), ("check", None))
        self.assertEqual(sorted(c["candidates"]), ["C100", "D200"])
        # the numbers are never in the words (a reader may open neither record)
        self.assertNotIn("C100", c["reason"])
        self.assertNotIn("D200", c["reason"])

    def test_the_named_customer_is_cited_from_pasted_text_and_two_named_are_a_check(self):
        self.w.obj("Customer", {"name": "Northern Wholesale Ltd", "code": "N300",
                                "erp_code": "N300"})
        self.w.obj("Customer", {"name": "Street numbered", "erp_code": "1200"})
        ctx = self.ctx()
        one = ea.order_entry(ctx, text="Bill to: Synth customer\nPO 77001\nSYN-10: 6 cases")
        c = one["header"]["customer"]
        self.assertEqual((c["status"], c["value"]), ("cited", "C100"))
        self.assertEqual(c["source"]["basis"], "the customer's name in the PO")
        two = ea.order_entry(ctx, text="Sold to Northern Wholesale Ltd, ship to Synth "
                                       "customer\nPO 77002")
        c = two["header"]["customer"]
        self.assertEqual((c["status"], sorted(c["candidates"])), ("check", ["C100", "N300"]))
        # a labelled number beats names; a vendor's own number is never the buyer's
        got = ea.order_entry(ctx, text="Vendor #: N300\nCustomer No: C100\nNorthern "
                                       "Wholesale Ltd\nPO 77003")["header"]["customer"]
        self.assertEqual((got["status"], got["value"]), ("cited", "C100"))
        none = ea.order_entry(ctx, text="Vendor #: N300\nPO 77004")["header"]["customer"]
        self.assertEqual(none["status"], "check")
        # an address block's street number is no customer number, nor an unlabelled one
        for text in ("Bill To: 1200 Industrial Pkwy\nPO 77005",
                     "Customer 1200 asked\nPO 77006"):
            got = ea.order_entry(ctx, text=text)["header"]["customer"]
            self.assertEqual(got["status"], "check", text)

    def test_several_po_numbers_or_no_sender_are_checks_with_candidates(self):
        self.items_known("SYN-10")
        ctx = self.ctx()
        got = ea.order_entry(ctx, text="PO 4512\nPO 4513\nSYN-10 soap: 6 cases")
        self.assertEqual(got["header"]["po_number"]["status"], "check")
        self.assertEqual(got["header"]["po_number"]["candidates"], ["4512", "4513"])
        self.assertEqual(got["header"]["customer"]["status"], "check")
        self.assertEqual(got["lines"][0]["unit_price"]["status"], "check")
        with self.assertRaises(ValueError):
            ea.order_entry(ctx)

    def test_a_last_price_is_cited_only_in_the_lines_unit_and_the_customers_currency(self):
        from operating.tests.test_proactive_business import money
        for per, ordered, reused in (("CASE", "6 cases", True), ("KG", "6 cases", False)):
            with self.subTest(per=per):
                self.setUp()
                self.w.rows["object_values"].append({
                    **[v for v in self.w.rows["object_values"]
                       if v["object_id"] == self.customer][0],
                    "property": "currency", "value": "CAD", "value_type": "text"})
                self.items_known("SYN-10")
                inv = self.w.obj("SalesInvoice", {"invoice_number": "INV-9",
                                                  "invoice_date": "2026-09-01",
                                                  "currency": "CAD"})
                self.w.link("SalesInvoice.billed_to", inv, self.customer)
                line = self.w.obj("SalesInvoiceLine", {"unit_price": "4.00",
                                                       "invoiced_quantity": ("10", per)})
                money(self.w, line, "unit_price", "CAD", per=per)
                self.w.link("SalesInvoiceLine.line_of", line, inv)
                self.w.link("SalesInvoiceLine.for_item", line, self.items["SYN-10"])
                msg = self.message(95, "Account No. C100\nPlease ship:\n"
                                       f"1. SYN-10 Gallon soap: {ordered}",
                                   subject="New PO 88130")
                bc = self.w.obj("Customer", {"name": "Synth customer", "erp_code": "C100"},
                                binding="bc.customers")
                handle = ("bc://production/c0c0c0c0-1111-4111-8111-000000000001/customers/"
                          "b1b1b1b1-2222-4222-8222-000000000002@v1")
                for row in self.w.rows["object_versions"] + self.w.rows["object_values"]:
                    if row["object_id"] == bc:
                        row["l1_handle"] = handle
                self.w.link("Customer.same_record_as", self.customer, bc)
                ctx = self.ctx()
                pre = ea.order_entry(ctx, email=msg)
                (ln,) = pre["lines"]
                entry = ea.bc_header(ctx, pre, "order")
                if reused:
                    self.assertEqual(ln["unit_price"]["status"], "cited")
                    self.assertEqual(ln["unit_price"]["value"], "4.00")
                    self.assertEqual(ln["unit_price"]["source"]["document"], "INV-9")
                    # the BC entry carries the cited price, and names its source record
                    # among those the reader must open before it reaches the desk
                    (bl,) = entry["payload"]["lines"]
                    self.assertEqual((bl["lineObjectNumber"], bl["quantity"], bl["unitPrice"]),
                                     ("SYN-10", "6", "4.00"))
                    self.assertIn(ln["unit_price"]["source"]["record"], entry["records"])
                    self.assertTrue({self.customer, bc, self.items["SYN-10"]}
                                    <= set(entry["records"]))
                    self.assertEqual(entry["party"]["bc_id"],
                                     "b1b1b1b1-2222-4222-8222-000000000002")
                else:
                    self.assertEqual((ln["unit_price"]["status"], ln["unit_price"]["value"]),
                                     ("check", None))

    def test_accuracy_counts_a_check_apart_from_a_wrong_value(self):
        pre = {"header": {"customer": ea.cited("C100", {}), "po_number": ea.check("none")},
               "lines": [{"item": ea.cited("SYN-10", {}), "quantity": ea.cited("6", {}),
                          "unit_price": ea.cited("4.00", {})},
                         {"item": ea.cited("SYN-20", {}), "quantity": ea.cited("2", {}),
                          "unit_price": ea.check("none")}]}
        got = ea.accuracy(pre, {"customer": ["C100"], "po_number": "88",
                                "lines": [("SYN-10", "6", "4"), ("SYN-30", "2", "1")]})
        # a line whose item is wrong leaves its quantity unscored, not wrong
        self.assertEqual(got, {"customer_right": 1, "po_number_check": 1, "item_right": 1,
                               "quantity_right": 1, "unit_price_right": 1, "item_wrong": 1,
                               "quantity_unscored": 1, "unit_price_check": 1,
                               "lines_keyed": 2, "lines_prefilled": 2})
        keyed_with_words = {"header": {"customer": ea.check("x"),
                                       "po_number": ea.cited("PO-153", {})}, "lines": []}
        self.assertEqual(ea.accuracy(keyed_with_words, {"po_number": "PO-153 Paw lotion"})
                         ["po_number_right"], 1)
        self.assertEqual(ea.accuracy(keyed_with_words, {"po_number": "PO-1534"})
                         ["po_number_wrong"], 1)


class PurchaseOrderAssist(Fixture):

    def ctx(self):
        self.build()
        st = pe._Store(self.dir / "s.sqlite", threads=1)
        self.addCleanup(st.close)
        return ap.Ctx(st, NOW.date(), time.monotonic() + 60, ())

    def po(self, supplier, day, lines):
        po = self.w.obj("PurchaseOrder", {"po_number": f"P-{day}", "order_date": day,
                                          "currency": "CAD"})
        self.w.link("PurchaseOrder.ordered_from", po, supplier)
        for item, qty, price in lines:
            ln = self.w.obj("PurchaseOrderLine", {"ordered_quantity": qty, "unit_price": price,
                                                  "uom": "KG"})
            self.w.link("PurchaseOrderLine.line_of", ln, po)
            self.w.link("PurchaseOrderLine.for_item", ln, self.items[item])
        return po

    def test_named_items_take_their_last_supplier_and_price_and_leave_the_quantity_a_check(self):
        self.items_known("SYN-10", "SYN-20")
        sup = self.w.obj("Supplier", {"name": "Synth supplier"})
        self.po(sup, "2026-09-01", [("SYN-10", "100", "2.00")])
        last = self.po(sup, "2026-10-01", [("SYN-10", "120", "2.10"), ("SYN-20", "5", "9")])
        self.po(sup, "2026-12-01", [("SYN-10", "999", "7.00")])     # after the day: unread
        so = self.w.obj("SalesOrderLine", {"outstanding_quantity": "30"})
        self.w.link("SalesOrderLine.for_item", so, self.items["SYN-10"])
        ctx = self.ctx()
        got = ea.purchase_order(ctx, items=["SYN-10"])
        # the items' last supplier is only suggested: the person names the supplier
        self.assertEqual((got["header"]["supplier"]["status"], got["header"]["supplier"]["value"]),
                         ("check", None))
        self.assertEqual([s["value"] for s in got["header"]["supplier"]["suggestions"]], [sup])
        self.assertEqual(got["lines"][0]["unit_price"]["status"], "check")
        got = ea.purchase_order(ctx, supplier=sup, items=["SYN-10"])
        self.assertEqual(got["header"]["supplier"]["status"], "cited")
        (ln,) = got["lines"]
        self.assertEqual((ln["unit_price"]["value"], ln["unit_price"]["source"]["record"]),
                         ("2.10", last))
        self.assertEqual((ln["quantity"]["status"], ln["quantity"]["value"]), ("check", None))
        self.assertEqual([s["value"] for s in ln["quantity"]["suggestions"]], ["30", "120"])

    def test_cardinality_a_demand_sum_is_suggested_only_while_every_line_is_cited(self):
        for n, shown in ((1, True), (20, True), (21, False), (60, False)):
            with self.subTest(n=n):
                self.setUp()
                self.items_known("SYN-10")
                sup = self.w.obj("Supplier", {"name": "Synth supplier"})
                self.po(sup, "2026-10-01", [("SYN-10", "120", "2.10")])
                for _ in range(n):
                    so = self.w.obj("SalesOrderLine", {"outstanding_quantity": "1"})
                    self.w.link("SalesOrderLine.for_item", so, self.items["SYN-10"])
                (ln,) = ea.purchase_order(self.ctx(), items=["SYN-10"])["lines"]
                demand = [s for s in ln["quantity"]["suggestions"]
                          if s["basis"] == "open sales order demand"]
                self.assertEqual(bool(demand), shown)
                if shown:
                    self.assertEqual((demand[0]["value"], len(demand[0]["records"])),
                                     (str(n), n))

    def test_a_supplier_alone_suggests_its_latest_items_and_two_suppliers_are_a_check(self):
        self.items_known("SYN-10", "SYN-20")
        a = self.w.obj("Supplier", {"name": "Synth A"})
        b = self.w.obj("Supplier", {"name": "Synth B"})
        self.po(a, "2026-10-01", [("SYN-10", "120", "2.10"), ("SYN-20", "5", "9")])
        self.po(b, "2026-10-02", [("SYN-10", "50", "2.50")])
        ctx = self.ctx()
        got = ea.purchase_order(ctx, supplier=a)
        self.assertEqual(sorted(ln["item"]["value"] for ln in got["lines"]),
                         ["SYN-10", "SYN-20"])
        both = ea.purchase_order(ctx, items=["SYN-10"])
        self.assertEqual(both["header"]["supplier"]["status"], "check")
        self.assertEqual([s["value"] for s in both["header"]["supplier"]["suggestions"]], [b])
        self.assertIn("of 2 suppliers", both["header"]["supplier"]["suggestions"][0]["basis"])
        self.assertEqual(both["lines"][0]["unit_price"]["status"], "check")
        none = ea.purchase_order(ctx, items=["ZZ-404"])
        self.assertEqual(none["header"]["supplier"]["status"], "check")
        # a named item no record holds stays on the PO as a check, never dropped
        self.assertEqual([(ln["item"]["status"], ln["item"]["value"]) for ln in none["lines"]],
                         [("check", None)])


class SupplierInvoiceAssist(Fixture):

    def ctx(self):
        self.build()
        st = pe._Store(self.dir / "s.sqlite", threads=1)
        self.addCleanup(st.close)
        return ap.Ctx(st, NOW.date(), time.monotonic() + 60, ())

    def test_an_invoice_prefills_vendor_number_date_total_and_its_po(self):
        sup = self.w.obj("Supplier", {"name": "Synth supplier", "rep_email": "ar@synth-supplier.example"})
        po = self.w.obj("PurchaseOrder", {"po_number": "4512", "order_date": "2026-09-01"})
        self.w.link("PurchaseOrder.ordered_from", po, sup)
        other = self.w.obj("Supplier", {"name": "Other synth"})
        twin = self.w.obj("PurchaseOrder", {"po_number": "4513", "order_date": "2026-09-01"})
        self.w.link("PurchaseOrder.ordered_from", twin, other)
        ctx = self.ctx()
        got = ea.supplier_invoice(ctx, sender="ar@synth-supplier.example", text=(
            "INVOICE # INV-77\nInvoice Date: Oct 3, 2026\nPO 4512\n"
            "SYN-10 Gallon soap 6 x 4.00 24.00\nTotal Due: $1,204.50"))
        h = got["header"]
        self.assertEqual({k: (f["status"], f["value"]) for k, f in h.items()}, {
            "vendor": ("cited", sup), "invoice_number": ("cited", "INV-77"),
            "invoice_date": ("cited", "2026-10-03"), "total": ("cited", "1204.50"),
            "purchase_order": ("cited", po)})
        # the same PO number from another vendor is never cited for this invoice
        other_po = ea.supplier_invoice(ctx, sender="ar@synth-supplier.example",
                                       text="Invoice # 9\nPO 4513\nTotal Due: 1.00")
        self.assertEqual(other_po["header"]["purchase_order"]["status"], "check")

    def test_an_ambiguous_date_two_totals_and_an_unknown_sender_are_checks(self):
        got = ea.supplier_invoice(self.ctx(), sender="x@unknown.example", text=(
            "Invoice No: 88\nInvoice Date: 10/03/2026\nTotal Due: 10.00\nAmount Due: 12.00"))
        h = got["header"]
        for k in ("vendor", "invoice_date", "total", "purchase_order"):
            self.assertEqual((h[k]["status"], h[k]["value"]), ("check", None), k)
        self.assertEqual(h["total"]["candidates"], ["10.00", "12.00"])
        self.assertEqual(h["invoice_number"]["value"], "88")


if __name__ == "__main__":
    unittest.main()
