"""The metrics catalogue (``operating.measures``) and plx.sql's ``{"measure": ...}``.

Checked at the boundary: every catalogue entry names only columns its view holds and every
statement it can make passes plx.sql's own checker; a name, dimension, period or argument
the catalogue does not hold is refused before anything runs (fail closed); through the door
a measure gives each reader exactly the rows the same statement sent as SQL gives them
(withheld cells stay withheld, a reader with nothing sees nothing), with its definition
beside them; a comparison's shares of the change add up to 100 per currency."""
from __future__ import annotations

import datetime as dt
import re
import unittest
from decimal import Decimal

from operating import measures
from operating import ontology_tools_sql as sqlt
from operating.ontology_tools_identity import ToolError
from operating.tests import test_ontology_tools_sql as tsql

FULL, OPS, NOBODY = tsql.FULL, tsql.OPS, tsql.NOBODY


def _true(cell):
    return cell in (True, "true", "t", 1)


def _variants():
    for m in measures.typed().values():
        yield m["id"], {}
        for d in m["dimensions"]:
            yield m["id"], {"by": d}
        if m["date"]:
            yield m["id"], {"compare": True}
            yield m["id"], {"compare": True, "by": m["dimensions"][0], "days": 7}
            yield m["id"], {"by": "month"}
            yield m["id"], {"by": "week", "days": 56}
            yield m["id"], {"period": "2026-09"}
            yield m["id"], {"by": "month", "period": "2026-Q3"}
            yield m["id"], {"compare": True, "by": m["dimensions"][0], "period": "2026-09"}
            yield m["id"], {"period": "2026-08-04..2026-08-17", "against": "2025"}
            yield m["id"], {"unusual": True}
            yield m["id"], {"by": "*", "compare": True}
            yield m["id"], {"by": "*", "compare": True, "period": "2026-09",
                            "filter": {m["dimensions"][0]: "x"}}
            yield m["id"], {"unusual": True, "by": m["dimensions"][0], "days": 30}
            yield m["id"], {"compare": True, "by": m["dimensions"][0], "period": "2026-09",
                            "filter": {m["dimensions"][-1]: ["O'Neil & Co", "x"]}}
        yield m["id"], {"by": m["dimensions"][0], "filter": {m["dimensions"][0]: "x"}}
    yield measures.SCAN, {"unusual": True}
    yield measures.SCAN, {"unusual": True, "days": 30}
    for r in measures.reports():
        yield r, {}


class Catalogue(unittest.TestCase):
    def test_every_entry_names_only_its_views_columns_and_every_statement_is_checked(self):
        views = sqlt._dictionary()
        for m in measures.typed().values():
            cols = set(views[m["view"]]["columns"])
            named = set(m["dimensions"]) | {c for c in (m["per"], m["date"]) if c}
            for agg in (m["value"], m.get("rate_value") or ""):
                named |= set(re.findall(r"\((?:DISTINCT )?([a-z_]+)\)", agg)) - {"distinct"}
            self.assertLessEqual(named, cols, m["id"])
            for field in ("id", "name", "definition", "owner", "unit"):
                self.assertTrue(m[field], (m["id"], field))
        self.assertGreater(len(measures.reports()), 0)
        # the people views (the Team rule cuts them per viewer after the door) are not in it
        import json
        tiles = json.loads(measures.REPORTS.read_text(encoding="utf-8"))["insights"]
        people = [t["id"] for t in tiles if t.get("scope") not in ("everyone", "full")
                  or t.get("person") or t.get("rollup")]
        self.assertTrue(people)
        for iid in people:
            with self.assertRaises(ToolError):
                measures.statement(iid)
        n = 0
        for name, opts in _variants():
            sql, entry = measures.statement(name, **opts)
            sqlt.check(sql, allowed_schema="")
            self.assertEqual((entry["id"], entry["sql"]), (name, sql))
            n += 1
        self.assertGreater(n, 100)

    def test_a_measure_leaves_out_pilot_rows_and_orders_of_no_record(self):
        """BC pilot invoices are not business (Sage is the system of record; plx.sql's
        rules say leave them out unless asked): every statement of a measure over a view
        with a ``pilot`` column reads only the other rows (live 11-21: revenue for July
        2026 CAD counted 930,586.59 of pilot lines, September's 10.00 made a "why" answer's
        change disagree with its own months)."""
        views = sqlt._dictionary()
        piloted = [m for m in measures.typed().values()
                   if "pilot" in views[m["view"]]["columns"]]
        self.assertTrue(piloted)
        # the same for orders no Sage copy holds (sales_orders.of_record = false: FileMaker
        # order entry, Portal-only, BC pilot; live 11-21 late_orders 66 for the 55 of record)
        of_record = [m for m in measures.typed().values()
                     if "of_record" in views[m["view"]]["columns"]]
        self.assertTrue(of_record)
        for name, opts in _variants():
            m = measures.measures().get(name)
            if m in piloted:
                # a stated parameter (S2, R1), applied at its default (off)
                self.assertRegex(measures.statement(name, **opts)[0], r"\bNOT \(pilot\)",
                                 (name, opts))
                self.assertIn("bc_pilot", [p["id"] for p in measures.parameters_of(m)])
            if m in of_record:
                self.assertRegex(measures.statement(name, **opts)[0],
                                 r"(?<!NOT )\bof_record\b(?! *=)", (name, opts))

    def test_a_named_period_reads_its_edges_in_the_company_time_zone(self):
        """A month of mail is the month in Toronto (live 11-21: Oct 2026 mail sent was 3
        messages more read in UTC): each edge is an untyped literal at midnight
        America/New_York, which a date column reads as that same date."""
        sql, _ = measures.statement("mail_sent", None, None, True, "2026-10")
        for edge in ("2026-09-01", "2026-10-01", "2026-11-01"):
            self.assertIn(f"'{edge} 00:00 America/New_York'", sql)
        self.assertNotIn("DATE '", sql)

    def test_what_the_catalogue_does_not_hold_is_refused(self):
        snapshot = next(m["id"] for m in measures.typed().values() if not m["date"])
        dated = next(m["id"] for m in measures.typed().values() if m["date"])
        DIM = measures.measures()[dated]["dimensions"][0]
        report = next(iter(measures.reports()))
        bad = [("no_such_measure", {}), (True, {}), (None, {}), (dated, {"by": "handle"}),
               (dated, {"by": "customer; DROP TABLE x"}), (dated, {"by": 3}),
               (dated, {"days": 0}), (dated, {"days": 99999}), (dated, {"days": "30"}),
               (dated, {"days": True}), (dated, {"compare": "yes"}),
               (dated, {"by": "month", "compare": True}), (snapshot, {"compare": True}),
               (snapshot, {"days": 30}), (snapshot, {"by": "month"}),
               (report, {"by": "customer"}), (report, {"days": 7}),
               # a named period: only the calendar forms, never guessed or mixed with days
               (dated, {"period": "September"}), (dated, {"period": "2026-13"}),
               (dated, {"period": "2026-02-30"}), (dated, {"period": "2026-Q5"}),
               (dated, {"period": "2026-09-30..2026-09-01"}), (dated, {"period": 202609}),
               (dated, {"period": "2026-09'; DROP TABLE x; --"}),
               (dated, {"period": "2026-09", "days": 30}), (dated, {"against": "2026-08"}),
               (dated, {"period": "2026-09", "against": "2026-09-15..2026-10-15"}),
               (snapshot, {"period": "2026-09"}), (report, {"period": "2026-09"}),
               # a filter: only the measure's own dimensions and plain text values
               (dated, {"filter": {"handle": "x"}}), (dated, {"filter": {"month": "x"}}),
               (dated, {"filter": "customer = 'x'"}), (dated, {"filter": {}}),
               (dated, {"filter": {DIM: 3}}), (dated, {"filter": {DIM: ""}}),
               (dated, {"filter": {DIM: "a\\' OR true --"}}), (dated, {"filter": {DIM: "a\nb"}}),
               (dated, {"filter": {DIM: ["x"] * 21}}), (dated, {"filter": {DIM: [None]}}),
               (report, {"filter": {DIM: "x"}}),
               # unusual: a dated measure's own periods, never with compare or a period
               (dated, {"unusual": "yes"}), (dated, {"unusual": True, "compare": True}),
               (dated, {"unusual": True, "period": "2026-09"}),
               (dated, {"unusual": True, "by": "month"}), (snapshot, {"unusual": True}),
               (report, {"unusual": True}), (measures.SCAN, {}),
               (measures.SCAN, {"unusual": True, "by": DIM}),
               (measures.SCAN, {"unusual": True, "filter": {DIM: "x"}}),
               (dated, {"by": "*"}), (dated, {"by": "*", "unusual": True}),
               # month by month against an earlier period names its period; a pair of
               # breakdowns is two different ones, never with compare
               (dated, {"by": "month", "compare": True}),
               (dated, {"by": [DIM, DIM]}), (dated, {"by": [DIM]}),
               (dated, {"by": [DIM, "month"], "compare": True}),
               (dated, {"by": ["week", "month"]}),
               (dated, {"period": "since:nonsense"}), (dated, {"period": "FY2026-Q5"}),
               (dated, {"period": "last_0_months"}),
               (snapshot, {"by": "*", "compare": True})]
        for name, opts in bad:
            with self.assertRaises(ToolError, msg=(name, opts)) as caught:
                measures.statement(name, **opts)
            self.assertEqual(caught.exception.code, "invalid_request")
        # a forecast: the next 1..12 months (1..26 weeks) of a dated measure only
        for name, horizon, opts in ((dated, 0, {}), (dated, 13, {}), (dated, "3", {}),
                                    (dated, True, {}), (dated, 27, {"by": "week"}),
                                    (dated, 3, {"by": DIM}), (snapshot, 3, {}),
                                    (report, 3, {}), ("nope", 3, {}),
                                    (dated, 3, {"filter": {"handle": "x"}})):
            with self.assertRaises(ToolError, msg=(name, horizon, opts)):
                measures.forecast_statement(name, horizon, **opts)
        tool = sqlt.SqlTool(base=None)
        for args in ({"measure": dated, "forecast": 3, "days": 30},
                     {"measure": dated, "forecast": 3, "compare": True},
                     {"measure": dated, "forecast": 3, "period": "2026-09"},
                     {"measure": dated, "forecast": 3, "unusual": True},
                     {"sql": "SELECT 1", "filter": {DIM: "x"}}, {"unusual": True},
                     {"describe": True, "forecast": 3}):
            self.assertEqual(tool.call("plx.sql", args, FULL)["error"]["code"],
                             "invalid_request", args)

    def test_the_scan_of_every_measure_is_one_checked_statement(self):
        """Every dated measure in one statement under plx.sql's 8,000-character limit
        (21 one-measure statements were 21,140 characters): one row per measure and
        currency, the snapshots named as not judged."""
        sql, entry = measures.statement(measures.SCAN, unusual=True)
        sqlt.check(sql, allowed_schema="")
        # SETS is a keyword only right after GROUPING: never a way past the function list
        sqlt.check("SELECT a, count(*) FROM t GROUP BY GROUPING SETS ((a), ())",
                   allowed_schema="")
        for bad in ("SELECT sets(1)", "SELECT x FROM t GROUP BY sets (a)"):
            with self.assertRaises(ToolError, msg=bad):
                sqlt.check(bad, allowed_schema="")
        for m in measures.typed().values():
            self.assertEqual(f"'{m['id']}'," in sql, bool(m["date"]), m["id"])
            if not m["date"]:
                self.assertIn(m["id"], entry["definition"])

    def test_a_forecast_states_its_method_band_and_holdout_and_refuses_what_it_cannot(self):
        """From the history rows alone (demand_forecast's method): the forecast runs from
        the current month on, each with its 80% band, and the holdout scores the same rule
        on the last months; a series below zero or with a withheld cell is not forecast
        (said), never guessed."""
        today = dt.date(2026, 11, 21)
        sql, entry = measures.forecast_statement("revenue", 3, today=today)
        self.assertIn("'2023-11-01 00:00 America/New_York'", sql)   # 36 full months
        self.assertIn("'2026-11-01 00:00 America/New_York'", sql)   # this month left out
        rows, month = [], dt.date(2023, 11, 1)
        while month < dt.date(2026, 11, 1):
            for cur, base in (("CAD", 1000), ("USD", -5), ("EUR", 50)):
                rows.append([month, cur, Decimal(base + 10 * (month.month % 4)), 3,
                             1 if cur == "EUR" and month.month == 5 else 0])
            month = measures._months_on(month, 1)
        cols = ["month", "revenue_currency", "value", "row_count", "unread_cells"]
        got_cols, got, cite = measures.forecasted(cols, rows, entry, today=today)
        self.assertEqual(got_cols, ["month", "revenue_currency", "value", "kind", "low_80",
                                    "high_80"])
        ahead = [r for r in got if r[3] == "forecast"]
        self.assertEqual([(r[0], r[1]) for r in ahead],
                         [("2026-11-01", "CAD"), ("2026-12-01", "CAD"), ("2027-01-01", "CAD")])
        for r in ahead:
            self.assertLessEqual(Decimal(r[4]), Decimal(r[2]))
            self.assertLessEqual(Decimal(r[2]), Decimal(r[5]))
        self.assertEqual(len([r for r in got if r[3] == "actual"]), len(rows))   # none lost
        said = {s["revenue_currency"]: s for s in cite["series"]}
        self.assertEqual(said["CAD"]["history"]["periods"], 36)
        self.assertEqual(said["CAD"]["holdout"]["periods"], 3)
        self.assertIn("naive_mae", said["CAD"]["holdout"])
        self.assertIn("below zero", said["USD"]["not_forecast"])
        self.assertIn("may not read", said["EUR"]["not_forecast"])
        self.assertIn("holdout", cite["method"])

    def test_plx_sql_takes_one_mode_and_the_measure_arguments_only_with_measure(self):
        tool = sqlt.SqlTool(base=None)
        for args in ({"measure": "revenue", "sql": "SELECT 1"},
                     {"measure": "revenue", "describe": True}, {"by": "customer"},
                     {"sql": "SELECT 1", "days": 7}, {"describe": True, "compare": True},
                     {"measure": "revenue", "unknown": 1}, {"measure": False},
                     {"measure": "nope"}, {"measure": "revenue", "by": "nope"},
                     {"sql": "SELECT 1", "period": "2026-09"}, {"against": "2026-08"},
                     {"measure": "revenue", "period": "Sept 2026"}):
            out = tool.call("plx.sql", args, FULL)
            self.assertEqual(out["error"]["code"], "invalid_request", args)
        # the list is definitions only: it reads no store (here: none at all)
        listed = tool.call("plx.sql", {"measure": True}, FULL)
        self.assertEqual({m["id"] for m in listed["measures"]}, set(measures.measures()))
        self.assertTrue(all(m["definition"] and m["owner"] for m in listed["measures"]))
        self.assertTrue(measures.lines())


class MovingPeriods(unittest.TestCase):
    def test_an_unknown_or_untyped_moving_period_is_refused_never_guessed(self):
        from unittest import mock
        for bad in ("this_decade", "last_0_months", "last_13_weeks", "next_month", "thismonth"):
            with self.assertRaises(ToolError, msg=bad):
                measures._period(bad, "period")
        self.assertEqual(measures._period("This Month", "period")[:4],
                         measures._period("this_month", "period")[:4])
        measures._is_day_column.cache_clear()
        with mock.patch("operating.ontology_tools_sql._dictionary",
                        return_value={"sales_invoice_lines": {"columns": {}}}):
            with self.assertRaises(ToolError):
                measures.statement("revenue", period="this_month")
            # a fixed period needs no type: its literal reads as either
            measures.statement("revenue", period="2026-11")
        measures._is_day_column.cache_clear()

    def test_the_period_before_a_moving_one_is_the_same_kind_just_before(self):
        from unittest import mock
        with mock.patch.object(measures, "business_today", return_value=dt.date(2028, 2, 29)):
            got = {t: measures._period(t, "p")[:2] for t in measures.RELATIVE}
            before = {t: measures._before(measures._period(t, "p"))[:2]
                      for t in measures.RELATIVE}
        d = dt.date
        self.assertEqual(got["this_month"], (d(2028, 2, 1), d(2028, 3, 1)))
        self.assertEqual(got["last_quarter"], (d(2027, 10, 1), d(2028, 1, 1)))
        self.assertEqual(got["this_week"], (d(2028, 2, 28), d(2028, 3, 6)))
        self.assertEqual(got["last_12_months"], (d(2027, 2, 1), d(2028, 2, 1)))
        self.assertEqual(got["year_to_date"], (d(2028, 1, 1), d(2028, 3, 1)))
        self.assertEqual(before["year_to_date"], (d(2027, 1, 1), d(2027, 3, 1)))
        self.assertEqual(before["last_year"], (d(2026, 1, 1), d(2027, 1, 1)))
        self.assertEqual(before["last_week"], (d(2028, 2, 14), d(2028, 2, 21)))


class MeasureDoor(unittest.TestCase):
    """Through plx.sql on the business-views fixture, as plx.sql's own tests read it."""

    setUpClass = classmethod(tsql.SqlDoor.setUpClass.__func__)
    tearDownClass = classmethod(tsql.SqlDoor.tearDownClass.__func__)
    setUp = tsql.SqlDoor.setUp
    base = tsql.SqlDoor.base

    def call(self, args, actor):
        return sqlt.SqlTool(self.base()).call("plx.sql", args, actor)

    def rows(self, out):
        self.assertNotIn("error", out, out)
        return [[tsql._cell(c) for c in r["cells"]] for r in out["rows"]]

    def covered_days(self, name, actor):
        """The longest period whose earlier half the source covers for this reader (its
        first record on or before the earlier period's start)."""
        m = measures.measures()[name]
        out = self.call({"sql": f"SELECT min({m['date']})::date FROM {m['view']}"}, actor)
        first = dt.date.fromisoformat(str(self.rows(out)[0][0])[:10])
        return max(1, (dt.date.today() - first).days // 2 - 1)

    def test_a_stated_exclusion_is_read_beside_the_figure_and_adds_up(self):
        """S2 through the door: a parameter left off makes the read carry ``excluded`` (its
        own result, the measure's value over the rows it leaves out); counted in, the
        figure equals the figure plus the excluded value and nothing is excluded. Fixture
        registry (MODEL's R1 not landed): a count measure with a parameter on its view."""
        from unittest import mock
        real = dict(measures.measures())
        m = next(x for x in real.values() if x["value"].startswith("count") and not x["date"])
        col = "source_systems"
        fixture = {**m, "parameters": [{"id": "one_system", "default": False,
                                         "excludes_where": f"{col} NOT LIKE '%,%'",
                                         "says": "held in one system only"}]}
        with mock.patch.object(measures, "measures", return_value={**real, m["id"]: fixture}):
            off = self.call({"measure": m["id"]}, FULL)
            on = self.call({"measure": m["id"], "parameters": {"one_system": True}}, FULL)
            bad = self.call({"measure": m["id"], "parameters": {"nope": True}}, FULL)
        if off.get("error", {}).get("code") in ("unknown_view", "not_ready"):
            self.skipTest(f"{m['view']} not in the fixture")
        self.assertIn("error", bad)
        (ex,) = off["excluded"]
        self.assertEqual((ex["parameter"], ex["says"]), ("one_system", "held in one system only"))
        value = lambda out: Decimal(str(tsql._cell(out["rows"][0]["cells"][out["columns"].index("value")]) or 0))
        self.assertEqual(value(on), value(off) + Decimal(str(tsql._cell(ex["value"]) or 0)))
        # n = how many records the parameter leaves out: the rows counted in add exactly n
        rows = lambda out: int(str(tsql._cell(out["rows"][0]["cells"][out["columns"].index("row_count")])))
        self.assertIsInstance(ex["n"], int)
        self.assertEqual(rows(on), rows(off) + ex["n"])
        self.assertEqual(on["excluded"], [])

    def test_a_moving_period_reads_what_today_s_dates_read_and_names_no_date(self):
        """this_month, year_to_date ... are statements on now(), never today's dates (a
        pinned chart of them moves with the date), and through the door they read exactly
        what the same days named as fixed dates read: on a date column and a timestamp one,
        alone and compared with the period before."""
        dated = [m for m in measures.typed().values() if m["date"]]
        kinds = {measures._is_day_column(m["view"], m["date"]) for m in dated}
        self.assertEqual(kinds, {True, False})      # both column types are exercised
        for text in measures.RELATIVE:
            p = measures._period(text, "period")
            fixed = f"{p[0].isoformat()}..{(p[1] - dt.timedelta(days=1)).isoformat()}"
            for m in dated:
                sql, cited = measures.statement(m["id"], period=text)
                self.assertIsNone(re.search(r"'\d{4}-\d{2}-\d{2}", sql), (text, m["id"]))
                self.assertIn("moves with the date", cited["period"])
                a = self.call({"measure": m["id"], "period": text}, FULL)
                if a.get("error", {}).get("code") in ("unknown_view", "not_ready"):
                    continue
                b = self.call({"measure": m["id"], "period": fixed}, FULL)
                self.assertEqual(self.rows(a), self.rows(b), (text, m["id"]))
            m = measures.measures()["revenue"]
            before = measures._before(p)
            against = f"{before[0].isoformat()}..{(before[1] - dt.timedelta(days=1)).isoformat()}"
            a = self.call({"measure": "revenue", "period": text, "compare": True,
                           "by": "customer"}, FULL)
            b = self.call({"measure": "revenue", "period": fixed, "against": against,
                           "by": "customer"}, FULL)
            self.assertEqual(self.rows(a), self.rows(b), text)

    def test_a_moving_edge_is_the_company_s_calendar_not_utc(self):
        """At 9 PM in Toronto on 30 November it is 1 December in UTC: "this_month" is still
        November, on a date column and on a timestamp column."""
        import psycopg
        late = "'2026-12-01 02:00+00'::timestamptz"      # 30 Nov, 9 PM in Toronto
        p = measures._period("this_month", "period")
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            day = [conn.execute("SELECT " + e.replace("now()", late)).fetchone()[0]
                   for e in (f"({x})::date" for x in p[4])]
            stamp = [conn.execute("SELECT " + f"(({x}) AT TIME ZONE 'America/New_York')"
                                  .replace("now()", late) + " AT TIME ZONE 'UTC'").fetchone()[0]
                     for x in p[4]]
            ytd = measures._before(measures._period("year_to_date", "period"))[4]
            last_year = [conn.execute("SELECT " + f"({x})::date".replace("now()", late))
                         .fetchone()[0] for x in ytd]
        self.assertEqual(day, [dt.date(2026, 11, 1), dt.date(2026, 12, 1)])
        self.assertEqual(stamp, [dt.datetime(2026, 11, 1, 4), dt.datetime(2026, 12, 1, 5)])
        self.assertEqual(last_year, [dt.date(2025, 1, 1), dt.date(2025, 12, 1)])

    def test_a_comparison_never_reads_the_whole_source_for_its_first_record(self):
        """Through a reader's door a view's unbounded min reads every row (~20 s for mail,
        three times per comparison): coverage is an EXISTS before the earlier period, and
        the first record is read only after the start, only when it is not covered."""
        for m in measures.typed().values():
            if not m["date"]:
                continue
            for by in [None] + m["dimensions"][:1]:
                sql, _ = measures.statement(m["id"], by, 20, True)
                self.assertNotIn(f"(SELECT min({m['date']}) FROM {m['view']})", sql, m["id"])
                self.assertEqual(sql.count(f"(SELECT min({m['date']}) FROM {m['view']} WHERE "
                                           f"{m['date']} > now() - interval '40 days')"), 1)

    def test_a_comparison_before_the_source_begins_is_unknown_not_zero(self):
        """An earlier period starting before the source's first record (a source captured
        only lately) is not read as 0: previous and change are NULL, the row says covered
        false, incomplete, and the date the source starts; within the source's reach the
        same measure compares as before."""
        for m in measures.typed().values():
            if not m["date"]:
                continue
            out = self.call({"measure": m["id"], "compare": True, "days": 3650}, FULL)
            if out.get("error", {}).get("code") in ("unknown_view", "not_ready"):
                continue    # a view the fixture does not build
            for r in self.rows(out):
                row = dict(zip(out["columns"], r))
                self.assertFalse(_true(row["covered"]), (m["id"], row))
                if int(row["current_rows"]) + int(row["previous_rows"]):
                    self.assertIsNotNone(row["data_from"], (m["id"], row))
                self.assertTrue(_true(row["incomplete"]), (m["id"], row))
                self.assertIsNone(row["previous"], (m["id"], row))
                self.assertIsNone(row["change"], (m["id"], row))
                self.assertIsNone(row["share_of_change_pct"], (m["id"], row))
        out = self.call({"measure": "invoices_issued", "compare": True,
                         "days": self.covered_days("invoices_issued", FULL)}, FULL)
        rows = [dict(zip(out["columns"], r)) for r in self.rows(out)]
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(_true(row["covered"]), row)
            self.assertIsNone(row["data_from"], row)
            if int(row["previous_rows"]) > 0:
                self.assertIsNotNone(row["previous"], row)
                self.assertIsNotNone(row["change"], row)

    def test_a_measure_gives_each_reader_what_its_statement_gives_them(self):
        cases = [("receivables_open", {}), ("receivables_open", {"by": "customer"}),
                 ("invoices_issued", {"days": 3650}), ("orders_booked", {"by": "month",
                                                                         "days": 3650}),
                 ("open_orders", {"by": "customer"})]
        for name, opts in cases:
            sql, _ = measures.statement(name, **opts)
            for actor in (FULL, OPS, NOBODY):
                got = self.call({"measure": name, **opts}, actor)
                same = self.call({"sql": sql}, actor)
                self.assertEqual(self.rows(got), self.rows(same), (name, opts, actor))
                self.assertEqual((got.get("partial"), got.get("withheld")),
                                 (same.get("partial"), same.get("withheld")), (name, actor))
                self.assertEqual(got["measure"]["id"], name)
                self.assertTrue(got["measure"]["definition"])
                if actor is NOBODY:
                    # nothing read: no value, and a comparison with nothing to compare
                    # says so (covered false, incomplete), never a figure
                    flags = ("covered", "incomplete")
                    self.assertTrue(all(c in (None, 0, "0") for r in self.rows(got)
                                        for c, k in zip(r, got["columns"]) if k not in flags),
                                    (name, self.rows(got)))
        # a finance cell the reader lacks stays withheld in the measure too
        ops = self.call({"measure": "receivables_open", "by": "customer"}, OPS)
        self.assertTrue(ops["partial"])
        for secret in tsql.tbv.SECRET:
            self.assertNotIn(secret, str(ops))

    def test_a_named_period_compares_exactly_the_calendar_days_it_names(self):
        """A "why did it change from <month> to <month>" reads the measure over the named
        calendar periods (live 11-21: with days only, Ask wrote its own SQL and its
        supplier parts counted draft POs the measure leaves out). Computed from the raw
        rows: each part's current and previous are its invoices on the named days (first
        and last day in, the day after out, pilot left out) and the parts add up to the
        whole change; without against the period just before is used."""
        out = self.call({"sql": "SELECT invoice_number, invoice_date, customer, pilot FROM "
                                "sales_invoices WHERE pilot OR NOT pilot"}, FULL)
        invoices = [dict(zip(out["columns"], r)) for r in self.rows(out)]
        days = sorted({dt.date.fromisoformat(str(r["invoice_date"])[:10]) for r in invoices})
        self.assertGreater(len(days), 2)
        # a period starting and ending on fixture days, so both edges are read
        a, b = days[len(days) // 2], days[-1]
        later = f"{a.isoformat()}..{b.isoformat()}"
        before = f"{days[0].isoformat()}..{(a - dt.timedelta(days=1)).isoformat()}"

        def count(lo, hi):
            per: dict = {}
            for r in invoices:
                d = dt.date.fromisoformat(str(r["invoice_date"])[:10])
                if lo <= d <= hi and not _true(r["pilot"]):
                    per.setdefault(r["customer"], set()).add(r["invoice_number"])
            return {k: len(v) for k, v in per.items()}

        now, then = count(a, b), count(days[0], a - dt.timedelta(days=1))
        self.assertTrue(now and then)
        got = self.call({"measure": "invoices_issued", "by": "customer", "period": later,
                         "against": before}, FULL)
        rows = [dict(zip(got["columns"], r)) for r in self.rows(got)]
        self.assertIn(later, got["measure"]["period"])
        for r in rows:
            self.assertEqual(int(r["current"] or 0), now.get(r["customer"], 0), r)
            self.assertEqual(int(r["previous"] or 0), then.get(r["customer"], 0), r)
        whole = sum(now.values()) - sum(then.values())
        self.assertEqual(sum(int(r["change"]) for r in rows), whole)
        # the same kind of period just before: a month against the month before it
        month = f"{b.year:04d}-{b.month:02d}"
        first = dt.date(b.year, b.month, 1)
        prev_last = first - dt.timedelta(days=1)
        got = self.call({"measure": "invoices_issued", "period": month, "compare": True},
                        FULL)
        row = dict(zip(got["columns"], self.rows(got)[0]))
        self.assertEqual(int(row["current"] or 0), sum(count(first, b).values()))
        prior = sum(count(dt.date(prev_last.year, prev_last.month, 1), prev_last).values())
        if _true(row["covered"]):
            self.assertEqual(int(row["previous"] or 0), prior)
        else:   # the fixture does not reach back that far: unknown, never 0
            self.assertIsNone(row["previous"])
        # a plain read of a named period counts only its days
        got = self.call({"measure": "invoices_issued", "period": later}, FULL)
        self.assertEqual(int(dict(zip(got["columns"], self.rows(got)[0]))["value"]),
                         sum(now.values()))

    def test_the_owner_measures_count_from_the_raw_rows(self):
        """Computed from the raw rows over a period holding the whole fixture: the loss on
        lines sold below cost sums the negative margins of lines sold for more than 0 (a
        credit or discount line is no sale below cost), per currency, pilot left out;
        customers invoiced counts each customer once."""
        def raw(sql):
            out = self.call({"sql": sql}, FULL)
            return [dict(zip(out["columns"], r)) for r in self.rows(out)]

        def per(name, key, **opts):
            out = self.call({"measure": name, **opts}, FULL)
            return {r.get(key, ""): r for r in (dict(zip(out["columns"], x))
                                                 for x in self.rows(out))}

        span = "2000-01-01..2099-12-31"
        lines = raw("SELECT revenue, margin, margin_currency, pilot FROM sales_invoice_lines "
                    "WHERE pilot OR NOT pilot")
        want: dict = {}
        for r in lines:
            if (r["margin"] is not None and r["revenue"] is not None and not _true(r["pilot"])
                    and Decimal(str(r["revenue"])) > 0 and Decimal(str(r["margin"])) < 0
                    and Decimal(str(r["margin"])) >= -4 * Decimal(str(r["revenue"]))):
                want[r["margin_currency"]] = want.get(r["margin_currency"], Decimal(0)) \
                    + Decimal(str(r["margin"]))
        self.assertTrue(want)
        got = per("loss_below_cost", "margin_currency", period=span)
        self.assertEqual({k: Decimal(str(v["value"])) for k, v in got.items()
                          if v["value"] is not None}, want)
        invoices = raw("SELECT customer, pilot FROM sales_invoices WHERE pilot OR NOT pilot")
        kept = {r["customer"] for r in invoices if not _true(r["pilot"]) and r["customer"]}
        self.assertTrue(kept)
        self.assertEqual(int(per("customers_invoiced", "", period=span)[""]["value"]),
                         len(kept))

    def test_a_cost_that_looks_wrong_is_listed_not_counted_and_hidden_margins_are_said(self):
        """A line costing more than 5 times its price (live: a case cost on an each line made
        2024 USD gross margin -103.6M) is left out of gross margin and the below-cost loss
        and counted by cost_looks_wrong; a reader who may not read a line's margin gets it
        counted in unread_cells, never silently dropped (live 11-21 review)."""
        span = "2000-01-01..2099-12-31"

        def rows(name, actor, **opts):
            out = self.call({"measure": name, "period": span, **opts}, actor)
            return [dict(zip(out["columns"], r)) for r in self.rows(out)]

        out = self.call({"sql": "SELECT handle FROM sales_invoice_lines WHERE NOT pilot AND "
                                "revenue > 0 AND margin IS NOT NULL ORDER BY handle LIMIT 1"},
                        FULL)
        handle = self.rows(out)[0][0]
        before = {r["margin_currency"]: r for r in rows("gross_margin", FULL)}
        wrong = sum(int(r["value"]) for r in rows("cost_looks_wrong", FULL))
        import psycopg
        table = f"{tsql.bv.SERVED_SCHEMA}.sales_invoice_lines"
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            kept = conn.execute(f"SELECT margin FROM {table} WHERE handle = %s",
                                (handle,)).fetchone()[0]
            conn.execute(f"UPDATE {table} SET margin = -10 * revenue WHERE handle = %s",
                         (handle,))
        try:
            after = {r["margin_currency"]: r for r in rows("gross_margin", FULL)}
            self.assertEqual(sum(int(r["value"]) for r in rows("cost_looks_wrong", FULL)),
                             wrong + 1)
            moved = [c for c in after if after[c]["value"] != before[c]["value"]]
            self.assertEqual(len(moved), 1)   # the line's own margin left the total
            self.assertEqual(int(after[moved[0]]["row_count"]),
                             int(before[moved[0]]["row_count"]) - 1)
        finally:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.execute(f"UPDATE {table} SET margin = %s WHERE handle = %s",
                             (kept, handle))
        # a reader whose margin is withheld: every line it cannot read is counted unread
        seen = rows("gross_margin", OPS)
        out = self.call({"sql": "SELECT count(*) FROM sales_invoice_lines WHERE NOT pilot "
                                "AND margin IS NULL AND margin_basis IS DISTINCT FROM "
                                "'cost_unknown'"}, OPS)
        hidden = int(self.rows(out)[0][0])
        self.assertGreater(hidden, 0)
        self.assertEqual(sum(int(r["unread_cells"]) for r in seen), hidden)

    def test_a_measure_counts_only_the_rows_its_rule_keeps(self):
        """Computed from the raw rows: invoices_issued counts the invoices that are not BC
        pilot ones, open_orders the open orders that are not quotes, whichever system holds them (11-23:
        the orders only FileMaker, the Portal or BC hold count too); the fixture holds a
        pilot invoice, so a measure counting it would be larger."""
        def raw(sql):
            out = self.call({"sql": sql}, FULL)
            return [dict(zip(out["columns"], r)) for r in self.rows(out)]

        def value(name, **opts):
            out = self.call({"measure": name, **opts}, FULL)
            rows = [dict(zip(out["columns"], r)) for r in self.rows(out)]
            self.assertEqual(len(rows), 1, (name, rows))
            return int(Decimal(str(rows[0]["value"])))

        since = dt.date.today() - dt.timedelta(days=3650)
        invoices = raw("SELECT invoice_number, invoice_date, pilot FROM sales_invoices "
                       "WHERE pilot OR NOT pilot")
        kept = {r["invoice_number"] for r in invoices if not _true(r["pilot"])
                and dt.date.fromisoformat(str(r["invoice_date"])[:10]) > since}
        self.assertTrue(any(_true(r["pilot"]) for r in invoices))
        self.assertEqual(value("invoices_issued", days=3650), len(kept))
        orders = raw("SELECT order_number, status, order_type, of_record, open_state FROM sales_orders "
                     "WHERE of_record OR NOT of_record")
        open_kept = {r["order_number"] for r in orders if r["status"] == "open"
                     and r["order_type"] != "quote" and r["open_state"] != "copy_of_record"}
        self.assertTrue(any(r["status"] == "open" and not _true(r["of_record"])
                            for r in orders))
        self.assertEqual(value("open_orders"), len(open_kept))

    def test_shares_of_a_change_add_up_to_100_per_currency(self):
        out = self.call({"measure": "receivables_open", "by": "customer"}, FULL)
        cols = out["columns"]
        by_cur: dict = {}
        for r in self.rows(out):
            row = dict(zip(cols, r))
            if row["share_pct"] is not None:
                by_cur.setdefault(row["amount_due_currency"], Decimal(0))
                by_cur[row["amount_due_currency"]] += Decimal(str(row["share_pct"]))
        self.assertTrue(by_cur)
        for cur, total in by_cur.items():
            self.assertAlmostEqual(float(total), 100.0, delta=0.1 * len(self.rows(out)), msg=cur)
        out = self.call({"measure": "invoices_issued", "by": "customer", "compare": True,
                         "days": self.covered_days("invoices_issued", FULL)}, FULL)
        cols = out["columns"]
        shares = [Decimal(str(dict(zip(cols, r))["share_of_change_pct"]))
                  for r in self.rows(out)]
        changes = [Decimal(str(dict(zip(cols, r))["change"])) for r in self.rows(out)]
        self.assertTrue(shares)
        if sum(changes):
            self.assertAlmostEqual(float(sum(shares)), 100.0, delta=0.1 * len(shares))

    def test_every_part_of_a_change_is_returned_and_adds_up_to_the_whole_change(self):
        """No part is folded or cut (guardrail 1): the rows of a breakdown by customer are
        every customer's, their changes add up to the whole change, the shares to 100."""
        days = self.covered_days("invoices_issued", FULL)
        whole = self.call({"measure": "invoices_issued", "compare": True, "days": days}, FULL)
        total = sum(Decimal(str(dict(zip(whole["columns"], r))["change"]))
                    for r in self.rows(whole))
        out = self.call({"measure": "invoices_issued", "by": "customer", "compare": True,
                         "days": days}, FULL)
        rows = [dict(zip(out["columns"], r)) for r in self.rows(out)]
        self.assertGreater(len(rows), 1)
        self.assertFalse(any(str(r["customer"]).startswith("All others (") for r in rows))
        self.assertEqual(sum(Decimal(str(r["change"])) for r in rows), total)
        if total:
            self.assertAlmostEqual(float(sum(Decimal(str(r["share_of_change_pct"]))
                                             for r in rows)), 100.0, delta=0.1 * len(rows))
        self.assertNotIn("LIMIT", measures.statement("invoices_issued", "customer")[0])

    def test_a_report_for_full_readers_runs_only_for_a_full_reader(self):
        """A report Insights lists only for the readers who see everyone (scope "full") is
        refused by name to anyone else, nothing read, and not listed for them."""
        from unittest import mock
        from operating.pg import people_excluded
        full = [r for r, e in measures.reports().items() if e["scope"] == "full"]
        self.assertTrue(full)
        as_full = mock.patch.object(people_excluded, "full_reader",
                                    lambda base, a: "tag" if a is FULL else "")
        with as_full:
            for actor in (OPS, NOBODY):
                listed = {r["id"] for r in self.call({"measure": True}, actor)["reports"]}
                self.assertFalse(listed & set(full))
                for rid in full:
                    out = self.call({"measure": rid}, actor)
                    self.assertEqual(out["error"]["code"], "not_readable", (rid, out))
                    self.assertNotIn("rows", out)
            listed = {r["id"] for r in self.call({"measure": True}, FULL)["reports"]}
            self.assertLessEqual(set(full), listed)
            out = self.call({"measure": "org_teams"}, FULL)
            self.assertNotIn("error", out, out)
            self.assertEqual(out["measure"]["id"], "org_teams")

    def test_a_comparison_never_reads_a_withheld_cell_as_zero(self):
        """A money cell the reader may not read is NULL: a period with rows but no readable
        value has no change (never "0 before, so all of it is new"), and a part with any
        unread cell is marked incomplete, in every currency when the unread rows' currency
        is blank too (live 11-22: a customer's one unpriced order this quarter read as "0,
        a drop of USD 43,700"; the fixture's Beta Inc revenue is such a part)."""
        unread = 0
        for m in measures.typed().values():
            if not m["date"] or m["value"].startswith("count("):
                continue
            for actor in (FULL, OPS):
                # the whole history (its earlier period before the source: not covered)
                # and the longest period the source covers
                for days in (3650, self.covered_days(m["id"], actor)):
                    out = self.call({"measure": m["id"], "by": m["dimensions"][0],
                                     "compare": True, "days": days}, actor)
                    rows = [dict(zip(out["columns"], r)) for r in self.rows(out)]
                    by, per = m["dimensions"][0], m["per"]
                    # a part whose rows with no readable value also have no currency or
                    # unit: those rows may be any of the part's, so all its rows are incomplete
                    blank = {row[by] for row in rows if per and row.get(per) is None
                             and int(row["unread_cells"]) > 0}
                    for row in rows:
                        blind = ((row["current"] is None and int(row["current_rows"]) > 0)
                                 or (row["previous"] is None
                                     and int(row["previous_rows"]) > 0))
                        if blind:
                            self.assertIsNone(row["change"], (m["id"], actor, row))
                            self.assertIsNone(row["share_of_change_pct"], (m["id"], row))
                        self.assertEqual(_true(row["incomplete"]),
                                         int(row["unread_cells"]) > 0
                                         or not _true(row["covered"])
                                         or row[by] in blank, (m["id"], days, row))
                        unread += int(row["unread_cells"])
        self.assertGreater(unread, 0)   # the fixture's finance marks reach a comparison


    def raw(self, sql, actor=FULL):
        out = self.call({"sql": sql}, actor)
        return [dict(zip(out["columns"], r)) for r in self.rows(out)]

    def test_a_filter_keeps_the_parts_it_names_and_each_reader_sees_what_sql_gives(self):
        """A follow-up's slice ("just that customer") is the earlier breakdown's own part,
        however the name is cased or padded; a name matching nothing is no rows (never a
        0); a quote in a name is a name, not SQL; each reader gets what the same statement
        sent as SQL gives them."""
        span = "2000-01-01..2099-12-31"
        whole = self.call({"measure": "revenue", "by": "customer", "period": span}, FULL)
        parts = [dict(zip(whole["columns"], r)) for r in self.rows(whole)]
        named = next(p["customer"] for p in parts if p["customer"] and p["value"] is not None)
        name = named if isinstance(named, str) else named["untrusted_text"]
        want = {p["revenue_currency"]: p["value"] for p in parts if p["customer"] == named}
        for typed in (name, f"  {name.upper()} "):
            out = self.call({"measure": "revenue", "period": span,
                             "filter": {"customer": typed}}, FULL)
            got = {r["revenue_currency"]: r["value"]
                   for r in (dict(zip(out["columns"], x)) for x in self.rows(out))}
            self.assertEqual(got, want, typed)
            self.assertEqual(out["measure"]["filter"], {"customer": typed})
        for typed in ("no such customer", "O'Neil'; DROP TABLE x; --"):
            out = self.call({"measure": "revenue", "period": span,
                             "filter": {"customer": typed}}, FULL)
            self.assertEqual(self.rows(out), [], typed)
        for actor in (FULL, OPS, NOBODY):
            args = {"measure": "revenue", "period": span, "by": "item_code",
                    "filter": {"customer": [name, "x"]}}
            sql, _ = measures.statement("revenue", "item_code", None, False, span, None,
                                        {"customer": [name, "x"]})
            self.assertEqual(self.rows(self.call(args, actor)),
                             self.rows(self.call({"sql": sql}, actor)), actor)

    def test_a_rate_is_its_numerator_over_its_denominator_from_the_raw_rows(self):
        """The share of open orders that are late and of receivables overdue (per currency),
        computed again here from the raw rows: never summed across currencies, the
        numerator and denominator beside the rate."""
        orders = self.raw("SELECT order_number, status, order_type, open_state, days_late "
                          "FROM sales_orders WHERE of_record IS NOT NULL")
        kept = {o["order_number"] for o in orders if o["status"] == "open"
                and o["order_type"] != "quote" and o["open_state"] == "live"}
        late = {o["order_number"] for o in orders if o["order_number"] in kept
                and o["days_late"] is not None and int(o["days_late"]) > 0}
        self.assertTrue(kept)
        out = self.call({"measure": "late_order_share"}, FULL)
        row = dict(zip(out["columns"], self.rows(out)[0]))
        self.assertEqual((int(row["numerator"]), int(row["denominator"])), (len(late), len(kept)))
        self.assertEqual(Decimal(str(row["value"])),
                         (Decimal(100 * len(late)) / len(kept)).quantize(Decimal("0.01")))
        docs = self.raw("SELECT amount_due, amount_due_currency, days_overdue "
                        "FROM customer_balances")
        want = {}
        for d in docs:
            if d["amount_due"] is None:
                continue
            num, den = want.get(d["amount_due_currency"], (Decimal(0), Decimal(0)))
            amt = Decimal(str(d["amount_due"]))
            over = d["days_overdue"] is not None and int(d["days_overdue"]) > 0
            want[d["amount_due_currency"]] = (num + (amt if over else 0), den + amt)
        out = self.call({"measure": "receivables_overdue_share"}, FULL)
        got = {r["amount_due_currency"]: r for r in (dict(zip(out["columns"], x))
                                                     for x in self.rows(out))}
        self.assertEqual(set(got), set(want))
        for cur, (num, den) in want.items():
            self.assertEqual(Decimal(str(got[cur]["numerator"])), num, cur)
            self.assertEqual(Decimal(str(got[cur]["denominator"])), den, cur)
            if den:
                self.assertEqual(Decimal(str(got[cur]["value"])),
                                 (100 * num / den).quantize(Decimal("0.01")), cur)

    def test_unusual_is_the_raw_periods_arithmetic_and_the_scan_agrees(self):
        """The latest period against the earlier ones from the raw rows (each invoice's age
        read once, bucketed, empty periods inside the source's reach 0); the scan of every
        measure gives the same figures for this one."""
        import statistics
        days = 400
        ages = self.raw("SELECT handle, extract(epoch FROM now() - invoice_date) AS s "
                        "FROM sales_invoices WHERE NOT pilot AND invoice_date <= now() AND "
                        f"invoice_date > now() - interval '{days * 13} days'")
        buckets: dict = {}
        for a in ages:
            buckets.setdefault(int(float(a["s"]) // (days * 86400)), set()).add(a["handle"])
        self.assertTrue(buckets)
        reach = max(buckets)
        values = [len(buckets.get(i, ())) for i in range(reach + 1)]
        out = self.call({"measure": "invoices_issued", "unusual": True, "days": days}, FULL)
        row = dict(zip(out["columns"], self.rows(out)[0]))
        self.assertEqual(int(row["current"]), values[0])
        self.assertEqual(int(row["periods_before"]), reach)
        if reach >= 1:
            self.assertEqual(Decimal(str(row["usual"])),
                             Decimal(statistics.mean(values[1:])).quantize(Decimal("0.01")))
        if reach >= 2:
            self.assertAlmostEqual(float(row["spread"]), statistics.stdev(values[1:]), delta=0.01)
        scan = self.call({"measure": measures.SCAN, "unusual": True, "days": days}, FULL)
        mine = [dict(zip(scan["columns"], r)) for r in self.rows(scan)
                if r[0] == "invoices_issued"]
        self.assertEqual(len(mine), 1)
        for k in ("current", "usual", "spread", "periods_before", "unusual"):
            self.assertEqual(mine[0][k], row[k], k)
        # a view this store does not serve (the fixture builds no comms_findings) leaves
        # its measures out, named, never the whole scan
        self.assertIn("comms_findings", scan["measure"]["not_read"])
        self.assertIn("complaints_count", scan["measure"]["not_read"])
        sql = scan["measure"]["sql"]
        self.assertNotIn("comms_findings", sql)
        for actor in (OPS, NOBODY):   # each reader: what the same statement gives them
            self.assertEqual(self.rows(self.call({"measure": measures.SCAN, "unusual": True,
                                                  "days": days}, actor)),
                             self.rows(self.call({"sql": sql}, actor)), actor)

    def test_a_forecast_carries_its_history_rows_unchanged_and_a_reader_with_nothing_none(self):
        out = self.call({"measure": "invoices_issued", "forecast": 2}, FULL)
        self.assertNotIn("error", out, out)
        self.assertEqual(out["columns"], ["month", "value", "kind", "low_80", "high_80"])
        plan = out["measure"]["forecast"]
        hist = self.call({"measure": "invoices_issued", "by": "month",
                          "period": f"{plan['history_from']}..{plan['history_to']}"}, FULL)
        want = [(str(r[0])[:10], r[1]) for r in self.rows(hist)]
        got = [(str(r[0])[:10], r[1]) for r in self.rows(out) if r[2] == "actual"]
        self.assertEqual(got, want)
        for s in plan["series"]:
            self.assertTrue("not_forecast" in s or "holdout" in s, s)
        empty = self.call({"measure": "invoices_issued", "forecast": 2}, NOBODY)
        self.assertTrue(all(r[1] in (None, "0", 0) for r in self.rows(empty)), self.rows(empty))
        self.assertFalse([r for r in self.rows(empty) if r[2] == "forecast"])

    def test_every_dimension_at_once_is_each_one_dimension_breakdown(self):
        """by "*": for each dimension the same largest parts as that dimension's own
        breakdown and an "All others" row, so each dimension's rows add up to the whole
        change per currency (one call where a why answer made one per dimension)."""
        days = self.covered_days("revenue", FULL)
        whole = self.call({"measure": "revenue", "compare": True, "days": days}, FULL)
        total = {dict(zip(whole["columns"], r))["revenue_currency"]:
                 dict(zip(whole["columns"], r))["change"] for r in self.rows(whole)}
        out = self.call({"measure": "revenue", "by": "*", "compare": True, "days": days}, FULL)
        rows = [dict(zip(out["columns"], r)) for r in self.rows(out)]
        dims = measures.measures()["revenue"]["dimensions"]
        self.assertEqual({r["dimension"] for r in rows}, set(dims))
        for dim in dims:
            for cur, want in total.items():
                mine = [r for r in rows if r["dimension"] == dim and r["revenue_currency"] == cur]
                if want is None:
                    continue
                self.assertEqual(sum(Decimal(str(r["change"])) for r in mine),
                                 Decimal(str(want)), (dim, cur))
            one = self.call({"measure": "revenue", "by": dim, "compare": True, "days": days},
                            FULL)
            parts = {(str(r[0]), r[1]): r for r in self.rows(one)
                     if not str(r[0]).startswith("All others")}
            for r in rows:
                if r["dimension"] == dim and not str(r["part"]).startswith("All others"):
                    got = parts[(str(r["part"]), r["revenue_currency"])]
                    self.assertEqual(dict(zip(one["columns"], got))["change"], r["change"])
        for actor in (OPS, NOBODY):
            sql, _ = measures.statement("revenue", "*", days, True)
            self.assertEqual(self.rows(self.call({"measure": "revenue", "by": "*",
                                                  "compare": True, "days": days}, actor)),
                             self.rows(self.call({"sql": sql}, actor)), actor)


if __name__ == "__main__":
    unittest.main()


class StatedExclusions(unittest.TestCase):
    """S2 (Ask plan; the frozen registry shape, tested on a fixture until MODEL's R1 lands
    in measures.json): a parameter's rows are left out unless switched on, its excluded
    rows are read by the same statement, and every reader of the definition (where_sql)
    sees the same rows; a parameter the entry does not hold is refused."""

    def setUp(self):
        from unittest import mock
        from operating import measures
        real = dict(measures.measures())
        late = dict(real["late_purchase_orders"])
        late["where"] = "verdict = 'late'"
        late["parameters"] = [{"id": "bc_only", "default": False,
                               "excludes_where": "coalesce(source_systems, '') = 'bc'",
                               "says": "held only in Business Central"}]
        real["late_purchase_orders"] = late
        patcher = mock.patch.object(measures, "measures", return_value=real)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.late = late

    def test_off_on_and_excluded(self):
        from operating import measures
        off, entry = measures.statement("late_purchase_orders")
        self.assertIn("NOT (coalesce(source_systems, '') = 'bc')", off)
        on, _ = measures.statement("late_purchase_orders", parameters={"bc_only": True})
        self.assertNotIn("source_systems", on)
        only, _ = measures.statement("late_purchase_orders", excluded="bc_only")
        self.assertIn("(coalesce(source_systems, '') = 'bc')", only)
        self.assertNotIn("NOT (coalesce", only)
        self.assertEqual(measures.where_sql("late_purchase_orders"),
                         "((verdict = 'late') AND NOT (coalesce(source_systems, '') = 'bc'))")
        # the version is the entry's, whatever a read switches on
        self.assertEqual(entry["version"], measures.version(self.late))
        for bad in ({"nope": True}, {"bc_only": "yes"}, ["bc_only"]):
            with self.assertRaises(Exception):
                measures.statement("late_purchase_orders", parameters=bad)

    def test_a_measure_without_parameters_reads_as_before(self):
        from operating import measures
        for mid, m in measures.typed().items():
            if m.get("parameters"):
                continue
            self.assertEqual(measures.effective_where(m), f"({m['where']})" if m["where"] else "")



class PeriodLanguage(MeasureDoor):
    """Period phrasings read as dates through the door give what the same days written out
    as fixed dates, in hand-written form, give (ASK-GENIE 17.4 G1, 20 #6)."""

    def dated(self):
        return [m for m in measures.typed().values() if m["date"] and not measures.is_rate(m)
                and m["dimensions"]][:3]

    def read(self, name, **args):
        out = self.call({"measure": name, **args}, FULL)
        if out.get("error", {}).get("code") in ("unknown_view", "not_ready"):
            self.skipTest(f"{name} not in the fixture")
        return out["columns"], self.rows(out)

    @staticmethod
    def rng(first, after):
        return f"{first.isoformat()}..{(after - dt.timedelta(days=1)).isoformat()}"

    def test_phrasings_equal_the_same_days_written_as_dates(self):
        today = measures.business_today()
        y = today.year
        q_first = dt.date(y, 3 * ((today.month - 1) // 3) + 1, 1)
        lq_first = measures._months_on(q_first, -3)
        m_first = dt.date(today.year, today.month, 1)
        cases = {
            "last_quarter": self.rng(lq_first, q_first),
            f"since:{y}-06": self.rng(dt.date(y, 6, 1), today + dt.timedelta(days=1)),
            "last_3_months": self.rng(measures._months_on(m_first, -3), m_first),
            "last_2_years": self.rng(dt.date(y - 2, 1, 1), dt.date(y, 1, 1)),
            f"FY{y}-Q3": f"{y}-Q3", f"FY{y}": f"{y}",
            "year_to_date": self.rng(dt.date(y, 1, 1), today + dt.timedelta(days=1)),
            f"since:{y}-06-15": self.rng(dt.date(y, 6, 15), today + dt.timedelta(days=1)),
        }
        seen = 0
        for m in self.dated():
            for phrase, fixed in cases.items():
                for by in (None, m["dimensions"][0], "month"):
                    args = {"by": by} if by else {}
                    a = self.read(m["id"], period=phrase, **args)
                    b = self.read(m["id"], period=fixed, **args)
                    self.assertEqual(a[1], b[1], (m["id"], phrase, by))
                    seen += len(a[1])
        self.assertGreater(seen, 0)          # the fixture holds rows in these windows

    def test_a_running_period_is_set_against_the_same_days_of_the_earlier_one(self):
        """This year (not over) against last year: last year is cut to the same days, the
        whole of last year is beside it as previous_full, and the cut figures equal the
        measure over those exact days written out."""
        today = measures.business_today()
        gone = (today - dt.date(today.year, 1, 1)).days + 1
        last = dt.date(today.year - 1, 1, 1)
        for m in self.dated():
            sql, cited = measures.statement(m["id"], compare=True, period="this_year",
                                            against=measures.SAME_LAST_YEAR)
            self.assertIn("previous_full", sql)
            self.assertIn("cut to the same", cited["period"])
            cols, rows = self.read(m["id"], compare=True, period="this_year",
                                   against=measures.SAME_LAST_YEAR)
            hand_cols, hand = self.read(
                m["id"], compare=True, period=f"{today.year}-01-01..{today.isoformat()}",
                against=self.rng(last, last + dt.timedelta(days=gone)))
            self.assertIn("previous_full", cols)
            at, hat = ({c: i for i, c in enumerate(x)} for x in (cols, hand_cols))
            self.assertEqual(len(rows), len(hand))
            for r, h in zip(rows, hand):
                for c in ("current", "previous", "current_rows", "previous_rows"):
                    self.assertEqual(r[at[c]], h[hat[c]], (m["id"], c))
            # previous_full is the measure over the whole earlier year, read plainly
            if not m["per"] and rows:
                pcols, plain = self.read(m["id"], period=str(today.year - 1))
                self.assertEqual(Decimal(str(rows[0][at["previous_full"]] or 0)),
                                 Decimal(str(plain[0][pcols.index("value")] or 0)), m["id"])

    def test_month_by_month_against_the_earlier_year_is_each_months_own_pair(self):
        """by month with compare: one row per month, the current month beside the same month
        a year earlier, each value equal to a plain read of that month written out."""
        today = measures.business_today()
        for m in self.dated():
            if m["per"]:
                continue            # one currency per read: the pairs are checked unit-free
            cols, rows = self.read(m["id"], by="month", compare=True, period=str(today.year - 1),
                                   against=str(today.year - 2))
            c = {n: i for i, n in enumerate(cols)}
            self.assertEqual(cols[:2], ["month", "previous_month"])
            for r in rows:
                cur_month, prev_month = r[c["month"]], r[c["previous_month"]]
                self.assertEqual(int(prev_month[:4]) + 1, int(cur_month[:4]))
                self.assertEqual(prev_month[5:], cur_month[5:])
                for label, col in ((cur_month, "current"), (prev_month, "previous")):
                    pcols, plain = self.read(m["id"], period=label)
                    want = Decimal(str(plain[0][pcols.index("value")] or 0)) if plain else None
                    got = r[c[col]]
                    if got is not None and want is not None:
                        self.assertEqual(Decimal(str(got)), want, (m["id"], label))

    def test_two_breakdowns_at_once_are_the_cells_of_the_one_breakdown(self):
        """by [dimension, month]: each cell is that dimension's part in that month; summed
        over months the cells equal the one-dimension read, and no cell is doubled."""
        for m in self.dated():
            if m["per"]:
                continue
            dim = m["dimensions"][0]
            cols, rows = self.read(m["id"], by=[dim, "month"], days=400)
            self.assertEqual(cols[:2], [dim, "month"])
            self.assertEqual(len({(r[0], r[1]) for r in rows}), len(rows))
            vi = cols.index("value")
            one_cols, one = self.read(m["id"], by=dim, days=400)
            want = {r[0]: Decimal(str(r[one_cols.index("value")] or 0)) for r in one}
            got: dict = {}
            for r in rows:
                got[r[0]] = got.get(r[0], Decimal(0)) + Decimal(str(r[vi] or 0))
            self.assertEqual(got, want, m["id"])


    def test_why_ranks_the_dimensions_from_the_comparisons_own_numbers(self):
        """why: per dimension, its parts, the fewest that make up half its movement, its
        largest part and that part's share of the whole change: each figure equals what the
        measure's own one-dimension comparison gives (the same rows read two ways)."""
        seen = 0
        for m in measures.typed().values():
            if not m["date"] or m["per"] or measures.is_rate(m) or not m["dimensions"]:
                continue
            try:
                d = self.covered_days(m["id"], FULL)
                cols, rows = self.read(m["id"], why=True, days=d)
            except (ValueError, AssertionError, unittest.SkipTest):
                continue                # a view this fixture holds no dated rows of
            at = {c: i for i, c in enumerate(cols)}
            for r in rows:
                dim = r[at["dimension"]]
                pcols, parts = self.read(m["id"], by=dim, compare=True, days=d)
                pi = {c: i for i, c in enumerate(pcols)}
                changes = [(abs(Decimal(str(p[pi["change"]]))), Decimal(str(p[pi["change"]])), p[0])
                           for p in parts if p[pi["change"]] is not None]
                self.assertEqual(int(r[at["parts"]]), len(parts), (m["id"], dim))
                whole = sum(c for _, c, _ in changes)
                self.assertEqual(Decimal(str(r[at["whole_change"]] or 0)), whole, (m["id"], dim))
                if changes:
                    moved = sum(a for a, _, _ in changes)
                    run, need = Decimal(0), 0
                    for a, _, _ in sorted(changes, key=lambda x: (-x[0], str(x[2]))):
                        run += a
                        need += 1
                        if run * 2 >= moved:
                            break
                    self.assertEqual(int(r[at["parts_for_half"]]), need, (m["id"], dim))
                    self.assertEqual(Decimal(str(r[at["top_change"]])),
                                     max(changes, key=lambda x: x[0])[1], (m["id"], dim))
                seen += 1
            ranks = [Decimal(str(r[at["concentration_pct"]])) for r in rows
                     if r[at["concentration_pct"]] is not None]
            self.assertEqual(ranks, sorted(ranks))      # the most concentrated dimension first
        self.assertGreater(seen, 0)
        for bad in ({"why": True, "by": "customer"}, {"why": "yes"}, {"why": True, "unusual": True}):
            with self.assertRaises(ToolError):
                measures.statement("revenue", **bad)


    def test_why_takes_a_filter_that_narrows_the_rows_it_ranks(self):
        """"why did purchases from DSM drop": the filter narrows the rows every dimension is
        ranked over (the statement carries it); a bad filter is still refused."""
        sql, _ = measures.statement("revenue", why=True, period="2026-10",
                                    filter={"customer": "Acme"})
        self.assertIn("lower(btrim(customer::text)) IN (lower('Acme'))", sql)
        with self.assertRaises(ToolError):
            measures.statement("revenue", why=True, filter={"nope": "x"})


for _name in dir(MeasureDoor):          # only this class's own tests run here
    if _name.startswith("test_") and _name not in PeriodLanguage.__dict__:
        setattr(PeriodLanguage, _name, None)


class GapList(unittest.TestCase):
    """What nothing covers is answered (never refused), said, and kept on the gap list."""

    def test_an_uncovered_measure_or_view_is_said_and_counted_each_time_asked(self):
        import os
        import tempfile
        from operating import gap_list
        with tempfile.TemporaryDirectory() as d:
            os.environ[gap_list.ENV] = os.path.join(d, "deep", "gaps.jsonl")
            try:
                for n in (1, 2, 3):
                    try:
                        measures.statement("supplier_lead_time_days")
                    except ToolError as exc:
                        self.assertEqual(exc.gap, ("measure", "supplier_lead_time_days"))
                        error = {"gap": gap_list.said(*exc.gap, gap_list.record(*exc.gap))}
                    self.assertIn(f"asked {n} time", error["gap"])
                gap_list.record("view", "receipts")
                top = gap_list.counts()
                self.assertEqual([(g["kind"], g["name"], g["asked"]) for g in top],
                                 [("measure", "supplier_lead_time_days", 3), ("view", "receipts", 1)])
                # a list that cannot be written is said in the error, not dropped
                os.environ[gap_list.ENV] = "/proc/nope/gaps.jsonl"
                self.assertIn("could not be put on the gap list",
                              gap_list.said("view", "x", gap_list.record("view", "x")))
                # a measure the catalogue holds is no gap
                try:
                    measures.statement("revenue")
                except ToolError as exc:
                    self.fail(exc)
            finally:
                os.environ.pop(gap_list.ENV, None)


class GapListAtTheDoor(MeasureDoor):
    def test_through_plx_sql_the_error_carries_the_gap_line_and_the_read_is_answered(self):
        import os
        import tempfile
        from operating import gap_list
        with tempfile.TemporaryDirectory() as d:
            os.environ[gap_list.ENV] = os.path.join(d, "gaps.jsonl")
            try:
                for args, kind in (({"measure": "ledger_spend"}, "measure"),
                                   ({"sql": "SELECT 1 FROM missing_chain_view"}, "view"),
                                   ({"describe": "nothing_here"}, "view")):
                    out = self.call(args, FULL)
                    self.assertIn("gap", out["error"], args)
                    self.assertIn("gap list", out["error"]["gap"])
                self.assertEqual(sum(g["asked"] for g in gap_list.counts()), 3)
                ok = self.call({"measure": "revenue"}, FULL)
                self.assertNotIn("gap", ok.get("error", {}))
            finally:
                os.environ.pop(gap_list.ENV, None)


for _name in dir(MeasureDoor):          # only this class's own tests run here
    if _name.startswith("test_") and _name not in GapListAtTheDoor.__dict__:
        setattr(GapListAtTheDoor, _name, None)


class MeasureAccuracyA8(unittest.TestCase):
    """PLAN-P2-A8: handle counts (never raw order/PO numbers), gross vs net AR as two
    stated measures, explicit denominators on ratio measures, and operational measures
    (awaiting_reply_by_person, open_alerts_by_person, po_count_and_value_by_supplier)."""

    def test_no_measure_counts_raw_order_or_po_numbers(self):
        bad_agg = re.compile(r"count\(\s*DISTINCT\s+(order_number|po_number|invoice_number)\b", re.I)
        for mid, m in measures.base_measures().items():
            for field in ("value", "rate_value", "sql"):
                text = str(m.get(field) or "")
                self.assertIsNone(bad_agg.search(text), (mid, field, text))

    def test_gross_and_net_ar_measures_are_both_clearly_stated(self):
        all_m = measures.base_measures()
        self.assertIn("receivables_open", all_m)
        self.assertIn("receivables_open_gross", all_m)
        net_sql, net_cite = measures.statement("receivables_open", unchecked=True)
        gross_sql, gross_cite = measures.statement("receivables_open_gross", unchecked=True)
        sqlt.check(net_sql, allowed_schema="")
        sqlt.check(gross_sql, allowed_schema="")
        self.assertIn("document_type = 'invoice'", gross_sql)
        self.assertNotIn("document_type = 'invoice'", net_sql)
        self.assertIn("net", net_cite["definition"].lower())
        self.assertIn("gross", gross_cite["definition"].lower())
        self.assertEqual(measures.version(all_m["receivables_open"]), all_m["receivables_open"]["version"])
        self.assertEqual(measures.version(all_m["receivables_open_gross"]), all_m["receivables_open_gross"]["version"])

    def test_ratio_measures_state_denominator_in_catalogue_listing_and_citation(self):
        listed = {e["id"]: e for e in measures.listing()["measures"]}
        prompt_text = "\n".join(measures.lines())
        ratios = [m for m in measures.base_measures().values() if m["unit"] == "percent"]
        self.assertGreaterEqual(len(ratios), 6)
        for m in ratios:
            mid = m["id"]
            self.assertTrue(m.get("denominator"), f"{mid} missing denominator")
            self.assertIn("denominator", m["definition"].lower(), mid)
            self.assertEqual(listed[mid].get("denominator"), m["denominator"], mid)
            self.assertIn(m["denominator"], prompt_text, mid)
            _sql, cite = measures.statement(mid, unchecked=True)
            self.assertEqual(cite.get("denominator"), m["denominator"], mid)
            self.assertEqual(measures.version(m), m["version"], mid)

    def test_operational_sql_measures_validate_and_pass_sql_check(self):
        all_m = measures.base_measures()
        for mid in ("awaiting_reply_by_person", "open_alerts_by_person", "po_count_and_value_by_supplier"):
            self.assertIn(mid, all_m)
            m = all_m[mid]
            sql, cite = measures.statement(mid)
            sqlt.check(sql, allowed_schema="")
            self.assertNotIn("biz.", sql.lower())
            self.assertEqual(cite["id"], mid)
            self.assertEqual(measures.version(m), m["version"], mid)
            self.assertTrue(measures.defined_on(m), mid)
            with self.assertRaises(ToolError):
                measures.statement(mid, by="supplier")
        po_sql, _ = measures.statement("po_count_and_value_by_supplier")
        self.assertIn("count(DISTINCT handle)", po_sql)
        self.assertNotIn("po_number", po_sql)
