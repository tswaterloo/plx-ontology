"""One link-tier rule for plx.around, plx.aggregate and plx.process.

``operating.ontology_tools._effective_tier`` is the one rule: a link touching a
``gold_native_staging`` row is capped at ``lead``, except a foreign key one source stores
between its own rows. The three tools are asked about the same stored links in one
store (the real compiled schema and stage graphs, synthetic records) and must agree:
either each counts the link at the default floor, or each holds it as a lead.

plx.process is stricter about the record on purpose: it serves the same
link tier, but a staging record reached from an authority record is never evidence
(``never_authority_record``), and every answer from a staging root is a lead
(``staging_root``). The last tests pin that split. Evidence label: isolated-fixture.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest

from ontology import schema as schema_loader
from ontology import store_ddl
from ontology.json_contract import validate
from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_aggregate as oa
from operating import ontology_tools_process as op
from operating import ontology_tools_traverse as tt

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = json.loads((ROOT / "operating/ontology_tool_contracts.json").read_text("utf-8"))
NOW = dt.datetime(2099, 2, 1, 12, 0, tzinfo=dt.timezone.utc)
T0 = "2099-01-01T00:00:00Z"
STAFF = Actor("plx", frozenset({"staff"}))
POLICY = ot.ReadPolicy("plx", {"api:*": frozenset({"staff"}), "bc:*": frozenset({"staff"})},
                       {"finance": frozenset({"staff"}), "pii": frozenset({"staff"}),
                        "l5": frozenset({"staff"}), "quality_controlled": frozenset({"staff"})})
STAGING = "gold_native_staging"
SCHEMA: ot.SchemaView | None = None
GRAPHS: dict | None = None


def setUpModule():
    global SCHEMA, GRAPHS
    SCHEMA = ot.SchemaView.from_compiled(schema_loader.load_schema())
    GRAPHS = op.load_graphs(schema=SCHEMA)


class Store:
    """A hand-filled projection store: objects with one version, values and links whose
    evidence is both ends' handles (what the projector stores for a key it reads)."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        store_ddl.create(self.conn)
        meta = {"schema_digest": "sha256:" + SCHEMA.digest_hex,
                "freshness": json.dumps([
                    {"source": s, "captured_at": "2099-02-01T11:00:00Z", "lag_seconds": 3600,
                     "completeness": "complete", "stale": False} for s in ("portal_api", "bc")]),
                "build_report": json.dumps({"counts": {}, "gaps": []})}
        self.conn.executemany("INSERT INTO store_meta VALUES (?, ?)", meta.items())
        self.relations = {b["id"]: (b["source"], b["relation"])
                          for spec in SCHEMA.object_types.values()
                          for b in spec.get("bindings", ())}
        self.handles: dict[str, str] = {}
        self.n = 0

    def obj(self, object_id, lineage):
        otype, binding, native = object_id.split(":", 2)
        source, relation = self.relations[binding]
        if source == "bc":
            audience, handle = (f"bc:company_a/{relation}",
                                f"bc://company_a/{relation}/{native}@cap-1")
        else:
            audience, handle = f"api:{relation}", f"api://{relation}/{native}@0000000000000001-1"
        self.handles[object_id] = handle
        self.conn.execute("INSERT INTO objects VALUES (?,?,?,?,?,?,?)",
                          (object_id, otype, binding, json.dumps([native]), audience, T0, None))
        self.conn.execute("INSERT INTO object_versions VALUES (?,?,?,?,?,?,?)",
                          (object_id, handle, _h(handle), lineage, "[]", T0, None))
        return object_id

    def val(self, object_id, prop, value_type, value):
        handle = self.handles[object_id]
        lineage = self.conn.execute("SELECT lineage FROM object_versions WHERE object_id = ?",
                                    (object_id,)).fetchone()[0]
        path = f"row.{prop}"
        self.conn.execute(
            "INSERT INTO object_values VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (object_id, prop, value_type, value, None, None, None, None, None, handle, path,
             _h(f"{handle}|{path}|{value}"), lineage, object_id.split(":")[1], "trim", 0, "[]",
             T0, None))

    def link(self, link_type, src, dst, *, tier="verified", kind="fk"):
        self.n += 1
        self.conn.execute(
            "INSERT INTO links(link_id, link_type, from_object, to_object, tier, binding_kind, "
            "reason, why_not, evidence_handles, markings, recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"lnk-{self.n}", link_type, src, dst, tier, kind,
             json.dumps([{"feature": "stored_key", "agrees": True}]), "[]",
             json.dumps([self.handles[src], self.handles[dst]]), "[]", T0))


def _h(text):
    return hashlib.sha256(text.encode()).hexdigest()


def order(s, key, lineage, status="CONFIRMED"):
    oid = s.obj(f"SalesOrder:portal.app_sales_order:{key}", lineage)
    s.val(oid, "status", "text", status)
    s.val(oid, "order_number", "text", key.upper())
    s.val(oid, "fulfillment_mode", "enum", "MAKE")
    return oid


def line(s, key, lineage, to_order, **link):
    oid = s.obj(f"SalesOrderLine:portal.app_sales_order_line:{key}", lineage)
    s.link("SalesOrderLine.line_of", oid, to_order, **link)
    return oid


def assembly(s, key, to_line):
    oid = s.obj(f"AssemblyOrder:portal.app_manufacturing_order:{key}", "gold_etl_from_fm")
    s.val(oid, "status", "text", "COMPLETED")
    s.val(oid, "deleted_at", "timestamp", None)  # the projector stores a source null
    s.link("AssemblyOrder.for_order_line", oid, to_line)
    return oid


class Tools:
    """plx.around, plx.aggregate and plx.process over one store and one reader."""

    def __init__(self, test, store):
        store.conn.commit()
        self.test = test
        self.base = ot.OntologyTools(ot.StoreReader(store.conn), POLICY, schema=SCHEMA,
                                     contracts=CONTRACTS, clock=lambda: NOW)
        self.around_tools = tt.TraverseTools(self.base)
        self.aggregate_tools = oa.AggregateTools(self.base)
        self.process_tools = op.ProcessTools(self.base, GRAPHS)

    def _valid(self, name, out):
        problems = validate(out, CONTRACTS["tools"][name]["output"], root=CONTRACTS)
        self.test.assertEqual(problems, [], json.dumps(out)[:2000])
        self.test.assertNotIn("error", out, json.dumps(out)[:2000])
        return out

    def around_tier(self, start, link_type, direction, far):
        """The tier plx.around serves on the edge from ``start`` to ``far``, asked at the
        weakest floor that still shows a lead, and whether the default floor counts it."""
        hop = {"from": start, "path": [{"link_type": link_type, "direction": direction}]}
        shown = self._valid("plx.around", self.around_tools.call(
            "plx.around", dict(hop, tier_floor="lead"), STAFF))
        [edge] = [e for e in shown["edges"] if {e["from"], e["to"]} == {start, far}]
        default = self._valid("plx.around", self.around_tools.call("plx.around", hop, STAFF))
        counted = far in [o["object_id"] for o in default["objects"]]
        return edge["tier"], counted

    def aggregate_counts(self, member, link_type, direction, far_property):
        """Whether plx.aggregate follows the link from ``member`` at its default floor:
        the member groups under the far record's value, never under null with a
        ``below_tier_floor`` gap."""
        out = self._valid("plx.aggregate", self.aggregate_tools.call("plx.aggregate", {
            "set": self.base.sets.new(STAFF, [member]), "measure": {"op": "count"},
            "group_by": [far_property],
            "path": [{"link_type": link_type, "direction": direction}]}, STAFF))
        self.test.assertNotIn("refused", out, out)
        [group] = out["groups"]
        reasons = {g["reason"] for g in out["gaps"]}
        if group["key"][far_property] is None:
            self.test.assertIn("below_tier_floor", reasons, out)
            return False
        self.test.assertNotIn("below_tier_floor", reasons, out)
        return True

    def process(self, root):
        answer = self.process_tools.process({"from": root, "process": "sales_order_lifecycle"},
                                            STAFF)
        self._valid("plx.process", answer.result)
        return {s["stage"]: s for s in answer.result["stages"]}


class OneTierTests(unittest.TestCase):
    def test_same_source_key_between_staging_rows_gets_one_tier_from_all_three(self):
        # A Portal order and its line, both Portal-native (staging): the Portal's own
        # foreign key between them exists whatever their lineage.
        s = Store()
        so = order(s, "so-s1", STAGING)
        ln = line(s, "so-s1-l1", STAGING, so)
        mo = assembly(s, "mo-s1", ln)
        t = Tools(self, s)
        self.assertEqual(t.around_tier(so, "SalesOrderLine.line_of", "reverse", ln),
                         ("verified", True))
        self.assertTrue(t.aggregate_counts(ln, "SalesOrderLine.line_of", "forward",
                                           "order_number"))
        # plx.process follows the same key (order -> line -> MO) and observes the MO;
        # the staging root still makes the answer a lead, never proven.
        mfg = t.process(so)["manufacturing_order"]
        self.assertEqual((mfg["state"], mfg["missing"]["state"], mfg["missing"]["reason"]),
                         ("missing", "unknown", "staging_root"))
        self.assertIn("observed reached", mfg["missing"]["detail"])
        self.assertNotIn("tier floor", mfg["missing"]["detail"])
        self.assertIn(mo, json.dumps(mfg))

    def test_text_join_to_a_staging_row_is_a_lead_in_all_three(self):
        # The same link type, stored by a text join (tier stored_by_text_join), between a
        # staging line and an authority order: capped at lead everywhere.
        s = Store()
        so = order(s, "so-g1", "gold_etl_from_fm")
        ln = line(s, "so-g1-l1", STAGING, so, tier="stored_by_text_join", kind="text_match")
        assembly(s, "mo-g1", ln)
        t = Tools(self, s)
        self.assertEqual(t.around_tier(so, "SalesOrderLine.line_of", "reverse", ln),
                         ("lead", False))
        self.assertFalse(t.aggregate_counts(ln, "SalesOrderLine.line_of", "forward",
                                            "order_number"))
        mfg = t.process(so)["manufacturing_order"]
        self.assertEqual((mfg["state"], mfg["missing"]["state"]), ("missing", "unknown"))
        self.assertIn("below the tier floor (lead)", mfg["missing"]["detail"])

    def test_cross_source_identity_link_to_a_staging_row_is_a_lead_in_all_three(self):
        # A Portal order and its BC twin billed by a BC invoice: a join across two
        # sources, never one source's own key. The authority order's twin link counts in
        # all three; the staging order's stays a lead in all three.
        s = Store()
        pairs = {}
        for key, lineage in (("so-g3", "gold_etl_from_fm"), ("so-s3", STAGING)):
            so = order(s, key, lineage)
            bc = s.obj(f"SalesOrder:bc.sales_orders:bc-{key}", "bc_company_a")
            s.val(bc, "order_number", "text", key.upper())
            s.link("SalesOrder.same_record_as", so, bc, kind="same_record")
            invoice = s.obj(f"SalesInvoice:bc.sales_invoices:inv-{key}", "bc_company_a")
            s.val(invoice, "status", "text", "Open")
            s.link("SalesInvoice.bills", invoice, bc)
            pairs[lineage] = (so, bc, invoice)
        t = Tools(self, s)
        for lineage, (tier, counted) in (("gold_etl_from_fm", ("verified", True)),
                                         (STAGING, ("lead", False))):
            so, bc, invoice = pairs[lineage]
            with self.subTest(lineage):
                self.assertEqual(t.around_tier(bc, "SalesOrder.same_record_as", "reverse", so),
                                 (tier, counted))
                self.assertEqual(t.aggregate_counts(bc, "SalesOrder.same_record_as",
                                                    "reverse", "order_number"), counted)
                # plx.process reads the invoice through the twin link (fallback path)
                # only when that link counts.
                stage = t.process(so)["customer_invoice"]
                self.assertEqual(invoice in stage["events"], counted, stage)
                if not counted:
                    self.assertEqual((stage["state"], stage["missing"]["state"]),
                                     ("missing", "unknown"))
                    self.assertNotIn("observed", stage["missing"]["detail"])


class ProcessRecordSplitTests(unittest.TestCase):
    """The documented split: one link tier, but plx.process never counts a staging
    record as evidence for an authority record."""

    def test_staging_record_behind_a_verified_key_is_never_process_evidence(self):
        s = Store()
        so = order(s, "so-g2", "gold_etl_from_fm")
        release = s.obj("ShipRelease:portal.app_ship_release:sr-2", STAGING)
        s.val(release, "status", "enum", "FULFILLED")
        s.link("ShipRelease.for_sales_order", release, so)
        t = Tools(self, s)
        # The link itself is the Portal's own key: verified in plx.around ...
        self.assertEqual(t.around_tier(so, "ShipRelease.for_sales_order", "reverse", release),
                         ("verified", True))
        self.assertTrue(t.aggregate_counts(release, "ShipRelease.for_sales_order", "forward",
                                           "order_number"))
        # ... and plx.process names that tier, but never counts the staging release.
        stage = t.process(so)["release_order"]
        self.assertEqual((stage["state"], stage["missing"]["state"]), ("missing", "unknown"))
        self.assertIn("ShipRelease.for_sales_order link (verified) reaches a "
                      "gold_native_staging record", stage["missing"]["detail"])
        self.assertNotIn(release, json.dumps(stage))


class SharedRuleTests(unittest.TestCase):
    """The one rule's input contract: anything it cannot read fails closed (a lead)."""

    ROW = {"tier": "verified", "from_lineage": STAGING, "to_lineage": STAGING,
           "binding_kind": "fk",
           "evidence_handles": json.dumps(["api://public.app_sales_order_line/a@1",
                                           "api://public.app_sales_order/b@1"])}

    def test_one_definition_serves_all_three_tools(self):
        self.assertIs(tt._effective_tier, ot._effective_tier)
        self.assertIs(oa._effective_tier, ot._effective_tier)
        self.assertIs(op._effective_tier, ot._effective_tier)
        self.assertIs(tt.NEVER_AUTHORITY, ot.NEVER_AUTHORITY)
        self.assertIs(op.NEVER_AUTHORITY, ot.NEVER_AUTHORITY)

    def test_unreadable_or_mixed_evidence_fails_closed(self):
        self.assertEqual(ot._effective_tier(self.ROW), "verified")
        cases = {
            "no binding columns": {k: v for k, v in self.ROW.items()
                                   if k not in ("binding_kind", "evidence_handles")},
            "not a key": dict(self.ROW, binding_kind="same_record"),
            "no evidence": dict(self.ROW, evidence_handles="[]"),
            "null evidence": dict(self.ROW, evidence_handles=None),
            "unparseable evidence": dict(self.ROW, evidence_handles="[not json"),
            "evidence not a list": dict(self.ROW, evidence_handles='{"a": 1}'),
            "unknown scheme": dict(self.ROW, evidence_handles='["zz://x/1@1"]'),
            "two sources": dict(self.ROW, evidence_handles=json.dumps(
                ["api://public.app_sales_order/b@1", "bc://company_a/salesOrders/c@1"])),
        }
        for name, row in cases.items():
            with self.subTest(name):
                self.assertEqual(ot._effective_tier(row), "lead")
        # Only a never-authority end is capped, and a lead is never raised.
        self.assertEqual(ot._effective_tier(dict(cases["two sources"], from_lineage="bc_a",
                                                 to_lineage=None)), "verified")
        self.assertEqual(ot._effective_tier(dict(self.ROW, tier="contradicted")),
                         "contradicted")


if __name__ == "__main__":
    unittest.main()
