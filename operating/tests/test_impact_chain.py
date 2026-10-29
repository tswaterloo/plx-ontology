"""Impact chain: from a late purchase order over recorded links
only to the FileMaker assembly order that consumes its item and the customer order it feeds,
with days at risk and, for a reader shown it, value at risk; the stored ``impact_chain``
investigate plan (F16), the set-based ``scan`` and the proactive engine's top finding.

The store is hand-filled with synthetic records over the real compiled schema and the
shipped process graphs (``tests.test_ontology_tools_process`` fixture). Every investigate
output is validated against operating/ontology_tool_contracts.json. Evidence label:
isolated-fixture.
"""

from __future__ import annotations

from contextlib import closing
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from ontology.json_contract import validate
from operating import impact_chain as ic
from operating import ontology_tools as ot
from operating import ontology_tools_investigate as oi
from operating import ontology_tools_process as op
from operating import ontology_tools_traverse as tt
from operating import proactive_engine as pe
from ontology.tests.test_po_lateness import bc_po, confirm, fm_twin, line, portal_po
from tests import test_ontology_tools_process as tp

CONTRACTS = tp.CONTRACTS
STAFF, SALES = tp.STAFF, tp.SALES
TODAY = "2099-02-01"   # tp.NOW's business date


def setUpModule():
    tp.setUpModule()


def late_po(s, key="ic", planned="2099-01-15", promise="2099-01-20"):
    """A Portal PO planned for 2099-01-15 (its FM twin), confirmed for 2099-01-20 and not
    received by 2099-02-01: late, measured today. Its one line (open by quantity) is
    the chain's, added by ``chain_fixture``."""
    po = portal_po(s, key=f"po-{key}", number=f"PO9{abs(hash(key)) % 100000:05d}",
                   ordered=None)
    fm_twin(s, po, planned, key=f"fm-{key}")
    confirm(s, po, f"c-{key}", promise, "2099-01-02")
    return po


def priced_lines(s, so, key, *, lines=(("1000.00", "100"), ("250.00", "10")), qty_unit="ea",
                 cost="6.50", cost_unit="PCS", cost_currency="CAD", amount_currency="CAD"):
    """Portal order lines of ``so`` (finance-marked amount, quantity in ``qty_unit``) that
    sell one Portal product whose BC twin records a finance-marked unit cost: with the
    defaults the margin at stake is 1000.00 - 100 x 6.50 + 250.00 - 10 x 6.50 = 535.00 CAD
    (BC PCS is the Portal's ea)."""
    fg = s.obj(f"Product:portal.app_product:fg-{key}")
    bc = s.obj(f"Product:bc.items:fg-{key}")
    s.link("Product.same_record_as", fg, bc, kind="same_record")
    if cost is not None:
        s.val(bc, "bc_unit_cost", "money", cost, unit=cost_unit, currency=cost_currency,
              markings='["finance"]', path="row.unitCost")
    out = []
    for i, (amount, quantity) in enumerate(lines):
        sl = s.obj(f"SalesOrderLine:portal.app_sales_order_line:{so.rsplit(':', 1)[1]}-{i}")
        s.link("SalesOrderLine.line_of", sl, so)
        s.link("SalesOrderLine.for_item", sl, fg)
        if amount is not None:
            s.val(sl, "line_total", "money", amount, currency=amount_currency,
                  markings='["finance"]', path="row.total_price")
        s.val(sl, "ordered_quantity", "quantity", quantity, unit=qty_unit, role="ordered",
              path="row.quantity")
        out.append(sl)
    return out


def chain_fixture(s, *, orders=1, required="2099-01-25", alloc_line=True, so_status="CONFIRMED",
                  ao_status="Committed", value="1250.50", key="ic",
                  planned="2099-01-15", promise="2099-01-20", margin=None, posted=None,
                  bc_root=False):
    """late PO -> line -> Portal item = FM packaging <- component -> FM AO <- allocation(s)
    -> FM order line -> FM order = Portal order (required date, finance-marked total).
    ``margin``: ``priced_lines`` keyword arguments for each Portal order (None: no lines).
    ``posted``: the line orders a BC item instead, reached from the Portal item only by the
    Portal entity map (``Product.bc_posted_as`` at this tier) and by a number match capped
    at lead (``bc_company_not_known``), as the live BC purchase orders do. ``bc_root``: the
    late order is that BC purchase order itself (its line's expected date ``planned``)."""
    if bc_root:
        # live shape (flagship probe 2026-10-27): an open BC order dated only by its open
        # line's expectedReceiptDate, the Portal order its number matches capped at lead
        ids = {"po": bc_po(s, key=f"bc-{key}", expected=None), "orders": [],
               "portal_orders": []}
        pol = line(s, ids["po"], f"{key}-1", "100", "0", binding="bc.purchase_order_lines",
                   due=planned)
        lead = portal_po(s, key=f"lead-{key}", expected="2099-03-01")
        s.link("PurchaseOrder.same_record_as", lead, ids["po"], tier="lead",
               kind="same_record")
    else:
        ids = {"po": late_po(s, key, planned, promise), "orders": [], "portal_orders": []}
        pol = line(s, ids["po"], f"{key}-1", "100", "0")
    item = s.obj(f"Product:portal.app_product:p-{key}")
    if posted is None:
        s.link("PurchaseOrderLine.for_item", pol, item)
    else:
        bc_item = ids["bc_item"] = s.obj(f"Product:bc.items:i-{key}")
        s.link("PurchaseOrderLine.for_item", pol, bc_item)
        s.link("Product.same_record_as", item, bc_item, tier="lead", kind="same_record")
        if posted:
            s.link("Product.bc_posted_as", item, bc_item, tier=posted, kind="same_record")
    pack = s.obj(f"Product:portal.fm_packaging:pk-{key}", "fm_bronze")
    s.link("Product.same_record_as", item, pack, kind="same_record")
    comp = s.obj(f"AssemblyOrderComponent:portal.fm_assembly_order_item:c-{key}", "fm_bronze")
    s.link("AssemblyOrderComponent.uses_item", comp, pack)
    ao = ids["ao"] = s.obj(f"AssemblyOrder:portal.fm_assembly_order:ao-{key}", "fm_bronze")
    s.val(ao, "status", "text", ao_status)
    s.link("AssemblyOrderComponent.component_of", comp, ao)
    for n in range(orders):
        alloc = s.obj(f"AssemblyOrderAllocation:portal.fm_order_entry_item_assembly:"
                      f"al-{key}-{n:03d}", "fm_bronze")
        s.link("AssemblyOrderAllocation.allocates", alloc, ao)
        ids.setdefault("allocations", []).append(alloc)
        sol = s.obj(f"SalesOrderLine:portal.fm_order_entry_item:oe-{key}-{n:03d}-1", "fm_bronze")
        if alloc_line:
            s.link("AssemblyOrderAllocation.for_order_line", alloc, sol)
        fm_so = s.obj(f"SalesOrder:portal.fm_order_entry:oe-{key}-{n:03d}", "fm_bronze")
        s.val(fm_so, "required_date", "date", required, path="row.datereq")
        s.link("SalesOrderLine.line_of", sol, fm_so)
        so = s.obj(f"SalesOrder:portal.app_sales_order:so-{key}-{n:03d}")
        s.val(so, "required_date", "date", required)
        s.val(so, "status", "text", so_status)
        if value is not None:
            s.val(so, "total_amount", "money", value, currency="CAD", markings='["finance"]')
        s.link("SalesOrder.same_record_as", so, fm_so, kind="same_record")
        if margin is not None:
            ids.setdefault("lines", []).extend(priced_lines(s, so, f"{key}-{n:03d}", **margin))
        ids["orders"].append(fm_so)
        ids["portal_orders"].append(so)
    return ids


# Every audience, every marking but finance: sees the whole chain, never the money.
OPS = tp.Actor("plx", frozenset({"ops"}))
NO_MONEY = tp.ot.ReadPolicy(
    "plx", {"api:*": frozenset({"staff", "ops"}), "bc:*": frozenset({"staff", "ops"})},
    {"finance": frozenset({"staff"}), "pii": frozenset({"staff", "ops"}),
     "l5": frozenset({"staff", "ops"}), "quality_controlled": frozenset({"staff", "ops"})})
# The purchasing reader as the live host grants it: purchase-order line prices
# (purchasing_prices), never finance; every audience of the chain.
PURCHASING = tp.Actor("plx", frozenset({"purchasing"}))
PURCHASING_POLICY = tp.ot.ReadPolicy(
    "plx", {"api:*": frozenset({"staff", "purchasing"}),
            "bc:*": frozenset({"staff", "purchasing"})},
    {"finance": frozenset({"staff"}), "purchasing_prices": frozenset({"staff", "purchasing"})})


def rows_of(store, policy=None):
    """The citation re-reader: a cited record's row with the values whose audience and
    markings the reader is granted (fails closed otherwise)."""
    def rows(handle, actor):
        found = store.conn.execute(
            "SELECT v.value, v.field_path, v.markings, o.audience_key FROM object_values v "
            "JOIN objects o ON o.object_id = v.object_id WHERE v.l1_handle = ?",
            (handle,)).fetchall()
        if not found or not (policy or tp.POLICY).may_read(actor, found[0][3]):
            return None
        held = (policy or tp.POLICY).held_markings(actor)
        row = {path.split(".", 1)[1]: value for value, path, markings, _aud in found
               if set(json.loads(markings)) <= held}
        return {"relation": handle.split("/")[2], "l1_handle": handle, "row": row}
    return rows


def table_of(store, rows=True, policy=None):
    table: dict = {}
    base = ot.OntologyTools(ot.StoreReader(store.conn), policy or tp.POLICY, schema=tp.SCHEMA,
                            contracts=CONTRACTS, clock=lambda: tp.NOW)
    ot.register(table, base)
    tt.register(table, tt.TraverseTools(base))
    op.register(table, op.ProcessTools(base, tp.GRAPHS))
    oi.register(table, oi.InvestigateTools(table, schema=tp.SCHEMA,
                                           rows=rows_of(store, policy) if rows else None,
                                           scope=oi.read_scope(base)))
    return table


class PlanTests(unittest.TestCase):
    def investigate(self, store, root, actor=STAFF, policy=None):
        """The whole answer: every page of it (an answer too large for one response is
        paged), each page validated against the tool contract."""
        handler = table_of(store, policy=policy)["plx.investigate"]["handler"]
        args = {"family": "F16", "root": root}
        out = handler(args, actor)
        answer = []
        while True:
            self.assertEqual(validate(out, CONTRACTS["tools"]["plx.investigate"]["output"],
                                      root=CONTRACTS), [], json.dumps(out)[:3000])
            self.assertNotIn("error", out, json.dumps(out)[:2000])
            answer += out["answer"]
            cursor = out["completeness"].get("next_cursor")
            if not cursor or out["completeness"].get("reason") != "answer_paged":
                break
            out = handler({**args, "cursor": cursor}, actor)
        out = {**out, "answer": answer}
        return out, {e["name"]: e for e in answer}

    def test_the_stored_plan_validates_and_is_family_f15(self):
        plan = json.loads((tp.ROOT / "ontology/plans/impact_chain.json").read_text("utf-8"))
        self.assertEqual(oi.check_plan(plan, tp.SCHEMA, CONTRACTS), [])
        self.assertEqual(plan["families"], ["F16"])

    def test_a_late_po_line_propagates_to_the_assembly_order_and_the_customer_order(self):
        s = tp.Store()
        ids = chain_fixture(s)
        out, entries = self.investigate(s, ids["po"])
        order = entries["impact_1"]
        # material not in by today (2099-02-01) vs required 2099-01-25: 7 days, lower bound
        self.assertEqual(order["value"], 7)
        self.assertIn(ids["ao"], order["basis"])
        self.assertIn(ids["orders"][0], order["basis"])
        crossed = [s for s in order["steps"] if s.startswith("crossed ")]
        for link_type in ("PurchaseOrderLine.line_of", "PurchaseOrderLine.for_item",
                          "Product.same_record_as", "AssemblyOrderComponent.uses_item",
                          "AssemblyOrderComponent.component_of",
                          "AssemblyOrderAllocation.allocates",
                          "AssemblyOrderAllocation.for_order_line", "SalesOrderLine.line_of"):
            self.assertTrue(any(link_type in step and "link lnk-" in step for step in crossed),
                            link_type)
        self.assertTrue(any(i.startswith("order_required_") for i in order["inputs"]))
        self.assertTrue(any(i.startswith("late_state_") for i in order["inputs"]))
        value = entries["impact_value_1"]
        self.assertEqual((Decimal(value["value"]), value["currency"]),
                         (Decimal("1250.50"), "CAD"))
        self.assertEqual(entries["impact"]["value"], 1)
        self.assertIn("1 customer orders at risk from 1 late POs, worst first",
                      entries["impact"]["steps"])
        self.assertIn(ic.LEAD_TIME_GAP, {g["reason"] for g in out["gaps"]})

    def test_the_entity_map_carries_a_bc_item_to_the_customer_order_naming_its_company(self):
        """A BC purchase order line orders a BC item; only the Portal entity map
        (Product.bc_posted_as, verified, joined on the BC systemId) makes it the Portal
        product the FileMaker assembly order consumes. The walk crosses it and the step
        names the BC company; the number match beside it stays a lead and is never the
        hop. A reader shown no BC record never gets the BC item's company."""
        s = tp.Store()
        ids = chain_fixture(s, key="em", posted="verified")
        out, entries = self.investigate(s, ids["po"])
        order = entries["impact_1"]
        self.assertEqual(order["value"], 7)
        self.assertIn(ids["orders"][0], order["basis"])
        crossed = [x for x in order["steps"] if x.startswith("crossed Product.")]
        self.assertTrue(crossed, order["steps"])
        posted = [x for x in crossed if x.startswith("crossed Product.bc_posted_as")]
        self.assertEqual(len(posted), 1, crossed)
        self.assertIn("tier verified", posted[0])
        self.assertIn("the BC record of company bc_company_a", posted[0])
        self.assertFalse([x for x in crossed if "tier lead" in x])
        self.assertEqual(ic.ranked_orders(out["answer"]),
                         [(ids["po"], min(ids["orders"][0], ids["portal_orders"][0]))])

    def test_a_lead_entity_map_row_or_none_stops_at_the_item(self):
        for tier in ("lead", None):
            with self.subTest(tier=tier):
                s = tp.Store()
                ids = chain_fixture(s, key=f"ld{tier}", posted=tier or "")
                out, entries = self.investigate(s, ids["po"])
                self.assertEqual(entries["impact"]["value"], 0)
                self.assertFalse([n for n in entries if n.startswith("impact_")])
                self.assertEqual(ic.ranked_orders(out["answer"]), [])
                # The plan reads firm links only, so the walk stops at the BC item; the
                # number match beside it is below the plan's tier floor.
                stop = {g["reason"] for g in out["gaps"]}
                self.assertIn("item_not_used_by_assembly_order", stop)
                self.assertIn("below_tier_floor", stop)

    def test_an_at_risk_po_is_decided_by_the_shared_po_status_rule(self):
        """Not yet late (requested-by 2099-02-10, today 2099-02-01) and the supplier's one
        promise 2099-02-20 is after it: ``plx.process`` says at risk (po_status), so the
        order required 2099-02-12 is 8 days at risk, on the plan and on the scan alike. A
        promise on the requested-by date is neither, whatever the dates before it."""
        s = tp.Store()
        risky = chain_fixture(s, planned="2099-02-10", promise="2099-02-20",
                              required="2099-02-12", key="ar")
        chain_fixture(s, planned="2099-02-10", promise="2099-02-10", required="2099-02-12",
                      key="ok")
        _, entries = self.investigate(s, risky["po"])
        self.assertEqual(entries["impact_1"]["value"], 8)
        self.assertEqual(entries["impact"]["value"], 1)
        path = store_file(s, tempfile.mkdtemp())
        st = pe._Store(path)
        try:
            table = table_of(s)
            found = ic.scan(st, lambda tool, args: table[tool]["handler"](args, STAFF))
        finally:
            st.close()
        self.assertEqual((found["late_pos_considered"], found["orders_at_risk"]), (1, 1))
        self.assertEqual(found["orders_excluded"]["po_not_late"], 1)
        self.assertEqual(found["samples"][0]["days_at_risk"], 8)

    def test_a_missing_link_is_a_typed_gap_never_an_inferred_join(self):
        s = tp.Store()
        ids = chain_fixture(s, alloc_line=False)
        # a decoy order with the same required date and an order line of the same native
        # key shape: nothing records a link to it, so it is never joined
        decoy = s.obj("SalesOrder:portal.app_sales_order:so-decoy")
        s.val(decoy, "required_date", "date", "2099-01-25")
        out, entries = self.investigate(s, ids["po"])
        self.assertEqual(entries["impact"]["value"], 0)
        self.assertFalse([n for n in entries if n.startswith("impact_")])
        self.assertNotIn(decoy, json.dumps(out["answer"]))
        gap = next(g for g in out["gaps"] if g["reason"] == "allocation_without_order_line")
        self.assertIn(ids["allocations"][0], gap["subject"])
        self.assertIn("FileMaker assembly allocation", gap["detail"])
        self.assertIn(gap["state"], ("absent_in_source", "unknown"))

    def test_an_assembly_order_with_no_order_link_is_its_own_typed_gap(self):
        s = tp.Store()
        ids = chain_fixture(s, orders=0)
        out, entries = self.investigate(s, ids["po"])
        self.assertEqual(entries["impact"]["value"], 0)
        gap = next(g for g in out["gaps"]
                   if g["reason"] == "assembly_order_not_linked_to_order_line")
        self.assertIn(ids["ao"], gap["subject"])

    def test_a_reader_without_money_visibility_sees_days_but_not_value(self):
        s = tp.Store()
        ids = chain_fixture(s)
        out, entries = self.investigate(s, ids["po"], OPS, NO_MONEY)
        self.assertEqual(entries["impact_1"]["value"], 7)
        self.assertNotIn("impact_value_1", entries)
        text = json.dumps(out)
        self.assertNotIn("1250.5", text)
        gap = next(g for g in out["gaps"] if g["reason"] == "value_at_risk_not_stated")
        self.assertNotIn("finance", gap["detail"])
        # the staff answer for the same root does state it: the difference is the reader
        _out, staff = self.investigate(s, ids["po"], STAFF, NO_MONEY)
        self.assertIn("impact_value_1", staff)
        # a reader who may not read the PO's lateness inputs (no BC) gets no verdict, so
        # no order at risk: undetermined is a typed gap, never late
        out, sales = self.investigate(s, ids["po"], SALES)
        self.assertEqual(sales["impact"]["value"], 0)
        self.assertNotIn("1250.5", json.dumps(out))
        self.assertIn("lateness_undetermined", {g["reason"] for g in out["gaps"]})

    def test_a_value_the_source_no_longer_states_is_not_used(self):
        s = tp.Store()
        ids = chain_fixture(s)
        table = table_of(s, rows=False)   # no re-read: a shown value is not proven
        out = table["plx.investigate"]["handler"]({"family": "F16", "root": ids["po"]}, STAFF)
        names = {e["name"] for e in out["answer"]}
        self.assertIn("impact_1", names)
        self.assertNotIn("impact_value_1", names)

    def test_closed_orders_and_realized_assembly_orders_put_nothing_at_risk(self):
        for kwargs in ({"so_status": "SHIPPED"}, {"ao_status": "Realized"}):
            with self.subTest(**kwargs):
                s = tp.Store()
                ids = chain_fixture(s, **kwargs)
                _out, entries = self.investigate(s, ids["po"])
                self.assertEqual(entries["impact"]["value"], 0)

    def test_an_order_required_after_the_material_date_is_not_at_risk(self):
        s = tp.Store()
        ids = chain_fixture(s, required="2099-03-01")
        _out, entries = self.investigate(s, ids["po"])
        self.assertEqual(entries["impact_1"]["value"], -28)
        self.assertEqual(entries["impact"]["value"], 0)
        self.assertNotIn("impact_value_1", entries)

    def test_a_finance_reader_gets_the_margin_at_stake_and_others_the_chain_without_money(self):
        s = tp.Store()
        ids = chain_fixture(s, margin={})
        out, entries = self.investigate(s, ids["po"])
        margin = entries["impact_margin_1"]
        self.assertEqual((Decimal(margin["value"]), margin["currency"]),
                         (Decimal("535.00"), "CAD"))
        # every line's arithmetic is a step, each input an answer entry it rests on
        # the stored plan has no landed-cost step: BC unitCost, labelled as such
        recorded = "recorded unit cost, excludes freight/duty"
        self.assertEqual(sum(1 for step in margin["steps"] if recorded in step
                             and " = " in step), 2)
        self.assertTrue(any("1000.00 CAD sales amount - 100 ea x 6.50 CAD/PCS" in step
                            for step in margin["steps"]))
        self.assertIn(f"cost basis: {recorded}", margin["steps"])
        self.assertIn(f"cost basis: {recorded}", entries["impact_margin"]["steps"])
        self.assertTrue(margin["inputs"])
        self.assertEqual(Decimal(entries["impact_margin"]["value"]), Decimal("535.00"))
        # each hop of the chain stays cited beside the money
        self.assertIn(ids["ao"], entries["impact_1"]["basis"])
        # a reader without finance sees the same chain and days, no money and no hint
        out, ops = self.investigate(s, ids["po"], OPS, NO_MONEY)
        self.assertEqual(ops["impact_1"]["value"], 7)
        self.assertFalse([n for n in ops if n.startswith("impact_margin")])
        text = json.dumps(out)
        for money in ("535.00", "6.50", "1000.00", "250.00", "185.00", "350.00"):
            self.assertNotIn(money, text)
        gap = next(g for g in out["gaps"] if g["reason"] == "line_amount_not_shown")
        self.assertIn("whether one is recorded is not said", gap["detail"])

    def test_a_late_bc_order_walks_the_entity_map_margin_for_finance_chain_for_purchasing(self):
        """The flagship's live shape: the late order is a BC purchase order dated by its open
        line (a lead Portal number match beside it), its line orders a BC item the Portal
        entity map makes the Portal product the FileMaker assembly order consumes. plx.process
        says late (the line date governs, the lead never), so the chain is walked: a finance
        reader gets the margin; the purchasing reader the same chain and days, no money."""
        s = tp.Store()
        ids = chain_fixture(s, key="bcr", posted="verified", bc_root=True, margin={})
        out, entries = self.investigate(s, ids["po"])
        self.assertNotIn("lateness_undetermined", {g["reason"] for g in out["gaps"]})
        self.assertEqual(entries["impact_1"]["value"], 7)
        self.assertIn(ids["ao"], entries["impact_1"]["basis"])
        self.assertTrue(any(x.startswith("crossed Product.bc_posted_as")
                            for x in entries["impact_1"]["steps"]))
        self.assertEqual(Decimal(entries["impact_margin_1"]["value"]), Decimal("535.00"))
        self.assertEqual(ic.ranked_orders(out["answer"]),
                         [(ids["po"], min(ids["orders"][0], ids["portal_orders"][0]))])
        out, buyer = self.investigate(s, ids["po"], PURCHASING, PURCHASING_POLICY)
        self.assertEqual(buyer["impact_1"]["value"], 7)
        self.assertIn(ids["ao"], buyer["impact_1"]["basis"])
        self.assertIn(ids["orders"][0], buyer["impact_1"]["basis"])
        self.assertEqual(ic.ranked_orders(out["answer"]),
                         [(ids["po"], min(ids["orders"][0], ids["portal_orders"][0]))])
        self.assertFalse([n for n in buyer if n.startswith(("impact_margin", "impact_value"))])
        text = json.dumps(out)
        for money in ("535.00", "6.50", "1000.00", "250.00", "1250.50"):
            self.assertNotIn(money, text)

    def test_margin_fails_closed_on_units_currencies_and_unpriced_items(self):
        cases = (({"qty_unit": "kg"}, "cost_unit_differs_from_quantity_unit"),
                 ({"cost_currency": "USD"}, "margin_currency_differs"),
                 ({"cost": "0"}, "landed_cost_not_positive"),
                 ({"cost": None}, "landed_cost_not_shown"),
                 ({"lines": (("1000.00", "100"), (None, "10"))}, "line_amount_not_shown"))
        for margin, reason in cases:
            with self.subTest(reason=reason):
                s = tp.Store()
                ids = chain_fixture(s, margin=margin)
                out, entries = self.investigate(s, ids["po"])
                self.assertEqual(entries["impact_1"]["value"], 7)
                # never a part of the order as the whole: no margin, a typed gap
                self.assertFalse([n for n in entries if n.startswith("impact_margin")])
                self.assertIn(reason, {g["reason"] for g in out["gaps"]})


class EngineTests(unittest.TestCase):
    def edges(self, *triples):
        return [ic.Edge(f"l{n}", t, a, b, tier) for n, (t, a, b, tier) in enumerate(triples)]

    def test_unknown_engine_arguments_and_verdicts_fail_closed(self):
        plan = json.loads((tp.ROOT / "ontology/plans/impact_chain.json").read_text("utf-8"))
        index = next(i for i, e in enumerate(plan["evidence"]) if e["kind"] == "compute")
        args = dict(plan["evidence"][index]["engine_args"], surprise=1)
        self.assertIn("unknown engine argument surprise",
                      ic.plan_check(args, plan, index, tp.SCHEMA))
        self.assertTrue(ic.plan_check("x", plan, index, tp.SCHEMA))
        # an unknown verdict is undetermined: a typed gap, never late
        found = ic.assess({"PurchaseOrder:x:1": ic.Late("PurchaseOrder:x:1", "maybe")},
                          ic.Graph([]), required={}, order_status={}, assembly_status={})
        self.assertEqual((found.considered, found.orders), ([], []))
        self.assertEqual([b.reason for b in found.breaks], ["lateness_undetermined"])
        # at risk with no promise date to put it at, or a flag that is not a boolean
        po = "PurchaseOrder:x:1"
        found = ic.assess({po: ic.Late(po, "not_due", at_risk=True)}, ic.Graph([]),
                          required={}, order_status={}, assembly_status={})
        self.assertEqual(found.undetermined, [po])
        found = ic.assess({po: ic.Late(po, "not_due", revised=date(2099, 3, 1),
                                       governing=date(2099, 2, 1), at_risk=None)},
                          ic.Graph([]), required={}, order_status={}, assembly_status={})
        self.assertEqual((found.considered, found.excluded["po_not_late"]), ([], 1))
        # late with no date to measure from is undetermined too, never "not late"
        found = ic.assess({"PurchaseOrder:x:1": ic.Late("PurchaseOrder:x:1", "late")},
                          ic.Graph([]), required={}, order_status={}, assembly_status={})
        self.assertEqual((found.undetermined, found.excluded["po_not_late"]),
                         (["PurchaseOrder:x:1"], 0))

    def test_a_lead_tier_link_is_not_crossed_and_says_so(self):
        po, pol = "PurchaseOrder:p:1", "PurchaseOrderLine:p:1"
        graph = ic.Graph(self.edges((ic.PO_LINE, pol, po, "verified"),
                                    (ic.LINE_ITEM, pol, "Product:p:1", "stored_by_text_join")))
        chains, breaks, _cut = ic.walk(po, graph)
        self.assertEqual(chains, [])
        self.assertEqual([(b.reason, b.subject) for b in breaks], [("link_lead_only", pol)])

    def test_an_item_whose_only_twin_is_a_lead_stops_at_that_lead(self):
        """The scan sees lead links: a BC item reached from the Portal product only by its
        number (lead) stops as link_lead_only, never as an item no assembly order uses."""
        po, pol, bc = "PurchaseOrder:p:1", "PurchaseOrderLine:p:1", "Product:bc.items:1"
        portal, pack = "Product:portal.app_product:1", "Product:portal.fm_packaging:1"
        for posted in ("lead", "verified"):
            with self.subTest(posted=posted):
                graph = ic.Graph(self.edges((ic.PO_LINE, pol, po, "verified"),
                                            (ic.LINE_ITEM, pol, bc, "verified"),
                                            (ic.ITEM_TWIN, portal, bc, "lead"),
                                            (ic.ITEM_POSTED, portal, bc, posted),
                                            (ic.ITEM_TWIN, portal, pack, "verified")))
                _chains, breaks, _cut = ic.walk(po, graph)
                reasons = [(b.reason, b.subject) for b in breaks]
                if posted == "lead":
                    self.assertEqual(reasons, [("link_lead_only", bc)])
                else:   # crossed: the FileMaker twin is reached, no component uses it here
                    self.assertEqual(reasons, [("item_not_used_by_assembly_order", bc)])

    def test_entity_map_wording_and_ranks_fail_closed(self):
        posted = ic.Edge("l1", ic.ITEM_POSTED, "Product:p:1", "Product:bc.items:1", "verified")
        self.assertIn("company bc_manufacturing", ic.posted_company(
            posted, {"Product:bc.items:1": {"lineage": "bc_manufacturing"}}))
        # a reader not shown the BC record's reference: the company is not named
        self.assertIn("not shown to this reader", ic.posted_company(posted, {}))
        self.assertEqual(ic.posted_company(ic.Edge("l2", ic.ITEM_TWIN, "a", "b", "verified"),
                                           {}), "")
        # ranks read only the engine's own wording, in rank order; anything else is skipped
        answer = [{"name": "impact_2", "kind": "computed", "steps": ["chain from P to S2"]},
                  {"name": "impact_1", "kind": "computed", "steps": ["chain from P to S1"]},
                  {"name": "impact_3", "kind": "computed", "steps": ["from P to S3"]},
                  {"name": "impact_value_1", "kind": "computed", "steps": ["chain from P to X"]},
                  {"name": "impact_4", "kind": "property", "steps": ["chain from P to S4"]},
                  "not an entry"]
        self.assertEqual(ic.ranked_orders(answer), [("P", "S1"), ("P", "S2")])

    def test_twin_values_that_disagree_give_no_value(self):
        a = {"value": "10.00", "currency": "CAD", "name": "v_1"}
        b = {"value": "12.00", "currency": "CAD", "name": "v_2"}
        got = ic.order_value(["SalesOrder:a:1", "SalesOrder:b:1"],
                             {"SalesOrder:a:1": [a], "SalesOrder:b:1": [b]}, {},
                             lambda _e: True)
        self.assertEqual((got[0], got[3]), (None, "order_values_disagree"))
        c = dict(b, value="10.00", currency="USD")
        got = ic.order_value(["SalesOrder:a:1", "SalesOrder:b:1"],
                             {"SalesOrder:a:1": [a], "SalesOrder:b:1": [c]}, {},
                             lambda _e: True)
        self.assertEqual(got[3], "order_values_disagree")
        self.assertIsNone(ic.order_value(["SalesOrder:a:1"], {"SalesOrder:a:1": [
            dict(a, value=10.0)]}, {}, lambda _e: True)[0])   # a float is not money text


def store_file(s, directory):
    path = Path(directory) / "store.sqlite"
    s.conn.commit()   # a backup waits forever on the source's open write transaction
    with closing(sqlite3.connect(path)) as target:
        s.conn.backup(target)
    return path


class ScanAndProactiveTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def caller(self, s, actor, policy=None):
        table = table_of(s, policy=policy)
        return lambda tool, args: table[tool]["handler"](args, actor)

    def test_cardinality_no_order_lost_or_duplicated_within_the_budget(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                s = tp.Store()
                ids = chain_fixture(s, orders=n, margin={})
                path = store_file(s, self.dir.name)
                st = pe._Store(path)
                try:
                    call = self.caller(s, STAFF)
                    started = time.monotonic()
                    found = ic.scan(st, call, value=lambda oid: call(
                        "plx.object", {"object_id": oid}),
                        margin_values=ic.store_margin_values(st))
                    elapsed = time.monotonic() - started
                finally:
                    st.close()
                    path.unlink()
                self.assertEqual(found["orders_at_risk"], n)
                keys = [o.key for o in found["assessment"].at_risk]
                self.assertEqual(len(keys), len(set(keys)))
                self.assertEqual(set(keys), {min(a, b) for a, b in
                                             zip(ids["orders"], ids["portal_orders"])})
                self.assertEqual(found["late_pos_considered"], 1)
                self.assertEqual(found["chains_broken"], 0)
                self.assertEqual(found["orders_with_value"], n)
                self.assertEqual(found["value_at_risk"],
                                 {"CAD": str(Decimal("1250.50") * n)})
                # the margin at stake: every order once, summed in Decimal, per currency
                self.assertEqual(found["orders_with_margin"], n)
                self.assertEqual(found["margin_at_stake"], {"CAD": str(Decimal("535.00") * n)})
                (impact,) = found["po_impacts"]
                self.assertEqual((impact["po"], impact["chain"], len(impact["orders"]),
                                  len(set(impact["orders"])), impact["orders_with_margin"]),
                                 (ids["po"], "full", n, n, n))
                self.assertEqual(impact["margin"], {"amount": str(Decimal("535.00") * n),
                                                    "currency": "CAD",
                                                    "basis": ic.COST_BASES[ic.RECORDED_COST]})
                self.assertEqual(found["late_pos_with_full_chain"], 1)
                self.assertLess(elapsed, 20)

    def test_entity_map_chains_cardinality_and_cost_through_the_bc_item(self):
        """The scan crosses the entity map like any verified twin: N orders fed through it
        are each reached once, within the budget, with their margin."""
        for n in (1, 10, 60):
            with self.subTest(n=n):
                s = tp.Store()
                ids = chain_fixture(s, orders=n, margin={}, posted="verified", key=f"cm{n}")
                path = store_file(s, self.dir.name)
                st = pe._Store(path)
                try:
                    call = self.caller(s, STAFF)
                    started = time.monotonic()
                    found = ic.scan(st, call, margin_values=ic.store_margin_values(st))
                    elapsed = time.monotonic() - started
                finally:
                    st.close()
                    path.unlink()
                keys = [o.key for o in found["assessment"].at_risk]
                self.assertEqual(len(keys), n)
                self.assertEqual(len(set(keys)), n)
                self.assertEqual(found["chains_broken"], 0)
                self.assertEqual(found["margin_at_stake"], {"CAD": str(Decimal("535.00") * n)})
                self.assertLess(elapsed, 20)

    def test_scan_counts_broken_chains_and_a_reader_without_finance_gets_no_value(self):
        s = tp.Store()
        chain_fixture(s, orders=2)
        chain_fixture(s, orders=1, alloc_line=False, key="br")
        path = store_file(s, self.dir.name)
        st = pe._Store(path)
        try:
            call = self.caller(s, OPS, NO_MONEY)
            found = ic.scan(st, call, value=lambda oid: call("plx.object", {"object_id": oid}))
        finally:
            st.close()
        self.assertEqual(found["orders_at_risk"], 2)
        self.assertEqual(found["broken_by_reason"], {"allocation_without_order_line": 1})
        self.assertEqual(found["broken_share_percent"], "33.3")
        self.assertEqual((found["orders_with_value"], found["value_at_risk"]), (0, {}))
        self.assertEqual(len(found["samples"]), 2)
        self.assertTrue(all(link["link_id"] for one in found["samples"]
                            for link in one["links"]))

    def test_a_restricted_scan_never_crosses_a_record_the_reader_may_not_read(self):
        s = tp.Store()
        ids = chain_fixture(s, orders=2)
        path = store_file(s, self.dir.name)
        st = pe._Store(path)
        try:
            call = self.caller(s, STAFF)
            everything = {r[0] for r in s.conn.execute("SELECT object_id FROM objects")}
            found = ic.scan(st, call, visible=everything - {ids["ao"]})
            hidden_order = ic.scan(st, call, visible=everything - {ids["orders"][0],
                                                                    ids["portal_orders"][0]})
        finally:
            st.close()
        # without the assembly order no chain exists: the PO is not walked past the
        # component, and nothing names the hidden record
        self.assertEqual((found["orders_at_risk"], found["chains_found"]), (0, 0))
        self.assertNotIn(ids["ao"], json.dumps(found["samples"]))
        # a hidden order (both twins) drops out; the other order is still at risk
        self.assertEqual(hidden_order["orders_at_risk"], 1)
        self.assertNotIn(ids["portal_orders"][0], json.dumps(hidden_order["samples"]))
        self.assertEqual(hidden_order["broken_by_reason"], {"order_line_not_linked_to_order": 1})

    def test_the_proactive_engine_raises_orders_at_risk_worst_first_and_trims_per_reader(self):
        s = tp.Store()
        chain_fixture(s, orders=1, required="2099-01-20")
        chain_fixture(s, orders=1, required="2099-01-28", key="b2")
        path = store_file(s, self.dir.name)
        inbox = pe.inbox_path(path)
        summary = pe.run(path, inbox, date(2099, 2, 1), call=self.caller(s, STAFF))
        self.assertEqual(summary["detectors"]["orders_at_risk"], "complete")
        self.assertEqual(summary["top_finding"]["text"],
                         "2 customer orders at risk from 2 late POs, worst first")
        self.assertNotIn("1250", json.dumps(summary))
        with sqlite3.connect(inbox) as conn:
            found = conn.execute("SELECT summary FROM gaps WHERE kind = 'order_at_risk' "
                                 "ORDER BY summary").fetchall()
        self.assertEqual(sorted(r[0] for r in found),
                         sorted(["12 days at risk from 1 late purchase order (days at risk "
                                 "is a lower bound: no assembly lead time is recorded)",
                                 "4 days at risk from 1 late purchase order (days at risk "
                                 "is a lower bound: no assembly lead time is recorded)"]))

        def read(actor, may_read=None):
            return pe.read_inbox(inbox, path, reader=pe.reader_key(actor),
                                 may_read=may_read or (lambda aud: tp.POLICY.may_read(actor,
                                                                                      aud)),
                                 held=tp.POLICY.held_markings(actor),
                                 decide=lambda handles: {h: None for h in handles})
        staff = [g for g in read(STAFF)["gaps"] if g["kind"] == "order_at_risk"]
        self.assertEqual(len(staff), 2)
        self.assertTrue(all("1250" not in json.dumps(g) for g in staff))
        # every record of the chain is its own citation group: a reader who may not open
        # the FileMaker assembly order (one link of the chain) never sees the gap
        no_ao = read(SALES, lambda aud: tp.POLICY.may_read(SALES, aud)
                     and "assembly_order" not in aud)
        self.assertEqual([g for g in no_ao["gaps"] if g["kind"] == "order_at_risk"], [])
        # nor does one who may not open the order's required date (the Portal order)
        no_so = read(SALES, lambda aud: tp.POLICY.may_read(SALES, aud)
                     and "app_sales_order" not in aud)
        self.assertEqual([g for g in no_so["gaps"] if g["kind"] == "order_at_risk"], [])

    def test_a_late_po_on_the_home_list_shows_its_margin_only_to_finance_readers(self):
        """One late PO feeding two orders: ``po_impact`` ("affects 2 sales orders", no money)
        for every reader who may open the chain, ``po_impact_margin`` (the margin at stake)
        only for one holding finance, and plx.find's home list shows one line per PO."""
        from operating import mcp_server

        s = tp.Store()
        chain_fixture(s, orders=2, required="2099-01-20", margin={})
        path = store_file(s, self.dir.name)
        inbox = pe.inbox_path(path)
        summary = pe.run(path, inbox, date(2099, 2, 1), call=self.caller(s, STAFF))
        self.assertNotIn("1070", json.dumps(summary))   # the run summary never holds money
        self.assertEqual(summary["gaps_by_kind"]["po_impact"], 1)
        self.assertEqual(summary["gaps_by_kind"]["po_impact_margin"], 1)

        def read(actor, policy):
            return pe.read_inbox(inbox, path, reader=pe.reader_key(actor),
                                 may_read=lambda aud: policy.may_read(actor, aud),
                                 held=policy.held_markings(actor),
                                 decide=lambda handles: {h: None for h in handles})
        staff, ops = read(STAFF, tp.POLICY), read(OPS, NO_MONEY)
        kinds = lambda got: sorted(g["kind"] for g in got["gaps"]  # noqa: E731
                                   if g["kind"].startswith("po_impact"))
        self.assertEqual(kinds(staff), ["po_impact", "po_impact_margin"])
        self.assertEqual(kinds(ops), ["po_impact"])
        money = next(g for g in staff["gaps"] if g["kind"] == "po_impact_margin")
        self.assertEqual(money["summary"], "affects 2 sales orders, 1070.00 CAD margin at "
                                           "stake (recorded unit cost, excludes freight/duty), "
                                           "worst 12 days at risk")
        plain = next(g for g in ops["gaps"] if g["kind"] == "po_impact")
        self.assertEqual(plain["summary"], "affects 2 sales orders, worst 12 days at risk")
        self.assertNotIn("1070", json.dumps(ops))
        self.assertNotIn("6.50", json.dumps(ops))
        home = {name: mcp_server._home_answer(got, (), None, None)["home"]["gaps"]
                for name, got in (("staff", staff), ("ops", ops))}
        self.assertEqual([g["kind"] for g in home["staff"] if g["kind"].startswith("po_")],
                         ["po_impact_margin"])
        self.assertEqual([g["kind"] for g in home["ops"] if g["kind"].startswith("po_")],
                         ["po_impact"])
        # a finance reader who may not open one priced line never sees the money claim
        hidden = read(STAFF, tp.ot.ReadPolicy(
            "plx", {"api:*": frozenset({"staff"}), "bc:*": frozenset({"staff"}),
                    "api:public.app_sales_order_line": frozenset()},
            {"finance": frozenset({"staff"}), "pii": frozenset({"staff"}),
             "l5": frozenset({"staff"}), "quality_controlled": frozenset({"staff"})}))
        self.assertEqual(kinds(hidden), ["po_impact"])
        # a later run that did not finish (its sum may be a part) keeps the money wording
        real = pe._orders_at_risk

        def partial(*args):
            found = real(*args)
            for gap in found["gaps"]:
                if gap["kind"] == "po_impact_margin":
                    gap["summary"] = "affects 1 sales order, 535.00 CAD margin at stake"
            return {**found, "status": "truncated", "reasons": [pe.TIME_BUDGET]}
        with mock.patch.object(pe, "_orders_at_risk", partial):
            pe.run(path, inbox, date(2099, 2, 1), call=self.caller(s, STAFF))
        again = next(g for g in read(STAFF, tp.POLICY)["gaps"]
                     if g["kind"] == "po_impact_margin")
        self.assertEqual(again["summary"], money["summary"])

    def landed_caller(self, s, actor, landed, policy=None):
        """The reader's tool caller, with ``plx.investigate`` of the landed-cost family
        answered by ``landed(po)`` (an answer as the landed_cost plan gives it)."""
        call = self.caller(s, actor, policy)
        asked = []

        def wrapped(tool, args):
            if tool == "plx.investigate" and args.get("family") == ic.LANDED_FAMILY:
                asked.append(args["root"])
                return landed(args["root"])
            return call(tool, args)
        wrapped.asked = asked
        return wrapped

    @staticmethod
    def bought(s, ids, n, key):
        """A purchase order line that buys order ``n``'s sold item (its Portal product)."""
        po = portal_po(s, key=f"po-buy-{key}", number=f"PO8{n:05d}", ordered=None)
        pol = line(s, po, f"buy-{key}", "10", "10")
        s.link("PurchaseOrderLine.for_item", pol,
               f"Product:portal.app_product:fg-ic-{n:03d}")
        return po, pol

    @staticmethod
    def answer(pol, value, *, estimate=False, unit="CAD/ea", handle="api://x/y", cited=True):
        name = "landed_cost_estimate_line_1" if estimate else "landed_cost_line_1"
        return {"answer": [
            {"name": name, "kind": "computed", "value": value, "unit": unit,
             "basis": [pol, "PurchaseOrder:portal.app_purchase_order:other"],
             "inputs": ["unit_price_1"], "steps": ["goods + freight + duty"]},
            {"name": "unit_price_1", "kind": "property", "value": "4",
             **({"citation": {"l1_handle": handle}} if cited else {})}]}

    def landed_scan(self, s, landed, actor=STAFF, policy=None):
        path = store_file(s, self.dir.name)
        st = pe._Store(path)
        try:
            call = self.landed_caller(s, actor, landed, policy)
            found = ic.scan(st, call, margin_values=ic.store_margin_values(st),
                            landed_values=ic.investigate_landed_values(call))
        finally:
            st.close()
            path.unlink()
        return found, call.asked

    def test_the_landed_cost_engine_figure_comes_first_and_keeps_its_label(self):
        """Cost basis: the
        engine's per-unit total (5.00 CAD/ea) replaces BC unitCost (6.50 CAD/PCS): 1000.00 -
        100 x 5.00 + 250.00 - 10 x 5.00 = 700.00 CAD; an estimate says "margin estimated"."""
        for estimate, label in ((False, ic.COST_BASES[ic.LANDED_ACTUAL]),
                                (True, ic.COST_BASES[ic.LANDED_ESTIMATE])):
            with self.subTest(estimate=estimate):
                s = tp.Store()
                ids = chain_fixture(s, margin={})
                po, pol = self.bought(s, ids, 0, "a")
                found, asked = self.landed_scan(
                    s, lambda root: self.answer(pol, "5.00", estimate=estimate))
                self.assertEqual(asked, [po])
                self.assertEqual(found["margin_at_stake"], {"CAD": "700.00"})
                (margin,) = found["margins"].values()
                self.assertEqual(margin.basis, label)
                self.assertTrue(all(label in step for step in margin.steps))
                self.assertEqual(found["po_impacts"][0]["margin"]["basis"], label)
                if estimate:
                    self.assertIn("margin estimated", label)

    def test_engine_and_recorded_costs_are_never_mixed_silently(self):
        """Order 0's item has an engine figure, order 1's has only BC unitCost: each order
        keeps its own basis, the PO's total names both."""
        s = tp.Store()
        ids = chain_fixture(s, orders=2, margin={})
        _po, pol = self.bought(s, ids, 0, "a")
        found, _ = self.landed_scan(s, lambda root: self.answer(pol, "5.00"))
        self.assertEqual(found["margin_at_stake"], {"CAD": "1235.00"})   # 700.00 + 535.00
        bases = sorted(m.basis for m in found["margins"].values())
        self.assertEqual(bases, sorted([ic.COST_BASES[ic.LANDED_ACTUAL],
                                        ic.COST_BASES[ic.RECORDED_COST]]))
        basis = found["po_impacts"][0]["margin"]["basis"]
        self.assertTrue(basis.startswith("mixed basis: "), basis)
        self.assertIn(ic.COST_BASES[ic.LANDED_ACTUAL], basis)
        self.assertIn(ic.COST_BASES[ic.RECORDED_COST], basis)

    def test_engine_figures_that_disagree_or_are_unusable_fail_closed(self):
        # two purchase orders give the item different landed costs: no margin, and the
        # recorded unit cost never stands in for it
        s = tp.Store()
        ids = chain_fixture(s, margin={})
        po_a, pol_a = self.bought(s, ids, 0, "a")
        po_b, pol_b = self.bought(s, ids, 0, "b")
        found, asked = self.landed_scan(s, lambda root: self.answer(
            pol_a, "5.00") if root == po_a else self.answer(pol_b, "5.50"))
        self.assertEqual(sorted(asked), sorted([po_a, po_b]))
        self.assertEqual(found["orders_with_margin"], 0)
        self.assertEqual(found["margin_not_stated_by_reason"], {"landed_costs_disagree": 1})
        # an uncited figure, one with no per-unit unit, no ISO currency, or an error answer
        # is not used: BC unitCost, labelled as recorded
        for landed in (lambda pol: self.answer(pol, "5.00", cited=False),
                       lambda pol: self.answer(pol, "5.00", unit="CAD"),
                       lambda pol: self.answer(pol, "5.00", unit="XXX/ea"),
                       lambda pol: {"error": {"code": "denied"}}):
            with self.subTest():
                s = tp.Store()
                ids = chain_fixture(s, margin={})
                _po, pol = self.bought(s, ids, 0, "a")
                found, _ = self.landed_scan(s, lambda root, p=pol, f=landed: f(p))
                self.assertEqual(found["margin_at_stake"], {"CAD": "535.00"})
                (margin,) = found["margins"].values()
                self.assertEqual(margin.basis, ic.COST_BASES[ic.RECORDED_COST])

    def test_a_home_margin_on_the_engine_figure_cites_its_inputs_for_finance_only(self):
        s = tp.Store()
        ids = chain_fixture(s, margin={})
        _po, pol = self.bought(s, ids, 0, "a")
        # the engine figure rests on the BC item's record (finance-marked as a figure)
        handle = s.handles["Product:bc.items:fg-ic-000"][0]
        path = store_file(s, self.dir.name)
        inbox = pe.inbox_path(path)
        pe.run(path, inbox, date(2099, 2, 1), call=self.landed_caller(
            s, STAFF, lambda root: self.answer(pol, "5.00", handle=handle)))

        def read(actor, policy):
            return pe.read_inbox(inbox, path, reader=pe.reader_key(actor),
                                 may_read=lambda aud: policy.may_read(actor, aud),
                                 held=policy.held_markings(actor),
                                 decide=lambda handles: {h: None for h in handles})
        staff, ops = read(STAFF, tp.POLICY), read(OPS, NO_MONEY)
        money = next(g for g in staff["gaps"] if g["kind"] == "po_impact_margin")
        self.assertEqual(money["summary"], "affects 1 sales order, 700.00 CAD margin at stake "
                                           "(landed cost (landed-cost engine, actual)), worst 7 "
                                           "days at risk")
        self.assertFalse([g for g in ops["gaps"] if g["kind"] == "po_impact_margin"])
        self.assertNotIn("700.00", json.dumps(ops))

    def test_a_reader_scan_states_no_margin_it_was_not_shown(self):
        s = tp.Store()
        chain_fixture(s, margin={})
        path = store_file(s, self.dir.name)
        st = pe._Store(path)
        try:
            found = {}
            for name, actor, policy in (("staff", STAFF, None), ("ops", OPS, NO_MONEY)):
                call = self.caller(s, actor, policy)
                found[name] = ic.scan(st, call, margin_values=ic.object_margin_values(
                    lambda oid, c=call: c("plx.object", {"object_id": oid})))
        finally:
            st.close()
        self.assertEqual(found["staff"]["margin_at_stake"], {"CAD": "535.00"})
        self.assertEqual((found["ops"]["orders_with_margin"], found["ops"]["margin_at_stake"]),
                         (0, {}))
        self.assertEqual(found["ops"]["margin_not_stated_by_reason"],
                         {"line_amount_not_shown": 1})
        self.assertIsNone(found["ops"]["po_impacts"][0]["margin"])
        self.assertEqual(found["ops"]["orders_at_risk"], 1)   # the chain itself stays

    def test_a_reader_whose_own_lateness_verdict_is_withheld_never_sees_the_staff_verdict(self):
        """The staff run's verdict rests on a BC receipt (or on no BC receipt at all) that
        SALES may not read: SALES's own plx.process verdict is undetermined, so the inbox
        never shows it the days, the receipt date or the not-received claim."""
        from ontology.tests.test_po_lateness import receipt

        for hidden_receipt in (True, False):
            with self.subTest(hidden_receipt=hidden_receipt), \
                    tempfile.TemporaryDirectory() as tmp:
                s = tp.Store()
                ids = chain_fixture(s, required="2099-01-20")
                if hidden_receipt:
                    receipt(s, ids["po"], "rc-hidden", "2099-01-29",
                            binding="bc.purchase_receipts")
                    # the line fully received: the staff verdict is late, received 01-29
                    s.conn.execute(
                        "UPDATE object_values SET value = '100' WHERE property = "
                        "'received_quantity' AND object_id LIKE 'PurchaseOrderLine:%'")
                path = store_file(s, tmp)
                inbox = pe.inbox_path(path)
                sales_verdict = self.caller(s, SALES)(
                    "plx.process", {"from": ids["po"], "process": "po_delivery_window"})
                self.assertEqual([e["state"] for e in sales_verdict["lateness"]],
                                 ["undetermined"])
                pe.run(path, inbox, date(2099, 2, 1), call=self.caller(s, STAFF))
                shown = {}
                for name, actor, policy in (("staff", STAFF, tp.POLICY), ("ops", OPS, NO_MONEY),
                                            ("sales", SALES, tp.POLICY)):
                    got = pe.read_inbox(
                        inbox, path, reader=pe.reader_key(actor),
                        may_read=lambda aud, a=actor, p=policy: p.may_read(a, aud),
                        held=policy.held_markings(actor),
                        decide=lambda handles: {h: None for h in handles})
                    shown[name] = [g for g in got["gaps"] if g["kind"] == "order_at_risk"]
                days = "9 days" if hidden_receipt else "12 days"
                for name in ("staff", "ops"):
                    self.assertEqual(len(shown[name]), 1, name)
                    self.assertTrue(shown[name][0]["summary"].startswith(days))
                self.assertEqual(shown["sales"], [])

    def test_an_order_at_risk_gap_without_recorded_requirements_shows_to_nobody(self):
        s = tp.Store()
        chain_fixture(s, required="2099-01-20")
        path = store_file(s, self.dir.name)
        inbox = pe.inbox_path(path)
        pe.run(path, inbox, date(2099, 2, 1), call=self.caller(s, STAFF))

        def staff_sees():
            got = pe.read_inbox(inbox, path, reader=pe.reader_key(STAFF),
                                may_read=lambda aud: tp.POLICY.may_read(STAFF, aud),
                                held=tp.POLICY.held_markings(STAFF),
                                decide=lambda handles: {h: None for h in handles})
            return [g for g in got["gaps"] if g["kind"] == "order_at_risk"]
        self.assertEqual(len(staff_sees()), 1)
        with closing(sqlite3.connect(inbox)) as conn:
            conn.execute("DELETE FROM gap_requires")
            conn.commit()
        self.assertEqual(staff_sees(), [])
        with closing(sqlite3.connect(inbox)) as conn:   # an inbox written before the table
            conn.execute("DROP TABLE gap_requires")
            conn.commit()
        self.assertEqual(staff_sees(), [])

    def test_without_a_caller_the_detector_is_not_configured(self):
        s = tp.Store()
        chain_fixture(s)
        path = store_file(s, self.dir.name)
        summary = pe.run(path, pe.inbox_path(path), date(2099, 2, 1))
        self.assertEqual(summary["detectors"]["orders_at_risk"], "not_configured")
        self.assertIsNone(summary["top_finding"])


if __name__ == "__main__":
    unittest.main()
