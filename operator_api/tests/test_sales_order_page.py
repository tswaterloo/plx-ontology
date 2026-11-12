"""A customer order's page says what the briefing claims about it, from the same
records, and says plainly what is missing."""
from __future__ import annotations

import unittest

from operator_api import backend as be
from operator_api import models as m


def value(prop, text):
    return {"property": prop, "value": text}


def fact(text):
    return m.Fact(text=text, sources=[], check="read_from_record")


class SalesOrderView(unittest.TestCase):
    def view(self, values, related, facts):
        return be.sales_order_view("OR9", values, related, facts, "CAD")

    def test_names_the_customer_and_drops_a_zero_header_total(self):
        customer = m.ObjectRef(object_id="c1", type="Customer", label="Acme Foods",
                               open_url="/customer/c1")
        summary, facts, absent = self.view(
            [value("order_date", "2026-03-04"), value("total_amount", "0.00"),
             value("currency", "CAD")], [customer],
            [fact("Total amount: 0.00 CAD"), fact("Order date: 4 Mar 2026")])
        self.assertEqual(summary, "Customer order OR9 for Acme Foods; ordered 2026-03-04.")
        self.assertEqual([f.text for f in facts], ["Order date: 4 Mar 2026"])
        self.assertEqual([a.what for a in absent], ["The order total"])
        # C-01: the value derived from its invoices is said as derived, never as its total
        summary, _facts, absent = be.sales_order_view(
            "OR9", [value("total_amount", "0.00")], [customer], [], "CAD",
            m.Money(amount="398.04", currency="CAD"), "invoices")
        self.assertEqual(summary, "Customer order OR9 for Acme Foods; value 398.04 CAD (from "
                                  "its invoices, before tax; the order header holds 0).")
        self.assertEqual(absent, [])

    def test_without_a_customer_or_total_it_says_so_and_never_invents(self):
        summary, facts, absent = self.view([], [], [])
        self.assertEqual(summary, "Customer order OR9.")
        self.assertEqual([a.what for a in absent],
                         ["The customer of this order", "The order total"])

    def test_a_real_total_is_stated_in_its_currency(self):
        summary, facts, absent = self.view([value("total_amount", "1,250.5")], [], [])
        self.assertIn("total 1250.5 CAD", summary)
        self.assertNotIn("The order total", [a.what for a in absent])

    def test_an_unreadable_total_is_not_stated(self):
        summary, _, absent = self.view([value("total_amount", "lots")], [], [])
        self.assertNotIn("total", summary)
        self.assertIn("The order total", [a.what for a in absent])


if __name__ == "__main__":
    unittest.main()
