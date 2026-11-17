"""A sales order's value (``operating.order_value``): a header total checked against the
order's own lines (CC-NY-1), Portal lines the ETL re-inserted never summed as real ones,
and a line priced by quantity x unit price net of its line discount (CC-NY-4).

Evidence label: isolated-fixture (synthetic rows, never live PLX data).
"""

from __future__ import annotations

from decimal import Decimal
import unittest

from operating import order_value as ov

APP = "portal.app_sales_order_line"
SAGE = "portal.sage_oe_order_detail"
BC = "bc.sales_order_lines"
D = Decimal


def cell(oid: str, prop: str, value: str, *, currency: str | None = "CAD",
         unit: str | None = None, tax_basis: str | None = None) -> dict:
    return {"object_id": oid, "property": prop, "value": value, "currency": currency,
            "unit": unit, "tax_basis": tax_basis, "l1_handle": f"h:{oid}:{prop}",
            "markings": '["finance"]'}


def header(total: str, subtotal: str | None = None, *, binding: str = "portal.app_sales_order",
           total_basis: str | None = None, currency: str = "CAD") -> dict:
    oid = f"SalesOrder:{binding}:1"
    out = {ov.TOTAL: [cell(oid, ov.TOTAL, total, currency=currency, tax_basis=total_basis)]}
    if subtotal is not None:
        out[ov.SUBTOTAL] = [cell(oid, ov.SUBTOTAL, subtotal, currency=currency,
                                 tax_basis="excl_tax")]
    return out


def line(n: int, total: str | None = None, *, binding: str = APP, qty: str | None = None,
         price: str | None = None, unit: str = "EA", discount: str | None = None,
         description: str | None = "Part", currency: str = "CAD") -> dict:
    oid = f"SalesOrderLine:{binding}:{n}"
    props: dict = {}
    if total is not None:
        props["line_total"] = [cell(oid, "line_total", total, currency=currency)]
    if qty is not None:
        props["ordered_quantity"] = [cell(oid, "ordered_quantity", qty, currency=None,
                                          unit=unit)]
    if price is not None:
        props["unit_price"] = [cell(oid, "unit_price", price, currency=currency, unit=unit)]
    if discount is not None:
        props[ov.LINE_DISCOUNT] = [cell(oid, ov.LINE_DISCOUNT, discount, currency=None)]
    if description is not None:
        props["description"] = [cell(oid, "description", description, currency=None)]
    return props


def judge(head: dict, lines, *, closed: bool = False) -> ov.OrderValue:
    return ov.judge(head, closed=closed, lines=lines, invoices=[],
                    currency=lambda r: r.get("currency"))


class HeaderAgainstLines(unittest.TestCase):
    def test_a_header_its_lines_contradict_is_no_value_and_both_figures_are_said(self):
        got = judge(header("117.00", "117.00"), [line(1, "1000.00"), line(2, "556.61",
                                                                          description="Other")])
        self.assertIsNone(got.amount)
        self.assertEqual(got.reason, ov.HEADER_DISAGREES)
        self.assertEqual({k: got.check[k] for k in ("status", "header", "lines", "currency",
                                                    "line_count", "header_property")},
                         {"status": ov.HEADER_DISAGREES, "header": "117.00",
                          "lines": "1556.61", "currency": "CAD", "line_count": 2,
                          "header_property": "subtotal"})
        # the cells compared are cited: the header's subtotal first, then each line's amount
        self.assertEqual([r["property"] for r in got.check_rows],
                         ["subtotal", "line_total", "line_total"])
        words = ov.check_words(got)
        self.assertIn("117.00 CAD", words)
        self.assertIn("1,556.61 CAD", words)
        self.assertTrue(words.startswith("No order value is given"))

    def test_lines_the_portal_import_repeated_are_counted_once_and_the_header_stands(self):
        """Live 11-14: 93 of 117 disagreeing Portal app orders match once exact repeats
        of a line are counted once."""
        got = judge(header("350.00", "350.00"), [line(n, "350.00") for n in range(3)])
        self.assertEqual((got.amount, got.basis, got.reason), (D("350.00"), ov.SOURCE_TOTAL, None))
        self.assertEqual((got.check["status"], got.check["lines"], got.check["repeated_lines"],
                          got.check["lines_without_repeats"]),
                         (ov.LINES_REPEATED, "1050.00", 2, "350.00"))
        words = ov.check_words(got)
        self.assertIn("2 of them repeat another line exactly", words)
        self.assertIn("350.00 CAD", words)
        # repeats that still do not match: no value, the deduplicated figure said too
        got = judge(header("200.00", "200.00"), [line(n, "350.00") for n in range(3)])
        self.assertEqual((got.amount, got.reason, got.check["lines_without_repeats"]),
                         (None, ov.HEADER_DISAGREES, "350.00"))
        self.assertIn("Counting the 2 repeated lines once gives 350.00 CAD", ov.check_words(got))

    def test_two_different_lines_with_the_same_amount_are_not_repeats(self):
        got = judge(header("700.00", "700.00"),
                    [line(1, "350.00", description="Lid"), line(2, "350.00",
                                                                description="Jar")])
        self.assertEqual((got.amount, got.check), (D("700.00"), None))

    def test_exact_repeats_outside_a_reinserted_binding_are_real_lines(self):
        # BC lines are records of their own: two identical lines are two lines
        got = judge(header("700.00", "700.00", binding="bc.sales_orders"),
                    [line(n, "350.00", binding=BC) for n in range(2)])
        self.assertEqual((got.amount, got.check), (D("700.00"), None))
        got = judge(header("0"), [line(n, "350.00", binding=BC) for n in range(2)])
        self.assertEqual((got.amount, got.basis), (D("700.00"), ov.ORDER_LINES))

    def test_rounding_per_line_agrees_and_tax_is_never_a_disagreement(self):
        lines = [line(n, "33.333") for n in range(3)]
        self.assertIsNone(judge(header("99.99", "99.99"), lines).check)
        self.assertEqual(judge(header("100.02", "100.02"), lines).reason, ov.HEADER_DISAGREES)
        # BC: the total includes tax, the subtotal is what the lines add up to
        bc = [line(1, "100.00", binding=BC)]
        got = judge(header("113.00", "100.00", binding="bc.sales_orders",
                           total_basis="incl_tax"), bc)
        self.assertEqual((got.amount, got.check), (D("113.00"), None))
        # no subtotal and a total stated with tax (or of unknown basis): not compared
        for basis in ("incl_tax", None, "unknown"):
            got = judge(header("113.00", binding="bc.sales_orders", total_basis=basis), bc)
            self.assertEqual((got.amount, got.check), (D("113.00"), None), basis)
        # a total stated before tax is compared when no subtotal is
        got = judge(header("90.00", total_basis="excl_tax"), [line(1, "100.00")])
        self.assertEqual((got.reason, got.check["header_property"]),
                         (ov.HEADER_DISAGREES, "total_amount"))

    def test_lines_not_known_in_full_priced_or_in_the_currency_leave_the_header_unchecked(self):
        head = header("117.00", "117.00")
        for lines in (None, [], [line(1)], [line(1, "0"), line(2, "0", description="x")],
                      [line(1, "500.00", currency="USD")],
                      [line(1, "500.00"), line(2, "9", currency="USD", description="x")]):
            got = judge(head, lines)
            self.assertEqual((got.amount, got.check), (D("117.00"), None), lines)

    def test_a_zero_header_never_sums_lines_the_import_repeated(self):
        got = judge(header("0"), [line(n, "350.00") for n in range(2)])
        self.assertEqual((got.amount, got.reason), (None, ov.LINES_REPEATED))
        got = judge(header("0"), [line(1, "350.00"), line(2, "350.00", description="Jar")])
        self.assertEqual((got.amount, got.basis), (D("700.00"), ov.ORDER_LINES))

    def test_cardinality_one_ten_sixty_lines_no_line_lost_or_doubled(self):
        for n in (1, 10, 60):
            distinct = [line(i, "10.005", description=f"part {i}") for i in range(n)]
            total = D("10.005") * n
            got = judge(header(str(total.quantize(D("0.01"))), str(total.quantize(D("0.01")))),
                        distinct)
            self.assertEqual((got.amount is not None, got.check), (True, None), n)
            doubled = distinct + [line(1000 + i, "10.005", description=f"part {i}")
                                  for i in range(n)]
            got = judge(header(str(total), str(total)), doubled)
            self.assertEqual((got.check["repeated_lines"], got.check["line_count"],
                              D(got.check["lines_without_repeats"])), (n, 2 * n, total), n)
            got = judge(header("0"), distinct)
            self.assertEqual(got.amount, total, n)


class LineDiscount(unittest.TestCase):
    def test_a_line_priced_by_quantity_and_price_is_net_of_its_line_discount(self):
        got = judge(header("0", binding="portal.sage_oe_order_header"),
                    [line(1, binding=SAGE, qty="10", price="20", discount="12.5",
                          description=None),
                     line(2, binding=SAGE, qty="2", price="5", discount="0", description=None)])
        self.assertEqual((got.amount, got.basis), (D("185.000"), ov.ORDER_LINES))
        self.assertIn(ov.LINE_DISCOUNT, [r["property"] for r in got.rows])

    def test_a_source_line_without_its_discount_cell_is_never_priced_gross(self):
        for binding in (SAGE, BC):
            got = judge(header("0", binding="portal.sage_oe_order_header"),
                        [line(1, binding=binding, qty="10", price="20", description=None)])
            self.assertEqual((got.amount, got.reason), (None, "line_discount_unknown"), binding)
        # a BC line's own amount (after its line discount) is read as is
        got = judge(header("0", binding="bc.sales_orders"), [line(1, "175.00", binding=BC,
                                                                  qty="10", price="20")])
        self.assertEqual(got.amount, D("175.00"))
        # a Portal line has no line discount: its quantity x price is its amount
        got = judge(header("0"), [line(1, qty="3", price="199.95")])
        self.assertEqual(got.amount, D("599.85"))

    def test_an_unreadable_or_impossible_discount_prices_nothing(self):
        for bad in ("abc", "-1", "100.5"):
            got = judge(header("0", binding="portal.sage_oe_order_header"),
                        [line(1, binding=SAGE, qty="10", price="20", discount=bad,
                              description=None)])
            self.assertEqual((got.amount, got.reason), (None, "line_unpriced"), bad)

    def test_the_reinserted_binding_names_a_relation_the_projection_collapses(self):
        import json
        from ontology.project import ETL_REINSERTED
        domains = json.loads((__import__("pathlib").Path(ov.__file__).resolve().parents[1]
                              / "ontology/schema/domains/orders.json").read_text())
        bindings = {b["id"]: b for t in domains["object_types"] for b in t["bindings"]}
        for binding in ov.REINSERTED_LINE_BINDINGS | ov.DISCOUNT_STATED_BY_SOURCE:
            self.assertIn(binding, bindings)
        for binding in ov.REINSERTED_LINE_BINDINGS:
            b = bindings[binding]
            self.assertIn(b["relation"], ETL_REINSERTED[b["source"]])


def sage_line(n: int, qty: str, price: str, discount: str, left: str | None) -> dict:
    props = line(n, binding=SAGE, qty=qty, price=price, discount=discount)
    if left is not None:
        props[ov.OUTSTANDING] = [cell(f"SalesOrderLine:{SAGE}:{n}", ov.OUTSTANDING, left,
                                      currency=None)]
    return props


class SageHeaderOutstanding(unittest.TestCase):
    """CC-NY-1: Sage's ordtotal (before tax) is the order's value until it ships in part,
    then the value still to ship; told apart by the lines' quantity still to ship."""

    def lines(self, first_left: str | None = "4", second_left: str | None = "0"):
        # ordered: 10 x 200 less 10% + 5 x 50 = 2,050; still to ship: 4 x 180 = 720
        return [sage_line(1, "10", "200", "10", first_left),
                sage_line(2, "5", "50", "0", second_left)]

    def head(self, total: str) -> dict:
        return header(total, binding="portal.sage_oe_order_header", total_basis="excl_tax")

    def test_a_header_equal_to_the_ordered_value_stands_unchecked(self):
        got = judge(self.head("2050.00"), self.lines())
        self.assertEqual((got.amount, got.basis, got.check), (D("2050.00"), ov.SOURCE_TOTAL, None))
        # nothing shipped yet: the remaining value is the ordered one, the header stands
        got = judge(self.head("2050.00"), self.lines("10", "5"))
        self.assertEqual((got.basis, got.check), (ov.SOURCE_TOTAL, None))

    def test_a_header_holding_what_is_still_to_ship_gives_the_lines_ordered_value(self):
        got = judge(self.head("720.00"), self.lines())
        self.assertEqual((got.amount, got.currency, got.basis), (D("2050.000"), "CAD",
                                                                 ov.ORDER_LINES))
        self.assertTrue(got.derived)
        self.assertEqual({k: got.check[k] for k in ("status", "header", "lines", "outstanding")},
                         {"status": ov.HEADER_OUTSTANDING, "header": "720.00",
                          "lines": "2050.0", "outstanding": "720.0"})
        # the header's own total is kept as the source copy, never the value
        self.assertEqual([r["value"] for r in got.source_rows], ["720.00"])
        cited = [r["property"] for r in got.check_rows]
        self.assertEqual(cited[0], ov.TOTAL)
        self.assertEqual(cited.count(ov.OUTSTANDING), 2)
        words = ov.check_words(got)
        self.assertTrue(words.startswith("The order's value is what its item lines ordered, "
                                         "2,050.00 CAD. The order's total before tax 720.00 "
                                         "CAD is what is still to ship"), words)
        self.assertEqual(ov.CHECK_REASONS[ov.HEADER_OUTSTANDING],
                         "order_total_is_value_still_to_ship")

    def test_a_closed_order_whose_header_is_what_was_left_has_no_value(self):
        got = judge(self.head("720.00"), self.lines(), closed=True)
        self.assertEqual((got.amount, got.reason, got.check["status"]),
                         (None, ov.HEADER_OUTSTANDING, ov.HEADER_OUTSTANDING))
        self.assertTrue(ov.check_words(got).startswith("No order value is given"))

    def test_an_unknown_remaining_quantity_or_neither_figure_is_a_disagreement(self):
        for lines in (self.lines(second_left=None), self.lines(first_left="lots"),
                      self.lines(first_left="-1")):
            got = judge(self.head("720.00"), lines)
            self.assertEqual((got.amount, got.reason), (None, ov.HEADER_DISAGREES))
        got = judge(self.head("999.00"), self.lines())
        self.assertEqual((got.amount, got.reason), (None, ov.HEADER_DISAGREES))
        self.assertNotIn("outstanding", got.check)
        # a total of unknown tax basis is never compared (the stored cell before the schema
        # states excl_tax): served as stated, as before
        got = judge(header("720.00", binding="portal.sage_oe_order_header",
                           total_basis="unknown"), self.lines())
        self.assertEqual((got.amount, got.basis, got.check), (D("720.00"), ov.SOURCE_TOTAL, None))

    def test_charge_lines_are_left_out_of_the_check_as_sages_total_leaves_them_out(self):
        def charge(n: int, kind: str | None = ov.CHARGE_LINE) -> dict:
            # a charge line serves no amount: quantity and price 0
            props = sage_line(n, "0", "0", "0", "0")
            if kind is not None:
                props[ov.LINE_KIND] = [cell(f"SalesOrderLine:{SAGE}:{n}", ov.LINE_KIND, kind,
                                            currency=None)]
            return props
        got = judge(self.head("2050.00"), [*self.lines(), charge(3)])
        self.assertEqual((got.basis, got.check), (ov.SOURCE_TOTAL, None))
        got = judge(self.head("720.00"), [*self.lines(), charge(3)])
        self.assertEqual((got.amount, got.basis, got.check["charge_lines"]),
                         (D("2050.0"), ov.ORDER_LINES, 1))
        self.assertIn("(its 1 charge line left out, as Sage's order total leaves them out)",
                      ov.check_words(got))
        got = judge(self.head("999.00"), [*self.lines(), charge(3)])
        self.assertEqual(got.reason, ov.HEADER_DISAGREES)
        # a line whose type is unreadable or an item line is never left out: unpriced, so
        # the header is not checked (served as stated, as before)
        for kind in (None, "1"):
            got = judge(self.head("720.00"), [*self.lines(), charge(3, kind)])
            self.assertEqual((got.basis, got.check), (ov.SOURCE_TOTAL, None))
        # a charge line type outside Sage's binding means nothing
        other = line(9, binding=BC, qty="0", price="0", discount="0")
        other[ov.LINE_KIND] = [cell(f"SalesOrderLine:{BC}:9", ov.LINE_KIND, "2", currency=None)]
        got = judge(self.head("720.00"), [*self.lines(), other])
        self.assertEqual((got.basis, got.check), (ov.SOURCE_TOTAL, None))

    def test_cardinality_one_ten_sixty_partly_shipped_lines(self):
        for n in (1, 10, 60):
            lines = [sage_line(i, "3", "10", "0", "1") for i in range(n)]
            got = judge(self.head(f"{10 * n}.00"), lines)
            self.assertEqual((got.amount, got.basis, got.check["line_count"]),
                             (D(30 * n), ov.ORDER_LINES, n))
            self.assertEqual(len(got.check_rows), 1 + 3 * n + n)


class ProjectedSageDiscount(unittest.TestCase):
    """A Sage order's lines projected by the schema: discper is served as
    line_discount_percent and the order valued from its lines net of it."""

    def project(self, rows: dict, read):
        """``read(store)`` over a store the schema projects from these Sage rows."""
        import hashlib
        import json
        import tempfile
        from datetime import datetime, timezone
        from pathlib import Path
        from ontology import identifiers
        from ontology.project import CapturedRecord, build_store
        from ontology.schema import load_schema
        from operating.proactive_engine import _Store

        stamp = "2026-11-14T00:00:00.000000Z"

        class Source:
            source = "portal_api"

            def describe(self):
                return {"generation": 1, "relations": {}}

            def records(self, relations):
                out = {}
                for rel in relations:
                    key, got = rows.get(rel, (None, []))
                    out[rel] = [CapturedRecord(
                        "portal_api", rel, json.dumps({key: r[key]}), r,
                        f"api://{rel}/{r[key]}@1", "row",
                        hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest(),
                        stamp, None, stamp, f"api:{rel}") for r in got]
                return out

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.sqlite"
            build_store(path, load_schema(), {"portal_api": Source()},
                        normalizer=identifiers.normalize,
                        clock=lambda: datetime(2026, 11, 14, tzinfo=timezone.utc))
            return read(_Store(path))

    @staticmethod
    def rows(ordtotal: str, shipped: str = "0") -> dict:
        left = str(10 - int(shipped))
        return {"sage.oe_order_header": ("orduniq", [
                    {"orduniq": 7, "ordnumber": "ORD0007", "ordtotal": ordtotal,
                     "orsourcurr": "CAD", "complete": "1", "type": "1"}]),
                "sage.oe_order_detail": ("_sage_pk", [
                    {"_sage_pk": "7-1", "orduniq": 7, "linenum": 1, "linetype": "1",
                     "item": "A", "origqty": "10", "qtyordered": left, "qtyshptodt": shipped,
                     "unitprice": "20", "ordunit": "EA", "discper": "12.5"},
                    {"_sage_pk": "7-2", "orduniq": 7, "linenum": 2, "linetype": "1",
                     "item": "B", "origqty": "2", "qtyordered": "2", "unitprice": "5",
                     "ordunit": "EA", "discper": "0"}])}

    def test_a_projected_sage_line_is_valued_net_of_its_discper(self):
        def read(st):
            lines = st.values(["SalesOrderLine"], (ov.LINE_DISCOUNT,))
            self.assertEqual(sorted(v["props"][ov.LINE_DISCOUNT][0]["value"]
                                    for v in lines.values()), ["0", "12.5"])
            (order,) = st.values([ov.TYPE], (ov.TOTAL,))
            return ov.for_store(st, [order])[order]
        got = self.project(self.rows("0"), read)
        # 10 x 20 less 12.5% + 2 x 5: the header's 0 says nothing
        self.assertEqual((got.amount, got.basis), (D("185.000"), ov.ORDER_LINES))
        self.assertIn(ov.LINE_DISCOUNT, [r["property"] for r in got.rows])

    def test_a_projected_sage_total_is_before_tax_and_checked_against_its_lines(self):
        """CC-NY-1: the schema states ordtotal before tax, so the order's lines check it;
        partly shipped (6 of 10 shipped), Sage's 80 is what is still to ship."""
        def read(st):
            (order,) = st.values([ov.TYPE], (ov.TOTAL,))
            got = ov.for_store(st, [order])[order]
            (basis,), = st.conn.execute("SELECT tax_basis FROM object_values WHERE "
                                        "object_id = ? AND property = ?", (order, ov.TOTAL))
            return got, basis
        got, basis = self.project(self.rows("185.00"), read)
        self.assertEqual(basis, "excl_tax")
        self.assertEqual((got.amount, got.basis, got.check), (D("185.00"), ov.SOURCE_TOTAL, None))
        # still to ship: 4 x 20 less 12.5% + 2 x 5 = 80
        got, _row = self.project(self.rows("80.00", shipped="6"), read)
        self.assertEqual((got.amount, got.basis, got.check["status"], got.check["outstanding"]),
                         (D("185.000"), ov.ORDER_LINES, ov.HEADER_OUTSTANDING, "80.000"))
        got, _row = self.project(self.rows("120.00", shipped="6"), read)
        self.assertEqual((got.amount, got.reason), (None, ov.HEADER_DISAGREES))


if __name__ == "__main__":
    unittest.main()
