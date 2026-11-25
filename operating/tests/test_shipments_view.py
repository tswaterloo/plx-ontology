"""Tests for typed biz.shipments view (PLAN-P2-A5 / PLAN-A18) and Weekly Review
door-guarded shipment on-time reporting."""
from __future__ import annotations

import datetime as dt
import unittest
from decimal import Decimal

from operating import briefs as br
from operating.pg import business_views as bv
from operating.pg import business_views_code as bvc
from operating.tests import test_briefs as tb


class ShipmentsViewContract(unittest.TestCase):
    def test_shipments_view_is_registered_with_expected_columns(self):
        self.assertIn("shipments", bv.VIEWS)
        v = bv.VIEWS["shipments"]
        cols = {c.name for c in v.cols}
        for need in ("shipment_number", "ship_date", "order_number", "order_date",
                     "required_date", "required_date_note", "on_time", "days_late",
                     "customer", "customer_chain", "pilot", "order_handle", "customer_handle"):
            self.assertIn(need, cols)
        self.assertFalse(v.covers)
        self.assertIn("Shipment", v.reads)
        self.assertIn("SalesOrder", v.reads)

    def test_comms_view_fingerprints_unchanged(self):
        expected_comms = {
            "attachments": "v1:f926da071d1733af776f038d5870a",
            "emails": "v1:29f575965c75c898ee5893b1e193a",
            "meeting_transcripts": "v1:10a011481443b441c92ce605e1df3",
            "meetings": "v1:b512330f32e40b7efd61667666fed",
            "sharepoint_files": "v1:4743f442f7326b9fd1b4e7c0f953c",
            "teams_messages": "v1:2c2c0f1eadf2f53318d572c1c7a5e",
        }
        for name, fp in expected_comms.items():
            self.assertEqual(bvc.fingerprint(bv.VIEWS[name]), fp, name)


class WeeklyReviewShipmentsSql(unittest.TestCase):
    def test_on_time_reads_typed_shipments_view_in_one_sql_call(self):
        st = tb.weekly_world(2)
        st.sees = tb.OPS
        sql_calls = []

        def call(tool, args):
            if tool == "plx.sql":
                sql_calls.append(args["sql"])
                if "ship_date >= '2026-11-02'" in args["sql"]:
                    return {
                        "columns": ["ship_date", "required_date", "on_time", "handle"],
                        "rows": [
                            ["2026-11-03", "2026-11-05", True, "Shipment:sage:1"],
                            ["2026-11-04", "2026-11-05", True, "Shipment:sage:2"],
                            ["2026-11-06", "2026-11-05", False, "Shipment:sage:3"],
                            ["2026-11-07", None, None, "Shipment:sage:4"],
                        ],
                    }
                return {
                    "columns": ["ship_date", "required_date", "on_time", "handle"],
                    "rows": [
                        ["2026-10-28", "2026-10-30", True, "Shipment:sage:0"],
                    ],
                }
            return st(tool, args)

        out = br.weekly_review(call, frozenset(tb.FIN), {"status": "read", "gaps": []},
                               week=dt.date(2026, 11, 4), today=dt.date(2026, 11, 9),
                               people=tb.PEOPLE)
        self.assertEqual(len(sql_calls), 2)
        self.assertTrue(all("FROM shipments" in s for s in sql_calls))
        fig = next(f for f in tb.section(out, "figures")["figures"]
                   if f["label"] == "Shipped on time")
        # 2 of 3 judged shipments on time = 66.7%, 1 undated not counted
        self.assertEqual(fig["value"], str(Decimal("66.7")))
        self.assertEqual(fig["previous"], str(Decimal("100.0")))
        self.assertIn("2 of 3 shipments", fig["note"])
        self.assertIn("1 without a readable required date not counted", fig["note"])
        self.assertEqual([s["handle"] for s in fig["sources"]],
                         ["Shipment:sage:1", "Shipment:sage:2", "Shipment:sage:3"])

        late = next(f for f in tb.section(out, "figures")["figures"]
                    if f["label"] == "Shipped late")
        self.assertEqual(late["value"], "1")
        self.assertEqual([s["handle"] for s in late["sources"]], ["Shipment:sage:3"])

        # No plx.find("Shipment") fallback needed when plx.sql succeeded
        shipment_finds = [a for t, a in st.calls
                          if t == "plx.find" and a.get("object_type") == "Shipment"]
        self.assertEqual(shipment_finds, [])


if __name__ == "__main__":
    unittest.main()
