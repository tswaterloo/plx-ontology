"""An open order is late only while it is still going (``business_views.open_state``): the
rule reads what the records state (lines owing nothing, the newest invoice) against how long
closed orders take, never a fixed age."""
import datetime as dt
import unittest
from decimal import Decimal
from types import SimpleNamespace as NS

from operating import measures, record_lists as rl
from operating.pg import business_views as bv

DAY = dt.date(2026, 11, 23)


def order(status="open", outstanding=None, ordered_on=None, last_invoice=None):
    return NS(status=status, outstanding=outstanding, ordered_on=ordered_on,
              last_invoice=last_invoice)


class OpenState(unittest.TestCase):
    def state(self, o, late, settle=121):
        return bv.open_state(o, late, DAY, settle)

    def test_a_closed_order_has_no_state_and_a_working_one_is_live(self):
        self.assertIsNone(self.state(order("closed"), None))
        self.assertEqual(self.state(order(ordered_on=DAY - dt.timedelta(30)), 5), "live")

    def test_lines_owing_nothing_are_not_late_but_unstated_lines_say_nothing(self):
        self.assertEqual(self.state(order(outstanding=Decimal(0)), 900), "nothing_outstanding")
        self.assertEqual(self.state(order(outstanding=Decimal(5)), 5), "live")
        self.assertEqual(self.state(order(outstanding=None), 5), "live")

    def test_stale_needs_both_a_long_lateness_and_no_recent_work(self):
        old = DAY - dt.timedelta(400)
        self.assertEqual(self.state(order(ordered_on=old), 300), "stale")
        self.assertEqual(self.state(order(ordered_on=old), 121), "live")   # not past the pace
        recent = order(ordered_on=old, last_invoice=DAY - dt.timedelta(10))
        self.assertEqual(self.state(recent, 300), "live")                  # still being invoiced
        self.assertEqual(self.state(order(), 300), "stale")                # nothing dated at all
        self.assertEqual(self.state(order(ordered_on=old), 300, settle=None), "live")  # no pace

    def test_the_pace_is_the_closed_orders_own_and_needs_enough_of_them(self):
        def closed(n):
            return [order("closed", ordered_on=DAY - dt.timedelta(500),
                          last_invoice=DAY - dt.timedelta(500 - i % 100)) for i in range(n)]
        self.assertIsNone(bv._settle_days(closed(bv.SETTLE_MIN_ORDERS - 1)))
        pace = bv._settle_days(closed(1000))
        self.assertGreaterEqual(pace, 97)
        self.assertLessEqual(pace, 100)
        self.assertEqual(bv._settle_days([order("open")] * 1000), None)   # open orders say nothing


def sales_row(handle, po, owner="c1", of_record=True, status="open", state="live"):
    row = bv.Row()
    row.objects = [f"obj-{handle}"]
    row.values.update(handle=handle, customer_po=po, customer_handle=owner,
                      of_record=of_record, status=status, open_state=state)
    return row


class CopiesOfOrdersOfRecord(unittest.TestCase):
    def state(self, rows, handle):
        return next(r for r in rows if r.values["handle"] == handle).values["open_state"]

    def test_a_copy_is_the_order_of_record_only_when_the_pair_is_held_once_each(self):
        rows = [sales_row("sage1", "PO-77"), sales_row("fm1", " po-77 ", of_record=False),
                sales_row("sage2", "NA"), sales_row("fm2", "NA", of_record=False),
                sales_row("fm3", "NA", of_record=False),      # a generic PO held twice
                sales_row("sage3", "PO-9"), sales_row("sage4", "PO-9"),
                sales_row("fm4", "PO-9", of_record=False),   # two orders of record: no proof
                sales_row("fm5", "PO-77", owner="c2", of_record=False),   # another customer
                sales_row("fm6", "", of_record=False)]
        bv.mark_copies(rows)
        self.assertEqual(self.state(rows, "fm1"), "copy_of_record")
        self.assertEqual(self.state(rows, "sage1"), "live")
        for h in ("fm2", "fm3", "fm4", "fm5", "fm6"):
            self.assertEqual(self.state(rows, h), "live", h)
        # the order of record's record is read into the copy's column (its permission class)
        copy = next(r for r in rows if r.values["handle"] == "fm1")
        self.assertIn("obj-sage1", copy.col_reads["open_state"])

    def test_a_closed_copy_has_no_state_to_change(self):
        rows = [sales_row("a", "P1"), sales_row("b", "P1", of_record=False, status="closed",
                                                state=None)]
        bv.mark_copies(rows)
        self.assertIsNone(self.state(rows, "b"))


class OneDefinitionOfLate(unittest.TestCase):
    def test_the_orders_measure_counts_only_live_open_orders_and_every_system(self):
        where = measures.where_sql("late_orders")
        # only live orders by default: the not-live ones are a stated parameter, counted in on request
        self.assertIn("NOT (coalesce(to_jsonb(sales_orders) ->> 'open_state', 'live') <> 'live')",
                      where)
        m = measures.measures()["late_orders"]
        self.assertEqual([p["id"] for p in measures.parameters_of(m)], ["not_live"])
        self.assertNotIn("open_state", measures.effective_where(m, {"not_live": True}))
        self.assertNotIn("AND of_record)", where)   # a copy-only order counts like any other
        self.assertIn("of_record IS NOT NULL", where)   # (names the column: no default filter)

    def test_the_purchase_order_list_counts_late_by_the_measure(self):
        sql = rl.queries()["list__purchase_orders__count"].sql
        self.assertIn(measures.where_sql("late_purchase_orders"), sql)
        choices = rl.queries()["list__purchase_orders__choices"].sql
        self.assertIn("late (not counted)", choices)


if __name__ == "__main__":
    unittest.main()
