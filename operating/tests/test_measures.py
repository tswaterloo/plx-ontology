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
               (report, {"by": "customer"}), (report, {"days": 7})]
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
                     {"measure": "nope"}, {"measure": "revenue", "by": "nope"}):
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
