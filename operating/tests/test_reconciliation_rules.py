"""REC-10 reconciliation rule: BC External Document No. vs the customer PO.

Evidence label: ``injected-transport``. BC reads go through the in-process ``FakeSender``
(no network, no credentials); the Portal side is plain fixture rows shaped after the
2026-09-30 live observations (``po_reference`` sometimes holds a person's name).
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from devloop import live_verify as lv
from integrations import bc_read
from integrations.bc_read import BusinessCentralReader
from integrations.live_source_guard import LiveSourceAudience
from integrations.source_mirror import SourceMirror
from integrations.tests.test_bc_read_entities import BASE as BASE_BC, Fixture, doc, filtered, gid
from integrations.tests.test_source_mirror import BASE, KEY, Clock, FakeWorkbench
from integrations.workbench_read_api import WorkbenchReadApiClient
from integrations.tests.test_live_source_adapters import COMPANY, ENV
from integrations.tests.test_live_source_adapters import FakeSender
from ontology.core import Actor
from operating import reconciliation_rules as rr

FINANCE = Actor("plx", frozenset({"staff:finance"}), "restricted")
NOBODY = Actor("plx", frozenset({"staff:nobody"}), "restricted")
PO = "PO12172025"
NUMBERS = {"order_number": "SO100351"}


def po_candidates(po_reference=PO, cust=None):
    out = [{"source": "po_reference", "value": po_reference}]
    if cust is not None:
        out.append({"source": "custom_fields.custPoCode", "value": cust})
    return out


def bc_reader(sales_orders=(), sales_invoices=()):
    c1 = f"{BASE_BC}/companies({COMPANY})"
    sender = FakeSender({
        f"{c1}/salesOrders": filtered(list(sales_orders)),
        f"{c1}/salesInvoices": filtered(list(sales_invoices))})
    api, _, tenant, environment = bc_read.build_bc(ENV, sender=sender)
    audience = LiveSourceAudience({"bc:production:*": ["staff:finance", "staff:operations"]},
                                  tenant="plx")
    return BusinessCentralReader(api, audience, tenant_id=tenant, environment=environment), sender


def classify(bc_value, **kw):
    kw.setdefault("candidates", po_candidates())
    kw.setdefault("order_numbers", NUMBERS)
    kw.setdefault("shipment_numbers", ["SHP-17"])
    return rr.classify_external_document(bc_value, **kw)


class ClassifyTests(unittest.TestCase):
    def test_matches_customer_po(self):
        got = classify(PO)
        self.assertEqual((got["class"], got["case_exact"]), ("matches_customer_po", True))
        self.assertEqual(classify("po12172025")["class"], "matches_customer_po")
        self.assertFalse(classify("po12172025")["case_exact"])

    def test_overwrite_by_ship_only_bridge(self):
        got = classify("SHP-17")
        self.assertEqual(got["class"], "matches_shipment_number")
        self.assertEqual(got["reason"], "ship_only_bridge_overwrite")
        self.assertTrue(got["customer_po_lost_in_bc"])

    def test_overwrite_without_portal_po_is_not_a_lost_po(self):
        got = classify("SHP-17", candidates=po_candidates(None))
        self.assertEqual(got["class"], "matches_shipment_number")
        self.assertFalse(got["customer_po_lost_in_bc"])

    def test_blank(self):
        for value in (None, "", "   "):
            self.assertEqual(classify(value)["class"], "blank")

    def test_unrelated_needs_a_complete_comparison(self):
        self.assertEqual(classify("ZZ-9")["class"], "unrelated")

    def test_person_name_po_reference_trap(self):
        # Portal sent "Jane Buyer" as the "PO" (no shape check at serializers.ts:1157).
        got = classify("Jane Buyer", candidates=po_candidates("Jane Buyer"))
        self.assertEqual((got["class"], got["reason"]),
                         ("unknown", "equals_not_po_like_portal_field"))
        self.assertFalse(got["portal_po_like_known"])
        # And the name is never counted as a customer PO for a different BC value either.
        other = classify("PO777", candidates=po_candidates("Jane Buyer"))
        self.assertEqual((other["class"], other["reason"]),
                         ("unknown", "no_po_like_portal_candidate"))

    def test_bc_value_is_never_taken_as_the_customer_po(self):
        got = classify("PO999", candidates=po_candidates(None))
        self.assertEqual(got["class"], "unknown")
        self.assertNotIn("customer_po", json.dumps(got))

    def test_shipment_numbers_not_read_is_unknown(self):
        got = classify("ZZ-9", shipment_numbers=None)
        self.assertEqual((got["class"], got["reason"]), ("unknown", "shipment_numbers_not_read"))
        lead = classify("SHP-88", shipment_numbers=None)
        self.assertTrue(lead["looks_like_ship_fallback"])
        self.assertEqual(lead["class"], "unknown")

    def test_matches_both_po_and_shipment_is_unknown(self):
        got = classify("SHP-17", candidates=po_candidates("SHP-17"))
        self.assertEqual((got["class"], got["reason"]), ("unknown", "matches_po_and_shipment"))

    def test_order_number_is_not_a_po(self):
        got = classify("SO100351", candidates=po_candidates("SO100351"))
        self.assertEqual(got["class"], "matches_order_number")
        self.assertFalse(got["portal_po_like_known"])

    def test_not_inspectable_candidate_is_unknown(self):
        got = classify("ZZ-9", candidates=[{"source": "po_reference",
                                            "exists": "column_not_mirrored", "value": None}])
        self.assertEqual((got["class"], got["reason"]),
                         ("unknown", "portal_field_not_inspectable"))

    def test_bc_cap_of_35_characters(self):
        long_po = "P" + "1" * 39
        self.assertEqual(classify(long_po[:35], candidates=po_candidates(long_po))["class"],
                         "matches_customer_po")


class ReconcileOrderTests(unittest.TestCase):
    def run_one(self, reader, **kw):
        kw.setdefault("order_number", "SO100351")
        kw.setdefault("candidates", po_candidates())
        kw.setdefault("order_numbers", NUMBERS)
        kw.setdefault("shipment_numbers", ["SHP-17"])
        kw.setdefault("portal_citations", ["api://public.app_sales_order/1"])
        return rr.reconcile_order(reader, FINANCE, COMPANY, **kw)

    def test_each_class_from_bc_sales_orders(self):
        cases = {PO: "matches_customer_po", "SHP-17": "matches_shipment_number",
                 "ZZ-9": "unrelated", "SO100351": "matches_order_number"}
        for value, expected in cases.items():
            reader, sender = bc_reader([doc(2, number="SO100351", externalDocumentNumber=value)])
            (finding,) = self.run_one(reader)
            self.assertEqual(finding["class"], expected, value)
            self.assertEqual(finding["schema"], rr.SCHEMA)
            self.assertFalse(finding["identity_asserted"])
            self.assertEqual(finding["evidence_label"], "injected-transport")
            self.assertTrue(any(c.startswith("bc://") for c in finding["citations"]))
            self.assertIn("api://public.app_sales_order/1", finding["citations"])
            self.assertTrue(all(call[0] == "GET" for call in sender.api_calls()))
        reader, _ = bc_reader([doc(2, number="SO100351")])
        (blank,) = self.run_one(reader)
        self.assertEqual(blank["class"], "blank")

    def test_unrelated_value_is_withheld(self):
        reader, _ = bc_reader([doc(2, number="SO100351", externalDocumentNumber="Jane Q")])
        (finding,) = self.run_one(reader)
        self.assertEqual(finding["class"], "unrelated")
        self.assertTrue(finding["bc_value_withheld"])
        self.assertNotIn("Jane Q", json.dumps(finding))

    def test_upstream_citations_name_the_overwrite(self):
        reader, _ = bc_reader([doc(2, number="SO100351", externalDocumentNumber="SHP-17")])
        (finding,) = self.run_one(reader)
        paths = {c["path"]: c for c in finding["upstream_citations"]}
        bridge_path = "bc-extensions/plx-receipt-bridge/src/PlxSalesBridge.Codeunit.al"
        bridge = [c for c in finding["upstream_citations"] if c["path"] == bridge_path
                  and c["commit"] == "8802bee"]
        self.assertEqual(len(bridge), 1)
        self.assertIn("overwrites", bridge[0]["fact"])
        self.assertIn("portal/src/lib/business-central/serializers.ts", paths)
        bridge_now = [c for c in finding["upstream_citations"] if c["commit"] == "1c5dddb"]
        self.assertEqual(sorted(c["lines"] for c in bridge_now), ["185-186", "289-294"])
        bridge_now = {c["lines"]: c for c in bridge_now}
        self.assertIn("no longer overwrites", bridge_now["185-186"]["fact"])
        self.assertIn("copies", bridge_now["289-294"]["fact"])

    def test_not_found_and_not_unique_are_unknown(self):
        reader, _ = bc_reader([])
        (none,) = self.run_one(reader)
        self.assertEqual((none["class"], none["reason"]), ("unknown", "bc_document_not_found"))
        reader, _ = bc_reader([doc(2, number="SO100351", externalDocumentNumber=PO),
                               doc(3, number="so100351", externalDocumentNumber=PO)])
        (many,) = self.run_one(reader)
        self.assertEqual(many["reason"], "bc_document_not_unique_or_withheld")

    def test_order_dates_compare_calendar_days_in_each_zone(self):
        """BC ``orderDate`` is the UTC day of the Portal instant (``toIsoDate``,
        serializers.ts@549f6f0:1036-1041): a zoned Portal timestamp is compared as its UTC
        day, a BC date as written. A Portal timestamp with no zone reads as UTC (the
        catalog's timestamp-utc mode, ``customer_po_coverage.parse_date_value``), never as
        local time; a BC value that is not a plain date is never given a day."""
        cases = (("2026-09-01T22:30:00-05:00", "2026-09-02", "matches_customer_po"),
                 ("2026-09-02T03:00:00+00", "2026-09-02", "matches_customer_po"),
                 ("2026-09-02T00:00:00.000Z", "2026-09-02", "matches_customer_po"),
                 ("2026-09-02", "2026-09-02", "matches_customer_po"),
                 (None, "2026-09-02", "matches_customer_po"),
                 ("2026-09-02T22:30:00-05:00", "2026-09-02", "order_date_conflict"),
                 ("2026-09-01T00:00:00Z", "2026-09-02", "order_date_conflict"),
                 ("2026-09-02T10:00:00", "2026-09-02", "matches_customer_po"),
                 ("2026-09-02T23:30:00", "2026-09-02", "matches_customer_po"),
                 ("2026-09-01T23:30:00", "2026-09-02", "order_date_conflict"),
                 ("2026-09-02T10:00:00Z", "2026-09-02T10:00:00", "order_date_not_comparable"),
                 ("2026-02-30", "2026-02-30", "order_date_not_comparable"),
                 ("next Tuesday", "2026-09-02", "order_date_not_comparable"))
        for portal, bc, expected in cases:
            reader, _ = bc_reader([doc(2, number="SO100351", externalDocumentNumber=PO,
                                       orderDate=bc)])
            (finding,) = self.run_one(reader, order_date=portal)
            self.assertEqual(finding["reason"] if finding["class"] == "unknown"
                             else finding["class"], expected, (portal, bc))

    def test_not_permitted_reads_nothing(self):
        reader, sender = bc_reader([doc(2, number="SO100351", externalDocumentNumber=PO)])
        (finding,) = rr.reconcile_order(reader, NOBODY, COMPANY, order_number="SO100351",
                                        candidates=po_candidates())
        self.assertEqual(finding["reason"], "bc_not_permitted")
        self.assertEqual(sender.api_calls(), [])

    def invoices(self, ext, date, map_ids):
        inv = [doc(5, number="PSI-1", orderNumber="SO100351", externalDocumentNumber=ext,
                   invoiceDate=date)]
        reader, _ = bc_reader(sales_invoices=inv)
        return self.run_one(reader, entity_set="salesInvoices", invoice_map_ids=map_ids)

    def test_portal_possible_invoice_accepts_any_known_carrier(self):
        cases = (("SO100351", "2026-10-05", {gid(5)}, "matches_order_number"),
                 ("SHP-17", "2026-10-05", {gid(5)}, "matches_shipment_number"),
                 (PO, "2026-09-20", None, "matches_customer_po"),
                 ("SHP-17", "2026-09-20", None, "matches_shipment_number"),
                 ("SO100351", "2026-09-20", None, "matches_order_number"))
        for ext, date, map_ids, expected in cases:
            (finding,) = self.invoices(ext, date, map_ids)
            self.assertEqual((finding["class"], finding["authorship"]),
                             (expected, "portal_possible"), (ext, date))

    def test_portal_possible_no_match_is_never_unrelated(self):
        for date, map_ids in (("2026-09-20", None), ("2026-10-05", {gid(5)})):
            (finding,) = self.invoices("ZZ-9", date, map_ids)
            self.assertEqual((finding["class"], finding["reason"]),
                             ("unknown", "unmatched_portal_possible"))

    def test_bc_native_invoice_matching_shipment_number(self):
        (finding,) = self.invoices("SHP-17", "2026-10-05", set())
        self.assertEqual((finding["class"], finding["authorship"]),
                         ("matches_shipment_number", "bc_native"))

    def test_bc_native_invoice_mismatch_is_not_a_serializer_verdict(self):
        (finding,) = self.invoices("SO100351", "2026-10-05", set())
        self.assertEqual(finding["authorship"], "bc_native")
        self.assertNotEqual(finding["reason"], "portal_serializer_order_number")
        (other,) = self.invoices("ZZ-9", "2026-10-05", set())
        self.assertEqual((other["class"], other["authorship"]), ("unrelated", "bc_native"))

    def test_unknown_authorship_is_unknown_never_a_mismatch(self):
        for ext, date, map_ids in (("ZZ-9", "2026-10-05", None), ("ZZ-9", None, set()),
                                   ("SHP-17", "2026-10-05", None)):
            (finding,) = self.invoices(ext, date, map_ids)
            self.assertEqual((finding["class"], finding["reason"], finding["authorship"]),
                             ("unknown", "invoice_authorship_unknown", "unknown"))
        self.assertEqual(rr.summarize([finding])["invoice_authorship"], {"unknown": 1})

    def test_bad_entity_set(self):
        reader, _ = bc_reader()
        with self.assertRaises(ValueError):
            self.run_one(reader, entity_set="purchaseOrders")

    def test_summary_counts_per_class(self):
        findings = []
        for value in (PO, "SHP-17", "SHP-17", None, "ZZ-9"):
            reader, _ = bc_reader([doc(2, number="SO100351", externalDocumentNumber=value)]
                                  if value is not None else [doc(2, number="SO100351")])
            findings += self.run_one(reader)
        summary = rr.summarize(findings)
        self.assertEqual(summary["by_class"], {
            "matches_customer_po": 1, "matches_shipment_number": 2,
            "matches_order_number": 0, "blank": 1, "unrelated": 1, "unknown": 0})
        self.assertEqual(summary["shipment_overwrite_with_portal_po_known"], 2)
        self.assertEqual(summary["findings"], 5)


COMPANY2 = "0f0e0d0c-0b0a-0908-0706-0504030201ff"
OPS = Actor("plx", frozenset({"staff:operations"}), "restricted")


class RecentOrdersTests(unittest.TestCase):
    """Real ``SourceMirror`` over the source-mirror tests' ``FakeWorkbench`` (so the
    mirror's limit/where/audience validation runs); BC through the injected sender."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.fake = FakeWorkbench()
        orders = dict(self.fake.catalog["public.app_sales_order"])
        orders["columns"] = [*orders["columns"], "order_date"]
        self.fake.catalog["public.app_sales_order"] = orders
        self.fake.catalog["public.app_shipment"] = {
            "columns": ["id", "shipment_code", "sales_order_id"], "key": ["id"],
            "domain": "orders", "since": None}
        self.fake.tables["public.app_sales_order"] = [
            {"id": 1, "order_number": "SO1", "po_reference": "PO-5501",
             "total_amount": "1", "custom_fields": None, "updated_at": "2026-09-29T09:00:00"},
            {"id": 2, "order_number": "SO2", "po_reference": "Jane Buyer",
             "total_amount": "1", "custom_fields": None, "updated_at": "2026-09-29T08:00:00"},
            {"id": 3, "order_number": "SO3", "po_reference": "PO-7",
             "total_amount": "1", "custom_fields": None, "updated_at": "2026-09-29T07:00:00"}]
        self.fake.tables["public.app_shipment"] = [
            {"id": 9, "shipment_code": "SHP-9", "sales_order_id": 3}]

    def mirror(self, audience=None):
        client = WorkbenchReadApiClient(BASE, KEY, fetch=self.fake)
        self.mirrors = getattr(self, "mirrors", 0) + 1  # a fresh store per call
        mirror = SourceMirror(Path(self.dir.name) / f"m{self.mirrors}.sqlite", client,
                              audience or {"public.*": ["staff:operations"]},
                              clock=Clock(), retry_delay_seconds=0)
        self.addCleanup(mirror.close)
        return mirror

    def test_bounded_run_classifies_portal_orders_against_bc(self):
        so = [doc(1, number="SO1", externalDocumentNumber="PO-5501"),
              doc(2, number="SO2", externalDocumentNumber="Jane Buyer"),
              doc(3, number="SO3", externalDocumentNumber="SHP-9")]
        reader, sender = bc_reader(so)
        result = rr.reconcile_recent_orders(self.mirror(), reader, OPS, [COMPANY], limit=3)
        by_number = {f["subject"]["order_number"]: f for f in result["findings"]}
        self.assertTrue(result["shipments_read"], result["shipments_unread_reason"])
        self.assertEqual(by_number["SO1"]["class"], "matches_customer_po")
        self.assertEqual(by_number["SO1"]["shipment_count"], 0)
        self.assertEqual(by_number["SO2"]["reason"], "equals_not_po_like_portal_field")
        self.assertEqual(by_number["SO3"]["class"], "matches_shipment_number")
        self.assertTrue(any(c.startswith("api://public.app_shipment")
                            for c in by_number["SO3"]["citations"]))
        self.assertEqual(result["summary"]["findings"], 3)
        self.assertTrue(all(c[0] == "GET" for c in sender.api_calls()))
        with self.assertRaises(ValueError):
            rr.reconcile_recent_orders(self.mirror(), reader, OPS, [COMPANY], limit=51)

    def test_unrelated_is_reachable_on_the_real_mirror(self):
        reader, _ = bc_reader([doc(1, number="SO1", externalDocumentNumber="ZZ-9")])
        result = rr.reconcile_recent_orders(self.mirror(), reader, OPS, [COMPANY], limit=1)
        self.assertEqual(result["findings"][0]["class"], "unrelated")

    def test_shipments_are_paged_beyond_one_mirror_page(self):
        self.fake.tables["public.app_shipment"] = [
            {"id": n, "shipment_code": f"SHP-{n}", "sales_order_id": 3}
            for n in range(1, 521)]
        reader, _ = bc_reader([doc(3, number="SO3", externalDocumentNumber="SHP-520")])
        result = rr.reconcile_recent_orders(self.mirror(), reader, OPS, [COMPANY], limit=3)
        self.assertTrue(result["shipments_read"])
        (finding,) = [f for f in result["findings"] if f["subject"]["order_number"] == "SO3"]
        self.assertEqual((finding["class"], finding["shipment_count"]),
                         ("matches_shipment_number", 520))

    def test_unreadable_shipments_stay_unread_not_unrelated(self):
        reader, _ = bc_reader([doc(1, number="SO1", externalDocumentNumber="ZZ-9")])
        mirror = self.mirror({"public.app_sales_order": ["staff:operations"]})
        result = rr.reconcile_recent_orders(mirror, reader, OPS, [COMPANY], limit=1)
        self.assertFalse(result["shipments_read"])
        self.assertTrue(result["shipments_unread_reason"].startswith("mirror_refused"))
        self.assertEqual(result["findings"][0]["reason"], "shipment_numbers_not_read")

    def test_one_finding_per_order_across_companies(self):
        c2 = f"{BASE_BC}/companies({COMPANY2})"
        reader, sender = bc_reader([doc(1, number="SO1", externalDocumentNumber="PO-5501")])
        sender.routes[f"{c2}/salesOrders"] = filtered([])
        result = rr.reconcile_recent_orders(self.mirror(), reader, OPS, [COMPANY, COMPANY2],
                                            limit=1)
        self.assertEqual([f["class"] for f in result["findings"]], ["matches_customer_po"])
        # Held by both companies: ambiguous, never classified.
        sender.routes[f"{c2}/salesOrders"] = filtered(
            [doc(7, number="SO1", externalDocumentNumber="PO-5501")])
        both = rr.reconcile_recent_orders(self.mirror(), reader, OPS, [COMPANY, COMPANY2],
                                          limit=1)
        self.assertEqual([(f["class"], f["reason"]) for f in both["findings"]],
                         [("unknown", "ambiguous_across_companies")])
        # An incomplete number index (truncated here) never proves the number absent:
        # that company still gets the lookup, which finds it.
        sender.routes[f"{c2}/salesOrders"] = filtered(
            [doc(8, number="SO8"), doc(9, number="SO9"), doc(7, number="so1")])
        with mock.patch.object(rr, "INDEX_PAGE", 2), mock.patch.object(rr, "INDEX_MAX_PAGES", 1):
            partial = rr.reconcile_recent_orders(self.mirror(), reader, OPS,
                                                 [COMPANY, COMPANY2], limit=1)
        self.assertEqual(sorted(f["reason"] for f in partial["findings"]),
                         ["ambiguous_across_companies", "bc_value_empty"])  # + SO8, blank
        self.assertEqual(partial["bc_sample"]["index_incomplete"], 1)

    def test_live_shaped_sample_reaches_orders_bc_holds(self):
        """Live 2026-10-08: the ten newest replica orders were never drained to BC
        (none_found 10/10). BC holds an older replica order and UAT-numbered ones.
        Live 2026-10-14: 20/20 not_in_served_replica, classified 0, because the replica
        join came before the BC value: classes BC alone settles (blank; the order's own
        number) are reported for every sampled order first, never joined."""
        rows = self.fake.tables["public.app_sales_order"]
        rows[0]["order_date"] = "2026-09-01T00:00:00Z"
        rows += [{"id": 100 + n, "order_number": f"OR{n}", "po_reference": f"PO-9{n}",
                  "total_amount": "1", "custom_fields": None,
                  "updated_at": f"2026-10-0{n % 7 + 1}T09:00:00"} for n in range(10)]
        so = [doc(30 + n, number=f"UAT-SO{n}", externalDocumentNumber=f"UAT-SH{n}")
              for n in range(3)]
        so += [doc(3, number="SO3", externalDocumentNumber="PO-7"),
               doc(1, number="SO1", externalDocumentNumber="PO-5501", orderDate="2026-09-02"),
               doc(2, number="SO2"),
               doc(40, number="UAT-SO8"),
               doc(41, number="UAT-SO9", externalDocumentNumber="uat-so9")]
        reader, sender = bc_reader(so)
        result = rr.reconcile_recent_orders(self.mirror(), reader, OPS, [COMPANY], limit=10)
        got = {f["subject"]["order_number"]: (f["class"], f.get("reason"))
               for f in result["findings"]}
        self.assertTrue(result["shipments_read"])
        # Newest first: the sample asks BC for orderDate desc.
        queries = [parse_qs(urlsplit(c[1]).query) for c in sender.api_calls()]
        (sample,) = [q for q in queries if q.get("$top") == ["10"]]
        self.assertEqual(sample["$orderby"], ["orderDate desc"])
        self.assertEqual(got["SO3"][0], "matches_customer_po")
        # BC alone settles these, held by the replica (SO2) or not (UAT-SO8/9).
        self.assertEqual(got["SO2"], ("blank", "bc_value_empty"))
        self.assertEqual(got["UAT-SO8"], ("blank", "bc_value_empty"))
        self.assertEqual(got["UAT-SO9"], ("matches_order_number", "equals_bc_document_number"))
        (own,) = [f for f in result["findings"] if f["subject"]["order_number"] == "UAT-SO9"]
        self.assertFalse(any(c.startswith("api://") for c in own["citations"]))
        self.assertIn("portal/src/lib/business-central/align-portal-so-number.ts",
                      {c["path"] for c in own["upstream_citations"]})
        # Same number, different order date: another order, never compared.
        self.assertEqual(got["SO1"], ("unknown", "order_date_conflict"))
        for n in range(3):
            self.assertEqual(got[f"UAT-SO{n}"], ("unknown", "not_in_served_replica"))
        # SO3 alone was compared with the Portal; SO2, UAT-SO8 and UAT-SO9 are BC-only.
        self.assertEqual((result["summary"]["portal_compared"], result["summary"]["bc_only"],
                          result["all_not_in_served_replica"]), (1, 3, False))
        self.assertEqual(result["bc_sample"].pop("bc_requests"), len(sender.calls))
        self.assertEqual(result["bc_sample"], {
            "documents": 8, "order_numbers": 8, "in_served_replica": 2,
            "not_in_served_replica": 3, "external_document_number_present": 6,
            "by_class": {"blank": 2, "matches_order_number": 1, "portal_dependent": 5,
                         "unknown": 0},
            "companies_unread": 0, "withheld": 0, "index_incomplete": 0})
        self.assertTrue(all(c[0] == "GET" for c in sender.api_calls()))
        # A BC reader the audience does not grant reads nothing and classifies nothing.
        reader, sender = bc_reader(so)
        none = rr.reconcile_recent_orders(self.mirror(), reader, NOBODY, [COMPANY], limit=10)
        self.assertEqual((none["findings"], none["bc_sample"]["companies_unread"]), ([], 1))
        self.assertEqual(sender.api_calls(), [])

    def test_invoices_join_on_their_order_number(self):
        inv = [doc(5, number="PSI-1", orderNumber="SO1", externalDocumentNumber="PO-5501",
                   invoiceDate="2026-09-20"),
               doc(6, number="PSI-2", externalDocumentNumber="SO2", invoiceDate="2026-09-20"),
               doc(7, number="PSI-3", orderNumber="SO1'x", invoiceDate="2026-09-20")]
        reader, _ = bc_reader(sales_invoices=inv)
        result = rr.reconcile_recent_orders(self.mirror(), reader, OPS, [COMPANY], limit=5,
                                            entity_set="salesInvoices")
        got = sorted((f["class"], f["reason"]) for f in result["findings"])
        self.assertEqual(got, [("matches_customer_po", "equals_portal_po_like_field"),
                               ("unknown", "bc_document_without_order_number"),
                               ("unknown", "order_number_invalid")])
        self.assertNotIn("SO1'x", json.dumps(result))

    def test_cardinality_no_order_lost_or_duplicated(self):
        """N = 1/10/30/60 orders over three companies, every one held by the replica: none
        lost or duplicated, and BC GETs stay one sample and one number index per company
        plus one lookup per order (36 at limit 10, where one lookup per order and company
        was 93)."""
        companies = [COMPANY, COMPANY2, "0f0e0d0c-0b0a-0908-0706-0504030201aa"]
        for total in (1, 10, 30, 60):
            per = -(-total // 3)
            self.fake.tables["public.app_sales_order"] = [
                {"id": n, "order_number": f"SO{n}", "po_reference": f"PO-{n}",
                 "total_amount": "1", "custom_fields": None,
                 "updated_at": "2026-09-29T09:00:00"} for n in range(total)]
            reader, sender = bc_reader()
            for i, company in enumerate(companies):
                sender.routes[f"{BASE_BC}/companies({company})/salesOrders"] = filtered(
                    [doc(n + 1, number=f"SO{n}", externalDocumentNumber=f"PO-{n}")
                     for n in range(i * per, min(total, (i + 1) * per))])
            result = rr.reconcile_recent_orders(self.mirror(), reader, OPS, companies,
                                                limit=per)
            numbers = [f["subject"]["order_number"] for f in result["findings"]]
            self.assertEqual(sorted(numbers), sorted(f"SO{n}" for n in range(total)), total)
            self.assertEqual(result["summary"]["by_class"]["matches_customer_po"], total)
            self.assertEqual(result["summary"]["portal_compared"], total)
            gets = [c for c in sender.api_calls() if c[0] == "GET"]
            self.assertEqual(len(gets), len(sender.api_calls()))
            self.assertLessEqual(len(gets), 2 * len(companies) + total, total)
            self.assertEqual(result["bc_sample"]["bc_requests"], len(sender.calls))

    def test_live_check_passes_only_on_a_portal_comparison(self):
        """The CLI's output against the real spec. A sample with no Portal-dependent
        document (every finding BC-only) compares nothing with the Portal: blocked
        source_gap no_portal_dependent_document, never a pass or a fail; one finding
        classified against a served Portal order passes; when the replica holds none of
        the orders the run needed it for, the row is blocked with that cause; a run with
        Portal-dependent documents that compares none otherwise fails."""
        spec = json.loads((Path(lv.ROOT) / lv.SPECS_DIR_NAME / "case.rec10_external_doc.json")
                          .read_text(encoding="utf-8"))
        lv.validate_spec(spec, stem="case.rec10_external_doc")
        args = argparse.Namespace(limit=3, entity_set="salesOrders")

        def run(*docs, companies=(COMPANY,)):
            reader, _ = bc_reader(list(docs))
            return json.loads(json.dumps(rr._run_split(self.mirror(), reader, OPS, OPS,
                                                       list(companies), args)))

        def blocked(out):
            (value,) = lv.resolve(out, spec["blocked_when"])
            return value if isinstance(value, str) and lv.ENUM_STRING.fullmatch(value) else None

        # (a) Every finding BC-only, one held by the replica (SO1's own number): no
        # Portal-dependent document, so blocked source_gap with its reason, never a pass.
        bc_only = (doc(1, number="SO1", externalDocumentNumber="SO1"), doc(2, number="UAT-SO2"),
                   doc(3, number="UAT-SO3", externalDocumentNumber="uat-so3"))
        out = run(*bc_only)
        self.assertEqual((out["summary"]["findings"], out["summary"]["bc_only"],
                          out["summary"]["portal_compared"]), (3, 3, 0))
        self.assertEqual(lv.check_expectations(spec, 0, out), ["summary.portal_compared < 1"])
        self.assertEqual(blocked(out), "no_portal_dependent_document")
        self.assertEqual(spec["blocked_cause"], "source_gap")
        self.assertEqual((out["all_not_in_served_replica"],
                          lv._recorded(spec, out)["no_portal_dependent_document"]), (False, True))
        # A company BC did not read could hold Portal-dependent documents: fail, not blocked.
        out = run(*bc_only, companies=(COMPANY, COMPANY2))
        self.assertEqual(out["bc_sample"]["companies_unread"], 1)
        self.assertIsNone(blocked(out))
        # N = 1/10/50 BC-only documents: every one found once, still blocked.
        for total in (1, 10, 50):
            args.limit = total
            out = run(*[doc(n + 1, number=f"UAT-SO{n}") for n in range(total)])
            self.assertEqual((out["summary"]["findings"], out["summary"]["bc_only"],
                              out["bc_sample"]["documents"], blocked(out)),
                             (total, total, total, "no_portal_dependent_document"), total)
        args.limit = 3
        # (b) One finding compared with a served Portal order passes.
        out = run(doc(1, number="SO1", externalDocumentNumber="PO-5501"),
                  doc(2, number="UAT-SO2", externalDocumentNumber="UAT-SO2"))
        self.assertEqual((out["summary"]["portal_compared"], out["summary"]["bc_only"]), (1, 1))
        self.assertEqual(lv.check_expectations(spec, 0, out), [])
        self.assertIsNone(blocked(out))
        # (c) The replica holds none of the Portal-dependent orders (drained from UAT),
        # beside a BC-only one: blocked not_in_served_replica, its expectations unmet.
        out = run(*[doc(n, number=f"UAT-SO{n}", externalDocumentNumber="X") for n in range(2)],
                  doc(5, number="UAT-SO5", externalDocumentNumber="UAT-SO5"))
        self.assertTrue(out["all_not_in_served_replica"])
        self.assertEqual(blocked(out), "not_in_served_replica")
        self.assertEqual(spec["blocked_cause"], "source_gap")
        self.assertEqual(lv.check_expectations(spec, 0, out), ["summary.portal_compared < 1"])
        recorded = lv._recorded(spec, out)
        self.assertEqual((recorded["all_not_in_served_replica"], recorded["summary.bc_only"],
                          recorded["bc_sample.not_in_served_replica"]), (True, 1, 2))
        self.assertEqual(recorded["summary.unknown_reasons"], {"not_in_served_replica": 2})
        # (d) A Portal-dependent order the replica holds, compared to nothing (its
        # po_reference is a person's name): fails, never blocked.
        out = run(doc(2, number="SO2", externalDocumentNumber="Jane Buyer"),
                  doc(5, number="UAT-SO5", externalDocumentNumber="UAT-SO5"))
        self.assertEqual((out["summary"]["portal_compared"], out["all_not_in_served_replica"],
                          out["no_portal_dependent_document"]), (0, False, False))
        self.assertIsNone(blocked(out))
        self.assertEqual(lv.check_expectations(spec, 0, out), ["summary.portal_compared < 1"])


class ShipmentLotTests(unittest.TestCase):
    """What a Portal sales shipment line sends BC as lots since TASK-3248
    (serializers.ts@c09de5d shipmentBaseLots / serializeSalesShipment)."""

    def test_a_bin_suffixed_lot_ships_as_its_base_lot(self):
        stamped = rr.shipment_line_bc_lots({"quantity": "12", "lot_number": " L240601@A-01-02 ",
                                            "expiry_date": "2027-06-01T00:00:00Z"})
        self.assertEqual((stamped["shape"], stamped["source"]), ("lot", "stamped"))
        self.assertEqual(stamped["lots"], [{"lot_number": "L240601", "quantity": Decimal("12"),
                                            "expiry_date": "2027-06-01"}])
        # One base lot fed from two bins: one lot for the whole line, earliest expiry.
        picked = rr.shipment_line_bc_lots({"quantity": 10, "lot_number": None, "lots": [
            {"lot_number": "L240601@A-01", "quantity": "4", "expiry_date": "2027-06-30"},
            {"lot_number": "L240601@B-07", "quantity": "6", "expiry_date": "2027-06-01"}]})
        self.assertEqual((picked["shape"], picked["source"]), ("lot", "picks"))
        self.assertEqual(picked["lots"], [{"lot_number": "L240601", "quantity": Decimal("10"),
                                           "expiry_date": "2027-06-01"}])

    def test_a_two_lot_line_ships_two_lots_with_their_expiries(self):
        out = rr.shipment_line_bc_lots({"quantity": 70, "lot_number": None, "lots": [
            {"lot_number": "L240715@C-03", "quantity": "40", "expiry_date": "2027-07-15"},
            {"lot_number": "L240601@A-01", "quantity": "10.5", "expiry_date": "2027-06-01"},
            {"lot_number": "L240601@A-02", "quantity": "19.5", "expiry_date": None},
            {"lot_number": "@A-09", "quantity": "5"},          # no base lot: dropped, as upstream
            {"lot_number": "L240801", "quantity": "0"}]})      # nothing taken: dropped
        self.assertEqual((out["shape"], out["source"]), ("lots", "picks"))
        self.assertEqual(out["lots"], [
            {"lot_number": "L240601", "quantity": Decimal("30.0"), "expiry_date": "2027-06-01"},
            {"lot_number": "L240715", "quantity": Decimal("40"), "expiry_date": "2027-07-15"}])
        self.assertEqual(out["skipped"], [{"lot_number": "@A-09", "reason": "no_base_lot"},
                                          {"lot_number": "L240801", "reason": "quantity_not_positive"}])
        # Only bridge 0.9.18.0 tracks a "lots" line; an older one posts it with no lot.
        self.assertEqual(out["bridge_min_version"], "0.9.18.0")
        self.assertTrue(any(c["commit"] == "c09de5d" and "lots" in c["fact"]
                            for c in rr.SHIPMENT_LOT_CITATIONS))

    def test_no_lot_and_unknown_shapes(self):
        self.assertEqual(rr.shipment_line_bc_lots({"quantity": 3, "lot_number": "", "lots": []}),
                         {"shape": "none", "source": "stamped", "lots": [], "skipped": []})
        # A bin with no base lot sends no lot, but upstream still sends the UTC day of the expiry.
        bin_only = rr.shipment_line_bc_lots({"quantity": 3, "lot_number": "@A-01",
                                             "expiry_date": "2027-06-01T23:30:00-05:00"})
        self.assertEqual((bin_only["shape"], bin_only["unlotted_expiry_date"]), ("none", "2027-06-02"))
        for bad in ({"quantity": 3, "lots": {"lot_number": "L1"}},      # not a list
                    {"quantity": 3, "lots": ["L1"]},                      # entry not a row
                    {"quantity": 3, "lot_number": 240601},                # lot not text
                    {"quantity": 3, "lot_number": "L1", "expiry_date": "soon"},
                    {"quantity": "three", "lot_number": "L1"}):
            with self.subTest(line=bad), self.assertRaises(ValueError):
                rr.shipment_line_bc_lots(bad)

    def test_cardinality_no_lot_lost_or_duplicated(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                lots = [{"lot_number": f"L{i:03d}@BIN{j}", "quantity": "1.25",
                         "expiry_date": f"2027-01-{1 + j:02d}"} for i in range(n) for j in range(2)]
                out = rr.shipment_line_bc_lots({"quantity": n * 2.5, "lots": list(reversed(lots))})
                self.assertEqual([lot["lot_number"] for lot in out["lots"]],
                                 [f"L{i:03d}" for i in range(n)])
                self.assertEqual(sum(lot["quantity"] for lot in out["lots"]), Decimal("2.5") * n)
                self.assertTrue(all(lot["expiry_date"] == "2027-01-01" for lot in out["lots"]))
                self.assertEqual(out["shape"], "lot" if n == 1 else "lots")


if __name__ == "__main__":
    unittest.main()
