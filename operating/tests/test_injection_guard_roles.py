"""operating.injection_guard trace roles on real-shaped Portal and BC rows.

The injection hook and the action catalog decide a cell's role by one rule
(``injection_guard.cited_role``): the schema binding, else the identifier kinds whose
carriers declare the column (identifiers.json), decided per value by the identifier
grammar; an undecidable role is ambiguous and supplies nothing. Rows are synthetic
values in real relation shapes.

Evidence label: isolated-fixture (in-memory citation contents; no source is read).
"""

from __future__ import annotations

import unittest

from operating import injection_guard as guard

ORDER = "api://public.app_sales_order/2@g1-c1"
ORDER_ROW = {"relation": "public.app_sales_order", "key": {"id": 2},
             "row": {"id": 2, "order_number": "SO-2026-0012", "po_reference": None}}


def api(relation, number, row):
    return {"handle": f"api://{relation}/{number}@g1-c1",
            "content": {"relation": relation, "key": {"id": number},
                        "row": {"id": number, **row}}}


def bc(relation, number, row):
    return {"handle": f"bc://Production/c1/{relation}/{number}@e1",
            "content": {"relation": relation, "key": {"id": number},
                        "row": {"id": number, **row}}}


def set_customer_po(value, *evidence, target=None, roles=None):
    target = target or {"handle": ORDER, "content": ORDER_ROW}
    effect = {"relation": target["content"]["relation"], "key": target["content"]["key"],
              "fields": ["po_reference"], "equals": value,
              "target_citation": target["handle"]}
    return guard.effect_trace(effect, [target, *evidence], None, roles=roles)


class CustomerPoRoleTests(unittest.TestCase):
    def test_po_reference_takes_only_a_customer_po_cell(self):
        # po_reference is read as raw text and as an identifier by the schema; its
        # carriers make it a customer PO, so an equal sales-order number never fills it.
        so = api("public.app_sales_order", 3, {"order_number": "SO-2026-0013"})
        refused = set_customer_po("SO-2026-0013", so)
        self.assertEqual(refused["reason"], guard.REFUSAL_ROLE)
        entry = api("public.order_entry", 7, {"custpocode": "PO4500"})
        traced = set_customer_po("PO4500", entry)
        self.assertTrue(traced["traced"], traced)
        trace = traced["traces"]["equals"]
        self.assertEqual((trace["citation"], trace["role"], trace["written_role"]),
                         (entry["handle"], "identifier:customer_po", "identifier:customer_po"))

    def test_a_po_reference_value_never_supplies_another_role(self):
        # The reverse: a po_reference cell is no Petra PO.
        po = api("public.app_purchase_order", 5, {"po_number": None})
        cited = api("public.app_sales_order", 4, {"po_reference": "PO4500"})
        effect = {"relation": "public.app_purchase_order", "key": {"id": 5},
                  "fields": ["po_number"], "equals": "PO4500", "target_citation": po["handle"]}
        self.assertEqual(guard.effect_trace(effect, [po, cited], None)["reason"],
                         guard.REFUSAL_ROLE)

    def test_the_catalog_parameter_role_is_the_written_role(self):
        # A target that declares no role for the field takes any cell; the action
        # type's parameter role does not.
        target = {"handle": "fixture://fixture.sales_order/{\"id\":2}",
                  "content": {"relation": "fixture.sales_order", "key": {"id": 2},
                              "row": {"id": 2, "po_reference": None}}}
        so = api("public.app_sales_order", 3, {"order_number": "SO-2026-0013"})
        self.assertTrue(set_customer_po("SO-2026-0013", so, target=target)["traced"])
        refused = set_customer_po("SO-2026-0013", so, target=target,
                                  roles={"po_reference": "identifier:customer_po"})
        self.assertEqual(refused["reason"], guard.REFUSAL_ROLE)


class SchemaTypedCellTests(unittest.TestCase):
    """Only a schema-typed cell supplies a value, and a field no role types takes none
    (the injection hook ``ActionPath.register`` runs)."""

    def test_an_untyped_cell_never_supplies_a_value(self):
        # A role-less fixture target takes any typed cell, but a free-text cell of a real
        # row is no typed value: the equal note text supplies nothing.
        target = {"handle": "fixture://fixture.sales_order/{\"id\":2}",
                  "content": {"relation": "fixture.sales_order", "key": {"id": 2},
                              "row": {"id": 2, "po_reference": None}}}
        noted = api("public.app_sales_order", 3, {"notes": "PO4500"})
        self.assertNotIn(("row.notes", "PO4500"),
                         list(guard.typed_values(noted["handle"], noted["content"])))
        refused = set_customer_po("PO4500", noted, target=target)
        self.assertEqual((refused["traced"], refused["reason"]), (False, guard.REFUSAL))

    def test_a_target_field_no_role_types_is_refused_for_a_real_owner(self):
        # public.app_assembly_order declares no role for this column: no cited cell,
        # however typed, may be written there.
        target = api("public.app_assembly_order", 9, {"remarks_c": None})
        so = api("public.app_sales_order", 3, {"order_number": "SO-2026-0013"})
        effect = {"relation": "public.app_assembly_order", "key": {"id": 9},
                  "fields": ["remarks_c"], "equals": "SO-2026-0013",
                  "target_citation": target["handle"]}
        refused = guard.effect_trace(effect, [target, so], None)
        self.assertEqual((refused["traced"], refused["reason"]),
                         (False, guard.REFUSAL_UNTYPED_FIELD))


class ExternalDocumentNumberTests(unittest.TestCase):
    """BC externalDocumentNumber holds the customer PO on sales orders and the Portal
    order number on sales invoices (plx-customer-portal@82549dd
    portal/src/lib/business-central/serializers.ts:1049, :1157-1158)."""

    def test_an_so_number_is_never_a_customer_po(self):
        for relation in ("salesInvoices", "salesOrders"):
            with self.subTest(relation=relation):
                cited = bc(relation, 1, {"externalDocumentNumber": "SO100000"})
                self.assertEqual(set_customer_po("SO100000", cited)["reason"],
                                 guard.REFUSAL_ROLE)
        # The grammar decides: an invoice's SO-shaped number is a sales-order number
        # (an invoice declares that carrier); on a sales order it contradicts the
        # declared customer PO and is ambiguous.
        invoice = bc("salesInvoices", 1, {"externalDocumentNumber": "SO100000"})
        order = bc("salesOrders", 1, {"externalDocumentNumber": "SO100000"})
        self.assertEqual(guard.cited_role(invoice["handle"], invoice["content"],
                                          "row.externalDocumentNumber"), "identifier:so_number")
        self.assertEqual(guard.cited_role(order["handle"], order["content"],
                                          "row.externalDocumentNumber"), guard.AMBIGUOUS_ROLE)
        # An SO number no shape reads (UAT-SO100018) on an invoice: two kinds held,
        # nothing decides, so it is ambiguous and never a customer PO.
        unshaped = bc("salesInvoices", 2, {"externalDocumentNumber": "UAT-SO100018"})
        self.assertEqual(guard.cited_role(unshaped["handle"], unshaped["content"],
                                          "row.externalDocumentNumber"), guard.AMBIGUOUS_ROLE)
        self.assertEqual(set_customer_po("UAT-SO100018", unshaped)["reason"],
                         guard.REFUSAL_ROLE)

    def test_an_undecidable_external_document_number_is_refused(self):
        # No shape decides between the kinds a sales order's externalDocumentNumber
        # carries (customer PO, BC external document No.): ambiguous, so refused even
        # for a field written with that very role.
        cited = bc("salesOrders", 2, {"externalDocumentNumber": "4500-12"})
        self.assertEqual(guard.cited_role(cited["handle"], cited["content"],
                                          "row.externalDocumentNumber"), guard.AMBIGUOUS_ROLE)
        self.assertEqual(set_customer_po("4500-12", cited)["reason"], guard.REFUSAL_ROLE)
        self.assertFalse(guard.same_role(guard.AMBIGUOUS_ROLE, guard.AMBIGUOUS_ROLE))
        self.assertFalse(guard.same_role(None, guard.AMBIGUOUS_ROLE))

    def test_a_written_so_number_is_not_written_as_a_customer_po(self):
        # The written field's role is decided for the value written: an SO-shaped value
        # written to a sales order's externalDocumentNumber is ambiguous.
        target = bc("salesOrders", 3, {"externalDocumentNumber": None})
        so = api("public.app_sales_order", 3, {"order_number": "SO100000"})
        effect = {"relation": "salesOrders", "key": {"id": 3},
                  "fields": ["externalDocumentNumber"], "equals": "SO100000",
                  "target_citation": target["handle"]}
        traced = guard.effect_trace(effect, [target, so], None)
        self.assertEqual(traced["reason"], guard.REFUSAL_ROLE)


if __name__ == "__main__":
    unittest.main()
