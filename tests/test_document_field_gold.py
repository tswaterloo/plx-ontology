"""The labelled-set scorer of document fields (``evals/document_field_gold.py``, AUD-07):
per-field precision, recall and fill from outcomes, amounts compared by digest and
currency only, a misread class's fields missed, unknown fields and stores refused, and a
report that names no value. Evidence: isolated-fixture."""

from __future__ import annotations

import json
import unittest

from evals import document_field_gold as gold


def _doc(index, labels, profile="supplier_invoice", store="sharepoint"):
    return {"index": index, "store": store, "profile": profile, "row": {}, "labels": labels}


class DocumentFieldGoldTests(unittest.TestCase):

    def test_outcomes_shares_and_gate(self):
        amount = {"digest": gold.amount_digest("1250.00"), "currency": "USD"}
        docs = [
            _doc(1, {"invoice_number": "INV-1", "amount": amount, "po_reference": None}),
            _doc(2, {"invoice_number": ["K7QX2ZPA 0042", "K7QX2ZPA-0042"],
                     "amount": {"digest": gold.amount_digest("9.5"), "currency": None},
                     "po_reference": "PO10000001"}),
            _doc(3, {"invoice_number": "INV-3", "amount": amount}),
            _doc(4, {"lot": "31003C"}, profile="certificate_of_analysis"),
        ]
        results = {
            1: {"status": "classified", "profile": "supplier_invoice", "fields": {
                "invoice_number": {"value": "inv-1"},                    # case folded
                "amount": {"value": "1250", "currency": "USD"},          # 1250.00 == 1250
                "po_reference": {"value": "PO-9"}}},                     # none printed
            2: {"status": "classified", "profile": "supplier_invoice", "fields": {
                "invoice_number": {"value": "K7QX2ZPA-0042"},
                "amount": {"value": "9.50", "currency": "CAD"},          # no label currency
                "po_reference": {"value": None, "gap": "label_seen_unread",
                                 "unread": "value_unplaced"}}},
            3: {"status": "classified", "profile": "supplier_invoice", "fields": {
                "invoice_number": {"value": "INV-4"},
                "amount": {"value": "1250.00", "currency": "CAD"}}},     # wrong currency
            4: {"status": "classified", "profile": "packing_slip", "fields": {
                "lot": {"value": "31003C"}}},                            # another class
        }
        report = gold.score(docs, lambda doc: results[doc["index"]])
        fields = report["fields"]
        self.assertEqual({k: fields["invoice_number"][k] for k in gold.OUTCOMES},
                         {"correct": 2, "wrong": 1, "missed": 0, "silent": 0})
        self.assertEqual(fields["invoice_number"]["precision"], round(2 / 3, 4))
        self.assertEqual({k: fields["amount"][k] for k in gold.OUTCOMES},
                         {"correct": 2, "wrong": 1, "missed": 0, "silent": 0})
        self.assertEqual({k: fields["po_reference"][k] for k in gold.OUTCOMES},
                         {"correct": 0, "wrong": 1, "missed": 1, "silent": 0})
        self.assertEqual(fields["po_reference"]["unread_why"], {"value_unplaced": 1})
        self.assertEqual((fields["po_reference"]["recall"], fields["po_reference"]["fill"]),
                         (0.0, 0.5))
        # A document read as another class states none of the labelled class's fields.
        self.assertEqual((fields["lot"]["missed"], fields["lot"]["class_differs"]), (1, 1))
        self.assertIsNone(fields["lot"]["precision"])
        self.assertFalse(report["gate"])   # 67% precision is under the 90% gate
        # The report counts; it never names a value or an amount's digest.
        text = json.dumps(report)
        for secret in ("INV-1", "K7QX2ZPA", "PO-9", "1250", "31003C", amount["digest"]):
            self.assertNotIn(secret, text)

    def test_a_clean_set_passes_and_unknown_input_is_refused(self):
        docs = [_doc(1, {"invoice_number": "INV-1"})]
        found = {"status": "classified", "profile": "supplier_invoice",
                 "fields": {"invoice_number": {"value": "INV-1"}}}
        self.assertTrue(gold.score(docs, lambda doc: found)["gate"])
        self.assertFalse(gold.score([], lambda doc: found)["gate"])   # nothing measured
        with self.assertRaises(ValueError):
            gold.score([_doc(1, {"total": "1"})], lambda doc: found)
        with self.assertRaises(ValueError):
            gold.capture_extractor({})(_doc(1, {}, store="teams"))
        # An unreadable document states nothing (missed), never an error mid-run.
        report = gold.score(docs, gold.capture_extractor({"sharepoint": "/nonexistent"}))
        self.assertEqual(report["fields"]["invoice_number"]["missed"], 1)

    def test_the_gate_holds_a_recall_floor_per_field(self):
        """A precise extractor that reads too few printed values fails the gate (11-15:
        precision 1.00 everywhere hid amount recall 0.41); a field printed but never stated
        fails it too; per-store recall is reported."""
        floor = gold.RECALL_FLOOR["amount"]
        total = 20
        read = int(floor * total)            # exactly at the floor: passes
        docs = [_doc(i, {"amount": {"digest": gold.amount_digest(str(i + 1)), "currency": None}},
                     store="mail" if i % 2 else "sharepoint") for i in range(total)]

        def reading(n):
            def extract(doc):
                value = str(doc["index"] + 1) if doc["index"] < n else None
                return {"status": "classified", "profile": "supplier_invoice",
                        "fields": {"amount": {"value": value, "currency": "USD"}}}
            return extract

        at_floor = gold.score(docs, reading(read))
        self.assertEqual(at_floor["fields"]["amount"]["precision"], 1.0)
        self.assertTrue(at_floor["fields"]["amount"]["gate_recall"])
        self.assertTrue(at_floor["gate"])
        under = gold.score(docs, reading(read - 1))
        self.assertEqual(under["fields"]["amount"]["precision"], 1.0)
        self.assertFalse(under["fields"]["amount"]["gate_recall"])
        self.assertFalse(under["fields"]["amount"]["gate"])
        self.assertFalse(under["gate"])      # precise but under the recall floor
        self.assertEqual(under["recall_floor"]["amount"], str(floor))
        stores = under["per_store"]
        read_mail = sum(1 for i in range(read - 1) if i % 2)
        self.assertEqual(stores["mail"]["amount"]["recall"], round(read_mail / (total // 2), 4))
        self.assertEqual(stores["sharepoint"]["amount"]["recall"],
                         round((read - 1 - read_mail) / (total // 2), 4))
        self.assertEqual(sum(stores[s]["amount"]["correct"] for s in stores), read - 1)
        self.assertNotIn("_printed", json.dumps(stores))
        nothing = gold.score(docs, reading(0))
        self.assertIsNone(nothing["fields"]["amount"]["precision"])
        self.assertFalse(nothing["gate"])    # printed, never stated: recall 0
        # Every field labelled none and none stated: nothing to read, the field passes.
        silent = gold.score([_doc(1, {"po_reference": None})], reading(0))
        self.assertTrue(silent["fields"]["po_reference"]["gate"])


if __name__ == "__main__":
    unittest.main()
