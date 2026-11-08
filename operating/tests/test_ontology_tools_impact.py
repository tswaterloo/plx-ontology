"""plx.around modes what_if / why (operating/ontology_tools_impact.py) over a hand-filled
store on the real compiled schema (the impact-chain fixture: late PO -> line -> item ->
FileMaker component -> assembly order -> allocation -> order line -> order = Portal order).
Every answer is validated against operating/ontology_tool_contracts.json by the tool itself
and again here. Evidence label: isolated-fixture.
"""

from __future__ import annotations

from decimal import Decimal
import json
import re
import time
import unittest

from ontology.json_contract import validate
from operating import ontology_tools as ot
from operating import ontology_tools_impact as oimp
from operating import ontology_tools_traverse as tt
from operating.tests import test_impact_chain as tic
from tests import test_ontology_tools_process as tp

CONTRACTS = tic.CONTRACTS
STAFF, SALES = tp.STAFF, tp.SALES


def setUpModule():
    tp.setUpModule()


def table_of(store, policy=None, **options):
    table: dict = {}
    base = ot.OntologyTools(ot.StoreReader(store.conn), policy or tp.POLICY, schema=tp.SCHEMA,
                            contracts=CONTRACTS, clock=lambda: tp.NOW)
    ot.register(table, base)
    traverse = tt.TraverseTools(base)
    tt.register(table, traverse)
    oimp.register(table, traverse, schema=tp.SCHEMA, contracts=CONTRACTS, **options)
    return table


def ask(table, arguments, actor=STAFF):
    out = table["plx.around"]["handler"](arguments, actor)
    assert validate(out, CONTRACTS["tools"]["plx.around"]["output"], root=CONTRACTS) == [], out
    return out


def delay(root, days=10, **more):
    return {"from": root, "mode": "what_if", "change": {"kind": "delay", "days": days}, **more}


class WhatIfTests(unittest.TestCase):
    def test_every_order_once_at_n_1_10_60_with_revenue_and_slip(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                s = tp.Store()
                ids = tic.chain_fixture(s, orders=n)
                started = time.monotonic()
                out = ask(table_of(s), delay(ids["po"], 10))
                self.assertLess(time.monotonic() - started, 20)
                # order n is required 2099-01-25 less n days; the late PO cannot arrive
                # before today (2099-02-01), +10 days = 2099-02-11: every order slips
                total = out["completeness"]["total"]
                self.assertEqual(total, n)
                listed = [a["object_id"] for a in out["affected"]]
                self.assertEqual(len(listed), len(set(listed)))
                self.assertLessEqual(set(listed), set(ids["portal_orders"]) | set(ids["orders"]))
                # an order and its FileMaker twin are one order, never two
                clusters = {frozenset([a["object_id"], *a["twins"]]) for a in out["affected"]}
                self.assertEqual(len(clusters), len(listed))
                for entry in out["affected"]:
                    self.assertEqual(entry["tier"], "firm")
                    self.assertTrue(entry["slips"])
                    self.assertEqual(entry["new_material_date"], "2099-02-11")
                    need = entry["required_date"]
                    self.assertEqual(entry["late_by_days"],
                                     (tp.dt.date(2099, 2, 11) - tp.dt.date.fromisoformat(need)).days)
                money = {m["measure"]: m for m in out["money"]}["revenue_at_risk"]
                self.assertEqual((money["currency"], money["orders"], money["tier"]),
                                 ("CAD", n, "firm"))
                self.assertEqual(Decimal(money["amount"]), Decimal("1250.50") * n)
                self.assertEqual(out["completeness"]["complete"], n <= oimp.MAX_LISTED)
                if n > oimp.MAX_LISTED:
                    self.assertEqual(out["completeness"]["reason"], "list_cap")
                self.assertLess(len(json.dumps(out).encode()), CONTRACTS["limits"]["max_result_bytes"])

    def test_an_order_required_after_the_new_date_does_not_slip_and_adds_no_revenue(self):
        s = tp.Store()
        ids = tic.chain_fixture(s, orders=1, required="2099-03-30")
        out = ask(table_of(s), delay(ids["po"], 5))
        entry, = out["affected"]
        self.assertFalse(entry["slips"])
        self.assertEqual(entry["late_by_days"], 0)
        self.assertNotIn("revenue", entry)
        self.assertEqual(out["money"], [])

    def test_a_closed_assembly_order_puts_nothing_downstream_at_risk(self):
        s = tp.Store()
        ids = tic.chain_fixture(s, orders=3, ao_status="Realized")
        out = ask(table_of(s), delay(ids["po"]))
        self.assertEqual(out["affected"], [])
        self.assertEqual(out["money"], [])

    def test_a_lead_link_makes_the_chain_lead_never_firm(self):
        s = tp.Store()
        ids = tic.chain_fixture(s, orders=2)
        s.conn.execute("UPDATE links SET tier = 'stored_by_text_join' "
                       "WHERE link_type = 'PurchaseOrderLine.for_item'")
        out = ask(table_of(s), delay(ids["po"]))
        self.assertEqual(len(out["affected"]), 2)
        self.assertEqual({a["tier"] for a in out["affected"]}, {"lead"})
        self.assertEqual({m["tier"] for m in out["money"]}, {"lead"})

    def test_price_change_is_decimal_per_currency_and_a_customer_loss_walks_upstream(self):
        s = tp.Store()
        ids = tic.chain_fixture(s, orders=2)
        s.conn.execute("UPDATE object_values SET currency = 'USD' WHERE object_id = ? AND "
                       "property = 'total_amount'", (ids["portal_orders"][1],))
        item = "Product:portal.app_product:p-ic"
        out = ask(table_of(s), {"from": item, "mode": "what_if",
                                "change": {"kind": "price_change", "percent": "-10"}})
        money = {(m["measure"], m["currency"]): m["amount"] for m in out["money"]}
        self.assertEqual(money, {("revenue_change", "CAD"): "-125.05",
                                 ("revenue_change", "USD"): "-125.05"})
        customer = s.obj("Customer:portal.app_customer:cu-1")
        for so in ids["portal_orders"]:
            s.link("SalesOrder.ordered_by", so, customer)
        lost = ask(table_of(s), {"from": customer, "mode": "what_if",
                                 "change": {"kind": "customer_lost"}})
        self.assertEqual(sorted(a["object_id"] for a in lost["affected"]),
                         sorted(ids["portal_orders"]))
        self.assertEqual(lost["customers"][0]["object_id"], customer)
        self.assertEqual({(m["measure"], m["currency"]) for m in lost["money"]},
                         {("revenue_lost", "CAD"), ("revenue_lost", "USD")})


class RestrictedReaderTests(unittest.TestCase):
    def test_a_reader_sees_only_what_it_may_and_no_withheld_count(self):
        s = tp.Store()
        ids = tic.chain_fixture(s, orders=4)
        hidden = s.obj("SalesOrder:bc.sales_orders:hidden-1")   # bc:* is staff only
        s.val(hidden, "required_date", "date", "2099-01-20")
        s.val(hidden, "total_amount", "money", "999.00", currency="CAD", markings='["finance"]')
        line = "SalesOrderLine:portal.fm_order_entry_item:oe-ic-000-1"
        s.link("SalesOrderLine.line_of", line, hidden)
        table = table_of(s)
        staff = ask(table, delay(ids["po"]))
        sales = ask(table, delay(ids["po"]), SALES)
        self.assertIn(hidden, json.dumps(staff))
        text = json.dumps(sales)
        self.assertNotIn(hidden, text)
        # the hidden order's finance-marked amount reaches no field (asserted on the fields:
        # fixture PO numbers are hashed and may hold any digits)
        self.assertNotIn("999.00", {e.get("revenue", {}).get("amount") for e in sales["affected"]})
        self.assertNotIn("999.00", {m["amount"] for m in sales["money"]})
        self.assertNotIn("1250.50", text)       # finance-marked totals
        self.assertNotIn('"withheld":', text)    # no withheld count object anywhere
        self.assertEqual(sales["money"], [])
        self.assertEqual(sales["completeness"]["total"], 4)
        # a walk with records hidden from the reader is never complete, and says so once,
        # with no count
        self.assertTrue(staff["completeness"]["complete"])
        self.assertEqual((sales["completeness"]["complete"], sales["completeness"]["reason"]),
                         (False, "withheld_by_permission"))
        hidden_gaps = [g for g in sales["gaps"] if g["reason"] == "withheld_by_permission"]
        self.assertEqual(len(hidden_gaps), 1)
        self.assertEqual(hidden_gaps[0]["state"], "inaccessible")
        self.assertIsNone(re.search(r"\d", hidden_gaps[0]["detail"]))
        self.assertIn("value_not_readable", {g["reason"] for g in sales["gaps"]})
        self.assertNotIn("absent_in_source", {g["state"] for g in sales["gaps"]})
        why = ask(table, {"from": ids["portal_orders"][0], "mode": "why"}, SALES)
        self.assertNotIn(hidden, json.dumps(why))
        # a record the reader may not read is not found, the same as one that does not exist
        refused = ask(table, delay(hidden), SALES)
        missing = ask(table, delay("SalesOrder:bc.sales_orders:nope"), STAFF)
        self.assertEqual(refused["error"]["code"], missing["error"]["code"])
        self.assertEqual(refused["error"]["message"], missing["error"]["message"])


class WhyTests(unittest.TestCase):
    def test_a_late_po_is_a_firm_cited_cause_and_missing_links_are_unknown(self):
        s = tp.Store()
        ids = tic.chain_fixture(s, orders=1)
        out = ask(table_of(s), {"from": ids["portal_orders"][0], "mode": "why"})
        cause = out["causes"][0]
        self.assertEqual((cause["cause"], cause["tier"]), ("late_supply", "firm"))
        self.assertEqual(cause["object_id"].split(":")[0], "PurchaseOrder")
        self.assertIn("row.expected_date", {c["field_path"] for c in cause["citations"]})
        self.assertIn("row.required_date", {c["field_path"] for c in cause["citations"]})
        self.assertEqual(len({c["cause"] for c in out["causes"]}), len(out["causes"]))
        states = {g["state"] for g in out["gaps"]}
        self.assertEqual(states, {"unknown"})
        reasons = {g["reason"] for g in out["gaps"]}
        self.assertIn("no_visible_mail", reasons)     # no-reply undecided, never "none"
        self.assertIn("next_link_not_found", reasons)  # no supplier link: unknown

    def test_short_receipt_qc_hold_and_freight_are_causes_with_their_tiers(self):
        s = tp.Store()
        ids = tic.chain_fixture(s, orders=1)
        s.conn.execute("DELETE FROM object_values WHERE object_id = ? AND property = "
                       "'received_quantity'", (ids["line"],))
        s.val(ids["line"], "received_quantity", "quantity", "40", unit="ea", role="received")
        s.conn.execute("UPDATE object_values SET unit = 'ea' WHERE object_id = ? AND property = "
                       "'ordered_quantity'", (ids["line"],))
        rl = s.obj("ReceiptLine:portal.app_receipt_line:rl-1")
        lot = s.obj("Lot:portal.app_receipt_line.supplier_lot:lot-1")
        s.link("ReceiptLine.receives_line", rl, ids["line"])
        s.link("ReceiptLine.received_lot", rl, lot)
        s.val(lot, "qc_status", "text", "Quarantine", markings='["quality_controlled"]')
        bill = s.obj("FreightBill:graph_mail.freight_bills:fb-1")
        s.link("FreightBill.for_purchase_order", bill, ids["po"], tier="lead", kind="mention")
        out = ask(table_of(s), {"from": ids["portal_orders"][0], "mode": "why"})
        got = {c["cause"]: c for c in out["causes"]}
        self.assertLessEqual({"late_supply", "short_receipt", "qc_hold", "freight"}, set(got))
        self.assertEqual(got["freight"]["tier"], "lead")
        self.assertEqual(got["short_receipt"]["object_id"], ids["line"])
        # the hold is quality-controlled: a reader without the marking never sees it
        sales = ask(table_of(s), {"from": ids["portal_orders"][0], "mode": "why"}, SALES)
        self.assertNotIn("qc_hold", {c["cause"] for c in sales["causes"]})


class BoundsAndInputTests(unittest.TestCase):
    def test_depth_and_time_caps_flag_a_partial_answer(self):
        s = tp.Store()
        ids = tic.chain_fixture(s, orders=2)
        shallow = ask(table_of(s, max_depth=3), delay(ids["po"]))
        self.assertEqual((shallow["completeness"]["complete"], shallow["completeness"]["reason"]),
                         (False, "depth_cap"))
        self.assertEqual(shallow["gaps"][0]["reason"], "depth_cap")
        ticks = iter([0.0, 0.0] + [100.0] * 10_000)
        late = ask(table_of(s, clock=lambda: next(ticks), budget_ms=100), delay(ids["po"]))
        self.assertEqual(late["completeness"]["reason"], "time_budget")
        self.assertFalse(late["completeness"]["complete"])

    def test_unknown_or_unexpected_input_fails_closed(self):
        s = tp.Store()
        ids = tic.chain_fixture(s, orders=1)
        table = table_of(s)
        po, so = ids["po"], ids["portal_orders"][0]
        for arguments in (
                {"from": po, "mode": "guess"},
                {"from": po, "mode": "what_if"},
                {"from": po, "mode": "what_if", "change": {"kind": "flood"}},
                {"from": po, "mode": "what_if", "change": {"kind": "delay"}},
                {"from": po, "mode": "what_if", "change": {"kind": "delay", "days": 0}},
                {"from": po, "mode": "what_if", "change": {"kind": "delay", "days": 3,
                                                          "percent": "5"}},
                {"from": po, "mode": "what_if", "change": {"kind": "price_change",
                                                          "percent": "0"}},
                {"from": po, "mode": "what_if", "change": {"kind": "customer_lost"}},
                {"from": so, "mode": "what_if", "change": {"kind": "out_of_stock"}},
                {"from": so, "mode": "why", "change": {"kind": "delay", "days": 1}},
                {"from": po, "mode": "why", "path": [{"link": "line_of"}]},
                {"from": "set:abcdefgh12", "mode": "why"},
                {"from": "Email:graph.messages:x", "mode": "why"},
                {"from": po, "path": [{"link_type": "PurchaseOrderLine.line_of",
                                       "direction": "reverse"}], "change": {"kind": "out_of_stock"}},
                {"from": po, "mode": "why", "unexpected": 1}):
            with self.subTest(arguments=arguments):
                self.assertEqual(ask(table, arguments)["error"]["code"], "invalid_input")

    def test_the_flow_file_fails_closed_on_anything_the_schema_does_not_declare(self):
        doc = json.loads(oimp.FLOWS_PATH.read_text("utf-8"))
        self.assertTrue(oimp.load_flows(tp.SCHEMA, doc).stages)
        broken = [
            {**doc, "stages": [*doc["stages"], {"type": "NoSuchType", "role": "x"}]},
            {**doc, "stages": [{**doc["stages"][1], "dates": ["no_such_date"]},
                               *doc["stages"][2:]]},
            {**doc, "excluded_links": ["No.such_link"]},
            {**doc, "evidence": [{"cause": "freight", "from": "PurchaseOrder",
                                  "path": [["SalesOrderLine.line_of", "reverse"]]}]},
            {**doc, "surprise": True},
        ]
        for bad in broken:
            with self.assertRaises(oimp.FlowError):
                oimp.load_flows(tp.SCHEMA, bad)

    def test_the_listing_describes_both_modes_within_the_limits(self):
        around = CONTRACTS["tools"]["plx.around"]
        self.assertLessEqual(len(around["description"]), CONTRACTS["limits"]["max_description_chars"])
        self.assertIn("What if", around["description"])
        self.assertIn("Why is", around["description"])
        self.assertEqual(around["input"]["properties"]["mode"]["enum"], ["what_if", "why"])


if __name__ == "__main__":
    unittest.main()
