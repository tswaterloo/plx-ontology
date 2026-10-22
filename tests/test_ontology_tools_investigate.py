"""Declarative plans over the generic primitives, on the fixture world.

The world is the synthetic oracle world the frozen dev items were drawn from
(``evals/oracle/fixture_world.py``), mirrored through the GET-only Portal client with an
injected transport and projected by the one projector (``scripts.eval_dev_baseline.
build_product``), so plx.find/object/around/timeline/process run their real code. Each
family's plan runs on a dev item of that family and the cited values are compared with the
item's oracle outcome. Every output is validated against the tool contract.
Evidence label: isolated-fixture (synthetic rows, never live PLX data).
"""

from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import unquote

from ontology import store_ddl
from ontology.json_contract import validate
from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_investigate as oi
from operating import ontology_tools_process as op
from operating import ontology_tools_traverse as tt

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = json.loads((ROOT / "operating/ontology_tool_contracts.json").read_text("utf-8"))
PLAN_FORMAT = json.loads((ROOT / "ontology/plans/_plan.schema.json").read_text("utf-8"))
DEV = ROOT / "evals" / "dev"


def _item(item_id: str) -> dict:
    return json.loads((DEV / f"{item_id}.json").read_text("utf-8"))


def _row(handle: str) -> tuple[str, str]:
    """(relation, canonical key) of an api:// citation: the row, any capture."""
    body = handle[len("api://"):].rsplit("@", 1)[0]
    relation, key = body.split("/", 1)
    return relation, json.dumps(json.loads(unquote(key)), sort_keys=True)


def _text(value):
    """A value as text: source text arrives as an untrusted_text envelope."""
    return value.get("untrusted_text") if isinstance(value, dict) else value


def _ok(test: unittest.TestCase, result: dict) -> None:
    test.assertEqual(validate(result, CONTRACTS["tools"]["plx.investigate"]["output"],
                              root=CONTRACTS), [])
    test.assertNotEqual((result.get("error") or {}).get("code"), "contract_violation", result)


class FixtureWorld:
    """One projection of the fixture world shared by the tests (built once)."""
    product = None
    tmp = None
    runtime = None
    schema = None

    @classmethod
    def build(cls) -> None:
        if cls.product is not None:
            return
        from evals import runner
        from ontology.schema import load_schema
        from operating.case_runtime import CaseRuntime
        from scripts import eval_dev_baseline as script
        cls.tmp = tempfile.TemporaryDirectory(prefix="plx-investigate-test-")
        items = runner.load_items(DEV, split="dev")
        relations = script.reachable_relations(
            load_schema(), {i["root"]["relation"] for i in items}, script.DEFAULT_LINK_DEPTH)
        cls.product = script.build_product(script.fixture_client(), Path(cls.tmp.name) / "w",
                                           relations, budget_seconds=None,
                                           clock=script.fixture_clock)
        cls.runtime = CaseRuntime(cls.product.state_dir, client=None,
                                  audience=cls.product.audience, tenant=script.TENANT)
        cls.schema = ot.default_schema()


def tearDownModule() -> None:  # noqa: N802 - unittest hook
    if FixtureWorld.runtime is not None:
        FixtureWorld.runtime.close()
    if FixtureWorld.tmp is not None:
        FixtureWorld.tmp.cleanup()
    # Unbuilt again: another module sharing the world in this process rebuilds it.
    FixtureWorld.product = FixtureWorld.tmp = FixtureWorld.runtime = None


class Base(unittest.TestCase):
    TENANT = "plx"
    PRINCIPAL = "eval:dev-baseline"  # the fixture product's mirror audience principal

    @classmethod
    def setUpClass(cls) -> None:
        FixtureWorld.build()
        from scripts.eval_oracle_dev import FIXTURE_NOW
        cls.at = FIXTURE_NOW
        cls.actor = Actor(cls.TENANT, frozenset({cls.PRINCIPAL}))

    def table(self, *, grants: dict | None = None, process: bool = False,
              drop: tuple[str, ...] = (), rows: bool = True,
              complete: bool = False) -> tuple[dict, oi.InvestigateTools]:
        reader = ot.StoreReader.open(FixtureWorld.product.store)
        self.addCleanup(reader.conn.close)
        who = {self.PRINCIPAL}
        audiences = grants if grants is not None else {
            key: set(who) for key in reader.audience_keys()}
        policy = ot.ReadPolicy(tenant=self.TENANT, audiences=audiences,
                               markings={m: set(who) for m in store_ddl.MARKINGS})
        base = ot.OntologyTools(reader, policy, schema=FixtureWorld.schema,
                                grammar=ot.default_grammar())
        table: dict = {}
        ot.register(table, base)
        tt.register(table, tt.TraverseTools(base))
        if process:
            op.register(table, op.ProcessTools(base))
        for name in drop:
            table.pop(name)
        tools = oi.InvestigateTools(
            table, schema=FixtureWorld.schema,
            rows=oi.citation_rows(FixtureWorld.runtime) if rows else None,
            complete=oi.relation_complete(base) if complete else None)
        self.hook = oi.relation_complete(base)
        oi.register(table, tools)
        return table, tools

    def investigate(self, table: dict, family: str, root, actor: Actor | None = None,
                    **extra) -> dict:
        arguments = {"family": family, "root": root, "as_known_at": self.at, **extra}
        result = table["plx.investigate"]["handler"](arguments, actor or self.actor)
        _ok(self, result)
        return result

    def assert_process_decides(self, result: dict, process: dict) -> None:
        """The three_way_match plan ran its plx.process step and publishes exactly that
        primitive's verdicts (dev-F4-0001: it decides none), never step_unavailable."""
        match = next(s for s in result["trace"] if s["step"] == "match")
        self.assertEqual((match["tool"], match["status"]), ("plx.process", "ok"))
        self.assertNotIn("step_unavailable", {g["reason"] for g in result["gaps"]})
        self.assertNotIn("step_unavailable", result["answerability"]["reasons"])
        verdicts = {e["name"]: e["value"] for e in result["answer"] if e["kind"] == "select"}
        for name, roles in (("ordered_vs_received", ["ordered", "received"]),
                            ("received_vs_invoiced", ["received", "invoiced"])):
            agrees = [c["agree"] for c in process["quantity_checks"] if c["roles"] == roles]
            decided = (False if False in agrees else None if None in agrees or not agrees
                       else True)
            self.assertEqual(verdicts[name], decided, name)
        self.assertLessEqual({g["reason"] for g in process["gaps"]},
                             {g["reason"] for g in result["gaps"]})

    @staticmethod
    def root_of(item: dict) -> dict:
        root = {"identifier": item["root"]["identifier"]}
        if item["root"].get("identifier_kind"):
            root["identifier_kind"] = item["root"]["identifier_kind"]
        return root

    @staticmethod
    def answer(result: dict, name: str) -> list[dict]:
        return [e for e in result["answer"]
                if e["name"] == name or e["name"].startswith(name + "_")
                and e["name"][len(name) + 1:].isdigit()]

    def assert_cites_expected_row(self, entry: dict, expected: dict) -> None:
        """The entry cites the same row and field as one of the oracle's citations."""
        wanted = {(_row(c["l1_handle"]), c["field_path"]) for c in expected["citations"]}
        got = (_row(entry["citation"]["l1_handle"]), entry["citation"]["field_path"])
        self.assertIn(got, wanted)


# =========================================================================== plans

class PlansAreDataTests(unittest.TestCase):
    def test_plans_validate_and_cover_the_four_eval_families(self):
        schema = ot.default_schema()
        plans = oi.load_plans(schema=schema)
        families = {f: p.id for p in plans.values() for f in p.doc["families"]}
        # F1-F4 are the evaluated families, F7 landed cost, F10 order margin, F11 decision
        # evidence, F12 commercial chain links, F13 item lineage,
        # F14 late PO, F15 counterparty position, F16 impact chain; held-out F5/F6 have no plan.
        self.assertEqual(set(families),
                         {"F1", "F2", "F3", "F4", "F7", "F10", "F11", "F12", "F13", "F14", "F15",
                          "F16"})
        evaluated = {json.loads(p.read_text("utf-8"))["family"]
                     for p in DEV.glob("dev-*.json")}
        self.assertLessEqual(evaluated, set(families))
        for plan in plans.values():
            self.assertRegex(plan.procedure, CONTRACTS["tools"]["plx.investigate"]["output"]
                             ["oneOf"][0]["properties"]["procedure"]["pattern"])
            self.assertEqual(validate(dict(plan.doc), PLAN_FORMAT, root=PLAN_FORMAT), [])

    def test_the_executor_names_no_type_link_or_property(self):
        """Plans are data: no object type, link type, plan id or family appears in the
        executor's code."""
        schema = ot.default_schema()
        source = (ROOT / "operating/ontology_tools_investigate.py").read_text("utf-8")
        code = source.split('"""', 2)[2]  # past the module docstring
        for name in list(schema.object_types) + list(schema.link_types):
            self.assertNotIn(f'"{name}"', code, name)
        output_keys = set(CONTRACTS["tools"]["plx.investigate"]["output"]["oneOf"][0]
                          ["properties"])
        for plan in oi.load_plans(schema=schema).values():
            if plan.id not in output_keys:  # "chain" is also the contract's output key
                self.assertNotIn(f'"{plan.id}"', code)
            for family in plan.doc["families"]:
                self.assertNotIn(f'"{family}"', code)

    def _broken(self, mutate) -> list[str]:
        doc = copy.deepcopy(json.loads((ROOT / "ontology/plans/order_quantities.json")
                                       .read_text("utf-8")))
        mutate(doc)
        return oi.check_plan(doc, ot.default_schema(), CONTRACTS)

    def test_loader_refuses_names_the_schema_or_contracts_do_not_declare(self):
        self.assertEqual(self._broken(lambda d: None), [])

        def undeclared_link(d):
            d["steps"][1]["args"]["path"][0]["link_type"] = "SalesOrderLine.made_up"
        def unknown_tool(d):
            d["steps"][1]["tool"] = "plx.sql"
        def bad_args(d):
            d["steps"][1]["args"]["limit"] = 999
        def disconnected(d):
            d["steps"][3]["from"] = "root"  # AssemblyOrder.for_order_line starts at a line
        def unknown_property(d):
            d["evidence"][0]["properties"] = ["made_up_quantity"]
        def forward_reference(d):
            d["steps"][2]["from"] = "shipments"
        def self_call(d):
            d["steps"][0]["tool"] = "plx.investigate"
        def links_beside_a_path(d):
            d["steps"][1]["links"] = {"binding_kind": "mention", "direction": "reverse"}
        def links_reaching_nothing(d):
            del d["steps"][1]["args"]["path"]
            d["steps"][1]["links"] = {"binding_kind": "made_up", "direction": "reverse"}
        def one_unknown_source(d):
            d["steps"][2]["from"] = ["root", "nowhere"]
        def distinct_property(d):
            d["evidence"][0]["distinct"] = "content"
        def party_check_off_around(d):
            d["steps"][0]["party_check"] = {}
        def party_check_against_unknown(d):
            d["steps"][1]["party_check"] = {"against": ["root", "later_or_nowhere"]}
        def zero_on_a_property(d):
            d["evidence"][0]["zero"] = "complete_search"
        def zero_on_a_collection_count(d):
            d["evidence"].append({"name": "roots", "kind": "count", "from": "root",
                                  "zero": "complete_search"})
        def zero_of_another_kind(d):
            d["evidence"].append({"name": "n", "kind": "count", "object_type": "SalesOrder",
                                  "bindings": ["portal.app_sales_order"], "zero": "assumed"})
        for mutate in (undeclared_link, unknown_tool, bad_args, disconnected,
                       unknown_property, forward_reference, self_call, links_beside_a_path,
                       links_reaching_nothing, one_unknown_source, distinct_property,
                       party_check_off_around, party_check_against_unknown,
                       zero_on_a_property, zero_on_a_collection_count, zero_of_another_kind):
            with self.subTest(mutate.__name__):
                self.assertTrue(self._broken(mutate))

    def test_a_family_claimed_twice_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("_plan.schema.json", "order_quantities.json"):
                (Path(tmp) / name).write_text(
                    (ROOT / "ontology/plans" / name).read_text("utf-8"), encoding="utf-8")
            doc = json.loads((Path(tmp) / "order_quantities.json").read_text("utf-8"))
            doc["id"] = "copy_of_quantities"
            (Path(tmp) / "copy_of_quantities.json").write_text(json.dumps(doc), "utf-8")
            with self.assertRaises(oi.PlanError):
                oi.load_plans(tmp, schema=ot.default_schema())


class FakeHostTests(unittest.TestCase):
    """The executor alone over canned primitive outputs (no store): every verdict is the
    deciding primitive's own, a crashing primitive is a gap, leads are labelled per entry,
    and the output still meets the contract."""
    DIGEST = "a" * 64
    PO = 'PurchaseOrder:portal.app_purchase_order:{"id":"p"}'
    LINES = {"PurchaseOrderLine.line_of": ["PurchaseOrderLine:portal.app_purchase_order_line:o"],
             "ReceiptLine.line_of": ["ReceiptLine:portal.app_receipt_line:r"]}
    ITEM = "Product:portal.app_product:i"

    def _meta(self):
        return {"schema_digest": self.DIGEST, "as_known_at": "2099-01-02", "freshness": []}

    def _ref(self, oid):
        return {"object_id": oid, "object_type": oid.split(":")[0], "display": None,
                "lineage": "gold_etl_from_fm"}

    def _edge(self, link, frm, to, tier):
        return {"link_id": f"L:{frm}:{to}", "link_type": link, "from": frm, "to": to,
                "tier": tier, "reason": [{"feature": "fk", "agrees": True}], "why_not": []}

    def table(self, checks=None, *, tiers=None, truncated=False, gaps=(), item=None,
              item_cites=(), tick=None):
        """``checks``: plx.process quantity_checks as (roles, agree) pairs (None: plx.process
        is not served); ``tiers``: link type -> edge tier (default verified); ``gaps``:
        plx.process's own gaps; ``item``: the product a line is for (default ``ITEM``),
        ``item_cites``: the feature citations of that for_item edge; ``tick``: called on
        every primitive call (a test clock)."""
        item = item or self.ITEM
        relation = {"PurchaseOrderLine": "app_purchase_order_line",
                    "ReceiptLine": "app_receipt_line"}
        done = {"complete": True, "truncated": False, "returned": 1}
        tiers = tiers or {}

        def obj(arguments, actor):
            oid = arguments["object_id"]
            kind = oid.split(":")[0]
            values = []
            if kind in relation:
                prop, role = (("ordered_quantity", "ordered") if kind == "PurchaseOrderLine"
                              else ("received_quantity", "received"))
                values.append({"property": prop, "value_type": "quantity", "value": "5",
                               "unit": "kg", "quantity_role": role, "markings": [],
                               "citation": {"l1_handle": f"api://public.{relation[kind]}/x@g-1",
                                            "field_path": "row.quantity", "digest": "b" * 64,
                                            "lineage": "gold_etl_from_fm"}})
            return {"object": self._ref(oid), "values": values, "conflicts": [],
                    "identity": [], "linked_counts": [], "gaps": [], "completeness": done,
                    "meta": self._meta()}

        def around(arguments, actor):
            last = arguments["path"][-1]["link_type"]
            start = arguments["from"]
            tier = tiers.get(last, "verified")
            if last.endswith(".for_item"):
                ends = [item]
                edges = [self._edge(last, start, item, tier)]
                if item_cites:
                    edges[0]["reason"][0]["citations"] = [dict(c) for c in item_cites]
            else:
                ends = self.LINES.get(last, [])
                edges = [self._edge(last, e, start, tier) for e in ends]
            # No set handle: the executor then starts each next step per object.
            return {"hops": [{"link_type": last, "direction": "reverse", "count": len(ends)}],
                    "edges": edges, "objects": [self._ref(e) for e in ends], "gaps": [],
                    "completeness": dict(done, returned=len(ends)), "meta": self._meta()}

        def process(arguments, actor):
            value = {"value_type": "quantity", "value": "5", "markings": [], "property":
                     "ordered_quantity", "citation": {
                         "l1_handle": "api://public.app_purchase_order_line/x@g-1",
                         "field_path": "row.quantity", "digest": "b" * 64,
                         "lineage": "gold_etl_from_fm"}}
            return {"process": "purchase_to_invoice", "stages": [],
                    "quantity_checks": [{"roles": list(roles), "values": [value], "agree": agree}
                                        for roles, agree in checks],
                    "gaps": list(gaps), "completeness": dict(done, truncated=truncated),
                    "meta": self._meta()}

        def boom(arguments, actor):
            raise RuntimeError("primitive crashed")
        table = {"plx.object": {"handler": obj}, "plx.around": {"handler": around},
                 "plx.timeline": {"handler": boom}}
        if checks is not None:
            table["plx.process"] = {"handler": process}
        if tick is not None:
            def ticking(handler):
                def call(arguments, actor):
                    tick()
                    return handler(arguments, actor)
                return call
            table = {name: {"handler": ticking(spec["handler"])} for name, spec in table.items()}
        return table

    def plan(self):
        def around(sid, frm, *hops):
            return {"id": sid, "tool": "plx.around", "from": frm, "bind": "from",
                    "args": {"path": [{"link_type": l, "direction": d} for l, d in hops]}}

        def verdict(name, roles=None):
            item = {"name": name, "kind": "select", "required": True, "step": "match",
                    "path": "quantity_checks.*.agree", "reduce": "all"}
            if roles:
                item["where"] = {"roles": roles}
            return item
        doc = {"$schema": "_plan.schema.json", "format": oi.PLAN_FORMAT, "id": "fake_match",
               "families": ["F7"], "title": "t", "description": "d",
               "root": {"object_types": ["PurchaseOrder"]}, "step_budget": 20,
               "steps": [
                   around("pol", "root", ("PurchaseOrderLine.line_of", "reverse")),
                   {"id": "pol_read", "tool": "plx.object", "from": "pol", "bind": "object_id"},
                   around("pol_item", "pol", ("PurchaseOrderLine.for_item", "forward")),
                   around("rl", "root", ("Receipt.receives", "reverse"),
                          ("ReceiptLine.line_of", "reverse")),
                   {"id": "rl_read", "tool": "plx.object", "from": "rl", "bind": "object_id"},
                   {"id": "events", "tool": "plx.timeline", "from": "root", "bind": "from"},
                   {"id": "match", "tool": "plx.process", "from": "root", "bind": "from",
                    "args": {"process": "purchase_to_invoice"}, "required": True}],
               "evidence": [
                   {"name": "ordered", "kind": "property", "object_type": "PurchaseOrderLine",
                    "properties": ["ordered_quantity"], "quantity_role": "ordered"},
                   {"name": "received", "kind": "property", "object_type": "ReceiptLine",
                    "properties": ["received_quantity"], "quantity_role": "received"},
                   verdict("ordered_vs_received", ["ordered", "received"]),
                   verdict("received_vs_invoiced", ["received", "invoiced"]),
                   verdict("three_way_match")]}
        self.assertEqual(validate(doc, PLAN_FORMAT, root=PLAN_FORMAT), [])
        self.assertEqual(oi.check_plan(doc, ot.default_schema(), CONTRACTS), [])
        return {"fake_match": oi.Plan(doc, "procedure:fake_match@" + "0" * 16)}

    def run_with(self, checks=None, **table):
        tools = oi.InvestigateTools(self.table(checks, **table), plans=self.plan(),
                                    schema=ot.default_schema(), rows=lambda h, a: None)
        result = tools.call("plx.investigate", {"family": "F7", "root": self.PO},
                            Actor("plx", frozenset({"x"})))
        _ok(self, result)
        return result

    @staticmethod
    def verdicts(result):
        return {e["name"]: e["value"] for e in result["answer"] if e["kind"] == "select"}

    # ---- time budget: the caller's clock stops the run like a spent step budget

    def timed_run(self, **arguments):
        clock = [0.0]

        def tick():
            clock[0] += 1.0   # every primitive call takes one second
        tools = oi.InvestigateTools(self.table([(("ordered", "received"), True)], tick=tick),
                                    plans=self.plan(), schema=ot.default_schema(),
                                    rows=lambda h, a: None, timer=lambda: clock[0])
        result = tools.call("plx.investigate", {"family": "F7", "root": self.PO, **arguments},
                            Actor("plx", frozenset({"x"})))
        _ok(self, result)
        return result

    def test_a_time_budget_stops_the_run_and_says_so(self):
        """A plan whose primitives outlast the caller's time budget stops calling them
        once it passes (the item still finishes before the run's deadline): the answer is
        partial, names time_budget_exhausted and a time_budget gap, and the steps not run are
        in the trace; without a time budget the same plan runs every step."""
        whole = self.timed_run()
        self.assertNotIn("time_budget_exhausted", whole["answerability"]["reasons"])
        self.assertFalse(any(s["status"] == "budget_exhausted" for s in whole["trace"]))
        cut = self.timed_run(time_budget_ms=2500)
        self.assertIn("time_budget_exhausted", cut["answerability"]["reasons"])
        self.assertNotEqual(cut["answerability"]["state"], "answerable")
        self.assertIn(("unknown", "time_budget"),
                      {(g["state"], g["reason"]) for g in cut["gaps"]})
        self.assertLessEqual(cut["steps"]["used"], 3)
        self.assertLess(cut["steps"]["used"], whole["steps"]["used"])
        self.assertIn("budget_exhausted", {s["status"] for s in cut["trace"]})
        # match (plx.process, required) never ran, so no verdict is published as decided
        self.assertEqual(set(self.verdicts(cut).values()) - {None}, set())

    def test_a_time_budget_outside_the_contract_is_refused(self):
        for bad in (0, -5, 600001, "30s", 1.5, True):
            with self.subTest(bad=bad):
                result = self.timed_run(time_budget_ms=bad)
                self.assertEqual(result["error"]["code"], "invalid_input")

    # ---- a counted record cited by the edge that reached it

    def count_plan(self):
        def around(sid, frm, *hops):
            return {"id": sid, "tool": "plx.around", "from": frm, "bind": "from",
                    "args": {"path": [{"link_type": l, "direction": d} for l, d in hops]}}
        doc = {"$schema": "_plan.schema.json", "format": oi.PLAN_FORMAT, "id": "fake_count",
               "families": ["F8"], "title": "t", "description": "d",
               "root": {"object_types": ["PurchaseOrder"]}, "step_budget": 20,
               "steps": [around("pol", "root", ("PurchaseOrderLine.line_of", "reverse")),
                         around("pol_item", "pol", ("PurchaseOrderLine.for_item", "forward"))],
               "evidence": [{"name": "product_count", "kind": "count",
                             "object_type": "Product", "bindings": ["portal.app_product"]}]}
        self.assertEqual(validate(doc, PLAN_FORMAT, root=PLAN_FORMAT), [])
        self.assertEqual(oi.check_plan(doc, ot.default_schema(), CONTRACTS), [])
        return {"fake_count": oi.Plan(doc, "procedure:fake_count@" + "0" * 16)}

    def counted(self, cites):
        product = 'Product:portal.app_product:{"id":"i"}'
        tools = oi.InvestigateTools(self.table(item=product, item_cites=cites),
                                    plans=self.count_plan(), schema=ot.default_schema(),
                                    rows=lambda h, a: None)
        result = tools.call("plx.investigate", {"family": "F8", "root": self.PO},
                            Actor("plx", frozenset({"x"})))
        _ok(self, result)
        return next(e for e in result["answer"] if e["name"] == "product_count")

    def test_a_count_cites_a_record_by_the_edge_that_reached_it(self):
        """A counted object with no value or display of its own (its plx.object read cites
        nothing) is cited by the reference row the reader was shown on the edge that reached
        it, only when that row is the object's own record: its binding's Portal relation
        under exactly its own key. Any other row, relation or scheme cites nothing, and the
        count then lists no records (never another row's citation)."""
        own = {"l1_handle": "api://public.app_product/%7B%22id%22%3A%22i%22%7D@00000000000000aa-1",
               "field_path": "row.id", "digest": "c" * 64, "lineage": "gold_etl_from_fm"}
        self.assertNotIn("records", self.counted(()))
        entry = self.counted([own])
        self.assertEqual(entry["value"], 1)
        self.assertEqual(entry["records"], [{"l1_handle": own["l1_handle"],
                                             "field_path": "row.id"}])
        for other in (dict(own, l1_handle="api://public.app_product/%7B%22id%22%3A%22j%22%7D@00000000000000aa-1"),
                      dict(own, l1_handle="api://public.app_supplier/%7B%22id%22%3A%22i%22%7D@00000000000000aa-1"),
                      dict(own, l1_handle="bc://prod/c1/items/%7B%22id%22%3A%22i%22%7D@00000000000000aa-1"),
                      dict(own, l1_handle="api://public.app_product/not-json@00000000000000aa-1")):
            with self.subTest(handle=other["l1_handle"]):
                entry = self.counted([other])
                self.assertEqual(entry["value"], 1)
                self.assertNotIn("records", entry)

    def test_verdicts_are_the_process_primitives_per_role_pair(self):
        result = self.run_with([(("ordered", "received"), True), (("ordered", "received"), True),
                                (("received", "invoiced"), False)])
        self.assertEqual(self.verdicts(result), {"ordered_vs_received": True,
                                                 "received_vs_invoiced": False,
                                                 "three_way_match": False})

    def test_an_undecided_check_is_never_read_as_agreement(self):
        result = self.run_with([(("ordered", "received"), True), (("ordered", "received"), None),
                                (("received", "invoiced"), True)])
        self.assertEqual(self.verdicts(result), {"ordered_vs_received": None,
                                                 "received_vs_invoiced": True,
                                                 "three_way_match": None})
        self.assertIn(("unknown", "not_decided", "ordered_vs_received"),
                      {(g["state"], g["reason"], g["subject"]) for g in result["gaps"]})
        self.assertIn("missing_ordered_vs_received", result["answerability"]["reasons"])
        # A decided disagreement settles "all" even next to an undecided item.
        result = self.run_with([(("ordered", "received"), None), (("ordered", "received"), False)])
        self.assertIs(self.verdicts(result)["ordered_vs_received"], False)
        # A truncated process result never yields agreement.
        result = self.run_with([(("ordered", "received"), True)], truncated=True)
        self.assertIsNone(self.verdicts(result)["ordered_vs_received"])

    def test_no_process_check_decides_nothing(self):
        """plx.process decided nothing: the plan publishes no verdict although the cited
        ordered and received quantities are equal (the executor never compares them)."""
        result = self.run_with([])
        self.assertEqual({e["value"] for e in self.answer_of(result, "ordered", "received")},
                         {"5"})
        self.assertEqual(set(self.verdicts(result).values()), {None})

    def test_without_plx_process_every_verdict_is_step_unavailable(self):
        result = self.run_with(None)
        self.assertEqual(set(self.verdicts(result).values()), {None})
        gaps = {(g["reason"], g["subject"]) for g in result["gaps"]}
        for name in ("ordered_vs_received", "received_vs_invoiced", "three_way_match"):
            self.assertIn(("step_unavailable", name), gaps)
        self.assertIn(("step_unavailable", "plx.process (match)"), gaps)
        self.assertIn("required_step_incomplete", result["answerability"]["reasons"])
        # a crashing primitive is a gap too, never a crash
        events = next(s for s in result["trace"] if s["step"] == "events")
        self.assertEqual((events["status"], events["reason"]), ("error", "tool_exception"))
        self.assertIn(("step_failed", "plx.timeline (events)"), gaps)
        # A row reader that reads nothing: the claim gate fails the claims, never passes.
        self.assertTrue(result["claim_checks"]["checked"])
        self.assertFalse(result["claim_checks"]["passed"])

    def test_entries_resting_on_lead_links_are_labelled(self):
        verified = self.run_with([])
        self.assertFalse(any("lead_basis" in e for e in verified["answer"]))
        lead = self.run_with([], tiers={"ReceiptLine.line_of": "lead"})
        marked = {e["name"]: e.get("lead_basis") for e in lead["answer"]
                  if e["kind"] == "property"}
        self.assertEqual(marked, {"ordered": None, "received": True})

    def test_the_listing_follows_the_recorder_and_host_state_gaps_ask_nobody(self):
        """docs/decisions/2026-10-10-follow-ups-ask-loop-writes.md: a host that records
        follow-ups lists plx.investigate as a writer, as its contract's read_only says; one
        that does not lists it read-only and refuses the option, writing nothing. A gap about
        the host's own state (a source it configures no reader for, a capture state its
        projection does not state) stays in the answer and is never a follow-up, whoever the
        resolver names for it (an undeclared owner, or the root's department as here); a gap
        a department answers is."""
        written = []

        class Cases:
            def get(self, actor, case_id):
                return {"gaps": [], "findings": {}}

        class Path:  # an ActionPath's two calls the recorder makes, recording what it writes
            cases = Cases()

            def execute_internal(self, actor, action, case_id, parameters, *, session=None):
                written.append(parameters)
                return {"read_back": {"record_id": f"G{len(written)}", "outcome": "verified"}}
        host_state = [{"state": "unknown", "reason": "freshness_unknown", "subject": "bc",
                       "detail": "the projection does not state this source's capture state"},
                      {"state": "not_ingested", "reason": "source_not_configured",
                       "subject": "graph_mail", "detail": "this host configures no reader"}]
        self.assertIs(CONTRACTS["tools"]["plx.investigate"]["read_only"], False)
        arguments = {"family": "F7", "root": self.PO,
                     "follow_ups": {"case_id": "case-" + "0" * 32}}
        for recorder in (None, oi.follow_up_recorder(Path(), None)):
            with self.subTest(recorder=recorder is not None):
                tools = oi.InvestigateTools(
                    self.table([(("ordered", "received"), None)], gaps=host_state),
                    plans=self.plan(), schema=ot.default_schema(), rows=lambda h, a: None,
                    follow_ups=recorder)
                listed: dict = {}
                oi.register(listed, tools)
                self.assertIs(listed["plx.investigate"]["annotations"]["readOnlyHint"],
                              recorder is None)
                result = listed["plx.investigate"]["handler"](arguments,
                                                              Actor("plx", frozenset({"x"})))
                _ok(self, result)
                if recorder is None:
                    self.assertEqual(result["error"]["code"], "follow_ups_not_configured")
                    self.assertEqual(written, [])
                    continue
                # In the answer, each naming whom the resolver found (here the root's).
                self.assertTrue(all({g["reason"]: g for g in result["gaps"]}[gap["reason"]]
                                    ["who_can_answer"] for gap in host_state))
                recorded = [w["missing"].rsplit(": ", 1)[1] for w in written]
                self.assertIn("NOT_DECIDED", recorded)
                self.assertFalse({"FRESHNESS_UNKNOWN", "SOURCE_NOT_CONFIGURED"} & set(recorded))

    @staticmethod
    def answer_of(result, *names):
        return [e for e in result["answer"] if e["name"] in names]

    def test_the_plan_format_has_no_comparison_of_its_own(self):
        doc = self.plan()["fake_match"].doc
        broken = copy.deepcopy(dict(doc))
        broken["evidence"].append({"name": "match", "kind": "check", "left": "ordered",
                                   "right": "received"})
        self.assertTrue(validate(broken, PLAN_FORMAT, root=PLAN_FORMAT))
        # A count over a type with several bindings must name them (twins never add up).
        counted = copy.deepcopy(dict(doc))
        counted["evidence"].append({"name": "order_count", "kind": "count",
                                    "object_type": "PurchaseOrderLine"})
        self.assertTrue(any("several bindings" in p for p in
                            oi.check_plan(counted, ot.default_schema(), CONTRACTS)))
        counted["evidence"][-1]["bindings"] = ["portal.app_purchase_order_line"]
        self.assertEqual(oi.check_plan(counted, ot.default_schema(), CONTRACTS), [])
        # where filters the elements of a select path's `*` only
        filtered = copy.deepcopy(dict(doc))
        filtered["evidence"][2]["path"] = "process"
        self.assertTrue(oi.check_plan(filtered, ot.default_schema(), CONTRACTS))
        # per groups a select path's `*`, numbered only as a property item listed before
        grouped = copy.deepcopy(dict(doc))
        grouped["evidence"][2].update(per="group", number_as="received")
        self.assertEqual(oi.check_plan(grouped, ot.default_schema(), CONTRACTS), [])
        grouped["evidence"][2]["number_as"] = "three_way_match"
        self.assertTrue(oi.check_plan(grouped, ot.default_schema(), CONTRACTS))

    # ------------------------------------------------------------- cited inputs

    def _cited(self, rel, key, value, role=None, prop="quantity", value_type="quantity"):
        out = {"property": prop, "value_type": value_type, "value": value, "markings": [],
               "citation": {"l1_handle": f"api://public.{rel}/{key}@g-1",
                            "field_path": f"row.{prop}", "digest": "b" * 64,
                            "lineage": "gold_etl_from_fm"}}
        if value_type == "quantity":
            out["unit"] = "ea"
        if role:
            out["quantity_role"] = role
        return out

    def _cited_run(self, evidence, steps, *, ends, checks=(), budget=40, displays=True):
        """plx.investigate over a fake host: plx.around from the root reaches ``ends``
        (pages of 50), each read by plx.object with one cited value; plx.process answers
        ``checks``."""
        done = {"complete": True, "truncated": False, "returned": 1}

        def obj(arguments, actor):
            oid = arguments["object_id"]
            key = oid.rsplit(":", 1)[1]
            values = [] if oid == self.PO else [self._cited(
                "app_product", key, {"untrusted_text": key.upper(), "origin": {
                    "citation": f"api://public.app_product/{key}@g-1"}}, prop="code",
                value_type="text")]
            return {"object": self._ref(oid), "values": values, "conflicts": [],
                    "identity": [], "linked_counts": [], "gaps": [], "completeness": done,
                    "meta": self._meta()}

        def around(arguments, actor):
            start = int(arguments.get("cursor") or 0)
            page = ends[start:start + 50]
            more = start + 50 < len(ends)
            return {"hops": [{"link_type": "PurchaseOrderLine.line_of", "direction": "reverse",
                              "count": len(ends)}],
                    "edges": [self._edge("PurchaseOrderLine.line_of", e, self.PO, "verified")
                              for e in page],
                    "objects": [dict(self._ref(e), display=None if not displays else {
                        "untrusted_text": e, "origin": {
                            "citation": f"api://public.x/{e.rsplit(':', 1)[1]}@g-1",
                            "field_path": "row.id"}}) for e in page],
                    "gaps": [], "completeness": dict(done, returned=len(page), complete=not more,
                                                     next_cursor=str(start + 50) if more
                                                     else None),
                    "meta": self._meta()}

        def process(arguments, actor):
            return {"process": "purchase_to_invoice", "stages": [],
                    "quantity_checks": list(checks), "gaps": [], "completeness": done,
                    "meta": self._meta()}
        table = {"plx.object": {"handler": obj}, "plx.around": {"handler": around},
                 "plx.process": {"handler": process}}
        doc = {"$schema": "_plan.schema.json", "format": oi.PLAN_FORMAT, "id": "fake_cited",
               "families": ["F7"], "title": "t", "description": "d",
               "root": {"object_types": ["PurchaseOrder"]}, "step_budget": budget,
               "steps": steps, "evidence": evidence}
        self.assertEqual(validate(doc, PLAN_FORMAT, root=PLAN_FORMAT), [])
        self.assertEqual(oi.check_plan(doc, ot.default_schema(), CONTRACTS), [])
        tools = oi.InvestigateTools(table, plans={"fake_cited": oi.Plan(
                                        doc, "procedure:fake_cited@" + "0" * 16)},
                                    schema=ot.default_schema(), rows=lambda h, a: None)
        result = tools.call("plx.investigate", {"family": "F7", "root": self.PO},
                            Actor("plx", frozenset({"x"})))
        _ok(self, result)
        self.assertEqual(result["steps"]["used"], sum(s["calls"] for s in result["trace"]))
        return result

    def test_per_item_verdicts_carry_the_cells_plx_process_compared(self):
        """One verdict per plx.process check group, numbered as the item entries (by code,
        not by key), each with the cited cells its checks compared; a price check is not a
        quantity verdict (where_any keeps the two quantity role pairs), an ungrouped check is
        a gap, the overall verdict stays whole."""
        a, b = "Product:portal.app_product:a", "Product:portal.app_product:b"
        cells = {n: self._cited("app_purchase_order_line", n, v, r) for n, v, r in (
            ("c1", "5", "ordered"), ("c2", "5", "received"), ("c3", "5", "invoiced"),
            ("c4", "3", "ordered"), ("c5", "2", "received"))}
        checks = [
            {"roles": ["ordered", "received"], "values": [cells["c1"], cells["c2"]],
             "agree": True, "group": a},
            {"roles": ["received", "invoiced"], "values": [cells["c2"], cells["c3"]],
             "agree": True, "group": a},
            {"roles": ["ordered", "invoiced"], "values": [cells["c1"]], "agree": False,
             "group": a},
            {"roles": ["ordered", "received"], "values": [cells["c4"], cells["c5"]],
             "agree": False, "group": b},
            {"roles": ["received", "invoiced"], "values": [], "agree": None}]
        result = self._cited_run(
            [{"name": "item", "kind": "property", "object_type": "Product",
              "properties": ["code"], "numbering": "value"},
             {"name": "match", "kind": "select", "required": True, "step": "match",
              "path": "quantity_checks.*.agree", "where_any": [
                  {"roles": ["ordered", "received"]}, {"roles": ["received", "invoiced"]}],
              "reduce": "all", "per": "group", "number_as": "item"},
             {"name": "three_way_match", "kind": "select", "step": "match",
              "path": "quantity_checks.*.agree", "reduce": "all"}],
            [{"id": "items", "tool": "plx.around", "from": "root", "bind": "from",
              "args": {"path": [{"link_type": "PurchaseOrderLine.line_of",
                                 "direction": "reverse"},
                                {"link_type": "PurchaseOrderLine.for_item",
                                 "direction": "forward"}]}},
             {"id": "item_read", "tool": "plx.object", "from": "items", "bind": "object_id"},
             {"id": "match", "tool": "plx.process", "from": "root", "bind": "from",
              "args": {"process": "purchase_to_invoice"}}],
            ends=[a, b], checks=checks)
        by_name = {e["name"]: e for e in result["answer"]}
        self.assertEqual({n: _text(by_name[n]["value"]) for n in ("item_1", "item_2")},
                         {"item_1": "B", "item_2": "A"} if a > b else
                         {"item_1": "A", "item_2": "B"})
        self.assertEqual(by_name["item_2"]["object_id"], b)  # numbered by code, not key
        self.assertEqual((by_name["match_1"]["value"], by_name["match_2"]["value"]),
                         (True, False))
        self.assertEqual([c["citation"] for c in by_name["match_1"]["cells"]],
                         [cells[n]["citation"] for n in ("c1", "c2", "c3")])
        self.assertEqual([c["citation"] for c in by_name["match_2"]["cells"]],
                         [cells[n]["citation"] for n in ("c4", "c5")])
        self.assertIs(by_name["three_way_match"]["value"], False)
        self.assertNotIn("cells", by_name["three_way_match"])  # an element compared nothing
        self.assertIn(("not_grouped", "match"),
                      {(g["reason"], g["subject"]) for g in result["gaps"]})

    def test_a_count_cites_each_counted_record_at_any_cardinality(self):
        """Every counted object gets a citation of its own record (its display, else a
        plx.object read within the step budget); none when one is missing or over 100."""
        lines = [f"PurchaseOrderLine:portal.app_purchase_order_line:l{i:03d}"
                 for i in range(101)]
        steps = [{"id": "lines", "tool": "plx.around", "from": "root", "bind": "from",
                  "args": {"path": [{"link_type": "PurchaseOrderLine.line_of",
                                     "direction": "reverse"}]}}]
        evidence = [{"name": "root_record_count", "kind": "count", "from": "root"},
                    {"name": "line_count", "kind": "count", "object_type": "PurchaseOrderLine",
                     "bindings": ["portal.app_purchase_order_line"]}]
        for n, displays in ((1, False), (10, True), (60, False), (60, True)):
            with self.subTest(n=n, displays=displays):
                result = self._cited_run(evidence, steps, ends=lines[:n], budget=128,
                                         displays=displays)
                counted = {e["name"]: e for e in result["answer"]}
                entry = counted["line_count"]
                self.assertEqual((entry["value"], len(entry["basis"])), (n, n))
                keys = [oid.rsplit(":", 1)[1] for oid in entry["basis"]]
                self.assertEqual([r["l1_handle"] for r in entry["records"]], [
                    f"api://public.{'x' if displays else 'app_product'}/{k}@g-1" for k in keys])
                # The root shows no value of its record, even when read: counted, uncited.
                self.assertEqual(counted["root_record_count"]["value"], 1)
                self.assertNotIn("records", counted["root_record_count"])
                reads = [s for s in result["trace"] if s["step"] == oi.RECORD_READS]
                self.assertEqual(sum(s["calls"] for s in reads), 1 + (0 if displays else n))
        # A budget too small to read every counted record: the count stands, uncited.
        result = self._cited_run(evidence, steps, ends=lines[:10], budget=6, displays=False)
        entry = next(e for e in result["answer"] if e["name"] == "line_count")
        self.assertEqual(entry["value"], 10)
        self.assertNotIn("records", entry)
        self.assertIn(("step_budget", "line_count"),
                      {(g["reason"], g["subject"]) for g in result["gaps"]})
        # Over 100 counted objects: the basis is capped, so no records either.
        result = self._cited_run(evidence, steps, ends=lines, budget=128)
        entry = next(e for e in result["answer"] if e["name"] == "line_count")
        self.assertEqual((entry["value"], len(entry["basis"])), (101, 100))
        self.assertNotIn("records", entry)


# =========================================================================== families

class PartyCheckTests(unittest.TestCase):
    """A plx.around step's ``party_check`` over canned primitives: records reached because
    they share a number with the root but whose reference identifier (the BC invoice's
    vendor number, ``ontology.resolve.corroborations``) conflicts with the root's party
    and agrees with none leave the collection and become one declared ``party_differs``
    gap, never a silent pick; a side this reader cannot read proves nothing (the record
    stays, no gap); records the step budget leaves unchecked stay and are said to be
    unchecked. Evidence label: isolated-fixture."""
    PO = 'PurchaseOrder:portal.app_purchase_order:{"id":"p"}'
    SUPPLIER = 'Supplier:portal.app_supplier:{"id":"s"}'

    @staticmethod
    def invoice(i):
        return 'PurchaseInvoice:bc.purchase_invoices:{"id":"i%02d"}' % i

    def table(self, n, *, supplier_visible=True):
        meta = {"schema_digest": "a" * 64, "as_known_at": "2099-01-02", "freshness": []}
        invoices = [self.invoice(i) for i in range(n)]
        calls = {"object": [], "around": []}

        def ref(oid):
            return {"object_id": oid, "object_type": oid.split(":")[0], "display": None,
                    "lineage": "gold_etl_from_fm"}

        def ident(prop, value, handle, path):
            return {"property": prop, "value_type": "identifier", "value": value,
                    "markings": [], "citation": {"l1_handle": handle, "field_path": path,
                                                 "digest": "b" * 64, "lineage": "bc"}}

        def obj(arguments, actor):
            oid = arguments["object_id"]
            calls["object"].append(oid)
            values = []
            if oid in invoices:
                i = invoices.index(oid)
                values.append(ident("vendor_number", "V1" if i % 2 == 0 else "V2",
                                    f"bc://prod/c1/purchaseInvoices/i{i:02d}@g-1",
                                    "row.vendorNumber"))
            elif oid == self.SUPPLIER and supplier_visible:
                values.append(ident("code", "V1", "api://public.app_supplier/s@g-1",
                                    "row.code"))
            return {"object": ref(oid), "values": values, "conflicts": [], "identity": [],
                    "linked_counts": [], "gaps": [], "meta": meta,
                    "completeness": {"complete": True, "truncated": False, "returned": 1}}

        def around(arguments, actor):
            link = arguments["path"][-1]["link_type"]
            calls["around"].append((arguments["from"], link))
            if link == "PurchaseInvoice.names_order" and arguments["from"] == self.PO:
                ends = invoices
            elif link == "PurchaseOrder.ordered_from" and arguments["from"] == self.PO \
                    and supplier_visible:
                ends = [self.SUPPLIER]
            else:
                ends = []
            edges = [{"link_id": f"L:{e}", "link_type": link, "from": e, "to": self.PO,
                      "tier": "lead", "reason": [{"feature": "number", "agrees": True}],
                      "why_not": []} for e in ends]
            return {"hops": [{"link_type": link, "direction": "reverse", "count": len(ends)}],
                    "edges": edges, "objects": [ref(e) for e in ends], "gaps": [], "meta": meta,
                    "completeness": {"complete": True, "truncated": False,
                                     "returned": len(ends)}}
        return {"plx.object": {"handler": obj}, "plx.around": {"handler": around}}, calls

    def plan(self, budget):
        doc = {"$schema": "_plan.schema.json", "format": oi.PLAN_FORMAT, "id": "fake_party",
               "families": ["F9"], "title": "t", "description": "d",
               "root": {"object_types": ["PurchaseOrder"]}, "step_budget": budget,
               "steps": [{"id": "named", "tool": "plx.around", "from": "root", "bind": "from",
                          "args": {"path": [{"link_type": "PurchaseInvoice.names_order",
                                             "direction": "reverse"}]},
                          "party_check": {}}],
               "evidence": [{"name": "invoice_count", "kind": "count", "from": "named"}]}
        self.assertEqual(validate(doc, PLAN_FORMAT, root=PLAN_FORMAT), [])
        self.assertEqual(oi.check_plan(doc, ot.default_schema(), CONTRACTS), [])
        return {"fake_party": oi.Plan(doc, "procedure:fake_party@" + "0" * 16)}

    def run_plan(self, n, *, budget=128, supplier_visible=True):
        table, calls = self.table(n, supplier_visible=supplier_visible)
        tools = oi.InvestigateTools(table, plans=self.plan(budget),
                                    schema=ot.default_schema(), rows=lambda h, a: None)
        result = tools.call("plx.investigate", {"family": "F9", "root": self.PO},
                            Actor("plx", frozenset({"x"})))
        _ok(self, result)
        return result, calls

    @staticmethod
    def kept(result):
        entry = next(e for e in result["answer"] if e["name"] == "invoice_count")
        return entry["value"], list(entry.get("basis") or ())

    def test_another_partys_same_number_records_are_a_declared_gap(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                result, calls = self.run_plan(n)
                value, basis = self.kept(result)
                own = {self.invoice(i) for i in range(n) if i % 2 == 0}
                other = {self.invoice(i) for i in range(n) if i % 2 == 1}
                self.assertEqual(value, len(own))
                self.assertEqual(set(basis), own)
                self.assertEqual(len(basis), len(set(basis)))
                gaps = [g for g in result["gaps"] if g["reason"] == "party_differs"]
                if other:
                    self.assertEqual(len(gaps), 1, gaps)
                    self.assertTrue(gaps[0]["subject"].startswith(
                        f"{len(other)} bc:purchaseInvoices records share the number"),
                        gaps[0])
                    # The detail names them (as many as its length allows), only those.
                    left_out = gaps[0]["detail"].split("left out: ", 1)[1]
                    for oid in other:
                        self.assertNotIn(oid, basis)
                    for oid in own:
                        self.assertNotIn(oid, left_out)
                else:
                    self.assertEqual(gaps, [])
                # Never in the chain either, and every record accounted for once.
                chain = json.dumps(result["chain"])
                for oid in other:
                    self.assertNotIn(json.dumps(oid)[1:-1], chain)
                self.assertEqual(value + len(other), n)
                self.assertLessEqual(result["steps"]["used"], result["steps"]["budget"])
                # The root, one check read per invoice, the root's supplier hop and read
                # once, and one record read per counted invoice: linear in N.
                self.assertEqual(len(calls["object"]), 2 + n + len(own))
                self.assertEqual(calls["around"].count((self.PO, "PurchaseOrder.ordered_from")),
                                 1)

    def test_a_spent_budget_keeps_the_unchecked_records_and_says_so(self):
        result, _ = self.run_plan(60, budget=20)
        value, basis = self.kept(result)
        self.assertLessEqual(result["steps"]["used"], 20)
        gaps = {(g["state"], g["reason"], g["subject"]) for g in result["gaps"]}
        self.assertIn(("unknown", "step_budget", "named: party check"), gaps)
        dropped = sum(int(g["subject"].split(" ", 1)[0]) for g in result["gaps"]
                      if g["reason"] == "party_differs")
        self.assertEqual(value + dropped, 60)
        self.assertEqual(len(basis), len(set(basis)))
        self.assertNotEqual(result["answerability"]["state"], "answerable")

    def test_a_reader_who_cannot_see_the_roots_party_drops_nothing(self):
        result, _ = self.run_plan(10, supplier_visible=False)
        value, basis = self.kept(result)
        self.assertEqual(value, 10)
        self.assertEqual(set(basis), {self.invoice(i) for i in range(10)})
        self.assertNotIn("party_differs", {g["reason"] for g in result["gaps"]})
        self.assertNotIn("app_supplier", json.dumps(result))


class FamilyTests(Base):
    def test_f3_quantities_cite_the_ordered_line_and_report_missing_roles(self):
        item = _item("dev-F3-0001-fixture")
        table, _ = self.table()
        result = self.investigate(table, "F3", self.root_of(item))
        self.assertTrue(result["procedure"].startswith("procedure:order_quantities@"))
        expected = {v["name"]: v for v in item["known_outcome"]["values"]}
        ordered = self.answer(result, "ordered")
        self.assertEqual(len(ordered), 1)
        self.assertEqual(ordered[0]["quantity_role"], "ordered")
        self.assertEqual(float(ordered[0]["value"]), float(expected["ordered"]["value"]))
        self.assertEqual(ordered[0]["unit"], expected["ordered"]["unit"])
        self.assert_cites_expected_row(ordered[0], expected["ordered"])
        # The claim gate re-read the row: the value holds; the role contract for this
        # column is not declared yet, so the role is unverified (never a silent pass).
        self.assertIn(ordered[0]["check"], ("passed", "unverified"))
        self.assertNotEqual(ordered[0].get("code"), "claim_value_mismatch")
        # produced: the assembly order's yield is empty in the source; shipped: nothing
        for name in ("produced", "shipped"):
            self.assertIsNone(expected[name]["value"])
            self.assertTrue(all(e["value"] is None for e in self.answer(result, name)), name)
        # Each gap names the answer entry it leaves null (``concerns``): the oracle's gaps
        # name the same values in their own words, so a scorer ties them by value.
        gaps = {(g["state"], g["reason"], g["subject"], g.get("concerns"))
                for g in result["gaps"]}
        self.assertIn(("absent_in_source", "value_empty", "AssemblyOrder.final_quantity",
                       "produced"), gaps)
        # No shipment of the order is not ingested, never absence; named by its role.
        self.assertIn(("not_ingested", "no_object_reached", "shipped quantity", "shipped"), gaps)
        self.assertEqual({g["concerns"] for g in item["known_outcome"]["gaps"]},
                         {"produced", "shipped"})
        self.assertEqual(result["answerability"]["state"], "partial")
        self.assertIn("missing_produced", result["answerability"]["reasons"])
        self.assertTrue(result["claim_checks"]["checked"])
        self.assertTrue(result["claim_checks"]["passed"])
        # Every cited answer value is also in values with its citation.
        cited = {(v["citation"]["l1_handle"], v["citation"]["field_path"])
                 for v in result["values"]}
        for entry in result["answer"]:
            if entry.get("citation") and entry["value"] is not None:
                self.assertIn((entry["citation"]["l1_handle"],
                               entry["citation"]["field_path"]), cited)

    def test_f3_mixed_unit_lines_state_the_canonical_unit_of_the_line_each_cites(self):
        from ontology.units import UOM_CANONICAL, unit_token
        table, _ = self.table()
        rows = oi.citation_rows(FixtureWorld.runtime)
        item = _item("dev-F3-0005-fixture")   # one cse and one ea line, shipped alike
        result = self.investigate(table, "F3", self.root_of(item))
        # Every quantity read whole: no unserved column the question never asked for
        # (FM totalprodqty_c, statusqc) makes the answer partial.
        self.assertEqual(result["answerability"], {"state": "answerable", "reasons": []})
        self.assertNotIn("not_served", {g["reason"] for g in result["gaps"]})
        quantities = [e for e in result["answer"] if e.get("quantity_role")]
        self.assertEqual(len(quantities), 5)
        for entry in quantities:
            cell = rows(entry["citation"]["l1_handle"], self.actor)["row"]
            # The unit stated is the canonical spelling of the very row the entry cites.
            self.assertIn(entry["unit"], UOM_CANONICAL)
            self.assertEqual(entry["unit"], unit_token(cell["uom"]), entry["name"])
        for expected in item["known_outcome"]["values"]:
            entry = next(e for e in quantities if (_row(e["citation"]["l1_handle"]),
                                                   e["citation"]["field_path"]) in
                         {(_row(c["l1_handle"]), c["field_path"])
                          for c in expected["citations"]})
            self.assertEqual((float(entry["value"]), entry["unit"], entry["quantity_role"]),
                             (float(expected["value"]), expected["unit"],
                              expected["quantity_role"]), expected["name"])
        # The produced figure is recorded in "each": stated as ea, while the claim gate
        # checked the cited cell in its own spelling and passed it.
        produced = self.answer(result, "produced")[0]
        read = table["plx.object"]["handler"](
            {"object_id": produced["object_id"], "properties": ["final_quantity"],
             "as_known_at": self.at}, self.actor)
        self.assertEqual([v["unit"] for v in read["values"]], ["each"])
        self.assertEqual(produced["unit"], "ea")
        self.assertTrue(result["claim_checks"]["checked"])
        self.assertTrue(result["claim_checks"]["passed"], result["claim_checks"])
        # An order no assembly order is linked to: produced is not ingested (the FM ETL never
        # links one), named by its role, and the answer is partial.
        result = self.investigate(table, "F3", self.root_of(_item("dev-F3-0004-fixture")))
        self.assertIn(("not_ingested", "no_object_reached", "produced quantity", "produced"),
                      {(g["state"], g["reason"], g["subject"], g.get("concerns"))
                       for g in result["gaps"]})
        self.assertEqual(result["answerability"]["state"], "partial")

    def test_f2_customer_po_reports_every_carrying_order_with_cited_status(self):
        item = _item("dev-F2-0001-fixture")
        table, _ = self.table()
        result = self.investigate(table, "F2", self.root_of(item))
        self.assertNotEqual(result["answerability"]["state"], "blocked", result["answerability"])
        expected = {v["name"]: v for v in item["known_outcome"]["values"]}
        numbers = self.answer(result, "order_number")
        match = [e for e in numbers if _text(e["value"])
                 == expected["order_number"]["value"]]
        self.assertEqual(len(match), 1, numbers)
        self.assert_cites_expected_row(match[0], expected["order_number"])
        status = [e for e in self.answer(result, "order_status")
                  if _row(e["citation"]["l1_handle"]) ==
                  _row(expected["order_status"]["citations"][0]["l1_handle"])]
        self.assertEqual([_text(e["value"]) for e in status],
                         [expected["order_status"]["value"]])
        shipped = self.answer(result, "shipment_status")
        self.assertTrue(any(_text(e["value"]) == expected["shipment_statuses"]["value"]
                            for e in shipped))
        # Counts are of reached, visible records and say which ones.
        count = self.answer(result, "order_count")[0]
        self.assertEqual(count["value"], len(count["basis"]))
        self.assertTrue(all(oid.startswith("SalesOrder:") for oid in count["basis"]))
        for entry in result["answer"]:
            if entry["kind"] == "property" and entry["value"] is not None:
                self.assertEqual(entry["check"], "passed", entry)

    def test_f2_follows_the_twin_of_a_carrier_recorded_in_another_system(self):
        """dev-F2-0004: the verified carrier is the FileMaker order entry; the Portal order
        (the oracle's answer) carries the PO only as a lead, so it is reached as the FM
        order's twin, with its cited number, status and shipment labelled lead_basis."""
        item = _item("dev-F2-0004-fixture")
        table, _ = self.table()
        result = self.investigate(table, "F2", self.root_of(item))
        expected = {v["name"]: v for v in item["known_outcome"]["values"]}
        numbers = self.answer(result, "order_number")
        self.assertEqual([_text(e["value"]) for e in numbers], [expected["order_number"]["value"]])
        self.assert_cites_expected_row(numbers[0], expected["order_number"])
        self.assertEqual([_text(e["value"]) for e in self.answer(result, "order_status")],
                         [expected["order_status"]["value"]])
        self.assertEqual([_text(e["value"]) for e in self.answer(result, "shipment_status")],
                         [expected["shipment_statuses"]["value"]])
        counted = {e["name"]: e for e in result["answer"] if e["kind"] == "count"}
        for name in ("order_count", "shipment_count"):
            self.assertEqual(counted[name]["value"], expected[name]["value"], name)
        self.assertEqual(counted["fm_order_entry_count"]["value"], 1)
        self.assertTrue(all(e.get("lead_basis") for e in numbers + [counted["order_count"]]))
        self.assertNotIn("lead_basis", counted["fm_order_entry_count"])
        weaker = next(g for g in result["gaps"] if g["reason"] == "weaker_candidates_not_followed")
        self.assertIn("(lead)", weaker["detail"])
        # Every hop read whole: the lead label is a gap and entries' lead_basis, not a reason
        # to call the answer partial.
        self.assertIn("lead_tier_links_followed", {g["reason"] for g in result["gaps"]})
        self.assertEqual(result["answerability"], {"state": "answerable", "reasons": []})
        # A hop answered short of its evidence makes the same answer partial; a gap state
        # outside the contract fails closed (never answerable).
        around = table["plx.around"]["handler"]
        for state in ("stale", "inaccessible", "not_ingested", "absent_in_source", "lost"):
            with self.subTest(state=state):
                def short(arguments, actor, state=state):
                    out = around(arguments, actor)
                    return dict(out, gaps=list(out.get("gaps") or ()) + [
                        {"state": state, "reason": "probe", "subject": "probe"}])
                table["plx.around"]["handler"] = short
                probed = table["plx.investigate"]["handler"](
                    {"family": "F2", "root": self.root_of(item), "as_known_at": self.at},
                    self.actor)
                if state == "lost":
                    self.assertEqual(probed["error"]["code"], "contract_violation")
                elif state == "absent_in_source":
                    self.assertEqual(probed["answerability"]["state"], "answerable")
                else:
                    self.assertEqual(probed["answerability"],
                                     {"state": "partial", "reasons": [f"gap_{state}"]})
        table["plx.around"]["handler"] = around

    def test_f1_chain_counts_each_system_once_and_labels_lead_links(self):
        for item_id in ("dev-F1-0002-fixture", "dev-F1-0009-fixture"):
            with self.subTest(item_id):
                item = _item(item_id)
                table, _ = self.table()
                result = self.investigate(table, "F1", self.root_of(item))
                self.assertEqual(result["root"]["object_type"], "SalesOrder")
                expected = {v["name"]: v["value"] for v in item["known_outcome"]["values"]}
                counted = {e["name"]: e for e in result["answer"] if e["kind"] == "count"}
                for name in ("customer_count", "sales_order_line_count", "shipment_count",
                             "sales_order_count"):
                    self.assertIn(name, counted)
                rows = oi.citation_rows(FixtureWorld.runtime)
                for name, entry in counted.items():
                    self.assertEqual(entry["value"], len(entry["basis"]))
                    if name in expected:
                        self.assertEqual(entry["value"], expected[name], name)
                    if "records" not in entry:
                        # No partial list: a count lists no records when a counted object
                        # shows this reader no cited value (the fixture's bare FM lines).
                        self.assertTrue(any(not table["plx.object"]["handler"](
                            {"object_id": oid, "as_known_at": self.at}, self.actor
                        ).get("values") for oid in entry["basis"]), name)
                        continue
                    # each counted object cites its own record, readable by this reader
                    self.assertEqual(len(entry["records"]), entry["value"], name)
                    for oid, record in zip(entry["basis"], entry["records"]):
                        self.assertEqual(_row(record["l1_handle"])[1], json.dumps(
                            json.loads(oid.split(":", 2)[2]), sort_keys=True))
                        self.assertIsNotNone(rows(record["l1_handle"], self.actor))
                self.assertEqual(counted["root_record_count"]["basis"],
                                 [result["root"]["object_id"]])
                # Leads are labelled as a gap per step and on each entry resting on one, never
                # as an answerability reason; a hop the capture does not hold (this world has
                # no BC) makes the answer partial.
                # Only the cross-source twin hop is a lead; the Portal's own foreign keys (its
                # customer, lines, shipments, products) are links, whatever the rows' lineage.
                self.assertEqual({g["subject"] for g in result["gaps"]
                                  if g["reason"] == "lead_tier_links_followed"},
                                 {"so_twin", "so_twin_of"})
                self.assertEqual(result["answerability"],
                                 {"state": "partial", "reasons": ["gap_not_ingested"]})
                self.assertTrue(any(g["state"] == "not_ingested" for g in result["gaps"]))
                self.assertNotIn("lead_basis", counted["customer_count"])
                self.assertNotIn("lead_basis", counted["sales_order_count"])
                # Steps that do not fit the root's type were skipped without a call.
                skipped = [s for s in result["trace"] if s["status"] == "skipped"]
                self.assertTrue(skipped)
                self.assertTrue(all(s["calls"] == 0 for s in skipped))
                self.assertEqual(result["steps"]["used"], sum(s["calls"] for s in result["trace"]))
        # dev-F1-0009 reaches the Portal order's FileMaker twin: one sales order and one FM
        # order entry, as the oracle counts them, never two sales orders.
        self.assertEqual(expected["fm_order_entry_count"], 1)
        self.assertEqual(counted["fm_order_entry_count"]["value"], 1)
        self.assertTrue(all(":portal.app_sales_order:" in oid
                            for oid in counted["sales_order_count"]["basis"]))
        # ... and one more hop from the twin's lines: the FM allocations and the FM assembly
        # orders they allocate, each counted per binding with its record cited.
        for name in ("fm_order_entry_item_assembly_count", "fm_assembly_order_count"):
            self.assertEqual(counted[name]["value"], expected[name], name)
            self.assertEqual(len(counted[name]["records"]), expected[name], name)

    def test_f4_verdicts_are_plx_process_verdicts(self):
        """dev-F4-0001: plx.process decides no three-way check (lead links, evidence not
        fully visible); the plan publishes exactly that, never a verdict of its own, while
        the ordered and received quantities stay cited context."""
        item = _item("dev-F4-0001-fixture")
        po = "PurchaseOrder:portal.app_purchase_order:" + json.dumps(
            item["root"]["key"], separators=(",", ":"))
        table, _ = self.table(process=True)
        result = self.investigate(table, "F4", po)
        self.assertEqual(next(s for s in result["trace"] if s["step"] == "match")["status"], "ok")
        expected = {v["name"]: v for v in item["known_outcome"]["values"]}
        for role in ("ordered", "received"):
            entries = self.answer(result, role)
            self.assertEqual(len(entries), 2, role)
            for entry in entries:
                twin = [e for n, e in expected.items() if n.startswith(role + "_") and
                        {_row(c["l1_handle"]) for c in e["citations"]}
                        == {_row(entry["citation"]["l1_handle"])}]
                self.assertEqual(len(twin), 1, entry)
                self.assertEqual(float(entry["value"]), float(twin[0]["value"]))
                self.assertEqual(entry["unit"], twin[0]["unit"])
                self.assertIn(entry["check"], ("passed", "unverified"))
        process = table["plx.process"]["handler"](
            {"from": po, "process": "purchase_to_invoice", "as_known_at": self.at}, self.actor)
        verdicts = {e["name"]: e["value"] for e in result["answer"] if e["kind"] == "select"}
        for name, roles in (("ordered_vs_received", ["ordered", "received"]),
                            ("received_vs_invoiced", ["received", "invoiced"])):
            agrees = [c["agree"] for c in process["quantity_checks"] if c["roles"] == roles]
            decided = (False if False in agrees else None if None in agrees or not agrees
                       else True)
            self.assertEqual(verdicts[name], decided, name)
        self.assertEqual({c["agree"] for c in process["quantity_checks"]} - {None}, set())
        self.assertEqual(set(verdicts.values()), {None})
        self.assertIn("missing_ordered_vs_received", result["answerability"]["reasons"])
        # plx.process's own reasons reach the answer's gaps.
        reasons = {g["reason"] for g in result["gaps"]}
        self.assertLessEqual({g["reason"] for g in process["gaps"]}, reasons)
        # plx.process is not on this host: every verdict is step_unavailable, never a crash.
        table, _ = self.table()
        result = self.investigate(table, "three_way_match", po)
        self.assertEqual(next(s for s in result["trace"] if s["step"] == "match")["status"],
                         "step_unavailable")
        gaps = {(g["state"], g["reason"], g["subject"]) for g in result["gaps"]}
        for name in ("ordered_vs_received", "received_vs_invoiced", "three_way_match"):
            self.assertIn(("unknown", "step_unavailable", name), gaps)
        self.assertIn(("unknown", "step_unavailable", "plx.process (match)"), gaps)
        self.assertEqual({e["value"] for e in result["answer"] if e["kind"] == "select"}, {None})
        self.assertIn("required_step_incomplete", result["answerability"]["reasons"])
        # No BC invoice twin in this world: invoiced is an explicit gap, not a number.
        self.assertIn("missing_invoiced", result["answerability"]["reasons"])

    def test_f4_po_number_the_projection_does_not_carry_blocks_root_not_found(self):
        """The identifier grammar declares PO numbers (petra_po). The fixture world's row
        renumbered to a BC No. outside the PO series (FXBC9001) is not of that grammar, so
        the projection carries no po_number for it: the root blocks with an explicit gap
        after the plan's fallback filter."""
        table, _ = self.table()
        po = 'PurchaseOrder:portal.app_purchase_order:{"id":"fx-po-bc-1"}'
        read = table["plx.object"]["handler"]({"object_id": po, "as_known_at": self.at},
                                              self.actor)
        self.assertEqual(read["object"]["object_id"], po)
        self.assertNotIn("po_number", {v["property"] for v in read["values"]})
        root = {"identifier": "FXBC9001", "identifier_kind": "po_number"}
        result = self.investigate(table, "F4", root)
        self.assertEqual(result["answerability"], {"state": "blocked",
                                                   "reasons": ["root_not_found"]})
        self.assertEqual(result["answer"], [])
        self.assertIn(("unknown", "root_not_found", "root:FXBC9001"),
                      {(g["state"], g["reason"], g["subject"]) for g in result["gaps"]})
        # the plan's fallback filter was tried too
        self.assertEqual(next(s for s in result["trace"] if s["step"] == "root")["calls"], 2)

    def test_f4_portal_form_po_root_resolves_through_plx_find(self):
        """A dev F4 root in the Portal's PO form (formatPoNumber PO{n}) is one exact
        plx.find hit on its own row, and the plan resolves it instead of blocking."""
        item = _item("dev-F4-0001-fixture")
        table, _ = self.table()
        po = "PurchaseOrder:portal.app_purchase_order:" + json.dumps(
            item["root"]["key"], separators=(",", ":"))
        found = table["plx.find"]["handler"](
            {"query": item["root"]["identifier"], "as_known_at": self.at}, self.actor)
        exact = [c for c in found["candidates"] if c["reason"] == "exact_identifier"]
        self.assertEqual([c["object"]["object_id"] for c in exact], [po])
        self.assertEqual(exact[0]["matched"]["identifier_kind"], "petra_po")
        result = self.investigate(table, "F4", self.root_of(item))
        self.assertNotIn("root_not_found", result["answerability"]["reasons"])
        self.assertEqual(next(s for s in result["trace"] if s["step"] == "root")["status"], "ok")

    def test_ambiguous_single_root_blocks_with_candidates(self):
        table, _ = self.table()
        result = self.investigate(table, "chain", {"identifier": "AO-98038A"})
        self.assertEqual(result["answerability"],
                         {"state": "blocked", "reasons": ["ambiguous_root"]})
        self.assertGreaterEqual(len(result["candidates"]), 2)
        self.assertEqual(result["answer"], [])
        self.assertEqual(result["chain"]["objects"], [])


# =========================================================================== safety

class ProvenZeroTests(Base):
    """A count is 0 only on a proven-complete search (``zero: complete_search``): the
    root's identifier search answered complete, the counted relation carries that
    identifier, and the host proves the relation wholly captured for this reader now
    (``oi.relation_complete``). Otherwise "no record reached" stays a gap (never 0).
    The customer PO of dev-F2-0004 has no Sage order header. Evidence label:
    isolated-fixture."""
    BINDING = "portal.sage_oe_order_header"

    def count(self, result):
        return next(e for e in result["answer"] if e["name"] == "sage_order_count")

    def assert_gap_not_zero(self, result):
        """No proof: the count is not stated (the item is optional), never 0."""
        entries = [e for e in result["answer"] if e["name"] == "sage_order_count"]
        self.assertFalse([e for e in entries if e["value"] == 0 or "proof" in e], entries)

    def run_f2(self, **table):
        tbl, _ = self.table(**table)
        return self.investigate(tbl, "F2", self.root_of(_item("dev-F2-0004-fixture")))

    def test_a_complete_search_proves_a_zero(self):
        entry = self.count(self.run_f2(complete=True))
        self.assertEqual(entry["value"], 0, entry)
        self.assertEqual(entry["basis"], [])
        proof = entry["proof"]
        self.assertEqual(proof["kind"], "complete_search")
        self.assertEqual(proof["identifier_kinds"], ["customer_po"])
        self.assertEqual([r["binding"] for r in proof["relations"]], [self.BINDING])
        self.assertLessEqual(proof["relations"][0]["complete_at"], self.at)

    def test_no_proof_leaves_the_count_a_gap(self):
        # No host proof at all: the zero is not a count.
        self.assert_gap_not_zero(self.run_f2())
        # A reader without the relation's grant: its records may be hidden from the search.
        reader = ot.StoreReader.open(FixtureWorld.product.store)
        keys = reader.audience_keys()
        reader.conn.close()
        hidden = [k for k in keys if "oe_order_header" in k]
        self.assertTrue(hidden, keys)
        self.assert_gap_not_zero(self.run_f2(complete=True, grants={
            k: {self.PRINCIPAL} for k in keys if k not in hidden}))

    def test_the_host_proves_a_relation_only_now_for_a_reader_who_holds_it(self):
        self.table()
        hook = self.hook
        self.assertIsNotNone(hook(self.BINDING, self.actor, self.at))
        stranger = Actor(self.TENANT, frozenset({"someone:else"}))
        for binding, actor, at in ((self.BINDING, stranger, self.at),
                                   (self.BINDING, self.actor, "2020-01-01T00:00:00Z"),
                                   (self.BINDING, self.actor, "not a time"),
                                   ("bc.sales_orders", self.actor, self.at),
                                   ("portal.made_up", self.actor, self.at)):
            with self.subTest(binding=binding, actor=sorted(actor.principals), at=at):
                self.assertIsNone(hook(binding, actor, at))


class DegradationAndPermissionTests(Base):
    def test_missing_primitives_degrade_to_step_unavailable(self):
        item = _item("dev-F3-0001-fixture")
        table, _ = self.table(drop=("plx.around", "plx.timeline"))
        result = self.investigate(table, "F3", self.root_of(item))
        statuses = {s["step"]: s["status"] for s in result["trace"]}
        self.assertEqual(statuses["lines"], "step_unavailable")
        self.assertIn("step_unavailable", {g["reason"] for g in result["gaps"]})
        self.assertIn("required_step_incomplete", result["answerability"]["reasons"])
        self.assertIn(result["answerability"]["state"], ("partial", "blocked"))
        self.assertTrue(all(e["value"] is None for e in result["answer"]))
        # without plx.find the root itself is unavailable, still an answer with gaps
        table, _ = self.table(drop=("plx.find",))
        result = self.investigate(table, "F3", self.root_of(item))
        self.assertEqual(result["answerability"]["state"], "blocked")
        self.assertIn("step_unavailable", result["answerability"]["reasons"])

    def test_permission_trimming_is_the_primitives(self):
        item = _item("dev-F3-0001-fixture")
        reader = ot.StoreReader.open(FixtureWorld.product.store)
        keys = reader.audience_keys()
        reader.conn.close()
        hidden = "api:public.app_sales_order_line"
        self.assertIn(hidden, keys)
        grants = {k: {self.PRINCIPAL} for k in keys if k != hidden}
        table, _ = self.table(grants=grants)
        result = self.investigate(table, "F3", self.root_of(item))
        # Nothing of the withheld relation reaches the answer: no object, edge end,
        # citation or value (a primitive's own gap may name the relation it searched).
        text = json.dumps({k: v for k, v in result.items() if k != "gaps"})
        self.assertNotIn("app_sales_order_line", text)
        self.assertNotIn("SalesOrderLine:", text)
        self.assertNotIn(item["known_outcome"]["values"][0]["value"] + ".0", text)
        self.assertIn("missing_ordered", result["answerability"]["reasons"])
        self.assertIn("withheld", result)
        # A count lists records only of what this reader was shown (F1 over the same grants).
        chain = self.investigate(table, "F1", self.root_of(item))
        self.assertNotIn("app_sales_order_line", json.dumps(
            {k: v for k, v in chain.items() if k != "gaps"}))
        self.assertTrue(any(e.get("records") for e in chain["answer"]))
        # A reader granted nothing sees no root at all.
        table, _ = self.table(grants={})
        result = self.investigate(table, "F3", self.root_of(item))
        self.assertEqual(result["answerability"]["state"], "blocked")
        self.assertEqual(result["chain"]["objects"], [])
        self.assertEqual(result["values"], [])

    def test_claims_go_through_the_claim_gate(self):
        item = _item("dev-F3-0001-fixture")
        # A row reader that disagrees with the projection: the gate fails the claim.
        table, tools = self.table()
        real = tools.rows

        def tampered(handle, actor):
            view = real(handle, actor)
            if view and "row" in view and "quantity" in view["row"]:
                view = dict(view, row=dict(view["row"], quantity="1.0"))
            return view
        tools.rows = tampered
        result = self.investigate(table, "F3", self.root_of(item))
        ordered = self.answer(result, "ordered")[0]
        self.assertEqual(ordered["check"], "failed")
        self.assertEqual(ordered["code"], "claim_value_mismatch")
        self.assertIn("claim_check_failed", result["answerability"]["reasons"])
        self.assertEqual(result["claim_checks"]["failures"],
                         [{"name": "ordered", "code": "claim_value_mismatch"}])
        # No row reader: nothing is called checked and the answer is at most partial.
        table, _ = self.table(rows=False)
        result = self.investigate(table, "F3", self.root_of(item))
        self.assertEqual(self.answer(result, "ordered")[0]["check"], "not_checked")
        self.assertFalse(result["claim_checks"]["checked"])
        self.assertIn("claims_not_checked", result["answerability"]["reasons"])

    def test_step_budget_stops_the_run_and_says_so(self):
        item = _item("dev-F3-0001-fixture")
        table, _ = self.table()
        result = self.investigate(table, "F3", self.root_of(item), step_budget=2)
        self.assertEqual(result["steps"], {"used": 2, "budget": 2})
        self.assertIn("step_budget_exhausted", result["answerability"]["reasons"])
        self.assertTrue(result["completeness"]["truncated"])
        self.assertIn("budget_exhausted", {s["status"] for s in result["trace"]})

    def test_an_answer_over_the_cap_sheds_edge_citations_before_edges(self):
        root = self.root_of(_item("dev-F3-0001-fixture"))
        table, tools = self.table()
        whole = self.investigate(table, "F3", root)
        cited = [e["link_id"] for e in whole["chain"]["edges"]
                 if any("citations" in f for f in e["reason"] + e["why_not"])]
        self.assertTrue(cited)
        tools.limits = dict(tools.limits, max_result_bytes=ot._envelope_bytes(whole) - 1)
        cut = self.investigate(table, "F3", root)
        self.assertEqual([e["link_id"] for e in cut["chain"]["edges"]],
                         [e["link_id"] for e in whole["chain"]["edges"]])
        self.assertLessEqual(ot._envelope_bytes(cut), tools.limits["max_result_bytes"])
        self.assertLess(sum(any("citations" in f for f in e["reason"] + e["why_not"])
                            for e in cut["chain"]["edges"]), len(cited))
        self.assertTrue(cut["completeness"]["truncated"])
        self.assertFalse(cut["completeness"]["complete"])


class NoPlanTests(Base):
    def test_unknown_family_is_no_plan_with_suggested_calls(self):
        table, _ = self.table()
        result = self.investigate(table, "F9", {"identifier": "FXSO100097"})
        self.assertEqual(result["error"]["code"], "no_plan")
        self.assertEqual([s["tool"] for s in result["suggested_calls"]], ["plx.find"])
        for call in result["suggested_calls"]:
            self.assertEqual(validate(call["arguments"], CONTRACTS["tools"][call["tool"]]
                                      ["input"], root=CONTRACTS), [])
        self.assertNotIn("answer", result)

    def test_root_type_no_plan_covers_is_no_plan_with_schema_derived_calls(self):
        item = _item("dev-F1-0002-fixture")
        table, _ = self.table()
        resolved = self.investigate(table, "F1", self.root_of(item))
        customer = next(o["object_id"] for o in resolved["chain"]["objects"]
                        if o["object_type"] == "Customer")
        result = self.investigate(table, "F3", customer)
        self.assertEqual(result["error"]["code"], "no_plan")
        tools = [s["tool"] for s in result["suggested_calls"]]
        self.assertEqual(tools[:2], ["plx.object", "plx.timeline"])
        self.assertIn("plx.around", tools)
        self.assertLessEqual(len(tools), 8)
        schema = FixtureWorld.schema
        for call in result["suggested_calls"]:
            self.assertEqual(validate(call["arguments"], CONTRACTS["tools"][call["tool"]]
                                      ["input"], root=CONTRACTS), [])
            for hop in call["arguments"].get("path", ()):
                link = schema.link_types[hop["link_type"]]
                start = link["from"] if hop["direction"] == "forward" else link["to"]
                self.assertEqual(start, "Customer")
        # Only tools the host serves are suggested.
        table, _ = self.table(drop=("plx.timeline",))
        result = self.investigate(table, "F3", customer)
        self.assertNotIn("plx.timeline", [s["tool"] for s in result["suggested_calls"]])


# =========================================================================== host

class CaseHostToolsTests(Base):
    """``operating.ontology_host.CaseHostTools.call``: plx.investigate on the case host's
    generic-tool table, under the policy the host decides per call from the readers'
    audiences, with claims checked against the runtime's per-reader citation reads."""

    def host(self, actor: Actor):
        from operating.ontology_host import CaseHostTools
        host = CaseHostTools(FixtureWorld.runtime, actor, store_path=FixtureWorld.product.store,
                             schema=FixtureWorld.schema)
        self.addCleanup(host.close)
        return host

    def test_case_host_serves_plx_investigate_over_its_own_primitives(self):
        item = _item("dev-F3-0001-fixture")
        host = self.host(self.actor)
        self.assertEqual(set(host.table), {"plx.schema", "plx.find", "plx.object",
                                           "plx.around", "plx.timeline", "plx.process",
                                           "plx.investigate"})
        from operating.ontology_host import table_tool_names
        self.assertEqual(set(host.table), table_tool_names())
        result = host.call("plx.investigate", {"family": "F3", "root": self.root_of(item),
                                               "as_known_at": self.at}, self.actor)
        _ok(self, result)
        ordered = self.answer(result, "ordered")
        self.assertEqual(len(ordered), 1)
        self.assertIn(ordered[0]["check"], ("passed", "unverified"))
        self.assertTrue(result["claim_checks"]["checked"])
        # plx.process is on the host's table: the three_way_match verdicts are its own,
        # never step_unavailable.
        po = "PurchaseOrder:portal.app_purchase_order:" + json.dumps(
            _item("dev-F4-0001-fixture")["root"]["key"], separators=(",", ":"))
        table_result = host.call("plx.investigate", {
            "family": "three_way_match", "root": po, "as_known_at": self.at}, self.actor)
        _ok(self, table_result)
        self.assert_process_decides(table_result, host.call("plx.process", {
            "from": po, "process": "purchase_to_invoice", "as_known_at": self.at},
            self.actor))
        with self.assertRaises(KeyError):
            host.call("plx.case_get", {}, self.actor)

    def test_case_host_policy_applies_to_every_plan_step(self):
        item = _item("dev-F3-0001-fixture")
        stranger = Actor(self.TENANT, frozenset({"someone:else"}))
        host = self.host(stranger)
        result = host.call("plx.investigate", {"family": "F3", "root": self.root_of(item),
                                               "as_known_at": self.at}, stranger)
        _ok(self, result)
        self.assertEqual(result["answerability"]["state"], "blocked")
        self.assertEqual(result["values"], [])
        self.assertEqual(result["chain"]["objects"], [])

    def test_a_broken_plan_file_is_an_error_answer_not_a_host_failure(self):
        from unittest import mock
        host = self.host(self.actor)
        with mock.patch.object(oi, "load_plans", side_effect=oi.PlanError("bad plan")):
            host.close()
            result = host.call("plx.investigate", {"family": "F3",
                                                   "root": {"identifier": "x"}}, self.actor)
            self.assertEqual(result["error"]["code"], "plans_invalid")
            found = host.call("plx.find", {"query": "x"}, self.actor)
            self.assertNotIn("error", found)


class McpHostTests(Base):
    """plx.investigate listed on the stdio case host (decision 2026-10-04): an MCP
    client's call passes ``MCPServer.handle`` (the employee gate, the per-call policy of
    ``CaseHostTools``, the output boundary) and its plans run over the host's own table,
    plx.process included."""

    def test_an_mcp_client_gets_plx_process_verdicts_for_three_way_match(self):
        from operating.mcp_server import TOOLS, MCPServer
        from operating.ontology_host import CaseHostTools
        from operating.tests.firewall_view import client_result
        spec = next(tool for tool in TOOLS if tool["name"] == "plx.investigate")
        self.assertTrue(spec["annotations"]["readOnlyHint"])
        listed = dict(spec["inputSchema"]["properties"])
        # The family argument names every plan the host runs (a client never guesses a
        # family into no_plan); the schema is otherwise the contract's.
        menu = listed["family"].pop("description")
        for plan in sorted((ROOT / "ontology" / "plans").glob("[!_]*.json")):
            self.assertIn(json.loads(plan.read_text("utf-8"))["id"] + ":", menu)
        self.assertEqual(listed, CONTRACTS["tools"]["plx.investigate"]["input"]["properties"])
        host = CaseHostTools(FixtureWorld.runtime, self.actor,
                             store_path=FixtureWorld.product.store, schema=FixtureWorld.schema)
        server = MCPServer(case_runtime=FixtureWorld.runtime, actor=self.actor, ontology=host)
        self.addCleanup(server.close)
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                  "clientInfo": {"name": "investigate-test", "version": "1"}}})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})

        def call(request_id: int, name: str, arguments: dict) -> dict:
            reply = server.handle({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                                   "params": {"name": name, "arguments": arguments}})
            result = client_result(name, reply["result"])
            self.assertFalse(result["isError"], result)
            return result["structuredContent"]

        po = "PurchaseOrder:portal.app_purchase_order:" + json.dumps(
            _item("dev-F4-0001-fixture")["root"]["key"], separators=(",", ":"))
        result = call(2, "plx.investigate", {"family": "three_way_match", "root": po,
                                             "as_known_at": self.at})
        _ok(self, result)
        process = call(3, "plx.process", {"from": po, "process": "purchase_to_invoice",
                                          "as_known_at": self.at})
        self.assert_process_decides(result, process)
        # The ordered quantities stay cited context, as the in-process run gives them.
        self.assertEqual(len(self.answer(result, "ordered")), 2)


# =========================================================================== evals

class BcHostClaimTests(unittest.TestCase):
    """Fresh BC roots on a host-built projection (``operating.host run`` over a BC-only
    config: the real GET-only ``BcCapture`` into the host's capture store, the projection
    built from it), investigated through ``CaseHostTools`` as the MCP host serves it. The
    values cite BC capture handles; the claim gate re-reads exactly those revisions from
    the host's capture store for the reader. Evidence label: injected-transport (the BC
    fake of ``integrations/tests/test_bc_capture.py``, synthetic records)."""

    @classmethod
    def setUpClass(cls) -> None:
        import io
        import shutil
        from contextlib import redirect_stderr, redirect_stdout
        from unittest import mock
        from integrations.live_source_guard import LiveSourceAudience
        from integrations.tests import test_bc_capture as tb
        from integrations.tests.test_live_source_adapters import ENV
        from ontology.tests.test_project_bc import DIST, MAN, gid, purchase_world, record
        from operating import host as hm
        from operating.case_runtime import CaseRuntime, LiveReaders

        def refuse(*args, **kwargs):
            raise AssertionError("network access attempted in an offline test")
        for target in ("socket.socket.connect", "socket.create_connection"):
            patcher = mock.patch(target, refuse)
            patcher.start()
            cls.addClassCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory(prefix="plx-investigate-bc-host-")
        cls.addClassCleanup(tmp.cleanup)
        root = Path(tmp.name)
        fake = purchase_world()
        fake.data[(MAN, "salesOrders")] = [record(
            gid(1, 21), number="SO-1", externalDocumentNumber="CPO-77", customerNumber="C-1",
            status="Open", orderDate="2026-09-01")]
        fake.data[(MAN, "salesShipments")] = [record(
            gid(1, 22), number="SS-1", orderNumber="SO-1", customerNumber="C-1",
            shipmentDate="2026-09-25", postingDate="2026-09-25")]
        files = {"audience.json": {"schema": "plx-live-source-audience/v1", "tenant": "plx",
                                   "default_classification": "restricted",
                                   "audiences": {"bc:production:*": ["svc:host",
                                                                     "staff:manager"],
                                                 # A reader granted another company only.
                                                 f"bc:production:{DIST}:*": [
                                                     "svc:host", "staff:manager",
                                                     "staff:distribution"]}},
                 # The manager holds the Portal too (a reader the Portal is withheld
                 # from never decides a BC side past its uncaptured twin hops).
                 "mirror.json": {"schema": "plx-source-mirror-audience/v1", "tenant": "plx",
                                 "audiences": {family: ["staff:manager"] for family in
                                               ("public.*", "sage.*", "silver.*")}},
                 "books.json": {"schema": "plx-bc-company-books/v1", "environments": tb.BOOKS},
                 "host.json": {"schema": hm.CONFIG_SCHEMA, "tenant": "plx",
                               "capture_principal": "svc:host",
                               "capture_audience": "audience.json",
                               "live_audience": "audience.json",
                               "mirror_audience": "mirror.json", "bc": {"books": "books.json"}}}
        for name, doc in files.items():
            (root / name).write_text(json.dumps(doc), "utf-8")
        state = root / "state"

        def cycle() -> None:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = hm.main(["run", "--state-dir", str(state), "--config",
                                str(root / "host.json")], environ=ENV, sender=fake,
                               sleep=lambda s: None)
            assert code == 0, err.getvalue()
        cycle()
        cls.between = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        # The first generation, kept as a projection that cites what BC then held.
        cls.before = root / "projection-before.sqlite"
        shutil.copyfile(hm.projection_path(state), cls.before)
        # The item record changes in BC; the next cycle captures its new revision.
        fake.data[(MAN, "items")][0].update({"@odata.etag": 'W/"changed"',
                                             "displayName": "Widget v2",
                                             "lastModifiedDateTime": "2099-01-01T00:00:00Z"})
        cycle()
        cls.store = hm.projection_path(state)
        cls.capture = state / hm.BC_DIR / hm.BC_FILE
        cls.live = LiveReaders(audience=LiveSourceAudience.from_file(root / "audience.json"))
        cls.root_dir = root
        cls.runtime = CaseRuntime(state, client=None, audience=root / "mirror.json",
                                  tenant="plx", live=cls.live)
        cls.addClassCleanup(cls.runtime.close)
        cls.manager = Actor("plx", frozenset({"staff:manager"}), "restricted")
        cls.outsider = Actor("plx", frozenset({"staff:outsider"}), "restricted")
        cls.other_company = Actor("plx", frozenset({"staff:distribution"}), "restricted")
        cls.po = 'PurchaseOrder:bc.purchase_orders:{"id":"%s"}' % gid(1, 1)

    def ask(self, actor: Actor, family: str, root, store=None, **extra) -> dict:
        from operating.ontology_host import CaseHostTools
        host = CaseHostTools(self.runtime, actor, store_path=store or self.store)
        self.addCleanup(host.close)
        result = host.call("plx.investigate", {"family": family, "root": root, **extra},
                           actor)
        _ok(self, result)
        return result

    def test_cited_bc_values_resolve_for_the_manager_and_never_for_an_outsider(self):
        from integrations.capture_store import is_capture_handle
        rows = oi.citation_rows(self.runtime)
        for family, root in (("three_way_match", self.po),
                             ("customer_po_status", {"identifier": "CPO-77"})):
            with self.subTest(family=family):
                result = self.ask(self.manager, family, root)
                cited = [e for e in result["answer"] if e["kind"] == "property"
                         and e["value"] is not None]
                self.assertTrue(cited)
                self.assertEqual(result["claim_checks"],
                                 {"checked": True, "passed": True, "claims": len(cited),
                                  "failures": []})
                self.assertNotIn("claim_check_failed", result["answerability"]["reasons"])
                handles = {e["citation"]["l1_handle"] for e in cited}
                self.assertTrue(all(h.startswith("bc://") and is_capture_handle(h)
                                    for h in handles), handles)
                # Every value is checked against its re-read row: passed, or unverified
                # only for a quantity role no BC role contract grounds yet.
                for entry in cited:
                    self.assertIn((entry["check"], entry.get("code")),
                                  (("passed", None), ("unverified", "claim_role_unverified")))
                self.assertTrue(any(e["check"] == "passed" for e in cited))
                # Every value the plan needs is cited, and each opens through plx.read.
                # The one verdict left is the price check: unit costs carry the finance
                # marking, which no case-host reader holds (and BC line prices declare no
                # tax basis), so it is undecided and listed, never read as agreement. The
                # Portal twin is not captured on this BC-only host: a not_ingested gap, so
                # the answer is partial, never answerable on one system alone.
                self.assertEqual(result["answerability"]["reasons"], {
                    "three_way_match": ["gap_not_ingested", "missing_three_way_match"],
                    "customer_po_status": ["gap_not_ingested"]}[family])
                self.assertEqual(result["answerability"]["state"], "partial")
                # Correctly partial: decided (order-quantities-answers) that a system the
                # host never captured stays not_ingested, never unknown. Each such gap is on
                # a hop the plan follows into that system (the Portal twin holds the
                # order's own status and shipments), never on a call that does not read it.
                short = {(g["state"], g["subject"]) for g in result["gaps"]
                         if g["state"] not in ("unknown", "absent_in_source")}
                self.assertEqual(short, {
                    "three_way_match": {("not_ingested", side)
                                        for side in ("ordered", "received", "invoiced")},
                    "customer_po_status": {("not_ingested",
                                            "SalesOrder.same_record_as reverse")}}[family])
                self.assertTrue(all("Portal" in g.get("detail", "") or "portal." in
                                    g.get("detail", "") for g in result["gaps"]
                                    if g["state"] == "not_ingested"))
                names = {e["name"]: e for e in result["answer"]}
                for name in {"three_way_match": ("item_1", "ordered", "received", "invoiced"),
                             "customer_po_status": ("order_number", "order_status",
                                                    "shipment_status")}[family]:
                    self.assertIn(names[name], cited, name)
                if family == "three_way_match":
                    self.assertEqual([names[n]["value"] for n in (
                        "ordered_vs_received", "received_vs_invoiced", "three_way_match")],
                        [True, True, None])
                    self.assertIn("marking_withheld", {g["reason"] for g in result["gaps"]})
                    # The item's own verdict, with exactly the three cells plx.process
                    # compared: the cited ordered, received and invoiced quantities.
                    self.assertIs(names["match_1"]["value"], True)
                    self.assertEqual([c["citation"] for c in names["match_1"]["cells"]],
                                     [names[n]["citation"] for n in
                                      ("ordered", "received", "invoiced")])
                else:
                    self.assertEqual((_text(names["order_status"]["value"]),
                                      names["order_status"]["property"],
                                      names["shipment_status"]["value"],
                                      names["shipment_status"]["property"]),
                                     ("Open", "bc_status", "2026-09-25", "ship_date"))
                for entry in cited:
                    self.assertEqual(self.plx_read(self.manager, entry["citation"]["l1_handle"])
                                     ["status"], "resolved")
                # A reader without the BC grant: refused at the root, no value, no claim,
                # and the claim gate's row reader refuses each row the manager read.
                refused = self.ask(self.outsider, family, root)
                self.assertEqual(refused["answerability"]["state"], "blocked")
                self.assertEqual((refused["values"], refused["chain"]["objects"],
                                  refused["claim_checks"]["failures"]), ([], [], []))
                for handle in handles:
                    self.assertIsNotNone(rows(handle, self.manager))
                    self.assertIsNone(rows(handle, self.outsider))
        # Fails closed: a handle of another capture store generation, or a capture store
        # that is not there (never created by the read).
        self.assertIsNone(rows(handle.rsplit("@", 1)[0] + "@" + "0" * 16 + "-1", self.manager))
        from operating.case_runtime import CaseRuntime
        bare = CaseRuntime(self.root_dir / "no-capture", client=None,
                           audience=self.root_dir / "mirror.json", tenant="plx", live=self.live)
        self.addCleanup(bare.close)
        self.assertIsNone(oi.citation_rows(bare)(handle, self.manager))
        self.assertFalse((self.root_dir / "no-capture" / "bc-capture").exists())

    def passed_values(self) -> list:
        passed = [e for family, root in (("three_way_match", self.po),
                                         ("customer_po_status", {"identifier": "CPO-77"}))
                  for e in self.ask(self.manager, family, root)["answer"]
                  if e["kind"] == "property" and e["check"] == "passed"]
        self.assertTrue(passed)
        return passed

    def plx_read(self, actor: Actor, handle: str, runtime=None, host=None) -> dict:
        from operating.ontology_host import CaseHostTools
        if host is None:
            host = CaseHostTools(runtime or self.runtime, actor, store_path=self.store)
            self.addCleanup(host.close)
        return host.call("plx.read", {"citation": handle}, actor)

    def employee_runtime(self, department_ids):
        from operating.case_runtime import CaseRuntime
        from operating.tests import test_employee_permissions as te
        policy = {**te.POLICY, "principals": {"staff:manager":
                                              te.POLICY["principals"]["staff:operations"]}}
        runtime = CaseRuntime(self.capture.parents[1], client=None,
                              audience=self.root_dir / "mirror.json", tenant="plx",
                              live=self.live, require_employee=True,
                              employee_permissions=te.resolver(
                                  te.portal_user(role="SUPER_ADMIN",
                                                 department_ids=frozenset(department_ids)),
                                  policy=policy))
        self.addCleanup(runtime.close)
        return runtime, Actor("plx", frozenset({"employee:" + te.OID}))

    def test_plx_read_and_a_case_open_exactly_the_bc_values_the_gate_passed(self):
        from operating.cases import _digest
        from operating.tests.test_employee_permissions import DEPT_OPS
        passed = self.passed_values()
        employee_runtime, employee = self.employee_runtime({DEPT_OPS})
        opener = Actor("plx", frozenset({"staff:manager", "staff:distribution"}), "restricted")
        case_id = self.runtime.cases.open(opener, "three-way match",
                                          audience=["staff:manager", "staff:distribution"])
        for runtime, reader in ((self.runtime, self.manager), (employee_runtime, employee)):
            rows = oi.citation_rows(runtime)
            for entry in passed:
                handle, path = entry["citation"]["l1_handle"], entry["citation"]["field_path"]
                with self.subTest(reader=sorted(reader.principals), handle=handle):
                    out = self.plx_read(reader, handle, runtime)
                    self.assertEqual(validate(out, CONTRACTS["tools"]["plx.read"]["output"],
                                              root=CONTRACTS), [])
                    self.assertEqual(out["status"], "resolved", out)
                    self.assertEqual(_text(out["fields"][path]), _text(entry["value"]))
                    # The digest a case records, of the same view the gate checked.
                    self.assertEqual(out["digest"], _digest(rows(handle, reader)))
                    self.assertEqual(out["digest"],
                                     _digest(runtime.resolve_citation(handle, reader)))
                    self.assertTrue(runtime.read_live(handle, reader)["captured"])
        for entry in passed:
            self.runtime.cases.finding(opener, case_id, f"{entry['name']} is "
                                       f"{entry['value']}", "fact", "supported",
                                       citations=[entry["citation"]["l1_handle"]])
        digests = {e["citation"]["l1_handle"]: self.plx_read(self.manager,
                                                             e["citation"]["l1_handle"])
                   ["digest"] for e in passed}
        findings = self.runtime.cases.get(self.manager, case_id)["findings"]
        self.assertEqual({c["handle"]: (c["status"], c["digest"]) for f in
                          findings["supported"] for c in f["citations"]},
                         {h: ("resolved", d) for h, d in digests.items()})
        # A case reader granted another BC company only: every finding withheld, no
        # statement, handle or digest.
        hidden = self.runtime.cases.get(self.other_company, case_id)["findings"]
        self.assertEqual(hidden["supported"], [])
        self.assertEqual(len(hidden["withheld"]), len(passed))
        self.assertTrue(all(f["statement"] is None and "handle" not in json.dumps(
            f["citations"]) for f in hidden["withheld"]))

    def test_an_outsider_is_refused_the_bc_values_on_every_path(self):
        from ontology.core import ValidationError
        from operating.cases import CitationUnresolvable
        from operating.employee_permissions import EmployeeAccessRefused
        passed = self.passed_values()
        refused_runtime, refused_employee = self.employee_runtime(set())
        readers = ((self.runtime, self.outsider), (self.runtime, self.other_company),
                   (refused_runtime, refused_employee))
        for runtime, reader in readers:
            rows = oi.citation_rows(runtime)
            for entry in passed:
                handle = entry["citation"]["l1_handle"]
                with self.subTest(reader=sorted(reader.principals), handle=handle):
                    out = self.plx_read(reader, handle, runtime)
                    self.assertIn(out["status"], ("withheld", "unresolvable"))
                    self.assertNotIn("fields", out)
                    self.assertNotIn("digest", out)
                    self.assertIsNone(rows(handle, reader))
                    self.assertFalse(runtime.read_live(handle, reader)["ok"])
                    with self.assertRaises((CitationUnresolvable, EmployeeAccessRefused)):
                        runtime.resolve_citation(handle, reader)
                    if reader is self.other_company:
                        case_id = runtime.cases.open(reader, "three-way match")
                        with self.assertRaises((ValidationError, CitationUnresolvable)):
                            runtime.cases.finding(reader, case_id, "a value", "fact",
                                                  "supported", citations=[handle])

    def test_reads_write_nothing_and_never_wait_on_a_held_capture_write_lock(self):
        import hashlib
        import sqlite3
        import time
        passed = self.passed_values()
        handles = [e["citation"]["l1_handle"] for e in passed]

        def on_disk() -> tuple:
            wal = self.capture.with_name(self.capture.name + "-wal")
            return (hashlib.sha256(self.capture.read_bytes()).hexdigest(),
                    wal.stat().st_size if wal.exists() else 0)
        from operating.ontology_host import CaseHostTools
        host = CaseHostTools(self.runtime, self.manager, store_path=self.store)
        self.addCleanup(host.close)
        before = on_disk()
        writer = sqlite3.connect(str(self.capture), isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")  # a capture holding the write lock
        try:
            started = time.monotonic()
            for handle in handles * 20:  # 60+ cited reads on every path
                self.assertEqual(self.plx_read(self.manager, handle, host=host)["status"],
                                 "resolved")
                self.assertIsNotNone(oi.citation_rows(self.runtime)(handle, self.manager))
                self.runtime.resolve_citation(handle, self.manager)
            elapsed = time.monotonic() - started
        finally:
            writer.execute("ROLLBACK")
        self.assertLess(elapsed, 20.0)  # a read-write open waited 30 s per handle
        self.assertEqual(on_disk(), before)

    def test_a_revision_the_capture_superseded_is_reported_changed_since(self):
        result = self.ask(self.manager, "three_way_match", self.po, store=self.before)
        item = next(e for e in result["answer"] if e["name"] == "item_1")
        self.assertEqual((item["value"], item["check"], item["code"]),
                         ("ITEM-A", "failed", "claim_changed_since"))
        self.assertEqual(result["claim_checks"]["failures"],
                         [{"name": "item_1", "code": "claim_changed_since"}])
        self.assertIn("claim_check_failed", result["answerability"]["reasons"])
        # The current generation cites the new revision, which passes.
        fresh = self.ask(self.manager, "three_way_match", self.po)
        self.assertNotEqual(next(e for e in fresh["answer"] if e["name"] == "item_1")
                            ["citation"]["l1_handle"], item["citation"]["l1_handle"])
        self.assertTrue(fresh["claim_checks"]["passed"])
        # plx.read and a case read the superseded revision alike: its own values, marked
        # changed_since with the current revision, never support.
        from ontology.core import ValidationError
        cited, current = (e["citation"]["l1_handle"] for e in (item, next(
            e for e in fresh["answer"] if e["name"] == "item_1")))
        out = self.plx_read(self.manager, cited)
        self.assertEqual((out["status"], out["current_citation"]), ("changed_since", current))
        self.assertEqual(_text(out["fields"][item["citation"]["field_path"]]), "ITEM-A")
        case_id = self.runtime.cases.open(self.manager, "item")
        with self.assertRaisesRegex(ValidationError, "citation_changed_since"):
            self.runtime.cases.finding(self.manager, case_id, "item ITEM-A", "fact",
                                       "supported", citations=[cited])
        # Asked as known before the change, the revision current then is the answer's own.
        then = self.ask(self.manager, "three_way_match", self.po, as_known_at=self.between)
        self.assertEqual(next(e for e in then["answer"] if e["name"] == "item_1")
                         ["citation"]["l1_handle"], item["citation"]["l1_handle"])
        self.assertTrue(then["claim_checks"]["passed"])


class NamedOrderInvoiceTests(unittest.TestCase):
    """A Portal PO that BC no longer holds as a purchase order (BC deletes a PO once it is
    fully invoiced and posted) while a posted BC invoice names its number: the three-way
    plan reaches that invoice's lines through PurchaseInvoice.names_order, as the Portal
    finds a PO's invoices and plx.process's invoiced side already reads them. The link is a
    lead (the BC company is not known on the Portal row), so the lines are labelled and
    the plan says so; every verdict stays plx.process's. The world is the frozen dev set's
    fixture world with its BC capture (the GET-only BC client over the fixture BC fake), as
    ``scripts.eval_dev_baseline fixture`` builds it. Evidence label: injected-transport."""

    @classmethod
    def setUpClass(cls) -> None:
        from evals import runner
        from ontology.schema import load_schema
        from scripts import eval_dev_baseline as script
        from scripts import eval_oracle_dev as dev
        tmp = tempfile.TemporaryDirectory(prefix="plx-investigate-names-order-")
        cls.addClassCleanup(tmp.cleanup)
        items = [i for i in runner.load_items(DEV, split="dev") if i["family"] == "F4"]
        schema = load_schema()
        relations = sorted(set(script.reachable_relations(
            schema, {i["root"]["relation"] for i in items}, script.DEFAULT_LINK_DEPTH))
            | set(script.plan_relations(schema, ["F4"]) or ()))
        capture, environment = script.fixture_bc_capture(dev.eval_world())
        cls.product = script.build_product(script.fixture_client(), Path(tmp.name) / "w",
                                           relations, budget_seconds=None,
                                           clock=script.fixture_clock, bc_capture=capture,
                                           bc_environment=environment)
        assert (cls.product.info.get("bc_capture") or {}).get("status") == "captured"
        cls.at = dev.FIXTURE_NOW
        cls.principal = script.PRINCIPAL

    def table(self, actor_principal: str, *, bc: bool = True) -> dict:
        reader = ot.StoreReader.open(self.product.store)
        self.addCleanup(reader.conn.close)
        keys = [k for k in reader.audience_keys() if bc or not k.startswith("bc:")]
        policy = ot.ReadPolicy(tenant="plx", audiences={k: {actor_principal} for k in keys},
                               markings={m: {actor_principal} for m in store_ddl.MARKINGS})
        base = ot.OntologyTools(reader, policy, schema=ot.default_schema(),
                                grammar=ot.default_grammar())
        table: dict = {}
        ot.register(table, base)
        tt.register(table, tt.TraverseTools(base))
        op.register(table, op.ProcessTools(base))
        oi.register(table, oi.InvestigateTools(table, schema=ot.default_schema(), rows=None))
        return table

    def run_plan(self, table: dict, item: dict) -> dict:
        actor = Actor("plx", frozenset({self.principal}))
        root = {"identifier": item["root"]["identifier"],
                "identifier_kind": item["root"]["identifier_kind"]}
        result = table["plx.investigate"]["handler"](
            {"family": "F4", "root": root, "as_known_at": self.at}, actor)
        _ok(self, result)
        return result

    def test_invoice_lines_named_by_the_po_number_are_cited_as_leads(self):
        item = _item("dev-F4-0001-fixture")
        table = self.table(self.principal)
        actor = Actor("plx", frozenset({self.principal}))
        po = "PurchaseOrder:portal.app_purchase_order:" + json.dumps(
            item["root"]["key"], separators=(",", ":"))
        # The shape under test: no BC purchase order (its twins are not BC records and no
        # invoice is linked to it or to them), and a BC invoice that names the PO's number.
        twins = table["plx.around"]["handler"](
            {"from": po, "path": [{"link_type": "PurchaseOrder.same_record_as",
                                   "direction": "forward"}],
             "tier_floor": "lead", "as_known_at": self.at}, actor)
        self.assertFalse([o for o in twins["objects"] if ":bc." in o["object_id"]])
        for start in [po] + [o["object_id"] for o in twins["objects"]]:
            hop = table["plx.around"]["handler"](
                {"from": start, "path": [{"link_type": "PurchaseInvoice.invoices",
                                          "direction": "reverse"}],
                 "tier_floor": "lead", "as_known_at": self.at}, actor)
            self.assertEqual(hop["hops"][-1]["count"], 0, start)
        named = table["plx.around"]["handler"](
            {"from": po, "path": [{"link_type": "PurchaseInvoice.names_order",
                                   "direction": "reverse"}],
             "tier_floor": "lead", "as_known_at": self.at}, actor)
        self.assertGreater(named["hops"][-1]["count"], 0, named)

        result = self.run_plan(table, item)
        expected = {v["name"]: v for v in item["known_outcome"]["values"]
                    if v["name"].startswith("invoiced") and v["value"] is not None}
        self.assertTrue(expected)
        invoiced = self.answer(result, "invoiced")
        matched = []
        for entry in invoiced:
            handle = entry["citation"]["l1_handle"]
            self.assertTrue(handle.startswith("bc://"), handle)
            self.assertEqual(entry["quantity_role"], "invoiced")
            # Reached only through a lead link: labelled on the entry, never silent.
            self.assertTrue(entry.get("lead_basis"), entry)
            twin = [e for e in expected.values()
                    if {c["l1_handle"].rsplit("@", 1)[0] for c in e["citations"]}
                    == {handle.rsplit("@", 1)[0]}]
            if twin:
                self.assertEqual(len(twin), 1, entry)
                self.assertEqual(float(entry["value"]), float(twin[0]["value"]))
                matched.append(twin[0]["name"])
            else:
                # Any other line of those invoices is one of no item (a freight charge),
                # as on a BC twin's invoices: context, never an item's invoiced quantity.
                items = table["plx.around"]["handler"](
                    {"from": entry["object_id"], "path": [
                        {"link_type": "PurchaseInvoiceLine.for_item", "direction": "forward"}],
                     "tier_floor": "lead", "as_known_at": self.at}, actor)
                self.assertEqual(items["hops"][-1]["count"], 0, entry)
        self.assertEqual(sorted(matched), sorted(expected))
        self.assertNotIn("missing_invoiced", result["answerability"]["reasons"])
        lead_steps = {g["subject"] for g in result["gaps"]
                      if g["reason"] == "lead_tier_links_followed"}
        named_steps = {s["step"] for s in result["trace"] if s["tool"] == "plx.around"
                       and s["status"] == "ok"} & lead_steps
        self.assertTrue(any("named" in s for s in named_steps), lead_steps)
        # Every verdict is still plx.process's own, never the plan's.
        process = table["plx.process"]["handler"](
            {"from": po, "process": "purchase_to_invoice", "as_known_at": self.at}, actor)
        verdicts = {e["name"]: e["value"] for e in result["answer"] if e["kind"] == "select"}
        for name, roles in (("ordered_vs_received", ["ordered", "received"]),
                            ("received_vs_invoiced", ["received", "invoiced"])):
            agrees = [c["agree"] for c in process["quantity_checks"] if c["roles"] == roles]
            self.assertEqual(verdicts[name], False if False in agrees else
                             None if None in agrees or not agrees else True, name)
        self.assertLessEqual({g["reason"] for g in process["gaps"]},
                             {g["reason"] for g in result["gaps"]})
        self.assertLessEqual(result["steps"]["used"], result["steps"]["budget"])
        self.assertNotIn("step_budget_exhausted", {g["reason"] for g in result["gaps"]})

    def test_a_reader_without_the_bc_grant_sees_no_named_invoice(self):
        item = _item("dev-F4-0001-fixture")
        result = self.run_plan(self.table(self.principal, bc=False), item)
        self.assertTrue(all(e["value"] is None for e in self.answer(result, "invoiced")))
        self.assertIn("missing_invoiced", result["answerability"]["reasons"])
        dumped = json.dumps(result)
        self.assertNotIn("bc://", dumped)
        self.assertNotIn("PurchaseInvoice", json.dumps(result["chain"]))

    def test_a_result_over_the_cap_drops_a_lead_edges_citations_before_a_verified_edges(self):
        """Fitting the envelope strips a lead edge's feature citations (they back no counted
        value) before those of a verified edge further down the chain, so the chain rows the
        claim gate counts stay cited when the lead's bytes are enough."""
        def edge(tier, ref):
            return {"from": f"o{ref}", "to": f"p{ref}", "tier": tier, "reason": [
                {"feature": "number", "citations": [f"bc://co/purchaseInvoices/{ref}" * 20]}]}
        out = {"chain": {"edges": [edge("lead", 1), edge("verified", 2)], "objects": []},
               "values": [], "answer": [], "gaps": [],
               "completeness": {"complete": True, "truncated": False}}
        lead_bare = copy.deepcopy(out)
        del lead_bare["chain"]["edges"][0]["reason"][0]["citations"]
        lead_bare["completeness"] = {"complete": False, "truncated": True,
                                     "reason": "result_too_large"}
        tools = object.__new__(oi.InvestigateTools)
        tools.limits = {"max_result_bytes": ot._envelope_bytes(lead_bare)}
        fitted = tools._fit(out)
        lead, verified = fitted["chain"]["edges"]
        self.assertNotIn("citations", lead["reason"][0])
        self.assertIn("citations", verified["reason"][0])
        self.assertEqual(fitted["completeness"]["reason"], "result_too_large")

    answer = staticmethod(Base.answer)


class EvalRunnerTests(Base):
    def test_the_eval_runner_records_and_scores_a_plx_investigate_run(self):
        from evals import baseline as bl
        from evals import runner
        from evals import score as scoring
        from scripts import eval_dev_baseline as script
        item = _item("dev-F3-0001-fixture")
        case_host = runner.CaseHost(FixtureWorld.product.state_dir,
                                    FixtureWorld.product.audience, script.TENANT,
                                    [self.PRINCIPAL])
        table = bl.ontology_tool_table(FixtureWorld.product.store, tenant=script.TENANT,
                                       principals=[self.PRINCIPAL])
        oi.register(table, oi.InvestigateTools(
            table, schema=FixtureWorld.schema, rows=oi.citation_rows(FixtureWorld.runtime)))
        host = bl.ToolHost(case_host, table, script.TENANT, [self.PRINCIPAL])
        root = self.root_of(item)
        at = self.at

        def script_fn(prompt, call):
            out = call("plx.investigate", {"family": item["family"], "root": root,
                                           "as_known_at": at})
            values = []
            for entry in out["answer"]:
                if entry["kind"] != "property":
                    continue
                value = {"name": entry["name"], "value": entry["value"]}
                if entry["value"] is not None:
                    value["citations"] = [{"l1_handle": entry["citation"]["l1_handle"],
                                           "field_path": entry["citation"]["field_path"]}]
                for key in ("unit", "quantity_role"):
                    if entry.get(key):
                        value[key] = entry[key]
                values.append(value)
            answer = {"format": scoring.ANSWER_FORMAT,
                      "answerability": out["answerability"]["state"],
                      "text": "Quantities as cited.", "values": values,
                      "gaps": [{"state": g["state"], "subject": g["subject"],
                                "reason": g["reason"]} for g in out["gaps"]]}
            return "```json\n" + json.dumps(answer) + "\n```"
        with runner.MirrorPinReader(FixtureWorld.product.mirror_path) as reader:
            pin = runner.make_run_pin(item, reader)
        try:
            transcript = runner.record(item, runner.ScriptedAgent(script_fn), host,
                                       evidence_label="isolated-fixture", run_pin=pin)
        finally:
            host.close()
        self.assertEqual([e["name"] for e in transcript["events"]], ["plx.investigate"])
        self.assertIsNotNone(transcript["answer"], transcript["answer_error"])
        self.assertIn("plx.investigate", {t["name"] for t in transcript["host"]["tools"]})
        result = scoring.score(item, transcript)
        ordered = next(v for v in result["values"] if v["name"] == "ordered")
        self.assertTrue(ordered["passed"], ordered)
        self.assertTrue(runner.verify_seal(transcript))


if __name__ == "__main__":
    unittest.main()
