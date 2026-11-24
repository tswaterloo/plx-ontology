"""One definition of a BC pilot purchase order and one cost-sanity rule: every late-PO check,
the late list, the sidebar's late by supplier and the supplier lead-time tile use the same
SQL fragment (never a filter of their own), and the price alerts' cost guard is the one
the margin measures use."""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from operating import page_reads as pr
from operating.pg import alerts
from operating.po_pilot import bc_pilot_po_sql, is_bc_pilot_po

ROOT = Path(__file__).resolve().parents[2]
SYSTEMS = ["bc", "portal", "filemaker", "sage"]


def holders():
    out = [None, ""]
    for mask in range(1, 16):
        out.append(",".join(s for i, s in enumerate(SYSTEMS) if mask >> i & 1))
    return out


class PilotPurchaseOrder(unittest.TestCase):
    def test_the_rule_in_words(self):
        """Only a BC-only order is a pilot one; any other system holding it makes it count
        (PO 106001, held by BC and the Portal, is a late PO the PO view shows)."""
        for held, pilot in (("bc", True), ("bc,portal", False), ("portal", False),
                            ("sage", False), ("filemaker,portal", False),
                            ("bc,filemaker,portal", False), ("bc,sage", False), (None, False),
                            ("", False)):
            self.assertIs(is_bc_pilot_po(held), pilot, held)

    def test_the_sql_agrees_with_the_python_rule_for_every_set_of_holders(self):
        sql = bc_pilot_po_sql("p")
        self.assertEqual(sql, "(coalesce(p.source_systems, '') = 'bc')")
        for held in holders():
            self.assertIs((held or "") == "bc", is_bc_pilot_po(held), held)

    def test_every_late_po_check_and_the_lead_time_tile_use_the_one_fragment(self):
        for rule in ("po_late", "po_due", "po_undated"):
            self.assertIn(bc_pilot_po_sql("r"), alerts.RULES[rule].select, rule)
        for name in ("late_po_count", "late_po_list", "late_po_suppliers"):
            self.assertIn(bc_pilot_po_sql(), pr.QUERIES[name].sql, name)
        for name in ("alerts_late_po_count", "alerts_late_po_list", "alerts_late_po_suppliers"):
            self.assertIn(bc_pilot_po_sql("p"), pr.QUERIES[name].sql, name)
        tiles = json.loads((ROOT / "operator_api/insights_builtin.json").read_text())["insights"]
        lead = next(t for t in tiles if t["id"] == "supplier_lead_time")["sql"]
        self.assertIn(bc_pilot_po_sql("p"), lead)
        self.assertNotIn("source_systems IS DISTINCT FROM", lead)


class CostSanity(unittest.TestCase):
    def test_the_price_alerts_leave_out_the_lines_the_margin_measures_do(self):
        measures = json.loads((ROOT / "operating/measures.json").read_text())
        wrong = next(m for m in measures["measures"] if m["id"] == "cost_looks_wrong")
        times = int(re.search(r"margin < -(\d+) \* revenue", wrong["where"]).group(1))
        self.assertEqual(times, alerts.COST_WRONG_TIMES - 1)
        self.assertIn(f"l.margin < -{times} * l.revenue", alerts.COST_LOOKS_WRONG_SQL)
        # the rule's floor takes only lines passing it; the money read uses the same one
        self.assertIn("AND NOT " + alerts.COST_LOOKS_WRONG_SQL + " THEN l.revenue - l.margin",
                      alerts._LINES)
        self.assertIn(alerts.COST_LOOKS_WRONG_SQL, pr.QUERIES["alerts_impact_top"].sql)

    def test_the_price_drop_does_not_judge_an_item_booked_at_no_cost(self):
        sql = " ".join(alerts.RULES["customer_price_drop"].select.split())
        self.assertIn("HAVING count(*) FILTER (WHERE floor > 0) * 2 >= count(*)", sql)

    def test_the_tiles_state_the_rules_thresholds(self):
        tiles = {t["id"]: t["sql"] for t in json.loads(
            (ROOT / "operator_api/insights_builtin.json").read_text())["insights"]}
        self.assertIn(f"paid < cost * {alerts.COST_TOLERANCE}", tiles["customers_below_cost"])
        self.assertIn(f"- {alerts.OWN_DROP})", tiles["customers_price_dropped"])
        self.assertIn(f"margin < -{alerts.COST_WRONG_TIMES - 1} * revenue", tiles["customers_below_cost"])
        for sql in (tiles["customers_below_cost"], tiles["customers_price_dropped"]):
            self.assertIn(f"{alerts.PRICE_DAYS}", sql)


if __name__ == "__main__":
    unittest.main()
