"""Ontology schema loader, validator, digest, drift report and skeleton generator (L4).

Fixtures: the committed skeleton and its pinned offline catalog
(``ontology/schema/generated/meta/portal_catalog.json``, built from the upstream
manifest, not live), the auditor's live purchase catalog read
(``operating/tests/fixtures/live_purchase_catalog_2026-10-01.json``, column names
only) and synthetic documents. No network: the live probe runs on an injected client.
"""

from __future__ import annotations

import copy
import dataclasses
import io
import json
import random
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock
from pathlib import Path

from ontology import schema as s
from scripts import gen_ontology_skeleton as gen
from scripts import live_read_probes

ROOT = Path(__file__).resolve().parents[2]
LIVE_PURCHASE_CATALOG = ROOT / "operating/tests/fixtures/live_purchase_catalog_2026-10-01.json"
LINK_CONTRACTS = json.loads((ROOT / "operating/link_contracts.json").read_text())
EXAMPLE = json.loads((ROOT / "ontology/schema/examples/domain.example.json").read_text())


def _docs():
    return copy.deepcopy(_DOCS)


# The Portal skeleton only: curated domain files and other sources' generated files
# (generated/graph_*, generated/bc_*: the BC files bind through curated BC types) belong
# to other lanes and are tested there, so these tests stay valid whatever
# ontology/schema/domains/ holds.
_DOCS = [(name, doc) for name, doc in s.load_documents() if name.startswith("generated/portal_")]
_CATALOG = s.load_catalog()
_COVERAGE = json.loads(s.COVERAGE_PATH.read_text())


def skeleton(catalog=None):
    return compile_docs(_docs(), catalog=catalog)


def compile_docs(docs, *, catalog=_CATALOG, identifiers=None):
    return s.compile_documents(docs, catalog=catalog, identifiers=identifiers,
                               link_contracts=LINK_CONTRACTS, coverage=_COVERAGE)


def find_type(docs, type_id):
    for _, doc in docs:
        for t in doc.get("object_types", []):
            if t["id"] == type_id:
                return t
    raise KeyError(type_id)


def find_link(docs, link_id):
    for _, doc in docs:
        for link in doc.get("link_types", []):
            if link["id"] == link_id:
                return link
    raise KeyError(link_id)


def example():
    return copy.deepcopy(EXAMPLE)


def with_example(ex=None):
    return _docs() + [("domains/example_orders.json", ex if ex is not None else example())]


def errors_of(fn):
    try:
        fn()
    except s.SchemaLoadError as exc:
        return exc.errors
    raise AssertionError("expected SchemaLoadError")


class CommittedSkeletonTests(unittest.TestCase):
    def test_skeleton_validates_against_the_pinned_catalog(self):
        compiled = compile_docs(_docs())
        # One bound Portal type per relation the live API served on 2026-10-26.
        self.assertEqual(len([t for t in compiled.object_types.values() if t["bindings"]]), 248)
        self.assertEqual({t["availability"]["state"] for t in compiled.object_types.values()
                          if not t["bindings"]}, {"not_served"})
        self.assertGreater(len(compiled.link_types), 0)
        self.assertEqual(compiled.shadowed, {})
        self.assertTrue(compiled.digest.startswith("sha256:"))

    def test_every_served_relation_is_bound_or_explicitly_excluded(self):
        compiled = compile_docs(_docs())
        served = set(_CATALOG.served())
        # The 104 relations of the manifest at Portal 52c9116 plus the 144 more the live
        # GET /v1/relations served on 2026-10-26 (live_added records its count and digest).
        self.assertEqual(len(_CATALOG.relations), 248)
        bound = set(compiled.portal_bindings())
        self.assertEqual(served - bound - set(compiled.excluded_relations), set())
        self.assertEqual(set(_COVERAGE["relations"]), set(_CATALOG.relations))
        for name, entry in _COVERAGE["relations"].items():
            self.assertTrue(("object_type" in entry) != ("excluded" in entry), name)
        drift = s.drift_report(compiled, _CATALOG)
        self.assertTrue(drift["ok"])
        self.assertEqual(drift["unbound_relations"], [])
        self.assertEqual(drift["unbound_columns"], {})  # every approved column is a property
        self.assertFalse(drift["catalog_changed"])
        self.assertEqual(drift["pinned_catalog_digest"], drift["catalog_digest"])

    def test_committed_skeleton_is_reproducible(self):
        outputs = gen.build()
        for path, text in outputs.items():
            self.assertEqual(path.read_text(encoding="utf-8"), text, str(path))
        self.assertEqual({p for p in s.GENERATED_DIR.glob("*.json")},
                         {p for p in outputs if p.parent == s.GENERATED_DIR})
        with redirect_stdout(io.StringIO()):
            self.assertEqual(gen.main(["generate", "--check"]), 0)

    # Columns the pinned manifest serves that the 2026-10-01 live read predates: Portal
    # 421d21f9 (TASK-3146, manifest approved) and c2ff912f (TASK-3355, nine drifted
    # relations recaptured) grew them; the read lists none the manifest dropped.
    GROWN_SINCE_LIVE_READ = {
        "public.app_purchase_order_line": 4, "public.app_purchase_requisition": 2,
        "public.app_receipt": 1, "public.app_receipt_line": 3, "public.app_shipment_line": 3,
        "public.generated_purchase_order": 42, "public.generated_purchase_order_item": 25,
        "public.packaging": 9, "public.packaging_supplier": 14, "public.receipt_packaging": 12,
        "silver.purchase_order_items": 1}

    def test_offline_catalog_agrees_with_the_live_purchase_catalog_read(self):
        """Every column, key and cursor the dated live read served is still pinned; the
        columns the manifest grew since are exactly ``GROWN_SINCE_LIVE_READ`` (a further
        change fails here until a new live read replaces the fixture). The daily live
        check ``schema.catalog_drift`` compares the whole pinned catalog."""
        live = s.load_catalog(LIVE_PURCHASE_CATALOG)
        self.assertEqual(len(live.relations), 22)
        grown = {}
        for name, rel in live.relations.items():
            pinned = _CATALOG.relations[name]
            self.assertEqual(set(rel.columns) - set(pinned.columns), set(), name)
            if set(pinned.columns) - set(rel.columns):
                grown[name] = len(set(pinned.columns) - set(rel.columns))
            self.assertEqual(rel.key, pinned.key, name)
            self.assertEqual(rel.since, pinned.since, name)
        self.assertEqual(grown, self.GROWN_SINCE_LIVE_READ)
        # The skeleton validates against the live read for the relations it covers: only
        # the grown columns are degraded there, each once, and nothing is unbound.
        compiled = skeleton()
        drift = s.drift_report(compiled, live)
        degraded = [(d["binding"], d["column"]) for d in drift["degraded"]]
        self.assertEqual(len(degraded), sum(self.GROWN_SINCE_LIVE_READ.values()))
        self.assertEqual(set(degraded), {
            ("gen." + name, column) for name, rel in live.relations.items()
            for column in set(_CATALOG.relations[name].columns) - set(rel.columns)})
        self.assertEqual(drift["unbound_relations"], [])

    def test_link_contracts_and_prisma_keys_become_links_with_honest_tiers(self):
        compiled = compile_docs(_docs())
        by_contract = {lb["link_contract"]: (lid, lb) for lid, link in compiled.link_types.items()
                       for lb in link["bindings"] if "link_contract" in lb}
        # A stored id Portal set by text join is never verified.
        self.assertEqual(by_contract["portal-receipt-to-po-by-id"][1]["tier_ceiling"],
                         "stored_by_text_join")
        self.assertEqual(by_contract["portal-receipt-to-po-by-number-text"][1]["tier_ceiling"], "lead")
        self.assertEqual(by_contract["portal-po-to-fm-generated-po"][1]["kind"], "json_path")
        # Prisma FK and the matching link contract are one link, not two.
        self.assertEqual(by_contract["portal-po-line-to-po"][0],
                         "RelPublicAppPurchaseOrderLine.purchase_order")
        # public.bc_invoice_snapshot is served live since the 2026-10-26 read: its text
        # agreements bind, and only as leads, never verified links.
        bc_contracts = [c for c in by_contract if c.startswith("bc-")]
        self.assertEqual(len(bc_contracts), 4)
        self.assertEqual(_COVERAGE["link_contracts"]["bc-invoice-to-portal-order-by-doc-number"],
                         "bound as RelPublicBcInvoiceSnapshot."
                         "bc_invoice_to_portal_order_by_doc_number")
        for contract in bc_contracts:
            self.assertEqual((by_contract[contract][1]["kind"],
                              by_contract[contract][1]["tier_ceiling"]),
                             ("text_match", "lead"), contract)
        for name, entry in _COVERAGE["fm_source_fk"].items():
            self.assertIn(entry["status"], ("bound", "no link contract"), name)

    def test_raw_money_is_finance_marked_and_carries_its_currency(self):
        # Review p0-b: Prisma Float/Decimal money columns were plain decimals with no
        # finance marking, so a reader without finance read a PR's estimated_cost.
        compiled = compile_docs(_docs())

        def prop(type_id, name):
            return next(p for p in compiled.object_types[type_id]["properties"]
                        if p["name"] == name)
        total = prop("RelPublicAppQuote", "total_amount")
        self.assertEqual((total["value_type"], total["money_measure"], total["markings"]),
                         ("money", "amount", ["finance"]))
        self.assertEqual(total["sources"][0]["currency_from"], "currency")
        price = prop("RelPublicAppQuoteLine", "unit_price")
        self.assertEqual((price["value_type"], price["money_measure"]), ("money", "unit_price"))
        self.assertEqual(price["sources"][0]["currency_from"],
                         {"via_link": "RelPublicAppQuoteLine.quote", "path": "currency"})
        self.assertEqual(price["sources"][0]["per_unit_from"], "uom")
        self.assertIn("currency_unknown",
                      prop("RelPublicAppPurchaseRequisition", "total_amount")["sources"][0])
        estimated = prop("RelPublicAppPrLineItem", "estimated_cost")
        self.assertEqual((estimated["value_type"], estimated["markings"]),
                         ("decimal", ["finance"]))
        # Every other raw column whose name reads as money is finance-marked too, except a
        # column markings.json scoped_markings declares: it carries that marking instead.
        scoped = {key: rule.marking for rule in gen.OVERRIDES.scoped_markings
                  for key in rule.columns}
        for tid, t in compiled.object_types.items():
            for p in t["properties"]:
                key = (t["bindings"][0]["relation"], p["sources"][0]["path"])
                if key in scoped:
                    self.assertEqual(p.get("markings"), [scoped[key]], f"{tid}.{p['name']}")
        self.assertTrue(scoped)
        unmarked = sorted(
            f"{tid}.{p['name']}" for tid, t in compiled.object_types.items()
            for p in t["properties"]
            if p["value_type"] in ("decimal", "integer", "text", "money")
            and gen._MONEY_NAME.search(p["sources"][0]["path"])
            and not gen._NOT_MONEY_NAME.search(p["sources"][0]["path"])
            and (t["bindings"][0]["relation"], p["sources"][0]["path"]) not in gen.NOT_MONEY
            and (t["bindings"][0]["relation"], p["sources"][0]["path"]) not in scoped
            and "finance" not in set(t.get("markings", [])) | set(p.get("markings", [])))
        self.assertEqual(unmarked, [])


class ValidatorTests(unittest.TestCase):
    def test_rejects_unknown_relation(self):
        docs = _docs()
        find_type(docs, "RelPublicAppSalesOrder")["bindings"][0]["relation"] = "public.app_nope"
        errs = errors_of(lambda: compile_docs(docs))
        self.assertTrue(any("unknown relation 'public.app_nope'" in e for e in errs), errs)

    def test_rejects_unknown_column_in_property_key_and_link_pair(self):
        docs = _docs()
        t = find_type(docs, "RelPublicAppSalesOrder")
        t["properties"][0]["sources"][0]["path"] = "no_such_column"
        t["properties"][1]["sources"][0]["when"] = [{"path": "no_when_column", "op": "present"}]
        t["bindings"][0]["key"] = ["missing_key"]
        find_link(docs, "RelPublicAppSalesOrder.customer")["bindings"][0]["pairs"][0]["from"] = "cust"
        errs = errors_of(lambda: compile_docs(docs))
        for column in ("no_such_column", "no_when_column", "missing_key", "cust"):
            self.assertTrue(any(f"unknown column '{column}'" in e for e in errs), (column, errs))

    def test_json_path_checks_its_column(self):
        docs = _docs()
        link = find_link(docs, "RelPublicAppSalesOrder.portal_so_to_fm_order_by_fmid")
        link["bindings"][0]["pairs"][0]["from"] = "customfields.fmId"
        errs = errors_of(lambda: compile_docs(docs))
        self.assertTrue(any("unknown column 'customfields'" in e for e in errs), errs)

    def test_not_served_property_may_name_an_unserved_column(self):
        docs = _docs()
        t = find_type(docs, "RelPublicAppSalesOrder")
        t["properties"].append({"name": "produced_qty", "value_type": "text",
                                "availability": "not_served",
                                "availability_reason": "column not in the reporting API",
                                "sources": [{"binding": t["bindings"][0]["id"],
                                             "path": "totalprodqty_c"}]})
        compile_docs(docs)

    def test_format_errors_are_reported_per_file(self):
        docs = _docs()
        docs[0][1]["object_types"][0]["bindings"][0]["source"] = "sql"
        errs = errors_of(lambda: compile_docs(docs))
        self.assertTrue(any(e.startswith(docs[0][0] + ": format:") for e in errs), errs)

    def test_link_endpoints_must_exist_and_own_their_bindings(self):
        docs = _docs()
        link = find_link(docs, "RelPublicAppSalesOrder.customer")
        link["to"] = "RelPublicAppSupplier"
        errs = errors_of(lambda: compile_docs(docs))
        self.assertTrue(any("belongs to RelPublicAppCustomer, not RelPublicAppSupplier" in e
                            for e in errs), errs)
        link["to"] = "Ghost"
        errs = errors_of(lambda: compile_docs(docs))
        self.assertTrue(any("to type 'Ghost' does not exist" in e for e in errs), errs)

    def test_stored_key_binding_comparing_text_is_rejected_by_binding(self):
        # DECISIONS/2026-10-02-stored-key-compare: fk/json_path/same_record compare
        # stored keys; text is a free-text compare, legal only on text_match.
        for link_id, kind in (("RelPublicAppSalesOrder.customer", "fk"),
                              ("RelPublicAppSalesOrder.portal_so_to_fm_order_by_fmid",
                               "json_path"),
                              ("RelPublicAppSalesOrder.customer", "same_record")):
            with self.subTest(link=link_id, kind=kind):
                docs = _docs()
                lb = find_link(docs, link_id)["bindings"][0]
                lb["kind"] = kind
                lb["pairs"][0]["compare"] = "text"
                errs = errors_of(lambda: compile_docs(docs))
                named = [e for e in errs if f"{link_id} binding 0 ({lb['from_binding']} -> "
                         f"{lb['to_binding']}): {kind} pair" in e and "compares 'text'" in e]
                self.assertEqual(len(named), 1, errs)
                # The C0 format rejects the same file too (not only the loader).
                self.assertTrue(any(": format: " in e and ".compare: 'text' is not one of"
                                    in e for e in errs), errs)

    def test_owner_write_takes_no_record_or_scope_from_the_user_objective(self):
        # One loader rule for every owner-system write, whatever the system: each parameter
        # naming a record or scope (MC repo, bucket and task, a BC company or series) that
        # also accepts the user objective is refused by name; no other error appears.
        docs = s.load_documents()
        opened = set()
        for _, doc in docs:
            for act in doc.get("action_types", []):
                for pname, param in act["params"].items():
                    if act["executor"] == "human_in_owner_system" and \
                            param["type"] in s.OWNER_REFERENCE_PARAMS:
                        param["provenance"] = sorted({*param["provenance"], "user_objective"})
                        opened.add(f"{act['id']}.{pname}")
        self.assertLessEqual({"MCCheckoutTask.repo", "MCCheckoutTask.task_id",
                              "MCCreateTask.bucket", "MCCreateTask.reporter",
                              "EnsureSalesOrderManualNos.series_code",
                              "EnsurePurchaseOrderManualNos.company"}, opened)
        errs = errors_of(lambda: s.compile_documents(
            docs, catalog=None, identifiers=json.loads((s.SCHEMA_DIR / "identifiers.json")
                                                       .read_text()),
            link_contracts=LINK_CONTRACTS, coverage=_COVERAGE,
            document_fields=json.loads(s.DOCUMENT_FIELDS_PATH.read_text())))
        self.assertEqual({e.split(":", 1)[0] for e in errs}, opened, errs)
        self.assertTrue(all("never the user objective" in e for e in errs), errs)

    def test_stored_key_via_pairs_must_not_compare_text(self):
        docs = _docs()
        lb = find_link(docs, "RelPublicAppSalesOrder.customer")["bindings"][0]
        lb["via"] = {"binding": lb["to_binding"],
                     "from_pairs": [{"from": "customer_id", "to": "id", "compare": "text_id"}],
                     "to_pairs": [{"from": "id", "to": "id", "compare": "text"}]}
        errs = errors_of(lambda: compile_docs(docs))
        self.assertTrue(any("RelPublicAppSalesOrder.customer binding 0" in e and
                            "via pair 'id' -> 'id' compares 'text'" in e for e in errs), errs)

    def test_text_compare_stays_legal_on_text_match_and_typed_compares_on_fk(self):
        docs = _docs()
        text_match = find_link(docs, "RelPublicAppReceipt.portal_receipt_to_po_by_number_text")
        self.assertEqual(text_match["bindings"][0]["kind"], "text_match")
        self.assertEqual(text_match["bindings"][0]["pairs"][0]["compare"], "text")
        for compare in ("integer_id", "text_id"):
            find_link(docs, "RelPublicAppSalesOrder.customer")["bindings"][0]["pairs"][0][
                "compare"] = compare
            compiled = compile_docs(docs)
            self.assertIn("RelPublicAppReceipt.portal_receipt_to_po_by_number_text",
                          compiled.link_types)
        # Every committed file (curated and generated) satisfies the rule.
        committed = s.load_documents(include_pending=True)
        self.assertGreater(len(committed), len(docs))
        self.assertEqual([e for name, doc in committed
                          for e in s.stored_key_compare_errors(name, doc)], [])

    def test_missing_compare_is_worded_as_missing_not_none(self):
        doc = {"link_types": [{"id": "L", "bindings": [{
            "kind": "fk", "from_binding": "a", "to_binding": "b",
            "pairs": [{"from": "x", "to": "y"}]}]}]}
        (error,) = s.stored_key_compare_errors("t.json", doc)
        self.assertIn("has no compare", error)
        self.assertNotIn("None", error)
        doc["link_types"][0]["bindings"][0]["pairs"][0]["compare"] = "text"
        (error,) = s.stored_key_compare_errors("t.json", doc)
        self.assertIn("compares 'text'", error)

    def test_unknown_link_contract_is_rejected(self):
        docs = _docs()
        lb = find_link(docs, "RelPublicAppSalesOrder.portal_so_to_fm_order_by_fmid")["bindings"][0]
        lb["link_contract"] = "no-such-contract"
        errs = errors_of(lambda: compile_docs(docs))
        self.assertTrue(any("unknown link contract 'no-such-contract'" in e for e in errs), errs)

    def test_duplicate_ids_and_navigation_names_are_rejected(self):
        docs = _docs()
        clone = copy.deepcopy(find_type(docs, "RelPublicAppSalesOrder"))
        clone["bindings"][0]["id"] = "gen.copy"
        clone["properties"] = [dict(p, sources=[dict(p["sources"][0], binding="gen.copy")])
                               for p in clone["properties"]]
        docs[0][1]["object_types"].append(clone)
        errs = errors_of(lambda: compile_docs(docs))
        self.assertTrue(any("object type 'RelPublicAppSalesOrder' declared twice" in e
                            for e in errs), errs)
        docs = _docs()
        find_link(docs, "RelPublicAppSalesOrder.customer_location")["name"] = "customer"
        errs = errors_of(lambda: compile_docs(docs))
        self.assertTrue(any("navigation name 'customer'" in e for e in errs), errs)

    def test_identifier_kinds_must_exist_when_the_registry_is_present(self):
        docs = _docs() + [("domains/example_orders.json", example())]
        registry = {"format": "plx-identifiers/v1", "kinds": {"so_number": {}}}
        errs = errors_of(lambda: compile_docs(docs, identifiers=registry))
        self.assertTrue(any("unknown identifier kind 'customer_erp_code'" in e for e in errs), errs)
        registry["kinds"].update({"customer_erp_code": {}, "customer_po": {}})
        compiled = compile_docs(docs, identifiers=registry)
        self.assertEqual(compiled.warnings, [])
        # Without the registry the kinds are unchecked and say so.
        compiled = compile_docs(docs)
        self.assertTrue(any("unchecked" in w for w in compiled.warnings))

    def test_authority_must_cover_every_source_and_only_produced_lineages(self):
        ex = example()
        docs = _docs() + [("domains/example_orders.json", ex)]
        order = next(t for t in ex["object_types"] if t["id"] == "SalesOrder")
        multi = [p for p in order["properties"] if len(p["sources"]) >= 2]
        self.assertTrue(multi, "the C0 example has a multi-source property")
        prop = multi[0]
        compile_docs(docs)  # as written: valid
        prop["authority"]["precedence"] = prop["authority"]["precedence"][:1]
        # An unranked lineage is never authority (C0): a warning, not an error.
        compiled = compile_docs(docs)
        self.assertTrue(any(f"{order['id']}.{prop['name']}: lineages" in w and
                            "never authority" in w for w in compiled.warnings), compiled.warnings)
        prop["authority"]["precedence"][0]["lineage"] = ["sage_bronze"]
        errs = errors_of(lambda: compile_docs(docs))
        self.assertTrue(any("names lineage 'sage_bronze' that no source binding produces" in e
                            for e in errs), errs)

    def test_c0_example_compiles_over_the_skeleton(self):
        compiled = compile_docs(with_example())
        self.assertIn("SalesOrderLine", compiled.object_types)
        # The line's portal binding inherits its header's lineage through line_of.
        line = compiled.bindings["portal.app_sales_order_line"][1]
        self.assertEqual(line["lineage"]["inherit"], "SalesOrderLine.line_of")

    def test_observed_property_must_exist(self):
        ex = example()
        order = next(t for t in ex["object_types"] if t["id"] == "SalesOrder")
        order["properties"] = [p for p in order["properties"] if p["name"] != "status"]
        errs = errors_of(lambda: compile_docs(with_example(ex)))
        self.assertIn("SalesOrder.status_changed: observed_property 'status' unknown", errs)


class FormatV1RepairTests(unittest.TestCase):
    """C0 9674d95: evidence refs, via_link hops, lineage inherit, per-source units."""

    def _line(self, ex):
        return next(t for t in ex["object_types"] if t["id"] == "SalesOrderLine")

    def test_evidence_refs_resolve_and_unknown_refs_are_rejected(self):
        ex = example()
        self.assertTrue(ex["evidence_catalog"])
        first = compile_docs(with_example(ex)).digest
        # Inlining every ref is the same schema.
        inlined, missing = s.resolve_evidence_refs(ex)
        self.assertEqual(missing, [])
        self.assertNotIn('"kind": "ref"', json.dumps(inlined))
        self.assertEqual(first, compile_docs(with_example(inlined)).digest)
        ref = next(iter(ex["evidence_catalog"]))
        ex["evidence_catalog"]["renamed"] = ex["evidence_catalog"].pop(ref)
        errs = errors_of(lambda: compile_docs(with_example(ex)))
        self.assertIn(f"domains/example_orders.json: unknown evidence ref {ref!r}", errs)

    def test_via_link_must_be_declared_from_this_type_and_to_one(self):
        ex = example()
        line = self._line(ex)
        prop = next(p for p in line["properties"] if p["name"] == "line_total")
        prop["currency_from"]["via_link"] = "SalesOrderLine.nowhere"
        errs = errors_of(lambda: compile_docs(with_example(ex)))
        self.assertTrue(any("'SalesOrderLine.nowhere' is not a declared link type" in e
                            for e in errs), errs)
        prop["currency_from"]["via_link"] = "SalesOrder.ordered_by"
        errs = errors_of(lambda: compile_docs(with_example(ex)))
        self.assertTrue(any("starts at SalesOrder, not SalesOrderLine" in e for e in errs), errs)
        ex = example()
        link = next(l for l in ex["link_types"] if l["id"] == "SalesOrderLine.line_of")
        link["cardinality"] = "N:M"
        errs = errors_of(lambda: compile_docs(with_example(ex)))
        self.assertTrue(any("is N:M; one hop must be N:1 or 1:1" in e for e in errs), errs)

    def test_via_link_needs_a_link_binding_starting_at_the_binding(self):
        ex = example()
        link = next(l for l in ex["link_types"] if l["id"] == "SalesOrderLine.line_of")
        link["bindings"] = [lb for lb in link["bindings"]
                            if lb["from_binding"] != "portal.fm_order_entry_item"]
        errs = errors_of(lambda: compile_docs(with_example(ex)))
        self.assertTrue(any("has no binding starting at portal.fm_order_entry_item" in e
                            for e in errs), errs)

    def test_via_link_path_is_checked_on_the_target_relation(self):
        ex = example()
        prop = next(p for p in self._line(ex)["properties"] if p["name"] == "line_total")
        prop["currency_from"]["path"] = "no_currency_here"
        errs = errors_of(lambda: compile_docs(with_example(ex)))
        self.assertTrue(any("unknown column 'no_currency_here' of public.app_sales_order" in e
                            for e in errs), errs)

    def test_lineage_inheritance_cycles_are_refused(self):
        ex = example()
        order = next(t for t in ex["object_types"] if t["id"] == "SalesOrder")
        ex["link_types"].append({
            "id": "SalesOrder.first_line", "from": "SalesOrder", "to": "SalesOrderLine",
            "name": "first_line", "reverse_name": "first_line_of", "cardinality": "1:1",
            "description": "Synthetic link to close an inheritance cycle.",
            "bindings": [{"kind": "fk", "tier_ceiling": "lead",
                          "from_binding": "portal.app_sales_order",
                          "to_binding": "portal.app_sales_order_line",
                          "pairs": [{"from": "id", "to": "sales_order_id", "compare": "text_id"}],
                          "evidence": [{"kind": "human", "who": "test", "when": "2026-10-01",
                                        "note": "synthetic"}]}]})
        binding = next(b for b in order["bindings"] if b["id"] == "portal.app_sales_order")
        binding["lineage"]["inherit"] = "SalesOrder.first_line"
        errs = errors_of(lambda: compile_docs(with_example(ex)))
        self.assertTrue(any(e.startswith("lineage inheritance cycle: ") and
                            "portal.app_sales_order" in e and "portal.app_sales_order_line" in e
                            for e in errs), errs)

    def test_per_source_unit_paths_are_checked(self):
        ex = example()
        qty = next(p for p in self._line(ex)["properties"] if p["name"] == "quantity")
        qty["sources"][0]["unit_from"] = "unit_of_measure"
        errs = errors_of(lambda: compile_docs(with_example(ex)))
        self.assertTrue(any("unknown column 'unit_of_measure'" in e for e in errs), errs)

    def test_role_only_unit_contracts_become_unit_unknown_quantities(self):
        compiled = compile_docs(_docs())
        qty = next(p for p in compiled.object_types["RelPublicOrderEntryItem"]["properties"]
                   if p["name"] == "qty")
        self.assertEqual((qty["value_type"], qty["quantity_role"]), ("quantity", "ordered"))
        self.assertIn("reason", qty["unit_unknown"])
        self.assertNotIn("unit", qty)
        report = _COVERAGE["unit_contracts"]
        self.assertEqual(sum("unit_unknown" in v for v in report.values()), 9)
        # A role that holds only on some rows is never applied to the whole column.
        self.assertIn("not applied: the role holds only when",
                      report["silver-production-tasks-qty-filled-on-filling"])

    def test_origin_and_domain_must_match_the_file(self):
        ex = example()
        errs = errors_of(lambda: compile_docs(_docs() + [("generated/example_orders.json", ex)]))
        self.assertTrue(any("origin 'curated'" in e for e in errs), errs)
        errs = errors_of(lambda: compile_docs(_docs() + [("domains/orders.json", ex)]))
        self.assertTrue(any("differs from the file name" in e for e in errs), errs)


class ShadowingTests(unittest.TestCase):
    def test_curated_binding_shadows_the_raw_type_and_its_links(self):
        docs = _docs() + [("domains/example_orders.json", example())]
        compiled = compile_docs(docs)
        self.assertIn("SalesOrder", compiled.object_types)
        self.assertIn("RelPublicAppSalesOrder", compiled.shadowed)
        self.assertIn("RelPublicAppCustomer", compiled.shadowed)
        self.assertNotIn("RelPublicAppSalesOrder", compiled.object_types)
        self.assertNotIn("RelPublicAppSalesOrderLine.sales_order", compiled.link_types)
        self.assertIn("RelPublicAppSalesOrderLine.sales_order", compiled.shadowed)
        drift = s.drift_report(compiled, _CATALOG)
        self.assertEqual(drift["unbound_relations"], [])
        # The curated type reads fewer columns: the rest are reported, never ignored (R9c).
        self.assertIn("public.app_sales_order", drift["unbound_columns"])
        self.assertIn("notes", drift["unbound_columns"]["public.app_sales_order"])
        self.assertNotIn("order_number", drift["unbound_columns"]["public.app_sales_order"])


class DigestTests(unittest.TestCase):
    def test_same_input_same_digest_regardless_of_order_and_formatting(self):
        first = compile_docs(_docs()).digest
        self.assertEqual(first, compile_docs(_docs()).digest)
        docs = [(name, json.loads(json.dumps(doc, indent=4))) for name, doc in reversed(_docs())]
        rng = random.Random(7)
        for _, doc in docs:
            rng.shuffle(doc["object_types"])
            rng.shuffle(doc["link_types"])
            for t in doc["object_types"]:
                rng.shuffle(t["properties"])
        self.assertEqual(first, compile_docs(docs).digest)
        with tempfile.TemporaryDirectory() as tmp:  # the file-loading path, skeleton only
            shutil.copy(s.FORMAT_PATH, tmp)
            shutil.copytree(s.GENERATED_DIR, Path(tmp) / "generated")
            for other in (Path(tmp) / "generated").glob("*.json"):
                if not other.name.startswith("portal_"):
                    other.unlink()
            self.assertEqual(first, s.load_schema(tmp, catalog=_CATALOG).digest)

    def test_digest_changes_with_content(self):
        first = compile_docs(_docs()).digest
        docs = _docs()
        find_type(docs, "RelPublicAppSalesOrder")["properties"][0]["value_type"] = "json"
        find_type(docs, "RelPublicAppSalesOrder")["properties"][0]["sources"][0]["parser"] = "as_is"
        self.assertNotEqual(first, compile_docs(docs).digest)
        docs = _docs()
        find_link(docs, "RelPublicAppSalesOrder.customer")["bindings"][0]["tier_ceiling"] = "lead"
        self.assertNotEqual(first, compile_docs(docs).digest)

    def test_precedence_order_is_semantic(self):
        ex = example()
        docs = _docs() + [("domains/example_orders.json", ex)]
        first = compile_docs(docs).digest
        order = next(t for t in ex["object_types"] if t["id"] == "SalesOrder")
        prop = next(p for p in order["properties"] if len(p["sources"]) >= 2)
        prop["authority"]["precedence"].reverse()
        self.assertNotEqual(first, compile_docs(docs).digest)

    def test_catalog_digest_is_order_independent(self):
        raw = json.loads(s.CATALOG_SNAPSHOT_PATH.read_text())
        shuffled = copy.deepcopy(raw)
        shuffled["relations"].reverse()
        for entry in shuffled["relations"]:
            entry["columns"].reverse()
        self.assertEqual(s.catalog_from_json(raw).digest, s.catalog_from_json(shuffled).digest)
        shuffled["relations"][0]["columns"].append("new_column")
        self.assertNotEqual(s.catalog_from_json(raw).digest, s.catalog_from_json(shuffled).digest)


class LoadCacheTests(unittest.TestCase):
    def test_memoized_by_content_copied_per_caller_and_format_checked_by_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "schema"
            shutil.copytree(s.SCHEMA_DIR, root)
            orders = root / "domains" / "orders.json"
            with mock.patch.object(s, "SCHEMA_DIR", root), \
                    mock.patch.dict(s._LOADED, clear=True), \
                    mock.patch.object(s, "compile_documents", wraps=s.compile_documents) as comp, \
                    mock.patch.object(s.json_contract, "validate",
                                      wraps=s.json_contract.validate) as validate:
                first = s.load_schema(root)
                self.assertEqual(validate.call_count, 0)  # committed files: the gate checks
                first.object_types.clear()
                first.link_types["x"] = {}
                second = s.load_schema(root)
                self.assertEqual(comp.call_count, 1)  # unchanged files: the cached compile
                self.assertGreater(len(second.object_types), 0)  # no leak from `first`
                self.assertNotIn("x", second.link_types)
                second.object_types.clear()
                self.assertGreater(len(s.load_schema(root).object_types), 0)
                doc = json.loads(orders.read_text())
                doc["object_types"][0]["description"] += " (edited)"
                orders.write_text(json.dumps(doc))
                third = s.load_schema(root)
                self.assertEqual(comp.call_count, 2)  # an edited file is a fresh load
                self.assertNotEqual(third.digest, second.digest)
                doc["zz_not_in_the_format"] = True
                orders.write_text(json.dumps(doc))
            # The gate's two checks still reject a committed file that breaks the format.
            with mock.patch.object(s, "SCHEMA_DIR", root), redirect_stdout(io.StringIO()) as out:
                self.assertEqual(gen.main(["generate", "--check"]), 1)
                self.assertEqual(s.main(["--root", str(root)]), 1)
            self.assertIn("domains/orders.json: format:", out.getvalue())


class DriftTests(unittest.TestCase):
    def _catalog(self, mutate):
        raw = json.loads(s.CATALOG_SNAPSHOT_PATH.read_text())
        mutate(raw["relations"])
        return s.catalog_from_json(raw)

    def test_new_relation_gone_column_and_gone_relation_are_reported(self):
        compiled = skeleton()

        def mutate(rels):
            order = next(r for r in rels if r["name"] == "public.app_sales_order")
            order["columns"].remove("po_reference")
            rels[:] = [r for r in rels if r["name"] != "public.app_quote_line"]
            rels.append({"name": "public.app_new_thing", "schema": "public",
                         "relation": "app_new_thing", "columns": ["id"], "key": ["id"],
                         "since": None, "unavailable": None})
        drift = s.drift_report(compiled, self._catalog(mutate))
        self.assertFalse(drift["ok"])
        self.assertTrue(drift["catalog_changed"])
        self.assertEqual(drift["unbound_relations"], ["public.app_new_thing"])
        self.assertEqual([m["relation"] for m in drift["missing_relations"]],
                         ["public.app_quote_line"])
        self.assertEqual([(d["object_type"], d["column"], d["availability"])
                          for d in drift["degraded"]],
                         [("RelPublicAppSalesOrder", "po_reference", "degraded")])

    def test_unavailable_relation_counts_as_missing(self):
        compiled = skeleton()

        def mutate(rels):
            next(r for r in rels if r["name"] == "silver.shipments")["unavailable"] = \
                "relation not found"
        drift = s.drift_report(compiled, self._catalog(mutate))
        self.assertEqual(drift["missing_relations"][0]["reason"], "relation not found")

    def test_validator_rejects_binding_to_unserved_relation(self):
        def mutate(rels):
            next(r for r in rels if r["name"] == "silver.shipments")["unavailable"] = "gone"
        errs = errors_of(lambda: compile_docs(_docs(), catalog=self._catalog(mutate)))
        self.assertTrue(any("'silver.shipments' is not served (gone)" in e for e in errs), errs)


class GeneratorTests(unittest.TestCase):
    PRISMA = '''
model Order {
  id         String   @id @default(cuid())
  number     String   @unique @map("order_number") // "// not a comment"
  total      Decimal? @db.Decimal(12, 2)
  shipDate   DateTime? @db.Date @map("ship_date")
  state      OrderState @default(OPEN)
  customerId String   @map("customer_id")
  ownerId    String?  @map("owner_id")
  customer   Customer @relation("orders", fields: [customerId], references: [id])
  owner      User?    @relation(fields: [ownerId], references: [id])
  lines      Line[]
  @@map("app_order")
}

model Line {
  id      Int    @id
  orderId String @map("order_id")
  order   Order  @relation(fields: [orderId], references: [id], onDelete: Cascade)
  @@unique([orderId, id])
  @@map("app_line")
}

model Customer {
  id     String  @id
  orders Order[] @relation("orders")
  @@map("app_customer")
}

model User {
  id String @id
  @@map("app_user")
}

enum OrderState {
  OPEN   @map("open")
  CLOSED
}
'''

    def _catalog(self):
        rel = lambda name, cols: {"name": f"public.{name}", "schema": "public", "relation": name,
                                  "domain": "orders", "columns": cols, "key": ["id"],
                                  "since": None, "unavailable": None}
        return {"format": s.CATALOG_FORMAT, "evidence_mode": "upstream-manifest",
                "source": {"repo": "plx-customer-portal", "commit": "abc1234"},
                "relations": [rel("app_order", ["id", "order_number", "total", "ship_date",
                                                "state", "customer_id", "owner_id"]),
                              rel("app_line", ["id", "order_id"]),
                              rel("app_customer", ["id"]),
                              {**rel("app_gone", ["id"]), "key": None,
                               "unavailable": "relation not found"}]}

    def test_prisma_facts(self):
        facts = gen.prisma_facts(self.PRISMA, ["app_order", "app_line", "app_customer"], "abc1234")
        order = facts["models"]["Order"]
        self.assertEqual(order["table"], "app_order")
        self.assertEqual(order["columns"]["order_number"]["field"], "number")
        self.assertEqual(order["unique"], [["order_number"]])
        self.assertEqual(order["id"], ["id"])
        rel = {r["field"]: r for r in order["relations"]}
        self.assertEqual(rel["customer"]["columns"], ["customer_id"])
        self.assertEqual(rel["customer"]["name"], "orders")
        self.assertEqual(rel["owner"]["target_table"], "app_user")
        self.assertEqual(facts["models"]["Line"]["unique"], [["order_id", "id"]])
        self.assertEqual(facts["enums"], {"OrderState": ["open", "CLOSED"]})
        self.assertNotIn("User", facts["models"])

    def test_generate_binds_served_excludes_unserved_and_never_invents_links(self):
        facts = gen.prisma_facts(self.PRISMA, ["app_order", "app_line", "app_customer"], "abc1234")
        contracts = {"contracts": [{"id": "order-to-gone", "from": {"relation": "public.app_order"},
                                    "to": {"relation": "public.app_gone"}, "match": "fk",
                                    "pairs": [{"from": "id", "to": "id", "compare": "text_id"}]}]}
        units = {"contracts": [{"id": "order-total-unit", "relation": "public.app_order",
                                "column": "total", "unit": "CAD-cents"}]}
        # A toy Portal: the real money.json and names.json name relations it does not have.
        overrides = dataclasses.replace(gen.OVERRIDES, money={}, names={})
        docs, coverage = gen.generate(self._catalog(), facts, contracts, units,
                                      {"fm_x": ("customer_id", "customerId")}, overrides)
        self.assertEqual(list(docs), ["portal_not_served_f_o.json", "portal_not_served_s_z.json",
                                      "portal_orders.json"])
        self.assertEqual(coverage["excluded_relations"],
                         {"public.app_gone": "not served by the reporting API: relation not found"})
        # The model outside the catalog and the excluded relation: not_served, with the ask.
        self.assertEqual(coverage["not_served"], {
            "public.app_gone": {"object_type": "RelPublicAppGone", "upstream_name": None,
                                "declared_by": "generated"},
            "public.app_user": {"object_type": "RelPublicAppUser", "upstream_name": "User",
                                "declared_by": "generated"}})
        (user,) = docs["portal_not_served_s_z.json"]["object_types"]
        self.assertEqual((user["id"], user["availability"]["state"],
                          user["availability"]["upstream_ask"], user["bindings"]),
                         ("RelPublicAppUser", "not_served", gen.UPSTREAM_ASK, []))
        doc = docs["portal_orders.json"]
        types = {t["id"]: t for t in doc["object_types"]}
        self.assertEqual(set(types), {"RelPublicAppOrder", "RelPublicAppLine", "RelPublicAppCustomer"})
        props = {p["name"]: p for p in types["RelPublicAppOrder"]["properties"]}
        self.assertEqual(props["total"]["value_type"], "decimal")
        self.assertEqual(props["total"]["unit"], "CAD-cents")
        self.assertEqual(props["ship_date"]["value_type"], "date")
        self.assertEqual(props["state"]["enum_values"], ["open", "CLOSED"])
        links = {l["id"]: l for l in doc["link_types"]}
        self.assertEqual(set(links), {"RelPublicAppOrder.customer", "RelPublicAppLine.order"})
        self.assertEqual(links["RelPublicAppOrder.customer"]["reverse_name"], "orders")
        self.assertEqual(links["RelPublicAppLine.order"]["reverse_name"], "lines")
        self.assertEqual(links["RelPublicAppLine.order"]["cardinality"], "N:1")
        (owner,) = coverage["prisma_relations_not_bound"]
        self.assertEqual((owner["field"], owner["reason"], owner["target_type"]),
                         ("owner", "target relation not served", "RelPublicAppUser"))
        self.assertEqual(props["owner_id"]["value_type"], "text")   # a plain reference value
        self.assertIn("not served", coverage["link_contracts"]["order-to-gone"])
        self.assertEqual(coverage["fm_source_fk"]["fm_x"]["served_relations_with_column"],
                         ["public.app_order"])
        catalog = s.catalog_from_json(self._catalog())
        compiled = s.compile_documents([(f"generated/{name}", d) for name, d in docs.items()],
                                       catalog=catalog, link_contracts=contracts,
                                       coverage=coverage)
        self.assertTrue(s.drift_report(compiled, catalog)["ok"])

    def test_offline_key_choice_mirrors_the_api(self):
        live = {"id": {"type": "text", "notnull": False},
                "code": {"type": "text", "notnull": True},
                "ts": {"type": "timestamp(3) without time zone", "notnull": True}}
        self.assertEqual(gen.choose_key(live, [{"name": "a", "type": "u", "cols": ["code"]}]),
                         (["code"], "unique"))
        self.assertEqual(gen.choose_key(live, [{"name": "a", "type": "u", "cols": ["ts"]}]),
                         (["id"], "id"))  # timestamps cannot be cursor keys
        self.assertEqual(gen.choose_key(live, []), (["id"], "id"))
        self.assertIsNone(gen.choose_key({"x": {"type": "text", "notnull": True}}, []))
        self.assertEqual(gen.since_mode("audtdate", "text"), "yyyymmdd-text")


class LiveProbeInjectedTests(unittest.TestCase):
    """schema-drift probe over an injected client (not a live read)."""

    class Client:
        def __init__(self, relations):
            self._relations = relations

        def relations(self):
            return self._relations

    def _relations(self):
        raw = json.loads(s.CATALOG_SNAPSHOT_PATH.read_text())
        return {e["name"]: {k: e[k] for k in ("name", "schema", "relation", "domain", "columns",
                                               "key", "since", "unavailable")}
                for e in raw["relations"]}

    def _run(self, relations):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(gen.ontology_schema, "load_schema",
                                                     side_effect=lambda: skeleton()):
            code = live_read_probes.main(["schema-drift"], environ={},
                                         clients={"schema-drift": self.Client(relations)})
        return code, json.loads(out.getvalue())

    def test_probe_reports_no_drift_for_the_pinned_catalog(self):
        code, report = self._run(self._relations())
        self.assertEqual(code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["unbound_relation_count"], 0)
        self.assertEqual(report["served_relations"], 248)
        self.assertEqual(report["catalog_evidence_mode"], "live-read")

    def test_probe_reports_degraded_and_unbound(self):
        rels = self._relations()
        rels["public.app_receipt"]["columns"].remove("po_reference")
        rels["sage.new_table"] = {"name": "sage.new_table", "schema": "sage",
                                  "relation": "new_table", "columns": ["k"], "key": ["k"]}
        code, report = self._run(rels)
        self.assertEqual(code, 0)
        self.assertFalse(report["ok"])
        self.assertEqual(report["degraded_count"], 1)
        self.assertEqual(report["unbound_relations"], ["sage.new_table"])

    def test_unavailable_relation_is_an_upstream_block_and_a_gone_one_fails(self):
        """The live 2026-10-14 state (eight bound sage relations listed with every approved
        column missing or not granted) blocks the spec, never passes it; a bound relation
        the catalog no longer lists fails it even beside such a block."""
        from devloop import live_verify
        spec = json.loads((Path(__file__).resolve().parents[2] / "devloop" / "live-checks" /
                           "schema.catalog_drift.json").read_text())
        gone = ("ar_customer", "ar_ship_to", "oe_shipment_detail", "oe_shipment_header",
                "po_detail", "po_header", "po_receipt_detail", "po_receipt_header")
        rels = self._relations()
        for name in gone:
            entry = rels[f"sage.{name}"]
            entry.update(key=None, since=None, unavailable=f"{len(entry['columns'])} approved "
                                                           "columns are missing or not granted")
        code, report = self._run(rels)
        self.assertEqual(code, 0)
        # The digest of this read: the 248-relation catalog pinned from the 2026-10-26 live
        # read with the eight sage relations above marked unavailable.
        self.assertEqual(report["catalog_digest_prefix"], "sha256:b07c7b8d256b6b0b")
        self.assertEqual((report["missing_relation_count"], report["missing_unexplained_count"]),
                         (8, 0))
        parts = live_verify.blocked_parts(report)
        self.assertEqual([(p["part"], p["cause"]) for p in parts],
                         [(f"sage.{n}", "relation_unavailable") for n in gone])
        self.assertTrue(all(p["reason"].endswith("missing or not granted") for p in parts))
        self.assertTrue(live_verify.check_expectations(spec, code, report))
        self.assertEqual(live_verify.check_expectations(spec, code, report,
                                                        spec["expect_blocked"]), [])
        del rels["sage.ic_item_uom"]
        code, report = self._run(rels)
        self.assertEqual(report["missing_unexplained_count"], 1)
        self.assertEqual(len(report["blocked_upstream"]), 8)
        self.assertIn("missing_unexplained_count != expected", live_verify.check_expectations(
            spec, code, report, spec["expect_blocked"]))

    def test_probe_without_credentials_fails_closed(self):
        err = io.StringIO()
        from contextlib import redirect_stderr
        with redirect_stderr(err):
            code = live_read_probes.main(["schema-drift"], environ={})
        self.assertEqual(code, 1)
        self.assertIn("LookupError", err.getvalue())


if __name__ == "__main__":
    unittest.main()
