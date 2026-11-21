"""The one chart spec (``operator_api.charts``): every kind is drawn from the rows of one
query result and nothing else; an unknown or malformed spec, a column the result lacks, two
currencies on one axis, a share of a result cut short or a pivot cell read twice draw
nothing; arithmetic a chart adds (pie remainder, histogram counts, trend, waterfall) is of
the returned rows only and adds up. Evidence: isolated-fixture."""

from __future__ import annotations

import json
import time
import unittest
from decimal import Decimal

from operator_api import charts as ch
from operator_api import models as m

RID = "sql://0123456789abcdef0123"


def result(columns, cells, truncated=False, sql="SELECT 1"):
    return {"type": "tool_call", "name": "plx.sql", "is_error": False,
            "arguments": {"sql": sql},
            "result": {"result": RID, "columns": list(columns), "truncated": truncated,
                       "rows": [{"cite": f"{RID}#row={i}", "cells": c}
                                for i, c in enumerate(cells)],
                       "as_of": "2026-11-18T22:00:00+00:00"}}


def drawn(spec, columns, cells, truncated=False):
    """The charts an answer carrying ``spec`` as its chart block gets (via the contract)."""
    text = "Answer.\n```chart\n" + json.dumps({"result": RID, **spec}) + "\n```\n"
    shown, out = ch.blocks(text, [result(columns, cells, truncated)])
    assert shown == "Answer."
    for c in out:
        m.AskChart(**c)   # the API contract holds every chart drawn
    return out


WEEKS = ["2026-10-05", "2026-10-12", "2026-10-19"]
LONG = [[w, d, str(n)] for w in WEEKS for d, n in (("Sales", 3), ("QA", 5), ("Finance", 1))]


class KindsTest(unittest.TestCase):
    def test_every_kind_draws_from_the_rows(self):
        cols = ("person", "mail", "teams")
        rows = [["A", "10", "4"], ["B", "7", "9"], ["C", "3", "1"]]
        for kind in ("bar", "line", "area", "combo", "table", "kpi", "funnel", "waterfall"):
            spec = {"type": kind, "x": "person", "y": ["mail"]}
            if kind == "combo":
                spec["y2"] = ["teams"]
            (c,) = drawn(spec, cols, rows)
            self.assertEqual(c["kind"], kind)
            self.assertEqual(c["labels"], ["A", "B", "C"])
            self.assertEqual(c["series"][0]["values"], [10.0, 7.0, 3.0])
            self.assertEqual(c["sql"], "SELECT 1")
        (combo,) = drawn({"type": "bar", "x": "person", "y": ["mail"], "y2": ["teams"]},
                         cols, rows)
        self.assertEqual(combo["kind"], "combo")   # a right axis makes a combo
        self.assertEqual(combo["series"][1], {"name": "teams", "values": [4.0, 9.0, 1.0],
                                              "axis": "right"})

    def test_aliases_and_options(self):
        (c,) = drawn({"type": "stacked_bar", "x": "week", "split": "dept", "y": "n"},
                     ("week", "dept", "n"), LONG)
        self.assertTrue(c["spec"]["stacked"])
        (d,) = drawn({"type": "doughnut", "x": "person", "y": ["n"]}, ("person", "n"),
                     [["A", "1"], ["B", "3"]])
        self.assertEqual(d["kind"], "donut")

    def test_split_pivots_long_rows_into_one_series_per_value(self):
        (c,) = drawn({"type": "bar", "x": "week", "split": "dept", "y": ["n"],
                      "stacked": True}, ("week", "dept", "n"), LONG)
        self.assertEqual(c["labels"], WEEKS)
        self.assertEqual([s["name"] for s in c["series"]], ["Sales", "QA", "Finance"])
        self.assertEqual(c["series"][1]["values"], [5.0, 5.0, 5.0])
        # every row is in exactly one cell: the cells sum to the rows
        self.assertEqual(sum(v for s in c["series"] for v in s["values"]),
                         sum(float(r[2]) for r in LONG))

    def test_a_split_value_with_no_amount_has_no_legend_entry(self):
        # the walk: a month chart split by status listed "Unknown" for rows of no value
        rows = [["2026-10", "Open", "210600"], ["2026-10", "Closed", "11970"],
                ["2026-10", "Unknown", None], ["2026-11", "Open", "200000"],
                ["2026-11", "Unknown", "0"]]
        (c,) = drawn({"type": "bar", "x": "month", "split": "status", "y": ["value"]},
                     ("month", "status", "value"), rows)
        self.assertEqual([s["name"] for s in c["series"]], ["Open", "Closed"])
        self.assertEqual(c["shown"], len(rows))   # nothing said to be left out
        # every split value holding an amount keeps its series
        (one,) = drawn({"type": "bar", "x": "month", "split": "status", "y": ["value"]},
                       ("month", "status", "value"), rows[:2] + [["2026-11", "Open", "5"]])
        self.assertEqual(len(one["series"]), 2)

    def test_a_pivot_cell_read_twice_is_refused_never_added(self):
        self.assertEqual(drawn({"type": "bar", "x": "week", "split": "dept", "y": ["n"]},
                               ("week", "dept", "n"), LONG + [LONG[0]]), [])

    def test_a_repeated_label_draws_the_column_that_tells_the_rows_apart(self):
        cols = ["customer_code", "month", "orders", "currency"]
        rows = [["R1", "2026-04", 19, "USD"], ["R2", "2026-04", 1, "USD"],
                ["R1", "2026-05", 11, "USD"]]
        out = ch.build({"kind": "bar", "x": "month", "y": ["orders"]}, cols, rows)
        self.assertEqual(out["spec"]["split"], "customer_code")
        self.assertEqual(out["labels"], ["2026-04", "2026-05"])
        self.assertEqual({s["name"]: s["values"] for s in out["series"]},
                         {"R1": [19.0, 11.0], "R2": [1.0, None]})
        for spec in ({"kind": "pie", "x": "month", "y": ["orders"]},
                     {"kind": "bar", "x": "month", "y": ["orders", "currency"]}):
            with self.assertRaisesRegex(ch.NotDrawn, "Two rows are for month 2026-04"):
                ch.build(spec, cols, rows)
        self.assertIsNone(ch.build({"kind": "bar", "x": "month", "y": ["orders"]},
                                   cols, rows[::2])["spec"]["split"])
        self.assertEqual(ch.build({"kind": "table", "x": "month", "y": ["orders"]},
                                  cols, rows)["labels"], ["2026-04", "2026-04", "2026-05"])

    def test_small_multiples_keep_the_split_in_each_panel(self):
        cols = ["week", "supplier", "spend", "currency"]
        rows = [["W1", "A", 10, "CAD"], ["W1", "B", 5, "CAD"], ["W2", "A", 7, "CAD"],
                ["W1", "C", 3, "USD"]]
        for spec in ({"kind": "bar", "x": "week", "y": ["spend"], "split": "supplier",
                      "facet": "currency"},
                     {"kind": "bar", "x": "week", "y": ["spend"], "facet": "currency"}):
            with self.subTest(spec=spec):
                out = ch.build(spec, cols, rows)
                cad, usd = out["panels"]
                self.assertEqual(cad["labels"], ["W1", "W2"])
                self.assertEqual({s["name"]: s["values"] for s in cad["series"]},
                                 {"A": [10.0, 7.0], "B": [5.0, None]})
                self.assertEqual((usd["labels"], [s["name"] for s in usd["series"]]),
                                 (["W1"], ["spend"] if "split" not in spec else ["C"]))

    def test_kpi_tiles_in_two_currencies_are_one_set_per_currency(self):
        cols = ["month", "currency", "revenue"]
        rows = [["2026-10", "CAD", 10], ["2026-11", "CAD", 12], ["2026-10", "USD", 7],
                ["2026-11", "USD", 5]]
        out = ch.build({"kind": "kpi", "x": "month", "y": ["revenue"], "facet": "currency"},
                       cols, rows)
        cad, usd = out["panels"]
        self.assertEqual((cad["title"], cad["series"][0]["values"]), ("CAD", [10.0, 12.0]))
        self.assertEqual((usd["title"], usd["series"][0]["values"]), ("USD", [7.0, 5.0]))
        with self.assertRaisesRegex(ch.NotDrawn, "currenc"):
            ch.build({"kind": "kpi", "x": "month", "y": ["revenue"]}, cols, rows)

    def test_shares_in_two_currencies_are_one_pie_per_currency(self):
        cols = ["line_type", "currency", "revenue"]
        rows = [["Misc", "CAD", 10], ["Item", "CAD", 90], ["Item", "USD", 19], ["Misc", "USD", 1]]
        out = ch.build({"kind": "donut", "x": "line_type", "y": ["revenue"], "facet": "currency"},
                       cols, rows)
        cad, usd = out["panels"]
        self.assertEqual((cad["title"], cad["labels"], cad["series"][0]["values"]),
                         ("CAD", ["Item", "Misc"], [90.0, 10.0]))
        self.assertEqual((usd["title"], usd["series"][0]["values"]), ("USD", [19.0, 1.0]))
        with self.assertRaisesRegex(ch.NotDrawn, "currenc"):
            ch.build({"kind": "donut", "x": "line_type", "y": ["revenue"]}, cols, rows)
        with self.assertRaisesRegex(ch.NotDrawn, "negative"):
            ch.build({"kind": "pie", "x": "line_type", "y": ["revenue"], "facet": "currency"},
                     cols, rows + [["Credit", "USD", -3]])

    def test_a_change_in_two_currencies_is_a_waterfall_per_currency(self):
        cols = ["customer", "currency", "change"]
        rows = [["A", "CAD", 10], ["B", "CAD", -4], ["All others (3)", "CAD", 2],
                ["C", "USD", -7]]
        out = ch.build({"kind": "waterfall", "x": "customer", "y": ["change"],
                        "facet": "currency"}, cols, rows)
        cad, usd = out["panels"]
        self.assertEqual((cad["labels"], cad["series"][0]["values"]),
                         (["A", "B", "All others (3)"], [10.0, -4.0, 2.0]))
        self.assertEqual(usd["series"][0]["values"], [-7.0])
        with self.assertRaisesRegex(ch.NotDrawn, "currenc"):
            ch.build({"kind": "waterfall", "x": "customer", "y": ["change"]}, cols, rows)
        with self.assertRaises(ch.NotDrawn):   # a split is no way to keep a sum apart
            ch.build({"kind": "waterfall", "x": "customer", "y": ["change"],
                      "split": "currency"}, cols, rows)
        twice = rows + [["A", "CAD", 1]]
        for split in ({}, {"split": "region"}):   # a label twice in a panel: no sum
            with self.subTest(split=split), self.assertRaises(ch.NotDrawn):
                ch.build({"kind": "waterfall", "x": "customer", "y": ["change"],
                          "facet": "currency", **split}, cols + ["region"],
                         [r + [f"R{i}"] for i, r in enumerate(twice)])

    def test_the_split_is_a_column_whose_values_tell_the_repeated_rows_apart(self):
        # region is the same on both April rows: it cannot be the split; customer can
        cols = ["month", "region", "orders", "customer"]
        rows = [["2026-04", "East", 19, "R1"], ["2026-04", "East", 1, "R2"],
                ["2026-05", "West", 11, "R1"]]
        out = ch.build({"kind": "bar", "x": "month", "y": ["orders"]}, cols, rows)
        self.assertEqual(out["spec"]["split"], "customer")
        # no column tells the two April rows apart: nothing is drawn
        same = [["2026-04", "East", 19, "R1"], ["2026-04", "East", 1, "R1"]]
        with self.assertRaisesRegex(ch.NotDrawn, "Two rows are for month 2026-04"):
            ch.build({"kind": "bar", "x": "month", "y": ["orders"]}, cols, same)

    def test_heatmap_rows_are_the_split_values(self):
        (c,) = drawn({"type": "heatmap", "x": "week", "split": "dept", "y": ["n"]},
                     ("week", "dept", "n"), LONG)
        self.assertEqual(len(c["series"]), 3)
        self.assertEqual(len(c["labels"]), 3)

    def test_small_multiples_one_panel_per_facet_value(self):
        (c,) = drawn({"type": "line", "x": "week", "facet": "dept", "y": ["n"]},
                     ("week", "dept", "n"), LONG)
        self.assertEqual([p["title"] for p in c["panels"]], ["Sales", "QA", "Finance"])
        self.assertEqual(c["panels"][2]["series"][0]["values"], [1.0, 1.0, 1.0])
        self.assertEqual(c["shown"], len(LONG))

    def test_pie_shares_keep_every_row_and_the_remainder_is_their_sum(self):
        rows = [[f"c{i}", str(i)] for i in range(1, 21)]
        (c,) = drawn({"type": "pie", "x": "customer", "y": ["v"]}, ("customer", "v"), rows)
        self.assertEqual(len(c["labels"]), ch.MAX_PIE_SLICES)
        self.assertEqual(c["labels"][-1], "All others (13)")
        self.assertEqual(sum(c["series"][0]["values"]), sum(range(1, 21)))   # nothing lost

    def test_histogram_counts_every_row_once(self):
        for n in (1, 10, 60, 200):
            rows = [[str(i * 1.5)] for i in range(n)] + [["n/a"]]
            (c,) = drawn({"type": "histogram", "y": ["hours"]}, ("hours",), rows)
            self.assertEqual(sum(c["series"][0]["values"]), n, n)
            self.assertEqual(c["skipped"], 1)
            self.assertEqual(len(c["edges"]), len(c["labels"]) + 1)
        (b,) = drawn({"type": "histogram", "y": ["hours"], "bins": 4}, ("hours",),
                     [["0"], ["1"], ["2"], ["3"], ["4"]])
        self.assertEqual(b["series"][0]["values"], [1.0, 1.0, 1.0, 2.0])

    def test_scatter_trend_is_the_least_squares_line_and_r(self):
        rows = [[str(x), str(2 * x + 1), f"p{x}"] for x in range(10)]
        (c,) = drawn({"type": "scatter", "x": "a", "y": ["b"], "label": "who", "trend": True},
                     ("a", "b", "who"), rows)
        self.assertAlmostEqual(c["trend"]["slope"], 2.0)
        self.assertAlmostEqual(c["trend"]["intercept"], 1.0)
        self.assertEqual(c["trend"]["r"], 1.0)
        self.assertEqual(c["labels"][3], "p3")
        self.assertIsNone(ch.trend([(1.0, 1.0), (1.0, 2.0), (1.0, 3.0)]))   # no x spread

    def test_kpi_needs_no_label_column(self):
        (c,) = drawn({"type": "kpi", "y": ["orders", "value"]}, ("orders", "value"),
                     [["12", "3400.50"]])
        self.assertEqual([s["values"] for s in c["series"]], [[12.0], [3400.5]])


class FailsClosedTest(unittest.TestCase):
    COLS = ("person", "v", "w")
    ROWS = [["A", "1", "2"]]

    def test_unknown_or_malformed_specs_draw_nothing(self):
        bad = [{"type": "radar", "x": "person", "y": ["v"]},
               {"type": "bar", "x": "person", "y": []},
               {"type": "bar", "x": "person", "y": ["nope"]},
               {"type": "bar", "x": "nope", "y": ["v"]},
               {"type": "bar", "x": "person", "y": ["v"], "stacked": "yes"},
               {"type": "bar", "x": "person", "y": [3]},
               {"type": "histogram", "y": ["v"], "bins": 0},
               {"type": "histogram", "y": ["v"], "bins": True},
               {"type": "pie", "x": "person", "y": ["v", "w"]},
               {"type": "scatter", "x": "person", "y": ["v"], "facet": "w"},
               {"type": "line", "x": "person", "y": ["v"], "y2": ["w"], "split": "w"},
               {"type": "pie", "x": "person", "y": ["v"], "y2": ["w"]},
               {"type": "bar", "y": ["v"]},
               {"type": "heatmap", "x": "person", "y": ["v"]}]
        for spec in bad:
            self.assertEqual(drawn(spec, self.COLS, self.ROWS), [], spec)
            self.assertIsNone(ch.parse(spec) if spec["y"] != ["nope"] and spec.get("x") != "nope"
                              else None)

    def test_text_where_a_number_belongs_draws_nothing(self):
        self.assertEqual(drawn({"type": "bar", "x": "person", "y": ["v"]}, self.COLS,
                               [["A", "lots", "1"]]), [])
        self.assertEqual(drawn({"type": "scatter", "x": "person", "y": ["v"]}, self.COLS,
                               [["A", "1", "1"]]), [])   # a scatter's x is a number

    def test_two_currencies_never_share_an_axis_or_a_total(self):
        cols = ("customer", "currency", "owed")
        rows = [["A", "CAD", "100.00"], ["B", "USD", "80.00"]]
        for kind in ("bar", "pie", "waterfall", "kpi", "line"):
            spec = {"type": kind, "x": "customer", "y": ["owed"]}
            self.assertEqual(drawn(spec, cols, rows), [], kind)
        (split,) = drawn({"type": "bar", "x": "customer", "split": "currency", "y": ["owed"]},
                         cols, rows)
        self.assertIsNone(split["currency"])
        for kind in ("bar", "area"):   # a stack's height would add the currencies
            self.assertEqual(drawn({"type": kind, "x": "customer", "split": "currency",
                                    "y": ["owed"], "stacked": True}, cols, rows), [], kind)
        (by,) = drawn({"type": "bar", "x": "currency", "y": ["owed"]}, cols, rows)
        self.assertEqual(by["labels"], ["CAD", "USD"])
        self.assertEqual(drawn({"type": "pie", "x": "currency", "y": ["owed"]}, cols, rows),
                         [])   # a share across currencies, even labelled by them
        (one,) = drawn({"type": "pie", "x": "customer", "y": ["owed"]}, cols,
                       [["A", "CAD", "100.00"], ["B", "CAD", "80.00"]])
        self.assertEqual(one["currency"], "CAD")

    def test_a_whole_of_a_result_cut_short_is_refused(self):
        rows = [[f"c{i}", str(i)] for i in range(1, 6)]
        for kind in ("pie", "donut", "waterfall", "histogram"):
            spec = {"type": kind, "x": "customer", "y": ["v"]}
            self.assertEqual(drawn(spec, ("customer", "v"), rows, truncated=True), [], kind)
        (bar,) = drawn({"type": "bar", "x": "customer", "y": ["v"]}, ("customer", "v"), rows,
                       truncated=True)
        self.assertTrue(bar["partial"])   # drawn, and says the rows are the first ones

    def test_negative_share_is_refused(self):
        self.assertEqual(drawn({"type": "pie", "x": "a", "y": ["v"]}, ("a", "v"),
                               [["x", "5"], ["y", "-1"]]), [])

    def test_a_block_naming_another_result_draws_nothing(self):
        text = ('```chart\n{"type": "bar", "result": "sql://ffffffffffffffffffff", '
                '"x": "person", "y": ["v"]}\n```')
        self.assertEqual(ch.blocks(text, [result(self.COLS, self.ROWS)]), ("", []))
        failed = dict(result(self.COLS, self.ROWS), is_error=True)
        text = text.replace("ffffffffffffffffffff", "0123456789abcdef0123")
        self.assertEqual(ch.blocks(text, [failed])[1], [])


class OpenPeriodTest(unittest.TestCase):
    """A period the data's as-of falls inside (this month, this week) is named: its figures
    are of part of it, and a chart or a KPI change would otherwise read as a drop."""

    def test_a_window_starting_inside_the_first_period_names_it(self):
        now = "2026-11-18T22:00:00+00:00"
        months = ["2026-09", "2026-10", "2026-11"]
        win = "SELECT m, n FROM t WHERE d >= now() - interval '{}' GROUP BY 1"
        self.assertEqual(ch.first_period(months, now, win.format("60 days")), "2026-09")
        self.assertEqual(ch.first_period(months, now, win.format("9 weeks")), "2026-09")
        self.assertIsNone(ch.first_period(months, now, win.format("48 days")))  # 1 Oct: whole
        self.assertIsNone(ch.first_period(months, now, win.format("200 days")))  # before Sep
        self.assertIsNone(ch.first_period(months, now, None))
        self.assertIsNone(ch.first_period(months, now, "SELECT m, n FROM t"))
        two = win.format("60 days") + " AND e > now() - interval '30 days'"
        self.assertIsNone(ch.first_period(months, now, two))                 # two bounds
        weeks = ["2026-10-26", "2026-11-02", "2026-11-09", "2026-11-16"]
        self.assertEqual(ch.first_period(weeks, now, win.format("20 days")), "2026-10-26")
        self.assertIsNone(ch.first_period(weeks, now, win.format("23 days")))  # 26 Oct whole
        days = ["2026-11-16", "2026-11-17", "2026-11-18"]
        self.assertIsNone(ch.first_period(days, now, win.format("2 days")))    # a day is whole
        self.assertIsNone(ch.first_period(["2026-11-09", "2026-11"], now, win.format("9 days")))
        self.assertIsNone(ch.first_period(months, now, win.format("3 months")))  # Aug not shown
        aug = ["2026-08"] + months
        self.assertEqual(ch.first_period(aug, now, win.format("3 months")), "2026-08")
        year = [f"{y}-{m:02d}" for y, m in [(2025, 11), (2025, 12)] + [(2026, k) for k in range(1, 12)]]
        self.assertEqual(ch.first_period(year, now, win.format("1 year")), "2025-11")
        days = "SELECT m, n FROM t WHERE d >= current_date - 60 GROUP BY 1"
        self.assertEqual(ch.first_period(months, now, days), "2026-09")
        self.assertIsNone(ch.first_period(months, now, "SELECT m FROM t WHERE d >= current_date - 2 * 30"))
        self.assertEqual(ch._months_back(__import__("datetime").date(2026, 3, 31), 1),
                         __import__("datetime").date(2026, 2, 28))
        c = ch.draw({"type": "bar", "x": "k", "y": ["n"]}, ("k", "n"),
                    [[x, "1"] for x in months], sql=win.format("60 days"), as_of=now)
        self.assertEqual(c.get("first_period"), "2026-09")

    def test_the_period_not_over_is_named_and_a_closed_or_mixed_axis_is_not(self):
        cases = [(["2026-10", "2026-11"], "2026-11"),               # as of 2026-11-18
                 (["2026-09", "2026-10"], None),
                 (["2026-11-02", "2026-11-09", "2026-11-16"], "2026-11-16"),   # weeks
                 (["2026-11-16", "2026-11-17", "2026-11-18"], "2026-11-18"),   # days
                 (["2026-11-02", "2026-11-09"], None),               # week before is over
                 (["2026-Q3", "2026-Q4"], "2026-Q4"), (["2025", "2026"], "2026"),
                 (["Acme", "2026-11"], None), (["2026-11-01", "2026-11"], None),
                 (["2026-11-18"], None)]                             # one date: no step
        evening = "2026-12-01T03:00:00+00:00"    # 10 PM on 30 Nov in New York: November
        self.assertEqual(ch.open_period(["2026-10", "2026-11"], evening), "2026-11")
        self.assertIsNone(ch.open_period(["2026-11", "2026-12"], "not a time"))
        for labels, want in cases:
            (c,) = drawn({"type": "bar", "x": "k", "y": ["n"]}, ("k", "n"),
                         [[x, "1"] for x in labels])
            self.assertEqual(c.get("open_period"), want, labels)
        (c,) = drawn({"type": "pie", "x": "k", "y": ["n"]}, ("k", "n"),
                     [["2026-10", "1"], ["2026-11", "2"]])
        self.assertIsNone(c.get("open_period"))                 # a share has no time axis

    def test_files_say_it_and_a_kpi_names_what_it_compares(self):
        from operating import chart_docs, chart_ooxml as ox
        (c,) = drawn({"type": "kpi", "x": "month", "y": ["orders"]}, ("month", "orders"),
                     [["2026-09", "300"], ["2026-10", None], ["2026-11", "197"]])
        self.assertIn("2026-11 is not over yet: its figures are of part of it.",
                      ox.provenance(c))
        _, (values, change), _ = chart_docs._grid(c)
        self.assertEqual(values, ["197"])
        self.assertEqual(change, ["-103 (-34%), 2026-11 so far vs 2026-09"])   # null skipped
        _, (_, change), _ = chart_docs._grid(dict(c, first_period="2026-09"))
        self.assertEqual(change, ["-103 (-34%), 2026-11 so far vs 2026-09 (part)"])  # both partial


class CardinalityTest(unittest.TestCase):
    def test_no_row_lost_or_duplicated_and_fast(self):
        for n in (1, 10, 60, 200):
            rows = [[f"p{i}", str(i), str(Decimal(i) / 3)] for i in range(n)]
            started = time.monotonic()
            (c,) = drawn({"type": "bar", "x": "p", "y": ["a", "b"]}, ("p", "a", "b"), rows)
            self.assertLess(time.monotonic() - started, 0.5)
            shown = min(n, ch.MAX_CHART_POINTS)
            self.assertEqual((c["rows"], c["shown"], len(c["labels"])), (n, shown, shown))
            self.assertEqual(c["labels"], [f"p{i}" for i in range(shown)])
            (t,) = drawn({"type": "scatter", "x": "a", "y": ["b"], "trend": n >= 3},
                         ("p", "a", "b"), rows)
            self.assertEqual(len(t["xs"]), n)


class DocumentChartsTest(unittest.TestCase):
    """plx.make_document's charts are drawn from their statement read again as the person,
    never from numbers the model put in the call."""

    def backend(self, out):
        from operator_api.assistant_tools import AssistantToolsMixin
        read = []

        class Worker:
            def run(self, reader, job, timeout=0, **_how):
                view = type("V", (), {})()
                view._call = lambda tool, args: (read.append((reader, args["sql"])), out)[1]
                return job(view)

        class B(AssistantToolsMixin):
            reads = Worker()
        return B(), read

    def test_drawn_from_the_rows_read_as_the_person(self):
        out = result(("person", "n"), [["A", "5"], ["B", "7"]])["result"]
        b, read = self.backend(out)
        got = b._document_charts("ana", [{"sql": "SELECT person, n FROM t", "type": "bar",
                                          "x": "person", "y": ["n"], "title": "N",
                                          "values": [999, 999]}])
        self.assertEqual(read, [("ana", "SELECT person, n FROM t")])
        self.assertEqual(got[0]["series"][0]["values"], [5.0, 7.0])
        self.assertEqual(got[0]["sql"], "SELECT person, n FROM t")

    def test_refused_with_why(self):
        from operator_api.errors import UserFacing
        out = result(("person", "currency", "n"), [["A", "CAD", "5"], ["B", "USD", "7"]])[
            "result"]
        b, _ = self.backend(out)
        for raw in ([{"type": "bar", "x": "person", "y": ["n"]}],          # no statement
                    [{"sql": "SELECT 1", "type": "radar", "x": "a", "y": ["n"]}],
                    [{"sql": "SELECT 1", "type": "pie", "x": "person", "y": ["n"]}],
                    "not a list", [{}] * 9):
            with self.assertRaises(UserFacing):
                b._document_charts("ana", raw)
        self.assertEqual(b._document_charts("ana", None), [])


if __name__ == "__main__":
    unittest.main()
