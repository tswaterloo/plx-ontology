"""Employee-facing usability of plx.schema and plx.find (operating/ontology_tools.py).

* plx.schema pages with a working cursor until every type, link, event and action was
  returned exactly once;
* a plx.find filter on a property the type does not declare is ``unknown_property``
  naming the valid ones, never a not_found that reads as absence;
* a host without the Portal API configured states ``source_not_configured`` for it,
  never a ``portal_api`` freshness gap, while a configured Portal that is down still
  reports one;
* a property whose ``when`` test reads a column no property states (its markings are
  unknown) never gives a card the build's per-record outcome of the tests: no reader
  passes a marking check on it, so the card keeps the report's typed ``value_unread``.

Evidence label: ``isolated-fixture`` and ``injected-transport`` (the down Portal is a
fetch that raises; the ``when`` rows are synthetic BC rows over the curated schema). No
network, no credentials.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from integrations.source_mirror import AUDIENCE_SCHEMA
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology import identifiers
from ontology.core import Actor
from ontology.project import build_store
from ontology.schema import load_schema
from ontology.tests.test_bc_mapping import MAN, StaticCaptures, bc
from operating import ontology_tools as ot
from operating.case_runtime import CaseRuntime
from operating.ontology_host import CaseHostTools
from tests.test_ontology_tools import FULL, PO, ContractMixin, build, tools

OPS = Actor("plx", frozenset({"staff:operations"}))


class SchemaPagingTests(ContractMixin, unittest.TestCase):
    def page_through(self, t, arguments):
        seen, cursor = [], None
        for _ in range(50):
            page = self.ok(t, "plx.schema", dict(arguments, **(
                {"cursor": cursor} if cursor else {})), OPS)
            for key in ("object_types", "link_types", "event_types", "action_types"):
                seen += [(key, item["id"]) for item in page.get(key, ())]
            done = page["completeness"]
            cursor = done["next_cursor"]
            self.assertEqual(done["complete"], cursor is None)
            if cursor is None:
                return seen, done["total"]
        self.fail("plx.schema never reached its last page")

    def test_paging_reaches_every_type_once(self):
        types = {f"Type{i:03d}": {"id": f"Type{i:03d}", "description": "d" * 1500,
                                  "availability": {"state": "served"}} for i in range(279)}
        types["SalesOrder"] = {"id": "SalesOrder", "availability": {"state": "served"}}
        links = {f"Type{i:03d}.to_order": {
            "id": f"Type{i:03d}.to_order", "from": f"Type{i:03d}", "to": "SalesOrder",
            "cardinality": "N:1", "bindings": []} for i in range(250)}
        big = ot.SchemaView(object_types=types, link_types=links, digest="a" * 64)
        t = tools(schema=big)
        first = self.ok(t, "plx.schema", {}, OPS)
        self.assertLess(first["completeness"]["returned"], 280 + 250)
        self.assertIsNotNone(first["completeness"]["next_cursor"])
        seen, total = self.page_through(t, {})
        self.assertEqual(len(seen), total)
        self.assertEqual(len(set(seen)), total)
        self.assertEqual({i for k, i in seen if k == "object_types"}, set(types))
        self.assertEqual(sum(k == "link_types" for k, _ in seen), 250)
        # The real schema too.
        seen, _ = self.page_through(tools(schema=ot.default_schema()), {"section": "objects"})
        self.assertIn(("object_types", "SalesOrder"), seen)
        # A cursor of another request (or reader) fails closed.
        cursor = first["completeness"]["next_cursor"]
        for arguments, actor in (({"section": "objects", "cursor": cursor}, OPS),
                                 ({"cursor": cursor}, FULL),
                                 ({"cursor": cursor + "x"}, OPS)):
            refused = t.call("plx.schema", arguments, actor)
            self.assertContract("plx.schema", refused)
            self.assertEqual(refused["error"]["code"], "invalid_cursor", arguments)


class UnknownFilterPropertyTests(ContractMixin, unittest.TestCase):
    def test_a_filter_on_an_undeclared_property_names_the_valid_ones(self):
        t = tools()
        for arguments in ({"object_type": "PurchaseOrder",
                           "filters": [{"property": "status", "op": "eq", "value": "OPEN"}]},
                          {"query": "PO-1", "object_type": "PurchaseOrder",
                           "filters": [{"property": "status", "op": "present"}]}):
            result = t.call("plx.find", arguments, FULL)
            self.assertContract("plx.find", result)
            error = result["error"]
            self.assertEqual(error["code"], "unknown_property")
            self.assertIn("status", error["message"])
            self.assertIn("po_number", error["hint"])
        # A declared property still filters.
        found = self.ok(t, "plx.find", {"object_type": "PurchaseOrder", "filters": [
            {"property": "po_number", "op": "present"}]})
        self.assertEqual(found["status"], "found")
        self.assertIn(PO, json.dumps(found))


class PortalConfigurationTests(ContractMixin, unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.audience = self.root / "audience.json"
        self.audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                             "audiences": {"public.*": ["staff:operations"]}}))
        self.schema = ot.default_schema()
        self.store = self.root / "projection.sqlite"
        build_store(self.store, self.schema, {}, normalizer=identifiers.normalize)

    def host_tools(self, name, client):
        """The case host's tools over an empty projection store and its mirror."""
        runtime = CaseRuntime(self.root / name, client=client, audience=self.audience,
                              tenant="plx", mirror_options={"retry_delay_seconds": 0})
        self.addCleanup(runtime.close)
        host = CaseHostTools(runtime, OPS, store_path=self.store, schema=self.schema,
                             bc=False)
        self.addCleanup(host.close)
        return host

    def portal_gaps(self, t):
        out = []
        for arguments in ({"query": "SO-99999"}, {"object_type": "PurchaseOrder", "filters": [
                {"property": "po_number", "op": "eq", "value": "PO-404"}]}):
            result = self.ok(t, "plx.find", arguments, OPS)
            self.assertEqual(result["status"], "not_found")
            self.assertNotIn("absent_in_source", {g["state"] for g in result["gaps"]})
            out.append({g["reason"] for g in result["gaps"] if g["subject"] == "portal_api"})
        return out

    def test_no_portal_freshness_gap_without_portal_config_but_one_when_it_is_down(self):
        for reasons in self.portal_gaps(self.host_tools("none", None)):
            self.assertEqual(reasons, {"source_not_configured"})

        def down(url, headers, *, max_bytes):
            raise OSError("connection refused")
        client = WorkbenchReadApiClient("https://api.example.test", "plxwb_" + "m" * 40,
                                        fetch=down)
        for reasons in self.portal_gaps(self.host_tools("down", client)):
            self.assertTrue(reasons & ot.FRESHNESS_REASONS, reasons)
            self.assertNotIn("source_not_configured", reasons)

    def test_a_capture_read_from_an_unconfigured_source_keeps_its_freshness_gap(self):
        store = build()
        store.conn.execute("UPDATE store_meta SET value = ? WHERE key = 'freshness'",
                           (json.dumps({"portal_api": {"captured_at": "2099-01-01T00:00:00Z",
                                                       "completeness": "partial",
                                                       "stale": True}}),))
        result = self.ok(tools(store, unconfigured=("portal_api",)), "plx.object",
                         {"object_id": PO})
        reasons = {g["reason"] for g in result["gaps"] if g["subject"] == "portal_api"}
        self.assertEqual(reasons, {"stale_source"})


class UnstatedWhenColumnTests(ContractMixin, unittest.TestCase):
    def card_reasons(self, stated):
        """SalesInvoice.customer_po's gaps on a card of an invoice failing its ``when``,
        with a test on ``shipNote`` added (stated by a property only when ``stated``) and
        another invoice's absent path also reported there."""
        schema = copy.deepcopy(load_schema())
        invoice = schema.object_types["SalesInvoice"]
        (po,) = [p for p in invoice["properties"] if p["name"] == "customer_po"]
        (source,) = [s for s in po["sources"] if s["binding"] == "bc.sales_invoices"]
        source["when"] = [*source["when"], {"path": "shipNote", "op": "present"}]
        if stated:
            invoice["properties"].append({"name": "ship_note", "value_type": "text", "sources": [
                {"binding": "bc.sales_invoices", "path": "shipNote", "parser": "as_is"}]})
        self.assertEqual(stated, any(str(s.get("path", "")).startswith("shipNote")
                                     for p in invoice["properties"] for s in p["sources"]
                                     if s["binding"] == "bc.sales_invoices"))
        rows = {"fails": {"externalDocumentNumber": "PO-1"},
                "absent": {"shipNote": "x"},
                "passes": {"externalDocumentNumber": "PO-3", "shipNote": "x"}}
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "store.sqlite"
        report = build_store(path, schema, {"bc": StaticCaptures("bc", [
            bc("salesInvoices", MAN, {"number": f"PSI-{i}", "orderNumber": "SO100200",
                                      **row, "id": i}) for i, row in rows.items()])},
            normalizer=identifiers.normalize)
        where = "SalesInvoice.customer_po@bc.sales_invoices"
        self.assertEqual({g["code"] for g in report["gaps"] if g["where"] == where},
                         {"source_condition_unmet", "path_absent"})
        reader = ot.StoreReader.open(path)
        self.addCleanup(reader.conn.close)
        t = ot.OntologyTools(reader, ot.ReadPolicy("plx", audiences={"bc:*": {"staff:ar"}}),
                             schema=ot.SchemaView.from_compiled(schema),
                             grammar=identifiers, contracts=ot.load_contracts())
        out = {}
        for i in rows:
            card = self.ok(t, "plx.object", {
                "object_id": f'SalesInvoice:bc.sales_invoices:{{"id":"{i}"}}'},
                Actor("plx", frozenset({"staff:ar"}), "restricted"))
            values = {v["property"]: v["value"] for v in card["values"]}
            out[i] = ("customer_po" in values,
                      [g["reason"] for g in card["gaps"]
                       if g["subject"] == "SalesInvoice.customer_po"])
        return out

    def test_a_when_on_an_unstated_column_withholds_the_per_record_outcome(self):
        # Unstated: no reader may learn the column, so the build's record that this
        # invoice failed the tests is withheld; the report's mixed causes stand.
        self.assertEqual(self.card_reasons(stated=False),
                         {"fails": (False, ["value_unread"]), "absent": (False, []),
                          "passes": (True, [])})
        # Stated by an unmarked property: the reader may read it, so the card answers.
        self.assertEqual(self.card_reasons(stated=True)["fails"],
                         (False, ["source_condition_unmet"]))


if __name__ == "__main__":
    unittest.main()
