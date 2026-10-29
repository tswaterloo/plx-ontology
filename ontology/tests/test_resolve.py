"""plx.find answered from live readers through ontology/resolve.py (one resolver).

Evidence label: ``injected-transport``. The real ``SourceMirror`` (behind the real
``WorkbenchReadApiClient``) runs against the source-mirror tests' ``FakeWorkbench``;
the real GET-only ``BusinessCentralReader`` against the adapter tests' ``FakeSender``.
The schema and identifier grammar are the compiled repository ones. Every row is
SYNTHETIC. Socket connections are refused for the whole module.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, unquote, urlsplit

from integrations import bc_read
from integrations.live_source_guard import BoundedBackoff, LiveSourceAudience
from integrations.source_mirror import SourceMirror
from integrations.tests.test_live_source_adapters import ENV, TENANT_ID, FakeSender, j
from integrations.tests.test_source_mirror import BASE, KEY, Clock, FakeWorkbench
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology import identifiers
from ontology import resolve
from ontology.core import Actor
from ontology.project import build_store
from ontology.schema import load_schema
from operating import ontology_tools as ot

ROOT = Path(__file__).resolve().parents[2]
BC_BASE = f"/v2.0/{TENANT_ID}/Production/api/v2.0"
CO_A = "0a0a0a0a-0000-0000-0000-00000000000a"
CO_B = "0b0b0b0b-0000-0000-0000-00000000000b"
CO_UNPINNED = "0c0c0c0c-0000-0000-0000-00000000000c"
BOOKS = {CO_A: "manufacturing", CO_B: "distribution"}
SALES = Actor("plx", frozenset({"staff:sales"}), "restricted")
OPS_ONLY = Actor("plx", frozenset({"staff:operations"}), "restricted")


def _cat(columns):
    return {"columns": columns, "key": ["id"], "domain": "test",
            "since": {"column": "updated_at", "mode": "timestamp-utc"}}


CATALOG = {
    "public.app_sales_order": _cat(["id", "order_number", "po_reference", "customer_id",
                                    "status", "custom_fields", "updated_at"]),
    "public.app_shipment": _cat(["id", "sales_order_id", "shipment_code", "updated_at"]),
}


def _so(ident, number, po=None, fields=None):
    return {"id": ident, "order_number": number, "po_reference": po, "customer_id": "c-1",
            "status": "CONFIRMED", "custom_fields": fields, "updated_at": "2026-09-28T08:00:00Z"}


def tables():
    return {
        "public.app_sales_order": [
            _so("so-1", "SO100201", po="CPO-DUP"),
            _so("so-2", "SO100305", po="CPO-DUP"),
            # Renumbered after drain: the BC number is kept in custom_fields only.
            _so("so-3", "SO100410", fields={"renumberedTo": "BCSO-77", "fmOeNumber": "OE-9"}),
            # An FM-bridged order: the FM order_entry erpcode is kept as custom_fields.erpCode.
            _so("so-4", "SO100420", fields={"fmId": 4410, "erpCode": "OR1000999001"}),
        ],
        "public.app_shipment": [{"id": "sh-1", "sales_order_id": "so-2",
                                 "shipment_code": "SH100002",
                                 "updated_at": "2026-09-28T08:00:00Z"},
                                # Issued by a Portal database that sets the environment
                                # reference prefix (create-shipment.ts prefixRef).
                                {"id": "sh-2", "sales_order_id": "so-1",
                                 "shipment_code": "UAT-SH100003",
                                 "updated_at": "2026-09-28T08:00:00Z"}],
    }


def _bc(ident, **fields):
    return {"@odata.etag": "W/\"JzE5OzEyMzQ1Njc4OTA7MDA7Jw==\"", "id": ident,
            "lastModifiedDateTime": "2026-09-28T08:00:00Z", **fields}


def _refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


class ResolveTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = load_schema()
        cls.view = ot.SchemaView.from_compiled(cls.schema)
        cls.contracts = ot.load_contracts()

    def setUp(self) -> None:
        for target in ("socket.socket.connect", "socket.create_connection"):
            patcher = patch(target, _refuse_network)
            patcher.start()
            self.addCleanup(patcher.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.fake = FakeWorkbench()
        self.fake.catalog = json.loads(json.dumps(CATALOG))
        self.fake.tables = tables()
        self.clock = Clock()
        self.bc_records = {
            CO_A: {"salesOrders": [_bc("aaaaaaaa-0000-0000-0000-000000000001",
                                       number="SO100201", externalDocumentNumber="CPO-X")],
                   "salesInvoices": []},
            CO_B: {"salesOrders": [], "salesInvoices": []},
            CO_UNPINNED: {"salesOrders": [_bc("cccccccc-0000-0000-0000-000000000001",
                                              number="SO100201")], "salesInvoices": []},
        }
        self.failures: dict[str, int] = {}  # company -> failing answers still to give
        self.failure_status = 503
        self.bc_urls: list[str] = []
        routes = {f"{BC_BASE}/companies": j(200, {"value": [
            {"id": company, "name": company[:4], "@odata.etag": "W/\"1\""}
            for company in (CO_A, CO_B, CO_UNPINNED)]})}
        for company, sets in self.bc_records.items():
            for entity_set in sets:
                routes[f"{BC_BASE}/companies({company})/{entity_set}"] = \
                    self._entity(company, entity_set)
        self.sender = FakeSender(routes)

    def _entity(self, company, entity_set):
        def answer(url):
            self.bc_urls.append(unquote(url))
            if self.failures.get(company):
                self.failures[company] -= 1
                return j(self.failure_status, {"error": {"code": "Refused"}})
            query = dict(parse_qsl(urlsplit(url).query))
            field, value = re.fullmatch(r"(\w+) eq '(.*)'", query["$filter"]).groups()
            hits = [r for r in self.bc_records[company][entity_set]
                    if str(r.get(field, "")).lower() == value.lower()]
            return 200, {}, json.dumps({"value": hits}).encode()
        return answer

    def tools(self, *, policy=None, store_rows=None):
        mirror = SourceMirror(self.root / "mirror.sqlite",
                              WorkbenchReadApiClient(BASE, KEY, fetch=self.fake),
                              {"public.*": ["staff:sales", "staff:operations"]},
                              clock=self.clock, retry_delay_seconds=0)
        self.addCleanup(mirror.close)
        self.mirror = mirror
        api, _, tenant, environment = bc_read.build_bc(
            ENV, sender=self.sender, retry=BoundedBackoff(max_retries=0))
        reader = bc_read.BusinessCentralReader(
            api, LiveSourceAudience({"bc:production:*": ["staff:sales", "staff:operations"]},
                                    tenant="plx"),
            tenant_id=tenant, environment=environment)
        resolver = resolve.SourceResolver(
            self.view, [resolve.MirrorLookup(mirror, clock=self.clock),
                        resolve.BcLookup(reader, books=BOOKS, clock=self.clock)],
            normalizer=identifiers.normalize, workdir=self.root / "work", clock=self.clock)
        store = self.root / f"store-{len(list(self.root.glob('store-*')))}.sqlite"
        build_store(store, self.schema, store_rows or {}, normalizer=identifiers.normalize,
                    clock=self.clock)
        reader_store = ot.StoreReader.open(store)
        self.addCleanup(reader_store.conn.close)
        self.resolver = resolver
        return ot.OntologyTools(
            reader_store, policy or ot.ReadPolicy("plx", audiences={
                "api:*": {"staff:sales", "staff:operations"},
                "bc:*": {"staff:sales", "staff:operations"}}),
            schema=self.view, grammar=identifiers, contracts=self.contracts,
            clock=self.clock, sources=resolver)

    @staticmethod
    def ids(result):
        return [c["object"]["object_id"] for c in result["candidates"]]

    def reasons(self, result):
        return {g["reason"] for g in result["gaps"]}


class ExactlyOneAndAmbiguousTests(ResolveTestCase):
    def test_exactly_one_live_match_is_found_with_its_key_binding_and_reason(self):
        tools = self.tools()
        out = tools.call("plx.find", {"query": "SO100305", "object_type": "SalesOrder"}, SALES)
        self.assertEqual(out["status"], "found", out)
        (candidate,) = out["candidates"]
        self.assertEqual(candidate["object"]["object_id"],
                         'SalesOrder:portal.app_sales_order:{"id":"so-2"}')
        matched = candidate["matched"]
        self.assertEqual((matched["identifier_kind"], matched["property"], matched["binding"],
                          matched["role"]),
                         ("so_number", "order_number", "portal.app_sales_order", "primary"))
        self.assertEqual(candidate["reason"], "exact_identifier")
        self.assertTrue(matched["raw"]["origin"]["citation"].startswith("api://"))
        self.assertEqual(matched["raw"]["origin"]["field_path"], "row.order_number")
        # The answer is a live point read: never complete, never fresh-by-assumption.
        (fresh,) = [f for f in out["meta"]["freshness"] if f["source"] == "portal_api"]
        self.assertEqual(fresh["completeness"], "partial")
        self.assertEqual(out["completeness"]["total"], 1)

    def test_one_number_in_two_systems_is_ambiguous_ranked_never_a_winner(self):
        tools = self.tools()
        out = tools.call("plx.find", {"query": "SO100201", "object_type": "SalesOrder"}, SALES)
        self.assertEqual(out["status"], "ambiguous", out)
        self.assertEqual(sorted(self.ids(out)), sorted([
            'SalesOrder:portal.app_sales_order:{"id":"so-1"}',
            'SalesOrder:bc.sales_orders:{"id":"aaaaaaaa-0000-0000-0000-000000000001"}']))
        self.assertEqual([c["rank"] for c in out["candidates"]], [1, 2])
        bindings = {c["matched"]["binding"] for c in out["candidates"]}
        self.assertEqual(bindings, {"portal.app_sales_order", "bc.sales_orders"})
        bc = [c for c in out["candidates"] if c["matched"]["binding"] == "bc.sales_orders"][0]
        self.assertEqual(bc["object"]["lineage"], "bc_manufacturing")
        self.assertTrue(bc["matched"]["raw"]["origin"]["citation"].startswith("bc://"))
        # The unpinned company is never read (as bc_read.BcCapture), and it is named.
        self.assertFalse(any(CO_UNPINNED in url for url in self.bc_urls))
        self.assertIn("source_unpinned", self.reasons(out))

    def test_two_orders_carrying_one_customer_po_are_ambiguous(self):
        tools = self.tools()
        out = tools.call("plx.find", {"query": "CPO-DUP",
                                      "object_type": "CustomerPurchaseOrder"}, SALES)
        self.assertEqual(out["status"], "ambiguous", out)
        self.assertEqual(len(out["candidates"]), 2)
        self.assertEqual({c["matched"]["property"] for c in out["candidates"]}, {"po_number"})
        self.assertTrue(out["set"].startswith("set:"))

    def test_no_live_match_is_unknown_never_absent_in_source(self):
        tools = self.tools()
        out = tools.call("plx.find", {"query": "SO999999", "object_type": "SalesOrder"}, SALES)
        self.assertEqual(out["status"], "not_found")
        self.assertNotIn("absent_in_source", {g["state"] for g in out["gaps"]})
        self.assertIn("no_visible_match", self.reasons(out))

    def test_a_projection_hit_never_reads_the_live_sources(self):
        from ontology.project import CapturedRecord
        row = _so("so-9", "SO100777")
        record = CapturedRecord("portal_api", "public.app_sales_order", '{"id":"so-9"}', row,
                                'api://public.app_sales_order/%7B%22id%22%3A%22so-9%22%7D@1',
                                "row", "d" * 64, "2026-09-28T08:00:00.000Z", None,
                                "2026-09-28T08:00:00.000Z", "api:public.app_sales_order")

        class Source:
            source = "portal_api"

            def records(self, relations):
                return {r: [record] if r == "public.app_sales_order" else [] for r in relations}

            def describe(self):
                return {"generation": "g", "relations": {}}

        tools = self.tools(store_rows={"portal_api": Source()})
        out = tools.call("plx.find", {"query": "SO100777", "object_type": "SalesOrder"}, SALES)
        self.assertEqual(out["status"], "found")
        self.assertEqual(self.fake.row_requests(), [])
        self.assertEqual(self.bc_urls, [])


class PermissionTests(ResolveTestCase):
    def test_permission_is_applied_before_any_read_or_rank(self):
        policy = ot.ReadPolicy("plx", audiences={
            "api:*": {"staff:operations"},
            "api:public.app_sales_order": set(),      # this unit is not granted
            "bc:*": {"staff:operations"},
            f"bc:{CO_A}/salesOrders": set()})         # nor this company's orders
        tools = self.tools(policy=policy)
        out = tools.call("plx.find", {"query": "SO100201", "object_type": "SalesOrder"},
                         OPS_ONLY)
        # Neither denied unit was read at all: no Portal page, no BC GET of that
        # company's orders (its invoices, a granted unit linked to orders, may be read).
        self.assertEqual(self.fake.row_requests("public.app_sales_order"), [])
        self.assertFalse(any(f"companies({CO_A})/salesOrders" in url for url in self.bc_urls))
        self.assertEqual(out["status"], "not_found")
        # Withheld units are not named, and nothing hints at what was trimmed.
        self.assertFalse(any("app_sales_order" in g["subject"] for g in out["gaps"]))
        self.assertEqual(out["withheld"], {"properties": None, "objects": None,
                                           "reason": "marking_and_audience"})
        self.assertNotIn("absent_in_source", {g["state"] for g in out["gaps"]})

    def test_denied_output_equals_output_without_the_denied_records(self):
        policy = ot.ReadPolicy("plx", audiences={
            "api:*": {"staff:sales"}, "bc:*": {"staff:sales"},
            f"bc:{CO_A}/salesOrders": set()})
        denied = self.tools(policy=policy).call(
            "plx.find", {"query": "SO100201", "object_type": "SalesOrder"}, SALES)
        self.bc_records[CO_A]["salesOrders"] = []
        absent = self.tools().call(
            "plx.find", {"query": "SO100201", "object_type": "SalesOrder"}, SALES)
        self.assertEqual(denied["status"], "found")
        self.assertEqual(self.ids(denied), self.ids(absent))
        self.assertEqual(self.ids(denied), ['SalesOrder:portal.app_sales_order:{"id":"so-1"}'])


class TransientFailureTests(ResolveTestCase):
    def test_a_refusal_is_not_retried_and_no_retry_is_counted(self):
        self.failures[CO_A] = 99
        self.failure_status = 403
        self.tools()
        res = self.resolver.resolve(SALES, [("so_number", "SO100201")], "SO100201",
                                    may_read=lambda key: True, object_type="SalesOrder")
        self.addCleanup(res.discard)
        # One GET per carrier unit of company A (its orders, and the invoices linked
        # to orders), none repeated.
        asked = [url for url in self.bc_urls if f"companies({CO_A})" in url]
        self.assertEqual(sorted(asked), sorted(set(asked)))
        self.assertTrue(any(f"companies({CO_A})/salesOrders?" in url for url in asked))
        self.assertEqual(res.retried, 0)
        self.assertIn(("bc", "salesOrders", "number", "unreadable"), res.unread)
        self.assertEqual(self.failures[CO_A], 99 - len(asked))

    def test_a_lookup_that_asks_nothing_is_counted_unsearched(self):
        class Silent:
            source = "portal_api"

            def probe(self, actor, relation, path, values, may_read):
                return []

            def describe(self):
                return {}

        resolver = resolve.SourceResolver(self.view, [Silent()], workdir=self.root / "w")
        res = resolver.resolve(SALES, [("so_number", "SO100201")], "SO100201",
                               may_read=lambda key: True, object_type="SalesOrder")
        self.assertGreaterEqual(res.counts["not_searchable"], 1)
        self.assertIn(("portal_api", "public.app_sales_order", "order_number",
                       "not_searchable"), res.unread)
        self.assertFalse(res.complete)

    def test_one_transient_failure_is_retried_and_the_record_is_found(self):
        self.failures[CO_A] = 1
        out = self.tools().call("plx.find", {"query": "SO100201", "object_type": "SalesOrder"},
                                SALES)
        self.assertEqual(out["status"], "ambiguous")
        self.assertIn('SalesOrder:bc.sales_orders:{"id":"aaaaaaaa-0000-0000-0000-000000000001"}',
                      self.ids(out))
        self.assertNotIn("source_unreadable", self.reasons(out))

    def test_a_company_failing_twice_is_counted_unread_and_the_others_still_answer(self):
        self.failures[CO_A] = 99
        tools = self.tools()
        out = tools.call("plx.find", {"query": "SO100201", "object_type": "SalesOrder"}, SALES)
        # The Portal record still answers; BC company A is named unreadable, not absent.
        self.assertEqual(self.ids(out), ['SalesOrder:portal.app_sales_order:{"id":"so-1"}'])
        # One gap per carrier unit company A left unread, each naming it (the orders,
        # and the invoices the schema links to orders); only BC is named.
        gap = [g for g in out["gaps"] if g["reason"] == "source_unreadable"]
        subjects = [g["subject"] for g in gap]
        self.assertEqual(len(subjects), len(set(subjects)), out["gaps"])
        self.assertIn("bc:salesOrders.number", subjects)
        self.assertTrue(all(g["searched"] == {"sources": ["bc"]} for g in gap))
        # Company B was still read after A failed (the resolution continued).
        self.assertTrue(any(f"companies({CO_B})/salesOrders" in url for url in self.bc_urls))
        # Each of company A's units was read twice (once, then the one retry), no more.
        asked = [url for url in self.bc_urls if f"companies({CO_A})" in url]
        self.assertEqual({asked.count(url) for url in asked}, {2})

    def test_resolution_counts_unread_probes(self):
        self.failures[CO_A] = 99
        self.tools()
        res = self.resolver.resolve(SALES, [("so_number", "SO100201")], "SO100201",
                                    may_read=lambda key: True, object_type="SalesOrder")
        self.addCleanup(res.discard)
        self.assertGreaterEqual(res.counts["unreadable"], 1)
        self.assertGreaterEqual(res.retried, 1)
        self.assertFalse(res.complete)
        self.assertIn(("bc", "salesOrders", "number", "unreadable"), res.unread)


class LiveAnswersInTraversalTests(ResolveTestCase):
    """A plx.find answered from a live lookup names objects that exist only in a
    discarded throw-away store: traversal must say so, never answer "nothing"."""

    PATH = [{"link_type": "SalesOrder.references_customer_po", "direction": "reverse"}]

    def traverse(self, tools):
        from operating.ontology_tools_traverse import TraverseTools
        return TraverseTools(tools)

    def test_a_live_set_is_refused_as_not_in_projection_never_empty_and_complete(self):
        tools = self.tools()
        found = tools.call("plx.find", {"query": "CPO-DUP",
                                        "object_type": "CustomerPurchaseOrder"}, SALES)
        self.assertEqual(found["status"], "ambiguous")
        traverse = self.traverse(tools)
        for name, arguments in (("plx.around", {"from": found["set"], "path": self.PATH}),
                                ("plx.timeline", {"from": found["set"]})):
            out = traverse.call(name, arguments, SALES)
            self.assertEqual(out["error"]["code"], "not_in_projection", out)
            self.assertIn("live source lookup", out["error"]["hint"])
            self.assertNotIn("completeness", out)

    def test_a_live_object_id_is_refused_with_the_live_hint(self):
        tools = self.tools()
        found = tools.call("plx.find", {"query": "SO100305", "object_type": "SalesOrder"},
                           SALES)
        oid = found["candidates"][0]["object"]["object_id"]
        out = self.traverse(tools).call("plx.around", {"from": oid, "path": [
            {"link_type": "SalesOrder.references_customer_po", "direction": "forward"}]},
            SALES)
        self.assertEqual(out["error"]["code"], "not_in_projection")
        self.assertIn("plx.read", out["error"]["hint"])
        # Owner-bound: another principal set, another clearance or another tenant
        # learns nothing about an id it never found (no existence oracle).
        path = [{"link_type": "SalesOrder.references_customer_po", "direction": "forward"}]
        for other in (Actor("plx", frozenset({"staff:operations"}), "restricted"),
                      Actor("plx", frozenset({"staff:sales"}), "internal"),
                      Actor("acme", frozenset({"staff:sales"}), "restricted")):
            for name, arguments in (("plx.around", {"from": oid, "path": path}),
                                    ("plx.timeline", {"from": oid})):
                with self.subTest(actor=other, tool=name):
                    refused = self.traverse(tools).call(name, arguments, other)
                    self.assertEqual(refused["error"]["code"], "object_not_found")
                    self.assertNotIn("live", refused["error"].get("hint", ""))
            denied = tools.call("plx.object", {"object_id": oid}, other)
            self.assertEqual(denied["error"]["code"], "object_not_found")
        # plx.object gives the owner the same live-lookup hint.
        own = tools.call("plx.object", {"object_id": oid}, SALES)
        self.assertEqual(own["error"]["code"], "not_in_projection")
        self.assertIn("plx.read", own["error"]["hint"])
        unknown = self.traverse(tools).call("plx.around", {
            "from": 'SalesOrder:portal.app_sales_order:{"id":"nobody"}', "path": [
                {"link_type": "SalesOrder.references_customer_po", "direction": "forward"}]},
            SALES)
        self.assertEqual(unknown["error"]["code"], "object_not_found")

    def test_a_handle_with_ids_missing_from_the_store_is_never_complete(self):
        from ontology.project import CapturedRecord
        row = _so("so-9", "SO100777", po="CPO-9")
        record = CapturedRecord("portal_api", "public.app_sales_order", '{"id":"so-9"}', row,
                                'api://public.app_sales_order/%7B%22id%22%3A%22so-9%22%7D@1',
                                "row", "d" * 64, "2026-09-28T08:00:00.000Z", None,
                                "2026-09-28T08:00:00.000Z", "api:public.app_sales_order")

        class Source:
            source = "portal_api"

            def records(self, relations):
                return {r: [record] if r == "public.app_sales_order" else [] for r in relations}

            def describe(self):
                return {"generation": "g", "relations": {}}

        tools = self.tools(store_rows={"portal_api": Source()})
        stored = 'CustomerPurchaseOrder:portal.app_sales_order.po_reference:{"id":"so-9"}'
        ghost = 'CustomerPurchaseOrder:portal.app_sales_order.po_reference:{"id":"ghost"}'
        handle = tools.sets.new(SALES, [stored, ghost])
        out = self.traverse(tools).call("plx.around", {"from": handle, "path": self.PATH},
                                        SALES)
        self.assertNotIn("error", out, out)
        self.assertFalse(out["completeness"]["complete"])
        self.assertEqual(out["completeness"]["reason"], "not_in_projection")
        gap = [g for g in out["gaps"] if g["reason"] == "not_in_projection"]
        self.assertEqual(len(gap), 1)
        self.assertIn("1 object(s)", gap[0]["detail"])
        timeline = self.traverse(tools).call("plx.timeline", {"from": handle}, SALES)
        self.assertFalse(timeline["completeness"]["complete"])

    def test_live_reads_never_hold_the_projection_store_lock(self):
        import threading
        tools = self.tools()
        held: list[bool] = []
        lookup = self.resolver.lookups["portal_api"]
        real = lookup.probe

        def probe(*args, **kwargs):
            free = []
            worker = threading.Thread(target=lambda: free.append(
                tools.store.lock.acquire(timeout=2) and (tools.store.lock.release() or True)))
            worker.start()
            worker.join()
            held.append(not free[0])
            return real(*args, **kwargs)
        lookup.probe = probe
        out = tools.call("plx.find", {"query": "SO100305", "object_type": "SalesOrder"}, SALES)
        self.assertEqual(out["status"], "found")
        self.assertTrue(held)
        self.assertFalse(any(held))


class CrossSystemKeyTests(ResolveTestCase):
    def test_bc_invoice_external_document_number_finds_the_portal_order(self):
        """A root chosen in BC (an invoice) is found in the Portal from the identifier
        BC carries, by the carrying property's kind (serializers.ts:1049)."""
        tools = self.tools()
        out = tools.call("plx.find", {"query": "SO100305", "object_type": "SalesOrder",
                                      "identifier_kind": "bc_external_document_no"}, SALES)
        self.assertEqual(out["status"], "found", out)
        self.assertEqual(self.ids(out), ['SalesOrder:portal.app_sales_order:{"id":"so-2"}'])

    def test_bc_order_number_finds_a_renumbered_portal_order_through_its_json_key(self):
        """BC salesInvoice.orderNumber is the BC sales-order No.; after drain the Portal
        keeps it in custom_fields.renumberedTo (align-portal-so-number.ts@c3ffda3)."""
        tools = self.tools()
        out = tools.call("plx.find", {"query": "BCSO-77", "object_type": "SalesOrder",
                                      "identifier_kind": "so_number"}, SALES)
        self.assertEqual(out["status"], "found", out)
        (candidate,) = out["candidates"]
        self.assertEqual(candidate["object"]["object_id"],
                         'SalesOrder:portal.app_sales_order:{"id":"so-3"}')
        self.assertEqual(candidate["matched"]["property"], "renumbered_to")
        self.assertEqual(candidate["matched"]["raw"]["origin"]["field_path"],
                         "row.custom_fields.renumberedTo")
        self.assertEqual(candidate["reason"], "alias_match")

    def test_a_shipment_code_finds_the_shipment_not_an_order(self):
        tools = self.tools()
        out = tools.call("plx.find", {"query": "SH100002", "object_type": "Shipment"}, SALES)
        self.assertEqual(self.ids(out), ['Shipment:portal.app_shipment:{"id":"sh-1"}'])
        none = tools.call("plx.find", {"query": "SH100002", "object_type": "SalesOrder",
                                       "identifier_kind": "bc_external_document_no"}, SALES)
        self.assertEqual(none["status"], "not_found")

    def test_an_environment_prefixed_shipment_code_finds_its_shipment_and_no_other(self):
        """Live shape (document-finance-queries.ts:4@c3ffda3): BC carries the Portal
        shipment code with its environment prefix (UAT-SH100017). The grammar must type
        it as a shipment number, or the shipment carrier is never asked; the prefix
        stays part of the value, so the unprefixed number of another database's
        shipment never matches."""
        tools = self.tools()
        out = tools.call("plx.find", {"query": "UAT-SH100003", "object_type": "Shipment"},
                         SALES)
        self.assertEqual(out["status"], "found", out)
        self.assertEqual(self.ids(out), ['Shipment:portal.app_shipment:{"id":"sh-2"}'])
        self.assertEqual(out["candidates"][0]["matched"]["identifier_kind"], "shipment_number")
        none = tools.call("plx.find", {"query": "SH100003", "object_type": "Shipment"}, SALES)
        self.assertEqual(none["status"], "not_found")

    def test_bc_external_document_number_finds_the_order_by_its_fm_erp_code(self):
        """The Portal matches an invoice's externalDocumentNumber against the order's
        custom_fields.erpCode too (resolveSalesOrderFinanceMatchKeys,
        document-finance-queries.ts:76-79@c3ffda3)."""
        tools = self.tools()
        out = tools.call("plx.find", {"query": "OR1000999001", "object_type": "SalesOrder",
                                      "identifier_kind": "bc_external_document_no"}, SALES)
        self.assertEqual(out["status"], "found", out)
        (candidate,) = out["candidates"]
        self.assertEqual(candidate["object"]["object_id"],
                         'SalesOrder:portal.app_sales_order:{"id":"so-4"}')
        self.assertEqual(candidate["matched"]["property"], "erp_code")
        self.assertEqual(candidate["matched"]["raw"]["origin"]["field_path"],
                         "row.custom_fields.erpCode")
        self.assertEqual(candidate["tier"], "lead")

    def test_every_declared_cross_system_identifier_key_is_searchable_by_find(self):
        """A text-match link that joins another system's value to an identifier of the
        target type is only usable when plx.find searches that target path: each such
        pair must be an identifier carrier of its kind (or an alias kind) on the
        target binding. (SalesInvoice.bills declared custom_fields.erpCode with no
        identifier property: find never asked it.)"""
        binding_type = {b["id"]: tid for tid, spec in self.view.object_types.items()
                        for b in spec.get("bindings", ())}
        checked = 0
        for lid, link in sorted(self.view.link_types.items()):
            for lb in link.get("bindings", ()):
                if lb.get("kind") != "text_match":
                    continue
                for pair in lb.get("pairs") or ():
                    kind = pair.get("identifier_kind")
                    target = binding_type.get(lb.get("to_binding"))
                    if pair.get("compare") != "identifier" or not kind or target is None:
                        continue
                    kinds = {kind, *identifiers.alias_kinds(kind)}
                    found = {(c.binding, c.path)
                             for c in resolve.carriers(self.view, kinds, target)}
                    with self.subTest(link=lid, to=pair["to"]):
                        self.assertIn((lb["to_binding"], pair["to"]), found)
                    checked += 1
        self.assertGreater(checked, 3)

    def test_corroborations_come_from_the_schema_links_only(self):
        """The second identifier a cross-system root must agree on: a BC invoice's
        customer number against the Portal order's customer ERP codes, a BC receipt's
        vendor number against the Portal PO's supplier code."""
        invoice = resolve.corroborations(self.view, "bc.sales_invoices",
                                         "portal.app_sales_order")
        self.assertEqual([(c.kind, c.root_path, c.from_path, c.linked_relation, c.to_path,
                           c.linked_paths) for c in invoice],
                         [("customer_erp_code", "customerNumber", "customer_id",
                           "public.app_customer", "id", ("erp_code", "bill_to_erp_code"))])
        receipt = resolve.corroborations(self.view, "bc.purchase_receipts",
                                         "portal.app_purchase_order")
        self.assertEqual([(c.kind, c.root_path, c.linked_relation, c.linked_paths)
                          for c in receipt],
                         [("supplier_code", "vendorNumber", "public.app_supplier", ("code",))])
        # The system the linked value must have been sent to (identifiers.sent_form).
        self.assertEqual({c.root_source for c in invoice + receipt}, {"bc"})
        self.assertEqual(resolve.corroborations(self.view, "bc.sales_invoices", "nope"), [])

    def test_identifiers_agree_after_the_grammar_normaliser_only(self):
        self.assertTrue(resolve.identifiers_agree("customer_erp_code", " c0002", "C0002",
                                                  identifiers.normalize))
        self.assertFalse(resolve.identifiers_agree("customer_erp_code", "C0001", "C0002",
                                                   identifiers.normalize))
        self.assertFalse(resolve.identifiers_agree("customer_erp_code", None, None,
                                                   identifiers.normalize))
        self.assertFalse(resolve.identifiers_agree("no_such_kind", "", "",
                                                   identifiers.normalize))
        self.assertTrue(resolve.identifiers_agree("no_such_kind", "ab", "AB",
                                                  identifiers.normalize))

    def test_carriers_come_from_the_schema_only(self):
        found = {(c.binding, c.path) for c in resolve.carriers(self.view, {"so_number"},
                                                               "SalesOrder")}
        self.assertIn(("portal.app_sales_order", "order_number"), found)
        self.assertIn(("bc.sales_orders", "number"), found)
        self.assertNotIn(("portal.app_sales_order", "po_reference"), found)


class CorroborationTests(ResolveTestCase):
    """plx.find corroborates each candidate against the records of other systems that
    carry the asked key (10-DECISION 3): agree / conflict / unverifiable with the cited
    values of both sides; a counterpart the reader cannot see is absent, never a
    conflict, and nothing is merged."""

    PORTAL = 'SalesOrder:portal.app_sales_order:{"id":"so-1"}'
    BC_SO = 'SalesOrder:bc.sales_orders:{"id":"aaaaaaaa-0000-0000-0000-000000000001"}'

    def setUp(self) -> None:
        super().setUp()
        self.fake.catalog["public.app_sales_order"]["columns"] += [
            "order_date", "subtotal", "total_amount", "currency"]
        self.fake.catalog["public.app_customer"] = _cat(["id", "erp_code", "bill_to_erp_code",
                                                         "updated_at"])
        for row in self.fake.tables["public.app_sales_order"]:
            row.update(order_date="2026-09-20T08:00:00Z", subtotal="1250.00",
                       total_amount="1250.00", currency="CAD")
        self.fake.tables["public.app_customer"] = [
            {"id": "c-1", "erp_code": "C0001", "bill_to_erp_code": None,
             "updated_at": "2026-09-28T08:00:00Z"}]
        self.bc_records[CO_A]["salesOrders"][0].update(
            orderDate="2026-09-20", totalAmountExcludingTax=1250,
            totalAmountIncludingTax=1250, currencyCode="CAD")

    # Amounts carry the finance marking: this reader holds it.
    FINANCE = ot.ReadPolicy("plx", audiences={"api:*": {"staff:sales"}, "bc:*": {"staff:sales"}},
                            markings={"finance": {"staff:sales"}})

    def find(self, query="SO100201", policy=None, **extra):
        return self.tools(policy=policy or self.FINANCE).call(
            "plx.find", {"query": query, "object_type": "SalesOrder", **extra}, SALES)

    @staticmethod
    def candidate(out, object_id):
        (found,) = [c for c in out["candidates"] if c["object"]["object_id"] == object_id]
        return found

    @staticmethod
    def features(candidate):
        return {f["feature"]: f for f in candidate.get("why_not", ())}

    @staticmethod
    def value_feature(candidate, field_path):
        """The shared-value feature citing ``field_path`` on either side."""
        (found,) = [f for f in candidate.get("why_not", ())
                    if f["feature"].startswith("value_")
                    and field_path in [c["field_path"] for c in f.get("citations", ())]]
        return found

    def authoritative_portal(self):
        # A Sage-backed gold order: its lineage is authority (gold_native_staging,
        # the fixture's default, never is).
        self.fake.tables["public.app_sales_order"][0]["custom_fields"] = {
            "source": "sage.oe_order_header"}

    def test_a_portal_order_and_a_bc_order_that_agree_cite_both_values(self):
        self.authoritative_portal()
        out = self.find()
        # Two records of one order in two systems: ranked candidates, never merged.
        self.assertEqual(out["status"], "ambiguous", out)
        self.assertEqual(sorted(self.ids(out)), sorted([self.PORTAL, self.BC_SO]))
        self.assertEqual([c["rank"] for c in out["candidates"]], [1, 2])
        for oid in (self.PORTAL, self.BC_SO):
            candidate = self.candidate(out, oid)
            # No second identifier is compared between two orders: values never
            # decide identity, so the verdict stays unverifiable.
            self.assertEqual(ot.corroboration_verdict(candidate), "unverifiable")
            self.assertNotIn("no_visible_counterpart", self.features(candidate))
            # Both subtotals are declared without tax: compared.
            total = self.value_feature(candidate, "row.subtotal")
            self.assertEqual((total["feature"], total["agrees"]), ("value_equal", True))
            # Both sides' values are cited: the counterpart's first, then the candidate's.
            paths = [(c["l1_handle"].split("://")[0], c["field_path"])
                     for c in total["citations"]]
            expected = [("bc", "row.totalAmountExcludingTax"), ("api", "row.subtotal")]
            self.assertEqual(paths, expected if oid == self.PORTAL else expected[::-1])
            self.assertIs(self.value_feature(candidate, "row.order_date")["agrees"], True)
        # A reader without the finance marking compares the date only: a marked value
        # is never read, cited or compared for it.
        plain = self.candidate(self.find(policy=ot.ReadPolicy("plx", audiences={
            "api:*": {"staff:sales"}, "bc:*": {"staff:sales"}})), self.PORTAL)
        self.assertIs(self.value_feature(plain, "row.order_date")["agrees"], True)
        self.assertNotIn("total_amount", json.dumps(plain))

    def test_a_differing_amount_is_reported_with_both_values_never_a_conflict(self):
        self.authoritative_portal()
        self.bc_records[CO_A]["salesOrders"][0]["totalAmountExcludingTax"] = 1300
        out = self.find()
        self.assertEqual(out["status"], "ambiguous")
        for oid in (self.PORTAL, self.BC_SO):
            candidate = self.candidate(out, oid)
            self.assertEqual(ot.corroboration_verdict(candidate), "unverifiable")
            total = self.value_feature(candidate, "row.subtotal")
            self.assertEqual((total["feature"], total["agrees"]), ("value_differs", False))
            self.assertEqual(sorted(c["field_path"] for c in total["citations"]),
                             ["row.subtotal", "row.totalAmountExcludingTax"])
            self.assertIs(self.value_feature(candidate, "row.order_date")["agrees"], True)

    def test_a_total_with_tax_is_never_compared_with_one_without(self):
        # BC totalAmountIncludingTax (incl_tax) 113 vs the Sage-ETL Portal total_amount
        # (excl_tax for that lineage) 100: the tax, not a difference.
        self.authoritative_portal()
        self.fake.tables["public.app_sales_order"][0].update(subtotal="100.00",
                                                             total_amount="100.00")
        self.bc_records[CO_A]["salesOrders"][0].update(totalAmountExcludingTax=100,
                                                       totalAmountIncludingTax=113)
        for oid in (self.PORTAL, self.BC_SO):
            candidate = self.candidate(self.find(), oid)
            total = self.value_feature(candidate, "row.totalAmountIncludingTax")
            self.assertEqual((total["feature"], total["agrees"]), ("value_not_comparable", None))
            self.assertIs(self.value_feature(candidate, "row.subtotal")["agrees"], True)
            self.assertNotIn("value_differs", self.features(candidate))

    def test_values_without_lineage_authority_are_not_comparable_never_different(self):
        # The fixture's Portal order is gold native staging: never authority.
        self.bc_records[CO_A]["salesOrders"][0]["orderDate"] = "2026-09-21"
        portal = self.candidate(self.find(), self.PORTAL)
        date = self.value_feature(portal, "row.order_date")
        self.assertEqual((date["feature"], date["agrees"]), ("value_not_comparable", None))
        self.assertNotIn("value_differs", self.features(portal))
        self.assertEqual(ot.corroboration_verdict(portal), "unverifiable")

    def test_an_absent_or_unreadable_counterpart_is_unverifiable_never_a_conflict(self):
        self.bc_records[CO_A]["salesOrders"] = []
        absent = self.find()
        self.failures[CO_A] = 99
        self.bc_records[CO_A]["salesOrders"] = [_bc(
            "aaaaaaaa-0000-0000-0000-000000000001", number="SO100201",
            totalAmountIncludingTax=9999)]
        unreadable = self.find()
        for out in (absent, unreadable):
            self.assertEqual(out["status"], "found", out)
            (portal,) = out["candidates"]
            self.assertEqual(ot.corroboration_verdict(portal), "unverifiable")
            features = self.features(portal)
            self.assertIsNone(features["corroboration"]["agrees"])
            self.assertIn("no_visible_counterpart", features)
            self.assertNotIn("citations", features["corroboration"])

    def test_a_counterpart_the_reader_may_not_read_leaks_nothing(self):
        no_bc = ot.ReadPolicy("plx", audiences={"api:*": {"staff:sales"}})
        withheld = self.find(policy=no_bc)
        self.assertFalse(any("companies(" in url for url in self.bc_urls))
        self.bc_records[CO_A]["salesOrders"] = []
        absent = self.find()
        self.assertEqual(withheld["status"], "found")
        (portal,) = withheld["candidates"]
        self.assertEqual(ot.corroboration_verdict(portal), "unverifiable")
        text = json.dumps(withheld)
        for leaked in ("bc://", "aaaaaaaa", "bc.sales_orders", "totalAmountIncludingTax"):
            self.assertNotIn(leaked, text)
        # The withheld answer's candidate equals the answer where BC holds no record.
        self.assertEqual(withheld["candidates"], absent["candidates"])

    def test_a_bc_invoice_customer_is_compared_with_the_orders_customer_through_its_fk(self):
        self.bc_records[CO_A]["salesInvoices"] = [_bc(
            "bbbbbbbb-0000-0000-0000-000000000001", number="PSI-9", customerNumber="c0001",
            externalDocumentNumber="SO100305", postingDate="2026-09-21")]
        args = {"identifier_kind": "bc_external_document_no"}
        portal_id = 'SalesOrder:portal.app_sales_order:{"id":"so-2"}'
        portal = self.candidate(self.find("SO100305", **args), portal_id)
        features = self.features(portal)
        self.assertEqual(ot.corroboration_verdict(portal), "agree")
        customer = features["corroboration_customer_number"]
        self.assertEqual([c["field_path"] for c in customer["citations"]],
                         ["row.customerNumber", "row.erp_code"])
        self.bc_records[CO_A]["salesInvoices"][0]["customerNumber"] = "C0002"
        conflict = self.candidate(self.find("SO100305", **args), portal_id)
        self.assertEqual(ot.corroboration_verdict(conflict), "conflict")
        # A reader who may not read customers cannot compare: unverifiable, and the
        # customer record is neither read nor cited.
        before = len(self.fake.row_requests("public.app_customer"))
        policy = ot.ReadPolicy("plx", audiences={
            "api:*": {"staff:sales"}, "api:public.app_customer": set(),
            "bc:*": {"staff:sales"}})
        hidden = self.candidate(self.find("SO100305", policy=policy, **args), portal_id)
        self.assertEqual(ot.corroboration_verdict(hidden), "unverifiable")
        self.assertEqual(len(self.fake.row_requests("public.app_customer")), before)
        self.assertNotIn("app_customer", json.dumps(hidden))

    def test_a_shipment_code_reaches_the_order_it_ships_through_the_declared_link(self):
        out = self.find("SH100002")
        self.assertEqual(out["status"], "found", out)
        (candidate,) = out["candidates"]
        self.assertEqual(candidate["object"]["object_id"],
                         'SalesOrder:portal.app_sales_order:{"id":"so-2"}')
        self.assertEqual(candidate["reason"], ot.LINKED_REASON)
        self.assertEqual((candidate["matched"]["binding"], candidate["matched"]["property"]),
                         ("portal.app_shipment", "shipment_number"))
        link = self.features(candidate)["linked_record"]
        self.assertIs(link["agrees"], True)
        self.assertEqual(sorted(c["field_path"] for c in link["citations"]),
                         ["row.id", "row.sales_order_id"])
        # Asked as a shipment, it stays the shipment.
        shipment = self.tools().call("plx.find", {"query": "SH100002",
                                                  "object_type": "Shipment"}, SALES)
        self.assertEqual(self.ids(shipment), ['Shipment:portal.app_shipment:{"id":"sh-1"}'])

    def test_a_link_the_reader_may_not_follow_gives_no_candidate(self):
        policy = ot.ReadPolicy("plx", audiences={
            "api:*": {"staff:sales"}, "api:public.app_shipment": set(), "bc:*": {"staff:sales"}})
        out = self.find("SH100002", policy=policy)
        self.assertEqual(out["status"], "not_found")
        self.assertNotIn("so-2", json.dumps(out))

    def test_comparisons_come_from_the_schema_only(self):
        self.assertEqual(resolve.shared_properties(self.view, "bc.sales_orders",
                                                   "portal.app_sales_order"),
                         [("order_date", "date"), ("subtotal", "money"),
                          ("total_amount", "money")])
        self.assertTrue(resolve.comparable(self.view, "bc.sales_invoices",
                                           "portal.app_sales_order"))
        # Two bindings of one source family are not cross-system counterparts.
        self.assertFalse(resolve.comparable(self.view, "portal.sage_oe_order_header",
                                            "portal.app_sales_order"))
        self.assertEqual(resolve.link_paths(self.view, "portal.app_shipment",
                                            "portal.app_sales_order"),
                         [("Shipment.ships", "sales_order_id", "id")])
        # A link read from its "one" side names many records: never followed.
        self.assertEqual(resolve.link_paths(self.view, "portal.app_sales_order",
                                            "portal.app_shipment"), [])

    def test_the_verdict_rule(self):
        def check(verdict, basis="linked_identifier"):
            return resolve.Check("p", basis, verdict, (), (), "x")
        self.assertEqual(resolve.verdict([]), "unverifiable")
        self.assertEqual(resolve.verdict([check("agree"), check("unverifiable")]), "agree")
        self.assertEqual(resolve.verdict([check("agree"), check("conflict")]), "conflict")
        # A shared value never decides identity, whatever it says.
        self.assertEqual(resolve.verdict([check("agree"),
                                          check("differs", "shared_property")]), "agree")
        self.assertEqual(resolve.verdict([check("differs", "shared_property")]),
                         "unverifiable")
        value = resolve.CitedValue("10.0", "api://x", "row.a", "d" * 64, "gold_etl_from_fm",
                                   "CAD", tax_basis="excl_tax")
        other = resolve.CitedValue("10", "bc://y", "row.b", "e" * 64, "bc_manufacturing",
                                   "USD", tax_basis="excl_tax")
        more = resolve.CitedValue("12", "bc://y", "row.b", "e" * 64, "bc_manufacturing",
                                  "CAD", tax_basis="excl_tax")
        self.assertEqual(resolve.compare_shared("t", "money", [value], [value]).verdict,
                         "agree")
        self.assertEqual(resolve.compare_shared("t", "money", [value], [more]).verdict,
                         "differs")
        # Amounts in different (or undeclared) currencies are not comparable.
        for right in (other, resolve.CitedValue("12", "bc://y", "row.b", "e" * 64,
                                                "bc_manufacturing")):
            refused = resolve.compare_shared("t", "money", [value], [right])
            self.assertEqual((refused.verdict, refused.why),
                             ("unverifiable", "currency_not_comparable"))
        kg = resolve.CitedValue("5", "api://x", "row.q", "d" * 64, "gold_etl_from_fm", unit="kg")
        ea = resolve.CitedValue("5", "bc://y", "row.r", "e" * 64, "bc_manufacturing", unit="ea")
        self.assertEqual(resolve.compare_shared("q", "quantity", [kg], [ea]).why,
                         "unit_not_comparable")
        kgs = resolve.CitedValue("5", "bc://y", "row.r", "e" * 64, "bc_manufacturing", unit="KGS")
        self.assertEqual(resolve.compare_shared("q", "quantity", [kg], [kgs]).verdict, "agree")
        # Money on two tax bases, or an unknown basis, is never compared (as plx.compare).
        incl = resolve.CitedValue("113", "bc://y", "row.t", "e" * 64, "bc_manufacturing",
                                  "CAD", tax_basis="incl_tax", binding="bc.sales_orders")
        excl = resolve.CitedValue("100", "api://x", "row.t", "d" * 64, "gold_etl_from_fm",
                                  "CAD", tax_basis="excl_tax", binding="portal.app_sales_order")
        unknown = resolve.CitedValue("100", "api://x", "row.t", "d" * 64, "gold_etl_from_fm",
                                     "CAD", tax_basis="unknown", binding="portal.sage_po_header")
        for left, right in ((incl, excl), (incl, unknown), (excl, unknown)):
            refused = resolve.compare_shared("total_amount", "money", [left], [right])
            self.assertEqual((refused.verdict, refused.why),
                             ("unverifiable", "tax_basis_not_comparable"))
        same = resolve.CitedValue("113.00", "api://x", "row.t", "d" * 64, "gold_etl_from_fm",
                                  "CAD", tax_basis="incl_tax", binding="portal.x")
        self.assertEqual(resolve.compare_shared("t", "money", [incl], [same]).verdict, "agree")
        self.assertEqual(resolve.compare_shared("t", "money", [], [other]).why,
                         "no_value_on_counterpart")

    def test_unit_prices_compare_only_per_one_known_denominator(self):
        def price(value, unit, *, per_unit=True, handle="api://x"):
            return resolve.CitedValue(value, handle, "row.p", "d" * 64, "gold_etl_from_fm",
                                      "CAD", unit, "excl_tax", "portal.x", per_unit)
        per_ea, per_kg, per_unknown = price("10", "ea"), price("10", "kg"), price("10", None)
        for right in (per_kg, per_unknown, price("10", None, per_unit=False)):
            refused = resolve.compare_shared("unit_price", "money", [per_ea], [right])
            self.assertEqual((refused.verdict, refused.why),
                             ("unverifiable", "price_denominator_not_comparable"))
        self.assertEqual(resolve.compare_shared("unit_price", "money", [per_ea],
                                                [price("10.00", "EA")]).verdict, "agree")
        self.assertEqual(resolve.compare_shared("unit_price", "money", [per_ea],
                                                [price("12", "each")]).verdict, "differs")



class DayPrefixLeadTests(ResolveTestCase):
    """grammar-2: an AO number typed without its letter (``AO-{YY}{day of year}``) is a
    lead to that day's AOs, in both spellings of the day (FM pads the day, the Portal
    does not), read by prefix only where the reader may read; never one AO."""

    AOS = [(1, "AO-26042A"), (2, "AO-26042B"), (3, "AO-2642C"), (4, "AO-26043A"),
           (5, "AO-264A"), (6, "AO-260421A")]

    def setUp(self) -> None:
        super().setUp()
        self.fake.catalog["public.assembly_order"] = _cat(["id", "nameamo_c", "updated_at"])
        self.fake.catalog["public.app_manufacturing_order"] = _cat(["id", "mo_code",
                                                                    "updated_at"])
        self.fake.tables["public.assembly_order"] = [
            {"id": ident, "nameamo_c": code, "updated_at": "2026-09-28T08:00:00Z"}
            for ident, code in self.AOS]
        self.fake.tables["public.app_manufacturing_order"] = [
            {"id": "mo-1", "mo_code": "AO-26042B", "updated_at": "2026-09-28T08:00:00Z"},
            {"id": "mo-2", "mo_code": "MO-LX8K2Z", "updated_at": "2026-09-28T08:00:00Z"}]
        self.resolutions = []

    def spy(self, tools):
        real = self.resolver.resolve

        def resolve_and_keep(*args, **kwargs):
            resolution = real(*args, **kwargs)
            self.resolutions.append(resolution)
            return resolution
        self.resolver.resolve = resolve_and_keep
        return tools

    def test_a_day_prefix_reads_both_spellings_and_ranks_leads_never_a_winner(self):
        tools = self.spy(self.tools())
        out = tools.call("plx.find", {"query": "AO-26042", "object_type": "AssemblyOrder"},
                         SALES)
        self.assertEqual(out["status"], "ambiguous", out)
        self.assertGreaterEqual(len(out["candidates"]), 2)
        for candidate in out["candidates"]:
            self.assertEqual(candidate["tier"], "lead")
            self.assertNotIn(candidate["reason"], ("exact_identifier", "alias_match"))
        self.assertEqual([c["rank"] for c in out["candidates"]],
                         list(range(1, len(out["candidates"]) + 1)))
        (resolution,) = self.resolutions
        leads = [(lead["value"], lead["binding"]) for lead in resolution.leads]
        # The spelling as typed first, then the Portal's unpadded day; never another
        # day (AO-26043A, AO-264A = day 4) nor a digit after the day (AO-260421A).
        self.assertEqual(leads, [("AO-26042A", "portal.fm_assembly_order"),
                                 ("AO-26042B", "portal.app_manufacturing_order"),
                                 ("AO-26042B", "portal.fm_assembly_order"),
                                 ("AO-2642C", "portal.fm_assembly_order")])
        for lead in resolution.leads:
            self.assertEqual((lead["kind"], lead["form"], lead["tier"], lead["exact"]),
                             ("ao_code", "day_prefix", "lead", False))
            self.assertTrue(lead["citation"].startswith("api://"))

    def test_the_only_ao_of_a_day_is_still_a_lead_never_an_exact_match(self):
        tools = self.spy(self.tools())
        out = tools.call("plx.find", {"query": "AO-26043", "object_type": "AssemblyOrder"},
                         SALES)
        (candidate,) = out["candidates"]
        self.assertEqual((candidate["tier"], candidate["matched"]["raw"]["untrusted_text"]),
                         ("lead", "AO-26043A"))
        self.assertNotIn(candidate["reason"], ("exact_identifier", "alias_match"))
        self.assertEqual([lead["value"] for lead in self.resolutions[0].leads], ["AO-26043A"])

    DAY_42 = {"AO-26042A", "AO-26042B", "AO-2642C"}

    def lead_values(self, out):
        return [c["matched"]["raw"]["untrusted_text"] for c in out["candidates"]]

    def assert_day_42_leads(self, out):
        self.assertEqual(out["status"], "ambiguous", out)
        # Every AO of the day, in both spellings of the day; one candidate per record.
        self.assertEqual(set(self.lead_values(out)), self.DAY_42)
        self.assertEqual(len(out["candidates"]), 4)
        for candidate in out["candidates"]:
            self.assertEqual((candidate["tier"], candidate["reason"]), ("lead", "prefix_lead"))
            self.assertIn({"feature": "prefix_form", "agrees": None}, candidate["why_not"])
        self.assertEqual(out["completeness"]["total"], 4)

    def test_find_m1_the_unpadded_day_is_every_ao_of_the_day_never_one_found(self):
        """Review grammar-2 m1: ``AO-2642`` answered ``found`` with the one AO whose
        display text holds the typed spelling, on a day with three AOs."""
        tools = self.tools()
        for query in ("AO-2642", "AO-26042", " ao-2642 "):
            with self.subTest(query=query):
                self.assert_day_42_leads(tools.call(
                    "plx.find", {"query": query, "object_type": "AssemblyOrder"}, SALES))

    def test_the_projection_store_answers_a_day_prefix_with_the_same_leads(self):
        from ontology.project import CapturedRecord

        def record(relation, ident, code, column):
            key = json.dumps({"id": ident}, separators=(",", ":"))
            return CapturedRecord(
                "portal_api", relation, key,
                {"id": ident, column: code, "updated_at": "2026-09-28T08:00:00Z"},
                f"api://{relation}/{ident}@1", "row", "d" * 64, "2026-09-28T08:00:00.000Z",
                None, "2026-09-28T08:00:00.000Z", f"api:{relation}")

        records = {"public.assembly_order": [record("public.assembly_order", i, code,
                                                    "nameamo_c") for i, code in self.AOS],
                   "public.app_manufacturing_order": [record(
                       "public.app_manufacturing_order", "mo-1", "AO-26042B", "mo_code")]}

        class Source:
            source = "portal_api"

            def records(self, relations):
                return {r: list(records.get(r, ())) for r in relations}

            def describe(self):
                return {"generation": "g", "relations": {}}

        tools = self.tools(store_rows={"portal_api": Source()})
        self.assert_day_42_leads(tools.call("plx.find", {"query": "AO-2642"}, SALES))
        self.assertEqual(self.fake.row_requests(), [])  # answered from the projection
        # A reader who may not read the relations gets no lead and no count of them.
        denied = self.tools(store_rows={"portal_api": Source()}, policy=ot.ReadPolicy(
            "plx", audiences={"api:public.app_sales_order": {"staff:sales"}}))
        out = denied.call("plx.find", {"query": "AO-2642"}, SALES)
        self.assertEqual((out["status"], out["candidates"]), ("not_found", []))

    def test_a_reader_without_the_relation_reads_no_prefix_and_gets_no_lead(self):
        self.tools()
        resolution = self.resolver.resolve(
            SALES, [("so_number", "AO-26042")], "AO-26042", object_type="AssemblyOrder",
            may_read=lambda key: key == "api:public.app_sales_order")
        self.addCleanup(resolution.discard)
        self.assertEqual(resolution.leads, ())
        self.assertIsNone(resolution.store_path)
        self.assertEqual(resolution.counts["withheld"], 2)
        self.assertFalse(resolution.complete)


def _captured(relation, key, row):
    from ontology.project import CapturedRecord
    text = json.dumps(key, separators=(",", ":"), sort_keys=True)
    return CapturedRecord("portal_api", relation, text,
                          {**row, **key, "updated_at": "2026-09-28T08:00:00Z"},
                          f"api://{relation}/{'-'.join(map(str, key.values()))}@1", "row",
                          "d" * 64, "2026-09-28T08:00:00.000Z", None,
                          "2026-09-28T08:00:00.000Z", f"api:{relation}")


class _Captures:
    source = "portal_api"

    def __init__(self, records):
        self._records = records

    def records(self, relations):
        return {r: [x for x in self._records if x.relation == r] for r in relations}

    def describe(self):
        return {"generation": "g", "relations": {}}


class SameNumberTwinFindTests(ResolveTestCase):
    """plx.find with a typed PO number's same-number twins (``SameNumberIndex``): the
    exact match decides the status; each twin rides on it as a related lead, verified
    only when a same_record link (the stored FM id) joins the two records."""

    def setUp(self) -> None:
        super().setUp()
        self.store = _Captures([
            _captured("public.app_purchase_order", {"id": "po-1"},
                      {"po_number": "PO#4821", "custom_fields": {"fmGeneratedPoId": 12}}),
            _captured("sage.po_header", {"porhseq": 71}, {"ponumber": "4821"}),
            _captured("public.generated_purchase_order", {"id": 12}, {"ponumber": "04821-FM12"}),
            _captured("public.app_purchase_order", {"id": "po-2"}, {"po_number": "DPO4821"}),
            _captured("public.app_purchase_order", {"id": "po-3"}, {"po_number": "PO48210"}),
            _captured("public.app_purchase_order", {"id": "po-4"},
                      {"po_number": "UAT-PO4821"})])

    def found(self, query):
        out = self.tools(store_rows={"portal_api": self.store}).call(
            "plx.find", {"query": query, "object_type": "PurchaseOrder"}, SALES)
        related = {}
        for candidate in out["candidates"]:
            for feature in candidate.get("why_not", ()):
                if feature["feature"].startswith("related_"):
                    handle = feature["citations"][0]["l1_handle"]
                    related[handle.split("@")[0]] = (feature["feature"], feature["agrees"])
        return out, related

    def test_an_exact_po_is_found_with_its_linked_and_unlinked_twins_as_related_leads(self):
        out, related = self.found("PO 4821")
        self.assertEqual(out["status"], "found", out)
        (candidate,) = out["candidates"]
        self.assertEqual((candidate["matched"]["raw"]["untrusted_text"], candidate["reason"]),
                         ("PO#4821", "exact_identifier"))
        self.assertEqual(related, {
            "api://public.generated_purchase_order/12": ("related_same_record", True),
            "api://sage.po_header/71": ("related_same_number_twin", None)})
        self.assertEqual(out["completeness"]["total"], 1)

    def test_a_bare_number_is_found_and_names_every_series_but_never_the_uat_stamp(self):
        out, related = self.found("4821")
        self.assertEqual(out["status"], "found", out)
        self.assertIn(out["candidates"][0]["reason"], ot.ROOT_REASONS)
        self.assertEqual(out["candidates"][0]["matched"]["raw"]["untrusted_text"], "4821")
        self.assertEqual(related, {
            "api://public.app_purchase_order/po-1": ("related_same_number_twin", None),
            "api://public.app_purchase_order/po-2": ("related_same_number_twin", None),
            "api://public.generated_purchase_order/12": ("related_same_number_twin", None)})


class SameNumberIndexTests(unittest.TestCase):
    """grammar-2: the shared resolver's same-number twins (moved from the mail layer):
    one number stored in several systems names every row, never one."""

    ROWS = [("gold-1", "PO#4821", "po_number"), ("sage-1", "4821", "sage.ponumber"),
            ("fm-1", "04821-FM12", "fm.ponumber"), ("gold-2", "DPO4821", "po_number"),
            ("gold-3", "UAT-PO#4821", "po_number"), ("gold-4", "PO-K3J9X2", "po_number"),
            ("gold-5", "PO48210", "po_number"), ("gold-1", "4821", "custom_fields.fmPoNumber")]

    def setUp(self) -> None:
        self.index = resolve.SameNumberIndex("petra_po")
        for ref, value, rule in self.ROWS:
            self.index.add(ref, value, rule)

    def test_a_series_number_names_its_bare_and_suffixed_twins_in_order(self):
        self.assertEqual(self.index.twins("PO 4821"),
                         {"gold-1": ["po_number", "custom_fields.fmPoNumber"],
                          "sage-1": ["sage.ponumber"], "fm-1": ["fm.ponumber"]})

    def test_a_bare_number_names_every_series_but_never_the_uat_stamp(self):
        self.assertEqual(list(self.index.twins("4821")), ["gold-1", "sage-1", "fm-1", "gold-2"])
        self.assertEqual(list(self.index.twins("UAT-PO4821")), ["gold-3"])

    def test_values_without_a_number_have_no_twins(self):
        for value in ("PO-K3J9X2", "", None, "not a number"):
            with self.subTest(value=value):
                self.assertEqual(self.index.twins(value), {})


if __name__ == "__main__":
    unittest.main()
