"""Versioned BC capture (``bc_read.BcCapture`` into ``capture_store``), injected transport.

No network and no credentials: every request goes to the in-process ``FakeBc``
below, which serves three companies x the capture entity sets with server-driven
paging (``@odata.nextLink`` + ``$skiptoken``), document lines by the nested
navigation, scripted throttles/failures, and records every call. Evidence label:
``injected-transport``. Live behaviour is checked by
live check ``bc.capture_full`` on reviewed main (live-unverified).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock
import uuid
from urllib.parse import parse_qs, quote, unquote, urlsplit

from devloop import live_verify

from integrations import bc_read, capture_store
from integrations import live_source_grammar as grammar
from integrations.bc_read import CAPTURE_ENTITY_SETS, BcCapture, BusinessCentralReader
from integrations.capture_store import CaptureError, CaptureStore, bc_unit, is_capture_handle
from integrations.live_source_grammar import (BC_DOCUMENT_LINES, BC_LINE_PARENT_FIELD,
                                              BC_RECORD_KEY_FIELD)
from integrations.live_source_guard import (BoundedBackoff, LiveSourceAudience,
                                            RequestBudgetExhausted, check_request)
from integrations.tests.test_live_source_adapters import ACCESS_TOKEN, CLIENT_SECRET, ENV, TENANT_ID
from ontology.core import Actor
from operating import claim_checks

HOST = "https://api.businesscentral.dynamics.com"
BASE = f"/v2.0/{TENANT_ID}/Production/api/v2.0"
ODATA = f"/v2.0/{TENANT_ID}/Production/ODataV4"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

# Three pinned companies (test GUIDs; the real pins live in bc_company_books.json) and one
# the book map does not pin. Names are deliberately misleading: books follow GUIDs only.
CO = ["c0000000-0000-4000-8000-000000000001", "c0000000-0000-4000-8000-000000000002",
      "c0000000-0000-4000-8000-000000000003"]
UNPINNED = "c0000000-0000-4000-8000-0000000000ff"
NAMES = {CO[0]: "PLX Distribution", CO[1]: "My Company", CO[2]: "PLX Consol",
         UNPINNED: "CRONUS Test"}
BOOKS = {"production": {CO[0]: "manufacturing", CO[1]: "distribution", CO[2]: "consol"}}

SERVICE = Actor("plx", frozenset({"svc:capture"}), "restricted")
BUYER = Actor("plx", frozenset({"staff:buyer"}), "restricted")
NOBODY = Actor("plx", frozenset({"staff:nobody"}), "restricted")
HEADERS_ONLY = Actor("plx", frozenset({"staff:headers"}), "restricted")


def audience() -> LiveSourceAudience:
    return LiveSourceAudience({
        "bc:production:*": ["svc:capture"],
        "bc:production:companies": ["svc:capture", "staff:buyer"],
        f"bc:production:{CO[0]}:purchaseorders": ["svc:capture", "staff:buyer",
                                                  "staff:headers"],
        f"bc:production:{CO[0]}:purchaseorderlines": ["svc:capture", "staff:buyer"],
    }, tenant="plx", default_classification="restricted")


def parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def gid(company: int, set_index: int, n: int) -> str:
    return f"{company + 1:08x}-{set_index + 1:04x}-4000-8000-{n:012x}"


class FakeBc:
    """BC API v2.0 over three companies; pages follow ``Prefer: odata.maxpagesize``."""

    LINE_SETS = set(BC_DOCUMENT_LINES.values())

    def __init__(self, sizes: dict[str, int] | None = None, *, companies=None,
                 names: dict[str, str] | None = None) -> None:
        self.companies = list(companies or [*CO, UNPINNED])
        self.names = dict(NAMES if names is None else names)
        sizes = sizes or {}
        self.data: dict[tuple[str, str], list[dict]] = {}
        self.lines: dict[tuple[str, str, str], list[dict]] = {}
        for ci, company in enumerate(self.companies):
            for si, entity_set in enumerate(CAPTURE_ENTITY_SETS):
                rows = []
                key = BC_RECORD_KEY_FIELD.get(entity_set, "id")
                for n in range(sizes.get(entity_set, 3)):
                    rows.append({"@odata.etag": f'W/"JzE{ci}{si}{n};"', key: gid(ci, si, n),
                                 "number": f"N{ci}{si}{n:04d}",
                                 "lastModifiedDateTime": "2026-09-30T10:00:00Z",
                                 "amount": f"#{n}.50"})
                    line_set = BC_DOCUMENT_LINES.get(entity_set)
                    if line_set:
                        parent = BC_LINE_PARENT_FIELD.get(line_set, "documentId")
                        self.lines[(company, entity_set, gid(ci, si, n))] = [
                            {"@odata.etag": f'W/"L{ci}{si}{n}{k}"',
                             "id": gid(ci, si + 20, n * 10 + k), parent: gid(ci, si, n),
                             "sequence": 10000 * (k + 1), "quantity": f"#{12 + k}"}
                            for k in range(2)]
                self.data[(company, entity_set)] = rows
        self.calls: list[tuple[str, str, dict]] = []
        self.script: list[tuple[re.Pattern, tuple]] = []
        # ODataV4 Page 50 ``PurchaseOrder`` rows per company (None: one per purchase order,
        # ``No`` its number, a synthetic ``Your_Reference``, the order's vendor number).
        self.references: dict[str, list[dict]] = {}

    def fail(self, pattern: str, *responses: tuple) -> None:
        for response in responses:
            self.script.append((re.compile(pattern), response))

    def fail_lines(self, document: str, *responses: tuple,
                   header: str = "purchaseOrders") -> None:
        """Fail one document's line reads both ways, the nested navigation and the flat
        read filtered by documentId (one response per GET, in order)."""
        lines = BC_DOCUMENT_LINES[header]
        flat = rf"{lines}\?.*documentId[+ ]eq[+ ]{document}"
        self.fail(rf"(?:{header}\({document}\)/{lines}|{flat})", *responses)

    @staticmethod
    def body(value) -> bytes:
        return re.sub(r'"#([0-9.]+)"', r"\1", json.dumps(value)).encode()

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append((method, url, dict(headers)))
        parts = urlsplit(url)
        if parts.hostname == "login.microsoftonline.com":
            return 200, {}, json.dumps({"access_token": ACCESS_TOKEN, "expires_in": 3599}).encode()
        assert method == "GET", "the BC fake only ever sees GETs"
        for index, (pattern, response) in enumerate(self.script):
            if pattern.search(unquote(url)):
                del self.script[index]
                return response
        path = unquote(parts.path)
        if path == f"{BASE}/companies":
            return 200, {}, self.body({"value": [
                {"id": c, "name": self.names.get(c, "X"), "displayName": self.names.get(c, "X"),
                 "@odata.etag": 'W/"c"'} for c in self.companies]})
        odata = re.fullmatch(re.escape(ODATA) + r"/Company\('([^']+)'\)/(\w+)", path)
        if odata is not None:
            return self.odata(odata.group(1), odata.group(2), parts, headers)
        found = re.fullmatch(re.escape(BASE) + r"/companies\(([^)]+)\)/(\w+)"
                             r"(?:\(([^)]+)\)(?:/(\w+))?)?", path)
        if found is None:
            return 404, {}, b'{"error":{"code":"BadRequest_NotFound"}}'
        company, entity_set, document, line_set = found.groups()
        if document is not None and line_set is None:  # get by id
            key = BC_RECORD_KEY_FIELD.get(entity_set, "id")
            found = [r for r in self.data.get((company, entity_set), []) if r[key] == document]
            return (200, {}, self.body(found[0])) if found else (
                404, {}, b'{"error":{"code":"BadRequest_NotFound"}}')
        if document is not None:
            rows = self.lines.get((company, entity_set, document))
            if rows is None:
                return 404, {}, b'{"error":{"code":"BadRequest_NotFound"}}'
        elif entity_set in self.LINE_SETS:  # the flat read: lines filtered by documentId
            field, op, value = parse_qs(parts.query)["$filter"][0].split(" ")
            assert (field, op) == ("documentId", "eq"), field
            rows = next((v for (c, _h, d), v in self.lines.items()
                         if c == company and d == value and v and
                         BC_DOCUMENT_LINES[_h] == entity_set), [])
        else:
            rows = self.data.get((company, entity_set))
            if rows is None:
                return 404, {}, b'{"error":{"code":"BadRequest_NotFound"}}'
        size = int(re.search(r"maxpagesize=(\d+)", headers.get("Prefer", "maxpagesize=100"))[1])
        query = parse_qs(parts.query)
        offset = self.offset(parts.path, query.get("$skiptoken", [None])[0])
        if offset is None:
            return 400, {}, b'{"error":{"code":"BadRequest"}}'
        kept = {k: v[0] for k, v in query.items() if k in ("$filter", "$select", "$expand")}
        if "$filter" in kept and kept["$filter"].startswith("documentId "):
            pass  # applied above
        elif "$filter" in kept:  # the forms the grammar builds
            text = kept["$filter"]
            if text.startswith("status ne 'Draft'"):  # posted documents only
                rows = [r for r in rows if r.get("status") != "Draft"]
                text = text[len("status ne 'Draft'"):].removeprefix(" and ")
            if text:
                field, op, instant = text.split(" ")
                assert (field, op) == ("lastModifiedDateTime", "ge"), kept
                rows = [r for r in rows if parse_utc(r["lastModifiedDateTime"])
                        >= parse_utc(instant)]
        if "$select" in kept:
            names = kept["$select"].split(",") + ["@odata.etag"]
            rows = [{k: r[k] for k in names if k in r} for r in rows]
        page = {"value": rows[offset:offset + size]}
        if offset + size < len(rows):
            # BC keeps the original query options in the continuation (UNVERIFIED live).
            extra = "".join(f"&{k}={quote(v, safe=',:')}" for k, v in sorted(kept.items()))
            page["@odata.nextLink"] = (f"{HOST}{parts.path}?aid=FINANCE{extra}&$skiptoken="
                                       f"{self.token(parts.path, offset + size)}")
        return 200, {}, self.body(page)

    def reference_rows(self, company: str) -> list[dict]:
        if company in self.references:
            return self.references[company]
        return [{"@odata.etag": f'W/"R{r["number"]}"', "No": r["number"],
                 "Your_Reference": f"FM-{r['number']}",
                 "Buy_from_Vendor_No": r.get("vendorNumber", "")}
                for r in self.data.get((company, "purchaseOrders"), [])]

    def odata(self, name: str, entity: str, parts, headers):
        """ODataV4 ``Company('<name>')/PurchaseOrder``: the page's key (``No``) pages it,
        as ``$skiptoken='<last No>'`` (UNVERIFIED live: the token shape)."""
        company = next((c for c in self.companies if self.names.get(c) == name), None)
        if company is None or entity != "PurchaseOrder":
            return 404, {}, b'{"error":{"code":"BadRequest_NotFound"}}'
        query = parse_qs(parts.query)
        assert query.get("$select") == [grammar.BC_ODATA_PO_SELECT], query
        rows = self.reference_rows(company)
        token = query.get("$skiptoken", [None])[0]
        offset = 0
        if token is not None:
            numbers = [r.get("No") for r in rows]
            last = token.strip("'")
            if last not in numbers:
                return 400, {}, b'{"error":{"code":"BadRequest"}}'
            offset = numbers.index(last) + 1
        size = int(re.search(r"maxpagesize=(\d+)", headers.get("Prefer", "maxpagesize=100"))[1])
        names = [*grammar.BC_ODATA_PO_FIELDS, "@odata.etag"]
        page = {"value": [{k: r[k] for k in names if k in r}
                          for r in rows[offset:offset + size]]}
        if offset + size < len(rows):
            page["@odata.nextLink"] = (
                f"{HOST}{parts.path}?$select={quote(grammar.BC_ODATA_PO_SELECT, safe=',')}"
                f"&$skiptoken={quote(repr(rows[offset + size - 1]['No']), safe='')}")
        return 200, {}, self.body(page)

    def offset(self, path: str, token: str | None) -> int | None:
        """The row offset a ``$skiptoken`` continues at (None: BC answers 400)."""
        return int(token or 0)

    def token(self, path: str, offset: int) -> str:
        return str(offset)

    def bc_gets(self, pattern: str = "") -> list[str]:
        return [url for method, url, _ in self.calls
                if "businesscentral" in url and re.search(pattern, unquote(url))]


class Clock:
    def __init__(self) -> None:
        self.now = T0
        self.ticks = 0.0

    def __call__(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        self.ticks += 1.0
        return self.ticks


class Harness:
    def __init__(self, test: unittest.TestCase, fake: FakeBc | None = None, *,
                 db: Path | None = None, books="injected") -> None:
        self.fake = fake or FakeBc()
        self.clock = Clock()
        self.sleeps: list[float] = []
        self.retry = BoundedBackoff(sleep=self.sleeps.append, jitter=lambda: 1.0,
                                    clock=self.clock)
        api, self.transport, tenant, environment = bc_read.build_bc(ENV, sender=self.fake,
                                                                     retry=self.retry)
        self.audience = audience()
        self.reader = BusinessCentralReader(api, self.audience, tenant_id=tenant,
                                            environment=environment)
        if db is None:
            scratch = tempfile.TemporaryDirectory()
            test.addCleanup(scratch.cleanup)
            db = Path(scratch.name) / "capture.sqlite"
        self.db = db
        self.store = CaptureStore(db, self.audience, clock=self.clock)
        test.addCleanup(self.store.close)
        # books="shipped": the pins committed in bc_company_books.json, as production runs.
        self.capture = BcCapture(self.reader, self.store,
                                 books=BOOKS if books == "injected" else None,
                                 monotonic=self.clock.monotonic)

    def run(self, **kwargs):
        kwargs.setdefault("page_size", 2)
        return self.capture.capture(SERVICE, **kwargs)

    def unit(self, report, company: str, entity_set: str) -> dict:
        entry = next(c for c in report["companies"] if c["company_id"] == company)
        return next(u for u in entry["units"] if u["entity_set"] == entity_set)

    def handles(self, company: str, entity_set: str, actor=SERVICE) -> list[str]:
        return [i["citation"] for i in self.store.records(
            actor, "bc", bc_unit(company, entity_set), limit=1000)["items"]]


ALL_UNITS = len(CAPTURE_ENTITY_SETS) + len(BC_DOCUMENT_LINES) + 1  # + the company record


SHIPPED = bc_read.load_company_books()["production"]
MY_COMPANY = next(iter(SHIPPED))  # the first pin; the next test needs it to be the only one
OTHERS = ["c1000000-0000-4000-8000-000000000001", "c1000000-0000-4000-8000-000000000002",
          "c1000000-0000-4000-8000-000000000003"]
PRODUCTION_NAMES = {OTHERS[0]: "CRONUS Test", OTHERS[1]: "PLX Consol",
                    OTHERS[2]: "PLX Distribution", MY_COMPANY: "My Company"}


class MyCompanyScopeTests(unittest.TestCase):
    """Production reads only My Company: a listing with CRONUS,
    PLX Consol, PLX Distribution and My Company captures only the pinned GUID."""

    def listing(self, **kwargs) -> FakeBc:
        return FakeBc(kwargs.pop("sizes", {"purchaseOrders": 5}),
                      companies=[OTHERS[0], OTHERS[1], MY_COMPANY, OTHERS[2]],
                      names=kwargs.pop("names", PRODUCTION_NAMES))

    def test_production_pins_exactly_one_company(self):
        self.assertEqual(list(SHIPPED), [MY_COMPANY])

    def test_only_the_pinned_my_company_is_captured_and_proved_by_counts(self):
        h = Harness(self, self.listing(), books="shipped")
        report = h.run()
        self.assertEqual(report["companies_listed"], 4)
        self.assertEqual([c["company_id"] for c in report["companies"]], [MY_COMPANY])
        for other in OTHERS:
            self.assertFalse(h.fake.bc_gets(other), "a company outside scope was read")
        self.assertEqual(report["unpinned_companies"], 0)
        self.assertEqual(report["out_of_scope_companies"], 3)
        self.assertEqual(report["pinned_name_mismatch"], 0)
        self.assertTrue(report["all_complete"])
        proof = report["company_proof"]
        self.assertEqual(proof, {"environment": "Production", "company_name": "My Company",
                                 "po_count": 5})
        text = json.dumps(proof)
        self.assertNotIn("N0", text)  # no PO number; the fake numbers are N<company><set>...
        self.assertNotIn(MY_COMPANY, text)

    def test_a_pinned_guid_listed_under_another_name_is_not_captured(self):
        names = dict(PRODUCTION_NAMES, **{MY_COMPANY: "CRONUS (Test)"})
        h = Harness(self, self.listing(names=names), books="shipped")
        report = h.run()
        self.assertEqual(report["companies"], [])
        self.assertEqual(report["pinned_name_mismatch"], 1)
        self.assertIsNone(report["company_proof"])
        self.assertFalse(report["all_complete"])
        self.assertFalse(h.fake.bc_gets(MY_COMPANY))

    def test_an_unreached_purchase_order_count_is_null_not_zero(self):
        fake = self.listing()
        fake.fail(rf"{MY_COMPANY}\)/purchaseOrders",
                  (404, {}, b'{"error":{"code":"BadRequest_NotFound"}}'))
        report = Harness(self, fake, books="shipped").run()
        self.assertEqual(report["company_proof"], {
            "environment": "Production", "company_name": "My Company", "po_count": None})

    def test_the_record_of_the_probe_summary_passes_the_live_check_spec(self):
        spec = json.loads((Path(__file__).resolve().parents[2] / "devloop" / "live-checks"
                           / "bc.capture_full.json").read_text(encoding="utf-8"))
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        result = bc_read.capture_probe(
            ENV, sender=self.listing(), db=Path(scratch.name) / "c.sqlite", page_size=2,
            resolve_sample=3, passes=2, mode="full", monotonic=Clock().monotonic,
            retry=BoundedBackoff(sleep=lambda s: None))
        self.assertEqual(result["companies_captured"], 1)
        self.assertEqual(set(result["company_proof"]), {"environment", "company_name",
                                                         "po_count"})
        # injected-transport is the only mark the fixture run is expected to fail on
        self.assertEqual(live_verify.check_expectations(spec, 0, result),
                         ["evidence_label != expected"])
        recorded = live_verify._recorded(spec, result)
        self.assertEqual(recorded["company_proof.environment"], "Production")
        self.assertEqual(recorded["company_proof.po_count"], 5)
        wrong = dict(result, company_proof=dict(result["company_proof"], company_name="CRONUS"))
        self.assertIn("company_proof.company_name != expected",
                      live_verify.check_expectations(spec, 0, wrong))

    def test_payment_journals_are_read_get_only_from_my_company_alone(self):
        h = Harness(self, self.listing(sizes={"customerPaymentJournals": 2,
                                              "vendorPaymentJournals": 3}), books="shipped")
        report = h.run(entity_sets=("customerPaymentJournals", "vendorPaymentJournals"))
        for name, count in (("customerPaymentJournals", 2), ("vendorPaymentJournals", 3)):
            unit = h.unit(report, MY_COMPANY, name)
            self.assertEqual((unit["state"], unit["records_current"]), ("complete", count))
            self.assertEqual(len(h.handles(MY_COMPANY, name)), count)
        for other in OTHERS:
            self.assertFalse(h.fake.bc_gets(other), "a company outside scope was read")
        self.assertTrue(h.fake.bc_gets(r"PaymentJournals"))
        self.assertEqual({m for m, u, _h in h.fake.calls if "/api/" in u or "businesscentral" in u} - {"GET"}, set())

    def test_a_draft_sales_invoice_is_never_read_as_posted(self):
        fake = FakeBc({"salesInvoices": 3})
        fake.data[(CO[0], "salesInvoices")][1]["status"] = "Draft"
        for n in (0, 2):
            fake.data[(CO[0], "salesInvoices")][n]["status"] = "Open"
        h = Harness(self, fake)
        report = h.run(entity_sets=("salesInvoices",))
        drafted = gid(0, CAPTURE_ENTITY_SETS.index("salesInvoices"), 1)
        unit = h.unit(report, CO[0], "salesInvoices")
        self.assertEqual((unit["state"], unit["records_current"]), ("complete", 2))
        rows = h.store.records(SERVICE, "bc", bc_unit(CO[0], "salesInvoices"),
                               limit=100)["items"]
        self.assertEqual(sorted(r["row"]["status"] for r in rows), ["Open", "Open"])
        self.assertEqual(h.unit(report, CO[0], "salesInvoiceLines")["records_current"], 4)
        for url in h.fake.bc_gets(r"salesInvoices\?"):
            self.assertIn("status ne 'Draft'", unquote(url).replace("+", " "))
            check_request("GET", url, environ=ENV)  # the GET guard sends the same query
        self.assertFalse(h.fake.bc_gets(drafted))

    def test_the_posted_only_filter_is_closed_to_other_sets_and_forms(self):
        built = grammar.bc_query("salesInvoices", posted_only=True)
        self.assertEqual(built, {"$filter": "status ne 'Draft'"})
        both = grammar.bc_query("salesInvoices", posted_only=True,
                                modified_since="2026-10-01T00:00:00.000Z")
        self.assertEqual(both["$filter"], "status ne 'Draft' and lastModifiedDateTime ge "
                                          "2026-10-01T00:00:00.000Z")
        for query in (built, both, grammar.bc_query("salesInvoices", posted_only=True,
                                                    keys_only=True)):
            self.assertTrue(grammar.bc_query_ok("salesInvoices", "&".join(
                f"{k}={quote(v, safe=',')}" for k, v in query.items())))
        with self.assertRaises(ValueError):
            grammar.bc_query("purchaseOrders", posted_only=True)
        for raw in ("$filter=status%20ne%20%27Open%27", "$filter=status%20ne%20%27Draft%27%20or%20x"):
            self.assertFalse(grammar.bc_query_ok("salesInvoices", raw), raw)


class FullCaptureTests(unittest.TestCase):
    def test_three_companies_every_entity_set_paged_complete_with_book_lineage(self):
        h = Harness(self, FakeBc({"purchaseOrders": 5, "itemLedgerEntries": 7}))
        report = h.run()
        self.assertEqual(report["evidence_label"], "injected-transport")
        self.assertEqual(report["companies_listed"], 4)
        self.assertEqual(report["unpinned_companies"], 1)  # never read, only counted
        self.assertFalse(h.fake.bc_gets(UNPINNED))
        self.assertEqual([c["book"] for c in report["companies"]],
                         ["manufacturing", "distribution", "consol"])
        self.assertEqual(ALL_UNITS, 36)  # bc.capture_full requires units_total >= 93
        self.assertEqual(report["units_total"], 3 * ALL_UNITS)
        self.assertEqual(report["units_complete"], 3 * ALL_UNITS)
        self.assertFalse(report["all_complete"])  # an unpinned company is not captured
        self.assertIsNone(report["stopped_reason"])
        po = h.unit(report, CO[0], "purchaseOrders")
        self.assertEqual((po["state"], po["records_current"], po["pages"]), ("complete", 5, 3))
        lines = h.unit(report, CO[0], "purchaseOrderLines")
        self.assertEqual((lines["state"], lines["records_current"]), ("complete", 10))
        self.assertEqual(h.unit(report, CO[2], "itemLedgerEntries")["records_current"], 7)
        self.assertEqual(h.unit(report, CO[1], "salesShipments")["state"], "complete")
        # Lineage follows the company GUID, never the (misleading) company name.
        for company, book in BOOKS["production"].items():
            item = h.store.records(SERVICE, "bc", bc_unit(company, "salesInvoiceLines"),
                                   limit=1)["items"][0]
            self.assertEqual(item["lineage"], f"bc_{book}")
            self.assertEqual(item["audience_key"], f"bc:{company}/salesInvoiceLines")
        # Pages after the first are requested with the server's continuation only.
        self.assertTrue(h.fake.bc_gets(
            r"purchaseOrders\?aid=FINANCE&\$expand=purchaseOrderLines&\$skiptoken=2$"))
        # The company record is a unit of its own, from the listing (no extra GET).
        company = h.unit(report, CO[1], "companies")
        self.assertEqual((company["state"], company["records_current"]), ("complete", 1))
        record = h.store.records(SERVICE, "bc", bc_unit(CO[1], "companies"))["items"][0]
        self.assertEqual((record["row"]["id"], record["row"]["displayName"], record["lineage"]),
                         (CO[1], NAMES[CO[1]], "bc_distribution"))
        self.assertFalse(h.fake.bc_gets(rf"companies\({CO[1]}\)/companies"))

    def test_credit_memos_quotes_and_payment_journals_are_paged_with_their_lines(self):
        headers = ("salesCreditMemos", "purchaseCreditMemos", "salesQuotes",
                   "customerPaymentJournals", "vendorPaymentJournals")
        h = Harness(self, FakeBc({name: 5 for name in headers}))
        report = h.run(entity_sets=headers)
        self.assertEqual({m for m, url, _ in h.fake.calls if "businesscentral" in url}, {"GET"})
        for header in headers:
            line_set = BC_DOCUMENT_LINES[header]
            unit = h.unit(report, CO[0], header)
            self.assertEqual((unit["state"], unit["records_current"], unit["pages"]),
                             ("complete", 5, 3), header)
            self.assertTrue(h.fake.bc_gets(
                rf"{header}\?aid=FINANCE&\$expand={line_set}&\$skiptoken=2$"))
            lines = h.unit(report, CO[0], line_set)
            self.assertEqual((lines["state"], lines["records_current"]), ("complete", 10))
            document = gid(0, CAPTURE_ENTITY_SETS.index(header), 0)
            self.assertEqual(len(h.fake.bc_gets(rf"{header}\({document}\)/{line_set}")), 1)
            line = h.store.records(SERVICE, "bc", bc_unit(CO[0], line_set),
                                   limit=1)["items"][0]
            self.assertEqual((line["lineage"], line["audience_key"]),
                             ("bc_manufacturing", f"bc:{CO[0]}/{line_set}"))
            self.assertTrue(h.store.resolve(line["citation"], SERVICE)["ok"])

    def test_aged_balances_are_keyed_by_party_and_an_unexposed_set_is_a_coded_gap(self):
        sets = ("agedAccountsReceivables", "agedAccountsPayables")
        fake = FakeBc({name: 3 for name in sets})
        for row in fake.data[(CO[0], "agedAccountsReceivables")]:
            del row["@odata.etag"]  # a recomputed report row may carry no etag
        not_found = (404, {}, b'{"error":{"code":"BadRequest_NotFound"}}')
        fake.fail(rf"{CO[0]}\)/agedAccountsPayables$", not_found)
        # A 404 on a later page is no unexposed set: the set answered its first page.
        fake.fail(rf"{CO[2]}\)/agedAccountsPayables\?aid=FINANCE&\$skiptoken=2$", not_found)
        h = Harness(self, fake)
        report = h.run(entity_sets=sets)
        receivables = h.unit(report, CO[0], "agedAccountsReceivables")
        self.assertEqual((receivables["state"], receivables["records_current"]), ("complete", 3))
        record = h.store.records(SERVICE, "bc", bc_unit(CO[0], "agedAccountsReceivables"),
                                 limit=1)["items"][0]
        self.assertEqual(record["row"]["customerId"],
                         gid(0, CAPTURE_ENTITY_SETS.index("agedAccountsReceivables"), 0))
        self.assertIn(f"/agedAccountsReceivables/{record['row']['customerId']}@",
                      record["citation"])
        self.assertTrue(h.store.resolve(record["citation"], SERVICE)["ok"])
        payables = h.unit(report, CO[0], "agedAccountsPayables")
        self.assertEqual((payables["state"], payables["reason"], payables["complete"]),
                         ("failed", "not_exposed", False))
        self.assertTrue(h.unit(report, CO[1], "agedAccountsPayables")["complete"])
        self.assertEqual((h.unit(report, CO[2], "agedAccountsPayables")["state"],
                          h.unit(report, CO[2], "agedAccountsPayables")["reason"]),
                         ("failed", "source_refused"))
        self.assertEqual(report["units_not_exposed"], 1)
        self.assertFalse(report["all_complete"])

    def test_bc_capture_full_holds_a_coded_gap_without_failing_on_it(self):
        # The shipped pin (Production / My Company only) against the real spec.
        fake = FakeBc({"purchaseOrders": 4}, companies=[OTHERS[0], MY_COMPANY, OTHERS[1]],
                      names=PRODUCTION_NAMES)
        fake.fail(rf"{MY_COMPANY}\)/agedAccountsPayables$",
                  *[(404, {}, b'{"error":{"code":"BadRequest_NotFound"}}')] * 2)  # both passes
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        report = bc_read.capture_probe(
            ENV, sender=fake, db=Path(scratch.name) / "c.sqlite", page_size=2,
            resolve_sample=3, passes=2, monotonic=Clock().monotonic,
            retry=BoundedBackoff(sleep=lambda s: None))
        self.assertEqual((report["units_total"], report["units_not_exposed"],
                          report["all_complete"]), (ALL_UNITS + 1, 1, False))
        # + 1: the Page 50 purchaseOrderReferences unit of the pinned company.
        spec = json.loads((Path(__file__).resolve().parents[2] / "devloop" / "live-checks"
                           / "bc.capture_full.json").read_text(encoding="utf-8"))
        # Only the fixture's own mark fails it: injected transport.
        self.assertEqual(live_verify.check_expectations(spec, 0, report),
                         ["evidence_label != expected"])
        self.assertEqual(live_verify._recorded(spec, report)["units_not_exposed"], 1)

    def test_the_capture_checks_complete_on_unserved_sets_and_name_other_gaps(self):
        # complete_when over the real capture_probe output: units a later ask backs are
        # accounted, any other incomplete unit still holds the check at incomplete.
        unserved = (400, {}, b'{"error":{"code":"Internal_DataNotFoundFilter"}}')
        for name, passes in (("bc.capture_full", 2), ("bc.capture_incremental", 3)):
            spec = json.loads((Path(__file__).resolve().parents[2] / "devloop" / "live-checks"
                               / f"{name}.json").read_text(encoding="utf-8"))
            for refusing, complete in (((CO[1], CO[2]), True), (CO, False)):
                fake = FakeBc({}, companies=CO)
                for company in refusing:
                    fake.fail(rf"{company}\)/customerPaymentJournals", *[unserved] * (2 * passes))
                scratch = tempfile.TemporaryDirectory()
                self.addCleanup(scratch.cleanup)
                report = bc_read.capture_probe(
                    ENV, sender=fake, db=Path(scratch.name) / "c.sqlite", page_size=2,
                    resolve_sample=0, passes=passes, books=BOOKS, monotonic=Clock().monotonic,
                    retry=BoundedBackoff(sleep=lambda s: None), key_pass_last=passes == 3)
                with self.subTest(name, complete=complete):
                    self.assertEqual(live_verify.completeness(spec, report)["complete"],
                                     complete)
                    self.assertEqual(report["units_unserved"], 4 if complete else 0)
                    recorded = live_verify._recorded(spec, report)
                    self.assertEqual(recorded.get("units_unserved"), 4 if complete else 0)

    def test_a_failed_line_navigation_is_read_once_by_the_filtered_line_set(self):
        # Live bc.capture_full: the nested navigation fails for a few purchase orders on
        # every run; the same lines are read by purchaseOrderLines?$filter=documentId eq.
        fake = FakeBc({"purchaseOrders": 3})
        bad = gid(0, 0, 1)
        fake.fail(rf"purchaseOrders\({bad}\)/purchaseOrderLines",
                  (500, {}, b'{"error":{"code":"Internal_ServerError"}}'))
        h = Harness(self, fake)
        lines = h.unit(h.run(entity_sets=("purchaseOrders",)), CO[0], "purchaseOrderLines")
        self.assertEqual((lines["state"], lines["records_current"], lines["dropped"],
                          lines["lines_via_filter"]), ("complete", 6, 0, 1))
        (flat,) = fake.bc_gets(rf"/purchaseOrderLines\?")
        check_request("GET", flat, environ=ENV)
        self.assertIn(f"documentId eq {bad}", unquote(flat).replace("+", " "))

    def test_a_refused_line_navigation_is_never_read_another_way(self):
        fake = FakeBc({"purchaseOrders": 3})
        fake.fail(rf"purchaseOrders\({gid(0, 0, 1)}\)/purchaseOrderLines",
                  (403, {}, b'{"error":{"code":"Authorization_RequestDenied"}}'))
        h = Harness(self, fake)
        lines = h.unit(h.run(entity_sets=("purchaseOrders",)), CO[0], "purchaseOrderLines")
        self.assertEqual((lines["state"], lines["reason"], lines["lines_via_filter"]),
                         ("in_progress", "line_reads_failed", 0))
        self.assertFalse(fake.bc_gets(r"/purchaseOrderLines\?"))

    def test_a_throttled_filtered_line_read_stops_the_call(self):
        fake = FakeBc({"purchaseOrders": 3})
        bad = gid(0, 0, 1)
        fake.fail(rf"purchaseOrders\({bad}\)/purchaseOrderLines",
                  (500, {}, b'{"error":{"code":"Internal_ServerError"}}'))
        fake.fail(rf"purchaseOrderLines\?.*documentId[+ ]eq[+ ]{bad}",
                  *[(429, {"Retry-After": "120"}, b'{"error":{"code":"TooManyRequests"}}')] * 3)
        h = Harness(self, fake)
        report = h.run(entity_sets=("purchaseOrders",))
        lines = h.unit(report, CO[0], "purchaseOrderLines")
        self.assertEqual((report["stopped_reason"], lines["state"], lines["reason"],
                          lines["dropped"]), ("throttled", "in_progress", "throttled", 0))
        self.assertFalse(fake.bc_gets(rf"purchaseOrders\({gid(0, 0, 2)}\)/purchaseOrderLines"))

    def test_every_request_is_a_get_through_the_guard_except_the_token_post(self):
        h = Harness(self)
        h.run()
        methods = {(m, urlsplit(u).hostname) for m, u, _ in h.fake.calls}
        self.assertEqual(methods, {("POST", "login.microsoftonline.com"),
                                   ("GET", "api.businesscentral.dynamics.com")})
        for method, url, headers in h.fake.calls:
            if method == "GET":
                check_request("GET", url, environ=ENV)  # each one passes the read-only guard
                self.assertNotIn(CLIENT_SECRET, url)
        self.assertEqual(h.transport.requests_sent, len(h.fake.calls))

    def test_a_second_capture_of_unchanged_records_adds_no_version(self):
        h = Harness(self)
        first = h.run()
        second = h.run()
        versions = sum(u["versions_total"] for c in second["companies"] for u in c["units"])
        current = sum(u["records_current"] for c in first["companies"] for u in c["units"])
        self.assertEqual(versions, current)
        self.assertEqual(sum(u["new_versions"] for c in second["companies"]
                             for u in c["units"]), 0)


class HistoryTests(unittest.TestCase):
    def test_a_changed_etag_adds_a_version_and_the_old_handle_still_resolves(self):
        h = Harness(self)
        h.run()
        [old] = [x for x in h.handles(CO[0], "purchaseOrders") if gid(0, 0, 1) in x]
        h.clock.now = T0 + timedelta(hours=1)
        row = h.fake.data[(CO[0], "purchaseOrders")][1]
        row.update({"@odata.etag": 'W/"JzChanged;"', "amount": "#99.75"})
        report = h.run()
        po = h.unit(report, CO[0], "purchaseOrders")
        self.assertEqual((po["records_current"], po["versions_total"], po["new_versions"]),
                         (3, 4, 1))
        then = h.store.resolve(old, SERVICE)
        self.assertTrue(then["ok"])
        self.assertEqual(then["status"], "superseded")
        self.assertEqual(then["row"]["amount"], "1.50")
        self.assertEqual(then["source_version"], 'W/"JzE001;"')
        self.assertEqual(then["capture"]["superseded_at"], "2026-10-01T13:00:00.000000Z")
        now = h.store.resolve(then["current_citation"], SERVICE)
        self.assertEqual((now["status"], now["row"]["amount"], now["source_version"]),
                         ("current", "99.75", 'W/"JzChanged;"'))
        # As known at the first capture, the old version is the one in force.
        known = h.store.records(SERVICE, "bc", bc_unit(CO[0], "purchaseOrders"),
                                as_known_at="2026-10-01T12:30:00Z")["items"]
        self.assertIn(old, [i["citation"] for i in known])

    def test_a_record_gone_from_a_complete_traversal_is_missing_not_deleted(self):
        h = Harness(self)
        h.run()
        si = CAPTURE_ENTITY_SETS.index("vendors")
        [gone] = [x for x in h.handles(CO[1], "vendors") if gid(1, si, 2) in x]
        del h.fake.data[(CO[1], "vendors")][2]
        report = h.run()
        vendors = h.unit(report, CO[1], "vendors")
        self.assertEqual((vendors["records_current"], vendors["records_missing"]), (3, 1))
        self.assertNotIn(gone, h.handles(CO[1], "vendors"))
        resolved = h.store.resolve(gone, SERVICE)
        self.assertTrue(resolved["ok"])
        self.assertEqual(resolved["missing_from_source_since"], "2026-10-01T12:00:00.000000Z")
        self.assertFalse(resolved["deletions_proven"])


class ResumeAndThrottleTests(unittest.TestCase):
    def test_a_budget_stop_resumes_after_the_last_committed_page(self):
        fake = FakeBc({"purchaseOrders": 9})
        h = Harness(self, fake)
        first = h.run(entity_sets=("purchaseOrders",), budget_seconds=3)
        self.assertEqual(first["stopped_reason"], "budget")
        po = h.unit(first, CO[0], "purchaseOrders")
        self.assertEqual((po["state"], po["reason"], po["resumable"]),
                         ("in_progress", "budget", True))
        self.assertLess(po["records_current"], 9)
        second = h.run(entity_sets=("purchaseOrders",))
        po = h.unit(second, CO[0], "purchaseOrders")
        self.assertEqual((po["state"], po["records_current"], po["versions_total"]),
                         ("complete", 9, 9))
        # The first page of company 0 was read once: the resume used the stored cursor.
        first_page = [u for u in fake.bc_gets(
            rf"{CO[0]}\)/purchaseOrders\?\$expand=purchaseOrderLines$")]
        self.assertEqual(len(first_page), 1)

    def test_a_budget_cut_keeps_the_root_invoice_sets_of_every_company(self):
        """Live 2026-10-07: company-major order spent the budget on the first company's
        lines and held no root of the others. A budget of one second per request that
        covers only the root listings holds every company's roots (N = 1, 10, 60)."""
        sets = ("salesInvoices", "purchaseInvoices", "purchaseOrders", "vendors")
        for n in (1, 10, 60):
            with self.subTest(invoices=n):
                fake = FakeBc({"salesInvoices": n, "purchaseInvoices": n})
                h = Harness(self, fake)
                capture = BcCapture(h.reader, h.store, books=BOOKS,
                                    monotonic=lambda: float(len(fake.calls)))
                pages = len(CO) * 2 * -(-n // 2)   # page size 2
                # + the token and the company listing
                report = capture.capture(SERVICE, entity_sets=sets, page_size=2,
                                         budget_seconds=2 + pages)
                self.assertEqual(report["stopped_reason"], "budget")
                for company in CO:
                    for root in bc_read.CAPTURE_ROOT_SETS:
                        unit = h.unit(report, company, root)
                        self.assertEqual((unit["state"], unit["records_current"]),
                                         ("complete", n), (company, root))
                        self.assertEqual(len(h.handles(company, root)), n)
                self.assertFalse(fake.bc_gets(r"\)/\w+Lines|purchaseOrders|vendors"))
                resumed = h.run(entity_sets=sets)
                self.assertEqual(resumed["units_complete"], resumed["units_total"])
                # per company: 4 sets, 3 line sets and the company record
                self.assertEqual(resumed["units_total"], len(CO) * 8)

    def test_one_large_root_set_never_starves_the_later_companies_roots(self):
        """Each root unit reads within an even share of the budget left: the first
        company's huge invoice set stops at its share end, every other company's roots are
        read whole, and the large set resumes on what they left (still before any line or
        other set)."""
        fake = FakeBc({"salesInvoices": 3, "purchaseInvoices": 3})
        rows = fake.data[(CO[0], "salesInvoices")]
        rows += [dict(rows[0], **{"@odata.etag": f'W/"big{n}"', "id": gid(0, 4, 100 + n)})
                 for n in range(200)]
        h = Harness(self, fake)
        capture = BcCapture(h.reader, h.store, books=BOOKS,
                            monotonic=lambda: float(len(fake.calls)))
        # one second per request; + the token and the company listing
        report = capture.capture(SERVICE, entity_sets=("salesInvoices", "purchaseInvoices",
                                                       "vendors"),
                                 page_size=2, budget_seconds=2 + 30)
        self.assertEqual(report["stopped_reason"], "budget")
        for company in CO:
            for root in ("salesInvoices", "purchaseInvoices"):
                unit = h.unit(report, company, root)
                if (company, root) == (CO[0], "salesInvoices"):
                    self.assertEqual((unit["state"], unit["reason"]), ("in_progress", "budget"))
                    # its share (5 pages) and then what the others left (15 pages)
                    self.assertEqual(unit["records_current"], 40)
                else:
                    self.assertEqual((unit["state"], unit["records_current"]), ("complete", 3),
                                     (company, root))
        self.assertFalse(fake.bc_gets(r"\)/\w+Lines|vendors"))

    def test_a_throttle_mid_pagination_names_the_paused_set_in_the_throttle_block(self):
        """The last company's vendors are throttled on their second page: the unit is
        paused (its first page kept) and the throttle block names its set, though every
        other company read that set whole."""
        fake = FakeBc({"vendors": 4})
        long_wait = (429, {"Retry-After": "120"}, b'{"error":{"code":"TooManyRequests"}}')
        fake.fail(rf"{CO[2]}\)/vendors\?aid=FINANCE.*skiptoken=2", long_wait, long_wait,
                  long_wait)
        h = Harness(self, fake)
        report = h.run(entity_sets=("vendors",))
        self.assertEqual(report["stopped_reason"], "throttled")
        unit = h.unit(report, CO[2], "vendors")
        self.assertEqual((unit["state"], unit["reason"], unit["records_current"]),
                         ("in_progress", "throttled", 2))
        for company in CO[:2]:
            self.assertEqual(h.unit(report, company, "vendors")["state"], "complete")
        self.assertEqual(report["throttle"], {"http_status": 429, "entity_sets": ["vendors"],
                                              "units": 1})

    def test_retry_after_is_honoured_and_a_persistent_throttle_defers_the_unit(self):
        fake = FakeBc({"vendors": 4})
        too_many = (429, {"Retry-After": "2"}, b'{"error":{"code":"TooManyRequests"}}')
        fake.fail(rf"{CO[0]}\)/vendors\?aid", too_many)  # once: retried after 2 s
        h = Harness(self, fake)
        report = h.run(entity_sets=("vendors",))
        self.assertEqual(h.unit(report, CO[0], "vendors")["state"], "complete")
        self.assertIn(2.0, h.sleeps)
        # Now BC keeps throttling with a long Retry-After: the bounded retry gives up,
        # the unit keeps its cursor and is deferred; the call stops.
        long_wait = (429, {"Retry-After": "120"}, b'{"error":{"code":"TooManyRequests"}}')
        fake.data[(CO[1], "vendors")][0]["@odata.etag"] = 'W/"new"'
        fake.fail(rf"{CO[1]}\)/vendors\?aid", long_wait, long_wait, long_wait)
        h.store.close()
        h.store = CaptureStore(h.db, h.audience, clock=h.clock)
        h.capture = BcCapture(h.reader, h.store, books=BOOKS, monotonic=h.clock.monotonic)
        stopped = h.run(entity_sets=("vendors",))
        self.assertEqual(stopped["stopped_reason"], "throttled")
        unit = h.unit(stopped, CO[1], "vendors")
        self.assertEqual((unit["state"], unit["reason"], unit["retry_not_before"]),
                         ("in_progress", "throttled", "2026-10-01T12:02:00.000000Z"))
        self.assertEqual(len(stopped["companies"]), 3)
        self.assertEqual(stopped["companies"][2]["units"], [])  # not reached
        self.assertFalse(stopped["all_complete"])
        before = len(fake.bc_gets(rf"{CO[1]}\)/vendors"))
        deferred = h.run(entity_sets=("vendors",))
        self.assertTrue(h.unit(deferred, CO[1], "vendors")["deferred"])
        self.assertEqual(len(fake.bc_gets(rf"{CO[1]}\)/vendors")), before)  # no request
        h.clock.now = T0 + timedelta(minutes=3)
        done = h.run(entity_sets=("vendors",))
        unit = h.unit(done, CO[1], "vendors")
        self.assertEqual((unit["state"], unit["records_current"]), ("complete", 4))
        self.assertTrue(done["companies"][2]["units"][0]["complete"])

    def test_the_throttle_report_follows_the_roots_first_unit_order(self):
        """Every company's roots are read before any line or other set, so a throttle on
        the first company's vendors leaves no root unread: the report names that company's
        paused vendors and every other company's lines and vendors, never the roots."""
        fake = FakeBc({"purchaseInvoices": 3, "vendors": 4})
        long_wait = (429, {"Retry-After": "120"}, b'{"error":{"code":"TooManyRequests"}}')
        fake.fail(rf"{CO[0]}\)/vendors\?aid", long_wait, long_wait, long_wait)
        h = Harness(self, fake)
        report = h.run(entity_sets=("purchaseInvoices", "vendors"))
        self.assertEqual(report["stopped_reason"], "throttled")
        lines = BC_DOCUMENT_LINES["purchaseInvoices"]
        self.assertEqual(report["throttle"], {"http_status": 429,
                                              "entity_sets": sorted([lines, "vendors"]),
                                              "units": 1})
        for company in CO:
            self.assertEqual(h.unit(report, company, "purchaseInvoices")["state"], "complete")
        self.assertEqual(h.unit(report, CO[0], lines)["state"], "complete")
        self.assertEqual([u["entity_set"] for u in report["companies"][1]["units"]],
                         ["purchaseInvoices"])

    def test_a_throttled_company_listing_is_a_throttle_stop_with_its_status(self):
        """Review 4eb0f8535a28 (b): a 429 on the companies listing raised SourceUnavailable
        (failed, upstream_outage) with no throttle block. It stops the call before any
        company with the throttle block every throttle gives, its HTTP status and every
        set left unread; nothing else is requested, and the next call reads it all."""
        fake = FakeBc({"purchaseInvoices": 3}, companies=list(CO))
        long_wait = (429, {"Retry-After": "120"}, b'{"error":{"code":"TooManyRequests"}}')
        fake.fail(r"/companies(\?|$)", long_wait, long_wait, long_wait)
        h = Harness(self, fake)
        report = h.run(entity_sets=("purchaseInvoices", "vendors"))
        self.assertEqual((report["stopped_reason"], report["companies"],
                          report["companies_listing_complete"], report["all_complete"]),
                         ("throttled", [], False, False))
        self.assertEqual(report["throttle"], {
            "http_status": 429, "units": 0, "companies_listing": True,
            "entity_sets": sorted(["purchaseInvoices", BC_DOCUMENT_LINES["purchaseInvoices"],
                                   "vendors"])})
        self.assertFalse(fake.bc_gets(r"companies\("))
        self.assertTrue(h.run(entity_sets=("purchaseInvoices", "vendors"))["all_complete"])

    def test_an_intermittent_failure_is_paused_and_reported_partial_then_resumes(self):
        fake = FakeBc({"customers": 6})
        h = Harness(self, fake)
        server_error = (500, {}, b'{"error":{"code":"Internal"}}')
        fake.fail(rf"{CO[0]}\)/customers\?aid=FINANCE&\$skiptoken=4", server_error)
        report = h.run(entity_sets=("customers",))
        unit = h.unit(report, CO[0], "customers")
        self.assertEqual((unit["state"], unit["reason"], unit["records_current"]),
                         ("in_progress", "source_unavailable", 4))
        self.assertFalse(unit["complete"])
        self.assertIn("HTTP 500", unit["error"])
        self.assertTrue(h.unit(report, CO[1], "customers")["complete"])  # others go on
        again = h.run(entity_sets=("customers",))
        self.assertEqual(h.unit(again, CO[0], "customers")["records_current"], 6)
        self.assertEqual(h.unit(again, CO[0], "customers")["state"], "complete")

    def test_an_expired_continuation_restarts_the_unit_once(self):
        fake = FakeBc({"items": 6})
        h = Harness(self, fake)
        h.run(entity_sets=("items",), budget_seconds=3)  # stops after a page or two
        fake.fail(r"/items\?aid", (400, {}, b'{"error":{"code":"BadRequest"}}'))
        report = h.run(entity_sets=("items",))
        unit = h.unit(report, CO[0], "items")
        self.assertEqual((unit["state"], unit["restarts"], unit["records_current"]),
                         ("complete", 1, 6))

    def test_a_refused_entity_set_fails_alone(self):
        fake = FakeBc()
        fake.fail(rf"{CO[2]}\)/salesShipments$", (403, {}, b'{"error":{"code":"Forbidden"}}'))
        h = Harness(self, fake)
        report = h.run()
        unit = h.unit(report, CO[2], "salesShipments")
        self.assertEqual((unit["state"], unit["reason"]), ("failed", "source_refused"))
        self.assertEqual(report["units_complete"], 3 * ALL_UNITS - 1)

    def test_item_ledger_is_bounded_per_call_and_completes_across_calls(self):
        fake = FakeBc({"itemLedgerEntries": 5})
        h = Harness(self, fake)
        first = h.run(entity_sets=("itemLedgerEntries",),
                      record_caps={"itemLedgerEntries": 3})
        unit = h.unit(first, CO[0], "itemLedgerEntries")
        self.assertEqual((unit["state"], unit["reason"], unit["records_current"]),
                         ("in_progress", "record_cap", 4))
        second = h.run(entity_sets=("itemLedgerEntries",),
                       record_caps={"itemLedgerEntries": 3})
        unit = h.unit(second, CO[0], "itemLedgerEntries")
        self.assertEqual((unit["state"], unit["records_current"]), ("complete", 5))

    def test_a_header_added_below_a_resumed_lines_run_is_read_before_complete(self):
        # Review 948726f (material): a resumed lines run skipped headers whose id sorted
        # before its cursor and still finished complete.
        fake = FakeBc({"purchaseOrders": 5})
        h = Harness(self, fake)
        first = h.run(entity_sets=("purchaseOrders",), budget_seconds=6)
        lines = h.unit(first, CO[0], "purchaseOrderLines")
        self.assertEqual((lines["state"], lines["records_current"]), ("in_progress", 4))
        low = "00000000-0000-4000-8000-000000000001"  # sorts before every read header
        fake.data[(CO[0], "purchaseOrders")].insert(0, {
            "@odata.etag": 'W/"new"', "id": low, "number": "N-new",
            "lastModifiedDateTime": "2026-10-01T11:00:00Z"})
        fake.lines[(CO[0], "purchaseOrders", low)] = [
            {"@odata.etag": 'W/"Lnew"', "id": "00000000-0000-4000-8000-0000000000aa",
             "documentId": low, "sequence": 10000, "quantity": "#7"}]
        # A call that re-captures the headers but runs out before the lines of the new
        # header: still not complete.
        short = h.run(entity_sets=("purchaseOrders",), budget_seconds=6)
        self.assertEqual(h.unit(short, CO[0], "purchaseOrders")["records_current"], 6)
        self.assertFalse(h.unit(short, CO[0], "purchaseOrderLines")["complete"])
        done = h.run(entity_sets=("purchaseOrders",))
        lines = h.unit(done, CO[0], "purchaseOrderLines")
        self.assertEqual((lines["state"], lines["records_current"]), ("complete", 11))
        self.assertEqual(len(fake.bc_gets(rf"purchaseOrders\({low}\)/purchaseOrderLines")), 1)
        new_line = [x for x in h.handles(CO[0], "purchaseOrderLines") if "0000000000aa" in x]
        self.assertEqual(h.store.resolve(new_line[0], SERVICE)["row"]["quantity"], "7")
        # Each old header's lines were read once across the three calls.
        self.assertEqual(len(fake.bc_gets(rf"purchaseOrders\({gid(0, 0, 0)}\)/")), 1)

    def test_failed_line_reads_stay_pending_and_the_next_pass_retries_exactly_those(self):
        # Live bc.capture_full (main 95802967): purchaseOrderLines ended partial /
        # line_reads_failed with 4 dropped on every run; a failed read was counted
        # dropped, so the run could never settle and each new run re-read every header.
        fake = FakeBc({"purchaseOrders": 5})
        bad = [gid(0, 0, 1), gid(0, 0, 3)]
        for doc in bad:
            fake.fail_lines(doc, *[(500, {}, b'{"error":{"code":"Internal_ServerError",'
                                              b'"message":"x"}}')] * 2)
        h = Harness(self, fake)
        report = h.run(entity_sets=("purchaseOrders",))
        lines = h.unit(report, CO[0], "purchaseOrderLines")
        self.assertEqual((lines["state"], lines["reason"], lines["records_current"],
                          lines["dropped"], lines["resumable"]),
                         ("in_progress", "line_reads_failed", 6, 0, True))
        self.assertFalse(lines["complete"])
        self.assertFalse(report["all_complete"])
        # Classified from fixed vocabularies: status and error code, never message text.
        self.assertEqual(lines["error"], "http_500:internal_server_error")
        self.assertTrue(h.unit(report, CO[1], "purchaseOrderLines")["complete"])
        before = len(fake.bc_gets(rf"{CO[0]}\)/purchaseOrders\(.*\)/purchaseOrderLines"))
        again = h.run(entity_sets=("purchaseOrders",))
        retried = fake.bc_gets(rf"{CO[0]}\)/purchaseOrders\(.*\)/purchaseOrderLines")[before:]
        self.assertEqual(sorted(re.search(r"purchaseOrders\(([^)]+)\)", unquote(u))[1]
                                for u in retried), sorted(bad))
        lines = h.unit(again, CO[0], "purchaseOrderLines")
        self.assertEqual((lines["state"], lines["records_current"], lines["dropped"]),
                         ("complete", 10, 0))

    def test_a_document_that_always_fails_does_not_freeze_the_lines_unit(self):
        # An unbounded retry kept the run in_progress forever, so a
        # line changed in BC under another, unchanged header was never captured again.
        fake = FakeBc({"purchaseOrders": 5})
        bad, other = gid(0, 0, 1), gid(0, 0, 3)
        fake.fail_lines(bad, *[(500, {}, b'{"error":{"code":"Internal_ServerError"}}')] * 10)
        h = Harness(self, fake)
        h.run(entity_sets=("purchaseOrders",))
        second = h.unit(h.run(entity_sets=("purchaseOrders",)), CO[0], "purchaseOrderLines")
        self.assertEqual((second["state"], second["reason"], second["dropped"],
                          second["error"]),
                         ("partial", "line_reads_failed", 1, "http_500:internal_server_error"))
        changed = fake.lines[(CO[0], "purchaseOrders", other)][0]
        changed.update({"@odata.etag": 'W/"Lchanged"', "quantity": "#999"})
        h.run(entity_sets=("purchaseOrders",))
        line = [x for x in h.handles(CO[0], "purchaseOrderLines") if changed["id"][-6:] in x]
        self.assertEqual(h.store.resolve(line[0], SERVICE)["row"]["quantity"], "999")

    def test_a_retry_stopped_by_the_request_budget_is_not_a_document_failure(self):
        fake = FakeBc({"purchaseOrders": 5})
        bad = gid(0, 0, 1)
        fake.fail(rf"purchaseOrders\({bad}\)/purchaseOrderLines",
                  (500, {}, b'{"error":{"code":"Internal_ServerError"}}'))
        h = Harness(self, fake)
        h.run(entity_sets=("purchaseOrders",))
        read = h.reader.list_lines

        def spent(actor, company, header_set, document, **kw):
            if document == bad:
                raise RequestBudgetExhausted()
            return read(actor, company, header_set, document, **kw)

        with mock.patch.object(h.reader, "list_lines", side_effect=spent):
            stopped = h.run(entity_sets=("purchaseOrders",))
        lines = h.unit(stopped, CO[0], "purchaseOrderLines")
        self.assertEqual((stopped["stopped_reason"], lines["state"], lines["reason"],
                          lines["dropped"]), ("budget", "in_progress", "budget", 0))
        lines = h.unit(h.run(entity_sets=("purchaseOrders",)), CO[0], "purchaseOrderLines")
        self.assertEqual((lines["state"], lines["records_current"], lines["dropped"]),
                         ("complete", 10, 0))

    def test_a_document_gone_from_bc_is_recorded_gone_not_a_perpetual_failure(self):
        fake = FakeBc({"purchaseOrders": 5})
        gone, kept = gid(0, 0, 2), gid(0, 0, 4)
        not_found = (404, {}, b'{"error":{"code":"BadRequest_NotFound"}}')
        fake.fail(rf"purchaseOrders\({gone}\)/purchaseOrderLines", not_found)
        fake.fail(rf"purchaseOrders\({gone}\)$", not_found)  # the header is gone too
        fake.fail_lines(kept, not_found, not_found)  # it is not, and its lines fail both ways
        h = Harness(self, fake)
        lines = h.unit(h.run(entity_sets=("purchaseOrders",)), CO[0], "purchaseOrderLines")
        self.assertEqual((lines["state"], lines["reason"], lines["lines_gone"],
                          lines["dropped"], lines["error"]),
                         ("in_progress", "line_reads_failed", 1, 0,
                          "http_404:bad_request_not_found"))
        fake.data[(CO[0], "purchaseOrders")] = [
            r for r in fake.data[(CO[0], "purchaseOrders")] if r["id"] != gone]
        done = h.run(entity_sets=("purchaseOrders",))
        lines = h.unit(done, CO[0], "purchaseOrderLines")
        self.assertEqual((lines["state"], lines["records_current"]), ("complete", 8))
        self.assertEqual(len(fake.bc_gets(rf"purchaseOrders\({gone}\)")), 2)  # never again
        self.assertEqual(len(fake.bc_gets(rf"purchaseOrders\({kept}\)/")), 2)

    def test_line_reads_failing_in_a_row_pause_the_unit(self):
        fake = FakeBc({"purchaseOrders": 5})
        for n in range(bc_read.LINE_FAILURE_STREAK):
            fake.fail_lines(gid(0, 0, n), *[(500, {}, b'{"error":{"code":"Internal"}}')] * 2)
        h = Harness(self, fake)
        report = h.run(entity_sets=("purchaseOrders",))
        lines = h.unit(report, CO[0], "purchaseOrderLines")
        self.assertEqual((lines["state"], lines["reason"], lines["records_current"],
                          lines["error"]), ("in_progress", "source_unavailable", 0, "http_500:other"))
        self.assertEqual(len(fake.bc_gets(rf"{CO[0]}\)/purchaseOrders\(")),
                         bc_read.LINE_FAILURE_STREAK)
        before = len(fake.bc_gets(rf"{CO[0]}\)/purchaseOrders\("))
        done = h.run(entity_sets=("purchaseOrders",))
        # The untried headers first, then the failed ones again.
        order = [re.search(r"purchaseOrders\(([^)]+)\)", unquote(u))[1]
                 for u in fake.bc_gets(rf"{CO[0]}\)/purchaseOrders\(")[before:]]
        self.assertEqual(order, [gid(0, 0, n) for n in (3, 4, 0, 1, 2)])
        lines = h.unit(done, CO[0], "purchaseOrderLines")
        self.assertEqual((lines["state"], lines["records_current"]), ("complete", 10))

    def test_lines_of_a_header_set_the_company_refuses_or_hides_fail_with_its_reason(self):
        # Live bc.capture_full: BC answers one company's payment journals 400 (journal
        # template missing); their lines unit read "headers_incomplete", a reason that
        # hid the refusal. It fails with the header's reason; other companies complete.
        refused = (400, {}, b'{"error":{"code":"internal_data_not_found_filter"}}')
        hidden = (404, {}, b'{"error":{"code":"BadRequest_NotFound"}}')
        for response, reason in ((refused, "source_refused"), (hidden, "not_exposed")):
            with self.subTest(reason=reason):
                fake = FakeBc({"customerPaymentJournals": 3})
                fake.fail(rf"{CO[1]}\)/customerPaymentJournals", response, response)  # the expand read, then the plain one
                h = Harness(self, fake)
                report = h.run(entity_sets=("customerPaymentJournals",))
                for name in ("customerPaymentJournals", "customerPayments"):
                    unit = h.unit(report, CO[1], name)
                    self.assertEqual((unit["state"], unit["reason"], unit["complete"]),
                                     ("failed", reason, False))
                self.assertTrue(h.unit(report, CO[0], "customerPayments")["complete"])
                self.assertFalse(report["all_complete"])

    def test_an_unserved_set_is_listed_with_its_later_ask_and_never_counted_complete(self):
        # Live bc.capture_full/incremental: BC answers two companies' payment journals 400
        # internal_data_not_found_filter (later ask bc-unserved-endpoints) while the third
        # serves them. Those units stay failed, are listed, and no longer hold all_complete.
        unserved = (400, {}, b'{"error":{"code":"Internal_DataNotFoundFilter"}}')
        sets = ("customerPaymentJournals",)
        fake = FakeBc({"customerPaymentJournals": 3}, companies=CO)
        for company in CO[1:]:
            fake.fail(rf"{company}\)/customerPaymentJournals", unserved, unserved)
        h = Harness(self, fake)
        report = h.run(entity_sets=sets)
        for company in CO[1:]:
            for name in ("customerPaymentJournals", "customerPayments"):
                unit = h.unit(report, company, name)
                self.assertEqual((unit["state"], unit["reason"], unit["complete"],
                                  unit["later_ask"], unit["accounted"]),
                                 ("failed", "source_refused", False,
                                  "bc-unserved-endpoints", True))
        self.assertEqual(report["units_complete"], 3 * 3 - 4)
        self.assertEqual(report["units_unserved"], 4)
        self.assertEqual(sorted({u["entity_set"] for u in report["unserved_units"]}),
                         ["customerPaymentJournals", "customerPayments"])
        self.assertTrue(report["all_complete"])
        # Not unserved: every company refused the set (no proof the request is valid), a
        # 400 with another code, and a 403 with the unserved code.
        other_400 = (400, {}, b'{"error":{"code":"BadRequest"}}')
        forbidden = (403, {}, b'{"error":{"code":"Internal_DataNotFoundFilter"}}')
        for label, response, refusing in (("all", unserved, CO), ("code", other_400, CO[1:2]),
                                          ("403", forbidden, CO[1:2])):
            with self.subTest(label):
                fake = FakeBc({"customerPaymentJournals": 3}, companies=CO)
                for company in refusing:
                    fake.fail(rf"{company}\)/customerPaymentJournals", response, response)
                other = Harness(self, fake).run(entity_sets=sets)
                self.assertEqual(other["units_unserved"], 0)
                self.assertFalse(other["all_complete"])
                self.assertFalse(any(u.get("accounted") for c in other["companies"]
                                     for u in c["units"] if not u["complete"]))

    def test_lines_of_a_header_unit_a_server_error_paused_name_its_error_and_complete_on_resume(self):
        # Live: lines units read headers_incomplete with no hint why. The reason stays typed
        # and carries the header unit's error; the next call resumes both and settles them.
        fake = FakeBc({"customerPaymentJournals": 3}, companies=CO)
        fake.fail(rf"{CO[1]}\)/customerPaymentJournals", (500, {}, b'{"error":{"code":"Internal"}}'))
        h = Harness(self, fake)
        first = h.run(entity_sets=("customerPaymentJournals",))
        header = h.unit(first, CO[1], "customerPaymentJournals")
        lines = h.unit(first, CO[1], "customerPayments")
        self.assertEqual((header["state"], header["reason"], header["accounted"]),
                         ("in_progress", "source_unavailable", False))
        self.assertEqual((lines["state"], lines["reason"], lines["error"], lines["accounted"]),
                         ("partial", "headers_incomplete", header["error"], False))
        self.assertFalse(first["all_complete"])
        again = h.run(entity_sets=("customerPaymentJournals",))
        for name in ("customerPaymentJournals", "customerPayments"):
            self.assertTrue(h.unit(again, CO[1], name)["accounted"])
        self.assertTrue(again["all_complete"])

    def test_a_company_listed_without_an_etag_is_versioned_by_its_content(self):
        row = {"id": CO[0], "name": "Plain", "displayName": "Plain"}
        for rename in (False, True):
            fake = FakeBc({})
            fake.fail(r"/api/v2\.0/companies(\?|$)",
                      (200, {}, fake.body({"value": [row, *[
                          {"id": c, "name": "X", "@odata.etag": 'W/"c"'} for c in CO[1:]]]})))
            h = Harness(self, fake)
            first = h.unit(h.run(entity_sets=("vendors",)), CO[0], "companies")
            self.assertEqual((first["state"], first["records_current"]), ("complete", 1))
            record = h.store.records(SERVICE, "bc", bc_unit(CO[0], "companies"))["items"][0]
            self.assertTrue(h.store.resolve(record["citation"], SERVICE)["ok"])
            if rename:  # a changed name is a new version, the old one kept
                fake.fail(r"/api/v2\.0/companies(\?|$)",
                          (200, {}, fake.body({"value": [{**row, "name": "Renamed"}, *[
                              {"id": c, "name": "X", "@odata.etag": 'W/"c"'}
                              for c in CO[1:]]]})))
                again = h.unit(h.run(entity_sets=("vendors",)), CO[0], "companies")
                self.assertEqual(again["versions_total"], 2)

    def test_lines_of_an_incomplete_header_unit_are_partial(self):
        fake = FakeBc({"salesInvoices": 5})
        fake.fail(rf"{CO[0]}\)/salesInvoices\?aid=FINANCE&\$expand=salesInvoiceLines"
                  r"&\$filter=[^&]*&\$skiptoken=2",
                  (500, {}, b'{"error":{"code":"Internal"}}'))
        h = Harness(self, fake)
        report = h.run(entity_sets=("salesInvoices",))
        self.assertEqual(h.unit(report, CO[0], "salesInvoices")["state"], "in_progress")
        lines = h.unit(report, CO[0], "salesInvoiceLines")
        self.assertEqual((lines["state"], lines["reason"]), ("partial", "headers_incomplete"))

    def test_a_line_naming_another_document_is_dropped_and_the_unit_is_partial(self):
        fake = FakeBc()
        doc = gid(0, 3, 0)  # salesOrders of company 0
        fake.lines[(CO[0], "salesOrders", doc)][1]["documentId"] = gid(0, 3, 2)
        h = Harness(self, fake)
        report = h.run(entity_sets=("salesOrders",))
        unit = h.unit(report, CO[0], "salesOrderLines")
        self.assertEqual((unit["state"], unit["dropped"], unit["records_current"]),
                         ("partial", 1, 5))
        self.assertTrue(h.unit(report, CO[1], "salesOrderLines")["complete"])


class OpaqueSkiptokenBc(FakeBc):
    """``FakeBc`` with BC's documented continuation shape (``live_source_grammar.
    _BC_CONTINUATION``, UNVERIFIED live): the nextLink names the request's own entity
    set, repeats its query and adds ``aid`` and an opaque server-minted ``$skiptoken``
    (a quoted key, percent-encoded) that only the server maps back to a position; a
    token it did not mint for that collection reads as BC's 400. ``unexpected[(company,
    entity_set, offset)] = n``: the next ``n`` continuations to ``offset`` of that unit
    also carry an option the request never sent; ``raw_spaces`` units echo their
    ``$filter`` in the nextLink with raw (unencoded) spaces."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.minted: dict[str, tuple[str, int]] = {}
        self.unexpected: dict[tuple[str, str, int], int] = {}
        self.raw_spaces: set[tuple[str, str]] = set()

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        status, extra, raw = super().__call__(method, url, headers, body, timeout, max_bytes)
        found = re.search(r"/companies\(([^)]+)\)/(\w+)$", unquote(urlsplit(url).path))
        if found and found.groups() in self.raw_spaces:
            raw = raw.replace(b"%20", b" ")  # only the echoed $filter carries %20
        return status, extra, raw

    def offset(self, path: str, token: str | None) -> int | None:
        if token is None:
            return 0
        minted = self.minted.get(token)
        return minted[1] if minted is not None and minted[0] == path else None

    def token(self, path: str, offset: int) -> str:
        token = "'{" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"{path}#{offset}")).upper() + "}'"
        self.minted[token] = (path, offset)
        found = re.search(r"/companies\(([^)]+)\)/(\w+)$", unquote(path))
        key = (*found.groups(), offset) if found else None
        if self.unexpected.get(key):
            self.unexpected[key] -= 1
            return quote(token, safe="") + "&$expand=dimensionSetLines"
        return quote(token, safe="")


class CyclingBc(FakeBc):
    """``FakeBc`` whose ``cycle`` units page in a loop: the first page links to
    ``$skiptoken=2``, then 2 -> 4 -> 2 ... over empty pages (no record cap is ever
    reached, so only a refused repeat ends the unit)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.cycle: set[tuple[str, str]] = set()

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        parts = urlsplit(url)
        found = re.search(r"/companies\(([^)]+)\)/(\w+)$", unquote(parts.path))
        token = parse_qs(parts.query).get("$skiptoken", [None])[0]
        if found and found.groups() in self.cycle and token in ("2", "4"):
            self.calls.append((method, url, dict(headers)))
            return 200, {}, self.body({"value": [], "@odata.nextLink": (
                f"{HOST}{parts.path}?aid=FINANCE&$skiptoken={6 - int(token)}")})
        return super().__call__(method, url, headers, body, timeout, max_bytes)


class RefusedContinuationTests(unittest.TestCase):
    """p0-f-bc: one unexpected BC ``@odata.nextLink`` stalled BC capture for good. Only
    its path was checked, so it was committed as the unit's cursor; following it raised
    ``ReadOnlyGuardError`` (never caught by the capture), the unit stayed ``in_progress``
    on that cursor and every later run raised again before reaching later units. Now a
    nextLink is checked whole (path and every non-paging option) when it arrives, and a
    stored cursor the same way before any request: the unit fails ``nextlink_refused``,
    its cursor is dropped, the next run restarts it from its first page and the refused
    link is never sent."""

    def test_an_unexpected_nextlink_fails_the_unit_and_the_next_run_restarts_it(self):
        fake = OpaqueSkiptokenBc({"vendors": 5})
        fake.unexpected[(CO[0], "vendors", 4)] = 2  # its second continuation, two runs
        h = Harness(self, fake)
        seen = 0
        for attempt in range(3):
            report = h.run(entity_sets=("vendors", "customers"))
            unit = h.unit(report, CO[0], "vendors")
            gets = fake.bc_gets(rf"{CO[0]}\)/vendors")
            self.assertNotIn("$skiptoken", unquote(gets[seen]))  # from its first page
            seen = len(gets)
            others = [u for c in report["companies"] for u in c["units"] if u is not unit]
            self.assertTrue(others and all(u["complete"] for u in others), others)
            if attempt < 2:
                self.assertEqual((unit["state"], unit["reason"]),
                                 ("failed", "nextlink_refused"))
                self.assertEqual(unit["error"], "SourceUnavailable: business-central "
                                                "nextLink escaped the collection")
                self.assertIsNone(h.store.run("bc", bc_unit(CO[0], "vendors"))["cursor"])
        self.assertEqual((unit["state"], unit["records_current"]), ("complete", 5))
        self.assertFalse(fake.bc_gets(r"\$expand"))  # the refused link was never sent

    def test_a_nextlink_cycle_fails_the_unit_after_bounded_gets_and_is_not_resumed(self):
        # Only a nextLink equal to the current page was refused, so
        # a 2 -> 4 -> 2 cycle over empty pages was followed forever (>400 GETs, no budget).
        fake = CyclingBc({"vendors": 5})
        fake.cycle.add((CO[0], "vendors"))
        h = Harness(self, fake)
        unit_id = bc_unit(CO[0], "vendors")
        seen = 0
        for _ in range(2):  # the dropped cursor is not resumed: each run starts over
            report = h.run(entity_sets=("vendors", "customers"), budget_seconds=None)
            unit = h.unit(report, CO[0], "vendors")
            self.assertEqual((unit["state"], unit["reason"]), ("failed", "nextlink_refused"))
            self.assertIn("pagination loop", unit["error"])
            self.assertIsNone(h.store.run("bc", unit_id)["cursor"])
            gets = [unquote(u) for u in fake.bc_gets(rf"{CO[0]}\)/vendors")][seen:]
            self.assertEqual([parse_qs(urlsplit(u).query).get("$skiptoken") for u in gets],
                             [None, ["2"], ["4"]])  # first page, 2, 4; the repeat of 2 unsent
            seen += len(gets)
            others = [u for c in report["companies"] for u in c["units"] if u is not unit]
            self.assertTrue(others and all(u["complete"] for u in others), others)
        fake.cycle.clear()
        again = h.unit(h.run(entity_sets=("vendors",)), CO[0], "vendors")
        self.assertEqual((again["state"], again["records_current"]), ("complete", 5))

    def test_a_stored_cursor_the_reader_refuses_fails_the_unit_and_restarts_it(self):
        # The state a store was left in before the fix: an open run whose committed
        # cursor the guard (or the reader) refuses.
        token = quote("'{B1C2D3E4-0000-4000-8000-00000000002A}'", safe="")
        vendors = f"{HOST}{BASE}/companies({CO[0]})/vendors"
        widened = quote("lastModifiedDateTime ge 2020-01-01T00:00:00.000Z", safe="")
        for cursor in (f"{vendors}?aid=FIN&$expand=dimensionSetLines&$skiptoken={token}",
                       f"{vendors}?aid=FIN&$filter={widened}&$skiptoken={token}",
                       f"{HOST}{BASE}/companies({CO[0]})/customers?aid=FIN&$skiptoken={token}"):
            with self.subTest(cursor=cursor):
                fake = OpaqueSkiptokenBc({"vendors": 3})
                h = Harness(self, fake)
                unit_id = bc_unit(CO[0], "vendors")
                run = h.store.begin_run("bc", unit_id, [h.reader.resource(CO[0], "vendors")])
                h.store.save_page(run["run_id"], [], cursor)
                first = h.run(entity_sets=("vendors",))
                unit = h.unit(first, CO[0], "vendors")
                self.assertEqual((unit["state"], unit["reason"]), ("failed", "nextlink_refused"))
                self.assertIsNone(h.store.run("bc", unit_id)["cursor"])
                self.assertFalse(fake.bc_gets(rf"{CO[0]}\)/"))  # nothing sent for the unit
                self.assertTrue(h.unit(first, CO[1], "vendors")["complete"])
                again = h.unit(h.run(entity_sets=("vendors",)), CO[0], "vendors")
                self.assertEqual((again["state"], again["records_current"]), ("complete", 3))
                self.assertNotIn("$skiptoken", unquote(fake.bc_gets(rf"{CO[0]}\)/")[0]))
                self.assertFalse(fake.bc_gets("B1C2D3E4"))

    def test_a_raw_space_nextlink_fails_the_unit_typed_and_fails_the_live_check(self):
        # Repair of 61a29755: the delta read's nextLink repeated $filter with RAW spaces;
        # it matched the request's decoded value, was stored as the cursor and refused by
        # check_request when followed (ReadOnlyGuardError, uncaught: stalled for good).
        fake = OpaqueSkiptokenBc({"salesShipments": 5})
        fake.raw_spaces.add((CO[0], "salesShipments"))
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)

        def probe(passes: int) -> dict:
            return bc_read.capture_probe(
                ENV, sender=fake, db=Path(scratch.name) / "c.sqlite", page_size=2,
                resolve_sample=0, passes=passes, books=BOOKS, monotonic=Clock().monotonic,
                retry=BoundedBackoff(sleep=lambda s: None), entity_sets=("salesShipments",))

        spec = json.loads((Path(__file__).resolve().parents[2] / "devloop" / "live-checks"
                           / "bc.capture_incremental.json").read_text(encoding="utf-8"))
        failure = "nextlink_refused_per_pass[*] != expected"
        report = probe(2)  # pass 1: full read; pass 2: the delta read with the raw link
        unit = report["companies"][0]["units"][0]
        self.assertEqual((unit["mode"], unit["state"], unit["reason"]),
                         ("incremental", "failed", "nextlink_refused"))
        self.assertEqual(report["nextlink_refused_per_pass"], [0, 1])
        self.assertIn(failure, live_verify.check_expectations(spec, 0, report))
        self.assertFalse([url for url in fake.bc_gets() if " " in url])  # never sent
        fake.raw_spaces.clear()
        sent = len(fake.bc_gets(rf"{CO[0]}\)/salesShipments"))
        healthy = probe(1)  # restarted from its first page
        self.assertNotIn("$skiptoken",
                         unquote(fake.bc_gets(rf"{CO[0]}\)/salesShipments")[sent]))
        self.assertEqual(healthy["companies"][0]["units"][0]["state"], "complete")
        self.assertEqual(healthy["nextlink_refused_per_pass"], [0])
        self.assertNotIn(failure, live_verify.check_expectations(spec, 0, healthy))

    def test_a_cursor_the_guard_refuses_when_sent_fails_the_unit_typed(self):
        # Backstop: should the reader's own check ever pass a cursor the guard refuses
        # when sending it, the unit still fails typed and drops it; never an uncaught
        # ReadOnlyGuardError that leaves the unit in_progress on that cursor.
        fake = OpaqueSkiptokenBc({"vendors": 3})
        h = Harness(self, fake)
        unit_id = bc_unit(CO[0], "vendors")
        run = h.store.begin_run("bc", unit_id, [h.reader.resource(CO[0], "vendors")])
        h.store.save_page(run["run_id"], [], f"{HOST}{BASE}/companies({CO[0]})/vendors"
                                              "?aid=FIN&$skiptoken=a b")
        with mock.patch.object(h.reader._api, "continuation_url",
                               side_effect=lambda first, cursor: cursor):
            first = h.run(entity_sets=("vendors",))
        unit = h.unit(first, CO[0], "vendors")
        self.assertEqual((unit["state"], unit["reason"]), ("failed", "nextlink_refused"))
        self.assertIn("refused by the guard", unit["error"])
        self.assertIsNone(h.store.run("bc", unit_id)["cursor"])
        self.assertFalse(fake.bc_gets(rf"{CO[0]}\)/"))
        self.assertTrue(h.unit(first, CO[1], "vendors")["complete"])
        self.assertEqual(h.unit(h.run(entity_sets=("vendors",)), CO[0], "vendors")["state"],
                         "complete")

    def test_a_refused_lines_continuation_marks_that_document_and_goes_on(self):
        fake = OpaqueSkiptokenBc({"purchaseOrders": 3})
        si = CAPTURE_ENTITY_SETS.index("purchaseOrders")
        doc = gid(0, si, 1)
        lines = f"{HOST}{BASE}/companies({CO[0]})/purchaseOrders({doc})/purchaseOrderLines"
        fake.fail(rf"purchaseOrders\({doc}\)/purchaseOrderLines", (200, {}, FakeBc.body({
            "value": fake.lines[(CO[0], "purchaseOrders", doc)][:1],
            "@odata.nextLink": f"{lines}?$expand=x&$skiptoken=%27L1%27"})))
        h = Harness(self, fake)
        report = h.run(entity_sets=("purchaseOrders",))
        unit = h.unit(report, CO[0], "purchaseOrderLines")
        self.assertEqual((unit["state"], unit["dropped"], unit["nextlink_refused"],
                          unit["records_current"]), ("partial", 1, 1, 4))
        for n in range(3):  # every other document's lines were still read
            self.assertTrue(fake.bc_gets(rf"purchaseOrders\({gid(0, si, n)}\)/purchaseOrderLines"))
        self.assertFalse(fake.bc_gets(r"\$expand=x"))
        again = h.unit(h.run(entity_sets=("purchaseOrders",)), CO[0], "purchaseOrderLines")
        self.assertEqual((again["state"], again["records_current"]), ("complete", 6))


class OfflineResolveAndClaimTests(unittest.TestCase):
    def test_handles_resolve_offline_from_a_reopened_store(self):
        h = Harness(self)
        h.run(entity_sets=("purchaseInvoices",))
        handles = h.handles(CO[0], "purchaseInvoiceLines")
        self.assertTrue(all(is_capture_handle(x) for x in handles))
        self.assertTrue(all(x.startswith(f"bc://production/{CO[0]}/purchaseInvoiceLines/")
                            for x in handles))
        h.store.close()
        calls = len(h.fake.calls)
        with CaptureStore(h.db, audience()) as offline:
            batch = offline.resolve_many(handles, SERVICE)
        self.assertEqual(len(h.fake.calls), calls)  # no source I/O at all
        self.assertEqual(batch["ok_count"], len(handles))
        self.assertEqual(batch["results"][0]["row"]["quantity"], "12")
        self.assertEqual(batch["results"][0]["key"], {"id": gid(0, 22, 0)})
        # The live citation of the same record is not a capture handle.
        live = batch["results"][0]["meta"]["live_citation"]
        self.assertFalse(is_capture_handle(live))
        # Another store generation never resolves these ids.
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        with CaptureStore(Path(scratch.name) / "other.sqlite", audience()) as other:
            self.assertEqual(other.resolve(handles[0], SERVICE)["error"], "not_visible")

    def test_claim_checks_accept_capture_handles(self):
        h = Harness(self)
        h.run(entity_sets=("purchaseOrders",))
        handle = [x for x in h.handles(CO[0], "purchaseOrderLines")
                  if gid(0, 20, 1) in x][0]  # second line of the first PO: quantity 13
        rows = h.store.claim_rows(SERVICE)

        def gate(value, field="row.quantity", actor_rows=rows):
            claims = claim_checks.parse_value_claims(
                [{"value": value, "citation": handle, "field": field}], [handle], [])
            return claim_checks.check_items(
                [{"id": "F1", "texts": [f"The line holds {value} units ({handle})."],
                  "value_claims": claims,
                  "citations": [{"handle": handle, "status": "resolved"}]}],
                rows=actor_rows)
        passed = gate("13")
        self.assertEqual(passed["failures"], [])
        self.assertEqual(passed["counts"]["passed"], 1)
        self.assertEqual([f["code"] for f in gate("14")["failures"]],
                         ["claim_value_mismatch"])
        denied = gate("13", actor_rows=h.store.claim_rows(NOBODY))
        self.assertEqual([f["code"] for f in denied["failures"]], ["claim_uncited"])


class LiveResolverAndBindingTests(unittest.TestCase):
    def test_the_live_resolver_refuses_capture_handles(self):
        h = Harness(self)
        h.run(entity_sets=("vendors",))
        [item] = [i for i in h.store.records(SERVICE, "bc", bc_unit(CO[0], "vendors"),
                                             limit=1)["items"]]
        with self.assertRaises(ValueError):
            h.reader.resolve_citation(item["citation"], SERVICE)
        live = h.reader.resolve_citation(item["meta"]["live_citation"], SERVICE)
        self.assertEqual(live["status"], "current")

    def test_a_store_holds_one_bc_tenant_and_environment(self):
        h = Harness(self)
        h.run(entity_sets=("vendors",))
        for env in ({**ENV, "BC_TENANT_ID": "22222222-2222-3333-4444-555555555555"},
                    {**ENV, "BC_ENVIRONMENT": "Sandbox"}):
            api, _t, tenant, environment = bc_read.build_bc(env, sender=h.fake, retry=h.retry)
            other = BusinessCentralReader(api, h.audience, tenant_id=tenant,
                                          environment=environment)
            calls = len(h.fake.calls)
            with self.assertRaises(CaptureError) as refused:
                BcCapture(other, h.store, books=BOOKS).capture(SERVICE, entity_sets=("vendors",))
            self.assertEqual(refused.exception.code, "binding_mismatch")
            self.assertEqual(len(h.fake.calls), calls)  # refused before any read

    def test_capture_probe_refuses_a_store_of_another_tenant(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        db = Path(scratch.name) / "c.sqlite"
        kwargs = dict(db=db, books=BOOKS, resolve_sample=0, entity_sets=("vendors",),
                      retry=BoundedBackoff(sleep=lambda s: None))
        bc_read.capture_probe(ENV, sender=FakeBc(), **kwargs)
        with self.assertRaises(CaptureError):
            bc_read.capture_probe({**ENV, "BC_TENANT_ID": "22222222-2222-3333-4444-555555555555"},
                                  sender=FakeBc(), **kwargs)


class PermissionTests(unittest.TestCase):
    def test_every_read_path_checks_the_reader_first(self):
        h = Harness(self)
        h.run(entity_sets=("purchaseOrders", "vendors"))
        po_unit = bc_unit(CO[0], "purchaseOrders")
        line_unit = bc_unit(CO[0], "purchaseOrderLines")
        po = h.handles(CO[0], "purchaseOrders")[0]
        line = h.handles(CO[0], "purchaseOrderLines")[0]
        vendor = h.handles(CO[0], "vendors")[0]
        # The buyer sees company 0's POs and their lines, nothing else.
        self.assertTrue(h.store.resolve(po, BUYER)["ok"])
        self.assertTrue(h.store.resolve(line, BUYER)["ok"])
        self.assertEqual(h.store.resolve(vendor, BUYER)["error"], "not_visible")
        self.assertEqual(len(h.store.records(BUYER, "bc", po_unit)["items"]), 3)
        with self.assertRaises(CaptureError):
            h.store.records(BUYER, "bc", bc_unit(CO[1], "purchaseOrders"))
        # Lines need the header set too; headers alone do not open them.
        self.assertTrue(h.store.resolve(po, HEADERS_ONLY)["ok"])
        self.assertEqual(h.store.resolve(line, HEADERS_ONLY)["error"], "not_visible")
        with self.assertRaises(CaptureError) as denied:
            h.store.records(HEADERS_ONLY, "bc", line_unit)
        self.assertEqual(denied.exception.code, "not_visible")
        with self.assertRaises(CaptureError):  # counts are not shown either
            h.store.completeness(NOBODY, "bc", po_unit)
        for actor in (NOBODY, Actor("other", frozenset({"svc:capture"}), "restricted"),
                      Actor("plx", frozenset({"svc:capture"}), "internal")):
            self.assertEqual(h.store.resolve(po, actor)["error"], "not_visible")

    def test_a_capture_without_a_grant_sends_no_request_for_that_set(self):
        h = Harness(self)
        report = h.capture.capture(BUYER, entity_sets=("purchaseOrders", "vendors"),
                                   page_size=2)
        self.assertFalse(h.fake.bc_gets("vendors"))
        self.assertEqual(h.unit(report, CO[0], "vendors")["state"], "not_visible")
        self.assertTrue(h.unit(report, CO[0], "purchaseOrderLines")["complete"])
        self.assertEqual(h.unit(report, CO[1], "purchaseOrders")["state"], "not_visible")

    def test_store_reads_honour_source_acl_required_like_the_live_reader(self):
        h = Harness(self)
        h.run(entity_sets=("vendors",))
        handle = h.handles(CO[0], "vendors")[0]
        strict = LiveSourceAudience({"bc:production:*": ["svc:capture"]}, tenant="plx",
                                    default_classification="restricted",
                                    source_acl_required=[f"bc:production:{CO[0]}:vendors"])
        h.store.close()
        with CaptureStore(h.db, strict) as store:
            # BC exposes no per-record ACL: an ACL-required resource is withheld, as live.
            self.assertEqual(store.resolve(handle, SERVICE)["error"], "not_visible")
            listing = store.records(SERVICE, "bc", bc_unit(CO[0], "vendors"))
            self.assertEqual((listing["items"], listing["withheld_count"]), ([], 3))
            self.assertEqual(len(store.records(SERVICE, "bc", bc_unit(CO[1], "vendors"))
                                 ["items"]), 3)
        self.assertFalse(strict.item_visible(SERVICE, f"bc:production:{CO[0]}:vendors",
                                             {"status": "not_exposed"}))

    def test_a_store_bound_to_another_tenant_refuses(self):
        h = Harness(self)
        h.run(entity_sets=("vendors",))
        handle = h.handles(CO[0], "vendors")[0]
        h.store.close()
        other = LiveSourceAudience({"bc:production:*": ["svc:capture"]}, tenant="elsewhere",
                                   default_classification="restricted")
        with CaptureStore(h.db, other) as store:
            actor = Actor("elsewhere", frozenset({"svc:capture"}), "restricted")
            self.assertEqual(store.resolve(handle, actor)["error"], "binding_mismatch")
            with self.assertRaises(CaptureError):
                store.begin_run("bc", bc_unit(CO[0], "vendors"),
                                [h.reader.resource(CO[0], "vendors")])


class CliTests(unittest.TestCase):
    def test_capture_command_prints_counts_and_shapes_only(self):
        fake = FakeBc({"purchaseOrders": 4})
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        clock = Clock()
        result = bc_read.capture_probe(
            ENV, sender=fake, db=Path(scratch.name) / "c.sqlite", page_size=2,
            resolve_sample=50, passes=2, books=BOOKS, monotonic=clock.monotonic,
            retry=BoundedBackoff(sleep=lambda s: None))
        self.assertEqual(result["companies_captured"], 3)
        self.assertEqual(result["unpinned_companies"], 1)
        self.assertEqual(result["books"], ["consol", "distribution", "manufacturing"])
        self.assertEqual(result["units_complete"], 3 * ALL_UNITS)
        self.assertEqual(result["new_versions_per_pass"][1], 0)
        self.assertTrue(result["resolve_all_ok"])
        self.assertEqual(result["handles_resolved"], 50)
        self.assertGreater(result["store_payload_bytes"], 0)
        self.assertEqual(result["store_pruned_versions"], 0)
        text = json.dumps(result)
        for secret in (CLIENT_SECRET, ACCESS_TOKEN, *CO, "PLX Distribution", "N00000",
                       "bc://", "JzE"):
            self.assertNotIn(secret, text)

    def test_the_shipped_book_map_pins_the_portal_guids(self):
        books = bc_read.load_company_books()
        self.assertEqual(sorted(books["production"].values()), ["manufacturing"])
        only, names = bc_read.load_company_scope()
        self.assertEqual(only, {"production"})
        self.assertEqual(names, {"production": {MY_COMPANY: "My Company"}})
        self.assertEqual(set(books["sandbox"].values()), {"manufacturing"})
        for lineage in ("bc_" + b for b in books["production"].values()):
            self.assertTrue(capture_store.lineage_ok(lineage))
        with self.assertRaises(ValueError):
            bc_read.load_company_books({"schema": "plx-bc-company-books/v1",
                                        "environments": {"production": {"PLX": "x"}}})


class CompanyListingCompletenessTests(unittest.TestCase):
    """Review dd380a4 (capture-bc-company-completeness): all_complete ignored an
    incomplete company listing, so a run that never saw a pinned company said complete."""

    class PagedCompanies(FakeBc):
        def __init__(self, per_page: int, **kwargs) -> None:
            super().__init__(**kwargs)
            self.per_page = per_page

        def __call__(self, method, url, headers, body, timeout, max_bytes):
            parts = urlsplit(url)
            if (parts.hostname != "login.microsoftonline.com"
                    and unquote(parts.path) == f"{BASE}/companies"):
                self.calls.append((method, url, dict(headers)))
                offset = int(parse_qs(parts.query).get("$skiptoken", ["0"])[0])
                chunk = self.companies[offset:offset + self.per_page]
                page = {"value": [{"id": c, "name": NAMES[c], "displayName": NAMES[c],
                                   "@odata.etag": 'W/"c"'} for c in chunk]}
                if offset + self.per_page < len(self.companies):
                    page["@odata.nextLink"] = (f"{HOST}{parts.path}?$skiptoken="
                                               f"{offset + self.per_page}")
                return 200, {}, self.body(page)
            return super().__call__(method, url, headers, body, timeout, max_bytes)

    def test_three_pinned_companies_one_per_page_are_not_all_complete(self):
        h = Harness(self, self.PagedCompanies(1, companies=list(CO)))
        report = h.run(entity_sets=("vendors",))
        self.assertFalse(report["companies_listing_complete"])
        self.assertEqual(report["pinned_companies_missing"], 1)
        self.assertEqual(report["units_complete"], report["units_total"])  # 2 of 3 read
        self.assertFalse(report["all_complete"])
        self.assertFalse(h.fake.bc_gets(CO[2]))

    def test_all_complete_once_every_pinned_company_is_listed_and_captured(self):
        h = Harness(self, self.PagedCompanies(2, companies=list(CO)))
        report = h.run(entity_sets=("vendors",))
        self.assertTrue(report["companies_listing_complete"])
        self.assertEqual(report["pinned_companies_missing"], 0)
        self.assertEqual(len(report["companies"]), 3)
        self.assertTrue(report["all_complete"])

    def test_a_pinned_company_absent_from_a_complete_listing_is_not_complete(self):
        h = Harness(self, FakeBc(companies=CO[:2]))
        report = h.run(entity_sets=("vendors",))
        self.assertTrue(report["companies_listing_complete"])
        self.assertEqual(report["pinned_companies_missing"], 1)
        self.assertFalse(report["all_complete"])


class ModifiedSinceGrammarTests(unittest.TestCase):
    """The GET-only grammar is a security boundary: exactly one new typed form."""

    PATH = f"{BASE}/companies({CO[0]})/"
    INSTANT = "2026-09-30T09:50:00.000Z"

    def ok(self, entity_set: str, query: str) -> bool:
        return grammar.bc_request_query_ok(self.PATH + entity_set, query)

    def test_the_typed_form_is_built_and_accepted_on_the_listed_sets_only(self):
        for entity_set in sorted(grammar.BC_MODIFIED_SINCE_SETS):
            query = grammar.bc_query(entity_set, modified_since=self.INSTANT)
            self.assertEqual(query, {"$filter": f"lastModifiedDateTime ge {self.INSTANT}"})
            url = grammar.url("bc.entities", query, tenant=TENANT_ID,
                              environment="Production", company=CO[0], entity=entity_set)
            check_request("GET", url, environ=ENV)  # the read-only guard accepts it
            parts = urlsplit(url)
            self.assertTrue(grammar.bc_request_query_ok(parts.path, parts.query))
            keys = grammar.bc_query(entity_set, keys_only=True)
            self.assertEqual(keys, {"$select": "id,lastModifiedDateTime"})
            self.assertTrue(self.ok(entity_set, "%24select=id%2ClastModifiedDateTime"))
        # With the server's continuation parameters, as a nextLink carries them.
        self.assertTrue(self.ok("items", "aid=FINANCE&%24filter=lastModifiedDateTime+ge+"
                                "2026-09-30T09%3A50%3A00.000Z&%24skiptoken=2"))
        self.assertTrue(self.ok("items", "$filter=lastModifiedDateTime ge "
                                "2026-09-30T09:50:00.000Z&$top=10"))

    def test_line_sets_and_unlisted_sets_refuse_both_forms(self):
        for entity_set in [*BC_DOCUMENT_LINES.values(), "accounts", "dimensions",
                           "agedAccountsPayables", "currencies"]:
            self.assertNotIn(entity_set, grammar.BC_MODIFIED_SINCE_SETS)
            with self.assertRaises(ValueError):
                grammar.bc_query(entity_set, modified_since=self.INSTANT)
            with self.assertRaises(ValueError):
                grammar.bc_query(entity_set, keys_only=True)
            self.assertFalse(self.ok(entity_set, f"$filter=lastModifiedDateTime ge "
                                     f"{self.INSTANT}"))
            self.assertFalse(self.ok(entity_set, "$select=id,lastModifiedDateTime"))
        for parent in BC_DOCUMENT_LINES:  # the nested line navigation too
            path = f"{BASE}/companies({CO[0]})/{parent}({gid(0, 0, 0)})/{BC_DOCUMENT_LINES[parent]}"
            self.assertFalse(grammar.bc_request_query_ok(
                path, f"$filter=lastModifiedDateTime ge {self.INSTANT}"))

    def test_no_other_operator_field_or_value_shape_passes(self):
        bad_filters = [
            "lastModifiedDateTime gt 2026-09-30T09:50:00.000Z",
            "lastModifiedDateTime le 2026-09-30T09:50:00.000Z",
            "lastModifiedDateTime eq 2026-09-30T09:50:00.000Z",
            "lastModifiedDateTime ne 2026-09-30T09:50:00.000Z",
            "lastModifiedDateTime  ge 2026-09-30T09:50:00.000Z",
            "lastModifiedDateTime ge  2026-09-30T09:50:00.000Z",
            "lastmodifieddatetime ge 2026-09-30T09:50:00.000Z",
            "systemModifiedAt ge 2026-09-30T09:50:00.000Z",
            "postingDate ge 2026-09-30T09:50:00.000Z",
            "lastModifiedDateTime ge 2026-09-30T09:50:00Z",
            "lastModifiedDateTime ge 2026-09-30T09:50:00.0000Z",
            "lastModifiedDateTime ge 2026-09-30T09:50:00.000+00:00",
            "lastModifiedDateTime ge 2026-09-30",
            "lastModifiedDateTime ge 2026-13-30T09:50:00.000Z",
            "lastModifiedDateTime ge 2026-02-30T09:50:00.000Z",
            "lastModifiedDateTime ge 2026-09-30T24:00:00.000Z",
            "lastModifiedDateTime ge '2026-09-30T09:50:00.000Z'",
            "lastModifiedDateTime ge 2026-09-30T09:50:00.000Z ",
            "lastModifiedDateTime ge 2026-09-30T09:50:00.000Z and number eq 'N1'",
            "lastModifiedDateTime ge 2026-09-30T09:50:00.000Z or 1 eq 1",
            "number eq 'N1' and lastModifiedDateTime ge 2026-09-30T09:50:00.000Z",
            "lastModifiedDateTime ge now()",
            "lastModifiedDateTime ge 2026-09-30T09:50:00.000Z)",
            "(lastModifiedDateTime ge 2026-09-30T09:50:00.000Z)",
            "lastModifiedDateTime ge ２０２６-09-30T09:50:00.000Z",
            "lastModifiedDateTime ge",
        ]
        for text in bad_filters:
            self.assertFalse(self.ok("items", "$filter=" + quote(text, safe="")), text)
            if text.startswith("lastModifiedDateTime ge "):
                with self.assertRaises(ValueError, msg=text):
                    grammar.bc_query("items", modified_since=text[len(
                        "lastModifiedDateTime ge "):])
        bad_queries = [
            f"$filter=lastModifiedDateTime ge {self.INSTANT}"
            f"&$filter=lastModifiedDateTime ge {self.INSTANT}",
            "$select=lastModifiedDateTime,id",
            "$select=id,lastModifiedDateTime,number",  # items: no spec allows it as keys
            "$select=id,lastModifiedDateTime&$expand=itemCategory",
            f"$filter=lastModifiedDateTime ge {self.INSTANT}&$skip=5",
            f"$filter=lastModifiedDateTime ge {self.INSTANT}&$skiptoken=a b",
        ]
        for query in bad_queries:
            self.assertFalse(self.ok("itemLedgerEntries", query), query)
        for value in (None, 1, b"x", self.INSTANT.lower(), self.INSTANT.replace("T", " ")):
            if value is None:
                continue
            with self.assertRaises(ValueError):
                grammar.bc_query("items", modified_since=value)
        with self.assertRaises(ValueError):  # one filter per read
            grammar.bc_query("items", modified_since=self.INSTANT, equals=("number", "N1"))
        with self.assertRaises(ValueError):
            grammar.bc_query("items", keys_only=True, select=["number"])
        with self.assertRaises(ValueError):
            grammar.bc_query("items", keys_only="yes")
        # The pre-existing forms are unchanged.
        self.assertEqual(grammar.bc_query("items", equals=("number", "N1")),
                         {"$filter": "number eq 'N1'"})
        self.assertFalse(self.ok("items", "$filter=number ge 'N1'"))


def set_modified(fake: FakeBc, company: str, entity_set: str, index: int, when: str,
                 **fields) -> dict:
    row = fake.data[(company, entity_set)][index]
    row.update({"lastModifiedDateTime": when, **fields})
    return row


class IncrementalCaptureTests(unittest.TestCase):
    """``mode="auto"``: modified-since reads after the first full pass, a key-only full
    pass weekly. Injected transport."""

    def auto(self, h: Harness, **kwargs):
        kwargs.setdefault("mode", "auto")
        return h.run(**kwargs)

    def test_first_pass_is_full_then_only_records_modified_since_the_watermark(self):
        fake = FakeBc({"salesShipments": 5})
        set_modified(fake, CO[0], "salesShipments", 3, "2026-09-30T11:20:30.456Z")
        h = Harness(self, fake)
        first = self.auto(h, entity_sets=("salesShipments",))
        unit = h.unit(first, CO[0], "salesShipments")
        self.assertEqual((unit["mode"], unit["keys_only"], unit["since"], unit["state"]),
                         ("full", False, None, "complete"))
        self.assertEqual(unit["last_full_pass_at"], "2026-10-01T12:00:00.000000Z")
        # One record changes (content and lastModifiedDateTime) after the first pass.
        h.clock.now = T0 + timedelta(hours=2)
        set_modified(fake, CO[0], "salesShipments", 1, "2026-10-01T13:00:00.000Z",
                     **{"@odata.etag": 'W/"v2"', "amount": "#7.25"})
        before = len(fake.calls)
        second = self.auto(h, entity_sets=("salesShipments",))
        unit = h.unit(second, CO[0], "salesShipments")
        # Watermark: the latest lastModifiedDateTime held (11:20:30.456) minus 10 min.
        self.assertEqual((unit["mode"], unit["since"]), ("incremental",
                                                         "2026-09-30T11:10:30.456Z"))
        reads = [u for u in fake.bc_gets(rf"{CO[0]}\)/salesShipments")
                 if urlsplit(u).query][-1:]
        self.assertIn("$filter=lastModifiedDateTime ge 2026-09-30T11:10:30.456Z",
                      unquote(reads[0]).replace("+", " "))
        # Only the two records modified since then come back; one is a new version.
        self.assertEqual((unit["records"], unit["rows_changed"], unit["new_versions"]),
                         (2, 1, 1))
        self.assertEqual(unit["requests"], 1)  # one delta page
        self.assertEqual(sum(u["requests"] for c in second["companies"] for u in c["units"])
                         + 1, len(fake.calls[before:]))  # + the company listing
        self.assertTrue(unit["complete"])
        # The delta read never marks records it did not see as missing ...
        self.assertEqual((unit["records_current"], unit["records_missing"]), (5, 0))
        # ... and the store says what it is: not a full traversal.
        store_view = h.store.completeness(SERVICE, "bc", bc_unit(CO[0], "salesShipments"))
        self.assertEqual((store_view["state"], store_view["reason"]),
                         ("partial", "incremental"))
        self.assertTrue(second["all_complete"] is False)  # an unpinned company is listed
        self.assertEqual(second["units_complete"], second["units_total"])
        # Every request still passes the read-only guard.
        for method, url, _ in fake.calls:
            if method == "GET":
                check_request("GET", url, environ=ENV)

    def test_the_weekly_key_only_full_pass_marks_missing_and_reads_changed_by_id(self):
        fake = FakeBc({"salesShipments": 4})
        h = Harness(self, fake)
        self.auto(h, entity_sets=("salesShipments",))
        h.clock.now = T0 + timedelta(days=1)
        self.assertEqual(h.unit(self.auto(h, entity_sets=("salesShipments",)), CO[0],
                                "salesShipments")["mode"], "incremental")
        gone = fake.data[(CO[0], "salesShipments")].pop(2)
        changed = set_modified(fake, CO[0], "salesShipments", 0, "2026-10-08T12:30:00.000Z",
                               **{"@odata.etag": 'W/"c2"', "amount": "#3.75"})
        h.clock.now = T0 + timedelta(days=7, hours=1)
        before = len(fake.calls)
        report = self.auto(h, entity_sets=("salesShipments",))
        unit = h.unit(report, CO[0], "salesShipments")
        self.assertEqual((unit["mode"], unit["keys_only"], unit["state"]),
                         ("full", True, "complete"))
        listed = [u for _m, u, _h in fake.calls[before:] if f"{CO[0]})/salesShipments" in u]
        self.assertTrue(all("lastModifiedDateTime" in unquote(u) for u in listed
                            if "salesShipments?" in u))
        # Only the changed record is read by id; the unchanged ones are confirmed by key.
        by_id = [u for u in listed if re.search(r"salesShipments\([0-9a-f-]+\)$", unquote(u))]
        self.assertEqual([unquote(u).rsplit("(", 1)[1][:-1] for u in by_id], [changed["id"]])
        self.assertEqual((unit["keys_refetched"], unit["rows_changed"]), (1, 1))
        # records_current counts current versions, the missing one included.
        self.assertEqual((unit["records_current"], unit["records_missing"]), (4, 1))
        missing = h.store.records(SERVICE, "bc", bc_unit(CO[0], "salesShipments"),
                                  include_missing=True, limit=10)["items"]
        marked = {i["key"]["id"]: i["missing_from_source_since"] for i in missing}
        self.assertEqual(marked[gone["id"]], "2026-10-08T13:00:00.000000Z")
        # Confirmed keys added no version: 4 first-pass versions + 1 changed record.
        self.assertEqual(unit["versions_total"], 5)
        # A pass that found a missing key brings the next full pass forward to a day.
        h.clock.now = T0 + timedelta(days=7, hours=5)
        self.assertEqual(h.unit(self.auto(h, entity_sets=("salesShipments",)), CO[0],
                                "salesShipments")["mode"], "incremental")
        h.clock.now = T0 + timedelta(days=8, hours=2)
        self.assertEqual(h.unit(self.auto(h, entity_sets=("salesShipments",)), CO[0],
                                "salesShipments")["mode"], "full")

    def test_a_change_the_watermark_missed_puts_the_unit_back_on_full_reads(self):
        fake = FakeBc({"salesShipments": 3})
        for company in CO:  # the watermark: 11:00 - 10 min
            set_modified(fake, company, "salesShipments", 2, "2026-09-30T11:00:00Z")
        h = Harness(self, fake)
        self.auto(h, entity_sets=("salesShipments",))
        # BC changed content without moving lastModifiedDateTime past the watermark.
        set_modified(fake, CO[1], "salesShipments", 0, "2026-09-30T10:00:00Z",
                     **{"@odata.etag": 'W/"silent"', "amount": "#9.00"})
        h.clock.now = T0 + timedelta(days=1)
        quiet = h.unit(self.auto(h, entity_sets=("salesShipments",)), CO[1], "salesShipments")
        self.assertEqual((quiet["mode"], quiet["rows_changed"]), ("incremental", 0))
        h.clock.now = T0 + timedelta(days=8)
        full = h.unit(self.auto(h, entity_sets=("salesShipments",)), CO[1], "salesShipments")
        self.assertEqual((full["mode"], full["rows_changed"]), ("full", 1))
        h.clock.now = T0 + timedelta(days=8, hours=1)
        after = self.auto(h, entity_sets=("salesShipments",))
        self.assertEqual(h.unit(after, CO[1], "salesShipments")["mode"], "full")  # stays full
        self.assertEqual(h.unit(after, CO[0], "salesShipments")["mode"], "incremental")

    def test_a_failed_delta_read_never_advances_the_watermark(self):
        fake = FakeBc({"salesShipments": 3})
        h = Harness(self, fake)
        self.auto(h, entity_sets=("salesShipments",))
        h.clock.now = T0 + timedelta(hours=1)
        set_modified(fake, CO[0], "salesShipments", 0, "2026-10-01T12:30:00.000Z",
                     **{"@odata.etag": 'W/"late"'})
        fake.fail(rf"{CO[0]}\)/salesShipments\?\$filter", (403, {}, b'{"error":{"code":"Forbidden"}}'))
        failed = h.unit(self.auto(h, entity_sets=("salesShipments",)), CO[0], "salesShipments")
        self.assertEqual((failed["mode"], failed["state"], failed["complete"]),
                         ("incremental", "failed", False))
        h.clock.now = T0 + timedelta(hours=2)
        again = h.unit(self.auto(h, entity_sets=("salesShipments",)), CO[0], "salesShipments")
        # After an unsettled run the unit gets a key-only full pass, not a later since.
        self.assertEqual((again["mode"], again["keys_only"], again["state"],
                          again["rows_changed"]), ("full", True, "complete", 1))

    def test_a_delta_run_stopped_on_its_first_page_resumes_with_the_same_since(self):
        fake = FakeBc({"salesShipments": 3})
        h = Harness(self, fake)
        self.auto(h, entity_sets=("salesShipments",))
        h.clock.now = T0 + timedelta(hours=1)
        fake.fail(rf"{CO[0]}\)/salesShipments\?\$filter",
                  *[(429, {"Retry-After": "120"}, b'{"error":{"code":"TooManyRequests"}}')] * 3)
        stopped = self.auto(h, entity_sets=("salesShipments",))
        self.assertEqual(stopped["stopped_reason"], "throttled")
        since = h.unit(stopped, CO[0], "salesShipments")["since"]
        # Newer data arrives meanwhile; the resumed run keeps the pinned since.
        set_modified(fake, CO[0], "salesShipments", 2, "2026-10-01T12:50:00.000Z",
                     **{"@odata.etag": 'W/"n"'})
        h.clock.now = T0 + timedelta(hours=1, minutes=5)
        done = h.unit(self.auto(h, entity_sets=("salesShipments",)), CO[0], "salesShipments")
        self.assertEqual((done["since"], done["mode"], done["complete"]),
                         (since, "incremental", True))
        last = [u for u in fake.bc_gets(rf"{CO[0]}\)/salesShipments\?")][-1]
        self.assertIn(since, unquote(last))

    def test_posted_invoice_lines_are_kept_receipt_lines_reread_and_a_draft_is_never_read(self):
        fake = FakeBc({"salesInvoices": 3, "purchaseReceipts": 2})
        for n in range(3):
            fake.data[(CO[0], "salesInvoices")][n]["status"] = "Draft" if n == 2 else "Open"
        h = Harness(self, fake)
        sets = ("salesInvoices", "purchaseReceipts")
        self.auto(h, entity_sets=sets)
        h.clock.now = T0 + timedelta(hours=1)
        # A posted invoice whose header changed (status Open -> Paid) gets a re-read.
        paid = set_modified(fake, CO[0], "salesInvoices", 1, "2026-10-01T12:40:00.000Z",
                            **{"@odata.etag": 'W/"paid"', "status": "Paid"})
        before = len(fake.calls)
        report = self.auto(h, entity_sets=sets)
        gets = [unquote(u) for _m, u, _h in fake.calls[before:]]
        line_reads = {u.rsplit("/", 2)[1] for u in gets if u.endswith("Lines")}
        unchanged_posted, draft = gid(0, 4, 0), gid(0, 4, 2)
        self.assertNotIn(f"salesInvoices({unchanged_posted})", line_reads)
        self.assertNotIn(f"salesInvoices({draft})", line_reads)  # posted-only: never read
        self.assertIn(f"salesInvoices({paid['id']})", line_reads)
        for n in range(2):  # receipts: always (Undo Receipt adds lines to the posted one)
            self.assertIn(f"purchaseReceipts({gid(0, 1, n)})", line_reads)
        lines = h.unit(report, CO[0], "salesInvoiceLines")
        self.assertEqual((lines["mode"], lines["lines_skipped"], lines["complete"]),
                         ("incremental", 1, True))
        # Skipped lines stay current (never marked missing) and the store says partial.
        self.assertEqual((lines["records_current"], lines["records_missing"]), (4, 0))
        self.assertEqual((lines["state"], lines["reason"]), ("partial", "incremental"))
        receipts = h.unit(report, CO[0], "purchaseReceiptLines")
        self.assertEqual((receipts["lines_skipped"], receipts["state"]), (0, "complete"))
        # The next delta run still skips both unchanged posted invoices.
        h.clock.now = T0 + timedelta(hours=2)
        again = h.unit(self.auto(h, entity_sets=sets), CO[0], "salesInvoiceLines")
        self.assertEqual(again["lines_skipped"], 2)
        # The weekly full pass reads every invoice's lines and completes the unit.
        h.clock.now = T0 + timedelta(days=7, hours=1)
        full = h.unit(self.auto(h, entity_sets=sets), CO[0], "salesInvoiceLines")
        self.assertEqual((full["mode"], full["lines_skipped"], full["state"]),
                         ("full", 0, "complete"))

    def test_changed_lines_of_an_unchanged_posted_invoice_stop_the_skip(self):
        fake = FakeBc({"purchaseInvoices": 2})
        for company in CO:
            for row in fake.data[(company, "purchaseInvoices")]:
                row["status"] = "Open"
        h = Harness(self, fake)
        sets = ("purchaseInvoices",)
        self.auto(h, entity_sets=sets)
        doc = gid(0, 2, 0)
        fake.lines[(CO[0], "purchaseInvoices", doc)][0]["quantity"] = "#99"
        fake.lines[(CO[0], "purchaseInvoices", doc)][0]["@odata.etag"] = 'W/"edited"'
        h.clock.now = T0 + timedelta(days=7, hours=1)  # the full pass sees the change
        full = h.unit(self.auto(h, entity_sets=sets), CO[0], "purchaseInvoiceLines")
        self.assertEqual((full["mode"], full["rows_changed"]), ("full", 1))
        h.clock.now = T0 + timedelta(days=7, hours=2)
        after = h.unit(self.auto(h, entity_sets=sets), CO[0], "purchaseInvoiceLines")
        # The skip is off for this unit: a full lines read, nothing skipped.
        self.assertEqual((after["mode"], after["lines_skipped"], after["state"]),
                         ("full", 0, "complete"))
        # Another company, whose posted lines did not change, keeps the skip.
        self.assertEqual(after["requests"], 2)  # both headers' lines re-read
        self.assertEqual(h.unit(self.auto(h, entity_sets=sets), CO[1],
                                "purchaseInvoiceLines")["lines_skipped"], 2)

    def test_full_mode_stays_a_full_content_read_every_call(self):
        h = Harness(self, FakeBc({"salesShipments": 3}))
        h.run(entity_sets=("salesShipments",))
        h.clock.now = T0 + timedelta(hours=1)
        unit = h.unit(h.run(entity_sets=("salesShipments",)), CO[0], "salesShipments")
        self.assertEqual((unit["mode"], unit["keys_only"], unit["state"]),
                         ("full", False, "complete"))
        self.assertFalse([u for u in h.fake.bc_gets("salesShipments") if "$filter" in unquote(u)
                          or "$select" in unquote(u)])

    def test_capture_probe_reports_mode_since_requests_and_rows_per_unit(self):
        fake = FakeBc({"salesShipments": 4})
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        clock = Clock()
        result = bc_read.capture_probe(
            ENV, sender=fake, db=Path(scratch.name) / "c.sqlite", page_size=2,
            resolve_sample=10, passes=2, books=BOOKS, monotonic=clock.monotonic,
            retry=BoundedBackoff(sleep=lambda s: None), entity_sets=("salesShipments",))
        self.assertEqual(result["mode"], "auto")
        self.assertEqual(result["modes_per_pass"], [["full"], ["incremental"]])
        self.assertEqual(result["rows_changed_per_pass"][1], 0)
        self.assertTrue(all(n > 0 for n in result["requests_per_pass"]))
        unit = result["companies"][0]["units"][0]
        for field in ("mode", "since", "requests", "rows_changed", "keys_only"):
            self.assertIn(field, unit)
        self.assertRegex(unit["since"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertTrue(result["resolve_all_ok"])
        text = json.dumps(result)
        for secret in (CLIENT_SECRET, ACCESS_TOKEN, *CO, "N00000", "bc://", "JzE"):
            self.assertNotIn(secret, text)
        # The live check's third pass: the key-only full pass, on demand.
        scratch2 = tempfile.TemporaryDirectory()
        self.addCleanup(scratch2.cleanup)
        three = bc_read.capture_probe(
            ENV, sender=FakeBc({"salesShipments": 4}), db=Path(scratch2.name) / "c.sqlite",
            page_size=2, resolve_sample=0, passes=3, books=BOOKS,
            monotonic=Clock().monotonic, retry=BoundedBackoff(sleep=lambda s: None),
            entity_sets=("salesShipments",), key_pass_last=True)
        self.assertEqual(three["modes_per_pass"], [["full"], ["incremental"], ["full"]])
        units = [u for c in three["companies"] for u in c["units"]
                 if u["entity_set"] == "salesShipments"]
        self.assertTrue(all(u["keys_only"] and u["state"] == "complete" for u in units))
        self.assertEqual(three["keys_refetched_per_pass"][2], 0)
        self.assertEqual(three["rows_changed_per_pass"][1:], [0, 0])


class ReviewRepairTests(unittest.TestCase):
    """Review 91614bf (changes_required): calculated fields, clean-only line skips,
    the incremental_disabled reset and per-key failures. Injected transport."""

    def current_row(self, h: Harness, company: str, entity_set: str, record_id: str):
        items = h.store.records(SERVICE, "bc", bc_unit(company, entity_set),
                                include_missing=True, limit=100)["items"]
        return next(i for i in items if i["key"]["id"] == record_id)

    def test_every_modified_since_set_declares_its_calculated_fields(self):
        for entity_set in grammar.BC_MODIFIED_SINCE_SETS:
            self.assertIn(entity_set, grammar.BC_CALCULATED_FIELDS)
            spec = grammar.BC_QUERY_SPECS.get(entity_set)
            if spec is not None:  # declared names are names of the select allowlist
                self.assertLessEqual(grammar.BC_CALCULATED_FIELDS[entity_set], spec.select)
        for entity_set, field in (("items", "inventory"), ("vendors", "balance"),
                                  ("customers", "balanceDue"),
                                  ("salesInvoices", "totalAmountIncludingTax"),
                                  ("purchaseInvoices", "status")):
            self.assertIn(field, grammar.BC_CALCULATED_FIELDS[entity_set])
            self.assertTrue(grammar.bc_has_calculated_fields(entity_set))
        self.assertTrue(grammar.bc_has_calculated_fields("accounts"))  # undeclared

    def test_a_calculated_field_change_with_the_same_etag_is_captured_in_auto_mode(self):
        fake = FakeBc({"items": 3, "vendors": 2})
        h = Harness(self, fake)
        sets = ("items", "vendors")
        h.run(mode="auto", entity_sets=sets)
        item = fake.data[(CO[0], "items")][0]
        vendor = fake.data[(CO[0], "vendors")][1]
        # BC FlowFields move; the etag and lastModifiedDateTime do not.
        item["inventory"], vendor["balance"] = "#42", "#1234.50"
        for days in (1, 8, 9):
            h.clock.now = T0 + timedelta(days=days)
            report = h.run(mode="auto", entity_sets=sets)
            for entity_set in sets:
                unit = h.unit(report, CO[0], entity_set)
                self.assertEqual((unit["mode"], unit["keys_only"], unit["since"],
                                  unit["full_reason"], unit["state"]),
                                 ("full", False, None, "calculated_fields", "complete"))
            self.assertEqual(self.current_row(h, CO[0], "items", item["id"])["row"]
                             ["inventory"], "42")
            self.assertEqual(self.current_row(h, CO[0], "vendors", vendor["id"])["row"]
                             ["balance"], "1234.50")
        listings = [unquote(u) for u in fake.bc_gets(r"/(items|vendors)\?")]
        self.assertFalse([u for u in listings if "$filter" in u or "$select" in u])
        # The key-only confirmation refuses such a set outright.
        with self.assertRaises(CaptureError):
            h.capture._confirm_keys(SERVICE, CO[0], "items", "bc_manufacturing", [], {},
                                    bc_read._default_plan())

    def test_a_posted_invoice_whose_lines_read_dropped_a_line_is_reread(self):
        fake = FakeBc({"salesInvoices": 3})
        for n in range(3):
            fake.data[(CO[0], "salesInvoices")][n]["status"] = "Open"
        doc = gid(0, 4, 0)
        fake.lines[(CO[0], "salesInvoices", doc)][1]["documentId"] = gid(0, 4, 2)
        h = Harness(self, fake)
        sets = ("salesInvoices",)
        first = h.unit(h.run(mode="auto", entity_sets=sets), CO[0], "salesInvoiceLines")
        self.assertEqual((first["state"], first["dropped"], first["complete"]),
                         ("partial", 1, False))
        h.clock.now = T0 + timedelta(hours=1)
        before = len(fake.calls)
        second = h.unit(h.run(mode="auto", entity_sets=sets), CO[0], "salesInvoiceLines")
        reread = [u for _m, u, _h in fake.calls[before:]
                  if f"salesInvoices({doc})/salesInvoiceLines" in unquote(u)]
        self.assertEqual(len(reread), 1)  # the incomplete one is read again
        self.assertFalse(second["complete"])
        self.assertEqual((second["state"], second["dropped"]), ("partial", 1))
        # Once BC serves it cleanly the lines unit completes a full pass ...
        fake.lines[(CO[0], "salesInvoices", doc)][1]["documentId"] = doc
        h.clock.now = T0 + timedelta(hours=2)
        third = h.unit(h.run(mode="auto", entity_sets=sets), CO[0], "salesInvoiceLines")
        self.assertEqual((third["mode"], third["state"], third["complete"]),
                         ("full", "complete", True))
        # ... and only then are the unchanged posted invoices skipped.
        h.clock.now = T0 + timedelta(hours=3)
        fourth = h.unit(h.run(mode="auto", entity_sets=sets), CO[0], "salesInvoiceLines")
        self.assertEqual((fourth["lines_skipped"], fourth["complete"]), (3, True))

    def test_an_incomplete_mark_disqualifies_the_skip_between_full_passes(self):
        fake = FakeBc({"salesInvoices": 2})
        for n in range(2):
            fake.data[(CO[0], "salesInvoices")][n]["status"] = "Open"
        h = Harness(self, fake)
        sets = ("salesInvoices",)
        h.run(mode="auto", entity_sets=sets)  # clean full pass
        doc = gid(0, 4, 0)
        # The header changes (so its lines are re-read) and that read drops a line.
        set_modified(fake, CO[0], "salesInvoices", 0, "2026-10-01T12:30:00.000Z",
                     **{"@odata.etag": 'W/"paid"', "status": "Paid"})
        fake.lines[(CO[0], "salesInvoices", doc)][1]["documentId"] = gid(0, 4, 1)
        h.clock.now = T0 + timedelta(hours=1)
        mid = h.unit(h.run(mode="auto", entity_sets=sets), CO[0], "salesInvoiceLines")
        self.assertEqual((mid["mode"], mid["lines_skipped"], mid["dropped"], mid["complete"]),
                         ("incremental", 1, 1, False))
        h.clock.now = T0 + timedelta(hours=2)
        before = len(fake.calls)
        nxt = h.unit(h.run(mode="auto", entity_sets=sets), CO[0], "salesInvoiceLines")
        reread = [u for _m, u, _h in fake.calls[before:]
                  if f"salesInvoices({doc})/salesInvoiceLines" in unquote(u)]
        self.assertEqual((nxt["mode"], nxt["lines_skipped"], len(reread)),
                         ("incremental", 1, 1))
        self.assertFalse(nxt["complete"])

    def test_incremental_reads_resume_after_two_clean_full_passes(self):
        fake = FakeBc({"salesShipments": 3})
        for company in CO:
            set_modified(fake, company, "salesShipments", 2, "2026-09-30T11:00:00Z")
        h = Harness(self, fake)
        sets = ("salesShipments",)
        h.run(mode="auto", entity_sets=sets)
        set_modified(fake, CO[1], "salesShipments", 0, "2026-09-30T10:00:00Z",
                     **{"@odata.etag": 'W/"silent"', "amount": "#9.00"})
        modes = []
        for hours in (24, 8 * 24, 8 * 24 + 1, 8 * 24 + 2, 8 * 24 + 3):
            h.clock.now = T0 + timedelta(hours=hours)
            modes.append(h.unit(h.run(mode="auto", entity_sets=sets), CO[1],
                                "salesShipments")["mode"])
        # delta (misses it), full (finds it), full, full (two clean), then delta again.
        self.assertEqual(modes, ["incremental", "full", "full", "full", "incremental"])

    def test_a_400_on_a_get_by_id_fails_that_key_without_restarting_the_run(self):
        fake = FakeBc({"salesShipments": 4})
        h = Harness(self, fake)
        sets = ("salesShipments",)
        h.run(mode="auto", entity_sets=sets)
        changed = set_modified(fake, CO[0], "salesShipments", 1, "2026-10-08T12:30:00.000Z",
                               **{"@odata.etag": 'W/"c2"'})
        fake.fail(rf"salesShipments\({changed['id']}\)$",
                  (400, {}, b'{"error":{"code":"BadRequest"}}'))
        h.clock.now = T0 + timedelta(days=7, hours=1)
        before = len(fake.calls)
        unit = h.unit(h.run(mode="auto", entity_sets=sets, page_size=10), CO[0],
                      "salesShipments")
        self.assertEqual((unit["mode"], unit["keys_only"], unit["key_failures"],
                          unit["restarts"]), ("full", True, 1, 0))
        self.assertFalse(unit["complete"])  # unsettled: a key was not read
        self.assertEqual((unit["state"], unit["reason"]), ("partial", "withheld_or_dropped"))
        listings = [u for _m, u, _h in fake.calls[before:]
                    if f"{CO[0]})/salesShipments?" in unquote(u)]
        self.assertEqual(len(listings), 1)  # the listing was not re-traversed
        h.clock.now = T0 + timedelta(days=7, hours=2)
        again = h.unit(h.run(mode="auto", entity_sets=sets), CO[0], "salesShipments")
        self.assertEqual((again["mode"], again["state"]), ("full", "complete"))


if __name__ == "__main__":
    unittest.main()
