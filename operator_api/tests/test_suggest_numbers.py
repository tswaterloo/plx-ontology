"""The record search for models finds a document by its number: an exact number ranks first
(a document whose number is typed beats customer POs that merely start with it), and the
system of record's copy ranks before BC's pilot copy of the same number
(``operating.po_pilot``, the one rule). ``search.entities`` rows as ``_pg_suggest`` reads
them (object id, entity type, name, aliases, exact, code, prefix, word, similarity)."""
from __future__ import annotations

import re
import unittest

from operating import po_pilot
from operator_api import suggest as sg

SAGE = 'Shipment:portal.sage_oe_shipment_header:{"shiuniq":"513"}'
BC = 'Shipment:bc.salesShipments:{"id":"a1"}'
UAT = 'CustomerPurchaseOrder:bc.salesOrders:{"id":"u1"}'


def row(oid, kind, name, alias, *, exact=False, code=False, prefix=False):
    return (1, oid, kind, name, alias, exact, code, prefix, False, 0.0)


class NumberedRecords(unittest.TestCase):
    def test_an_exact_number_beats_a_customer_po_starting_with_it_and_sage_beats_bc(self):
        rows = [row(UAT, "customer_po", "UAT-SH100001", ["UAT-SH100001"], prefix=True),
                row(BC, "shipment", "Shipment SH1000000001", ["SH1000000001"], exact=True,
                    code=True),
                row(SAGE, "shipment", "Shipment SH1000000001", ["SH1000000001"], exact=True,
                    code=True)]
        out = sg.suggestions(rows, 10, same_record=lambda ids: set())
        ids = [s.object_id for s in out]
        self.assertEqual(ids[:2], [SAGE, BC])        # the system of record's copy, then BC's
        self.assertEqual(ids[-1], UAT)               # the prefix match of a PO after both
        self.assertEqual([s.match for s in out], ["exact", "exact", "prefix"])
        # a Sage invoice found by its number leads whatever else shares the typed text
        inv = 'SalesInvoice:portal.sage_oe_invoice_header:{"k":"1"}'
        out = sg.suggestions(rows + [row(inv, "sales_invoice", "Invoice IN1000180232",
                                         ["IN1000180232"], exact=True, code=True)], 10,
                             same_record=lambda ids: set())
        self.assertIn("Invoice IN1000180232", [s.label for s in out[:3]])

    def test_the_pilot_rule_in_sql_and_python_agree(self):
        pattern = re.compile(r"'(\^[^']+)'")
        regex = pattern.search(po_pilot.bc_pilot_object_sql("x")).group(1)
        for oid in (SAGE, BC, UAT, "", "Shipment", "Customer:portal.app_customer:{}",
                    "Customer:bc.customers:{}"):
            with self.subTest(oid=oid):
                self.assertEqual(bool(re.search(regex, oid)), po_pilot.is_bc_pilot_object(oid))
        self.assertTrue(po_pilot.bc_pilot_record_sql("r.system").startswith("(r.system = 'bc'"))
        self.assertTrue(po_pilot.is_bc_pilot_object(BC))
        self.assertFalse(po_pilot.is_bc_pilot_object(SAGE))


if __name__ == "__main__":
    unittest.main()
