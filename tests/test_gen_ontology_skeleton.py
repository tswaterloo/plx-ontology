"""The source-independent generator (``ontology/generator``), its declarative overrides and
the Portal adapter's unserved declarations and ranked upstream ask.

Evidence label: ``isolated-fixture`` (synthetic rows generated from the real compiled
schema by ``ontology/generator/fixture.py``, projected into a temporary store and
read through the real ontology tools; hand-written facts for a non-Portal source) and
``injected-transport`` (SharePoint files captured over an injected Graph drive for the
production item gates). No network, never live.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from integrations.capture_mail import MailCaptureStore
from integrations.live_source_guard import LiveSourceAudience
from integrations.tests import test_capture_sharepoint as sp
from ontology import generator as gen
from ontology.generator import fixture
from ontology import store_ddl
from ontology.project import SharePointCaptures, build_store
from ontology import schema as ontology_schema
from ontology.schema import SCHEMA_DIR, load_schema
from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_aggregate as oa
from scripts import gen_ontology_skeleton as skel
from scripts import source_registry

AT = dt.datetime(2026, 1, 3, tzinfo=dt.timezone.utc)
HELD = frozenset({"finance", "pii"})


def gap_id(source: str, key: str) -> str:
    return "gap-" + hashlib.sha256(f"{source}\0{key}".encode()).hexdigest()[:12]


def _overrides_copy(test: unittest.TestCase) -> Path:
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    path = Path(tmp.name) / "overrides"
    shutil.copytree(gen.OVERRIDES_DIR, path)
    return path


def _edit(path: Path, change) -> None:
    doc = json.loads(path.read_text())
    change(doc)
    path.write_text(json.dumps(doc))


class OverridesTests(unittest.TestCase):
    def test_a_bad_override_fails_the_load(self):
        cases = {
            "unknown column key": ("money.json", lambda d: d["columns"]["public.app_quote"]
                                   ["subtotal"].update(currency="CAD")),
            "unknown marking": ("markings.json",
                                lambda d: d["name_rules"][0].update(marking="secret")),
            "unknown lineage": ("lineage.json", lambda d: d["sources"]["portal_api"][0]
                                .update(lineage="gold")),
            "unknown tax basis": ("money.json", lambda d: d["columns"]["public.app_quote"]
                                  ["subtotal"].update(tax_basis="gross")),
            "missing file": ("identity.json", None),
            "not JSON": ("money.json", "{"),
            # An empty or finance-less rule list silently drops every cost column's
            # marking for a source no money.json entry covers.
            "no name rules": ("markings.json", lambda d: d.update(name_rules=[])),
            "no finance name rule": ("markings.json",
                                     lambda d: d["name_rules"][0].update(marking="pii")),
            "money not finance-marked": ("money.json", lambda d: d.update(markings=["pii"])),
            # An unknown key source would make a fallback key a verified unique identity.
            "unknown verified key source": ("identity.json", lambda d: d[
                "verified_key_sources"].append("unique-index")),
            "non-string pattern": ("markings.json",
                                   lambda d: d["name_rules"][0].update(matches=5)),
            "non-string applied": ("markings.json",
                                   lambda d: d["name_rules"][0].update(applied=None)),
            "non-string exception label": ("markings.json", lambda d: d["name_rules"][0]
                                           .update(exception_label=["not money"])),
            "lineage sources not an object": ("lineage.json", lambda d: d.update(sources=[])),
            "money relation no lineage claims": ("money.json", lambda d: d["columns"].update(
                {"pubic.app_quote": {"subtotal": {"measure": "amount", "tax_basis": "unknown"}}})),
            # A curated id must be a C0 type id and name one relation; a marking it carries
            # is an access decision, so an unknown one fails.
            "name not a type id": ("names.json", lambda d: d["sources"]["portal_api"][
                "public.app_pick_list"].update(object_type="pick list")),
            "name twice": ("names.json", lambda d: d["sources"]["portal_api"].update(
                {"public.app_pick_line": {"object_type": "PickList", "note": "again"}})),
            "name marking unknown": ("names.json", lambda d: d["sources"]["portal_api"][
                "public.app_eqms_record"].update(markings=["qc"])),
        }
        for name, (file, change) in cases.items():
            with self.subTest(name):
                directory = _overrides_copy(self)
                if change is None:
                    (directory / file).unlink()
                elif isinstance(change, str):
                    (directory / file).write_text(change)
                else:
                    _edit(directory / file, change)
                with self.assertRaises(gen.OverrideError):
                    gen.load_overrides(directory)


class SourceIndependenceTests(unittest.TestCase):
    """Facts of a source the Portal adapter knows nothing about."""

    @staticmethod
    def facts():
        def relation(name, key_source, columns):
            return {"name": name, "domain": "billing", "excluded": None, "key": ["id"],
                    "key_source": key_source,
                    "columns": [{"name": c, "value_type": t} for c, t in columns],
                    "description": f"ERP relation {name}.", "evidence": [],
                    "key_evidence": {"kind": "workbench_code", "path": "x", "note": "key"},
                    "record_evidence": None}
        return {
            "source": "erp_test", "file_prefix": "erp_", "description": "ERP test.",
            "upstream_pins": {},
            "relations": [
                relation("erp.invoice", "primary-key",
                         [("id", "text"), ("amount_due", "decimal"), ("shipping_fee", "decimal"),
                          ("rate_pct", "decimal"), ("memo", "text")]),
                relation("erp.invoice_line", "id", [("id", "text"), ("invoice_id", "text")]),
            ],
            "links": [{"from": "erp.invoice_line", "to": "erp.invoice", "field": "invoice",
                       "columns": ["invoice_id"], "references": ["id"], "compares": ["text_id"],
                       "unique": False, "reverse": None, "description": "line -> invoice",
                       "evidence": []}],
        }

    def test_overrides_not_code_decide_lineage_identity_money_and_markings(self):
        empty = {"contracts": []}
        docs, report = gen.generate(self.facts(), empty, empty, {}, gen.load_overrides())
        # No lineage rule for the source: nothing is bound (fail closed).
        self.assertEqual((docs, sorted(report["excluded"])), ({}, ["erp.invoice", "erp.invoice_line"]))

        directory = _overrides_copy(self)
        _edit(directory / "lineage.json",
              lambda d: d["sources"].update(erp_test=[{"prefix": "erp.", "lineage": "fm_bronze"}]))
        _edit(directory / "money.json", lambda d: d["columns"].update(
            {"erp.invoice": {"amount_due": {"measure": "amount", "tax_basis": "incl_tax"}}}))
        docs, report = gen.generate(self.facts(), empty, empty, {}, gen.load_overrides(directory))
        [doc] = docs.values()
        types = {t["id"]: t for t in doc["object_types"]}
        invoice = {p["name"]: p for p in types["RelErpInvoice"]["properties"]}
        self.assertEqual((invoice["amount_due"]["value_type"], invoice["amount_due"]["markings"]),
                         ("money", ["finance"]))
        self.assertEqual(invoice["amount_due"]["sources"][0]["tax_basis"], "incl_tax")
        self.assertIn("currency_unknown", invoice["amount_due"]["sources"][0])
        self.assertEqual(invoice["shipping_fee"]["markings"], ["finance"])   # name rule
        for name in ("rate_pct", "memo", "id"):
            self.assertNotIn("markings", invoice[name], name)
        self.assertEqual([(k["tier"], k["unique"]) for k in types["RelErpInvoice"]["identity_keys"]],
                         [("verified", True)])
        self.assertEqual([(k["tier"], k["unique"])
                          for k in types["RelErpInvoiceLine"]["identity_keys"]], [("lead", False)])
        self.assertEqual(types["RelErpInvoice"]["bindings"][0]["source"], "erp_test")
        self.assertEqual([link["id"] for link in doc["link_types"]], ["RelErpInvoiceLine.invoice"])

        # A declared money relation or column the source does not have (a typo), or a
        # column it serves as other than a decimal (the name rules skip it, so it would
        # go unmarked), fails generation; a relation it has but does not serve now is
        # reported.
        spec = {"measure": "amount", "tax_basis": "unknown"}
        facts = self.facts()
        facts["relations"].append(dict(facts["relations"][0], name="erp.credit_note",
                                       excluded="not served by the source"))
        for relation, column in (("erp.invoices", "amount_due"), ("erp.invoice", "amount_owed"),
                                 ("erp.invoice", "memo"), ("erp.credit_note", "amount_due")):
            with self.subTest(f"{relation}.{column}"):
                _edit(directory / "money.json",
                      lambda d: d.update(columns={relation: {column: spec}}))
                if relation == "erp.credit_note":
                    _, report = gen.generate(facts, empty, empty, {},
                                             gen.load_overrides(directory))
                    self.assertEqual(report["money"][f"{relation}.{column}"],
                                     "not applied: not served by the source")
                    continue
                with self.assertRaises(gen.OverrideError):
                    gen.generate(facts, empty, empty, {}, gen.load_overrides(directory))


class PortalUnservedTests(unittest.TestCase):
    """The Portal adapter over a toy Portal and a toy registry: every relation a route reads
    is served or declared once as not_served with the ask, and the ask ranks them."""

    PRISMA = '''
model Order {
  id String @id
  @@map("app_order")
}

// a plain comment is not documentation
/// Append-only: UPDATE and DELETE are refused by trigger.
model User {
  id                  String    @id
  hashedPassword      String?   @map("hashed_password")
  /// Expires after one hour.
  resetTokenExpiresAt DateTime? @map("reset_token_expires_at") // not this one
  @@map("app_user")
}
'''

    @staticmethod
    def registry(routes):
        entries = [{"id": f"portal.prisma_model:{m}", "system": "portal", "kind": "prisma_model",
                    "name": m, "table": t, "evidence": ["plx-customer-portal:x@abcdef123456"]}
                   for m, t in (("Order", "app_order"), ("User", "app_user"))]
        entries += [{"id": f"portal.api_route:portal/src/app/api/r{n}", "system": "portal",
                     "kind": "api_route", "name": f"/api/r{n}", "models": models,
                     "relations": relations,
                     "evidence": [f"plx-customer-portal:portal/src/app/api/r{n}/route.ts"
                                  "@abcdef123456:3"]}
                    for n, (models, relations) in enumerate(routes)]
        return {"entries_digest": "sha256:toy", "entries": entries,
                "upstream": {"plx-customer-portal": {"commit": "abcdef1234567890"}}}

    def generate(self, routes, names=None, registry=None):
        catalog = {"format": ontology_schema.CATALOG_FORMAT, "evidence_mode": "upstream-manifest",
                   "source": {"repo": "plx-customer-portal", "commit": "abc1234"},
                   "relations": [{"name": "public.app_order", "schema": "public",
                                  "relation": "app_order", "domain": "orders", "columns": ["id"],
                                  "key": ["id"], "since": None, "unavailable": None}]}
        prisma = skel.prisma_facts(self.PRISMA, ["app_order"], "abc1234")
        overrides = dataclasses.replace(skel.OVERRIDES, money={}, names=names or {})
        empty = {"contracts": []}
        return skel.generate(catalog, prisma, empty, empty, {}, overrides,
                             registry=registry or self.registry(routes))

    def test_every_route_read_is_declared_once_and_ranked(self):
        for n in (1, 10, 60):
            with self.subTest(routes=n):
                routes = ([(["Order"], [f"public.fm_{i:02}"]) for i in range(n)] +
                          [(["Order", "User"], ["public.fm_00"]), (["Order"], [])])
                docs, coverage = self.generate(routes)
                wanted = {f"public.fm_{i:02}" for i in range(n)} | {"public.app_user"}
                self.assertEqual(set(coverage["not_served"]), wanted)
                types = {t["id"]: t for d in docs.values() for t in d["object_types"]
                         if t["availability"]["state"] == "not_served"}
                self.assertEqual(len(types), len(wanted))
                fm = types["RelPublicFm00"]
                self.assertEqual(fm["availability"]["upstream_ask"], skel.UPSTREAM_ASK)
                self.assertIn("read by 2 Portal API routes", fm["availability"]["reason"])
                self.assertEqual([(e["path"], e.get("lines")) for e in fm["evidence"][1:]],
                                 [("portal/src/app/api/r0/route.ts", "3"),
                                  (f"portal/src/app/api/r{n}/route.ts", "3")])
                ask = coverage["upstream_ask"]
                self.assertEqual(ask["routes"], {"with_data": n + 2, "fully_served": 1,
                                                 "need_unserved": n + 1})
                self.assertEqual(ask["relations_needed"], n + 1)
                ranked = ask["ranked"]
                listed = min(n + 1, skel.ASK_TOP)
                self.assertEqual(len(ranked), listed)
                self.assertEqual(len({r["relation"] for r in ranked}), listed)
                self.assertEqual([r["routes_completed_total"] for r in ranked],
                                 list(range(1, listed + 1)))
                # fm_00 completes its own route and opens the other to app_user alone.
                self.assertEqual((ranked[0]["relation"], ranked[0]["routes_reading"]),
                                 ("public.fm_00", 2))
                self.assertEqual(ranked[1]["relation"], "public.app_user")
                self.assertEqual(ranked[1]["credential_columns"], ["hashed_password"])
                self.assertEqual(ask["sensitive"], ["public.app_user"])
                # Upstream's /// notes on the unserved model and its columns reach its type.
                [user_prisma] = [e for e in types["RelPublicAppUser"]["evidence"]
                                 if e["path"] == "portal/prisma/schema.prisma"]
                self.assertEqual(user_prisma["note"], 'model User @@map("app_user"); /// '
                                 "Append-only: UPDATE and DELETE are refused by trigger.; "
                                 "reset_token_expires_at: /// Expires after one hour.")
                self.assertIsNone(ranked[0]["credential_columns"])   # no model: unknown
                self.assertEqual(ask["gaps"], {gap_id("coverage", key): key for key in (
                    "registry:portal.api_route:not_served",
                    "registry:portal.prisma_model:not_served")})
                # Registry data the adapter does not read (another kind, the whole-entries
                # digest) leaves the output as it is; a route's reads do not.
                other = self.registry(routes)
                other["entries"].append({"id": "plx-mc.mcp_tool:x", "system": "plx-mc",
                                         "kind": "mcp_tool", "name": "x", "evidence": []})
                other["entries_digest"] = "sha256:other"
                self.assertEqual(self.generate(routes, registry=other), (docs, coverage))
                moved = self.registry(routes[:-1] + [(["Order"], ["public.fm_new"])])
                self.assertNotEqual(self.generate(routes, registry=moved)[1]["inputs"]["registry"],
                                    coverage["inputs"]["registry"])

    def test_curated_names_keep_id_anchor_and_markings_and_fail_closed(self):
        name = {"object_type": "Formula", "note": "named by a process", "anchor": True,
                "markings": ["quality_controlled"]}
        docs, coverage = self.generate([(["Order"], ["public.fm_00"])],
                                       {"portal_api": {"public.fm_00": name}})
        [formula] = [t for d in docs.values() for t in d["object_types"] if t["id"] == "Formula"]
        self.assertEqual((formula["anchor"], formula["markings"], formula["bindings"]),
                         (True, ["quality_controlled"], []))
        self.assertEqual(coverage["not_served"]["public.fm_00"]["object_type"], "Formula")
        self.assertEqual(coverage["upstream_ask"]["ranked"][0]["object_type"], "Formula")
        # A name for a relation the source serves (or never declares) fails generation.
        for relation in ("public.app_order", "public.fm_typo"):
            with self.subTest(relation), self.assertRaises(gen.OverrideError):
                self.generate([(["Order"], ["public.fm_00"])],
                              {"portal_api": {relation: name}})


class CommittedCoverageTests(unittest.TestCase):
    """The committed registry, schema and coverage: no route stops at no_binding, and the
    ranked ask names declared not_served types and adds up."""

    def test_no_route_unbound_and_the_ask_adds_up(self):
        rows = source_registry.coverage(live=None)
        reasons = {r.get("reason") for r in rows if r["kind"] == "api_route"}
        self.assertNotIn("no_binding", reasons)
        self.assertIn("not_served", reasons)
        compiled = load_schema()
        coverage = json.loads(ontology_schema.COVERAGE_PATH.read_text(encoding="utf-8"))
        ask = coverage["upstream_ask"]
        self.assertEqual(ask["routes"]["with_data"],
                         ask["routes"]["fully_served"] + ask["routes"]["need_unserved"])
        self.assertEqual(sum(r["routes_completed"] for r in ask["ranked"]),
                         ask["ranked"][-1]["routes_completed_total"])
        for item in ask["ranked"]:
            availability = compiled.object_types[item["object_type"]]["availability"]
            self.assertEqual((availability["state"], availability["upstream_ask"]),
                             ("not_served", skel.UPSTREAM_ASK), item["relation"])
        # The curated not_served ids are the generated declarations of their tables, and a
        # curated access decision (QualityRecord's marking) survives the fold.
        for tid in ("PickList", "Formulation", "BillOfMaterials", "QualityRecord"):
            self.assertEqual(compiled.origin_of_type[tid], "generated", tid)
        self.assertEqual(compiled.object_types["QualityRecord"]["markings"], ["quality_controlled"])
        self.assertTrue(compiled.object_types["PickList"]["anchor"])


class SyntheticReadersTests(unittest.TestCase):
    """Synthetic rows of the real schema, projected, read by two readers of every audience:
    one holds finance and pii, the other every marking but those. The item gate is the
    fixture's (``fixture.is_synthetic``: synthetic handles only), so this proves marking
    enforcement, never an item ACL; ``ProductionItemGateTests`` reads through the
    production mail and SharePoint gates. Generated types are read from the generated
    files alone too: a curated binding shadows the raw type of its relation, and the
    curated quote types shadow every generated money column a currency comes with."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        generated_root = Path(cls.tmp.name) / "schema"
        # The Portal skeleton (the generated BC files bind through curated BC types).
        shutil.copytree(SCHEMA_DIR / "generated", generated_root / "generated",
                        ignore=lambda _d, names: [n for n in names if n.startswith("bc_")])
        shutil.copy(SCHEMA_DIR / "identifiers.json", generated_root)
        cls.stores = {}
        for generated, schema in ((False, load_schema()),
                                  (True, load_schema(generated_root, check_format=False))):
            path = Path(cls.tmp.name) / f"store-{generated}.sqlite"
            build_store(path, schema, fixture.sources(schema, fixture.min_rows(schema)),
                        clock=lambda: AT)
            cls.stores[generated] = (schema, ot.StoreReader.open(path))

    @classmethod
    def tearDownClass(cls):
        for _schema, reader in cls.stores.values():
            reader.conn.close()
        cls.tmp.cleanup()

    def tools(self, markings, generated):
        schema, reader = self.stores[generated]
        who = frozenset({"reader"})
        policy = ot.ReadPolicy("plx", {k: who for k in reader.audience_keys()},
                               {m: who for m in markings})
        base = ot.OntologyTools(reader, policy, schema=ot.SchemaView.from_compiled(schema),
                                grammar=ot.default_grammar(), clock=lambda: AT,
                                item_gate=lambda _actor, handle: fixture.is_synthetic(handle))
        return base, Actor("plx", who)

    def marked(self, marking, generated):
        """The first property marked only ``marking`` on an unmarked type (generated
        types: bound by the generator, ``gen.<relation>``); a finance one is money with a
        currency source, so a holder can sum it."""
        schema = self.stores[generated][0]
        return next((tid, p["name"], p["sources"][0]["binding"])
                    for tid, t in sorted(schema.object_types.items()) if not t.get("markings")
                    for p in t["properties"] if p.get("markings") == [marking]
                    and p["sources"][0]["binding"].startswith("gen.") == generated
                    and (marking != "finance" or "currency_from" in p["sources"][0]))

    def test_marked_values_reach_only_the_holder(self):
        for marking, generated in (("finance", True), ("pii", False)):
            holder, holder_actor = self.tools(HELD, generated)
            other, other_actor = self.tools(set(store_ddl.MARKINGS) - HELD, generated)
            tid, prop, bid = self.marked(marking, generated)
            with self.subTest(f"{tid}.{prop}"):
                find = {"object_type": tid, "filters": [{"property": prop, "op": "present"}],
                        "limit": 50}
                found = holder.call("plx.find", find, holder_actor)
                ids = [c["object"]["object_id"] for c in found["candidates"]
                       if c["object"]["object_id"].split(":", 2)[1] == bid]
                self.assertTrue(ids, found)
                read = holder.call("plx.object", {"object_id": ids[0]}, holder_actor)
                self.assertIn(prop, {v["property"] for v in read["values"]})
                self.assertNotIn("withheld", read)

                denied = other.call("plx.find", find, other_actor)
                self.assertEqual(denied["candidates"], [])
                self.assertIn("marking_withheld", {g["reason"] for g in denied["gaps"]})
                read = other.call("plx.object", {"object_id": ids[0]}, other_actor)
                self.assertNotIn(prop, {v["property"] for v in read["values"]})
                self.assertEqual(read["withheld"]["reason"], "marking")
                if marking == "finance":   # a sum of the generated money column
                    sums = []
                    for base, actor in ((holder, holder_actor), (other, other_actor)):
                        out = oa.AggregateTools(base).call("plx.aggregate", {
                            "set": base.sets.new(actor, ids),
                            "measure": {"property": prop, "op": "sum"}}, actor)
                        sums.append(out)
                    self.assertIsNotNone(sums[0]["groups"][0]["value"])
                    self.assertIsNone(sums[1]["groups"][0]["value"])
                    self.assertIn("marking_withheld", {g["reason"] for g in sums[1]["gaps"]})


class ProductionItemGateTests(unittest.TestCase):
    """Two readers holding every audience and marking, through the production item gates
    (``MailCaptureStore.item_gate``, ``SharePointCaptureStore.item_gate``) over one store:
    synthetic rows for every family but SharePoint, whose files were captured with their
    item permissions."""

    def test_item_acls_split_real_files_and_grant_no_synthetic_record(self):
        case = sp.CaptureCase("run")
        case.setUp()
        self.addCleanup(case.tearDown)
        self.assertTrue(case.capture().run(sp.scope())["complete"])
        at = dt.datetime(2026, 10, 2, tzinfo=dt.timezone.utc)   # after the capture
        schema = load_schema()
        path = case.state.parent / "ontology.sqlite"
        sources = fixture.sources(schema, fixture.min_rows(schema))
        with SharePointCaptures(case.state) as files:
            build_store(path, schema, {**sources, "sharepoint": files}, clock=lambda: at)
        reader = ot.StoreReader.open(path)
        self.addCleanup(reader.conn.close)
        mail = MailCaptureStore(case.state.parent / "mail")
        self.addCleanup(mail.close)
        mail_gate = mail.item_gate(LiveSourceAudience(
            {"mail:*": ["staff:quality", "staff:sales"]}, tenant="plx"))
        files_gate = case.store.item_gate(sp.reader_audience())
        who = frozenset({"staff:quality", "staff:sales"})
        policy = ot.ReadPolicy("plx", {k: who for k in reader.audience_keys()},
                               {m: who for m in store_ddl.MARKINGS})

        def tools(gate):
            return ot.OntologyTools(reader, policy, schema=ot.SchemaView.from_compiled(schema),
                                    grammar=ot.default_grammar(), clock=lambda: at,
                                    item_gate=gate)
        production = tools(lambda actor, handle: (
            files_gate if handle.startswith("sharepoint://") else mail_gate)(actor, handle))
        synthetic = tools(lambda _actor, handle: fixture.is_synthetic(handle))

        files = {"object_type": "SharePointFile",
                 "filters": [{"property": "name", "op": "present"}], "limit": 50}
        quotes = {"object_type": "ForwarderQuote",
                  "filters": [{"property": "amount", "op": "present"}], "limit": 50}
        for actor, names in ((sp.QUALITY, {"AO packet PO100234.pdf", "release.txt",
                                           sp.NAME_SECRET}),
                             (sp.EMPLOYEE, {"release.txt"})):
            with self.subTest(actor=sorted(actor.principals)):
                found = production.call("plx.find", files, actor)
                self.assertEqual({c["object"]["display"]["untrusted_text"]
                                  for c in found["candidates"]}, names)
                # A synthetic mail quote (finance-marked amount) is granted only by the
                # fixture gate: the production gate shows it to neither reader.
                [granted] = synthetic.call("plx.find", quotes, actor)["candidates"]
                self.assertEqual(production.call("plx.find", quotes, actor)["candidates"], [])
                read = production.call("plx.object",
                                       {"object_id": granted["object"]["object_id"]}, actor)
                self.assertEqual(read["error"]["code"], "object_not_found")


if __name__ == "__main__":
    unittest.main()
