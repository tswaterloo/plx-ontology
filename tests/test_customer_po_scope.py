"""A customer PO is unique only per customer (identifiers.json ``identity_scope``).

The Portal matches a PO only with the order's customer and counts two live hits as no
match (plx-customer-portal@40f7802 order-identity.ts:resolveOrderByNumber:125-149). Here:
plx.find roots a customer PO only within its one customer, a PO without a customer or
under two customers is a typed lead, the case path never opens on such a lead (nor on a
case or spacing variant), identity review never spans two customers, a restricted reader
learns nothing from a record it may not read, and the link contracts cite the pinned
upstream commits. One root gate (``roots_exactly``): find_root, the case path and
plx.investigate give every candidate one verdict, and plx.investigate's twin roots
(``_root_twins``) never join two customers' POs.

Evidence label: ``isolated-fixture``. The compiled repository schema and identifier
grammar over a projection of SYNTHETIC Portal rows; no network. The Portal-and-BC
worlds add BC rows through the BC fake and the real GET-only capture
(injected-transport).
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import types
import unittest

from ontology import identifiers
from ontology.project import CapturedRecord, build_store
from ontology.schema import load_schema
from ontology.types import Actor
from operating import case_workspace_live as live_ws
from operating import ontology_tools as ot
from operating import ontology_tools_evidence as ev
from operating import ontology_tools_investigate as oi
from operating import ontology_tools_traverse as tt

ROOT = Path(__file__).resolve().parents[1]
CPO = "CustomerPurchaseOrder"
SALES = Actor("plx", frozenset({"staff:sales"}), "restricted")
POLICY = ot.ReadPolicy("plx", audiences={"api:*": {"staff:sales"}})


def _so(ident, po, customer, status="CONFIRMED", **fields):
    return {"id": ident, "order_number": f"SO{ident[3:]:0>6}", "po_reference": po,
            "customer_id": customer, "status": status,
            "updated_at": "2026-09-28T08:00:00Z", **fields}


ROWS = [_so("so-1", "PO-4471", "c-1"),
        # One PO number used by two customers (Portal TASK-2401).
        _so("so-2", "PO-5000", "c-1"), _so("so-3", "PO-5000", "c-2"),
        _so("so-4", "PO-6000", None),          # a PO without a customer
        _so("so-5", "po  7000", "c-3"),        # stored in another case and spacing
        # One customer reusing one PO on two orders (a Portal warning, not a constraint).
        _so("so-6", "PO-8000", "c-1"), _so("so-7", "PO-8000", "c-1")]


def _handle(ident):
    key = json.dumps({"id": ident}, separators=(",", ":"))
    return f"api://public.app_sales_order/{key}@1"


def _record(row, relation="public.app_sales_order", key_field="id"):
    key = json.dumps({key_field: row[key_field]}, separators=(",", ":"))
    return CapturedRecord("portal_api", relation, key, row, f"api://{relation}/{key}@1", "row",
                          "d" * 64, "2026-09-28T08:00:00.000Z", None,
                          "2026-09-28T08:00:00.000Z", f"api:{relation}")


class _Rows:
    source = "portal_api"

    def __init__(self, rows, others=()):
        """``rows`` of app_sales_order; ``others``: (relation, key field, row)."""
        self.rows = [_record(row) for row in rows] + [_record(row, relation, key)
                                                      for relation, key, row in others]

    def records(self, relations):
        return {r: [x for x in self.rows if x.relation == r] for r in relations}

    def describe(self):
        return {"generation": "g", "relations": {}}


def _oid(ident):
    return f'{CPO}:portal.app_sales_order.po_reference:{{"id":"{ident}"}}'


def _features(candidate):
    return {f["feature"]: f["agrees"] for f in candidate.get("why_not", ())}


class ScopeCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = load_schema()
        cls.view = ot.SchemaView.from_compiled(cls.schema)
        cls.contracts = ot.load_contracts()

    def tools(self, rows=ROWS, finance=(), others=()):
        """Tools over a projection of ``rows`` (and ``others``, ``_Rows``); the values of
        the ``finance`` properties and every row of the ``finance`` objects (ids or
        properties) carry the finance marking, which SALES does not hold."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "store.sqlite"
        build_store(path, self.schema, {"portal_api": _Rows(rows, others)},
                    normalizer=identifiers.normalize)
        self.store_path = path
        with sqlite3.connect(path) as conn:
            for name in finance:
                column = "object_id" if ":" in name else "property"
                for table in ("object_versions", "object_values", "identifiers"):
                    if column == "object_id" or table == "object_values":
                        conn.execute(f"UPDATE {table} SET markings = '[\"finance\"]' "
                                     f"WHERE {column} = ?", (name,))
        reader = ot.StoreReader.open(path)
        self.addCleanup(reader.conn.close)
        return ot.OntologyTools(reader, POLICY, schema=self.view, grammar=identifiers,
                                contracts=self.contracts)

    def find(self, tools, query):
        out = tools.call("plx.find", {"query": query, "object_type": CPO}, SALES)
        self.assertNotIn("error", out, out)
        return out

    def opener(self, tools, object_type=CPO):
        """The case path's root step (``CaseOpener._find``) over these tools."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        opener = live_ws.CaseOpener(types.SimpleNamespace(client_configured=False), SALES,
                                    temp.name, audience=types.SimpleNamespace(
                                        allows=lambda *_: True))
        return lambda value: opener._find(
            lambda name, arguments: tools.call(name, arguments, SALES), value, object_type)


class FindScopeTests(ScopeCase):
    def test_one_customers_po_matches_exactly_and_roots_the_case(self):
        tools = self.tools()
        out = self.find(tools, "PO-4471")
        self.assertEqual(out["status"], "found", out)
        (candidate,) = out["candidates"]
        self.assertIn(candidate["reason"], ot.ROOT_REASONS)
        (scope,) = [f for f in candidate["why_not"] if f["feature"] == ot.SCOPE_FEATURE]
        self.assertIs(scope["agrees"], True)
        # The scope is the carrier's scope_field from the registry, cited from the row.
        self.assertEqual((scope["citations"][0]["l1_handle"],
                          scope["citations"][0]["field_path"]),
                         (_handle("so-1"), "row.customer_id"))
        self.assertTrue(ot.roots_exactly(candidate, "PO-4471", identifiers))
        self.assertEqual(self.opener(tools)("PO-4471")["object_id"], _oid("so-1"))
        # Asked as a sales order, the order carrying it roots through its link.
        self.assertEqual(self.opener(tools, "SalesOrder")("PO-4471")["object_id"],
                         'SalesOrder:portal.app_sales_order:{"id":"so-1"}')

    def test_po_under_two_customers_has_no_exact_match(self):
        tools = self.tools()
        out = self.find(tools, "PO-5000")
        self.assertEqual(out["status"], "ambiguous", out)
        self.assertEqual({c["object"]["object_id"] for c in out["candidates"]},
                         {_oid("so-2"), _oid("so-3")})
        for candidate in out["candidates"]:
            self.assertEqual((candidate["reason"], candidate["tier"]),
                             (ot.SCOPE_LEAD_REASON, "lead"))
            self.assertIs(_features(candidate)["identity_scope_ambiguous"], False)
            self.assertFalse(ot.roots_exactly(candidate, "PO-5000", identifiers))
        (gap,) = [g for g in out["gaps"] if g["reason"] == "identity_scope_ambiguous"]
        self.assertTrue(gap["detail"].startswith("2 "), gap)
        # The orders carrying it are candidates through their link, leads saying why.
        orders = tools.call("plx.find", {"query": "PO-5000", "object_type": "SalesOrder"},
                            SALES)
        self.assertEqual(orders["status"], "ambiguous", orders)
        for candidate in orders["candidates"]:
            self.assertIs(_features(candidate)["identity_scope_ambiguous"], False)
            self.assertFalse(ot.roots_exactly(candidate, "PO-5000", identifiers))
        with self.assertRaises(live_ws.OpenError) as refused:
            self.opener(tools)("PO-5000")
        self.assertEqual(refused.exception.code, "ambiguous_identifier")

    def test_po_without_a_customer_is_a_typed_lead_never_a_root(self):
        tools = self.tools()
        out = self.find(tools, "PO-6000")
        (candidate,) = out["candidates"]
        self.assertEqual((candidate["reason"], candidate["tier"]),
                         (ot.SCOPE_LEAD_REASON, "lead"))
        self.assertIsNone(_features(candidate)["no_scope_value"])
        self.assertFalse(ot.roots_exactly(candidate, "PO-6000", identifiers))
        for object_type in (CPO, "SalesOrder"):
            with self.assertRaises(live_ws.OpenError) as refused:
                self.opener(tools, object_type)("PO-6000")
            self.assertEqual(refused.exception.code, "root_lead_only:no_scope_value")

    def test_case_or_spacing_variant_is_a_lead_the_literal_text_roots(self):
        tools = self.tools()
        variant = self.find(tools, "PO 7000")
        (candidate,) = variant["candidates"]
        # Found within its one customer, but only after normalisation: never a root.
        self.assertIs(_features(candidate)[ot.SCOPE_FEATURE], True)
        self.assertFalse(ot.roots_exactly(candidate, "PO 7000", identifiers))
        with self.assertRaises(live_ws.OpenError) as refused:
            self.opener(tools)("PO 7000")
        self.assertEqual(refused.exception.code, "root_lead_only:normalized_only")
        self.assertEqual(self.opener(tools)(" po  7000 ")["object_id"], _oid("so-5"))

    def test_marked_forms_resolve_to_the_customer_po_and_never_root(self):
        # The registry's marked form (identifiers.json customer_po marker): plx.find reads
        # it as the customer PO it marks, a typed lead, so the one scope gate refuses it.
        tools = self.tools()
        for query, ident in (("PO#PO-6000", "so-4"), ("PO #PO-6000", "so-4"),
                             ("PO# 7000", "so-5"), ("PO#PO-4471", "so-1")):
            with self.subTest(query=query):
                out = self.find(tools, query)
                (candidate,) = out["candidates"]
                self.assertEqual(candidate["object"]["object_id"], _oid(ident))
                self.assertEqual((candidate["reason"], candidate["tier"],
                                  candidate["matched"]["identifier_kind"]),
                                 (ot.MARKED_REASON, "lead", "customer_po"))
                self.assertEqual(ot.scoped_root_lead(candidate, query, identifiers),
                                 ot.MARKED_REASON)
                with self.assertRaises(live_ws.OpenError) as refused:
                    self.opener(tools)(query)
                self.assertEqual(refused.exception.code, "root_lead_only:marked_form")
        # Under two customers the marked form is ambiguous, never a pick.
        self.assertEqual(self.find(tools, "PO#PO-5000")["status"], "ambiguous")

    def test_a_match_on_the_po_carrier_is_typed_whatever_the_spelling(self):
        # Display text (or a filter) matching the registry's customer_po carrier field
        # (identifiers.json carriers: app_sales_order.po_reference) is typed customer_po,
        # so the scope gate refuses it: no spelling list, no display_text gate.
        tools = self.tools()
        for query, ident, why in (("PO 6000", "so-4", "no_scope_value"),
                                  ("po 6000", "so-4", "no_scope_value"),
                                  ("PO#6000", "so-4", "no_scope_value"),
                                  ("PO # 6000", "so-4", "no_scope_value"),
                                  ("#PO-6000", "so-4", "no_scope_value"),
                                  ("7000", "so-5", "normalized_only")):
            with self.subTest(query=query):
                (candidate,) = self.find(tools, query)["candidates"]
                self.assertEqual((candidate["object"]["object_id"],
                                  candidate["matched"]["identifier_kind"]),
                                 (_oid(ident), "customer_po"))
                self.assertEqual(ot.scoped_root_lead(candidate, query, identifiers), why)
                with self.assertRaises(live_ws.OpenError) as refused:
                    self.opener(tools)(query)
                self.assertEqual(refused.exception.code, f"root_lead_only:{why}")
        listed = tools.call("plx.find", {"object_type": CPO, "filters": [
            {"property": "po_number", "op": "eq", "value": "PO-6000"}]}, SALES)
        (candidate,) = listed["candidates"]
        self.assertEqual(ot.scoped_root_lead(candidate, "PO-6000", identifiers),
                         "no_scope_value")
        self.assertEqual(self.opener(tools)("PO-4471")["object_id"], _oid("so-1"))

    def test_cancelled_order_never_makes_the_po_ambiguous(self):
        # The Portal skips a cancelled order (status <> 'CANCELLED'): another customer's
        # cancelled order with the PO leaves the live one the root.
        rows = [_so("so-1", "PO-4471", "c-1"), _so("so-9", "PO-4471", "c-9", "CANCELLED")]
        tools = self.tools(rows=rows)
        out = self.find(tools, "PO-4471")
        self.assertEqual(out["status"], "found", out)
        self.assertNotIn("identity_scope_ambiguous", {g["reason"] for g in out["gaps"]})
        (candidate,) = out["candidates"]
        self.assertTrue(ot.roots_exactly(candidate, "PO-4471", identifiers))
        # The cancelled order rides on the root as a related lead, never a candidate.
        self.assertIsNone(_features(candidate)["related_scope_lead"])
        self.assertEqual(self.opener(tools)("PO-4471")["object_id"], _oid("so-1"))
        alone = self.find(self.tools(rows=rows[1:]), "PO-4471")
        self.assertIsNone(_features(alone["candidates"][0])["scope_status_excluded"])
        # Both live: two customers, no root.
        live = self.tools(rows=[rows[0], _so("so-9", "PO-4471", "c-9")])
        self.assertEqual(self.find(live, "PO-4471")["status"], "ambiguous")

    def test_restricted_reader_answer_equals_the_store_without_the_denied_record(self):
        denied = self.tools(finance=[_oid("so-3")])
        without = self.tools(rows=[r for r in ROWS if r["id"] != "so-3"])
        answers = [self.find(tools, "PO-5000") for tools in (denied, without)]
        for out in answers:
            self.assertEqual(out["status"], "found", out)
            self.assertNotIn("identity_scope_ambiguous", {g["reason"] for g in out["gaps"]})
            self.assertTrue(ot.roots_exactly(out["candidates"][0], "PO-5000", identifiers))
        self.assertEqual(*[(out["candidates"], out["gaps"]) for out in answers])

    def test_withheld_scope_value_is_a_lead_and_never_read(self):
        # The projection marks values per property; a reader lacking that marking
        # learns only that the scope is withheld, never the customer.
        tools = self.tools(finance=["customer_ref"])
        out = self.find(tools, "PO-4471")
        (candidate,) = out["candidates"]
        self.assertEqual(candidate["reason"], ot.SCOPE_LEAD_REASON)
        self.assertIsNone(_features(candidate)["scope_value_withheld"])
        self.assertNotIn("c-1", json.dumps(out))

    def test_cardinality_one_ten_sixty_customers_of_one_po(self):
        for n in (1, 10, 60):
            with self.subTest(customers=n):
                rows = [_so(f"so-{i}", "PO-9100", f"c-{i}") for i in range(n)]
                tools = self.tools(rows=rows)
                start = time.monotonic()
                out = tools.call("plx.find", {"query": "PO-9100", "object_type": CPO,
                                              "limit": 50}, SALES)
                self.assertLess(time.monotonic() - start, 5.0)
                self.assertEqual(out["status"], "found" if n == 1 else "ambiguous")
                self.assertEqual(out["completeness"]["total"], n)
                ids = [c["object"]["object_id"] for c in out["candidates"]]
                self.assertEqual(len(ids), len(set(ids)))
                self.assertEqual(sum(ot.roots_exactly(c, "PO-9100", identifiers)
                                     for c in out["candidates"]), 1 if n == 1 else 0)
                # The carrier typed by display text: every record, none rooting.
                out = tools.call("plx.find", {"query": "PO 9100", "object_type": CPO,
                                              "limit": 50}, SALES)
                self.assertEqual(out["completeness"]["total"], n)
                self.assertEqual({ot.scoped_root_lead(c, "PO 9100", identifiers)
                                  for c in out["candidates"]},
                                 {"normalized_only" if n == 1 else "identity_scope_ambiguous"})


def _investigate(test, tools, family, identifier, actor=SALES, kind=None):
    table: dict = {}
    ot.register(table, tools)
    tt.register(table, tt.TraverseTools(tools))
    oi.register(table, oi.InvestigateTools(table, schema=tools.schema))
    root = {"identifier": identifier, **({"identifier_kind": kind} if kind else {})}
    out = table["plx.investigate"]["handler"]({"family": family, "root": root}, actor)
    test.assertNotIn("error", out, out)
    return out


class InvestigateRootTests(ScopeCase):
    def investigate(self, tools, family, identifier):
        return _investigate(self, tools, family, identifier)

    def test_plans_never_root_on_a_customer_po_lead(self):
        tools = self.tools()
        for family in ("F2", "decision_evidence"):
            for identifier, why in (("PO-6000", "no_scope_value"),
                                    ("PO 7000", "normalized_only"),
                                    ("PO#PO-6000", "marked_form"),
                                    ("PO #PO-6000", "marked_form"),
                                    ("PO# 7000", "marked_form"),
                                    ("PO 6000", "no_scope_value"),
                                    ("PO#6000", "no_scope_value"),
                                    ("#PO-6000", "no_scope_value"),
                                    ("7000", "normalized_only")):
                with self.subTest(family=family, identifier=identifier):
                    out = self.investigate(tools, family, identifier)
                    self.assertEqual(out["answerability"]["state"], "blocked", out)
                    self.assertIn("root_lead_only", out["answerability"]["reasons"])
                    (gap,) = [g for g in out["gaps"] if g["reason"] == "root_lead_only"]
                    self.assertTrue(gap["detail"].startswith(why + ":"), gap)
                    self.assertNotIn("root", out)
            with self.subTest(family=family, identifier="PO-4471"):
                out = self.investigate(tools, family, "PO-4471")
                self.assertNotIn("root_lead_only", out["answerability"]["reasons"])
                self.assertEqual(out["root"]["object_id"], _oid("so-1"))


class OneRootGateTests(ScopeCase):
    """roots_exactly is the one root gate: find_root and the find eval call it, and the
    case path (CaseOpener) and plx.investigate (``scoped_root_lead``) give a scoped
    kind's candidate the same verdict."""

    def verdicts(self, tools, query, object_type=CPO, family="decision_evidence"):
        """(candidate, roots_exactly, the case path's root or refusal, plx.investigate's
        root or refusal) for ``query``."""
        out = tools.call("plx.find", {"query": query, "object_type": object_type}, SALES)
        (candidate,) = out["candidates"]
        try:
            opened = self.opener(tools, object_type)(query)["object_id"]
        except live_ws.OpenError as refused:
            opened = refused.code
        answer = _investigate(self, tools, family, query)
        planned = (answer["root"]["object_id"] if "root" in answer else
                   next(g["detail"].split(":", 1)[0] for g in answer["gaps"]
                        if g["reason"] == "root_lead_only"))
        return candidate, ot.roots_exactly(candidate, query, identifiers), opened, planned

    def test_a_carrier_display_text_match_on_the_literal_po_roots_nowhere(self):
        # A reader without the PO identifier's marking still sees the display text: the
        # carrier-typed display_text match holds the literal PO within its one customer,
        # yet it is a lead in all three (before: the case path and plans rooted it).
        tools = self.tools()
        with sqlite3.connect(self.store_path) as conn:
            conn.execute("UPDATE identifiers SET markings = '[\"finance\"]' "
                         "WHERE property = 'po_number'")
        candidate, exact, opened, planned = self.verdicts(tools, "PO-4471")
        self.assertEqual((candidate["reason"], candidate["matched"]["identifier_kind"],
                          candidate["matched"]["raw"]["untrusted_text"]),
                         ("display_text", "customer_po", "PO-4471"))
        self.assertIs(_features(candidate)[ot.SCOPE_FEATURE], True)
        self.assertEqual((exact, opened, planned),
                         (False, "root_lead_only:display_text", "display_text"))
        # A filter on the carrier is the same lead.
        (listed,) = self.tools().call("plx.find", {"object_type": CPO, "filters": [
            {"property": "po_number", "op": "eq", "value": "PO-4471"}]}, SALES)["candidates"]
        self.assertEqual((listed["reason"], ot.roots_exactly(listed, "PO-4471", identifiers),
                          ot.scoped_root_lead(listed, "PO-4471", identifiers)),
                         ("filter_match", False, "filter_match"))

    def test_a_linked_po_gets_one_verdict(self):
        tools = self.tools()
        order = 'SalesOrder:portal.app_sales_order:{"id":"so-1"}'
        candidate, exact, opened, planned = self.verdicts(
            tools, "PO-4471", "SalesOrder", "order_quantities")
        self.assertEqual(candidate["reason"], ot.LINKED_REASON)
        self.assertEqual((exact, opened, planned), (True, order, order))
        # Linked from a customerless PO: a lead in all three.
        candidate, exact, opened, planned = self.verdicts(
            tools, "PO-6000", "SalesOrder", "order_quantities")
        self.assertEqual(candidate["reason"], ot.LINKED_REASON)
        self.assertEqual((exact, opened, planned),
                         (False, "root_lead_only:no_scope_value", "no_scope_value"))


class TwinRootScopeTests(ScopeCase):
    """plx.investigate's twin roots (``_root_twins``): a PO held on the Portal order and on
    a BC order is one record's twins only within one customer, and a Sage-loaded order's
    header is its twin only through the verified same-record link (linked_alias)."""

    def world(self, bc_customer, readable_bc=("salesOrders", "customers")):
        """A Portal order (customer c-1, Portal customer C-1 in BC) and a BC order of
        ``bc_customer`` both holding PO-4471; the reader holds ``readable_bc`` sets."""
        from ontology import store_ddl
        from ontology.project_bc import BcCaptures
        from ontology.tests import test_project_bc as pb
        fake = pb.purchase_world()
        fake.data[(pb.MAN, "salesOrders")] = [pb.record(
            pb.gid(1, 21), number="SO-1", externalDocumentNumber="PO-4471",
            customerNumber=bc_customer, status="Open", orderDate="2026-09-01")]
        fake.data[(pb.MAN, "customers")] = [
            pb.record(pb.gid(1, 22), number="C-1", displayName="Acme"),
            pb.record(pb.gid(1, 23), number="C-9", displayName="Other")]
        harness = pb.tb.Harness(self, fake)
        harness.run(entity_sets=pb.CAPTURE_ENTITY_SETS)
        portal = _Rows([_so("so-1", "PO-4471", "c-1", custom_fields={"source": "fm-oe-ensure"})],
                       [("public.app_customer", "id", {
                           "id": "c-1", "name": "Acme", "code": "AC", "erp_code": "C-1",
                           "external_id": "sage-cust-C-1",
                           "updated_at": "2026-09-28T08:00:00Z"})])
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "store.sqlite"
        with BcCaptures(harness.db) as bc:
            build_store(path, self.schema, {"bc": bc, "portal_api": portal},
                        normalizer=identifiers.normalize)
        reader = ot.StoreReader.open(path)
        self.addCleanup(reader.conn.close)
        audiences = {"api:*": {pb.READER}}
        audiences.update({f"bc:{company}/{name}": {pb.READER} for name in readable_bc
                          for company in (pb.MAN, pb.DIST)})
        policy = ot.ReadPolicy("plx", audiences=audiences,
                               markings={m: {pb.READER} for m in store_ddl.MARKINGS})
        self.bc_customers = (pb.gid(1, 22), pb.gid(1, 23))
        return ot.OntologyTools(reader, policy, schema=self.view, grammar=identifiers,
                                contracts=self.contracts), pb.FULL

    def test_a_po_in_two_customers_is_never_a_twin_root(self):
        portal, both = _oid("so-1"), ("salesOrders", "customers")
        # Asked as BC's alias kind (unscoped), the matches are still customer POs.
        for bc_customer, readable, twins, kind in (("C-9", both, False, None),
                                                   ("C-9", both, False,
                                                    "bc_external_document_no"),
                                                   ("C-1", both, True, None),
                                                   ("C-1", ("salesOrders",), False, None)):
            with self.subTest(bc_customer=bc_customer, readable=readable, kind=kind):
                tools, actor = self.world(bc_customer, readable)
                found = tools.call("plx.find", {"query": "PO-4471", "object_type": CPO}, actor)
                self.assertEqual(found["status"], "ambiguous", found)
                self.assertTrue(all(ot.roots_exactly(c, "PO-4471", identifiers)
                                    for c in found["candidates"]))
                out = _investigate(self, tools, "decision_evidence", "PO-4471", actor, kind)
                reasons = {g["reason"] for g in out["gaps"]}
                if twins:
                    self.assertEqual(out["root"]["object_id"], portal)
                    self.assertIn("root_twins_not_roots", reasons)
                else:
                    # Another customer, or one this reader cannot see: never joined.
                    self.assertNotIn("root", out)
                    self.assertIn("ambiguous_root", out["answerability"]["reasons"])
                    self.assertNotIn("root_twins_not_roots", reasons)
                    if "customers" not in readable:
                        self.assertFalse([c for c in self.bc_customers if c in json.dumps(out)])

    def test_a_sage_loaded_order_roots_through_its_verified_sage_twin(self):
        header = {"orduniq": "7001", "ordnumber": "SO000001", "customer": "C-1",
                  "ponumber": "PO-1", "orddate": "2026-09-01"}
        order = 'SalesOrder:portal.app_sales_order:{"id":"so-1"}'
        sage = 'SalesOrder:portal.sage_oe_order_header:{"orduniq":"7001"}'
        for custom, twin in (({"source": "sage.oe_order_header", "sageSourceOrdUniq": "7001"},
                              True),
                             ({"source": "fm-oe-ensure"}, False)):
            with self.subTest(linked=twin):
                tools = self.tools(rows=[_so("so-1", "PO-1", "c-1", custom_fields=custom)],
                                   others=[("sage.oe_order_header", "orduniq", header)])
                found = tools.call("plx.find", {"query": "SO000001", "object_type": "SalesOrder"},
                                   SALES)
                self.assertEqual({c["object"]["object_id"] for c in found["candidates"]},
                                 {order, sage}, found)
                out = _investigate(self, tools, "order_quantities", "SO000001")
                if twin:
                    self.assertEqual(out["root"]["object_id"], order)
                    (gap,) = [g for g in out["gaps"] if g["reason"] == "root_twins_not_roots"]
                    self.assertIn("verified same-record link", gap["detail"])
                else:
                    # A same-number header no verified link joins is another order.
                    self.assertNotIn("root", out)
                    self.assertIn("ambiguous_root", out["answerability"]["reasons"])


class IdentityReviewTests(ScopeCase):
    def near_misses(self, tools, ident):
        out = ev.EvidenceTools(tools).call("plx.identity", {
            "object_ids": [_oid(ident)], "include_near_misses": True}, SALES)
        self.assertNotIn("error", out, out)
        return out

    def test_review_candidates_never_span_two_customers(self):
        tools = self.tools()
        two = self.near_misses(tools, "so-2")
        (miss,) = two["near_misses"]
        self.assertEqual(miss["object"]["object_id"], _oid("so-3"))
        self.assertIn({"feature": ot.SCOPE_FEATURE, "agrees": False}, miss["failed"])
        self.assertNotIn("ambiguous_identity", {g["reason"] for g in two["gaps"]})
        # One customer's reused PO is still an identity question for a person.
        reused = self.near_misses(tools, "so-6")
        self.assertIn("ambiguous_identity", {g["reason"] for g in reused["gaps"]})
        self.assertNotIn(ot.SCOPE_FEATURE,
                         {f["feature"] for m in reused["near_misses"] for f in m["failed"]})

    def test_a_record_without_a_customer_stays_an_identity_question(self):
        # Its customer is unknown, so nothing keeps it apart: a person decides.
        tools = self.tools(rows=ROWS + [_so("so-8", "PO-6000", "c-1")])
        out = self.near_misses(tools, "so-8")
        (miss,) = out["near_misses"]
        self.assertEqual(miss["object"]["object_id"], _oid("so-4"))
        self.assertIn({"feature": ot.SCOPE_FEATURE, "agrees": None}, miss["failed"])
        self.assertIn("ambiguous_identity", {g["reason"] for g in out["gaps"]})


class BcCompanyScopeTests(unittest.TestCase):
    """BC customer numbers repeat across companies: the BC scope is company plus
    customerNumber (identifiers.json scope_within). Evidence label: injected-transport
    (the BC fake through the real GET-only capture, ``ontology/tests/test_project_bc``)."""

    def world(self, companies):
        from ontology.tests import test_project_bc as pb
        fake = pb.purchase_world()
        for c, company in companies:
            fake.data[(company, "salesOrders")] = [pb.record(
                pb.gid(c, 21), number="SO-1", externalDocumentNumber="CPO-77",
                customerNumber="C-1", status="Open", orderDate="2026-09-01")]
        w = pb.BcWorld(self, fake)
        oid = lambda c: ('CustomerPurchaseOrder:bc.sales_orders.external_document_number:'
                         '{"id":"%s"}' % pb.gid(c, 21))
        return w, oid, pb.FULL

    def test_two_companies_customers_sharing_a_number_are_two_scopes(self):
        from ontology.tests.test_project_bc import DIST, MAN
        w, oid, actor = self.world(((1, MAN), (2, DIST)))
        out = w.call("plx.find", {"query": "CPO-77", "object_type": CPO})
        self.assertEqual(out["status"], "ambiguous", out)
        self.assertEqual({c["object"]["object_id"] for c in out["candidates"]},
                         {oid(1), oid(2)})
        self.assertFalse(any(ot.roots_exactly(c, "CPO-77", identifiers)
                             for c in out["candidates"]))
        self.assertIn("identity_scope_ambiguous", {g["reason"] for g in out["gaps"]})
        review = ev.EvidenceTools(w.base).call("plx.identity", {
            "object_ids": [oid(1)], "include_near_misses": True}, actor)
        (miss,) = review["near_misses"]
        self.assertIn({"feature": ot.SCOPE_FEATURE, "agrees": False}, miss["failed"])
        self.assertNotIn("ambiguous_identity", {g["reason"] for g in review["gaps"]})
        # One company's order is one customer's PO: it roots.
        w, oid, _ = self.world(((1, MAN),))
        (candidate,) = w.call("plx.find", {"query": "CPO-77", "object_type": CPO})["candidates"]
        self.assertTrue(ot.roots_exactly(candidate, "CPO-77", identifiers))


class InvoiceCustomerPoCardTests(ScopeCase):
    """A BC invoice states a customer PO only when posted from a BC order (the
    SalesInvoice.customer_po source's ``when``, orderNumber present). plx.object on a
    direct invoice shows that as a typed ``source_condition_unmet`` gap, for every such
    invoice and not only those the build report samples; a reader without the BC grant
    learns nothing. Evidence label: ``isolated-fixture`` (synthetic BC rows)."""

    def card(self, count, absent=False):
        from ontology.tests.test_bc_mapping import MAN, StaticCaptures, bc
        direct = [f"bbbbbbbb-0000-4000-8000-{n:012d}" for n in range(count)]
        rows = [bc("salesInvoices", MAN, {"id": i, "number": f"PSI-{n}",
                                          "externalDocumentNumber": f"SO-{n}"})
                for n, i in enumerate(direct)]
        rows.append(bc("salesInvoices", MAN, {"id": "posted-1", "number": "PSI-P",
                                              "orderNumber": "SO100200",
                                              "externalDocumentNumber": "PO-CUST-77"}))
        if absent:   # posted from an order, its External Document No. not served
            rows.append(bc("salesInvoices", MAN, {"id": "posted-2", "number": "PSI-Q",
                                                  "orderNumber": "SO100200"}))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "store.sqlite"
        build_store(path, self.schema, {"bc": StaticCaptures("bc", rows)},
                    normalizer=identifiers.normalize)
        reader = ot.StoreReader.open(path)
        self.addCleanup(reader.conn.close)
        tools = ot.OntologyTools(reader, ot.ReadPolicy("plx", audiences={"bc:*": {"staff:ar"}}),
                                 schema=self.view, grammar=identifiers,
                                 contracts=self.contracts)
        oid = lambda i: f'SalesInvoice:bc.sales_invoices:{{"id":"{i}"}}'

        def call(i, actor=Actor("plx", frozenset({"staff:ar"}), "restricted")):
            out = tools.call("plx.object", {"object_id": oid(i)}, actor)
            po = [v for v in out.get("values", ()) if v["property"] == "customer_po"]
            gaps = [g["reason"] for g in out.get("gaps", ())
                    if g["subject"] == "SalesInvoice.customer_po"]
            return out, po, gaps
        return direct, call

    def test_a_direct_invoice_card_says_its_source_states_no_customer_po(self):
        for count in (1, 10, 60):
            direct, call = self.card(count)
            for invoice in direct:
                out, po, gaps = call(invoice)
                self.assertNotIn("error", out, out)
                self.assertEqual((po, gaps), ([], ["source_condition_unmet"]), invoice)
            (gap,) = [g for g in call(direct[-1])[0]["gaps"]
                      if g["reason"] == "source_condition_unmet"]
            self.assertIn("orderNumber present", gap["detail"])
            _, po, gaps = call("posted-1")
            self.assertEqual(([v["value"]["untrusted_text"] for v in po], gaps),
                             (["PO-CUST-77"], []))
            denied, _, _ = call(direct[0], Actor("plx", frozenset({"staff:sales"}),
                                                 "restricted"))
            self.assertEqual(denied["error"]["code"], "object_not_found")
        # Another cause reported at the source: the build recorded every invoice that
        # failed the tests, so one the report does not sample still reads why.
        direct, call = self.card(10, absent=True)
        for invoice in (direct[0], direct[-1]):   # sampled, not sampled
            self.assertEqual(call(invoice)[2], ["source_condition_unmet"])
        self.assertEqual(call("posted-2")[2], [])   # passes the tests; its path absent


class RegistryContractTests(unittest.TestCase):
    def test_every_declared_scope_field_is_projected(self):
        """Each carrier scope_field of a scoped kind is a property source of the binding
        that projects that carrier, so plx.find can read the scope it declares."""
        schema = load_schema()
        sourced = {(b["relation"], s["path"])
                   for spec in schema.object_types.values()
                   for b in spec.get("bindings", ())
                   for p in spec.get("properties", ())
                   for s in p.get("sources", ()) if s.get("binding") == b["id"]}
        checked = 0
        for kind in identifiers.kinds():
            if not identifiers.kind_spec(kind).get("identity_scope"):
                continue
            for carrier in identifiers.carriers(kind):
                if carrier.get("scope_field"):
                    checked += 1
                    self.assertIn((carrier["relation"], carrier["scope_field"]), sourced)
        self.assertGreater(checked, 0)

    def test_link_contract_citations_name_the_pinned_upstream_commit(self):
        pins: dict[str, set[str]] = {}
        for pin in json.loads((ROOT / "devloop/upstream-pins.json").read_text())["pins"]:
            if pin["repo"] == "plx-customer-portal" and pin.get("status") == "resolved":
                pins.setdefault(pin["path"], set()).add(pin["commit"])
        contracts = json.loads((ROOT / "operating/link_contracts.json").read_text())
        checked = []
        for contract in contracts["contracts"]:
            for text in contract["evidence"]:
                cited = re.match(r"Portal ([0-9a-f]{12})\b", text)
                if cited is None:
                    continue
                for path in re.findall(r"[\w./-]+\.(?:ts|prisma)\b", text):
                    pinned = {c for p, commits in pins.items()
                              if p.endswith("/" + path) for c in commits}
                    if pinned:
                        checked.append(path)
                        self.assertIn(cited.group(1), {c[:12] for c in pinned},
                                      f"{contract['id']}: {path}")
        self.assertIn("lib/orders/order-identity.ts", checked)


if __name__ == "__main__":
    unittest.main()
