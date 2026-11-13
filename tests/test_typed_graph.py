"""Typed models over the served graph (operating/typed_graph), read through the tools.

The fixture is a projection store (ontology/store_ddl.py) holding real-shaped records of
the real schema's types (PurchaseOrder, PurchaseOrderLine, ReceiptLine, Lot, Supplier,
Product) under the compiled schema, read by the real OntologyTools (plx.object) and
TraverseTools (plx.around) with a ReadPolicy: the same access path a host serves. All
identifiers are synthetic.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
import unittest
from pathlib import Path

from ontology import store_ddl
from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_traverse as tt
from operating.typed_graph import (CardinalityError, GraphReadError, GraphRepository,
                                   Money, Quantity, SchemaMismatch)
from operating.typed_graph import models as m

ROOT = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2099, 3, 1, 12, 0, tzinfo=dt.timezone.utc)
T0 = "2099-01-01T00:00:00Z"


def _schema():
    from ontology import schema as loader
    return ot.SchemaView.from_compiled(loader.load_schema())


SCHEMA = _schema()
POLICY = ot.ReadPolicy(
    tenant="plx", audiences={"api:*": {"ops", "full"}, "bc:*": {"full"}},
    markings={"finance": {"full"}, "purchasing_prices": {"full"}})
FULL = Actor("plx", frozenset({"full"}))
OPS = Actor("plx", frozenset({"ops"}))


def _h(text):
    return hashlib.sha256(text.encode()).hexdigest()


class Store:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        store_ddl.create(self.conn)
        self.conn.execute("INSERT INTO store_meta VALUES ('schema_digest', ?)",
                          ("sha256:" + (SCHEMA.digest_hex or _h(SCHEMA.digest)),))
        fresh = [{"source": s, "captured_at": "2099-03-01T11:00:00Z", "lag_seconds": 3600,
                  "completeness": "complete", "stale": False} for s in ("portal_api", "bc")]
        self.conn.execute("INSERT INTO store_meta VALUES ('freshness', ?)", (json.dumps(fresh),))
        self.conn.execute("INSERT INTO store_meta VALUES ('build_report', ?)",
                          (json.dumps({"counts": {}, "gaps": []}),))
        self.handles = {}

    def obj(self, object_id, *, audience="api:public.app_purchase_order", markings="[]",
            handle=None):
        otype, binding, native = object_id.split(":", 2)
        handle = handle or f"api://public.{binding.split('.')[-1]}/{native}@0000000000000001-1"
        self.conn.execute("INSERT INTO objects VALUES (?,?,?,?,?,?,?)",
                          (object_id, otype, binding, json.dumps([native]), audience, T0, None))
        self.conn.execute("INSERT INTO object_versions VALUES (?,?,?,?,?,?,?)",
                          (object_id, handle, _h(handle), "gold_etl_from_fm", markings, T0, None))
        self.handles[object_id] = handle
        return object_id

    def val(self, object_id, prop, value_type, value, *, markings="[]", currency=None,
            unit=None, role=None, field_path=None):
        handle = self.handles[object_id]
        path = field_path or f"row.{prop}"
        self.conn.execute(
            "INSERT INTO object_values VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (object_id, prop, value_type, value, unit, role, currency, None, None, handle,
             path, _h(f"{handle}|{path}|{value}"), "gold_etl_from_fm", object_id.split(":")[1],
             "as_is", 0, markings, T0, None))

    def link(self, link_id, link_type, src, dst, tier="verified"):
        hs, hd = self.handles[src], self.handles[dst]
        reason = [{"feature": "pair", "agrees": True, "citations": [
            {"l1_handle": hs, "field_path": "row.id", "digest": _h(hs),
             "lineage": "gold_etl_from_fm"}]}]
        self.conn.execute(
            "INSERT INTO links(link_id, link_type, from_object, to_object, tier, binding_kind, "
            "reason, why_not, evidence_handles, markings, recorded_at, superseded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (link_id, link_type, src, dst, tier, "fk", json.dumps(reason), "[]",
             json.dumps([hs, hd]), "[]", T0, None))


PO = "PurchaseOrder:portal.app_purchase_order:po-1"
SUP = "Supplier:portal.app_supplier:s-1"
LOT = "Lot:portal.app_receipt_line.supplier_lot:lot-1"
RL = "ReceiptLine:portal.app_receipt_line:rl-1"


def line_id(n):
    return f"PurchaseOrderLine:portal.app_purchase_order_line:pol-{n}"


def build(lines=2, extra=None):
    s = Store()
    s.obj(PO)
    s.val(PO, "po_number", "identifier", "PO-9001")
    s.val(PO, "order_date", "date", "2099-01-15")
    s.val(PO, "total_cost", "money", "1234.50", currency="CAD",
          markings='["purchasing_prices"]')
    s.val(PO, "bc_fully_received", "boolean", "true")
    s.obj(SUP, audience="api:public.app_supplier")
    s.val(SUP, "name", "text", "Fixture Supplies Ltd")
    s.val(SUP, "lead_time_days", "integer", "14")
    s.link("l-po-sup", "PurchaseOrder.ordered_from", PO, SUP)
    for n in range(1, lines + 1):
        oid = s.obj(line_id(n), audience="api:public.app_purchase_order_line")
        s.val(oid, "ordered_quantity", "quantity", "25.5", unit="kg", role="ordered")
        s.val(oid, "unit_price", "money", "4.10", currency="USD",
              markings='["purchasing_prices"]')
        s.link(f"l-line-{n}", "PurchaseOrderLine.line_of", oid, PO)
    s.obj(RL, audience="api:public.app_receipt_line")
    s.val(RL, "received_quantity", "quantity", "25.5", unit="kg", role="received")
    s.obj(LOT, audience="api:public.app_receipt_line")
    s.val(LOT, "lot_number", "identifier", "LOT-77")
    s.val(LOT, "expiry_date", "date", "2100-06-30")
    s.link("l-rl-lot", "ReceiptLine.received_lot", RL, LOT)
    if lines:
        s.link("l-rl-pol", "ReceiptLine.receives_line", RL, line_id(1))
    if extra:
        extra(s)
    s.conn.commit()
    return s


class Reads:
    """The host's tool table, reduced to the two tools the repository calls."""

    def __init__(self, store):
        base = ot.OntologyTools(ot.StoreReader(store.conn), POLICY,
                                item_gate=lambda actor, handle: True, schema=SCHEMA,
                                grammar=None, clock=lambda: NOW)
        self.traverse = tt.TraverseTools(base)
        self.base = base
        self.calls = []

    def call(self, name, arguments, actor):
        self.calls.append(name)
        if name == "plx.around":
            return self.traverse.call(name, arguments, actor)
        return self.base.call(name, arguments, actor)


def repo(store, actor=FULL):
    return GraphRepository(Reads(store), actor)


class RoundTrip(unittest.TestCase):
    def test_typed_values_keep_their_source(self):
        po = repo(build()).get(PO, m.PurchaseOrder)
        self.assertIsInstance(po, m.PurchaseOrder)
        self.assertEqual(po.po_number.value, "PO-9001")
        self.assertEqual(po.order_date.value, dt.date(2099, 1, 15))
        self.assertEqual(po.total_cost.value, Money(Decimal("1234.50"), "CAD"))
        self.assertIsInstance(po.total_cost.value.amount, Decimal)
        self.assertIs(po.bc_fully_received.value, True)
        self.assertEqual(po.total_cost.citation.l1_handle,
                         "api://public.app_purchase_order/po-1@0000000000000001-1")
        self.assertEqual(po.total_cost.markings, ("purchasing_prices",))
        self.assertEqual(po.conflicts, frozenset())
        self.assertTrue(po.complete)
        supplier = po.ordered_from()
        self.assertIsInstance(supplier, m.Supplier)
        self.assertEqual(supplier.lead_time_days.value, 14)
        self.assertEqual(supplier.name.value, "Fixture Supplies Ltd")
        self.assertTrue(supplier.name.untrusted)   # text stays marked as source text
        line = po.lines()[0]
        self.assertEqual(line.ordered_quantity.value,
                         Quantity(Decimal("25.5"), "kg", "ordered"))
        self.assertEqual(line.unit_price.value, Money(Decimal("4.10"), "USD"))

    def test_relationships_walk_both_ways(self):
        r = repo(build())
        line = r.get(line_id(1), m.PurchaseOrderLine)
        self.assertEqual(line.line_of().object_id, PO)
        receipt_lines = line.receipt_lines()
        self.assertEqual([x.object_id for x in receipt_lines], [RL])
        lot = receipt_lines[0].received_lot()
        self.assertEqual(lot.lot_number.value, "LOT-77")
        self.assertEqual(lot.expiry_date.value, dt.date(2100, 6, 30))
        self.assertEqual([x.object_id for x in lot.receiving_lines()], [RL])
        certificates = r.get(PO, m.PurchaseOrder).certificates_of_analysis()
        self.assertEqual((list(certificates), certificates.complete), ([], True))

    def test_a_wrong_model_or_unknown_object_type_fails(self):
        r = repo(build())
        with self.assertRaises(SchemaMismatch):
            r.get(PO, m.Supplier)
        bare = GraphRepository(Reads(build()), FULL, models={"Supplier": m.Supplier})
        with self.assertRaises(SchemaMismatch):
            bare.get(PO)


class Cardinality(unittest.TestCase):
    def test_declared_ends_are_one_or_many(self):
        self.assertEqual(m.PurchaseOrderLine.line_of.__annotations__["return"],
                         "Optional[PurchaseOrder]")
        self.assertEqual(m.PurchaseOrder.lines.__annotations__["return"],
                         "Related[PurchaseOrderLine]")
        self.assertEqual(m.PurchaseOrder.certificates_of_analysis.__annotations__["return"],
                         "Related[CertificateOfAnalysis]")   # N:M

    def test_a_to_one_link_reaching_two_objects_fails_closed(self):
        other = "PurchaseOrder:portal.app_purchase_order:po-2"

        def second_parent(s):
            s.obj(other)
            s.val(other, "po_number", "identifier", "PO-9002")
            s.link("l-line-1b", "PurchaseOrderLine.line_of", line_id(1), other)
        r = repo(build(extra=second_parent))
        line = r.get(line_id(1), m.PurchaseOrderLine)
        with self.assertRaises(CardinalityError) as raised:
            line.line_of()
        self.assertEqual(set(raised.exception.found), {PO, other})

    def test_a_to_one_read_cut_off_raises_instead_of_returning_one(self):
        store = build()
        reads = Reads(store)
        real = reads.call

        def trimmed(name, arguments, actor):
            out = real(name, arguments, actor)
            if name == "plx.around":
                out = dict(out)
                out["completeness"] = {"complete": False, "next_cursor": "c1"}
            return out
        reads.call = trimmed
        line = GraphRepository(reads, FULL).get(line_id(1), m.PurchaseOrderLine)
        with self.assertRaises(GraphReadError):
            line.line_of()

    def test_a_lead_is_not_a_second_parent(self):
        other = "PurchaseOrder:portal.app_purchase_order:po-2"

        def lead_parent(s):
            s.obj(other)
            s.link("l-line-1b", "PurchaseOrderLine.line_of", line_id(1), other, tier="lead")
        line = repo(build(extra=lead_parent)).get(line_id(1), m.PurchaseOrderLine)
        self.assertEqual(line.line_of().object_id, PO)

    def test_lines_at_1_10_60_none_lost_or_doubled(self):
        for count in (1, 10, 60):
            r = repo(build(lines=count))
            started = time.monotonic()
            lines = r.get(PO, m.PurchaseOrder).lines()
            elapsed = time.monotonic() - started
            ids = [x.object_id for x in lines]
            self.assertEqual(sorted(ids), sorted(line_id(n) for n in range(1, count + 1)))
            self.assertEqual(len(ids), len(set(ids)))
            self.assertTrue(lines.complete, count)
            self.assertLess(elapsed, 30, f"{count} lines took {elapsed:.1f}s")

    def test_a_walk_stopped_by_the_page_limit_says_incomplete(self):
        r = GraphRepository(Reads(build(lines=60)), FULL, max_pages=1)
        lines = r.get(PO, m.PurchaseOrder).lines()
        self.assertLess(len(lines), 60)
        self.assertGreater(len(lines), 0)
        self.assertFalse(lines.complete)


class Permissions(unittest.TestCase):
    def _store(self):
        def hidden(s):
            bc = "PurchaseOrderLine:bc.purchase_order_lines:bc-1"
            s.obj(bc, audience="bc:company_a/purchaseOrderLines",
                  handle="bc://company_a/purchaseOrderLines/bc-1@cap-1")
            s.link("l-line-bc", "PurchaseOrderLine.line_of", bc, PO)
            marked = "PurchaseOrderLine:portal.app_purchase_order_line:pol-fin"
            s.obj(marked, audience="api:public.app_purchase_order_line",
                  markings='["finance"]')
            s.link("l-line-fin", "PurchaseOrderLine.line_of", marked, PO)
        return build(extra=hidden)

    def test_a_restricted_reader_sees_only_what_the_tools_show_them(self):
        store = self._store()
        full = repo(store, FULL).get(PO, m.PurchaseOrder)
        ops = repo(store, OPS).get(PO, m.PurchaseOrder)
        self.assertIsNotNone(full.total_cost)
        self.assertIsNone(ops.total_cost)                      # purchasing_prices
        self.assertNotIn("total_cost", ops.values)
        self.assertTrue(ops.withheld and ops.withheld.get("properties"))
        self.assertEqual(ops.po_number.value, "PO-9001")
        full_lines = {x.object_id for x in full.lines()}
        ops_lines = ops.lines()
        self.assertEqual(len(full_lines), 4)
        self.assertEqual({x.object_id for x in ops_lines}, {line_id(1), line_id(2)})
        for line in ops_lines:
            self.assertIsNone(line.unit_price)
            self.assertNotIn("unit_price", line.values)
            self.assertNotIn("4.10", json.dumps({k: [c.raw for c in v]
                                                for k, v in line.values.items()}))
        self.assertIsNotNone(ops_lines.withheld)

    def test_an_object_the_reader_may_not_see_is_none_like_a_missing_one(self):
        store = self._store()
        ops = repo(store, OPS)
        self.assertIsNone(ops.get("PurchaseOrderLine:bc.purchase_order_lines:bc-1"))
        self.assertIsNone(ops.get("PurchaseOrderLine:portal.app_purchase_order_line:pol-fin"))
        self.assertIsNone(ops.get("PurchaseOrderLine:portal.app_purchase_order_line:nope"))
        self.assertIsNotNone(repo(store, FULL).get(
            "PurchaseOrderLine:bc.purchase_order_lines:bc-1"))

    def test_a_walk_from_a_hidden_object_reaches_nothing(self):
        store = self._store()
        full = repo(store, FULL)
        hidden_line = full.get("PurchaseOrderLine:bc.purchase_order_lines:bc-1")
        ops = repo(store, OPS)
        rebound = m.PurchaseOrderLine(object_id=hidden_line.object_id, _repo=ops)
        self.assertIsNone(rebound.line_of())

    def test_a_hidden_second_parent_is_not_revealed_by_a_cardinality_error(self):
        hidden = "PurchaseOrder:bc.purchase_orders:bc-po"

        def bc_parent(s):
            s.obj(hidden, audience="bc:company_a/purchaseOrders",
                  handle="bc://company_a/purchaseOrders/bc-po@cap-1")
            s.link("l-line-bc-po", "PurchaseOrderLine.line_of", line_id(1), hidden)
        store = build(extra=bc_parent)
        ops_line = repo(store, OPS).get(line_id(1), m.PurchaseOrderLine)
        self.assertEqual(ops_line.line_of().object_id, PO)
        full_line = repo(store, FULL).get(line_id(1), m.PurchaseOrderLine)
        with self.assertRaises(CardinalityError):
            full_line.line_of()

    def test_every_read_is_a_tool_call(self):
        reads = Reads(build())
        GraphRepository(reads, OPS).get(PO, m.PurchaseOrder).lines()
        self.assertEqual(set(reads.calls), {"plx.object", "plx.around"})


class FailClosed(unittest.TestCase):
    def test_values_that_do_not_parse_never_reach_a_typed_field(self):
        prod = "Product:portal.app_product:p-1"

        def odd(s):
            s.obj(prod, audience="api:public.app_product")
            s.val(prod, "product_type", "enum", "NOT_A_TYPE")
            s.val(prod, "bc_unit_cost", "money", "twelve", currency="CAD",
                  markings='["finance"]')
        product = repo(build(extra=odd)).get(prod, m.Product)
        # served as stored: the model refuses the undeclared member and keeps it raw
        self.assertIsNone(product.product_type.value)
        self.assertEqual((product.product_type.problem, product.product_type.raw),
                         ("enum_value_undeclared", "NOT_A_TYPE"))
        # refused by the tool: no value, its gap carried on the model
        self.assertIsNone(product.bc_unit_cost)
        self.assertIn("Product.bc_unit_cost", {g["subject"] for g in product.gaps})

    def test_the_model_keeps_an_unparseable_value_raw_with_a_problem(self):
        from operating.typed_graph.runtime import parse_value
        citation = {"l1_handle": "api://public.x/1@1-1", "field_path": "row.x",
                    "digest": "0" * 64, "lineage": "gold_etl_from_fm"}
        spec = m.Product.PROPERTIES["product_type"]
        odd = parse_value({"property": "product_type", "value_type": "enum",
                           "value": "NOT_A_TYPE", "citation": citation, "markings": []}, spec)
        self.assertIsNone(odd.value)
        self.assertEqual((odd.problem, odd.raw), ("enum_value_undeclared", "NOT_A_TYPE"))
        spec = m.Product.PROPERTIES["bc_unit_cost"]
        cost = parse_value({"property": "bc_unit_cost", "value_type": "money",
                            "value": "NaN", "currency": "CAD", "citation": citation,
                            "markings": ["finance"]}, spec)
        self.assertIsNone(cost.value)
        self.assertEqual(cost.problem, "not_a_money")
        blank = parse_value({"property": "bc_unit_cost", "value_type": "money",
                             "value": "3.10", "currency": None,
                             "currency_gap": {"reason": "currency_unknown"},
                             "citation": citation, "markings": []}, spec)
        self.assertEqual(blank.value, Money(Decimal("3.10"), None, "currency_unknown"))

    def test_disagreeing_values_leave_the_field_empty_and_named(self):
        def twice(s):
            s.val(PO, "order_date", "date", "2099-01-16", field_path="row.alt")
        po = repo(build(extra=twice)).get(PO, m.PurchaseOrder)
        self.assertIsNone(po.order_date)
        self.assertIn("order_date", po.conflicts)
        self.assertEqual({c.value for c in po.values_of("order_date")},
                         {dt.date(2099, 1, 15), dt.date(2099, 1, 16)})
        self.assertEqual(po.po_number.value, "PO-9001")

    def test_a_served_value_of_another_type_or_property_is_a_schema_mismatch(self):
        def wrong_type(s):
            s.val(PO, "expected_date", "text", "soon")
        with self.assertRaises(SchemaMismatch):
            repo(build(extra=wrong_type)).get(PO, m.PurchaseOrder)

        def undeclared(s):
            s.val(PO, "not_a_declared_property", "text", "x")
        with self.assertRaises(SchemaMismatch):
            repo(build(extra=undeclared)).get(PO, m.PurchaseOrder)

    def test_a_tool_error_raises(self):
        class Broken:
            def call(self, name, arguments, actor):
                return {"error": {"code": "store_unavailable", "message": "down"}}
        with self.assertRaises(GraphReadError) as raised:
            GraphRepository(Broken(), FULL).get(PO)
        self.assertEqual(raised.exception.code, "store_unavailable")


class Generated(unittest.TestCase):
    def test_models_are_up_to_date_with_the_schema(self):
        done = subprocess.run([sys.executable, str(ROOT / "tools" / "gen_models.py"),
                               "--check"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_every_declared_link_has_both_accessors(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import gen_models
        _types, links = gen_models.declared()
        reached = {(acc.__name__, cls.OBJECT_TYPE) for cls in m.MODELS.values()
                   for acc in vars(cls).values() if callable(acc)}
        self.assertEqual(len(links), len(set(links)))
        source = (ROOT / "operating/typed_graph/models.py").read_text("utf-8")
        for link in links:
            self.assertIn(f'LinkSpec("{link}", "forward"', source)
            self.assertIn(f'LinkSpec("{link}", "reverse"', source)
        self.assertTrue(reached)

    def test_unknown_cardinality_and_value_type_fail_generation(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import gen_models
        types = {"A": {"properties": [{"name": "x", "value_type": "text"}]}}
        link = {"id": "A.b", "from": "A", "to": "A", "name": "b", "reverse_name": "c",
                "cardinality": "2:N"}
        with self.assertRaises(gen_models.GenerationError):
            gen_models.build(types, {"A.b": link}, ["A"], ["A.b"])
        bad = {"A": {"properties": [{"name": "x", "value_type": "geo"}]}}
        with self.assertRaises(gen_models.GenerationError):
            gen_models.build(bad, {}, ["A"], [])


if __name__ == "__main__":
    unittest.main()
