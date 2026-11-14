"""Lot impact: from a supplier lot over recorded firm-tier links only
to the purchase order and supplier it was received on, and to the customer orders the
assembly orders that consumed it are made or allocated for, with the ordered quantity
touched, a typed gap at each unrecorded hop and name-only (lead) matches kept as
candidates; the stored ``lot_impact`` investigate plan (F17), reached through
``plx.investigate``, refused to a reader without the quality_controlled marking.

Synthetic records over the real compiled schema (``tests.test_ontology_tools_process``
fixture); every investigate output is validated against the tool contracts. The live
check's report is checked against live check ``process.lot_impact`` from a
scan of this fixture. Evidence label: isolated-fixture.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import time
from decimal import Decimal
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from ontology.json_contract import validate
from operating import lot_impact as li
from operating import ontology_tools as ot
from operating import ontology_tools_investigate as oi
from operating import ontology_tools_process as op
from operating import ontology_tools_traverse as tt
from operating.tests.test_impact_chain import rows_of
from scripts import lot_impact_probe as probe
from tests import test_ontology_tools_process as tp

CONTRACTS = tp.CONTRACTS
STAFF, SALES = tp.STAFF, tp.SALES   # SALES holds no quality_controlled marking
PLAN = tp.ROOT / "ontology/plans/lot_impact.json"
QC = '["quality_controlled"]'

# The link types the next build serves (schema declarations as
# they stand there, evidence trimmed): the walk is tested over them before main's schema
# declares them. A declaration main gains must agree (test_overlay_agrees_with_the_schema).
NEXT_LINKS = {link["id"]: link for link in (
    {"id": "Batch.for_sales_order", "from": "Batch", "to": "SalesOrder",
     "name": "for_sales_order", "reverse_name": "batches", "cardinality": "N:1",
     "bindings": [{"kind": "fk", "tier_ceiling": "verified", "from_binding": "portal.fm_batch",
                   "to_binding": "portal.fm_order_entry",
                   "pairs": [{"from": "oelist_c", "to": "erpcode", "compare": "identifier"}]}]},
    {"id": "BatchSchedule.for_bulk_product", "from": "BatchSchedule", "to": "Product",
     "name": "for_bulk_product", "reverse_name": "batch_schedules", "cardinality": "N:1",
     "bindings": [{"kind": "fk", "tier_ceiling": "verified",
                   "from_binding": "portal.fm_batch_calibration",
                   "to_binding": "portal.app_product",
                   "pairs": [{"from": "bulkcode", "to": "code", "compare": "identifier"}]}]},
    {"id": "SalesInvoiceLine.of_sales_order", "from": "SalesInvoiceLine", "to": "SalesOrder",
     "name": "of_sales_order", "reverse_name": "sage_invoice_lines", "cardinality": "N:1",
     "bindings": [{"kind": "fk", "tier_ceiling": "verified",
                   "from_binding": "portal.sage_oe_invoice_detail",
                   "to_binding": "portal.sage_oe_order_header",
                   "pairs": [{"from": "ordnumber", "to": "ordnumber",
                              "compare": "identifier"}]}]},
    {"id": "SalesInvoiceLine.of_shipment", "from": "SalesInvoiceLine", "to": "Shipment",
     "name": "of_shipment", "reverse_name": "sage_invoice_lines", "cardinality": "N:1",
     "bindings": [{"kind": "fk", "tier_ceiling": "verified",
                   "from_binding": "portal.sage_oe_invoice_detail",
                   "to_binding": "portal.sage_oe_shipment_header",
                   "pairs": [{"from": "shinumber", "to": "shinumber",
                              "compare": "identifier"}]}]},
    {"id": "ShipmentLine.of_sales_order", "from": "ShipmentLine", "to": "SalesOrder",
     "name": "of_sales_order", "reverse_name": "sage_shipment_lines", "cardinality": "N:1",
     "bindings": [{"kind": "fk", "tier_ceiling": "verified",
                   "from_binding": "portal.sage_oe_shipment_detail",
                   "to_binding": "portal.sage_oe_order_header",
                   "pairs": [{"from": "ordnumber", "to": "ordnumber",
                              "compare": "identifier"}]}]})}
NEXT = None   # tp.SCHEMA with NEXT_LINKS (the schema's own declarations win)
# A served schema that does not declare them yet (main's before migration-next 85e9bc7dcc
# declared all five): the if_declared steps are skipped there, never guessed.
BEFORE = None


def setUpModule():
    global NEXT
    tp.setUpModule()
    global BEFORE
    NEXT = dataclasses.replace(tp.SCHEMA, link_types={**NEXT_LINKS, **tp.SCHEMA.link_types})
    BEFORE = dataclasses.replace(tp.SCHEMA, link_types={
        k: v for k, v in tp.SCHEMA.link_types.items() if k not in NEXT_LINKS})


def customer(s, system, key):
    """The fixture customer ``key`` as one system records it; the Sage and BC records are
    recorded as the same customer as the Portal's (verified same_record links)."""
    oid = {"portal": f"Customer:portal.app_customer:cu-{key}",
           "sage": f"Customer:portal.sage_ar_customer:scu-{key}",
           "bc": f"Customer:bc.customers:bcu-{key}"}[system]
    if oid not in s.handles:
        s.obj(oid)
        if system != "portal":
            s.link("Customer.same_record_as", customer(s, "portal", key), oid,
                   kind="same_record")
    return oid


def consumed(s, comp, lot, tier="reviewed"):
    """The lot-consumption link as a review records it (quality-controlled)."""
    s.link("AssemblyOrderComponent.consumed_lot", comp, lot, tier="lead", kind="text_match")
    s.conn.execute("UPDATE links SET markings = ? WHERE link_id = ?", (QC, f"lnk-{s.n}"))
    if tier == "reviewed":
        s.conn.execute("UPDATE links SET tier = 'reviewed', binding_kind = 'reviewed', "
                       "reviewed_by = 'qa@example.test', "
                       "reviewed_at = ?, review_action_id = ? WHERE link_id = ?",
                       (tp.T0, f"act-{s.n}", f"lnk-{s.n}"))


def lot_fixture(s, *, orders=2, key="la", qty=("40", "60"), without_ao=(), lead=False,
                ship_on=None, supplier=None, consumption=True):
    """Lot <- receipt line -> receipt -> PO -> supplier; per order n: FM component
    consuming the lot -> FM assembly order <- allocation -> FM order line -> FM order =
    Portal order <- shipment. ``without_ao``: components with no recorded assembly order;
    ``lead``: one more component matched to the lot by lot number only (lead tier), whose
    assembly order reaches its own order. ``ship_on``: order n -> where its shipment is
    recorded: "portal" (default), "bc" or "sage" (on a copy the Portal order links to, two
    links from the FileMaker order) or "none"; ``supplier``: the lot number's supplier key
    (default ``key``); ``consumption`` False: no consumption link (a review records it)."""
    ids = {"orders": [], "lines": [], "comps": [], "portal_orders": [], "shipments": []}
    lot = ids["lot"] = s.obj(f"Lot:portal.fm_receipt_raw_material.lotnoqc:rm-{key}",
                             "fm_bronze")
    s.val(lot, "lot_number", "identifier", f"LOT-{key.upper()}9" if supplier is None
          else "LOT-SHARED9", path="row.lotnoqc")
    line = ids["receipt_line"] = s.obj(f"ReceiptLine:portal.fm_receipt_raw_material:rm-{key}",
                                       "fm_bronze")
    receipt = s.obj(f"Receipt:portal.fm_receipt:r-{key}", "fm_bronze")
    pol = s.obj(f"PurchaseOrderLine:portal.fm_generated_purchase_order_item:pi-{key}",
                "fm_bronze")
    po = ids["po"] = s.obj(f"PurchaseOrder:portal.fm_generated_purchase_order:po-{key}",
                           "fm_bronze")
    sup = ids["supplier"] = f"Supplier:portal.fm_supplier:s-{supplier or key}"
    if sup not in s.handles:   # one supplier may deliver several lot records
        s.obj(sup, "fm_bronze")
    s.link("ReceiptLine.received_lot", line, lot)
    s.link("Lot.received_in", lot, receipt)
    s.link("ReceiptLine.line_of", line, receipt)
    s.link("ReceiptLine.receives_line", line, pol)
    s.link("PurchaseOrderLine.line_of", pol, po)
    s.link("PurchaseOrder.ordered_from", po, sup)
    s.link("Receipt.supplied_by", receipt, sup)

    def chain(n, tier="reviewed", quantity="1"):
        comp = s.obj(f"AssemblyOrderComponent:portal.fm_assembly_order_item:c-{key}-{n:03d}",
                     "fm_bronze")
        if consumption:
            consumed(s, comp, lot, tier)
        if n in without_ao:
            ids["comps"].append(comp)
            return None
        ao = s.obj(f"AssemblyOrder:portal.fm_assembly_order:ao-{key}-{n:03d}", "fm_bronze")
        s.link("AssemblyOrderComponent.component_of", comp, ao)
        alloc = s.obj(f"AssemblyOrderAllocation:portal.fm_order_entry_item_assembly:"
                      f"al-{key}-{n:03d}", "fm_bronze")
        s.link("AssemblyOrderAllocation.allocates", alloc, ao)
        sol = s.obj(f"SalesOrderLine:portal.fm_order_entry_item:oe-{key}-{n:03d}-1",
                    "fm_bronze")
        s.val(sol, "ordered_quantity", "quantity", quantity, role="ordered", path="row.qty")
        s.link("AssemblyOrderAllocation.for_order_line", alloc, sol)
        fm_so = s.obj(f"SalesOrder:portal.fm_order_entry:oe-{key}-{n:03d}", "fm_bronze")
        s.link("SalesOrderLine.line_of", sol, fm_so)
        so = s.obj(f"SalesOrder:portal.app_sales_order:so-{key}-{n:03d}")
        s.link("SalesOrder.same_record_as", so, fm_so, kind="same_record")
        s.link("SalesOrder.ordered_by", so, customer(s, "portal", key))
        where = (ship_on or {}).get(n, "portal")
        shipped = so
        if where == "bc":
            shipped = s.obj(f"SalesOrder:bc.sales_orders:bso-{key}-{n:03d}")
            s.link("SalesOrder.same_record_as", so, shipped, kind="same_record")
            ship = s.obj(f"Shipment:bc.sales_shipments:bsh-{key}-{n:03d}")
        elif where == "sage":
            shipped = s.obj(f"SalesOrder:portal.sage_oe_order_header:sso-{key}-{n:03d}")
            s.link("SalesOrder.same_record_as", so, shipped, kind="same_record")
            ship = s.obj(f"Shipment:portal.sage_oe_shipment_header:ssh-{key}-{n:03d}")
        else:
            ship = s.obj(f"Shipment:portal.app_shipment:sh-{key}-{n:03d}")
        if where != "none":
            s.link("Shipment.ships", ship, shipped)
            s.link("Shipment.shipped_to", ship,
                   customer(s, where if where in ("bc", "sage") else "portal", key))
            ids["shipments"].append(ship)
        else:
            ids["shipments"].append(None)
        ids.setdefault("copies", []).append(shipped)
        ids["comps"].append(comp)
        ids["lines"].append(sol)
        ids["portal_orders"].append(so)
        return fm_so

    for n in range(orders):
        fm_so = chain(n, quantity=qty[n] if n < len(qty) else "1")
        if fm_so is not None:
            ids["orders"].append(fm_so)
    if lead:
        ids["candidate_order"] = chain(900, tier="lead", quantity="7")
        ids["candidate"] = ids["comps"].pop()
        for k in ("lines", "portal_orders", "shipments", "copies"):
            ids[k].pop()
    return ids


def batch_fixture(s, *, key="ba", allocated=(), direct=None, filled=None, invoiced=None,
                  ship_on=None, after_build=True):
    """A production batch (FileMaker record and the batch record it was upserted into) that
    reaches customer orders, all synthetic:
    ``direct`` n: the batch's OE list names FM order n (Batch.for_sales_order, after build);
    ``filled`` n: an FM assembly order filled from the batch is allocated to a line of order n;
    ``allocated``: FM order-entry batch allocations to a line of each order n.
    Each FM order n is the Portal order n, which is the Sage order n. ``ship_on`` n ->
    "line" (a Sage shipment line of the Sage order, after build), "invoice" (a Sage invoice
    line of the Sage order billing the shipment, after build), "portal" (the Portal
    shipment header) or "none" (default). ``after_build`` False: no after-build link."""
    ids = {"orders": {}, "lines": {}, "shipments": {}}
    fm = ids["batch"] = s.obj(f"Batch:portal.fm_batch:b-{key}", "fm_bronze")
    s.val(fm, "lot_number", "text", f"BL-{key.upper()}7", path="row.lotno_c")
    gold = ids["gold"] = s.obj(f"Batch:portal.app_batch_record:g-{key}")
    s.link("Batch.same_record_as", fm, gold, kind="same_record")
    sched = ids["schedule"] = s.obj(f"BatchSchedule:portal.fm_batch_calibration:q-{key}",
                                    "fm_bronze")
    s.link("BatchSchedule.for_batch", sched, fm)
    ids["product"] = s.obj(f"Product:portal.app_product:p-{key}")
    if after_build:
        s.link("BatchSchedule.for_bulk_product", sched, ids["product"])

    def order(n, quantity):
        if n in ids["orders"]:
            return ids["lines"][n]
        fm_so = ids["orders"][n] = s.obj(f"SalesOrder:portal.fm_order_entry:oe-{key}-{n}",
                                         "fm_bronze")
        sol = ids["lines"][n] = s.obj(f"SalesOrderLine:portal.fm_order_entry_item:"
                                      f"oe-{key}-{n}-1", "fm_bronze")
        s.val(sol, "ordered_quantity", "quantity", quantity, role="ordered", path="row.qty")
        s.link("SalesOrderLine.line_of", sol, fm_so)
        so = s.obj(f"SalesOrder:portal.app_sales_order:so-{key}-{n}")
        s.link("SalesOrder.same_record_as", so, fm_so, kind="same_record")
        sage = s.obj(f"SalesOrder:portal.sage_oe_order_header:sso-{key}-{n}")
        s.link("SalesOrder.same_record_as", so, sage, kind="same_record")
        s.link("SalesOrder.ordered_by", so, customer(s, "portal", key))
        where = (ship_on or {}).get(n, "none")
        ship = None
        if where == "portal":
            ship = s.obj(f"Shipment:portal.app_shipment:sh-{key}-{n}")
            s.link("Shipment.ships", ship, so)
        elif where in ("line", "invoice") and after_build:
            ship = s.obj(f"Shipment:portal.sage_oe_shipment_header:ssh-{key}-{n}")
            if where == "line":
                line = s.obj(f"ShipmentLine:portal.sage_oe_shipment_detail:sd-{key}-{n}")
                s.link("ShipmentLine.of_sales_order", line, sage)
                s.link("ShipmentLine.line_of", line, ship)
            else:
                line = s.obj(f"SalesInvoiceLine:portal.sage_oe_invoice_detail:id-{key}-{n}")
                s.link("SalesInvoiceLine.of_sales_order", line, sage)
                s.link("SalesInvoiceLine.of_shipment", line, ship)
        if ship is not None:
            s.link("Shipment.shipped_to", ship,
                   customer(s, "portal" if where == "portal" else "sage", key))
        ids["shipments"][n] = ship
        return sol

    for n in direct or ():
        order(n, "5")
        if after_build:
            s.link("Batch.for_sales_order", fm, ids["orders"][n])
    for n in filled or ():
        sol = order(n, "12")
        ao = s.obj(f"AssemblyOrder:portal.fm_assembly_order:ao-{key}-{n}", "fm_bronze")
        s.link("AssemblyOrder.fills_from", ao, fm)
        alloc = s.obj(f"AssemblyOrderAllocation:portal.fm_order_entry_item_assembly:"
                      f"al-{key}-{n}", "fm_bronze")
        s.link("AssemblyOrderAllocation.allocates", alloc, ao)
        s.link("AssemblyOrderAllocation.for_order_line", alloc, sol)
    for n in allocated:
        sol = order(n, "30")
        alloc = s.obj(f"BatchAllocation:portal.fm_order_entry_item_batch:ba-{key}-{n}",
                      "fm_bronze")
        s.link("BatchAllocation.allocates", alloc, fm)
        s.link("BatchAllocation.for_order_line", alloc, sol)
    return ids


def table_of(store, policy=None, held=True, schema=None):
    table: dict = {}
    policy = policy or tp.POLICY
    schema = schema or tp.SCHEMA
    base = ot.OntologyTools(ot.StoreReader(store.conn), policy, schema=schema,
                            contracts=CONTRACTS, clock=lambda: tp.NOW)
    ot.register(table, base)
    tt.register(table, tt.TraverseTools(base))
    op.register(table, op.ProcessTools(base, tp.GRAPHS))
    oi.register(table, oi.InvestigateTools(
        table, schema=schema, rows=rows_of(store, policy), scope=oi.read_scope(base),
        held_markings=policy.held_markings if held else None))
    return table


class Asker:
    schema = None   # tp.SCHEMA

    def ask(self, store, arguments, actor=STAFF, **kw):
        kw.setdefault("schema", self.schema)
        out = table_of(store, **kw)["plx.investigate"]["handler"](arguments, actor)
        self.assertEqual(validate(out, CONTRACTS["tools"]["plx.investigate"]["output"],
                                  root=CONTRACTS), [], json.dumps(out)[:3000])
        return out

    def investigate(self, store, root, actor=STAFF):
        """Every page of the answer (a paged answer repeats its shared entries); the gaps
        are listed on the first page only (answer_page.gaps_on_page), so they are its."""
        arguments = {"family": "F17", "root": root}
        answer: dict = {}
        self.pages = 0
        gaps = None
        while True:
            self.pages += 1
            out = self.ask(store, arguments, actor)
            self.assertNotIn("error", out, json.dumps(out)[:2000])
            if gaps is None:
                gaps = out["gaps"]
            for e in out["answer"]:
                self.assertEqual(answer.setdefault(e["name"], e), e)
            cursor = out["completeness"].get("next_cursor")
            if not cursor:
                return dict(out, gaps=gaps), answer
            arguments = {**arguments, "cursor": cursor}

    @staticmethod
    def orders(answer):
        return {n: e for n, e in answer.items()
                if n.startswith("lot_impact_") and n.removeprefix("lot_impact_").isdigit()}



class LotImpactPlanTests(Asker, unittest.TestCase):
    def test_lot_reaches_two_customer_orders_through_two_assembly_orders(self):
        s = tp.Store()
        ids = lot_fixture(s)
        for root in (ids["lot"], {"identifier": "LOT-LA9"}):
            out, answer = self.investigate(s, root)
            orders = self.orders(answer)
            self.assertEqual(answer["lot_impact"]["value"], 2)
            self.assertEqual(len(orders), 2, sorted(answer))
            by_order = {next(b for b in e["basis"] if b in ids["orders"]): e
                        for e in orders.values()}
            self.assertEqual(set(by_order), set(ids["orders"]))
            for n, (order, qty) in enumerate(zip(ids["orders"], ("40", "60"))):
                entry = by_order[order]
                self.assertEqual(Decimal(entry["value"]), Decimal(qty))
                self.assertIsInstance(entry["value"], str)
                self.assertEqual(entry["quantity_role"], "ordered")
                self.assertNotIn("lead_basis", entry)
                cited = [st for st in entry["steps"] if st.startswith("crossed ")]
                # a review this host's store does not hold is cited by an opaque id
                self.assertTrue(cited and all(re.search(r"\(tier \w+, link (lnk|rvw-withheld)-"
                                                        r"\w+\)$", st) for st in cited),
                                entry["steps"])
                self.assertTrue(any("AssemblyOrderComponent.consumed_lot" in st
                                    and "tier reviewed" in st for st in cited))
                self.assertIn(ids["comps"][n], entry["basis"])
                self.assertIn(ids["portal_orders"][n], entry["basis"])
                self.assertIn(ids["shipments"][n], entry["basis"])
                # every input is a cited quantity of the order line it names
                for name in entry["inputs"]:
                    self.assertEqual(answer[name]["object_id"], ids["lines"][n])
                    self.assertIn("l1_handle", answer[name]["citation"])
            supply = [e for n, e in answer.items() if n.startswith("lot_impact_supply_")]
            self.assertEqual(len(supply), 1)
            self.assertIn(ids["po"], supply[0]["basis"])
            self.assertIn(ids["supplier"], supply[0]["basis"])
            self.assertFalse({g["reason"] for g in out["gaps"]}
                             & set(li.BREAKS), out["gaps"])

    def test_shipment_on_a_bc_or_sage_copy_two_links_away_is_reached(self):
        s = tp.Store()
        ids = lot_fixture(s, orders=3, qty=("1", "2", "3"),
                          ship_on={0: "bc", 1: "sage", 2: "none"})
        out, answer = self.investigate(s, ids["lot"])
        by_order = {next(b for b in e["basis"] if b in ids["orders"]): e
                    for e in self.orders(answer).values()}
        self.assertEqual(set(by_order), set(ids["orders"]))
        for n in (0, 1):   # FM order <- Portal order -> BC / Sage copy <- shipment
            entry = by_order[ids["orders"][n]]
            self.assertIn(ids["copies"][n], entry["basis"])
            self.assertIn(ids["shipments"][n], entry["basis"])
            self.assertTrue(any(st.startswith(f"shipment {ids['shipments'][n]} recorded")
                                for st in entry["steps"]), entry["steps"])
            self.assertFalse(any("no shipment" in st for st in entry["steps"]))
        # No shipment on any record read: said as such, with a typed gap, never "none recorded".
        steps = by_order[ids["orders"][2]]["steps"]
        self.assertIn("no shipment found on the records read", steps)
        self.assertFalse(any("shipment of the order is recorded" in st for st in steps))
        gap = next(g for g in out["gaps"] if g["reason"] == "shipment_not_found")
        self.assertIn(gap["state"], ("unknown",))   # only the records read: never proof
        self.assertIn("records read", gap["detail"])
        named = lambda n: {ids["orders"][n], ids["portal_orders"][n]} & set(
            gap["subject"].split(", "))     # an order is named by one record of its cluster
        self.assertEqual((bool(named(0)), bool(named(1)), bool(named(2))),
                         (False, False, True), gap["subject"])

    def test_same_lot_number_from_two_suppliers_is_never_one_count(self):
        s = tp.Store()
        a = lot_fixture(s, key="ka", orders=2, supplier="x")
        b = lot_fixture(s, key="kb", orders=1, qty=("5",), supplier="y")
        out, answer = self.investigate(s, {"identifier": "LOT-SHARED9"})
        self.assertIsNone(answer["lot_impact"]["value"])
        gap = next(g for g in out["gaps"] if g["reason"] == "lot_number_ambiguous")
        self.assertEqual(gap["state"], "unknown")
        groups = {e["value"]: e for n, e in answer.items()
                  if n.startswith("lot_impact_by_supplier_")}
        self.assertEqual(sorted(groups), [1, 2])
        self.assertIn(a["supplier"], groups[2]["basis"])
        self.assertNotIn(b["supplier"], groups[2]["basis"])
        self.assertEqual(set(a["orders"]) & set(groups[2]["basis"]), set(a["orders"]))
        self.assertIn(b["supplier"], groups[1]["basis"])
        self.assertFalse(set(a["orders"]) & set(groups[1]["basis"]))
        # each order is still its own entry, reached from its own supplier's lot
        self.assertEqual(len(self.orders(answer)), 3)
        # the same supplier's lot received on two receipt lines is one lot: one count
        s = tp.Store()
        lot_fixture(s, key="kc", orders=1, supplier="x")
        lot_fixture(s, key="kd", orders=1, supplier="x")
        out, answer = self.investigate(s, {"identifier": "LOT-SHARED9"})
        self.assertEqual(answer["lot_impact"]["value"], 2)
        self.assertNotIn("lot_number_ambiguous", {g["reason"] for g in out["gaps"]})
        self.assertFalse([n for n in answer if "_by_supplier_" in n])

    def test_component_without_assembly_order_is_a_typed_gap_not_a_row(self):
        s = tp.Store()
        ids = lot_fixture(s, without_ao=(1,))
        out, answer = self.investigate(s, ids["lot"])
        self.assertEqual(answer["lot_impact"]["value"], 1)
        self.assertEqual(len(self.orders(answer)), 1)
        gap = next(g for g in out["gaps"] if g["reason"] == "component_without_assembly_order")
        self.assertIn(ids["comps"][1], gap["subject"])
        self.assertIn(gap["state"], ("absent_in_source", "unknown"))

    def test_name_only_match_stays_a_candidate_and_is_not_walked(self):
        s = tp.Store()
        ids = lot_fixture(s, lead=True)
        out, answer = self.investigate(s, ids["lot"])
        self.assertEqual(answer["lot_impact"]["value"], 2)
        orders = self.orders(answer)
        self.assertFalse(any(ids["candidate_order"] in e["basis"] for e in orders.values()))
        candidates = [e for n, e in answer.items() if n.startswith("lot_impact_candidate_")]
        self.assertEqual([e["value"] for e in candidates], [ids["candidate"]])
        self.assertIs(candidates[0]["lead_basis"], True)
        self.assertTrue(any("candidate" in st for st in candidates[0]["steps"]))
        self.assertNotIn("quantity_role", candidates[0])

    def test_reader_without_quality_scope_gets_no_result_through_any_consumer(self):
        s = tp.Store()
        ids = lot_fixture(s, lead=True)
        for root in (ids["lot"], {"identifier": "LOT-LA9"}):
            out = self.ask(s, {"family": "F17", "root": root}, SALES)
            self.assertEqual(out["error"]["code"], "scope_not_held")
            self.assertNotIn("answer", out)
            self.assertNotIn("LA9", json.dumps(out))
        # A host that cannot say which markings a reader holds refuses too (fails closed).
        out = self.ask(s, {"family": "F17", "root": ids["lot"]}, STAFF, held=False)
        self.assertEqual(out["error"]["code"], "scope_not_held")
        # The same plan inline without its requirement: the consumption links stay hidden.
        plan = json.loads(PLAN.read_text("utf-8"))
        plan["id"] = "lot_impact_unscoped"
        del plan["requires_markings"]
        out = self.ask(s, {"plan": plan, "root": ids["lot"]}, SALES)
        answer = {e["name"]: e for e in out.get("answer", ())}
        self.assertFalse(self.orders(answer))
        self.assertFalse([n for n in answer if "candidate" in n])
        self.assertNotIn("AssemblyOrderComponent:", json.dumps(out))
        around = table_of(s)["plx.around"]["handler"](
            {"from": ids["lot"], "tier_floor": "lead", "path": [
                {"link_type": "AssemblyOrderComponent.consumed_lot",
                 "direction": "reverse"}]}, SALES)
        self.assertNotIn("AssemblyOrderComponent:", json.dumps(around))

    def test_cardinality_no_order_lost_or_duplicated(self):
        for n in (1, 10, 60):
            s = tp.Store()
            ids = lot_fixture(s, orders=n, key=f"n{n}", qty=(), ship_on={
                i: ("portal", "bc", "sage")[i % 3] for i in range(n)})
            out, answer = self.investigate(s, ids["lot"])
            orders = self.orders(answer)
            reached = [b for e in orders.values() for b in e["basis"] if b in ids["orders"]]
            self.assertEqual(len(reached), len(set(reached)), n)
            self.assertEqual(answer["lot_impact"]["value"], len(orders))
            self.assertEqual(set(reached), set(ids["orders"]), n)
            # every shipment, on the Portal order or its BC / Sage copy, under its own order
            shipped = [b for e in orders.values() for b in e["basis"] if b in ids["shipments"]]
            self.assertEqual(sorted(shipped), sorted(ids["shipments"]), n)
            self.assertNotIn("shipment_not_found", {g["reason"] for g in out["gaps"]})
            unstated = [e for e in orders.values() if e["value"] is None]
            if n <= 50:
                self.assertNotIn("page_cap", {g["reason"] for g in out["gaps"]})
                self.assertFalse(unstated, n)
            else:   # line reads cut at max_each, and said so: never a guessed quantity
                self.assertIn(("page_cap", "line_read"),
                              {(g["reason"], g["subject"]) for g in out["gaps"]})
                self.assertEqual(len(unstated), n - 50)
                self.assertTrue(any("cut" in st for st in answer["lot_impact"]["steps"]))
            self.assertLessEqual(self.pages, n // 3 + 2, n)   # paging ends, no wall clock

    def test_engine_arguments_fail_closed(self):
        plan = json.loads(PLAN.read_text("utf-8"))
        item = next(e for e in plan["evidence"] if e["kind"] == "compute")
        for args in ({**item["engine_args"], "guess": "lot"},
                     {"lot": item["engine_args"]["lot"]},
                     {**item["engine_args"], "ordered": item["engine_args"]["lot"]}):
            bad = copy.deepcopy(plan)
            bad["id"] = "lot_impact_bad"
            next(e for e in bad["evidence"] if e["kind"] == "compute")["engine_args"] = args
            out = self.ask(tp.Store(), {"plan": bad, "root": "Lot:portal.fm_receipt_raw_material.lotnoqc:none"})
            self.assertEqual(out["error"]["code"], "plan_engine_args", args)


class BatchRecallTests(Asker, unittest.TestCase):
    """A production batch by its id or lot number: the orders, shipments and customers it
    reaches, over the links main serves and those the next build adds (``NEXT``)."""

    def setUp(self):
        self.schema = NEXT

    def by_order(self, answer, ids):
        out = {}
        for e in self.orders(answer).values():
            hit = [n for n, so in ids["orders"].items() if so in e["basis"]]
            self.assertEqual(len(hit), 1, e["basis"])
            out[hit[0]] = e
        return out

    def test_a_batch_reaching_a_shipped_and_an_unshipped_order(self):
        """Acceptance: both orders listed, the shipment under the first, a
        shipment_not_found gap naming the second only; quantities Decimal."""
        s = tp.Store()
        ids = batch_fixture(s, direct=(0,), filled=(1,), ship_on={0: "line"})
        for root in (ids["batch"], ids["gold"], {"identifier": "BL-BA7"}):
            out, answer = self.investigate(s, root)
            self.assertEqual(answer["lot_impact"]["value"], 2, root)
            got = self.by_order(answer, ids)
            self.assertEqual(set(got), {0, 1})
            self.assertIn(ids["shipments"][0], got[0]["basis"])
            self.assertTrue(any("ShipmentLine.of_sales_order" in st for st in got[0]["steps"]))
            self.assertTrue(any("Batch.for_sales_order" in st for st in got[0]["steps"]))
            self.assertIsNone(got[0]["value"])   # the OE list names the order, not lines
            self.assertTrue(any("not its lines" in st for st in got[0]["steps"]))
            self.assertEqual(Decimal(got[1]["value"]), Decimal("12"))
            self.assertTrue(any("AssemblyOrder.fills_from" in st for st in got[1]["steps"]))
            gaps = {(g["reason"], g["subject"]) for g in out["gaps"]}
            missing = [g for g in out["gaps"] if g["reason"] == "shipment_not_found"]
            self.assertEqual(len(missing), 1, out["gaps"])
            self.assertIn(f"so-ba-1", missing[0]["subject"])
            self.assertNotIn("so-ba-0", missing[0]["subject"])
            self.assertEqual(missing[0]["state"], "unknown")   # never proof of no shipment
            self.assertIn(("touched_order_lines_not_recorded",
                           sorted([ids["orders"][0], "SalesOrder:portal.app_sales_order:so-ba-0",
                                   "SalesOrder:portal.sage_oe_order_header:sso-ba-0"])[0]),
                          gaps)
            self.assertFalse([g for g in out["gaps"] if g["reason"] == "link_not_served"])
            # one customer across the Portal and Sage records, shipped to on order 0 only
            customers = answer["lot_impact_customers"]
            self.assertEqual(customers["value"], 1)
            self.assertEqual(set(customers["basis"]), {customer(s, "portal", "ba"),
                                                       customer(s, "sage", "ba")})
            self.assertTrue(any("shipped on order" in st and "with no shipment" in st
                                for st in customers["steps"]), customers["steps"])
            batch = [e for n, e in answer.items() if n.startswith("lot_impact_batch_")]
            self.assertEqual(len(batch), 1)
            self.assertEqual(set(ids[k] for k in ("batch", "gold", "product")) - set(
                batch[0]["basis"]), set())

    def test_shipments_by_invoice_line_and_header_and_allocations(self):
        s = tp.Store()
        ids = batch_fixture(s, allocated=(0, 1, 2), ship_on={0: "invoice", 1: "portal"})
        out, answer = self.investigate(s, ids["batch"])
        got = self.by_order(answer, ids)
        self.assertEqual(set(got), {0, 1, 2})
        self.assertEqual({n: Decimal(e["value"]) for n, e in got.items()},
                         {n: Decimal("30") for n in range(3)})
        self.assertTrue(any("SalesInvoiceLine.of_shipment" in st for st in got[0]["steps"]))
        for n in (0, 1):
            self.assertIn(ids["shipments"][n], got[n]["basis"])
        missing = [g["subject"] for g in out["gaps"] if g["reason"] == "shipment_not_found"]
        self.assertEqual(len(missing), 1)
        self.assertIn("so-ba-2", missing[0])

    def test_before_the_schema_declares_them_the_after_build_hops_are_named_not_guessed(self):
        """Before the build serves them: the plan validates, the after-build steps are not
        run and say so, and what is served (fills_from, allocations) is still walked."""
        self.assertEqual(oi.check_plan(json.loads(PLAN.read_text("utf-8")), BEFORE,
                                       CONTRACTS), [])
        s = tp.Store()
        ids = batch_fixture(s, direct=(0,), filled=(1,), allocated=(2,),
                            ship_on={1: "portal"}, after_build=False)
        out, answer = self.investigate_on(s, ids["batch"], BEFORE)
        got = self.by_order(answer, ids)
        self.assertEqual(set(got), {1, 2})   # order 0 is reached only by the OE list
        unserved = {g["subject"]: g for g in out["gaps"] if g["reason"] == "link_not_served"}
        self.assertEqual(set(unserved), {"batch_orders", "schedule_products", "ship_lines",
                                         "invoice_lines", "invoice_shipments"})
        self.assertIn("Batch.for_sales_order", unserved["batch_orders"]["detail"])
        self.assertEqual(unserved["batch_orders"]["who_can_answer"],
                         ["undeclared:Workbench operator"])
        trace = {t["step"]: t for t in out["trace"]}
        self.assertEqual((trace["batch_orders"]["status"], trace["batch_orders"]["reason"]),
                         ("skipped", "link_not_declared"))

    def investigate_on(self, store, root, schema):
        self.schema = schema
        try:
            return self.investigate(store, root)
        finally:
            self.schema = NEXT

    def test_the_plan_is_whole_once_the_schema_declares_the_new_links(self):
        """With the next build's declarations every step is checked as written: no
        if_declared step hides a broken path."""
        plan = json.loads(PLAN.read_text("utf-8"))
        flagged = [st["id"] for st in plan["steps"] if st.get("if_declared")]
        self.assertEqual(sorted(flagged), ["batch_orders", "invoice_lines",
                                           "invoice_shipments", "schedule_products",
                                           "ship_line_shipments", "ship_lines"])
        for st in plan["steps"]:
            st.pop("if_declared", None)
        self.assertEqual(oi.check_plan(plan, NEXT, CONTRACTS), [])
        # main's schema declares them all now: the plan is whole there as written
        self.assertEqual(oi.check_plan(plan, tp.SCHEMA, CONTRACTS), [])
        # a schema without them: an unflagged step is a broken path
        self.assertTrue(oi.check_plan(plan, BEFORE, CONTRACTS))
        self.assertEqual(li.plan_check(
            next(e for e in plan["evidence"] if e["kind"] == "compute")["engine_args"],
            plan, len(plan["evidence"]) - 1, BEFORE), [])

    def test_overlay_agrees_with_the_schema(self):
        """A next-build link main's schema already declares is declared the same way."""
        for link_id, link in NEXT_LINKS.items():
            self.assertIn(link_id, li.AFTER_BUILD)
            real = tp.SCHEMA.link_types.get(link_id)
            if real is not None:
                self.assertEqual({k: real[k] for k in ("from", "to", "name")},
                                 {k: link[k] for k in ("from", "to", "name")}, link_id)
        self.assertEqual(set(li.AFTER_BUILD), set(NEXT_LINKS))

    def test_a_text_joined_customer_record_is_never_merged(self):
        """Conservative identity: a customer record joined only by a text match (lead
        tier) counts apart, said as such; never one customer by a shared name."""
        s = tp.Store()
        ids = batch_fixture(s, key="bt", allocated=(0,), ship_on={0: "portal"})
        fm_customer = s.obj("Customer:portal.fm_customer:fcu-bt", "fm_bronze")
        s.link("SalesOrder.ordered_by", ids["orders"][0], fm_customer)
        s.link("Customer.same_record_as", customer(s, "portal", "bt"), fm_customer,
               tier="stored_by_text_join", kind="same_record")
        out, answer = self.investigate(s, ids["batch"])
        customers = answer["lot_impact_customers"]
        self.assertEqual(customers["value"], 2)
        self.assertIn(fm_customer, customers["basis"])

    def test_a_batch_linked_to_nothing_is_a_typed_gap(self):
        s = tp.Store()
        ids = batch_fixture(s, key="bn")
        out, answer = self.investigate(s, ids["batch"])
        self.assertEqual(answer["lot_impact"]["value"], 0)
        gap = next(g for g in out["gaps"] if g["reason"] == "batch_not_linked_to_order")
        self.assertIn(ids["batch"], gap["subject"])
        # a complete run over links the reader may see: recorded as absent
        self.assertEqual(gap["state"], "absent_in_source")
        # while the served schema cannot hold the OE-list link, never a proof of absence
        out, answer = self.investigate_on(s, ids["batch"], BEFORE)
        gap = next(g for g in out["gaps"] if g["reason"] == "batch_not_linked_to_order")
        self.assertEqual(gap["state"], "unknown")

    def test_reader_without_quality_scope_is_refused_for_a_batch(self):
        s = tp.Store()
        ids = batch_fixture(s, direct=(0,), ship_on={0: "line"})
        for root in (ids["batch"], {"identifier": "BL-BA7"}):
            out = self.ask(s, {"family": "F17", "root": root}, SALES)
            self.assertEqual(out["error"]["code"], "scope_not_held")
            self.assertNotIn("answer", out)
            self.assertNotIn("BA7", json.dumps(out))
            self.assertNotIn("SalesOrder:", json.dumps(out))

    def test_an_expired_reused_set_never_fails_a_step(self):
        """A later step asks an earlier step's set handles instead of each object; a set
        evicted under load or past its ttl (handle_expired) is asked again per object, never
        a failed step: the same orders, shipments and customers at more calls, and where
        those calls outrun the step budget the answer says it is partial."""
        def expired():
            return mock.patch.object(ot.SetRegistry, "resolve", lambda *_: None)
        for n in (1, 3, 10):
            s = tp.Store()
            ids = batch_fixture(s, key=f"exp{n}", allocated=range(n), ship_on={
                i: ("line", "invoice", "portal")[i % 3] for i in range(n)})
            kept_trace = self.ask(s, {"family": "F17", "root": ids["batch"]})["trace"]
            with expired():
                gone_first = self.ask(s, {"family": "F17", "root": ids["batch"]})
            gone_trace = gone_first["trace"]
            self.assertFalse([t for t in gone_trace if t["status"] == "error"], (n, gone_trace))
            self.assertNotIn("step_failed", gone_first["answerability"]["reasons"], n)
            calls = {t["step"]: t["calls"] for t in kept_trace}
            self.assertTrue(any(t["calls"] > calls[t["step"]] for t in gone_trace), n)
            if n == 10:   # per-object calls run out of steps: said, never read as whole
                self.assertEqual(gone_first["answerability"]["state"], "partial")
                self.assertIn("step_budget_exhausted", gone_first["answerability"]["reasons"])
                continue
            kept_out, kept = self.investigate(s, ids["batch"])
            with expired():
                gone_out, gone = self.investigate(s, ids["batch"])
            self.assertEqual(gone, kept, n)
            # the plan's gaps are the same; a primitive's own no-match notes name the source
            # of the objects each call started from, so they differ by how it was asked
            def plan_gaps(out):
                return sorted(json.dumps(g) for g in out["gaps"] if g["reason"]
                              not in {"no_match", "not_applicable_to_source"})
            self.assertEqual(plan_gaps(gone_out), plan_gaps(kept_out), n)
            self.assertEqual(set(self.by_order(gone, ids)), set(range(n)), n)
            self.assertEqual([(t["step"], t["status"], t["objects"]) for t in gone_trace],
                             [(t["step"], t["status"], t["objects"]) for t in kept_trace], n)

    def test_batch_cardinality_no_order_lost_or_duplicated(self):
        for n in (1, 10, 60):
            s = tp.Store()
            ids = batch_fixture(s, key=f"c{n}", allocated=range(n), ship_on={
                i: ("line", "invoice", "portal")[i % 3] for i in range(n)})
            started = time.monotonic()
            out, answer = self.investigate(s, ids["batch"])
            seconds = time.monotonic() - started
            got = self.by_order(answer, ids)
            self.assertEqual(set(got), set(range(n)), n)
            self.assertEqual(answer["lot_impact"]["value"], n)
            shipped = [b for e in got.values() for b in e["basis"]
                       if b in ids["shipments"].values()]
            self.assertEqual(sorted(shipped), sorted(ids["shipments"].values()), n)
            self.assertNotIn("shipment_not_found", {g["reason"] for g in out["gaps"]}, n)
            first = self.ask(s, {"family": "F17", "root": ids["batch"]})
            self.assertNotIn("budget_exhausted", {t["status"] for t in first["trace"]}, n)
            self.assertLessEqual(first["steps"]["used"], 128, n)
            if n <= 50:
                self.assertTrue(all(e["value"] == "30" for e in got.values()), n)
                self.assertEqual(answer["lot_impact_customers"]["value"], 1, n)
            else:   # line reads cut at max_each, and said so: never a guessed quantity
                self.assertIn("page_cap", {g["reason"] for g in out["gaps"]})
            self.assertLess(seconds, 60, n)

    def test_hand_check_worksheet_and_score(self):
        """The post-apply hand check (scripts.recall_handcheck): one verdict pair per order
        of a real answer, counts without ids, and a pass only at >= 90% with every order
        judged."""
        from scripts import recall_handcheck as hc
        s = tp.Store()
        ids = batch_fixture(s, direct=(0,), filled=(1,), ship_on={0: "line"})
        out = self.ask(s, {"family": "F17", "root": ids["batch"]})
        lines, counts = hc.section("oe_list", ids["batch"], out)
        self.assertEqual((counts["orders"], counts["shipped"]), (2, 1))
        self.assertNotIn("SalesOrder:", json.dumps(counts))
        text = "\n".join(lines)
        self.assertEqual(hc.score(text)["orders_listed"], 2)
        self.assertFalse(hc.score(text)["pass"])   # nothing judged yet

        def sheet(right, wrong, open_=0):
            return "\n".join(["- [x] correct: a", "- [ ] wrong: b"] * right
                             + ["- [ ] correct: a", "- [x] wrong: b"] * wrong
                             + ["- [ ] correct: a", "- [ ] wrong: b"] * open_)
        self.assertTrue(hc.score(sheet(14, 1))["pass"])     # 93%
        self.assertFalse(hc.score(sheet(13, 2))["pass"])    # 87%
        self.assertFalse(hc.score(sheet(15, 0, 1))["pass"])  # one order not judged
        refused = hc.section("oe_list", ids["batch"], {"error": {"code": "scope_not_held"}})
        self.assertEqual(refused[1], {"error": "scope_not_held"})


class LotConsumptionReviewTests(unittest.TestCase):
    """The consumption link as a person's plx.review accept_link records it and the next
    build's review step (reviews.link_step) projects it: no marking is set by hand."""

    def test_reviewed_consumption_keeps_the_quality_marking_for_every_reader(self):
        from ontology import reviews, store_ddl
        from ontology.store import StoreBuilder
        from operating import ontology_tools_review as rt
        from tests import test_ontology_tools_review as tr

        class Builder:
            _INSERT = StoreBuilder._INSERT

            def __init__(self, conn):
                self.connection = conn

            def add(self, table, rows):
                return StoreBuilder.add(self, table, rows)

        s = tp.Store()
        ids = lot_fixture(s, orders=1, qty=("4",), consumption=False)
        s.val(ids["comps"][0], "lot_number", "identifier", "LOT-LA9", path="row.lotno")
        qa, sales = tr.employee_session(tr.OID_A), tr.employee_session(tr.OID_B)
        a, b = qa.employee.principal, sales.employee.principal
        policy = ot.ReadPolicy("plx", {"api:*": {a, b}, "bc:*": {a, b}},
                               {m: ({a} if m == "quality_controlled" else {a, b})
                                for m in store_ddl.MARKINGS})
        with tempfile.TemporaryDirectory() as tmp:
            store = reviews.ReviewStore(Path(tmp) / "reviews.sqlite", clock=lambda: (
                dt.datetime(2099, 1, 20, tzinfo=dt.timezone.utc)))
            base = ot.OntologyTools(ot.StoreReader(s.conn), policy, schema=tp.SCHEMA,
                                    contracts=CONTRACTS, clock=lambda: tp.NOW)
            review = rt.ReviewTools(base, store, session=qa, gate=tr.identity_reviewer)
            evidence = [{"l1_handle": s.handles[oid][0], "digest": tp._h(s.handles[oid][0]),
                         "field_path": path}
                        for oid, path in ((ids["comps"][0], "row.lotno"),
                                          (ids["lot"], "row.lotnoqc"))]
            out = review.call("plx.review", {
                "mode": "record", "decision": "accept_link", "subject": ids["comps"][0],
                "target": ids["lot"], "link_type": li.CONSUMED, "evidence": evidence},
                qa.actor)
            self.assertNotIn("error", out, out)
            reviews.link_step(store, "plx")(
                Builder(s.conn), type("Projector", (), {"links": tp.SCHEMA.link_types})())
        s.conn.commit()   # the build commits before any reader is served
        rows = s.conn.execute("SELECT tier, binding_kind, markings FROM links "
                              "WHERE link_type = ?", (li.CONSUMED,)).fetchall()
        self.assertEqual([(t, k, json.loads(m)) for t, k, m in rows],
                         [("reviewed", "reviewed", ["quality_controlled"])])
        tools = table_of(s, policy)
        path = {"from": ids["lot"], "tier_floor": "lead", "path": [
            {"link_type": li.CONSUMED, "direction": "reverse"}]}
        seen = tools["plx.around"]["handler"](path, qa.actor)
        self.assertIn(ids["comps"][0], json.dumps(seen))
        hidden = tools["plx.around"]["handler"](path, sales.actor)
        self.assertNotIn("AssemblyOrderComponent:", json.dumps(hidden))
        # through the plan: the quality reader reaches the order, the other is refused
        out = tools["plx.investigate"]["handler"]({"family": "F17", "root": ids["lot"]},
                                                  qa.actor)
        answer = {e["name"]: e for e in out["answer"]}
        self.assertEqual(answer["lot_impact"]["value"], 1)
        self.assertEqual(Decimal(answer["lot_impact_1"]["value"]), Decimal("4"))
        out = tools["plx.investigate"]["handler"]({"family": "F17", "root": ids["lot"]},
                                                  sales.actor)
        self.assertEqual(out["error"]["code"], "scope_not_held")


class LotImpactScanTests(unittest.TestCase):
    def test_scan_counts_match_the_live_check_spec(self):
        from verify import live_verify
        s = tp.Store()
        lot_fixture(s, key="sa")
        lot_fixture(s, key="sb", without_ao=(0, 1))
        lot_fixture(s, key="sc", orders=0)
        held = tp.POLICY.held_markings(STAFF)
        report = li.scan(s.conn, held=held)
        self.assertEqual(report["lots_investigated"], 3)
        self.assertEqual(report["lots_with_order"], 1)
        self.assertEqual(report["orders_reached"], 2)
        self.assertEqual(report["stopped_by_gap"], {"component_without_assembly_order": 1,
                                                    "lot_consumption_not_recorded": 1})
        # A reader without the marking sees no consumption: every lot stops there.
        hidden = li.scan(s.conn, held=tp.POLICY.held_markings(SALES))
        self.assertEqual(hidden["lots_with_order"], 0)
        self.assertEqual(hidden["stopped_by_gap"], {"lot_consumption_not_recorded": 3})
        spec = json.loads((tp.ROOT / "devloop/live-checks/process.lot_impact.json")
                          .read_text("utf-8"))
        self.assertEqual(live_verify.validate_spec(spec, stem="process.lot_impact"), [])
        refused = {"error": {"code": "scope_not_held", "message": "x"}}
        for found, held, outcomes in ((report, held, [{}] * 3),
                                      (hidden, tp.POLICY.held_markings(SALES), [refused])):
            doc = {**probe.report_of(found, outcomes, held=held, budget_exhausted=False,
                                     contract_failures=0),
                   "sources": ["portal-workbench-api"], "blocked_upstream": [],
                   "bc_capture": {"evidence_label": "live-read",
                                  "environment_is_production": True}}
            self.assertEqual(live_verify.check_expectations(spec, 0, doc), [], doc)
            self.assertFalse(set(doc) - set(spec["record"]), doc)
        self.assertIs(doc["quality_scope_held"], False)
        self.assertEqual((doc["plan_refused_scope"], doc["investigate_errors"]), (1, 0))
        # An investigate error fails the check: it never passes on a broken answer.
        bad = probe.report_of(report, [{"error": {"code": "plan_engine_args"}}], held=held,
                              budget_exhausted=False, contract_failures=0)
        self.assertTrue(live_verify.check_expectations(spec, 0, {**doc, **bad}))


if __name__ == "__main__":
    unittest.main()
