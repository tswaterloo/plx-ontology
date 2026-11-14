"""Cross-layer invariants of the landed-cost contract.

The parity goldens (``test_portal_landed_engine``) prove the Portal port number for number;
these prove the Workbench contract around it, over generated cost sets of 1, 10 and 60 inputs
in several currencies: money is conserved (the total is the sum of every input converted at
its rate, recomputed here independently with Decimal), each input is counted once, a total
is stated only over the whole set, units mean what they say, and every input keeps its
provenance in a digest that moves when any input moves. ``LandedOutcome`` is the typed reading
that combining code (a purchase order's lines) goes through; a record that breaks the rules is
refused there, never summed.
"""
from __future__ import annotations

import copy
import random
import unittest
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from operating import landed_cost as lc
from operating.landed_cost_base import LandedCostError
from operating.landed_cost_results import (
    FreightQuoteScope, LandedOutcome, LineSetCompleteness, product_agrees)

AS_OF = "2026-11-02"
RATES = {"CAD": Decimal("0.7213"), "EUR": Decimal("1.0841"), "GBP": Decimal("1.2675")}
CURRENCIES = ("USD", "CAD", "EUR", "GBP")


def generated(n: int, seed: int, *, unknown_at: int | None = None,
              drop_fx: str | None = None) -> tuple[list[dict], dict, list[dict], Decimal]:
    """``n`` cost inputs (the first is goods per EA), the quantity, the FX table and the
    total in USD that they must give, recomputed here at 50 digits."""
    rnd = random.Random(seed)
    qty = Decimal(rnd.randint(1, 50_000))
    costs, expected, used = [], Decimal(0), set()
    for i in range(n):
        cur = CURRENCIES[rnd.randrange(len(CURRENCIES))]
        category = "goods" if i == 0 else rnd.choice(("freight", "duty", "other", "goods"))
        per_unit = i == 0 or rnd.random() < 0.3
        value = Decimal(rnd.randint(1, 9_999_999)) / Decimal(10) ** rnd.randint(0, 4)
        spec = {"name": f"input_{i}", "category": category, "literal": str(value),
                "unit": f"{cur}/EA" if per_unit else cur}
        if i == unknown_at:
            spec = {"name": f"input_{i}", "category": category,
                    "unknown": "not quoted yet", "who_can_answer": "PROC"}
        else:
            amount = value * qty if per_unit else value
            with localcontext() as ctx:
                ctx.prec = 50
                expected += amount * (RATES[cur] if cur != "USD" else 1)
            if cur != "USD":
                used.add(cur)
        costs.append(spec)
    fx = [{"from": cur, "to": "USD", "rate": str(RATES[cur]), "unit": f"USD/{cur}",
           "as_of": "2026-10-30"} for cur in sorted(used) if cur != drop_fx]
    return costs, {"literal": str(qty), "unit": "EA"}, fx, expected


def compute(costs, quantity, fx):
    return lc.compute_landed_cost(costs, quantity, "USD", as_of=AS_OF, fx=fx)


class ConservationOfMoney(unittest.TestCase):
    def test_the_total_is_every_input_converted_once_at_1_10_and_60_inputs(self):
        for n in (1, 10, 60):
            for seed in range(5):
                costs, quantity, fx, expected = generated(n, seed)
                record = compute(costs, quantity, fx)
                outcome = LandedOutcome.from_record(record)
                self.assertTrue(outcome.computed, (n, seed, record["unknowns"]))
                self.assertEqual(outcome.total.value, expected, (n, seed))
                self.assertEqual(outcome.total.unit, "USD")
                self.assertEqual(outcome.per_unit.unit, "USD/EA")
                # no input lost or counted twice: one record and one step per input
                self.assertEqual([r["name"] for r in record["inputs"]],
                                 [c["name"] for c in costs])
                self.assertEqual(record["unused_fx"], [])   # every rate given was applied
                self.assertTrue(product_agrees(outcome.per_unit.value, outcome.quantity,
                                               outcome.total.value), (n, seed))

    def test_a_total_with_a_long_tail_is_read_whole_never_refused(self):
        # a live-shaped freight share carries 40+ digits (BIZ-011 review: a product taken at
        # 28 digits refused it and the estimate lost its value)
        tails = ("371.19942001000000000000000000000000000000000101", "0.333333333333333333333"
                 "33333333333333333333333333", "98765.43210987654321098765432109876543210987")
        for tail in tails:
            for qty in ("1", "7", "3000", "123457"):
                record = compute([{"name": "goods", "category": "goods", "literal": "2.5",
                                   "unit": "USD/EA"},
                                  {"name": "freight", "category": "freight", "literal": tail,
                                   "unit": "USD"}], {"literal": qty, "unit": "EA"}, [])
                outcome = LandedOutcome.from_record(record)
                with localcontext() as ctx:
                    ctx.prec = 100
                    expected = Decimal(tail) + Decimal("2.5") * Decimal(qty)
                with localcontext() as ctx:   # the evaluator keeps 50 significant digits
                    ctx.prec, ctx.rounding = 50, ROUND_HALF_EVEN
                    expected = +expected
                self.assertEqual(outcome.total.value, expected, (tail, qty))
                # a millionth of a dollar off is refused
                with localcontext() as ctx:
                    ctx.prec = 100
                    off = Decimal(record["total"]["value"]) + Decimal("0.000001")
                with self.assertRaisesRegex(LandedCostError, "total_is_not_per_unit"):
                    LandedOutcome.from_record(dict(record, total={**record["total"],
                                                                  "value": str(off)}))

    def test_the_order_of_the_inputs_moves_neither_the_total_nor_the_per_unit_cost(self):
        costs, quantity, fx, _ = generated(10, 7)
        first = LandedOutcome.from_record(compute(costs, quantity, fx))
        shuffled = list(costs)
        random.Random(1).shuffle(shuffled)
        again = LandedOutcome.from_record(compute(shuffled, quantity, fx))
        self.assertEqual((first.total, first.per_unit), (again.total, again.per_unit))


class LongSums(unittest.TestCase):
    def test_a_sum_past_the_evaluators_length_is_added_in_parts_to_the_same_total(self):
        # 60 purchase order lines (the plan reads at most 50: "line_1 + .. + line_50" is
        # 488 characters, just under the evaluator's 500) and 60 inputs (above) both pass it
        unit = lc.parse_unit("USD")
        for n in (1, 10, 50, 60, 400):
            values = {f"line_{i}": (Decimal(i) / 7, unit) for i in range(1, n + 1)}
            steps: list[str] = []
            total, got_unit = lc._sum_calc(values, steps, "purchase order landed cost")
            with localcontext() as ctx:
                ctx.prec = 50
                expected = sum((v for v, _ in values.values()), Decimal(0))
            self.assertEqual(got_unit, unit)
            self.assertLessEqual(abs(total - expected), Decimal("1e-45"), n)
            fits = len(" + ".join(values)) <= lc.MAX_SUM_EXPR
            self.assertEqual(len(steps) == 1, fits, n)
            self.assertTrue(steps[-1].startswith("purchase order landed cost: "), steps[-1])
            # every line is named in exactly one part
            if not fits:
                named = [name for step in steps if ", part " in step
                         for name in step.split(": ", 1)[1].split(" [")[0].split(" + ")]
                self.assertEqual(sorted(named), sorted(values), n)


class ComponentUniqueness(unittest.TestCase):
    def test_an_input_named_twice_is_refused_not_summed(self):
        costs, quantity, fx, _ = generated(10, 3)
        costs[5] = {**costs[5], "name": costs[2]["name"]}
        with self.assertRaisesRegex(LandedCostError, "cost_needs_unique_name"):
            compute(costs, quantity, fx)

    def test_a_rate_given_twice_is_refused(self):
        costs, quantity, fx, _ = generated(10, 3)
        self.assertTrue(fx)
        with self.assertRaisesRegex(LandedCostError, "duplicate_fx"):
            compute(costs, quantity, fx + [dict(fx[0])])


class WholeSetTotals(unittest.TestCase):
    def test_one_unknown_input_leaves_no_total_and_a_labelled_partial(self):
        for n in (1, 10, 60):
            costs, quantity, fx, expected = generated(n, 11, unknown_at=n - 1)
            record = compute(costs, quantity, fx)
            outcome = LandedOutcome.from_record(record)
            self.assertFalse(outcome.computed)
            self.assertIsNone(outcome.total)
            self.assertIsNone(outcome.per_unit)
            self.assertEqual(outcome.unknown_inputs, (f"input_{n - 1}",))
            if n == 1:
                self.assertIsNone(outcome.known_subtotal)
            else:
                self.assertEqual(outcome.known_subtotal.value, expected)
                self.assertIn("partial", record["known_subtotal"]["label"])

    def test_a_missing_rate_leaves_the_input_unknown_never_unconverted(self):
        for seed in range(20):
            costs, quantity, fx, _ = generated(10, seed)
            if not fx:
                continue
            dropped = fx[0]["from"]
            record = compute(costs, quantity, fx[1:])
            outcome = LandedOutcome.from_record(record)
            self.assertFalse(outcome.computed)
            gone = {c["name"] for c in costs if c["unit"].split("/")[0] == dropped}
            self.assertEqual(set(outcome.unknown_inputs), gone)
            return
        self.fail("no generated set needed a rate")

    def test_a_purchase_order_total_is_never_built_from_an_inconsistent_record(self):
        costs, quantity, fx, _ = generated(10, 5)
        good = compute(costs, quantity, fx)
        LandedOutcome.from_record(good)
        broken = {
            "computed_with_unknown_inputs": dict(good, unknowns=[{"input": "x"}]),
            "computed_without_total_and_per_unit": dict(good, total=None),
            "total_not_in_target_currency": dict(good, total={**good["total"], "unit": "CAD"}),
            "per_unit_not_target_per_quantity_unit":
                dict(good, per_unit={**good["per_unit"], "unit": "USD"}),
            "total_is_not_per_unit_times_quantity":
                dict(good, total={**good["total"],
                                  "value": str(Decimal(good["total"]["value"])
                                               + Decimal("0.01"))}),
            "unknown_with_a_total": dict(good, status="unknown", unknowns=[{"input": "x"}]),
            "unknown_without_an_unknown_input": dict(good, status="unknown", total=None,
                                                     per_unit=None),
            "status:pending": dict(good, status="pending"),
            "total_not_decimal_text": dict(good, total={**good["total"], "value": 2602.0}),
            "total_not_finite": dict(good, total={**good["total"], "value": "NaN"}),
            "input_named_twice": dict(good, inputs=good["inputs"] + good["inputs"][:1]),
            "input_kind:guessed": dict(good, inputs=[{**good["inputs"][0], "kind": "guessed"}]
                                       + good["inputs"][1:]),
            "citation_without_handle": dict(good, inputs=[{**good["inputs"][0],
                                                           "kind": "citation"}]
                                            + good["inputs"][1:]),
            "derived_without_rule": dict(good, inputs=[{**good["inputs"][0], "kind": "derived"}]
                                         + good["inputs"][1:]),
            "computed_without_inputs": dict(good, inputs=[]),
        }
        for why, record in broken.items():
            with self.subTest(why), self.assertRaisesRegex(LandedCostError, f"landed_record_inconsistent:{why}"):
                LandedOutcome.from_record(record)


class SemanticUnits(unittest.TestCase):
    def test_a_cost_per_another_unit_or_two_currencies_is_refused(self):
        costs, quantity, fx, _ = generated(1, 2)
        for unit in ("USD/KG", "USD*CAD", "EA"):
            with self.assertRaises((LandedCostError, lc.UnitMismatch, ValueError),
                                   msg=unit):
                compute([{**costs[0], "unit": unit}], quantity, fx)

    def test_a_currency_quantity_is_refused(self):
        costs, _, fx, _ = generated(1, 2)
        with self.assertRaises((LandedCostError, lc.UnitMismatch)):
            compute(costs, {"literal": "10", "unit": "USD"}, fx)


class Provenance(unittest.TestCase):
    def test_every_input_keeps_its_kind_value_and_unit_and_the_digest_follows_them(self):
        costs, quantity, fx, _ = generated(10, 9)
        record = compute(costs, quantity, fx)
        for spec, rec in zip(costs, record["inputs"]):
            self.assertEqual(rec["kind"], "literal")
            self.assertEqual(Decimal(rec["value"]), Decimal(spec["literal"]))
            self.assertEqual(rec["unit"], spec["unit"])
            self.assertEqual(rec["category"], spec["category"])
        self.assertEqual(compute(costs, quantity, fx)["digest"], record["digest"])
        for i in (0, 4, 9):
            moved = copy.deepcopy(costs)
            moved[i]["literal"] = str(Decimal(moved[i]["literal"]) + Decimal("0.01"))
            self.assertNotEqual(compute(moved, quantity, fx)["digest"], record["digest"], i)
        if fx:
            moved_fx = copy.deepcopy(fx)
            moved_fx[0]["rate"] = str(Decimal(moved_fx[0]["rate"]) + Decimal("0.0001"))
            self.assertNotEqual(compute(costs, quantity, moved_fx)["digest"], record["digest"])

    def test_a_cited_input_names_its_citation_and_an_unresolvable_one_is_unknown(self):
        costs, quantity, fx, _ = generated(1, 4)
        cited = [{"name": "input_0", "category": "goods", "citation": "l1:po/1",
                  "field": "row.unit_price", "unit": costs[0]["unit"]}]
        rows = {"l1:po/1": {"row": {"unit_price": "3.25"}}}

        def resolver(handle, actor):
            return rows[handle]
        record = lc.compute_landed_cost(cited, quantity, "USD", as_of=AS_OF, fx=fx,
                                        resolver=resolver)
        self.assertEqual(record["inputs"][0]["kind"], "citation")
        self.assertIn("l1:po/1", str(record["inputs"][0]))
        unresolved = lc.compute_landed_cost(cited, quantity, "USD", as_of=AS_OF, fx=fx,
                                            resolver=lambda h, a: None)
        self.assertEqual(LandedOutcome.from_record(unresolved).unknown_inputs, ("input_0",))


class TypedQuoteScopeAndReadCompleteness(unittest.TestCase):
    QUOTE = {"_source": "l1:quote/1", "amount": "4200", "currency": "USD", "basis": "shipment",
             "includes": ["origin_charges", "main_carriage", "destination_charges"], "excludes": [],
             "valid_until": "2026-12-01"}

    def test_a_quote_stands_as_freight_only_whole_valid_and_with_its_total(self):
        whole = FreightQuoteScope.from_assessment(
            lc.assess_freight_quote(self.QUOTE, as_of=AS_OF))
        self.assertTrue(whole.usable_as_total_freight)
        self.assertEqual(whole.total.value, Decimal("4200"))
        self.assertEqual(whole.open_codes, ())
        self.assertTrue(whole.duties_and_taxes_separate)
        partial = FreightQuoteScope.from_assessment(lc.assess_freight_quote(
            {**self.QUOTE, "includes": ["destination_charges"]}, as_of=AS_OF))
        self.assertFalse(partial.usable_as_total_freight)
        self.assertIsNone(partial.total)
        self.assertIn("FREIGHT_SCOPE_PARTIAL", partial.open_codes)
        expired = FreightQuoteScope.from_assessment(lc.assess_freight_quote(
            {**self.QUOTE, "valid_until": "2026-10-01"}, as_of=AS_OF))
        self.assertEqual(expired.open_codes, ("FREIGHT_QUOTE_EXPIRED",))
        per_box = lc.assess_freight_quote({**self.QUOTE, "basis": "40HQ", "amount": "2100"},
                                          as_of=AS_OF, basis_count={"count": 2,
                                                                    "_source": "l1:pack/1"})
        self.assertEqual(FreightQuoteScope.from_assessment(per_box).total.value,
                         Decimal("4200"))

    def test_an_assessment_that_contradicts_itself_is_refused(self):
        good = lc.assess_freight_quote(self.QUOTE, as_of=AS_OF)
        broken = {
            "usable_with_open_reasons": dict(good, unknowns=[{"code": "FREIGHT_QUOTE_EXPIRED"}]),
            "usable_without_a_total": dict(good, total_freight=None),
            "total_without_being_usable": dict(good, usable_as_total_freight=False),
            "total_not_positive": dict(good, total_freight={**good["total_freight"],
                                                            "value": "0"}),
            "shape": dict(good, source=None),
        }
        for why, assessed in broken.items():
            with self.subTest(why), self.assertRaisesRegex(
                    LandedCostError, f"freight_assessment_inconsistent:{why}"):
                FreightQuoteScope.from_assessment(assessed)

    def test_a_line_set_is_whole_only_read_complete_covered_and_reached(self):
        linked, ids = {"l1", "l2"}, ["l1", "l2"]
        self.assertTrue(LineSetCompleteness.of(complete=True, covered=True, linked=linked,
                                               line_ids=ids, unproven=[]).whole)
        cases = {
            "1 of 2 line(s)": dict(unproven=["l2"]),
            "truncated or cut": dict(complete=False),
            "3 line(s) of the purchase order carry a marking": dict(covered=False, withheld=3),
            "read scope is not proven": dict(covered=False),
            "no line of the purchase order was reached": dict(linked=set(), line_ids=[]),
        }
        for words, change in cases.items():
            kwargs = {"complete": True, "covered": True, "linked": linked, "line_ids": ids,
                      "unproven": [], **change}
            got = LineSetCompleteness.of(**kwargs)
            self.assertFalse(got.whole, words)
            self.assertIn(words, got.why)
        # an unread line outranks every other reason, and a withheld price is named in it
        got = LineSetCompleteness.of(complete=False, covered=False, linked=linked, line_ids=ids,
                                     unproven=["l1"], withheld=1, price_withheld="finance")
        self.assertTrue(got.why.startswith("1 of 2 line(s)") and "(finance)" in got.why)


if __name__ == "__main__":
    unittest.main()
