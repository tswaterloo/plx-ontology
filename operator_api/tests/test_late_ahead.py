"""A late purchase order's page is read ahead whole (the warm-up): its trail, then the slow
sections in the page's order, so a first open is not cold (live 11-23: 9.6 + 8.4 + 5.3 s).
One section failing leaves the rest of that order's sections and the next order read.
SYNTHETIC records; evidence label: isolated-fixture."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from operator_api import backend_screens as bs
from operator_api import page_sql


def late(n: int) -> SimpleNamespace:
    return SimpleNamespace(object_id=f"PurchaseOrder:{n}", open_url=f"/po/{n}?h=h{n}")


class Ahead(unittest.TestCase):
    def run_ahead(self, orders, fail=None, opens=()):
        calls = []

        def section(reader, otype, oid, name):
            calls.append((oid, name))
            if fail and (oid, name) == fail:
                raise RuntimeError("cold read failed")
        me = SimpleNamespace(object_section=section, biz_page=lambda *a: None)
        cls = next(c for c in vars(bs).values() if isinstance(c, type) and "_late_trails_ahead" in vars(c))
        with mock.patch.object(page_sql, "alerts_on", return_value=True), \
                mock.patch.object(page_sql, "open_po_ids", return_value=list(opens)), \
                mock.patch.object(page_sql, "alert_late_pos",
                                  return_value=(len(orders), orders, None)):
            got = cls._late_trails_ahead(me, object())
        return got, calls

    def test_each_late_order_is_read_trail_then_the_slow_sections_in_page_order(self):
        got, calls = self.run_ahead([late(1), late(2)])
        self.assertEqual(got, 2)
        self.assertEqual(calls, [(f"PurchaseOrder:{n}", s) for n in (1, 2)
                                 for s in ("trail", "late", "people", "landed_cost")])

    def test_a_failing_section_stops_that_order_only(self):
        got, calls = self.run_ahead([late(1), late(2)], fail=("PurchaseOrder:1", "people"))
        self.assertEqual([c for c in calls if c[0].endswith(":1")],
                         [("PurchaseOrder:1", "trail"), ("PurchaseOrder:1", "late"),
                          ("PurchaseOrder:1", "people")])
        self.assertEqual(len([c for c in calls if c[0].endswith(":2")]), 4)

    def test_other_open_orders_get_the_sections_but_not_the_lateness_trail(self):
        got, calls = self.run_ahead([late(1)], opens=["PurchaseOrder:1", "PurchaseOrder:7", "abc#9"])
        # the late order whole; another open one (never the late one twice) sections only
        self.assertEqual([c for c in calls if c[0].endswith(":7")],
                         [("PurchaseOrder:7", s) for s in ("late", "people", "landed_cost")])
        self.assertEqual(len([c for c in calls if c[0].endswith(":1")]), 4)
        self.assertFalse([c for c in calls if c[0].startswith("abc")])

    def test_open_orders_the_door_cannot_list_leave_the_late_ones_read(self):
        calls = []
        me = SimpleNamespace(object_section=lambda r, t, o, n: calls.append((o, n)),
                             biz_page=lambda *a: None)
        cls = next(c for c in vars(bs).values() if isinstance(c, type) and "_late_trails_ahead" in vars(c))
        with mock.patch.object(page_sql, "alerts_on", return_value=True), \
                mock.patch.object(page_sql, "open_po_ids", side_effect=RuntimeError("busy")), \
                mock.patch.object(page_sql, "alert_late_pos", return_value=(1, [late(1)], None)):
            self.assertEqual(cls._late_trails_ahead(me, object()), 1)
        self.assertEqual(len(calls), 4)

    def test_a_bare_handle_is_not_a_record_and_nothing_is_read_for_it(self):
        got, calls = self.run_ahead([SimpleNamespace(object_id="abc#1", open_url="")])
        self.assertEqual((got, calls), (0, []))


if __name__ == "__main__":
    unittest.main()
