"""Source unit contracts on case calculations (isolated fixtures, injected transport).

Evidence label: ``isolated-fixture`` / ``injected-transport``. No output here is
live Portal data; the built-in contract's live presence is a queued live-verify.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ontology.core import ValidationError
from operating.case_runtime import CaseRuntime
from operating.cases import CaseStore, QuantityRoleMismatch, UnitMismatch
from operating.source_units import SCHEMA, SourceUnitContracts
from operating.tests.test_cases import ENG_OPS, FakeResolver
from operating.tests.test_mcp_case_tools import OPS, CaseToolsTestCase

FORMULA = "api://silver.formula_items/%7B%22id%22%3A7%7D@g1-3"
FORMULA_RUNS = "api://silver.formula_items/%7B%22id%22%3A8%7D@g1-3"
COST = "api://silver.formula_costs/%7B%22id%22%3A7%7D@g1-3"
ORIGINAL = "original://inbox/formula-card.pdf@sha256-abc"


def contract(**overrides) -> dict:
    entry = {"id": "fi-pct", "relation": "silver.formula_items", "column": "percentage",
             "unit": "1", "meaning": "0-1 fraction", "evidence": ["pinned upstream file"],
             "verification": "fixture"}
    entry.update(overrides)
    return entry


def document(*entries) -> dict:
    return {"schema": SCHEMA, "contracts": list(entries) or [contract()]}


class ContractFileValidation(unittest.TestCase):
    def test_builtin_contract_pins_formula_percentage_as_fraction(self):
        contracts = SourceUnitContracts.builtin()
        found = contracts.lookup(FORMULA, {"relation": "silver.formula_items"},
                                 "row.percentage")
        self.assertEqual(found["unit"], "1")
        self.assertTrue(found["evidence"])
        # The live catalog (2026-10-01) does not serve silver.formula_items: the contract is
        # marked unverified so nothing treats it as live-verified.
        self.assertTrue(found["verification"].startswith(
            "unverified: relation absent from live catalog (2026-10-01)"), found["verification"])
        self.assertTrue(any("not among the 93" in item for item in found["evidence"]))

    def test_invalid_documents_are_refused(self):
        cases = {
            "unit_contracts_schema_mismatch": {"schema": "other", "contracts": []},
            "unit_contract_invalid_unit": document(contract(unit="per cent")),
            "unit_contract_evidence_required": document(contract(evidence=[])),
            "unit_contract_duplicate_column": document(contract(), contract(id="fi-pct-2")),
            "unit_contract_duplicate_id": document(contract(), contract(column="other")),
            "unit_contract_invalid_relation": document(contract(relation="a.b.c")),
            "unit_contract_invalid_column": document(contract(column="row.percentage")),
            "unit_contract_invalid_id": document(contract(id="")),
            "unit_contracts_unknown_key": {**document(), "contract": []},
            "unit_contract_unknown_key": document(contract(verfication="typo")),
            "unit_contract_invalid_meaning": document(contract(meaning=" ")),
            "unit_contract_invalid_verification": document(
                {k: v for k, v in contract().items() if k != "verification"}),
        }
        for code, doc in cases.items():
            with self.subTest(code=code):
                with self.assertRaises(ValidationError) as caught:
                    SourceUnitContracts(doc)
                self.assertTrue(str(caught.exception).startswith(code), str(caught.exception))

    def test_unreadable_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "unit_contracts_unreadable"):
                SourceUnitContracts.from_file(path)

    def test_lookup_matches_top_level_columns_of_api_citations_only(self):
        contracts = SourceUnitContracts(document())
        view = {"relation": "silver.formula_items"}
        self.assertIsNotNone(contracts.lookup(FORMULA, view, "row.percentage"))
        self.assertIsNotNone(contracts.lookup(FORMULA, view, "rows.1.percentage"))
        # The api view also carries key columns under "key": a contracted key
        # column read from there is checked too.
        self.assertIsNotNone(contracts.lookup(FORMULA, view, "key.percentage"))
        for field in ("row.percentage.value", "row.custom_fields.percentage", "percentage",
                      "rows.01.percentage", "row.percentage2"):  # nested or non-canonical: no contract
            self.assertIsNone(contracts.lookup(FORMULA, view, field), field)
        # The relation is taken from the resolved view, never from the handle text.
        self.assertIsNone(contracts.lookup(FORMULA, {"relation": "silver.other"},
                                           "row.percentage"))
        self.assertIsNone(contracts.lookup(FORMULA, {}, "row.percentage"))
        self.assertIsNone(contracts.lookup(ORIGINAL, view, "row.percentage"))
        # Callers get a copy; mutating it cannot change the contract.
        found = contracts.lookup(FORMULA, view, "row.percentage")
        found["unit"] = "percent"
        found["evidence"].append("x")
        again = contracts.lookup(FORMULA, view, "row.percentage")
        self.assertEqual((again["unit"], again["evidence"]), ("1", ["pinned upstream file"]))


class ComputeWithContracts(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.resolver = FakeResolver()
        self.resolver.add(FORMULA, {"relation": "silver.formula_items",
                                    "row": {"percentage": "0.125", "grams": "250"}}, {"eng"})
        self.resolver.add(FORMULA_RUNS, {"relation": "silver.formula_items",
                                         "rows": [{"percentage": "0.5"},
                                                  {"percentage": "0.25"}]}, {"eng"})
        self.resolver.add(COST, {"relation": "silver.formula_costs",
                                 "row": {"cost": "3.20"}}, {"eng"})
        self.store = CaseStore(Path(tmp.name) / "cases.db", citation_resolver=self.resolver,
                               unit_contracts=SourceUnitContracts(document()))
        self.case_id = self.store.open(ENG_OPS, "What share of the formula is item 7?")

    def cite(self, field: str, unit: str | None = None, handle: str = FORMULA) -> dict:
        spec = {"citation": handle, "field": field}
        if unit is not None:
            spec["unit"] = unit
        return spec

    def calculations(self) -> list:
        return self.store.get(ENG_OPS, self.case_id)["calculations"]

    def test_fraction_declared_as_percent_is_refused_and_nothing_recorded(self):
        with self.assertRaises(UnitMismatch) as caught:
            self.store.compute(ENG_OPS, self.case_id,
                               {"share": self.cite("row.percentage", "percent")},
                               "share", "percent")
        message = str(caught.exception)
        self.assertIn("unit_contradicts_source_contract:share", message)
        self.assertIn("fi-pct", message)
        self.assertEqual(self.calculations(), [])

    def test_contract_unit_fills_an_omitted_unit_and_is_recorded_as_its_basis(self):
        receipt = self.store.compute(ENG_OPS, self.case_id,
                                     {"share": self.cite("row.percentage"),
                                      "hundred": {"literal": "100", "unit": "percent"}},
                                     "share * hundred", "percent")
        self.assertEqual((receipt["result"], receipt["unit"]), ("12.5", "percent"))
        digest = receipt["input_units"]["share"].pop("unit_contract_digest")
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(receipt["input_units"], {
            "share": {"unit": "1", "unit_basis": "source_contract", "unit_contract": "fi-pct"},
            "hundred": {"unit": "percent", "unit_basis": "caller_declared"}})
        inputs = self.calculations()[0]["inputs"]
        self.assertEqual(inputs["share"]["unit_contract_digest"], digest)
        self.assertEqual((inputs["share"]["unit"], inputs["share"]["unit_basis"],
                          inputs["share"]["unit_contract"]), ("1", "source_contract", "fi-pct"))
        self.assertEqual(inputs["hundred"]["unit_basis"], "caller_declared")

    def test_matching_declared_unit_is_accepted_and_multi_run_rows_are_covered(self):
        receipt = self.store.compute(ENG_OPS, self.case_id,
                                     {"a": self.cite("rows.0.percentage", "1", FORMULA_RUNS),
                                      "b": self.cite("rows.1.percentage", None, FORMULA_RUNS)},
                                     "a + b", "1")
        self.assertEqual(receipt["result"], "0.75")
        self.assertEqual({v["unit_basis"] for v in receipt["input_units"].values()},
                         {"source_contract"})
        with self.assertRaisesRegex(UnitMismatch, "unit_contradicts_source_contract:b"):
            self.store.compute(ENG_OPS, self.case_id,
                               {"b": self.cite("rows.1.percentage", "percent", FORMULA_RUNS)},
                               "b", "percent")

    def test_key_copy_of_a_contracted_column_is_checked(self):
        self.resolver.add(FORMULA, {"relation": "silver.formula_items",
                                    "key": {"percentage": "0.125"},
                                    "row": {"percentage": "0.125"}}, {"eng"})
        with self.assertRaisesRegex(UnitMismatch, "unit_contradicts_source_contract:k"):
            self.store.compute(ENG_OPS, self.case_id,
                               {"k": self.cite("key.percentage", "percent")}, "k", "percent")
        self.assertEqual(self.calculations(), [])

    def test_non_canonical_row_indexes_cannot_bypass_the_contract(self):
        # rows.01 / rows.00 / non-ASCII digits read the
        # contracted cell while the contract lookup did not match the path.
        for field in ("rows.01.percentage", "rows.00.percentage", "rows.\u0661.percentage",
                      "rows.\u00b9.percentage", "rows.+1.percentage", "rows. 1.percentage"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValidationError, "field_not_found"):
                    self.store.compute(ENG_OPS, self.case_id,
                                       {"x": self.cite(field, "percent", FORMULA_RUNS)},
                                       "x", "percent")
        self.assertEqual(self.calculations(), [])
        # The canonical spellings still read, under the contract.
        receipt = self.store.compute(ENG_OPS, self.case_id,
                                     {"x": self.cite("rows.0.percentage", None, FORMULA_RUNS)},
                                     "x", "1")
        self.assertEqual((receipt["result"], receipt["input_units"]["x"]["unit_basis"]),
                         ("0.5", "source_contract"))

    def test_uncontracted_columns_stay_caller_declared_and_still_need_a_unit(self):
        receipt = self.store.compute(ENG_OPS, self.case_id,
                                     {"grams": self.cite("row.grams", "g")}, "grams", "g")
        self.assertEqual(receipt["input_units"]["grams"],
                         {"unit": "g", "unit_basis": "caller_declared"})
        with self.assertRaisesRegex(ValidationError, "unit_required"):
            self.store.compute(ENG_OPS, self.case_id,
                               {"cost": self.cite("row.cost", None, COST)}, "cost", "USD")
        with self.assertRaisesRegex(ValidationError, "unit_required"):
            self.store.compute(ENG_OPS, self.case_id, {"x": {"literal": "1"}}, "x", "1")

    def test_a_contract_does_not_bypass_the_declared_result_unit(self):
        # Adding a fraction to a percent is a unit mismatch, not a silent sum.
        with self.assertRaises(UnitMismatch):
            self.store.compute(ENG_OPS, self.case_id,
                               {"share": self.cite("row.percentage"),
                                "pct": {"literal": "12.5", "unit": "percent"}},
                               "share + pct", "percent")

    def test_store_without_contracts_keeps_caller_declared_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CaseStore(Path(tmp) / "c.db", citation_resolver=self.resolver)
            case_id = store.open(ENG_OPS, "q")
            receipt = store.compute(ENG_OPS, case_id,
                                    {"share": self.cite("row.percentage", "percent")},
                                    "share", "percent")
            self.assertEqual(receipt["input_units"]["share"]["unit_basis"], "caller_declared")
            with self.assertRaisesRegex(ValidationError, "unit_required"):
                store.compute(ENG_OPS, case_id, {"share": self.cite("row.percentage")},
                              "share", "1")

    def test_calculations_recorded_before_unit_basis_existed_still_read(self):
        # An event written by the previous code: inputs carry no unit_basis.
        digest = self.store.compute(ENG_OPS, self.case_id, {"g": self.cite("row.grams", "g")},
                                    "g", "g")
        recorded = self.calculations()[0]["inputs"]["g"]["citation"]["digest"]
        legacy = {"calc_id": "C2", "expression": "g", "result": "250", "unit": "g",
                  "inexact": False,
                  "inputs": {"g": {"kind": "citation", "field": "row.grams",
                                   "citation": {"handle": FORMULA, "digest": recorded},
                                   "value": "250", "unit": "g"}}}
        self.store._mutate(ENG_OPS, self.case_id, "calculation.recorded", lambda state: legacy)
        calcs = self.calculations()
        self.assertEqual(digest["result"], "250")
        self.assertEqual(len(calcs), 2)
        self.assertNotIn("unit_basis", calcs[1]["inputs"]["g"])
        self.assertEqual(calcs[1]["inputs_status"], "resolved")
        self.assertTrue(self.store.verify_chain(ENG_OPS, self.case_id))


ITEM = "api://silver.assembly_order_items/%7B%22id%22%3A1%7D@g1-3"
LINK = "api://public.order_entry_item_assembly/%7B%22id%22%3A2%7D@g1-3"
TASK = "api://silver.production_tasks/%7B%22id%22%3A3%7D@g1-3"
PRICE = "api://public.app_purchase_order_line/%7B%22id%22%3A4%7D@g1-3"


class QuantityRoles(unittest.TestCase):
    """Planned, actual, manufactured and filled are different quantities of one
    dimension: never combined without a declared relation (builtin role contracts)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.resolver = FakeResolver()
        self.resolver.add(ITEM, {"relation": "silver.assembly_order_items",
                                 "row": {"quantity": "1509.0", "actual_amount": "1503.0"}},
                          {"eng"})
        self.resolver.add(LINK, {"relation": "public.order_entry_item_assembly",
                                 "row": {"qtymfg_c": "1503.0"}}, {"eng"})
        self.resolver.add(TASK, {"relation": "silver.production_tasks",
                                 # Not a Filling task: a Filling task's qty is pinned
                                 # "filled" by a conditional contract, and these tests
                                 # need a cited quantity with no contract.
                                 "row": {"qty": "1502.0", "task_type": "Rework"}}, {"eng"})
        self.resolver.add(PRICE, {"relation": "public.app_purchase_order_line",
                                  "row": {"unit_price": "0.802"}}, {"eng"})
        self.store = CaseStore(Path(tmp.name) / "cases.db", citation_resolver=self.resolver,
                               unit_contracts=SourceUnitContracts.builtin())
        self.case_id = self.store.open(ENG_OPS, "Was the AO produced as planned?")
        self.planned = {"citation": ITEM, "field": "row.quantity", "unit": "EA"}
        self.actual = {"citation": ITEM, "field": "row.actual_amount", "unit": "EA"}

    def calculations(self) -> list:
        return self.store.get(ENG_OPS, self.case_id)["calculations"]

    def test_contracted_roles_are_not_subtracted_without_a_declared_relation(self):
        for expression in ("planned - actual", "planned + actual", "actual / planned"):
            with self.subTest(expression=expression), \
                    self.assertRaisesRegex(QuantityRoleMismatch, "quantity_role_mismatch"):
                self.store.compute(ENG_OPS, self.case_id,
                                   {"planned": self.planned, "actual": self.actual},
                                   expression, "1" if "/" in expression else "EA")
        self.assertEqual(self.calculations(), [])

    def test_a_declared_relation_is_recorded_with_roles_and_their_basis(self):
        relation = {"left": "planned", "op": "-", "right": "actual",
                    "as": "planned_not_produced"}
        receipt = self.store.compute(ENG_OPS, self.case_id,
                                     {"planned": self.planned, "actual": self.actual},
                                     "planned - actual", "EA", relations=[relation])
        self.assertEqual((receipt["result"], receipt["result_role"]),
                         ("6", "planned_not_produced"))
        self.assertEqual(receipt["input_units"]["planned"]["role_contract"],
                         "silver-assembly-order-items-quantity-planned")
        calc = self.calculations()[0]
        self.assertEqual(calc["relations"], [relation])
        self.assertEqual({name: (spec["role"], spec["role_basis"])
                          for name, spec in calc["inputs"].items()},
                         {"planned": ("planned", "source_contract"),
                          "actual": ("actual", "source_contract")})
        self.assertTrue(self.store.verify_chain(ENG_OPS, self.case_id))

    def test_role_declarations_are_checked_against_contracts_and_used(self):
        with self.assertRaisesRegex(QuantityRoleMismatch, "role_contradicts_source_contract"):
            self.store.compute(ENG_OPS, self.case_id,
                               {"m": {**self.actual, "role": "manufactured"}}, "m", "EA")
        filled = {"citation": TASK, "field": "row.qty", "unit": "EA"}
        manufactured = {"citation": LINK, "field": "row.qtymfg_c", "unit": "EA"}
        # An uncontracted quantity beside a roled one must be given a role.
        with self.assertRaisesRegex(QuantityRoleMismatch, "manufactured-unroled"):
            self.store.compute(ENG_OPS, self.case_id, {"m": manufactured, "f": filled},
                               "m - f", "EA")
        relation = {"left": "manufactured", "op": "-", "right": "filled", "as": "not_filled"}
        with self.assertRaisesRegex(ValidationError, "unused_role_relation:0"):
            self.store.compute(ENG_OPS, self.case_id, {"m": manufactured}, "m", "EA",
                               relations=[relation])
        for bad in ([{**relation, "left": "filled"}], [relation, dict(relation)],
                    [{**relation, "op": "%"}], [{**relation, "as": "Not Valid"}]):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                self.store.compute(ENG_OPS, self.case_id, {"m": manufactured}, "m", "EA",
                                   relations=bad)
        receipt = self.store.compute(ENG_OPS, self.case_id,
                                     {"m": manufactured, "f": {**filled, "role": "filled"}},
                                     "m - f", "EA", relations=[relation])
        self.assertEqual((receipt["result"], receipt["result_role"]), ("1", "not_filled"))
        self.assertEqual(receipt["input_units"]["f"]["role_basis"], "caller_declared")

    def test_scales_and_prices_keep_the_quantity_role_and_unroled_math_is_unchanged(self):
        price = {"citation": PRICE, "field": "row.unit_price", "unit": "USD/EA"}
        value = self.store.compute(ENG_OPS, self.case_id, {"p": price, "q": self.planned},
                                   "p * q", "USD")
        self.assertEqual((value["result"], value["result_role"]), ("1210.218", "planned"))
        # Only a unit-1 literal is a scale; a literal quantity takes a role like any other.
        scaled = self.store.compute(ENG_OPS, self.case_id,
                                    {"q": self.planned, "k": {"literal": "2", "unit": "1"},
                                     "d": {"literal": "3", "unit": "EA", "role": "planned"}},
                                    "q * k - d", "EA")
        self.assertEqual(scaled["result_role"], "planned")
        ratio = self.store.compute(ENG_OPS, self.case_id,
                                   {"a": self.planned, "b": {**self.planned}}, "a / b", "1")
        self.assertIsNone(ratio["result_role"])
        plain = self.store.compute(ENG_OPS, self.case_id, {"p": price}, "p * 2", "USD/EA")
        self.assertNotIn("result_role", plain)
        self.assertNotIn("relations", self.calculations()[-1])
        self.assertNotIn("role", self.calculations()[-1]["inputs"]["p"])

    def test_a_literal_with_a_unit_is_a_quantity_not_a_neutral_scale(self):
        # 'manufactured - 1502 EA' must not borrow the manufactured role for the literal.
        manufactured = {"citation": LINK, "field": "row.qtymfg_c", "unit": "EA"}
        literal = {"literal": "1502", "unit": "EA"}
        for expression in ("m - x", "x - m", "m + x"):
            with self.subTest(expression=expression), \
                    self.assertRaisesRegex(QuantityRoleMismatch, "unroled"):
                self.store.compute(ENG_OPS, self.case_id, {"m": manufactured, "x": literal},
                                   expression, "EA")
        self.assertEqual(self.calculations(), [])
        # Declaring the literal's role and the relation is the way to do it.
        receipt = self.store.compute(
            ENG_OPS, self.case_id, {"m": manufactured, "x": {**literal, "role": "filled"}},
            "m - x", "EA", relations=[{"left": "manufactured", "op": "-", "right": "filled",
                                       "as": "not_filled"}])
        self.assertEqual((receipt["result"], receipt["result_role"]), ("1", "not_filled"))
        # Unit-1 literals and number literals stay neutral scales.
        for inputs, expression in (({"m": manufactured, "k": {"literal": "2", "unit": "1"}},
                                    "m * k"), ({"m": manufactured}, "m * 2 - m")):
            with self.subTest(expression=expression):
                self.assertEqual(self.store.compute(ENG_OPS, self.case_id, inputs, expression,
                                                    "EA")["result_role"], "manufactured")

    def test_a_role_made_dimensionless_by_an_unroled_quantity_has_no_role(self):
        other = {"citation": TASK, "field": "row.qty", "unit": "EA"}  # no role, no contract
        for expression in ("p / x", "x / p"):
            with self.subTest(expression=expression):
                ratio = self.store.compute(ENG_OPS, self.case_id,
                                           {"p": self.planned, "x": other}, expression, "1")
                self.assertIsNone(ratio["result_role"])
        # A percentage of it is still not a planned quantity.
        percent = self.store.compute(ENG_OPS, self.case_id, {"p": self.planned, "x": other},
                                     "(p / x - 1) * 100", "1")
        self.assertIsNone(percent["result_role"])
        self.assertEqual([calc["result_role"] for calc in self.calculations()],
                         [None, None, None])
        # A rate that keeps a dimension keeps the role: planned pieces / pieces per EA.
        derived = self.store.compute(
            ENG_OPS, self.case_id, {"p": {**self.planned, "unit": "pc"},
                                    "k": {"literal": "1", "unit": "pc/EA"}}, "p / k", "EA")
        self.assertEqual((derived["result"], derived["result_role"]), ("1509", "planned"))

    def test_signs_are_tracked_so_a_difference_needs_a_difference_relation(self):
        plus = {"left": "planned", "op": "+", "right": "actual", "as": "planned_and_actual"}
        minus = {"left": "actual", "op": "-", "right": "planned", "as": "actual_over_planned"}
        inputs = {"p": self.planned, "a": self.actual}
        for expression in ("-p + a", "a + -p", "a - p", "a - (+p)", "-1 * p + a"):
            with self.subTest(expression=expression), \
                    self.assertRaisesRegex(QuantityRoleMismatch, "actual-planned"):
                self.store.compute(ENG_OPS, self.case_id, inputs, expression, "EA",
                                   relations=[plus])
        for expression in ("p - -a", "-(-p - a)", "p + a", "a + p"):
            with self.subTest(expression=expression):
                receipt = self.store.compute(ENG_OPS, self.case_id, inputs, expression, "EA",
                                             relations=[plus])
                self.assertEqual(receipt["result_role"], "planned_and_actual")
        receipt = self.store.compute(ENG_OPS, self.case_id, inputs, "-p + a", "EA",
                                     relations=[minus])
        self.assertEqual((receipt["result"], receipt["result_role"]),
                         ("-6", "actual_over_planned"))
        # Residual sign cases: a same-role difference has no known sign beside another
        # role, and a negative unit-1 literal is a sign flip, not a neutral +1.
        for expression, extra in (("a + (p - p - p)", {}), ("a + (p - p)", {}),
                                  ("a + p * k", {"k": {"literal": "-1", "unit": "1"}}),
                                  ("a + p / k", {"k": {"literal": "-2", "unit": "1"}})):
            with self.subTest(expression=expression), \
                    self.assertRaisesRegex(QuantityRoleMismatch, "sign_unknown|actual-planned"):
                self.store.compute(ENG_OPS, self.case_id, {**inputs, **extra}, expression,
                                   "EA", relations=[plus])
        flipped = self.store.compute(ENG_OPS, self.case_id,
                                     {**inputs, "k": {"literal": "-1", "unit": "1"}},
                                     "a + p * k", "EA", relations=[minus])
        self.assertEqual((flipped["result"], flipped["result_role"]),
                         ("-6", "actual_over_planned"))
        # Within one role a difference is still that role.
        same = self.store.compute(ENG_OPS, self.case_id, {"p": self.planned, "q": self.planned},
                                  "p - q", "EA")
        self.assertEqual(same["result_role"], "planned")
        # A difference relation never matches its operands swapped.
        with self.assertRaisesRegex(QuantityRoleMismatch, "planned-actual"):
            self.store.compute(ENG_OPS, self.case_id, inputs, "p - a", "EA", relations=[minus])

    def test_no_expression_launders_one_role_as_another(self):
        # '(planned * planned) / planned -
        # actual' recorded planned - actual as an 'actual' quantity, then the same through
        # unit-1 roles, role literals, unroled divisors and division written first
        # ('1 / o * p * b - a'). Every route is refused, in every order.
        planned, actual = self.planned, self.actual
        p1, a1 = {**planned, "unit": "1"}, {**actual, "unit": "1"}

        def lp(value, unit):
            return {"literal": value, "unit": unit, "role": "planned"}

        def la(value, unit):
            return {"literal": value, "unit": unit, "role": "actual"}

        unroled = {"literal": "1503", "unit": "EA"}
        other = {"citation": TASK, "field": "row.qty", "unit": "EA"}  # cited, no role
        P, A, O, B = planned, actual, lp("1", "EA"), la("1", "EA")
        laundering = (
            # probes of the first deep re-review
            ({"planned": p1, "actual": a1}, "(planned * planned) / planned - actual", "1"),
            ({"p": P, "a": A, "o": O, "b": B}, "p / o * b - a", "EA"),
            ({"p": P, "a": A, "x": unroled}, "p / x * a - a", "EA"),
            # probes of the second deep re-review (division written first)
            ({"p": P, "a": A, "o": O, "b": B}, "1 / o * p * b - a", "EA"),
            ({"p": P, "a": A, "o": O, "b": B}, "p * (1 / o) * b - a", "EA"),
            ({"p": P, "q": P, "a": A}, "1 / q * p * a", "EA"),
            ({"p": P, "q": P, "a": A}, "1 / q * p * a - a", "EA"),
            ({"p": P, "q": P, "a": A}, "a * (1 / q * p)", "EA"),
            ({"p": P, "q": P, "a": A, "x": unroled}, "x / q * p / x * a", "EA"),
            ({"p": P, "q": P, "a": A, "x": other}, "x / q * p / x * a - a", "EA"),
            ({"p": P, "a": A, "o": O}, "(1 / o * p - 1) * a", "EA"),
            ({"planned": p1, "actual": a1, "o": lp("1", "1")},
             "1 / o * planned * actual - actual", "1"),
            ({"p": P, "e": la("1", "EA")}, "p * e / e", "EA"),
            ({"p": P, "a": A, "k": lp("0", "1")}, "a + p * k", "EA"),
            ({"p": P, "a": A, "k": {"literal": "0", "unit": "1"}}, "a + p * k", "EA"),
            ({"p": P, "a": A, "k": {"literal": "-1", "unit": "1"}}, "a + p * k * k", "EA"),
            ({"p": P, "a": A}, "a + 0 * p", "EA"),
            ({"p": P, "a": A}, "a - (p - p)", "EA"),
            ({"p": P, "n": la("1", "1")}, "n * p", "EA"),
            ({"p": P, "a": A, "x": unroled}, "(x / p) * p * a / x", "EA"),
            # earlier forms
            ({"p": P, "a": A}, "(p * p) / p - a", "EA"),
            ({"p": P, "a": A}, "(p * p) / a - a", "EA"),
            ({"p": P, "a": A}, "p * (p / p) * p / p - a", "EA"),
            ({"p": P, "q": P, "a": A}, "p / q * a - a", "EA"),
            ({"p": P, "a": A, "o": O}, "(p / o - 1) * a", "EA"),
            ({"p": lp("5", "1"), "a": la("3", "1"), "o": lp("1", "1")}, "p / o - a", "1"),
            ({"p": P, "a": A, "o": O, "b": B}, "(p / o) * (a / b) * b - a", "EA"),
            ({"p": P, "a": A, "x": unroled}, "p / x * a", "EA"),
            ({"p": P, "a": A, "x": unroled}, "x / p * a", "EA"),
            ({"p": P, "a": A, "x": {"literal": "1", "unit": "EA"}}, "p * x / x - a", "EA"),
            ({"p": P, "a": A, "n": lp("1", "1")}, "n * p - a", "EA"),
            ({"p": P, "a": A, "b": B}, "p * b / b - a", "EA"),
            ({"p": P, "a": A}, "p - a + a - a", "EA"),
            ({"p": P}, "p * p", "EA^2"),
            ({"k": lp("8", "kg"), "e": lp("4", "EA"), "a": A}, "k / e * a", "kg"),
        )
        for inputs, expression, unit in laundering:
            with self.subTest(expression=expression, inputs=sorted(inputs)), \
                    self.assertRaises(QuantityRoleMismatch):
                self.store.compute(ENG_OPS, self.case_id, inputs, expression, unit)
        self.assertEqual(self.calculations(), [])
        for inputs, expression, code in (
                ({"p": P, "q": P, "a": A}, "p / q * a", "cancelled_into_other_role:actual:planned"),
                ({"p": P, "q": P, "a": A}, "1 / q * p * a", "cancelled_into_other_role:actual:planned"),
                ({"p": P}, "p * p", "product_unsupported:planned\\^2"),
                ({"p": P, "a": A}, "p * p / p - a", "mismatch:planned-actual")):
            with self.subTest(expression=expression), \
                    self.assertRaisesRegex(QuantityRoleMismatch, "quantity_role_" + code):
                self.store.compute(ENG_OPS, self.case_id, inputs, expression,
                                   "EA^2" if expression == "p * p" else "EA")
        # A relation may not relabel a combination as one of its own operands.
        with self.assertRaisesRegex(ValidationError, "role_relation_result_names_an_operand"):
            self.store.compute(ENG_OPS, self.case_id, {"p": P, "a": A}, "p - a", "EA",
                               relations=[{"left": "planned", "op": "-", "right": "actual",
                                           "as": "actual"}])

        # What stays legitimate: a declared relation, a dimensionless same-role ratio
        # (no role), that ratio times its role, a count without a role times a quantity,
        # a same-role count cancelled again, a price times a quantity, an unroled round
        # trip and a percentage.
        legit = (
            ({"p": P, "a": A}, "p - a", "EA",
             [{"left": "planned", "op": "-", "right": "actual", "as": "planned_not_produced"}],
             ("6", "planned_not_produced")),
            ({"p": P, "q": P}, "p / q", "1", None, ("1", None)),
            ({"p": P, "q": P, "r": P}, "p / q * r", "EA", None, ("1509", "planned")),
            ({"p": P, "q": P, "r": P}, "1 / q * r * p", "EA", None, ("1509", "planned")),
            ({"n": {"literal": "3", "unit": "1"}, "p": P}, "n * p", "EA", None,
             ("4527", "planned")),
            ({"n": lp("2", "1"), "p": P}, "n * p / n", "EA", None, ("1509", "planned")),
            ({"p": P, "x": unroled}, "x / p * p", "EA", None, ("1503", None)),
            ({"p": P, "x": {"literal": "0.5", "unit": "USD/EA"}}, "p * x", "USD", None,
             ("754.5", "planned")),
            ({"p": P, "x": unroled}, "(p / x - 1) * 100", "1", None, (None, None)),
        )
        for inputs, expression, unit, relations, (value, role) in legit:
            with self.subTest(expression=expression):
                receipt = self.store.compute(ENG_OPS, self.case_id, inputs, expression, unit,
                                             relations=relations)
                if value is not None:
                    self.assertEqual(receipt["result"], value)
                self.assertEqual(receipt.get("result_role"), role)

    def test_the_result_type_comes_from_the_expression_and_labels_must_fit_it(self):
        # Claim-types redesign: a relation's 'as' is a label. The
        # result's quantity type is the closed algebra of operating.quantity_types over the
        # expression tree, recorded as result_type, and a label naming another type than
        # the form it labels is refused before anything is recorded.
        manufactured = {"citation": LINK, "field": "row.qtymfg_c", "unit": "EA"}
        inputs = {"m": manufactured, "p": self.planned}
        for relations, form in (
                ([{"left": "planned", "op": "-", "right": "manufactured", "as": "planned_unmade"},
                  {"left": "manufactured", "op": "+", "right": "planned_unmade",
                   "as": "produced_total"}], "names produced but the value is planned"),
                ([{"left": "planned", "op": "-", "right": "manufactured", "as": "filled_gap"},
                  {"left": "manufactured", "op": "+", "right": "filled_gap", "as": "total"}],
                 "names filled but the value is planned - manufactured")):
            with self.subTest(label=relations[0]["as"]), \
                    self.assertRaisesRegex(QuantityRoleMismatch,
                                           "quantity_label_inconsistent: .*" + form):
                self.store.compute(ENG_OPS, self.case_id, inputs, "m + (p - m)", "EA",
                                   relations=relations)
        self.assertEqual(self.calculations(), [])
        receipt = self.store.compute(ENG_OPS, self.case_id, inputs, "m + (p - m)", "EA",
                                     relations=[{"left": "planned", "op": "-",
                                                 "right": "manufactured", "as": "unmade"},
                                                {"left": "manufactured", "op": "+",
                                                 "right": "unmade", "as": "total"}])
        self.assertEqual((receipt["result"], receipt["result_role"], receipt["result_type"]),
                         ("1509", "total", "planned"))
        self.assertEqual(self.calculations()[-1]["result_type"], "planned")
        # A difference of two types is untyped, under a label that names its parts.
        receipt = self.store.compute(ENG_OPS, self.case_id, {"p": self.planned, "a": self.actual},
                                     "p - a", "EA", relations=[{
                                         "left": "planned", "op": "-", "right": "actual",
                                         "as": "planned_not_produced"}])
        self.assertEqual((receipt["result_role"], receipt["result_type"]),
                         ("planned_not_produced", None))
        # One cell twice is 2 x planned: the combination guard's label is planned, the
        # value is no planned figure (untyped); a unit-1 literal 1 is the number 1.
        doubled = self.store.compute(ENG_OPS, self.case_id,
                                     {"p": self.planned, "q": dict(self.planned)}, "p + q", "EA")
        self.assertEqual((doubled["result"], doubled["result_role"], doubled["result_type"]),
                         ("3018", "planned", None))
        one = self.store.compute(ENG_OPS, self.case_id,
                                 {"p": self.planned, "k": {"literal": "1", "unit": "1"}},
                                 "p * k", "EA")
        self.assertEqual(one["result_type"], "planned")
        two = self.store.compute(ENG_OPS, self.case_id,
                                 {"p": self.planned, "k": {"literal": "2", "unit": "1"}},
                                 "p * k", "EA")
        self.assertEqual((two["result_role"], two["result_type"]), ("planned", None))

    def test_role_answers_do_not_depend_on_factor_order_or_grouping(self):
        """Property: every ordering and grouping of a product's factors gets one answer."""
        import itertools

        from operating.cases import parse_unit, quantity_role

        roles = {"p": "planned", "q": "planned", "r": "planned", "o": "planned",
                 "n": "planned", "a": "actual", "b": "actual", "e": "actual",
                 "x": None, "c": "", "u": None}
        units = {name: parse_unit("EA") for name in roles}
        units.update(n=parse_unit("1"), c=parse_unit("1"), u=parse_unit("USD/EA"))

        def variants(factors):
            """Linear orders (division first written as '1 / d'), and N / D grouping."""
            for order in itertools.permutations(factors):
                text = ""
                for op, name in order:
                    text = (name if op == "*" else f"1 / {name}") if not text \
                        else f"{text} {op} {name}"
                yield text
            top = [name for op, name in factors if op == "*"] or ["1"]
            bottom = [name for op, name in factors if op == "/"]
            yield (f"({' * '.join(top)}) / ({' * '.join(bottom)})" if bottom
                   else " * ".join(reversed(top)))
            if bottom:
                yield f"{' * '.join(top)} * (1 / ({' * '.join(bottom)}))"

        def answer(expression):
            try:
                return quantity_role(expression, roles, [], units)[0]
            except QuantityRoleMismatch:
                return "refused"

        def f(spec):
            return [(spec[i], spec[i + 1]) for i in range(0, len(spec), 2)]

        cases = (
            # refused, in every order
            (f("*p/o*b"), " - a", "refused"),
            (f("*p/q*a"), "", "refused"),
            (f("*x/q*p/x*a"), " - a", "refused"),
            (f("*p*p/p"), " - a", "refused"),
            (f("*p/x*a"), " - a", "refused"),
            (f("*p*b/b"), " - a", "refused"),
            (f("*p*e/e"), "", "refused"),
            (f("*p*p"), "", "refused"),
            (f("*x/p*p*a/x"), "", "refused"),
            (f("*n*p"), " - a", "refused"),
            # accepted, with one answer in every order
            (f("*p/q*r"), "", "planned"),
            (f("*p/q*r"), " - p", "planned"),
            (f("*x/p*p"), "", None),
            (f("*c*p"), "", "planned"),
            (f("*n*p/n"), "", "planned"),
            (f("*p*u"), "", "planned"),
            (f("*p/q"), "", None),
        )
        for factors, suffix, expected in cases:
            for text in variants(factors):
                expression = f"({text}){suffix}" if suffix else text
                with self.subTest(expression=expression):
                    self.assertEqual(answer(expression), expected)

    def test_contract_files_accept_a_role_without_a_unit(self):
        role_only = SourceUnitContracts(document(contract(unit=None, role="planned")))
        found = role_only.lookup(FORMULA, {"relation": "silver.formula_items"},
                                 "row.percentage")
        self.assertEqual((found.get("unit"), found["role"]), (None, "planned"))
        for code, entry in (("unit_contract_invalid_role", contract(role="Planned")),
                            ("unit_contract_invalid_unit", contract(unit=None))):
            with self.subTest(code=code), self.assertRaisesRegex(ValidationError, code):
                SourceUnitContracts(document(entry))

    def test_a_conditional_contract_applies_only_to_rows_meeting_its_condition(self):
        contracts = SourceUnitContracts.builtin()

        def task(task_type):
            return {"relation": "silver.production_tasks",
                    "row": {"qty": "1502.0", "task_type": task_type}}
        found = contracts.lookup(TASK, task("Filling"), "row.qty")
        self.assertEqual((found["id"], found["role"], found["when"]),
                         ("silver-production-tasks-qty-filled-on-filling", "filled",
                          {"column": "task_type", "equals": "Filling"}))
        self.assertEqual(contracts.lookup(TASK, task(" Filling "), "row.qty")["role"],
                         "filled")
        for other in ("Line clearance", "filling", None):
            with self.subTest(task_type=other):
                self.assertIsNone(contracts.lookup(TASK, task(other), "row.qty"))
        # The Filling task's qty is then a filled quantity in compute, not caller-declared.
        self.resolver.add(TASK, task("Filling"), {"eng"})
        receipt = self.store.compute(ENG_OPS, self.case_id, {
            "f": {"citation": TASK, "field": "row.qty", "unit": "EA"}}, "f", "EA")
        self.assertEqual((receipt["result_role"], receipt["input_units"]["f"]["role_basis"]),
                         ("filled", "source_contract"))
        # Conditions must be distinct, and never beside an unconditional contract.
        conditional = contract(unit=None, role="filled")
        conditional["when"] = {"column": "task_type", "equals": "Filling"}
        plain = dict(contract(unit=None, role="planned"), id="other-id")
        for entries in ([conditional, dict(conditional, id="other-id")], [conditional, plain]):
            with self.subTest(entries=len(entries)), \
                    self.assertRaisesRegex(ValidationError, "unit_contract_duplicate_column"):
                SourceUnitContracts({"schema": "plx-source-unit-contracts/v1",
                                     "contracts": entries})
        with self.assertRaisesRegex(ValidationError, "unit_contract_invalid_when"):
            SourceUnitContracts(document(dict(conditional, when={"column": "task_type"})))


class RuntimeWiring(CaseToolsTestCase):
    def contracts_file(self, entries: list) -> Path:
        path = self.root / "units.json"
        path.write_text(json.dumps({"schema": SCHEMA, "contracts": entries}), encoding="utf-8")
        return path

    def runtime_with(self, unit_contracts) -> CaseRuntime:
        from integrations.tests.test_source_mirror import BASE, KEY
        from integrations.workbench_read_api import WorkbenchReadApiClient
        client = WorkbenchReadApiClient(BASE, KEY, fetch=self.fake)
        runtime = CaseRuntime(self.state, client=client, audience=self.audience_path,
                              tenant="plx", unit_contracts=unit_contracts,
                              mirror_options={"clock": self.clock, "retry_delay_seconds": 0})
        self.addCleanup(runtime.close)
        return runtime

    def test_default_runtime_uses_builtin_contracts(self):
        runtime = self.runtime()
        self.assertEqual(len(runtime.unit_contracts), len(SourceUnitContracts.builtin()))

    def test_bad_contract_file_fails_at_startup(self):
        path = self.contracts_file([contract(evidence=[])])
        with self.assertRaisesRegex(ValidationError, "unit_contract_evidence_required"):
            self.runtime_with(path)

    def test_mcp_compute_enforces_a_contract_on_a_mirrored_column(self):
        path = self.contracts_file([contract(id="ao-qtytotal-ea", relation="public.assembly_order",
                                             column="qtytotal", unit="EA")])
        server = self.server(OPS, self.runtime_with(path))
        case_id, citation = self.open_case_with_fact(server)
        refused = self.refused(server, "plx.case_compute", {
            "case_id": case_id,
            "inputs": {"planned": {"citation": citation, "field": "row.qtytotal",
                                   "unit": "case"}},
            "expression": "planned", "unit": "case"})
        self.assertIn("unit_contradicts_source_contract", refused)
        computed = self.ok(server, "plx.case_compute", {
            "case_id": case_id,
            "inputs": {"planned": {"citation": citation, "field": "row.qtytotal"},
                       "made": {"literal": "1503", "unit": "EA"}},
            "expression": "planned - made", "unit": "EA"})["calculation"]
        self.assertEqual((computed["result"], computed["unit"]), ("6", "EA"))
        planned = dict(computed["input_units"]["planned"])
        self.assertRegex(planned.pop("unit_contract_digest"), r"^[0-9a-f]{64}$")
        self.assertEqual(planned, {"unit": "EA", "unit_basis": "source_contract",
                                   "unit_contract": "ao-qtytotal-ea"})
        view = self.ok(server, "plx.case_get", {"case_id": case_id})
        calcs = view.get("calculations") or view["case"]["calculations"]
        self.assertEqual(len(calcs), 1)
        self.assertEqual(calcs[0]["inputs"]["planned"]["unit_basis"], "source_contract")


if __name__ == "__main__":
    unittest.main()
