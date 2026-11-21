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
    for m in measures.measures().values():
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
    for r in measures.reports():
        yield r, {}


class Catalogue(unittest.TestCase):
    def test_every_entry_names_only_its_views_columns_and_every_statement_is_checked(self):
        views = sqlt._dictionary()
        for m in measures.measures().values():
            cols = set(views[m["view"]]["columns"])
            named = set(m["dimensions"]) | {c for c in (m["per"], m["date"]) if c}
            named |= set(re.findall(r"\((?:DISTINCT )?([a-z_]+)\)", m["value"])) - {"distinct"}
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
        piloted = [m for m in measures.measures().values()
                   if "pilot" in views[m["view"]]["columns"]]
        self.assertTrue(piloted)
        # the same for orders no Sage copy holds (sales_orders.of_record = false: FileMaker
        # order entry, Portal-only, BC pilot; live 11-21 late_orders 66 for the 55 of record)
        of_record = [m for m in measures.measures().values()
                     if "of_record" in views[m["view"]]["columns"]]
        self.assertTrue(of_record)
        for name, opts in _variants():
            m = measures.measures().get(name)
            if m in piloted:
                self.assertRegex(measures.statement(name, **opts)[0], r"\bNOT pilot\b",
                                 (name, opts))
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
        snapshot = next(m["id"] for m in measures.measures().values() if not m["date"])
        dated = next(m["id"] for m in measures.measures().values() if m["date"])
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
               (dated, {"period": "2026-09", "by": "month", "compare": True}),
               (snapshot, {"period": "2026-09"}), (report, {"period": "2026-09"})]
        for name, opts in bad:
            with self.assertRaises(ToolError, msg=(name, opts)) as caught:
                measures.statement(name, **opts)
            self.assertEqual(caught.exception.code, "invalid_request")

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

    def test_a_comparison_never_reads_the_whole_source_for_its_first_record(self):
        """Through a reader's door a view's unbounded min reads every row (~20 s for mail,
        three times per comparison): coverage is an EXISTS before the earlier period, and
        the first record is read only after the start, only when it is not covered."""
        for m in measures.measures().values():
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
        for m in measures.measures().values():
            if not m["date"]:
                continue
            out = self.call({"measure": m["id"], "compare": True, "days": 3650}, FULL)
            if out.get("error", {}).get("code") == "unknown_view":
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
        pilot ones, open_orders the open orders Sage holds (of_record) that are not quotes;
        the fixture holds a pilot invoice and an order held only outside Sage, so a measure
        counting them would be larger."""
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
        orders = raw("SELECT order_number, status, order_type, of_record FROM sales_orders "
                     "WHERE of_record OR NOT of_record")
        open_kept = {r["order_number"] for r in orders if r["status"] == "open"
                     and r["order_type"] != "quote" and _true(r["of_record"])}
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

    def test_the_parts_of_a_change_add_up_to_the_whole_change_past_the_top(self):
        """Past the largest changes the rest is one "All others (n)" row per currency, so a
        waterfall of the parts ends on the whole change; break the remainder and this fails."""
        days = self.covered_days("invoices_issued", FULL)
        whole = self.call({"measure": "invoices_issued", "compare": True, "days": days}, FULL)
        total = sum(Decimal(str(dict(zip(whole["columns"], r))["change"]))
                    for r in self.rows(whole))
        old = measures.TOP
        try:
            measures.TOP = 1
            out = self.call({"measure": "invoices_issued", "by": "customer", "compare": True,
                             "days": days}, FULL)
        finally:
            measures.TOP = old
        rows = [dict(zip(out["columns"], r)) for r in self.rows(out)]
        self.assertGreater(len(rows), 1)
        self.assertEqual(sum(1 for r in rows if not str(r["customer"]).startswith("All others (")),
                         1)
        self.assertEqual(sum(Decimal(str(r["change"])) for r in rows), total)
        if total:   # the remainder carries its share too: the shares still add up to 100
            self.assertAlmostEqual(float(sum(Decimal(str(r["share_of_change_pct"]))
                                             for r in rows)), 100.0, delta=0.1 * len(rows))

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
        unread cell is marked incomplete."""
        unread = 0
        for m in measures.measures().values():
            if not m["date"] or m["value"].startswith("count("):
                continue
            for actor in (FULL, OPS):
                # the whole history (its earlier period before the source: not covered)
                # and the longest period the source covers
                for days in (3650, self.covered_days(m["id"], actor)):
                    out = self.call({"measure": m["id"], "by": m["dimensions"][0],
                                     "compare": True, "days": days}, actor)
                    for r in self.rows(out):
                        row = dict(zip(out["columns"], r))
                        blind = ((row["current"] is None and int(row["current_rows"]) > 0)
                                 or (row["previous"] is None
                                     and int(row["previous_rows"]) > 0))
                        if blind:
                            self.assertIsNone(row["change"], (m["id"], actor, row))
                            self.assertIsNone(row["share_of_change_pct"], (m["id"], row))
                        self.assertEqual(_true(row["incomplete"]),
                                         int(row["unread_cells"]) > 0
                                         or not _true(row["covered"]), (m["id"], days, row))
                        unread += int(row["unread_cells"])
        self.assertGreater(unread, 0)   # the fixture's finance marks reach a comparison


if __name__ == "__main__":
    unittest.main()
