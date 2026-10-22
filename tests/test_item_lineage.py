"""Item lineage (F13): an item's twins, the records that use and buy it, an FG item's
products, and a typed gap with its question for every item and screening-job link no source
the Workbench reads records (operating/item_lineage.py over operating/commercial_chain.py).

The engine tests drive the ``through`` closure directly; the plan tests build a store from
Portal rows (FileMaker packaging and raw material sharing a code with a Portal product, a
finished-product row using the packaging, supplier offers, a PO line, an FG item) and run
``plx.investigate`` for family F13.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest

from ontology import identifiers, store_ddl
from ontology import schema as schema_loader
from ontology.json_contract import validate
from ontology.project import CapturedRecord, build_store
from ontology.types import Actor
from operating import commercial_chain as cc
from operating import item_lineage as il
from operating import ontology_tools as ot
from operating import ontology_tools_investigate as oi
from operating import ontology_tools_traverse as tt

CONTRACTS = ot.load_contracts()
OUTPUT = CONTRACTS["tools"]["plx.investigate"]["output"]
NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.timezone.utc)
T0 = "2026-09-25T10:00:00.000000Z"
READER = Actor("plx", frozenset({"staff:ops"}), "restricted")

GOLD = 'Product:portal.app_product:{"id":"%s"}'
PACK = 'Product:portal.fm_packaging:{"id":%d}'
RAW = 'Product:portal.fm_raw_material:{"id":%d}'
BOM = 'ProductComponent:portal.fm_finished_product_item:{"id":%d}'
OFFER = 'SupplierItem:portal.app_supplier_product:{"id":"%s"}'
RAW_OFFER = 'SupplierItem:portal.fm_raw_material_supplier:{"id":%d}'
POL = 'PurchaseOrderLine:portal.app_purchase_order_line:{"id":"%s"}'
FG = 'FgItem:portal.app_fg_item:{"id":"%s"}'
TWIN = "Product.same_record_as"


def edge(link_type: str, frm: str, to: str, tier: str = "verified") -> dict:
    return {"link_id": f"{link_type}:{frm}:{to}", "link_type": link_type, "from": frm,
            "to": to, "tier": tier, "reason": "", "why_not": None}


def run(hop: dict, roots: list[str], edges: list) -> dict:
    evidence = {"r": [{"name": "r", "kind": "count", "value": len(roots), "basis": roots}]}
    return cc.plan_run({"roots": "r", "hops": [hop]}, evidence, edges, items={}, name="item",
                       as_of="2026-10-06", resolver=None, actor=READER, complete=True,
                       covers=lambda _: True)


OFFERS = {"label": "offers", "from_types": ["Product"], "link_types": ["SupplierItem.for_item"],
          "through": [TWIN], "depth": 2}


class EngineTests(unittest.TestCase):
    def test_a_twins_twin_stands_in_for_the_root_only_as_deep_as_depth(self):
        edges = [edge(TWIN, GOLD % "g1", PACK % 7), edge(TWIN, GOLD % "g1", RAW % 5),
                 edge("SupplierItem.for_item", RAW_OFFER % 50, RAW % 5)]
        out = run(OFFERS, [PACK % 7], edges)
        found = [e for e in out["entries"] if e["name"] != "item"]
        self.assertEqual([e["value"] for e in found], [RAW_OFFER % 50])
        self.assertEqual(found[0]["basis"], [PACK % 7, GOLD % "g1", RAW % 5, RAW_OFFER % 50])
        self.assertEqual(sum(TWIN in s for s in found[0]["steps"]), 2)
        self.assertEqual(out["gaps"], [])
        out = run(dict(OFFERS, depth=1), [PACK % 7], edges)
        self.assertEqual(out["entries"], [])
        self.assertEqual([(g["reason"], g["state"]) for g in out["gaps"]],
                         [("link_not_recorded", "unknown")])   # the depth cut a twin off

    def test_no_link_is_absent_only_when_the_closure_ran_out_inside_its_depth(self):
        out = run(OFFERS, [PACK % 7], [edge(TWIN, GOLD % "g1", PACK % 7)])
        self.assertEqual([(g["reason"], g["state"]) for g in out["gaps"]],
                         [("link_not_recorded", "absent_in_source")])

    def test_the_closure_is_capped(self):
        edges = [edge(TWIN, PACK % 1, RAW % n) for n in range(100)]
        edges.append(edge("SupplierItem.for_item", RAW_OFFER % 99, RAW % 99))
        out = run(OFFERS, [PACK % 1], edges)
        self.assertEqual(out["entries"], [])   # the 100th twin is past the 32-record cap
        self.assertEqual([g["state"] for g in out["gaps"]], ["unknown"])   # cut, not absent

    def test_a_purchase_orders_twin_reaches_the_payment_on_its_invoice(self):
        po, twin = 'PurchaseOrder:portal.app_purchase_order:{"id":"p1"}', \
            'PurchaseOrder:bc.purchase_orders:{"id":"b1"}'
        inv, pay = 'PurchaseInvoice:bc.purchase_invoices:{"id":"i1"}', \
            'Payment:bc.vendor_payments:{"id":"y1"}'
        plan = oi.load_plans(schema=ot.default_schema())["commercial_chain"]
        hop = next(h for item in plan.doc["evidence"] if item["kind"] == "compute"
                   for h in item["engine_args"]["hops"] if h["label"] == "invoice_payment")
        edges = [edge("PurchaseOrder.same_record_as", po, twin),
                 edge("PurchaseInvoice.invoices", inv, twin),
                 edge("Payment.applies_to_purchase_invoice", pay, inv)]
        out = run(hop, [po], edges)
        self.assertEqual([e["value"] for e in out["entries"] if e["name"] != "item"], [pay])
        self.assertEqual(out["entries"][0]["basis"], [po, twin, inv, pay])

    def test_the_plan_check_refuses_a_bad_depth(self):
        schema = ot.default_schema()
        doc = json.loads(json.dumps(oi.load_plans(schema=schema)["item_lineage"].doc))
        self.assertEqual(doc["families"], ["F13"])
        hops = doc["evidence"][1]["engine_args"]["hops"]
        bound = next(h for h in hops if "through" in h)
        bound["depth"] = 9
        del next(h for h in hops if h["label"] == "fg_minted")["link_types"]
        next(h for h in hops if "unbound" in h)["depth"] = 2
        problems = oi.check_plan(doc, schema, CONTRACTS)
        self.assertEqual(len(problems), 3, problems)
        self.assertIs(il.PLAN_ENGINES["item_lineage"]["run"], cc.plan_run)
        self.assertIn("item_lineage", oi.default_engines())

    def test_the_plan_questions_name_no_case_and_cost_per_good_unit_is_never_guessed(self):
        plan = oi.load_plans(schema=ot.default_schema())["item_lineage"]
        hops = [h for item in plan.doc["evidence"][1:] for h in item["engine_args"]["hops"]]
        labels = {h["label"] for h in hops}
        self.assertLessEqual({"item_twins", "proof_to_item", "replaced_item", "fg_revision",
                              "used_in_products", "alias_confirmed", "screened_from",
                              "screening_job", "screening_decision", "conversion_step",
                              "gl_lineage", "cost_policy"}, labels)
        for hop in hops:
            self.assertNotRegex(hop.get("question", ""), r"\d{3,}|PO-?\d|P-\d", hop["label"])
        decision = next(h for h in hops if h["label"] == "screening_decision")
        self.assertIn("never estimated", decision["unbound"]["detail"])


class Static:
    def __init__(self, source: str, records: list[CapturedRecord]) -> None:
        self.source, self._records = source, records

    def records(self, relations):
        return {r: [x for x in self._records if x.relation == r] for r in relations}

    def describe(self):
        return {"generation": "static", "relations": {}}


def portal(table: str, key, row: dict, n: int) -> CapturedRecord:
    relation = f"public.{table}"
    return CapturedRecord("portal_api", relation, json.dumps({"id": key}), {"id": key, **row},
                          f"api://{relation}/{key}@g-1", "row", f"{n:064x}", T0, None, T0,
                          f"api:{relation}")


class PlanTests(unittest.TestCase):
    """g1 (code PK-100) is copied by FileMaker packaging 7 and raw material 5; finished
    product row 70 uses packaging 7, raw material supplier row 50 offers raw material 5, sp1
    offers g1 and PO line l1 orders it. g2 (PK-200) has no twin and no use. FG item f1 was
    minted as g1."""

    @classmethod
    def setUpClass(cls):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        cls.tmp = tempfile.TemporaryDirectory()
        path = Path(cls.tmp.name) / "ontology.sqlite"
        rows = [
            portal("app_product", "g1", {"code": "PK-100", "custom_fields": {}}, 1),
            portal("app_product", "g2", {"code": "PK-200", "custom_fields": {}}, 2),
            portal("packaging", 7, {"packcode": "PK-100"}, 3),
            portal("raw_material", 5, {"rawcode": "PK-100"}, 4),
            portal("finished_product_item", 70, {"idpack": 7}, 5),
            portal("raw_material_supplier", 50, {"idraw": 5}, 6),
            portal("app_supplier_product", "sp1", {"product_id": "g1"}, 7),
            portal("app_purchase_order_line", "l1", {"product_id": "g1"}, 8),
            portal("app_fg_item", "f1", {"code": "FG-100", "minted_product_id": "g1"}, 9)]
        cls.compiled = schema_loader.load_schema()
        build_store(path, cls.compiled, {"portal_api": Static("portal_api", rows)},
                    normalizer=identifiers.normalize, clock=lambda: NOW)
        cls.path = path

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def investigate(self, root: str) -> dict:
        policy = ot.ReadPolicy(tenant="plx", audiences={"api:*": {"staff:ops"}},
                               markings={m: {"staff:ops"} for m in store_ddl.MARKINGS})
        reader = ot.StoreReader.open(self.path)
        self.addCleanup(reader.conn.close)
        base = ot.OntologyTools(reader, policy, schema=ot.SchemaView.from_compiled(self.compiled),
                                grammar=None, contracts=CONTRACTS, clock=lambda: NOW)
        table: dict = {}
        ot.register(table, base)
        tt.register(table, tt.TraverseTools(base))
        oi.register(table, oi.InvestigateTools(table, rows=lambda h, a: None,
                                               scope=oi.read_scope(base), clock=lambda: NOW))
        question, answer, out = {"family": "F13", "root": root}, [], None
        while True:   # the answer is paged by computed unit; read every page
            page = table["plx.investigate"]["handler"](question, READER)
            self.assertEqual(validate(page, OUTPUT, root=CONTRACTS), [], json.dumps(page)[:1500])
            self.assertNotIn("error", page, json.dumps(page)[:1500])
            out = out or page
            answer += [e for e in page["answer"] if e not in answer]
            cursor = page["completeness"].get("next_cursor")
            if not cursor:
                return dict(out, answer=answer)
            question = dict(question, cursor=cursor)

    @staticmethod
    def links(out: dict, item: str, label: str) -> list:
        return sorted(e["value"] for e in out["answer"]
                      if e["name"].startswith(f"{item}_{label}_"))

    @staticmethod
    def gaps(out: dict) -> dict:
        return {g["reason"]: g for g in out["gaps"]}

    def test_packaging_reaches_its_twins_uses_and_offers_through_the_portal_product(self):
        out = self.investigate(PACK % 7)
        self.assertEqual(self.links(out, "item_links", "item_twins"), [GOLD % "g1"])
        self.assertEqual(self.links(out, "item_links", "used_in_products"), [BOM % 70])
        self.assertEqual(self.links(out, "item_links", "supplier_offers"),
                         [OFFER % "sp1", RAW_OFFER % 50])
        self.assertEqual(self.links(out, "screening_job", "purchase_lines"), [POL % "l1"])
        gaps = self.gaps(out)
        for reason, state in (("artwork_item_link_not_served", "inaccessible"),
                              ("screening_decision_not_recorded", "unknown"),
                              ("gl_posting_lineage_not_ingested", "not_ingested"),
                              ("item_alias_not_recorded", "unknown")):
            self.assertEqual(gaps[reason]["state"], state, reason)
            self.assertIn("ask:", gaps[reason]["detail"])
        self.assertNotIn("item_twin_not_recorded", gaps)
        self.assertNotEqual(out["answerability"]["state"], "answerable")

    def test_an_item_with_no_recorded_link_is_a_typed_gap_never_a_guess(self):
        out = self.investigate(GOLD % "g2")
        self.assertEqual(self.links(out, "item_links", "item_twins"), [])
        gaps = self.gaps(out)
        for reason in ("item_twin_not_recorded", "item_not_in_any_bom", "item_not_purchased"):
            # The store captured no bc.items, so the reader cannot have seen every such link.
            self.assertEqual(gaps[reason]["state"], "unknown", reason)
        self.assertNotIn("fg_revision_not_served", gaps)   # an FG item's question

    def test_an_fg_item_reaches_the_product_it_was_minted_as(self):
        out = self.investigate(FG % "f1")
        self.assertEqual(self.links(out, "item_links", "fg_minted"), [GOLD % "g1"])
        gaps = self.gaps(out)
        self.assertIn("fg_reference_not_recorded", gaps)
        self.assertEqual(gaps["fg_revision_not_served"]["state"], "inaccessible")
        self.assertNotIn("screening_job_source_unknown", gaps)   # a Product's question


if __name__ == "__main__":
    unittest.main()
