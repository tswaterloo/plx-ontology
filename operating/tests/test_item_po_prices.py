"""``plx.aggregate`` mode ``business_documents`` view ``item_po_prices`` (AUDIT1-08, Vince
11-05: "are any of these packaging costs incorrect as per our last PO price or average PO
price ... can you tell if any of them with costs at $0 are in fact incorrect because Petra
actually bought them?"): per item, the last PO price and the quantity-weighted average
per currency and unit, a stated cost judged against them, $0 items a PO paid for, one
order's copies in two systems never counted twice, Decimal money, a restricted reader, a
code naming no item reading the same as one naming an item the reader may not read,
refused inputs, and cardinality N=1/10/60.

SYNTHETIC fixtures only (made-up codes, suppliers and prices); evidence label:
isolated-fixture.
"""

from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from operating import business_documents as bd
from operating.tests.test_business_documents import AS_OF, EVERY, Fixture, Guard
from tools.timing_policy import budget

PRICES = '["purchasing_prices"]'


class World(Fixture):
    def bought(self, item: str, price: str, qty: str, day: str, *, currency: str = "CAD",
               uom: str | None = "ea", audience: str = "api:a", po: str | None = None,
               markings: str = PRICES) -> tuple[str, str]:
        if po is None:
            po = self.w.obj("PurchaseOrder", {"po_number": f"PO{self.w.n}", "order_date": day,
                                              "currency": currency}, audience=audience)
        values = {"unit_price": price, "ordered_quantity": (qty, "EA"),
                  "received_quantity": (qty, "EA")}
        if uom:
            values["uom"] = uom
        line = self.w.obj("PurchaseOrderLine", values, audience=audience, markings=markings)
        self.w.link("PurchaseOrderLine.line_of", line, po)
        self.w.link("PurchaseOrderLine.for_item", line, item)
        return po, line


class ItemPoPrices(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir, self.n = Path(tmp.name), 0

    def build(self, f: World):
        self.n += 1
        conn = f.build(self.dir / f"s{self.n}.sqlite")
        self.addCleanup(conn.close)
        return conn

    def ask(self, conn, items, *, currency="CAD", may_read=lambda k: True, held=EVERY):
        out = bd.answer(conn, view="item_po_prices", may_read=may_read, held=held,
                        guard=Guard(), as_of=AS_OF,
                        args={"items": items, **({"currency": currency} if currency else {})})
        self.assertNotIn("error", bd._checked(out))
        return out

    def world(self):
        f = World()
        bottle, cap, label, pump = (f.item(c) for c in ("P-0101", "P-0102", "P-0103", "P-0104"))
        f.bought(bottle, "0.50", "1000", "2026-03-01")
        f.bought(bottle, "0.56", "3000", "2026-09-01")   # last; average 0.545
        f.bought(cap, "0.10", "500", "2026-05-01")
        # label: customer-supplied, never bought (no PO line); pump: bought in USD only
        f.bought(pump, "1.20", "100", "2026-06-01", currency="USD")
        return f, {"bottle": bottle, "cap": cap, "label": label, "pump": pump}

    def test_vinces_sheet_zero_costs_bought_and_costs_off_the_po_price(self) -> None:
        f, ids = self.world()
        conn = self.build(f)
        out = self.ask(conn, [{"code": "P-0101", "cost": "0.52"}, {"code": "P-0102", "cost": "0"},
                              {"code": "P-0103", "cost": "0"}, {"code": "P-0104", "cost": "1.20"},
                              {"code": "P-0101", "cost": "0.40"}])
        rows = out["rows"]
        self.assertEqual([r["verdict"] for r in rows],
                         ["matches_average", "zero_but_bought", "zero_never_bought",
                          "currency_differs", "differs"])
        bottle = rows[0]
        self.assertEqual((bottle["last"]["price"], bottle["last"]["dated"],
                          bottle["last"]["currency"]), ("0.56", "2026-09-01", "CAD"))
        self.assertEqual(bottle["average"], [{"currency": "CAD", "uom": "ea", "lines": 2,
                                              "quantity": "4000", "price": "0.5450"}])
        self.assertEqual((bottle["po_lines"], bottle["priced_lines"], bottle["received_lines"]),
                         (2, 2, 2))
        self.assertEqual(rows[1]["last"]["price"], "0.10")   # the $0 cost a PO paid for
        self.assertIsNone(rows[2]["last"])
        self.assertEqual(rows[2]["cites"], [{"object_id": ids["label"]}])
        self.assertIn({"object_id": bottle["last"]["po"]}, bottle["cites"])
        self.assertEqual(bottle["markings"], ["purchasing_prices"])

    def test_one_orders_copies_in_two_systems_are_never_averaged_twice(self) -> None:
        f, ids = self.world()
        po, _ = f.bought(ids["cap"], "0.20", "500", "2026-10-01")
        twin = f.w.obj("PurchaseOrder", {"po_number": "BC-1", "order_date": "2026-10-01",
                                         "currency": "CAD"})
        f.w.link("PurchaseOrder.same_record_as", po, twin)
        f.bought(ids["cap"], "0.20", "500", "2026-10-01", po=twin)
        conn = self.build(f)
        (row,) = self.ask(conn, [{"code": "P-0102"}])["rows"]
        self.assertEqual(row["po_lines"], 2)   # 0.10 x 500 and one copy of 0.20 x 500
        self.assertEqual(row["average"][0]["price"], "0.1500")
        self.assertEqual(row["verdict"], "no_cost_stated")

    def test_a_restricted_reader_sees_no_price_it_may_not_read(self) -> None:
        f, ids = self.world()
        f.bought(ids["cap"], "9.99", "10", "2026-11-01", audience="api:secret")
        conn = self.build(f)
        out = self.ask(conn, [{"code": "P-0102", "cost": "0"}],
                       may_read=lambda k: k != "api:secret")
        (row,) = out["rows"]
        self.assertEqual(row["last"]["price"], "0.10")
        self.assertEqual(out["withheld_counts"]["po_line_not_readable"], 1)
        # the hidden line could be the last price: shown, never judged
        self.assertEqual((row["verdict"], row["lines_withheld"]), ("lines_withheld", 1))
        # without the price marking no line is read: the $0 is never called "never bought"
        none = self.ask(conn, [{"code": "P-0102", "cost": "0"}], held=EVERY - {"purchasing_prices"})
        (blind,) = none["rows"]
        self.assertEqual((blind["verdict"], blind["last"], blind["average"]),
                         ("lines_withheld", None, []))
        self.assertNotIn("markings", blind)
        # a code naming no item reads as one naming an item the reader may not read
        hidden = self.ask(conn, [{"code": "P-0102"}, {"code": "P-9999"}],
                          may_read=lambda k: k != "api:a")
        self.assertEqual(hidden["rows"], [])
        self.assertEqual(hidden["codes_not_found"], ["P-0102", "P-9999"])
        self.assertEqual(hidden["counts"]["code_not_found"], 2)

    def test_a_price_marked_on_its_value_gives_no_verdict_oracle(self) -> None:
        """The real shape (ontology/schema/domains/purchasing.json): the PO line's version
        carries no marking, its unit_price VALUE carries purchasing_prices. A reader without
        it submitting graded costs learns nothing about the hidden price."""
        f, _ids = self.world()
        conn = self.build(f)
        conn.execute("UPDATE object_versions SET markings = '[]' WHERE object_id IN "
                     "(SELECT object_id FROM objects WHERE object_type = 'PurchaseOrderLine')")
        conn.execute("UPDATE object_values SET markings = '[]' WHERE property <> 'unit_price' "
                     "AND object_id IN (SELECT object_id FROM objects "
                     "WHERE object_type = 'PurchaseOrderLine')")
        conn.commit()
        costs = [{"code": "P-0101", "cost": f"0.{n:02d}"} for n in range(40, 70)]
        # the owner's read of the same shape still judges the costs
        full = self.ask(conn, costs)
        self.assertEqual(full["counts"]["verdict_matches_last"]
                         + full["counts"].get("verdict_matches_average", 0)
                         + full["counts"]["verdict_differs"], 30)
        blind = self.ask(conn, costs, held=EVERY - {"purchasing_prices"})
        self.assertFalse([k for k in blind["counts"] if k.startswith("verdict_")
                          and k != "verdict_lines_withheld"], blind["counts"])
        self.assertNotIn("last_price_two_on_one_day", blind["counts"])
        for row in blind["rows"]:
            self.assertEqual((row["verdict"], row["last"], row["average"], row["lines_withheld"]),
                             ("lines_withheld", None, [], 2))
            self.assertNotIn("0.5", json.dumps(row).replace(row["stated_cost"], ""))
        self.assertEqual(blind["withheld_counts"]["po_line_price_not_readable"], 60)

    def test_an_ambiguous_item_entry_names_the_item_not_none(self) -> None:
        f = World()
        f.item("P-0200")
        f.item("P-0200")
        conn = self.build(f)
        out = self.ask(conn, [{"code": "P-0200"}])
        self.assertEqual(out.get("codes_ambiguous"), ["P-0200"])
        self.assertNotIn("None", json.dumps(out.get("codes_ambiguous")))

    def test_units_and_currency_are_never_compared_across(self) -> None:
        f = World()
        tube = f.item("P-0201")
        f.bought(tube, "50.00", "10", "2026-04-01", uom="case of 100")
        conn = self.build(f)
        out = self.ask(conn, [{"code": "P-0201", "cost": "0.50"}])
        self.assertEqual([r["verdict"] for r in out["rows"]], ["units_differ"])
        self.assertEqual(self.ask(conn, [{"code": "P-0201", "cost": "0.5"}], currency=None)
                         ["rows"][0]["verdict"], "currency_not_stated")

    def test_a_line_with_no_one_order_is_never_read_as_never_bought(self) -> None:
        f = World()
        jar = f.item("P-0202")
        line = f.w.obj("PurchaseOrderLine", {"unit_price": "2.00"}, markings=PRICES)
        f.w.link("PurchaseOrderLine.for_item", line, jar)
        conn = self.build(f)
        (row,) = self.ask(conn, [{"code": "P-0202", "cost": "0"}])["rows"]
        self.assertEqual((row["verdict"], row["lines_unlinked"]), ("lines_unlinked", 1))

    def test_two_prices_on_the_newest_day_name_no_last_price(self) -> None:
        f, ids = self.world()
        f.bought(ids["cap"], "0.12", "100", "2026-10-02")
        f.bought(ids["cap"], "0.14", "100", "2026-10-02")
        conn = self.build(f)
        out = self.ask(conn, [{"code": "P-0102", "cost": "0.13"}])
        (row,) = out["rows"]
        self.assertIsNone(row["last"])
        self.assertEqual(out["counts"]["last_price_two_on_one_day"], 1)
        self.assertEqual(row["verdict"], "differs")   # judged against the average only
        self.assertEqual(row["average"][0]["price"], "0.1086")   # 76.00 / 700

    def test_a_zero_price_line_carries_its_marking(self) -> None:
        f = World()
        f.bought(f.item("P-0301"), "0", "10", "2026-04-01")
        conn = self.build(f)
        (row,) = self.ask(conn, [{"code": "P-0301", "cost": "0"}])["rows"]
        self.assertEqual((row["verdict"], row["markings"]), ("zero_never_bought",
                                                              ["purchasing_prices"]))

    def test_unknown_inputs_are_refused(self) -> None:
        f, _ = self.world()
        conn = self.build(f)

        class Base:   # the tool over this store, every reader allowed
            class store:
                pass
            class policy:
                may_read = staticmethod(lambda actor, key: True)
                held_markings = staticmethod(lambda actor: EVERY)
        Base.store.conn = conn
        tool = bd.BusinessDocumentsTool(Base, guard=lambda: Guard())
        for bad in ([], [{"code": "P-0101", "cost": "abc"}], [{"code": "P-0101", "x": 1}],
                    [{"cost": "1"}], [{"code": "P-0101", "cost": 0.5}], "P-0101",
                    [{"code": "P-0101"}] * 201, [{"code": "P-0101", "cost": True}]):
            with self.subTest(bad=bad):
                out = tool.call({"mode": "business_documents", "view": "item_po_prices",
                                 "items": bad}, None)
                self.assertEqual(out["error"]["code"], "invalid_input")
        self.assertIn("error", tool.call({"mode": "business_documents", "view": "item_po_prices",
                                          "items": [{"code": "P-0101"}], "currency": "C$D"}, None))
        self.assertIn("error", tool.call({"mode": "business_documents", "view": "formula_lines",
                                          "items": [{"code": "P-0101"}]}, None))
        ok = tool.call({"mode": "business_documents", "view": "item_po_prices",
                        "items": [{"code": "P-0101", "cost": "0.56"}], "currency": "cad"}, None)
        self.assertEqual(ok["rows"][0]["verdict"], "matches_last")

    def test_many_items_none_lost_duplicated_or_slow(self) -> None:
        import time
        for n in (1, 10, 60):
            with self.subTest(n=n):
                f = World()
                codes = [f"P-{3000 + i}" for i in range(n)]
                for i, code in enumerate(codes):
                    f.bought(f.item(code), f"{i + 1}.00", "10", "2026-02-01")
                conn = self.build(f)
                started = time.monotonic()
                out = self.ask(conn, [{"code": c, "cost": "0"} for c in codes])
                self.assertLess(time.monotonic() - started, budget(10))
                self.assertEqual([r["code"] for r in out["rows"]], codes)
                self.assertEqual([r["last"]["price"] for r in out["rows"]],
                                 [f"{i + 1}.00" for i in range(n)])
                self.assertEqual(out["counts"]["verdict_zero_but_bought"], n)


if __name__ == "__main__":
    unittest.main()
