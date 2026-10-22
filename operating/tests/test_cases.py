from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from ontology.core import Actor, ValidationError
from operating.cases import (
    CaseClosed, CaseIntegrityError, CaseNotFound, CaseStore, CitationError,
    CitationUnresolvable, ExpressionError, UnitMismatch, _digest, evaluate, format_unit,
    parse_unit,
)

REPO = Path(__file__).resolve().parents[2]

ENG = Actor("plx", frozenset({"eng"}))
ENG_OPS = Actor("plx", frozenset({"eng", "ops"}))
OPS = Actor("plx", frozenset({"ops"}))
FINANCE = Actor("plx", frozenset({"finance"}))
OTHER_TENANT = Actor("other", frozenset({"eng"}))
PUBLIC_ENG = Actor("plx", frozenset({"eng"}), clearance="public")
RESTRICTED_ENG = Actor("plx", frozenset({"eng"}), clearance="restricted")

AO = "api://fm_assembly_order/{\"id\":\"AO-26005B\"}@cap-1"
PACK = "api://fm_assembly_task/{\"id\":\"T-9\"}@cap-1"
PRICE = "api://sage_po_line/{\"po\":\"PO-77\",\"line\":1}@cap-2"


class FakeResolver:
    """Stands in for the source-mirror resolver: dict or a typed error."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[dict, set[str]]] = {}
        self.calls = 0

    def add(self, handle: str, row: dict, readable_by: set[str]) -> str:
        self.rows[handle] = (row, set(readable_by))
        return handle

    def revoke(self, handle: str, principal: str) -> None:
        self.rows[handle][1].discard(principal)

    def grant(self, handle: str, principal: str) -> None:
        self.rows[handle][1].add(principal)

    def __call__(self, handle: str, actor: Actor) -> dict:
        self.calls += 1
        entry = self.rows.get(handle)
        if entry is None or not (entry[1] & actor.principals):
            raise CitationError("unresolvable")
        return copy.deepcopy(entry[0])


def fixed_clock():
    counter = [0]

    def tick() -> datetime:
        counter[0] += 1
        return datetime(2026, 9, 30, 12, 0, counter[0] % 60, counter[0], tzinfo=timezone.utc)
    return tick


class CaseTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "cases.db"
        self.resolver = FakeResolver()
        self.resolver.add(AO, {"row": {"ordered": "1920", "manufactured": "1503", "damaged": "30",
                                       "planned": 1509}, "capture": "cap-1"}, {"eng", "ops"})
        self.resolver.add(PACK, {"row": {"damaged_packs": "30"}}, {"eng", "ops"})
        self.resolver.add(PRICE, {"row": {"unit_price": "12.50", "cases": 40}}, {"eng", "ops"})
        self.store = CaseStore(self.path, citation_resolver=self.resolver)

    def open_case(self, actor: Actor = ENG_OPS, **kwargs) -> str:
        return self.store.open(actor, "Why was AO-26005B short?", roots=["AO-26005B"], **kwargs)


class DurabilityAndHistory(CaseTestBase):
    def test_survives_reopen_and_new_process(self):
        case_id = self.open_case()
        self.store.finding(ENG_OPS, case_id, "Ordered 1920 EA", "fact", "supported", [AO])
        reopened = CaseStore(self.path, citation_resolver=self.resolver)
        view = reopened.get(ENG_OPS, case_id)
        self.assertEqual(view["question"], "Why was AO-26005B short?")
        self.assertEqual(view["roots"], ["AO-26005B"])
        self.assertEqual(view["findings"]["supported"][0]["statement"], "Ordered 1920 EA")
        code = ("import sys, json\nfrom ontology.core import Actor\nfrom operating.cases import CaseStore\n"
                "s = CaseStore(sys.argv[1])\n"
                "a = Actor('plx', frozenset({'eng', 'ops'}))\n"
                "s.gap(a, sys.argv[2], 'sibling AOs', 'may share the order', 'planner', 'not_ingested')\n"
                "print(json.dumps(len(s.get(a, sys.argv[2])['gaps'])))\n")
        out = subprocess.run([sys.executable, "-c", code, str(self.path), case_id], cwd=REPO,
                             env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                             capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "1")
        self.assertEqual(len(self.store.get(ENG_OPS, case_id)["gaps"]), 1)

    def test_history_is_append_only_ordered_and_chained(self):
        case_id = self.open_case()
        first = self.store.finding(ENG_OPS, case_id, "A", "interpretation", "unknown")
        second = self.store.gap(ENG_OPS, case_id, "x", "y", ["z"], "unknown")
        self.store.close(ENG_OPS, case_id, "done")
        timeline = self.store.get(ENG_OPS, case_id)["timeline"]
        self.assertEqual([item["seq"] for item in timeline], [1, 2, 3, 4])
        self.assertEqual([item["kind"] for item in timeline],
                         ["case.opened", "finding.recorded", "gap.recorded", "case.closed"])
        self.assertEqual((first["seq"], second["seq"]), (2, 3))
        self.assertTrue(all(item["actor"] == "eng,ops" and item["at"] for item in timeline))
        self.assertTrue(self.store.verify_chain(ENG_OPS, case_id))
        raw = sqlite3.connect(self.path)
        for sql in ("UPDATE case_event SET actor='mallory'", "DELETE FROM case_event"):
            with self.assertRaises(sqlite3.DatabaseError) as caught:
                raw.execute(sql)
            self.assertIn("append_only", str(caught.exception))
        raw.close()

    def test_tampering_is_detected_and_blocks_writes(self):
        case_id = self.open_case()
        self.store.gap(ENG_OPS, case_id, "x", "y", "z", "unknown")
        raw = sqlite3.connect(self.path)
        raw.execute("DROP TRIGGER case_event_no_update")
        raw.execute("UPDATE case_event SET payload_json=replace(payload_json,'\"x\"','\"forged\"') "
                    "WHERE kind='gap.recorded'")
        raw.commit()
        raw.close()
        self.assertFalse(self.store.verify_chain(ENG_OPS, case_id))
        self.assertFalse(self.store.get(ENG_OPS, case_id)["integrity"]["chain_ok"])
        with self.assertRaises(CaseIntegrityError):
            self.store.gap(ENG_OPS, case_id, "more", "y", "z", "unknown")

    def test_crashed_writer_leaves_no_partial_state(self):
        case_id = self.open_case()

        class Crashing(CaseStore):
            def _before_commit(self):
                raise RuntimeError("power cut")

        crashing = Crashing(self.path, citation_resolver=self.resolver)
        with self.assertRaises(RuntimeError):
            crashing.finding(ENG_OPS, case_id, "lost", "fact", "supported", [AO])
        with self.assertRaises(RuntimeError):
            crashing.open(ENG_OPS, "never happened")
        view = self.store.get(ENG_OPS, case_id)
        self.assertEqual([item["seq"] for item in view["timeline"]], [1])
        self.assertEqual(len(self.store.list(ENG_OPS)), 1)
        receipt = self.store.finding(ENG_OPS, case_id, "kept", "fact", "supported", [AO])
        self.assertEqual((receipt["seq"], receipt["finding_id"]), (2, "F1"))
        self.assertTrue(self.store.verify_chain(ENG_OPS, case_id))

    def test_process_killed_mid_transaction_leaves_nothing(self):
        case_id = self.open_case()
        code = ("import os, sys\nfrom ontology.core import Actor\nfrom operating.cases import CaseStore\n"
                "class Dying(CaseStore):\n    def _before_commit(self):\n        os._exit(9)\n"
                "a = Actor('plx', frozenset({'eng', 'ops'}))\n"
                "Dying(sys.argv[1]).gap(a, sys.argv[2], 'never committed', 'w', 'who', 'unknown')\n")
        proc = subprocess.run([sys.executable, "-c", code, str(self.path), case_id], cwd=REPO,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True)
        self.assertEqual(proc.returncode, 9)
        view = self.store.get(ENG_OPS, case_id)
        self.assertEqual(view["gaps"], [])
        self.assertEqual(len(view["timeline"]), 1)
        self.assertEqual(self.store.gap(ENG_OPS, case_id, "after", "w", "who", "unknown")["seq"], 2)
        self.assertTrue(self.store.verify_chain(ENG_OPS, case_id))

    def test_injected_clock_stamps_events(self):
        store = CaseStore(Path(self.tmp.name) / "clock.db", clock=fixed_clock())
        case_id = store.open(ENG, "q")
        store.gap(ENG, case_id, "m", "w", "who", "unknown")
        ats = [item["at"] for item in store.get(ENG, case_id)["timeline"]]
        self.assertEqual(ats, sorted(ats))
        self.assertTrue(ats[0].startswith("2026-09-30T12:00:01"))
        with self.assertRaises(ValidationError):
            CaseStore(Path(self.tmp.name) / "naive.db", clock=datetime.now).open(ENG, "q")


class Findings(CaseTestBase):
    def test_fact_requires_a_citation(self):
        case_id = self.open_case()
        for citations in ([], None, ()):
            with self.assertRaises(ValidationError) as caught:
                self.store.finding(ENG_OPS, case_id, "Ordered 1920 EA", "fact", "supported", citations)
            self.assertIn("fact_requires_citation", str(caught.exception))
        self.assertEqual(len(self.store.get(ENG_OPS, case_id)["timeline"]), 1)

    def test_unresolvable_citation_is_rejected_for_this_actor(self):
        case_id = self.open_case()
        with self.assertRaises(CitationUnresolvable):
            self.store.finding(ENG_OPS, case_id, "x", "fact", "supported", ["api://nope@cap"])
        self.resolver.add("api://finance_only@c", {"v": 1}, {"finance"})
        with self.assertRaises(CitationUnresolvable):
            self.store.finding(ENG_OPS, case_id, "x", "fact", "supported", [AO, "api://finance_only@c"])
        for error in (LookupError, PermissionError):
            def raising(handle, actor, error=error):
                raise error(handle)
            broken = CaseStore(self.path, citation_resolver=raising)
            with self.assertRaises(CitationUnresolvable):
                broken.finding(ENG_OPS, case_id, "x", "fact", "supported", [AO])
        with self.assertRaises(CitationUnresolvable):
            CaseStore(self.path).finding(ENG_OPS, case_id, "x", "fact", "supported", [AO])
        self.assertEqual(len(self.store.get(ENG_OPS, case_id)["timeline"]), 1)

    def test_resolver_bug_is_not_swallowed_on_write(self):
        case_id = self.open_case()
        broken = CaseStore(self.path, citation_resolver=lambda handle, actor: 1 / 0)
        with self.assertRaises(ZeroDivisionError):
            broken.finding(ENG_OPS, case_id, "x", "fact", "supported", [AO])

    def test_interpretation_is_labelled_and_may_cite_or_not(self):
        case_id = self.open_case()
        plain = self.store.finding(ENG_OPS, case_id, "Probably a rework batch", "interpretation", "unknown")
        cited = self.store.finding(ENG_OPS, case_id, "Shortfall looks like damage", "interpretation",
                                   "supported", [AO])
        fact = self.store.finding(ENG_OPS, case_id, "Ordered 1920 EA", "fact", "supported", [AO])
        self.assertEqual(plain["label"], "model_output_interpretation")
        view = self.store.get(ENG_OPS, case_id)
        by_id = {item["id"]: item for bucket in ("supported", "unknown")
                 for item in view["findings"][bucket]}
        self.assertEqual(by_id[plain["finding_id"]]["label"], "model_output_interpretation")
        self.assertEqual(by_id[cited["finding_id"]]["label"], "model_output_interpretation")
        self.assertEqual(by_id[fact["finding_id"]]["label"], "cited_fact")
        self.assertEqual(by_id[cited["finding_id"]]["citations"][0]["status"], "resolved")
        with self.assertRaises(CitationUnresolvable):
            self.store.finding(ENG_OPS, case_id, "cites what it cannot see", "interpretation",
                               "supported", ["api://nope@cap"])

    def test_finding_validation(self):
        case_id = self.open_case()
        for statement, kind, status in (("", "fact", "supported"), ("s", "opinion", "supported"),
                                        ("s", "fact", "proven"), ("x" * 4001, "fact", "supported")):
            with self.assertRaises(ValidationError):
                self.store.finding(ENG_OPS, case_id, statement, kind, status, [AO])
        with self.assertRaises(ValidationError):
            self.store.finding(ENG_OPS, case_id, "s", "fact", "supported", AO)  # bare string

    def test_conflicting_findings_are_kept_and_supersede_does_not_edit(self):
        case_id = self.open_case()
        a = self.store.finding(ENG_OPS, case_id, "Planned qty was 1509", "fact", "supported", [AO])
        b = self.store.finding(ENG_OPS, case_id, "Planned qty was 1920", "fact", "contradicted", [AO])
        view = self.store.get(ENG_OPS, case_id)
        self.assertEqual(len(view["findings"]["supported"]), 1)
        self.assertEqual(len(view["findings"]["contradicted"]), 1)
        replacement = self.store.finding(ENG_OPS, case_id, "Planned qty was 1509 per link table",
                                         "fact", "supported", [AO])
        receipt = self.store.supersede(ENG_OPS, case_id, a["finding_id"], "narrower wording",
                                       replaced_by=replacement["finding_id"])
        self.assertEqual(receipt["kind"], "finding.superseded")
        view = self.store.get(ENG_OPS, case_id)
        self.assertEqual([item["id"] for item in view["findings"]["supported"]],
                         [replacement["finding_id"]])
        old = view["findings"]["superseded"][0]
        self.assertEqual(old["statement"], "Planned qty was 1509")
        self.assertEqual(old["superseded"]["reason"], "narrower wording")
        self.assertEqual(old["superseded"]["replaced_by"], replacement["finding_id"])
        self.assertEqual(len(view["findings"]["contradicted"]), 1)
        self.assertEqual([item["kind"] for item in view["timeline"]].count("finding.recorded"), 3)
        with self.assertRaises(ValidationError):
            self.store.supersede(ENG_OPS, case_id, a["finding_id"], "again")
        with self.assertRaises(ValidationError):
            self.store.supersede(ENG_OPS, case_id, "F99", "missing")
        with self.assertRaises(ValidationError):
            self.store.supersede(ENG_OPS, case_id, b["finding_id"], "bad replacement", replaced_by="F99")
        with self.assertRaises(ValidationError):
            self.store.supersede(ENG_OPS, case_id, b["finding_id"], "")


class Compute(CaseTestBase):
    def cited(self, field: str, unit: str, handle: str = AO) -> dict:
        return {"citation": handle, "field": field, "unit": unit}

    def test_ao_shortfall_and_separate_damaged_unit(self):
        case_id = self.open_case()
        receipt = self.store.compute(
            ENG_OPS, case_id,
            {"ordered": self.cited("row.ordered", "EA"),
             "manufactured": self.cited("row.manufactured", "EA")},
            "ordered - manufactured", "EA")
        self.assertEqual((receipt["result"], receipt["unit"], receipt["inexact"]), ("417", "EA", False))
        with self.assertRaises(UnitMismatch):
            self.store.compute(ENG_OPS, case_id,
                               {"ordered": self.cited("row.ordered", "EA"),
                                "damaged": self.cited("row.damaged", "pack")},
                               "ordered - damaged", "EA")
        kept = self.store.compute(ENG_OPS, case_id, {"damaged": self.cited("row.damaged", "pack")},
                                  "damaged", "pack")
        self.assertEqual((kept["result"], kept["unit"]), ("30", "pack"))
        calc = self.store.get(ENG_OPS, case_id)["calculations"][0]
        self.assertEqual(calc["result"], "417")
        self.assertEqual(calc["expression"], "ordered - manufactured")
        self.assertEqual(calc["inputs"]["ordered"]["value"], "1920")
        self.assertEqual(calc["inputs"]["ordered"]["citation"]["handle"], AO)
        self.assertEqual(calc["inputs"]["ordered"]["field"], "row.ordered")
        self.assertEqual(len(self.store.get(ENG_OPS, case_id)["calculations"]), 2)

    def test_money_units_multiply_symbolically(self):
        case_id = self.open_case()
        receipt = self.store.compute(
            ENG_OPS, case_id,
            {"price": self.cited("row.unit_price", "USD/case", PRICE),
             "qty": self.cited("row.cases", "case", PRICE)},
            "price * qty", "USD")
        self.assertEqual((receipt["result"], receipt["unit"]), ("500", "USD"))
        self.assertEqual(self.store.compute(ENG_OPS, case_id, {"total": {"literal": "500", "unit": "USD"},
                                                              "qty": {"literal": 40, "unit": "case"}},
                                            "total / qty", "USD/case")["result"], "12.5")
        with self.assertRaises(UnitMismatch):  # USD/case + USD is not a sum
            self.store.compute(ENG_OPS, case_id, {"p": {"literal": "1", "unit": "USD/case"},
                                                  "t": {"literal": "1", "unit": "USD"}}, "p + t", "USD")
        with self.assertRaises(UnitMismatch):  # different currencies never add
            self.store.compute(ENG_OPS, case_id, {"a": {"literal": "1", "unit": "USD"},
                                                  "b": {"literal": "1", "unit": "EUR"}}, "a + b", "USD")
        with self.assertRaises(UnitMismatch):  # declared unit must match derived
            self.store.compute(ENG_OPS, case_id, {"p": {"literal": "2", "unit": "USD/case"},
                                                  "q": {"literal": "3", "unit": "case"}}, "p * q", "USD/case")

    def test_decimal_exactness_and_inexact_flag(self):
        case_id = self.open_case()
        one = {"literal": "1", "unit": "1"}
        exact = self.store.compute(ENG_OPS, case_id, {"x": {"literal": "0.1", "unit": "1"},
                                                      "y": {"literal": "0.2", "unit": "1"}}, "x + y", "1")
        self.assertEqual((exact["result"], exact["inexact"]), ("0.3", False))
        third = self.store.compute(ENG_OPS, case_id, {"one": one, "three": {"literal": 3, "unit": "1"}},
                                   "one / three", "1")
        self.assertTrue(third["inexact"])
        self.assertEqual(third["result"], "0." + "3" * 50)
        self.assertEqual(self.store.compute(ENG_OPS, case_id, {"a": {"literal": "1.10", "unit": "USD"},
                                                               "fee": {"literal": ".5", "unit": "USD"}},
                                            "a * 100 + fee", "USD")["result"], "110.5")
        with self.assertRaises(UnitMismatch):  # bare numbers are dimensionless, never silently USD
            self.store.compute(ENG_OPS, case_id, {"a": {"literal": "1.10", "unit": "USD"}},
                               "a + 0.5", "USD")
        self.assertEqual(self.store.compute(ENG_OPS, case_id, {"a": {"literal": "7", "unit": "EA"},
                                                               "b": {"literal": "3", "unit": "EA"}},
                                            "(a − b) × 2 ÷ 4", "EA")["result"], "2")
        self.assertEqual(self.store.compute(ENG_OPS, case_id, {"a": {"literal": "5", "unit": "EA"}},
                                            "-a + a", "EA")["result"], "0")
        self.assertEqual(evaluate("2 * 3", {})[0], Decimal("6"))
        huge = {"a": (Decimal("9" * 50), ())}
        with self.assertRaises(ExpressionError):
            evaluate("a*a*a*a", huge)

    def test_no_floats_anywhere(self):
        case_id = self.open_case()
        for bad in (1.5, True, float("nan")):
            with self.assertRaises(ValidationError):
                self.store.compute(ENG_OPS, case_id, {"a": {"literal": bad, "unit": "EA"}}, "a", "EA")
        # A cited non-finite float is still refused.
        self.resolver.add("api://f@nan", {"row": {"qty": float("nan")}}, {"eng"})
        with self.assertRaises(ValidationError):
            self.store.compute(ENG_OPS, case_id, {"a": self.cited("row.qty", "EA", "api://f@nan")},
                               "a", "EA")
        with self.assertRaises(ExpressionError):
            self.store.compute(ENG_OPS, case_id, {"a": {"literal": "1", "unit": "EA"}}, "a * 1e3", "EA")
        with self.assertRaises(ValidationError):
            self.store.propose(ENG_OPS, case_id, "cap", {"price": 12.5})
        for text in ("NaN", "Infinity", "1e999999", "1e-999", "abc", ""):
            with self.assertRaises(ValidationError):
                self.store.compute(ENG_OPS, case_id, {"a": {"literal": text, "unit": "EA"}}, "a", "EA")

    def test_cited_json_fraction_is_read_with_a_recorded_rule(self):
        # A JSON number with a fraction arrives as a float, so a
        # cited price or weight could not be computed at all.
        case_id = self.open_case()
        self.resolver.add("api://f@c", {"row": {"qty": 1.5, "price": 35.099999999999994,
                                                "total": 1e16, "whole": 2.0}}, {"eng"})
        result = self.store.compute(
            ENG_OPS, case_id, {"q": self.cited("row.qty", "EA", "api://f@c"),
                               "p": self.cited("row.price", "USD/EA", "api://f@c")},
            "q * p", "USD")
        self.assertEqual((result["result"], result["inexact"]), ("52.65", False))
        calc = self.store.get(ENG_OPS, case_id)["calculations"][-1]
        self.assertEqual(calc["inputs"]["q"]["value"], "1.5")
        self.assertEqual(calc["inputs"]["q"]["source_number"],
                         {"raw": "1.5", "raw_type": "float", "rule": "binary64-shortest-repr",
                          "changed_by_normalization": False})
        self.assertEqual(calc["inputs"]["p"]["value"], "35.1")
        self.assertEqual(calc["inputs"]["p"]["source_number"],
                         {"raw": "35.099999999999994", "raw_type": "float",
                          "rule": "binary64-artifact-rounded-to-15-significant-digits",
                          "changed_by_normalization": True})
        big = self.store.compute(
            ENG_OPS, case_id, {"t": self.cited("row.total", "USD", "api://f@c"),
                               "w": self.cited("row.whole", "1", "api://f@c")}, "t * w", "USD")
        self.assertEqual(big["result"], "20000000000000000")
        # Strings and integers are unchanged and carry no source_number.
        self.resolver.add("api://f@s", {"row": {"qty": "35.099999999999994", "n": 3}}, {"eng"})
        exact = self.store.compute(
            ENG_OPS, case_id, {"q": self.cited("row.qty", "EA", "api://f@s"),
                               "n": self.cited("row.n", "1", "api://f@s")}, "q * n", "EA")
        self.assertEqual(exact["result"], "105.299999999999982")
        inputs = self.store.get(ENG_OPS, case_id)["calculations"][-1]["inputs"]
        self.assertNotIn("source_number", inputs["q"])
        self.assertNotIn("source_number", inputs["n"])

    def test_input_validation(self):
        case_id = self.open_case()
        lit = {"literal": "1", "unit": "EA"}
        bad_inputs = [
            {}, {"a": lit, "unused": lit}, {"1a": lit}, {"if": lit}, {"a": {"unit": "EA"}},
            {"a": {"literal": "1"}}, {"a": {"literal": "1", "unit": ""}},
            {"a": {"literal": "1", "citation": AO, "field": "row.ordered", "unit": "EA"}},
            {"a": {"citation": AO, "field": "row.missing", "unit": "EA"}},
            {"a": {"citation": AO, "unit": "EA"}},
            {"a": {"citation": "api://nope@c", "field": "x", "unit": "EA"}},
        ]
        for inputs in bad_inputs:
            with self.assertRaises((ValidationError, CitationUnresolvable), msg=str(inputs)):
                self.store.compute(ENG_OPS, case_id, inputs, "a", "EA")
        with self.assertRaises(ExpressionError):
            self.store.compute(ENG_OPS, case_id, {"a": lit}, "a + b", "EA")
        with self.assertRaises(ExpressionError):
            self.store.compute(ENG_OPS, case_id, {"a": lit, "z": {"literal": "0", "unit": "EA"}},
                               "a / z", "1")
        with self.assertRaises(ExpressionError):
            self.store.compute(ENG_OPS, case_id, {"a": lit}, "a / 0", "1")
        self.assertEqual(len(self.store.get(ENG_OPS, case_id)["calculations"]), 0)

    def test_expression_injection_is_rejected_and_never_executed(self):
        case_id = self.open_case()
        marker = Path(self.tmp.name) / "pwned"
        hostile = [
            f"__import__('os').system('touch {marker}')", f"__import__('os').system(\"touch {marker}\")",
            "().__class__.__bases__", "a.real", "a.__class__", "a[0]", "a[0:1]", "lambda: 1",
            "[x for x in (1,)]", "{1: 2}", "{1}", "(1, 2)", "a if a else a", "a ** 2", "a // a",
            "a % a", "a @ a", "a << 1", "a & a", "a | a", "~a", "not a", "a and a", "a < a",
            "(a := 1)", "a; b", "a\nb", "a,b", "'x'", '"x"', "b'x'", "f'{a}'", "True", "None",
            "...", "1j", "1e3", "0x10", "0b1", "1_0", "a.b", "abs(a)", "print(a)", "open",
            "eval('1')", "exec('1')", "a == a", "await a", "yield a", "*a", "**a", "",
            "   ", "1 +", "(", ")", "a b", "a $ a", "a\x00", "a" * 501, "(" * 300 + "a" + ")" * 300,
            "+".join(["a"] * 250), "+".join(["a"] * 500),
        ]
        for expression in hostile:
            with self.assertRaises((ExpressionError, ValidationError), msg=repr(expression)[:80]):
                self.store.compute(ENG_OPS, case_id, {"a": {"literal": "1", "unit": "EA"},
                                                      "b": {"literal": "1", "unit": "EA"}},
                                   expression, "EA")
        self.assertFalse(marker.exists())
        self.assertEqual(len(self.store.get(ENG_OPS, case_id)["calculations"]), 0)
        with self.assertRaises(ExpressionError):
            evaluate("a.__class__", {"a": (Decimal(1), ())})

    def test_unit_algebra(self):
        self.assertEqual(format_unit(parse_unit("USD/case")), "USD/case")
        self.assertEqual(format_unit(parse_unit("USD*case/case")), "USD")
        self.assertEqual(format_unit(parse_unit("1")), "1")
        self.assertEqual(format_unit(parse_unit("1/case")), "1/case")
        self.assertEqual(format_unit(parse_unit("case*USD")), "USD*case")
        self.assertEqual(format_unit(parse_unit("USD/case/day")), "USD/case/day")
        self.assertEqual(format_unit(parse_unit("EA*EA")), "EA^2")
        for bad in ("", " ", None, "USD/case*day", "US D", "$", "USD//case", "/case", "3", "EA^0"):
            with self.assertRaises(ValidationError, msg=repr(bad)):
                parse_unit(bad)
        value, unit, inexact, used = evaluate("p * q / q", {"p": (Decimal("2"), parse_unit("USD/case")),
                                                            "q": (Decimal("3"), parse_unit("case"))})
        self.assertEqual((value, format_unit(unit), used), (Decimal("2"), "USD/case", {"p", "q"}))
        with self.assertRaises(UnitMismatch):
            evaluate("a + 1", {"a": (Decimal(1), parse_unit("EA"))})
        self.assertEqual(evaluate("a - a", {"a": (Decimal(1), parse_unit("EA"))})[1], parse_unit("EA"))


class Visibility(CaseTestBase):
    def test_audience_defaults_and_intersection(self):
        self.assertEqual(self.store.get(ENG_OPS, self.open_case())["audience"], ["eng", "ops"])
        narrowed = self.open_case(audience=["ops", "finance"])
        self.assertEqual(self.store.get(ENG_OPS, narrowed)["audience"], ["ops"])
        for audience in (["*"], ["eng", "*"], ["finance"], [], "eng"):
            with self.assertRaises(ValidationError, msg=str(audience)):
                self.open_case(audience=audience)
        wildcard_actor = Actor("plx", frozenset({"*", "eng"}))
        case_id = self.store.open(wildcard_actor, "q")
        self.assertEqual(self.store.get(wildcard_actor, case_id)["audience"], ["eng"])
        with self.assertRaises(ValidationError):
            self.store.open(Actor("plx", frozenset({"*"})), "q")

    def test_outsiders_cannot_read_write_or_learn_the_case_exists(self):
        case_id = self.open_case(audience=["eng"])
        self.store.finding(ENG, case_id, "Ordered 1920 EA", "fact", "supported", [AO])
        outsiders = [OPS, FINANCE, OTHER_TENANT, PUBLIC_ENG]
        for outsider in outsiders:
            calls = [
                lambda: self.store.get(outsider, case_id),
                lambda: self.store.finding(outsider, case_id, "x", "interpretation", "unknown"),
                lambda: self.store.supersede(outsider, case_id, "F1", "no"),
                lambda: self.store.compute(outsider, case_id, {"a": {"literal": "1", "unit": "EA"}}, "a", "EA"),
                lambda: self.store.gap(outsider, case_id, "m", "w", "who", "unknown"),
                lambda: self.store.propose(outsider, case_id, "cap", {}),
                lambda: self.store.close(outsider, case_id, "no"),
                lambda: self.store.verify_chain(outsider, case_id),
            ]
            for call in calls:
                with self.assertRaises(CaseNotFound):
                    call()
            self.assertEqual(self.store.list(outsider), [])
        with self.assertRaises(CaseNotFound) as missing:
            self.store.get(OPS, "case-does-not-exist")
        with self.assertRaises(CaseNotFound) as hidden:
            self.store.get(OPS, case_id)
        self.assertEqual(str(missing.exception), str(hidden.exception))
        self.assertEqual(len(self.store.get(ENG, case_id)["timeline"]), 2)

    def test_clearance_gates_visibility(self):
        case_id = self.store.open(RESTRICTED_ENG, "restricted matter")
        self.assertEqual(self.store.get(RESTRICTED_ENG, case_id)["classification"], "restricted")
        with self.assertRaises(CaseNotFound):
            self.store.get(ENG, case_id)
        self.assertEqual(self.store.list(ENG), [])
        low = self.store.open(ENG, "internal matter")
        self.assertEqual(self.store.get(RESTRICTED_ENG, low)["classification"], "internal")

    def test_changed_principals_revoke_case_access(self):
        case_id = self.open_case()
        self.assertTrue(self.store.get(ENG_OPS, case_id))
        demoted = Actor("plx", frozenset({"guest"}))
        with self.assertRaises(CaseNotFound):
            self.store.get(demoted, case_id)
        self.assertEqual(self.store.get(ENG, case_id)["case_id"], case_id)

    def test_visibility_is_tenant_clearance_and_audience(self):
        from operating.cases import _visible
        # (actor, visible at public / internal / restricted) for audience ["eng"]:
        # same tenant, clearance at least the classification, a shared principal.
        table = ((ENG, (True, True, False)), (ENG_OPS, (True, True, False)),
                 (OPS, (False, False, False)), (FINANCE, (False, False, False)),
                 (OTHER_TENANT, (False, False, False)), (PUBLIC_ENG, (True, False, False)),
                 (RESTRICTED_ENG, (True, True, True)))
        for actor, expected in table:
            for classification, visible in zip(("public", "internal", "restricted"), expected):
                case = {"tenant": "plx", "classification": classification, "audience": ["eng"]}
                self.assertEqual(_visible(case, actor), visible, (actor, classification))
        self.assertFalse(_visible({"tenant": "plx", "classification": "public", "audience": ["*"]}, ENG))

    def test_list_filters_status_and_limits(self):
        mine = [self.store.open(ENG, f"q{i}") for i in range(4)]
        theirs = self.store.open(OPS, "not for eng")
        self.store.close(ENG, mine[0], "done")
        listed = self.store.list(ENG)
        self.assertEqual([item["case_id"] for item in listed], mine[::-1])
        self.assertEqual([item["case_id"] for item in self.store.list(ENG, status="closed")], [mine[0]])
        self.assertEqual(len(self.store.list(ENG, status="open")), 3)
        self.assertEqual(len(self.store.list(ENG, limit=2)), 2)
        self.assertEqual([item["case_id"] for item in self.store.list(OPS)], [theirs])
        self.assertEqual(listed[-1]["status"], "closed")
        self.assertEqual(listed[0]["events"], 1)
        for kwargs in ({"status": "weird"}, {"limit": 0}, {"limit": 201}, {"limit": True}):
            with self.assertRaises(ValidationError):
                self.store.list(ENG, **kwargs)


class Revocation(CaseTestBase):
    def build(self):
        case_id = self.open_case()
        self.store.finding(ENG_OPS, case_id, "Ordered 1920 EA", "fact", "supported", [AO])
        self.store.finding(ENG_OPS, case_id, "Just a plain hunch", "interpretation", "unknown")
        self.store.compute(ENG_OPS, case_id,
                           {"ordered": {"citation": AO, "field": "row.ordered", "unit": "EA"},
                            "made": {"citation": AO, "field": "row.manufactured", "unit": "EA"}},
                           "ordered - made", "EA")
        self.store.compute(ENG_OPS, case_id, {"a": {"literal": "2", "unit": "EA"}}, "a * 2", "EA")
        return case_id

    def test_revoked_citation_is_unavailable_without_leaking(self):
        case_id = self.build()
        full = self.store.get(OPS, case_id)
        self.assertEqual(full["findings"]["supported"][0]["statement"], "Ordered 1920 EA")
        self.assertEqual(full["calculations"][0]["result"], "417")
        self.resolver.revoke(AO, "ops")
        redacted = self.store.get(OPS, case_id)
        self.assertEqual(redacted["findings"]["supported"], [])
        withheld = redacted["findings"]["withheld"][0]
        self.assertEqual(withheld["content"], "withheld_citation_unavailable")
        self.assertEqual(withheld["citations"], [{"index": 0, "status": "citation_unavailable"}])
        self.assertIsNone(withheld["statement"])
        self.assertEqual([item["statement"] for item in redacted["findings"]["unknown"]],
                         ["Just a plain hunch"])
        calc, literal = redacted["calculations"]
        self.assertIsNone(calc["result"])
        self.assertEqual(calc["content"], "withheld_citation_unavailable")
        self.assertEqual(literal["result"], "4")
        blob = json.dumps(redacted)
        for secret in ("Ordered 1920", AO, "fm_assembly_order", "row.ordered", "ordered - made"):
            self.assertNotIn(secret, blob, secret)
        # Numbers are matched as whole tokens: real-clock timestamps and random ids in the
        # projection may contain the same digits (e.g. ".883417") without leaking anything.
        for secret in ("1920", "1503", "417"):
            self.assertIsNone(re.search(rf"(?<![0-9A-Za-z]){secret}(?![0-9A-Za-z])", blob), secret)
        # eng still holds the citation and keeps a full view; the case history is untouched.
        self.assertEqual(self.store.get(ENG, case_id)["findings"]["supported"][0]["statement"],
                         "Ordered 1920 EA")
        self.assertEqual(len(redacted["timeline"]), len(full["timeline"]))
        self.resolver.grant(AO, "ops")
        self.assertEqual(self.store.get(OPS, case_id)["calculations"][0]["result"], "417")

    def test_partially_available_citations_withhold_the_finding(self):
        case_id = self.open_case()
        self.store.finding(ENG_OPS, case_id, "Both rows agree", "fact", "supported", [AO, PRICE])
        self.resolver.revoke(PRICE, "ops")
        view = self.store.get(OPS, case_id)
        self.assertEqual(view["findings"]["supported"], [])
        statuses = [item["status"] for item in view["findings"]["withheld"][0]["citations"]]
        self.assertEqual(statuses, ["resolved", "citation_unavailable"])
        self.assertNotIn(PRICE, json.dumps(view))

    def test_a_member_cannot_supersede_a_finding_it_sees_only_as_withheld(self):
        case_id = self.open_case()
        first = self.store.finding(ENG_OPS, case_id, "Both rows agree", "fact", "supported",
                                   [AO, PRICE])
        mine = self.store.finding(OPS, case_id, "Ordered 1920 EA", "fact", "supported", [AO])
        self.resolver.revoke(PRICE, "ops")
        with self.assertRaisesRegex(ValidationError, "^finding_not_visible$"):
            self.store.supersede(OPS, case_id, first["finding_id"], "looks wrong")
        with self.assertRaisesRegex(ValidationError, "^replacement_finding_not_visible$"):
            self.store.supersede(OPS, case_id, mine["finding_id"], "see F1",
                                 replaced_by=first["finding_id"])
        view = self.store.get(ENG_OPS, case_id)
        self.assertEqual(view["findings"]["superseded"], [])
        self.assertEqual([e["kind"] for e in view["timeline"]].count("finding.superseded"), 0)
        # A member who can read it may still supersede it.
        self.store.supersede(ENG_OPS, case_id, first["finding_id"], "narrower wording")
        self.assertEqual([f["id"] for f in self.store.get(ENG_OPS, case_id)["findings"]
                          ["superseded"]], [first["finding_id"]])

    def test_unavailable_without_resolver_or_when_resolver_breaks(self):
        case_id = self.build()
        with self.assertLogs("operating.cases", level="WARNING"):
            for resolver in (None, lambda handle, actor: 1 / 0, lambda handle, actor: "not a dict"):
                view = CaseStore(self.path, citation_resolver=resolver).get(ENG_OPS, case_id)
                self.assertEqual(view["findings"]["supported"], [])
                self.assertEqual(len(view["findings"]["withheld"]), 1)
                # The statement and the quoted cell, never a bare "1920" that a timestamp's
                # microseconds or a random id may hold.
                for secret in ("Ordered 1920", '"1920"'):
                    self.assertNotIn(secret, json.dumps(view), secret)

    def test_superseded_findings_are_also_withheld(self):
        case_id = self.open_case()
        first = self.store.finding(ENG_OPS, case_id, "Old claim", "fact", "supported", [AO])
        self.store.supersede(ENG_OPS, case_id, first["finding_id"], "outdated")
        self.resolver.revoke(AO, "ops")
        view = self.store.get(OPS, case_id)
        self.assertEqual(view["findings"]["superseded"], [])
        self.assertTrue(view["findings"]["withheld"][0]["superseded"])
        self.assertNotIn("Old claim", json.dumps(view))
        self.assertNotIn("outdated", json.dumps(view))

    def test_citation_digest_records_what_was_seen(self):
        case_id = self.open_case()
        self.store.finding(ENG_OPS, case_id, "a", "fact", "supported", [AO])
        self.store.finding(ENG_OPS, case_id, "b", "fact", "supported", [AO, AO])
        cites = [item["citations"] for item in self.store.get(ENG_OPS, case_id)["findings"]["supported"]]
        self.assertEqual(cites[0][0]["digest"], cites[1][0]["digest"])
        self.assertEqual(len(cites[1]), 1)
        self.assertEqual(len(cites[0][0]["digest"]), 64)
        # A recorded digest never moves: a view without ``derived`` hashes as it always
        # has; one with it hashes as the same view without its derived keys (decision
        # 2026-10-18-pinned-citation-digest-bytes), and a malformed one hashes whole.
        pinned = {"citation": "sharepoint://d/i@e#sha256=ab", "digest": "d1",
                  "content_sha256": "ab", "status": "current", "changed_since": False,
                  "page": None, "row": {"fields": {"po_number": {"value": "4500012345"}}}}
        self.assertEqual(_digest(pinned),
                         "052b1bc698dd6f8c6fe8277c9e607cdf7b13455874a8350e3286160218fefa4f")
        bytes_only = {key: value for key, value in pinned.items() if key != "row"}
        self.assertEqual(_digest({**pinned, "derived": ["row"]}), _digest(bytes_only))
        self.assertNotEqual(_digest(pinned), _digest(bytes_only))
        self.assertNotEqual(_digest({**pinned, "derived": "row"}), _digest(bytes_only))

    def test_changed_cited_content_is_reported_never_supported(self):
        # CR-003 regression: a handle that resolves to different content than
        # was cited (e.g. a reused capture id) must not keep the finding supported.
        case_id = self.build()
        other = self.store.finding(ENG_OPS, case_id, "Price row seen", "fact", "supported",
                                   [PRICE])
        recorded = self.store.get(OPS, case_id)
        digest = recorded["findings"]["supported"][0]["citations"][0]["digest"]
        self.resolver.rows[AO][0]["row"]["ordered"] = "9999"
        view = self.store.get(OPS, case_id)
        self.assertEqual([item["id"] for item in view["findings"]["supported"]],
                         [other["finding_id"]])
        changed = view["findings"]["citation_content_changed"]
        self.assertEqual([item["id"] for item in changed], ["F1"])
        self.assertIsNone(changed[0]["status"])
        self.assertEqual(changed[0]["recorded_status"], "supported")
        self.assertEqual(changed[0]["content"], "citation_content_changed")
        cite = changed[0]["citations"][0]
        self.assertEqual((cite["status"], cite["digest"]), ("citation_content_changed", digest))
        self.assertNotEqual(cite["current_digest"], digest)
        calc, literal = view["calculations"]
        self.assertEqual(calc["result"], "417")  # recorded history is never recomputed
        self.assertEqual(calc["inputs"]["ordered"]["value"], "1920")
        self.assertEqual(calc["inputs_status"], "citation_content_changed")
        self.assertEqual(calc["inputs"]["ordered"]["citation"]["status"],
                         "citation_content_changed")
        self.assertEqual(literal["inputs_status"], "resolved")
        self.resolver.rows[AO][0]["row"]["ordered"] = "1920"
        restored = self.store.get(OPS, case_id)
        self.assertEqual(restored["findings"]["citation_content_changed"], [])
        self.assertEqual(restored["calculations"][0]["inputs_status"], "resolved")
        self.assertEqual(restored["calculations"][0]["inputs"]["ordered"]["citation"]["status"],
                         "resolved")

    def test_superseded_finding_with_changed_content_stays_superseded(self):
        case_id = self.open_case()
        first = self.store.finding(ENG_OPS, case_id, "Old claim", "fact", "supported", [AO])
        self.store.supersede(ENG_OPS, case_id, first["finding_id"], "outdated")
        self.resolver.rows[AO][0]["row"]["ordered"] = "1"
        view = self.store.get(OPS, case_id)
        self.assertEqual(view["findings"]["citation_content_changed"], [])
        self.assertEqual(view["findings"]["superseded"][0]["citations"][0]["status"],
                         "citation_content_changed")


class GapsProposalsAndClose(CaseTestBase):
    def test_gap_states_are_distinct_and_validated(self):
        case_id = self.open_case()
        states = ("absent_in_source", "inaccessible", "stale", "not_ingested", "unknown")
        for state in states:
            self.store.gap(ENG_OPS, case_id, f"m-{state}", "matters", "Ricardo", state)
        gaps = self.store.get(ENG_OPS, case_id)["gaps"]
        self.assertEqual([gap["state"] for gap in gaps], list(states))
        self.assertEqual(gaps[0]["who_can_answer"], ["Ricardo"])
        self.assertEqual(gaps[0]["id"], "G1")
        with self.assertRaises(ValidationError):
            self.store.gap(ENG_OPS, case_id, "m", "w", "who", "missing")
        with self.assertRaises(ValidationError):
            self.store.gap(ENG_OPS, case_id, "", "w", "who", "unknown")
        self.store.gap(ENG_OPS, case_id, "m", "w", ["a", "b"], "unknown")

    def test_proposal_is_recorded_only(self):
        case_id = self.open_case()
        receipt = self.store.propose(ENG_OPS, case_id, "portal.sales_order.correct",
                                     {"order": "SO-1", "price": "12.50", "lines": [{"n": 1}]},
                                     ["order still open", {"check": "price unchanged"}])
        self.assertEqual(receipt["status"],
                         "proposed — requires approval through the existing approval journal")
        proposal = self.store.get(ENG_OPS, case_id)["proposals"][0]
        self.assertEqual(proposal["id"], "P1")
        self.assertEqual(proposal["capability"], "portal.sales_order.correct")
        self.assertEqual(proposal["payload"]["lines"], [{"n": 1}])
        self.assertEqual(proposal["preconditions"], ["order still open", {"check": "price unchanged"}])
        self.assertEqual(proposal["status"], receipt["status"])
        raw = sqlite3.connect(self.path)
        tables = {name for (name,) in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        raw.close()
        self.assertEqual(tables - {"sqlite_sequence"}, {"case_event"})
        for bad in ({"capability": "", "payload": {}}, {"capability": "c", "payload": []},
                    {"capability": "c", "payload": {}, "preconditions": "text"}):
            with self.assertRaises(ValidationError):
                self.store.propose(ENG_OPS, case_id, **bad)

    def test_close_and_closed_case_rejects_writes(self):
        case_id = self.open_case()
        with self.assertRaises(ValidationError):
            self.store.close(ENG_OPS, case_id, " ")
        self.store.close(ENG_OPS, case_id, "Shortfall explained; damage kept separate")
        view = self.store.get(ENG_OPS, case_id)
        self.assertEqual((view["status"], view["outcome"], view["closed_by"]),
                         ("closed", "Shortfall explained; damage kept separate", "eng,ops"))
        self.assertTrue(view["closed_at"])
        for call in (lambda: self.store.close(ENG_OPS, case_id, "again"),
                     lambda: self.store.finding(ENG_OPS, case_id, "x", "interpretation", "unknown"),
                     lambda: self.store.gap(ENG_OPS, case_id, "m", "w", "who", "unknown"),
                     lambda: self.store.propose(ENG_OPS, case_id, "c", {})):
            with self.assertRaises(CaseClosed):
                call()

    def test_open_validation(self):
        for question in ("", "  ", None, "x" * 4001, "bad\x00control"):
            with self.assertRaises(ValidationError):
                self.store.open(ENG, question)
        for roots in ("AO-1", [1], [""], ["r"] * 21):
            with self.assertRaises(ValidationError):
                self.store.open(ENG, "q", roots=roots)
        with self.assertRaises(ValidationError):
            self.store.open("eng", "q")
        self.assertEqual(self.store.get(ENG, self.store.open(ENG, "q", roots=None))["roots"], [])


class CitedGapsAndProposals(CaseTestBase):
    """Gaps and proposals that quote cited data are gated exactly like findings."""

    # Each secret holds a character no ISO timestamp or digest in a view does (a bare "12.50"
    # matched a time such as 05:46:12.500635), so a probe fails only on a real leak.
    QUOTED = "1,212.50"
    SECRETS = ("PO-77", QUOTED, PRICE, "sage_po_line", "unit_price")

    def build(self) -> str:
        case_id = self.open_case()
        self.store.gap(ENG_OPS, case_id, "Confirmed price for PO-77 line 1",
                       f"sage_po_line says {self.QUOTED} USD but no quote is on file",
                       ["Buyer"], "unknown", [PRICE])
        self.store.gap(ENG_OPS, case_id, "Inspection count", "not in any source",
                       "Quality lead", "not_ingested")
        self.store.propose(ENG_OPS, case_id, "portal.po_line.set_unit_price",
                           {"po": "PO-77", "line": 1, "unit_price": self.QUOTED},
                           [f"unit_price still {self.QUOTED} in sage_po_line"], [PRICE])
        return case_id

    def assert_redacted(self, view: dict) -> None:
        cited_gap, plain_gap = view["gaps"]
        self.assertEqual(cited_gap["content"], "withheld_citation_unavailable")
        for name in ("missing", "why", "who_can_answer", "state"):
            self.assertIsNone(cited_gap[name], name)
        self.assertEqual(cited_gap["citations"], [{"index": 0, "status": "citation_unavailable"}])
        self.assertEqual(plain_gap["missing"], "Inspection count")
        self.assertEqual(plain_gap["citations_status"], "uncited")
        [proposal] = view["proposals"]
        self.assertEqual(proposal["content"], "withheld_citation_unavailable")
        for name in ("capability", "payload", "preconditions"):
            self.assertIsNone(proposal[name], name)
        blob = json.dumps(view)
        for secret in self.SECRETS:
            self.assertNotIn(secret, blob, secret)

    def test_reader_without_the_citation_sees_neither_gap_nor_proposal_content(self):
        case_id = self.build()
        self.resolver.revoke(PRICE, "ops")
        self.assert_redacted(self.store.get(OPS, case_id))
        full = self.store.get(ENG, case_id)
        self.assertEqual(full["gaps"][0]["missing"], "Confirmed price for PO-77 line 1")
        self.assertEqual(full["gaps"][0]["citations_status"], "resolved")
        self.assertEqual(full["proposals"][0]["payload"]["unit_price"], self.QUOTED)
        self.assertEqual(full["proposals"][0]["citations"][0]["handle"], PRICE)

    def test_revocation_takes_effect_on_the_next_read_and_restoring_shows_it_again(self):
        case_id = self.build()
        self.assertEqual(self.store.get(OPS, case_id)["proposals"][0]["capability"],
                         "portal.po_line.set_unit_price")
        self.resolver.revoke(PRICE, "ops")
        self.assert_redacted(self.store.get(OPS, case_id))
        self.resolver.grant(PRICE, "ops")
        self.assertEqual(self.store.get(OPS, case_id)["gaps"][0]["state"], "unknown")

    def test_changed_citation_content_is_reported_not_vouched_for(self):
        case_id = self.build()
        self.resolver.rows[PRICE][0]["row"]["unit_price"] = "13.00"
        view = self.store.get(ENG_OPS, case_id)
        self.assertEqual(view["gaps"][0]["citations_status"], "citation_content_changed")
        self.assertEqual(view["proposals"][0]["citations_status"], "citation_content_changed")

    def test_unresolvable_citation_refuses_the_write(self):
        case_id = self.open_case()
        for write in (lambda: self.store.gap(OPS, case_id, "m", "w", "who", "unknown", [PRICE + "x"]),
                      lambda: self.store.propose(OPS, case_id, "c", {}, [], ["api://nope/1@1"])):
            with self.assertRaises(CitationUnresolvable):
                write()
        view = self.store.get(ENG_OPS, case_id)
        self.assertEqual((view["gaps"], view["proposals"]), ([], []))

    def test_withheld_items_do_not_name_the_rows_they_cite(self):
        case_id = self.open_case()
        self.store.finding(ENG_OPS, case_id, "Price and order", "fact", "supported", [PRICE, AO])
        self.store.propose(ENG_OPS, case_id, "c", {"x": "y"}, [], [PRICE, AO])
        self.resolver.revoke(PRICE, "ops")
        view = self.store.get(OPS, case_id)
        self.assertEqual(view["findings"]["withheld"][0]["citations"],
                         [{"index": 0, "status": "citation_unavailable"},
                          {"index": 1, "status": "resolved"}])
        self.assertEqual(view["proposals"][0]["citations"],
                         view["findings"]["withheld"][0]["citations"])
        self.assertNotIn(AO, json.dumps(view))

    def test_proposal_with_evidence_is_one_transaction(self):
        case_id = self.open_case()

        class Crashing(CaseStore):
            def _before_commit(self):
                raise RuntimeError("power cut")

        crashing = Crashing(self.path, citation_resolver=self.resolver)
        with self.assertRaises(RuntimeError):
            crashing.propose(ENG_OPS, case_id, "c", {"price": "12.50"}, [], [PRICE],
                             evidence={"statement": "PO-77 line 1 costs 12.50"})
        view = self.store.get(ENG_OPS, case_id)
        self.assertEqual([item["seq"] for item in view["timeline"]], [1])
        receipt = self.store.propose(ENG_OPS, case_id, "c", {"price": "12.50"}, [], [PRICE],
                                     evidence={"statement": "PO-77 line 1 costs 12.50"})
        self.assertEqual((receipt["proposal_id"], receipt["evidence_finding_id"],
                          receipt["duplicate"]), ("P1", "F1", False))
        view = self.store.get(ENG_OPS, case_id)
        self.assertEqual([item["kind"] for item in view["timeline"]],
                         ["case.opened", "finding.recorded", "proposal.recorded"])
        [finding] = view["findings"]["supported"]
        self.assertEqual((finding["id"], finding["kind"]), ("F1", "fact"))
        self.assertEqual(view["proposals"][0]["evidence_finding_id"], "F1")
        with self.assertRaises(ValidationError):  # a fact always needs a citation
            self.store.propose(ENG_OPS, case_id, "c", {}, [], [], evidence={"statement": "x"})
        self.assertTrue(self.store.verify_chain(ENG_OPS, case_id))

    def test_idempotency_key_returns_the_existing_proposal_and_is_never_shown(self):
        case_id = self.open_case()
        key = "k-" + "a" * 64
        first = self.store.propose(ENG_OPS, case_id, "c", {"price": "12.50"}, [], [PRICE],
                                   evidence={"statement": "costs 12.50"}, idempotency_key=key)
        again = self.store.propose(ENG_OPS, case_id, "c", {"price": "12.50"}, [], [PRICE],
                                   evidence={"statement": "costs 12.50"}, idempotency_key=key)
        self.assertTrue(again["duplicate"])
        self.assertEqual((again["proposal_id"], again["evidence_finding_id"], again["seq"]),
                         (first["proposal_id"], first["evidence_finding_id"], first["seq"]))
        view = self.store.get(ENG_OPS, case_id)
        self.assertEqual((len(view["proposals"]), view["integrity"]["events"]), (1, 3))
        self.assertNotIn(key, json.dumps(view))
        other = self.store.propose(ENG_OPS, case_id, "c", {"price": "12.50"}, [], [PRICE],
                                   idempotency_key=key + "b")
        self.assertEqual((other["proposal_id"], other["duplicate"]), ("P2", False))
        # The key alone never identifies a proposal: without the stored citations it is a
        # new proposal, so the key cannot confirm a proposal the caller could not read.
        uncited = self.store.propose(ENG_OPS, case_id, "c", {}, [], [], idempotency_key=key)
        self.assertEqual((uncited["proposal_id"], uncited["duplicate"]), ("P3", False))


class ConcurrentWriters(CaseTestBase):
    def test_two_connections_do_not_corrupt_sequence(self):
        case_id = self.open_case()
        stores = [CaseStore(self.path, citation_resolver=self.resolver) for _ in range(2)]
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []
        ids: list[str] = []

        def writer(store: CaseStore, name: str) -> None:
            try:
                barrier.wait()
                for index in range(25):
                    ids.append(store.finding(ENG_OPS, case_id, f"{name}-{index}", "fact",
                                             "supported", [AO])["finding_id"])
                    store.gap(ENG_OPS, case_id, f"{name}-gap-{index}", "w", "who", "unknown")
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(store, name))
                   for store, name in zip(stores, ("a", "b"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        view = self.store.get(ENG_OPS, case_id)
        self.assertEqual([item["seq"] for item in view["timeline"]], list(range(1, 102)))
        self.assertTrue(view["integrity"]["chain_ok"])
        self.assertEqual(len(set(ids)), 50)
        self.assertEqual(len(view["findings"]["supported"]), 50)
        self.assertEqual(len({gap["id"] for gap in view["gaps"]}), 50)
        raw = sqlite3.connect(self.path)
        self.assertEqual(raw.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        raw.close()

    def test_two_processes_share_one_case(self):
        case_id = self.open_case()
        code = ("import sys\nfrom ontology.core import Actor\nfrom operating.cases import CaseStore\n"
                "s = CaseStore(sys.argv[1])\na = Actor('plx', frozenset({'eng', 'ops'}))\n"
                "for i in range(15):\n"
                "    s.gap(a, sys.argv[2], sys.argv[3] + str(i), 'w', 'who', 'unknown')\n")
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        procs = [subprocess.Popen([sys.executable, "-c", code, str(self.path), case_id, name],
                                  cwd=REPO, env=env, stderr=subprocess.PIPE, text=True)
                 for name in ("p", "q")]
        for proc in procs:
            _, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err)
        view = self.store.get(ENG_OPS, case_id)
        self.assertEqual([item["seq"] for item in view["timeline"]], list(range(1, 32)))
        self.assertTrue(view["integrity"]["chain_ok"])
        self.assertEqual(len({gap["id"] for gap in view["gaps"]}), 30)

    def test_concurrent_opens_get_distinct_cases(self):
        stores = [CaseStore(self.path) for _ in range(3)]
        made: list[str] = []
        threads = [threading.Thread(target=lambda s=s: made.extend(s.open(ENG, "q") for _ in range(10)))
                   for s in stores]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(set(made)), 30)
        self.assertEqual(len(self.store.list(ENG, limit=200)), 30)


if __name__ == "__main__":
    unittest.main()
