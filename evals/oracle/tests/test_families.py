"""Oracle F1-F4 on a small hand-written world (injected transport; evidence: fixture).

Every expected value below was worked out by hand from the rows in ``small_world`` (not
by running the oracle), so a wrong join or sum fails here.
"""

from __future__ import annotations

import unittest

from evals.oracle import BcReads, PortalReads, Sources, make_oracle, run
from evals.oracle.fixture_world import World, bc_record

T = "2099-01-01T00:00:00Z"
KNOWN = {"time": "2099-02-01T00:00:00Z", "l1_generations": {"portal_api": "0" * 16}}


def small_world() -> World:
    rel = {
        "public.app_customer": [{"id": "c1", "code": "TC1", "updated_at": T},
                                {"id": "c2", "code": "TC2", "updated_at": T}],
        "public.app_product": [{"id": "p1", "code": "TI-1", "updated_at": T},
                               {"id": "p2", "code": "TI-2", "updated_at": T}],
        "public.app_sales_order": [
            {"id": "so1", "order_number": "TSO-1", "po_reference": "TCPO-9", "status": "SHIPPED",
             "customer_id": "c1", "custom_fields": {"fmId": 41}, "required_date": "2098-05-01",
             "updated_at": T},
            {"id": "so2", "order_number": "TSO-2", "po_reference": "TCPO-9", "status": "Open",
             "customer_id": "c1", "custom_fields": '{"sageOrdUniq": "77"}',
             "required_date": None, "updated_at": T},
            {"id": "so3", "order_number": "TSO-3", "po_reference": "Pat", "status": "COMPLETE",
             "customer_id": "c2", "custom_fields": {}, "required_date": "2098-06-01",
             "updated_at": T}],
        "public.app_sales_order_line": [
            {"id": "l1", "sales_order_id": "so1", "product_id": "p1", "quantity": "10.0",
             "uom": "ea", "updated_at": T},
            {"id": "l2", "sales_order_id": "so1", "product_id": "p2", "quantity": "5.0",
             "uom": "ea", "updated_at": T},
            {"id": "l3", "sales_order_id": "so2", "product_id": "p1", "quantity": "10.0",
             "uom": "ea", "updated_at": T},
            {"id": "l4", "sales_order_id": "so2", "product_id": "p2", "quantity": "2.0",
             "uom": "cse", "updated_at": T}],
        "public.app_manufacturing_order": [
            {"id": "m1", "mo_code": "AO-T1", "sales_order_line_id": "l1", "status": "Realized",
             "yield_actual": "9.0", "uom": "ea", "date_realized": None, "deleted_at": None,
             "updated_at": T},
            {"id": "m2", "mo_code": "AO-T2", "sales_order_line_id": "l2", "status": "Completed",
             "yield_actual": "5.0", "uom": "ea", "date_realized": None, "deleted_at": None,
             "updated_at": T},
            {"id": "m3", "mo_code": "AO-T3", "sales_order_line_id": "l2", "status": "Cancelled",
             "yield_actual": "100.0", "uom": "ea", "date_realized": None,
             "deleted_at": "2098-01-01T00:00:00Z", "updated_at": T}],
        "public.assembly_order": [{"id": 501, "nameamo_c": "AO-T1", "audittimestampmodified": T},
                                  {"id": 502, "nameamo_c": "AO-FMONLY",
                                   "audittimestampmodified": T}],
        "public.order_entry_item_assembly": [{"id": 1, "idassembly": 501, "idoeitem": 601}],
        "public.order_entry_item": [{"id": 601, "idoe": 41, "audittimestampmodified": T}],
        "public.order_entry": [{"id": 41, "custpocode": "TCPO-9", "audittimestampmodified": T}],
        "sage.oe_order_header": [{"orduniq": "77", "ordnumber": "S77", "ponumber": "TCPO-9",
                                  "audtdate": "20981231"}],
        "public.app_shipment": [
            {"id": "s1", "sales_order_id": "so1", "shipment_code": "TSH1", "status": "SHIPPED",
             "ship_date": "2098-05-02", "updated_at": T},
            {"id": "s2", "sales_order_id": "so1", "shipment_code": "TSH2", "status": "CANCELLED",
             "ship_date": None, "updated_at": T}],
        "public.app_shipment_line": [
            {"id": "sl1", "shipment_id": "s1", "product_id": "p1", "quantity": "10.0",
             "uom": "ea", "updated_at": T},
            {"id": "sl2", "shipment_id": "s1", "product_id": "p2", "quantity": "4.0",
             "uom": "ea", "updated_at": T},
            {"id": "sl3", "shipment_id": "s2", "product_id": "p2", "quantity": "99.0",
             "uom": "ea", "updated_at": T}],
        "public.app_supplier": [{"id": "v1", "code": "TV1", "updated_at": T}],
        "public.app_purchase_order": [
            {"id": "po1", "po_number": "TPO-1", "status": "RECEIVED", "supplier_id": "v1",
             "closed_at": None, "custom_fields": {"fmPoNumber": "FMP1"}, "updated_at": T},
            {"id": "po2", "po_number": "TPO-2", "status": "RECEIVED", "supplier_id": "v1",
             "closed_at": None, "custom_fields": {}, "updated_at": T}],
        "public.app_purchase_order_line": [
            {"id": "pl1", "purchase_order_id": "po1", "product_id": "p1", "quantity": "10.0",
             "quantity_received": "10.0", "unit_price": "1.5", "uom": "ea", "updated_at": T},
            {"id": "pl2", "purchase_order_id": "po1", "product_id": "p2", "quantity": "4.0",
             "quantity_received": "3.0", "unit_price": "2", "uom": "kgs", "updated_at": T},
            {"id": "pl3", "purchase_order_id": "po2", "product_id": "p1", "quantity": "7.0",
             "quantity_received": "7.0", "unit_price": "1.5", "uom": "ea", "updated_at": T}],
        "public.app_receipt": [
            {"id": "r1", "receipt_number": "TR-1", "purchase_order_id": "po1",
             "status": "RELEASED", "vendor_invoice_number": "VINV-1", "updated_at": T},
            {"id": "r2", "receipt_number": "TR-2", "purchase_order_id": "po2",
             "status": "RELEASED", "vendor_invoice_number": None, "updated_at": T}],
        "public.app_receipt_line": [
            {"id": "rl1", "receipt_id": "r1", "product_id": "p1", "quantity": "6.0", "uom": "ea",
             "updated_at": T},
            {"id": "rl2", "receipt_id": "r1", "product_id": "p1", "quantity": "4.0", "uom": "ea",
             "updated_at": T},
            {"id": "rl3", "receipt_id": "r1", "product_id": "p2", "quantity": "3.0", "uom": "kg",
             "updated_at": T},
            {"id": "rl4", "receipt_id": "r2", "product_id": "p1", "quantity": "7.0", "uom": "ea",
             "updated_at": T}],
        "public.generated_purchase_order": [{"id": 9, "ponumber": "FMP1",
                                             "audittimestampmodified": T}],
    }
    invoice = bc_record("purchaseInvoices", 0, 1, number="PI-1", vendorInvoiceNumber="VINV-1",
                        vendorNumber="TV1", status="Paid")  # found by vendor invoice No. only
    lookalike = bc_record("purchaseInvoices", 1, 2, number="PI-2", orderNumber="tpo-1",
                          status="Paid")  # BC matches case-insensitively; the oracle must not
    sales_invoice = bc_record("salesInvoices", 0, 3, number="SI-1",
                              externalDocumentNumber="TSO-1", status="Paid")
    bc = {"purchaseInvoices": [invoice, lookalike], "salesInvoices": [sales_invoice]}
    lines = {invoice["id"]: [
        bc_record("purchaseInvoiceLines", 0, 11, documentId=invoice["id"], lineType="Item",
                  lineObjectNumber="TI-1", quantity=10, unitOfMeasureCode="PCS"),
        bc_record("purchaseInvoiceLines", 0, 12, documentId=invoice["id"], lineType="Item",
                  lineObjectNumber="TI-2", quantity=3.0, unitOfMeasureCode="KG"),
        bc_record("purchaseInvoiceLines", 0, 13, documentId=invoice["id"], lineType="Charge",
                  lineObjectNumber="FREIGHT", quantity=1, unitOfMeasureCode=None)]}
    return World(rel, bc, lines)


def sources(world: World, *, bc: bool = True) -> Sources:
    return Sources(PortalReads(world.read_relation), BcReads(world.bc_port()) if bc else None)


def values(result: dict) -> dict:
    return {v["name"]: v for v in result["values"]}


def reasons(result: dict) -> set:
    return {g["reason"] for g in result["gaps"]}


class F3QuantitiesTest(unittest.TestCase):
    def test_ordered_produced_shipped_by_role(self):
        result = run("F3", {"identifier": "TSO-1", "identifier_kind": "so_number"}, KNOWN,
                     sources(small_world())).result
        got = values(result)
        # ordered 10 + 5; produced 9 + 5 (deleted m3 excluded); shipped 10 + 4 (cancelled
        # shipment s2 with 99 excluded).
        self.assertEqual((got["ordered"]["value"], got["ordered"]["unit"]), ("15", "ea"))
        self.assertEqual((got["produced"]["value"], got["produced"]["unit"]), ("14", "ea"))
        self.assertEqual((got["shipped"]["value"], got["shipped"]["unit"]), ("14", "ea"))
        self.assertEqual({v["quantity_role"] for v in result["values"]},
                         {"ordered", "produced", "shipped"})
        self.assertEqual(result["answerability"], "answerable")
        self.assertEqual(result["gaps"], [])
        cited = {c["l1_handle"].split("/")[2] for c in got["shipped"]["citations"]}
        self.assertEqual(cited, {"public.app_shipment_line"})
        self.assertEqual(len(got["shipped"]["citations"]), 2)

    def test_units_never_added_and_absences_are_gaps(self):
        result = run("F3", {"identifier": "TSO-2", "identifier_kind": "so_number"}, KNOWN,
                     sources(small_world())).result
        got = values(result)
        self.assertEqual((got["ordered_1"]["value"], got["ordered_1"]["unit"]), ("2", "cse"))
        self.assertEqual((got["ordered_2"]["value"], got["ordered_2"]["unit"]), ("10", "ea"))
        self.assertNotIn("ordered", got)
        self.assertIsNone(got["produced"]["value"])
        self.assertIsNone(got["shipped"]["value"])
        self.assertEqual(reasons(result), {"no_linked_assembly_order", "no_shipment_rows"})
        # The FM ETL never sets sales_order_line_id: no linked AO is not ingested, not absent.
        self.assertEqual({g["state"] for g in result["gaps"]}, {"not_ingested"})
        self.assertEqual(result["answerability"], "partial")

    def test_chain_hops_point_at_parent_line(self):
        result = run("F3", {"identifier": "TSO-1", "identifier_kind": "so_number"}, KNOWN,
                     sources(small_world())).result
        chain = result["chain"]
        index = {c["key"]["id"]: i for i, c in enumerate(chain)}
        self.assertEqual(chain[index["m1"]]["hop_from"], index["l1"])
        self.assertEqual(chain[index["m2"]]["hop_from"], index["l2"])
        self.assertNotIn("m3", index)
        self.assertNotIn("s2", index)


class F1ChainTest(unittest.TestCase):
    def test_walks_declared_keys_across_systems(self):
        result = run("F1", {"identifier": "TSO-1", "identifier_kind": "so_number"}, KNOWN,
                     sources(small_world())).result
        chain = result["chain"]
        by_id = {(c["relation"], c["key"].get("id")): c for c in chain}
        expected = {("public.app_sales_order", "so1"), ("public.app_customer", "c1"),
                    ("public.app_sales_order_line", "l1"), ("public.app_sales_order_line", "l2"),
                    ("public.app_product", "p1"), ("public.app_product", "p2"),
                    ("public.app_manufacturing_order", "m1"),
                    ("public.app_manufacturing_order", "m2"),
                    ("public.app_manufacturing_order", "m3"),
                    ("public.assembly_order", 501), ("public.order_entry_item_assembly", 1),
                    ("public.order_entry_item", 601), ("public.order_entry", 41),
                    ("public.app_shipment", "s1"), ("public.app_shipment", "s2"),
                    ("public.app_shipment_line", "sl1"), ("public.app_shipment_line", "sl2"),
                    ("public.app_shipment_line", "sl3"),
                    ("bc.sales_invoices", "cccccccc-0000-0000-0000-000000000003")}
        self.assertEqual(set(by_id), expected)
        # The customer is terminal: its other order (TSO-2) is not part of this chain.
        self.assertNotIn(("public.app_sales_order", "so2"), by_id)
        root = chain[0]
        self.assertEqual((root["relation"], root["key"]), ("public.app_sales_order",
                                                           {"id": "so1"}))
        self.assertNotIn("hop_from", root)
        fm = by_id[("public.order_entry", 41)]
        self.assertEqual((fm["fk"], fm["hop_from"]), ("so.fm_order_entry", 0))
        got = values(result)
        self.assertEqual(got["sales_order_line_count"]["value"], 2)
        self.assertEqual(got["bc_sales_invoice_count"]["value"], 1)
        self.assertEqual(got["root_record_count"]["value"], 1)
        self.assertEqual(result["answerability"], "answerable")
        self.assertTrue(any("serializers.ts:1049" in c for c in
                            run("F1", {"identifier": "TSO-1", "identifier_kind": "so_number"},
                                KNOWN, sources(small_world())).derived_from))

    def test_bc_root_links_back_to_portal(self):
        result = run("F1", {"identifier": "SI-1", "identifier_kind": "bc_number"}, KNOWN,
                     sources(small_world())).result
        chain = result["chain"]
        self.assertEqual(chain[0]["relation"], "bc.sales_invoices")
        so = [c for c in chain if c["relation"] == "public.app_sales_order"]
        self.assertEqual([(c["key"], c["fk"]) for c in so], [({"id": "so1"},
                                                              "bc_sales_invoice.so")])

    def test_exact_identifier_only(self):
        result = run("F1", {"identifier": "tso-1", "identifier_kind": "so_number"}, KNOWN,
                     sources(small_world())).result
        self.assertEqual(result["answerability"], "blocked")
        self.assertEqual(reasons(result), {"root_not_found"})
        self.assertNotIn("chain", result)

    def test_ao_number_falls_back_to_fm_assembly(self):
        result = run("F1", {"identifier": "AO-FMONLY", "identifier_kind": "ao_number"}, KNOWN,
                     sources(small_world())).result
        self.assertEqual(result["chain"][0]["relation"], "public.assembly_order")
        gold = run("F1", {"identifier": "AO-T1", "identifier_kind": "ao_number"}, KNOWN,
                   sources(small_world())).result
        self.assertEqual(gold["chain"][0]["relation"], "public.app_manufacturing_order")
        self.assertIn(("public.app_sales_order", {"id": "so1"}),
                      [(c["relation"], c["key"]) for c in gold["chain"]])

    def test_failed_read_is_inaccessible_not_absent(self):
        world = small_world()
        world.fail.add("public.app_shipment")
        result = run("F1", {"identifier": "TSO-1", "identifier_kind": "so_number"}, KNOWN,
                     sources(world)).result
        states = {(g["state"], g["subject"]) for g in result["gaps"]}
        self.assertIn(("inaccessible", "public.app_shipment"), states)
        self.assertEqual(result["answerability"], "partial")

    def test_without_bc_the_bc_leg_is_not_ingested(self):
        result = run("F1", {"identifier": "TSO-1", "identifier_kind": "so_number"}, KNOWN,
                     sources(small_world(), bc=False)).result
        self.assertIn("bc_not_read", reasons(result))
        self.assertFalse(any(c["relation"].startswith("bc.") for c in result["chain"]))

    def test_changed_rows_and_moved_roots_are_stale(self):
        early = {"time": "2098-01-01T00:00:00Z", "l1_generations": {"portal_api": "0" * 16}}
        result = run("F1", {"identifier": "TSO-1", "identifier_kind": "so_number"}, early,
                     sources(small_world())).result
        self.assertIn("changed_after_as_known_at", reasons(result))
        moved = run("F1", {"identifier": "TSO-1", "identifier_kind": "so_number",
                           "relation": "public.app_sales_order", "key": {"id": "so9"}},
                    KNOWN, sources(small_world())).result
        self.assertIn("root_moved", reasons(moved))
        self.assertEqual(moved["answerability"], "partial")

    def test_interface_callable(self):
        oracle = make_oracle("F1", sources(small_world()))
        result = oracle({"identifier": "TPO-1", "identifier_kind": "po_number"}, KNOWN)
        self.assertEqual(set(result) - {"chain"}, {"answerability", "values", "gaps"})
        with self.assertRaises(KeyError):
            make_oracle("F9", sources(small_world()))


class F2CustomerPoTest(unittest.TestCase):
    def test_every_order_carrying_the_po(self):
        result = run("F2", {"identifier": "TCPO-9", "identifier_kind": "customer_po"}, KNOWN,
                     sources(small_world())).result
        got = values(result)
        self.assertEqual(got["order_count"]["value"], 2)
        self.assertEqual(got["order_number_1"]["value"], "TSO-1")
        self.assertEqual(got["order_status_1"]["value"], "SHIPPED")
        self.assertEqual(got["shipment_count_1"]["value"], 1)  # cancelled s2 excluded
        self.assertEqual(got["shipment_statuses_1"]["value"], "SHIPPED")
        self.assertEqual(got["order_number_2"]["value"], "TSO-2")
        self.assertEqual(got["order_status_2"]["value"], "CONFIRMED")  # "Open" normalised
        self.assertIsNone(got["required_date_2"]["value"])
        self.assertIsNone(got["shipment_statuses_2"]["value"])
        self.assertEqual(got["sage_order_count"]["value"], 1)
        self.assertEqual(result["answerability"], "partial")
        self.assertEqual(reasons(result), {"field_empty", "no_shipment_rows"})

    def test_single_order_names_are_plain(self):
        result = run("F2", {"identifier": "Pat", "identifier_kind": "customer_po"}, KNOWN,
                     sources(small_world())).result
        got = values(result)
        self.assertEqual(got["order_number"]["value"], "TSO-3")
        self.assertEqual(got["sage_order_count"]["value"], 0)

    def test_other_roots_are_refused(self):
        result = run("F2", {"identifier": "TSO-1", "identifier_kind": "so_number"}, KNOWN,
                     sources(small_world())).result
        self.assertEqual((result["answerability"], reasons(result)),
                         ("blocked", {"family_root_unsupported"}))


class F4ThreeWayMatchTest(unittest.TestCase):
    def test_ordered_received_invoiced_per_item(self):
        answer = run("F4", {"identifier": "TPO-1", "identifier_kind": "po_number"}, KNOWN,
                     sources(small_world()))
        result = answer.result
        got = values(result)
        self.assertEqual(got["item_1"]["value"], "TI-1")
        self.assertEqual((got["ordered_1"]["value"], got["ordered_1"]["unit"]), ("10", "ea"))
        self.assertEqual(got["received_1"]["value"], "10")  # 6 + 4 over two receipt lines
        self.assertEqual((got["invoiced_1"]["value"], got["invoiced_1"]["unit"]), ("10", "PCS"))
        self.assertIs(got["match_1"]["value"], True)  # ea -> PCS per Portal's UoM catalogue
        self.assertEqual(got["item_2"]["value"], "TI-2")
        self.assertEqual((got["ordered_2"]["value"], got["received_2"]["value"],
                          got["invoiced_2"]["value"]), ("4", "3", "3"))
        self.assertIs(got["match_2"]["value"], False)  # kgs/kg -> KG; 4 ordered, 3 received
        self.assertIs(got["three_way_match"]["value"], False)
        self.assertEqual(result["answerability"], "answerable")
        invoices = [c for c in result["chain"] if c["relation"] == "bc.purchase_invoices"]
        # Only the vendor-invoice match; the case-different orderNumber look-alike is refused.
        self.assertEqual([(c["key"]["id"], c["fk"]) for c in invoices],
                         [("cccccccc-0000-0000-0000-000000000001",
                           "receipt.bc_purchase_invoice")])
        self.assertEqual(invoices[0]["hop_from"],
                         [c["key"].get("id") for c in result["chain"]].index("r1"))
        self.assertNotIn("FREIGHT", [v["value"] for v in result["values"]])

    def test_missing_invoice_is_a_gap_not_a_match(self):
        result = run("F4", {"identifier": "TPO-2", "identifier_kind": "po_number"}, KNOWN,
                     sources(small_world())).result
        got = values(result)
        self.assertEqual((got["ordered_1"]["value"], got["received_1"]["value"]), ("7", "7"))
        self.assertIsNone(got["invoiced_1"]["value"])
        self.assertIsNone(got["match_1"]["value"])
        self.assertIsNone(got["three_way_match"]["value"])
        self.assertIn("no_invoice_line", reasons(result))
        self.assertEqual(result["answerability"], "partial")

    def test_without_bc_invoices_are_not_ingested(self):
        result = run("F4", {"identifier": "TPO-1", "identifier_kind": "po_number"}, KNOWN,
                     sources(small_world(), bc=False)).result
        self.assertEqual({(g["state"], g["reason"]) for g in result["gaps"]},
                         {("not_ingested", "bc_not_read")})
        self.assertIsNone(values(result)["three_way_match"]["value"])


class BcIdentityTest(unittest.TestCase):
    """A BC number match is identity only inside one company and, for purchase documents,
    for the PO's own vendor (SOURCE-NOTES: number matches are leads)."""

    @staticmethod
    def with_invoice(company: int, vendor: str, quantity: int) -> World:
        world = small_world()
        other = bc_record("purchaseInvoices", company, 7, number="PI-7",
                          vendorInvoiceNumber="VINV-1", vendorNumber=vendor, status="Paid")
        world.bc["purchaseInvoices"].append(other)
        world.bc_lines[other["id"]] = [bc_record(
            "purchaseInvoiceLines", company, 71, documentId=other["id"], lineType="Item",
            lineObjectNumber="TI-1", quantity=quantity, unitOfMeasureCode="PCS")]
        return world

    def test_foreign_vendor_invoice_is_dropped_with_a_gap(self):
        result = run("F4", {"identifier": "TPO-1", "identifier_kind": "po_number"}, KNOWN,
                     sources(self.with_invoice(1, "TV9", 4))).result
        got = values(result)
        self.assertEqual(got["invoiced_1"]["value"], "10")  # not 14: TV9's invoice is not ours
        self.assertIs(got["match_1"]["value"], True)
        self.assertIn("bc_vendor_differs", reasons(result))
        self.assertEqual(result["answerability"], "partial")
        ids = [c["key"]["id"] for c in result["chain"] if c["relation"] == "bc.purchase_invoices"]
        self.assertEqual(ids, ["cccccccc-0000-0000-0000-000000000001"])

    def test_same_vendor_in_two_companies_is_kept_but_not_identity(self):
        result = run("F4", {"identifier": "TPO-1", "identifier_kind": "po_number"}, KNOWN,
                     sources(self.with_invoice(1, "TV1", 4))).result
        got = values(result)
        self.assertEqual(got["invoiced_1"]["value"], "14")  # both kept, flagged
        self.assertIn("bc_match_spans_companies", reasons(result))
        self.assertEqual(result["answerability"], "partial")

    def test_f1_chain_flags_a_number_found_in_two_companies(self):
        world = small_world()
        world.bc["salesInvoices"].append(bc_record(
            "salesInvoices", 1, 8, number="SI-9", externalDocumentNumber="TSO-1",
            status="Paid"))
        result = run("F1", {"identifier": "TSO-1", "identifier_kind": "so_number"}, KNOWN,
                     sources(world)).result
        self.assertIn("bc_match_spans_companies", reasons(result))
        self.assertEqual(result["answerability"], "partial")
        f1 = run("F1", {"identifier": "TPO-1", "identifier_kind": "po_number"}, KNOWN,
                 sources(self.with_invoice(1, "TV9", 4))).result
        self.assertIn("bc_vendor_differs", reasons(f1))
        self.assertNotIn("cccccccc-0000-0000-0001-000000000007",
                         [c["key"].get("id") for c in f1["chain"]])

    def test_unverifiable_vendor_keeps_documents_with_a_gap(self):
        world = small_world()
        world.relations["public.app_supplier"][0]["code"] = "12345"  # FM numeric: no BC No.
        result = run("F4", {"identifier": "TPO-1", "identifier_kind": "po_number"}, KNOWN,
                     sources(world)).result
        self.assertEqual(values(result)["invoiced_1"]["value"], "10")
        self.assertIn("bc_vendor_unverifiable", reasons(result))

    def test_vendor_match_ignores_case_like_bc_codes(self):
        world = small_world()
        world.relations["public.app_supplier"][0]["code"] = "tv1"  # BC stores TV1
        result = run("F4", {"identifier": "TPO-1", "identifier_kind": "po_number"}, KNOWN,
                     sources(world)).result
        self.assertEqual(values(result)["invoiced_1"]["value"], "10")
        self.assertNotIn("bc_vendor_differs", reasons(result))

    def test_numbers_are_trimmed_like_portal(self):
        world = small_world()
        world.relations["public.app_receipt"][0]["vendor_invoice_number"] = " VINV-1 "
        result = run("F4", {"identifier": "TPO-1", "identifier_kind": "po_number"}, KNOWN,
                     sources(world)).result
        self.assertEqual(values(result)["invoiced_1"]["value"], "10")


class ProducedChainTest(unittest.TestCase):
    """Produced follows the Portal's qty_manufactured chain (decision eval-produced-role):
    SUM(status entry qtymanuf) per AO, else yield_actual; labelled derived."""

    def test_status_entries_are_summed_and_yield_actual_is_the_fallback(self):
        world = small_world()
        # AO-T1 (m1, yield_actual 9) has FM AO 501: entries 6 + 2, one without quantity
        # (SQL SUM skips it), so m1 produced 8. AO-T2 (m2) has no FM AO: yield_actual 5.
        world.relations["public.assembly_order_status"] = [
            {"id": 1, "idassembly": 501, "qtymanuf": "6", "audittimestampmodified": T},
            {"id": 2, "idassembly": 501, "qtymanuf": None, "audittimestampmodified": T},
            {"id": 3, "idassembly": 501, "qtymanuf": "2", "audittimestampmodified": T},
            {"id": 4, "idassembly": 502, "qtymanuf": "50", "audittimestampmodified": T}]
        result = run("F3", {"identifier": "TSO-1", "identifier_kind": "so_number"}, KNOWN,
                     sources(world)).result
        produced = values(result)["produced"]
        self.assertEqual((produced["value"], produced["unit"]), ("13", "ea"))
        self.assertEqual((produced["property"], produced["basis"]), ("qty_manufactured",
                                                                    "derived"))
        cited = sorted((c["l1_handle"].split("/")[2], c["field_path"])
                       for c in produced["citations"])
        self.assertEqual(cited, [("public.app_manufacturing_order", "row.yield_actual"),
                                 ("public.assembly_order_status", "row.qtymanuf"),
                                 ("public.assembly_order_status", "row.qtymanuf")])
        chain = result["chain"]
        index = {(c["relation"], str(c["key"]["id"])): i for i, c in enumerate(chain)}
        fm = index[("public.assembly_order", "501")]
        self.assertEqual(chain[fm]["hop_from"], index[("public.app_manufacturing_order", "m1")])
        self.assertEqual({chain[index[("public.assembly_order_status", n)]]["hop_from"]
                          for n in ("1", "2", "3")}, {fm})
        self.assertNotIn(("public.assembly_order_status", "4"), index)
        self.assertEqual(result["answerability"], "answerable")

    def test_without_status_entries_produced_is_yield_actual_labelled_derived(self):
        result = run("F3", {"identifier": "TSO-1", "identifier_kind": "so_number"}, KNOWN,
                     sources(small_world())).result
        produced = values(result)["produced"]
        self.assertEqual((produced["value"], produced["basis"]), ("14", "derived"))
        self.assertEqual({c["field_path"] for c in produced["citations"]}, {"row.yield_actual"})

    def test_no_status_quantity_and_no_yield_is_a_gap_never_planned_quantity(self):
        world = small_world()
        world.relations["public.app_manufacturing_order"][0]["yield_actual"] = None
        result = run("F3", {"identifier": "TSO-1", "identifier_kind": "so_number"}, KNOWN,
                     sources(world)).result
        self.assertIsNone(values(result)["produced"]["value"])
        self.assertIn("qty_manufactured_empty", reasons(result))
        self.assertEqual(result["answerability"], "partial")


class StatusVocabularyTest(unittest.TestCase):
    def test_only_shipped_and_delivered_shipments_have_shipped(self):
        world = small_world()
        world.relations["public.app_shipment"][0]["status"] = "PACKED"
        world.relations["public.app_shipment"][1]["status"] = "DELIVERED"
        result = run("F3", {"identifier": "TSO-1", "identifier_kind": "so_number"}, KNOWN,
                     sources(world)).result
        # s1 (10 + 4) is only packed; s2 (99) was delivered.
        self.assertEqual(values(result)["shipped"]["value"], "99")
        for status in ("PICKING", "CONFIRMED"):
            world.relations["public.app_shipment"][1]["status"] = status
            result = run("F3", {"identifier": "TSO-1", "identifier_kind": "so_number"}, KNOWN,
                         sources(world)).result
            self.assertIsNone(values(result)["shipped"]["value"], status)
            self.assertIn("no_shipment_rows", reasons(result))

    def test_customer_po_status_lists_shipped_and_delivered_shipments_only(self):
        world = small_world()
        world.relations["public.app_shipment"][1]["status"] = "PICKING"
        result = run("F2", {"identifier": "TCPO-9", "identifier_kind": "customer_po"}, KNOWN,
                     sources(world)).result
        self.assertEqual(values(result)["shipment_count_1"]["value"], 1)
        world.relations["public.app_shipment"][1]["status"] = "DELIVERED"
        result = run("F2", {"identifier": "TCPO-9", "identifier_kind": "customer_po"}, KNOWN,
                     sources(world)).result
        self.assertEqual(values(result)["shipment_count_1"]["value"], 2)
        self.assertEqual(values(result)["shipment_statuses_1"]["value"], "DELIVERED,SHIPPED")

    def test_cancelled_assembly_and_draft_shipment_do_not_count(self):
        world = small_world()
        world.relations["public.app_manufacturing_order"][1]["status"] = "Cancelled"
        world.relations["public.app_shipment"][0]["status"] = "DRAFT"
        result = run("F3", {"identifier": "TSO-1", "identifier_kind": "so_number"}, KNOWN,
                     sources(world)).result
        got = values(result)
        self.assertEqual(got["produced"]["value"], "9")  # m2 (5) cancelled, m3 deleted
        self.assertIsNone(got["shipped"]["value"])  # s1 is a draft, s2 cancelled
        self.assertIn("no_shipment_rows", reasons(result))


if __name__ == "__main__":
    unittest.main()


# ----------------------------------------------------------------- findings (dd380a4)

def _rows(world: World, relation: str) -> list:
    return world.relations[relation]


class TruncationIsNeverSilentTest(unittest.TestCase):
    """Finding oracle-f4-silent-value-truncation: 21 items x 5 values + 1 > 100."""

    def world(self) -> World:
        w = small_world()
        rel = w.relations
        invoice = w.bc["purchaseInvoices"][0]
        rel["public.app_product"] = [{"id": f"q{i}", "code": f"TI-{i:02d}", "updated_at": T}
                                     for i in range(1, 22)]
        rel["public.app_purchase_order_line"] = [
            line for line in rel["public.app_purchase_order_line"]
            if line["purchase_order_id"] != "po1"] + [
            {"id": f"pq{i}", "purchase_order_id": "po1", "product_id": f"q{i}",
             "quantity": "10", "quantity_received": "10", "unit_price": "1", "uom": "ea",
             "updated_at": T} for i in range(1, 22)]
        rel["public.app_receipt_line"] = [
            line for line in rel["public.app_receipt_line"] if line["receipt_id"] != "r1"] + [
            {"id": f"rq{i}", "receipt_id": "r1", "product_id": f"q{i}", "quantity": "10",
             "uom": "ea", "updated_at": T} for i in range(1, 22)]
        # item 21 is invoiced 9, the only mismatch, and the last item listed.
        w.bc_lines[invoice["id"]] = [bc_record(
            "purchaseInvoiceLines", 0, 100 + i, documentId=invoice["id"], lineType="Item",
            lineObjectNumber=f"TI-{i:02d}", quantity=(9 if i == 21 else 10),
            unitOfMeasureCode="PCS") for i in range(1, 22)]
        return w

    def test_overall_verdict_is_kept_over_all_items_and_the_answer_is_partial(self):
        result = run("F4", {"identifier": "TPO-1", "identifier_kind": "po_number"}, KNOWN,
                     sources(self.world())).result
        got = values(result)
        self.assertEqual(len(result["values"]), 100)
        self.assertIs(got["three_way_match"]["value"], False)  # item 21 decides it
        self.assertIn("values_truncated", reasons(result))
        self.assertEqual(result["answerability"], "partial")
        gap = next(g for g in result["gaps"] if g["reason"] == "values_truncated")
        self.assertEqual(gap["state"], "unknown")
        self.assertIn("6 of 106", gap["subject"])

    def test_a_verdict_names_every_cell_beyond_max_citations(self):
        """Item 1 received over 12 lines is decided on 14 cells;
        all are the verdict's citations (never the first 8), and a verdict over more cells
        than one calculation states is unknown with a gap, never a cut requirement."""
        from evals.oracle import families
        w = small_world()
        lines = w.relations["public.app_receipt_line"]
        lines[:] = [line for line in lines if line["id"] not in ("rl1", "rl2")] + [
            {"id": f"rl1-{i}", "receipt_id": "r1", "product_id": "p1", "quantity": "1.0",
             "uom": "ea", "updated_at": T} for i in range(10)] + [
            {"id": f"rl1-{i}", "receipt_id": "r1", "product_id": "p1", "quantity": "0.0",
             "uom": "ea", "updated_at": T} for i in range(10, 12)]
        root = {"identifier": "TPO-1", "identifier_kind": "po_number"}
        got = values(run("F4", root, KNOWN, sources(w)).result)
        self.assertIs(got["match_1"]["value"], True)
        cited = got["match_1"]["citations"]
        self.assertEqual(len(cited), 14)
        self.assertEqual(sum("app_receipt_line" in c["l1_handle"] for c in cited), 12)
        cap, families.MAX_VERDICT_CELLS = families.MAX_VERDICT_CELLS, 13
        try:
            result = run("F4", root, KNOWN, sources(w)).result
        finally:
            families.MAX_VERDICT_CELLS = cap
        self.assertIsNone(values(result)["match_1"]["value"])
        self.assertNotIn("citations", values(result)["match_1"])
        self.assertIn("verdict_cells_over_cap", reasons(result))

    def test_under_the_cap_nothing_is_cut(self):
        result = run("F4", {"identifier": "TPO-1", "identifier_kind": "po_number"}, KNOWN,
                     sources(small_world())).result
        self.assertNotIn("values_truncated", reasons(result))
        self.assertEqual(len(result["values"]), 2 * 5 + 1)


class F4UnitMissingTest(unittest.TestCase):
    """Finding oracle-f4-unknown-units-match: a leg without a unit is never matched."""

    def test_null_units_leave_the_verdict_unknown(self):
        w = small_world()
        for table in ("public.app_purchase_order_line", "public.app_receipt_line"):
            for row in _rows(w, table):
                row["uom"] = None
        for row in _rows(w, "public.app_purchase_order_line"):
            if row["id"] == "pl2":
                row["quantity"] = "3.0"  # equal numbers: only the unit rule can stop a match
        for lines in w.bc_lines.values():
            for line in lines:
                line["fields"]["unitOfMeasureCode"] = None
        result = run("F4", {"identifier": "TPO-1", "identifier_kind": "po_number"}, KNOWN,
                     sources(w)).result
        got = values(result)
        self.assertIsNone(got["match_1"]["value"])
        self.assertIsNone(got["match_2"]["value"])
        self.assertIsNone(got["three_way_match"]["value"])
        self.assertEqual(got["ordered_1"]["value"], "10")  # the number itself is still known
        self.assertNotIn("unit", got["ordered_1"])
        self.assertIn("unit_missing", reasons(result))
        self.assertEqual(result["answerability"], "partial")

    def test_one_leg_without_unit_is_enough(self):
        w = small_world()
        for line in w.bc_lines[w.bc["purchaseInvoices"][0]["id"]]:
            if line["fields"]["lineObjectNumber"] == "TI-1":
                line["fields"]["unitOfMeasureCode"] = ""
        result = run("F4", {"identifier": "TPO-1", "identifier_kind": "po_number"}, KNOWN,
                     sources(w)).result
        got = values(result)
        self.assertIsNone(got["match_1"]["value"])
        self.assertIs(got["match_2"]["value"], False)
        self.assertIs(got["three_way_match"]["value"], False)  # one known mismatch decides
        self.assertEqual({g["subject"] for g in result["gaps"] if g["reason"] == "unit_missing"},
                         {"invoiced quantity of item 1"})

    def test_invoice_lines_changed_after_as_known_at_are_stale(self):
        w = small_world()
        w.bc_lines[w.bc["purchaseInvoices"][0]["id"]][0]["last_modified"] = \
            "2099-03-01T00:00:00Z"
        result = run("F4", {"identifier": "TPO-1", "identifier_kind": "po_number"}, KNOWN,
                     sources(w)).result
        self.assertIn("changed_after_as_known_at", reasons(result))


class F3ShipmentWithoutLinesTest(unittest.TestCase):
    """Finding oracle-f3-partial-shipment-lines."""

    def world(self) -> World:
        w = small_world()
        for row in _rows(w, "public.app_shipment"):
            if row["id"] == "s2":
                row["status"] = "SHIPPED"
        w.relations["public.app_shipment_line"] = [
            line for line in _rows(w, "public.app_shipment_line") if line["shipment_id"] != "s2"]
        return w

    def test_one_unlined_shipped_header_makes_the_shipped_total_unknown(self):
        result = run("F3", {"identifier": "TSO-1", "identifier_kind": "so_number"}, KNOWN,
                     sources(self.world())).result
        got = values(result)
        self.assertIsNone(got["shipped"]["value"])  # 14 from s1 alone is a lower bound
        self.assertEqual(result["answerability"], "partial")
        gaps = [g for g in result["gaps"] if g["reason"] == "shipment_without_lines"]
        self.assertEqual(gaps, [{"state": "absent_in_source",
                                 "subject": "shipped quantity of shipment TSH2",
                                 "reason": "shipment_without_lines",
                                 "concerns": "shipped"}])
        self.assertEqual(got["ordered"]["value"], "15")

    def test_all_shipments_without_lines_is_the_shipped_value_s_one_gap(self):
        """One gap state per value, never also no_shipment_rows."""
        w = small_world()
        w.relations["public.app_shipment_line"] = []
        result = run("F3", {"identifier": "TSO-1", "identifier_kind": "so_number"}, KNOWN,
                     sources(w)).result
        self.assertEqual([g for g in result["gaps"] if g.get("concerns") == "shipped"],
                         [{"state": "absent_in_source", "subject": "shipped quantity",
                           "reason": "shipment_without_lines", "concerns": "shipped"}])
        self.assertNotIn("no_shipment_rows", reasons(result))
        self.assertIsNone(values(result)["shipped"]["value"])


# ----------------------------------------------------------------- held-out families

PO1 = {"identifier": "TPO-1", "identifier_kind": "po_number"}


class F5UnmatchedInvoicesTest(unittest.TestCase):
    """Hand-derived: PO 1's only own-vendor invoice PI-1 carries VINV-1, which receipt r1
    records; the case-different orderNumber look-alike PI-2 is refused."""

    def test_invoice_tied_to_a_receipt_is_matched(self):
        result = run("F5", PO1, KNOWN, sources(small_world())).result
        got = values(result)
        self.assertEqual(got["invoice_count"]["value"], 1)
        self.assertEqual(got["invoice_1"]["value"], "PI-1")
        self.assertIs(got["unmatched_1"]["value"], False)
        self.assertEqual(got["unmatched_invoice_count"]["value"], 0)
        self.assertEqual(result["answerability"], "answerable")
        self.assertTrue(got["invoice_1"]["citations"][0]["l1_handle"].startswith("bc://"))

    def add_invoice(self, w: World, **fields) -> None:
        invoice = bc_record("purchaseInvoices", 0, 7, number="PI-7", vendorNumber="TV1",
                            status="Paid", orderNumber="TPO-1", **fields)
        w.bc["purchaseInvoices"].append(invoice)

    def test_invoice_no_receipt_records_is_unmatched(self):
        w = small_world()
        self.add_invoice(w, vendorInvoiceNumber="VINV-7")
        result = run("F5", PO1, KNOWN, sources(w)).result
        got = values(result)
        self.assertEqual((got["invoice_1"]["value"], got["unmatched_1"]["value"]), ("PI-1", False))
        self.assertEqual((got["invoice_2"]["value"], got["unmatched_2"]["value"]), ("PI-7", True))
        self.assertEqual(got["unmatched_invoice_count"]["value"], 1)
        self.assertEqual(result["answerability"], "answerable")

    def test_trimmed_exact_text_like_portal(self):
        w = small_world()
        self.add_invoice(w, vendorInvoiceNumber=" vinv-1 ")  # case differs: not the same No.
        for row in _rows(w, "public.app_receipt"):
            if row["id"] == "r1":
                row["vendor_invoice_number"] = "VINV-1 "
        got = values(run("F5", PO1, KNOWN, sources(w)).result)
        self.assertIs(got["unmatched_1"]["value"], False)  # "VINV-1 " trims to VINV-1
        self.assertIs(got["unmatched_2"]["value"], True)

    def test_receipt_without_a_vendor_invoice_number_makes_it_unknown(self):
        w = small_world()
        self.add_invoice(w, vendorInvoiceNumber="VINV-7")
        _rows(w, "public.app_receipt").append(
            {"id": "r9", "receipt_number": "TR-9", "purchase_order_id": "po1",
             "status": "RELEASED", "vendor_invoice_number": "  ", "updated_at": T})
        result = run("F5", PO1, KNOWN, sources(w)).result
        got = values(result)
        self.assertIs(got["unmatched_1"]["value"], False)
        self.assertIsNone(got["unmatched_2"]["value"])
        self.assertIsNone(got["unmatched_invoice_count"]["value"])
        self.assertIn("receipt_invoice_number_empty", reasons(result))
        self.assertEqual(result["answerability"], "partial")

    def test_invoice_without_vendor_invoice_number_is_unknown(self):
        w = small_world()
        self.add_invoice(w)
        result = run("F5", PO1, KNOWN, sources(w)).result
        self.assertIsNone(values(result)["unmatched_2"]["value"])
        self.assertIn("vendor_invoice_number_empty", reasons(result))

    def test_without_bc_nothing_is_counted(self):
        result = run("F5", PO1, KNOWN, sources(small_world(), bc=False)).result
        got = values(result)
        self.assertIsNone(got["invoice_count"]["value"])
        self.assertIsNone(got["unmatched_invoice_count"]["value"])
        self.assertIn("bc_not_read", reasons(result))
        self.assertEqual(result["answerability"], "partial")

    def test_other_roots_are_refused(self):
        result = run("F5", {"identifier": "TSO-1", "identifier_kind": "so_number"}, KNOWN,
                     sources(small_world())).result
        self.assertEqual(result["answerability"], "blocked")


SO1 = {"identifier": "TSO-1", "identifier_kind": "so_number"}


class F6ShippedShortTest(unittest.TestCase):
    """Hand-derived: TSO-1 orders TI-1 10 ea and TI-2 5 ea; shipment s1 ships 10 and 4
    (cancelled s2 with 99 does not count), so TI-2 shipped short by 1."""

    def test_short_item_and_overall_verdict(self):
        result = run("F6", SO1, KNOWN, sources(small_world())).result
        got = values(result)
        self.assertEqual((got["item_1"]["value"], got["ordered_1"]["value"],
                          got["shipped_1"]["value"], got["short_1"]["value"]),
                         ("TI-1", "10", "10", False))
        self.assertEqual((got["item_2"]["value"], got["ordered_2"]["value"],
                          got["shipped_2"]["value"], got["short_2"]["value"]),
                         ("TI-2", "5", "4", True))
        self.assertEqual(got["short_item_count"]["value"], 1)
        self.assertIs(got["shipped_short"]["value"], True)
        self.assertEqual(got["shipped_2"]["quantity_role"], "shipped")
        self.assertEqual(result["answerability"], "answerable")

    def test_full_and_over_shipment_is_not_short(self):
        w = small_world()
        for row in _rows(w, "public.app_shipment_line"):
            if row["id"] == "sl2":
                row["quantity"] = "6.0"
        got = values(run("F6", SO1, KNOWN, sources(w)).result)
        self.assertIs(got["short_2"]["value"], False)
        self.assertIs(got["shipped_short"]["value"], False)
        self.assertEqual(got["short_item_count"]["value"], 0)

    def test_an_ordered_item_never_shipped_is_short(self):
        w = small_world()
        w.relations["public.app_shipment_line"] = [
            line for line in _rows(w, "public.app_shipment_line") if line["id"] != "sl2"]
        got = values(run("F6", SO1, KNOWN, sources(w)).result)
        self.assertEqual((got["shipped_2"]["value"], got["short_2"]["value"]), ("0", True))

    def test_different_units_are_never_compared(self):
        w = small_world()
        for row in _rows(w, "public.app_shipment_line"):
            if row["id"] == "sl2":
                row["uom"] = "cse"
        result = run("F6", SO1, KNOWN, sources(w)).result
        got = values(result)
        self.assertIsNone(got["short_2"]["value"])
        self.assertIsNone(got["short_item_count"]["value"])
        self.assertIsNone(got["shipped_short"]["value"])  # no known short, one unknown
        self.assertIn("unit_differs", reasons(result))

    def test_unit_aliases_compare_like_portal(self):
        w = small_world()
        for row in _rows(w, "public.app_shipment_line"):
            row["uom"] = "Each"
        got = values(run("F6", SO1, KNOWN, sources(w)).result)
        self.assertIs(got["short_2"]["value"], True)

    def test_missing_unit_is_unknown(self):
        w = small_world()
        for row in _rows(w, "public.app_sales_order_line"):
            if row["id"] == "l1":
                row["uom"] = None
        result = run("F6", SO1, KNOWN, sources(w)).result
        self.assertIsNone(values(result)["short_1"]["value"])
        self.assertIs(values(result)["shipped_short"]["value"], True)  # TI-2 is known short
        self.assertIn("unit_missing", reasons(result))

    def test_unlined_shipment_or_no_shipment_rows_is_unknown(self):
        w = F3ShipmentWithoutLinesTest().world()
        result = run("F6", SO1, KNOWN, sources(w)).result
        got = values(result)
        self.assertIsNone(got["shipped_1"]["value"])
        self.assertIsNone(got["shipped_short"]["value"])
        self.assertIn("shipment_without_lines", reasons(result))
        none = run("F6", {"identifier": "TSO-3", "identifier_kind": "so_number"}, KNOWN,
                   sources(small_world())).result
        self.assertIn(("not_ingested", "no_shipment_rows"),
                      {(g["state"], g["reason"]) for g in none["gaps"]})
        self.assertEqual(none["answerability"], "partial")

    def test_other_roots_are_refused(self):
        result = run("F6", PO1, KNOWN, sources(small_world())).result
        self.assertEqual(result["answerability"], "blocked")


class ReviewRepairTest(unittest.TestCase):
    """F6 unresolved lines; F5 trims like SQL TRIM."""

    def test_f6_unresolved_ordered_line_blocks_a_negative(self):
        w = small_world()
        _rows(w, "public.app_sales_order_line").append(
            {"id": "l9", "sales_order_id": "so1", "product_id": "p-unknown", "quantity": "7.0",
             "uom": "ea", "updated_at": T})
        for row in _rows(w, "public.app_shipment_line"):
            if row["id"] == "sl2":
                row["quantity"] = "5.0"  # every resolved item shipped in full
        result = run("F6", SO1, KNOWN, sources(w)).result
        got = values(result)
        self.assertIs(got["short_1"]["value"], False)
        self.assertIs(got["short_2"]["value"], False)
        self.assertIsNone(got["short_item_count"]["value"])
        self.assertIsNone(got["shipped_short"]["value"])  # the 7 ea line may never have shipped
        self.assertIn("item_code_unresolved", reasons(result))
        self.assertEqual(result["answerability"], "partial")

    def test_f6_known_short_stays_true_beside_an_unresolved_line(self):
        w = small_world()
        _rows(w, "public.app_sales_order_line").append(
            {"id": "l9", "sales_order_id": "so1", "product_id": None, "quantity": "7.0",
             "uom": "ea", "updated_at": T})
        got = values(run("F6", SO1, KNOWN, sources(w)).result)
        self.assertIs(got["shipped_short"]["value"], True)  # TI-2 is short regardless
        self.assertIsNone(got["short_item_count"]["value"])

    def test_f5_trims_spaces_only_like_sql_trim(self):
        w = small_world()
        w.bc["purchaseInvoices"].append(bc_record(
            "purchaseInvoices", 0, 7, number="PI-7", vendorNumber="TV1", status="Paid",
            orderNumber="TPO-1", vendorInvoiceNumber="VINV-7"))
        _rows(w, "public.app_receipt").append(
            {"id": "r9", "receipt_number": "TR-9", "purchase_order_id": "po1",
             "status": "RELEASED", "vendor_invoice_number": "VINV-7\t", "updated_at": T})
        got = values(run("F5", PO1, KNOWN, sources(w)).result)
        # SQL TRIM keeps the tab, so Portal does not tie VINV-7 to that receipt.
        self.assertIs(got["unmatched_2"]["value"], True)
        _rows(w, "public.app_receipt")[-1]["vendor_invoice_number"] = "  VINV-7  "
        got = values(run("F5", PO1, KNOWN, sources(w)).result)
        self.assertIs(got["unmatched_2"]["value"], False)


class F6UnresolvedShipmentLineTest(unittest.TestCase):
    """TI-2 (ordered 5 ea) shipped in full on a line whose product
    does not resolve; the oracle must not call it short."""

    def test_unresolved_shipped_line_makes_totals_lower_bounds(self):
        w = small_world()
        for row in _rows(w, "public.app_shipment_line"):
            if row["id"] == "sl2":
                row["product_id"] = "p-unknown"
                row["quantity"] = "5.0"
        result = run("F6", SO1, KNOWN, sources(w)).result
        got = values(result)
        self.assertEqual(got["item_2"]["value"], "TI-2")
        self.assertIsNone(got["short_2"]["value"])        # was a definite True
        self.assertIsNone(got["shipped_2"]["value"])      # "0" is only a lower bound
        self.assertIs(got["short_1"]["value"], False)     # 10 shipped >= 10 still holds
        self.assertIsNone(got["shipped_1"]["value"])
        self.assertIsNone(got["shipped_short"]["value"])
        self.assertIsNone(got["short_item_count"]["value"])
        self.assertIn("shipped_item_unresolved", reasons(result))
        self.assertEqual(result["answerability"], "partial")
