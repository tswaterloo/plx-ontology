"""Checked value claims: structured claims verified exactly, prose fail-closed.

Evidence label: ``isolated-fixture`` for the pure gate (rows built here with the values of
the AO-26005B live trace: AO item 58383 planned 1509 / actual 1503, link 1245 qtymfg_c
1503, Filling task 14355 qty 1502, Line clearance task 14354 qty 1, order line 1140 qty
1920, Portal manufacturing order quantity / size_pieces 1509) and ``injected-transport``
for the AO-26005B case driven through the MCP case tools over
``FakeWorkbench``. Nothing here is live data. The prose phrasings are the reviewer's probes
(review of 7604d54: separators, bullets, tables, newlines, number words, extra rows).
"""

from __future__ import annotations

from decimal import Decimal
import time
import unittest
from unittest.mock import patch

from ontology.core import Actor, ValidationError
from operating import answer_gate, case_verification, claim_checks
from operating import playbook_eval as pe
from operating.source_units import SourceUnitContracts
from operating.tests import test_ao_26005b_case as ao_tests
from operating.tests import test_customer_po_coverage as cpo_tests
from operating import customer_po_coverage as cpo
from operating.tests.test_playbook_eval import CPO, REGISTRY, CustomerPoEvalBase, current_copy

CONTRACTS = SourceUnitContracts.builtin()
ITEM = "api://silver.assembly_order_items/%7B%22id%22%3A58383%7D@g-8"
LINK = "api://public.order_entry_item_assembly/%7B%22id%22%3A1245%7D@g-6"
LINE = "api://public.order_entry_item/%7B%22id%22%3A1140%7D@g-7"
FILLING = "api://silver.production_tasks/%7B%22id%22%3A14355%7D@g-9"
CLEARANCE = "api://silver.production_tasks/%7B%22id%22%3A14354%7D@g-9"
MO = "api://public.app_manufacturing_order/%7B%22id%22%3A%22mo-10718%22%7D@g-1"
PO_LINE = "api://public.app_purchase_order_line/%7B%22id%22%3A%22pol-0%22%7D@g-5"
DAMAGE = "api://silver.assembly_order_damage/%7B%22id%22%3A5445%7D@g-2"
ROWS = {
    ITEM: {"relation": "silver.assembly_order_items",
           "row": {"id": 58383, "assembly_order_id": 10718, "quantity": 1509.0,
                   "actual_amount": 1503.0, "qty_per_batch": 1}},
    LINK: {"relation": "public.order_entry_item_assembly",
           "row": {"id": 1245, "idassembly": 10718, "qtymfg_c": 1503.0, "qtyplan_c": 0.0}},
    LINE: {"relation": "public.order_entry_item", "row": {"id": 1140, "idoe": 351, "qty": 1920}},
    FILLING: {"relation": "silver.production_tasks",
              "row": {"id": 14355, "task_type": "Filling", "qty": "1502.0"}},
    CLEARANCE: {"relation": "silver.production_tasks",
                "row": {"id": 14354, "task_type": "Line clearance", "qty": "1.0"}},
    MO: {"relation": "public.app_manufacturing_order",
         "row": {"id": "mo-10718", "quantity": "1509", "size_pieces": "1509",
                 "qty_sample": "1", "qty_damaged": "0"}},
    DAMAGE: {"relation": "silver.assembly_order_damage",
             "row": {"id": 5445, "assembly_item_id": 58383, "qty_damaged": 3.0}},
    PO_LINE: {"relation": "public.app_purchase_order_line",
              "row": {"id": "pol-0", "code": "P-02458", "quantity": 12500.0, "uom": "EA"}},
}


def _input(handle, field, unit, role=None):
    spec = {"kind": "citation", "field": field, "unit": unit,
            "citation": {"handle": handle, "status": "resolved"}}
    if role:
        spec["role"] = role
    return spec


C1 = {"id": "C1", "expression": "planned / per_unit - produced / per_unit", "result": "6",
      "unit": "EA", "result_role": "planned_not_produced",
      "relations": [{"left": "planned", "op": "-", "right": "actual",
                     "as": "planned_not_produced"}],
      "inputs": {"planned": _input(ITEM, "row.quantity", "pc", "planned"),
                 "produced": _input(ITEM, "row.actual_amount", "pc", "actual"),
                 "per_unit": _input(ITEM, "row.qty_per_batch", "pc/EA")}}
C2 = {"id": "C2", "expression": "q", "result": "12500", "unit": "EA", "result_role": None,
      "inputs": {"q": _input(PO_LINE, "row.quantity", "EA")}}
C3 = {"id": "C3", "expression": "ordered - produced / per_unit", "result": "417",
      "unit": "EA", "result_role": "ordered_not_produced_by_this_ao",
      "inputs": {"ordered": _input(LINE, "row.qty", "EA", "ordered"),
                 "produced": _input(ITEM, "row.actual_amount", "pc", "actual"),
                 "per_unit": _input(ITEM, "row.qty_per_batch", "pc/EA")}}
# planned - damaged declared "as produced": a caller naming a combination a source role.
C4 = {"id": "C4", "expression": "p - d", "result": "1509", "unit": "pc",
      "result_role": "produced",
      "relations": [{"left": "planned", "op": "-", "right": "damaged", "as": "produced"}],
      "inputs": {"p": _input(MO, "row.quantity", "pc", "planned"),
                 "d": _input(MO, "row.qty_damaged", "pc", "damaged")}}
# A one-input pass-through whose recorded role a caller declared on an uncontracted column.
C5 = {"id": "C5", "expression": "q", "result": "1", "unit": "EA", "result_role": "inspected",
      "inputs": {"q": _input(MO, "row.qty_sample", "EA", "inspected")}}
# A pass-through of a contract-pinned input: the pinned role, whatever was recorded.
C6 = {"id": "C6", "expression": "q", "result": "1509", "unit": "EA", "result_role": "produced",
      "inputs": {"q": _input(MO, "row.size_pieces", "EA", "produced")}}
CALCS = [C1, C2, C3, C4, C5, C6]


def derived_calc(name):
    """C7: the MO's planned quantity minus its damaged count, the result named ``name``."""
    return {"id": "C7", "expression": "p - d", "result": "1509", "unit": "pc",
            "relations": [{"left": "planned", "op": "-", "right": "damaged", "as": name}],
            "inputs": {"p": _input(MO, "row.quantity", "pc", "planned"),
                       "d": _input(MO, "row.qty_damaged", "pc", "damaged")}}


def good_calc(name):
    """C7: the link's manufactured quantity minus the MO's damaged count (good produced
    units, Portal schema.prisma@878705b:2534), the result named ``name``."""
    return {"id": "C7", "expression": "m - d", "result": "1503", "unit": "pc",
            "relations": [{"left": "manufactured", "op": "-", "right": "damaged", "as": name}],
            "inputs": {"m": _input(LINK, "row.qtymfg_c", "pc"),
                       "d": _input(MO, "row.qty_damaged", "pc")}}


def vc(value, handle=None, field=None, role=None, unit=None, calculation=None):
    out = {"value": value}
    if calculation:
        out["calculation"] = calculation
    else:
        out.update(citation=handle, field=field)
    if role:
        out["role"] = role
    if unit:
        out["unit"] = unit
    return out


def gate(text, citations, claims=(), rows=None, calculations=CALCS, outcome=None,
         contracts=CONTRACTS, returned=None, question=None, **item):
    handles = list(citations)
    parsed = claim_checks.parse_value_claims(list(claims), handles,
                                             [calc["id"] for calc in calculations])
    rows = ROWS if rows is None else rows
    return claim_checks.check_items(
        [{"id": "F1", "texts": [text], "value_claims": parsed,
          "citations": [{"handle": handle, "status": "resolved"} for handle in handles],
          **item}],
        rows=rows.get, calculations=calculations, contracts=contracts, outcome=outcome,
        returned=returned, question=question)


def codes(result):
    return [item["code"] for item in result["failures"]]


def checked(result):
    return [claim for entry in result["items"] for claim in entry["claims"]]


QUANTITY_TYPES_NOT_PLANNED = ("produced", "manufactured", "actual", "filled", "inspected",
                              "accepted", "ordered", "damaged")


class StructuredClaimTests(unittest.TestCase):
    def test_a_planned_figure_claimed_as_any_other_quantity_type_is_caught(self):
        for role in QUANTITY_TYPES_NOT_PLANNED:
            with self.subTest(role=role):
                result = gate("AO-26005B: 1509.", [ITEM],
                              [vc("1509", ITEM, "row.quantity", role)])
                self.assertEqual(codes(result), ["claim_role_mismatch"])
                self.assertFalse(result["passed"])
                self.assertIn("planned quantity (contract "
                              "silver-assembly-order-items-quantity-planned)",
                              result["failures"][0]["detail"])
        # A quantity field with a role contract needs the claim to state its role.
        self.assertEqual(codes(gate("1509.", [ITEM], [vc("1509", ITEM, "row.quantity")])),
                         ["claim_role_mismatch"])

    def test_the_correct_claims_pass_on_their_source_contracts(self):
        result = gate("Planned 1509, produced 1503 (link 1503), filled 1,502, ordered 1920.",
                      [ITEM, LINK, FILLING, LINE],
                      [vc("1509", ITEM, "row.quantity", "planned"),
                       vc("1503", ITEM, "row.actual_amount", "produced"),
                       vc("1503", LINK, "row.qtymfg_c", "manufactured"),
                       vc("1502", FILLING, "row.qty", "filled"),
                       vc(1920, LINE, "row.qty", "ordered")])
        self.assertTrue(result["passed"], result["failures"])
        self.assertEqual([claim["role_basis"] for claim in checked(result)],
                         ["source_contract"] * 5)
        self.assertEqual(checked(result)[3]["role_contract"],
                         "silver-production-tasks-qty-filled-on-filling")

    def test_the_manufacturing_order_size_is_planned_never_produced(self):
        for field in ("row.quantity", "row.size_pieces"):
            with self.subTest(field=field):
                self.assertEqual(codes(gate("AO-26005B produced 1509.", [MO],
                                            [vc("1509", MO, field, "produced")])),
                                 ["claim_role_mismatch"])
                self.assertTrue(gate("AO-26005B planned 1509.", [MO],
                                     [vc("1509", MO, field, "planned")])["passed"])

    def test_a_quantity_type_on_a_field_without_a_role_contract_is_never_a_clean_pass(self):
        # A Line clearance task's qty: the conditional contract covers Filling tasks only.
        for handle, field, role in ((CLEARANCE, "row.qty", "filled"),
                                    (MO, "row.qty_sample", "produced"),
                                    (PO_LINE, "row.quantity", "ordered")):
            with self.subTest(field=field, role=role):
                value = ROWS[handle]["row"][field[4:]]
                result = gate(f"{value}.", [handle], [vc(str(value), handle, field, role)])
                self.assertEqual(codes(result), ["claim_role_unverified"])
        # Untyped (no quantity type claimed) on an unroled field passes, counted as untyped.
        result = gate("1 at line clearance.", [CLEARANCE],
                      [vc("1", CLEARANCE, "row.qty")])
        self.assertTrue(result["passed"])
        self.assertEqual(result["counts"]["untyped"], 1)

    def test_a_value_the_cited_field_does_not_hold_fails(self):
        self.assertEqual(codes(gate("planned 1504", [ITEM],
                                    [vc("1504", ITEM, "row.quantity", "planned")])),
                         ["claim_value_mismatch"])
        self.assertEqual(codes(gate("planned 1509", [ITEM],
                                    [vc("1509", ITEM, "row.no_such", "planned")])),
                         ["claim_value_mismatch"])
        # The cited row cannot be read now (revoked, changed): uncited, never trusted.
        self.assertEqual(codes(gate("planned 1509", [ITEM],
                                    [vc("1509", ITEM, "row.quantity", "planned")], rows={})),
                         ["claim_uncited"])
        # The re-read is a changed-since view (a live record's new version, a capture
        # revision superseded before the answer's moment): never verified.
        moved = {**ROWS, ITEM: {"citation": ITEM, "status": "changed_since",
                                "changed_since": True}}
        self.assertEqual(codes(gate("planned 1509", [ITEM],
                                    [vc("1509", ITEM, "row.quantity", "planned")], rows=moved)),
                         ["claim_changed_since"])
        self.assertEqual(codes(gate("A shortfall of 6.", [ITEM], [vc(
            "6", role="planned_not_produced", calculation="C1")], rows=moved)),
            ["claim_changed_since"])
        # A superseded capture revision keeps its row (an as_known_at read checks it) but
        # is flagged changed_since: the flag decides, whatever the status says, and an
        # unexpected flag value fails closed.
        for flag in (True, "yes"):
            kept = {**ROWS, ITEM: {**ROWS[ITEM], "status": "superseded", "changed_since": flag}}
            self.assertEqual(codes(gate("planned 1509", [ITEM], [
                vc("1509", ITEM, "row.quantity", "planned")], rows=kept)),
                ["claim_changed_since"])

    def test_a_decimal_comma_is_no_thousands_separator(self):
        # Review p0-b: to_decimal stripped every comma, so a cell "1,5" held 15.
        rows = {PO_LINE: {"relation": "public.app_purchase_order_line",
                          "row": {"id": "pol-0", "quantity": "1,5", "total": "12,500"}}}
        self.assertEqual(codes(gate("15 EA", [PO_LINE], [vc("15", PO_LINE, "row.quantity")],
                                    rows=rows, calculations=[])), ["claim_value_mismatch"])
        self.assertTrue(gate("12500", [PO_LINE], [vc("12500", PO_LINE, "row.total")],
                             rows=rows, calculations=[])["passed"])

    def test_an_identifier_claim_holds_in_its_canonical_form_only_on_a_declared_carrier(self):
        # po-lateness live check: plx.investigate claims PurchaseOrder.po_number as the
        # projection stores it (PO1001) on BC purchaseOrders.number, which holds PO-1001.
        bc = ("bc://production/c0000000-0000-4000-8000-000000000001/purchaseOrders/"
              "00000001-0001-4000-8000-000000000001@r-8")
        portal = "api://public.app_purchase_order/%7B%22id%22%3A%22po-1%22%7D@g-1"
        rows = {bc: {"citation": bc, "row": {"number": "PO-1001", "vendorName": "PO-1001",
                                             "postingDescription": "PO-1001-FM12"}},
                portal: {"relation": "public.app_purchase_order",
                         "row": {"id": "po-1", "po_number": "PO-1001-FM12"}}}

        def one(value, handle, field):
            return codes(gate(f"Purchase order {value}.", [handle], [vc(value, handle, field)],
                              rows=rows, calculations=[]))
        self.assertEqual(one("PO1001", bc, "row.number"), [])         # canonical form
        self.assertEqual(one("PO-1001", bc, "row.number"), [])        # as written
        self.assertEqual(one("PO1001-FM12", portal, "row.po_number"), [])
        for value, handle, field in (
                ("PO1002", bc, "row.number"),           # another identifier
                ("po1001", bc, "row.number"),           # a spelling that is not canonical
                ("PO-1-001", bc, "row.number"),
                ("PO1001", portal, "row.po_number"),    # the FM suffix is identity
                ("PO1001", bc, "row.vendorName"),       # no carrier: exact text only
                ("PO1001-FM12", bc, "row.postingDescription")):
            self.assertEqual(one(value, handle, field), ["claim_value_mismatch"],
                             (value, field))

    def test_units_must_match_the_contract_or_the_recorded_unit(self):
        self.assertEqual(codes(gate("12500.0 kg", [PO_LINE],
                                    [vc("12500.0", PO_LINE, "row.quantity", unit="kg")])),
                         ["claim_unit_mismatch"])
        result = gate("12500.0 EA", [PO_LINE], [vc("12500.0", PO_LINE, "row.quantity",
                                                   unit="EA")])
        self.assertTrue(result["passed"])
        self.assertEqual(checked(result)[0]["unit_basis"], "recorded_calculation")
        # No unit recorded anywhere: the stated unit passes, counted as unverified.
        result = gate("1920 EA", [LINE], [vc("1920", LINE, "row.qty", "ordered", "EA")],
                      calculations=[])
        self.assertEqual(result["counts"]["unit_unverified"], 1)

    def test_a_calculation_claim_is_its_recomputed_result_and_role(self):
        # planned - actual is a difference of two types: untyped, so it is cited as a
        # number ("a shortfall of 6"), under its label, and never typed by a word.
        self.assertTrue(gate("A shortfall of 6 finished goods.", [ITEM],
                             [vc("6", role="planned_not_produced", unit="EA",
                                 calculation="C1")])["passed"])
        for claim, code in ((vc("6", role="produced", calculation="C1"),
                             "claim_role_unverified"),
                            (vc("6", role="planned", calculation="C1"), "claim_role_unverified"),
                            (vc("6", role="other_label", calculation="C1"),
                             "claim_role_mismatch"),
                            (vc("7", role="planned_not_produced", calculation="C1"),
                             "claim_value_mismatch"),
                            (vc("6", role="planned_not_produced", unit="kg",
                                calculation="C1"), "claim_unit_mismatch")):
            with self.subTest(claim=claim):
                self.assertEqual(codes(gate(f"{claim['value']}.", [ITEM], [claim])), [code])
        # A calculation that reads a row the item does not cite is not its evidence.
        self.assertEqual(codes(gate("417.", [ITEM], [vc(
            "417", role="ordered_not_produced_by_this_ao", calculation="C3")])),
            ["claim_uncited"])
        # A recorded result that does not recompute from the rows is not evidence.
        tampered = dict(C1, result="7")
        self.assertEqual(codes(gate("7.", [ITEM], [vc("7", role="planned_not_produced",
                                                      calculation="C1")],
                                    calculations=[tampered])), ["claim_uncited"])

    def test_a_calculation_role_is_recomputed_from_contracts_never_taken_as_declared(self):
        # Review of fb5ac6b: "planned - damaged as produced" laundered a planned figure.
        result = gate("1509 finished units.", [MO],
                      [vc("1509", role="produced", unit="pc", calculation="C4")])
        self.assertEqual(codes(result), ["claim_role_mismatch"])
        self.assertIn("a source role", result["failures"][0]["detail"])
        # A declared role on an uncontracted input grounds nothing.
        self.assertEqual(codes(gate("1 unit.", [MO], [vc("1", role="inspected",
                                                         calculation="C5")])),
                         ["claim_role_unverified"])
        # A pass-through takes the contract's role: size_pieces is planned, not produced.
        self.assertEqual(codes(gate("1509 units.", [MO], [vc("1509", role="produced",
                                                             calculation="C6")])),
                         ["claim_role_mismatch"])
        self.assertTrue(gate("1509 units planned.", [MO], [vc("1509", role="planned",
                                                              calculation="C6")])["passed"])
        # A derived name holds only when the algebra over the contracted roles yields it.
        self.assertTrue(gate("6 short.", [ITEM], [vc("6", role="planned_not_produced",
                                                     calculation="C1")])["passed"])

    def test_a_derived_name_never_chooses_its_quantity_type(self):
        # Review of 8855f22: "planned - damaged as produced_net" let "produced 1509" pass,
        # because the wording took the type from the caller's name. The type now comes
        # from the expression tree (planned - damaged is a planned-family figure by the
        # documented SUBTRACTIONS rule) and the name is a label that must fit it.
        for name, text, why in (
                ("produced_net", "AO-26005B produced 1509 good finished units.",
                 "the label 'produced_net' names produced but the value is planned - damaged"),
                ("produced_good", "AO-26005B produced 1509 good finished units.",
                 "names produced"),
                ("filled_capacity", "We filled 1509 units.", "names filled"),
                ("inspected_ok", "QC inspected 1509 units.", "names inspected"),
                ("net_produced", "AO-26005B produced 1509 units.",
                 "subtracts or compares produced")):
            with self.subTest(name=name):
                calc = derived_calc(name)
                result = gate(text, [MO], [vc("1509", role=name, unit="pc",
                                              calculation="C7")], calculations=[calc])
                self.assertFalse(result["passed"])
                self.assertIn("claim_role_mismatch", codes(result))
                [failure] = [item for item in result["failures"]
                             if item["code"] == "claim_role_mismatch"]
                self.assertIn(why, failure["detail"])
        # planned - damaged has no documented sub-quantity rule (review of d889cba): it is
        # untyped, so no type word may describe it under any label.
        for name in ("good_units", "planned_net"):
            with self.subTest(label=name):
                calc = derived_calc(name)
                claim = [vc("1509", role=name, unit="pc", calculation="C7")]
                for text in ("AO-26005B produced 1509 units.",
                             "1509 planned units net of damage.",
                             "AO-26005B was planned at 1509 units."):
                    self.assertEqual(codes(gate(text, [MO], claim, calculations=[calc])),
                                     ["claim_wording_role_unverified"])
                self.assertEqual(codes(gate("1509 units.", [MO], [
                    vc("1509", role="planned", unit="pc", calculation="C7")],
                    calculations=[calc])), ["claim_role_unverified"])
                self.assertTrue(gate("1509 units net of damage.", [MO], claim,
                                     calculations=[calc])["passed"])
        # manufactured - damaged is good produced units (Portal schema.prisma@878705b:2534):
        # named as one it is one, worded as one, claimable as its type or its alias ...
        calc = good_calc("manufactured_net")
        claim = [vc("1503", role="manufactured_net", unit="pc", calculation="C7")]
        result = gate("1503 good units produced.", [LINK, MO], claim, calculations=[calc])
        self.assertTrue(result["passed"], result["failures"])
        self.assertEqual(checked(result)[0]["quantity_type"], "manufactured")
        for role in ("manufactured", "produced"):
            self.assertTrue(gate("1503 good units produced.", [LINK, MO],
                                 [vc("1503", role=role, unit="pc", calculation="C7")],
                                 calculations=[calc])["passed"])
        # ... and never as planned, filled or inspected.
        for text in ("AO-26005B planned 1503 good finished units.", "We filled 1503 units.",
                     "QC inspected 1503 units.", "All 1,503 units passed QC."):
            with self.subTest(text=text):
                self.assertEqual(codes(gate(text, [LINK, MO], claim, calculations=[calc])),
                                 ["claim_wording_role_mismatch"])

    def test_differences_are_untyped_and_labels_follow_aliases_and_chains(self):
        # actual - filled: a difference of two types is untyped; its label may still say
        # produced_not_filled (produced accepts actual, filled is the subtracted type).
        calc = {"id": "C8", "expression": "produced / per_unit - filled", "result": "1",
                "unit": "EA", "relations": [{"left": "actual", "op": "-", "right": "filled",
                                             "as": "produced_not_filled"}],
                "inputs": {"produced": _input(ITEM, "row.actual_amount", "pc"),
                           "per_unit": _input(ITEM, "row.qty_per_batch", "pc/EA"),
                           "filled": _input(FILLING, "row.qty", "EA")}}
        claim = [vc("1", role="produced_not_filled", unit="EA", calculation="C8")]
        result = gate("Line clearance took 1 finished good.", [ITEM, FILLING], claim,
                      calculations=[calc])
        self.assertTrue(result["passed"], result["failures"])
        self.assertIsNone(checked(result)[0]["quantity_type"])
        for text in ("1 produced but not filled.", "1 filled.", "Filled, 1 unit."):
            with self.subTest(text=text):
                self.assertEqual(codes(gate(text, [ITEM, FILLING], claim, calculations=[calc])),
                                 ["claim_wording_role_unverified"])
        for label in ("filled_not_produced", "produced_not_ordered", "filled_total"):
            with self.subTest(label=label):
                wrong = dict(calc, relations=[{**calc["relations"][0], "as": label}])
                self.assertEqual(codes(gate("Took 1.", [ITEM, FILLING], [
                    vc("1", role=label, unit="EA", calculation="C8")], calculations=[wrong])),
                    ["claim_role_mismatch"])
        # A chain of documented subtractions stays typed: (manufactured - damaged) - damaged.
        chained = {"id": "C9", "expression": "m - d - e", "result": "1500", "unit": "pc",
                   "relations": [{"left": "manufactured", "op": "-", "right": "damaged",
                                  "as": "manufactured_net"},
                                 {"left": "manufactured_net", "op": "-", "right": "damaged",
                                  "as": "manufactured_net_less_damaged"}],
                   "inputs": {"m": _input(LINK, "row.qtymfg_c", "pc"),
                              "d": _input(MO, "row.qty_damaged", "pc"),
                              "e": _input(DAMAGE, "row.qty_damaged", "pc")}}
        cites = [LINK, MO, DAMAGE]
        claim = [vc("1500", role="manufactured_net_less_damaged", calculation="C9")]
        result = gate("1500 produced, net of damage.", cites, claim, calculations=[chained])
        self.assertTrue(result["passed"], result["failures"])
        self.assertEqual(checked(result)[0]["quantity_type"], "manufactured")
        self.assertEqual(codes(gate("planned 1500.", cites, claim, calculations=[chained])),
                         ["claim_wording_role_mismatch"])
        # The same cell subtracted twice is m - 2d: no longer a documented subtraction.
        twice = dict(chained, result="1503",
                     inputs={**chained["inputs"], "e": _input(MO, "row.qty_damaged", "pc")})
        result = gate("1503 produced, net.", [LINK, MO], [
            vc("1503", role="manufactured_net_less_damaged", calculation="C9")],
            calculations=[twice])
        self.assertEqual(codes(result), ["claim_wording_role_unverified"])
        # Untyped samples in the chain leave it untyped, however it is labelled.
        sampled = dict(chained, expression="m - d - s", result="1502",
                       relations=[chained["relations"][0], {
                           "left": "manufactured_net", "op": "-", "right": "inspected",
                           "as": "produced_after_samples"}],
                       inputs={**chained["inputs"], "s": _input(MO, "row.qty_sample", "pc",
                                                                "inspected")})
        sampled["inputs"].pop("e")
        result = gate("1502 units.", [LINK, MO], [vc("1502", role="produced_after_samples",
                                                      calculation="C9")],
                      calculations=[sampled])
        self.assertEqual(codes(result), ["claim_role_mismatch"])
        # A relation whose operands are not the inputs' contracted roles grounds nothing.
        loose = dict(derived_calc("planned_net"), relations=[
            {"left": "planned", "op": "-", "right": "damaged", "as": "planned_net"},
            {"left": "fudge", "op": "+", "right": "planned", "as": "fudge_total"}])
        result = gate("1509 planned.", [MO], [vc("1509", role="planned_net",
                                                 calculation="C7")], calculations=[loose])
        self.assertFalse(result["passed"])
        # (The failed claim keeps its ungrounded name, so the planned word also blocks.)
        self.assertEqual(codes(result), ["claim_role_mismatch", "claim_wording_role_mismatch"])
        self.assertIn("does not match its inputs' contracted roles",
                      result["failures"][0]["detail"])

    def test_value_claims_are_refused_at_write_when_malformed(self):
        for claim, code in (
                ({"value": 1509.0, "citation": ITEM, "field": "row.quantity"},
                 "value_claim_value_must_be_integer_or_string"),
                ({"value": "1509", "citation": LINE, "field": "row.qty"},
                 "value_claim_citation_not_cited"),
                ({"value": "1509", "citation": ITEM, "field": "quantity"},
                 "invalid_value_claim_field"),
                ({"value": "1509", "citation": ITEM, "field": "row.quantity", "role": "made"},
                 "invalid_value_claim_role"),
                ({"value": "6", "calculation": "C9"}, "value_claim_calculation_not_found"),
                ({"value": "6", "calculation": "C1", "citation": ITEM, "field": "row.x"},
                 "value_claim_needs_citation_or_calculation"),
                ({"value": "6"}, "value_claim_needs_citation_or_calculation"),
                ({"value": "6", "calculation": "C1", "extra": 1}, "invalid_value_claim")):
            with self.subTest(code=code), self.assertRaisesRegex(ValidationError, code):
                claim_checks.parse_value_claims([claim], [ITEM], ["C1"])


def calc_of(cid, expression, relations, inputs, result, unit="pc"):
    return {"id": cid, "expression": expression, "result": result, "unit": unit,
            "relations": relations, "inputs": inputs}


P_ITEM = _input(ITEM, "row.quantity", "pc")
A_ITEM = _input(ITEM, "row.actual_amount", "pc")
M_LINK = _input(LINK, "row.qtymfg_c", "pc")
P_MO = _input(MO, "row.quantity", "pc")
D_MO = _input(MO, "row.qty_damaged", "pc")


class ReviewProbeRegressions(unittest.TestCase):
    """Review of 69467b8 (probes p1.py, p4.py) and of 8855f22 on the pure gate: a sum, a
    commuted operand or an identity never relabels a figure; the type is the form's."""

    def probe(self, expression, relations, inputs, result, role, text, handles=(ITEM, MO, LINK),
              outcome=None):
        calc = calc_of("C7", expression, relations, inputs, result)
        return gate(text, list(handles), [vc(result, role=role, unit="pc", calculation="C7")],
                    calculations=CALCS + [calc], outcome=outcome)

    def test_round_five_sum_and_identity_relabels_fail(self):
        short = {"left": "planned", "op": "-", "right": "actual", "as": "planned_short"}
        total = {"left": "actual", "op": "+", "right": "planned", "as": "produced_total"}
        cases = (
            ("m + (p - m)", [{"left": "planned", "op": "-", "right": "manufactured",
                              "as": "planned_unmade"},
                             {"left": "manufactured", "op": "+", "right": "planned_unmade",
                              "as": "produced_total"}], {"m": M_LINK, "p": P_MO}, "1509",
             "produced_total", "the label 'produced_total' names produced but the value is "
                               "planned"),
            ("a + (p - a)", [short, {"left": "actual", "op": "+", "right": "planned_short",
                                     "as": "produced_total"}], {"a": A_ITEM, "p": P_ITEM},
             "1509", "produced_total", "the value is planned"),
            ("p + a - a", [total, {"left": "produced_total", "op": "-", "right": "actual",
                                   "as": "produced_x"}], {"a": A_ITEM, "p": P_ITEM}, "1509",
             "produced_x", "names produced"),
            ("p + a * 0", [total], {"a": A_ITEM, "p": P_ITEM}, "1509", "produced_total",
             "the value is planned"),
            ("p + d", [{"left": "damaged", "op": "+", "right": "planned",
                        "as": "damaged_total"}], {"p": P_MO, "d": D_MO}, "1509",
             "damaged_total", "the value is damaged + planned"),
            ("p - (p - a)", [short, {"left": "planned", "op": "-", "right": "planned_short",
                                     "as": "planned_x"}], {"p": P_ITEM, "a": A_ITEM}, "1503",
             "planned_x", "the value is actual"),
            ("p / a * a", [], {"a": A_ITEM, "p": P_ITEM}, "1509", "planned",
             "cancelled_into_other_role"))
        texts = ("AO-26005B produced 1509 finished goods.", "1509 damaged units.",
                 "1503 units were planned.", "AO-26005B produced 1503 units.")
        for expression, relations, inputs, result, role, why in cases:
            for text in texts:
                if result not in text:
                    continue
                with self.subTest(expression=expression, text=text):
                    outcome = f"Closed: {text}"
                    out = self.probe(expression, relations, inputs, result, role, text,
                                     outcome=outcome)
                    self.assertFalse(out["passed"])
                    [failure] = [item for item in out["failures"]
                                 if item["code"] == "claim_role_mismatch"]
                    self.assertIn(why, failure["detail"])
                    self.assertIn(("outcome", "claim_wording_role_mismatch"),
                                  {(item["item"], item["code"]) for item in out["failures"]})

    def test_one_cell_under_two_spellings_is_one_variable(self):
        from operating import quantity_types as qt
        row, key = qt.cell(ITEM, "row.quantity"), qt.cell(ITEM, "key.quantity")
        self.assertEqual(row, key)
        self.assertIsNone(qt.type_calculation("p + q", {"p": (row, "planned"),
                                                        "q": (key, "planned")})["type"])
        self.assertEqual(qt.type_calculation("p + q", {
            "p": (row, "planned"), "q": (qt.cell(MO, "row.quantity"), "planned")})["type"],
            "planned")

    def test_one_row_read_in_two_captures_is_one_cell(self):
        # Review of d889cba (rvct_p.py): a handle's '@generation-capture' tail named a new
        # variable, so p + q over one planned cell read twice passed as 3018 planned.
        from operating import quantity_types as qt
        again = ITEM.replace("@g-8", "@g-9")
        self.assertEqual(qt.cell(ITEM, "row.quantity"), qt.cell(again, "row.quantity"))
        self.assertNotEqual(qt.cell(ITEM, "row.quantity"), qt.cell(MO, "row.quantity"))
        rows = dict(ROWS, **{again: ROWS[ITEM]})
        calc = calc_of("C7", "p + q", [], {"p": P_ITEM, "q": _input(again, "row.quantity",
                                                                     "pc")}, "3018")
        out = gate("3018 units.", [ITEM, again], [vc("3018", role="planned", unit="pc",
                                                      calculation="C7")],
                   rows=rows, calculations=[calc])
        self.assertEqual(codes(out), ["claim_role_unverified"])
        out = gate("3018 planned.", [ITEM, again], [vc("3018", unit="pc", calculation="C7")],
                   rows=rows, calculations=[calc])
        self.assertEqual(codes(out), ["claim_wording_role_unverified"])

    def test_one_row_read_at_two_values_never_cancels(self):
        # Review of 6948548 (rvct2/cancel.py): with the capture tail dropped, m + p - q over
        # one planned cell read at 1509 (capture 9) and 1600 (capture 12) cancelled to m
        # and 1412 passed as manufactured. Two reads of one cell merge only at one value.
        from operating import quantity_types as qt
        again = ITEM.replace("@g-8", "@g-12")
        changed = {"relation": "silver.assembly_order_items",
                   "row": {**ROWS[ITEM]["row"], "quantity": 1600.0}}
        rows = dict(ROWS, **{again: changed})
        p = {**P_ITEM, "value": "1509"}
        q = {**_input(again, "row.quantity", "pc"), "value": "1600"}
        m = {**M_LINK, "value": "1503"}
        relations = [{"left": "manufactured", "op": "+", "right": "planned", "as": "sum_x"},
                     {"left": "sum_x", "op": "-", "right": "planned", "as": "net_x"}]
        calc = calc_of("C7", "m + p - q", relations, {"m": m, "p": p, "q": q}, "1412")
        for role, text in (("manufactured", "AO-26005B produced 1412 units."),
                           ("net_x", "AO-26005B produced 1412 units.")):
            with self.subTest(role=role):
                out = gate(text, [LINK, ITEM, again], [vc("1412", role=role, unit="pc",
                                                           calculation="C7")],
                           rows=rows, calculations=[calc])
                self.assertFalse(out["passed"])
                self.assertIn("claim_role_mismatch", codes(out))
                self.assertIn("one_cell_two_values", out["failures"][0]["detail"])
        cells = {"m": (qt.cell(LINK, "row.qtymfg_c"), "manufactured", "1503"),
                 "p": (qt.cell(ITEM, "row.quantity"), "planned", "1509"),
                 "q": (qt.cell(again, "row.quantity"), "planned", "1600")}
        result = qt.type_calculation("m + p - q", cells)
        self.assertIsNone(result["type"])
        # The same value read twice is one cell (1509.0 and 1509 are one number).
        same = dict(cells, q=(qt.cell(again, "row.quantity"), "planned", "1509.0"))
        self.assertEqual(qt.type_calculation("m + p - q", same)["type"], "manufactured")

    def test_two_reads_compare_the_re_read_values_not_the_recorded_ones(self):
        # Review of 5548698 (rvct2/cancel.py): inputs without a recorded value merged.
        again = ITEM.replace("@g-8", "@g-12")
        changed = {"relation": "silver.assembly_order_items",
                   "row": {**ROWS[ITEM]["row"], "quantity": 1600.0}}
        relations = [{"left": "manufactured", "op": "+", "right": "planned", "as": "sum_x"},
                     {"left": "sum_x", "op": "-", "right": "planned", "as": "net_x"}]
        calc = calc_of("C7", "m + p - q", relations,
                       {"m": M_LINK, "p": P_ITEM, "q": _input(again, "row.quantity", "pc")},
                       "1412")
        out = gate("AO-26005B produced 1412 units.", [LINK, ITEM, again],
                   [vc("1412", role="manufactured", unit="pc", calculation="C7")],
                   rows=dict(ROWS, **{again: changed}), calculations=[calc])
        self.assertIn("claim_role_mismatch", codes(out))
        self.assertIn("one_cell_two_values", out["failures"][0]["detail"])

    def test_nested_division_by_a_sum_types_fast_and_fails_closed(self):
        # Review of d889cba (rvct_dos.py): opaque keys built from repr strings doubled per
        # nesting level; 'x/(y+x/(y+...))' at 24 levels took 12.6 s. Opaque factors are
        # structural now and nest at most MAX_OPAQUE_DEPTH deep (deeper: untyped).
        import time
        from operating import quantity_types as qt

        def nested(depth, leaf="x"):
            return "x/(y+" * depth + leaf + ")" * depth
        inputs = {"x": (("c", "x"), "planned"), "y": (("c", "y"), None)}
        deepest = nested(83)
        self.assertLessEqual(len(deepest), 500)
        started = time.monotonic()
        for depth in range(1, 84):
            result = qt.type_calculation(nested(depth), inputs, label="planned_ratio")
            self.assertIsNone(result["type"])
            if depth > qt.MAX_OPAQUE_DEPTH:
                self.assertIsNone(result["form"])
                self.assertIn("cannot be checked", result["problem"])
        self.assertLess(time.monotonic() - started, 1.0)
        # Division by a sum stays opaque: sound, not complete (this is x, left untyped).
        self.assertIsNone(qt.type_calculation("x / (y + x) * (y + x)", inputs)["type"])
        # Through the gate: a claim on such a calculation is checked at once (49 levels is
        # the deepest the evaluator accepts; deeper is expression_too_complex).
        deepest = nested(49)
        line_id = _input(LINE, "row.id", "1")
        line_oe = _input(LINE, "row.idoe", "1")
        from operating.cases import evaluate
        values = {"x": (claim_checks.to_decimal(1140), ()), "y": (claim_checks.to_decimal(351), ())}
        value = format(evaluate(deepest, values)[0], "f")
        calc = calc_of("C7", deepest, [], {"x": line_id, "y": line_oe}, value, unit="1")
        started = time.monotonic()
        out = gate("A ratio is recorded.", [LINE], [vc(value, calculation="C7")],
                   calculations=[calc])
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(out["passed"], out["failures"])
        # With a contracted role beside it the guard passes and the algebra runs too.
        values["p"] = (claim_checks.to_decimal(1509), (("pc", 1),))
        value = format(evaluate(f"p * ({deepest})", values)[0], "f")
        calc = calc_of("C7", f"p * ({deepest})", [], {"p": P_ITEM, "x": line_id, "y": line_oe},
                       value)
        started = time.monotonic()
        out = gate("A scaled ratio is recorded.", [LINE, ITEM], [vc(value, calculation="C7")],
                   calculations=[calc])
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(out["passed"], out["failures"])
        self.assertEqual(codes(gate("A planned ratio.", [LINE, ITEM], [
            vc(value, role="planned", calculation="C7")], calculations=[calc])),
            ["claim_role_unverified"])

    def test_a_literal_offset_is_untyped(self):
        # 'a + 6' (p1.py): a bare 6 has no unit, so the expression does not even recompute
        # in pc; a literal 6 pc is an unroled quantity the combination guard refuses; and
        # the type algebra leaves any constant term untyped.
        from operating import quantity_types as qt
        out = self.probe("a + 6", [], {"a": A_ITEM}, "1509", "actual",
                         "AO-26005B produced 1509 units.")
        self.assertIn("claim_uncited", codes(out))
        six = {"kind": "literal", "value": "6", "unit": "pc"}
        out = self.probe("a + k", [], {"a": A_ITEM, "k": six}, "1509", "actual",
                         "AO-26005B produced 1509 units.")
        self.assertFalse(out["passed"])
        self.assertIn("claim_role_mismatch", codes(out))
        self.assertIsNone(qt.type_calculation("a + k", {"a": ("a", "actual"), "k": ("k", None)},
                                              constants={"k": "6"})["type"])
        self.assertIsNone(qt.type_calculation("a + 6", {"a": ("a", "actual")})["type"])

    def test_identities_type_the_figure_they_equal_and_name_nothing_else(self):
        # With labels that name no type the identities compute; each is the figure it
        # equals, so the truthful claim passes and the relabel fails.
        for expression, relations, inputs, result, truth in (
                ("m + (p - m)", [{"left": "planned", "op": "-", "right": "manufactured",
                                  "as": "unmade"},
                                 {"left": "manufactured", "op": "+", "right": "unmade",
                                  "as": "total"}], {"m": M_LINK, "p": P_MO}, "1509", "planned"),
                ("p - (p - a)", [{"left": "planned", "op": "-", "right": "actual",
                                  "as": "short"},
                                 {"left": "planned", "op": "-", "right": "short",
                                  "as": "rest"}], {"p": P_ITEM, "a": A_ITEM}, "1503", "actual"),
                ("p + a * 0", [{"left": "actual", "op": "+", "right": "planned",
                                "as": "total"}], {"a": A_ITEM, "p": P_ITEM}, "1509",
                 "planned")):
            with self.subTest(expression=expression):
                good = self.probe(expression, relations, inputs, result, truth,
                                  f"{result} {truth}.")
                self.assertTrue(good["passed"], good["failures"])
                for role in QUANTITY_TYPES_NOT_PLANNED + ("planned",):
                    if claim_checks._qt.accepts(role, truth):
                        continue
                    bad = self.probe(expression, relations, inputs, result, role,
                                     f"{result} units.")
                    self.assertEqual(codes(bad), ["claim_role_mismatch"], role)
                label = self.probe(expression, relations, inputs, result,
                                   relations[-1]["as"], f"AO-26005B produced {result}.")
                if truth == "planned":
                    self.assertEqual(codes(label), ["claim_wording_role_mismatch"])

    def test_round_four_outcome_and_wording_probes_still_fail(self):
        # p4.py: every outcome phrasing over an honest planned claim fails, now including
        # 'passed QC' (an inspected-family word).
        claims = [vc("1509", ITEM, "row.quantity", "planned")]
        for outcome in ("Closed. Production output: 1509.", "Closed; we made 1509 units.",
                        "Closed. 1509 units shipped to the customer.", "Closed. 1509 manufactured.",
                        "Closed. All 1,509 units passed QC.", "Closed. QC passed 1509 units.",
                        "Closed. 1509 passed inspection.", "Closed. 1509 were produced"):
            with self.subTest(outcome=outcome):
                result = gate("Planned 1509.", [ITEM], claims, outcome=outcome)
                self.assertEqual([(item["item"], item["code"]) for item in result["failures"]],
                                 [("outcome", "claim_wording_role_mismatch")])
        # 'passed' alone is no type word.
        self.assertTrue(gate("Planned 1509; the order passed to planning.", [ITEM],
                             claims)["passed"])


class TypeAlgebraProperties(unittest.TestCase):
    """Property: over random expressions, the type algebra never relabels a figure. An
    independent exact oracle (Fraction evaluation, coefficients by unit steps) decides
    what each expression really is; a typed answer must be exactly such a form."""

    TYPES = {"p": "planned", "q": "planned", "a": "actual", "m": "manufactured",
             "d": "damaged", "e": "damaged", "f": "filled", "x": None, "y": None}

    @staticmethod
    def expression(rng, depth):
        if depth == 0 or rng.random() < 0.3:
            return rng.choice(["p", "q", "a", "m", "d", "e", "f", "x", "y", "0", "1", "2"])
        op = rng.choice("++--*/")
        return f"({TypeAlgebraProperties.expression(rng, depth - 1)} {op} " \
               f"{TypeAlgebraProperties.expression(rng, depth - 1)})"

    @staticmethod
    def value(expression, values):
        import ast
        from fractions import Fraction

        def walk(node):
            if isinstance(node, ast.Expression):
                return walk(node.body)
            if isinstance(node, ast.Constant):
                return Fraction(node.value)
            if isinstance(node, ast.Name):
                return values[node.id]
            if isinstance(node, ast.UnaryOp):
                return -walk(node.operand)
            left, right = walk(node.left), walk(node.right)
            return {ast.Add: left + right, ast.Sub: left - right,
                    ast.Mult: left * right}.get(type(node.op)) if not isinstance(
                        node.op, ast.Div) else left / right
        return walk(ast.parse(expression, mode="eval"))

    def oracle(self, expression, rng):
        """The type the expression really is (None when it is no such form), or
        'skip' when it divides by zero at the sample points."""
        from fractions import Fraction
        names = list(self.TYPES)
        try:
            base = {name: Fraction(rng.randint(3, 97)) for name in names}
            at = self.value(expression, base)
            coefficients = {}
            for name in names:
                coefficients[name] = self.value(expression, {**base, name: base[name] + 1}) - at
            for _ in range(4):
                point = {name: Fraction(rng.randint(3, 97)) for name in names}
                if self.value(expression, point) != sum(coefficients[name] * point[name]
                                                        for name in names):
                    return None  # not linear without a constant
        except ZeroDivisionError:
            return "skip"
        positive = {self.TYPES[name] for name, coef in coefficients.items() if coef == 1}
        negative = {self.TYPES[name] for name, coef in coefficients.items() if coef == -1}
        if any(coef not in (-1, 0, 1) for coef in coefficients.values()) or None in (
                positive | negative) or len(positive) != 1:
            return None
        [kind] = positive
        return kind if all((kind, other) in claim_checks._qt.SUBTRACTIONS
                           for other in negative) else None

    def test_random_expressions_are_never_relabelled(self):
        import random
        from operating import quantity_types as qt
        rng = random.Random(20261001)
        typed = untyped = 0
        for _ in range(3000):
            expression = self.expression(rng, rng.randint(1, 4))
            truth = self.oracle(expression, rng)
            if truth == "skip":
                continue
            names = [name for name in self.TYPES if name in expression]
            answer = qt.type_calculation(expression, {
                name: (("cell", name), self.TYPES[name]) for name in names})["type"]
            with self.subTest(expression=expression):
                if answer is not None:
                    self.assertEqual(answer, truth)  # soundness: never another type
                elif "/" not in expression:
                    self.assertIsNone(truth)  # exact without division: complete too
            typed += answer is not None
            untyped += answer is None
        self.assertGreater(typed, 100)
        self.assertGreater(untyped, 100)

    def test_random_labels_never_name_another_type_than_the_form(self):
        import random
        from operating import quantity_types as qt
        rng = random.Random(7)
        for _ in range(1500):
            expression = self.expression(rng, rng.randint(1, 3))
            names = [name for name in self.TYPES if name in expression]
            inputs = {name: (("cell", name), self.TYPES[name]) for name in names}
            result = qt.type_calculation(expression, inputs)
            if result["type"] is None:
                continue
            for word in sorted(qt.QUANTITY_TYPES):
                with self.subTest(expression=expression, word=word):
                    problem = qt.type_calculation(expression, inputs,
                                                  label=f"{word}_total")["problem"]
                    self.assertEqual(problem is None, qt.accepts(word, result["type"]))

    def test_random_claims_on_the_gate_pass_only_as_what_the_value_is(self):
        import random
        from operating.cases import evaluate, parse_unit
        rng = random.Random(5447)
        cells = {"p": P_ITEM, "q": P_MO, "a": A_ITEM, "m": M_LINK, "d": D_MO,
                 "e": _input(DAMAGE, "row.qty_damaged", "pc"),
                 "f": _input(FILLING, "row.qty", "pc"), "x": _input(CLEARANCE, "row.qty", "pc"),
                 "y": _input(MO, "row.qty_sample", "pc")}
        numbers = {"p": "1509", "q": "1509", "a": "1503", "m": "1503", "d": "0", "e": "3",
                   "f": "1502", "x": "1", "y": "1"}
        handles = [ITEM, MO, LINK, DAMAGE, FILLING, CLEARANCE]
        passed = 0
        for _ in range(600):
            expression = self.expression(rng, rng.randint(1, 3))
            names = [name for name in cells if name in expression]
            try:
                result, _unit, _inexact, _used = evaluate(expression, {
                    name: (claim_checks.to_decimal(numbers[name]), parse_unit("pc"))
                    for name in names})
            except Exception:  # noqa: BLE001 - unit or zero-division refusals are not probes
                continue
            text = format(result.normalize(), "f")
            if not names or len(text) > 60:
                continue
            truth = self.oracle(expression, rng)
            calc = calc_of("C7", expression, [], {name: cells[name] for name in names}, text)
            for role in (None, *sorted(claim_checks.QUANTITY_ROLES)):
                out = gate(f"Value {text}.", handles,
                           [vc(text, role=role, calculation="C7")], calculations=[calc])
                with self.subTest(expression=expression, role=role):
                    if out["passed"]:
                        passed += 1
                        if role is None:
                            self.assertIn(truth, (None, "skip"))
                        else:
                            self.assertTrue(claim_checks._qt.accepts(role, truth), truth)
                    for word in ("produced", "planned", "filled", "damaged"):
                        worded = gate(f"{word} {text}.", handles,
                                      [vc(text, role=role, calculation="C7")],
                                      calculations=[calc])
                        if worded["passed"]:
                            self.assertTrue(claim_checks._qt.accepts(
                                claim_checks._TYPE_WORDS[word], truth), (word, truth))
        self.assertGreater(passed, 50)


# The reviewer's probe phrasings (scratchpad probe.py on 7604d54): every one carries a
# number, and with no structured claim every one must fail closed.
PROBES = (
    "produced 1,509", "Produced: 1,509.", "Produced (1,509).", "Produced - 1509",
    "Produced = 1509", "Produced quantity was 1509", "The quantity produced was 1509 units",
    "The quantity produced on the line was 1509", "1509 units were produced",
    "1509 jugs were produced on line 01", "1509 finished gallon jugs were produced",
    "A total of 1509 finished gallon jugs were produced", "Produced 1509.0",
    "Produced 1,509.00 EA", "Produced 1509 EA", "produced 1509 pcs", "Output: 1509",
    "Output was 1509 units", "made 1509", "completed 1509", "Production of 1509",
    "Production was 1509", "The line produced all 1509.", "Produced, 1509", "Filled 1509",
    "Fill count 1509", "fill qty 1509", "Inspection passed 1509",
    "QA inspected all 1509 units", "of the 1920 ordered, 1509 were produced",
    "ordered 1920, of which 1509 produced", "1509 produced and 1503 filled",
    "Produced 1509 (planned 1509)", "Produced 1509 vs planned 1509",
    "Not planned: 1509 produced", "Planned and produced 1509", "Produced and planned 1509",
    "produced 100%", "produced 1509-1510", "produced between 1509 and 1510",
    "produced about 1.5k", "produced one thousand five hundred nine",
    "- produced 1509\n- planned 1503", "| produced | 1509 |", "produced\n1509",
    "PRODUCED 1509", "Produced: 1509 units\nFilled: 1502", "AO-26005B 1509 produced",
    "produced 1509 (AO-26005B)", "produced 1509 jug", "producing 1509", "production 1509",
    "manufacture of 1509", "yield 1509", "1503 produced", "1 produced", "filled 1503",
    "planned 1503", "damaged 0", "filled 1502 and produced 1503",
    "filled 1502 vs produced 1503", "filled 1502, less than produced 1503",
    "filled 1502 is less than the 1503 produced", "1509 planned exceeds 1503 produced",
    "1509 planned, 1503 produced, a gap of 6", "the shortfall vs plan was 6",
    "produced 1509 kg", "produced 1509 L", "produced $1509", "produced 1,509 cases",
    "produced 378 CS", "Filled (1509) on 2026-01-07.",
    "The quantity produced on line 01 was 1509.", "Planned 1509; produced 1503; filled 1502.",
    "Produced: one thousand five hundred and nine units", "Produced: twelve")


class ProseTests(unittest.TestCase):
    def test_every_probe_phrasing_without_a_claim_fails_closed(self):
        for text in PROBES:
            with self.subTest(text=text):
                result = gate(text, [ITEM, MO, FILLING, LINE])
                self.assertFalse(result["passed"])
                self.assertEqual(set(codes(result)), {"claim_unstructured"})

    def test_a_number_is_covered_only_by_a_claim_of_the_same_value(self):
        claims = [vc("1509", ITEM, "row.quantity", "planned")]
        self.assertTrue(gate("Planned: 1,509 finished goods.", [ITEM], claims)["passed"])
        self.assertTrue(gate("planned 1509.00", [ITEM], claims)["passed"])
        result = gate("Planned 1509; produced 1503.", [ITEM], claims)
        self.assertEqual([(item["code"], item["value"]) for item in result["failures"]],
                         [("claim_unstructured", "1503")])
        # Numbers in words count from ten up and are compared by value.
        self.assertTrue(gate("planned fifteen hundred nine", [ITEM], claims)["passed"])
        self.assertEqual(codes(gate("planned one thousand", [ITEM], claims)),
                         ["claim_unstructured"])

    def test_a_number_keeps_its_written_scale(self):
        # Review p0-b: "%", "k" and "M" were dropped, so "0.25%" passed over a cell holding
        # the fraction 0.25 (25 %). Percent contract (Dot and plx-reviewer-p0b-1: "2500%"
        # passed over a claim of 25 percent, 25 USD, 25 kg or a bare 25): a number written
        # as a percent is covered only by a claim with a percent unit, at percent scale.
        formula = "api://silver.formula_items/%7B%22id%22%3A7%7D@g-3"
        rows = {formula: {"relation": "silver.formula_items",
                          "row": {"id": 7, "percentage": "0.25", "batch_qty": "10000",
                                  "margin": "25"}}}

        def claim(value, unit=None, field="row.margin"):
            return [vc(value, formula, field, **({"unit": unit} if unit else {}))]

        percent, fraction = claim("25", "percent"), claim("0.25", field="row.percentage")
        for text in ("A 25% margin.", "A 25 percent margin.", "A 25 pct margin.",
                     "A 25.0% margin.", "A twenty-five percent margin.", "A -25% margin."):
            with self.subTest(honest=text):
                self.assertTrue(gate(text, [formula], percent, rows=rows,
                                     calculations=[])["passed"])
        for text, claims in (("A 2500% margin.", percent), ("A 0.25% margin.", percent),
                             ("A 2500 percent margin.", percent), ("A margin of 25.", percent),
                             ("A margin of 25 USD.", percent),
                             ("A 2500% margin.", claim("25", "USD")),
                             ("A 2500% margin.", claim("25", "kg")),
                             ("A 2500% margin.", claim("25")), ("A 25% margin.", claim("25")),
                             ("A 25% margin.", claim("25", "USD")),
                             ("A twenty-five percent margin.", claim("25")),
                             ("Glycerin is 25% of the formula.", fraction),
                             ("Glycerin is 0.25% of the formula.", fraction)):
            with self.subTest(dishonest=text, unit=claims[0].get("unit")):
                self.assertEqual(codes(gate(text, [formula], claims, rows=rows,
                                            calculations=[])), ["claim_unstructured"])
        for claims in (percent, claim("25", "USD"), claim("25")):
            closed = gate("A margin of 25.", [formula], claims, rows=rows, calculations=[],
                          outcome="Closed: a 2500% margin.")
            self.assertEqual([(f["code"], f["value"]) for f in closed["failures"]
                              if f["item"] == "outcome"],
                             [("claim_unstructured", "2500%")])
        batch = claim("10000", field="row.batch_qty")
        self.assertTrue(gate("A 10k batch.", [formula], batch, rows=rows,
                             calculations=[])["passed"])
        result = gate("A 10M batch.", [formula], batch, rows=rows, calculations=[])
        self.assertEqual(codes(result), ["claim_unstructured"])

    def test_bullets_tables_and_separators_are_covered_by_their_claims(self):
        claims = [vc("1503", ITEM, "row.actual_amount", "produced"),
                  vc("1502", FILLING, "row.qty", "filled")]
        for text in ("Produced: 1503 units\nFilled: 1502", "- produced 1503\n- filled 1502",
                     "| produced | 1503 |\n| filled | 1502 |", "Produced (1503), filled (1502)",
                     "Produced = 1503; Filled = 1502"):
            with self.subTest(text=text):
                result = gate(text, [ITEM, FILLING], claims)
                self.assertTrue(result["passed"], result["failures"])
                self.assertEqual(result["leads"], 0)

    def test_prose_that_types_a_number_against_its_claim_blocks(self):
        # Review of fb5ac6b: an honest claim (planned) under dishonest prose (produced)
        # passed. A quantity-type word bound to a number now has to agree with its claim.
        honest = [vc("1509", ITEM, "row.quantity", "planned"),
                  vc("1502", FILLING, "row.qty", "filled")]
        for text in ("AO-26005B produced 1509 finished goods.", "Produced: 1,509.",
                     "Produced (1509).", "Produced = 1509", "| produced | 1509 |",
                     "1509 units were produced", "Produced: 1509 units\nFilled: 1502",
                     "filled 1509 units", "QA inspected all 1509", "production 1509",
                     "- produced 1509\n- filled 1502"):
            with self.subTest(text=text):
                result = gate(text, [ITEM, FILLING], honest)
                self.assertFalse(result["passed"])
                self.assertEqual(codes(result), ["claim_wording_role_mismatch"])
        # An untyped claim under typed prose is not a verified quantity type either.
        result = gate("The MO produced 1.", [MO], [vc("1", MO, "row.qty_sample")])
        self.assertEqual(codes(result), ["claim_wording_role_unverified"])
        # Next to an identifier the word is only an advisory lead (order line 1140 was
        # produced by other AOs).
        result = gate("Whether order line 1140 was produced by other assembly orders.",
                      [LINE], [vc("1140", LINE, "row.id", "identifier")])
        self.assertTrue(result["passed"])
        self.assertEqual(result["leads"], 1)
        # A negated word asserts no type.
        self.assertTrue(gate("1509 units, not produced yet.", [ITEM], honest[:1])["passed"])

    def test_correct_prose_binds_each_word_to_its_own_number(self):
        # The reviewer measured two false fails among seven correct phrasings when a
        # trailing word bound forward; clause separators keep each word with its number.
        claims = [vc("1509", ITEM, "row.quantity", "planned"),
                  vc("1503", ITEM, "row.actual_amount", "actual"),
                  vc("1502", FILLING, "row.qty", "filled")]
        for text in ("1509 planned, 1503 produced, 1502 filled.",
                     "Of the 1509 planned, 1503 were produced.",
                     "Planned 1509 and produced 1503; filled 1502.",
                     "1509 planned and 1503 produced.",
                     "Produced: 1503\nPlanned: 1509\nFilled: 1502",
                     "1503 were produced against 1509 planned.",
                     "Produced (1503), short of the 1509 planned.",
                     "1509 planned exceeds 1503 produced.",
                     "Filled 1502 versus produced 1503."):
            with self.subTest(text=text):
                result = gate(text, [ITEM, FILLING], claims)
                self.assertTrue(result["passed"], result["failures"])

    def test_an_untyped_calculation_is_cited_as_a_number_never_typed_by_a_word(self):
        # planned - actual is untyped by the type algebra: its label types no word, and a
        # type word bound to it, or unbound in its sentence, blocks.
        claim = [vc("6", role="planned_not_produced", unit="EA", calculation="C1")]
        for text in ("6 planned but not produced.", "AO-26005B produced 6.",
                     "Planned, 6 short.", "6 fewer than planned."):
            with self.subTest(text=text):
                self.assertEqual(codes(gate(text, [ITEM], claim)),
                                 ["claim_wording_role_unverified"])
        both = claim + [vc("1509", ITEM, "row.quantity", "planned"),
                        vc("1503", ITEM, "row.actual_amount", "actual")]
        self.assertTrue(gate("Planned 1509, produced 1503: a shortfall of 6.", [ITEM],
                             both)["passed"])
        # Called directly, a role name outside ROLES is ungrounded and agrees with no word.
        failures, _leads = claim_checks.wording_check(
            "produced 1509", [{"value": "1509", "kind": "number", "role": "produced_net"}])
        self.assertEqual([item["code"] for item in failures], ["claim_wording_role_mismatch"])

    def test_an_unbound_type_word_is_checked_against_its_sentence(self):
        # Review of 8855f22 (minor): comma and window phrasings escaped the binder.
        honest = [vc("1509", ITEM, "row.quantity", "planned")]
        for text in ("Produced, 1509 finished goods.",
                     "AO-26005B was produced in full, 1509 finished goods.",
                     "Production complete, 1509 units.", "Filled; 1509 units.",
                     "Inspected and released 1509 units.", "Inspected and passed: 1509 units.",
                     "The line produced the full batch of 1509 units.",
                     "1509 finished gallon jugs were produced.",
                     "Production ran to completion with 1509 units.",
                     "Finished goods produced\n1509"):
            with self.subTest(text=text):
                result = gate(text, [ITEM], honest)
                self.assertFalse(result["passed"])
                self.assertEqual(codes(result), ["claim_wording_role_mismatch"])
        # A sentence end separates: the produced word is not about the planned figure.
        self.assertTrue(gate("Planned 1509. Production has not reported yet.", [ITEM],
                             honest)["passed"])
        # Numbers already described by their own words do not answer for another word.
        claims = honest + [vc("1503", ITEM, "row.actual_amount", "actual"),
                           vc("1502", FILLING, "row.qty", "filled")]
        for text in ("Planned 1509; production then made 1503 and filled 1502.",
                     "Of 1509 planned, the line made 1503, and filling took 1502.",
                     "The production record: planned 1509, made 1503, filled 1502."):
            with self.subTest(text=text):
                result = gate(text, [ITEM, FILLING], claims)
                self.assertTrue(result["passed"], result["failures"])
        # Beside only untyped or identifier numbers it is an advisory lead, not a block.
        result = gate("Production used line clearance row(s), 1 unit.", [CLEARANCE],
                      [vc("1", CLEARANCE, "row.qty")])
        self.assertTrue(result["passed"], result["failures"])
        self.assertEqual(result["leads"], 1)

    def test_an_identifier_claim_silences_a_word_only_on_an_id_column(self):
        # qty_sample is a count: calling its value an identifier does not make
        # "produced 1" a lead.
        result = gate("The MO produced 1.", [MO], [vc("1", MO, "row.qty_sample", "identifier")])
        self.assertEqual(codes(result), ["claim_wording_role_unverified"])
        result = gate("Order line 1140 was produced.", [LINE],
                      [vc("1140", LINE, "row.id", "identifier")])
        self.assertTrue(result["passed"])
        self.assertEqual(result["leads"], 1)

    def test_identifiers_dates_times_and_handles_are_not_numbers(self):
        text = (f"AO-26005B for OR1000151885 (customer PO PO12172025) was filled on "
                f"2026-01-07 from 17:25:00, i.e. Jan 7, 2026 or 7 January; see {ITEM}.")
        # No number: only the ISO date, which needs a date claim (TextValueClaimTests).
        self.assertEqual([(f["code"], f["value"]) for f in gate(text, [ITEM])["failures"]],
                         [("claim_unstructured", "2026-01-07")])
        self.assertTrue(gate(text.replace("2026-01-07", "Jan 7"), [ITEM])["passed"])
        self.assertTrue(gate("Its SHA-256 equals the recorded hash (ISO-8601, UTF-8).",
                             [ITEM])["passed"])
        self.assertEqual(codes(gate("SHA-256 says 1509.", [ITEM])), ["claim_unstructured"])

    def test_glued_and_hyphenated_digits_are_numbers_unless_the_prefix_is_a_known_id(self):
        for text in ("produced x1509", "produced SKU-1509", "produced v1509",
                     "produced P-1509"):
            with self.subTest(text=text):
                self.assertEqual(codes(gate(text, [ITEM])), ["claim_unstructured"])
        for text in ("see F2 and G1", "AO-77 and SO-12", "SO-2026-0003", "UTF-8"):
            with self.subTest(text=text):
                self.assertTrue(gate(text, [ITEM])["passed"])

    def test_the_answer_text_reads_a_hyphenated_or_grammar_label_as_no_figure(self):
        """GP-05 and review of af134706d2 (supervisor decision 2026-10-29). In the answer
        text (the close outcome) a hyphenated label after a prefix that is no quantity,
        unit, term or currency word, or a whole token of an identifier kind's shape,
        states no figure. A bare letters+digits token and a hyphenated quantity, unit,
        term or currency token stay figures: each refuses when the record says another
        number (ITEM's row says 1509). A claim's own text keeps every code a figure."""
        claims = [vc("1509", ITEM, "row.quantity", "planned")]

        def outcome(text):
            return gate("planned 1509", [ITEM], claims, outcome=text)
        for text in ("Terms are NET30.", "Ship QTY-12 now.", "MOQ500 applies.", "LT14 days.",
                     "KG25 net.", "PLT3 left.", "CTN120 packed.", "R500 spent.",
                     "W30 delivery.", "FY2026 plan.", "The Q9 review.", "NET-30 terms.",
                     "Paid USD-12.", "Paid CAD500.", "x82 boxes.", "KG-25 net.",
                     # money and period words hyphenated need a claim as bare ones do
                     "PRICE-500 agreed.", "AMT-500 due.", "COST-500 now.", "DUE-30 days.",
                     "ETA-14 set.", "MONTHS-3 left.", "FEE-40 paid.", "YEARS-2 term."):
            with self.subTest(refused=text):
                result = outcome(text)
                self.assertEqual([(f["item"], f["code"]) for f in result["failures"]],
                                 [("outcome", "claim_unstructured")])
        for text in ("Line P-02458 holds it.", "SKU-2222 shipped.", "File F-26-0001 is in.",
                     "Batch BC-12345D held.", "QRM-12345 is open.", "PO5447 and PO-5 ran.",
                     "Order OR1000000001 closed.", "planned 1509 on SKU-1509.",
                     # a raw material's one-letter code is no rand amount
                     "Raw material R-1696 used."):
            with self.subTest(passed=text):
                self.assertTrue(outcome(text)["passed"], outcome(text)["failures"])
        # the same codes in a claim's own text are claimed as text, as before
        self.assertEqual(codes(gate("Line P-02458.", [ITEM], claims)), ["claim_unstructured"])

    def test_a_token_the_question_holds_is_restated_never_a_figure_of_the_answer(self):
        """Supervisor decision 2026-10-29 (GP-05, RM-05): a whole token of the answer text
        holding a letter and a digit that the question holds as a whole token, equal but
        for case, is the question restated. A bare number, a number and a unit apart, a
        token written otherwise, and the same token with no question stay figures."""
        claims = [vc("1509", ITEM, "row.quantity", "planned")]

        def outcome(text, question):
            result = gate("planned 1509", [ITEM], claims, outcome=text, question=question)
            return [(f["item"], f["code"], f["value"]) for f in result["failures"]]
        asked = "What did the NGen Q9 review of MIDAS2 (PO-00349) and lot 12 kg reject?"
        for text in ("The Q9 review rejected it.", "No arrival for MIDAS2 is recorded.",
                     "The q9 review.", "PO-00349 (midas2) planned 1509."):
            with self.subTest(restated=text):
                self.assertEqual(outcome(text, asked), [])
        for text, value in (("The Q9 review cut 12 rows.", "12"), ("It holds 12 kg.", "12"),
                            ("The Q9-10 review.", "9"), ("The Q10 review.", "10"),
                            ("NET30 terms.", "30"), ("The Q9 review.", "9")):
            with self.subTest(figure=text):
                question = None if text == "The Q9 review." else asked
                self.assertIn(("outcome", "claim_unstructured", value),
                              outcome(text, question))
        # a quantity glued to its unit or term is a figure though the question holds it
        glued = "Did 12kg, 500ML, NET30, QTY-12, PRICE-500 or W30 apply to L9 and Q9?"
        for text, value in (("It was 12kg.", "12"), ("It was 500ML.", "500"),
                            ("It was NET30.", "30"), ("It was QTY-12.", "12"),
                            ("It was PRICE-500.", "500"), ("It was W30.", "30")):
            with self.subTest(glued=text):
                self.assertIn(("outcome", "claim_unstructured", value), outcome(text, glued))
        self.assertEqual(outcome("L9 and Q9 did.", glued), [])
        # a token inside a text claim's value stays: the claim covers its text whole
        self.assertEqual(claim_checks.asked_tokens_blanked(
            "Send it for Q9 by 9/30. Q9 too.", asked, ["Send it for Q9 by 9/30."]),
            "Send it for Q9 by 9/30.    too.")

    def test_text_claims_cover_codes_with_digits(self):
        claims = [vc("P-02458", PO_LINE, "row.code"),
                  vc("12500.0", PO_LINE, "row.quantity", unit="EA")]
        self.assertTrue(gate("Purchase order line for code P-02458: quantity 12500.0, uom EA.",
                             [PO_LINE], claims)["passed"])
        self.assertEqual(codes(gate("Line P-02458.", [PO_LINE])), ["claim_unstructured"])
        self.assertEqual(codes(gate("Line P-02459.", [PO_LINE], [vc("P-02459", PO_LINE,
                                                                     "row.code")])),
                         ["claim_value_mismatch"])  # the failing claim still blocks

    def test_extra_cited_rows_neither_cover_nor_rescue_a_number(self):
        # 1509 is in the MO row too, but no claim states it: still unstructured.
        self.assertEqual(codes(gate("produced 1509", [ITEM, MO, LINE, LINK, FILLING])),
                         ["claim_unstructured"])
        # A claim citing the planned field fails however many other rows are cited.
        self.assertEqual(codes(gate("produced 1509", [ITEM, MO, LINE, LINK, FILLING],
                                    [vc("1509", MO, "row.size_pieces", "produced")])),
                         ["claim_role_mismatch"])

    def test_the_close_outcome_is_covered_by_the_claims_of_the_case(self):
        claims = [vc("1509", ITEM, "row.quantity", "planned")]
        self.assertTrue(gate("planned 1509", [ITEM], claims,
                             outcome="Closed: planned 1509, see F1.")["passed"])
        result = gate("planned 1509", [ITEM], claims, outcome="Closed: 77 short.")
        self.assertEqual([(item["item"], item["code"]) for item in result["failures"]],
                         [("outcome", "claim_unstructured")])

    def test_the_close_outcome_is_worded_by_the_grounded_types_of_the_case(self):
        # Review of 8855f22: the outcome, the case's answer, was never wording-checked.
        claims = [vc("1509", ITEM, "row.quantity", "planned")]
        for outcome in ("Closed: AO-26005B produced 1509 finished goods.",
                        "AO-26005B produced 1509 finished goods; closed.",
                        "Closed. Filled, 1509 units.", "Closed: QC inspected all 1509."):
            with self.subTest(outcome=outcome):
                result = gate("Planned 1509.", [ITEM], claims, outcome=outcome)
                self.assertFalse(result["passed"])
                self.assertEqual([(item["item"], item["code"]) for item in result["failures"]],
                                 [("outcome", "claim_wording_role_mismatch")])
        # A derived name in the case does not type the outcome either.
        claim = [vc("1503", role="manufactured_net", unit="pc", calculation="C7")]
        result = gate("1503 good units produced.", [LINK, MO], claim,
                      calculations=[good_calc("manufactured_net")],
                      outcome="Closed: planned 1503 good units.")
        self.assertEqual([(item["item"], item["code"]) for item in result["failures"]],
                         [("outcome", "claim_wording_role_mismatch")])
        self.assertTrue(gate("Planned 1509.", [ITEM], claims,
                             outcome="Closed: 1509 were planned; production not yet "
                                     "reconciled.")["passed"])

    def test_only_workbench_written_link_statements_are_exempt(self):
        text = "Workbench link check x: row 1245 idassembly=10718 equals row 10718 id=10718."
        self.assertTrue(gate(text, [LINK], workbench_statement=True)["passed"])
        self.assertEqual(set(codes(gate(text, [LINK]))), {"claim_unstructured"})


AO_V2 = current_copy(REGISTRY.get("ao_quantity_reconciliation", 2))


PO = "api://public.app_purchase_order/%7B%22id%22%3A%22po-synth-5447%22%7D@g-3"
PO_ROWS = {PO: {"relation": "public.app_purchase_order", "row": {
    "id": "po-synth-5447", "status": "REVISED", "supplier_name": "Acme Packaging Ltd",
    "fulfillment_mode": "PENDING_APPROVAL", "expected_date": "2026-01-07T00:00:00.000Z",
    "shipped_at": "2026-01-07T23:30:00-05:00", "date_generated": "2026-01-02"}}}


class TextValueClaimTests(unittest.TestCase):
    """Directive 912374bc973a: text, status and date values are checked as values, and a
    claim's text must state the values its claims back, as its numbers must be claimed."""

    def check(self, text, *claims, outcome=None, contracts=CONTRACTS):
        return gate(text, [PO], [vc(value, PO, f"row.{column}") for column, value in claims],
                    rows=PO_ROWS, outcome=outcome, contracts=contracts)

    def failed(self, text, *claims, contracts=CONTRACTS):
        return [(f["code"], f["value"])
                for f in self.check(text, *claims, contracts=contracts)["failures"]]

    def test_a_wrong_status_name_or_date_fails_and_the_matching_value_passes(self):
        for text, claims in (
                # A status column folds case (and "_" as a space); an enum column too.
                ("The order is Revised.", [("status", "Revised")]),
                ("The order is revised.", [("status", "REVISED")]),
                ("It is pending approval.", [("fulfillment_mode", "pending approval")]),
                ("Ordered from Acme Packaging Ltd.", [("supplier_name", "Acme Packaging Ltd")]),
                # A date is the cell's calendar date as written, never a UTC day.
                ("Expected on 2026-01-07.", [("expected_date", "2026-01-07")]),
                ("Shipped 2026-01-07.", [("shipped_at", "2026-01-07")]),
                ("Shipped Jan 7, 2026.", [("shipped_at", "2026-01-07")]),
                ("Shipped 2026-01-08T04:30:00Z.", [("shipped_at", "2026-01-08T04:30:00Z")]),
                # An item without text states nothing: its claims stand alone.
                ("", [("status", "REVISED")])):
            with self.subTest(text=text):
                out = self.check(text, *claims)
                self.assertTrue(out["passed"], out["failures"])
        for text, claims, failed in (
                # The value claim is right and the text says another value.
                ("The order is Closed.", [("status", "REVISED")], ["claim_value_unstated"]),
                ("The order is not revised.", [("status", "REVISED")],
                 ["claim_value_unstated"]),
                ("Ordered from Acme Packaging Inc.", [("supplier_name", "Acme Packaging Ltd")],
                 ["claim_value_unstated"]),
                ("Expected on 2026-01-08.", [("expected_date", "2026-01-07")],
                 ["claim_unstructured", "claim_value_unstated"]),
                # A date in the text with no claim at all, as an unclaimed number.
                ("Expected on 2026-01-08.", [], ["claim_unstructured"]),
                # The value claim itself is wrong: a name is exact, a status or a date is
                # never another one.
                ("Ordered from acme packaging ltd.", [("supplier_name", "acme packaging ltd")],
                 ["claim_value_mismatch"]),
                ("The order is Closed.", [("status", "CLOSED")], ["claim_value_mismatch"]),
                ("Shipped 2026-01-08.", [("shipped_at", "2026-01-08")],
                 ["claim_value_mismatch"])):
            with self.subTest(text=text, claims=claims):
                self.assertEqual(codes(self.check(text, *claims)), failed)
        unstated = self.check("The order is Closed.", ("status", "REVISED"))["failures"][0]
        self.assertEqual((unstated["value"], unstated["detail"].split(";")[0]),
                         ("REVISED", "the text does not state this enum value"))
        # The close outcome's dates need a claim of the case, as its numbers do.
        self.assertEqual(codes(self.check("Expected on 2026-01-07.",
                                          ("expected_date", "2026-01-07"),
                                          outcome="Closed: expected 2026-01-09.")),
                         ["claim_unstructured"])

    def test_dates_compare_by_instant_or_by_the_date_as_written(self):
        """e356dc4031b9 item 4: ``_same_value`` on timestamps, directly."""
        def same(claimed, cell):
            claim = {"value": claimed, "kind": "text"}
            return claim_checks._same_value(claim, cell, claim_checks.value_form(claim))
        for claimed, cell, expected in (
                ("2026-10-01T15:00:00Z", "2026-10-01T15:00:00.000000+00:00", True),
                ("2026-10-01T15:00:00Z", "2026-10-01T11:00:00-04:00", True),
                ("2026-10-01T15:00:00.5Z", "2026-10-01T15:00:00.500+00:00", True),
                # Postgres renders "+hh" and nine fraction digits.
                ("2026-10-01T15:00:00Z", "2026-10-01 17:00:00.000000000+02", True),
                ("2026-10-01T15:00:00Z", "2026-10-01T15:00:01Z", False),
                # No offset is no instant: never equal to one with an offset.
                ("2026-10-01T15:00:00Z", "2026-10-01T15:00:00", False),
                ("2026-10-01T15:00:00", "2026-10-01 15:00:00", True),
                ("2026-10-01T15:00:00Z", "2026-10-01", False),
                # A bare date: the cell's calendar date as written in its own offset.
                ("2026-10-01", "2026-10-01T23:30:00-04:00", True),
                ("2026-10-01", "2026-10-02T03:30:00Z", False),
                ("2026-10-01", "2026-10-01 23:30:00-04", True),
                ("2026-10-01", "20261001", True), ("2026-10-01", 20261001, True),
                ("2026-10-01T00:00:00Z", "20261001", False),
                ("2026-10-01", "2026-10-01", True),
                ("2026-02-30", "2026-02-30", True),     # exactly the cell's text
                ("2026-02-30", "2026-03-02", False)):
            with self.subTest(claimed=claimed, cell=cell):
                self.assertIs(same(claimed, cell), expected)

    def test_a_timestamp_is_covered_whole_only_at_an_instant_a_claim_establishes(self):
        """da836e2f3f55 item 1 and Dot 94539984: a date claim read from a cell covers the
        cell's timestamp written whole (no time or offset left as numbers), never another
        instant of the day; shipped_at is 2026-01-07T23:30:00-05:00."""
        day = ("shipped_at", "2026-01-07")
        for text in ("Shipped 2026-01-07T23:30:00-05:00.", "Shipped 2026-01-08T04:30:00Z.",
                     "Shipped 2026-01-07 23:30:00-05."):
            with self.subTest(text=text):
                self.assertEqual(self.failed(text, day), [])
        # 16.5 h early (07:00 on the claimed day in the cell's -05:00) or on another day.
        for text, written in (("Shipped 2026-01-07 12:00:00+00:00.", "2026-01-07 12:00:00+00:00"),
                              ("Shipped 2026-01-06T23:30:00-05:00.", "2026-01-06T23:30:00-05:00")):
            with self.subTest(text=text):
                self.assertEqual(self.failed(text, day), [("claim_unstructured", written),
                                                          ("claim_value_unstated", "2026-01-07")])
        # A claimed timestamp covers its instant in any offset; never another instant, nor
        # a time without an offset (claimed or written).
        instant = ("shipped_at", "2026-01-08T04:30:00Z")
        self.assertEqual(self.failed("Shipped 2026-01-07T23:30:00-05:00.", instant), [])
        for text in ("Shipped 2026-01-08T04:30:01Z.", "Shipped 2026-01-08T04:30:00."):
            with self.subTest(text=text):
                self.assertEqual([code for code, _ in self.failed(text, instant)],
                                 ["claim_unstructured", "claim_value_unstated"])
        for claimed in ("2026-01-08T04:30:01Z", "2026-01-07T23:30:00"):
            with self.subTest(claimed=claimed):
                self.assertEqual(self.failed(f"Shipped {claimed}.", ("shipped_at", claimed)),
                                 [("claim_value_mismatch", claimed)])

    def test_a_claimed_timestamp_states_its_day_in_the_zone_its_source_declares(self):
        """The architect's rule (da836e2f3f55): expected_date is 2026-01-07T00:00:00.000Z, its
        UTC day, unless its source declares a zone (here a unit contract's time_zone)."""
        stamp = ("expected_date", "2026-01-07T00:00:00.000Z")
        zoned = SourceUnitContracts({"schema": "plx-source-unit-contracts/v1", "contracts": [{
            "id": "po-expected-date-zone", "relation": "public.app_purchase_order",
            "column": "expected_date", "time_zone": "America/Toronto",
            "meaning": "test", "evidence": ["test"], "verification": "test"}]})
        for text, contracts, failed in (
                ("Expected on 2026-01-07.", CONTRACTS, []),
                ("Expected on 2026-01-06.", CONTRACTS, ["claim_unstructured",
                                                        "claim_value_unstated"]),
                ("Expected on 2026-01-06.", zoned, []),
                ("Expected Jan 6, 2026.", zoned, []),
                ("Expected on 2026-01-07.", zoned, ["claim_unstructured",
                                                    "claim_value_unstated"])):
            with self.subTest(text=text, zoned=contracts is zoned):
                self.assertEqual([code for code, _ in self.failed(text, stamp,
                                                                  contracts=contracts)], failed)
        # A bare date claimed on the cell is its day in that zone too.
        for value, contracts, failed in (("2026-01-07", CONTRACTS, []),
                                         ("2026-01-06", zoned, []),
                                         ("2026-01-06", CONTRACTS, ["claim_value_mismatch"]),
                                         ("2026-01-07", zoned, ["claim_value_mismatch"])):
            with self.subTest(value=value, zoned=contracts is zoned):
                self.assertEqual([code for code, _ in self.failed(
                    f"Expected on {value}.", ("expected_date", value), contracts=contracts)],
                    failed)
        # A declared zone that does not load reads no day: refused at load, else the claim.
        with self.assertRaisesRegex(ValidationError, "unit_contract_invalid_time_zone"):
            SourceUnitContracts({"schema": "plx-source-unit-contracts/v1", "contracts": [{
                "id": "x", "relation": "public.app_purchase_order", "column": "expected_date",
                "time_zone": "Nowhere/City", "meaning": "t", "evidence": ["t"],
                "verification": "t"}]})
        unknown = lambda *_: {"id": "x", "time_zone": "Nowhere/City"}  # noqa: E731
        self.assertEqual(self.failed("Expected on 2026-01-07.", ("expected_date", "2026-01-07"),
                                     contracts=unknown), [("claim_value_mismatch", "2026-01-07")])

    def test_a_date_inside_an_identifier_token_needs_no_date_claim(self):
        """da836e2f3f55 item 2: a lot code is an identifier, not an ISO date."""
        self.assertEqual(self.failed("Lot 2026-01-07-A was received."), [])
        self.assertEqual(self.failed("Received 2026-01-07, lot A."),
                         [("claim_unstructured", "2026-01-07")])

    def test_no_before_a_number_is_the_abbreviation_of_number(self):
        """da836e2f3f55 item 3: "No. 5" right after a label is a number; after a
        separator (an answer position, line breaks included) it is still the answer."""
        for value, text, supported in (
                ("QC Lot", "QC Lot No. 5 passed.", 1),
                ("Packaging PO", "Packaging PO No. 123 received.", 1),
                ("QC passed", "QC passed? No. 3 units failed.", 0),
                ("QC passed", "QC passed? No.\n3 units failed.", 0),
                ("approved by QA", "Batch approved by QA: No. 5 of 7.", 0),
                ("approved by QA", "approved by QA = No.", 0)):
            with self.subTest(text=text):
                self.assertEqual(claim_checks.supported_quotes(value, text), supported)

    def test_no_longer_and_not_yet_negate(self):
        """da836e2f3f55 item 4: "no longer X" and "not yet X" do not state X."""
        for text in ("The order is no longer REVISED.", "The order is not yet revised.",
                     "The order isn't yet REVISED."):
            with self.subTest(text=text):
                self.assertEqual(self.failed(text, ("status", "REVISED")),
                                 [("claim_value_unstated", "REVISED")])
        self.assertEqual(claim_checks.supported_quotes(
            "approved", "The re-quote is no longer approved."), 0)
        # A negated type word types no number: "not yet produced" says nothing produced.
        self.assertTrue(gate("Not yet produced: 1509 planned.", [ITEM],
                             [vc("1509", ITEM, "row.quantity", "planned")])["passed"])


class AoCaseClaimTests(unittest.TestCase):
    """AO-26005B through the MCP case tools: the shipped investigation's claims hold; a
    planned figure claimed as produced, or a number without a claim, fails case
    verification and the playbook."""

    def setUp(self) -> None:
        self.h = ao_tests.Ao26005bCaseTest("setUp")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.runtime = self.h.runtime()
        self.server = self.h.server(ao_tests.OPS_QUALITY, self.runtime)
        self.call = self.h.caller(self.server)
        self.actor = Actor("plx", ao_tests.OPS_QUALITY)
        self.out = ao_tests.investigate(self.call)
        self.case_id = self.out["case_id"]

    def add(self, statement, keys, claims=None):
        arguments = {"case_id": self.case_id, "kind": "fact", "status": "supported",
                     "statement": statement, "citations": [self.out["cite"][k] for k in keys]}
        if claims is not None:
            arguments["value_claims"] = claims
        return self.call("plx.case_add_finding", arguments)["finding"]

    def verdicts(self):
        result = case_verification.check_claims(self.runtime, self.actor, self.case_id)
        report = pe.evaluate(AO_V2, self.call, self.case_id)
        return result, report

    def test_the_shipped_investigation_passes_the_gate(self):
        result, report = self.verdicts()
        self.assertTrue(result["passed"], result["failures"])
        self.assertEqual(result["writes"], 0)
        self.assertGreaterEqual(result["counts"]["source_contract"], 6)
        typed = {(claim["value"], claim.get("role"), claim["role_basis"])
                 for entry in result["items"] for claim in entry["claims"]}
        self.assertLessEqual({("1509", "planned", "source_contract"),
                              ("1503", "actual", "source_contract"),
                              ("1503", "manufactured", "source_contract"),
                              ("1502", "filled", "source_contract"),
                              ("1509", "planned", "source_contract"),  # MO size_pieces
                              ("20", "damaged", "calculation")}, typed)
        self.assertTrue(report["summary"]["passed_all"], report["claim_checks"]["failures"])
        self.assertEqual(report["summary"]["claim_failures"], 0)

    def test_a_planned_figure_claimed_as_produced_fails_on_the_item_or_the_mo_row(self):
        for key, field in (("item", "row.quantity"), ("mo", "row.size_pieces"),
                           ("mo", "row.quantity")):
            with self.subTest(row=key, field=field):
                self.setUp()
                finding = self.add("AO-26005B produced 1509 finished goods.", [key],
                                   [{"value": "1509", "citation": self.out["cite"][key],
                                     "field": field, "role": "produced"}])
                result, report = self.verdicts()
                self.assertEqual([(item["item"], item["code"]) for item in result["failures"]],
                                 [(finding["finding_id"], "claim_role_mismatch")])
                self.assertFalse(report["summary"]["passed_all"])
                self.assertEqual(report["summary"]["claim_failure_codes"],
                                 ["claim_role_mismatch"])
                # The playbook's own checks all pass: only the claim gate catches it.
                self.assertEqual(report["summary"]["failed_ids"], [])

    def test_the_reviewer_probes_fail_closed_on_the_real_case(self):
        for text, keys in (("Produced: 1,509 finished goods.", ["item"]),
                           ("AO-26005B produced 1509 finished goods.", ["mo"]),
                           ("Filled (1509) on 2026-01-07.", ["item"]),
                           ("The quantity produced on line 01 was 1509.", ["item"])):
            with self.subTest(text=text):
                self.setUp()
                self.add(text, keys)
                result, report = self.verdicts()
                self.assertFalse(result["passed"])
                self.assertEqual({item["code"] for item in result["failures"]},
                                 {"claim_unstructured"})
                self.assertFalse(report["summary"]["passed_all"])

    def refused_compute(self, expression, inputs, relations):
        """plx.case_compute that must be refused (and record nothing): its error text."""
        before = len(self.runtime.cases.get(self.actor, self.case_id)["calculations"])
        result = self.h.call(self.server, "plx.case_compute", {
            "case_id": self.case_id, "expression": expression, "unit": "pc",
            "inputs": inputs, "relations": relations})
        self.assertTrue(result["isError"], result)
        self.assertEqual(len(self.runtime.cases.get(self.actor, self.case_id)["calculations"]),
                         before)
        return result["content"][0]["text"]

    def compute(self, expression, inputs, relations):
        return self.call("plx.case_compute", {
            "case_id": self.case_id, "expression": expression, "unit": "pc",
            "inputs": inputs, "relations": relations})["calculation"]

    def inputs(self, **fields):
        """name -> (cite key, field) as plx.case_compute inputs in pc."""
        return {name: {"citation": self.out["cite"][key], "field": field, "unit": "pc"}
                for name, (key, field) in fields.items()}

    def assert_blocked(self, text, claims, code, keys=("link", "mo"), outcome=None):
        finding = self.add(text, list(keys), claims)
        if outcome:
            self.call("plx.case_close", {"case_id": self.case_id, "outcome": outcome})
        result, report = self.verdicts()
        self.assertFalse(result["passed"])
        self.assertIn(code, {item["code"] for item in result["failures"]})
        self.assertIn(finding["finding_id"] if not outcome else "outcome",
                      {item["item"] for item in result["failures"]})
        self.assertFalse(self.call("plx.case_get", {"case_id": self.case_id, "section": "claims"})["passed"])
        self.assertFalse(report["summary"]["passed_all"])
        return result

    def test_a_calculation_relabelled_as_produced_is_refused_on_the_real_case(self):
        # planned - damaged "as produced" / "as produced_net" / filled_* / inspected_*: the
        # label names another type than the form (planned - damaged, a planned figure),
        # so compute refuses it and records nothing (review of fb5ac6b, of 8855f22).
        inputs = self.inputs(p=("mo", "row.quantity"), d=("mo", "row.qty_damaged"))
        for name in ("produced", "produced_net", "produced_good", "filled_capacity",
                     "inspected_ok", "manufactured_net"):
            with self.subTest(name=name):
                text = self.refused_compute("p - d", inputs, [
                    {"left": "planned", "op": "-", "right": "damaged", "as": name}])
                self.assertIn("quantity_label_inconsistent", text)
                self.assertIn("planned - damaged", text)

    def test_round_five_sum_relabels_are_refused_on_the_real_case(self):
        # Review of 69467b8 (probes p2/p3): the reviewer's relations, verbatim. Each label
        # names a type the simplified form is not, so compute refuses and records nothing.
        made = self.inputs(m=("link", "row.qtymfg_c"), p=("mo", "row.quantity"))
        item = self.inputs(a=("item", "row.actual_amount"), p=("item", "row.quantity"))
        mo = self.inputs(p=("mo", "row.quantity"), d=("mo", "row.qty_damaged"))
        for expression, inputs, relations, form in (
                ("m + (p - m)", made,
                 [{"left": "planned", "op": "-", "right": "manufactured", "as": "planned_unmade"},
                  {"left": "manufactured", "op": "+", "right": "planned_unmade",
                   "as": "produced_total"}], "the value is planned"),
                ("a + (p - a)", item,
                 [{"left": "planned", "op": "-", "right": "actual", "as": "planned_short"},
                  {"left": "actual", "op": "+", "right": "planned_short",
                   "as": "produced_total"}], "the value is planned"),
                ("p + a - a", item,
                 [{"left": "actual", "op": "+", "right": "planned", "as": "produced_total"},
                  {"left": "produced_total", "op": "-", "right": "actual",
                   "as": "produced_x"}], "names produced"),
                ("p + a * 0", item,
                 [{"left": "actual", "op": "+", "right": "planned", "as": "produced_total"}],
                 "the value is planned"),
                ("p + d", mo, [{"left": "damaged", "op": "+", "right": "planned",
                                "as": "damaged_total"}], "damaged + planned"),
                ("p - (p - a)", item,
                 [{"left": "planned", "op": "-", "right": "actual", "as": "planned_short"},
                  {"left": "planned", "op": "-", "right": "planned_short",
                   "as": "planned_x"}], "the value is actual")):
            with self.subTest(expression=expression):
                text = self.refused_compute(expression, inputs, relations)
                self.assertIn("quantity_label_inconsistent", text)
                self.assertIn(form, text)

    def test_a_sum_is_typed_by_its_simplified_form_on_the_real_case(self):
        # m + (p - m) under labels that name no type is computed, and it is what it is:
        # the planned 1509. Claimed or worded as produced it fails check_claims,
        # plx.case_get section claims and AO_V2, in a finding and in the close outcome.
        made = self.inputs(m=("link", "row.qtymfg_c"), p=("mo", "row.quantity"))
        relations = [{"left": "planned", "op": "-", "right": "manufactured", "as": "unmade"},
                     {"left": "manufactured", "op": "+", "right": "unmade", "as": "total"}]
        receipt = self.compute("m + (p - m)", made, relations)
        self.assertEqual((receipt["result"], receipt["result_role"], receipt["result_type"]),
                         ("1509", "total", "planned"))
        calc = receipt["calc_id"]
        for role, code in (("produced", "claim_role_mismatch"),
                           ("manufactured", "claim_role_mismatch")):
            with self.subTest(role=role):
                self.setUp()
                made = self.inputs(m=("link", "row.qtymfg_c"), p=("mo", "row.quantity"))
                receipt = self.compute("m + (p - m)", made, relations)
                self.assert_blocked("AO-26005B produced 1509 finished goods.", [
                    {"value": "1509", "calculation": receipt["calc_id"], "role": role,
                     "unit": "pc"}], code)
        self.setUp()
        made = self.inputs(m=("link", "row.qtymfg_c"), p=("mo", "row.quantity"))
        calc = self.compute("m + (p - m)", made, relations)["calc_id"]
        self.assert_blocked("AO-26005B produced 1509 finished goods.", [
            {"value": "1509", "calculation": calc, "role": "total", "unit": "pc"}],
            "claim_wording_role_mismatch")
        self.setUp()
        made = self.inputs(m=("link", "row.qtymfg_c"), p=("mo", "row.quantity"))
        calc = self.compute("m + (p - m)", made, relations)["calc_id"]
        self.assert_blocked("AO-26005B: 1509 planned finished goods.", [
            {"value": "1509", "calculation": calc, "role": "planned", "unit": "pc"}],
            "claim_wording_role_mismatch",
            outcome="Closed: AO-26005B produced 1509 finished goods.")
        # Truthfully worded, the same calculation passes everything.
        self.setUp()
        made = self.inputs(m=("link", "row.qtymfg_c"), p=("mo", "row.quantity"))
        calc = self.compute("m + (p - m)", made, relations)["calc_id"]
        self.add("AO-26005B: 1509 planned finished goods.", ["link", "mo"],
                 [{"value": "1509", "calculation": calc, "role": "planned", "unit": "pc"}])
        result, report = self.verdicts()
        self.assertTrue(result["passed"], result["failures"])
        self.assertTrue(report["summary"]["passed_all"])

    def test_nested_division_stays_fast_through_mcp(self):
        # Review of d889cba (rvct_dos_mcp.py): the claim check hung once a finding
        # cited a deeply nested division; with a declared role plx.case_compute hung.
        import time
        line = self.out["cite"]["line"]
        expression = "x/(y+" * 49 + "x" + ")" * 49  # the evaluator's deepest
        inputs = {"x": {"citation": line, "field": "row.id", "unit": "1"},
                  "y": {"citation": line, "field": "row.idoe", "unit": "1"}}
        started = time.monotonic()
        receipt = self.call("plx.case_compute", {"case_id": self.case_id, "unit": "1",
                                                 "expression": expression, "inputs": inputs})
        self.assertLess(time.monotonic() - started, 1.0)
        calc = receipt["calculation"]
        self.add("A ratio of the line ids is recorded.", ["line"],
                 [{"value": calc["result"], "calculation": calc["calc_id"]}])
        started = time.monotonic()
        self.assertTrue(self.call("plx.case_get", {"case_id": self.case_id, "section": "claims"})["passed"])
        self.assertLess(time.monotonic() - started, 1.0)
        started = time.monotonic()
        # With roles the combination guard and the type algebra both run: answered at once
        # (here the guard refuses planned beside an unroled quantity).
        roled = self.h.call(self.server, "plx.case_compute", {
            "case_id": self.case_id, "unit": "1", "expression": expression,
            "inputs": {**inputs, "x": {**inputs["x"], "role": "planned"}}})
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(roled["isError"])
        started = time.monotonic()
        same = self.h.call(self.server, "plx.case_compute", {
            "case_id": self.case_id, "unit": "1", "expression": expression,
            "inputs": {"x": {**inputs["x"], "role": "planned"},
                       "y": {**inputs["y"], "role": "planned"}}})
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(same["isError"])  # a planned ratio summed with planned
        # A contracted planned factor beside the ratio: the guard passes, the algebra runs.
        started = time.monotonic()
        scaled = self.call("plx.case_compute", {
            "case_id": self.case_id, "unit": "pc", "expression": f"p * ({expression})",
            "inputs": {**inputs, "p": {"citation": self.out["cite"]["item"],
                                       "field": "row.quantity", "unit": "pc"}}})["calculation"]
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual((scaled["result_role"], scaled["result_type"]), ("planned", None))

    def test_a_recaptured_changed_row_never_cancels_through_mcp(self):
        # Review of 6948548 (rvct2/cancel_mcp.py): the AO item row read at capture 9
        # (quantity 1509) and, after it changed, at capture 12 (1600).
        def read():
            return self.call("mirror.rows", {
                "relation": "silver.assembly_order_items", "where": [["id", "eq", 58383]],
                "limit": 5})["rows"][0]["citation"]
        first = read()
        for row in self.h.fake.tables["silver.assembly_order_items"]:
            if row.get("id") == 58383:
                row["quantity"] = 1600.0
        self.h.clock.advance(7 * 86400)
        second = read()
        self.assertNotEqual(first, second)
        inputs = {"m": {"citation": self.out["cite"]["link"], "field": "row.qtymfg_c",
                        "unit": "pc"},
                  "p": {"citation": first, "field": "row.quantity", "unit": "pc"},
                  "q": {"citation": second, "field": "row.quantity", "unit": "pc"}}
        text = self.refused_compute("m + p - q", inputs, [
            {"left": "manufactured", "op": "+", "right": "planned", "as": "sum_x"},
            {"left": "sum_x", "op": "-", "right": "planned", "as": "net_x"}])
        self.assertIn("one_cell_two_values", text)

    def test_a_mixed_sum_is_untyped_on_the_real_case(self):
        # p + d (MO quantity + qty_damaged = 1509) under a type-free label: untyped, so no
        # quantity type can be claimed and no type word may describe it.
        mo = self.inputs(p=("mo", "row.quantity"), d=("mo", "row.qty_damaged"))
        relations = [{"left": "damaged", "op": "+", "right": "planned", "as": "sum"}]
        receipt = self.compute("p + d", mo, relations)
        self.assertIsNone(receipt["result_type"])
        self.assert_blocked("1509 units in all.", [
            {"value": "1509", "calculation": receipt["calc_id"], "role": "damaged",
             "unit": "pc"}], "claim_role_unverified", keys=("mo",))
        for text in ("1509 damaged units.", "Damaged, 1509 units.",
                     "AO-26005B produced 1509 finished goods."):
            with self.subTest(text=text):
                self.setUp()
                mo = self.inputs(p=("mo", "row.quantity"), d=("mo", "row.qty_damaged"))
                calc = self.compute("p + d", mo, relations)["calc_id"]
                self.assert_blocked(text, [{"value": "1509", "calculation": calc,
                                            "role": "sum", "unit": "pc"}],
                                    "claim_wording_role_unverified", keys=("mo",))
        self.setUp()
        mo = self.inputs(p=("mo", "row.quantity"), d=("mo", "row.qty_damaged"))
        calc = self.compute("p + d", mo, relations)["calc_id"]
        self.add("The two MO fields add up to 1509.", ["mo"],
                 [{"value": "1509", "calculation": calc, "role": "sum", "unit": "pc"}])
        self.assertTrue(self.verdicts()[0]["passed"])

    def test_a_produced_close_outcome_over_a_planned_claim_fails_on_the_real_case(self):
        # Review of 8855f22 probe_outcome.
        mo = self.out["cite"]["mo"]
        self.add("AO-26005B was planned at 1509 units.", ["mo"],
                 [{"value": "1509", "citation": mo, "field": "row.quantity",
                   "role": "planned"}])
        self.assertTrue(self.verdicts()[0]["passed"])
        self.call("plx.case_close", {"case_id": self.case_id,
                                     "outcome": "AO-26005B produced 1509 finished goods; "
                                                "closed."})
        result, report = self.verdicts()
        self.assertEqual([(item["item"], item["code"]) for item in result["failures"]],
                         [("outcome", "claim_wording_role_mismatch")])
        self.assertFalse(report["summary"]["passed_all"])

    def test_the_read_only_mcp_tool_reports_the_same_verdict(self):
        before = len(self.runtime.cases.get(self.actor, self.case_id)["timeline"])
        self.add("AO-26005B produced 1509 finished goods.", ["item"],
                 [{"value": "1509", "citation": self.out["cite"]["item"],
                   "field": "row.quantity", "role": "planned"}])
        result = self.call("plx.case_get", {"case_id": self.case_id, "section": "claims"})
        self.assertFalse(result["passed"])
        self.assertEqual([item["code"] for item in result["failures"]],
                         ["claim_wording_role_mismatch"])
        self.assertEqual(len(self.runtime.cases.get(self.actor, self.case_id)["timeline"]),
                         before + 1)  # only the finding added above; the tool wrote nothing

    def test_a_correct_bulleted_claim_passes(self):
        self.add("Produced: 1503 units\nFilled: 1502", ["item", "filling"], [
            {"value": "1503", "citation": self.out["cite"]["item"],
             "field": "row.actual_amount", "role": "produced"},
            {"value": "1502", "citation": self.out["cite"]["filling"], "field": "row.qty",
             "role": "filled", "unit": "EA"}])
        result, report = self.verdicts()
        self.assertTrue(result["passed"], result["failures"])
        self.assertTrue(report["summary"]["passed_all"])

    def test_superseding_the_wrong_claim_restores_the_case(self):
        finding = self.add("AO-26005B filled 1509.", ["item"],
                           [{"value": "1509", "citation": self.out["cite"]["item"],
                             "field": "row.quantity", "role": "filled"}])
        self.assertFalse(self.verdicts()[0]["passed"])
        self.runtime.cases.supersede(self.actor, self.case_id, finding["finding_id"],
                                     "1509 is the planned quantity, not a filled count",
                                     author_kind="agent")  # the agent that wrote it
        self.assertTrue(self.verdicts()[0]["passed"])

    def test_only_tool_written_statements_are_marked_workbench(self):
        # A caller cannot mark its own statement as Workbench-written through the tool.
        refused = self.h.call(self.server, "plx.case_add_finding", {
            "case_id": self.case_id, "kind": "fact", "status": "supported",
            "statement": "produced 1509", "citations": [self.out["cite"]["item"]],
            "workbench_statement": True})
        self.assertTrue(refused["isError"])
        receipt = self.runtime.cases.finding(self.actor, self.case_id, "sha256 0a1b 2 page(s)",
                                             "fact", "supported", [self.out["cite"]["item"]],
                                             workbench_statement=True)
        view = self.runtime.cases.get(self.actor, self.case_id)
        [entry] = [item for item in view["findings"]["supported"]
                   if item["id"] == receipt["finding_id"]]
        self.assertEqual(entry["statement_by"], "workbench")
        self.assertTrue(self.verdicts()[0]["passed"])

    def test_malformed_claims_are_refused_by_the_tool_and_record_nothing(self):
        before = len(self.runtime.cases.get(self.actor, self.case_id)["timeline"])
        refused = self.h.call(self.server, "plx.case_add_finding", {
            "case_id": self.case_id, "kind": "fact", "status": "supported",
            "statement": "planned 1509", "citations": [self.out["cite"]["item"]],
            "value_claims": [{"value": "1509", "citation": self.out["cite"]["mo"],
                              "field": "row.size_pieces", "role": "planned"}]})
        self.assertTrue(refused["isError"])
        self.assertIn("value_claim_citation_not_cited", refused["content"][0]["text"])
        self.assertEqual(len(self.runtime.cases.get(self.actor, self.case_id)["timeline"]),
                         before)

    def test_a_gap_and_the_close_outcome_are_checked_too(self):
        self.call("plx.case_record_gap", {
            "case_id": self.case_id, "state": "unknown", "missing": "Why 6 were not produced",
            "why": "Nobody recorded it.", "who_can_answer": ["Production planning"],
            "citations": [self.out["cite"]["item"]]})
        result = self.verdicts()[0]
        self.assertEqual([item["code"] for item in result["failures"]], ["claim_unstructured"])
        self.call("plx.case_close", {"case_id": self.case_id,
                                     "outcome": "Planned 1509, produced 1503; 99 unexplained."})
        result = self.verdicts()[0]
        self.assertEqual([(item["item"], item["value"]) for item in result["failures"]][-1:],
                         [("outcome", "99")])

    def test_a_finding_withheld_from_the_reader_is_not_a_pass(self):
        reader = Actor("plx", frozenset({"staff:operations"}))
        result = case_verification.check_claims(self.runtime, reader, self.case_id)
        self.assertFalse(result["passed"])
        self.assertIn("F2", result["unchecked_items"])

    def test_a_proposal_read_back_reports_its_evidence_claims(self):
        item = self.out["cite"]["item"]
        receipt = self.runtime.cases.propose(
            self.actor, self.case_id, "fm.assembly_order.review", {"note": "review"},
            citations=[item], evidence={"statement": "AO-26005B produced 1509."},
            expected_effect={"relation": "silver.assembly_order_items", "key": {"id": 58383},
                             "fields": ["quantity"], "equals": 1509})
        result = case_verification.verify_proposal(self.runtime, self.actor, self.case_id,
                                                   receipt["proposal_id"])
        self.assertEqual(result["outcome"], "effect_observed")  # the read-back is unchanged
        self.assertFalse(result["evidence_claims"]["passed"])
        self.assertEqual([item["code"] for item in result["evidence_claims"]["failures"]],
                         ["claim_unstructured"])


class OriginalPageClaimTests(unittest.TestCase):
    """Review of 6948548 (rvct2/orig.py): the paper/FM conflict gaps of the AO case with an
    attached original wrote 20, 3, 11, 11 without claims. FM figures are claimed on their
    rows, the paper figure on the page's machine-read text, and 'page N' is a locator."""

    PAGE = "original://orig-1#page=3"
    VIEW = {"original_id": "orig-1", "page": 3, "text": "P-01274 11 shipper box\nP-0121: 07",
            "text_source": "ocr"}

    def page_gate(self, text, claims):
        rows = dict(ROWS, **{self.PAGE: self.VIEW})
        return gate(text, [self.PAGE], claims, rows=rows)

    def test_a_page_figure_is_claimed_on_the_page_text_untyped(self):
        out = self.page_gate("Page 3 of the original reads 11.",
                             [{"value": "11", "citation": self.PAGE, "field": "text"}])
        self.assertTrue(out["passed"], out["failures"])
        self.assertEqual(checked(out)[0]["role_basis"], "untyped")
        self.assertTrue(self.page_gate("It reads 7.", [{"value": "7", "citation": self.PAGE,
                                                        "field": "text"}])["passed"])
        self.assertEqual(codes(self.page_gate("It reads 12.", [
            {"value": "12", "citation": self.PAGE, "field": "text"}])), ["claim_value_mismatch"])
        self.assertEqual(codes(self.page_gate("It reads 11.", [
            {"value": "11", "citation": self.PAGE, "field": "text", "role": "damaged"}])),
            ["claim_role_unverified"])
        # The machine-read figure types no prose.
        self.assertEqual(codes(self.page_gate("11 damaged boxes.", [
            {"value": "11", "citation": self.PAGE, "field": "text"}])),
            ["claim_wording_role_unverified"])
        # A row citation has no page text.
        self.assertEqual(codes(gate("11.", [ITEM], [{"value": "11", "citation": ITEM,
                                                    "field": "text"}])),
                         ["claim_value_mismatch"])

    def test_page_numbers_are_locators_only_for_pages_the_item_cites(self):
        # Review of 5548698 (rvct3/loc.py): "pages N, M ..." blanked any number after a
        # comma, 'and', 'to' or a dash. Now a number is a locator only when the item cites
        # that page (original://...#page=N); the run ends at the first other number.
        page3, page4 = self.PAGE, self.PAGE.replace("page=3", "page=4")
        view4 = dict(self.VIEW, page=4)
        rows = dict(ROWS, **{page3: self.VIEW, page4: view4})
        for text, cites, quantity in (
                ("On pages 3 and 4, 1600 units were produced.", [page3, page4], "1600"),
                ("pages 3, 1509 units", [page3], "1509"),
                ("On pages 2 to 3, 1509 units were planned.", [page3], "1509"),
                ("pages 3 to 20 boxes were damaged", [page3], "20"),
                ("Page 3, 1600 units", [page3], "1600"),
                ("page 3 shows 1509 units.", [ITEM], "1509")):
            with self.subTest(text=text):
                out = gate(text, cites, rows=rows)
                self.assertFalse(out["passed"])
                self.assertEqual(set(codes(out)), {"claim_unstructured"})
                self.assertIn(quantity, [item["value"] for item in out["failures"]])
        for text, cites in (("See page 3.", [page3]), ("See pages 3 and 4.", [page3, page4]),
                            ("Page 3 of the attached original.", [page3])):
            with self.subTest(text=text):
                self.assertTrue(gate(text, cites, rows=rows)["passed"])
        # Without a page citation every number is a quantity.
        self.assertEqual(codes(gate("See page 3.", [ITEM])), ["claim_unstructured"])
        self.assertEqual([str(v) for _w, v in claim_checks.prose_numbers(
            "pages 3 and 4, 1600 units", pages=frozenset({3, 4}))], ["1600"])
        self.assertEqual([str(v) for _w, v in claim_checks.prose_numbers(
            "pages 3 and 11", pages=frozenset({3, 11}))], [])
        self.assertEqual(claim_checks.cited_pages([page3, ITEM,
                                                   "original://o-2#extraction=2&page=11"]),
                         frozenset({3, 11}))

    def test_a_type_word_never_binds_to_a_cited_page_number(self):
        # Review of 8257aee (rvct4/word.py, outc.py): the wording check kept "page 3", so
        # "produced" bound to the unclaimed 3 and was used up; 1509 (planned) then passed.
        rows = dict(ROWS, **{self.PAGE: self.VIEW})
        planned = vc("1509", ITEM, "row.quantity", "planned")
        for text in ("Produced (page 3): 1509 units.", "Produced per page 3: 1509 units.",
                     "Produced on page 3: 1509.", "1509 units which page 3 says were produced."):
            with self.subTest(text=text):
                out = gate(text, [ITEM, self.PAGE], [planned], rows=rows)
                self.assertEqual(codes(out), ["claim_wording_role_mismatch"])
                out = gate("Planned 1509.", [ITEM, self.PAGE], [planned], rows=rows,
                           outcome="Closed. " + text)
                self.assertEqual([(item["item"], item["code"]) for item in out["failures"]],
                                 [("outcome", "claim_wording_role_mismatch")])
        self.assertTrue(gate("Planned (page 3): 1509 units.", [ITEM, self.PAGE], [planned],
                             rows=rows)["passed"])

    def test_page_takes_one_number_and_a_repeat_ends_the_run(self):
        page4 = self.PAGE.replace("page=3", "page=4")
        rows = dict(ROWS, **{self.PAGE: self.VIEW, page4: dict(self.VIEW, page=4)})
        for text, cites, quantity in (("Page 3, 3 cartons were damaged.", [self.PAGE], "3"),
                                      ("pages 3 and 4, 4 cartons", [self.PAGE, page4], "4"),
                                      ("page 3 4 cartons", [self.PAGE, page4], "4")):
            with self.subTest(text=text):
                out = gate(text, cites, rows=rows)
                self.assertEqual(codes(out), ["claim_unstructured"])
                self.assertEqual([item["value"] for item in out["failures"]], [quantity])

    def test_a_claimed_value_is_never_a_page_locator(self):
        # Review of 5dea961 (rvct5/coin.py): a claimed number that coincided with a cited
        # page was blanked, so its type word was never checked. It now stays a quantity.
        page = lambda n: self.PAGE.replace("page=3", f"page={n}")  # noqa: E731
        rows = dict(ROWS, **{page(n): dict(self.VIEW, page=n) for n in (1, 3, 4, 11)})
        damaged3 = {"value": "3", "citation": DAMAGE, "field": "row.qty_damaged",
                    "role": "damaged"}
        for text in ("Pages 4 and 3 units were produced.", "Per pages 4, 3 units were produced."):
            with self.subTest(text=text):
                out = gate(text, [DAMAGE, page(3), page(4)], [damaged3], rows=rows)
                self.assertEqual(codes(out), ["claim_wording_role_mismatch"])
                out = gate("3 damaged.", [DAMAGE, page(3), page(4)], [damaged3], rows=rows,
                           outcome="Closed. " + text)
                self.assertEqual([(item["item"], item["code"]) for item in out["failures"]],
                                 [("outcome", "claim_wording_role_mismatch")])
        # The paper's 11 claimed on page 3's text (untyped): pages 1, 3 and 11 cited.
        rows11 = dict(rows, **{page(3): dict(self.VIEW, text="Shipper boxes damaged 11")})
        for text in ("Pages 1 and 3, 11 cartons were damaged.",
                     "Pages 1, 3 and 11 cartons were damaged."):
            with self.subTest(text=text):
                out = gate(text, [page(1), page(3), page(11)],
                           [{"value": "11", "citation": page(3), "field": "text"}], rows=rows11)
                self.assertEqual(codes(out), ["claim_wording_role_unverified"])
        # The honest wording still passes: 3 is damaged.
        self.assertTrue(gate("Pages 4 and 3 damaged units.", [DAMAGE, page(3), page(4)],
                             [damaged3], rows=rows)["passed"])

    def test_page_abbreviations_stay_quantities_and_split_no_sentence(self):
        # Review of 9d55e3f (rvct6/split.py, oc.py, abbr2.py): "p. 4" ended the sentence
        # for the wording check, so "Produced (p. 4): 1509." passed with 1509 planned, and
        # digits right after a letter and a period ("p.3") were no number at all. Only
        # "page"/"pages" are locators; abbreviations stay quantities needing claims.
        page4 = self.PAGE.replace("page=3", "page=4")
        rows = dict(ROWS, **{self.PAGE: self.VIEW, page4: dict(self.VIEW, page=4)})
        cites = [ITEM, DAMAGE, self.PAGE, page4]
        damaged3 = [vc("3", DAMAGE, "row.qty_damaged", "damaged")]
        planned = [vc("1509", ITEM, "row.quantity", "planned")]
        for text, claims in (("Produced (p. 4): 3.", damaged3),
                             ("Produced per p. 4: 3 cartons.", damaged3),
                             ("Produced, see pp. 3-4: 1509 units.", planned),
                             ("Produced (pp. 3, 4): 1509.", planned),
                             ("Produced (pg. 4): 1509.", planned),
                             ("Produced (pgs. 3-4): 1509.", planned),
                             ("Produced (pg 4): 1509.", planned),
                             ("Produced (page 4): 1509.", planned),
                             ("Produced (p. 4) 1509 units.", planned),
                             ("p. 3 units were produced.", damaged3),
                             ("p.3 units were produced.", damaged3),
                             ("pg 3 units were produced.", damaged3),
                             ("pp. 4, 3 units were produced.", damaged3)):
            with self.subTest(text=text):
                self.assertFalse(gate(text, cites, claims, rows=rows)["passed"])
                out = gate("Planned 1509; 3 damaged.", cites, damaged3 + planned, rows=rows,
                           outcome="Closed. " + text)
                self.assertFalse(out["passed"])
                self.assertEqual({item["item"] for item in out["failures"]}, {"outcome"})
        # The digits after "p." are a number, and an abbreviation's period is no sentence end.
        self.assertEqual([str(v) for _w, v in claim_checks.prose_numbers("p.3 units")], ["3"])
        self.assertEqual(codes(gate("p.3 units were produced.", cites, damaged3, rows=rows)),
                         ["claim_wording_role_mismatch"])
        self.assertEqual(codes(gate("See p. 3.", [self.PAGE], rows=rows)),
                         ["claim_unstructured"])
        # "page 3" with page 3 cited is a locator: nothing to claim, nothing typed.
        self.assertTrue(gate("page 3 units were produced.", [self.PAGE], rows=rows)["passed"])

    def test_an_abbreviation_joins_a_sentence_only_before_a_number(self):
        # Review of e7a1db3 (rvct7/join*.py, gate.py): the abbreviation list ignored every
        # such period, so "...produced on Line E. The FM row counts 1503." joined two
        # sentences and "produced" found the produced 1503. Only "p. 4" / "No. 3" (a digit
        # right after, or after one space) are joined now; e and i are no abbreviations.
        claims = [vc("1509", ITEM, "row.quantity", "planned"),
                  vc("1503", ITEM, "row.actual_amount", "actual")]
        for text in ("The 1509 units were then all produced on Line E. The FM row counts 1503.",
                     "The 1509 units were then all produced in Room I. The FM row counts 1503.",
                     "The 1509 units were then all produced, per the batch record ref. "
                     "The FM row counts 1503.",
                     "All 1509 units were then fully produced, see Fig. The FM row counts 1503.",
                     "Of the 1509 units, every one was produced: no. The FM row counts 1503."):
            with self.subTest(text=text):
                self.assertEqual(codes(gate(text, [ITEM], claims)),
                                 ["claim_wording_role_mismatch"])
                out = gate("Planned 1509; produced 1503.", [ITEM], claims,
                           outcome="Closed. " + text)
                self.assertEqual([(item["item"], item["code"]) for item in out["failures"]],
                                 [("outcome", "claim_wording_role_mismatch")])
        # Before a number the period is still no sentence end; i.e. / e.g. are whole tokens.
        damaged3 = [vc("3", DAMAGE, "row.qty_damaged", "damaged")]
        for text in ("Produced, approx. 3.", "Produced the whole lot, i.e. 3 cartons.",
                     "It was produced on schedule as per qty. 3."):
            with self.subTest(text=text):
                self.assertEqual(codes(gate(text, [DAMAGE], damaged3)),
                                 ["claim_wording_role_mismatch"])
        self.assertTrue(gate("All of it was produced, approx. Damaged 3.", [DAMAGE],
                             damaged3)["passed"])

    def test_a_page_claim_reads_grouped_numbers_and_checks_a_printed_unit(self):
        rows = dict(ROWS, **{self.PAGE: dict(self.VIEW, text="Total 1,509 units; box 509")})
        claim = {"value": "1509", "citation": self.PAGE, "field": "text"}
        self.assertTrue(gate("It reads 1509.", [self.PAGE], [claim], rows=rows)["passed"])
        self.assertEqual(codes(gate("It reads 1.", [self.PAGE], [
            {**claim, "value": "1"}], rows=rows)), ["claim_value_mismatch"])

        def unit_gate(text, value, unit):
            page = {self.PAGE: dict(self.VIEW, text=text)}
            shown = value + ("%" if unit == "percent" else "")
            return gate(f"It reads {shown}.", [self.PAGE], [{**claim, "value": value,
                                                             "unit": unit}],
                        rows=dict(ROWS, **page))

        # Dot P8-AO-original-page-unit-route: a unit the page prints after the figure must
        # agree (count words are EA, % is percent); with none printed the unit is left
        # unverified for the reader (the scorer's expected unit) to judge.
        for text, value, unit, basis in (
                ("Total 1,509 units; box 509", "1509", "EA", "page_text"),
                ("Filled qty: 1502 EA", "1502", "ea", "page_text"),
                ("Yield 25%", "25", "percent", "page_text"),
                ("Total 1,509 units; box 509", "509", "EA", "unverified")):
            out = unit_gate(text, value, unit)
            self.assertTrue(out["passed"], (text, out["failures"]))
            self.assertEqual(checked(out)[0]["unit_basis"], basis)
        for text, value, unit in (("Net 25 kg", "25", "percent"), ("Yield 25%", "0.25", "EA"),
                                  ("Filled qty: 1502 EA", "1502", "kg")):
            self.assertEqual(codes(unit_gate(text, value, unit)), ["claim_unit_mismatch"])

    def test_a_page_unit_spelled_out_is_the_unit_it_names(self):
        def unit_gate(text, value, unit):
            page = {self.PAGE: dict(self.VIEW, text=text)}
            return gate(f"It reads {value}.", [self.PAGE], [
                {"value": value, "citation": self.PAGE, "field": "text", "unit": unit}],
                rows=dict(ROWS, **page))

        # Spelled-out, plural and abbreviated forms name the unit the gate knows: a
        # contradiction is refused, the same unit in any spelling is verified.
        for text, value, claimed, other in (
                ("Net weight 68,160 grams", "68160", "g", "kg"),
                ("Net weight 68 kilograms", "68", "kgs", "g"),
                ("Net weight 68 kilos", "68", "kilogram", "lb"),
                ("Gross 12 KGS", "12", "kg", "lb"),
                ("Shipped 3 tonnes", "3", "mt", "kg"),
                ("Shipped 3 metric tons", "3", "tonne", "lb"),
                ("Shipped 3 t, by sea", "3", "tons", "kg"),
                ("Packed 40 pounds", "40", "lbs", "kg"),
                ("Filled 20 litres", "20", "L", "ml"),
                ("Filled 20 liters", "20", "litre", "gal"),
                ("Filled 250 millilitres", "250", "ml", "l"),
                ("Made 400 pieces", "400", "pcs", "kg"),
                ("Made 400 each", "400", "ea", "cases"),
                ("Packed 30 cases", "30", "cs", "pallets"),
                ("Stacked 4 pallets", "4", "plt", "cases")):
            with self.subTest(text=text):
                out = unit_gate(text, value, claimed)
                self.assertTrue(out["passed"], out["failures"])
                self.assertEqual(checked(out)[0]["unit_basis"], "page_text")
                self.assertEqual(codes(unit_gate(text, value, other)), ["claim_unit_mismatch"])
        # Common abbreviations of a claim's unit name the page's unit too.
        for text, value, claimed in (
                ("Net 500 grams", "500", "gms"), ("Net 500 grams", "500", "gm"),
                ("Filled 20 litres", "20", "ltr"), ("Filled 20 litres", "20", "ltrs"),
                ("Filled 20 litres", "20", "lts"), ("Sold 6 packs", "6", "pk"),
                ("Sold 6 packs", "6", "pkg"), ("Sold 6 boxes", "6", "bx"),
                ("Shipped 5 t", "5", "MTs"), ("Net 68 kilograms", "68", "kilogramme"),
                ("Net 68 kilograms", "68", "kgm"), ("Filled 250 millilitres", "250", "mls"),
                ("Packed 40 pounds", "40", "lbm")):
            with self.subTest(text=text, claimed=claimed):
                out = unit_gate(text, value, claimed)
                self.assertTrue(out["passed"], out["failures"])
                self.assertEqual(checked(out)[0]["unit_basis"], "page_text")
        # A plural abbreviation is its singular's unit, on the page and in the claim
        # ("120 pkgs" was unverified against a claim in packs).
        for text, value, claimed, other in (
                ("Shipped 120 pkgs", "120", "pack", "box"), ("Sold 6 packs", "6", "pkgs", "bx"),
                ("Shipped 120 pks", "120", "pkg", "box"), ("Sold 6 boxes", "6", "bxs", "pk"),
                ("Stacked 4 plts", "4", "pallets", "cases"),
                ("Stacked 4 pallets", "4", "plts", "bxs")):
            with self.subTest(text=text, claimed=claimed):
                out = unit_gate(text, value, claimed)
                self.assertTrue(out["passed"], out["failures"])
                self.assertEqual(checked(out)[0]["unit_basis"], "page_text")
                self.assertEqual(codes(unit_gate(text, value, other)), ["claim_unit_mismatch"])
        # A claim's unit spelling the gate does not know is unverified, never refused:
        # only a known unit contradicts a known page unit.
        for text, value, claimed in (("Net 500 grams", "500", "grs"),
                                     ("Gross 25 kg", "25", "kilogs"),
                                     ("Filled 20 litres", "20", "litrz")):
            with self.subTest(text=text, claimed=claimed):
                out = unit_gate(text, value, claimed)
                self.assertTrue(out["passed"], out["failures"])
                self.assertEqual(checked(out)[0]["unit_basis"], "unverified")
        # A letter that only looks like a unit: inside a token, or an abbreviation's
        # letter before a period, is no unit; the claim's unit stays unverified.
        for text, value in (("PO 123 t-shirt, 7 t-shirts packed", "7"),
                            ("Packed 7 l. per line", "7"), ("Packed 7 L/min", "7"),
                            ("Packed 7 g's", "7")):
            with self.subTest(text=text):
                out = unit_gate(text, value, "kg")
                self.assertTrue(out["passed"], out["failures"])
                self.assertEqual(checked(out)[0]["unit_basis"], "unverified")
        # A case number before the figure is no unit of it.
        out = unit_gate("Moved case 12 to bay", "12", "kg")
        self.assertNotEqual(checked(out)[0].get("unit_basis"), "page_text")

    def test_the_ao_case_with_its_attached_original_passes_the_gate(self):
        from operating.tests import test_ao_originals as orig_tests
        harness = orig_tests.AoOriginalsInvestigationTest("setUp")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        server = harness.server(orig_tests.OPS_QUALITY, harness.runtime())
        call = harness.caller(server)
        out = orig_tests.investigate(call, orig_tests.PAPER, harness.originals())
        self.assertTrue(out["checks"]["original_attached"])
        verdict = call("plx.case_get", {"case_id": out["case_id"], "section": "claims"})
        self.assertTrue(verdict["passed"], verdict["failures"])
        view = harness.case_view(server, out["case_id"])
        [conflict] = [gap for gap in view["gaps"] if "shipper box damage" in gap["missing"]]
        self.assertEqual({(item.get("field"), item["value"]) for item in conflict["value_claims"]},
                         {("row.qty_damaged", "20"), ("text", "11")})


class OriginalTextQuoteTests(unittest.TestCase):
    """devloop/DECISIONS/2026-10-06-use-case-evals-text-claims.md option 1: a text value
    cited on the re-read text of an original (a captured file's or attachment's text, a
    message body, an original page) is a quote, verified verbatim on token boundaries."""

    FILE = "sharepoint://b!drive/01ITEM@%22%7Betag%7D%2C3%22#sha256=" + "a" * 64
    OTHER = "sharepoint://b!drive/02ITEM@%22%7Betag%7D%2C1%22"
    MAIL = "graph-mail://ops@example.com/AAMk1@CQAAAB"
    REFUSAL = "graph-mail://ops@example.com/AAMk2@CQAAAC"
    NOTE = "api://public.app_note/%7B%22id%22%3A1%7D@g-1"
    NAMED_ITEM = "api://public.app_item/%7B%22id%22%3A7%7D@g-1"
    PAGE = OriginalPageClaimTests.PAGE
    TEXTS = {
        FILE: {"relation": "sharepoint.driveItems", "row": {
            "content_text": "Who changed the ship date?\r\nOutcome:  the supplier moved it to"
                            "\r\nJan 9. Total 1,509 units after the re-quote (\ufb01nal)."}},
        OTHER: {"relation": "sharepoint.driveItems",
                "row": {"content_text": "Outcome: the forwarder held it until Jan 12."}},
        MAIL: {"relation": "graph-mail.messages",
               "row": {"body_text": "Approved by\u00a0Dana on Monday."}},
        REFUSAL: {"relation": "graph-mail.messages", "row": {"body_text": (
            "The re-quote was not approved. PO\u00a0held; ship date never confirmed, "
            "non\u2011conforming lot. Total 12,500.00 units.")}},
        NOTE: {"relation": "public.app_note", "row": {"text": "the supplier moved it to Jan 9."}},
        NAMED_ITEM: {"relation": "public.app_item", "row": {"name": "Lavender soap bar 500 ml"}},
        PAGE: OriginalPageClaimTests.VIEW}

    def quote(self, value, handle=FILE, field="row.content_text", cites=None, role=None):
        return gate(value, cites or [handle], [vc(value, handle, field, role)],
                    rows=dict(ROWS, **self.TEXTS))

    def test_an_exact_quote_of_the_cited_original_verifies_and_nothing_else_does(self):
        for value, handle, field in (
                ("the supplier moved it to Jan 9.", self.FILE, "row.content_text"),
                ("Outcome: the supplier moved it to Jan 9.", self.FILE, "row.content_text"),
                ("after the re-quote (final).", self.FILE, "row.content_text"),
                ("Approved by Dana", self.MAIL, "row.body_text"),
                ("P-01274 11 shipper box", self.PAGE, "text")):
            with self.subTest(value=value):
                out = self.quote(value, handle, field)
                self.assertTrue(out["passed"], out["failures"])
                self.assertEqual((checked(out)[0]["role_basis"],
                                  checked(out)[0]["quantity_type"]), ("untyped", None))
        for value, handle, field, cites in (
                ("the supplier moved it to Jan 8.", self.FILE, "row.content_text", None),
                ("the Supplier moved it to Jan 9.", self.FILE, "row.content_text", None),
                ("quote (final).", self.FILE, "row.content_text", None),
                ("Total 1,5", self.FILE, "row.content_text", None),
                ("upplier moved it", self.FILE, "row.content_text", None),
                ("01274 11 shipper box", self.PAGE, "text", None),
                # Quoted from another original, uncited or cited beside the claimed one.
                ("the forwarder held it until Jan 12.", self.FILE, "row.content_text", None),
                ("the forwarder held it until Jan 12.", self.FILE, "row.content_text",
                 [self.FILE, self.OTHER]),
                # A structured column is no text field: its value is the whole cell.
                ("Lavender soap", self.NAMED_ITEM, "row.name", None)):
            with self.subTest(value=value, handle=handle):
                self.assertEqual(codes(self.quote(value, handle, field, cites)),
                                 ["claim_value_mismatch"])
        # A record's free-text column is prose too, whatever the record (a note's text).
        self.assertTrue(self.quote("the supplier moved it", self.NOTE, "row.text")["passed"])
        # A number is a figure of the text, never the whole cell (``TextFigureTests``); on a
        # structured column it keeps the field rule. A quote is untyped: a quantity role on
        # it is never verified.
        self.assertTrue(self.quote("1509")["passed"])
        self.assertEqual(codes(self.quote("500", self.NAMED_ITEM, "row.name")),
                         ["claim_value_mismatch"])
        self.assertEqual(codes(self.quote("the supplier moved it to Jan 9.", role="damaged")),
                         ["claim_role_unverified"])
        # A number inside a quote is untyped: a type word bound to it is not verified.
        claim = [vc("Total 1,509 units", self.FILE, "row.content_text")]
        rows = dict(ROWS, **self.TEXTS)
        self.assertTrue(gate("Total 1,509 units", [self.FILE], claim, rows=rows)["passed"])
        self.assertEqual(codes(gate("Produced: Total 1,509 units", [self.FILE], claim,
                                    rows=rows)), ["claim_wording_role_unverified"])

    def test_a_claims_quote_is_checked_as_well_as_its_value_never_instead(self):
        """v6 scoreboard (``invalid_value_claim:0``): a ``quote`` beside a cited value was
        an unknown key. It is accepted and checked: it must hold the value as written and
        the cited text must hold it; a forged one refuses a claim whose value holds."""
        rows = dict(ROWS, **self.TEXTS)

        def check(value, quote, handle=self.FILE, field="row.content_text"):
            claim = dict(vc(value, handle, field), quote=quote)
            return gate(value, [handle], [claim], rows=rows)

        for value, quote, handle, field in (
                ("moved it to Jan 9.", "Outcome: the supplier moved it to Jan 9.",
                 self.FILE, "row.content_text"),
                ("P-01274", "P-01274 11 shipper box", self.PAGE, "text"),
                # Any other cell: the quote verbatim in it, a number cell that number.
                ("P-02458", "P-02458", PO_LINE, "row.code"),
                ("12500", "12500", PO_LINE, "row.quantity"),
                ("12500", "12,500.0", PO_LINE, "row.quantity")):
            with self.subTest(quote=quote):
                out = check(value, quote, handle, field)
                self.assertTrue(out["passed"], out["failures"])
                self.assertEqual(checked(out)[0]["quote_basis"], "verified")
        for value, quote, handle, field in (
                # Forged: not in the cited text (or from another original).
                ("moved it to Jan 9.", "Outcome: the forwarder moved it to Jan 9.",
                 self.FILE, "row.content_text"),
                ("moved it to Jan 9.", "the forwarder held it until Jan 12.",
                 self.FILE, "row.content_text"),
                # Real passage, but it does not hold the value (or holds it negated).
                ("moved it to Jan 9.", "Who changed the ship date?",
                 self.FILE, "row.content_text"),
                ("approved", "The re-quote was not approved.", self.REFUSAL,
                 "row.body_text"),
                ("P-01274", "11 shipper box", self.PAGE, "text"),
                ("P-02458", "P-02458 lid", PO_LINE, "row.code"),
                ("12500", "12500 EA", PO_LINE, "row.quantity"),
                ("12500", "12501", PO_LINE, "row.quantity")):
            with self.subTest(quote=quote, value=value):
                self.assertIn("claim_value_mismatch", codes(check(value, quote, handle,
                                                                   field)))
        # A quote on a computed value, or not a short string, is malformed.
        for claim in ({"value": "1503", "calculation": "C7", "quote": "1503 units"},
                      {"value": "1", "op": "count", "of": [{"citation": MO}],
                       "quote": "one"},
                      dict(vc("P-02458", PO_LINE, "row.code"), quote=""),
                      dict(vc("P-02458", PO_LINE, "row.code"), quote=["P-02458"]),
                      dict(vc("P-02458", PO_LINE, "row.code"), quote="x" * 501)):
            with self.subTest(claim=claim):
                with self.assertRaisesRegex(claim_checks.ValidationError,
                                            "invalid_value_claim_quote:0"):
                    claim_checks.parse_value_claims([claim], [MO, PO_LINE], ["C7"])

    def test_a_short_or_negated_quote_is_refused(self):
        """Directive af63ee29e91d: any token-bounded substring verified, however short
        (``approved`` out of ``not approved``, ``.``, ``PO``)."""
        for value, handle, field in (
                ("approved", self.REFUSAL, "row.body_text"),
                (".", self.REFUSAL, "row.body_text"),
                ("PO", self.REFUSAL, "row.body_text"),
                ("confirmed", self.REFUSAL, "row.body_text"),
                ("conforming lot", self.REFUSAL, "row.body_text"),
                ("shipper", self.PAGE, "text"),
                ("box", self.PAGE, "text")):
            with self.subTest(value=value, handle=handle):
                self.assertEqual(codes(self.quote(value, handle, field)),
                                 ["claim_value_mismatch"])
        # A quote that includes the negator, two words of the passage, eight characters or
        # one identifier-shaped token holds.
        for value, handle, field in (
                ("not approved", self.REFUSAL, "row.body_text"),
                ("was not approved.", self.REFUSAL, "row.body_text"),
                ("never confirmed", self.REFUSAL, "row.body_text"),
                ("PO held", self.REFUSAL, "row.body_text"),
                ("non\u2011conforming lot", self.REFUSAL, "row.body_text"),
                ("Approved", self.MAIL, "row.body_text"),
                ("P-01274", self.PAGE, "text")):
            with self.subTest(value=value):
                out = self.quote(value, handle, field)
                self.assertTrue(out["passed"], out["failures"])
        # Only the negated occurrence is refused: one un-negated occurrence supports it.
        self.assertEqual(claim_checks.supported_quotes(
            "approved", "Not approved Monday; approved Tuesday."), 1)
        # "No" after a label before an identifier is the abbreviation of number; a negating
        # answer after the passage reverses it (direction review of a5b32e19).
        for value, text, supported in (
                ("LOT-FXA", "Lot No LOT-FXA", 1), ("PO5447", "PO No PO5447", 1),
                ("PO5447", "There is no PO5447", 0), ("QC passed", "QC passed? No", 0),
                ("QC passed", "QC passed: not yet.", 0), ("QC passed", "QC passed - NO", 0),
                ("QC passed", "QC passed. No issues found.", 1),
                ("QC passed? No", "QC passed? No", 1)):
            with self.subTest(value=value, text=text):
                self.assertEqual(claim_checks.supported_quotes(value, text), supported)

    def test_a_quote_never_ends_inside_a_joined_token(self):
        for value, text in (("Jan 9", "Jan 9,2026"), ("1", "1,509"), ("re", "re-quote"),
                            ("509 units", "1,509 units"), ("30 boxes", "at 10:30 boxes"),
                            ("quote today", "re-quote today")):
            with self.subTest(value=value):
                self.assertEqual(claim_checks.quoted_spans(value, text), 0)

    def test_a_comma_between_words_separates_a_csv_field(self):
        # A CSV row: the comma between two words is a field separator, not a joiner, so a
        # field quoted whole verifies; a negator in the next field reverses nothing.
        row = ("13,Lost: rejected or expired,Portal,app_quote,status REJECTED/EXPIRED "
               "· rejected_at,no reason list,gap")
        for value, supported in (("status REJECTED/EXPIRED · rejected_at", 1),
                                 ("Lost: rejected or expired", 1),
                                 ("Status:Open", 0), ("REJECTED/EXP", 0)):
            with self.subTest(value=value):
                self.assertEqual(claim_checks.supported_quotes(value, row), supported)
        self.assertEqual(claim_checks.supported_quotes("Open item", "Status:Open item"), 1)

    def test_a_number_is_no_quote_and_a_quotes_unit_is_unverified(self):
        # The body holds 12,500.00 as a figure: a number claim of it is the figure rule
        # (``TextFigureTests``), never a quote; a quoted sentence's unit is unverified.
        out = self.quote("12500", self.REFUSAL, "row.body_text")
        self.assertTrue(out["passed"], out["failures"])
        self.assertEqual(checked(out)[0]["value_basis"], "text_figure")
        out = gate("Total 12,500.00 units", [self.REFUSAL],
                   [vc("Total 12,500.00 units", self.REFUSAL, "row.body_text", unit="EA")],
                   rows=dict(ROWS, **self.TEXTS))
        self.assertTrue(out["passed"], out["failures"])
        self.assertEqual(checked(out)[0].get("unit_basis"), "unverified")


class TextFigureTests(unittest.TestCase):
    """A number inside a record's free text (``claim_checks.text_field``: a SharePoint
    file's ``content_text``, a Mission Control task's ``description``) is cited as a figure
    of that text, the rule an original page's figure follows: saved runs refused such
    claims as "holds another value" because the whole cell was compared."""

    FILE = "sharepoint://b!drive/03ITEM@%22%7Betag%7D%2C2%22"
    TASK = "mc://live/tasks/TASK-1@abc-1"
    NOTE = "api://public.app_note/%7B%22id%22%3A2%7D@g-1"
    NAMED = "api://public.app_item/%7B%22id%22%3A7%7D@g-1"
    TEXT = ("Worked example: the bill of lading's 245 cartons only added up at 10 kits per "
            "case (the labels), not 12 (Sage). Hold 30 cases for QC and re-check in 30 days. "
            "Deposit US$10,800, a 30% deposit; freight $900. At least 40 pallets moved. "
            "Dated 2026-10-12, lot AO-26005B. The order was restated 144 units short; "
            "144 units short is final.")
    ROWS = {FILE: {"relation": "sharepoint.driveItems", "row": {"content_text": TEXT}},
            TASK: {"relation": "mission_control.tasks", "row": {"description": (
                "DVS stock check: 5 of 17 label SKUs hold superseded artwork.")}},
            NOTE: {"relation": "public.app_note", "row": {"notes": "12"}},
            NAMED: {"relation": "public.app_item", "row": {"name": "Lavender soap 500 ml"}}}

    def check(self, value, handle=FILE, field=None, rows=None, **extra):
        field = field or ("row.description" if handle == self.TASK else "row.content_text")
        said = extra.pop("said", value)
        claim = dict(vc(value, handle, field, extra.pop("role", None),
                        extra.pop("unit", None)), **extra)
        return gate(said, [handle], [claim], rows=dict(ROWS, **(rows or self.ROWS)))

    def test_a_figure_of_a_records_text_passes_untyped_and_pinned_by_a_quote(self):
        for value, handle, extra in (
                ("245", self.FILE, {}),
                ("245", self.FILE, {"quote": "the bill of lading's 245 cartons"}),
                ("5", self.TASK, {}), ("17", self.TASK, {"quote": "5 of 17 label SKUs"}),
                # One value with one meaning, written twice, is no ambiguity.
                ("144", self.FILE, {"unit": "EA"}),
                ("10800", self.FILE, {"currency": "USD"}),
                ("30", self.FILE, {"unit": "percent", "said": "30%"}),
                ("12", self.NOTE, {"field": "row.notes"})):
            with self.subTest(value=value, extra=extra):
                out = self.check(value, handle, **extra)
                self.assertTrue(out["passed"], out["failures"])
                result = checked(out)[0]
                self.assertEqual((result["role_basis"], result["quantity_type"]),
                                 ("untyped", None))
                self.assertEqual("quote_basis" in result, "quote" in extra)
        self.assertEqual(checked(self.check("245"))[0]["value_basis"], "text_figure")
        # The rendered answer never shows a text field whole as the value "as recorded".
        self.assertIsNone(answer_gate._recorded(
            vc("5", self.TASK, "row.description"), self.ROWS.get))

    def test_ambiguous_wrong_negated_bounded_or_mistyped_figures_are_refused(self):
        refused = (
            # 30 cases and 30 days: two meanings, no quote to pin one.
            ("30", {}, "claim_value_mismatch", "ambiguous"),
            ("999", {}, "claim_value_mismatch", "does not hold"),
            ("12", {}, "claim_value_mismatch", "negated"),
            ("12", {"quote": "not 12 (Sage)"}, "claim_value_mismatch", "negated"),
            ("40", {}, "claim_value_mismatch", "bound"),
            # A quote must be in the text, hold the value and tell passages apart.
            ("30", {"quote": "Hold 31 cases"}, "claim_value_mismatch", "quote"),
            ("245", {"quote": "Hold 30 cases"}, "claim_value_mismatch", "quote"),
            ("245", {"quote": "245"}, "claim_value_mismatch", "too short"),
            # A date or an identifier's digits are no figure.
            ("10", {"quote": "Dated 2026-10-12"}, "claim_value_mismatch", "quote"),
            ("26005", {}, "claim_value_mismatch", "does not hold"),
            ("144", {"unit": "kg"}, "claim_unit_mismatch", "prints"),
            ("10800", {"currency": "CAD"}, "claim_currency_mismatch", "USD"),
            ("900", {"currency": "USD"}, "claim_currency_mismatch", "bare $"),
            ("245", {"role": "produced"}, "claim_role_unverified", "role contract"))
        for value, extra, code, detail in refused:
            with self.subTest(value=value, extra=extra):
                out = self.check(value, **extra)
                self.assertEqual(codes(out), [code], out["failures"])
                self.assertIn(detail, out["failures"][0]["detail"])
        # The quote that pins one meaning passes the ambiguous value.
        out = self.check("30", quote="re-check in 30 days")
        self.assertTrue(out["passed"], out["failures"])

    def test_structured_cells_rereads_and_calculations_keep_their_rules(self):
        # A structured column is never prose: a number in it is the whole cell or nothing.
        self.assertEqual(codes(self.check("500", self.NAMED, "row.name")),
                         ["claim_value_mismatch"])
        # A field the record does not have, or a row this reader no longer reads, fails.
        self.assertEqual(codes(self.check("245", field="row.body")), ["claim_value_mismatch"])
        unread = gate("245", [self.FILE], [vc("245", self.FILE, "row.content_text")],
                      rows=dict(ROWS))
        self.assertEqual(codes(unread), ["claim_uncited"])
        # A product of two figures is no figure of the text, and a sum over text is refused.
        self.assertEqual(codes(self.check("120")), ["claim_value_mismatch"])
        summed = gate("255", [self.FILE], [{"value": "255", "op": "sum", "of": [
            {"citation": self.FILE, "field": "row.content_text"}]}],
            rows=dict(ROWS, **self.ROWS))
        self.assertFalse(summed["passed"])

    # Review of 74849da938: each form a number of prose takes that is not the claimed
    # value (all refused on main, where the whole cell was compared).
    NOT_THE_VALUE = (
        # Dates, phone numbers, identifiers and labelled numbers.
        ("14", "Ship date 10/14/2026, 3 pallets."), ("10", "Ship date 10/14/2026."),
        ("12", "Delivered Oct 12."), ("12", "Delivered 12 Oct 2026."),
        ("2026", "Dated 2026-10-12."), ("30", "Booked at 10:30 today."),
        ("905", "Call 905-555-0187."), ("555", "Call 905-555-0187."),
        ("187", "Call 905-555-0187."), ("905", "Call (905) 555-0187."),
        ("555", "Call +1 905 555 0187."),
        ("1509", "Item SKU-1509 shipped."), ("1509", "Item SKU 1509 shipped."),
        ("4521", "Invoice #4521 paid."), ("4521", "Invoice 4521 paid."),
        ("4521", "PO 4521 was raised."), ("12345", "Code ABC12345."),
        ("12345", "Code 12345."), ("5", "Lot No. 5 held."), ("30", "Bin #30 is full."),
        ("3", "See page 3 of 5."), ("2", "See line 2."), ("1", "The 1st lot."),
        ("5", "Gap 3 (stages 5, 8, 13)."), ("5", "Fix (5) first."),
        ("2", "Version 2.1 shipped."), ("26005", "AO-26005B was released."),
        # A label with a colon, number word or other delimiter (re-check of 464b1af27c).
        ("1509", "SKU: 1509 shipped."), ("4521", "PO: 4521"), ("4521", "P.O. 4521"),
        ("4521", "Invoice: 4521"), ("4521", "Order #: 4521"), ("12345", "Account: 12345"),
        ("26005", "Batch: 26005"), ("5", "Lot: 5"), ("4521", "Invoice No. 4521"),
        ("4521", "Order no: 4521"), ("4521", "Lot - 4521"), ("3301", "Quote # 3301"),
        ("7731", "Job 7731"),
        # Addresses, years and decimal commas.
        ("123", "123 Main St, Toronto"), ("200", "Suite 200"),
        ("10001", "New York, NY 10001"), ("44113", "Zip 44113"),
        ("2026", "In 2026 we moved."), ("1", "1,2 kg"), ("2", "1,2 kg"),
        # Bounds, approximations and ranges.
        ("30", "max 30 cases"), ("30", "max. 30 cases"), ("30", "maximum 30 cases"),
        ("30", "min 30 cases"), ("30", "minimum 30 cases"), ("30", "30 max"),
        ("30", "30 cases max"), ("30", "30 or less"), ("30", "30 or fewer"),
        ("30", "30 at most"), ("30", "at most 30"), ("30", "at least 30"),
        ("30", "30 or more"), ("30", "30+ cases"), ("30", "up to 30 cases"),
        ("30", "more than 30 cases"), ("30", "no more than 30 cases"),
        ("30", "under 30 cases"), ("30", "about 30 cases"), ("30", "~30 cases"),
        ("30", "≥ 30 cases"), ("20", "20-30 cases"), ("30", "20-30 cases"),
        ("30", "20 – 30 cases"), ("30", "20 to 30 cases"),
        ("20", "between 20 and 30 cases"), ("30", "between 20 and 30 cases"),
        # Negations.
        ("12", "not 12 cases"), ("30", "excluding 30 cases"), ("30", "except 30 cases"),
        ("30", "other than 30 cases"), ("30", "30 cases? No."), ("30", "30 cases: not yet."),
        # Scale words scale the figure.
        ("1.5", "US$1.5 million"), ("12", "12 thousand units"), ("3", "3 dozen jars"))

    def test_a_number_of_prose_that_is_not_the_value_supports_no_claim(self):
        for value, text in self.NOT_THE_VALUE:
            with self.subTest(value=value, text=text):
                out = self.check(value, rows={self.FILE: {
                    "relation": "sharepoint.driveItems", "row": {"content_text": text}}})
                self.assertEqual(codes(out), ["claim_value_mismatch"], out["failures"])
        for value, text, extra in (
                ("14", "14 cartons arrived.", {}), ("12", "Loaded 12 pallets.", {}),
                ("1500000", "US$1.5 million", {"currency": "USD"}),
                ("12000", "12 thousand units", {}), ("36", "3 dozen jars", {}),
                ("30", "a 30-day extension", {}), ("500", "500ml bottles", {}),
                ("1710", "USD 1,710.00 freight", {"currency": "USD"}),
                ("1509", "Total: 1,509 units", {"unit": "EA"}),
                # One value written twice with one meaning needs no quote.
                ("30", "30 cases ordered, 30 cases damaged.", {})):
            with self.subTest(value=value, text=text):
                out = self.check(value, rows={self.FILE: {
                    "relation": "sharepoint.driveItems", "row": {"content_text": text}}},
                    **extra)
                self.assertTrue(out["passed"], out["failures"])

    def test_an_all_caps_packing_list_states_its_figures(self):
        """Re-check of 464b1af27c: a capitalised quantity label before a figure (a scanned
        packing list) is no id label: the figures pass, with and without a quote, on a
        record's text and on an original page; a year counted with a unit passes too."""
        scan = ("PACKING LIST\nTOTAL QTY 1513\nCARTONS 245\nGROSS WT 1,850 KG\n"
                "QTY 1502 EA\nSHIPPED 2000 CARTONS")
        page = OriginalPageClaimTests.PAGE
        for value, extra in (("1513", {}), ("245", {}), ("1850", {"unit": "kg"}),
                             ("1502", {"unit": "EA"}), ("2000", {}),
                             ("1513", {"quote": "TOTAL QTY 1513"}),
                             ("1850", {"quote": "GROSS WT 1,850 KG"})):
            with self.subTest(value=value, extra=extra):
                out = self.check(value, rows={self.FILE: {
                    "relation": "sharepoint.driveItems", "row": {"content_text": scan}}},
                    **extra)
                self.assertTrue(out["passed"], out["failures"])
                rows = dict(ROWS, **{page: dict(OriginalPageClaimTests.VIEW, text=scan)})
                out = gate(value, [page], [dict({"value": value, "citation": page,
                                                 "field": "text"}, **extra)], rows=rows)
                self.assertTrue(out["passed"], out["failures"])
        # The refusal names what the text prints, never "None".
        out = self.check("900", currency="USD")
        self.assertIn("with $", out["failures"][0]["detail"])
        self.assertNotIn("None", out["failures"][0]["detail"])

    def test_end_to_end_a_date_or_a_bound_never_verifies_a_count(self):
        for text, said, value in (("Ship date 10/14/2026", "14 cartons arrived.", "14"),
                                  ("max 12 pallets per truck", "12 pallets were loaded.",
                                   "12")):
            with self.subTest(text=text):
                rows = dict(ROWS, **{self.FILE: {"relation": "sharepoint.driveItems",
                                                 "row": {"content_text": text}}})
                out = gate(said, [self.FILE], [vc(value, self.FILE, "row.content_text")],
                           rows=rows)
                self.assertFalse(out["passed"])
                self.assertIn("claim_value_mismatch", codes(out))

    def test_one_rule_for_an_original_pages_figures(self):
        """The page-figure rule is the text-figure rule: page figures that are dates,
        bounds, labels or negated, which a page claim passed before, are refused; the
        page's real figures still pass."""
        page = OriginalPageClaimTests.PAGE

        def on_page(text, value, said=None, **extra):
            rows = dict(ROWS, **{page: dict(OriginalPageClaimTests.VIEW, text=text)})
            return gate(said or value, [page], [dict({"value": value, "citation": page,
                                                      "field": "text"}, **extra)], rows=rows)
        for text, value in (("Ship date 10/14/2026", "14"), ("max 12 pallets", "12"),
                            ("SKU 1509 label", "1509"), ("not 12 (Sage)", "12"),
                            ("Call 905-555-0187", "555"), ("20-30 cases", "30"),
                            ("30 cases; QC in 30 days", "30")):
            with self.subTest(text=text):
                self.assertEqual(codes(on_page(text, value)), ["claim_value_mismatch"])
        for text, value, extra in (("P-01274 11 shipper box\nP-0121: 07", "11", {}),
                                   ("P-01274 11 shipper box\nP-0121: 07", "7", {}),
                                   ("30 cases; QC in 30 days", "30",
                                    {"quote": "QC in 30 days"}),
                                   ("Yield 25%", "25", {"unit": "percent", "said": "25%"})):
            with self.subTest(text=text, value=value):
                out = on_page(text, value, **extra)
                self.assertTrue(out["passed"], out["failures"])

    def test_a_heading_labels_number_is_no_figure(self):
        """main red since daad9589a3 (evals PinnedOriginalTests): "release 2:" read as a
        figure made the page's "2 pallets" ambiguous. A word and a number with a colon at a
        clause start numbers what follows; a quantity label's number still counts."""
        page = OriginalPageClaimTests.PAGE
        release = "Release 1: 10,032 lb x 2 pallets; release 2: 6,000 lb x 3."

        def on_page(text, value, **extra):
            rows = dict(ROWS, **{page: dict(OriginalPageClaimTests.VIEW, text=text)})
            return gate(value, [page], [dict({"value": value, "citation": page,
                                              "field": "text"}, **extra)], rows=rows)
        for text, value in ((release, "2"), (release, "3"), (release, "10032"),
                            ("Qty: 2", "2"), ("Step 3: pack 2 cases.", "2"),
                            ("Pallets 4: stacked", "4"), ("Received 3: ok", "3")):
            with self.subTest(text=text, value=value):
                out = on_page(text, value)
                self.assertTrue(out["passed"], out["failures"])
        self.assertEqual([figure.value for figure in claim_checks._text_figures(release)],
                         [10032, 2, 6000, 3])
        # Only at a clause start and only with the colon: elsewhere the number is a figure.
        for text, value in (("Release 1: 10,032 lb", "1"), ("Step 3: pack 2 cases", "3"),
                            ("x; item 4: none", "4"), ("Line 5: ok", "5"),
                            *((f"{word.title()} 6: ok", "6")
                              for word in claim_checks.HEADING_WORDS)):
            with self.subTest(heading=text):
                self.assertEqual(codes(on_page(text, value)), ["claim_value_mismatch"])
        for text in ("shipped release 2: done", "release 2 pallets", "Qty 2: kept",
                     "Qty: 2", "Received 2: ok", "x. Pallets 2: stacked"):
            with self.subTest(figure=text):
                self.assertIn(Decimal(2), [f.value for f in claim_checks._text_figures(text)])

    def test_n_1_10_60_figures_of_one_text_none_lost_or_slowed(self):
        for count in (1, 10, 60):
            with self.subTest(count=count):
                text = " ".join(f"Bay {n}: {1000 + n} cartons." for n in range(count)) * 40
                rows = dict(ROWS, **{self.FILE: {"relation": "sharepoint.driveItems",
                                                 "row": {"content_text": text}}})
                claims = [vc(str(1000 + n), self.FILE, "row.content_text")
                          for n in range(count)] + [vc("999", self.FILE, "row.content_text")]
                started = time.monotonic()
                out = claim_checks.check_items(
                    [{"id": f"F{n}", "texts": [], "value_claims":
                      claim_checks.parse_value_claims([claim], [self.FILE], []),
                      "citations": [{"handle": self.FILE, "status": "resolved"}]}
                     for n, claim in enumerate(claims)], rows=rows.get)
                self.assertLess(time.monotonic() - started, 5)
                self.assertEqual(out["counts"]["passed"], count)
                self.assertEqual([failure["item"] for failure in out["failures"]],
                                 [f"F{count}"])


class CustomerPoClaimTests(CustomerPoEvalBase, unittest.TestCase):
    """Review of fb5ac6b, on the generic tools: customer-PO texts on Portal SO-YYYY-####
    orders and numeric POs pass the claim gate when their numbers carry value claims
    citing the source fields, and fail it when they do not."""

    def setUp(self) -> None:
        pass  # each subtest sets up its own fixture after patching the rows

    def case_for(self, label, po, *, claims=True):
        with patch.dict(cpo_tests.ORDERS[2], {"order_number": label}), \
                patch.dict(cpo_tests.SAGE_ROWS[0], {"ponumber": po}):
            CustomerPoEvalBase.setUp(self)
            [order] = self.coverage(origins=["sage_import"],
                                    statuses=["absent_in_this_source"])["orders"]
            evidence = self.actual("mirror.rows", {
                "relation": "sage.oe_order_header", "where": [["ordnumber", "eq", "S3"]]})
            sage = evidence["rows"][0]["citation"]
            case_id = self.actual("plx.case_open", {
                "question": "Missing customer POs", "roots": [order["order_number"]]})["case_id"]
            label_claim = [{"value": label, "citation": order["citation"],
                            "field": "row.order_number"}]
            self.actual("plx.case_propose", {
                "case_id": case_id, "capability": "portal.sales_order.set_customer_po",
                "payload": {"proposed_customer_po": po, "evidence_citation": sage,
                            "evidence_field": "ponumber"},
                "citations": [sage, order["citation"]],
                "expected_effect": {"relation": cpo.RELATION, "key": order["key"],
                                    "fields": ["po_reference"], "equals": po},
                "evidence": {
                    "statement": f"The cited Sage header holds {po} in ponumber; proposed as "
                                 f"the customer PO of sales order {label}.",
                    **({"value_claims": [{"value": po, "citation": sage,
                                          "field": "row.ponumber"}] + label_claim}
                       if claims else {})}})
            for state, missing in (("absent_in_source", f"Customer PO of sales order {label}"),
                                   ("not_ingested",
                                    f"Customer PO documents for sales orders {label}")):
                self.actual("plx.case_record_gap", {
                    "case_id": case_id, "state": state, "missing": missing,
                    "why": "Every exposed PO field is empty; mail, SharePoint and BC are not "
                           "ingested.", "who_can_answer": ["PLX order entry"],
                    "citations": [order["citation"]],
                    **({"property": "SalesOrder.customer_po"}
                       if state == "absent_in_source" else {}),
                    **({"value_claims": label_claim} if claims else {})})
            return case_id

    def test_real_order_and_po_shapes_pass_the_customer_po_playbook(self):
        label = cpo_tests.ORDERS[2]["order_number"]
        for label, po in ((label, "PO-7788"), (label, "4500123456"), (label, "5447")):
            with self.subTest(label=label, po=po):
                case_id = self.case_for(label, po)
                report = pe.evaluate(CPO, self.call, case_id, transcript=self.actual.names)
                self.assertEqual(report["claim_checks"]["failures"], [])
                self.assertTrue(report["summary"]["passed_all"], report["checks"])
                self.doCleanups()

    def test_a_portal_so_year_label_in_caller_text_is_an_identifier_not_numbers(self):
        # Review of ee55df3: the "-0003" tail of a Portal SO-YYYY-#### label before a
        # full stop was read as the number -3; the label is one identifier.
        case_id = self.case_for("SO-2026-0003", "PO-7788")
        report = pe.evaluate(CPO, self.call, case_id, transcript=self.actual.names)
        check = next(c for c in report["checks"] if c["id"] == "no_uncited_numbers")
        self.assertTrue(check["passed"], check)
        self.assertEqual(report["claim_checks"]["failures"], [])
        self.assertTrue(report["summary"]["passed_all"], report["checks"])
        from operating.claim_checks import IDENTIFIER
        self.assertEqual(IDENTIFIER.findall("sales order SO-2026-0003."), ["SO-2026-0003"])
        self.assertEqual(IDENTIFIER.findall("total 1509.5"), [])

    def test_an_uncovered_number_in_caller_text_fails_the_gate(self):
        case_id = self.case_for(cpo_tests.ORDERS[2]["order_number"], "4500123456",
                                claims=False)
        report = pe.evaluate(CPO, self.call, case_id)
        self.assertIn("claim_unstructured",
                      {item["code"] for item in report["claim_checks"]["failures"]})
        self.assertFalse(report["summary"]["passed_all"])


# Directive 24e7aa78f585: BC capture rows (a capture view: the store's own citation, its
# row) and Portal receipt rows, synthetic, for the Ask's BC roles and its counts and sums.
CO = "0c5a1b2c-3d4e-4f50-8a6b-7c8d9e0f1a2b"


def bc(entity_set, ident, capture=1):
    return f"bc://production/{CO}/{entity_set}/{ident}@00000000000000a1-{capture}"


def bc_view(handle, **row):
    return {"citation": handle, "source": "bc", "kind": "bc", "key": {"id": row["id"]},
            "row": row, "lineage": "bc_manufacturing", "status": "current",
            "changed_since": False}


def receipt(index):
    return (f"api://public.app_receipt/%7B%22id%22%3A%22r-{index}%22%7D@g-{index}",
            {"relation": "public.app_receipt", "key": {"id": f"r-{index}"},
             "row": {"id": f"r-{index}", "amount": f"{index}.25"}})


RCPT_LINE = bc("purchaseReceiptLines", "rl-1")
PO_LINE_BC = bc("purchaseOrderLines", "pl-1")
BC_ROWS = {RCPT_LINE: bc_view(RCPT_LINE, id="rl-1", quantity=40, unitCost=1.5),
           PO_LINE_BC: bc_view(PO_LINE_BC, id="pl-1", quantity=50, receivedQuantity=40)}


def op(value, name, inputs, role=None, unit=None, currency=None):
    out = {"value": value, "op": name, "of": inputs}
    out.update({key: given for key, given in (("role", role), ("unit", unit),
                                              ("currency", currency)) if given})
    return out


def aggregate_count(handles, value=None, **result):
    """What ``plx.aggregate`` returned for a count measured on a property (contract shape):
    one group listing a value citation per counted record."""
    group = {"key": {}, "value": str(len(handles) if value is None else value),
             "inputs": len(handles), "markings": [],
             "inputs_citations": [{"l1_handle": h, "field_path": "row.id", "digest": "0" * 64,
                                   "lineage": "portal_native"} for h in handles]}
    out = {"groups": [group], "partial": False, "gaps": [],
           "completeness": {"complete": True, "truncated": False, "returned": 1, "total": 1},
           "meta": {}}
    out.update(result)
    return claim_checks.returned_evidence(
        "plx.aggregate", {"set": "set:s1", "measure": {"property": "receipt_number",
                                                       "op": "count"}}, out)


def money(handle, field, currency, value="1"):
    """A money value as a tool returns it (``$defs/value``)."""
    return {"property": "total_amount", "value_type": "money", "value": value,
            "currency": currency, "markings": [],
            "citation": {"l1_handle": handle, "field_path": field, "digest": "0" * 64,
                         "lineage": "portal_native"}}


def sales_order(index, amount):
    return (f"api://public.app_sales_order/%7B%22id%22%3A%22so-{index}%22%7D@g-{index}",
            {"relation": "public.app_sales_order", "key": {"id": f"so-{index}"},
             "row": {"id": f"so-{index}", "total_amount": amount}})


class AskClaimFormTests(unittest.TestCase):
    def test_a_bc_quantity_role_holds_only_when_the_cited_row_carries_it(self):
        def check(text, claim, handles=(RCPT_LINE,), rows=BC_ROWS):
            return gate(text, list(handles), [claim], rows=rows, calculations=())
        result = check("Received 40.", vc("40", RCPT_LINE, "row.quantity", "received"))
        self.assertTrue(result["passed"], result["failures"])
        self.assertEqual(checked(result)[0]["role_contract"],
                         "ontology:ReceiptLine.received_quantity")
        # The role is the cited row's: an order line's quantity is ordered, its received
        # quantity received; a scorer's re-read names the entity set as its relation.
        lines = {PO_LINE_BC: {"relation": "bc.purchaseOrderLines",
                              "row": BC_ROWS[PO_LINE_BC]["row"]}}
        self.assertTrue(check("Received 40.", vc("40", PO_LINE_BC, "row.receivedQuantity",
                                                 "received"), [PO_LINE_BC], lines)["passed"])
        for handle, field, value, role in ((RCPT_LINE, "row.quantity", "40", "ordered"),
                                           (PO_LINE_BC, "row.quantity", "50", "received")):
            with self.subTest(field=field, role=role):
                self.assertEqual(codes(check(f"{value}.", vc(value, handle, field, role),
                                             [handle])), ["claim_role_mismatch"])
        # A column the schema gives no role, and an unroled claim, verify no type word.
        self.assertEqual(codes(check("1.5.", vc("1.5", RCPT_LINE, "row.unitCost",
                                                "received"))), ["claim_role_unverified"])
        self.assertTrue(check("40 on the line.", vc("40", RCPT_LINE, "row.quantity"))["passed"])
        self.assertEqual(codes(check("Received 40.", vc("40", RCPT_LINE, "row.quantity"))),
                         ["claim_wording_role_unverified"])

    def test_a_count_is_exact_only_when_a_complete_count_entry_binds_it(self):
        """Directive 2aafc4270e73 item 2: an Ask that saw 12 receipts and cited 8 must not
        pass "8 receipts"; a count is exact only over a complete count a tool returned."""
        for size in (1, 10, 60):
            with self.subTest(size=size):
                rows = dict(receipt(index) for index in range(size))
                handles = list(rows)
                claim = op(str(size), "count", [{"citation": h} for h in handles],
                           role="receipt_count")
                # No complete count entry: a lower bound, stated only as one.
                self.assertTrue(gate(f"At least {size} receipts.", handles, [claim],
                                     rows=rows, calculations=())["passed"])
                self.assertEqual(codes(gate(f"{size} receipts.", handles, [claim],
                                            rows=rows, calculations=())),
                                 ["claim_count_lower_bound"])
                self.assertEqual(codes(gate(f"At least {size + 1} receipts.", handles,
                                            [{**claim, "value": str(size + 1)}], rows=rows,
                                            calculations=())), ["claim_value_mismatch"])
                # A complete count listing every record binds it: exact, in any wording.
                bound = gate(f"{size} receipts.", handles, [claim], rows=rows,
                             calculations=(), returned=aggregate_count(handles))
                self.assertTrue(bound["passed"], bound["failures"])
                self.assertEqual(checked(bound)[0]["bound"], "exact")
        # 12 receipts seen, 8 cited: "12" binds to the complete count of the 12, "8" does
        # not (no entry of 8), whatever the claim's own citations add up to.
        rows = dict(receipt(index) for index in range(12))
        seen, cited = list(rows), list(rows)[:8]
        counted = aggregate_count(seen)
        twelve = op("12", "count", [{"citation": h} for h in cited])
        self.assertTrue(gate("12 receipts.", cited, [twelve], rows=rows, calculations=(),
                             returned=counted)["passed"])
        self.assertEqual(codes(gate("8 receipts.", cited, [{**twelve, "value": "8"}],
                                    rows=rows, calculations=(), returned=counted)),
                         ["claim_count_lower_bound"])
        # An entry that is not complete binds nothing: partial, gapped (twins: no value),
        # truncated, or not listing every record it counts.
        twins_gap = {"state": "unknown", "reason": "cross_system_twins", "subject": "members"}
        for name, entry in (
                ("partial", aggregate_count(seen, partial=True)),
                ("gap", aggregate_count(seen, gaps=[twins_gap])),
                ("truncated", aggregate_count(seen, completeness={
                    "complete": False, "truncated": True, "returned": 1,
                    "reason": "group_cap"})),
                ("unlisted", aggregate_count(seen[:11], value=12))):
            with self.subTest(entry=name):
                self.assertEqual(codes(gate("12 receipts.", cited, [twelve], rows=rows,
                                            calculations=(), returned=entry)),
                                 ["claim_value_mismatch"])
        # The close outcome states a lower-bound count only as one, too; in words below
        # ten as well (counts per order are mostly small), never another number's word.
        handles = seen[:3]
        claim = op("3", "count", [{"citation": h} for h in handles])
        for text, outcome, failed in (
                ("At least 3 receipts.", "3 receipts arrived.", ["claim_count_lower_bound"]),
                ("Three receipts arrived.", None, ["claim_count_lower_bound"]),
                ("At least 3 receipts.", "Three receipts arrived.",
                 ["claim_count_lower_bound"]),
                ("At least three receipts arrived.", "At least three arrived.", []),
                ("At least 3 receipts; one of the receipts is late.", None, [])):
            with self.subTest(text=text, outcome=outcome):
                self.assertEqual(codes(gate(text, handles, [claim], rows=rows,
                                            calculations=(), outcome=outcome)), failed)
        # One record under two captures counts once; an unreadable or changed row fails.
        again = handles[0].replace("@g-0", "@g-9")
        twice = gate("3 receipts.", handles + [again],
                     [op("3", "count", [{"citation": h} for h in handles[1:] + [again,
                                                                               handles[0]]])],
                     rows={**rows, again: rows[handles[0]]}, calculations=())
        self.assertEqual(codes(twice), ["claim_value_mismatch"])
        self.assertIn("one record twice", twice["failures"][0]["detail"])
        hidden = {h: rows[h] for h in handles[1:]}
        self.assertEqual(codes(gate("At least 3 receipts.", handles, [claim], rows=hidden,
                                    calculations=())), ["claim_uncited"])
        moved = {**rows, handles[2]: {**rows[handles[2]], "changed_since": True}}
        self.assertEqual(codes(gate("At least 3 receipts.", handles, [claim], rows=moved,
                                    calculations=())), ["claim_changed_since"])
        # A count types no word, has no unit and cites only the item's own citations.
        self.assertEqual(codes(gate("3 or more received.", handles, [claim], rows=rows,
                                    calculations=())), ["claim_wording_role_unverified"])
        self.assertEqual(codes(gate("At least 3 receipts.", handles, [{**claim, "unit": "EA"}],
                                    rows=rows, calculations=())), ["claim_unit_mismatch"])
        for bad in ({**claim, "role": "received"}, {**claim, "field": "row.id"},
                    {**claim, "citation": handles[0]}, {**claim, "op": "max"},
                    op("3", "count", [{"citation": handles[0]}] * 2),
                    op("3", "count", [{"citation": list(rows)[5]}])):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                claim_checks.parse_value_claims([bad], handles, [])

    def test_twins_count_once(self):
        """A Portal receipt and its BC capture are one receipt held in two systems: with no
        complete count entry they count once, never as two records."""
        portal, _view = receipt(1)
        bc_twin = bc("purchaseReceipts", "pr-1")
        rows = {portal: _view, bc_twin: bc_view(bc_twin, id="pr-1")}
        both = [portal, bc_twin]
        pair = op("2", "count", [{"citation": h} for h in both])
        result = gate("At least 2 receipts.", both, [pair], rows=rows, calculations=())
        self.assertEqual(codes(result), ["claim_value_mismatch"])
        self.assertIn("one record held in two systems", result["failures"][0]["detail"])
        self.assertTrue(gate("At least 1 receipt.", both, [{**pair, "value": "1"}],
                             rows=rows, calculations=())["passed"])
        # Two records of one source are two records.
        other, other_view = receipt(2)
        rows[other] = other_view
        self.assertTrue(gate("At least 2 receipts.", [portal, other, bc_twin],
                             [op("2", "count", [{"citation": h}
                                                for h in (portal, other, bc_twin)])],
                             rows=rows, calculations=())["passed"])

    def test_a_money_sum_carries_the_currency_of_its_cited_values(self):
        """Directive 2aafc4270e73 item 3: a sum of money states the currency the tools
        returned for its values, or it is refused."""
        rows = dict(sales_order(index, f"{index}00.50") for index in (1, 2))
        handles = list(rows)
        cells = [{"citation": h, "field": "row.total_amount"} for h in handles]
        returned = {"counts": [], "currencies": {}}
        for handle in handles:
            claim_checks.returned_evidence("plx.object", {}, {"values": [
                money(handle, "row.total_amount", "USD")]}, returned)
        total = op("301.00", "sum", cells, currency="USD")
        result = gate("They total 301.00 USD.", handles, [total], rows=rows, calculations=(),
                      returned=returned)
        self.assertTrue(result["passed"], result["failures"])
        cad = {**returned["currencies"], (handles[1], "row.total_amount"): "CAD"}
        for name, claim, given in (
                # The schema types the column money: a currency-less sum is refused, with
                # or without the tools' currencies.
                ("no currency", {k: v for k, v in total.items() if k != "currency"}, returned),
                ("no currency, no tool read", {k: v for k, v in total.items()
                                               if k != "currency"}, None),
                ("another currency", {**total, "currency": "CAD"}, returned),
                ("no tool returned the currency", total, None),
                ("two currencies", total, {"counts": [], "currencies": cad})):
            with self.subTest(case=name):
                self.assertEqual(codes(gate("They total 301.00.", handles, [claim], rows=rows,
                                            calculations=(), returned=given)),
                                 ["claim_currency_mismatch"])
        # A currency over values no tool returned as money (and the schema does not type
        # money) is not verified; a quantity sum states none.
        receipts = dict(receipt(index) for index in range(2))
        amounts = op("1.50", "sum", [{"citation": h, "field": "row.amount"}
                                     for h in receipts], currency="USD")
        self.assertEqual(codes(gate("1.50.", list(receipts), [amounts], rows=receipts,
                                    calculations=())), ["claim_currency_mismatch"])
        for bad in (op("2", "count", [{"citation": handles[0]}], currency="USD"),
                    {**total, "currency": "usd"}, {**total, "currency": "US$"},
                    {"value": "so-1", "citation": handles[0], "field": "row.id",
                     "currency": "USD"}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                claim_checks.parse_value_claims([bad], handles, [])

    def test_a_sum_claim_is_recomputed_and_typed_from_the_rows_it_cites(self):
        rows = dict(receipt(index) for index in range(60))
        handles = list(rows)
        total = sum(Decimal(f"{index}.25") for index in range(60))
        claim = op(str(total), "sum", [{"citation": h, "field": "row.amount"}
                                       for h in handles], unit="USD", currency="USD")
        returned = {"counts": [], "currencies": {(h, "row.amount"): "USD" for h in handles}}
        result = gate(f"They total {total} USD.", handles, [claim], rows=rows,
                      calculations=(), returned=returned)
        self.assertTrue(result["passed"], result["failures"])
        self.assertEqual(codes(gate("They total 1.", handles, [{**claim, "value": "1"}],
                                    rows=rows, calculations=(), returned=returned)),
                         ["claim_value_mismatch"])
        mixed = op("1.25", "sum", [{"citation": handles[0], "field": "row.amount",
                                    "unit": "USD"},
                                   {"citation": handles[1], "field": "row.amount",
                                    "unit": "CAD"}])
        self.assertEqual(codes(gate("1.25.", handles, [mixed], rows=rows, calculations=())),
                         ["claim_unit_mismatch"])
        # Summed BC quantities take their type from the rows' schema roles, never the claim.
        both = [RCPT_LINE, PO_LINE_BC]
        received = op("80", "sum", [{"citation": RCPT_LINE, "field": "row.quantity"},
                                    {"citation": PO_LINE_BC, "field": "row.receivedQuantity"}],
                      role="received")
        self.assertTrue(gate("Received 80.", both, [received], rows=BC_ROWS,
                             calculations=())["passed"])
        self.assertEqual(codes(gate("80.", both, [{**received, "role": "ordered"}],
                                    rows=BC_ROWS, calculations=())), ["claim_role_mismatch"])
        mixed_roles = op("90", "sum", [{"citation": RCPT_LINE, "field": "row.quantity"},
                                       {"citation": PO_LINE_BC, "field": "row.quantity"}],
                         role="received")
        # Received plus ordered combine into no type (as a recorded calculation's do).
        self.assertEqual(codes(gate("90.", both, [mixed_roles], rows=BC_ROWS,
                                    calculations=())), ["claim_role_mismatch"])


RRO_PAGE = "original://rro#page=8"
RRO_TEXT = ("| Item | Ordered | Price |\n| --- | --- | --- |\n"
            "| BZ-DEO-20F Golden | 10,032 | 1.65 |\n| BZ-DEO-21F Whipped | 10,032 | 1.65 |\n"
            "| BZ-DEO-22F Toasted | 6,000 | 1.65 |\n| BZ-DEO-23F Golden | 6,000 | 1.65 |\n"
            "| BZ-DEO-24F Santal | 6,000 | 1.65 |\n| BZ-DEO-26F to 35F (10 SKUs) | 3,024 each "
            "| 1.65 |\n| Total | 68,304 | 68,160 | 144 |\n\nComment: 15% deposit, $18,901.08 "
            "received; 15% of $123,429.27 is $18,514.39.\n"
            "| Total open, all 1–30 days |  | 9,900.00 |\nHold 9,900.00 for review.\n")
RRO_VIEW = {"original_id": "rro", "page": 8, "text": RRO_TEXT, "text_source": "pdf"}
DATED_PAGE = "original://rro#page=10"
DATED_TEXT = ("Invoice IN1000180047, 31 Aug 2026.\nAll seven invoices are dated 31 Aug. The "
              "due date is not 30 Aug. Pick-up September 2. Statement 28/09/2026; "
              "reminder 05/08/2026.")
DATED_VIEW = {"original_id": "rro", "page": 10, "text": DATED_TEXT, "text_source": "pdf"}


def expression(value, expr, inputs, **more):
    """An expression value claim over page figures (``op: expression``)."""
    return {"value": value, "op": "expression", "expression": expr,
            "of": [{"citation": RRO_PAGE, "field": "text", **given} for given in inputs],
            **more}


class GateCorrectValueTests(unittest.TestCase):
    """v8 scoreboard (rerun cards A01, A05, A07, A08, RRO-09, PS-04, EMD-10): the gate
    refused or withheld values that the cited text supports. An expression over figures
    the gate verifies on the page is one value claim, recomputed as the scorer recomputes
    it; a printed date in another written form is that day where nothing makes it
    ambiguous; a table-row quote matches across runs of spaces and pipes; and a value that
    cannot pass says why in words a repair can act on."""

    ROWS = dict(ROWS, **{RRO_PAGE: RRO_VIEW, DATED_PAGE: DATED_VIEW})

    def page(self, claims, cites=(RRO_PAGE,), text=""):
        return gate(text, list(cites), claims, rows=self.ROWS, calculations=())

    def test_an_expression_over_verified_page_figures_is_recomputed(self):
        deposit = {"value": "18,901.08", "quote": "$18,901.08 received"}
        fifteen = {"value": "18514.39", "quote": "is $18,514.39"}
        for claim in (
                expression("386.69", "x0 - x1", [deposit, fifteen]),     # A07
                expression("16905.24", "x0 * x1 * x2", [                 # A05
                    {"value": "68,304", "quote": "| Total | 68,304 |"},
                    {"value": "1.65", "quote": "| 10,032 | 1.65 |"},
                    {"value": "15%", "quote": "15% deposit"}]),
                # A01: a figure printed twice is two inputs, each quoted where printed.
                expression("68304", "x0 + x1 + x2 + x3 + x4 + x5 * x6", [
                    {"value": "10032", "quote": "| BZ-DEO-20F Golden | 10,032 |"},
                    {"value": "10032", "quote": "| BZ-DEO-21F Whipped | 10,032 |"},
                    {"value": "6000", "quote": "BZ-DEO-22F Toasted | 6,000"},
                    {"value": "6000", "quote": "BZ-DEO-23F Golden | 6,000"},
                    {"value": "6000", "quote": "BZ-DEO-24F Santal | 6,000"},
                    {"value": "3024", "quote": "3,024 each"},
                    {"value": "10", "quote": "(10 SKUs)"}]),
                # A percent input printed as a percent, given bare under its unit.
                expression("0.25", "round_to_cents(x0 * x1)",
                           [{"value": "1.65", "quote": "| 10,032 | 1.65 |"},
                            {"value": "15", "unit": "%", "quote": "15% deposit"}])):
            with self.subTest(claim=claim["expression"]):
                out = self.page([claim])
                self.assertTrue(out["passed"], out["failures"])
                (result,) = checked(out)
                self.assertEqual((result["role_basis"], result["quantity_type"]),
                                 ("calculation", None))
        for claim, code, said in (
                (expression("386.70", "x0 - x1", [deposit, fifteen]),
                 "claim_value_mismatch", "is 386.69"),
                (expression("0.2475", "round_to_cents(x0 * x1)", [
                    {"value": "1.65", "quote": "| 10,032 | 1.65 |"},
                    {"value": "15%", "quote": "15% deposit"}]), "claim_value_mismatch", "0.25"),
                # An input the page does not print, or prints under another quote.
                (expression("386.70", "x0 - x1", [{"value": "18,901.09"}, fifteen]),
                 "claim_value_mismatch", "input x0"),
                (expression("386.69", "x0 - x1", [
                    {"value": "18,901.08", "quote": "$18,901.08 paid"}, fifteen]),
                 "claim_value_mismatch", "input x0"),
                # A bare figure under a percent unit: the page never prints it as a percent.
                (expression("1504.8", "x0 * x1", [{"value": "10,032", "unit": "%"},
                                                  {"value": "15%", "quote": "15% deposit"}]),
                 "claim_value_mismatch", "percent"),
                (expression("386.69", "x0 - x1", [deposit, fifteen], role="ordered"),
                 "claim_role_unverified", "no quantity type"),
                (expression("386.69", "x0 - x1", [deposit, fifteen], unit="kg"),
                 "claim_unit_mismatch", "kg")):
            with self.subTest(failing=claim["value"], code=code):
                out = self.page([claim])
                self.assertEqual(codes(out), [code], out["failures"])
                self.assertIn(said, out["failures"][0]["detail"])
        # The shape is refused before any read: a literal operand, an unused or unknown
        # input, an input the item does not cite, a copied input, no expression.
        for claim, code in (
                (expression("18801.08", "x0 - 100", [deposit]), "value_claim_expression_literal"),
                # Each input once (x0 / x0, x0 - x0 + x1 build a constant).
                (expression("1", "x0 / x0", [deposit]), "value_claim_expression_repeated_input"),
                (expression("18514.39", "x0 - x0 + x1", [deposit, fifteen]),
                 "value_claim_expression_repeated_input"),
                (expression("37802.16", "x0 + x0", [deposit]),
                 "value_claim_expression_repeated_input"),
                (expression("18901.08", "x0", [deposit, fifteen]),
                 "value_claim_expression_unused_input"),
                (expression("18901.08", "x0 + y", [deposit]), "value_claim_expression_name"),
                (expression("37802.16", "x0 + x1", [deposit, deposit]),
                 "value_claim_duplicate_input"),
                (dict(expression("1", "x0", [deposit]), expression=""),
                 "invalid_value_claim_expression"),
                ({**expression("1", "x0", [deposit]), "of": [{**deposit, "citation": PAGE_OTHER,
                                                              "field": "text"}]},
                 "value_claim_citation_not_cited"),
                ({"value": "1", "citation": RRO_PAGE, "field": "text", "expression": "x0"},
                 "invalid_value_claim")):
            with self.subTest(refused=code):
                with self.assertRaises(ValidationError) as raised:
                    self.page([claim])
                self.assertTrue(str(raised.exception).startswith(code), str(raised.exception))

    def test_a_markdown_heading_run_is_no_number_label(self):
        # A05's page: "Stages 4–6\n\n### 15% deposit" read "#" as the number sign of
        # "# 15", so "15% deposit" was refused in the gate and the scorer alike.
        page = "original://map#page=8"
        text = ("cancel the BC order as well.\n\nStages 4–6\n\n### 15% deposit, "
                "recorded as a comment\n\nShelf # 4521 holds it. Box #77 too.")
        rows = {page: {"original_id": "map", "page": 8, "text": text, "text_source": "pdf"}}

        def figure(value, quote=None, unit=None):
            claim = {"value": value, "citation": page, "field": "text",
                     **({"quote": quote} if quote else {}), **({"unit": unit} if unit else {})}
            return gate("", [page], [claim], rows=rows, calculations=())
        self.assertTrue(figure("15", "15% deposit", "percent")["passed"])
        self.assertTrue(figure("15", "### 15% deposit", "percent")["passed"])
        # One "#" still names a number: never a quantity.
        for value in ("4521", "77"):
            with self.subTest(value=value):
                self.assertEqual(codes(figure(value)), ["claim_value_mismatch"])

    def test_n_1_10_60_expression_inputs_none_lost_or_slowed(self):
        rows = "".join(f"| Lot {n} | {100 + n} kits |\n" for n in range(60))
        view = {"original_id": "lots", "page": 1, "text": rows, "text_source": "pdf"}
        page = "original://lots#page=1"
        for count in (1, 10, 60):
            inputs = [{"citation": page, "field": "text", "value": str(100 + n),
                       "quote": f"| Lot {n} | {100 + n} kits |"} for n in range(count)]
            claim = {"value": str(sum(100 + n for n in range(count))), "op": "expression",
                     "expression": " + ".join(f"x{n}" for n in range(count)), "of": inputs}
            started = time.monotonic()
            out = gate("", [page], [claim], rows={page: view}, calculations=())
            self.assertTrue(out["passed"], (count, out["failures"]))
            wrong = dict(claim, value=str(int(claim["value"]) + 1))
            self.assertEqual(codes(gate("", [page], [wrong], rows={page: view},
                                        calculations=())), ["claim_value_mismatch"])
            self.assertLess(time.monotonic() - started, 5.0)

    def test_a_printed_date_states_the_iso_day_only_where_nothing_is_ambiguous(self):
        def dated(value, quote=None, view=DATED_VIEW):
            claim = {"value": value, "citation": DATED_PAGE, "field": "text",
                     **({"quote": quote} if quote else {})}
            return gate("", [DATED_PAGE], [claim], rows={DATED_PAGE: view}, calculations=())
        for value, quote in (("2026-08-31", None),        # RRO-09: "dated 31 Aug"
                             ("2026-08-31", "All seven invoices are dated 31 Aug."),
                             ("2026-08-31", "Invoice IN1000180047, 31 Aug 2026"),
                             ("2026-09-02", "Pick-up September 2"),
                             ("2026-09-28", "Statement 28/09/2026")):
            with self.subTest(value=value, quote=quote):
                out = dated(value, quote)
                self.assertTrue(out["passed"], out["failures"])
        for value, quote, view in (
                ("2026-08-30", None, DATED_VIEW),            # printed only negated
                ("2026-09-01", None, DATED_VIEW),            # not printed
                ("2025-08-31", None, DATED_VIEW),            # another year
                ("2026-08-05", None, DATED_VIEW),            # 05/08: day and month swap
                ("2026-05-08", None, DATED_VIEW),
                ("2026-08-31", "All seven invoices are dated 31 Aug.",   # the page prints
                 dict(DATED_VIEW, text=DATED_TEXT.replace("31 Aug 2026", "1 Sep 2025")),
                 ),                                          # two years: no year implied
                ("2026-08-30", "The due date is not 30 Aug", DATED_VIEW)):
            with self.subTest(refused=value, quote=quote):
                self.assertEqual(codes(dated(value, quote, view)), ["claim_value_mismatch"])
        # A record's free text follows the same rule.
        note = "sharepoint://b!drive/04ITEM@%22%7Betag%7D%2C1%22"
        rows = {note: {"relation": "sharepoint.driveItems", "row": {"content_text": DATED_TEXT}}}
        self.assertTrue(gate("", [note], [vc("2026-08-31", note, "row.content_text")],
                             rows=rows, calculations=())["passed"])
        self.assertEqual(codes(gate("", [note], [vc("2026-08-05", note, "row.content_text")],
                                    rows=rows, calculations=())), ["claim_value_mismatch"])

    def test_a_printed_day_without_its_year_holds_only_under_one_year_of_the_text(self):
        """Review of 1cbae4da8d (rv-gate attack2/attack3): "31 Aug" took its year from the
        agent's quote or from a text printing another year in a form the gate did not
        read (25, '25, FY2025). A printed day without its year holds only when no year in
        any form follows it and the whole cited text prints exactly one year, the claim's."""
        page = "original://doc#page=1"
        note = "sharepoint://b!drive/04ITEM@%22%7Betag%7D%2C1%22"

        def dated(text, value, quote=None, on_note=False):
            handle = note if on_note else page
            rows = {note: {"relation": "sharepoint.driveItems", "row": {"content_text": text}},
                    page: {"original_id": "doc", "page": 1, "text": text, "text_source": "pdf"}}
            claim = {"value": value, "citation": handle,
                     "field": "row.content_text" if on_note else "text",
                     **({"quote": quote} if quote else {})}
            return gate("", [handle], [claim], rows=rows, calculations=())
        for text, value, quote, on_note in (
                ("Invoice dated 31 Aug 25. Budget 2026.", "2026-08-31", None, False),
                ("Invoice dated 31 Aug 25.", "2025-08-31", None, False),
                ("Invoice dated 31 Aug '25. Budget 2026.", "2026-08-31", None, False),
                ("FY2025 close: invoices dated 31 Aug. Plan 2026 next.", "2026-08-31",
                 "invoices dated 31 Aug. Plan 2026", False),
                ("Year 2025 close: invoices dated 31 Aug. Plan for 2026 next.", "2026-08-31",
                 "invoices dated 31 Aug. Plan for 2026", False),
                ("FY2025 invoices dated 31 Aug. Printed 2026.", "2026-08-31", None, False),
                ("FY25 invoices dated 31 Aug. Printed 2026.", "2026-08-31", None, False),
                ("Invoices dated 31 Aug.", "2026-08-31", None, False),
                ("Paid 31 Aug 2025. Next due 31 Aug.", "2026-08-31", None, False),
                ("Ship 2025-08-31. Also 31 Aug.", "2026-08-31", None, False),
                ("Inv 31 Aug 25 | Printed 01 Sep 2026", "2026-08-31", None, False),
                ("Paid 31/08/25. Due 31 Aug. Printed 2026.", "2026-08-31", None, False),
                ("dated 31 Aug 25; 2026 plan", "2026-08-31", None, True),
                ("Statement 02/03/2026.", "2026-03-02", None, False),
                ("Statement 02/03/2026.", "2026-02-03", None, False)):
            with self.subTest(refused=text, value=value):
                out = dated(text, value, quote, on_note)
                self.assertEqual(codes(out), ["claim_value_mismatch"])
        self.assertIn("no unambiguous year", dated("Invoices dated 31 Aug 25. 2026.",
                                                   "2026-08-31")["failures"][0]["detail"])
        for text, value, quote in (
                ("Invoice 31 Aug 2025", "2025-08-31", None),
                ("Statement 13/02/2026.", "2026-02-13", None),
                ("Last year's invoices were dated 31 Aug. Report printed 2026.", "2026-08-31",
                 None),
                ("Meeting Aug 31 10:00 2026", "2026-08-31", None),
                # RRO page 10: the next day of a list is no two-digit year.
                ("Emails of 18 Aug, 31 Aug and 2 Sep 2026.", "2026-08-31", None),
                ("Emails of 31 Aug, 25 Aug and 2 Sep 2026.", "2026-08-31", None),
                # A trailing dot after the month: the day is "31 Aug", inside the quote.
                ("Invoices dated 31 Aug. Printed 2026.", "2026-08-31", "dated 31 Aug"),
                ("Invoices dated 31 Aug. Printed 2026.", "2026-08-31", "dated 31 Aug.")):
            with self.subTest(passes=text, quote=quote):
                out = dated(text, value, quote)
                self.assertTrue(out["passed"], out["failures"])
        # The day printed elsewhere than the quote: the detail names the date form.
        out = dated("Invoices dated 31 Aug. Printed 2026.", "2026-08-31", "Printed 2026")
        self.assertIn("the quote holds no printed form of the day", out["failures"][0]["detail"])

    def test_an_expression_states_a_figure_of_every_input_in_one_currency_and_unit(self):
        # Review of 1cbae4da8d and 1bdaaf699d (rv-gate attack5 c1, c2, c7): CAD 100 + USD 50
        # is an untyped 150; a bare $ beside a named currency, and 10 kg - 500 g., too.
        page = "original://mix#page=1"
        text = ("Freight CAD 100 paid. Duty USD 50 paid. Deposit $40 held. Fee 30 kg box. "
                "Tare 12 lb box. Count 6 cartons. Rate 2.50 per carton. Charge US$70 due. "
                "Bond $25 held. Gross 10 kg. Tare 500 g.")
        rows = {page: {"original_id": "mix", "page": 1, "text": text, "text_source": "pdf"}}

        def computed(value, expr, *figures):
            claim = {"value": value, "op": "expression", "expression": expr, "of": [
                {"citation": page, "field": "text", "value": figure} for figure in figures]}
            return gate("", [page], [claim], rows=rows, calculations=())
        for value, expr, figures, said in (
                ("150", "x0 + x1", ("100", "50"), "expression_mixed_units"),
                ("90", "x0 + x1", ("50", "40"), "expression_mixed_units"),     # USD and $
                ("140", "x0 + x1", ("100", "40"), "expression_mixed_units"),   # CAD and $
                ("110", "x0 + x1", ("40", "70"), "expression_mixed_units"),    # $ and US$
                ("-490", "x0 - x1", ("10", "500"), "expression_mixed_units"),  # kg, "g."
                ("42", "x0 + x1", ("30", "12"), "expression_mixed_units"),
                ("5000", "x0 * x1", ("100", "50"), "expression_mixed_units")):
            with self.subTest(refused=expr, figures=figures):
                out = computed(value, expr, *figures)
                self.assertEqual(codes(out)[:1], ["claim_unit_mismatch"], out)
                self.assertIn(said, out["failures"][0]["detail"])
        for value, expr, figures in (("65", "x0 + x1", ("40", "25")),     # two bare $
                                     ("15", "x0 * x1", ("6", "2.50")),    # cartons by rate
                                     ("5000", "x0 * x1", ("10", "500"))):  # kg by g
            with self.subTest(passes=expr, figures=figures):
                out = computed(value, expr, *figures)
                self.assertTrue(out["passed"], out["failures"])

    def test_two_inputs_on_one_printed_figure_are_one_input(self):
        """Re-check of a04a19a551 (attack8 D1-D3, D6): once-only by name let one figure
        enter twice under two names, quoted and not, or written 1,000 and 1000."""
        page = "original://once#page=1"
        rows = {page: {"original_id": "once", "page": 1, "text": "Fee $100.00 due. Tax $5.00. "
                       "Fee 1,000 net.", "text_source": "pdf"}}

        def computed(value, expr, *inputs):
            claim = {"value": value, "op": "expression", "expression": expr, "of": [
                {"citation": page, "field": "text", **given} for given in inputs]}
            return gate("", [page], [claim], rows=rows, calculations=())
        for value, expr, inputs in (
                ("1", "x0 / x1", ({"value": "100.00"},
                                  {"value": "100.00", "quote": "Fee $100.00"})),
                ("1", "x0 / x1", ({"value": "100.00", "quote": "$100.00 due"},
                                  {"value": "100.00", "quote": "Fee $100.00"})),
                ("0", "x0 - x1", ({"value": "1,000"}, {"value": "1000"})),
                ("5", "x0 - x1 + x2", ({"value": "100.00"},
                                       {"value": "100.00", "quote": "Fee $100.00"},
                                       {"value": "5.00"}))):
            with self.subTest(inputs=inputs):
                out = computed(value, expr, *inputs)
                self.assertEqual(codes(out), ["claim_value_mismatch"], out)
                self.assertIn("value_claim_expression_repeated_input",
                              out["failures"][0]["detail"])
        self.assertTrue(computed("105", "x0 + x1", {"value": "100.00"},
                                 {"value": "5.00"})["passed"])

    def test_a_refusing_word_reaches_across_one_line_break_a_meaning_word_never(self):
        """Review of 1bdaaf699d (rv-gate attack5 b3, b4, b10, b11): line scope dropped the
        negator, bound or range on the line before the figure. Line scope only narrows
        ambiguity: a word that refuses or bounds a figure still reaches one line back."""
        page = "original://wrap#page=1"

        def figure(text, value):
            rows = {page: {"original_id": "wrap", "page": 1, "text": text,
                           "text_source": "pdf"}}
            return gate("", [page], [{"value": value, "citation": page, "field": "text"}],
                        rows=rows, calculations=())
        for text, value in (("Shipped quantity is not\n900 kits", "900"),
                            ("At least\n900 kits", "900"),
                            ("Qty 20 -\n30 units", "30"),
                            ("Qty between 20 and\n30 units", "30"),
                            ("Shipped quantity is not\r\n900 kits", "900"),
                            # Re-check of a04a19a551 (attack8 L3, L6, L9): the words after
                            # a figure that refuse or bound it reach one line on too.
                            ("Lead time 20\nto 30 days", "20"),
                            ("Qty 900 -\n1,000 units", "900"),
                            ("Load 900\nmax", "900"),
                            ("Order 900\nor more units", "900"),
                            ("Received 900\nNo.", "900")):
            with self.subTest(refused=text):
                self.assertEqual(codes(figure(text, value)), ["claim_value_mismatch"])
        # Two lines back is another sentence; a meaning word stays on its own line.
        for text, value in (("Shipped quantity is not\nlate.\nReceived 900 kits", "900"),
                            ("Ordered 5,984\nSTAND by. Kept 5,984.", "5984"),
                            ("Received 900\nkits.\nNot shipped yet", "900"),
                            # As on one line, "not shipped" answers nothing about 900.
                            ("Received 900\nnot shipped", "900")):
            with self.subTest(passes=text):
                out = figure(text, value)
                self.assertTrue(out["passed"], out["failures"])

    def test_a_table_row_quote_matches_across_runs_of_spaces_and_pipes(self):
        def quoted(quote, value="9900"):
            return self.page([{"value": value, "citation": RRO_PAGE, "field": "text",
                               "quote": quote}])
        # A08: the agent's row had one empty cell more than the page prints.
        for quote in ("Total open, all 1–30 days |  |  | 9,900.00",
                      "| Total open, all 1–30 days | 9,900.00 |",
                      "Total open, all 1–30 days\t|\t\t| 9,900.00"):
            with self.subTest(quote=quote):
                out = quoted(quote)
                self.assertTrue(out["passed"], out["failures"])
        for quote in ("Total open all 1–30 days | 9,900.00",       # a word differs
                      "Total open, all 1–30 days | 9,900.00 | 0.00",
                      "Total open, all 1–30 days 9,900.00"):        # no cell break
            with self.subTest(refused=quote):
                self.assertEqual(codes(quoted(quote)), ["claim_value_mismatch"])
        # The passage the quote matched, as the page writes it.
        self.assertEqual(claim_checks.verbatim_quote(
            "Total open, all 1\u201330 days |  |  | 9,900.00", RRO_TEXT),
            "Total open, all 1\u201330 days |  | 9,900.00")
        self.assertIsNone(claim_checks.verbatim_quote("Total open | 9,901.00", RRO_TEXT))
        # Bare, 9900 has two meanings on the page: the quote still pins one.
        self.assertIn("ambiguous", self.page([{"value": "9900", "citation": RRO_PAGE,
                                               "field": "text"}])["failures"][0]["detail"])

    def test_a_value_that_cannot_pass_says_why_in_repairable_words(self):
        # PS-04: a passage over 200 characters as the value.
        long = "A new packaging component is approved only when " + "x " * 100
        with self.assertRaises(ValidationError) as raised:
            self.page([{"value": long, "citation": RRO_PAGE, "field": "text"}])
        self.assertTrue(str(raised.exception).startswith("value_claim_value_too_long:0"))
        # EMD-10: a value longer than the quote meant to hold it.
        text = ("a waiver releases an expired lot to one batch without a new date. With "
                "Strict Expiration Posting, BC refuses consumption of that lot.")
        note = "sharepoint://b!drive/05ITEM@%22%7Betag%7D%2C1%22"
        rows = {note: {"relation": "sharepoint.driveItems", "row": {"content_text": text}}}
        claim = dict(vc(text, note, "row.content_text"),
                     quote="a waiver releases an expired lot to one batch without a new date")
        out = gate("", [note], [claim], rows=rows, calculations=())
        self.assertEqual(codes(out), ["claim_value_mismatch"])
        self.assertIn("is not inside its own quote", out["failures"][0]["detail"])
        self.assertLess(len(out["failures"][0]["detail"]), 400)


PAGE_OTHER = "original://other#page=1"


if __name__ == "__main__":
    unittest.main()
