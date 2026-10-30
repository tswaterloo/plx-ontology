"""BC ontology generated from the pinned API v2.0 ``$metadata`` (``ontology/generator/bc.py``).

Evidence labels: the committed pin is the live ``$metadata`` the verifier published on
2026-10-13 (sha256 09a82605, ``live-capture``); ``isolated-fixture`` for the parse, drift and
registry probes (``FIXTURE``, aligned with that document where the schema binds it) and
``injected-transport`` for the ``$metadata`` GET and the capture, both through the real
GET-only client served by ``FakeBc`` (``integrations/tests/test_bc_capture.py``). The live
counterpart is live check ``schema.bc_metadata_drift`` (live-unverified).
"""

from __future__ import annotations

import contextlib
import dataclasses
from decimal import Decimal
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock
from urllib.parse import unquote, urlsplit

from verify import live_verify as lv
from integrations import bc_read
from integrations import live_source_grammar as grammar
from integrations.live_source_guard import ReadOnlyGuardError
from integrations.tests import test_bc_capture as tb
from ontology import schema as ontology_schema
from ontology import store_ddl
from ontology.generator import OverrideError, bc, load_overrides
from ontology.tests import test_project_bc as tp
from ontology.types import Actor
from operating import ontology_tools as ot
from scripts import source_registry as sr

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DOCUMENT = "integrations/tests/fixtures/bc_api_v2_metadata.xml"
FIXTURE = (ROOT / FIXTURE_DOCUMENT).read_text(encoding="utf-8")
SPEC = json.loads((ROOT / "devloop/live-checks/schema.bc_metadata_drift.json").read_text())
CAPTURE_SPEC = json.loads((ROOT / "devloop/live-checks/bc.capture_full.json").read_text())
MAN, DIST = tp.MAN, tp.DIST
NOT_SERVED = {"business-central.entity_set:postedSalesInvoices",
              "business-central.entity_set:salesReceivablesSetup",
              "business-central.api_page:plxCustomerShipTos",
              # read by the Portal's BC invoice probe since 6a67d48 (TASK-2270), not captured
              "business-central.entity_set:pdfDocumentContent",
              # read-only Workbench API pages the PLX BC extension adds in Portal ca27b78a
              # (TASK-2913); not captured
              "business-central.api_page:plxPostedSalesInvoices",
              "business-central.api_page:plxPostedSalesInvoiceLines",
              "business-central.api_page:plxSalesReceivablesSetups"}
ACTIONS = {"business-central.web_service_action:PLXPurchaseOrderRef_EnsureOrderManualNos",
           "business-central.web_service_action:PLXSalesBridge_EnsureSalesOrderManualNos"}
GENERATED = {n for n, e in json.loads(bc.COVERAGE_PATH.read_text())["entity_sets"].items()
             if "object_type" in e}


class MetadataSender:
    """``FakeBc`` that also answers the one ``$metadata`` GET."""

    def __init__(self, document: str = FIXTURE, fake: tb.FakeBc | None = None) -> None:
        self.document, self.fake, self.gets = document.encode(), fake or tb.FakeBc(), 0

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        if unquote(urlsplit(url).path).endswith("/api/v2.0/$metadata"):
            assert method == "GET" and body is None
            self.gets += 1
            return 200, {}, self.document
        return self.fake(method, url, headers, body, timeout, max_bytes)


def run_drift(document: str = FIXTURE) -> tuple[int, dict | None, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = bc_read.main(["metadata-drift"], environ=tb.ENV, sender=MetadataSender(document))
    return code, json.loads(out.getvalue()) if code == 0 else None, err.getvalue()


def edit_type(document: str, entity_type: str, old: str, new: str) -> str:
    """``document`` with ``old`` replaced by ``new`` inside one EntityType element."""
    block = re.search(rf'<EntityType Name="{entity_type}">.*?</EntityType>', document, re.S)
    assert block and old in block[0], (entity_type, old)
    return document[:block.start()] + block[0].replace(old, new) + document[block.end():]


def live_result(document: dict, spec: dict = SPEC) -> dict:
    """A live result document holding one check as the verifier records it."""
    detail = lv._cap({"fields": lv._recorded(spec, document)})
    return {"_from": "test", "run": "test", "generated_at": "2026-10-07T00:00:00Z",
            "checks": [{"id": spec["id"], "source": spec["source"], "result": "pass",
                        "detail": detail}]}


class GenerationTests(unittest.TestCase):
    def test_generated_files_reproduce_from_the_live_pin(self):
        for path, text in bc.build().items():
            self.assertEqual(path.read_text(encoding="utf-8"), text, path)
        pinned = bc.load_pin()
        # Honest label: the document the verifier published (the live-results branch
        # results/artifacts/schema.bc_metadata_drift/<sha256>.xml), re-pinned in review.
        self.assertEqual((pinned["evidence_mode"], pinned["document"]["path"],
                          pinned["document"]["sha256"][:16], pinned["document"]["captured_at"]),
                         ("live-capture", bc.LIVE_DOCUMENT, "09a82605df002ea2",
                          "2026-10-13T11:01:25Z"))
        # The read grammar decides what is pinned in detail: never employees.
        self.assertIn("employees", pinned["entity_set_names"])
        self.assertNotIn("employees", pinned["entity_sets"])

    def test_curated_bindings_win_over_generated_ones_on_the_same_set(self):
        docs = [(n, d) for n, d in ontology_schema.load_documents()
                if not n.startswith("generated/")]
        registry = sr.load_registry()
        curated = {"format": ontology_schema.FORMAT, "domain": "probe_locations",
                   "origin": "curated", "description": "probe", "object_types": [{
                       "id": "ProbeLocation", "description": "probe",
                       "availability": {"state": "served"},
                       "bindings": [{"id": "bc.probe_locations", "source": "bc",
                                     "relation": "locations", "key": ["id"],
                                     "lineage": {"from_capture": "bc_company"},
                                     "evidence": [{"kind": "workbench_code", "path": "x.py"}]}],
                       "properties": [{"name": "code", "value_type": "text", "sources": [
                           {"binding": "bc.probe_locations", "path": "code",
                            "parser": "as_is"}]}],
                       "evidence": [{"kind": "workbench_code", "path": "x.py"}]}]}
        out, coverage = bc.generate(bc.load_pin(), docs + [("domains/probe_locations.json",
                                                            curated)], registry)
        types = {t["id"] for d in out.values() for t in d["object_types"]}
        links = {l["id"]: l for d in out.values() for l in d["link_types"]}
        self.assertNotIn("RelBcLocations", types)
        self.assertEqual(coverage["entity_sets"]["locations"]["bound_by"]["binding"],
                         "bc.probe_locations")
        # A generated set's navigation binds to the curated binding; curated to curated
        # (SalesOrderLine -> location) is left to the curated files.
        self.assertEqual((links["RelBcSalesQuoteLines.location"]["to"],
                          links["RelBcSalesQuoteLines.location"]["bindings"][0]["to_binding"]),
                         ("ProbeLocation", "bc.probe_locations"))
        self.assertNotIn("SalesOrderLine.location", links)
        # Without regeneration the loader shadows the generated type on that set.
        compiled = ontology_schema.compile_documents(
            ontology_schema.load_documents() + [("domains/probe_locations.json", curated)],
            check_format=False)
        self.assertIn("RelBcLocations", compiled.shadowed)
        self.assertNotIn("RelBcLocations", compiled.object_types)
        self.assertNotIn("SalesOrderLine.location", compiled.link_types)
        self.assertEqual(compiled.bindings["bc.probe_locations"][0], "ProbeLocation")

    def test_a_bc_money_declaration_the_source_does_not_serve_fails_generation(self):
        overrides = load_overrides()
        money = {"salesQuotes": {"totalAmountInclTax": {"measure": "amount",
                                                        "tax_basis": "incl_tax",
                                                        "currency_from": "currencyCode"}}}
        typo = dataclasses.replace(overrides, source_money={"bc": money})
        docs = [(n, d) for n, d in ontology_schema.load_documents()
                if not n.startswith("generated/")]
        with self.assertRaisesRegex(OverrideError, "salesQuotes.totalAmountInclTax"):
            bc.generate(bc.load_pin(), docs, sr.load_registry(), typo)

    def test_a_document_outside_the_csdl_shape_fails_closed(self):
        top = '<EntitySet Name="companies" EntityType="Microsoft.NAV.company" />'
        locations = '<EntitySet Name="locations" EntityType="Microsoft.NAV.location" />'
        assert FIXTURE.count(top) == FIXTURE.count(locations) == 1
        for document, code in (
                ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]>' + FIXTURE.split("?>", 1)[1],
                 "doctype"),
                (FIXTURE.replace('Name="companies" EntityType', 'Name="companyList" EntityType'),
                 "no_companies"),
                (edit_type(FIXTURE, "location", 'Name="code" Type="Edm.String"',
                           'Name="code" Type="Edm.Untyped"'), "unknown_property_type"),
                (edit_type(FIXTURE, "location", '<Key><PropertyRef Name="id" /></Key>', ""),
                 "invalid_key_or_property"),
                # A container-level set naming a company collection with another type,
                # one name declared twice in the container, a line collection of another
                # type than the container-level set of its name.
                (FIXTURE.replace(locations, locations.replace("location\"", "item\"")),
                 "duplicate_entity_set"),
                (FIXTURE.replace(top, top + locations), "duplicate_entity_set"),
                (FIXTURE.replace('"customerPayments" EntityType="Microsoft.NAV.customerPayment"',
                                 '"customerPayments" EntityType="Microsoft.NAV.item"'),
                 "line_set_entity_type"),
                ("<not-csdl/>", "not_csdl")):
            with self.assertRaises(bc.MetadataError) as raised:
                bc.parse_csdl(document.encode())
            self.assertEqual(raised.exception.code, code)
        # Live BC lists every company collection (and the journals' payment lines) again at
        # container level: each is that collection, one entity set, scope as contained.
        container = re.search(r"<EntityContainer.*?</EntityContainer>", FIXTURE, re.S)[0]
        bare = FIXTURE.replace(container, f'<EntityContainer Name="default">{top}'
                                          "</EntityContainer>")
        parsed = bc.parse_csdl(FIXTURE.encode())["entity_sets"]
        self.assertEqual(parsed, bc.parse_csdl(bare.encode())["entity_sets"])
        self.assertEqual([(parsed[n]["scope"], parsed[n]["parents"]) for n in
                          ("locations", "customerPayments", "companies")],
                         [("company", []), ("lines", ["customerPaymentJournals"]),
                          ("environment", [])])
        # The live check publishes the class (error_code), never the message.
        code, report, err = run_drift("<edmx:Edmx")
        self.assertEqual((code, report), (1, None))
        self.assertEqual({k: v for k, v in json.loads(err).items() if k != "message"},
                         {"error": "MetadataError", "error_code": "not_xml"})
        self.assertEqual(lv._stderr_error(err)["error_code"], "not_xml")

    def test_a_property_the_core_vocabulary_makes_read_only_is_pinned_computed(self):
        core = "Org.OData.Core.V1."
        permissions = (f'<Annotation Term="{core}Permissions"><Collection><EnumMember>'
                       f'{core}Permission/{{}}</EnumMember></Collection></Annotation>')
        document = edit_type(FIXTURE, "location", '<Property Name="code" Type="Edm.String" />',
                             '<Property Name="code" Type="Edm.String">'
                             + permissions.format("Read") + '</Property>')
        document = edit_type(document, "location", '<Property Name="city" Type="Edm.String" />',
                             '<Property Name="city" Type="Edm.String">'
                             + permissions.format("ReadWrite") + '</Property>')
        document = edit_type(document, "location", '<Property Name="state" Type="Edm.String" />',
                             '<Property Name="state" Type="Edm.String"><Annotation Term="'
                             f'{core}Computed" Bool="false" /></Property>')
        document = document.replace("  </Schema>", '   <Annotations Target="Microsoft.NAV.'
                                    'location/country"><Annotation Term="Core.Computed" />'
                                    "</Annotations>\n  </Schema>", 1)
        computed = {p["name"] for p in bc.parse_csdl(document.encode())["entity_sets"][
            "locations"]["properties"] if p.get("computed")}
        self.assertEqual(computed, {"code", "country"})

    def test_only_the_metadata_operation_is_read_as_a_document(self):
        sender = MetadataSender()
        api, transport, tenant, environment = bc_read.build_bc(tb.ENV, sender=sender)
        where = {"tenant": tenant, "environment": environment}
        metadata = grammar.url("bc.metadata", **where)
        base = metadata[:-len("/$metadata")]
        for wrong in (  # one per wrong-path class
                grammar.url("bc.companies", **where),                       # a BC record set
                grammar.url("bc.entity", company=tb.CO[0], entity="items",  # a BC record
                            record=tb.CO[1], **where),
                grammar.url("graph.user_chats", user=tb.CO[0]),             # another source
                metadata + "?$format=json",                                 # a query on it
                base + "/$batch",                                           # outside the grammar
                base.replace("/api/v2.0", "/ODataV4") + "/$metadata",       # another BC root
                base + "/%2e%2e/$metadata",                                 # an encoded segment
                metadata.replace("https://", "http://"),                    # not https
                metadata.replace("api.businesscentral.dynamics.com", "example.com")):
            with self.assertRaises(ReadOnlyGuardError, msg=wrong):
                api.get_document(wrong)
        self.assertEqual(transport.requests_sent, 0)   # refused before any I/O
        self.assertTrue(api.get_document(metadata).startswith(b"<?xml"))
        self.assertEqual(sender.gets, 1)


class DriftAndRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        code, cls.report, _ = run_drift()
        assert code == 0

    def test_the_drift_result_meets_the_spec_only_from_a_live_read(self):
        report = self.report
        bound = {b["relation"] for _, b in ontology_schema.load_schema().bindings.values()
                 if b["source"] == "bc"}
        self.assertEqual(set(report["bound_entity_sets"]), bound)
        self.assertEqual((report["ok"], report["evidence_label"], report["missing_column_count"]),
                         (True, "injected-transport", 0))
        self.assertEqual(report["custom_api_pages_in_metadata"], [])
        # An injected transport never passes as live; the same output read live passes.
        self.assertEqual(lv.check_expectations(SPEC, 0, report), ["evidence_label != expected"])
        self.assertEqual(lv.check_expectations(SPEC, 0, dict(report, evidence_label="live-read")),
                         [])
        fields = live_result(report)["checks"][0]["detail"]["fields"]
        self.assertNotIn("fields_truncated", live_result(report)["checks"][0]["detail"])
        self.assertEqual(set(fields["bound_entity_sets"]), bound)

    def test_a_changed_document_names_what_it_breaks(self):
        document = edit_type(FIXTURE, "location", '<Property Name="code" Type="Edm.String" />', "")
        document = document.replace(
            '<NavigationProperty Name="salesQuotes" Type="Collection(Microsoft.NAV.salesQuote)" '
            'ContainsTarget="true" />',
            '<NavigationProperty Name="postedSalesInvoices" '
            'Type="Collection(Microsoft.NAV.salesInvoice)" ContainsTarget="true" />').replace(
            '<EntitySet Name="salesQuotes" EntityType="Microsoft.NAV.salesQuote" />', "")
        # BC starts serving a column the schema declares not_served, and marks a bound
        # column read-only (the twin would still accept a write to it).
        document = edit_type(document, "purchaseInvoice", "</Key>",
                             '</Key><Property Name="remainingAmount" Type="Edm.Decimal" />')
        document = edit_type(document, "purchaseOrder", '<Property Name="number" Type="Edm.String" />',
                             '<Property Name="number" Type="Edm.String"><Annotation '
                             'Term="Org.OData.Core.V1.Computed" /></Property>')
        code, report, _ = run_drift(document)
        self.assertEqual(code, 0)
        self.assertEqual((report["missing_entity_sets"], report["now_served"],
                          report["computed_changed"], report["ok"]),
                         (["salesQuotes"], ["postedSalesInvoices", "purchaseInvoices.remainingAmount"],
                          ["purchaseOrders.number"], False))
        self.assertIn("locations.code", report["missing_columns"])
        self.assertEqual(set(lv.check_expectations(SPEC, 0, dict(report, evidence_label="live-read"))),
                         {"missing_entity_set_count != expected", "missing_column_count != expected",
                          "now_served_count != expected", "computed_changed_count != expected",
                          "ok != expected"})

    def test_a_new_decimal_column_of_a_generated_set_is_drift_until_declared(self):
        decimal = '<Property Name="probeAmount" Type="Edm.Decimal" />'
        text = '<Property Name="probeNote" Type="Edm.String" />'
        code, report, _ = run_drift(edit_type(FIXTURE, "location", "</Key>", "</Key>" + text))
        self.assertEqual((report["unbound_columns"], report["undeclared_decimal_columns"],
                          report["ok"]), (["locations.probeNote"], [], True))
        live = bc.pin(edit_type(FIXTURE, "location", "</Key>", "</Key>" + decimal).encode(),
                      evidence_mode="live-capture", document=bc.LIVE_DOCUMENT)
        args = (live, bc.load_pin(), ontology_schema.load_schema(),
                json.loads(bc.COVERAGE_PATH.read_text()), sr.load_registry())
        report = dict(bc.drift(*args), evidence_label="live-read", requests_sent=2)
        self.assertEqual((report["undeclared_decimal_columns"], report["ok"]),
                         (["locations.probeAmount"], False))
        self.assertEqual(set(lv.check_expectations(SPEC, 0, report)),
                         {"undeclared_decimal_column_count != expected", "ok != expected"})
        # Declared in the money overrides, it is money on regeneration: no drift.
        overrides = load_overrides()
        money = {"locations": {"probeAmount": {"measure": "amount", "tax_basis": "unknown",
                                               "currency_from": "code"}}}
        declared = dataclasses.replace(overrides, source_money={
            "bc": {**overrides.source_money["bc"], **money}})
        self.assertTrue(bc.drift(*args, overrides=declared)["ok"])

    def test_every_bc_entry_is_evaluated_from_a_pass_except_coded_not_served_and_actions(self):
        live = dict(self.report, evidence_label="live-read")
        facts = sr.RepoFacts()
        doc = sr.gaps(None, [live_result(live)], facts)
        rows = {r["id"]: r for r in doc["uncovered"] if r["system"] == "business-central"}
        # A write action bound by its action type is terminal: verified per call by
        # read-back, never captured, never a gap (gap-799cbc8aaa44, gap-4e9b0fd4c236 gone).
        self.assertEqual(set(rows), NOT_SERVED)
        statuses = {r["id"]: r["status"] for r in sr.coverage(None, None, facts)}
        self.assertEqual({statuses[i] for i in ACTIONS}, {"bound_action"})
        self.assertEqual(doc["counts"]["by_system"]["business-central"]["bound_action"],
                         len(ACTIONS))
        self.assertEqual({rows[i]["reason"] for i in NOT_SERVED}, {"not_served"})
        # Each not_served set or page waits on its own ask, never the Portal manifest's Q3.
        compiled = ontology_schema.load_schema()
        asks = {e["upstream_name"]: compiled.object_types[e["object_type"]]["availability"]
                ["upstream_ask"] for e in json.loads(bc.COVERAGE_PATH.read_text())
                ["not_served"].values()}
        self.assertEqual(asks, {"pdfDocumentContent": "bc_api_v2:pdfDocumentContent",
                                "postedSalesInvoices": "bc_api_v2:postedSalesInvoices",
                                "salesReceivablesSetup": "bc_api_v2:salesReceivablesSetup",
                                "plxCustomerShipTos": "workbench_grammar:plxCustomerShipTos",
                                **{name: "workbench_grammar:" + name for name in (
                                    "plxPostedSalesInvoices", "plxPostedSalesInvoiceLines",
                                    "plxSalesReceivablesSetups")}})
        entity_sets = [e for e in sr.load_registry()["entries"]
                       if e["system"] == "business-central" and e["kind"] == "entity_set"]
        # Portal 38cd3f3 adds salesCreditMemos/salesCreditMemoLines (7d9bbdc) and pdfDocumentContent;
        # 71f7214 adds accounts and vendors (lib/business-central/ap-misc.ts, TASK-805 misc AP bills).
        self.assertEqual(len(entity_sets), 31)
        self.assertEqual(doc["counts"]["by_system"]["business-central"]["evaluated"],
                         len(entity_sets) - 3 + 1)   # the three not_served entity sets above;
        # + the salesInvoiceLines api_page the Portal reads at the 0323e19c pin, evaluated
        self.assertEqual(statuses["business-central.api_page:salesInvoiceLines"],
                         "fixture_queryable")   # without live results; the pass evaluates it
        self.assertNotIn("business-central.api_page:salesInvoiceLines", rows)
        self.assertFalse([b for b in doc["bound_without_entry"]
                          if b["source"] == "bc" and b.get("registry_entry")])

    def test_the_full_capture_check_names_every_generated_set_whatever_the_budget_cuts(self):
        """Live 2026-10-08: the budget stopped bc.capture_full in its first company before
        any generated set, so no check named them (gap-4a587705a12d). The check's own
        command, against a BC whose order documents outlast both passes' budgets."""
        fake = tb.FakeBc({"purchaseOrders": 200, "salesOrders": 200})
        real = bc_read.capture_probe

        def run(argv: list[str]) -> dict:
            with tempfile.TemporaryDirectory() as tmp:
                out = io.StringIO()
                # Four budget seconds per request; the test company map.
                clock = lambda: 4.0 * len(fake.calls)  # noqa: E731
                with mock.patch.object(bc_read, "capture_probe", lambda *a, **k: real(
                        *a, **dict(k, monotonic=clock, books=tb.BOOKS))), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(bc_read.main(
                        [a.replace("{tmp}", tmp).replace("{session_state}", tmp) for a in argv],
                        environ=tb.ENV, sender=fake), 0)
            return json.loads(out.getvalue())

        command = CAPTURE_SPEC["command"][3:]
        report = run(command)
        self.assertEqual(report["stopped_reason"], "budget")
        # The output still meets the spec's contract: only the test's unpinned company, the cut
        # and the production pin fail it (units_total counts planned units, units_reached fewer;
        # this fixture captures several test companies, not the one pinned My Company, so the
        # one-company proof is absent and companies_captured is not 1), never an unknown field.
        self.assertEqual(set(lv.check_expectations(CAPTURE_SPEC, 0, dict(
            report, evidence_label="live-read"))),
            {"unpinned_companies != expected", "units_reached != units_total",
             "companies_captured != expected", "missing company_proof.environment",
             "missing company_proof.company_name", "missing company_proof.po_count"})
        self.assertIsNone(report["company_proof"])
        index = sr.LiveIndex([live_result(report, CAPTURE_SPEC)])
        self.assertEqual({n for n in GENERATED if not index.naming("bc", {n})}, set())
        # Without the flag the same budget reaches none of them.
        report = run([a for a in command if a != "--generated-first"])
        index = sr.LiveIndex([live_result(report, CAPTURE_SPEC)])
        self.assertEqual({n for n in GENERATED if index.naming("bc", {n})}, set())

    def test_a_missing_coverage_report_refuses_the_generated_first_capture_with_its_code(self):
        fake, err = tb.FakeBc(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(bc, "COVERAGE_PATH", Path(tmp) / "bc_coverage.json"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = bc_read.main(["capture", "--generated-first"], environ=tb.ENV, sender=fake)
        self.assertEqual((code, fake.calls), (1, []))   # refused before any request
        self.assertEqual(lv._stderr_error(err.getvalue())["error_code"], "coverage_unreadable")


def setup_world(n: int) -> tb.FakeBc:
    """``n`` locations per company plus one quote with one line; the distribution quote
    line names the manufacturing location's id (a cross-company probe)."""
    fake = tb.FakeBc({name: 0 for name in bc_read.CAPTURE_ENTITY_SETS}, companies=tb.CO)
    for c, company in ((1, MAN), (2, DIST)):
        fake.data[(company, "locations")] = [tp.record(tp.gid(c, 40, i), code=f"L{i}",
                                                      displayName=f"Site {i}")
                                             for i in range(n)]
        quote = tp.gid(c, 41)
        fake.data[(company, "salesQuotes")] = [tp.record(
            quote, number="SQ-1", currencyCode="USD", totalAmountIncludingTax="#1200.00")]
        fake.lines[(company, "salesQuotes", quote)] = [tp.record(
            tp.gid(c, 42), documentId=quote, sequence=10000, lineType="Item",
            locationId=tp.gid(1, 40, 0), quantity="#10", unitOfMeasureCode="EA",
            unitPrice="#120.00", amountIncludingTax="#1200.00")]
    return fake


def oid(object_type: str, binding: str, record: str) -> str:
    return f'{object_type}:{binding}:{{"id":"{record}"}}'


def found(w: tp.BcWorld, object_type: str, prop: str, actor: Actor) -> list[str]:
    args = {"object_type": object_type, "limit": 50, "filters": [{"property": prop, "op": "present"}]}
    ids: list[str] = []
    while True:
        out = w.call("plx.find", args, actor)
        ids += [c["object"]["object_id"] for c in out.get("candidates", ())]
        cursor = out.get("completeness", {}).get("next_cursor")
        if not cursor:
            return ids
        args = dict(args, cursor=cursor)


class CompanyScopeTests(unittest.TestCase):
    """Generated BC types projected from a real capture store, per company."""

    SETS = ("locations", "salesQuotes")

    def test_a_reader_granted_one_company_set_sees_nothing_of_another(self):
        for n in (1, 10, 60):
            w = tp.BcWorld(self, setup_world(n), entity_sets=self.SETS)
            location = oid("RelBcLocations", "gen.bc.locations", tp.gid(1, 40, 0))
            line = oid("RelBcSalesQuoteLines", "gen.bc.sales_quote_lines", tp.gid(1, 42))
            # Full reader: no record lost or duplicated, and the distribution line naming
            # the manufacturing location's id is no link (one company per link).
            self.assertEqual(len(set(found(w, "RelBcLocations", "code", tp.FULL))), 2 * n, n)
            out = w.around(location, "RelBcSalesQuoteLines.location")
            self.assertEqual([o["object_id"] for o in out["objects"]], [line], n)
            granted = Actor("plx", frozenset({"staff:locations"}))
            w.tools(ot.ReadPolicy(tenant="plx", audiences={f"bc:{MAN}/locations":
                                                           {"staff:locations"}},
                                  markings={m: {"staff:locations"} for m in store_ddl.MARKINGS}))
            mine = found(w, "RelBcLocations", "code", granted)
            self.assertEqual(len(mine), n)
            self.assertTrue(all(f'"{tp.gid(1, 40, 0)[:8]}' in i for i in mine), n)
            self.assertEqual(found(w, "RelBcSalesQuotes", "number", granted), [])
            out = w.around(location, "RelBcSalesQuoteLines.location", actor=granted)
            self.assertEqual(out["objects"], [])
            other = w.base.call("plx.object", {"object_id": oid(
                "RelBcLocations", "gen.bc.locations", tp.gid(2, 40, 0))}, granted)
            self.assertFalse(other.get("values"))

    def test_quote_line_money_is_decimal_in_its_header_currency(self):
        fake = setup_world(1)
        quote = tp.gid(1, 41)
        first = fake.lines[(MAN, "salesQuotes", quote)][0]
        first["amountIncludingTax"] = "#0.10"
        fake.lines[(MAN, "salesQuotes", quote)].append(dict(
            first, id=tp.gid(1, 43), sequence=20000, amountIncludingTax="#0.20"))
        w = tp.BcWorld(self, fake, entity_sets=self.SETS)
        lines = w.around(oid("RelBcSalesQuotes", "gen.bc.sales_quotes", quote),
                         "RelBcSalesQuoteLines.sales_quote")
        self.assertEqual(len(lines["objects"]), 2)
        values = {v["property"]: v for v in w.call("plx.object", {
            "object_id": oid("RelBcSalesQuoteLines", "gen.bc.sales_quote_lines", tp.gid(1, 42))})
            ["values"]}
        price = values["unit_price"]
        self.assertEqual((price["value"], price["currency"], price["unit"], price["markings"]),
                         ("120.00", "USD", "EA", ["finance"]))
        out = w.call("plx.aggregate", {"set": lines["set"], "measure": {
            "property": "amount_including_tax", "op": "sum"}})
        self.assertEqual([(Decimal(g["value"]), g["currency"], g["inputs"]) for g in out["groups"]],
                         [(Decimal("0.30"), "USD", 2)])
        for citation in out["groups"][0]["inputs_citations"]:
            self.assertTrue(w.resolved(citation["l1_handle"])["ok"])

if __name__ == "__main__":
    unittest.main()
