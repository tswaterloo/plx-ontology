"""plx.investigate with an inline plan (``plan``, format plx-investigate-plan/v1) on the
fixture world of ``tests/test_ontology_tools_investigate.py``.

A valid inline plan runs the stored plans' executor (engines, tiers, gaps, citations and
claim gate); one the live-schema check refuses is a typed ``plan_<problem>`` error with a
hint, before any step runs; a stored family still runs its stored plan. Evidence label:
isolated-fixture (synthetic rows, never live PLX data).
"""

from __future__ import annotations

import copy
import json
import unittest

from ontology.types import Actor
from operating import ontology_tools_investigate as oi
from tests import test_ontology_tools_investigate as fixture

EXAMPLES = json.loads(oi.INLINE_EXAMPLES.read_text("utf-8"))["examples"]
STORED = json.loads((oi.PLAN_DIR / "order_quantities.json").read_text("utf-8"))  # F3
ROOTS = {"Supplier": 'Supplier:portal.app_supplier:{"id":"fx-supp-1"}',
         "PurchaseOrder": 'PurchaseOrder:portal.app_purchase_order:{"id":"fx-po-1"}',
         "Shipment": 'Shipment:portal.app_shipment:{"id":"fx-shp-1"}'}


def tearDownModule() -> None:  # noqa: N802 - unittest hook
    fixture.tearDownModule()  # cleans the shared world and marks it unbuilt


class InlinePlanTests(fixture.Base):
    def ask(self, table: dict, arguments: dict, actor: Actor | None = None) -> dict:
        result = table["plx.investigate"]["handler"](
            {"as_known_at": self.at, **arguments}, actor or self.actor)
        fixture._ok(self, result)
        return result

    def test_an_inline_plan_answers_as_the_stored_plan_it_copies(self):
        table, _ = self.table()
        root = self.root_of(fixture._item("dev-F3-0001-fixture"))
        copied = {**copy.deepcopy(STORED), "id": "order_quantities_inline"}
        outsider = Actor(self.TENANT, frozenset({"staff:no-grant"}))
        for actor in (self.actor, outsider):
            stored = self.ask(table, {"family": "F3", "root": root}, actor)
            inline = self.ask(table, {"plan": copied, "root": root}, actor)
            self.assertTrue(stored["procedure"].startswith("procedure:order_quantities@"))
            self.assertTrue(inline["procedure"].startswith(
                "procedure:inline-order_quantities_inline@"))
            self.assertEqual(inline["meta"]["procedure"], inline["procedure"])
            # Same answer, values, citations, gaps and claim checks: only the label differs.
            for result in (stored, inline):
                result.pop("procedure")
                result["meta"].pop("procedure")
            self.assertEqual(inline, stored)
        self.assertEqual(inline["answerability"]["reasons"], ["root_not_found"])
        self.assertFalse(inline["values"])
        mine = self.ask(table, {"plan": copied, "root": root})
        self.assertTrue(mine["claim_checks"]["checked"])
        self.assertTrue(any(e["value"] is not None and e.get("citation")
                            for e in mine["answer"]))
        # Stored first: the stored plan's own document under its id is the stored plan; a
        # different one under that id is refused rather than shadowing it.
        same = self.ask(table, {"plan": copy.deepcopy(STORED), "root": root})
        self.assertTrue(same["procedure"].startswith("procedure:order_quantities@"))
        changed = {**copy.deepcopy(STORED), "step_budget": STORED["step_budget"] - 1}
        refused = self.ask(table, {"plan": changed, "root": root})
        self.assertEqual(refused["error"]["code"], "plan_id_taken")
        self.assertIn("family", refused["error"]["hint"])

    def test_the_worked_examples_answer_or_refuse_as_they_state(self):
        table, _ = self.table()
        for example in EXAMPLES:
            plan = example["plan"]
            with self.subTest(plan["id"]):
                root = ROOTS[plan["root"]["object_types"][0]]
                result = self.ask(table, {"plan": copy.deepcopy(plan), "root": root})
                if example["expect"] == "answered":
                    self.assertNotIn("error", result)
                    self.assertTrue(result["procedure"].startswith(
                        f"procedure:inline-{plan['id']}@"))
                    self.assertTrue(result["claim_checks"]["checked"])
                    self.assertEqual(result["claim_checks"]["failures"], [])
                else:
                    self.assertEqual(result["error"]["code"], example["expect"])
                    self.assertTrue(result["error"]["hint"])
                    self.assertNotIn("answer", result)
        # The documents of a shipment: its number is cited and re-read by the claim gate;
        # the fixture has no mail or SharePoint document, so those counts are typed gaps.
        shipment = next(e["plan"] for e in EXAMPLES if e["plan"]["id"] == "shipment_documents")
        result = self.ask(table, {"plan": shipment, "root": ROOTS["Shipment"]})
        number = [e for e in result["answer"] if e["name"] == "shipment_number"]
        self.assertEqual([(e["check"], bool(e["citation"])) for e in number], [("passed", True)])
        # The fixture holds no past-due order: an honest no_match gap, never an empty pass.
        due = next(e["plan"] for e in EXAMPLES if e["plan"]["id"] == "supplier_orders_past_due")
        result = self.ask(table, {"plan": due, "root": ROOTS["Supplier"]})
        self.assertIn("no_match", {g["reason"] for g in result["gaps"]})
        self.assertNotEqual(result["answerability"]["state"], "answerable")

    def test_a_plan_the_schema_check_refuses_is_a_typed_error_before_any_step(self):
        table, _ = self.table()
        root = ROOTS["PurchaseOrder"]
        lines = next(e["plan"] for e in EXAMPLES if e["plan"]["id"] == "order_lines_not_received")

        def variant(change) -> dict:
            doc = copy.deepcopy(lines)
            change(doc)
            return doc

        def link(doc):
            doc["steps"][0]["args"]["path"][0]["link_type"] = "PurchaseOrderLine.ships_with"

        def filtered(doc):
            doc["steps"][0]["args"]["filters"][0]["property"] = "quantity_left"

        def read(doc):
            doc["steps"][1]["args"]["properties"].append("due_date")

        def budget(doc):
            doc["step_budget"] = oi.MAX_STEP_BUDGET + 1

        def steps(doc):
            doc["step_budget"] = 1

        def unknown_key(doc):
            doc["owner"] = "planner"

        cases = (("undeclared link", link, "plan_undeclared_link"),
                 ("unknown filter property", filtered, "plan_unknown_property"),
                 ("unknown read property", read, "plan_unknown_property"),
                 ("budget over the maximum", budget, "plan_over_budget"),
                 ("more steps than its budget", steps, "plan_over_budget"),
                 ("a key the format does not declare", unknown_key, "plan_format_invalid"))
        for label, change, code in cases:
            with self.subTest(label):
                result = self.ask(table, {"plan": variant(change), "root": root})
                self.assertEqual(result["error"]["code"], code)
                self.assertTrue(result["error"]["hint"])
                self.assertNotIn("trace", result)
        # family and plan together, neither, or follow_ups on an inline plan: invalid input.
        case = {"case_id": "case-" + "0" * 32}
        for arguments, says in (({"family": "F3", "plan": lines, "root": root}, "exactly one"),
                                ({"root": root}, "exactly one"),
                                ({"plan": lines, "root": root, "follow_ups": case},
                                 "stored plans only")):
            with self.subTest(sorted(arguments)):
                error = self.ask(table, arguments)["error"]
                self.assertEqual(error["code"], "invalid_input")
                self.assertIn(says, error["message"])


if __name__ == "__main__":
    unittest.main()
