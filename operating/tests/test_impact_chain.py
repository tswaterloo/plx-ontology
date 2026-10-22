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

from ontology.json_contract import validate
from operating import impact_chain as ic
from operating import ontology_tools as ot
from operating import ontology_tools_investigate as oi
from operating import ontology_tools_process as op
from operating import ontology_tools_traverse as tt
from operating import proactive_engine as pe
from ontology.tests.test_po_lateness import confirm, fm_twin, line, portal_po
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


def chain_fixture(s, *, orders=1, required="2099-01-25", alloc_line=True, so_status="CONFIRMED",
                  ao_status="Committed", value="1250.50", key="ic",
                  planned="2099-01-15", promise="2099-01-20"):
    """late PO -> line -> Portal item = FM packaging <- component -> FM AO <- allocation(s)
    -> FM order line -> FM order = Portal order (required date, finance-marked total)."""
    ids = {"po": late_po(s, key, planned, promise), "orders": [], "portal_orders": []}
    pol = line(s, ids["po"], f"{key}-1", "100", "0")
    item = s.obj(f"Product:portal.app_product:p-{key}")
    s.link("PurchaseOrderLine.for_item", pol, item)
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
        ids["orders"].append(fm_so)
        ids["portal_orders"].append(so)
    return ids


# Every audience, every marking but finance: sees the whole chain, never the money.
OPS = tp.Actor("plx", frozenset({"ops"}))
NO_MONEY = tp.ot.ReadPolicy(
    "plx", {"api:*": frozenset({"staff", "ops"}), "bc:*": frozenset({"staff", "ops"})},
    {"finance": frozenset({"staff"}), "pii": frozenset({"staff", "ops"}),
     "l5": frozenset({"staff", "ops"}), "quality_controlled": frozenset({"staff", "ops"})})


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
        out = table_of(store, policy=policy)["plx.investigate"]["handler"]({"family": "F16", "root": root},
                                                            actor)
        self.assertEqual(validate(out, CONTRACTS["tools"]["plx.investigate"]["output"],
                                  root=CONTRACTS), [], json.dumps(out)[:3000])
        self.assertNotIn("error", out, json.dumps(out)[:2000])
        return out, {e["name"]: e for e in out["answer"]}

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
                ids = chain_fixture(s, orders=n)
                path = store_file(s, self.dir.name)
                st = pe._Store(path)
                try:
                    call = self.caller(s, STAFF)
                    started = time.monotonic()
                    found = ic.scan(st, call, value=lambda oid: call(
                        "plx.object", {"object_id": oid}))
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
