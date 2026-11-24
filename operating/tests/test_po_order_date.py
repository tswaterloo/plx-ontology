"""A purchase order's order date in ``purchase_orders``: the earliest day any copy states (no
threshold). The Portal's migration day on a FileMaker order, or a BC posting after the Portal
order, is never earlier than the order it copies; a sentinel day is none; an order with no
stated day states none. Checked on the view's own date rule over copies."""
from __future__ import annotations

import datetime as dt
import unittest
from types import SimpleNamespace

from operating.pg import business_views_supply as sup


def copy(day):
    cell = None if day is None else SimpleNamespace(value=day, handle=f"h-{day}",
                                                    field_path="order_date", marks="[]")
    return SimpleNamespace(cell=lambda prop: cell if prop == "order_date" else None,
                           handles={}, object_id=f"o-{day}")


class Row:
    def __init__(self):
        self.values, self.cites = {}, {}

    def read(self, cell, column, value=None, *, rec=None):
        self.values[column] = None if cell is None else value
        if cell is not None:
            self.cites[column] = cell.handle


def date_of(*days):
    row = Row()
    sup._stated_order_date(row, SimpleNamespace(members=[copy(d) for d in days]))
    return row


class OrderDateTest(unittest.TestCase):
    def test_the_earliest_copy_is_the_order_date_and_is_cited(self):
        got = date_of("2026-03-28", "2018-12-06")   # Portal migration day, FileMaker's own
        self.assertEqual(got.values["order_date"], dt.date(2018, 12, 6))
        self.assertEqual(got.cites["order_date"], "h-2018-12-06")
        # a BC posting a few days after the Portal order
        self.assertEqual(date_of("2026-10-31", "2026-10-27").values["order_date"],
                         dt.date(2026, 10, 27))

    def test_one_copy_keeps_its_day_even_a_busy_one(self):
        self.assertEqual(date_of("2026-03-28").values["order_date"], dt.date(2026, 3, 28))

    def test_a_sentinel_or_blank_is_no_day(self):
        self.assertEqual(date_of("1900-01-01", "2026-05-02").values["order_date"],
                         dt.date(2026, 5, 2))
        self.assertIsNone(date_of("1900-01-01").values["order_date"])
        self.assertIsNone(date_of(None, "").values["order_date"])


if __name__ == "__main__":
    unittest.main()
