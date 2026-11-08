"""The History tab's reads go through plx.object as the reader: a change log page by
cursor and the record as recorded on a date; what the reader may not see is said in
words with no number, and a record they cannot read is not found."""
from __future__ import annotations

import json
import unittest

from operator_api import history

OID = "SalesOrder:portal.app_sales_order:so-1"
HANDLE = "api://public.app_sales_order/so-1@2"


def url(kind, oid):
    return f"/object/{kind}/{oid}"


def value(prop, raw, **extra):
    return dict({"property": prop, "value_type": "text", "value": raw,
                 "citation": {"l1_handle": HANDLE, "field_path": f"row.{prop}",
                              "digest": "0" * 64, "lineage": "gold_etl_from_fm"},
                 "markings": []}, **extra)


class View:
    def __init__(self, card):
        self.card, self.calls = card, []

    def _call(self, name, args):
        self.calls.append((name, args))
        return self.card


class HistoryTabTests(unittest.TestCase):
    def test_a_page_of_changes_in_words_with_its_source_and_cursor(self):
        view = View({"object": {"object_type": "SalesOrder", "display": {"untrusted_text": "SO 42"}},
                     "history": [
                         {"at": "2099-01-05T00:00:00Z", "kind": "changed",
                          "source": {"l1_handle": HANDLE, "lineage": "gold_etl_from_fm"},
                          "changes": [{"property": "total_amount",
                                       "before": [value("total_amount", "1000", value_type="money",
                                                        currency="CAD")],
                                       "after": [value("total_amount", "1250.5", value_type="money",
                                                       currency="CAD")]}]},
                         {"at": "2099-01-04T00:00:00Z", "kind": "link_added",
                          "link": {"link_type": "SalesInvoice.invoices_order",
                                   "direction": "reverse", "tier": "lead", "basis": "fk",
                                   "other": {"object_id": "SalesInvoice:bc:inv-1",
                                             "object_type": "SalesInvoice",
                                             "display": {"untrusted_text": "PSI 1"}}}},
                         {"at": "2099-01-03T00:00:00Z", "kind": "something_new"}],
                     "completeness": {"next_cursor": "h1.next"}})
        out = history.history(view, OID, "h1.prev", open_url=url)
        self.assertEqual(view.calls, [("plx.object", {"object_id": OID, "history": True,
                                                      "cursor": "h1.prev"})])
        self.assertEqual([e.kind for e in out.entries], ["changed", "link_added"])  # unknown kind dropped
        change = out.entries[0].changes[0]
        self.assertEqual((change.before, change.after), (["1,000.00 CAD"], ["1,250.50 CAD"]))
        self.assertEqual(out.entries[0].source, "Portal sales order")
        self.assertEqual(out.entries[1].link.tier, "suggested")
        self.assertEqual(out.next_cursor, "h1.next")
        self.assertFalse(out.partial)

    def test_hidden_changes_are_said_without_a_number_and_unreadable_is_not_found(self):
        out = history.history(View({"object": {}, "history": [], "withheld": {
            "properties": None, "objects": None, "reason": "marking"},
            "completeness": {}}), OID, None, open_url=url)
        self.assertTrue(out.partial)
        self.assertNotRegex(out.message, r"\d")
        with self.assertRaises(KeyError):
            history.history(View({"error": {"code": "object_not_found"}}), OID, None,
                            open_url=url)
        with self.assertRaises(ValueError):   # a cursor not of this record
            history.history(View({"error": {"code": "invalid_input"}}), OID, "h1.x",
                            open_url=url)

    def test_as_of_reads_the_record_then_and_refuses_a_time_it_cannot_read(self):
        view = View({"object": {"object_type": "SalesOrder"},
                     "values": [value("customer_po", {"untrusted_text": "PO 7781"}),
                                value("customer_po", {"untrusted_text": "PO 7781"})],
                     "linked_counts": [{"link_type": "SalesInvoice.invoices_order",
                                        "direction": "reverse", "count": 2}],
                     "withheld": {"properties": 1, "objects": None, "reason": "marking"},
                     "meta": {"as_known_at": "2099-01-03T23:59:59.999Z"}})
        out = history.as_of(view, OID, "2099-01-03")
        self.assertEqual(view.calls[0][1]["as_known_at"], "2099-01-03")
        self.assertEqual([(v.label, v.values) for v in out.values], [("Customer PO", ["PO 7781"])])
        self.assertEqual([link.count for link in out.links], [2])
        self.assertTrue(out.partial)
        self.assertNotIn("1", out.message)
        for bad in ("yesterday", "2099-13-40", "2099-01-03T10:00"):
            with self.subTest(bad), self.assertRaises(ValueError):
                history.as_of(view, OID, bad)
        with self.assertRaises(KeyError):
            history.as_of(View({"error": {"code": "object_not_found"}}), OID, "2099-01-03")


if __name__ == "__main__":
    unittest.main()
