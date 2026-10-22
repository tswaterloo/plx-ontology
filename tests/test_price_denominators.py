"""Unit prices are rates: the shipped schema, the projection and plx.aggregate/plx.compare.

Finding UF-PRICE-DENOMINATOR: two Portal sales order lines priced 10 CAD excluding tax,
one per ``ea`` and one per ``kg``, compared equal and summed. The curated schema now
declares ``SalesOrderLine.unit_price`` a ``unit_price`` read per the line's ``uom``
(plx-customer-portal@b46934a ``portal/src/lib/sales-order/service.ts:153-165``), unknown
for lines the Sage ETL writes next to the product's uom; the projection stores the
denominator as the value's unit; the tools compare and combine unit prices only per one
known denominator and never sum them, while line totals (amounts) still sum per currency
and tax basis.

The projection is built from hand-made capture rows with the real compiled schema (not
business data). Evidence label: isolated-fixture.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ontology import identifiers
from ontology.json_contract import validate
from ontology.project import CapturedRecord, build_store, object_id_of
from ontology.schema import load_schema
from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_aggregate as oa

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = json.loads((ROOT / "operating/ontology_tool_contracts.json").read_text("utf-8"))
T = "2026-10-01T00:00:00.000000Z"
NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)
STAFF = Actor("plx", frozenset({"staff"}))
POLICY = ot.ReadPolicy("plx", {"api:*": frozenset({"staff"})},
                       {"finance": frozenset({"staff"})})

ORDERS = [
    {"id": "so-n", "order_number": "SO-N", "currency": "CAD", "custom_fields": {}},
    {"id": "so-u", "order_number": "SO-U", "currency": "USD", "custom_fields": {}},
    {"id": "so-s", "order_number": "SO-S", "currency": "CAD",
     "custom_fields": {"source": "sage.oe_order_header"}},
]
LINES = [  # id, order, unit price, uom, quantity, line total
    ("l-ea", "so-n", "10", "ea", "5", "50"),
    ("l-kg", "so-n", "10", "kg", "2", "20"),
    ("l-EA", "so-n", "10", "EA", "3", "30"),
    ("l-usd", "so-u", "10", "ea", "1", "10"),
    ("l-sage", "so-s", "10", "ea", "4", "40"),
    # Receiving's unitsCompatible plural and a JS-trimmed (BOM) spelling of one unit.
    ("l-bags", "so-n", "7", "\ufeffBags", "1", "7"),
    ("l-bag", "so-n", "7", "bag\u3000", "1", "7"),
    ("l-pails", "so-n", "7", "pails", "1", "7"),
    ("l-pail", "so-n", "7", "pail", "1", "7"),
]
ROWS = {
    "public.app_sales_order": ORDERS,
    "public.app_sales_order_line": [
        {"id": i, "sales_order_id": so, "unit_price": price, "uom": uom, "quantity": qty,
         "total_price": total} for i, so, price, uom, qty, total in LINES],
}


def line(native):
    return object_id_of("SalesOrderLine", "portal.app_sales_order_line", {"id": native})


class Source:
    source = "portal_api"

    def describe(self):
        return {"generation": 1, "relations": {}}

    def records(self, relations):
        return {rel: [CapturedRecord("portal_api", rel, json.dumps({"id": row["id"]}), row,
                                     f"api://{rel}/{row['id']}@1", "row",
                                     hashlib.sha256(row["id"].encode()).hexdigest(), T,
                                     None, T, f"api:{rel}") for row in ROWS.get(rel, [])]
                for rel in relations}


class PriceDenominatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = load_schema()
        cls.temp = tempfile.TemporaryDirectory()
        path = Path(cls.temp.name) / "store.sqlite"
        cls.report = build_store(path, cls.schema, {"portal_api": Source()},
                                 normalizer=identifiers.normalize, clock=lambda: NOW)
        cls.conn = sqlite3.connect(str(path), check_same_thread=False)
        base = ot.OntologyTools(ot.StoreReader(cls.conn), POLICY,
                                schema=ot.SchemaView.from_compiled(cls.schema),
                                contracts=CONTRACTS, item_gate=lambda actor, handle: True,
                                clock=lambda: NOW)
        cls.tools = oa.AggregateTools(base)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.temp.cleanup()

    def call(self, name, arguments):
        out = self.tools.call(name, arguments, STAFF)
        self.assertEqual(validate(out, CONTRACTS["tools"][name]["output"], root=CONTRACTS),
                         [], out)
        return out

    def stored(self, native, prop):
        return tuple(self.conn.execute(
            "SELECT value, unit, currency, lineage FROM object_values WHERE object_id = ? "
            "AND property = ?", (line(native), prop)).fetchone())

    def compare(self, left, right):
        [row] = self.call("plx.compare", {"left": line(left), "right": line(right),
                                          "properties": ["unit_price"]})["rows"]
        return row["comparison"], row.get("reason")

    def aggregate(self, natives, prop, op):
        handle = self.tools.base.sets.new(STAFF, [line(n) for n in natives])
        return self.call("plx.aggregate", {"set": handle,
                                           "measure": {"property": prop, "op": op}})

    def test_the_projection_carries_the_price_denominator_with_the_money_value(self):
        self.assertEqual(self.stored("l-ea", "unit_price"),
                         ("10", "ea", "CAD", "gold_native_staging"))
        self.assertEqual(self.stored("l-kg", "unit_price")[1], "kg")
        self.assertEqual(self.stored("l-EA", "unit_price")[1], "EA")  # as the source spells it
        # The Sage ETL stores the product's uom next to a Sage order-unit price: unknown.
        self.assertEqual(self.stored("l-sage", "unit_price"),
                         ("10", None, "CAD", "gold_etl_from_sage"))
        self.assertEqual(self.stored("l-ea", "line_total"), ("50", None, "CAD",
                                                             "gold_native_staging"))
        codes = {g["code"] for g in self.report["gaps"]}
        self.assertNotIn("per_unit_unresolved", codes)
        self.assertNotIn("currency_unresolved", codes)

    def test_equal_prices_per_ea_and_per_kg_are_not_comparable(self):
        self.assertEqual(self.compare("l-ea", "l-kg"),
                         ("incomparable", "price_denominator_differs"))

    def test_one_denominator_spelled_two_ways_compares(self):
        self.assertEqual(self.compare("l-ea", "l-EA"), ("equal", None))

    def test_an_unknown_denominator_says_so(self):
        self.assertEqual(self.compare("l-ea", "l-sage"),
                         ("incomparable", "price_denominator_unknown"))

    def test_the_unknown_denominator_is_a_visible_projection_gap(self):
        # The Sage ETL lineage's price is per no grounded unit: the build report names it.
        gaps = {(g["code"], g["where"]): g for g in self.report["gaps"]}
        gap = gaps.get(("price_denominator_unknown",
                        "SalesOrderLine.unit_price@portal.app_sales_order_line"))
        self.assertIsNotNone(gap, sorted(gaps))
        self.assertEqual(gap["count"], 1)
        self.assertEqual(gap["samples"], ["api://public.app_sales_order_line/l-sage@1"])

    def test_receiving_plurals_and_js_whitespace_fold_like_the_portal(self):
        # plx-customer-portal@6e76a5b apply-po-line-receive.ts unitsCompatible: bags/bag are
        # one unit; trim() strips a BOM and an ideographic space. pails is not folded.
        self.assertEqual(self.compare("l-bags", "l-bag"), ("equal", None))
        self.assertEqual(self.compare("l-pails", "l-pail"),
                         ("incomparable", "price_denominator_differs"))
        [group] = self.aggregate(["l-bags", "l-bag"], "unit_price", "max")["groups"]
        self.assertEqual((group["value"], group["unit"]), ("7", "bag"))

    def test_a_sum_of_unit_prices_is_refused_with_its_reason(self):
        out = self.aggregate(["l-ea", "l-EA"], "unit_price", "sum")
        self.assertEqual(out["refused"]["code"], "role_mixing")
        self.assertIn("SalesOrderLine.unit_price is a unit price", out["refused"]["message"])
        self.assertIn("no amount", out["refused"]["message"])
        self.assertEqual(out["groups"], [])
        self.assertFalse(out["completeness"]["complete"])

    def test_min_and_max_of_unit_prices_need_one_known_denominator(self):
        [group] = self.aggregate(["l-ea", "l-EA"], "unit_price", "max")["groups"]
        self.assertEqual((group["value"], group["currency"], group["unit"]), ("10", "CAD", "ea"))
        self.assertEqual(self.aggregate(["l-ea", "l-kg"], "unit_price", "min")
                         ["refused"]["code"], "unit_mixing")
        self.assertEqual(self.aggregate(["l-ea", "l-sage"], "unit_price", "max")
                         ["refused"]["code"], "unit_unknown")

    def test_line_totals_still_sum_by_currency_and_tax_basis(self):
        out = self.aggregate(["l-ea", "l-kg", "l-EA", "l-sage"], "line_total", "sum")
        self.assertNotIn("refused", out)
        [group] = out["groups"]
        self.assertEqual((group["value"], group["currency"], group["inputs"]),
                         ("140", "CAD", 4))
        self.assertNotIn("tax_basis_unknown", {g["reason"] for g in out["gaps"]})  # excl_tax
        self.assertEqual(self.aggregate(["l-ea", "l-usd"], "line_total", "sum")
                         ["refused"]["code"], "currency_mixing")


if __name__ == "__main__":
    unittest.main()
