"""The answer code states from a plan result (``operating.plan_answer``), and the loop's
``answer_plan`` action over it. Evidence: isolated-fixture."""

from __future__ import annotations

import copy
import unittest

from operating import answer_gate, plan_answer

HANDLE = "bc://production/c1/purchaseOrderLines/L1@v-1"


def prop(name, value, handle=HANDLE, field="row.number", **extra):
    return {"name": name, "kind": "property", "value": value, "check": "passed",
            "citation": {"l1_handle": handle, "field_path": field}, **extra}


def line_entry(n, date="2026-07-14"):
    handle = f"bc://production/c1/purchaseOrderLines/L{n}@v-{n}"
    return {"name": f"open_line_{n}", "kind": "select", "value": date,
            "check": "not_applicable",
            "cells": [{"property": "bc_expected_receipt_date", "value": date,
                       "citation": {"l1_handle": handle,
                                    "field_path": "row.expectedReceiptDate"}}]}


def result(entries, **extra):
    return {"answer": entries, "completeness": {"complete": True, "truncated": False},
            "answerability": {"state": "partial"},
            "gaps": [{"state": "unknown", "reason": "no_match", "subject": "Receipt.receives"}],
            **extra}


class FromPlanTests(unittest.TestCase):
    def test_each_valued_entry_is_one_cited_claim_and_each_gap_an_unknown(self):
        out = plan_answer.from_plan(result([
            prop("po_1", "PO1"), line_entry(1), line_entry(2),
            {"name": "late_1", "kind": "select", "value": None, "check": "not_applicable"},
            {"name": "why_1", "kind": "select", "value": "partial_receipt",
             "check": "not_applicable"}]))
        self.assertEqual([(c["value"], c["citation"], c["field"]) for c in out["claims"]], [
            ("PO1", HANDLE, "row.number"),
            ("2026-07-14", "bc://production/c1/purchaseOrderLines/L1@v-1",
             "row.expectedReceiptDate"),
            ("2026-07-14", "bc://production/c1/purchaseOrderLines/L2@v-2",
             "row.expectedReceiptDate")])
        self.assertEqual(len({c["id"] for c in out["claims"]}), 3)
        self.assertEqual(out["unknowns"], [
            "undetermined: late_1 (the plan states no value)",
            "not one record's value: why_1 = partial_receipt",
            "unknown: Receipt.receives - no_match"])
        self.assertEqual(set(out), {"action", "claims", "unknowns"})
        for claim in out["claims"]:
            self.assertLessEqual(set(claim), answer_gate.CLAIM_KEYS)

    def test_a_lead_a_role_and_a_unit_are_stated_as_the_entry_has_them(self):
        out = plan_answer.from_plan(result([
            prop("po_2", "PO1", lead_basis=True),
            prop("qty_1", "4", field="row.quantity", quantity_role="ordered", unit="KG"),
            {**prop("qty_2", "5", field="row.quantity", quantity_role="ordered", unit="KG"),
             "check": "unverified"}]))
        first, second, third = out["claims"]
        self.assertIs(first["lead"], True)
        self.assertEqual((second["role"], second["unit"]), ("ordered", "KG"))
        self.assertNotIn("role", third)   # an unverified role is never claimed
        self.assertNotIn("lead", second)

    def test_what_code_cannot_state_whole_is_left_to_the_planner(self):
        base = [prop("po_1", "PO1")]
        for label, bad in (
                ("an error", {"error": {"code": "x"}}),
                ("no answer", {"answer": []}),
                ("a cut list", {**result(base), "completeness": {"truncated": True,
                                                                "reason": "step_budget"}}),
                ("a fitted result missing a cell", {**result([
                    {"name": "open_line_1", "kind": "select", "value": "2026-07-14",
                     "check": "not_applicable", "inputs_cut": True}]),
                    "completeness": {"truncated": True, "reason": "result_too_large"}}),
                ("no completeness", {"answer": base}),
                ("a failed check", result([{**prop("a", "x"), "check": "failed"}])),
                ("a float", result([prop("a", 1.5)])),
                ("a bool", result([prop("a", True)])),
                ("no citation", result([{"name": "a", "kind": "property", "value": "x",
                                         "check": "passed"}])),
                ("a count", result([{"name": "n", "kind": "count", "value": 3}])),
                ("a computed value", result([{"name": "t", "kind": "computed", "value": 3}])),
                ("too many", result([prop(f"p{i}", "x") for i in range(21)])),
                ("only nulls", result([{"name": "late_1", "kind": "select", "value": None}]))):
            with self.subTest(label):
                self.assertIsNone(plan_answer.from_plan(bad))
        self.assertIsNone(plan_answer.from_plan(None))
        self.assertIsNone(plan_answer.from_plan([prop("a", "x")]))

    def test_a_result_the_host_only_fitted_is_stated_when_every_cell_is_there(self):
        fitted = {**result([prop("po_1", "PO1"), line_entry(1)]),
                  "completeness": {"complete": False, "truncated": True,
                                   "reason": "result_too_large"}}
        out = plan_answer.from_plan(fitted)
        self.assertEqual(len(out["claims"]), 2)
        # a verdict whose cells the host did not cut is only listed, never claimed
        fitted["answer"].append({"name": "why_1", "kind": "select", "value": "a > b",
                                 "check": "not_applicable"})
        self.assertEqual(len(plan_answer.from_plan(fitted)["claims"]), 2)

    def test_an_answerable_plan_lists_no_gap_and_the_input_is_not_changed(self):
        given = result([prop("po_1", "PO1")], answerability={"state": "answerable"})
        before = copy.deepcopy(given)
        out = plan_answer.from_plan(given)
        self.assertEqual(out["unknowns"], [])
        self.assertEqual(given, before)
        # ...but a list the host cut is stated even then: the claims are not all the lines
        given["gaps"].append({"state": "unknown", "reason": "lines_truncated",
                              "subject": "receipt lateness",
                              "detail": "4 of 14 lines are not listed"})
        self.assertEqual(plan_answer.from_plan(given)["unknowns"],
                         ["unknown: receipt lateness - 4 of 14 lines are not listed"])

    def test_unknowns_past_the_answer_limit_are_counted_not_dropped(self):
        limit = answer_gate.MAX_UNKNOWNS
        for gaps, listed in ((limit, limit), (limit + 5, limit - 1)):
            given = result([prop("po_1", "PO1")], gaps=[
                {"state": "unknown", "subject": f"S{i}"} for i in range(gaps)])
            out = plan_answer.from_plan(given)
            self.assertEqual(len(out["unknowns"]), limit, gaps)
            self.assertEqual(out["unknowns"][:listed], [f"unknown: S{i}" for i in range(listed)])
            if listed < gaps:
                self.assertIn(f"{gaps - listed} more gaps", out["unknowns"][-1])

    def test_n_lines_give_n_claims_each_citing_its_own_line(self):
        for n in (1, 10, 20):
            out = plan_answer.from_plan(result([line_entry(i) for i in range(1, n + 1)]))
            self.assertEqual([c["citation"] for c in out["claims"]],
                             [f"bc://production/c1/purchaseOrderLines/L{i}@v-{i}"
                              for i in range(1, n + 1)], n)


if __name__ == "__main__":
    unittest.main()
