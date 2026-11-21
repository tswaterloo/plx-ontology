"""Which system a record's source is named as (walk 11-21: Sage shipments and receipts the
Portal's database mirrors were labelled "Customer Portal" on their record pages)."""

from __future__ import annotations

import unittest

from operator_api import doc_fill_api, serving

SAGE = "api://sage.oe_shipment_header/%7B%22shiuniq%22%3A%22513%22%7D@5a1c29f59e06162d-424"
PORTAL = "api://public.app_sales_order/%7B%22id%22%3A%22b7%22%7D@5a1c29f59e06162d-21999"


class SourceSystem(unittest.TestCase):
    def test_a_mirrored_system_is_named_as_itself(self):
        cases = {SAGE: "Sage", "api://sage.po_receipt_header/x@1": "Sage",
                 "api://SAGE.ar_customer/x@1": "Sage", PORTAL: "Portal",
                 "api://silver.customers/x@1": "Portal",
                 # unknown or malformed: the Portal, as before (never another system)
                 "api://other.table/x@1": "Portal", "api://": "Portal",
                 "api:///x": "Portal", "bc://production/x/salesOrders/y@1": "BC"}
        for handle, system in cases.items():
            self.assertEqual(serving.system_of(handle), system, handle)
            self.assertEqual(doc_fill_api.system_of(handle), system, handle)

    def test_record_words_say_the_system(self):
        self.assertEqual(serving.record_words(SAGE), "Sage oe shipment header")
        self.assertEqual(serving.record_words(PORTAL), "Portal sales order")


if __name__ == "__main__":
    unittest.main()
