"""plx.aggregate / plx.compare over a fixture projection store.

The store is created with the frozen DDL (ontology/store_ddl.py) and filled by hand
with synthetic records; the schema is the real compiled ontology schema, so link
cardinalities, property types and roles are the shipped ones. Every output is
validated against operating/ontology_tool_contracts.json (the tools do it too; the
test repeats it independently). All identifiers are synthetic. Evidence label:
isolated-fixture.
"""

from __future__ import annotations

import copy
import datetime as dt
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

from ontology import schema as schema_loader
from ontology import project, store_ddl, units
from ontology.json_contract import validate
from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_aggregate as oa
from operating import ontology_tools_process as op

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = json.loads((ROOT / "operating/ontology_tool_contracts.json").read_text("utf-8"))
NOW = dt.datetime(2099, 2, 1, 12, 0, tzinfo=dt.timezone.utc)
T0 = "2099-01-01T00:00:00Z"
T1 = "2099-01-15T00:00:00Z"
SCHEMA: ot.SchemaView | None = None

STAFF = Actor("plx", frozenset({"staff"}))
SALES = Actor("plx", frozenset({"sales"}))
POLICY = ot.ReadPolicy(
    "plx",
    {"api:*": frozenset({"staff", "sales"}), "bc:*": frozenset({"staff"})},
    {"finance": frozenset({"staff"}), "pii": frozenset({"staff"}),
     "l5": frozenset({"staff"}), "quality_controlled": frozenset({"staff"})})


def setUpModule():
    global SCHEMA
    SCHEMA = ot.SchemaView.from_compiled(schema_loader.load_schema())


def _h(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _relation(binding):
    for spec in SCHEMA.object_types.values():
        for b in spec.get("bindings", ()):
            if b["id"] == binding:
                return b["source"], b["relation"]
    raise KeyError(binding)


class Store:
    """A hand-filled projection: objects with versions, values and links."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        store_ddl.create(self.conn)
        meta = {"schema_digest": "sha256:" + SCHEMA.digest_hex,
                "freshness": json.dumps([
                    {"source": s, "captured_at": "2099-02-01T11:00:00Z", "lag_seconds": 3600,
                     "completeness": "complete", "stale": False} for s in ("portal_api", "bc")]),
                "build_report": json.dumps({"counts": {}, "gaps": []})}
        for key, value in meta.items():
            self.conn.execute("INSERT INTO store_meta VALUES (?, ?)", (key, value))
        self.handles = {}
        self.n = 0

    def obj(self, object_id, *, markings="[]", missing=None, superseded=None, lineage=None):
        otype, binding, native = object_id.split(":", 2)
        source, relation = _relation(binding)
        if source == "bc":
            audience = f"bc:company_a/{relation}"
            handle = f"bc://company_a/{relation}/{native}@cap-1"
            lineage = lineage or "bc_company_a"
        else:
            audience = f"api:{relation}"
            handle = f"api://{relation}/{native}@0000000000000001-1"
            lineage = lineage or "gold_etl_from_fm"
        self.handles[object_id] = (handle, lineage)
        self.conn.execute("INSERT INTO objects VALUES (?,?,?,?,?,?,?)",
                          (object_id, otype, binding, json.dumps([native]), audience, T0, missing))
        self.conn.execute("INSERT INTO object_versions VALUES (?,?,?,?,?,?,?)",
                          (object_id, handle, _h(handle), lineage, markings, T0, superseded))
        return object_id

    def version(self, object_id, recorded_at):
        """A second, later version (the first is superseded at ``recorded_at``)."""
        old, lineage = self.handles[object_id]
        handle = old.replace("-1", "-2")
        self.conn.execute("UPDATE object_versions SET superseded_at = ? WHERE object_id = ?",
                          (recorded_at, object_id))
        self.conn.execute("INSERT INTO object_versions VALUES (?,?,?,?,?,?,?)",
                          (object_id, handle, _h(handle), lineage, "[]", recorded_at, None))
        self.handles[object_id] = (handle, lineage)
        return handle

    def val(self, object_id, prop, value_type, value, *, unit=None, role=None, currency=None,
            markings="[]", path=None, rank=0, recorded_at=T0, superseded=None, handle=None):
        handle = handle or self.handles[object_id][0]
        lineage = self.handles[object_id][1]
        binding = object_id.split(":")[1]
        path = path or f"row.{prop}"
        self.conn.execute(
            "INSERT INTO object_values VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (object_id, prop, value_type, value, unit, role, currency, None, None, handle,
             path, _h(f"{handle}|{path}|{value}"), lineage, binding, "trim", rank, markings,
             recorded_at, superseded))

    def link(self, link_type, src, dst, tier="verified", kind="fk"):
        self.n += 1
        handles = [self.handles[src][0], self.handles[dst][0]]
        self.conn.execute(
            "INSERT INTO links(link_id, link_type, from_object, to_object, tier, binding_kind, "
            "reason, why_not, evidence_handles, markings, recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"lnk-{self.n}", link_type, src, dst, tier, kind,
             json.dumps([{"feature": "stored_key", "agrees": True}]), "[]",
             json.dumps(handles), "[]", T0))


S1 = "Supplier:portal.app_supplier:s1"
S2 = "Supplier:portal.app_supplier:s2"
PO1 = "PurchaseOrder:portal.app_purchase_order:p1"
PO2 = "PurchaseOrder:portal.app_purchase_order:p2"
PO3 = "PurchaseOrder:portal.app_purchase_order:p3"
L1 = "PurchaseOrderLine:portal.app_purchase_order_line:l1"
L2 = "PurchaseOrderLine:portal.app_purchase_order_line:l2"
L3 = "PurchaseOrderLine:portal.app_purchase_order_line:l3"
L4 = "PurchaseOrderLine:portal.app_purchase_order_line:l4"
L5 = "PurchaseOrderLine:bc.purchase_order_lines:l5"
R1 = "ReceiptLine:portal.app_receipt_line:r1"
BA = "BatchAllocation:portal.fm_order_entry_item_batch:b1"
AI = "RelSilverAssemblyOrderItems:gen.silver.assembly_order_items:a1"


def world():
    """Two suppliers, three POs, five PO lines (one in BC), one receipt line."""
    s = Store()
    for oid in (S1, S2, PO1, PO2, PO3, L1, L2, L3, L4, L5, R1, BA, AI):
        s.obj(oid)
    s.val(S1, "name", "text", "Acme Packaging")
    s.val(S2, "name", "text", "Bolt Plastics")
    s.link("PurchaseOrder.ordered_from", PO1, S1)
    s.link("PurchaseOrder.ordered_from", PO2, S2)
    s.link("PurchaseOrder.ordered_from", PO3, S1)
    for line, po in ((L1, PO1), (L2, PO1), (L3, PO2), (L4, PO3)):
        s.link("PurchaseOrderLine.line_of", line, po)
    s.val(L1, "ordered_quantity", "quantity", "100", unit="each", role="ordered")
    s.val(L1, "received_quantity", "quantity", "98", unit="EA", role="received")
    s.val(L1, "unit_price", "money", "2.50", unit="each", currency="CAD",
          markings='["finance"]')
    s.val(L1, "description", "text", "Bottle 500ml")
    s.val(L2, "ordered_quantity", "quantity", "50", unit="pcs", role="ordered")
    s.val(L2, "received_quantity", "quantity", "50", unit="pcs", role="received")
    s.val(L2, "description", "text", "bottle  500ML")
    s.val(L3, "ordered_quantity", "quantity", "10", unit="kg", role="ordered")
    s.val(L4, "ordered_quantity", "quantity", "20", unit="each", role="ordered")
    s.val(L4, "unit_price", "money", "1.75", unit="each", currency="USD",
          markings='["finance"]')
    s.val(L5, "ordered_quantity", "quantity", "7", unit="each", role="ordered")
    s.val(R1, "received_quantity", "quantity", "98", unit="each", role="received")
    s.val(BA, "quantity", "quantity", "40", unit="each", role="committed")
    s.val(AI, "quantity", "quantity", "40", unit="each", role="planned")
    return s


def tools(store):
    base = ot.OntologyTools(ot.StoreReader(store.conn), POLICY, schema=SCHEMA,
                            contracts=CONTRACTS, clock=lambda: NOW)
    return oa.AggregateTools(base)


class _Base(unittest.TestCase):
    def setUp(self):
        self.store = world()
        self.tools = tools(self.store)

    def set_of(self, actor, *ids):
        return self.tools.base.sets.new(actor, list(ids))

    def call(self, name, arguments, actor=STAFF):
        # A store is served as committed (an uncommitted word-index write keeps SQLite
        # from dropping the call's gate function).
        self.store.conn.commit()
        out = self.tools.call(name, arguments, actor)
        problems = validate(out, CONTRACTS["tools"][name]["output"], root=CONTRACTS)
        self.assertEqual(problems, [], out)
        return out

    def agg(self, actor, ids, measure, **extra):
        return self.call("plx.aggregate", dict({"set": self.set_of(actor, *ids),
                                                "measure": measure}, **extra), actor)


class AggregateTests(_Base):
    def line_totals(self):
        """Line amounts (quantity x unit price) of L1 and L4: money that sums."""
        self.store.val(L1, "line_total", "money", "250", currency="CAD", markings='["finance"]')
        self.store.val(L4, "line_total", "money", "35", currency="USD", markings='["finance"]')

    def test_sum_is_exact_cited_and_role_typed(self):
        out = self.agg(STAFF, [L1, L2], {"property": "ordered_quantity", "op": "sum"})
        self.assertNotIn("error", out)
        [group] = out["groups"]
        self.assertEqual(group["value"], "150")
        self.assertEqual(group["quantity_role"], "ordered")
        self.assertEqual(group["unit"], "ea")  # each + pcs fold to Portal's "ea"
        self.assertEqual(group["inputs"], 2)
        handles = sorted(c["l1_handle"] for c in group["inputs_citations"])
        self.assertEqual(handles, sorted([self.store.handles[L1][0], self.store.handles[L2][0]]))
        self.assertTrue(all(c["field_path"] == "row.ordered_quantity"
                            for c in group["inputs_citations"]))
        self.assertFalse(out["partial"])
        self.assertTrue(out["completeness"]["complete"])

    def test_group_by_follows_to_one_path_and_keys_are_enveloped(self):
        out = self.agg(STAFF, [L1, L2, L4], {"property": "ordered_quantity", "op": "sum"},
                       group_by=["name"],
                       path=[{"link_type": "PurchaseOrderLine.line_of", "direction": "forward"},
                             {"link_type": "PurchaseOrder.ordered_from", "direction": "forward"}])
        self.assertNotIn("refused", out)
        [group] = out["groups"]
        self.assertEqual(group["key"]["name"]["untrusted_text"], "Acme Packaging")
        self.assertEqual(group["key"]["name"]["origin"]["citation"], self.store.handles[S1][0])
        self.assertEqual(group["value"], "170")
        self.assertEqual(group["inputs"], 3)

    def test_unlinked_ambiguous_and_weak_links_are_explicit(self):
        path = [{"link_type": "PurchaseOrderLine.line_of", "direction": "forward"},
                {"link_type": "PurchaseOrder.ordered_from", "direction": "forward"}]
        unlinked = "PurchaseOrderLine:portal.app_purchase_order_line:u1"
        self.store.obj(unlinked)
        self.store.val(unlinked, "ordered_quantity", "quantity", "5", unit="each",
                       role="ordered")
        out = self.agg(STAFF, [L1, unlinked], {"property": "ordered_quantity", "op": "sum"},
                       group_by=["name"], path=path)
        keys = [g["key"]["name"] for g in out["groups"]]
        self.assertEqual(keys[-1], None)  # the unlinked member groups under null, last
        self.assertEqual(out["groups"][-1]["value"], "5")
        self.assertIn("no_group_link", {g["reason"] for g in out["gaps"]})
        # A member that reaches two objects over a to-one link is never grouped.
        self.store.link("PurchaseOrderLine.line_of", L1, PO2)
        out = self.agg(STAFF, [L1, L2], {"property": "ordered_quantity", "op": "sum"},
                       group_by=["name"], path=path)
        self.assertEqual([g["value"] for g in out["groups"]], ["50"])
        self.assertTrue(out["partial"])
        self.assertIn("ambiguous_group_link", {g["reason"] for g in out["gaps"]})
        # A lead link is below the floor and is not followed.
        self.store.link("PurchaseOrderLine.line_of", unlinked, PO3, tier="lead",
                        kind="text_match")
        out = self.agg(STAFF, [unlinked], {"property": "ordered_quantity", "op": "sum"},
                       group_by=["name"], path=path)
        self.assertIsNone(out["groups"][0]["key"]["name"])
        self.assertIn("below_tier_floor", {g["reason"] for g in out["gaps"]})

    def test_roles_are_never_mixed(self):
        out = self.agg(STAFF, [BA, AI], {"property": "quantity", "op": "sum"})
        self.assertEqual(out["refused"]["code"], "role_mixing")
        self.assertEqual(out["groups"], [])
        self.assertFalse(out["completeness"]["complete"])
        # Each role on its own sums, and keeps its role.
        planned = self.agg(STAFF, [AI], {"property": "quantity", "op": "sum"})
        committed = self.agg(STAFF, [BA], {"property": "quantity", "op": "sum"})
        self.assertEqual(planned["groups"][0]["quantity_role"], "planned")
        self.assertEqual(committed["groups"][0]["quantity_role"], "committed")

    def test_units_are_never_mixed_or_guessed(self):
        out = self.agg(STAFF, [L1, L3], {"property": "ordered_quantity", "op": "sum"})
        self.assertEqual(out["refused"]["code"], "unit_mixing")
        self.store.val(L2, "invoiced_quantity", "quantity", "5", role="invoiced")
        self.store.val(L1, "invoiced_quantity", "quantity", "5", unit="each", role="invoiced")
        out = self.agg(STAFF, [L1, L2], {"property": "invoiced_quantity", "op": "max"})
        self.assertEqual(out["refused"]["code"], "unit_unknown")

    def test_mixed_currency_is_refused(self):
        self.line_totals()
        out = self.agg(STAFF, [L1, L4], {"property": "line_total", "op": "sum"})
        self.assertEqual(out["refused"]["code"], "currency_mixing")
        self.assertIn("CAD", out["refused"]["message"])
        self.assertIn("USD", out["refused"]["message"])
        self.store.val(L3, "line_total", "money", "30", markings='["finance"]')
        out = self.agg(STAFF, [L1, L3], {"property": "line_total", "op": "sum"})
        self.assertEqual(out["refused"]["code"], "currency_unknown")

    def test_money_sum_carries_currency_and_markings(self):
        self.line_totals()
        self.store.val(L2, "line_total", "money", "12.50", currency="CAD", markings='["finance"]')
        out = self.agg(STAFF, [L1, L2], {"property": "line_total", "op": "sum"})
        [group] = out["groups"]
        self.assertEqual((group["value"], group["currency"]), ("262.5", "CAD"))
        self.assertNotIn("unit", group)  # an amount is per nothing
        self.assertEqual(group["markings"], ["finance"])

    def test_unit_prices_are_rates_never_summed(self):
        self.store.val(L2, "unit_price", "money", "0.25", unit="EA", currency="CAD",
                       markings='["finance"]')
        out = self.agg(STAFF, [L1, L2], {"property": "unit_price", "op": "sum"})
        self.assertEqual(out["refused"]["code"], "role_mixing")
        self.assertIn("PurchaseOrderLine.unit_price is a unit price", out["refused"]["message"])
        self.assertEqual((out["groups"], out["completeness"]["reason"]),
                         ([], "refused_role_mixing"))
        # One denominator (each = EA after the Portal fold): a max stands, per that unit.
        [group] = self.agg(STAFF, [L1, L2], {"property": "unit_price", "op": "max"})["groups"]
        self.assertEqual((group["value"], group["currency"], group["unit"]),
                         ("2.5", "CAD", "ea"))
        self.store.val(L3, "unit_price", "money", "9", unit="kg", currency="CAD",
                       markings='["finance"]')
        out = self.agg(STAFF, [L1, L3], {"property": "unit_price", "op": "min"})
        self.assertEqual(out["refused"]["code"], "unit_mixing")
        self.assertIn("unit prices are per different units", out["refused"]["message"])
        self.store.val(L5, "unit_price", "money", "2.50", currency="CAD",
                       markings='["finance"]')
        out = self.agg(STAFF, [L1, L5], {"property": "unit_price", "op": "max"})
        self.assertEqual(out["refused"]["code"], "unit_unknown")
        self.assertIn("no known denominator", out["refused"]["message"])

    def test_permission_trims_before_aggregation(self):
        self.line_totals()
        self.store.val(L2, "line_total", "money", "12.50", currency="CAD", markings='["finance"]')
        out = self.agg(SALES, [L1, L2], {"property": "line_total", "op": "sum"})
        [group] = out["groups"]
        # Sales holds no finance marking: no figure, no citation, stated as withheld.
        self.assertIsNone(group["value"])
        self.assertEqual(group["inputs"], 0)
        self.assertNotIn("inputs_citations", group)
        self.assertTrue(out["partial"])
        self.assertEqual(out["withheld"], {"properties": 2, "objects": None, "reason": "marking"})
        self.assertIn("marking_withheld", {g["reason"] for g in out["gaps"]})
        self.assertNotIn('"250"', json.dumps(out))
        # A count of members is not withheld.
        count = self.agg(SALES, [L1, L2], {"op": "count"})
        self.assertEqual(count["groups"][0]["value"], "2")

    def test_hidden_members_never_count_and_make_the_answer_partial(self):
        handle = self.set_of(SALES, L1, L5)  # L5 sits in BC, which sales may not read
        out = self.call("plx.aggregate", {"set": handle, "measure": {
            "property": "ordered_quantity", "op": "sum"}}, SALES)
        [group] = out["groups"]
        self.assertIsNone(group["value"])
        self.assertTrue(out["partial"])
        self.assertEqual(out["withheld"]["reason"], "audience")
        self.assertNotIn(self.store.handles[L5][0], json.dumps(out))
        staff = self.agg(STAFF, [L1, L5], {"property": "ordered_quantity", "op": "sum"})
        self.assertEqual(staff["groups"][0]["value"], "107")

    def test_a_set_whose_every_member_is_hidden_has_no_count(self):
        handle = self.set_of(SALES, L5)  # L5 sits in BC, which sales may not read
        out = self.call("plx.aggregate", {"set": handle, "measure": {"op": "count"}}, SALES)
        [group] = out["groups"]
        self.assertIsNone(group["value"])  # unknown, never "0"
        self.assertTrue(out["partial"])
        self.assertFalse(out["completeness"]["complete"])
        self.assertIn("policy_changed_since_handle", {g["reason"] for g in out["gaps"]})

    def test_twins_in_two_systems_are_never_counted_or_summed_twice(self):
        twin = self.store.obj("ReceiptLine:bc.purchase_receipt_lines:r1")
        self.store.val(twin, "received_quantity", "quantity", "98", unit="each",
                       role="received")
        self.store.link("ReceiptLine.same_record_as", R1, twin, kind="same_record")
        for measure in ({"property": "received_quantity", "op": "sum"}, {"op": "count"}):
            with self.subTest(measure["op"]):
                out = self.agg(STAFF, [R1, twin], measure)
                [group] = out["groups"]
                self.assertIsNone(group["value"])  # never 196 or 2
                self.assertTrue(out["partial"])
                self.assertIn("cross_system_twins", {g["reason"] for g in out["gaps"]})
        alone = self.agg(STAFF, [R1], {"property": "received_quantity", "op": "sum"})
        self.assertEqual(alone["groups"][0]["value"], "98")

    def test_missing_value_is_explicit_never_a_partial_sum(self):
        self.line_totals()
        out = self.agg(STAFF, [L1, L2], {"property": "line_total", "op": "sum"})
        [group] = out["groups"]
        self.assertIsNone(group["value"])
        self.assertEqual(group["inputs"], 1)
        self.assertTrue(out["partial"])
        self.assertEqual(out["completeness"]["reason"], "partial_inputs")
        self.assertIn("measure_value_missing", {g["reason"] for g in out["gaps"]})

    def test_conflicting_sources_are_not_picked(self):
        self.store.val(L3, "received_quantity", "quantity", "9", unit="kg", role="received",
                       path="row.qty_a", rank=None)
        self.store.val(L3, "received_quantity", "quantity", "8", unit="kg", role="received",
                       path="row.qty_b", rank=None)
        out = self.agg(STAFF, [L3], {"property": "received_quantity", "op": "sum"})
        self.assertIsNone(out["groups"][0]["value"])
        self.assertIn("measure_value_conflict", {g["reason"] for g in out["gaps"]})
        # With a declared precedence the ranked value is used, and that is stated.
        self.store.conn.execute("UPDATE object_values SET authority_rank = 0 WHERE "
                                "field_path = 'row.qty_a'")
        self.store.conn.execute("UPDATE object_values SET authority_rank = 1 WHERE "
                                "field_path = 'row.qty_b'")
        out = self.agg(STAFF, [L3], {"property": "received_quantity", "op": "sum"})
        self.assertEqual(out["groups"][0]["value"], "9")
        self.assertIn("authority_precedence_used", {g["reason"] for g in out["gaps"]})

    def test_fan_out_and_many_to_many_are_refused(self):
        out = self.agg(STAFF, [PO1, PO2], {"op": "count"}, group_by=["description"],
                       path=[{"link_type": "PurchaseOrderLine.line_of", "direction": "reverse"}])
        self.assertEqual(out["refused"]["code"], "fan_out")
        self.assertIn("plx.around", out["refused"]["hint"])
        out = self.agg(STAFF, [PO1], {"op": "count"}, group_by=["subject"],
                       path=[{"link_type": "Email.mentions_purchase_order",
                              "direction": "reverse"}])
        self.assertEqual(out["refused"]["code"], "n_to_m_allocation")

    def test_truncation_is_flagged(self):
        ids = []
        for i in range(oa.MAX_GROUPS + 1):
            oid = f"PurchaseOrderLine:portal.app_purchase_order_line:t{i:03d}"
            self.store.obj(oid)
            self.store.val(oid, "description", "text", f"item {i:03d}")
            ids.append(oid)
        out = self.agg(STAFF, ids, {"op": "count"}, group_by=["description"])
        self.assertTrue(out["completeness"]["truncated"])
        self.assertFalse(out["completeness"]["complete"])
        # More groups than one answer holds: the byte limit cuts first, and says so.
        self.assertIn(out["completeness"]["reason"], ("group_cap", "result_size"))
        self.assertEqual(out["completeness"]["total"], oa.MAX_GROUPS + 1)
        self.assertEqual(out["completeness"]["returned"], len(out["groups"]))
        self.assertLess(len(out["groups"]), oa.MAX_GROUPS + 1)
        # Without the byte limit the group cap applies.
        self.tools.limits = dict(self.tools.limits, max_result_bytes=10**7)
        out = self.call("plx.aggregate", {"set": self.set_of(STAFF, *ids),
                                          "measure": {"op": "count"},
                                          "group_by": ["description"]})
        self.assertEqual(out["completeness"]["reason"], "group_cap")
        self.assertEqual(len(out["groups"]), oa.MAX_GROUPS)

    def test_numeric_group_keys_group_by_value(self):
        self.store.val(L3, "received_quantity", "quantity", "20.00", unit="each", role="received")
        self.store.val(L4, "received_quantity", "quantity", "20", unit="each", role="received")
        out = self.agg(STAFF, [L1, L3, L4], {"op": "count"}, group_by=["received_quantity"])
        self.assertEqual([g["value"] for g in out["groups"]], ["2", "1"])  # 20 before 98
        self.assertEqual(out["groups"][0]["key"]["received_quantity"]["untrusted_text"], "20.00")

    def test_count_distinct_min_max(self):
        out = self.agg(STAFF, [L1, L2, L4], {"property": "ordered_quantity",
                                             "op": "count_distinct"})
        self.assertEqual(out["groups"][0]["value"], "3")
        self.assertEqual(self.agg(STAFF, [L1, L2, L4], {"property": "ordered_quantity",
                                                       "op": "min"})["groups"][0]["value"], "20")
        self.assertEqual(self.agg(STAFF, [L1, L2, L4], {"property": "ordered_quantity",
                                                       "op": "max"})["groups"][0]["value"], "100")

    def test_count_carries_the_members_markings(self):
        # A count has no value rows: its markings are the members' own, plus, along a
        # path, the links followed and the dimension objects' versions.
        self.store.conn.execute("UPDATE object_versions SET markings = '[\"finance\"]' "
                                "WHERE object_id IN (?, ?)", (L1, L2))
        self.store.conn.execute("UPDATE object_versions SET markings = '[\"pii\"]' "
                                "WHERE object_id = ?", (S1,))
        self.store.conn.execute("UPDATE links SET markings = '[\"l5\"]' WHERE from_object = ?",
                                (PO1,))
        out = self.agg(STAFF, [L1, L2], {"op": "count"})
        self.assertEqual(out["groups"][0]["value"], "2")
        self.assertEqual(out["groups"][0]["markings"], ["finance"])
        out = self.agg(STAFF, [L1, L2], {"op": "count"}, group_by=["name"], path=[
            {"link_type": "PurchaseOrderLine.line_of", "direction": "forward"},
            {"link_type": "PurchaseOrder.ordered_from", "direction": "forward"}])
        [group] = out["groups"]
        self.assertEqual(group["value"], "2")
        self.assertEqual(group["markings"], ["finance", "l5", "pii"])

    def test_hidden_group_link_goes_to_null_incomplete_and_withheld(self):
        path = [{"link_type": "PurchaseOrderLine.line_of", "direction": "forward"},
                {"link_type": "PurchaseOrder.ordered_from", "direction": "forward"}]
        self.store.conn.execute("UPDATE links SET markings = '[\"finance\"]' "
                                "WHERE from_object = ?", (PO1,))
        out = self.agg(SALES, [L1, L2], {"op": "count"}, group_by=["name"], path=path)
        [group] = out["groups"]
        self.assertIsNone(group["key"]["name"])
        self.assertIsNone(group["value"])  # its members' true group is withheld
        self.assertTrue(out["partial"])
        self.assertFalse(out["completeness"]["complete"])
        self.assertEqual(out["withheld"]["reason"], "marking")
        self.assertIn("group_link_withheld", {g["reason"] for g in out["gaps"]})
        text = json.dumps(out)
        self.assertNotIn("Acme", text)
        self.assertNotIn(self.store.handles[S1][0], text)
        # A member that truly reaches nothing stays a complete null group.
        self.store.conn.execute("UPDATE links SET markings = '[]'")
        unlinked = self.store.obj("PurchaseOrderLine:portal.app_purchase_order_line:u2")
        out = self.agg(SALES, [unlinked], {"op": "count"}, group_by=["name"], path=path)
        self.assertEqual(out["groups"][0]["value"], "1")
        self.assertTrue(out["completeness"]["complete"])
        # A hidden target object (outside the reader's audience) is withheld the same way.
        bc_supplier = self.store.obj("Supplier:bc.vendors:v9")
        self.store.link("PurchaseOrder.ordered_from", PO2, bc_supplier)
        self.store.conn.execute("DELETE FROM links WHERE from_object = ? AND to_object = ?",
                                (PO2, S2))
        out = self.agg(SALES, [L3], {"op": "count"}, group_by=["name"], path=path)
        self.assertIsNone(out["groups"][0]["value"])
        self.assertEqual(out["withheld"]["reason"], "audience")
        self.assertNotIn("v9", json.dumps(out))

    def test_units_and_currencies_are_checked_per_group(self):
        self.line_totals()
        self.store.val(L3, "line_total", "money", "40.00", currency="USD", markings='["finance"]')
        path = [{"link_type": "PurchaseOrderLine.line_of", "direction": "forward"},
                {"link_type": "PurchaseOrder.ordered_from", "direction": "forward"}]
        out = self.agg(STAFF, [L1, L3], {"property": "line_total", "op": "sum"},
                       group_by=["name"], path=path)
        self.assertNotIn("refused", out)
        self.assertEqual([(g["value"], g["currency"]) for g in out["groups"]],
                         [("250", "CAD"), ("40", "USD")])
        # One group that mixes currencies is still refused.
        out = self.agg(STAFF, [L1, L4], {"property": "line_total", "op": "sum"},
                       group_by=["name"], path=path)
        self.assertEqual(out["refused"]["code"], "currency_mixing")

    def test_withheld_reason_is_the_values_own(self):
        # A value read from an item-gated record the reader may not open: audience.
        mail = "graph-mail://mbx/msg-1"
        self.store.conn.execute("INSERT INTO object_versions VALUES (?,?,?,?,?,?,?)",
                                (L3, mail, _h(mail), "mail", "[]", "2098-12-31T00:00:00Z",
                                 "2098-12-31T00:00:01Z"))
        self.store.val(L3, "received_quantity", "quantity", "9", unit="kg", role="received",
                       handle=mail)
        out = self.agg(STAFF, [L3], {"property": "received_quantity", "op": "sum"})
        self.assertIsNone(out["groups"][0]["value"])
        self.assertEqual(out["withheld"]["reason"], "audience")

    def test_value_hidden_by_another_rows_marking_is_withheld_for_marking(self):
        # A held value is unseen only because another source's row of the same property
        # carries finance: the reason is the marking, not the audience.
        self.store.val(L1, "ordered_quantity", "quantity", "100", unit="each", role="ordered",
                       path="row.qty_finance", markings='["finance"]')
        out = self.agg(SALES, [L1], {"property": "ordered_quantity", "op": "sum"})
        self.assertIsNone(out["groups"][0]["value"])
        self.assertEqual(out["withheld"]["reason"], "marking")

    def test_hidden_link_below_the_tier_floor_is_not_followed(self):
        # A hidden lead link is one the walk would not follow anyway: the member keeps its
        # visible group, as it does for a reader who may see that link.
        path = [{"link_type": "PurchaseOrderLine.line_of", "direction": "forward"},
                {"link_type": "PurchaseOrder.ordered_from", "direction": "forward"}]
        self.store.link("PurchaseOrder.ordered_from", PO1, S2, tier="lead", kind="text_match")
        self.store.conn.execute("UPDATE links SET markings = '[\"finance\"]' WHERE tier = 'lead'")
        for actor in (STAFF, SALES):
            out = self.agg(actor, [L1, L2], {"op": "count"}, group_by=["name"], path=path)
            [group] = out["groups"]
            self.assertEqual((group["key"]["name"]["untrusted_text"], group["value"]),
                             ("Acme Packaging", "2"))
            self.assertTrue(out["completeness"]["complete"], actor)
            self.assertNotIn("withheld", out)

    def test_measure_over_several_object_types_is_refused(self):
        out = self.agg(STAFF, [L1, R1], {"property": "received_quantity", "op": "sum"})
        self.assertEqual(out["refused"]["code"], "role_mixing")
        self.assertIn("ReceiptLine", out["refused"]["message"])
        self.assertEqual(out["groups"], [])
        # No 98 + 98 anywhere (meta is left out: its schema digest is hex and may hold it).
        self.assertNotIn("196", json.dumps({k: v for k, v in out.items() if k != "meta"}))
        # Counting members across types is fine.
        self.assertEqual(self.agg(STAFF, [L1, R1], {"op": "count"})["groups"][0]["value"], "2")

    def test_plain_decimals_without_a_unit_never_sum_rank_or_compare(self):
        # Review p0-b: a decimal records no currency or unit, so USD 10000 + CNY 10000 summed
        # to a complete 20000 and the two compared equal.
        pr1, pr2 = ("PurchaseRequisitionLine:portal.app_pr_line_item:" + n for n in "xy")
        for oid, quantity in ((pr1, "4"), (pr2, "6")):
            self.store.obj(oid)
            self.store.val(oid, "estimated_cost", "decimal", "10000", markings='["finance"]')
            self.store.val(oid, "quantity", "decimal", quantity, unit="kg")
        for op in ("sum", "min", "max", "count_distinct"):
            with self.subTest(op=op):
                out = self.agg(STAFF, [pr1, pr2], {"property": "estimated_cost", "op": op})
                self.assertEqual(out["refused"]["code"], "unit_unknown")
                self.assertIn("PurchaseRequisitionLine.estimated_cost is a plain decimal",
                              out["refused"]["message"])
                self.assertEqual(out["groups"], [])
        out = self.call("plx.compare", {"left": pr1, "right": pr2,
                                        "properties": ["estimated_cost", "quantity"]})
        rows = {r["property"]: r for r in out["rows"]}
        self.assertEqual((rows["estimated_cost"]["comparison"], rows["estimated_cost"]["reason"]),
                         ("incomparable", "unit_unknown"))
        # A decimal whose unit is recorded combines within that unit.
        self.assertEqual(rows["quantity"]["comparison"], "different")
        [group] = self.agg(STAFF, [pr1, pr2], {"property": "quantity", "op": "sum"})["groups"]
        self.assertEqual((group["value"], group["unit"]), ("10", "kg"))

    def test_input_errors(self):
        out = self.agg(STAFF, [L1], {"property": "description", "op": "sum"})
        self.assertEqual(out["error"]["code"], "measure_not_numeric")
        out = self.agg(STAFF, [L1], {"property": "no_such_property", "op": "sum"})
        self.assertEqual(out["error"]["code"], "unknown_property")
        out = self.agg(STAFF, [L1], {"op": "sum"})
        self.assertEqual(out["error"]["code"], "invalid_input")
        out = self.call("plx.aggregate", {"set": "set:unknownhandle1", "measure": {"op": "count"}})
        self.assertEqual(out["error"]["code"], "handle_expired")
        # Another reader's handle is unknown to this one.
        out = self.call("plx.aggregate", {"set": self.set_of(STAFF, L1),
                                          "measure": {"op": "count"}}, SALES)
        self.assertEqual(out["error"]["code"], "handle_expired")


class CompareTests(_Base):
    def cmp(self, left, right, actor=STAFF, **extra):
        return self.call("plx.compare", dict({"left": left, "right": right}, **extra), actor)

    def rows(self, out):
        return {r["property"]: r for r in out["rows"]}

    def test_po_line_vs_receipt_line(self):
        out = self.cmp(L1, R1, properties=["received_quantity", "ordered_quantity"])
        rows = self.rows(out)
        self.assertEqual(rows["received_quantity"]["comparison"], "equal")  # EA == each
        self.assertEqual(rows["received_quantity"]["left"]["citation"]["l1_handle"],
                         self.store.handles[L1][0])
        self.assertEqual(rows["received_quantity"]["right"]["citation"]["l1_handle"],
                         self.store.handles[R1][0])
        self.assertEqual(rows["ordered_quantity"]["comparison"], "missing_right")
        self.assertTrue(out["completeness"]["complete"])

    def test_differences_are_reported_not_resolved(self):
        out = self.cmp(L1, L2, properties=["ordered_quantity", "description"])
        rows = self.rows(out)
        self.assertEqual(rows["ordered_quantity"]["comparison"], "different")
        self.assertEqual(rows["ordered_quantity"]["reason"], "left_greater")
        self.assertEqual(rows["ordered_quantity"]["left"]["value"], "100")
        self.assertEqual(rows["ordered_quantity"]["right"]["value"], "50")
        self.assertEqual(rows["description"]["comparison"], "equal")
        self.assertEqual(rows["description"]["reason"], "equal_after_normalizing")

    def test_roles_units_and_currencies_are_respected(self):
        rows = self.rows(self.cmp(BA, AI, properties=["quantity"]))
        self.assertEqual(rows["quantity"]["comparison"], "incomparable")
        self.assertEqual(rows["quantity"]["reason"], "quantity_role_differs")
        rows = self.rows(self.cmp(L1, L3, properties=["ordered_quantity"]))
        self.assertEqual(rows["ordered_quantity"]["reason"], "unit_differs")
        rows = self.rows(self.cmp(L1, L4, properties=["unit_price"]))
        self.assertEqual(rows["unit_price"]["comparison"], "incomparable")
        self.assertEqual(rows["unit_price"]["reason"], "currency_differs")

    def test_conflicting_side_is_incomparable(self):
        self.store.val(L2, "unit_price", "money", "2.50", currency="CAD", path="row.price_a",
                       rank=None, markings='["finance"]')
        self.store.val(L2, "unit_price", "money", "2.40", currency="CAD", path="row.price_b",
                       rank=None, markings='["finance"]')
        out = self.cmp(L1, L2, properties=["unit_price"])
        row = self.rows(out)["unit_price"]
        self.assertEqual((row["comparison"], row["reason"]),
                         ("incomparable", "side_values_conflict"))
        self.assertIsNone(row["right"])  # no winner shown for the conflicting side
        self.assertIn("side_values_conflict", {g["reason"] for g in out["gaps"]})

    def test_reader_permissions_apply_before_compare(self):
        out = self.cmp(L1, L4, SALES, properties=["unit_price", "ordered_quantity"])
        rows = self.rows(out)
        self.assertEqual(rows["unit_price"]["comparison"], "incomparable")
        self.assertIn(rows["unit_price"]["reason"], ("left_withheld", "right_withheld"))
        self.assertIsNone(rows["unit_price"]["left"])
        self.assertNotIn("2.50", json.dumps(out))
        self.assertEqual(rows["ordered_quantity"]["comparison"], "different")
        self.assertEqual(out["withheld"]["properties"], 2)
        self.assertFalse(out["completeness"]["complete"])
        # Without a property list the withheld property is counted, not named.
        unnamed = self.cmp(L1, L4, SALES)
        self.assertNotIn("unit_price", {r["property"] for r in unnamed["rows"]})
        self.assertEqual(unnamed["withheld"]["properties"], 2)
        # An object outside the reader's audience is not found, as in plx.object.
        hidden = self.cmp(L1, L5, SALES)
        self.assertEqual(hidden["error"]["code"], "object_not_found")

    def test_versions_compare_as_known_at(self):
        handle = self.store.version(L3, T1)
        self.store.conn.execute("UPDATE object_values SET superseded_at = ? WHERE object_id = ?",
                                (T1, L3))
        self.store.val(L3, "ordered_quantity", "quantity", "12", unit="kg", role="ordered",
                       recorded_at=T1, handle=handle)
        out = self.cmp(L3, L3, left_as_known_at="2099-01-10", properties=["ordered_quantity"])
        row = self.rows(out)["ordered_quantity"]
        self.assertEqual((row["left"]["value"], row["right"]["value"]), ("10", "12"))
        self.assertEqual(row["comparison"], "different")

    def test_citation_side_reads_its_own_revision(self):
        old = self.store.handles[L3][0]
        handle = self.store.version(L3, T1)
        self.store.conn.execute("UPDATE object_values SET superseded_at = ? WHERE object_id = ?",
                                (T1, L3))
        self.store.val(L3, "ordered_quantity", "quantity", "12", unit="kg", role="ordered",
                       recorded_at=T1, handle=handle)
        out = self.cmp(L3, {"citation": old}, properties=["ordered_quantity"])
        row = self.rows(out)["ordered_quantity"]
        self.assertEqual((row["comparison"], row["reason"]), ("different", "left_greater"))
        self.assertEqual(row["right"]["value"], "10")
        self.assertEqual(row["right"]["citation"]["l1_handle"], old)
        self.assertTrue(out["completeness"]["complete"])
        # Without a property list the cited record's properties are still read.
        self.assertEqual(self.rows(self.cmp(L3, {"citation": old}))["ordered_quantity"]
                         ["comparison"], "different")
        # Read explicitly at a time the cited values were not live: never "missing".
        out = self.cmp(L3, {"citation": old}, right_as_known_at="2099-01-20",
                       properties=["ordered_quantity"])
        row = self.rows(out)["ordered_quantity"]
        self.assertEqual((row["comparison"], row["reason"]),
                         ("incomparable", "right_citation_not_live"))
        self.assertFalse(out["completeness"]["complete"])
        self.assertIn("citation_not_live", {g["reason"] for g in out["gaps"]})

    def test_compare_checks_properties_against_the_schema(self):
        out = self.cmp(L1, L2, properties=["ordered_quantity", "no_such_property"])
        self.assertEqual(out["error"]["code"], "unknown_property")
        self.assertIn("no_such_property", out["error"]["message"])

    def test_object_vs_citation(self):
        out = self.cmp(L1, {"citation": self.store.handles[R1][0]},
                       properties=["received_quantity"])
        self.assertEqual(self.rows(out)["received_quantity"]["comparison"], "equal")
        missing = self.cmp(L1, {"citation": "api://public.app_receipt_line/zz@1"})
        self.assertEqual(missing["error"]["code"], "citation_not_found")


class MissingFromSourceTests(_Base):
    """A member the source's latest traversal no longer returned is absent_in_source only
    when that source is a current capture of a complete traversal; otherwise unknown with
    why (operating/absence_claims.traversal_problem, the case drivers' rule)."""

    def traversal(self, **portal):
        self.store.conn.execute("UPDATE objects SET missing_from_source_since = ? "
                                "WHERE object_id = ?", (T1, L2))
        self.store.conn.execute("UPDATE store_meta SET value = ? WHERE key = 'freshness'", (
            json.dumps([dict({"source": s, "captured_at": "2099-02-01T11:00:00Z",
                              "lag_seconds": 3600, "completeness": "complete", "stale": False},
                             **(portal if s == "portal_api" else {}))
                        for s in ("portal_api", "bc")]),))

    @staticmethod
    def missing(out):
        return [g for g in out["gaps"] if g["reason"] == "missing_from_source"]

    def test_an_incomplete_or_stale_traversal_never_says_absent(self):
        for portal, said in (({"completeness": "partial"}, "not known to be complete"),
                             ({"completeness": "unknown"}, "not known to be complete"),
                             ({"stale": True}, "not refreshed")):
            with self.subTest(portal=portal):
                self.traversal(**portal)
                out = self.agg(STAFF, [L1, L2], {"property": "ordered_quantity",
                                                 "op": "sum"})
                [gap] = self.missing(out)
                self.assertEqual(gap["state"], "unknown")
                self.assertIn(said, gap["detail"])
                self.assertEqual(out["groups"][0]["value"], "150")  # still counted
                [side] = self.missing(self.call("plx.compare", {
                    "left": L1, "right": L2, "properties": ["ordered_quantity"]}))
                self.assertEqual((side["state"], side["subject"]), ("unknown", f"right: {L2}"))
                self.assertIn(said, side["detail"])

    def test_a_complete_current_traversal_says_absent(self):
        self.traversal()
        [gap] = self.missing(self.agg(STAFF, [L1, L2], {"property": "ordered_quantity",
                                                        "op": "sum"}))
        self.assertEqual(gap["state"], "absent_in_source")
        [side] = self.missing(self.call("plx.compare", {
            "left": L1, "right": L2, "properties": ["ordered_quantity"]}))
        self.assertEqual(side["state"], "absent_in_source")


class AsKnownAtTests(_Base):
    """Members, values and absence as known at T from the versioned captures (10-DECISION
    5): a record first captured after T is not counted at T, one live at T is, and a
    capture history that cannot show the traversal known at T answers unknown, never
    absent_in_source (absent is not missing; unreadable is not empty)."""

    LATER = "2099-01-20T00:00:00Z"

    def capture_later(self, object_id):
        for table in ("object_versions", "object_values"):
            self.store.conn.execute(f"UPDATE {table} SET recorded_at = ? WHERE object_id = ?",
                                    (self.LATER, object_id))
        self.store.conn.execute("UPDATE objects SET first_recorded_at = ? WHERE object_id = ?",
                                (self.LATER, object_id))

    def missing_since(self, when, object_id=L2):
        self.store.conn.execute("UPDATE objects SET missing_from_source_since = ? "
                                "WHERE object_id = ?", (when, object_id))

    @staticmethod
    def reasons(out):
        return {g["reason"]: g for g in out["gaps"]}

    def test_a_record_first_captured_after_t_is_not_counted_at_t(self):
        self.capture_later(L2)
        measure = {"property": "ordered_quantity", "op": "sum"}
        before = self.agg(STAFF, [L1, L2], measure, as_known_at="2099-01-10")
        self.assertEqual(before["groups"][0]["value"], "100")  # L1 only
        self.assertTrue(before["completeness"]["complete"])
        gaps = self.reasons(before)
        self.assertEqual(gaps["captured_after_as_known_at"]["state"], "not_ingested")
        self.assertNotIn("policy_changed_since_handle", gaps)  # not a revocation
        self.assertNotIn("withheld", before)
        count = self.agg(STAFF, [L1, L2], {"op": "count"}, as_known_at="2099-01-10")
        self.assertEqual(count["groups"][0]["value"], "1")
        after = self.agg(STAFF, [L1, L2], measure, as_known_at="2099-01-22")
        self.assertEqual(after["groups"][0]["value"], "150")
        self.assertNotIn("captured_after_as_known_at", self.reasons(after))
        side = self.call("plx.compare", {"left": L1, "right": L2,
                                         "right_as_known_at": "2099-01-10"})
        self.assertEqual(side["error"]["code"], "not_known_at")

    def test_a_record_existing_at_t_counts_and_is_not_missing_before_it_went_missing(self):
        self.missing_since("2099-01-25T00:00:00Z")
        measure = {"property": "ordered_quantity", "op": "sum"}
        out = self.agg(STAFF, [L1, L2], measure, as_known_at="2099-01-22")
        self.assertEqual(out["groups"][0]["value"], "150")
        self.assertNotIn("missing_from_source", self.reasons(out))
        side = self.call("plx.compare", {"left": L1, "right": L2, "right_as_known_at":
                                         "2099-01-22", "properties": ["ordered_quantity"]})
        self.assertNotIn("missing_from_source", self.reasons(side))
        now = self.agg(STAFF, [L1, L2], measure)
        self.assertEqual(self.reasons(now)["missing_from_source"]["state"], "absent_in_source")

    def test_a_traversal_a_later_capture_replaced_answers_unknown_never_absent(self):
        # Missing since Jan 25, latest complete capture Feb 1 11:00: at Jan 26 the
        # traversal known then was replaced and its completeness is not recorded.
        self.missing_since("2099-01-25T00:00:00Z")
        measure = {"property": "ordered_quantity", "op": "sum"}
        for at, state in (("2099-01-26", "unknown"),
                          ("2099-02-01T11:30:00Z", "absent_in_source")):
            with self.subTest(at=at):
                gap = self.reasons(self.agg(STAFF, [L1, L2], measure,
                                            as_known_at=at))["missing_from_source"]
                self.assertEqual(gap["state"], state)
                side = self.reasons(self.call("plx.compare", {
                    "left": L1, "right": L2, "right_as_known_at": at,
                    "properties": ["ordered_quantity"]}))["missing_from_source"]
                self.assertEqual(side["state"], state)
                if state == "unknown":
                    self.assertIn("replaced the traversal known then", gap["detail"])
                    self.assertIn("replaced the traversal known then", side["detail"])

    def soft_delete(self, object_id, since):
        self.store.conn.execute("INSERT INTO object_removals (object_id, basis, since) "
                                "VALUES (?, 'source_soft_delete', ?)", (object_id, since))

    def test_a_deleted_member_is_left_out_from_its_removal_time_and_counted_before(self):
        self.soft_delete(L2, "2099-01-25T00:00:00Z")
        self.soft_delete(L5, "2099-01-25T00:00:00Z")
        measure = {"property": "ordered_quantity", "op": "sum"}
        members = [L1, L2, L4, L5]
        before = self.agg(STAFF, members, measure, as_known_at="2099-01-22")
        self.assertEqual(before["groups"][0]["value"], "177")      # 100 + 50 + 20 + 7
        self.assertNotIn("deleted_in_source", self.reasons(before))
        now = self.agg(STAFF, members, measure)
        self.assertEqual(now["groups"][0]["value"], "120")         # 100 + 20, complete
        self.assertTrue(now["completeness"]["complete"], now)
        gap = self.reasons(now)["deleted_in_source"]
        self.assertEqual(gap["state"], "absent_in_source")
        self.assertTrue(gap["detail"].startswith("2 affected"), gap)
        count = self.agg(STAFF, members, {"op": "count"})
        self.assertEqual(count["groups"][0]["value"], "2")
        grouped = self.agg(STAFF, members, {"op": "count"}, group_by=["description"])
        self.assertEqual(sum(int(g["value"]) for g in grouped["groups"]), 2)
        # A reader who cannot see L5 counts L1 alone: never L5's removal nor its record.
        sales = self.agg(SALES, [L1, L5], measure)
        self.assertEqual(self.reasons(sales).get("deleted_in_source"), None)
        self.assertNotIn(self.store.handles[L5][0], json.dumps(sales))
        # plx.compare takes the same removal test.
        side = self.reasons(self.call("plx.compare", {"left": L1, "right": L2,
                                                      "properties": ["ordered_quantity"]}))
        self.assertEqual(side["deleted_in_source"]["subject"], f"right: {L2}")
        self.assertNotIn("deleted_in_source", self.reasons(self.call("plx.compare", {
            "left": L1, "right": L2, "right_as_known_at": "2099-01-22",
            "properties": ["ordered_quantity"]})))

    def test_an_unparseable_removal_time_reads_removed_at_an_unknown_time(self):
        self.soft_delete(L2, "deleted sometime")
        measure = {"property": "ordered_quantity", "op": "sum"}
        for at in (None, "2099-01-10"):
            with self.subTest(as_known_at=at):
                out = self.agg(STAFF, [L1, L2], measure,
                               **({"as_known_at": at} if at else {}))
                self.assertEqual(out["groups"][0]["value"], "100")  # never read as held
                self.assertIn("deleted_in_source", self.reasons(out))
        gap = self.reasons(self.tools.base.call("plx.object", {"object_id": L2}, STAFF)
                           )["deleted_in_source"]
        self.assertIn("since an unknown time", gap["detail"])
        self.assertNotIn("deleted sometime", gap["detail"])

    def test_freshness_over_the_contract_cap_is_stated_never_silently_dropped(self):
        extra = {f"x{i}": f"extra_{i}" for i in range(10)}
        contracts = copy.deepcopy(CONTRACTS)
        contracts["$defs"]["freshness"]["properties"]["source"]["enum"] += sorted(
            extra.values())
        policy = ot.ReadPolicy("plx", dict(POLICY.audiences, **{f"{k}:*": frozenset({"staff"})
                                                               for k in extra}),
                               POLICY.markings)
        with mock.patch.dict(ot._AUDIENCE_SOURCE, extra):
            base = ot.OntologyTools(ot.StoreReader(self.store.conn), policy, schema=SCHEMA,
                                    contracts=contracts, clock=lambda: NOW)
            tools_ = oa.AggregateTools(base)
            out = tools_.call("plx.aggregate", {"set": base.sets.new(STAFF, []),
                                                "measure": {"op": "count"}}, STAFF)
        self.assertEqual(len(out["meta"]["freshness"]), 8, out)
        gap = self.reasons(out)["freshness_truncated"]
        self.assertIn("12 sources", gap["detail"])  # portal_api, bc and the 10 extra


class HostWiringTests(unittest.TestCase):
    def test_register_refuses_shadowing_and_specs_match_the_contract(self):
        store = world()
        table = {}
        oa.register(table, tools(store))
        self.assertEqual(set(table), set(oa.TOOL_NAMES))
        for name in oa.TOOL_NAMES:
            self.assertEqual(table[name]["inputSchema"]["properties"],
                             CONTRACTS["tools"][name]["input"]["properties"])
            self.assertTrue(table[name]["annotations"]["readOnlyHint"])
        with self.assertRaises(ValueError):
            oa.register(table, tools(store))

    def test_case_host_tools_route_both_tools_under_the_policy_of_the_call(self):
        from operating.ontology_host import CaseHostTools

        store = world()

        class Host(CaseHostTools):
            policy = POLICY

            def policy_now(self, actor):
                return self.policy

        host = Host(runtime=None, actor=STAFF, schema=SCHEMA)
        host._tools = tools(store).base
        handle = host._tools.sets.new(STAFF, [L1, L2])
        measure = {"set": handle, "measure": {"property": "ordered_quantity", "op": "sum"}}
        self.assertEqual(host.call("plx.aggregate", measure, STAFF)["groups"][0]["value"], "150")
        rows = host.call("plx.compare", {"left": L1, "right": L2,
                                         "properties": ["ordered_quantity"]}, STAFF)["rows"]
        self.assertEqual(rows[0]["comparison"], "different")
        # The policy is decided per call: a revoked audience applies to the next call.
        Host.policy = ot.ReadPolicy("plx", {"api:*": frozenset({"sales"})})
        self.assertEqual(host.call("plx.compare", {"left": L1, "right": L2}, STAFF)
                         ["error"]["code"], "object_not_found")

    def test_unit_tokens_follow_portal_spellings(self):
        self.assertIs(oa.unit_token, units.unit_token)  # one fold, not a private copy
        self.assertEqual(oa.unit_token(" Each "), "ea")
        self.assertEqual(oa.unit_token("PCS"), "ea")
        self.assertEqual(oa.unit_token("GR"), "gr")  # Portal folds no BC code back
        self.assertEqual(oa.unit_token("kgs"), "kg")
        self.assertEqual(oa.unit_token("pallet"), "pallet")  # unknown stays itself
        self.assertIsNone(oa.unit_token("  "))


BC_SO = "SalesOrder:bc.sales_orders:b1"
BC_SO2 = "SalesOrder:bc.sales_orders:b2"
FM_SO = "SalesOrder:portal.app_sales_order:f1"
FM_SO2 = "SalesOrder:portal.app_sales_order:f2"
NATIVE_SO = "SalesOrder:portal.app_sales_order:n1"
NATIVE_SO2 = "SalesOrder:portal.app_sales_order:n2"
SAGE_SO = "SalesOrder:portal.sage_oe_order_header:s1"


class TaxBasisTests(_Base):
    """Money on two tax bases never sums or compares (the schema's tax_basis)."""

    def setUp(self):
        super().setUp()
        s = self.store
        for oid, amount, lineage in ((BC_SO, "113.00", None), (BC_SO2, "56.50", None),
                                     (FM_SO, "100.00", None), (FM_SO2, "50.00", None),
                                     (NATIVE_SO, "113.00", "gold_native_staging"),
                                     (NATIVE_SO2, "10.00", "gold_native_staging"),
                                     (SAGE_SO, "100.00", "sage_bronze")):
            s.obj(oid, lineage=lineage)
            s.val(oid, "total_amount", "money", amount, currency="CAD", markings='["finance"]')

    def total(self, *ids):
        return self.agg(STAFF, list(ids), {"property": "total_amount", "op": "sum"})

    def test_incl_tax_and_excl_tax_totals_are_refused_naming_both_bases(self):
        out = self.total(BC_SO, FM_SO)
        self.assertEqual(out["refused"]["code"], "role_mixing")
        self.assertIn("excl_tax", out["refused"]["message"])
        self.assertIn("incl_tax", out["refused"]["message"])
        self.assertEqual(out["groups"], [])
        self.assertEqual(out["completeness"]["reason"], "refused_role_mixing")

    def test_unknown_never_mixes_with_a_known_basis_or_another_column(self):
        out = self.total(BC_SO, NATIVE_SO)
        self.assertEqual(out["refused"]["code"], "role_mixing")
        self.assertIn("incl_tax", out["refused"]["message"])
        self.assertIn("unknown from portal.app_sales_order.total_amount",
                      out["refused"]["message"])
        out = self.total(NATIVE_SO, SAGE_SO)  # two unknown columns may differ
        self.assertEqual(out["refused"]["code"], "role_mixing")
        self.assertIn("portal.sage_oe_order_header.total_amount", out["refused"]["message"])

    def test_same_basis_sums(self):
        for ids, want in (((BC_SO, BC_SO2), "169.5"), ((FM_SO, FM_SO2), "150"),
                          ((NATIVE_SO, NATIVE_SO2), "123")):
            out = self.total(*ids)
            self.assertNotIn("refused", out)
            [group] = out["groups"]
            self.assertEqual((Decimal(group["value"]), group["currency"]),
                             (Decimal(want), "CAD"))

    def test_a_sum_of_unknown_basis_says_so_and_a_known_one_does_not(self):
        out = self.total(NATIVE_SO, NATIVE_SO2)
        [gap] = [g for g in out["gaps"] if g["reason"] == "tax_basis_unknown"]
        self.assertIn("tax_basis: unknown", gap["detail"])
        self.assertIn("portal.app_sales_order.total_amount", gap["detail"])
        self.assertEqual(out["groups"][0]["value"], "123")  # combined, not refused
        for ids in ((BC_SO, BC_SO2), (FM_SO, FM_SO2)):
            self.assertNotIn("tax_basis_unknown",
                             {g["reason"] for g in self.total(*ids)["gaps"]})

    def test_compare_across_bases_is_incomparable(self):
        cases = ((BC_SO, FM_SO, "tax_basis_differs"), (BC_SO, NATIVE_SO, "tax_basis_unknown"),
                 (NATIVE_SO, SAGE_SO, "tax_basis_unknown"))
        for left, right, reason in cases:
            out = self.call("plx.compare", {"left": left, "right": right,
                                            "properties": ["total_amount"]})
            [row] = out["rows"]
            self.assertEqual((row["comparison"], row.get("reason")), ("incomparable", reason))
        for left, right, verdict in ((BC_SO, BC_SO2, "different"), (FM_SO, FM_SO2, "different"),
                                     (NATIVE_SO, NATIVE_SO2, "different")):
            [row] = self.call("plx.compare", {"left": left, "right": right,
                                              "properties": ["total_amount"]})["rows"]
            self.assertEqual(row["comparison"], verdict)


class UnitFoldAgreementTests(unittest.TestCase):
    """The projection conflict key, plx.process and plx.compare fold units alike."""

    PAIRS = (("KGS", "kg", True), ("Each", "pcs", True), ("CS", "case", True),
             ("GR", "g", False), ("bags", "bag", True), ("pails", "pail", False),
             ("kg", "g", False))

    def test_three_readers_agree(self):
        for a, b, same in self.PAIRS:
            with self.subTest(a=a, b=b):
                s = Store()
                s.obj(L1)
                s.obj(L2)
                s.val(L1, "ordered_quantity", "quantity", "10", unit=a, role="ordered")
                s.val(L2, "ordered_quantity", "quantity", "10", unit=b, role="ordered")
                [row] = tools(s).call("plx.compare", {"left": L1, "right": L2,
                                                      "properties": ["ordered_quantity"]},
                                      STAFF)["rows"]
                self.assertEqual(row["comparison"] == "equal", same, row)
                if not same:
                    self.assertEqual(row["reason"], "unit_differs")
                key = lambda u: project._conflict_key(  # noqa: E731
                    {"value": "10", "unit": u, "currency": None})
                self.assertEqual(key(a) == key(b), same)
                self.assertEqual(op._units_compatible(a, b), same)


if __name__ == "__main__":
    unittest.main()
