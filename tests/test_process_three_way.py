"""plx.process three-way match and order-lifecycle stage evaluation (task process-3way).

Fixture worlds for the purchase graph (PO -> supplier confirmation -> receipt(s) ->
vendor invoice -> G/L) and the sales graph (SO -> production -> shipment ->
invoice), run through the real stage graphs (ontology/schema/process/*.json) and
the real compiled ontology schema, on the hand-filled projection store of
tests/test_ontology_tools_process.py. Every answer is validated against the
frozen plx.process contract. Records are synthetic; no real case id appears.
Evidence label: isolated-fixture.
"""

from __future__ import annotations

import copy
import json
import unittest
from unittest import mock

from operating import ontology_tools as ot
from operating import ontology_tools_process as op
from operating import ontology_tools_traverse as tt
from tests import test_ontology_tools_process as base

EACH, CASE, USD = "each", "case", "USD"
FIN = '["finance"]'
# A price carries the unit it is per as its unit (the projection reads each line's UoM
# through the schema's per_unit_from); the fixtures price every line per its own unit.


def setUpModule():
    base.setUpModule()


# --------------------------------------------------------------------------- worlds

def bc_purchase(s, key, *, ordered, receipts=(), invoices=(), gl=False):
    """A BC purchase order ``key``. ``ordered``: [(item, qty, unit, price)];
    ``receipts``: one [(item, qty, unit)] per receipt; ``invoices``: one
    (status, [(item, qty, unit, price)]) per invoice; ``gl``: G/L entries for each
    invoice. Returns the ids by role."""
    ids = {"items": {}, "receipt_lines": [], "invoice_lines": [], "invoices": [], "gl": []}

    def item(name):
        if name not in ids["items"]:
            ids["items"][name] = s.obj(f"Product:bc.items:{key}-{name}")
        return ids["items"][name]

    ids["po"] = po = s.obj(f"PurchaseOrder:bc.purchase_orders:{key}")
    s.val(po, "bc_status", "text", "Open")  # released: issued (a Draft is not)
    for n, (name, qty, unit, price) in enumerate(ordered):
        line = s.obj(f"PurchaseOrderLine:bc.purchase_order_lines:{key}-{n}")
        s.val(line, "ordered_quantity", "quantity", qty, unit=unit, role="ordered")
        if price is not None:
            s.val(line, "bc_direct_unit_cost", "money", price, unit=unit, currency=USD,
                  markings=FIN)
        s.link("PurchaseOrderLine.line_of", line, po)
        s.link("PurchaseOrderLine.for_item", line, item(name))
        ids.setdefault("po_lines", []).append(line)
    for r, rows in enumerate(receipts):
        rec = s.obj(f"Receipt:bc.purchase_receipts:{key}-r{r}")
        s.link("Receipt.receives", rec, po)
        ids.setdefault("receipts", []).append(rec)
        for n, (name, qty, unit) in enumerate(rows):
            line = s.obj(f"ReceiptLine:bc.purchase_receipt_lines:{key}-r{r}-{n}")
            s.val(line, "received_quantity", "quantity", qty, unit=unit, role="received")
            s.link("ReceiptLine.line_of", line, rec)
            s.link("ReceiptLine.for_item", line, item(name))
            ids["receipt_lines"].append(line)
    for i, (status, rows) in enumerate(invoices):
        inv = s.obj(f"PurchaseInvoice:bc.purchase_invoices:{key}-i{i}")
        s.val(inv, "status", "text", status)
        s.link("PurchaseInvoice.invoices", inv, po)
        ids["invoices"].append(inv)
        for n, (name, qty, unit, price) in enumerate(rows):
            line = s.obj(f"PurchaseInvoiceLine:bc.purchase_invoice_lines:{key}-i{i}-{n}")
            s.val(line, "invoiced_quantity", "quantity", qty, unit=unit, role="invoiced")
            s.val(line, "unit_cost", "money", price, unit=unit, currency=USD, markings=FIN)
            s.link("PurchaseInvoiceLine.line_of", line, inv)
            s.link("PurchaseInvoiceLine.for_item", line, item(name))
            ids["invoice_lines"].append(line)
        if gl:
            entry = s.obj(f"GeneralLedgerEntry:bc.general_ledger_entries:{key}-g{i}")
            s.link("GeneralLedgerEntry.for_purchase_invoice", entry, inv, tier="corroborated",
                   kind="same_record")
            ids["gl"].append(entry)
    return ids


def sage_multi_po_receipt(s, key="900"):
    """A Sage PO whose receipt header names it (Receipt.receives, capped at
    corroborated: one Sage receipt may cover several POs and its lines carry no PO
    key)."""
    po = s.obj(f"PurchaseOrder:portal.sage_po_header:{key}", "sage_bronze")
    line = s.obj(f"PurchaseOrderLine:portal.sage_po_detail:{key}-1", "sage_bronze")
    s.val(line, "ordered_quantity", "quantity", "100", unit=EACH, role="ordered")
    s.link("PurchaseOrderLine.line_of", line, po)
    rec = s.obj(f"Receipt:portal.sage_po_receipt_header:r{key}", "sage_bronze")
    s.link("Receipt.receives", rec, po, tier="corroborated")
    lines = []
    for n, qty in ((1, "70"), (2, "60")):  # 130 received on a receipt covering other POs too
        rl = s.obj(f"ReceiptLine:portal.sage_po_receipt_detail:r{key}-{n}", "sage_bronze")
        s.val(rl, "received_quantity", "quantity", qty, unit=EACH, role="received")
        s.link("ReceiptLine.line_of", rl, rec)
        lines.append(rl)
    return {"po": po, "receipt": rec, "receipt_lines": lines}


def gold_sales(s, key, *, ordered, shipments=(), invoices=(), mode="DISTRIBUTE",
               status_markings="[]", status="CONFIRMED"):
    """A gold sales order with a BC twin. ``ordered``: [(item, qty, unit, price)];
    ``shipments``: (status, [(item, qty, unit)]) per gold shipment; ``invoices``:
    (status, [(item, qty, unit, price)]) per BC invoice of the twin. Items: a gold
    product joined to its BC item by a verified identity link."""
    ids = {"items": {}, "shipments": [], "ship_lines": [], "invoices": [], "invoice_lines": []}

    def item(name, system):
        if name not in ids["items"]:
            gold = s.obj(f"Product:portal.app_product:{key}-{name}")
            bc = s.obj(f"Product:bc.items:{key}-{name}")
            s.link("Product.same_record_as", gold, bc, kind="same_record")
            ids["items"][name] = (gold, bc)
        return ids["items"][name][0 if system == "gold" else 1]

    ids["so"] = so = s.obj(f"SalesOrder:portal.app_sales_order:{key}", "gold_etl_from_fm")
    s.val(so, "status", "text", status)
    s.val(so, "fulfillment_mode", "enum", mode)
    for n, (name, qty, unit, price) in enumerate(ordered):
        line = s.obj(f"SalesOrderLine:portal.app_sales_order_line:{key}-{n}", "gold_etl_from_fm")
        s.val(line, "ordered_quantity", "quantity", qty, unit=unit, role="ordered")
        s.val(line, "unit_price", "money", price, unit=unit, currency=USD, markings=FIN)
        s.link("SalesOrderLine.line_of", line, so)
        s.link("SalesOrderLine.for_item", line, item(name, "gold"))
    for h, (status, rows) in enumerate(shipments):
        sh = s.obj(f"Shipment:portal.app_shipment:{key}-s{h}", "gold_etl_from_fm")
        s.val(sh, "status", "text", status, markings=status_markings)
        s.link("Shipment.ships", sh, so)
        ids["shipments"].append(sh)
        for n, (name, qty, unit) in enumerate(rows):
            line = s.obj(f"ShipmentLine:portal.app_shipment_line:{key}-s{h}-{n}",
                         "gold_etl_from_fm")
            s.val(line, "shipped_quantity", "quantity", qty, unit=unit, role="shipped")
            s.link("ShipmentLine.line_of", line, sh)
            s.link("ShipmentLine.for_item", line, item(name, "gold"))
            ids["ship_lines"].append(line)
    ids["bc_so"] = twin = s.obj(f"SalesOrder:bc.sales_orders:{key}")
    s.link("SalesOrder.same_record_as", so, twin, kind="same_record")
    for i, (status, rows) in enumerate(invoices):
        inv = s.obj(f"SalesInvoice:bc.sales_invoices:{key}-i{i}")
        s.val(inv, "status", "text", status)
        s.link("SalesInvoice.bills", inv, twin)
        ids["invoices"].append(inv)
        for n, (name, qty, unit, price) in enumerate(rows):
            line = s.obj(f"SalesInvoiceLine:bc.sales_invoice_lines:{key}-i{i}-{n}")
            s.val(line, "invoiced_quantity", "quantity", qty, unit=unit, role="invoiced")
            s.val(line, "unit_price", "money", price, unit=unit, currency=USD, markings=FIN)
            s.link("SalesInvoiceLine.line_of", line, inv)
            s.link("SalesInvoiceLine.for_item", line, item(name, "bc"))
            ids["invoice_lines"].append(line)
    return ids


def gold_purchase(s, key, *, ordered, received):
    """A gold (Portal) PO of one item with one receipt joined by its FK
    (Receipt.receives at stored_by_text_join, the binding's ceiling)."""
    item = s.obj(f"Product:portal.app_product:{key}-a")
    po = s.obj(f"PurchaseOrder:portal.app_purchase_order:{key}")
    s.val(po, "status", "text", "ORDERED")
    pol = s.obj(f"PurchaseOrderLine:portal.app_purchase_order_line:{key}-1")
    s.val(pol, "ordered_quantity", "quantity", ordered, unit=EACH, role="ordered")
    s.link("PurchaseOrderLine.line_of", pol, po)
    s.link("PurchaseOrderLine.for_item", pol, item)
    rec = s.obj(f"Receipt:portal.app_receipt:{key}-r")
    s.link("Receipt.receives", rec, po, tier="stored_by_text_join")
    rl = s.obj(f"ReceiptLine:portal.app_receipt_line:{key}-r-1")
    s.val(rl, "received_quantity", "quantity", received, unit=EACH, role="received")
    s.link("ReceiptLine.line_of", rl, rec)
    s.link("ReceiptLine.for_item", rl, item)
    return {"po": po, "receipt": rec, "item": item}


def sage_twin_shipment(s, ids, key, rows):
    """A Sage twin of a gold order (``gold_sales`` ids) with one shipment of
    [(item, qty, unit)]."""
    sage_so = s.obj(f"SalesOrder:portal.sage_oe_order_header:{key}", "sage_bronze")
    s.link("SalesOrder.same_record_as", ids["so"], sage_so, kind="same_record")
    sh = s.obj(f"Shipment:portal.sage_oe_shipment_header:{key}-s", "sage_bronze")
    s.link("Shipment.ships", sh, sage_so)
    for n, (name, qty, unit) in enumerate(rows):
        item = s.obj(f"Product:portal.sage_ic_item:{key}-{name}", "sage_bronze")
        s.link("Product.same_record_as", ids["items"][name][0], item, kind="same_record")
        line = s.obj(f"ShipmentLine:portal.sage_oe_shipment_detail:{key}-s-{n}", "sage_bronze")
        s.val(line, "shipped_quantity", "quantity", qty, unit=unit, role="shipped")
        s.link("ShipmentLine.line_of", line, sh)
        s.link("ShipmentLine.for_item", line, item)
    return sh


def record_received(s, ids, figures, unit=EACH):
    """The PO's lines' own received figure (PurchaseOrderLine.received_quantity, as
    BC receivedQuantity / Portal quantity_received roll receipts up), one per line."""
    for line, figure in zip(ids["po_lines"], figures):
        s.val(line, "received_quantity", "quantity", figure, unit=unit, role="received")


def figure_marked(marking):
    """PurchaseOrderLine's schema with its received figure declaring ``marking`` (the
    projector then marks every such value)."""
    spec = copy.deepcopy(base.SCHEMA.object_types["PurchaseOrderLine"])
    for prop in spec["properties"]:
        if prop["name"] == "received_quantity":
            prop["markings"] = [marking]
    return spec


def checks_by_roles(result):
    return {tuple(c["roles"]): c for c in result["quantity_checks"]}


def cited(check):
    return {v["citation"]["l1_handle"] for v in check["values"]}


def gap_reasons(result):
    return [g["reason"] for g in result["gaps"]]


class _Base(base.Mixin, unittest.TestCase):
    def run_po(self, store, root, graphs=None):
        # a PO is named: an open PO's unnamed call routes to po_delivery_window
        # (open_when, by the one PO status definition), and these are three-way checks
        args = {"from": root}
        if root.startswith("PurchaseOrder:"):
            args["process"] = "purchase_to_invoice"
        answer = self.answer(op.ProcessTools(base.tools(store).base, graphs or base.GRAPHS),
                             args)
        return answer, base.stages(answer.result), checks_by_roles(answer.result)


# --------------------------------------------------------------------------- purchase

class PurchaseThreeWayTests(_Base):
    def test_graph_is_po_confirmation_receipt_invoice_gl(self):
        graph = base.GRAPHS["purchase_to_invoice"]
        self.assertEqual([s["id"] for s in graph["stages"]],
                         ["purchase_order", "supplier_confirmation", "receipt", "vendor_invoice",
                          "gl_posting", "vendor_credit", "vendor_payment"])
        conf = graph["stages"][1]
        self.assertIs(conf["optional"], True)
        self.assertIn("runner.ts@c3ffda3", conf["source_note"])
        evidence = " ".join(graph["evidence"])
        self.assertIn("process_stage_map.json", evidence)
        self.assertIn("sage-multi-po-receipts", evidence)

    def test_full_match_every_stage_cited_and_every_comparison_agrees(self):
        s = base.Store()
        ids = bc_purchase(s, "po-full", ordered=[("a", "100", EACH, "2.50")],
                          receipts=[[("a", "100", EACH)]],
                          invoices=[("Paid", [("a", "100", EACH, "2.50")])], gl=True)
        answer, st, checks = self.run_po(s, ids["po"])
        self.assertEqual({k: v["state"] for k, v in st.items()},
                         {"purchase_order": "present", "supplier_confirmation": "missing",
                          "receipt": "present", "vendor_invoice": "present",
                          "gl_posting": "present", "vendor_credit": "missing",
                          "vendor_payment": "present"})
        self.assertIn(ids["gl"][0], st["gl_posting"]["events"])
        self.assertIn(ids["invoices"][0], st["vendor_invoice"]["events"])
        self.assertEqual(st["vendor_invoice"]["citations"][0]["l1_handle"],
                         f"bc://company_a/purchaseInvoices/po-full-i0@cap-1")
        # the confirmation is not recorded by Portal or BC: only documents carry it, and a
        # document source with item permissions never proves its absence (a typed gap)
        self.assertEqual((st["supplier_confirmation"]["missing"]["state"],
                          st["supplier_confirmation"]["missing"]["reason"]),
                         ("unknown", "absence_unproven"))
        self.assertIn("graph_mail", st["supplier_confirmation"]["missing"]["detail"])
        self.assertEqual(sorted(checks), [("ordered", "invoiced"), ("ordered", "received"),
                                          ("received", "invoiced")])
        for check in checks.values():
            self.assertIs(check["agree"], True, check)
        self.assertIn("ordered 100 each (1 lines) vs received 100 each (1 lines); "
                      "difference 0", checks[("ordered", "received")]["note"])
        self.assertEqual(cited(checks[("received", "invoiced")]),
                         {"bc://company_a/purchaseReceiptLines/po-full-r0-0@cap-1",
                          "bc://company_a/purchaseInvoiceLines/po-full-i0-0@cap-1"})
        self.assertIn("unit price 2.5 USD; exact", checks[("ordered", "invoiced")]["note"])
        self.assertIsNone(answer.result["current_stage"])  # optional stages never block
        self.assertFalse(any(c["finding"] for c in answer.roots[0]["checks"]))

    def test_partial_receipt_is_a_cited_one_sided_mismatch(self):
        s = base.Store()
        ids = bc_purchase(s, "po-part", ordered=[("a", "100", EACH, "2.50")],
                          receipts=[[("a", "60", EACH)]],
                          invoices=[("Open", [("a", "60", EACH, "2.50")])])
        answer, st, checks = self.run_po(s, ids["po"])
        qty = checks[("ordered", "received")]
        self.assertIs(qty["agree"], False)
        self.assertIn("ordered 100 each (1 lines) vs received 60 each (1 lines); "
                      "difference 40; tolerance 0.0001", qty["note"])
        self.assertEqual({v["quantity_role"] for v in qty["values"]}, {"ordered", "received"})
        self.assertEqual({v["value"] for v in qty["values"]}, {"100", "60"})
        self.assertIs(checks[("received", "invoiced")]["agree"], True)
        self.assertEqual(st["gl_posting"]["missing"]["state"], "absent_in_source")
        self.assertEqual(answer.result["current_stage"], "gl_posting")
        (finding,) = [c for c in answer.roots[0]["checks"] if c["finding"]]
        self.assertEqual(finding["item"]["roles"], ["ordered", "received"])

    def test_over_invoice_is_found_on_quantity_and_price(self):
        s = base.Store()
        ids = bc_purchase(s, "po-over", ordered=[("a", "100", EACH, "2.50")],
                          receipts=[[("a", "100", EACH)]],
                          invoices=[("Open", [("a", "120", EACH, "2.75")])])
        _, _, checks = self.run_po(s, ids["po"])
        self.assertIs(checks[("ordered", "received")]["agree"], True)
        inv = checks[("received", "invoiced")]
        self.assertIs(inv["agree"], False)
        self.assertIn("received 100 each (1 lines) vs invoiced 120 each (1 lines); "
                      "difference -20; exact", inv["note"])
        price = checks[("ordered", "invoiced")]
        self.assertIs(price["agree"], False)
        self.assertIn("ordered unit price 2.5 USD vs invoiced 2.75 USD; exact", price["note"])

    def test_prices_per_two_units_are_never_compared(self):
        """2.50 USD per each on the PO and 2.50 USD per case on the invoice are not one
        price: the check stays undecided with a gap, never agrees."""
        s = base.Store()
        ids = bc_purchase(s, "po-uom", ordered=[("a", "120", EACH, "2.50")],
                          receipts=[[("a", "120", EACH)]],
                          invoices=[("Open", [("a", "10", CASE, "2.50")])])
        answer, _, checks = self.run_po(s, ids["po"])
        price = checks[("ordered", "invoiced")]
        self.assertIsNone(price["agree"])
        self.assertIn("(cse, ea)", price["note"])
        self.assertIn("price_denominator_mismatch",
                      {g["reason"] for g in answer.result["gaps"]})

    def test_missing_confirmation_and_invoice_are_explicit_gaps(self):
        s = base.Store()
        ids = bc_purchase(s, "po-noconf", ordered=[("a", "10", EACH, "1")],
                          receipts=[[("a", "10", EACH)]])
        answer, st, checks = self.run_po(s, ids["po"])
        conf = st["supplier_confirmation"]
        self.assertEqual(conf["state"], "missing")
        self.assertEqual((conf["missing"]["state"], conf["missing"]["reason"]),
                         ("unknown", "absence_unproven"))
        self.assertIn("sharepoint", conf["missing"]["detail"])
        self.assertEqual(st["receipt"]["state"], "present")  # receiving never waits on it
        self.assertEqual((st["vendor_invoice"]["missing"]["state"],
                          st["vendor_invoice"]["missing"]["reason"]),
                         ("absent_in_source", "no_evidence_record"))
        self.assertEqual(answer.result["current_stage"], "vendor_invoice")
        self.assertIsNone(checks[("received", "invoiced")]["agree"])

    def test_served_confirmations_reach_the_stage_and_prove_its_absence(self):
        """Synthetic: SupplierConfirmation served by a stand-in binding (the future
        document projection). The shipped graph's declared paths reach it unchanged."""
        schema = served_confirmation_schema()
        graphs = op.load_graphs(schema=schema)
        s = base.Store()
        confirmed = bc_purchase(s, "po-c", ordered=[("a", "10", EACH, "1")],
                                receipts=[[("a", "10", EACH)]])
        bare = bc_purchase(s, "po-d", ordered=[("a", "10", EACH, "1")])
        conf = s.obj("SupplierConfirmation:fixture.supplier_confirmations:pi-1")
        s.link("SupplierConfirmation.confirms", conf, confirmed["po"], tier="corroborated",
               kind="mention")
        tools = op.ProcessTools(ot.OntologyTools(ot.StoreReader(s.conn), base.POLICY,
                                                 schema=schema, contracts=base.CONTRACTS,
                                                 clock=lambda: base.NOW), graphs)
        st = base.stages(self.answer(tools, {"from": confirmed["po"]}).result)
        self.assertEqual(st["supplier_confirmation"]["state"], "present")
        self.assertEqual(st["supplier_confirmation"]["events"], [conf])
        result = self.answer(tools, {"from": bare["po"]}).result
        st = base.stages(result)
        self.assertEqual((st["supplier_confirmation"]["missing"]["state"],
                          st["supplier_confirmation"]["missing"]["reason"]),
                         ("absent_in_source", "no_evidence_record"))
        self.assertEqual(result["current_stage"], "receipt")  # optional: never current

    def test_unit_mismatch_is_undecided_with_a_gap_and_never_converted(self):
        s = base.Store()
        ids = bc_purchase(s, "po-unit", ordered=[("a", "10", CASE, "24")],
                          receipts=[[("a", "120", EACH)]],
                          invoices=[("Open", [("a", "120", EACH, "2")])])
        answer, _, checks = self.run_po(s, ids["po"])
        qty = checks[("ordered", "received")]
        self.assertIsNone(qty["agree"])
        self.assertIn("(case, each)", qty["note"])
        self.assertEqual({(v["value"], v["unit"]) for v in qty["values"]},
                         {("10", CASE), ("120", EACH)})
        (gap,) = [g for g in answer.result["gaps"] if g["reason"] == "unit_mismatch"]
        self.assertEqual(gap["state"], "unknown")
        self.assertTrue(gap["subject"].startswith("ordered vs received "))
        self.assertIs(checks[("received", "invoiced")]["agree"], True)  # same unit there
        self.assertFalse(any(c["finding"] for c in answer.roots[0]["checks"]
                             if c["item"]["roles"] == ["ordered", "received"]))

    def test_several_receipts_of_one_po_are_summed_and_each_cited(self):
        s = base.Store()
        ids = bc_purchase(s, "po-multi", ordered=[("a", "100", EACH, "2.50")],
                          receipts=[[("a", "60", EACH)], [("a", "40", EACH)]],
                          invoices=[("Open", [("a", "60", EACH, "2.50")]),
                                    ("Open", [("a", "40", EACH, "2.50")])])
        answer, st, checks = self.run_po(s, ids["po"])
        self.assertEqual(len(st["receipt"]["events"]), 2)
        qty = checks[("ordered", "received")]
        self.assertIs(qty["agree"], True)
        self.assertIn("vs received 100 each (2 lines)", qty["note"])
        self.assertEqual(cited(qty) - {"bc://company_a/purchaseOrderLines/po-multi-0@cap-1"},
                         {"bc://company_a/purchaseReceiptLines/po-multi-r0-0@cap-1",
                          "bc://company_a/purchaseReceiptLines/po-multi-r1-0@cap-1"})
        self.assertIs(checks[("received", "invoiced")]["agree"], True)
        self.assertIn("vs invoiced 100 each (2 lines)", checks[("received", "invoiced")]["note"])

    def test_sage_receipt_covering_several_pos_is_cited_never_attributed(self):
        s = base.Store()
        ids = sage_multi_po_receipt(s)
        answer, st, checks = self.run_po(s, ids["po"])
        self.assertEqual(st["purchase_order"]["state"], "present")
        self.assertEqual(st["receipt"]["state"], "present")  # the receipt is real evidence
        self.assertIn(ids["receipt"], st["receipt"]["events"])
        qty = checks[("ordered", "received")]
        self.assertIsNone(qty["agree"])
        self.assertNotIn("130", qty["note"])  # the receipt total is never this PO's
        self.assertEqual(cited(qty) - {"api://sage.po_detail/900-1@0000000000000001-1"},
                         {f"api://sage.po_receipt_detail/r900-{n}@0000000000000001-1"
                          for n in (1, 2)})
        (gap,) = [g for g in answer.result["gaps"] if g["reason"] == "quantity_not_attributable"]
        self.assertEqual((gap["state"], gap["subject"]), ("unknown", "received"))
        self.assertIn("2 received line(s)", gap["detail"])
        self.assertIn("multipor", gap["detail"])

    def test_lead_invoice_row_leaves_the_three_way_sum_undecided(self):
        # A verified invoice 60 plus a text-lead invoice 40
        s = base.Store()
        ids = bc_purchase(s, "po-lead", ordered=[("a", "100", EACH, "2.50")],
                          receipts=[[("a", "100", EACH)]],
                          invoices=[("Open", [("a", "60", EACH, "2.50")])])
        lead = s.obj("PurchaseInvoice:bc.purchase_invoices:po-lead-x")
        s.val(lead, "status", "text", "Open")
        s.link("PurchaseInvoice.invoices", lead, ids["po"], tier="lead", kind="text_match")
        line = s.obj("PurchaseInvoiceLine:bc.purchase_invoice_lines:po-lead-x-0")
        s.val(line, "invoiced_quantity", "quantity", "40", unit=EACH, role="invoiced")
        s.link("PurchaseInvoiceLine.line_of", line, lead)
        s.link("PurchaseInvoiceLine.for_item", line, ids["items"]["a"])
        answer, _, checks = self.run_po(s, ids["po"])
        self.assertIsNone(checks[("received", "invoiced")]["agree"])
        self.assertIn("lead_evidence_not_counted", gap_reasons(answer.result))
        self.assertFalse(any(c["finding"] for c in answer.roots[0]["checks"]))

    def test_receipt_matched_by_key_and_by_text_counts_once_and_decides(self):
        # A gold receipt reaches its PO by FK and by po_reference
        # text; the lead to a record already counted blocks nothing
        s = base.Store()
        ids = gold_purchase(s, "gp-dup", ordered="100", received="60")
        s.link("Receipt.receives", ids["receipt"], ids["po"], tier="lead", kind="text_match")
        answer, _, checks = self.run_po(s, ids["po"])
        qty = checks[("ordered", "received")]
        self.assertIs(qty["agree"], False)
        self.assertIn("ordered 100 each (1 lines) vs received 60 each (1 lines)", qty["note"])
        self.assertNotIn("lead_evidence_not_counted", gap_reasons(answer.result))

    def test_draft_and_cancelled_invoices_do_not_count(self):
        s = base.Store()
        ids = bc_purchase(s, "po-drafts", ordered=[("a", "100", EACH, "2.50")],
                          receipts=[[("a", "100", EACH)]],
                          invoices=[("Open", [("a", "100", EACH, "2.50")]),
                                    ("Draft", [("a", "50", EACH, "2.50")]),
                                    ("Canceled", [("a", "100", EACH, "9.99")])])
        _, st, checks = self.run_po(s, ids["po"])
        self.assertEqual(st["vendor_invoice"]["state"], "present")
        inv = checks[("received", "invoiced")]
        self.assertIs(inv["agree"], True)
        self.assertIn("vs invoiced 100 each (1 lines)", inv["note"])
        self.assertNotIn("purchaseInvoiceLines/po-drafts-i1", json.dumps(inv["values"]))
        self.assertIs(checks[("ordered", "invoiced")]["agree"], True)  # 9.99 never compared

    def test_loader_checks_the_new_side_rules(self):
        bad = copy.deepcopy(base.GRAPHS["purchase_to_invoice"])
        sides = {x["role"]: x for x in bad["quantity_checks"][0]["sides"]}
        sides["received"]["not_attributable"][0]["from_binding"] = "bc.purchase_orders"
        sides["invoiced"]["ignore_through"][0]["object_type"] = "Shipment"
        problems = op.check_graph(bad, base.SCHEMA)
        self.assertTrue(any("is not a from-binding of Receipt.receives" in p for p in problems))
        self.assertTrue(any("ignore_through type Shipment is not on the side's paths" in p
                            for p in problems), problems)


def served_confirmation_schema():
    view = base.SCHEMA
    types = dict(view.object_types)
    types["SupplierConfirmation"] = dict(
        types["SupplierConfirmation"], availability={"state": "served"},
        bindings=[{"id": "fixture.supplier_confirmations", "source": "portal_api",
                   "relation": "fixture.supplier_confirmations", "key": ["id"]}])
    links = dict(view.link_types)
    links["SupplierConfirmation.confirms"] = dict(
        links["SupplierConfirmation.confirms"],
        bindings=[{"kind": "mention", "tier_ceiling": "corroborated",
                   "from_binding": "fixture.supplier_confirmations",
                   "to_binding": "bc.purchase_orders"}])
    base._RELATION["fixture.supplier_confirmations"] = ("portal_api",
                                                        "fixture.supplier_confirmations")
    return ot.SchemaView(types, links, view.event_types, view.action_types, view.digest)


# --------------------------------------------------------------------------- scoped completeness

PARTIAL_BC = [{"source": "portal_api", "captured_at": "2099-02-01T11:00:00Z",
               "completeness": "complete", "stale": False},
              {"source": "bc", "captured_at": "2099-02-01T11:00:00Z",
               "completeness": "partial", "stale": False}]


def po_claims(ids, *, drop=(), edit=None):
    """The claims ``integrations.bc_purchase.scope_claims`` makes of a complete per-PO
    read of ``ids`` (company_a): {relation: [claim]}; ``drop`` leaves relations out,
    ``edit`` {relation: changes} alters their claim."""
    native = {oid: oid.split(":", 2)[2] for oid in [ids["po"]] + ids["receipts"]
              + ids["invoices"] + ids["po_lines"] + ids["receipt_lines"]
              + ids["invoice_lines"]}
    claims = {"purchaseOrderLines": ([ids["po"]], ["documentId"]),
              "purchaseReceipts": ([ids["po"]], ["orderNumber"]),
              "purchaseReceiptLines": (ids["receipts"], ["documentId"]),
              "purchaseInvoices": ([ids["po"]], ["orderNumber", "orderId"]),
              "purchaseInvoiceLines": (ids["invoices"], ["documentId"]),
              "items": (ids["po_lines"] + ids["receipt_lines"] + ids["invoice_lines"],
                        ["number"])}
    out = {}
    for relation, (anchors, by) in claims.items():
        if relation in drop:
            continue
        claim = {"scope": "po=PO-SC", "company_id": "company_a",
                 "anchors": [native[a] for a in anchors], "by": by, "complete": True,
                 "read_at": "2099-02-01T11:00:00Z", **(edit or {}).get(relation, {})}
        out[relation] = {"last_refresh_at": "2099-02-01T11:00:00Z",
                         "last_full_traversal_at": None, "scopes": [claim]}
    return out


class ScopedCompletenessTests(_Base):
    """A side is decided over a per-PO read of BC
    only where a complete claim scoped to the records it
    starts from covers every hop; otherwise the gap names the cause."""

    def world(self, **claims):
        s = base.Store(freshness=PARTIAL_BC)
        ids = bc_purchase(s, "po-sc", ordered=[("a", "100.5", EACH, "2.50")],
                          receipts=[[("a", "100", EACH)]],
                          invoices=[("Open", [("a", "100", EACH, "2.50")])])
        s.conn.execute("INSERT INTO store_meta VALUES ('source_freshness', ?)",
                       (json.dumps({"bc": po_claims(ids, **claims)}),))
        return s, ids

    def test_a_fully_read_po_decides_both_sides_and_finds_the_shortfall(self):
        s, ids = self.world()
        answer, st, checks = self.run_po(s, ids["po"])
        short = checks[("ordered", "received")]
        self.assertIs(short["agree"], False)
        self.assertEqual({(v["quantity_role"], v["value"]) for v in short["values"]},
                         {("ordered", "100.5"), ("received", "100")})
        self.assertIn("difference 0.5", short["note"])
        self.assertEqual(cited(short), {"bc://company_a/purchaseOrderLines/po-sc-0@cap-1",
                                        "bc://company_a/purchaseReceiptLines/po-sc-r0-0@cap-1"})
        self.assertIs(checks[("received", "invoiced")]["agree"], True)
        # the scoped claim decides the sides; the family stays partial in every answer
        self.assertEqual({f["source"]: f["completeness"]
                          for f in answer.result["meta"]["freshness"]}, {"bc": "partial"})
        self.assertNotIn("scope_not_read", gap_reasons(answer.result))

    def test_an_unfinished_or_unscoped_read_keeps_the_side_undecided_with_its_cause(self):
        for claims, side, cause in (
                ({"edit": {"purchaseReceipts": {"complete": False}}}, "received",
                 "source_capture_incomplete"),
                ({"drop": ["purchaseReceiptLines"]}, "received", "scope_not_read"),
                # the invoices link joins orderId: an orderNumber-only read never covers it
                ({"edit": {"purchaseInvoices": {"by": ["orderNumber"]}}}, "invoiced",
                 "scope_not_read"),
                # a claim covers only the records it names
                ({"edit": {"purchaseReceipts": {"anchors": ["po-other"]}}}, "received",
                 "scope_not_read")):
            with self.subTest(claims=claims):
                s, ids = self.world(**claims)
                answer, _, checks = self.run_po(s, ids["po"])
                self.assertEqual({c["agree"] for roles, c in checks.items() if side in roles},
                                 {None})
                self.assertIn((side, cause), [(g["subject"], g["reason"])
                                              for g in answer.result["gaps"]])
                self.assertNotIn("evidence_not_fully_visible", gap_reasons(answer.result))
        # Permissions are unchanged: the claims never stand in for a grant. A grant of the
        # PO company's units the sides read decides as the full grant does (BC links never
        # cross companies); a grant missing one of them leaves that side withheld.
        s, ids = self.world()
        full = {roles: c["agree"] for roles, c in self.run_po(s, ids["po"])[2].items()}
        self.assertEqual(full[("ordered", "received")], False)  # the shortfall, decided
        for denied, expected in ((None, full), ("bc:company_a/purchaseReceiptLines", {
                roles: None if "received" in roles else agree for roles, agree in full.items()})):
            audiences = {"api:*": frozenset({"ops"}),
                         "bc:company_a/purchase*": frozenset({"ops"}),
                         "bc:company_a/items": frozenset({"ops"})}
            if denied:
                audiences[denied] = frozenset()
            policy = ot.ReadPolicy("plx", audiences, {"finance": frozenset({"ops"})})
            tools = op.ProcessTools(ot.OntologyTools(ot.StoreReader(s.conn), policy,
                                                     schema=base.SCHEMA,
                                                     contracts=base.CONTRACTS,
                                                     clock=lambda: base.NOW), base.GRAPHS)
            result = self.answer(tools, {"from": ids["po"]},
                                 actor=ot.Actor("plx", frozenset({"ops"}))).result
            self.assertEqual(checks_by_roles(result).keys(), full.keys())
            self.assertEqual({roles: c["agree"] for roles, c in checks_by_roles(result).items()},
                             expected, denied)
        self.assertIn(("received", "evidence_not_fully_visible"),
                      [(g["subject"], g["reason"]) for g in result["gaps"]])


    def test_absent_in_bc_needs_a_complete_zero_read_of_every_line(self):
        """A BC PO with no
        posted receipt or invoice has those stages absent_in_bc, with the read time, only
        when every line was read completely and records 0; anything less stays
        undecided."""
        def world(received="0", invoiced="0", **claims):
            s = base.Store(freshness=PARTIAL_BC)
            ids = bc_purchase(s, "po-ab", ordered=[("a", "100", EACH, "2.50")])
            ids.setdefault("receipts", [])
            if received is not None:
                record_received(s, ids, [received])
            if invoiced is not None:
                for line in ids["po_lines"]:
                    s.val(line, "invoiced_quantity", "quantity", invoiced, unit=EACH,
                          role="invoiced")
            s.conn.execute("INSERT INTO store_meta VALUES ('source_freshness', ?)",
                           (json.dumps({"bc": po_claims(ids, **claims)}),))
            return self.run_po(s, ids["po"])[1]

        st = world()
        for stage, figure in (("receipt", "received_quantity"),
                              ("vendor_invoice", "invoiced_quantity")):
            self.assertEqual(st[stage]["missing"]["reason"], "absent_in_bc", stage)
            self.assertIn(f"PurchaseOrderLine.{figure} 0", st[stage]["missing"]["detail"])
            self.assertIn("read completely at 2099-02-01T11:00:00Z",
                          st[stage]["missing"]["detail"])
            self.assertEqual({c["field_path"] for c in st[stage]["citations"]},
                             {f"row.{figure}"})
        # A non-zero figure is a conflict, an unread one is unproven; a figure missing
        # leaves the stage as it was before absent_by (no record, never absent_in_bc).
        for label, kwargs, stage, reason in (
                ("partly received", {"received": "40"}, "receipt",
                 "recorded_measure_conflict"),
                ("partly invoiced", {"invoiced": "40"}, "vendor_invoice",
                 "recorded_measure_conflict"),
                ("no received figure", {"received": None}, "receipt", "no_evidence_record"),
                ("no invoiced figure", {"invoiced": None}, "vendor_invoice",
                 "no_evidence_record"),
                ("lines read incompletely",
                 {"edit": {"purchaseOrderLines": {"complete": False}}}, "receipt",
                 "recorded_measure_unproven"),
                ("receipts read incompletely",
                 {"edit": {"purchaseReceipts": {"complete": False}}}, "receipt",
                 "absence_unproven"),
                ("invoices not read", {"drop": ["purchaseInvoices"]}, "vendor_invoice",
                 "absence_unproven")):
            with self.subTest(label):
                self.assertEqual(world(**kwargs)[stage]["missing"]["reason"], reason)

    def test_a_reviewed_link_to_another_company_keeps_a_one_company_reader_undecided(self):
        """The per-company grant rule rests on the projector pairing BC records within one
        company; a person-reviewed link has no such check. A company_b receipt reviewed
        onto a company_a PO: the full reader counts it, a company_a-only reader never
        decides without it (no decided False), and says why."""
        def company_b(s, object_id):
            otype, binding, native = object_id.split(":", 2)
            relation = base._relation(binding)[1]
            handle = f"bc://company_b/{relation}/{native}@cap-1"
            s.handles[object_id] = (handle, "bc_company_b")
            s.conn.execute("INSERT INTO objects VALUES (?,?,?,?,?,?,?)",
                           (object_id, otype, binding, json.dumps([native]),
                            f"bc:company_b/{relation}", base.T0, None))
            s.conn.execute("INSERT INTO object_versions VALUES (?,?,?,?,?,?,?)",
                           (object_id, handle, base._h(handle), "bc_company_b", "[]", base.T0,
                            None))
            if binding in base.BC_LINE_BINDINGS:
                s.val(object_id, "line_type", "text", "Item")
            return object_id

        policy = ot.ReadPolicy("plx", {"api:*": frozenset({"ops"}),
                                       "bc:company_a/*": frozenset({"ops"}),
                                       "bc:company_b/*": frozenset({"other"})},
                               {"finance": frozenset({"ops"})})
        for reviewed in (False, True):
            s = base.Store()
            ids = bc_purchase(s, "po-x", ordered=[("a", "100.5", EACH, None)],
                              receipts=[[("a", "100", EACH)]])
            if reviewed:
                receipt = company_b(s, "Receipt:bc.purchase_receipts:po-x-rB")
                line = company_b(s, "ReceiptLine:bc.purchase_receipt_lines:po-x-rB-0")
                s.val(line, "received_quantity", "quantity", "0.5", unit=EACH, role="received")
                s.conn.execute(
                    "INSERT INTO links(link_id, link_type, from_object, to_object, tier, "
                    "binding_kind, reason, why_not, evidence_handles, markings, recorded_at, "
                    "reviewed_by, reviewed_at, review_action_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("rvw-1", "Receipt.receives", receipt, ids["po"], "reviewed", "reviewed",
                     json.dumps([{"feature": "person_reviewed", "agrees": True}]), "[]",
                     json.dumps(sorted([s.handles[receipt][0], s.handles[ids["po"]][0]])),
                     "[]", base.T0, "employee:x", base.T0, "rev-1"))
                s.link("ReceiptLine.line_of", line, receipt)
                s.link("ReceiptLine.for_item", line, ids["items"]["a"])
            full = self.run_po(s, ids["po"])[2][("ordered", "received")]["agree"]
            tools = op.ProcessTools(ot.OntologyTools(ot.StoreReader(s.conn), policy,
                                                     schema=base.SCHEMA,
                                                     contracts=base.CONTRACTS,
                                                     clock=lambda: base.NOW), base.GRAPHS)
            result = self.answer(tools, {"from": ids["po"]},
                                 actor=ot.Actor("plx", frozenset({"ops"}))).result
            mine = checks_by_roles(result)[("ordered", "received")]["agree"]
            self.assertEqual((full, mine), (True, None) if reviewed else (False, False))
            self.assertEqual(("received", "evidence_not_fully_visible") in
                             [(g["subject"], g["reason"]) for g in result["gaps"]], reviewed)


# --------------------------------------------------------------------------- sales

class GoldPurchaseOriginTests(_Base):
    """find-po: which gold POs are real is the binding's upstream origin stamp
    (custom_fields.source of the silver ETL / FM mirror). A stamped row decides its
    stages; any other row (Portal-created, QA or demo, no stamp) stays a lead and every
    stage says which upstream marker it lacks, never silently decided."""

    def gold_po(self, lineage):
        """A gold PO of ``lineage`` with one line received in full (its records FM)."""
        s = base.Store()
        po = s.obj("PurchaseOrder:portal.app_purchase_order:gpo-1", lineage)
        s.val(po, "status", "text", "RECEIVED")
        line = s.obj("PurchaseOrderLine:portal.app_purchase_order_line:gpo-1-1")
        s.val(line, "ordered_quantity", "quantity", "40", unit=EACH, role="ordered")
        s.link("PurchaseOrderLine.line_of", line, po)
        rec = s.obj("Receipt:portal.app_receipt:gr-1")
        s.link("Receipt.receives", rec, po, tier="stored_by_text_join")
        rl = s.obj("ReceiptLine:portal.app_receipt_line:gr-1-1")
        s.val(rl, "received_quantity", "quantity", "40", unit=EACH, role="received")
        s.link("ReceiptLine.line_of", rl, rec)
        return self.run_po(s, po)

    def test_an_fm_stamped_gold_po_decides_its_stages(self):
        answer, st, _ = self.gold_po("gold_etl_from_fm")
        self.assertEqual((st["purchase_order"]["state"], st["receipt"]["state"]),
                         ("present", "present"))
        self.assertNotIn("root_lineage_staging", gap_reasons(answer.result))

    def test_an_unstamped_gold_po_labels_every_stage_with_the_missing_upstream_marker(self):
        answer, st, checks = self.gold_po("gold_native_staging")
        missing = st["purchase_order"]["missing"]
        self.assertEqual((st["purchase_order"]["state"], missing["state"], missing["reason"]),
                         ("missing", "unknown", "staging_root"))
        self.assertIn("authority only with custom_fields.source eq "
                      '"etl-silver-to-gold" (etl-silver-to-gold.ts@', missing["basis"])
        # Its receipt is reached only through a link touching the staging row: a lead.
        self.assertEqual((st["receipt"]["state"], st["receipt"]["missing"]["state"]),
                         ("missing", "unknown"))
        (gap,) = [g for g in answer.result["gaps"] if g["reason"] == "root_lineage_staging"]
        for cited in ('custom_fields.source eq "mirror-fm-pos-to-gold" '
                      "(mirror-fm-pos-to-gold.mjs@",
                      "a row with no marker is gold_native_staging",
                      'gold_native_staging markers po_number prefix "UAT-" (uat-prefix.ts@'):
            self.assertIn(cited, gap["basis"])
        # The detail is one plain sentence, never the code paths, never cut mid-word.
        self.assertEqual(gap["detail"], "This record comes from the Portal's staging data, "
                         "so every stage answer from it is a lead to confirm, not an "
                         "authority.")
        self.assertNotIn(".ts@", missing["detail"])
        self.assertTrue(all(c["agree"] is None for c in checks.values()))


class SalesFulfilmentTests(_Base):
    def run_so(self, store, root):
        return self.run_po(store, root)

    def test_full_match_through_shipment_and_bc_invoice(self):
        s = base.Store()
        ids = gold_sales(s, "so-full", ordered=[("a", "10", EACH, "5.00")],
                         shipments=[("SHIPPED", [("a", "10", EACH)])],
                         invoices=[("Open", [("a", "10", EACH, "5.00")])])
        answer, st, checks = self.run_so(s, ids["so"])
        self.assertEqual(answer.result["process"], "sales_order_lifecycle")
        self.assertEqual(st["sales_order"]["state"], "present")
        self.assertEqual(st["manufacturing_order"]["state"], "not_applicable")  # DISTRIBUTE
        self.assertEqual(st["shipment"]["state"], "present")
        self.assertEqual(st["customer_invoice"]["state"], "present")
        self.assertEqual(sorted(checks), [("ordered", "invoiced"), ("ordered", "shipped"),
                                          ("shipped", "invoiced")])
        for check in checks.values():
            self.assertIs(check["agree"], True, check)
        inv = checks[("shipped", "invoiced")]
        self.assertEqual(cited(inv), {"api://public.app_shipment_line/so-full-s0-0"
                                      "@0000000000000001-1",
                                      "bc://company_a/salesInvoiceLines/so-full-i0-0@cap-1"})
        self.assertIn("ordered and invoiced unit price 5 USD; exact",
                      checks[("ordered", "invoiced")]["note"])

    def test_short_shipment_is_a_mismatch(self):
        s = base.Store()
        ids = gold_sales(s, "so-short", ordered=[("a", "10", EACH, "5")],
                         shipments=[("SHIPPED", [("a", "6", EACH)])],
                         invoices=[("Open", [("a", "6", EACH, "5")])])
        answer, _, checks = self.run_so(s, ids["so"])
        ship = checks[("ordered", "shipped")]
        self.assertIs(ship["agree"], False)
        self.assertIn("ordered 10 each (1 lines) vs shipped 6 each (1 lines); difference 4; "
                      "exact", ship["note"])
        self.assertIs(checks[("shipped", "invoiced")]["agree"], True)
        self.assertTrue(any(c["finding"] for c in answer.roots[0]["checks"]))

    def test_over_invoice_against_shipped(self):
        s = base.Store()
        ids = gold_sales(s, "so-over", ordered=[("a", "10", EACH, "5")],
                         shipments=[("SHIPPED", [("a", "10", EACH)])],
                         invoices=[("Open", [("a", "12", EACH, "5")])])
        _, _, checks = self.run_so(s, ids["so"])
        inv = checks[("shipped", "invoiced")]
        self.assertIs(inv["agree"], False)
        self.assertIn("shipped 10 each (1 lines) vs invoiced 12 each (1 lines); difference -2",
                      inv["note"])

    def test_complete_and_legacy_open_orders_are_compared_never_read_as_drafts(self):
        # "Open" is the FileMaker-import status the Portal reads as CONFIRMED.
        for status in ("COMPLETE", "Open"):
            with self.subTest(status):
                s = base.Store()
                ids = gold_sales(s, "so-done", ordered=[("a", "10", EACH, "5")],
                                 shipments=[("SHIPPED", [("a", "6", EACH)])], status=status)
                answer, st, checks = self.run_so(s, ids["so"])
                self.assertEqual(st["sales_order"]["state"], "present")
                ship = checks[("ordered", "shipped")]
                self.assertIs(ship["agree"], False)  # the shortfall shows, never a draft
                self.assertIn("ordered 10 each (1 lines) vs shipped 6 each (1 lines); "
                              "difference 4", ship["note"])
                self.assertNotIn("side_draft", gap_reasons(answer.result))

    def test_a_cancelled_order_is_no_live_order_never_a_draft(self):
        s = base.Store()
        ids = gold_sales(s, "so-void", ordered=[("a", "10", EACH, "5")],
                         shipments=[("SHIPPED", [("a", "6", EACH)])], status="CANCELLED")
        answer, st, checks = self.run_so(s, ids["so"])
        self.assertEqual(st["sales_order"]["state"], "missing")
        self.assertIsNone(checks[("ordered", "shipped")]["agree"])
        self.assertNotIn("side_draft", gap_reasons(answer.result))

    def test_a_line_the_source_no_longer_returns_is_held_out_of_the_sum(self):
        s = base.Store()
        ids = gold_sales(s, "so-gone", ordered=[("a", "16", EACH, "5")],
                         shipments=[("SHIPPED", [("a", "10", EACH), ("a", "6", EACH)])])
        gone = ids["ship_lines"][0]
        s.conn.execute("UPDATE objects SET missing_from_source_since = ? WHERE object_id = ?",
                       (base.T0, gone))
        answer, _, checks = self.run_so(s, ids["so"])
        ship = checks[("ordered", "shipped")]
        self.assertIsNone(ship["agree"])  # never "shipped 16 (2 lines)" agreeing with 16
        self.assertNotIn("vs shipped 16", ship["note"])
        self.assertIn("no longer returned by the source", ship["note"])
        gap = next(g for g in answer.result["gaps"] if g["reason"] == "missing_from_source")
        self.assertEqual((gap["state"], gap["subject"]), ("unknown", "shipped"))
        self.assertIn(gone, gap["detail"])

    def test_cancelled_shipment_lines_do_not_count_as_portal_sums_them(self):
        s = base.Store()
        ids = gold_sales(s, "so-cx", ordered=[("a", "10", EACH, "5")],
                         shipments=[("SHIPPED", [("a", "10", EACH)]),
                                    ("CANCELLED", [("a", "10", EACH)])])
        _, _, checks = self.run_so(s, ids["so"])
        ship = checks[("ordered", "shipped")]
        self.assertIs(ship["agree"], True)
        self.assertIn("vs shipped 10 each (1 lines)", ship["note"])

    def test_unreadable_shipment_status_leaves_the_shipped_sums_undecided(self):
        s = base.Store()
        ids = gold_sales(s, "so-mark", ordered=[("a", "10", EACH, "5")],
                         shipments=[("SHIPPED", [("a", "10", EACH)])], status_markings=FIN)
        reader = ot.Actor("plx", frozenset({"ops"}))
        policy = ot.ReadPolicy("plx", {"api:*": frozenset({"ops"}), "bc:*": frozenset({"ops"})})
        tools = op.ProcessTools(ot.OntologyTools(ot.StoreReader(s.conn), policy,
                                                 schema=base.SCHEMA, contracts=base.CONTRACTS,
                                                 clock=lambda: base.NOW), base.GRAPHS)
        result = self.answer(tools, {"from": ids["so"]}, actor=reader).result
        for check in result["quantity_checks"]:
            if "shipped" in check["roles"]:
                self.assertIsNone(check["agree"])

    def test_unit_mismatch_on_shipment(self):
        s = base.Store()
        ids = gold_sales(s, "so-unit", ordered=[("a", "10", CASE, "5")],
                         shipments=[("SHIPPED", [("a", "120", EACH)])])
        answer, _, checks = self.run_so(s, ids["so"])
        self.assertIsNone(checks[("ordered", "shipped")]["agree"])
        self.assertIn("unit_mismatch", gap_reasons(answer.result))

    def test_shipment_without_lines_is_not_a_zero_shipment(self):
        s = base.Store()
        ids = gold_sales(s, "so-nolines", ordered=[("a", "10", EACH, "5")],
                         shipments=[("SHIPPED", [])])
        answer, st, checks = self.run_so(s, ids["so"])
        self.assertEqual(st["shipment"]["state"], "present")
        ship = checks[("ordered", "shipped")]
        self.assertIsNone(ship["agree"])
        self.assertIn("header_without_lines", gap_reasons(answer.result))


    def test_lead_invoice_on_the_path_leaves_the_sum_undecided(self):
        s = base.Store()
        ids = gold_sales(s, "so-lead", ordered=[("a", "10", EACH, "5")],
                         shipments=[("SHIPPED", [("a", "10", EACH)])],
                         invoices=[("Open", [("a", "6", EACH, "5")])])
        lead = s.obj("SalesInvoice:bc.sales_invoices:so-lead-x")
        s.val(lead, "status", "text", "Open")
        s.link("SalesInvoice.bills", lead, ids["so"], tier="lead", kind="text_match")
        line = s.obj("SalesInvoiceLine:bc.sales_invoice_lines:so-lead-x-0")
        s.val(line, "invoiced_quantity", "quantity", "4", unit=EACH, role="invoiced")
        s.link("SalesInvoiceLine.line_of", line, lead)
        s.link("SalesInvoiceLine.for_item", line, ids["items"]["a"][1])
        answer, _, checks = self.run_so(s, ids["so"])
        inv = checks[("shipped", "invoiced")]
        self.assertIsNone(inv["agree"])  # never "invoiced 6" as a confident finding
        (gap,) = [g for g in answer.result["gaps"] if g["reason"] == "lead_evidence_not_counted"]
        self.assertEqual(gap["subject"], "invoiced")
        self.assertIn("text", gap["detail"])
        self.assertFalse(any(c["finding"] for c in answer.roots[0]["checks"]))

    def test_sage_and_bc_twins_both_invoiced_are_a_conflict_never_a_sum(self):
        s = base.Store()
        ids = gold_sales(s, "so-two", ordered=[("a", "10", EACH, "5")],
                         shipments=[("SHIPPED", [("a", "10", EACH)])],
                         invoices=[("Open", [("a", "10", EACH, "5")])])
        sage_so = s.obj("SalesOrder:portal.sage_oe_order_header:so-two", "sage_bronze")
        s.link("SalesOrder.same_record_as", ids["so"], sage_so, kind="same_record")
        sage_item = s.obj("Product:portal.sage_ic_item:so-two-a", "sage_bronze")
        s.link("Product.same_record_as", ids["items"]["a"][0], sage_item, kind="same_record")
        inv = s.obj("SalesInvoice:portal.sage_oe_invoice_header:so-two-i", "sage_bronze")
        s.link("SalesInvoice.bills", inv, sage_so)
        line = s.obj("SalesInvoiceLine:portal.sage_oe_invoice_detail:so-two-i-0", "sage_bronze")
        s.val(line, "invoiced_quantity", "quantity", "10", unit=EACH, role="invoiced")
        s.link("SalesInvoiceLine.line_of", line, inv)
        s.link("SalesInvoiceLine.for_item", line, sage_item)
        answer, _, checks = self.run_so(s, ids["so"])
        inv_check = checks[("shipped", "invoiced")]
        self.assertIsNone(inv_check["agree"])
        self.assertNotIn("invoiced 20", inv_check["note"])
        self.assertIn("cross_system_conflict", gap_reasons(answer.result))
        self.assertFalse(any(c["finding"] for c in answer.roots[0]["checks"]))

    def test_an_invoice_header_without_lines_never_reads_as_zero(self):
        s = base.Store()
        ids = gold_sales(s, "so-hdr", ordered=[("a", "10", EACH, "5"), ("b", "5", EACH, "5")],
                         shipments=[("SHIPPED", [("a", "10", EACH), ("b", "5", EACH)])],
                         invoices=[("Open", [("a", "10", EACH, "5")]), ("Open", [])])
        answer, _, _ = self.run_so(s, ids["so"])
        for check in answer.result["quantity_checks"]:
            if "invoiced" in check["roles"]:
                self.assertIsNone(check["agree"], check)
                self.assertNotIn("invoiced 0", check["note"])
        (gap,) = [g for g in answer.result["gaps"] if g["reason"] == "header_without_lines"]
        self.assertEqual(gap["subject"], "invoiced")
        self.assertIn(ids["invoices"][1], gap["detail"])

    def test_ordered_vs_shipped_is_exact_and_status_cancel_matches_like_portal(self):
        graph = base.GRAPHS["sales_order_lifecycle"]
        (cmp,) = [c for c in graph["quantity_checks"][0]["compare"]
                  if c["roles"] == ["ordered", "shipped"]]
        self.assertEqual((cmp["tolerance"], cmp["rule"]), ("0", "covers"))
        s = base.Store()
        ids = gold_sales(s, "so-cxl", ordered=[("a", "10", EACH, "5")],
                         shipments=[("SHIPPED", [("a", "10", EACH)])],
                         invoices=[("Open", [("a", "10", EACH, "5")]),
                                   ("Cancelled by user", [("a", "10", EACH, "5")])])
        _, _, checks = self.run_so(s, ids["so"])
        self.assertIs(checks[("shipped", "invoiced")]["agree"], True)

    # One classification per record reached on a side

    def test_lead_to_an_invoice_already_counted_through_the_twin_blocks_nothing(self):
        # Portal writes the gold order number into a pushed BC invoice's
        # externalDocumentNumber: a text lead to the invoice the twin path counts
        s = base.Store()
        ids = gold_sales(s, "so-push", ordered=[("a", "10", EACH, "5")],
                         shipments=[("SHIPPED", [("a", "10", EACH)])],
                         invoices=[("Open", [("a", "10", EACH, "5")])])
        s.link("SalesInvoice.bills", ids["invoices"][0], ids["so"], tier="lead",
               kind="text_match")
        answer, st, checks = self.run_so(s, ids["so"])
        self.assertEqual(st["customer_invoice"]["state"], "present")
        for check in checks.values():
            self.assertIs(check["agree"], True, check)
        self.assertIn("shipped 10 each (1 lines) vs invoiced 10 each (1 lines)",
                      checks[("shipped", "invoiced")]["note"])
        self.assertNotIn("lead_evidence_not_counted", gap_reasons(answer.result))

    def test_lead_gap_names_only_records_reached_by_nothing_but_leads(self):
        s = base.Store()
        ids = gold_sales(s, "so-mix", ordered=[("a", "10", EACH, "5")],
                         shipments=[("SHIPPED", [("a", "10", EACH)])],
                         invoices=[("Open", [("a", "6", EACH, "5")])])
        s.link("SalesInvoice.bills", ids["invoices"][0], ids["so"], tier="lead",
               kind="text_match")
        lead = s.obj("SalesInvoice:bc.sales_invoices:so-mix-x")
        s.val(lead, "status", "text", "Open")
        s.link("SalesInvoice.bills", lead, ids["so"], tier="lead", kind="text_match")
        answer, _, checks = self.run_so(s, ids["so"])
        self.assertIsNone(checks[("shipped", "invoiced")]["agree"])
        (gap,) = [g for g in answer.result["gaps"] if g["reason"] == "lead_evidence_not_counted"]
        self.assertIn(lead, gap["detail"])
        self.assertNotIn(ids["invoices"][0], gap["detail"])  # counted: never "not counted"
        self.assertIn("1 record(s)", gap["detail"])

    def test_twin_never_replaces_an_own_shipment_header_without_lines(self):
        # a gold SHIPPED shipment whose lines were not captured, and a Sage twin
        # shipment of 6 against 10 ordered: undecided, never "short by 4"
        s = base.Store()
        ids = gold_sales(s, "so-gap", ordered=[("a", "10", EACH, "5")],
                         shipments=[("SHIPPED", [])])
        sage_twin_shipment(s, ids, "so-gap", [("a", "6", EACH)])
        answer, st, checks = self.run_so(s, ids["so"])
        self.assertEqual(st["shipment"]["state"], "present")
        self.assertIn(("ordered", "shipped"), checks)
        ship = checks[("ordered", "shipped")]
        self.assertIsNone(ship["agree"])
        self.assertNotIn("difference 4", ship["note"])
        self.assertNotIn("twin system", ship["note"])
        (gap,) = [g for g in answer.result["gaps"] if g["reason"] == "header_without_lines"]
        self.assertEqual(gap["subject"], "shipped")
        self.assertIn(ids["shipments"][0], gap["detail"])
        self.assertIn("twin system", gap["detail"])
        self.assertFalse(any(c["finding"] for c in answer.roots[0]["checks"]))

    # Shipped means shipped; the stage follows the twin

    def test_in_progress_shipments_are_never_summed_as_shipped(self):
        for status in ("PICKING", "DRAFT", "PACKED", "CONFIRMED"):
            with self.subTest(status=status):
                s = base.Store()
                ids = gold_sales(s, f"so-ip-{status}", ordered=[("a", "10", EACH, "5")],
                                 shipments=[("SHIPPED", [("a", "6", EACH)]),
                                            (status, [("a", "4", EACH)])],
                                 invoices=[("Open", [("a", "6", EACH, "5")])])
                answer, st, checks = self.run_so(s, ids["so"])
                self.assertEqual(st["shipment"]["state"], "present")
                ship = checks[("ordered", "shipped")]
                self.assertIs(ship["agree"], False)  # 4 have not left yet
                self.assertIn("vs shipped 6 each (1 lines)", ship["note"])
                self.assertIn("4 each", ship["note"])
                inv = checks[("shipped", "invoiced")]
                self.assertIs(inv["agree"], True)  # the invoice matches what shipped
                self.assertIn("shipped 6 each (1 lines) vs invoiced 6 each", inv["note"])
                (gap,) = [g for g in answer.result["gaps"]
                          if g["reason"] == "in_progress_not_counted"]
                self.assertEqual(gap["subject"], "shipped")
                self.assertIn(ids["shipments"][1], gap["detail"])
                self.assertNotIn(ids["shipments"][0], gap["detail"])

    def test_sage_only_shipment_reaches_the_stage_and_decides_from_the_twin(self):
        s = base.Store()
        ids = gold_sales(s, "so-sg", ordered=[("a", "10", EACH, "5")])
        sh = sage_twin_shipment(s, ids, "so-sg", [("a", "10", EACH)])
        answer, st, checks = self.run_so(s, ids["so"])
        self.assertEqual(st["shipment"]["state"], "present")
        self.assertIn(sh, st["shipment"]["events"])
        (detail,) = [x for x in answer.roots[0]["stages"] if x["stage"] == "shipment"]
        self.assertEqual(detail["records"], [sh])
        self.assertIn("twin system", detail["detail"])
        ship = checks[("ordered", "shipped")]
        self.assertIs(ship["agree"], True)
        self.assertIn("shipped figures come from the twin system", ship["note"])
        self.assertIn("api://sage.oe_shipment_detail/so-sg-s-0@0000000000000001-1", cited(ship))

    def test_cancelled_only_own_shipment_falls_back_to_the_twin_in_stage_and_sum(self):
        s = base.Store()
        ids = gold_sales(s, "so-cs", ordered=[("a", "10", EACH, "5")],
                         shipments=[("CANCELLED", [("a", "10", EACH)])])
        sage_twin_shipment(s, ids, "so-cs", [("a", "6", EACH)])
        _, st, checks = self.run_so(s, ids["so"])
        self.assertEqual(st["shipment"]["state"], "present")
        ship = checks[("ordered", "shipped")]
        self.assertIs(ship["agree"], False)
        self.assertIn("difference 4", ship["note"])
        self.assertIn("twin system", ship["note"])

    def test_an_own_in_progress_shipment_keeps_the_own_system_in_stage_and_sum(self):
        s = base.Store()
        ids = gold_sales(s, "so-pk", ordered=[("a", "10", EACH, "5")],
                         shipments=[("PICKING", [("a", "10", EACH)])])
        sage_twin_shipment(s, ids, "so-pk", [("a", "6", EACH)])
        _, st, checks = self.run_so(s, ids["so"])
        self.assertEqual(st["shipment"]["state"], "missing")  # its own system: not shipped
        ship = checks[("ordered", "shipped")]
        self.assertIsNone(ship["agree"])
        self.assertNotIn("twin system", ship["note"])

    def test_invoice_stage_reads_the_twin_only_when_the_own_paths_reach_no_record(self):
        graph = base.GRAPHS["sales_order_lifecycle"]
        (stage,) = [s for s in graph["stages"] if s["id"] == "customer_invoice"]
        twin = [[{"link": "SalesOrder.same_record_as", "direction": "forward"},
                 {"link": "SalesInvoice.bills", "direction": "reverse"}]]
        for spec in stage["evidence"]:
            self.assertEqual(spec["paths"], [[{"link": "SalesInvoice.bills",
                                               "direction": "reverse"}]])
            self.assertEqual(spec["fallback_paths"], twin)
        # Sage-only: the order's own paths reach nothing, its Sage twin is invoiced
        s = base.Store()
        ids = gold_sales(s, "so-si", ordered=[("a", "10", EACH, "5")],
                         shipments=[("SHIPPED", [("a", "10", EACH)])])
        sage_so = s.obj("SalesOrder:portal.sage_oe_order_header:so-si", "sage_bronze")
        s.link("SalesOrder.same_record_as", ids["so"], sage_so, kind="same_record")
        inv = s.obj("SalesInvoice:portal.sage_oe_invoice_header:so-si-i", "sage_bronze")
        s.link("SalesInvoice.bills", inv, sage_so)
        answer, st, _ = self.run_so(s, ids["so"])
        self.assertEqual(st["customer_invoice"]["state"], "present")
        (detail,) = [x for x in answer.roots[0]["stages"] if x["stage"] == "customer_invoice"]
        self.assertEqual(detail["records"], [inv])
        self.assertIn("twin system", detail["detail"])
        # an own (Sage root) invoice is read from the own paths, never as a twin
        s = base.Store()
        so = s.obj("SalesOrder:portal.sage_oe_order_header:so-own", "sage_bronze")
        inv = s.obj("SalesInvoice:portal.sage_oe_invoice_header:so-own-i", "sage_bronze")
        s.link("SalesInvoice.bills", inv, so)
        answer, st, _ = self.run_so(s, so)
        self.assertEqual(st["customer_invoice"]["state"], "present")
        (detail,) = [x for x in answer.roots[0]["stages"] if x["stage"] == "customer_invoice"]
        self.assertEqual(detail["records"], [inv])
        self.assertIsNone(detail["detail"])

    def test_own_lead_only_invoice_never_proves_absence_and_never_hides_the_twin(self):
        s = base.Store()
        ids = gold_sales(s, "so-ld", ordered=[("a", "10", EACH, "5")],
                         shipments=[("SHIPPED", [("a", "10", EACH)])])
        lead = s.obj("SalesInvoice:bc.sales_invoices:so-ld-x")
        s.val(lead, "status", "text", "Open")
        s.link("SalesInvoice.bills", lead, ids["so"], tier="lead", kind="text_match")
        _, st, _ = self.run_so(s, ids["so"])
        self.assertEqual((st["customer_invoice"]["state"],
                          st["customer_invoice"]["missing"]["state"]),
                         ("missing", "unknown"))  # the lead may be real: never absent
        inv = s.obj("SalesInvoice:bc.sales_invoices:so-ld-i0")
        s.val(inv, "status", "text", "Open")
        s.link("SalesInvoice.bills", inv, ids["bc_so"])
        _, st, _ = self.run_so(s, ids["so"])
        self.assertEqual(st["customer_invoice"]["state"], "present")

    def test_a_cancelled_invoice_reached_only_by_a_lead_blocks_nothing(self):
        s = base.Store()
        ids = gold_sales(s, "so-cl", ordered=[("a", "10", EACH, "5")],
                         shipments=[("SHIPPED", [("a", "10", EACH)])],
                         invoices=[("Open", [("a", "10", EACH, "5")])])
        lead = s.obj("SalesInvoice:bc.sales_invoices:so-cl-x")
        s.val(lead, "status", "text", "Canceled")
        s.link("SalesInvoice.bills", lead, ids["so"], tier="lead", kind="text_match")
        answer, _, checks = self.run_so(s, ids["so"])
        for check in checks.values():
            self.assertIs(check["agree"], True, check)
        self.assertNotIn("lead_evidence_not_counted", gap_reasons(answer.result))

    def test_lead_gap_is_not_claimed_for_a_reader_who_cannot_see_the_counted_path(self):
        s = base.Store()
        ids = gold_sales(s, "so-rr", ordered=[("a", "10", EACH, "5")],
                         shipments=[("SHIPPED", [("a", "10", EACH)])],
                         invoices=[("Open", [("a", "10", EACH, "5")])])
        s.link("SalesInvoice.bills", ids["invoices"][0], ids["so"], tier="lead",
               kind="text_match")
        reader = ot.Actor("plx", frozenset({"ops"}))
        policy = ot.ReadPolicy("plx", {"api:*": frozenset({"ops"}),
                                       "bc:company_a/salesInvoice*": frozenset({"ops"}),
                                       "bc:company_a/items": frozenset({"ops"})})
        tools = op.ProcessTools(ot.OntologyTools(ot.StoreReader(s.conn), policy,
                                                 schema=base.SCHEMA, contracts=base.CONTRACTS,
                                                 clock=lambda: base.NOW), base.GRAPHS)
        result = self.answer(tools, {"from": ids["so"]}, actor=reader).result
        reasons = gap_reasons(result)
        self.assertIn("evidence_not_fully_visible", reasons)
        self.assertNotIn("lead_evidence_not_counted", reasons)  # the full reader counts it
        for check in result["quantity_checks"]:
            if "invoiced" in check["roles"]:
                self.assertIsNone(check["agree"])

    def test_missing_shipment_and_invoice_are_gaps(self):
        s = base.Store()
        ids = gold_sales(s, "so-open", ordered=[("a", "10", EACH, "5")])
        answer, st, checks = self.run_so(s, ids["so"])
        # an FM-era order: FM shipments reach app_shipment with no sales_order_id, so no
        # shipment linked to it proves nothing (unknown, never "not shipped")
        self.assertEqual((st["shipment"]["missing"]["state"], st["shipment"]["missing"]["reason"]),
                         ("unknown", "absence_unproven"))
        self.assertIn("etl-silver-to-gold.ts@12db891", st["shipment"]["missing"]["detail"])
        self.assertEqual(st["customer_invoice"]["state"], "missing")
        self.assertIn(st["customer_invoice"]["missing"]["state"], ("absent_in_source", "unknown"))
        self.assertIsNone(checks[("ordered", "shipped")]["agree"])
        # the same order from the Sage-era ETL links its shipments: the absence is proven
        s.conn.execute("UPDATE object_versions SET lineage = 'gold_etl_from_sage' "
                       "WHERE object_id = ?", (ids["so"],))
        st = self.run_so(s, ids["so"])[1]
        self.assertEqual((st["shipment"]["missing"]["state"], st["shipment"]["missing"]["reason"]),
                         ("absent_in_source", "no_evidence_record"))


# --------------------------------------------------------------------------- monotonicity

# --------------------------------------------------------------------------- process-3way-2

class SystemOfRecordRuleTests(_Base):
    """Stages and quantity sides choose own or twin system by one rule."""

    def run_so(self, store, root):
        return self.run_po(store, root)

    def test_a_twin_holding_only_a_draft_is_read_as_the_draft_it_is(self):
        # before: the stage read the twin only when the twin was reached, so a twin
        # holding a draft invoice left the stage "no record" while the quantity rule
        # looked at the twin's records
        s = base.Store()
        ids = gold_sales(s, "so-td", ordered=[("a", "10", EACH, "5")],
                         shipments=[("SHIPPED", [("a", "10", EACH)])],
                         invoices=[("Draft", [("a", "10", EACH, "5")])])
        answer, st, checks = self.run_so(s, ids["so"])
        inv = st["customer_invoice"]
        self.assertEqual((inv["state"], inv["missing"]["state"], inv["missing"]["reason"]),
                         ("missing", "absent_in_source", "status_not_reached"))
        self.assertIn("twin system", inv["missing"]["detail"])
        (detail,) = [x for x in answer.roots[0]["stages"] if x["stage"] == "customer_invoice"]
        self.assertEqual(detail["records"], ids["invoices"])
        self.assertIn("side_draft", gap_reasons(answer.result))
        self.assertIsNone(checks[("shipped", "invoiced")]["agree"])


    def test_an_own_draft_beside_a_posted_twin_keeps_stage_and_side_on_the_own_system(self):
        # A draft is a record for the stage and the side alike
        s = base.Store()
        ids = gold_sales(s, "so-od", ordered=[("a", "10", EACH, "5")],
                         shipments=[("SHIPPED", [("a", "10", EACH)])],
                         invoices=[("Open", [("a", "10", EACH, "5")])])
        draft = s.obj("SalesInvoice:bc.sales_invoices:so-od-own")
        s.val(draft, "status", "text", "Draft")
        s.link("SalesInvoice.bills", draft, ids["so"])
        line = s.obj("SalesInvoiceLine:bc.sales_invoice_lines:so-od-own-0")
        s.val(line, "invoiced_quantity", "quantity", "10", unit=EACH, role="invoiced")
        s.link("SalesInvoiceLine.line_of", line, draft)
        s.link("SalesInvoiceLine.for_item", line, ids["items"]["a"][1])
        answer, st, checks = self.run_so(s, ids["so"])
        # the own draft decides the stage (reached only through a lead-ceiling binding,
        # so its status cannot prove absence), and the side reads that same system
        self.assertEqual(st["customer_invoice"]["missing"]["reason"], "partial_visibility")
        self.assertNotIn("twin system", st["customer_invoice"]["missing"]["detail"])
        inv = checks[("shipped", "invoiced")]
        self.assertIsNone(inv["agree"])
        self.assertNotIn("twin system", inv["note"])
        self.assertNotIn(f"bc://company_a/salesInvoiceLines/so-od-i0-0@cap-1", cited(inv))

    def test_loader_refuses_a_side_twin_its_stage_does_not_declare(self):
        graph = copy.deepcopy(base.GRAPHS["purchase_to_invoice"])
        (stage,) = [x for x in graph["stages"] if x["id"] == "receipt"]
        for spec in stage["evidence"]:
            spec["paths"] += spec.pop("fallback_paths")
        self.assertIn("side received has fallback_paths but stage receipt declares none",
                      " ".join(op.check_graph(graph, base.SCHEMA)))

    def test_a_portal_po_reads_its_twin_path_for_invoices_and_proves_none(self):
        # A Portal PO's own source carries no invoice path, so its
        # twin path is the only one; "received, not invoiced yet" stays proven
        for twin in ("none", "bc_without_invoice", "bc_invoiced"):
            s = base.Store()
            ids = gold_purchase(s, f"g-{twin}", ordered="100", received="100")
            if twin != "none":
                bc = s.obj(f"PurchaseOrder:bc.purchase_orders:g-{twin}-bc")
                s.link("PurchaseOrder.same_record_as", ids["po"], bc, kind="same_record")
            if twin == "bc_invoiced":
                inv = s.obj(f"PurchaseInvoice:bc.purchase_invoices:g-{twin}-i")
                s.val(inv, "status", "text", "Open")
                s.link("PurchaseInvoice.invoices", inv, bc)
            answer, st, _ = self.run_po(s, ids["po"])
            if twin == "bc_invoiced":
                self.assertEqual(st["vendor_invoice"]["state"], "present")
                self.assertEqual(answer.result["current_stage"], "gl_posting")
                continue
            for stage in ("vendor_invoice", "gl_posting"):
                missing = st[stage]["missing"]
                self.assertEqual((missing["state"], missing["reason"]),
                                 ("absent_in_source", "no_evidence_record"), (twin, stage))
            self.assertEqual(answer.result["current_stage"], "vendor_invoice", twin)

    def test_a_path_of_leads_never_proves_absence(self):
        # rule A: an FM order's only invoice path runs FM -> gold ->
        # SalesInvoice.bills, a lead from gold: unknown with or without the gold twin
        for twin in (False, True):
            s = base.Store()
            so = s.obj("SalesOrder:portal.fm_order_entry:fm-a", "fm_bronze")
            if twin:
                gold = s.obj("SalesOrder:portal.app_sales_order:fm-a-g", "gold_etl_from_fm")
                s.link("SalesOrder.same_record_as", so, gold, kind="same_record")
            answer = self.answer(op.ProcessTools(base.tools(s).base, base.GRAPHS),
                                 {"from": so, "process": "sales_order_lifecycle"})
            missing = base.stages(answer.result)["customer_invoice"]["missing"]
            self.assertEqual((missing["state"], missing["reason"]),
                             ("unknown", "not_applicable_to_source"), twin)
        t = op.ProcessTools(base.tools(base.Store()).base, base.GRAPHS)
        lead_path = [{"link": "SalesOrder.same_record_as", "direction": "forward"},
                     {"link": "SalesInvoice.bills", "direction": "reverse"}]
        self.assertFalse(t._countable(lead_path, "portal.fm_order_entry",
                                      op.DEFAULT_TIER_FLOOR, None))
        self.assertTrue(t._countable(lead_path[1:], "bc.sales_orders",
                                     op.DEFAULT_TIER_FLOOR, None))

    def test_a_twin_joined_only_by_a_lead_row_is_unproven_never_not_applicable(self):
        # The schema declares a counted twin path; this PO's data
        # reaches its BC twin only through a text lead, so the absence is unproven
        for invoiced in (False, True):
            s = base.Store()
            ids = gold_purchase(s, f"g-lt{int(invoiced)}", ordered="100", received="100")
            bc = s.obj(f"PurchaseOrder:bc.purchase_orders:g-lt{int(invoiced)}-bc")
            s.link("PurchaseOrder.same_record_as", ids["po"], bc, tier="lead",
                   kind="text_match")
            if invoiced:
                inv = s.obj("PurchaseInvoice:bc.purchase_invoices:g-lt1-i")
                s.val(inv, "status", "text", "Open")
                s.link("PurchaseInvoice.invoices", inv, bc)
            answer, st, _ = self.run_po(s, ids["po"])
            for stage in ("vendor_invoice", "gl_posting"):
                missing = st[stage]["missing"]
                self.assertEqual((missing["state"], missing["reason"]),
                                 ("unknown", "absence_unproven"), (invoiced, stage))
                self.assertIn("PurchaseOrder.same_record_as", missing["detail"])
            self.assertNotIn("current_stage", answer.result)

    def test_purchase_stages_read_the_twin_only_as_a_fallback(self):
        graph = base.GRAPHS["purchase_to_invoice"]
        for stage in graph["stages"]:
            for spec in stage["evidence"]:
                for path in spec["paths"]:
                    self.assertFalse(path and path[0]["link"] == "PurchaseOrder.same_record_as",
                                     stage["id"])
        self.assertTrue(all(spec.get("fallback_paths") for stage in graph["stages"]
                            if stage["id"] in ("receipt", "vendor_invoice")
                            for spec in stage["evidence"]))


class RecordedMeasureTests(_Base):
    """PurchaseOrderLine.received_quantity beside the receipt-line sum."""

    def test_graph_declares_the_po_line_figure_with_its_upstream_evidence(self):
        (check,) = base.GRAPHS["purchase_to_invoice"]["quantity_checks"]
        (side,) = [x for x in check["sides"] if x["role"] == "received"]
        self.assertEqual((side["recorded_on"]["role"], side["recorded_on"]["quantity"]),
                         ("ordered", "received_quantity"))
        self.assertIn("apply-po-line-receive.ts@0dab896", side["recorded_on"]["evidence"])
        graph = copy.deepcopy(base.GRAPHS["purchase_to_invoice"])
        (side,) = [x for x in graph["quantity_checks"][0]["sides"] if x["role"] == "received"]
        side["recorded_on"]["quantity"] = "ordered_quantity"
        self.assertIn("ordered_quantity is not a received quantity property",
                      " ".join(op.check_graph(graph, base.SCHEMA)))
        side["recorded_on"] = {"role": "received", "quantity": "received_quantity",
                               "evidence": "x"}
        self.assertIn("is recorded on received, which is not another side",
                      " ".join(op.check_graph(graph, base.SCHEMA)))

    def test_agreeing_figure_decides_as_before(self):
        s = base.Store()
        ids = bc_purchase(s, "po-ra", ordered=[("a", "100", EACH, "2")],
                          receipts=[[("a", "60", EACH)], [("a", "40", EACH)]],
                          invoices=[("Open", [("a", "100", EACH, "2")])])
        record_received(s, ids, ["100"])
        answer, _, checks = self.run_po(s, ids["po"])
        self.assertIs(checks[("ordered", "received")]["agree"], True)
        self.assertIs(checks[("received", "invoiced")]["agree"], True)
        self.assertFalse({"recorded_measure_conflict", "recorded_measure_unproven"}
                         & set(gap_reasons(answer.result)))

    def test_figure_differing_from_the_receipt_sum_is_a_conflict_citing_both(self):
        s = base.Store()
        ids = bc_purchase(s, "po-rc", ordered=[("a", "100", EACH, "2"), ("b", "5", EACH, "1")],
                          receipts=[[("a", "60", EACH)], [("a", "40", EACH)]],
                          invoices=[("Open", [("a", "100", EACH, "2")])])
        record_received(s, ids, ["90", "5"])
        answer, _, _ = self.run_po(s, ids["po"])
        gaps = {g["subject"]: g for g in answer.result["gaps"]
                if g["reason"] == "recorded_measure_conflict"}
        item_a, item_b = ids["items"]["a"], ids["items"]["b"]
        self.assertEqual(sorted(gaps), sorted([f"received {item_a}", f"received {item_b}"]))
        detail = gaps[f"received {item_a}"]["detail"]
        self.assertIn("PurchaseOrderLine lines record 90 each received (received_quantity: "
                      f"{ids['po_lines'][0]})", detail)
        self.assertIn("the received lines sum to 100 each", detail)
        self.assertIn("record 5 each received", gaps[f"received {item_b}"]["detail"])
        self.assertIn("sum to 0 each", gaps[f"received {item_b}"]["detail"])
        conflicts = {c["group"]: c for c in answer.roots[0]["conflicts"]}
        self.assertEqual((conflicts[item_a]["recorded"], conflicts[item_a]["summed"]),
                         ("90 each", "100 each"))
        rows = answer.roots[0]["checks"]
        for roles in (["ordered", "received"], ["received", "invoiced"]):
            (item,) = [c["item"] for c in rows if c["item"]["roles"] == roles
                       and c["group"] == item_a]
            self.assertIsNone(item["agree"], roles)
            self.assertIn("recorded_measure_conflict", item["note"])
            handles = {v["citation"]["l1_handle"] for v in item["values"]}
            self.assertIn("bc://company_a/purchaseReceiptLines/po-rc-r0-0@cap-1", handles)
            self.assertIn("bc://company_a/purchaseReceiptLines/po-rc-r1-0@cap-1", handles)
            # recorded once (the gap, and the cited conflict in roots), never per item
            self.assertNotIn("received_quantity",
                             {v["property"] for v in item["values"]
                              if "purchaseOrderLines" in v["citation"]["l1_handle"]})
        self.assertIn(("bc://company_a/purchaseOrderLines/po-rc-0@cap-1", "received_quantity"),
                      {(v["citation"]["l1_handle"], v["property"])
                       for v in conflicts[item_a]["values"]})
        (price,) = [c["item"] for c in rows if c["item"]["roles"] == ["ordered", "invoiced"]
                    and c["group"] == item_a]
        self.assertIs(price["agree"], True)  # prices are not that quantity
        self.assertFalse(any(c["finding"] for c in rows))

    def test_a_figure_beside_no_receipt_at_all_is_a_conflict_citing_it(self):
        # A PO line saying 100 received while no
        # receipt exists is never dropped
        s = base.Store()
        ids = bc_purchase(s, "po-r0", ordered=[("a", "100", EACH, "2")])
        record_received(s, ids, ["100"])
        answer, st, checks = self.run_po(s, ids["po"])
        # rule C: the figure unproves the absence, in the stage's
        # structured state, and the order's position is no longer certain
        missing = st["receipt"]["missing"]
        self.assertEqual((st["receipt"]["state"], missing["state"], missing["reason"]),
                         ("missing", "unknown", "recorded_measure_conflict"))
        self.assertIn("no received record exists, yet PurchaseOrderLine.received_quantity "
                      f"records 100 each received ({ids['po_lines'][0]})", missing["detail"])
        self.assertIn("bc://company_a/purchaseOrderLines/po-r0-0@cap-1",
                      {c["l1_handle"] for c in st["receipt"]["citations"]})
        self.assertNotIn("current_stage", answer.result)
        (gap,) = [g for g in answer.result["gaps"] if g["reason"] == "recorded_measure_conflict"]
        self.assertEqual(gap["subject"], f"received {ids['items']['a']}")
        self.assertIn(f"record 100 each received (received_quantity: {ids['po_lines'][0]}), yet "
                      "no received record exists (stage receipt is proven absent)", gap["detail"])
        qty = checks[("ordered", "received")]
        self.assertIsNone(qty["agree"])
        self.assertIn("recorded_measure_conflict", qty["note"])
        (conflict,) = answer.roots[0]["conflicts"]
        self.assertEqual((conflict["recorded"], conflict["summed"]),
                         ("100 each", "no received line"))
        self.assertEqual([v["citation"]["l1_handle"] for v in conflict["values"]],
                         ["bc://company_a/purchaseOrderLines/po-r0-0@cap-1"])
        s = base.Store()  # a zero figure beside no receipt: both say nothing was received
        ids = bc_purchase(s, "po-z0", ordered=[("a", "100", EACH, "2")])
        record_received(s, ids, ["0"])
        answer, st, _ = self.run_po(s, ids["po"])
        self.assertFalse({"recorded_measure_conflict", "recorded_measure_unproven"}
                         & set(gap_reasons(answer.result)))
        # a complete read of a zero figure proves it (2026-10-14-three-way-absence-proven)
        self.assertEqual(st["receipt"]["missing"]["reason"], "absent_in_bc")
        self.assertEqual(answer.result["current_stage"], "receipt")

    def test_the_figure_unproves_the_absence_before_requirements_are_read(self):
        graph = copy.deepcopy(base.GRAPHS["purchase_to_invoice"])
        (stage,) = [x for x in graph["stages"] if x["id"] == "vendor_invoice"]
        stage["requires"] = {"*": ["stage:purchase_order", "stage:receipt"]}
        graphs = dict(base.GRAPHS, purchase_to_invoice=graph)
        for figure, state, reason in (("100", "missing", "requirement_unproven"),
                                      ("0", "out_of_order", None)):
            s = base.Store()
            ids = bc_purchase(s, f"po-rq{figure}", ordered=[("a", "100", EACH, "2")],
                              invoices=[("Open", [("a", "100", EACH, "2")])])
            record_received(s, ids, [figure])
            _, st, _ = self.run_po(s, ids["po"], graphs)
            self.assertEqual((st["vendor_invoice"]["state"],
                              st["vendor_invoice"].get("missing", {}).get("reason")),
                             (state, reason), figure)

    def test_a_reader_who_cannot_read_the_figure_never_gets_the_absence(self):
        # a reader of every source but not of the (finance-marked) figure: a full-grant
        # reader sees the conflict, so this one must not get "proven absent"
        s = base.Store()
        ids = bc_purchase(s, "po-rm", ordered=[("a", "10", EACH, "2")])
        s.val(ids["po_lines"][0], "received_quantity", "quantity", "10", unit=EACH,
              role="received", markings=FIN)  # as the projector marks a marked property
        policy = ot.ReadPolicy("plx", {"api:*": frozenset({"staff", "buyer"}),
                                       "bc:*": frozenset({"staff", "buyer"})},
                               {"finance": frozenset({"staff"})})
        buyer = ot.Actor("plx", frozenset({"buyer"}))
        reasons = {}
        with mock.patch.dict(base.SCHEMA.object_types,
                             {"PurchaseOrderLine": figure_marked("finance")}):
            for label, actor in (("staff", base.STAFF), ("buyer", buyer)):
                tools = ot.OntologyTools(ot.StoreReader(s.conn), policy, schema=base.SCHEMA,
                                         contracts=base.CONTRACTS, clock=lambda: base.NOW)
                answer = self.answer(op.ProcessTools(tools, base.GRAPHS), {"from": ids["po"]},
                                     actor)
                missing = base.stages(answer.result)["receipt"]["missing"]
                self.assertEqual(missing["state"], "unknown")
                reasons[label] = missing["reason"]
        self.assertEqual(reasons, {"staff": "recorded_measure_conflict",
                                   "buyer": "recorded_measure_unproven"})

    def test_zero_against_non_zero_contradicts_in_any_unit(self):
        # A 0 bag figure beside 250 kg received, and its mirror
        for figure, kg, said in (("0", "250", "a non-zero quantity"), ("10", "0", "only zero")):
            s = base.Store()
            ids = bc_purchase(s, f"po-z{figure}", ordered=[("a", "10", "bag", "2")],
                              receipts=[[("a", kg, "kg")]])
            record_received(s, ids, [figure], unit="bag")
            answer, _, checks = self.run_po(s, ids["po"])
            qty = checks[("ordered", "received")]
            self.assertIsNone(qty["agree"], figure)
            self.assertNotIn("measured_by", qty)
            (gap,) = [g for g in answer.result["gaps"]
                      if g["reason"] == "recorded_measure_conflict"]
            self.assertIn(f"record {said} (kg)", gap["detail"])

    def test_lines_in_the_figure_unit_can_contradict_it_and_then_nothing_is_decided(self):
        # rule B: 12 bag on the lines already exceed the 8 bag figure
        s = base.Store()
        ids = bc_purchase(s, "po-mx", ordered=[("a", "10", "bag", "2")],
                          receipts=[[("a", "12", "bag"), ("a", "125", "kg")]])
        record_received(s, ids, ["8"], unit="bag")
        answer, _, checks = self.run_po(s, ids["po"])
        qty = checks[("ordered", "received")]
        self.assertIsNone(qty["agree"])
        self.assertNotIn("measured_by", qty)
        (gap,) = [g for g in answer.result["gaps"] if g["reason"] == "recorded_measure_conflict"]
        self.assertIn("sum to 12 bag", gap["detail"])
        s = base.Store()  # mixed and consistent: still never decided from the figure
        ids = bc_purchase(s, "po-mc", ordered=[("a", "10", "bag", "2")],
                          receipts=[[("a", "5", "bag"), ("a", "125", "kg")]])
        record_received(s, ids, ["10"], unit="bag")
        answer, _, checks = self.run_po(s, ids["po"])
        self.assertIsNone(checks[("ordered", "received")]["agree"])
        self.assertNotIn("measured_by", checks[("ordered", "received")])
        self.assertIn("recorded_measure_unproven", gap_reasons(answer.result))

    def test_many_conflicting_items_fit_and_the_cut_gap_list_says_so(self):
        # Items whose figure
        # differs (3 checks each, over the count cap) answer within max_result_bytes; a
        # stale-class gap (here the store built from another schema) stays listed first.
        # The checks fill the response, so n leaves room for one item gap beside them.
        # Each undecided check also carries its typed undecided_reason, so 2 x n of the
        # 20 listed checks are undecided and the rest are the agreeing ordered/invoiced.
        n = 7
        s = base.Store()
        s.conn.execute("UPDATE store_meta SET value = ? WHERE key = 'schema_digest'",
                       ("sha256:" + "0" * 64,))
        # short item names: the stages' plain readings (a few hundred bytes) fill the room
        names = [f"i{k}" for k in range(n)]
        # A paid invoice: its payment stage is present, no stage gap competes for room
        ids = bc_purchase(s, "po-big", ordered=[(x, "10", EACH, "2") for x in names],
                          receipts=[[(x, "10", EACH) for x in names]],
                          invoices=[("Paid", [(x, "10", EACH, "2") for x in names])])
        record_received(s, ids, ["9"] * n)
        answer, _, _ = self.run_po(s, ids["po"])  # validates the contract, no error
        size = len(json.dumps(answer.result, separators=(",", ":")).encode())
        self.assertLess(size, base.CONTRACTS["limits"]["max_result_bytes"])
        gaps = answer.result["gaps"]
        self.assertLessEqual(len(gaps), 20)
        self.assertEqual((gaps[0]["state"], gaps[0]["reason"]), ("stale", "schema_drift"))
        self.assertEqual(gaps[-1]["reason"], "gaps_truncated")  # with how many are not
        dropped = int(gaps[-1]["detail"].split()[0])
        self.assertEqual(dropped + len(gaps) - 1, 1 + n)  # the drift and one per item
        self.assertIn("recorded_measure_conflict", gap_reasons(answer.result))
        completeness = answer.result["completeness"]
        self.assertEqual((completeness["truncated"], completeness["complete"]), (True, False))
        self.assertIn(completeness["reason"], ("check_cap", "gap_cap"))  # 3 x n items
        self.assertEqual(sum(c["conflict"] for c in answer.roots[0]["conflicts"]), n)
        listed = answer.result["quantity_checks"]
        self.assertEqual(len(listed), 20)  # nothing dropped for size
        self.assertEqual(sum(item["agree"] is None for item in listed), 2 * n)
        for item in listed:  # each undecided one names the recorded conflict of its item
            if item["roles"] == ["ordered", "invoiced"]:  # 10 and 10: no figure between
                self.assertIs(item["agree"], True)
                self.assertNotIn("undecided_reason", item)
                continue
            self.assertIsNone(item["agree"])
            self.assertIn("recorded_measure_conflict", item["note"])
            self.assertEqual(item["undecided_reason"], "recorded_measure_conflict")

    def test_an_answer_over_the_byte_limit_is_cut_from_the_tail_never_refused(self):
        s = base.Store()
        names = [f"i{k:02d}" for k in range(6)]  # 18 items: under the count cap
        ids = bc_purchase(s, "po-fit", ordered=[(x, "10", EACH, "2") for x in names],
                          receipts=[[(x, "10", EACH) for x in names]],
                          invoices=[("Open", [(x, "10", EACH, "2") for x in names])])
        record_received(s, ids, ["9"] * len(names))
        tools = op.ProcessTools(base.tools(s).base, base.GRAPHS)
        tools.base.limits = dict(tools.base.limits, max_result_bytes=24000)
        answer = self.answer(tools, {"from": ids["po"]})
        result = answer.result
        self.assertLessEqual(ot._envelope_bytes(result), 24000)
        self.assertLess(len(result["quantity_checks"]), 3 * len(names))
        self.assertEqual(result["gaps"][-1]["reason"], "gaps_truncated")
        self.assertEqual((result["completeness"]["truncated"], result["completeness"]["reason"]),
                         (True, "result_size"))

    def test_findings_outlive_agreements_under_the_count_cap_and_the_size_cut(self):
        # One over-invoice on the last of 8 items (24 check items)
        s = base.Store()
        names = [f"i{k:02d}" for k in range(8)]
        ids = bc_purchase(s, "po-ord", ordered=[(x, "10", EACH, "2") for x in names],
                          receipts=[[(x, "10", EACH) for x in names]],
                          invoices=[("Open", [(x, "10" if x != "i07" else "12", EACH, "2")
                                              for x in names])])
        answer, _, _ = self.run_po(s, ids["po"])
        listed = answer.result["quantity_checks"]
        self.assertEqual(len(listed), 20)
        self.assertEqual(answer.result["completeness"]["reason"], "check_cap")
        self.assertIs(listed[0]["agree"], False)
        self.assertIn("Product:bc.items:po-ord-i07", listed[0]["note"])
        tools = op.ProcessTools(base.tools(s).base, base.GRAPHS)
        tools.base.limits = dict(tools.base.limits, max_result_bytes=12000)
        result = self.answer(tools, {"from": ids["po"]}).result
        self.assertLess(len(result["quantity_checks"]), 20)
        self.assertIs(result["quantity_checks"][0]["agree"], False)
        agrees = [c["agree"] for c in result["quantity_checks"]]
        self.assertEqual(agrees, sorted(agrees, key={False: 0, None: 1, True: 2}.get))

    def test_gaps_dropped_only_for_size_say_result_size(self):
        s = base.Store()
        names = [f"i{k:02d}" for k in range(4)]
        ids = bc_purchase(s, "po-gs", ordered=[(x, "10", EACH, "2") for x in names],
                          receipts=[[(x, "10", EACH) for x in names]])
        record_received(s, ids, ["9"] * len(names))
        tools = op.ProcessTools(base.tools(s).base, base.GRAPHS)
        full = self.answer(tools, {"from": ids["po"]}).result
        self.assertNotIn("reason", full["completeness"])
        size = ot._envelope_bytes(full)
        tools.base.limits = dict(tools.base.limits, max_result_bytes=size - 200)
        result = self.answer(tools, {"from": ids["po"]}).result
        self.assertEqual(len(result["quantity_checks"]), len(full["quantity_checks"]))
        self.assertEqual(result["gaps"][-1]["reason"], "gaps_truncated")
        self.assertEqual(result["completeness"]["reason"], "result_size")

    def test_receipts_in_another_unit_are_measured_by_the_po_line_figure(self):
        # The comparison names its measure and
        # keeps the gaps saying the receipt-line sum was not comparable
        for figure, agree in (("10", True), ("8", False)):
            s = base.Store()
            ids = bc_purchase(s, f"po-bk{figure}", ordered=[("a", "10", "bag", "2")],
                              receipts=[[("a", "250", "kg")]])
            record_received(s, ids, [figure], unit="BAG")
            answer, _, checks = self.run_po(s, ids["po"])
            qty = checks[("ordered", "received")]
            self.assertIs(qty["agree"], agree)
            self.assertEqual(qty["measured_by"], "po_line_recorded")
            self.assertIn(f"measured by po_line_recorded: ordered 10 BAG/bag vs received "
                          f"{figure} BAG/bag as PurchaseOrderLine.received_quantity records it",
                          qty["note"])
            self.assertIn(f"bc://company_a/purchaseOrderLines/po-bk{figure}-0@cap-1",
                          {v["citation"]["l1_handle"] for v in qty["values"]})
            reasons = gap_reasons(answer.result)
            self.assertIn("unit_mismatch", reasons)  # the receipt lines were not compared
            self.assertIn("recorded_measure_unproven", reasons)
        s = base.Store()  # a figure in yet another unit decides nothing
        ids = bc_purchase(s, "po-bkx", ordered=[("a", "10", "bag", "2")],
                          receipts=[[("a", "250", "kg")]])
        record_received(s, ids, ["10"], unit="drum")
        _, _, checks = self.run_po(s, ids["po"])
        self.assertIsNone(checks[("ordered", "received")]["agree"])
        self.assertNotIn("measured_by", checks[("ordered", "received")])

    def test_unit_labels_match_as_portal_and_are_never_converted(self):
        s = base.Store()
        ids = bc_purchase(s, "po-rp", ordered=[("a", "10", EACH, "2")],
                          receipts=[[("a", "10", EACH)]])
        record_received(s, ids, ["10"], unit="pcs")  # one unit, another label
        answer, _, checks = self.run_po(s, ids["po"])
        self.assertIs(checks[("ordered", "received")]["agree"], True)
        self.assertNotIn("recorded_measure_unproven", gap_reasons(answer.result))
        s = base.Store()
        ids = bc_purchase(s, "po-rk", ordered=[("a", "10", EACH, "2")],
                          receipts=[[("a", "10", EACH)]])
        record_received(s, ids, ["4"], unit="kg")
        answer, _, checks = self.run_po(s, ids["po"])
        self.assertIsNone(checks[("ordered", "received")]["agree"])
        (gap,) = [g for g in answer.result["gaps"] if g["reason"] == "recorded_measure_unproven"]
        self.assertIn("is in kg and the received lines in each", gap["detail"])
        self.assertNotIn("recorded_measure_conflict", gap_reasons(answer.result))

    def test_a_figure_on_only_some_lines_of_an_item_is_never_summed(self):
        s = base.Store()
        ids = bc_purchase(s, "po-rs", ordered=[("a", "6", EACH, "2"), ("a", "4", EACH, "2")],
                          receipts=[[("a", "10", EACH)]])
        record_received(s, ids, ["6"])
        answer, _, checks = self.run_po(s, ids["po"])
        self.assertIsNone(checks[("ordered", "received")]["agree"])
        (gap,) = [g for g in answer.result["gaps"] if g["reason"] == "recorded_measure_unproven"]


class UnitLabelTests(_Base):
    def test_units_compatible_uses_the_one_portal_fold(self):
        # ontology/units.py = Portal normalizeProductUom (product-uom.ts@fa46f2d) plus the
        # receiving unitsCompatible plurals (apply-po-line-receive.ts@6e76a5b)
        same = [("ea", "pcs"), ("EA", "each"), (" Pcs ", "unit"), ("pc", "units"),
                ("kg", "KGS"), ("kg", "Kilograms"), ("case", "CS"), ("bag", "BAG"),
                ("kg", "KG"), ("gr", "GR"), ("bag", "bags"), ("drum", "Drums")]
        for a, b in same:
            self.assertTrue(op._units_compatible(a, b), (a, b))
        other = [("ea", "kg"), ("pail", "pails"), ("roll", "rolls"), ("g", "GR"),
                 ("bag", "drum"), ("ea", "case"), ("", ""), (None, None), ("ea", None),
                 ("  ", "ea")]
        for a, b in other:
            self.assertFalse(op._units_compatible(a, b), (a, b))

    def test_ea_and_pcs_compare_ea_and_kg_do_not(self):
        s = base.Store()
        ids = bc_purchase(s, "po-ua", ordered=[("a", "10", "ea", "2")],
                          receipts=[[("a", "6", "pcs"), ("a", "4", "each")]])
        _, _, checks = self.run_po(s, ids["po"])
        qty = checks[("ordered", "received")]
        self.assertIs(qty["agree"], True)
        self.assertIn("ordered 10 ea/each/pcs (1 lines) vs received 10 ea/each/pcs", qty["note"])
        s = base.Store()
        ids = bc_purchase(s, "po-uk", ordered=[("a", "10", "ea", "2")],
                          receipts=[[("a", "10", "kg")]])
        answer, _, checks = self.run_po(s, ids["po"])
        self.assertIsNone(checks[("ordered", "received")]["agree"])
        self.assertIn("unit_mismatch", gap_reasons(answer.result))


    def test_kgs_and_kg_compare_gr_and_g_do_not(self):
        s = base.Store()
        ids = bc_purchase(s, "po-kg", ordered=[("a", "10", "kg", "2")],
                          receipts=[[("a", "6", "KGS"), ("a", "4", "Kilograms")]])
        _, _, checks = self.run_po(s, ids["po"])
        self.assertIs(checks[("ordered", "received")]["agree"], True)
        s = base.Store()
        ids = bc_purchase(s, "po-gr", ordered=[("a", "10", "g", "2")],
                          receipts=[[("a", "10", "GR")]])
        answer, _, checks = self.run_po(s, ids["po"])
        self.assertIsNone(checks[("ordered", "received")]["agree"])
        self.assertIn("unit_mismatch", gap_reasons(answer.result))


class TaxBasisPriceTests(_Base):
    """Prices on two tax bases, or an unknown one, never get a verdict (as plx.compare)."""

    def run_with(self, store, root, view):
        tools = ot.OntologyTools(ot.StoreReader(store.conn), base.POLICY, schema=view,
                                 contracts=base.CONTRACTS, clock=lambda: base.NOW)
        answer = self.answer(op.ProcessTools(tools, base.GRAPHS), {"from": root,
                                                                 "process": "purchase_to_invoice"})
        return answer, checks_by_roles(answer.result)

    def portal_po_bc_invoice(self, key, price, invoiced_price):
        """A Portal PO of FM lineage (line unit_price) invoiced by a BC invoice of its
        BC twin."""
        s = base.Store()
        item = s.obj(f"Product:bc.items:{key}-a")
        po = s.obj(f"PurchaseOrder:portal.app_purchase_order:{key}", "gold_etl_from_fm")
        s.val(po, "status", "text", "SUBMITTED")
        line = s.obj(f"PurchaseOrderLine:portal.app_purchase_order_line:{key}-0",
                     "gold_etl_from_fm")
        s.val(line, "ordered_quantity", "quantity", "10", unit=EACH, role="ordered")
        s.val(line, "unit_price", "money", price, unit=EACH, currency=USD, markings=FIN)
        s.link("PurchaseOrderLine.line_of", line, po)
        s.link("PurchaseOrderLine.for_item", line, item)
        twin = s.obj(f"PurchaseOrder:bc.purchase_orders:{key}-bc")  # its BC twin
        s.link("PurchaseOrder.same_record_as", po, twin, kind="same_record")
        inv = s.obj(f"PurchaseInvoice:bc.purchase_invoices:{key}-i")
        s.val(inv, "status", "text", "Open")
        s.link("PurchaseInvoice.invoices", inv, twin)
        il = s.obj(f"PurchaseInvoiceLine:bc.purchase_invoice_lines:{key}-i-0")
        s.val(il, "invoiced_quantity", "quantity", "10", unit=EACH, role="invoiced")
        s.val(il, "unit_cost", "money", invoiced_price, unit=EACH, currency=USD, markings=FIN)
        s.link("PurchaseInvoiceLine.line_of", il, inv)
        s.link("PurchaseInvoiceLine.for_item", il, item)
        return s, po

    def test_incl_tax_invoice_vs_excl_tax_fm_po_is_refused_naming_both(self):
        s, po = self.portal_po_bc_invoice("po-tx", "100", "113")
        # The invoice side declares incl_tax; the Portal FM-lineage line is excl_tax as
        # the shipped schema grounds it.
        view = base.with_price_basis(base.REAL_SCHEMA, base.GRAPHS, "incl_tax",
                                     only=[("PurchaseInvoiceLine", "unit_cost")])
        answer, checks = self.run_with(s, po, view)
        price = checks[("ordered", "invoiced")]
        self.assertIsNone(price["agree"])
        self.assertIn("tax bases excl_tax, incl_tax", price["note"])
        (gap,) = [g for g in answer.result["gaps"] if g["reason"] == "tax_basis_differs"]

    def test_the_shipped_schema_never_decides_against_an_unknown_basis(self):
        s, po = self.portal_po_bc_invoice("po-tu", "2.50", "2.60")
        answer, checks = self.run_with(s, po, base.REAL_SCHEMA)
        price = checks[("ordered", "invoiced")]
        self.assertIsNone(price["agree"])
        self.assertIn("tax bases excl_tax, unknown (unknown from "
                      "PurchaseInvoiceLine.unit_cost@bc.purchase_invoice_lines)", price["note"])
        self.assertIn("tax_basis_unknown", gap_reasons(answer.result))
        s = base.Store()  # BC to BC: two unknown columns may differ, so no verdict either
        ids = bc_purchase(s, "po-bb", ordered=[("a", "10", EACH, "2.50")],
                          invoices=[("Open", [("a", "10", EACH, "2.50")])])
        answer, checks = self.run_with(s, ids["po"], base.REAL_SCHEMA)
        self.assertIsNone(checks[("ordered", "invoiced")]["agree"])
        self.assertIn("tax_basis_unknown", gap_reasons(answer.result))

    def test_one_declared_basis_decides(self):
        s, po = self.portal_po_bc_invoice("po-te", "2.50", "2.60")
        view = base.with_price_basis(base.REAL_SCHEMA, base.GRAPHS, "excl_tax",
                                     only=[("PurchaseInvoiceLine", "unit_cost")])
        _, checks = self.run_with(s, po, view)
        price = checks[("ordered", "invoiced")]
        self.assertIs(price["agree"], False)
        self.assertIn("ordered unit price 2.5 USD vs invoiced 2.6 USD", price["note"])


class ThreeWayMonotonicityTests(unittest.TestCase):
    """The permission-monotonicity property of tests/test_ontology_tools_process.py
    (every answer equals the full-grant reader's or is undecided), over the worlds
    this task adds: several receipts, a multi-PO Sage receipt, ignored and unreadable
    headers, unit mismatch, sales fulfilment."""

    @staticmethod
    def worlds():
        def world(build):
            s = base.Store()
            return s, build(s)

        def purchase(s):
            a = bc_purchase(s, "m-a", ordered=[("a", "100", EACH, "2.50"), ("b", "5", CASE, "1")],
                            receipts=[[("a", "60", EACH)], [("a", "40", EACH), ("b", "60", EACH)]],
                            invoices=[("Open", [("a", "100", EACH, "2.60")]),
                                      ("Draft", [("a", "1", EACH, "2.50")])], gl=True)
            return [a["po"]]

        def marked_invoice(s):
            a = bc_purchase(s, "m-b", ordered=[("a", "10", EACH, "1")],
                            receipts=[[("a", "10", EACH)]],
                            invoices=[("Open", [("a", "10", EACH, "1")]),
                                      ("Canceled", [("a", "10", EACH, "1")])])
            s.conn.execute("UPDATE object_values SET markings = ? WHERE object_id = ? AND "
                           "property = 'status'", (FIN, a["invoices"][1]))
            return [a["po"]]

        def sage(s):
            return [sage_multi_po_receipt(s)["po"]]

        def sales(s):
            a = gold_sales(s, "m-s", ordered=[("a", "10", EACH, "5")],
                           shipments=[("SHIPPED", [("a", "6", EACH)]),
                                      ("CANCELLED", [("a", "4", EACH)])],
                           invoices=[("Open", [("a", "6", EACH, "5")])])
            b = gold_sales(s, "m-t", ordered=[("a", "10", EACH, "5")],
                           shipments=[("CANCELLED", [("a", "10", EACH)])],
                           status_markings=FIN)
            return [a["so"], b["so"]]

        def lead_and_twins(s):
            # A text-lead invoice, Sage and BC twins both invoiced,
            # and an invoice header without lines
            a = gold_sales(s, "m-l", ordered=[("a", "10", EACH, "5")],
                           shipments=[("SHIPPED", [("a", "10", EACH)])],
                           invoices=[("Open", [("a", "6", EACH, "5")])])
            lead = s.obj("SalesInvoice:bc.sales_invoices:m-l-x")
            s.val(lead, "status", "text", "Open")
            s.link("SalesInvoice.bills", lead, a["so"], tier="lead", kind="text_match")
            b = gold_sales(s, "m-w", ordered=[("a", "10", EACH, "5")],
                           shipments=[("SHIPPED", [("a", "10", EACH)])],
                           invoices=[("Open", [("a", "10", EACH, "5")])])
            sage_so = s.obj("SalesOrder:portal.sage_oe_order_header:m-w", "sage_bronze")
            s.link("SalesOrder.same_record_as", b["so"], sage_so, kind="same_record")
            sage_item = s.obj("Product:portal.sage_ic_item:m-w-a", "sage_bronze")
            s.link("Product.same_record_as", b["items"]["a"][0], sage_item, kind="same_record")
            inv = s.obj("SalesInvoice:portal.sage_oe_invoice_header:m-w-i", "sage_bronze")
            s.link("SalesInvoice.bills", inv, sage_so)
            line = s.obj("SalesInvoiceLine:portal.sage_oe_invoice_detail:m-w-i-0", "sage_bronze")
            s.val(line, "invoiced_quantity", "quantity", "10", unit=EACH, role="invoiced")
            s.link("SalesInvoiceLine.line_of", line, inv)
            s.link("SalesInvoiceLine.for_item", line, sage_item)
            c = gold_sales(s, "m-h", ordered=[("a", "10", EACH, "5"), ("b", "5", EACH, "5")],
                           shipments=[("SHIPPED", [("a", "10", EACH), ("b", "5", EACH)])],
                           invoices=[("Open", [("a", "10", EACH, "5")]), ("Open", [])])
            return [a["so"], b["so"], c["so"]]

        def purchase_lead(s):
            a = bc_purchase(s, "m-pl", ordered=[("a", "100", EACH, "2.50")],
                            receipts=[[("a", "100", EACH)]],
                            invoices=[("Open", [("a", "60", EACH, "2.50")])])
            lead = s.obj("PurchaseInvoice:bc.purchase_invoices:m-pl-x")
            s.val(lead, "status", "text", "Open")
            s.link("PurchaseInvoice.invoices", lead, a["po"], tier="lead", kind="text_match")
            return [a["po"]]

        def redundant_leads(s):
            # Leads to records also counted (pushed BC invoice,
            # gold receipt by FK and text) and a lineless gold shipment with a Sage twin
            a = gold_sales(s, "m-r", ordered=[("a", "10", EACH, "5")],
                           shipments=[("SHIPPED", [("a", "10", EACH)])],
                           invoices=[("Open", [("a", "10", EACH, "5")])])
            s.link("SalesInvoice.bills", a["invoices"][0], a["so"], tier="lead",
                   kind="text_match")
            b = gold_purchase(s, "m-gp", ordered="100", received="60")
            s.link("Receipt.receives", b["receipt"], b["po"], tier="lead", kind="text_match")
            return [a["so"], b["po"]]

        def lineless_with_twin(s):
            a = gold_sales(s, "m-n", ordered=[("a", "10", EACH, "5")],
                           shipments=[("SHIPPED", [])])
            sage_twin_shipment(s, a, "m-n", [("a", "6", EACH)])
            b = gold_sales(s, "m-o", ordered=[("a", "10", EACH, "5")])
            sage_twin_shipment(s, b, "m-o", [("a", "6", EACH)])
            return [a["so"], b["so"]]

        def shipped_means_shipped(s):
            # In-progress shipments, a Sage-only shipment, a
            # cancelled-only and a picking-only own shipment beside a Sage twin, and a
            # cancelled invoice reached only by a lead
            a = gold_sales(s, "m-ip", ordered=[("a", "10", EACH, "5")],
                           shipments=[("SHIPPED", [("a", "6", EACH)]),
                                      ("PICKING", [("a", "4", EACH)])],
                           invoices=[("Open", [("a", "6", EACH, "5")])], status_markings=FIN)
            b = gold_sales(s, "m-sg", ordered=[("a", "10", EACH, "5")])
            sage_twin_shipment(s, b, "m-sg", [("a", "10", EACH)])
            c = gold_sales(s, "m-cs", ordered=[("a", "10", EACH, "5")],
                           shipments=[("CANCELLED", [("a", "10", EACH)])], status_markings=FIN)
            sage_twin_shipment(s, c, "m-cs", [("a", "6", EACH)])
            d = gold_sales(s, "m-pk", ordered=[("a", "10", EACH, "5")],
                           shipments=[("DRAFT", [("a", "10", EACH)])])
            sage_twin_shipment(s, d, "m-pk", [("a", "10", EACH)])
            e = gold_sales(s, "m-cl", ordered=[("a", "10", EACH, "5")],
                           shipments=[("SHIPPED", [("a", "10", EACH)])],
                           invoices=[("Open", [("a", "10", EACH, "5")])])
            lead = s.obj("SalesInvoice:bc.sales_invoices:m-cl-x")
            s.val(lead, "status", "text", "Canceled")
            s.link("SalesInvoice.bills", lead, e["so"], tier="lead", kind="text_match")
            return [a["so"], b["so"], c["so"], d["so"], e["so"]]

        def recorded_measure(s):
            # process-3way-2: PO-line received figures that agree, conflict, sit on an
            # item with no receipt, and are in an incompatible unit
            a = bc_purchase(s, "m-rm", ordered=[("a", "100", EACH, "2"), ("b", "5", EACH, "1")],
                            receipts=[[("a", "60", EACH)], [("a", "40", EACH)]],
                            invoices=[("Open", [("a", "100", EACH, "2")])])
            record_received(s, a, ["90", "5"])
            b = bc_purchase(s, "m-ra", ordered=[("a", "10", EACH, "2")],
                            receipts=[[("a", "10", EACH)]],
                            invoices=[("Open", [("a", "10", EACH, "2")])])
            record_received(s, b, ["10"], unit="pcs")
            c = bc_purchase(s, "m-rk", ordered=[("a", "10", EACH, "2")],
                            receipts=[[("a", "10", EACH)]])
            record_received(s, c, ["4"], unit="kg")
            d = bc_purchase(s, "m-r0", ordered=[("a", "10", EACH, "2")])  # no receipt
            record_received(s, d, ["10"])
            # a Portal PO with no receipt beside its line figure, and a mixed-unit
            # receipt contradicting its figure
            g = s.obj("PurchaseOrder:portal.app_purchase_order:m-gr")
            s.val(g, "status", "text", "ORDERED")
            gl = s.obj("PurchaseOrderLine:portal.app_purchase_order_line:m-gr-1")
            s.val(gl, "ordered_quantity", "quantity", "10", unit=EACH, role="ordered")
            s.val(gl, "received_quantity", "quantity", "10", unit=EACH, role="received")
            s.link("PurchaseOrderLine.line_of", gl, g)
            m = bc_purchase(s, "m-mx", ordered=[("a", "10", "bag", "2")],
                            receipts=[[("a", "12", "bag"), ("a", "125", "kg")]])
            record_received(s, m, ["8"], unit="bag")
            z = bc_purchase(s, "m-zk", ordered=[("a", "10", "bag", "2")],
                            receipts=[[("a", "250", "kg")]])
            record_received(s, z, ["0"], unit="bag")  # zero against 250 kg
            e = bc_purchase(s, "m-bk", ordered=[("a", "10", "bag", "2")],
                            receipts=[[("a", "250", "kg")]],
                            invoices=[("Open", [("a", "10", "bag", "2")])])
            record_received(s, e, ["9"], unit="bag")  # decided by the figure
            return [a["po"], b["po"], c["po"], d["po"], e["po"], g, m["po"], z["po"]]

        def twin_draft(s):
            a = gold_sales(s, "m-td", ordered=[("a", "10", EACH, "5")],
                           shipments=[("SHIPPED", [("a", "10", EACH)])],
                           invoices=[("Draft", [("a", "10", EACH, "5")])])
            return [a["so"]]

        def invoice_twin(s):
            # the customer_invoice stage reads its twin only when its own paths reach
            # no record: a Sage-only invoice, and an own lead-only invoice
            a = gold_sales(s, "m-si", ordered=[("a", "10", EACH, "5")],
                           shipments=[("SHIPPED", [("a", "10", EACH)])])
            sage_so = s.obj("SalesOrder:portal.sage_oe_order_header:m-si", "sage_bronze")
            s.link("SalesOrder.same_record_as", a["so"], sage_so, kind="same_record")
            s.link("SalesInvoice.bills",
                   s.obj("SalesInvoice:portal.sage_oe_invoice_header:m-si-i", "sage_bronze"),
                   sage_so)
            b = gold_sales(s, "m-ld", ordered=[("a", "10", EACH, "5")],
                           shipments=[("SHIPPED", [("a", "10", EACH)])])
            lead = s.obj("SalesInvoice:bc.sales_invoices:m-ld-x")
            s.val(lead, "status", "text", "Open")
            s.link("SalesInvoice.bills", lead, b["so"], tier="lead", kind="text_match")
            return [a["so"], b["so"]]

        return {"three_way": world(purchase), "marked_cancel": world(marked_invoice),
                "sage_multi_po": world(sage), "sales_fulfilment": world(sales),
                "lead_and_twins": world(lead_and_twins), "purchase_lead": world(purchase_lead),
                "redundant_leads": world(redundant_leads),
                "lineless_with_twin": world(lineless_with_twin),
                "shipped_means_shipped": world(shipped_means_shipped),
                "invoice_twin": world(invoice_twin),
                "recorded_measure": world(recorded_measure), "twin_draft": world(twin_draft)}

    def test_a_marked_figure_keeps_every_answer_monotone(self):
        # rule C: with the PO-line figure marked, a reader who
        # cannot read it never gets the absence a full-grant reader sees contradicted
        checker = base.PermissionMonotonicityTests(
            "test_every_answer_is_the_full_grant_answer_or_undecided")
        worlds = {k: v for k, v in self.worlds().items() if k == "recorded_measure"}
        for store, _ in worlds.values():  # as the projector marks a marked property
            store.conn.execute("UPDATE object_values SET markings = '[\"l5\"]' WHERE "
                               "property = 'received_quantity' AND object_id LIKE "
                               "'PurchaseOrderLine:%'")
        with mock.patch.dict(base.SCHEMA.object_types,
                             {"PurchaseOrderLine": figure_marked("l5")}):
            checked, decided = checker.assert_monotone(worlds)
        self.assertGreater(checked, 1000)
        self.assertGreater(decided, 100)

    def test_every_answer_is_the_full_grant_answer_or_undecided(self):
        checker = base.PermissionMonotonicityTests(
            "test_every_answer_is_the_full_grant_answer_or_undecided")
        checked, decided = checker.assert_monotone(self.worlds())
        self.assertGreater(checked, 2000)
        self.assertGreater(decided, 500)


class WithholdingIsNeverAbsenceTests(_Base):
    """One store, one reader without the finance marking: its plx.around, plx.find
    filter and plx.process answers differ from the full reader's only by withholding
    (a subset, undecided, an ``unknown`` gap), never by an absence the full reader
    disproves."""

    FULL = ot.Actor("plx", frozenset({"staff", "finance"}))
    NARROW = ot.Actor("plx", frozenset({"staff"}))
    POLICY = ot.ReadPolicy("plx", {"api:*": frozenset({"staff"}), "bc:*": frozenset({"staff"})},
                           {"finance": frozenset({"finance"})})

    def assert_withheld_only(self, full_ids, narrow_ids, narrow_gaps, where):
        self.assertLessEqual(set(narrow_ids), set(full_ids), where)
        if set(narrow_ids) != set(full_ids):
            states = {g["state"] for g in narrow_gaps}
            self.assertNotIn("absent_in_source", states, where)
            self.assertIn("unknown", states, where)

    def assert_find_withheld_only(self, tools, asked, expected):
        """plx.find for both readers: the full reader finds ``expected``; the narrow
        answer is a subset that never claims absence nor a count of zero."""
        full, narrow = (tools.base.call("plx.find", asked, actor)
                        for actor in (self.FULL, self.NARROW))
        ids_of = lambda r: [c["object"]["object_id"] for c in r["candidates"]]  # noqa: E731
        self.assertEqual(ids_of(full), expected, asked)
        self.assert_withheld_only(ids_of(full), ids_of(narrow), narrow["gaps"], asked)
        if expected and not ids_of(narrow):
            self.assertIsNone(narrow["completeness"]["total"], asked)
            self.assertFalse(narrow["completeness"]["complete"], asked)

    def test_a_reader_without_a_marking_gets_withheld_answers_never_false_absences(self):
        s = base.Store()
        # plx.process: a price disagreement (2.5 vs 9.99), both prices finance-marked.
        ids = bc_purchase(s, "po-wh", ordered=[("a", "100", EACH, "2.5")],
                          receipts=[[("a", "100", EACH)]],
                          invoices=[("Open", [("a", "100", EACH, "9.99")])])
        # plx.around: a PO with one plain receipt and one finance-marked receipt, the
        # marked one posted to the item ledger.
        po = s.obj("PurchaseOrder:bc.purchase_orders:po-path")
        plain = s.obj("Receipt:bc.purchase_receipts:po-path-r1")
        marked = s.obj("Receipt:bc.purchase_receipts:po-path-r2", markings=FIN)
        entry = s.obj("ItemLedgerEntry:bc.item_ledger_entries:ile-1")
        for receipt in (plain, marked):
            s.link("Receipt.receives", receipt, po)
        s.link("ItemLedgerEntry.posted_by_receipt", entry, marked)
        # plx.find: a supplier item, a type the schema marks finance as a whole.
        supplier_item = s.obj("SupplierItem:portal.app_supplier_product:si-1", markings=FIN)
        s.val(supplier_item, "lead_time_days", "integer", "14")
        tools = op.ProcessTools(ot.OntologyTools(ot.StoreReader(s.conn), self.POLICY,
                                                 schema=base.SCHEMA, contracts=base.CONTRACTS,
                                                 clock=lambda: base.NOW), base.GRAPHS)
        traverse = tt.TraverseTools(tools.base)

        path = {"from": po, "path": [
            {"link_type": "Receipt.receives", "direction": "reverse"},
            {"link_type": "ItemLedgerEntry.posted_by_receipt", "direction": "reverse"}]}
        full, narrow = (traverse.call("plx.around", path, actor)
                        for actor in (self.FULL, self.NARROW))
        self.assertEqual([o["object_id"] for o in full["objects"]], [entry])
        self.assert_withheld_only([o["object_id"] for o in full["objects"]],
                                  [o["object_id"] for o in narrow["objects"]],
                                  narrow["gaps"], "plx.around")

        for op_, value in (("absent", None), ("gte", "5")):
            spec = {"property": "unit_cost", "op": op_, **({"value": value} if value else {})}
            self.assert_find_withheld_only(
                tools, {"object_type": "PurchaseInvoiceLine", "filters": [spec]},
                ids["invoice_lines"] if op_ == "gte" else [])
        self.assert_find_withheld_only(tools, {"object_type": "SupplierItem", "filters": [
            {"property": "lead_time_days", "op": "gte", "value": "10"}]}, [supplier_item])

        full, narrow = (self.answer(tools, {"from": ids["po"]}, actor=actor).result
                        for actor in (self.FULL, self.NARROW))
        price = ("ordered", "invoiced")
        self.assertIs(checks_by_roles(full)[price]["agree"], False)
        self.assertIn("2.5 USD vs invoiced 9.99 USD", checks_by_roles(full)[price]["note"])
        decided = {r: c["agree"] for r, c in checks_by_roles(narrow).items()
                   if c["agree"] is not None}
        for roles, check in checks_by_roles(full).items():
            if roles in decided:
                self.assertEqual(decided[roles], check["agree"], roles)
            else:   # undecided or not shown: an unknown gap says so, never silence
                self.assertIn("unknown", {g["state"] for g in narrow["gaps"]
                                          if g["subject"].startswith(" vs ".join(roles))},
                              roles)
        self.assertNotIn(price, decided)


if __name__ == "__main__":
    unittest.main()
