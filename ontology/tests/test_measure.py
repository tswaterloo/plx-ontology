"""The one money/quantity value type: dimensions survive every combination, and a total
over incomplete inputs is never exact."""

from __future__ import annotations

import unittest
from decimal import Decimal

from ontology import measure as ms


def cad(raw, **kw):
    return ms.measure(raw, "amount", currency="CAD", **kw)


class TotalInvariants(unittest.TestCase):
    def test_incomplete_inputs_never_give_an_exact_total(self):
        # an unread amount, an unknown currency, an unknown unit or a short read each make
        # the total partial, at any cardinality
        for n in (1, 10, 60):
            whole = [cad("1.10") for _ in range(n)]
            self.assertEqual((ms.total(whole).state, ms.total(whole).amount),
                             ("exact", Decimal("1.10") * n))
            for broken in (cad(None), ms.measure("5", "amount", currency=None),
                           ms.measure("5", "amount", currency="$"),
                           ms.measure("3", "quantity", unit=None)):
                got = ms.total(whole + [broken])
                self.assertFalse(got.exact, broken)
                self.assertEqual(got.missing, 1)
            short = ms.total(whole, expected=n + 1)
            self.assertEqual((short.state, short.missing), ("partial", 1))

    def test_a_partial_subtotal_is_a_lower_bound_only_when_no_value_can_be_negative(self):
        got = ms.total([cad("100"), cad("-40"), cad(None)])   # a credit memo among them
        self.assertEqual((got.state, got.amount, got.at_least), ("partial", Decimal(60), False))
        self.assertFalse(ms.total([cad("100"), cad(None)]).at_least)   # not stated
        self.assertTrue(ms.total([cad("100"), cad(None)], nonnegative=True).at_least)
        self.assertFalse(ms.total([cad("100"), cad("-1"), cad(None)], nonnegative=True).at_least)

    def test_dimensions_never_combine(self):
        usd = ms.measure("5", "amount", currency="usd")
        got = ms.total([cad("10"), usd, cad("2")])
        self.assertEqual((got.state, got.amount), ("mixed", None))
        self.assertEqual({d.currency: a for d, a in got.parts.items()},
                         {"CAD": Decimal(12), "USD": Decimal(5)})
        # tax bases and quantity roles are dimensions too
        self.assertEqual(ms.total([cad("1", basis="incl_tax"), cad("1", basis="excl_tax")]).state,
                         "mixed")
        self.assertEqual(ms.total([ms.measure("1", "quantity", unit="kg", role="planned"),
                                   ms.measure("1", "quantity", unit="kg", role="filled")]).state,
                         "mixed")
        # a spelling fold is no conversion: KGS is kg, g is not
        kg = ms.total([ms.measure("1", "quantity", unit="KGS"), ms.measure("2", "quantity", unit="kg")])
        self.assertEqual((kg.state, kg.amount, kg.dim.unit), ("exact", Decimal(3), "kg"))
        self.assertEqual(ms.total([ms.measure("1", "quantity", unit="kg"),
                                   ms.measure("1", "quantity", unit="g")]).state, "mixed")
        # a unit price is never summed
        price = ms.measure("4.25", "unit_price", currency="CAD", unit="kg")
        self.assertEqual(ms.total([price, price]).state, "refused")

    def test_unknown_units_are_never_compatible_and_extremes_stay_per_dimension(self):
        none_a, none_b = (ms.measure("1", "quantity", unit=None) for _ in range(2))
        self.assertFalse(ms.comparable(none_a, none_b))
        self.assertFalse(ms.comparable(ms.measure("1", "quantity", unit="kg"), none_a))
        self.assertTrue(ms.comparable(ms.measure("1", "quantity", unit="EA"),
                                      ms.measure("9", "quantity", unit="each")))
        got = ms.extremes([cad("5"), cad("50"), ms.measure("40", "amount", currency="USD"),
                           cad(None)])
        self.assertEqual({d.currency: v for d, v in got.items()},
                         {"CAD": (Decimal(5), Decimal(50)), "USD": (Decimal(40), Decimal(40))})

    def test_amounts_are_read_exactly_and_unknown_kinds_fail_closed(self):
        self.assertEqual(ms.decimal_of(0.1), Decimal("0.1"))
        for bad in (True, float("nan"), float("inf"), "NaN", "1,000", "$5", "", None, [1]):
            self.assertIsNone(ms.decimal_of(bad), bad)
        with self.assertRaises(ValueError):
            ms.dimension("weight")
        self.assertEqual(ms.format_measure(ms.measure("-12345.50", "amount", currency="CAD")),
                         "-12,345.5 CAD")
        self.assertEqual(ms.format_measure(ms.measure("10", "quantity", unit="KGS")), "10 kg")
        self.assertEqual(ms.format_measure(ms.measure("4.25", "unit_price", currency="cad",
                                                      unit="kg")), "4.25 CAD per kg")
        self.assertEqual(ms.format_measure(ms.measure("2", "amount", currency=None)),
                         "2 unknown currency")


class MoneyText(unittest.TestCase):
    def test_an_amount_is_stated_exactly_with_its_currency_or_not_at_all(self):
        self.assertEqual(ms.money_text("1234.5", "CAD"), "1,234.50 CAD")
        self.assertEqual(ms.money_text(Decimal("0.125"), "usd"), "0.125 USD")
        self.assertEqual(ms.money_text("-1000000", "EUR"), "-1,000,000.00 EUR")
        self.assertEqual(ms.money_text(0.1 + 0.2, "CAD"), "0.30 CAD")   # float noise
        for raw, currency in (("NaN", "CAD"), ("Infinity", "CAD"), (True, "CAD"),
                              ("12", None), ("12", ""), ("12", "dollars?"), ("", "CAD")):
            with self.subTest(raw=raw, currency=currency):
                self.assertIsNone(ms.money_text(raw, currency))


if __name__ == "__main__":
    unittest.main()
