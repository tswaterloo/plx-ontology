"""BC purchase/sales/item reads, number lead lookups and case leads (injected transport).

No network and no credentials: every request goes to the in-process
``FakeSender`` from ``test_live_source_adapters``. Evidence label:
``injected-transport``.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import unittest
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from integrations import bc_read, live_source_grammar as grammar
from integrations.bc_case_evidence import CAVEATS, bc_leads_for_number
from integrations.bc_read import CASE_ENTITY_SETS, BusinessCentralReader
from integrations.live_source_guard import (LiveSourceAudience, NotVisible, ReadOnlyGuardError,
                                            SourceUnavailable, check_request,
                                            is_nextlink_refusal, unavailable_detail)
from integrations.tests.test_live_source_adapters import (ACCESS_TOKEN, CLIENT_SECRET, COMPANY,
                                                          ENV, TENANT_ID, FakeSender, j)
from ontology.core import Actor


BASE = f"/v2.0/{TENANT_ID}/Production/api/v2.0"
COMPANY2 = "0f0e0d0c-0b0a-0908-0706-0504030201ff"
HOST = "https://api.businesscentral.dynamics.com"
FINANCE = Actor("plx", frozenset({"staff:finance"}), "restricted")
BUYER = Actor("plx", frozenset({"staff:buyer"}), "restricted")
NOBODY = Actor("plx", frozenset({"staff:nobody"}), "restricted")


def gid(n: int) -> str:
    return f"bbbbbbbb-0000-0000-0000-{n:012d}"


def doc(n: int, **fields: Any) -> dict:
    return {"@odata.etag": f'W/"JzQ0O{n}Jw=="', "id": gid(n),
            "lastModifiedDateTime": "2026-09-30T10:00:00Z", **fields}


def line(n: int, document: str, **fields: Any) -> dict:
    return {"@odata.etag": f'W/"L{n}"', "id": gid(900 + n), "documentId": document, **fields}


def raw_json(value: Any) -> str:
    """JSON text with exact decimal literals (``Decimal`` marker strings unquoted)."""
    text = json.dumps(value)
    for marker in ("1234.10", "0.4525", "50000.00", "1920"):
        text = text.replace(f'"#{marker}"', marker)
    return text


def filtered(rows: list[dict]):
    """Route that applies the request's single ``field eq 'value'`` filter and $top."""
    def route(url: str):
        query = parse_qs(urlsplit(url).query)
        result = rows
        if "$filter" in query:
            field, _, literal = query["$filter"][0].partition(" eq ")
            value = literal.strip("'")
            result = [r for r in rows if str(r.get(field, "")).casefold() == value.casefold()]
        if "$top" in query:
            result = result[:int(query["$top"][0])]
        return 200, {}, raw_json({"value": result}).encode()
    return route


class Fixture:
    """Two companies; PO/SO/invoice/item/vendor/customer rows with lines."""

    def __init__(self) -> None:
        self.po = doc(1, number="PO10123", vendorNumber="V0042", currencyCode="USD",
                      totalAmountIncludingTax="#1234.10", status="Open")
        self.so = doc(2, number="SO100351", externalDocumentNumber="PO12172025",
                      customerNumber="C0089", totalAmountIncludingTax="#50000.00")
        self.so_other = doc(3, number="SO100999", externalDocumentNumber="SO100351")
        self.so_c2 = doc(4, number="SO100351", externalDocumentNumber="SHIP-77")
        self.inv = doc(5, number="PSI-1", orderNumber="SO100351",
                       externalDocumentNumber="SO100351")
        self.pinv = doc(6, number="PPI-1", orderNumber="PO10123", vendorInvoiceNumber="INV-9")
        self.item = doc(7, number="DVS-5003", displayName="bulk", unitCost="#0.4525")
        self.receipt = doc(8, number="PRC-1", vendorNumber="V0042")
        self.po_lines = [line(1, gid(1), sequence=10000, quantity="#1920",
                              directUnitCost="#0.4525", lineObjectNumber="DVS-5003"),
                         line(2, gid(999), sequence=20000, quantity=1)]
        c1 = f"{BASE}/companies({COMPANY})"
        c2 = f"{BASE}/companies({COMPANY2})"
        self.routes: dict[str, Any] = {
            f"{BASE}/companies": j(200, {"value": [
                {"id": COMPANY, "name": "PETRA", "@odata.etag": 'W/"1"'},
                {"id": COMPANY2, "name": "DISTRIBUTION", "@odata.etag": 'W/"2"'}]}),
            f"{c1}/purchaseOrders": filtered([self.po]),
            f"{c1}/purchaseOrders({gid(1)})": lambda url: (200, {}, raw_json(self.po).encode()),
            f"{c1}/purchaseOrders({gid(1)})/purchaseOrderLines": filtered(self.po_lines),
            f"{c1}/purchaseOrderLines({gid(901)})":
                lambda url: (200, {}, raw_json(self.po_lines[0]).encode()),
            f"{c1}/purchaseInvoices": filtered([self.pinv]),
            f"{c1}/purchaseReceipts": filtered([self.receipt]),
            f"{c1}/salesOrders": filtered([self.so, self.so_other]),
            f"{c1}/salesInvoices": filtered([self.inv]),
            f"{c1}/items": filtered([self.item]),
            f"{c1}/vendors": filtered([doc(9, number="V0042", displayName="Supplier Co")]),
            f"{c1}/customers": filtered([doc(10, number="C0089", displayName="Customer Co")]),
            f"{c2}/salesOrders": filtered([self.so_c2]),
            f"{c2}/salesInvoices": filtered([]),
            f"{c2}/purchaseOrders": filtered([]),
            f"{c2}/purchaseInvoices": filtered([]),
        }
        self.sender = FakeSender(self.routes)
        self.api, self.transport, tenant, environment = bc_read.build_bc(ENV, sender=self.sender)
        self.audience = LiveSourceAudience({
            "bc:production:*": ["staff:finance"],
            "bc:production:companies": ["staff:finance", "staff:buyer"],
            f"bc:production:{COMPANY}:purchaseorders": ["staff:finance", "staff:buyer"],
            f"bc:production:{COMPANY}:salesorders": ["staff:finance", "staff:buyer"]},
            tenant="plx")
        self.reader = BusinessCentralReader(self.api, self.audience, tenant_id=tenant,
                                            environment=environment)

    def entity_calls(self) -> list[tuple[str, str]]:
        return [(c[0], c[1]) for c in self.sender.api_calls()]


class BcQueryGrammarTests(unittest.TestCase):
    def test_typed_queries_and_refusals(self):
        self.assertEqual(grammar.bc_query("purchaseOrders", equals=("number", "PO5447"), top=51),
                         {"$filter": "number eq 'PO5447'", "$top": "51"})
        self.assertEqual(grammar.bc_query("salesOrders", equals=("id", gid(2).upper())),
                         {"$filter": f"id eq {gid(2)}"})
        self.assertEqual(grammar.bc_query("vendors", select=["number", "displayName"]),
                         {"$select": "displayName,id,lastModifiedDateTime,number"})
        self.assertEqual(grammar.bc_query("salesOrderLines", select=["quantity"]),
                         {"$select": "documentId,id,quantity"})
        bad = [dict(equals=("vendorName", "x")),            # field not filterable
               dict(equals=("number", "PO' or 1 eq 1")),     # quote / expression
               dict(equals=("number", "PO5447 ")),           # trailing space
               dict(equals=("number", "x" * 36)),
               dict(equals=("id", "not-a-guid")),
               dict(top=0), dict(top=1001), dict(top=True), dict(top="5"),
               dict(select="number"), dict(select=["email"]), dict(select=[])]
        for kwargs in bad:
            with self.assertRaises(ValueError, msg=kwargs):
                grammar.bc_query("vendors", **kwargs)
        with self.assertRaises(ValueError):
            grammar.bc_query("generalLedgerEntries", equals=("number", "1"))  # no spec
        with self.assertRaises(ValueError):
            grammar.bc_query("employees", top=1)
        self.assertEqual(grammar.bc_query("generalLedgerEntries", top=5), {"$top": "5"})

    def test_query_ok_accepts_only_what_bc_query_builds(self):
        built = urlencode(grammar.bc_query("salesInvoices", equals=("orderNumber", "SO-1"),
                                           top=5, select=["number"]))
        self.assertTrue(grammar.bc_query_ok("salesInvoices", built))
        self.assertTrue(grammar.bc_query_ok("salesInvoices", ""))
        self.assertTrue(grammar.bc_query_ok("salesInvoices", built + "&aid=FIN&$skiptoken=abc"))
        for tampered in (built.replace("orderNumber", "customerName"),
                         "%24filter=number+eq+%27a%27+or+number+eq+%27b%27",
                         "%24filter=number+eq+%27a%27&%24filter=number+eq+%27b%27",
                         "%24expand=salesOrderLines", "%24expand=salesInvoiceLines,customer",
                         "%24top=5000",
                         "%24select=email", built + "&x=1"):
            self.assertFalse(grammar.bc_query_ok("salesInvoices", tampered), tampered)

    def test_orderby_is_one_allowlisted_field_and_direction(self):
        built = grammar.bc_query("salesInvoices", top=40, orderby="postingDate desc",
                                 select=["externalDocumentNumber"])
        self.assertEqual(built["$orderby"], "postingDate desc")
        query = urlencode(built)
        self.assertTrue(grammar.bc_query_ok("salesInvoices", query))
        self.assertTrue(grammar.bc_query_ok("salesInvoices", query + "&$skiptoken=abc"))
        self.assertTrue(grammar.bc_query_ok("purchaseReceipts", urlencode(
            grammar.bc_query("purchaseReceipts", orderby="postingDate asc"))))
        for entity_set, orderby in (("salesInvoices", "customerName desc"),  # not allowlisted
                                    ("salesInvoices", "postingDate"),        # no direction
                                    ("salesInvoices", "postingDate desc,number asc"),
                                    ("salesInvoices", "postingDate DESC"),
                                    ("vendors", "number desc"),             # no orderby list
                                    ("generalLedgerEntries", "postingDate desc")):
            with self.assertRaises(ValueError, msg=(entity_set, orderby)):
                grammar.bc_query(entity_set, orderby=orderby)
        for tampered in (query.replace("postingDate+desc", "customerName+desc"),
                         query.replace("postingDate+desc", "postingDate+desc%2Cnumber+asc"),
                         query + "&%24orderby=number+desc"):
            self.assertFalse(grammar.bc_query_ok("salesInvoices", tampered), tampered)
        url = f"{HOST}{BASE}/companies({COMPANY})/salesInvoices?{query}"
        self.assertEqual(check_request("GET", url), "api.businesscentral.dynamics.com")
        with self.assertRaises(ReadOnlyGuardError):
            check_request("GET", url.replace("postingDate+desc", "customerName+desc"))

    def test_line_templates_are_literal_pairs(self):
        url = grammar.url("bc.lines.purchaseOrders", tenant=TENANT_ID, environment="Production",
                          company=COMPANY, record=gid(1))
        self.assertTrue(url.endswith(f"/companies({COMPANY})/purchaseOrders({gid(1)})"
                                     "/purchaseOrderLines"))
        self.assertEqual(check_request("GET", url), "api.businesscentral.dynamics.com")
        prefix = f"{HOST}{BASE}/companies({COMPANY})"
        for refused in (f"{prefix}/purchaseOrders({gid(1)})/salesOrderLines",
                        f"{prefix}/salesOrders({gid(1)})/purchaseOrderLines",
                        f"{prefix}/employees({gid(1)})",
                        f"{prefix}/purchaseOrders({gid(1)})/Microsoft.NAV.receiveAndInvoice",
                        f"{prefix}/purchaseOrders({gid(1)})/purchaseOrderLines/Microsoft.NAV.x",
                        f"{prefix}/purchaseOrders({gid(1)})/purchaseOrderLines({gid(2)})/x"):
            with self.assertRaises(ReadOnlyGuardError, msg=refused):
                check_request("GET", refused)


class BcGuardQueryTests(unittest.TestCase):
    """Raw URLs straight into ``check_request`` (bypassing the reader)."""

    C = f"{HOST}{BASE}/companies({COMPANY})"

    def test_guard_accepts_exactly_the_built_queries(self):
        accepted = [
            f"{HOST}{BASE}/companies",
            f"{HOST}{BASE}/companies?aid=FIN&$skiptoken=abc",
            f"{self.C}/purchaseOrders",
            f"{self.C}/purchaseOrders?" + urlencode(grammar.bc_query(
                "purchaseOrders", equals=("number", "PO10123"), top=51)),
            f"{self.C}/salesOrders?" + urlencode(grammar.bc_query(
                "salesOrders", equals=("externalDocumentNumber", "PO 4500"),
                select=["number"])),
            f"{self.C}/salesOrders?%24top=5&aid=FIN&%24skiptoken=x123",
            f"{self.C}/generalLedgerEntries?%24top=5",
            f"{self.C}/purchaseOrders({gid(1)})",
            f"{self.C}/purchaseOrders({gid(1)})/purchaseOrderLines?"
            + urlencode(grammar.bc_query("purchaseOrderLines", select=["quantity"])),
            f"{self.C}/purchaseOrders({gid(1)})/purchaseOrderLines?%24skiptoken=x",
            f"{self.C}/purchaseOrders?%24expand=purchaseOrderLines",
        ]
        for url in accepted:
            self.assertEqual(check_request("GET", url), "api.businesscentral.dynamics.com", url)

    def test_guard_refuses_other_queries_before_io(self):
        refused = [
            f"{self.C}/purchaseOrders?%24expand=salesOrderLines",
            f"{self.C}/purchaseOrders?%24expand=purchaseOrderLines(%24top%3D1)",
            f"{self.C}/purchaseOrders?%24expand=purchaseOrderLines&%24select=number",
            f"{self.C}/customers?%24expand=purchaseOrderLines",
            f"{self.C}/purchaseOrders?%24apply=aggregate(x)",
            f"{self.C}/purchaseOrders?%24search=PO",
            f"{self.C}/purchaseOrders?%24orderby=number",
            f"{self.C}/purchaseOrders?%24count=true",
            f"{self.C}/purchaseOrders?%24skip=10",
            f"{self.C}/purchaseOrders?%24filter=number+ne+%27x%27",
            f"{self.C}/purchaseOrders?%24filter=number+eq+%27a%27+or+number+eq+%27b%27",
            f"{self.C}/purchaseOrders?%24filter=startswith(number,%27PO%27)",
            f"{self.C}/purchaseOrders?%24filter=vendorName+eq+%27Acme%27",
            f"{self.C}/purchaseOrders?%24filter=number+eq+%27PO1%27&%24filter=id+eq+x",
            f"{self.C}/purchaseOrders?%24top=0",
            f"{self.C}/purchaseOrders?%24top=1001",
            f"{self.C}/purchaseOrders?%24select=number",   # 'always' fields missing
            f"{self.C}/customers?%24select=email,id,lastModifiedDateTime",
            f"{self.C}/generalLedgerEntries?%24filter=number+eq+%271%27",
            f"{self.C}/purchaseOrders?aid=FIN%20X",
            f"{self.C}/purchaseOrders?%24skiptoken=a%20b",
            f"{self.C}/purchaseOrders?%24FILTER=number+eq+%27x%27",
            f"{self.C}/purchaseOrders?x=1",
            f"{self.C}/purchaseOrders?%24top",
            f"{self.C}/purchaseOrders({gid(1)})?%24select=number",
            f"{self.C}/purchaseOrders({gid(1)})?%24expand=purchaseOrderLines",
            f"{self.C}/purchaseOrders({gid(1)})/purchaseOrderLines?%24filter=quantity+eq+1",
            f"{self.C}/purchaseOrders({gid(1)})/purchaseOrderLines?%24select=directUnitCost",
            f"{HOST}{BASE}/companies?%24filter=name+eq+%27PETRA%27",
            f"{HOST}{BASE}/companies?%24top=1",
        ]
        for url in refused:
            with self.assertRaises(ReadOnlyGuardError, msg=url):
                check_request("GET", url)

    def test_nextlink_outside_the_grammar_is_refused_before_io(self):
        # p0-f-bc: refused where it arrives, as a typed source refusal (callers'
        # SourceUnavailable handling), not later by check_request as a PermissionError.
        f = Fixture()
        nxt = f"{self.C}/vendors?%24expand=x&%24skiptoken=2"
        f.routes[f"{BASE}/companies({COMPANY})/vendors"] = lambda url: j(200, {
            "value": [doc(30, number="V1")], "@odata.nextLink": nxt})
        with self.assertRaises(SourceUnavailable) as caught:
            f.reader.list_entities(FINANCE, COMPANY, "vendors")
        self.assertTrue(is_nextlink_refusal(caught.exception))
        self.assertEqual(unavailable_detail(caught.exception), "nextlink_escaped")
        self.assertEqual(caught.exception.pages_read, 1)
        self.assertNotIn(nxt, [c[1] for c in f.sender.calls])

    def test_bc_nextlink_with_continuation_is_followed(self):
        f = Fixture()
        first = f"{BASE}/companies({COMPANY})/vendors"
        nxt = f"{self.C}/vendors?%24top=3&aid=FIN&%24skiptoken=V1"

        def route(url):
            if "skiptoken" in url:
                return j(200, {"value": [doc(32, number="V2")]})
            return j(200, {"value": [doc(31, number="V1")], "@odata.nextLink": nxt})
        f.routes[first] = route
        listing = f.reader.list_entities(FINANCE, COMPANY, "vendors", top=3)
        self.assertEqual([r["fields"]["number"] for r in listing["items"]], ["V1", "V2"])
        self.assertTrue(listing["completeness"]["complete"])


class BcEntityReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.f = Fixture()
        self.reader = self.f.reader

    def test_every_case_entity_reads_with_citations_and_exact_numbers(self):
        for entity_set in CASE_ENTITY_SETS:
            listing = self.reader.list_entities(FINANCE, COMPANY, entity_set, top=10)
            self.assertTrue(listing["completeness"]["complete"], entity_set)
            for record in listing["items"]:
                self.assertTrue(record["citation"].startswith(
                    f"bc://production/{COMPANY}/{entity_set}/{record['id']}@"), entity_set)
                self.assertEqual(record["source_acl"]["status"], "not_exposed")
        po = self.reader.list_entities(FINANCE, COMPANY, "purchaseOrders")["items"][0]
        self.assertEqual(po["fields"]["totalAmountIncludingTax"], "1234.10")
        item = self.reader.list_entities(FINANCE, COMPANY, "items")["items"][0]
        self.assertEqual(item["fields"]["unitCost"], "0.4525")
        self.assertTrue(all(method == "GET" for method, _ in self.f.entity_calls()))
        tops = {parse_qs(urlsplit(url).query).get("$top", [None])[0]
                for _, url in self.f.entity_calls()}
        self.assertIn("10", tops)

    def test_lines_by_document_with_provenance_and_both_grants(self):
        lines = self.reader.list_lines(FINANCE, COMPANY, "purchaseOrders", gid(1).upper())
        self.assertEqual([r["id"] for r in lines["items"]], [gid(901)])
        first = lines["items"][0]
        self.assertEqual(first["fields"]["quantity"], "1920")
        self.assertEqual(first["fields"]["directUnitCost"], "0.4525")
        self.assertEqual(first["entity_set"], "purchaseOrderLines")
        self.assertEqual(lines["completeness"]["provenance_dropped"], 1)
        self.assertEqual(lines["completeness"]["items_returned"], 1)
        self.assertEqual(lines["document"], {"entity_set": "purchaseOrders", "id": gid(1)})
        self.assertEqual(self.reader.resolve_citation(first["citation"], FINANCE)["status"],
                         "current")
        before = len(self.f.sender.calls)
        with self.assertRaises(NotVisible):  # buyer: header granted, lines not
            self.reader.list_lines(BUYER, COMPANY, "purchaseOrders", gid(1))
        with self.assertRaises(ValueError):
            self.reader.list_lines(FINANCE, COMPANY, "items", gid(7))
        self.assertEqual(len(self.f.sender.calls), before)

    def test_listing_truncation_is_reported(self):
        many = [doc(100 + n, number=f"V{n}") for n in range(5)]
        next_url = (f"{HOST}{BASE}/companies({COMPANY})/vendors?$skiptoken=x")
        self.f.routes[f"{BASE}/companies({COMPANY})/vendors"] = lambda url: j(200, {
            "value": many[:2], "@odata.nextLink": next_url})
        listing = self.reader.list_entities(FINANCE, COMPANY, "vendors", page_size=2,
                                            max_pages=1)
        self.assertTrue(listing["completeness"]["truncated"])
        self.assertEqual(listing["completeness"]["truncation_reason"], "page_cap")

    def test_free_filter_is_gone(self):
        with self.assertRaises(TypeError):
            self.reader.list_entities(FINANCE, COMPANY, "purchaseOrders",
                                      odata_filter="number eq 'PO5447'")


class FindByNumberTests(unittest.TestCase):
    def setUp(self) -> None:
        self.f = Fixture()
        self.reader = self.f.reader

    def test_single_none_and_exact_filter(self):
        found = self.reader.find_by_number(FINANCE, COMPANY, "purchaseOrders", "PO10123")
        self.assertEqual(found["status"], "single_candidate")
        self.assertFalse(found["ambiguous"])
        self.assertFalse(found["identity_asserted"])
        self.assertEqual(found["evidence_role"], "lead")
        self.assertTrue(found["matches"][0]["case_exact"])
        query = parse_qs(urlsplit(self.f.entity_calls()[-1][1]).query)
        self.assertEqual(query["$filter"], ["number eq 'PO10123'"])
        self.assertEqual(query["$top"], ["51"])
        none = self.reader.find_by_number(FINANCE, COMPANY, "purchaseOrders", "PO99999")
        self.assertEqual((none["status"], none["match_count"]), ("none_found", 0))

    def test_all_matches_returned_never_one_picked(self):
        self.f.routes[f"{BASE}/companies({COMPANY})/salesOrders"] = filtered(
            [self.f.so, doc(11, number="so100351")])
        found = self.reader.find_by_number(FINANCE, COMPANY, "salesOrders", "SO100351")
        self.assertEqual(found["status"], "ambiguous")
        self.assertTrue(found["ambiguous"])
        self.assertEqual(found["match_count"], 2)
        self.assertEqual(sorted(m["case_exact"] for m in found["matches"]), [False, True])
        self.assertNotIn("selected", json.dumps(found))

    def test_over_cap_is_truncated_and_ambiguous(self):
        rows = [doc(200 + n, number="PO10123") for n in range(4)]
        self.f.routes[f"{BASE}/companies({COMPANY})/purchaseOrders"] = filtered(rows)
        found = self.reader.find_by_number(FINANCE, COMPANY, "purchaseOrders", "PO10123",
                                           max_matches=3)
        self.assertEqual(found["status"], "truncated")
        self.assertTrue(found["ambiguous"])
        self.assertEqual(found["match_count"], 3)
        self.assertEqual(parse_qs(urlsplit(self.f.entity_calls()[-1][1]).query)["$top"], ["4"])

    def test_returned_value_must_equal_query(self):
        self.f.routes[f"{BASE}/companies({COMPANY})/purchaseOrders"] = lambda url: j(200, {
            "value": [doc(1, number="PO10123"), doc(12, number="PO10123-FM7")]})
        found = self.reader.find_by_number(FINANCE, COMPANY, "purchaseOrders", "PO10123")
        self.assertEqual(found["match_count"], 1)
        self.assertEqual(found["completeness"]["mismatch_dropped"], 1)

    def test_other_number_fields_and_refusals_before_io(self):
        found = self.reader.find_by_number(FINANCE, COMPANY, "salesOrders", "PO12172025",
                                           field="externalDocumentNumber")
        self.assertEqual([m["record"]["id"] for m in found["matches"]], [gid(2)])
        before = len(self.f.sender.calls)
        for kwargs in (dict(entity_set="salesOrders", number="x' or '1"),
                       dict(entity_set="salesOrders", number="SO1", field="customerName"),
                       dict(entity_set="employees", number="E1"),
                       dict(entity_set="salesOrders", number="SO1", max_matches=0)):
            with self.assertRaises(ValueError, msg=kwargs):
                self.reader.find_by_number(FINANCE, COMPANY, **kwargs)
        with self.assertRaises(NotVisible):
            self.reader.find_by_number(NOBODY, COMPANY, "salesOrders", "SO1")
        self.assertEqual(len(self.f.sender.calls), before)

    def test_withheld_records_make_the_lookup_partial(self):
        reader = BusinessCentralReader(self.f.api, LiveSourceAudience(
            {"bc:production:*": ["staff:finance"]}, tenant="plx",
            source_acl_required=["bc:*"]), tenant_id=TENANT_ID, environment="Production")
        found = reader.find_by_number(FINANCE, COMPANY, "purchaseOrders", "PO10123")
        self.assertEqual((found["status"], found["match_count"]), ("partially_withheld", 0))
        self.assertTrue(found["ambiguous"])


class CaseLeadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.f = Fixture()

    def test_sales_number_returns_cited_leads_across_companies_with_caveats(self):
        leads = bc_leads_for_number(self.f.reader, FINANCE, "SO100351", side="sales")
        self.assertEqual(leads["evidence_role"], "lead")
        self.assertFalse(leads["identity_asserted"])
        self.assertTrue(leads["complete"])
        self.assertTrue(leads["ambiguous"])
        self.assertEqual(leads["status"], "ambiguous")
        by_key = {(c["company_id"], c["entity_set"], c["id"]): c for c in leads["candidates"]}
        self.assertEqual(set(by_key), {(COMPANY, "salesOrders", gid(2)),
                                       (COMPANY, "salesOrders", gid(3)),
                                       (COMPANY, "salesInvoices", gid(5)),
                                       (COMPANY2, "salesOrders", gid(4))})
        so = by_key[(COMPANY, "salesOrders", gid(2))]
        self.assertTrue(so["citation"].startswith(f"bc://production/{COMPANY}/salesOrders/"))
        self.assertEqual(so["summary"]["totalAmountIncludingTax"], "50000.00")
        self.assertIn(CAVEATS["order_number_rewritten"], so["matched_by"][0]["caveats"])
        other = by_key[(COMPANY, "salesOrders", gid(3))]
        self.assertEqual(other["matched_by"][0]["field"], "externalDocumentNumber")
        self.assertIn(CAVEATS["external_doc_no_overwritten"], other["matched_by"][0]["caveats"])
        invoice = by_key[(COMPANY, "salesInvoices", gid(5))]
        self.assertEqual(sorted(m["field"] for m in invoice["matched_by"]),
                         ["externalDocumentNumber", "orderNumber"])  # one candidate, two routes
        self.assertTrue(all(c["identity_asserted"] is False for c in leads["candidates"]))
        self.assertIn(CAVEATS["lead_not_identity"], leads["caveats"])
        self.assertTrue(all(m == "GET" for m, _ in self.f.entity_calls()))

    def test_unpermitted_and_unavailable_searches_are_visible_gaps(self):
        self.f.routes[f"{BASE}/companies({COMPANY})/salesInvoices"] = j(400, {"error": {}})
        leads = bc_leads_for_number(self.f.reader, BUYER, "PO10123", side="both",
                                    company_ids=[COMPANY])
        statuses = {(s["entity_set"], s["field"]): s["status"] for s in leads["searches"]}
        self.assertEqual(statuses[("purchaseOrders", "number")], "single_candidate")
        self.assertEqual(statuses[("purchaseInvoices", "orderNumber")], "not_permitted")
        self.assertFalse(leads["complete"])
        self.assertTrue(leads["ambiguous"])  # one visible candidate, but gaps remain
        self.assertEqual(leads["status"], "ambiguous")
        self.assertFalse(any("purchaseInvoices" in url or "salesInvoices" in url
                             for _, url in self.f.entity_calls()))  # no I/O when not permitted
        finance = bc_leads_for_number(self.f.reader, FINANCE, "PO10123", side="both",
                                      company_ids=[COMPANY])
        failed = [s for s in finance["searches"] if s["entity_set"] == "salesInvoices"]
        self.assertTrue(failed and all(s["status"] == "unavailable" for s in failed))
        po_candidates = {c["entity_set"] for c in finance["candidates"]}
        self.assertEqual(po_candidates, {"purchaseOrders", "purchaseInvoices"})
        pinv = [c for c in finance["candidates"] if c["entity_set"] == "purchaseInvoices"][0]
        self.assertIn(CAVEATS["field_unverified_live"], pinv["matched_by"][0]["caveats"])

    def test_no_company_grant_and_bad_number(self):
        leads = bc_leads_for_number(self.f.reader, NOBODY, "PO10123")
        self.assertEqual(leads["status"], "none_found_incomplete")
        self.assertEqual(self.f.sender.calls, [])
        with self.assertRaises(ValueError):
            bc_leads_for_number(self.f.reader, FINANCE, "PO'1")
        with self.assertRaises(ValueError):
            bc_leads_for_number(self.f.reader, FINANCE, "PO1", side="hr")


class CaseEntityProbeTests(unittest.TestCase):
    def test_probe_reports_counts_and_field_names_only(self):
        f = Fixture()
        f.routes[f"{BASE}/companies({COMPANY2})/purchaseReceipts"] = j(404, {})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bc_read.main(["probe", "--case-entities"], environ=ENV,
                                sender=FakeSender(f.routes))
        self.assertEqual(code, 0, err.getvalue())
        report = json.loads(out.getvalue())
        companies = report["case_entities"]
        self.assertEqual([c["company_index"] for c in companies], [0, 1])
        first = {e["entity_set"]: e for e in companies[0]["entities"]}
        self.assertEqual(set(first), set(CASE_ENTITY_SETS))
        po = first["purchaseOrders"]
        self.assertEqual(po["records_returned"], 1)
        self.assertIn("vendorNumber", po["field_names"])
        self.assertIn("orderDate", po["select_allowlist_absent"])
        self.assertEqual(po["lines"]["records_returned"], 1)
        self.assertEqual(po["lines"]["provenance_dropped"], 1)
        self.assertEqual(po["lines"]["citation_roundtrip"], "current")
        self.assertEqual(po["citation_roundtrip"], "current")
        second = {e["entity_set"]: e for e in companies[1]["entities"]}
        self.assertTrue(second["purchaseReceipts"]["error"].startswith("SourceNotFound"))
        self.assertTrue(second["items"]["error"].startswith("SourceNotFound"))
        rendered = out.getvalue() + err.getvalue()
        for leaked in (CLIENT_SECRET, ACCESS_TOKEN, "PETRA", "DISTRIBUTION", "PO10123",
                       "SO100351", "PO12172025", "1234", "0.4525", "Supplier Co", "V0042",
                       COMPANY, COMPANY2, gid(1)):
            self.assertNotIn(leaked, rendered, leaked)
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bc_read.main(["probe", "--sample", "9"], environ=ENV,
                                          sender=FakeSender(f.routes)), 1)

    def test_finance_probe_reports_select_acceptance_and_unexposed_sets_as_gaps(self):
        f = Fixture()
        c1 = f"{BASE}/companies({COMPANY})"
        entry = doc(20, entryNumber=7, documentNumber="PPI-1", itemNumber="DVS-5003",
                    quantity="#1920")
        # The BC API v2.0 agedAccountsReceivable fields: a report row, no lastModifiedDateTime.
        aged = {"customerId": gid(21), "customerNumber": "C0089", "name": "Customer",
                "currencyCode": "USD", "balanceDue": "#1234.10", "currentAmount": 0,
                "period1Amount": 0, "period2Amount": 0, "period3Amount": 0,
                "agedAsOfDate": "2026-09-30", "periodLengthFilter": "30D"}

        def ledger(url: str):  # BC answers 400 to a $select naming a field it lacks
            if "salesAmountActual" in parse_qs(urlsplit(url).query).get("$select", [""])[0]:
                return j(400, {"error": {"code": "BadRequest"}})
            return filtered([entry])(url)
        f.routes[f"{c1}/itemLedgerEntries"] = ledger
        f.routes[f"{c1}/agedAccountsReceivables"] = filtered([aged])
        f.routes[f"{c1}/agedAccountsReceivables({gid(21)})"] = (
            lambda url: (200, {}, raw_json(aged).encode()))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bc_read.main(["probe", "--finance-entities"], environ=ENV,
                                          sender=FakeSender(f.routes)), 0)
        report = json.loads(out.getvalue())
        first = {e["entity_set"]: e for e in report["finance_entities"][0]["entities"]}
        self.assertEqual(set(first), set(bc_read.FINANCE_ENTITY_SETS))
        ile = first["itemLedgerEntries"]
        self.assertEqual((ile["records_returned"], ile["select_accepted"], ile["select_error"]),
                         (1, False, "http_400:bad_request"))
        self.assertEqual(report["finance_select_rejected"], ["itemLedgerEntries"])
        receivables = first["agedAccountsReceivables"]
        self.assertEqual((receivables["records_returned"], receivables["has_etag"],
                          receivables["citation_roundtrip"], receivables["select_accepted"],
                          receivables["select_allowlist_absent"]),
                         (1, False, "current", True, []))
        # Every set a company answers 404 for is a coded gap, not an error or zero rows.
        self.assertIn({"company_index": 0, "entity_set": "agedAccountsPayables"},
                      report["finance_not_exposed"])
        self.assertTrue(first["agedAccountsPayables"]["not_exposed"])
        self.assertEqual(report["finance_errors"], 0)
        for leaked in ("PPI-1", "DVS-5003", "C0089", "1234", COMPANY, gid(21)):
            self.assertNotIn(leaked, out.getvalue(), leaked)
        # The live check fails on exactly the rejected allowlist, never on a coded gap, and
        # on My Company's absence (this fixture's one company is not the pinned required
        # book, directive 8a8b59c65f56).
        from devloop import live_verify
        spec = json.loads((Path(__file__).resolve().parents[2] / "devloop" / "live-checks"
                           / "bc.ledgers.json").read_text())
        self.assertEqual(live_verify.check_expectations(spec, 0, report), [
            "finance_required_unserved not empty",
            "finance_select_rejected not empty",
            "finance_entities[*].entities[*].select_allowlist_absent not empty"])

    def test_each_failed_entity_read_names_its_set_status_and_code_class(self):
        """bc.case_entities used to say only "error present"; now each failure names its
        entity set, part, HTTP status and error-code class, never the body text."""
        f = Fixture()
        planted = "PLANTED-BODY-do-not-leak " + ACCESS_TOKEN
        c1 = f"{BASE}/companies({COMPANY})"
        f.routes[f"{c1}/vendors"] = j(403, {"error": {"code": "Authorization_RequestDenied",
                                                      "message": planted}})
        f.routes[f"{c1}/purchaseOrders({gid(1)})/purchaseOrderLines"] = j(
            400, {"error": {"code": "BadRequest_NotFound", "message": planted}})
        f.routes[f"{c1}/items"] = j(500, {"error": {"code": "Custom" + CLIENT_SECRET,
                                                    "message": planted}})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bc_read.main(["probe", "--case-entities"], environ=ENV,
                                          sender=FakeSender(f.routes)), 0)
        report = json.loads(out.getvalue())
        errors = {(e["company_index"], e["entity_set"], e["part"]): e
                  for e in report["case_entity_errors"]}
        self.assertEqual(errors[(0, "vendors", "header")], {
            "company_index": 0, "entity_set": "vendors", "part": "header",
            "error_class": "SourceUnavailable", "http_status": 403,
            "error_code": "authorization_request_denied", "detail": "http_403"})
        lines = errors[(0, "purchaseOrderLines", "lines")]
        self.assertEqual((lines["http_status"], lines["error_code"], lines["detail"]),
                         (400, "bad_request_not_found", "http_400"))
        # A code outside the fixed vocabulary is a class, never the code text.
        self.assertEqual((errors[(0, "items", "header")]["http_status"],
                          errors[(0, "items", "header")]["error_code"]), (500, "other"))
        # Company 2 lacks most sets: Graph-style 404s, each named.
        self.assertEqual(errors[(1, "items", "header")]["error_class"], "SourceNotFound")
        self.assertEqual(errors[(1, "items", "header")]["http_status"], 404)
        for leaked in ("PLANTED", ACCESS_TOKEN, CLIENT_SECRET, COMPANY, COMPANY2):
            self.assertNotIn(leaked, out.getvalue(), leaked)
        # The live check still fails on any error and now records which set failed.
        from devloop import live_verify
        spec = json.loads((Path(__file__).resolve().parents[2] / "devloop" / "live-checks"
                           / "bc.case_entities.json").read_text())
        self.assertIn("case_entities[*].entities[*].error present",
                      live_verify.check_expectations(spec, 0, report))
        recorded = live_verify._recorded(spec, report)
        pairs = list(zip(recorded["case_entity_errors[*].entity_set"],
                         recorded["case_entity_errors[*].http_status"],
                         recorded["case_entity_errors[*].error_code"]))
        self.assertIn(("vendors", 403, "authorization_request_denied"), pairs)
        self.assertIn(("purchaseOrderLines", 400, "bad_request_not_found"), pairs)


if __name__ == "__main__":
    unittest.main()
