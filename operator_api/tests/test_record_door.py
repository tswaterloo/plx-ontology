"""A purchase order's page from the record page door (``PLX_RECORD_DOOR``) is the page the
business views give: the same cells, the same withheld marks, the same line order and scale
of every amount; a page the door cannot say whole is read the old way (fail closed).
SYNTHETIC rows only; evidence label: isolated-fixture (the live equivalence is
``tools/record_door_equivalence.py``)."""
from __future__ import annotations

import json
import os
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from operator_api import page_sql as ps

HEADER = {"po_number": "P1", "supplier": "Acme", "supplier_handle": "s1", "status": "open",
          "order_date": "2026-06-01", "requested_date": "2026-06-24", "promised_date": None,
          "verdict": "late", "days_late": 20, "total_currency": "CAD", "purchaser": "Ann",
          "line_count": 2, "source_systems": "bc,portal", "total_basis": None,
          "lines_total": Decimal("1993.30"), "lines_total_currency": "CAD"}
LINES = [{"item_code": "A-1", "description": "Soap", "ordered_quantity": Decimal("10"),
          "received_quantity": Decimal("0"), "invoiced_quantity": Decimal("0"), "uom": "EA",
          "unit_price": Decimal("19.9"), "unit_price_currency": "CAD",
          "line_total": Decimal("199.00"), "line_total_currency": "CAD", "expected_date": "2026-07-01",
          "handle": "l1"},
         {"item_code": "B-2", "description": "Oil", "ordered_quantity": Decimal("3"),
          "received_quantity": Decimal("1"), "invoiced_quantity": Decimal("0"), "uom": "KG",
          "unit_price": Decimal("5"), "unit_price_currency": "CAD", "line_total": Decimal("15.00"),
          "line_total_currency": "CAD", "expected_date": None, "handle": "l2"}]


def jtext(d: dict) -> str:
    class E(json.JSONEncoder):
        def default(self, o):
            return float(o) if False else str(o)
    return json.dumps(d, cls=E).replace('"1993.30"', "1993.30").replace('"199.00"', "199.00") \
        .replace('"15.00"', "15.00").replace('"19.9"', "19.9").replace('"10"', "10") \
        .replace('"0"', "0").replace('"3"', "3").replace('"1"', "1").replace('"5"', "5")


def items(total_visible: bool = True, line_total_visible: bool = True) -> list[dict]:
    head = {k: v for k, v in HEADER.items()}
    out = [{"section": "header", "seq": 0, "payload_text": jtext(head), "src_handle": "h1",
            "source_systems": "bc,portal", "handle": "h1#R", "withheld": []}]
    out.append({"section": "cell", "seq": 0,
                "payload_text": jtext({"total": Decimal("1993.30")}) if total_visible else None,
                "src_handle": "h1", "source_systems": "bc,portal", "handle": "h1#R#total",
                "withheld": [] if total_visible else ["payload"]})
    for n, line in enumerate(LINES):
        body = {k: v for k, v in line.items() if k not in ("handle", "line_total")}
        out.append({"section": "lines", "seq": n, "payload_text": jtext(body),
                    "src_handle": line["handle"], "source_systems": "bc,portal",
                    "handle": f"{line['handle']}#R", "withheld": []})
        visible = line_total_visible
        out.append({"section": "line_cell", "seq": n,
                    "payload_text": jtext({"line_total": line["line_total"]}) if visible else None,
                    "src_handle": line["handle"], "source_systems": "bc,portal",
                    "handle": f"{line['handle']}#R#line_total",
                    "withheld": [] if visible else ["payload"]})
    return out


FRESH = [["purchase_orders", "2026-11-23T10:20:00+00:00"],
         ["purchase_order_lines", "2026-11-23T10:21:00+00:00"],
         ["page_items:record", "2026-11-23T10:22:58+00:00"],
         ["records", "2026-11-23T10:18:55+00:00"]]


def backend_with(page_items: list[dict], *, views=None, states=None):
    """A backend whose door answers the record page and the views' reads."""
    views = views or {}

    class Worker:
        def run(self, reader, fn, timeout=None, priority=None):
            return fn(SimpleNamespace(_call=lambda tool, args: answer(args)))

    def answer(args):
        out = {}
        for r in args["reads"]:
            if r["name"] == "record_page":
                cols = ["section", "seq", "payload", "payload_text", "src_handle",
                        "source_systems", "handle", "withheld"]
                out[r["name"]] = {"columns": cols, "rows": [
                    [i["section"], i["seq"], None, i["payload_text"], i["src_handle"],
                     i["source_systems"], i["handle"], i["withheld"]] for i in page_items]}
            elif r["name"] == "views_as_of":
                out[r["name"]] = {"columns": ["view", "built_at"], "rows": FRESH if states is None else states}
            else:
                got = views[r["name"]]
                out[r["name"]] = {"columns": list(got[0]), "rows": [list(x.values()) for x in got]}
        return {"reads": out}
    return SimpleNamespace(access_worker=Worker())


class RecordDoor(unittest.TestCase):
    def old_page(self, withheld_total=False, withheld_line_total=False):
        h = {**HEADER, "handle": "h1", "total": None if withheld_total else Decimal("1993.30"),
             "withheld": ["total"] if withheld_total else []}
        lines = [{**{k: v for k, v in line.items()},
                  "line_total": None if withheld_line_total else line["line_total"],
                  "withheld": ["line_total"] if withheld_line_total else []} for line in LINES]
        views = {"purchase_order": [h], "purchase_order_lines": lines}
        with mock.patch.object(ps, "read", wraps=ps.read):
            return ps.order(backend_with([], views=views), object(), "purchase_order", "h1")

    def new_page(self, **kw):
        return ps.order_from_record(backend_with(items(**kw)), object(), "R")

    @staticmethod
    def same(a, b):
        a, b = a.model_dump(mode="json"), b.model_dump(mode="json")
        a.pop("took_ms"), b.pop("took_ms")
        a.pop("sources"), b.pop("sources")     # the door page says the items' own time
        return a == b

    def test_the_door_page_is_the_views_page_with_every_amount_at_its_scale(self):
        new = self.new_page()
        self.assertTrue(self.same(self.old_page(), new))
        text = json.dumps(new.model_dump(mode="json"))
        self.assertIn("1993.30", text)          # trailing zeros kept, as the numeric gives it
        self.assertIn("199.00", text)

    def test_a_cell_the_reader_may_not_read_is_withheld_never_shown_and_never_zero(self):
        for kw, old in (({"total_visible": False}, {"withheld_total": True}),
                        ({"line_total_visible": False}, {"withheld_line_total": True})):
            with self.subTest(kw):
                new = self.new_page(**kw)
                self.assertTrue(self.same(self.old_page(**old), new))
                self.assertIn('"hidden": true', json.dumps(new.model_dump(mode="json")))

    def test_a_page_the_door_cannot_say_whole_is_read_the_old_way(self):
        # no items; a refused header; a page cut at the read's limit
        self.assertIsNone(ps.order_from_record(backend_with([]), object(), "R"))
        broken = items()
        broken[0] = {**broken[0], "payload_text": None}
        self.assertIsNone(ps.order_from_record(backend_with(broken), object(), "R"))
        with mock.patch.object(ps, "RECORD_PAGE_LIMIT", len(items())):
            self.assertIsNone(ps.order_from_record(backend_with(items()), object(), "R"))
        self.assertIsNone(ps.order_from_record(backend_with(items()), object(), ""))

    def stale_page(self, states):
        return ps.order_from_record(backend_with(items(), states=states), object(), "R")

    def test_items_older_than_a_view_they_rest_on_are_never_served(self):
        # a failed follow-up build: the order views were rebuilt after the items
        for view, at in (("purchase_orders", "2026-11-23T10:44:00+00:00"),
                         ("purchase_order_lines", "2026-11-23T10:23:00+00:00")):
            with self.subTest(view):
                states = [[v, at if v == view else t] for v, t in FRESH]
                self.assertIsNone(self.stale_page(states))
        # an unreadable source stamp, and no stamp of the items' own: not trusted either
        self.assertIsNone(self.stale_page([[v, "garbage" if v == "purchase_orders" else t]
                                           for v, t in FRESH]))
        self.assertIsNone(self.stale_page([r for r in FRESH if r[0] != "page_items:record"]))
        self.assertIsNone(self.stale_page([]))

    def test_the_records_stamp_moving_alone_never_sends_the_page_back_to_the_views(self):
        # the generic refresh rewrites ``records`` every cycle; the items' own freshness test
        # (record_pages.stale) ignores it, so the door must too
        for later in ("2026-11-23T10:50:46+00:00", "2030-01-01T00:00:00+00:00"):
            states = [[v, later if v in ("records", "record_links") else t] for v, t in FRESH]
            self.assertIsNotNone(self.stale_page(states), later)

    def test_the_page_says_the_items_own_time_as_its_as_of(self):
        got = self.stale_page(FRESH)
        self.assertIsNotNone(got)
        self.assertIn("as of Nov 23, 5:22 AM ET", got.sources)   # 10:22:58 UTC: the items' time
        self.assertNotIn("5:20 AM", got.sources)                 # not an older view's time
        # a view with no stamp of its own (record_links keeps none) never blocks the page
        self.assertIsNotNone(self.stale_page(FRESH))

    def test_the_switch_is_off_unless_exactly_one(self):
        for value, want in (("", False), ("0", False), ("true", False), (" 1", False),
                            ("1 ", False), ("1\n", False), ("1", True)):
            with mock.patch.dict(os.environ, {ps.RECORD_SWITCH: value}):
                self.assertEqual(ps.record_door_on(), want, value)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(ps.record_door_on())


if __name__ == "__main__":
    unittest.main()
