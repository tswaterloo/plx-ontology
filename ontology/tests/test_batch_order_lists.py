"""Batch.for_sales_order reads a batch's OE list as a list of lines.

public.batch.oelist_c holds one order number per line (CR-separated), most often the same
number once per component ("OR1000138953\\rOR1000138953"), sometimes several orders, and
often notes or months. The shapes below are the live ones (2026-11-12, counts of distinct
values by shape: one number 249, a number repeated 94/26/11, a number with words 22,
month text 17+13, a range "OR1000152592-55" 10). Built from hand-made capture rows with the
real compiled schema. Evidence label: isolated-fixture.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ontology import identifiers
from ontology.project import CapturedRecord, build_store
from ontology.schema import load_schema

T = "2026-10-12T00:00:00.000000Z"
ORDERS = [
    {"id": 1, "erpcode": "OR1000138953"}, {"id": 2, "erpcode": "OR1000145109"},
    {"id": 3, "erpcode": "OR1000140122"}, {"id": 4, "erpcode": "OR1000152592-55"},
    # one number on two orders: the key cannot say which
    {"id": 5, "erpcode": "OR1000149999"}, {"id": 6, "erpcode": "OR1000149999"},
    # placeholders typed into the order's code field (live: KATERINA, TBD, WIP, STOCK, dates)
    {"id": 7, "erpcode": "STOCK"}, {"id": 8, "erpcode": "05-10-2026"},
    # a placeholder on two orders is shared, so it names neither
    {"id": 9, "erpcode": "TBD"}, {"id": 10, "erpcode": "TBD"},
]
BATCHES = {
    10: "OR1000138953\rOR1000138953",                     # one order repeated: links
    11: "OR1000145109\rOR1000145109\rOR1000145109",       # three times: still one link
    12: "OR1000140122",                                   # a single number (already linked)
    13: "OR1000138953\rOR1000145109",                     # two orders: both link
    14: "OR1000138953\nOR1000149999\r\nOR1000140122",     # one line is ambiguous: the rest link
    15: "BIOPHORA APRIL 2025\rBIOPHORA APRIL 2025",       # months: nothing
    16: "OR1000150427 Gallon Order",                      # a number with words: nothing
    17: "OR1000152592-55",                                # the range is one order number
    18: "OR1000138953\rBIOPHORA JAN 2025",                # a number among notes: links
    19: "",                                               # blank
    20: "\r\r",                                           # only breaks
    21: "STOCK",                                          # an order whose code is a placeholder
    22: "05-10-2026\r05-10-2026",                         # an order whose code is a date
    23: "TBD",                                            # shared by two orders: unresolved
}
ROWS = {
    "public.order_entry": ORDERS,
    # the same erpcode column joins the Sage order elsewhere (SalesOrder.erp_order) and keeps
    # a placeholder there: the two links' key sets are separate
    "sage.oe_order_header": [{"orduniq": 1, "ordnumber": "OR1000138953"},
                             {"orduniq": 2, "ordnumber": "STOCK"}],
    "public.batch": [{"id": i, "oelist_c": v, "namebatch_c": f"BC-{i}"}
                     for i, v in BATCHES.items()],
}


KEYS = {"sage.oe_order_header": "orduniq"}


class Source:
    source = "portal_api"

    def describe(self):
        return {"generation": 1, "relations": {}}

    def records(self, relations):
        return {rel: [CapturedRecord("portal_api", rel,
                                     json.dumps({KEYS.get(rel, "id"): row[KEYS.get(rel, "id")]}),
                                     row,
                                     f"api://{rel}/{row[KEYS.get(rel, 'id')]}@1", "row",
                                     hashlib.sha256(str(row[KEYS.get(rel, 'id')]).encode())
                                     .hexdigest(), T,
                                     None, T, f"api:{rel}")
                      for row in ROWS.get(rel, [])]
                for rel in relations}


def oid(type_id: str, binding: str, key) -> str:
    return f"{type_id}:{binding}:{json.dumps({'id': key}, separators=(',', ':'))}"


class BatchOrderListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        path = cls.path = Path(cls.temp.name) / "store.sqlite"
        cls.report = build_store(path, load_schema(), {"portal_api": Source()},
                                 normalizer=identifiers.normalize,
                                 clock=lambda: datetime(2026, 10, 13, tzinfo=timezone.utc))
        conn = sqlite3.connect(str(path))
        try:
            cls.rows = conn.execute(
                "SELECT from_object, to_object, tier FROM links WHERE link_type = "
                "'Batch.for_sales_order'").fetchall()
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def orders(self, batch: int) -> dict:
        frm = oid("Batch", "portal.fm_batch", batch)
        return {to: tier for f, to, tier in self.rows if f == frm}

    def order(self, n: int) -> str:
        return oid("SalesOrder", "portal.fm_order_entry", n)

    def test_a_repeated_order_number_links_that_order_once(self):
        self.assertEqual(self.orders(10), {self.order(1): "verified"})
        self.assertEqual(self.orders(11), {self.order(2): "verified"})

    def test_a_single_number_and_a_range_number_link_as_before(self):
        self.assertEqual(self.orders(12), {self.order(3): "verified"})
        self.assertEqual(self.orders(17), {self.order(4): "verified"})

    def test_a_list_of_several_orders_links_every_one(self):
        self.assertEqual(self.orders(13), {self.order(1): "verified", self.order(2): "verified"})

    def test_a_line_naming_two_orders_is_left_unresolved_and_the_other_lines_link(self):
        self.assertEqual(self.orders(14), {self.order(1): "verified", self.order(3): "verified"})

    def test_a_number_among_notes_links_and_notes_alone_link_nothing(self):
        self.assertEqual(self.orders(18), {self.order(1): "verified"})
        for batch in (15, 16, 19, 20, 23):
            self.assertEqual(self.orders(batch), {}, batch)

    def test_an_order_whose_code_is_a_unique_placeholder_or_date_links_like_any_other(self):
        self.assertEqual(self.orders(21), {self.order(7): "verified"})
        self.assertEqual(self.orders(22), {self.order(8): "verified"})

    def test_another_link_on_the_same_column_keeps_its_placeholder_keys(self):
        with sqlite3.connect(str(self.path)) as conn:
            erp = conn.execute("SELECT from_object FROM links WHERE link_type = "
                               "'SalesOrder.erp_order'").fetchall()
        self.assertIn((self.order(7),), erp)       # STOCK joins the Sage STOCK order there

    def test_no_link_is_written_twice(self):
        self.assertEqual(len(self.rows), len(set((f, t) for f, t, _ in self.rows)))
        self.assertEqual(len(self.rows), 1 + 1 + 1 + 2 + 2 + 1 + 1 + 1 + 1)


if __name__ == "__main__":
    unittest.main()
