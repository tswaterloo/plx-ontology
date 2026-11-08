"""Margin variance (``plx.aggregate`` mode ``margin_variance``, ``operating.margin_variance``)
at the tool boundary, over the synthetic Business Central world of
``tests.test_customer_profit`` (real capture, projection, generic tools and F10 runs).
Evidence label: isolated-fixture.

Period 2026-08-15..2026-10-30 holds SO-101 (FG-9: 10 invoiced for 100.00, a credit memo of
10.00 returning one unit at cost 15.00, shipped cost 150.00: margin -45.00). The baseline
2026-06-01..2026-08-14 holds SO-102 (FG-9: 5 for 60.00, cost 80.00) and SO-103 (FG-1: 4 for
40.00, cost 16.00): margin 4.00. Change -49.00 = price (10 - 12) x 10 = -20.00 + material
cost -(15 - 16) x 10 = 10.00 + returns/credits -((10 - 15)/10 - 0) x 10 = 5.00 + mix
(10 - 5) x -4 - 24 = -44.00.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import json
from types import SimpleNamespace
import unittest

from ontology import store_ddl
from operating import margin_variance as mv
from tests.test_customer_profit import CO, READER, TODAY, World

WINDOWS = {"from": "2026-08-15", "to": "2026-10-30", "baseline_from": "2026-06-01",
           "baseline_to": "2026-08-14"}


def serve(world: World, held: frozenset[str] = frozenset(store_ddl.MARKINGS)) -> dict:
    """The world's table with plx.aggregate's modes registered as the host does."""
    base = SimpleNamespace(store=world.base.store,
                           policy=SimpleNamespace(held_markings=lambda _actor: held),
                           _as_known_at=lambda _a: dt.datetime(2026, 10, 30, 12))
    from operating.ontology_host import aggregate_modes
    from operating.ontology_tools_aggregate import AggregateTools
    answers = AggregateTools(world.base)
    modes = aggregate_modes(world.table, base, prime=False)
    plain = modes["plx.aggregate"]["handler"]
    return {**world.table, "plx.aggregate": {"handler": lambda arguments, actor: (
        plain(arguments, actor) if isinstance(arguments, dict) and "mode" in arguments
        else answers.call("plx.aggregate", arguments, actor))}}


def ask(world: World, table: dict, variance: dict) -> dict:
    from tests.test_customer_profit import FULL
    return table["plx.aggregate"]["handler"]({"mode": "margin_variance",
                                              "variance": variance}, FULL)


def parts(block: dict) -> tuple:
    c = block["components"]
    return (c["price"], c["material_cost"], c["returns_credits"], c["mix"])


class MarginVarianceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.case = unittest.TestCase()
        cls._cleanups_ = []
        cls.case.addCleanup = cls._cleanups_.append   # type: ignore[method-assign]
        cls.world = World(cls.case)
        cls.table = serve(cls.world)
        cls.customer = cls.world.customer("C-50")

    @classmethod
    def tearDownClass(cls) -> None:
        for cleanup in reversed(cls._cleanups_):
            cleanup()

    def order(self, number: str) -> str:
        found = self.world.call("plx.find", {"query": number, "object_type": "SalesOrder"})
        hits = found.get("candidates") or found.get("results") or []
        return (hits[0].get("object") or hits[0])["object_id"]

    def test_a_customer_margin_change_splits_into_parts_that_sum_to_it(self):
        got = ask(self.world, self.table, {"subject": self.customer, **WINDOWS})
        self.assertEqual(got["mode"], "margin_variance", got)
        (cad,) = got["by_currency"]
        self.assertEqual(cad["currency"], "CAD")
        self.assertEqual((cad["current"]["revenue"], cad["current"]["cost"],
                          cad["current"]["margin"], cad["current"]["orders"]),
                         ("90.00", "135.00", "-45.00", 1))
        self.assertEqual((cad["baseline"]["revenue"], cad["baseline"]["cost"],
                          cad["baseline"]["margin"], cad["baseline"]["orders"]),
                         ("100.00", "96.00", "4.00", 2))
        self.assertEqual(cad["change"], "-49.00")
        self.assertEqual(parts(cad), ("-20.00", "10.00", "5.00", "-44.00"))
        self.assertEqual(sum(map(Decimal, parts(cad))), Decimal(cad["change"]))
        sold = {d["sold"]: d for d in cad["drivers"]}
        self.assertEqual(set(sold), {"both", "baseline_only"})
        self.assertEqual(sold["baseline_only"]["mix"], "-24.00")
        self.assertTrue(all(d["citations"] for d in cad["drivers"]))
        self.assertFalse(cad["freight"]["in_margin"])
        self.assertIn("FREIGHT_DUTY_NOT_IN_ITEM_COST", {g["code"] for g in got["gaps"]})
        # SO-104 (not invoiced) and SO-105 (2024) are in neither window
        self.assertEqual(got["orders"], {"current": 1, "baseline": 2, "without_margin": 0,
                                         "not_read": 0})

    def test_an_order_against_its_customer_and_a_product_on_its_own_lines(self):
        so101 = self.order("SO-101")
        got = ask(self.world, self.table, {"subject": so101})
        self.assertEqual(got["subject"], {"object_id": so101, "kind": "order"})
        self.assertEqual(got["period"], {"from": "2026-09-05", "to": "2026-09-05"})
        self.assertEqual(got["baseline"]["to"], "2026-09-04")
        (cad,) = got["by_currency"]
        self.assertEqual((cad["change"], parts(cad)),
                         ("-49.00", ("-20.00", "10.00", "5.00", "-44.00")))
        fg9 = cad["drivers"][0]["item"]
        product = ask(self.world, self.table, {"subject": fg9, **WINDOWS})
        (cad,) = product["by_currency"]
        self.assertEqual(product["subject"]["kind"], "product")
        self.assertEqual((cad["current"]["margin"], cad["baseline"]["margin"]),
                         ("-45.00", "-20.00"))
        self.assertEqual((cad["change"], parts(cad)),
                         ("-25.00", ("-20.00", "10.00", "5.00", "-20.00")))

    def test_a_period_reads_every_order_and_never_sums_a_margin_it_cannot_check(self):
        got = ask(self.world, self.table, dict(WINDOWS))
        self.assertEqual(got["subject"], {"object_id": None, "kind": "period"})
        (cad,) = got["by_currency"]
        self.assertEqual((cad["change"], parts(cad)),
                         ("-49.00", ("-20.00", "10.00", "5.00", "-44.00")))
        # SO-201 bills USD against a CAD cost: no checked margin, left out and said
        self.assertEqual(got["orders"]["without_margin"], 1)
        self.assertFalse(got["complete"])
        self.assertIn("ORDERS_WITHOUT_MARGIN", {g["code"] for g in got["gaps"]})

    def test_unknown_or_unexpected_input_fails_closed(self):
        from tests.test_customer_profit import FULL
        handler = self.table["plx.aggregate"]["handler"]
        for variance, code in (({"subject": self.customer, "currency": "CAD"}, "invalid_input"),
                               ({"subject": "Shipment:bc.x:1"}, "invalid_input"),
                               ({"from": "2026-13-01"}, "invalid_input"),
                               ({"from": "2026-08-01", "to": "2026-10-30",
                                 "baseline_from": "2026-07-01",
                                 "baseline_to": "2026-08-10"}, "invalid_input"),
                               ({"subject": 'Customer:bc.customers:{"id":"x"}'},
                                "subject_not_readable")):
            with self.subTest(variance=variance):
                got = handler({"mode": "margin_variance", "variance": variance}, FULL)
                self.assertEqual(got["error"]["code"], code, got)
        self.assertEqual(handler({"mode": "margin_variance", "variance": []}, FULL)
                         ["error"]["code"], "invalid_input")
        # the host's mode table: an unknown mode is refused, the cash forecast still served
        self.assertEqual(handler({"mode": "what_if"}, FULL)["error"]["code"], "invalid_input")
        self.assertNotEqual(handler({"mode": "cash_forecast"}, FULL).get("error", {})
                            .get("code"), "invalid_input")
        # any other call is plx.aggregate's (or an earlier mode's) as before
        other = handler({"measure": {"op": "count"}, "object_type": "SalesOrder"}, FULL)
        self.assertNotEqual(other.get("mode"), "margin_variance")


class RestrictedReaderTests(unittest.TestCase):
    def test_a_reader_without_finance_gets_no_figure(self):
        world = World(self, markings=tuple(m for m in store_ddl.MARKINGS if m != "finance"))
        got = ask(world, serve(world, frozenset(m for m in store_ddl.MARKINGS
                                                if m != "finance")),
                  {"subject": world.customer("C-50"), **WINDOWS})
        self.assertEqual(got["error"]["code"], "finance_not_held")
        self.assertNotIn("45", json.dumps(got))

    def test_orders_a_reader_may_not_read_are_neither_counted_nor_named(self):
        sets = ("companies", "customers", "itemLedgerEntries", "items", "purchaseOrders",
                "purchaseOrderLines", "salesCreditMemos", "salesCreditMemoLines",
                "salesInvoices", "salesInvoiceLines", "salesOrderLines", "salesShipments")
        world = World(self, audiences={**{f"bc:{CO}/{s}": {READER} for s in sets},
                                       "api:*": {READER}})
        table = serve(world)
        for variance in ({"subject": world.customer("C-50"), **WINDOWS}, dict(WINDOWS)):
            with self.subTest(subject=variance.get("subject")):
                got = ask(world, table, variance)
                self.assertEqual(got["by_currency"], [], got)
                self.assertEqual((got["orders"]["current"], got["orders"]["baseline"],
                                  got["orders"]["without_margin"]), (0, 0, 0))
                self.assertFalse(got["complete"])
                shown = json.dumps(got)
                for number in ("SO-101", "SO-102", "SO-103", "salesOrders"):
                    self.assertNotIn(number, shown)


if __name__ == "__main__":
    unittest.main()
