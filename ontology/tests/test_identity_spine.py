"""The identity spine: cross-system links on stored keys, candidates kept as candidates,
and the supplier Lot each receipt line carries.

Evidence label: ``isolated-fixture`` (synthetic captures through the real projector, the
compiled repository schema and the identifier grammar; no source is read).

* A Portal row that stores the key BC receives (a product code sent as the BC item No.,
  a customer's erp_code) links to that BC record on the stored key, held at lead while
  the BC company is unknown (never verified across systems by itself). An order number
  or a free-text PO matched across systems is a candidate: two BC orders of one number
  are two competing candidates, never one link.
* FM's customer PO (``custpocode``) is not unique: two orders carrying one PO are two
  CustomerPurchaseOrder records found as lead candidates, never joined exactly.
* Lot is served from the receipt lines that carry a supplier lot (FM lotnoqc, the gold
  line's supplier_lot_number): one Lot per carrying line, linked from that line
  (``ReceiptLine.received_lot``); a blank lot makes none, at any cardinality.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ontology import identifiers, identity_spine, schema
from ontology.core import Actor
from ontology.project import CapturedRecord, build_store
from operating import ontology_tools as ot

T = "2026-10-01T00:00:00+00:00"
RM, PK, GOLD = ("public.receipt_raw_material", "public.receipt_packaging",
                "public.app_receipt_line")
OPS = Actor("plx", frozenset({"ops"}))


class Source:
    """Portal rows (``portal_api``) or BC rows per company (``bc``: rows carry
    ``_company``)."""

    def __init__(self, source, rows):
        self.source, self.rows = source, rows

    def describe(self):
        return {"generation": 1, "relations": {}}

    def records(self, relations):
        return {rel: [self._record(rel, dict(row)) for row in self.rows.get(rel, [])]
                for rel in relations}

    def _record(self, rel, row):
        key = json.dumps({"id": row["id"]})
        digest = hashlib.sha256(f"{self.source}/{rel}/{key}".encode()).hexdigest()
        if self.source == "bc":
            company = row.pop("_company", "bc_manufacturing")
            return CapturedRecord("bc", rel, f"{company}/{row['id']}", row,
                                  f"bc://production/{company}/{rel}/{row['id']}@X", "row",
                                  digest, T, None, T, f"bc:production:{company}:{rel}",
                                  capture_lineage=company)
        return CapturedRecord("portal_api", rel, key, row, f"api://{rel}/{row['id']}@X", "row",
                              digest, T, None, T, f"api:{rel}")


def build(test, portal, bc=None):
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    path = Path(temp.name) / "store.sqlite"
    compiled = schema.load_schema()
    sources = {"portal_api": Source("portal_api", portal)}
    if bc is not None:
        sources["bc"] = Source("bc", bc)
    build_store(path, compiled, sources, normalizer=identifiers.normalize,
                clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc))
    conn = sqlite3.connect(str(path), check_same_thread=False)
    test.addCleanup(conn.close)
    tools = ot.OntologyTools(
        ot.StoreReader(conn),
        ot.ReadPolicy(tenant="plx", audiences={"api:*": {"ops"}, "bc:*": {"ops"}}),
        schema=ot.SchemaView.from_compiled(compiled), grammar=identifiers,
        item_gate=lambda actor, handle: True)
    return compiled, conn, tools


def links(conn, link_type):
    return [(r[0], r[1], r[2], json.loads(r[3] or "[]")) for r in conn.execute(
        "SELECT from_object, to_object, tier, why_not FROM links WHERE link_type = ? "
        "AND superseded_at IS NULL ORDER BY from_object, to_object", (link_type,))]


def spec_of(key):
    return json.loads((Path(__file__).resolve().parents[2] / "devloop" / "live-checks"
                       / "ontology.identity_spine.json").read_text())[key]


def raw(index, lot):
    return {"id": index, "idreceipt": 5, "idpogenitem": 7, "idlabid": f"26161-{index}",
            "sumqty": "10", "lotnoqc": lot, "expirydate": "2028-08-31"}


class CrossSystemLinks(unittest.TestCase):
    PORTAL = {
        "public.app_product": [{"id": "p-1", "code": "GLY-USP"}],
        "public.app_customer": [{"id": "c-1", "erp_code": "C0001", "name": "Acme Labs"}],
        "public.app_sales_order": [{"id": "so-1", "order_number": "SO100306",
                                    "customer_id": "c-1"}],
    }
    BC = {
        "items": [{"id": "i-1", "number": "GLY-USP"}],
        "customers": [{"id": "k-1", "number": "C0001", "displayName": "Acme Labs"}],
        # One order number in two BC companies: two candidates, never one record.
        "salesOrders": [{"id": "o-1", "number": "SO100306", "_company": "bc_manufacturing"},
                        {"id": "o-2", "number": "SO100306", "_company": "bc_distribution"}],
    }

    def test_a_stored_key_links_portal_to_bc_held_at_lead_by_the_company_cap(self):
        compiled, conn, _tools = build(self, self.PORTAL, self.BC)
        for link_type, frm, to in (
                ("Product.same_record_as", 'Product:portal.app_product:{"id":"p-1"}',
                 'Product:bc.items:{"id":"i-1"}'),
                ("Customer.same_record_as", 'Customer:portal.app_customer:{"id":"c-1"}',
                 'Customer:bc.customers:{"id":"k-1"}')):
            with self.subTest(link_type=link_type):
                (link,) = [row for row in links(conn, link_type) if row[0] == frm]
                self.assertEqual(link[1:3], (to, "lead"))
                self.assertIn({"feature": "tier_cap", "agrees": None, "cap": "lead",
                               "because": "bc_company_not_known"}, link[3])
        summary = identity_spine.summarize(conn, compiled, ("portal_api", "bc"))
        cross = summary["cross_system"]
        self.assertEqual((cross["stored_key"], cross["company_capped"],
                          cross["stored_key_by_tier"]), (2, 2, {"lead": 2}))
        self.assertEqual(cross["unclassed"], 0)
        self.assertEqual(summary["not_ingested"], [])

    def test_a_number_matched_by_text_stays_competing_candidates(self):
        compiled, conn, _tools = build(self, self.PORTAL, self.BC)
        so = 'SalesOrder:portal.app_sales_order:{"id":"so-1"}'
        matched = [row for row in links(conn, "SalesOrder.same_record_as") if row[0] == so]
        self.assertEqual({row[1] for row in matched}, {'SalesOrder:bc.sales_orders:{"id":"o-1"}',
                                                       'SalesOrder:bc.sales_orders:{"id":"o-2"}'})
        self.assertEqual({row[2] for row in matched}, {"lead"})
        cross = identity_spine.summarize(conn, compiled, ("portal_api", "bc"))["cross_system"]
        self.assertEqual((cross["candidates"], cross["competing"]), (2, 1))
        self.assertEqual(cross["candidates_by_tier"], {"lead": 2})
        # A find of the number is ambiguous among lead candidates, never a lone winner.
        found = _tools.call("plx.find", {"query": "SO100306", "object_type": "SalesOrder"}, OPS)
        self.assertGreaterEqual(len(found["candidates"]), 2, found)
        self.assertNotEqual(found["status"], "found")

    def test_a_name_only_match_stays_competing_lead_candidates(self):
        # No stored key joins these: the Portal supplier code is FM-style digits and the
        # customer has no erp_code. One name in two BC companies is two candidates.
        portal = {"public.app_supplier": [{"id": "s-1", "code": "40112", "name": "Acme Supply"}],
                  "public.app_customer": [{"id": "c-2", "company_name": "Beta Foods"}]}
        bc = {"vendors": [{"id": "v-1", "number": "V0001", "displayName": "Acme Supply"},
                          {"id": "v-2", "number": "V0002", "displayName": "Acme Supply",
                           "_company": "bc_distribution"}],
              "customers": [{"id": "k-2", "number": "C0002", "displayName": "Beta Foods"},
                            {"id": "k-3", "number": "C0003", "displayName": "Beta Foods",
                             "_company": "bc_distribution"}]}
        compiled, conn, _tools = build(self, portal, bc)
        for link_type, frm, targets in (
                ("Supplier.same_record_as", 'Supplier:portal.app_supplier:{"id":"s-1"}',
                 {'Supplier:bc.vendors:{"id":"v-1"}', 'Supplier:bc.vendors:{"id":"v-2"}'}),
                ("Customer.same_record_as", 'Customer:portal.app_customer:{"id":"c-2"}',
                 {'Customer:bc.customers:{"id":"k-2"}', 'Customer:bc.customers:{"id":"k-3"}'})):
            with self.subTest(link_type=link_type):
                matched = [row for row in links(conn, link_type) if row[0] == frm]
                self.assertEqual({row[1] for row in matched}, targets)
                self.assertEqual({row[2] for row in matched}, {"lead"})
        cross = identity_spine.summarize(conn, compiled, ("portal_api", "bc"))["cross_system"]
        self.assertEqual((cross["stored_key"], cross["candidates"], cross["competing"]),
                         (0, 4, 2))
        self.assertEqual((cross["candidates_by_tier"], cross["unclassed"]), ({"lead": 4}, 0))

    def test_a_name_match_on_a_stored_key_pair_is_not_a_candidate(self):
        # The projector stores links per binding: a pair joined on erp_code also holds the
        # company-name lead. That lead is not a second identity question for the pair.
        portal = {"public.app_customer": [{"id": "c-1", "erp_code": "C0001",
                                           "company_name": "Acme Labs"}]}
        bc = {"customers": [{"id": "k-1", "number": "C0001", "displayName": "Acme Labs"}]}
        compiled, conn, _tools = build(self, portal, bc)
        kinds = {row[0] for row in conn.execute(
            "SELECT binding_kind FROM links WHERE link_type = 'Customer.same_record_as' "
            "AND superseded_at IS NULL")}
        self.assertIn("text_match", kinds)
        cross = identity_spine.summarize(conn, compiled, ("portal_api", "bc"))["cross_system"]
        self.assertEqual((cross["stored_key"], cross["candidates"],
                          cross["candidates_on_stored_key"], cross["competing"]), (1, 0, 1, 0))

    def test_a_non_unique_fm_customer_po_never_links_exactly(self):
        compiled, conn, tools = build(self, {"public.order_entry": [
            {"id": 11, "custpocode": "PO-4471"}, {"id": 12, "custpocode": "PO-4471"}]})
        cpos = [r[0] for r in conn.execute(
            "SELECT object_id FROM objects WHERE object_type = 'CustomerPurchaseOrder'")]
        self.assertEqual(len(cpos), 2)
        verified = conn.execute(
            "SELECT COUNT(*) FROM links WHERE tier = 'verified' AND from_object IN (?, ?) "
            "AND to_object IN (?, ?)", (*cpos, *cpos)).fetchone()[0]
        self.assertEqual(verified, 0)
        found = tools.call("plx.find", {"query": "PO-4471",
                                        "object_type": "CustomerPurchaseOrder"}, OPS)
        self.assertEqual(found["status"], "ambiguous", found)
        self.assertEqual({c["object"]["object_id"] for c in found["candidates"]}, set(cpos))
        self.assertNotIn("verified", {c["tier"] for c in found["candidates"]})

    def test_a_portal_only_store_names_bc_not_ingested_and_counts_no_link(self):
        compiled, conn, _tools = build(self, self.PORTAL)
        summary = identity_spine.summarize(conn, compiled, ("portal_api", "bc"))
        self.assertEqual(summary["not_ingested"], ["bc"])
        self.assertEqual((summary["cross_system"]["stored_key"],
                          summary["cross_system"]["candidates"]), (0, 0))

    def test_an_unknown_source_or_a_non_store_fails_closed(self):
        compiled, conn, _tools = build(self, self.PORTAL)
        for bound in (("portal_api", "sage_direct"), ()):
            with self.subTest(bound=bound), self.assertRaisesRegex(
                    ValueError, "identity_spine_unknown_source"):
                identity_spine.summarize(conn, compiled, bound)
        empty = sqlite3.connect(":memory:")
        self.addCleanup(empty.close)
        with self.assertRaisesRegex(ValueError, "identity_spine_not_a_store"):
            identity_spine.summarize(empty, compiled, ("portal_api",))

    def test_the_summary_carries_counts_only(self):
        compiled, conn, _tools = build(self, self.PORTAL, self.BC)
        text = identity_spine.dumps(identity_spine.summarize(conn, compiled,
                                                             ("portal_api", "bc")))
        for value in ("GLY-USP", "C0001", "SO100306", "Acme", "p-1", "so-1", "i-1"):
            self.assertNotIn(value.casefold(), text.casefold())


class LiveProbe(unittest.TestCase):
    """``scripts.live_read_probes identity-spine`` over a session generation (stubbed
    ``open_session``) holding a projected fixture store."""

    def run_probe(self, opened):
        import contextlib
        import io
        from types import SimpleNamespace
        from unittest import mock

        from scripts import live_read_probes, ontology_projection_probe

        if opened is not None:
            path = Path(opened["store"])
            opened = {"manifest": {"sources": opened["sources"], "blocked_upstream": []},
                      "generation": SimpleNamespace(store=path, mode="reused")}
        out = io.StringIO()
        with mock.patch.object(ontology_projection_probe, "open_session",
                               return_value=opened), contextlib.redirect_stdout(out):
            code = live_read_probes.main(["identity-spine", "--session-projection", "s"],
                                         environ={})
        return code, json.loads(out.getvalue())

    def store(self, bc):
        _c, conn, _t = build(self, {**CrossSystemLinks.PORTAL,
                                    RM: [raw(1, "LOT-1")]}, bc)
        (path,) = [row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"]
        return path

    def test_the_probe_meets_its_spec_on_a_portal_and_bc_generation(self):
        code, out = self.run_probe({"store": self.store(CrossSystemLinks.BC),
                                    "sources": ["bc", "portal_api"]})
        spec = {"expect": spec_of("expect"), "record": spec_of("record")}
        self.assertEqual(code, spec["expect"]["exit_code"])

        def field(path):
            value = out
            for part in path.split("."):
                value = value[part]
            return value
        for path, want in spec["expect"]["json_equals"].items():
            self.assertEqual(field(path), want, path)
        for path, least in spec["expect"]["json_min"].items():
            self.assertGreaterEqual(field(path), least, path)
        for path in spec["expect"]["json_fields"] + spec["record"]:
            if path not in ("error", "sources_blocked"):
                field(path)
        self.assertNotIn("sources_blocked", out)
        for value in ("GLY-USP", "C0001", "SO100306", "LOT-1"):
            self.assertNotIn(value.casefold(), json.dumps(out).casefold())

    def test_a_generation_without_bc_is_blocked_and_none_is_a_fail(self):
        code, out = self.run_probe({"store": self.store(None), "sources": ["portal_api"]})
        self.assertEqual((code, out["sources_blocked"], out["not_ingested"],
                          out["session_sources"], out["sources"]["bc"]),
                         (0, ["bc"], ["bc"], ["portal_api"], 0))
        for path in spec_of("expect_blocked")["json_fields"]:
            self.assertIn(path, out)
        code, out = self.run_probe(None)
        self.assertEqual((out["error"], out["session_projection"]["opened"]),
                         ("session_projection_unavailable", False))


class LotOnReceiptLines(unittest.TestCase):
    def test_lot_is_served_from_the_receipt_lines_that_carry_a_supplier_lot(self):
        compiled = schema.load_schema()
        lot = compiled.object_types["Lot"]
        self.assertEqual(lot["availability"]["state"], "partial")
        self.assertEqual({(b["relation"], json.dumps(b["where"])) for b in lot["bindings"]}, {
            (RM, '[{"path": "lotnoqc", "op": "present"}]'),
            (PK, '[{"path": "lotnoqc", "op": "present"}]'),
            (GOLD, '[{"path": "supplier_lot_number", "op": "present"}]')})
        (number,) = [p for p in lot["properties"] if p["name"] == "lot_number"]
        self.assertEqual((number["value_type"], number["identifier_kind"]),
                         ("identifier", "lot_number"))
        (key,) = lot["identity_keys"]
        self.assertEqual((key["tier"], key["unique"]), ("lead", False))
        link = compiled.link_types["ReceiptLine.received_lot"]
        self.assertEqual((link["from"], link["to"], link["cardinality"]),
                         ("ReceiptLine", "Lot", "N:1"))
        self.assertEqual(compiled.object_types["InventoryLot"]["availability"]["state"],
                         "not_served")

    def test_each_line_with_a_lot_binds_one_lot_and_a_blank_lot_none(self):
        _compiled, conn, tools = build(self, {
            RM: [raw(11, " SUP-LOT-A1 "), raw(12, ""), raw(13, None)],
            PK: [{"id": 21, "idreceipt": 6, "idpogenitem": 8, "idlab": "RP0000001",
                  "sumqty": "5", "lotnoqc": "PK-9"}],
            GOLD: [{"id": "rl-1", "receipt_id": "r-1", "product_id": "p-1", "quantity": "4",
                    "uom": "kg", "lab_id": "26161-9", "supplier_lot_number": "GL-7"},
                   {"id": "rl-2", "receipt_id": "r-1", "product_id": "p-1", "quantity": "4",
                    "uom": "kg", "lab_id": "26161-8", "supplier_lot_number": "GL-7"}],
            "public.app_receipt": [{"id": "r-1", "receipt_number": "26161-9"}],
            "public.receipt": [{"id": 5}],
            "public.app_product": [{"id": "p-1", "code": "GLY-USP"}]})
        self.assertEqual([(f, t) for f, t, _tier, _w in links(conn, "ReceiptLine.received_lot")], [
            ('ReceiptLine:portal.app_receipt_line:{"id":"rl-1"}',
             'Lot:portal.app_receipt_line.supplier_lot:{"id":"rl-1"}'),
            ('ReceiptLine:portal.app_receipt_line:{"id":"rl-2"}',
             'Lot:portal.app_receipt_line.supplier_lot:{"id":"rl-2"}'),
            ('ReceiptLine:portal.fm_receipt_packaging:{"id":21}',
             'Lot:portal.fm_receipt_packaging.lotnoqc:{"id":21}'),
            ('ReceiptLine:portal.fm_receipt_raw_material:{"id":11}',
             'Lot:portal.fm_receipt_raw_material.lotnoqc:{"id":11}')])
        self.assertEqual({t for _f, t, _tier, _w in links(conn, "Lot.received_in")}, {
            'Receipt:portal.app_receipt:{"id":"r-1"}', 'Receipt:portal.fm_receipt:{"id":5}'})
        self.assertEqual({t for _f, t, _tier, _w in links(conn, "Lot.of_item")},
                         {'Product:portal.app_product:{"id":"p-1"}'})
        # One lot on two lines is two Lot records found as lead candidates, never one.
        found = tools.call("plx.find", {"query": "gl-7", "object_type": "Lot"}, OPS)
        self.assertEqual(found["status"], "ambiguous", found)
        self.assertEqual(len(found["candidates"]), 2)
        self.assertEqual({c["tier"] for c in found["candidates"]} & {"verified"}, set())
        # The lot names the lot, never the line holding it.
        line = tools.call("plx.find", {"query": "GL-7", "object_type": "ReceiptLine"}, OPS)
        self.assertEqual((line["status"], line["candidates"]), ("not_found", []))

    def test_every_line_binds_once_at_any_cardinality(self):
        compiled = schema.load_schema()
        for n in (1, 10, 60):
            with self.subTest(n=n):
                _c, conn, _t = build(self, {RM: [raw(i, f"SUP-{i:03d}")
                                                 for i in range(1, n + 1)] + [raw(n + 1, "")]})
                lot = identity_spine.summarize(conn, compiled, ("portal_api",))["lot"]
                self.assertEqual(lot, {"receipt_lines": n + 1,
                                       "receipt_lines_with_supplier_lot": n,
                                       "receipt_lines_linked": n, "lots": n,
                                       "lots_without_number": 0})

    def test_a_placeholder_lot_is_a_lot_without_a_number_never_linked_as_one(self):
        compiled, conn, _t = build(self, {RM: [raw(1, "N/A"), raw(2, "REAL-1")]})
        lot = identity_spine.summarize(conn, compiled, ("portal_api",))["lot"]
        self.assertEqual((lot["lots"], lot["lots_without_number"], lot["receipt_lines_linked"],
                          lot["receipt_lines_with_supplier_lot"]), (2, 1, 1, 1))


if __name__ == "__main__":
    unittest.main()
