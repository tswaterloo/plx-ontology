"""BC purchase-order ``Your_Reference`` (the FileMaker PO number) from ODataV4 Page 50
``PurchaseOrder``: the one web-service read the GET guard admits, and its capture unit
``purchaseOrderReferences`` (``bc_read.BcCapture._capture_references``).

API v2.0 ``purchaseOrders`` does not expose Your Reference; the lead's live probe of
2026-10-21 found it on ODataV4 ``Company('My Company')/PurchaseOrder`` (fields ``No``,
``Your_Reference``, ``Buy_from_Vendor_No``). Evidence label: ``injected-transport`` (the
in-process ``FakeBc`` of ``test_bc_capture``; synthetic numbers; no network, no
credentials). The live read is live-unverified until ``bc.capture_full`` runs it.
"""

from __future__ import annotations

import functools
import json
import unittest
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

from integrations import bc_read
from integrations import live_source_grammar as grammar
from integrations.bc_read import REFERENCE_SET
from integrations.capture_store import CaptureError, bc_unit
from integrations.live_source_guard import ReadOnlyGuardError, _same_collection_next, check_request
from integrations.tests.test_bc_capture import (BUYER, CO, NAMES, ODATA, SERVICE, FakeBc,
                                                Harness)
from integrations.tests.test_live_source_adapters import ENV, TENANT_ID
from ontology.core import Actor

PINNED = "My Company"  # the one pinned name (bc_company_books.json); CO[1] lists under it
BASE_URL = f"https://api.businesscentral.dynamics.com{ODATA}"
PAGE = f"{BASE_URL}/Company('My%20Company')/PurchaseOrder"
SELECT = "%24select=No%2CYour_Reference%2CBuy_from_Vendor_No"
BOOKS = {"production": {CO[1]: "manufacturing"}}
INJECTED_NAMES = {"production": {CO[1]: PINNED}}
NO_ORDERS = Actor("plx", frozenset({"staff:companies"}), "restricted")


def guarded(url: str, method: str = "GET") -> bool:
    try:
        check_request(method, url, environ=ENV)
    except ReadOnlyGuardError:
        return False
    return True


class OdataGuardTests(unittest.TestCase):
    """The narrowest read-only extension: GET, ``Company('<pinned name>')/PurchaseOrder``,
    exactly the built ``$select`` (and an optional built ``$filter`` / ``$top``), the
    nextLink continuation; nothing else."""

    def test_the_built_url_is_the_one_shape_the_guard_sends(self):
        url = grammar.url("bc.odata_purchase_orders", grammar.bc_odata_purchase_order_query(),
                          company=PINNED, tenant=TENANT_ID, environment="Production")
        self.assertEqual(url, f"{PAGE}?{SELECT}")
        self.assertTrue(guarded(url))
        one = grammar.url("bc.odata_purchase_orders",
                          grammar.bc_odata_purchase_order_query(number="PO106001", top=2),
                          company=PINNED, tenant=TENANT_ID, environment="Production")
        self.assertTrue(guarded(one))

    def test_other_methods_entity_sets_keys_actions_and_companies_are_refused(self):
        refused = {
            "POST": f"{PAGE}?{SELECT}", "PATCH": f"{PAGE}?{SELECT}",
            "PUT": f"{PAGE}?{SELECT}", "DELETE": f"{PAGE}?{SELECT}",
        }
        for method, url in refused.items():
            self.assertFalse(guarded(url, method), method)
        for url in (
                f"{BASE_URL}/Company('My%20Company')/PurchaseOrderPurchLines?{SELECT}",
                f"{BASE_URL}/Company('My%20Company')/Customer?{SELECT}",
                f"{BASE_URL}/Company('My%20Company')/PurchaseOrder('Order','PO1')?{SELECT}",
                f"{BASE_URL}/Company('My%20Company')/PurchaseOrder/Microsoft.NAV.post?{SELECT}",
                f"{BASE_URL}/Company('My%20Company')/PurchaseOrder('PO1')/Microsoft.NAV.post",
                f"{BASE_URL}/Company('My%20Company')/PurchaseOrder/$count?{SELECT}",
                f"{BASE_URL}/Company('CRONUS%20Canada')/PurchaseOrder?{SELECT}",
                f"{BASE_URL}/Company('My%20Company%27)/PurchaseOrder?{SELECT}",
                f"{BASE_URL}/Company({CO[1]})/PurchaseOrder?{SELECT}",
                f"{BASE_URL}/Company/PurchaseOrder?{SELECT}",
                f"{BASE_URL}/$metadata",
                f"{BASE_URL}/Company('My%20Company')/PurchaseOrder/$batch",
        ):
            self.assertFalse(guarded(url), url)

    def test_unexpected_query_options_are_refused(self):
        for query in (
                "",                                           # no $select: every field
                "%24select=No%2CYour_Reference",              # a narrower field list
                "%24select=No%2CYour_Reference%2CBuy_from_Vendor_No%2CAmount",
                "%24select=Your_Reference%2CNo%2CBuy_from_Vendor_No",  # not as built
                f"{SELECT}&%24expand=PurchaseOrderPurchLines",
                f"{SELECT}&%24orderby=No",
                f"{SELECT}&%24filter=Your_Reference%20eq%20%27FM1%27",
                f"{SELECT}&%24filter=No%20ne%20%27PO1%27",
                f"{SELECT}&%24filter=No%20eq%20%27PO1%27%20or%20true",
                f"{SELECT}&%24top=0", f"{SELECT}&%24top=5000", f"{SELECT}&%24skip=10",
                f"{SELECT}&%24count=true", f"{SELECT}&%24apply=groupby((No))",
                f"{SELECT}&%24select=No", f"{SELECT}&company=x",
                f"{SELECT}&%24skiptoken=%27a%27b%27%28%29",
        ):
            self.assertFalse(guarded(f"{PAGE}?{query}" if query else PAGE), query)

    def test_a_nextlink_continues_only_the_same_read(self):
        request = f"{PAGE}?{SELECT}"
        good = f"{PAGE}?{SELECT}&%24skiptoken=%27PO106001%27"
        self.assertEqual(_same_collection_next(request, good, environ=ENV), good)
        for link in (
                f"{BASE_URL}/Company('My%20Company')/PurchaseOrderPurchLines?{SELECT}"
                "&%24skiptoken=%271%27",
                f"{PAGE}?%24skiptoken=%27PO106001%27",                      # $select dropped
                f"{PAGE}?{SELECT}&%24expand=X&%24skiptoken=%271%27",
                f"{BASE_URL}/Company('CRONUS%20Canada')/PurchaseOrder?{SELECT}"
                "&%24skiptoken=%271%27",
                f"http://api.businesscentral.dynamics.com{ODATA}/Company('My%20Company')"
                f"/PurchaseOrder?{SELECT}&%24skiptoken=%271%27",
        ):
            self.assertIsNone(_same_collection_next(request, link, environ=ENV), link)

    def test_company_names_are_pinned_and_plain(self):
        self.assertTrue(grammar.bc_company_name_ok(PINNED))
        for name in ("CRONUS Canada", "My  Company", "My Company'", "My Company)", "",
                     "My/Company", " My Company", None, 7):
            self.assertFalse(grammar.bc_company_name_ok(name), name)
            with self.assertRaises(ValueError):
                grammar.url("bc.odata_purchase_orders",
                            grammar.bc_odata_purchase_order_query(), company=name,
                            tenant=TENANT_ID, environment="Production")
        with self.assertRaises(ValueError):
            grammar.bc_odata_purchase_order_query(number="PO1' or 1 eq 1")


def harness(test, fake=None, **kwargs) -> Harness:
    h = Harness(test, fake or FakeBc({"purchaseOrders": 5}))
    h.capture.books = BOOKS
    h.capture.names = kwargs.get("names", INJECTED_NAMES)
    return h


def odata_gets(h: Harness) -> list[str]:
    return h.fake.bc_gets(r"/ODataV4/")


class ReferenceCaptureTests(unittest.TestCase):
    """The reference traversal reads at ``bc_read.REFERENCE_PAGE_SIZE`` (BC's page ceiling),
    whatever the record page size of the call; these tests shrink it to 2 rows a page so a
    few synthetic orders cross pages (nextLink, page cap)."""

    def setUp(self):
        patcher = mock.patch.object(bc_read, "REFERENCE_PAGE_SIZE", 2)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_reference_page_size_is_the_ceiling_not_the_record_page_size(self):
        with mock.patch.object(bc_read, "REFERENCE_PAGE_SIZE", 1000):
            h = harness(self, FakeBc({"purchaseOrders": 60}))
            unit = h.unit(h.run(entity_sets=("purchaseOrders",), page_size=2), CO[1],
                          REFERENCE_SET)
        self.assertEqual((unit["state"], unit["references"]["stored"]), ("complete", 60))
        (prefer,) = [head.get("Prefer", "") for _m, url, head in h.fake.calls
                     if "/ODataV4/" in unquote(url)]   # one page, no nextLink followed
        self.assertIn("maxpagesize=1000", prefer)

    def test_each_reference_is_stored_under_its_api_order_with_three_fields(self):
        h = harness(self)
        report = h.run(entity_sets=("purchaseOrders",))
        unit = h.unit(report, CO[1], REFERENCE_SET)
        self.assertEqual((unit["state"], unit["complete"]), ("complete", True))
        self.assertEqual(unit["references"]["stored"], 5)
        self.assertEqual(sum(v for k, v in unit["references"].items() if k != "stored"), 0)
        self.assertEqual(report["units_total"], 1 + 1 + 1 + 1)   # set, lines, company, page
        self.assertEqual(report["units_complete"], report["units_total"])
        gets = odata_gets(h)
        self.assertEqual(len(gets), 3)            # 5 rows at 2 per page, followed by nextLink
        for url in gets:
            check_request("GET", url, environ=ENV)
            self.assertIn("Company('My Company')/PurchaseOrder?", unquote(url))
            self.assertEqual(parse_qs(urlsplit(url).query)["$select"],
                             [grammar.BC_ODATA_PO_SELECT])
        self.assertEqual({m for m, u, _h in h.fake.calls if "businesscentral" in u}, {"GET"})
        orders = {r["row"]["number"]: r for r in h.store.records(
            SERVICE, "bc", bc_unit(CO[1], "purchaseOrders"), limit=100)["items"]}
        refs = h.store.records(SERVICE, "bc", bc_unit(CO[1], REFERENCE_SET), limit=100)["items"]
        self.assertEqual(len(refs), 5)
        for ref in refs:
            self.assertEqual(set(ref["row"]), set(grammar.BC_ODATA_PO_FIELDS))
            order = orders[ref["row"]["No"]]
            self.assertEqual(ref["citation"].split("/")[-1].split("@")[0],
                             order["citation"].split("/")[-1].split("@")[0])
            self.assertIn(f"/{REFERENCE_SET}/", ref["citation"])

    def test_no_pinned_name_means_no_odata_read(self):
        h = harness(self, names={})
        report = h.run(entity_sets=("purchaseOrders",))
        self.assertFalse(odata_gets(h))
        self.assertNotIn(REFERENCE_SET, [u["entity_set"] for c in report["companies"]
                                         for u in c["units"]])
        self.assertEqual(report["units_total"], 3)
        h = harness(self)
        report = h.run(entity_sets=("vendors",))    # no purchaseOrders: nothing to join to
        self.assertFalse(odata_gets(h))

    def test_an_injected_name_the_grammar_does_not_pin_fails_closed_without_a_request(self):
        fake = FakeBc({"purchaseOrders": 2}, names={**NAMES, CO[1]: "CRONUS Canada"})
        h = harness(self, fake, names={"production": {CO[1]: "CRONUS Canada"}})
        unit = h.unit(h.run(entity_sets=("purchaseOrders",)), CO[1], REFERENCE_SET)
        self.assertEqual((unit["state"], unit["reason"]), ("failed", "company_name_refused"))
        self.assertFalse(odata_gets(h))

    def test_unsettled_purchase_orders_skip_the_page(self):
        fake = FakeBc({"purchaseOrders": 5})
        fake.fail(rf"{CO[1]}\)/purchaseOrders", (500, {}, b"{}"), (500, {}, b"{}"),
                  (500, {}, b"{}"), (500, {}, b"{}"))
        h = harness(self, fake)
        report = h.run(entity_sets=("purchaseOrders",))
        unit = h.unit(report, CO[1], REFERENCE_SET)
        self.assertEqual((unit["state"], unit["reason"]), ("skipped", "purchase_orders_not_settled"))
        self.assertFalse(odata_gets(h))
        self.assertFalse(report["all_complete"])

    def test_rows_not_tied_to_one_held_order_are_counted_never_stored(self):
        fake = FakeBc({"purchaseOrders": 4})
        orders = fake.data[(CO[1], "purchaseOrders")]
        orders[3]["vendorNumber"] = "V1"
        orders[2]["number"] = orders[1]["number"]          # two held orders, one number
        rows = fake.reference_rows(CO[1])
        fake.references[CO[1]] = [
            dict(rows[0], Your_Reference="  "),                  # blank: stored, counted
            dict(rows[1]),                                        # ambiguous held order
            {"No": "PO-NOT-HELD", "Your_Reference": "FM-9", "Buy_from_Vendor_No": ""},
            dict(rows[3], Buy_from_Vendor_No="V2"),               # vendor differs
            {"No": "", "Your_Reference": "FM-8"},                 # no number
            {"No": "DUP", "Your_Reference": "FM-7"}, {"No": "DUP", "Your_Reference": "FM-6"},
        ]
        h = harness(self, fake)
        unit = h.unit(h.run(entity_sets=("purchaseOrders",)), CO[1], REFERENCE_SET)
        self.assertEqual(unit["references"], {
            "no_number": 1, "ambiguous_api_record": 1, "duplicate_number": 2,
            "vendor_mismatch": 1, "no_api_record": 1, "blank_reference": 1, "stored": 1})
        self.assertEqual((unit["state"], unit["complete"]), ("partial", False))
        refs = h.store.records(SERVICE, "bc", bc_unit(CO[1], REFERENCE_SET), limit=100)["items"]
        self.assertEqual([r["row"]["No"] for r in refs], [rows[0]["No"]])

    def test_an_order_created_after_the_order_read_leaves_the_page_complete(self):
        fake = FakeBc({"purchaseOrders": 2})
        fake.references[CO[1]] = [*fake.reference_rows(CO[1]),
                                  {"No": "PO-NEW", "Your_Reference": "FM-1"}]
        h = harness(self, fake)
        unit = h.unit(h.run(entity_sets=("purchaseOrders",)), CO[1], REFERENCE_SET)
        self.assertEqual((unit["state"], unit["references"]["no_api_record"],
                          unit["references"]["stored"]), ("complete", 1, 2))

    def test_a_refused_nextlink_fails_the_unit_and_stores_nothing(self):
        class Escaping(FakeBc):
            def odata(self, name, entity, parts, headers):
                status, head, body = super().odata(name, entity, parts, headers)
                page = json.loads(body)
                if "@odata.nextLink" in page:
                    page["@odata.nextLink"] = page["@odata.nextLink"].replace(
                        "/PurchaseOrder?", "/PurchaseOrderPurchLines?")
                return status, head, self.body(page)
        h = harness(self, Escaping({"purchaseOrders": 5}))
        unit = h.unit(h.run(entity_sets=("purchaseOrders",)), CO[1], REFERENCE_SET)
        self.assertEqual((unit["state"], unit["reason"]), ("failed", "nextlink_refused"))
        self.assertFalse(h.fake.bc_gets(r"PurchaseOrderPurchLines"))
        self.assertEqual(h.store.records(SERVICE, "bc", bc_unit(CO[1], REFERENCE_SET),
                                         limit=10)["items"], [])

    def test_a_truncated_read_is_partial(self):
        h = harness(self)
        h.reader.purchase_order_references = functools.partial(
            type(h.reader).purchase_order_references, h.reader, max_pages=1)
        unit = h.unit(h.run(entity_sets=("purchaseOrders",)), CO[1], REFERENCE_SET)
        self.assertEqual((unit["state"], unit["reason"]), ("partial", "page_cap"))
        self.assertEqual(unit["references"]["stored"], 2)

    def test_a_page_listing_an_order_no_more_marks_its_reference_missing(self):
        fake = FakeBc({"purchaseOrders": 3})
        h = harness(self, fake)
        h.run(entity_sets=("purchaseOrders",))
        fake.references[CO[1]] = fake.reference_rows(CO[1])[:2]
        h.clock.now = h.clock.now.replace(hour=13)
        unit = h.unit(h.run(entity_sets=("purchaseOrders",)), CO[1], REFERENCE_SET)
        self.assertEqual((unit["state"], unit["records_current"], unit["records_missing"]),
                         ("complete", 3, 1))   # current versions, the missing one included

    def test_a_reader_without_the_orders_sees_no_reference_and_reads_none(self):
        h = harness(self)
        h.run(entity_sets=("purchaseOrders",))
        # BUYER holds purchaseOrders of CO[0] only: CO[1]'s references stay invisible.
        for actor in (BUYER, NO_ORDERS):
            with self.assertRaises(CaptureError) as refused:
                h.store.records(actor, "bc", bc_unit(CO[1], REFERENCE_SET), limit=10)
            self.assertEqual(refused.exception.code, "not_visible")
        before = len(odata_gets(h))
        report = h.capture.capture(BUYER, entity_sets=("purchaseOrders",), page_size=2)
        self.assertEqual(len(odata_gets(h)), before)
        units = [u for c in report["companies"] for u in c["units"]
                 if u["entity_set"] == REFERENCE_SET]
        self.assertEqual([u["state"] for u in units], ["not_visible"])

    def test_cardinality_probe_keeps_every_row(self):
        for n in (1, 10, 60):
            with self.subTest(n=n), mock.patch.object(bc_read, "REFERENCE_PAGE_SIZE", 7):
                h = harness(self, FakeBc({"purchaseOrders": n}))
                unit = h.unit(h.run(entity_sets=("purchaseOrders",), page_size=7), CO[1],
                              REFERENCE_SET)
                self.assertEqual((unit["state"], unit["references"]["stored"],
                                  unit["records_current"]), ("complete", n, n))
                self.assertEqual(len(odata_gets(h)), -(-n // 7))
                refs = h.store.records(SERVICE, "bc", bc_unit(CO[1], REFERENCE_SET),
                                       limit=100)["items"]
                self.assertEqual(len({r["row"]["No"] for r in refs}), n)


if __name__ == "__main__":
    unittest.main()
