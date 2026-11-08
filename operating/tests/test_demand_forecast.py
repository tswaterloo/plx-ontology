"""Demand forecasts (``operating.demand_forecast``, ``plx.aggregate`` mode
``demand_forecast``) and the ``reorder_soon`` alert (``operating.proactive_stock``) through
the engine, the per-reader inbox, the briefing and the tool's answer.

SYNTHETIC fixtures only (``World`` stores, synthetic codes); evidence label: isolated-fixture.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import sqlite3
import time
from types import SimpleNamespace
import unittest

from operating import briefing
from operating import demand_forecast as df
from operating import proactive_engine as pe
from operating import proactive_stock as ops
from operating.tests.test_proactive_business import NO_FINANCE, item, money, purchase
from operating.tests.test_proactive_engine import ALL_MARKINGS, AS_OF, Engine, World

LOTS = (("InventoryLot", "quantity_on_hand", "InventoryLot.of_item"),)
FIN = '["finance"]'


def month_day(months_back: int) -> str:
    """The 15th of the month ``months_back`` before AS_OF's month."""
    index = AS_OF.year * 12 + AS_OF.month - 1 - months_back
    return dt.date(index // 12, index % 12 + 1, 15).isoformat()


def invoice(w: World, product: str, qty: str, day: str, *, customer: str | None = None,
            unit: str | None = "kg", binding: str = "portal.sage_oe_invoice_header",
            audience: str = "api:sales", status: str | None = None,
            amount: str | None = None, number: str | None = None) -> tuple[str, str]:
    head = w.obj("SalesInvoice", {"invoice_date": day,
                                  "customer_number": number or "SYNTHC1",
                                  **({"status": status} if status else {})},
                 binding=binding, audience=audience)
    values = {"invoiced_quantity": (qty, unit, "invoiced")}
    if amount is not None:
        values["extended_amount"] = amount
    line = w.obj("SalesInvoiceLine", values, audience=audience)
    if amount is not None:
        for row in w.rows["object_values"]:
            if row["object_id"] == line and row["property"] == "extended_amount":
                row["markings"] = FIN
        money(w, line, "extended_amount", "CAD")
    w.link("SalesInvoiceLine.line_of", line, head)
    w.link("SalesInvoiceLine.for_item", line, product)
    if customer:
        w.link("SalesInvoice.billed_to", head, customer)
    return head, line


def seasonal(w: World, product: str, months: int = 30, base: int = 100, **kw) -> list[int]:
    """Monthly sales with a yearly pattern and a rising level; the quantities, oldest first."""
    sold = []
    for back in range(months, 0, -1):
        index = AS_OF.year * 12 + AS_OF.month - 1 - back
        qty = base + 20 * (index % 12) + (months - back) * 2
        invoice(w, product, str(qty), month_day(back), **kw)
        sold.append(qty)
    return sold


class Method(unittest.TestCase):

    def test_two_seasons_use_last_season_plus_its_change_and_less_smooths(self):
        values = [float(10 + (i % 12) + i // 12 * 12) for i in range(24)]
        got = df.fit(values, 12, 3)
        self.assertEqual(got.method, "seasonal_naive_trend")
        # same month last season plus the change in the per-month average (12)
        self.assertEqual([got.point(h) for h in (1, 2, 3)], [34.0, 35.0, 36.0])
        self.assertEqual(got.point(13), values[12] + 24)          # a season further: twice
        self.assertEqual(got.sigma, 0.0)
        flat = df.fit([5.0, 5.0, 5.0, 5.0, 5.0, 5.0], 12, 3)
        self.assertEqual((flat.method, flat.point(1), flat.point(9)),
                         ("exponential_smoothing", 5.0, 5.0))
        self.assertIsNone(df.fit([1.0] * 5, 12, 3))               # under 2 x horizon
        noisy = df.fit([3.0, 9.0, 4.0, 8.0, 2.0, 7.0, 5.0], 12, 3)
        self.assertGreater(noisy.spread(3), noisy.spread(1))      # wider further out
        self.assertEqual(df.horizon_end(dt.date(2028, 2, 29), "month"),
                         dt.date(2028, 4, 30))                  # a leap day reads fine
        # series in different units: medians of their own ratios, never summed quantities
        kg = {"periods": 3, "abs_error": 10.0, "actual": 100.0, "error": 10.0,
              "naive_abs_error": 20.0, "inside_80": 3}
        each = {"periods": 3, "abs_error": 30000.0, "actual": 100000.0, "error": -30000.0,
                "naive_abs_error": 10000.0, "inside_80": 0}
        self.assertEqual(df.pooled([kg, each]), {
            "series": 2, "median_wape": "0.20", "median_naive_wape": "0.15",
            "beats_naive": "0.50", "median_bias_share": "-0.10", "within_80": "0.50"})
        held = df.holdout(values, 12, 3)
        self.assertEqual(held["method"], "exponential_smoothing")  # 21 periods left: smoothing
        self.assertEqual(held["periods"], 3)


class Forecast(Engine):

    def book(self, w: World, **kw) -> dict:
        w.build(self.store)
        return df.read_book(str(self.store), AS_OF, **kw)

    def ask(self, book: dict, **kw) -> dict:
        options = {"may_read": lambda _a: True, "held": ALL_MARKINGS, **kw}
        bucket = options.get("bucket", "month")
        return df.answer(book, AS_OF, reorder=(book.get("reorder") or {}).get(bucket),
                         **options)

    def test_a_series_reports_method_history_intervals_and_holdout(self):
        w = World()
        p = item(w, ())
        sold = seasonal(w, p, amount="10.00")
        invoice(w, p, "7", month_day(3), unit=None)                  # no unit: not counted
        invoice(w, p, "9", month_day(3), status="Canceled")          # cancelled: not counted
        # the successor system's first few lines: the history still ends where the main
        # source's records end, never read as demand stopping
        invoice(w, p, "1", (AS_OF - dt.timedelta(days=3)).isoformat(),
                binding="bc.sales_invoices")
        got = self.ask(self.book(w))
        tool = df.DemandForecastTool.__new__(df.DemandForecastTool)
        self.assertEqual(tool._checked(got), got)                     # the contract holds
        self.assertTrue(got["complete"])
        self.assertEqual(got["not_counted"], {"invoice_cancelled_or_unposted": 1,
                                              "unit_not_stated": 1})
        (s,) = got["series"]
        self.assertEqual((s["item"]["object_id"], s["item"]["code"], s["unit"]),
                         (p, "SYNTH-ITEM", "kg"))
        self.assertEqual(s["method"], "seasonal_naive_trend")
        # the records reach the 15th of last month: that month is not whole, so not used
        self.assertEqual(got["through"], month_day(1))
        self.assertEqual(s["history"], {"from": month_day(30)[:7], "to": month_day(2)[:7],
                                        "periods": 29, "total": str(sum(sold[:-1]))})
        self.assertEqual(s["lead_periods"], 1)
        self.assertEqual(len(s["forecast"]), 3)
        self.assertEqual(s["forecast"][0]["period"], AS_OF.isoformat()[:7])
        for f in s["forecast"]:
            self.assertLessEqual(Decimal(f["low_95"]), Decimal(f["low_80"]))
            self.assertLessEqual(Decimal(f["low_80"]), Decimal(f["quantity"]))
            self.assertLessEqual(Decimal(f["quantity"]), Decimal(f["high_80"]))
            self.assertLessEqual(Decimal(f["high_80"]), Decimal(f["high_95"]))
        self.assertEqual(Decimal(s["forecast_total"]),
                         sum(Decimal(f["quantity"]) for f in s["forecast"]))
        self.assertEqual(set(s["holdout"]), {"periods", "method", "mae", "wape", "bias",
                                             "naive_mae", "within_80"})
        self.assertEqual(s["revenue"], [{"currency": "CAD", "amount": "290.00"}])
        self.assertEqual(got["holdout"]["series"], 1)
        # one series: the pooled medians are its own (unit-free, never summed across units)
        self.assertEqual(got["holdout"]["median_wape"], s["holdout"]["wape"])
        # weekly: 13 weeks ahead
        weekly = self.ask(self.book(w), bucket="week")
        self.assertEqual((weekly["horizon"], len(weekly["series"][0]["forecast"])), (13, 13))

    def test_twins_count_once_and_customers_split_series(self):
        w = World()
        p = item(w, ())
        twin = w.obj("Product", {"code": "SYNTH-ITEM"}, audience="api:items")
        w.link("Product.same_record_as", p, twin)
        c1 = w.obj("Customer", {"customer_number": "SYNTHC1"}, audience="api:sales")
        c2 = w.obj("Customer", {"customer_number": "SYNTHC2"}, audience="api:sales")
        for back in range(2, 10):
            invoice(w, p, "10", month_day(back), customer=c1)
            # the same invoice in BC and the Portal: one sale
            bc, _ = invoice(w, twin, "10", month_day(back), customer=c2, number="SYNTHC2",
                            binding="bc.sales_invoices")
            portal, _ = invoice(w, twin, "10", month_day(back), customer=c2, number="SYNTHC2")
            w.link("SalesInvoice.same_record_as", bc, portal)
        book = self.book(w)
        by_item = self.ask(book)
        (s,) = by_item["series"]
        # 7 whole months (the records end mid-month 2) x (10 + 10): twins once
        self.assertEqual(s["history"]["total"], "140")
        split = self.ask(book, by="customer_item")
        self.assertEqual(sorted((x["customer"]["number"], x["history"]["total"])
                                for x in split["series"]), [("SYNTHC1", "70"),
                                                            ("SYNTHC2", "70")])
        self.assertEqual(split["series"][0]["method"], "exponential_smoothing")
        leap = df.read_book(str(self.store), dt.date(2028, 2, 29))   # a leap day reads fine
        self.assertEqual(len(leap["lines"]), 16)

    def test_a_restricted_reader_counts_only_what_it_may_read(self):
        w = World()
        p = item(w, ("50",))
        seasonal(w, p, amount="10.00")
        seasonal(w, p, base=1000, audience="api:secret", amount="99.00")
        purchase(w, p, "40", expected=(AS_OF + dt.timedelta(days=20)).isoformat(),
                 audience="api:secret")
        book = self.book(w, on_hand=LOTS)
        full = self.ask(book)
        hidden = self.ask(book, may_read=lambda a: a != "api:secret", held=NO_FINANCE)
        self.assertTrue(full["complete"])
        self.assertFalse(hidden["complete"])
        self.assertNotIn("not_counted", hidden)
        (s,) = hidden["series"]
        self.assertLess(Decimal(s["history"]["total"]),
                        Decimal(full["series"][0]["history"]["total"]))
        self.assertNotIn("revenue", s)                              # no finance: no revenue
        # inbound on a purchase order it may not read: the reorder check is not judged
        self.assertEqual(s["reorder"], {"status": "not_evaluated",
                                        "reason": "supply_not_all_readable"})
        self.assertEqual(full["series"][0]["reorder"]["inbound_by_end"], "40")
        hidden_text = repr(hidden)
        for line in book["lines"]:
            if "api:secret" in {book["audiences"][r] for r in line.records}:
                self.assertNotIn(line.handles[0][0], hidden_text)
        nothing = self.ask(book, may_read=lambda _a: False)
        self.assertEqual((nothing["series"], nothing["holdout"]["series"]), ([], 0))
        # an unknown audience or unreadable marking fails closed
        line = book["lines"][0]
        self.assertFalse(df.visible(line, {}, lambda _a: True, ALL_MARKINGS))
        broken = df.Line(**{**line.__dict__, "handles": (("h", "o", "not json"),)})
        self.assertFalse(df.visible(broken, book["audiences"], lambda _a: True,
                                    ALL_MARKINGS))

    def test_the_forecast_is_a_mode_of_plx_aggregate_beside_the_cash_forecast(self):
        from operating import cash_forecast as cf

        w = World()
        seasonal(w, item(w, ()))
        w.build(self.store)
        held = {"fin": frozenset({"finance"}), "ops": frozenset()}
        base = SimpleNamespace(
            store=SimpleNamespace(conn=sqlite3.connect(self.store)),
            policy=SimpleNamespace(held_markings=lambda actor: held[actor],
                                   may_read=lambda actor, key: True),
            _as_known_at=lambda _a: dt.datetime(2026, 10, 19, 6), _store_meta=lambda: {})
        self.addCleanup(base.store.conn.close)
        table = {"plx.aggregate": {"name": "plx.aggregate",
                                   "handler": lambda arguments, actor: {"groups": []}}}
        cf.register(table, base)
        tool = df.register(table, base)
        tool.wait = 120.0
        call = table["plx.aggregate"]["handler"]
        self.assertEqual(call({"measure": {"op": "count"}, "object_type": "X"}, "ops"),
                         {"groups": []})                          # plx.aggregate as before
        self.assertEqual(call({"mode": "cash_forecast"}, "ops")["error"]["code"],
                         "finance_not_held")                      # the cash mode as before
        got = call({"mode": "demand_forecast", "measure": {"op": "count"},
                    "object_type": "SalesInvoiceLine"}, "ops")   # quantities: no finance
        self.assertEqual((got["mode"], len(got["series"])), ("demand_forecast", 1), got)
        self.assertEqual(got["reorder"]["status"], "blocked")    # on hand not served
        self.assertIn("not_served_Product__quantity_on_hand", got["reorder"]["reasons"])
        self.assertIn("item stock on hand", got["reorder"]["waiting_on"])
        for bad in ({"bucket": "day"}, {"group_by": ["status"]}, {"cursor": "x1"}):
            self.assertEqual(call({"mode": "demand_forecast", **bad}, "ops")["error"]["code"],
                             "invalid_input", bad)
        split = call({"mode": "demand_forecast", "group_by": ["customer"], "bucket": "week"},
                     "fin")
        self.assertEqual((split["by"], split["bucket"]), ("customer_item", "week"))


class ReorderSoon(Engine):

    def run_rule(self, w: World, **options) -> dict:
        return self.run_engine(w, only=[ops.REORDER_RULE],
                               on_hand_sources=options.pop("on_hand", LOTS), **options)

    def test_blocked_naming_the_field_while_on_hand_is_not_served(self):
        w = World()
        seasonal(w, item(w, ()))
        summary = self.run_rule(w, on_hand=())
        self.assertEqual(summary["detectors"][ops.REORDER_RULE], "blocked")
        reasons = self.read()["detector_reasons"][ops.REORDER_RULE]
        self.assertIn("not_served_Product__quantity_on_hand", reasons)
        checks = {c["check"]: c for c in briefing._checks(self.read())}
        self.assertIn("item stock on hand", checks[ops.REORDER_RULE]["words"])

    def test_an_item_whose_forecast_exceeds_on_hand_plus_inbound_is_raised_once(self):
        w = World()
        short = item(w, ("100",))
        seasonal(w, short)                                       # ~3 x 200 forecast
        purchase(w, short, "50", expected=(AS_OF + dt.timedelta(days=30)).isoformat())
        purchase(w, short, "500", expected=(AS_OF + dt.timedelta(days=400)).isoformat())
        covered = item(w, ("5000",))
        seasonal(w, covered)
        young = item(w, ("1",))
        invoice(w, young, "10", month_day(2))                    # too little history
        summary = self.run_rule(w)
        self.assertEqual(summary["detectors"][ops.REORDER_RULE], "complete")
        self.assertEqual(summary["not_evaluated"][ops.REORDER_RULE],
                         {"too_little_history": 1})
        gaps = self.read(kinds=[ops.REORDER])["gaps"]
        self.assertEqual([g["object_id"] for g in gaps], [short])
        gap = gaps[0]
        event = gap["event"]
        self.assertEqual(event["supply"], "150")                 # 100 on hand + 50 by the end
        self.assertEqual(Decimal(event["shortfall"]),
                         Decimal(event["forecast"]) - Decimal("150"))
        self.assertTrue(gap["summary"].startswith("Reorder soon: forecast demand of "))
        self.assertIn("plus 50 kg due in on 1 open purchase order by", gap["summary"])
        self.assertEqual(gap["follow_up"]["priority"], "high")
        self.assertEqual(briefing.NEXT_ACTIONS[ops.REORDER][1], "reorder_raise_po")
        self.assertEqual(briefing.EVENT_TYPES[ops.REORDER]["label"], "Reorder soon")
        self.assertIn(ops.REORDER, briefing.TODAY_WORDS)
        self.assertIn(ops.REORDER_RULE, briefing.CHECK_WORDS)
        # it rests on no other inbound order being recorded: a reader not granted every
        # audience of invoices, purchase orders and stock sees nothing
        self.assertEqual(self.read(kinds=[ops.REORDER],
                                   may_read=lambda a: a != "api:po")["gaps"], [])

    def test_every_item_is_judged_once_at_1_10_and_60(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World()
                for _ in range(n):
                    seasonal(w, item(w, ("10",)))
                    seasonal(w, item(w, ("9000",)))
                started = time.monotonic()
                summary = self.run_rule(w)
                self.assertLess(time.monotonic() - started, 60)
                self.assertEqual(summary["detectors"][ops.REORDER_RULE], "complete")
                found = self.read(kinds=[ops.REORDER], limit=1000)["gaps"]
                self.assertEqual(len(found), n)
                self.assertEqual(len({g["object_id"] for g in found}), n)


if __name__ == "__main__":
    unittest.main()
