"""plx.investigate family ``commercial_chain`` (F12): the recorded links of a quote, order or
purchase order, and a typed gap with the owner's question for each link no source records.

The engine (``operating/commercial_chain.py``) is tested on its own over hand-made edges, then
the plan runs at the tool boundary over synthetic Portal rows (quotes, sales orders and their
FileMaker twins, purchase orders and a receipt) projected by the real build; every output is
validated against the tool contract. Evidence label: isolated-fixture (synthetic records,
never live PLX data).
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
from operating import ontology_tools as ot
from operating import ontology_tools_investigate as oi
from operating import ontology_tools_traverse as tt

CONTRACTS = ot.load_contracts()
OUTPUT = CONTRACTS["tools"]["plx.investigate"]["output"]
NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.timezone.utc)
T0 = "2026-09-25T10:00:00.000000Z"
READER = Actor("plx", frozenset({"staff:ops"}), "restricted")

Q = 'Quote:portal.app_quote:{"id":"%s"}'
SO = 'SalesOrder:portal.app_sales_order:{"id":"%s"}'
FM = 'SalesOrder:portal.fm_order_entry:{"id":%d}'
PO = 'PurchaseOrder:portal.app_purchase_order:{"id":"%s"}'
RCPT = 'Receipt:portal.app_receipt:{"id":"%s"}'


def edge(link_type: str, frm: str, to: str, tier: str = "verified") -> dict:
    return {"link_id": f"{link_type}:{frm}:{to}", "link_type": link_type, "from": frm,
            "to": to, "tier": tier, "reason": "", "why_not": None}


def roots(*ids: str) -> dict:
    return {"r": [{"name": "r", "kind": "count", "value": len(ids), "basis": list(ids)}]}


def run(args: dict, evidence: dict, edges: list, *, complete: bool = True,
        covers=lambda _: True) -> dict:
    return cc.plan_run(args, evidence, edges, items={}, name="chain", as_of="2026-10-06",
                       resolver=None, actor=READER, complete=complete, covers=covers)


ORDER_TO_QUOTE = {"label": "order_to_quote", "from_types": ["SalesOrder"],
                  "link_types": ["SalesOrder.created_from_quote", "Quote.converted_to"],
                  "through": ["SalesOrder.same_record_as"], "gap": "order_quote_link_not_recorded",
                  "question": "Where is the quote of an order entered outside the Portal?",
                  "who_can_answer": "operations"}


class EngineTests(unittest.TestCase):
    def test_a_recorded_link_or_one_through_a_twin_is_an_entry(self):
        out = run({"roots": "r", "hops": [ORDER_TO_QUOTE]},
                  roots(SO % "s1", FM % 9),
                  [edge("SalesOrder.created_from_quote", SO % "s1", Q % "q1"),
                   edge("SalesOrder.same_record_as", SO % "s3", FM % 9),
                   edge("Quote.converted_to", Q % "q3", SO % "s3")])
        got = {e["name"]: e for e in out["entries"]}
        self.assertEqual(sorted(e["value"] for n, e in got.items() if n != "chain"),
                         [Q % "q1", Q % "q3"])
        self.assertEqual(got["chain"]["value"], 2)
        through = next(e for e in got.values() if e["value"] == Q % "q3")
        self.assertEqual(through["basis"], [FM % 9, SO % "s3", Q % "q3"])
        self.assertTrue(any("SalesOrder.same_record_as" in s for s in through["steps"]))
        self.assertEqual((out["gaps"], out["reasons"]), ([], []))

    def test_no_link_is_absent_only_when_the_walk_saw_every_such_link(self):
        args = {"roots": "r", "hops": [ORDER_TO_QUOTE]}
        out = run(args, roots(SO % "s2"), [])
        self.assertEqual([(g["reason"], g["state"]) for g in out["gaps"]],
                         [("order_quote_link_not_recorded", "absent_in_source")])
        self.assertIn("ask: Where is the quote", out["gaps"][0]["detail"])
        self.assertEqual(out["gaps"][0]["who_can_answer"], ["operations"])
        self.assertEqual(out["reasons"], ["order_quote_link_not_recorded"])
        for kwargs in ({"complete": False}, {"covers": lambda t: t != "Quote.converted_to"},
                       {"covers": lambda t: t != "SalesOrder.same_record_as"}):   # a hidden twin
            out = run(args, roots(SO % "s2"), [], **kwargs)
            self.assertEqual(out["gaps"][0]["state"], "unknown", kwargs)

    def test_a_lead_link_is_listed_as_a_lead_and_a_contradicted_one_is_none(self):
        args = {"roots": "r", "hops": [ORDER_TO_QUOTE]}
        out = run(args, roots(SO % "s1"),
                  [edge("SalesOrder.created_from_quote", SO % "s1", Q % "q1", "lead")])
        self.assertEqual([e["value"] for e in out["entries"][:-1]], [Q % "q1"])
        self.assertEqual([g["reason"] for g in out["gaps"]], ["link_lead_only"])
        self.assertEqual(out["reasons"], [])   # a lead is listed, not a missing piece
        out = run(args, roots(SO % "s1"),
                  [edge("SalesOrder.created_from_quote", SO % "s1", Q % "q1", "contradicted")])
        self.assertEqual(out["entries"], [])
        self.assertEqual([g["reason"] for g in out["gaps"]], ["order_quote_link_not_recorded"])

    def test_an_unbound_link_is_its_typed_gap_for_each_root_of_its_types(self):
        hop = {"label": "deposit_to_po", "from_types": ["PurchaseOrder"],
               "unbound": {"reason": "deposit_link_source_unknown", "state": "unknown",
                           "detail": "no source is known"},
               "question": "How is a deposit recorded?", "who_can_answer": "operations"}
        out = run({"roots": "r", "hops": [hop]}, roots(PO % "p1", SO % "s1"), [])
        self.assertEqual([(g["reason"], g["state"], g["subject"]) for g in out["gaps"]],
                         [("deposit_link_source_unknown", "unknown", f"deposit_to_po {PO % 'p1'}")])
        self.assertIn("How is a deposit recorded?", out["gaps"][0]["detail"])

    def test_no_root_is_its_own_gap(self):
        out = run({"roots": "r", "hops": [ORDER_TO_QUOTE]}, {"r": []}, [])
        self.assertEqual(out["reasons"], ["no_chain_root"])

    def test_the_plan_check_refuses_a_broken_hop(self):
        schema = ot.default_schema()
        plan = oi.load_plans(schema=schema)["commercial_chain"]
        self.assertEqual(plan.doc["families"], ["F12"])
        doc = json.loads(json.dumps(plan.doc))
        hops = doc["evidence"][-1]["engine_args"]["hops"]
        hops[0]["link_types"] = ["Quote.no_such_link"]
        hops[1]["from_types"] = ["NoSuchType"]
        hops[5]["unbound"]["state"] = "maybe"
        hops[6]["through"] = ["SalesOrder.same_record_as"]   # unbound hops have no through
        doc["evidence"][-1]["engine_args"]["roots"] = "commercial_links"
        problems = oi.check_plan(doc, schema, CONTRACTS)
        self.assertEqual(len(problems), 5, problems)

    def test_the_plan_questions_name_no_case(self):
        plan = oi.load_plans(schema=ot.default_schema())["commercial_chain"]
        hops = plan.doc["evidence"][-1]["engine_args"]["hops"]
        labels = {h["label"] for h in hops}
        self.assertLessEqual({"order_to_quote", "quote_to_order", "po_to_receipt",
                              "deposit_to_po", "supply_agreement", "won_tier_to_order_line",
                              "legacy_quote_screens", "complaint_to_resolution"}, labels)
        for hop in hops:
            self.assertNotRegex(hop.get("question", ""), r"\d{3,}|PO-?\d", hop["label"])


class Static:
    def __init__(self, source: str, records: list[CapturedRecord]) -> None:
        self.source, self._records = source, records

    def records(self, relations):
        return {r: [x for x in self._records if x.relation == r] for r in relations}

    def describe(self):
        return {"generation": "static", "relations": {}}


def portal(table: str, key, row: dict, n: int) -> CapturedRecord:
    relation = f"public.{table}"   # the Portal's table (FM order entries: public.order_entry)
    return CapturedRecord("portal_api", relation, json.dumps({"id": key}), {"id": key, **row},
                          f"api://{relation}/{key}@g-1", "row", f"{n:064x}", T0, None, T0,
                          f"api:{relation}")


class PlanTests(unittest.TestCase):
    """q1 was converted to s1 (both sides stamped); s3's conversion from q3 is stamped on the
    quote only and s3's FileMaker twin f3 is asked about; s2 was entered with no quote; p1 has
    a receipt, p2 none."""

    @classmethod
    def setUpClass(cls):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        cls.tmp = tempfile.TemporaryDirectory()
        path = Path(cls.tmp.name) / "ontology.sqlite"
        rows = [
            portal("app_quote", "q1", {"quote_number": "QUO-2026-0101", "status": "CONVERTED",
                                       "custom_fields": {"convertedSalesOrderId": "s1"}}, 1),
            portal("app_quote", "q2", {"quote_number": "QUO-2026-0102", "status": "DRAFT",
                                       "custom_fields": {}}, 2),
            portal("app_quote", "q3", {"quote_number": "QUO-2026-0103", "status": "CONVERTED",
                                       "custom_fields": {"convertedSalesOrderId": "s3"}}, 3),
            portal("app_sales_order", "s1", {"order_number": "SO-9101",
                                             "custom_fields": {"sourceQuoteId": "q1"}}, 4),
            portal("app_sales_order", "s2", {"order_number": "SO-9102", "custom_fields": {}}, 5),
            portal("app_sales_order", "s3", {"order_number": "SO-9103",
                                             "custom_fields": {"fmId": 3}}, 6),
            portal("order_entry", 3, {}, 7),
            portal("app_purchase_order", "p1", {"po_number": "PO109001", "custom_fields": {}}, 8),
            portal("app_purchase_order", "p2", {"po_number": "PO109002", "custom_fields": {}}, 9),
            portal("app_receipt", "r1", {"purchase_order_id": "p1"}, 10)]
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
        out = table["plx.investigate"]["handler"]({"family": "F12", "root": root}, READER)
        self.assertEqual(validate(out, OUTPUT, root=CONTRACTS), [], json.dumps(out)[:1500])
        self.assertNotIn("error", out, json.dumps(out)[:1500])
        return out

    @staticmethod
    def links(out: dict, label: str) -> list:
        return [e["value"] for e in out["answer"]
                if e["name"].startswith(f"commercial_links_{label}_")]

    @staticmethod
    def gaps(out: dict) -> dict:
        return {g["reason"]: g for g in out["gaps"]}

    def test_a_converted_quote_and_its_order_link_both_ways(self):
        out = self.investigate(Q % "q1")
        self.assertEqual(self.links(out, "quote_to_order"), [SO % "s1"])
        gaps = self.gaps(out)
        self.assertNotIn("quote_order_link_not_recorded", gaps)
        self.assertEqual(gaps["won_tier_not_recorded"]["state"], "absent_in_source")
        self.assertEqual(gaps["supply_agreement_source_unknown"]["state"], "unknown")
        self.assertIn("ask:", gaps["supply_agreement_source_unknown"]["detail"])
        out = self.investigate(SO % "s1")
        self.assertEqual(self.links(out, "order_to_quote"), [Q % "q1"])

    def test_a_twin_reaches_the_quote_its_portal_order_was_converted_from(self):
        out = self.investigate(FM % 3)
        self.assertEqual(self.links(out, "order_to_quote"), [Q % "q3"])

    def test_an_order_or_quote_with_no_recorded_link_is_a_typed_gap_never_a_guess(self):
        out = self.investigate(SO % "s2")
        self.assertEqual(self.links(out, "order_to_quote"), [])
        gap = self.gaps(out)["order_quote_link_not_recorded"]
        self.assertIn(gap["state"], ("absent_in_source", "unknown"))
        self.assertIn("ask:", gap["detail"])
        self.assertNotEqual(out["answerability"]["state"], "answerable")
        out = self.investigate(Q % "q2")
        self.assertIn("quote_order_link_not_recorded", self.gaps(out))

    def test_a_purchase_order_gets_its_receipt_and_a_deposit_question(self):
        out = self.investigate(PO % "p1")
        self.assertEqual(self.links(out, "po_to_receipt"), [RCPT % "r1"])
        gaps = self.gaps(out)
        self.assertEqual(gaps["deposit_link_source_unknown"]["state"], "unknown")
        self.assertIn("complaint_source_unknown", gaps)
        out = self.investigate(PO % "p2")
        self.assertIn("receipt_not_recorded", self.gaps(out))


if __name__ == "__main__":
    unittest.main()
