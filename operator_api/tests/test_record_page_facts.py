"""A record page says what is the record's own: its source system read from the handle
(a Sage table served through the Portal's API is Sage), and an event it takes no part in
(a linked customer's other orders) is marked as not its own, so it is never shown as the
record's last change."""
from __future__ import annotations

import unittest

from operator_api import models as m
from operator_api.page_parts import label_timeline
from operator_api.serving import record_words, system_of

SHIPMENT = 'Shipment:sage.oe_shipment_header:{"shiuniq":"513"}'
CUSTOMER = 'Customer:sage.ar_customer:{"idcust":"GRANITE"}'


class NoReads:
    def _call(self, tool, args):   # every name is given: nothing may be read
        raise AssertionError(f"unexpected read {tool}")


class RecordPageFacts(unittest.TestCase):
    def test_a_sage_table_through_the_portal_api_is_sage(self):
        handle = "api://sage.oe_shipment_header/%7B%22shiuniq%22%3A%22513%22%7D@5a1c-424"
        self.assertEqual(system_of(handle), "Sage")
        self.assertEqual(record_words(handle), "Sage oe shipment header")
        self.assertEqual(system_of("api://public.app_purchase_order/1@x"), "Portal")
        self.assertEqual(system_of("bc://purchase_orders/1"), "BC")
        # a BC record is named by its table, never by the environment ("BC production")
        bc = "bc://production/ca41a27f-809d-f111-8072-6045bd5bf768/customers/002742ac-10a7-f111-aaa8-002248ab0c4d"
        self.assertEqual(record_words(bc), "BC customer")
        self.assertEqual(record_words("bc://production/ca41a27f-809d-f111-8072-6045bd5bf768/"
                                      "purchaseOrders/1"), "BC purchase order")

    def test_an_event_the_record_takes_no_part_in_is_not_its_own(self):
        page = m.ObjectPage.model_construct(object_id=SHIPMENT, timeline=[
            m.TimelineEvent(at="2016-10-06", system="Sage", title="Shipped"),
            m.TimelineEvent(at="2019-09-05", system="Sage", title="Ordered"),
            m.TimelineEvent(at="2016-10-01", system="Sage", title="Noted")])
        events = [{"objects": [{"object_id": SHIPMENT}, {"object_id": CUSTOMER}]},
                  {"objects": [{"object_id": CUSTOMER}]},
                  {}]
        label_timeline(NoReads(), page, {CUSTOMER: "GRANITE CLUB"}, {}, events)
        self.assertEqual([e.own for e in page.timeline], [True, False, True])
        self.assertEqual(page.timeline[1].title, "Ordered: customer GRANITE CLUB")


if __name__ == "__main__":
    unittest.main()
