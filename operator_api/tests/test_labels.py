"""The words Home and the source page show (``operator_api.backend``): an opened email is
titled by its subject; an at-risk record reads by its own label, never "<Type> record";
a quantity conflict reads in words with sensible precision and its unit on each value.
Synthetic records; evidence label: isolated-fixture."""

from __future__ import annotations

from decimal import Decimal
import unittest

from operating import proactive_gaps as pg
from operator_api import backend as be


class Labels(unittest.TestCase):
    def test_an_opened_email_is_titled_by_its_subject(self):
        got = be.source_payload("graph-mail://box/AAA@g-1", {"row": {
            "subject": "RE: PO 77 is ready", "from": "a@example.com", "body_preview": "x"}}, 0)
        self.assertEqual(got.title, "RE: PO 77 is ready")
        self.assertIsNone(be.source_payload("graph-mail://box/B@g-1", {"row": {"x": 1}},
                                            0).title)

    def test_an_at_risk_record_reads_by_its_label_and_a_quantity_conflict_in_words(self):
        def pair(left, right):
            return [{"kind": "value_disagreement", "subject": "AssemblyOrder.ordered_boxes",
                     "values": [{"value_type": "quantity", "value": left, "unit": "box",
                                 "quantity_role": "ordered", "l1_handle": "api://a/1@g",
                                 "field_path": "row.x"},
                                {"value_type": "quantity", "value": right, "unit": "box",
                                 "quantity_role": "ordered", "l1_handle": "api://b/2@g",
                                 "field_path": "row.y"}]}]
        # one quantity rounded two ways is no risk at all
        self.assertEqual(pg._conflicting_quantities(pair("1257", "1257.1666666666667")), [])
        summary = pg._conflicting_quantities([{
            "kind": "value_disagreement", "subject": "AssemblyOrder.ordered_boxes",
            "values": [{"value_type": "quantity", "value": "1200", "unit": "box",
                        "quantity_role": "ordered", "l1_handle": "api://a/1@g",
                        "field_path": "row.x"},
                       {"value_type": "quantity", "value": "1257.1666666666667",
                        "unit": "box", "quantity_role": "ordered", "l1_handle": "api://b/2@g",
                        "field_path": "row.y"}]}])
        self.assertEqual(len(summary), 1)
        gap = {"object_id": "AssemblyOrder:x:1", "object_type": "AssemblyOrder",
               "kind": "conflicting_quantity", "summary": summary[0]["summary"]}
        _late, risk = be.attention_items({"gaps": [gap]}, {})
        self.assertEqual(risk[0].label, "Assembly order")
        self.assertEqual(risk[0].reason, "Assembly order ordered boxes: sources report "
                                         "different ordered quantity values (1200 box and "
                                         "1257.17 box)")
        _late, risk = be.attention_items({"gaps": [gap]}, {"AssemblyOrder:x:1": "AO-1"})
        self.assertEqual(risk[0].label, "AO-1")
        # an inbox built before the engine rounded reads the same
        old = dict(gap, summary="AssemblyOrder.ordered_boxes: sources report different "
                                "ordered quantity values (1200, 1257.1666666666667 box)")
        self.assertEqual(be.attention_items({"gaps": [old]}, {})[1][0].reason,
                         risk[0].reason)
        self.assertEqual(pg.shown_numbers([Decimal("6.888"), Decimal("6.889")]),
                         ["6.888", "6.889"])


if __name__ == "__main__":
    unittest.main()
