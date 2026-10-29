"""Hardcoded-case gate tests: positive (must flag) and negative (must pass) samples.

``scripts/check_no_hardcoded_cases.py`` is the `no-hardcoded-cases` gate check. These
tests build a temporary repo with the real denylist and prove what fails (case
literals in code, JSON values, JS strings, case-named modules/functions, expired
allowlist entries, identifiers of eval items nobody listed, product code reaching
evals/ or tests/) and what passes (docstrings, comments, provenance JSON keys,
tests/, live-checks and the eval tooling), and that the current tree passes today.
BaseAllowlistTests prove the base (origin/main) rules in a scratch git repo: a
candidate cannot add or extend an allowlist entry, an expired entry fails only a
candidate that changes its file, the ratchet only shrinks and a deleted eval item's
identifiers stay denylisted.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_no_hardcoded_cases as gate  # noqa: E402

TODAY = dt.date(2026, 10, 1)


class HardcodedCaseGateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        deny = json.loads((ROOT / gate.DENYLIST).read_text(encoding="utf-8"))
        deny["allowlist"] = []
        deny["ratchet"] = []
        self.deny = deny
        self._save_denylist()

    def _save_denylist(self):
        path = self.root / gate.DENYLIST
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.deny), encoding="utf-8")

    def _reset(self):
        for top in self.root.iterdir():
            if top.name != "devloop":
                shutil.rmtree(top)

    def write(self, relative: str, text: str):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def run_gate(self, today: dt.date = TODAY) -> tuple[int, str]:
        code, lines = gate.evaluate(self.root, today)
        return code, "\n".join(lines)

    def assert_fails(self, needle: str, today: dt.date = TODAY):
        code, report = self.run_gate(today)
        self.assertEqual(code, 1, report)
        self.assertIn(needle, report)

    def assert_passes(self):
        code, report = self.run_gate()
        self.assertEqual(code, 0, report)
        self.assertIn("violations=0", report)

    def test_empty_tree_passes(self):
        self.assert_passes()

    def test_literal_in_code_fails(self):
        for literal, case_id in (("AO-26005B", "AO-26005B"), ("ao-26005b", "AO-26005B"),
                                 ("26005B", "AO-26005B"), ("PO#5447", "PO5447"),
                                 ("PO 5447", "PO5447"), ("OR1000151885", "OR1000151885"),
                                 ("PO12172025", "PO12172025"), ("DVS-5003", "DVS-5003"),
                                 ("the SoapBox order", "SoapBox"), ("SR-101", "SR-101"),
                                 ("Diversified Hospitality", "Diversified Hospitality"),
                                 ("david simnick", "David Simnick")):
            with self.subTest(literal=literal):
                self.write("operating/general.py", f"X = 1\nORDER = {literal!r}\n")
                self.assert_fails(f"operating/general.py:2 identifier {case_id}=")

    def test_expected_value_numbers_fail(self):
        self.write("reasoning/general.py", "def f(q):\n    return q == 1509 or q == '1,503'\n")
        code, report = self.run_gate()
        self.assertEqual(code, 1)
        self.assertIn("reasoning/general.py:2 identifier AO-26005B-quantities='1509'", report)
        self.assertIn("reasoning/general.py:2 identifier AO-26005B-quantities='1,503'", report)
        self.write("reasoning/general.py", "ITEM = 10718\n")
        self.assert_fails("reasoning/general.py:1 identifier AO-26005B-item='10718'")

    def test_unrelated_numbers_pass(self):
        self.write("operating/general.py", "A = 15090\nB = 'PO54470'\nC = 1500\n")
        self.assert_passes()

    def test_docstring_and_comment_pass(self):
        self.write("operating/general.py",
                   '"""Module docstring: e.g. AO-26005B planned 1,509."""\n'
                   "# comment: PO5447 from SoapBox\n"
                   "class C:\n    '''Class docstring cites SR-101.'''\n"
                   "    def f(self):\n        \"\"\"Function docstring: OR1000151885.\"\"\"\n"
                   "        return 1  # DVS-5003\n")
        self.assert_passes()

    def test_second_string_statement_is_not_a_docstring(self):
        self.write("operating/general.py", '"""Doc."""\n"AO-26005B"\n')
        self.assert_fails("operating/general.py:2 identifier AO-26005B=")

    def test_fstring_part_fails(self):
        self.write("operating/general.py", "def f(x):\n    return f'{x} for PO5447'\n")
        self.assert_fails("operating/general.py:2 identifier PO5447=")

    def test_provenance_json_passes_and_other_value_fails(self):
        self.write("operating/contracts.json", json.dumps({
            "note": "seen on AO-26005B", "contracts": [{
                "id": "c1", "evidence": ["AO 10718 planned 1,509", {"why": "PO5447"}],
                "verification": "one live transaction (AO-26005B)",
                "sources": [{"source": "SoapBox thread"}]}]}, indent=1))
        self.assert_passes()
        self.write("operating/contracts.json", json.dumps({
            "note": "fine", "contracts": [{"id": "c1", "evidence": ["AO-26005B"],
                                           "order": "AO-26005B"}]}, indent=1))
        self.assert_fails("operating/contracts.json:9 identifier AO-26005B=")

    def test_json_number_and_key_fail(self):
        self.write("integrations/map.json", '{\n  "planned": 1509\n}\n')
        self.assert_fails("integrations/map.json:2 identifier AO-26005B-quantities='1509'")
        self.write("integrations/map.json", '{\n  "PO5447": {"x": 1}\n}\n')
        self.assert_fails("integrations/map.json:2 identifier PO5447=")

    def test_the_app_api_is_scanned(self):
        self.assertIn("operator_api", gate.SCANNED)
        self.write("operator_api/mock.py", "LABEL = 'PO 90412'\n")
        self.assert_passes()
        self.write("operator_api/mock.py", "LABEL = 'demo for PO5447'\n")
        self.assert_fails("operator_api/mock.py:1 identifier PO5447=")

    def test_js_string_literal_fails_and_comment_passes(self):
        self.write("operator_ui/app.js", "// demo for PO5447\n/* AO-26005B */\nconst a = 'ok';\n")
        self.assert_passes()
        self.write("operator_ui/app.js", "const a = 'x';\nconst b = `line\nfor AO-26005B`;\n")
        self.assert_fails("operator_ui/app.js:3 identifier AO-26005B=")
        self.write("operator_ui/app.js", 'const c = "SoapBox";\n')
        self.assert_fails("operator_ui/app.js:1 identifier SoapBox=")

    def test_case_named_module_and_symbol_fail(self):
        for name in ("operating/order_case_script.py", "operating/packaging_case.py",
                     "operating/ao_originals_expected.json",
                     "operator_ui/case_fixtures.py", "engineering/soapbox_costs.py",
                     "scripts/run_26005b.py", "ontology/po5447_links.py"):
            with self.subTest(name=name):
                self._reset()
                self.write(name, "{}\n" if name.endswith(".json") else "X = 1\n")
                self.assert_fails(f"{name}:1 module_name")
        self._reset()
        self.write("operating/general.py", "def reconcile_ao26005b():\n    pass\n")
        self.assert_fails("operating/general.py:1 module_name reconcile_ao26005b")
        self.write("operating/general.py", "class SoapBoxLandedCost:\n    pass\n")
        self.assert_fails("operating/general.py:1 module_name SoapBoxLandedCost")

    def test_tests_live_checks_and_docs_are_ignored(self):
        for name in ("tests/test_x.py", "operating/tests/test_y.py",
                     "devloop/live-checks/ao.json", "devloop/CONTEXT/x.py",
                     "devloop/tests/test_z.py", "docs/x.py", "EVIDENCE/x.json",
                     "examples/x.py", "operator_ui/x.test.js", "operating/README.md",
                     "devloop/sub/nested.py", "devloop/notes.json"):
            body = '{"order": "AO-26005B"}' if name.endswith(".json") else "X = 'AO-26005B'\n"
            self.write(name, body)
        self.write("tests/ao_case_script.py", "X = 1\n")
        self.assert_passes()

    def test_top_level_devloop_python_is_scanned(self):
        self.write("devloop/controller.py", "X = 'PO5447'\n")
        self.assert_fails("devloop/controller.py:1 identifier PO5447=")

    def test_allowlist_passes_before_expiry_and_fails_after(self):
        self.write("operating/packaging_case_script.py", "X = 'SoapBox example'\n")
        self.assert_fails("module_name")
        self.deny["allowlist"] = [
            {"path": "operating/packaging_case_script.py", "rule": rule, "match": match,
             "reason": "case script pending deletion", "expires": "2026-10-15"}
            for rule, match in (("module_name", "packaging_case_script.py"),
                                ("identifier", "SoapBox"))]
        self._save_denylist()
        code, report = self.run_gate(dt.date(2026, 10, 15))
        self.assertEqual(code, 0, report)
        self.assertIn("allowlisted=2 expiring_soonest=2026-10-15", report)
        self.assert_fails("allowlist expired: operating/packaging_case_script.py identifier "
                          "SoapBox (case script pending deletion) — delete or generalise",
                          today=dt.date(2026, 10, 16))

    def test_allowlist_is_per_rule_and_per_path(self):
        self.write("operating/general.py", "X = 'AO-26005B'\n")
        self.write("operating/other.py", "Y = 'AO-26005B'\n")
        self.deny["allowlist"] = [{"path": "operating/general.py", "rule": "module_name",
                                   "match": "general.py", "reason": "r",
                                   "expires": "2026-12-31"},
                                  {"path": "operating/other.py", "rule": "identifier",
                                   "match": "AO-26005B", "reason": "r",
                                   "expires": "2026-12-31"}]
        self._save_denylist()
        code, report = self.run_gate()
        self.assertEqual(code, 1)
        self.assertIn("operating/general.py:1 identifier", report)
        self.assertNotIn("operating/other.py:1 identifier", report)
        self.assertIn("allowlist unused (delete it): operating/general.py module_name "
                      "general.py", report)

    def test_allowlist_is_per_match_within_a_file(self):
        self.write("operating/other.py", "Y = 'AO-26005B'\nZ = 'PO5447'\n")
        self.deny["allowlist"] = [{"path": "operating/other.py", "rule": "identifier",
                                   "match": "AO-26005B", "reason": "r",
                                   "expires": "2026-12-31"}]
        self._save_denylist()
        code, report = self.run_gate()
        self.assertEqual(code, 1, report)
        self.assertNotIn("operating/other.py:1 identifier", report)
        self.assertIn("operating/other.py:2 identifier PO5447='PO5447'", report)

    def test_expired_unused_entry_still_fails(self):
        self.deny["allowlist"] = [{"path": "operating/gone.py", "rule": "identifier",
                                   "match": "PO5447", "reason": "deleted",
                                   "expires": "2026-09-30"}]
        self._save_denylist()
        self.assert_fails("allowlist expired: operating/gone.py identifier PO5447 (deleted)")

    def test_bad_schema_or_entry_is_rejected(self):
        for bad in ({"path": "x.py", "rule": "everything", "match": "PO5447",
                     "reason": "r", "expires": "2026-12-31"},
                    {"path": "x.py", "rule": "identifier", "reason": "whole file",
                     "expires": "2026-12-31"}):
            with self.subTest(bad=bad):
                self.deny["allowlist"] = [bad]
                self._save_denylist()
                with self.assertRaises(ValueError):
                    gate.evaluate(self.root, TODAY)
        self.deny["schema"] = "other/v1"
        self.deny["allowlist"] = []
        self._save_denylist()
        with self.assertRaises(ValueError):
            gate.evaluate(self.root, TODAY)

    def test_eval_and_test_references_in_product_code_fail(self):
        # The shape of the sites found on main: a product constant built from an evals/
        # path (ao_case_script.ORIGINALS_EXPECTED) imported by other product modules inside
        # a function (case_workspace_live, case_console), and a console seeding tests/.
        self.write("operating/case_script.py",
                   "from pathlib import Path\n"
                   "EXPECTED = Path(__file__).parents[1] / 'evals' / 'dev' / 'x.json'\n"
                   "def load(path=EXPECTED):\n    return path\n")
        self.write("operating/workspace.py", "def main():\n"
                   "    from .case_script import EXPECTED\n    return EXPECTED\n")
        self.write("operator_ui/console.py", "import operating.case_script as cs\n"
                   "def seed():\n    from .tests import case_fixtures\n"
                   "    return cs.load()\n")
        self.write("integrations/reader.py",
                   "from evals import score\nFIXTURE = 'tests/fixtures/x.json'\n")
        self.write("operator_ui/app.js", "const a = 'evals/exposed/sources.json';\n")
        self.write("scripts/registry.py", "from scripts.eval_status import main\n"
                   "from scripts import check_oracle_imports\n")
        code, report = self.run_gate()
        self.assertEqual(code, 1, report)
        for needle in ("operating/case_script.py:2 eval_reference path evals",
                       "operating/workspace.py:2 eval_reference derived "
                       "operating.case_script.EXPECTED",
                       "operator_ui/console.py:3 eval_reference import operator_ui.tests",
                       "operator_ui/console.py:4 eval_reference derived "
                       "operating.case_script.load",
                       "integrations/reader.py:1 eval_reference import evals",
                       "integrations/reader.py:2 eval_reference path tests/fixtures/x.json",
                       "operator_ui/app.js:1 eval_reference path evals/exposed/sources.json",
                       "scripts/registry.py:1 eval_reference import scripts.eval_status",
                       "scripts/registry.py:2 eval_reference import "
                       "scripts.check_oracle_imports"):
            self.assertIn(needle, report)
        # Fixed generally (documents found through the product's own lookup), with eval
        # data named only in prose, tests and the eval tooling: clean.
        self._reset()
        self.write("operating/case_script.py",
                   '"""Its expected identity lives in evals/dev/cases (prose)."""\n'
                   "def locate(find, code):\n    return find(code)\n")
        self.write("operating/workspace.py", "from .case_script import locate\n"
                   "def main(find):\n    return locate(find, 'x') or 'tests' in ('a',)\n")
        self.write("operating/tests/test_workspace.py", "from evals import score\n")
        self.write("scripts/eval_workspace.py", "from evals import runner\nD = 'evals/dev'\n")
        # A check_* script that reads no eval data is a shared helper, not eval tooling.
        self.write("scripts/registry.py", "from scripts.check_upstream_drift import checkout_for\n")
        self.assert_passes()

    def test_eval_item_identifiers_are_denylisted_without_listing_them(self):
        item = {"format": "plx-eval-item/v1", "id": "dev-F7-9001",
                "question": {"text": "For ZQ-77341, what did invoice INV-SK40321 bill?"},
                "root": {"identifier": "ZQ-77341", "identifier_kind": "po_number"},
                "known_outcome": {"values": [{"name": "billed", "value": "4,821.75"},
                                             {"name": "lines", "value": 378},
                                             {"name": "billed_on", "value": "2026-01-07"}]}}
        self.write("evals/dev/dev-F7-9001.json", json.dumps(item))
        self.write("operating/general.py", "A = 'zq77341'\nB = 4821.75\nC = 'INV-SK40321'\n"
                   "D = 378\nE = '2026-01-07'\n")
        code, report = self.run_gate()
        self.assertEqual(code, 1, report)
        for line, case_id in ((1, "ZQ-77341"), (2, "4,821.75"), (3, "INV-SK40321")):
            self.assertIn(f"operating/general.py:{line} identifier eval:{case_id}=", report)
        self.assertNotIn("operating/general.py:4", report)  # small numbers, dates: no
        self.assertNotIn("operating/general.py:5", report)
        # Over the real evaluation data: values no hand-listed pattern covers.
        self._reset()
        for path in (ROOT / "evals").rglob("*.json"):
            self.write(path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8"))
        self.write("operating/general.py", "X = ['PI-SWC024613', 'P-02458', 'F-18-0025', "
                   "'AO-26005C.pdf', 6060.25, 'FXSO100048']\n")
        code, report = self.run_gate()
        self.assertEqual(code, 1, report)
        for case_id in ("PI-SWC024613", "P-02458", "F-18-0025", "AO-26005C.pdf",
                        "6060.25", "FXSO100048"):
            self.assertIn(f"operating/general.py:1 identifier eval:{case_id}=", report)

    def test_main_exit_codes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gate.main([str(self.root)]), 0)
            self.write("operating/general.py", "X = 'PO5447'\n")
            self.assertEqual(gate.main([str(self.root)]), 1)
        self.assertIn("operating/general.py:1 identifier PO5447='PO5447'", out.getvalue())
        self.assertTrue(out.getvalue().rstrip().endswith("no-hardcoded-cases: FAIL"))

    def test_real_repo_passes_today(self):
        code, lines = gate.evaluate(ROOT)
        self.assertEqual(code, 0, "\n".join(lines))

    def test_allowlist_entries_expire_by_mid_october(self):
        deny = json.loads((ROOT / gate.DENYLIST).read_text(encoding="utf-8"))
        for entry in deny["allowlist"]:
            self.assertLessEqual(entry["expires"], "2026-10-15", entry)
            self.assertTrue((ROOT / entry["path"]).is_file(), entry)
            self.assertTrue(entry.get("match"), entry)

    def test_stage_map_is_case_free_and_its_example_lives_in_evals(self):
        deny = gate.load_denylist(ROOT)
        stage_map = "operating/process_stage_map.json"
        text = (ROOT / stage_map).read_text(encoding="utf-8")
        self.assertEqual(gate.scan_json(text, stage_map, deny), [])
        cases = "evals/dev/cases/packaging_stage_map_cases.json"
        found = {key for _, _, _, key, _ in
                 gate.scan_json((ROOT / cases).read_text(encoding="utf-8"), cases, deny)}
        self.assertTrue({"PO5447", "SoapBox", "David Simnick"} <= found, found)
        raw = json.loads((ROOT / gate.DENYLIST).read_text(encoding="utf-8"))
        # The last three entries (the case scripts) retired in task allowlist-3.
        self.assertEqual(raw["allowlist"], [])


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c",
                           "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                           *args], check=True, capture_output=True, text=True).stdout


class BaseAllowlistTests(unittest.TestCase):
    """The base ref (main) is authoritative for the allowlist; see the checker
    docstring. A git repo whose ``base`` branch plays origin/main and whose HEAD is
    the candidate."""

    LEGACY = "operating/legacy_case.py"
    OTHER = "operating/other_case.py"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        _git(self.root, "init", "-q", "-b", "base")
        deny = json.loads((ROOT / gate.DENYLIST).read_text(encoding="utf-8"))
        deny["allowlist"] = [self.entry(self.LEGACY, "2026-10-15"),
                             self.entry(self.OTHER, "2026-10-08")]
        deny["ratchet"] = []
        self.deny = deny
        self.save()
        self.write(self.LEGACY, "X = 'PO5447'\n")
        self.write(self.OTHER, "Y = 'PO5447'\n")
        self.write("operating/general.py", "Z = 1\n")
        self.commit("main state")
        _git(self.root, "checkout", "-q", "-b", "candidate")

    @staticmethod
    def entry(path: str, expires: str, match: str = "PO5447") -> dict:
        return {"path": path, "rule": "identifier", "match": match,
                "reason": "pending burn-down", "expires": expires}

    def save(self):
        self.write(gate.DENYLIST, json.dumps(self.deny, indent=1))

    def write(self, relative: str, text: str):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, message: str):
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", message)

    def run_gate(self, today=TODAY, base="base", **kw) -> tuple[int, str]:
        code, lines = gate.evaluate(self.root, today, base=base, **kw)
        return code, "\n".join(lines)

    def test_unchanged_candidate_passes(self):
        code, report = self.run_gate()
        self.assertEqual(code, 0, report)
        self.assertIn("allowlisted=2", report)
        self.assertIn("base=base", report)

    def test_candidate_adds_allowlist_entry_is_refused(self):
        self.write("operating/general.py", "Z = 'PO5447'\n")
        self.deny["allowlist"].append(self.entry("operating/general.py", "2026-10-15"))
        self.save()
        self.commit("sneak in an entry")
        code, report = self.run_gate()
        self.assertEqual(code, 1, report)
        self.assertIn("allowlist refused (candidate adds an entry the base does not "
                      "have): operating/general.py identifier PO5447", report)
        self.assertIn("operating/general.py:1 identifier PO5447='PO5447'", report)

    def test_candidate_widens_entry_to_another_match_is_refused(self):
        self.write(self.LEGACY, "X = 'PO5447'\nW = 'SoapBox'\n")
        self.deny["allowlist"].append(self.entry(self.LEGACY, "2026-10-15", "SoapBox"))
        self.save()
        code, report = self.run_gate()
        self.assertEqual(code, 1, report)
        self.assertIn("allowlist refused (candidate adds an entry the base does not "
                      f"have): {self.LEGACY} identifier SoapBox", report)
        self.assertIn(f"{self.LEGACY}:2 identifier SoapBox=", report)

    def test_candidate_extends_expiry_is_refused(self):
        self.deny["allowlist"][1]["expires"] = "2026-12-31"
        self.save()
        self.commit("extend")
        code, report = self.run_gate()
        self.assertEqual(code, 1, report)
        self.assertIn("allowlist refused (candidate extends expiry to 2026-12-31 past "
                      f"the base's 2026-10-08): {self.OTHER} identifier PO5447", report)
        self.assertIn(f"{self.OTHER}:1 identifier PO5447=", report)

    def test_candidate_may_shrink_or_shorten(self):
        self.write(self.LEGACY, "X = 1\n")
        self.deny["allowlist"] = [self.entry(self.OTHER, "2026-10-05")]
        self.save()
        code, report = self.run_gate()
        self.assertEqual(code, 0, report)
        self.assertIn("allowlisted=1 expiring_soonest=2026-10-05", report)

    def test_candidate_cannot_drop_a_base_identifier(self):
        self.deny["identifiers"] = [i for i in self.deny["identifiers"]
                                    if i["id"] not in ("PO5447", "PO5447-number")]
        self.deny["allowlist"] = []
        self.save()
        code, report = self.run_gate()
        self.assertEqual(code, 1, report)
        self.assertIn(f"{self.OTHER}:1 identifier PO5447=", report)

    def test_expired_entry_in_untouched_file_passes(self):
        self.write("operating/general.py", "Z = 2\n")
        self.commit("unrelated work")
        code, report = self.run_gate(today=dt.date(2026, 10, 9))
        self.assertEqual(code, 0, report)
        self.assertIn("allowlist expired, not enforced (file unchanged since base): "
                      f"{self.OTHER} identifier PO5447", report)

    def test_expired_entry_in_changed_file_fails(self):
        self.write(self.OTHER, "Y = 'PO5447'\nV = 2\n")
        self.commit("touch the expired file")
        code, report = self.run_gate(today=dt.date(2026, 10, 9))
        self.assertEqual(code, 1, report)
        self.assertIn(f"allowlist expired: {self.OTHER} identifier PO5447 "
                      "(pending burn-down) — delete or generalise", report)
        self.assertNotIn(f"allowlist expired: {self.LEGACY}", report)

    def test_expired_entry_fails_for_uncommitted_or_untracked_change(self):
        self.write(self.OTHER, "Y = 'PO5447'\nV = 3\n")  # working tree only
        code, report = self.run_gate(today=dt.date(2026, 10, 9))
        self.assertEqual(code, 1, report)
        self.assertIn(f"allowlist expired: {self.OTHER}", report)

    def test_strict_expiry_fails_every_expired_entry(self):
        code, report = self.run_gate(today=dt.date(2026, 10, 9), strict_expiry=True)
        self.assertEqual(code, 1, report)
        self.assertIn(f"allowlist expired: {self.OTHER}", report)

    def test_legacy_whole_file_base_entry_covers_narrowed_candidate_entry(self):
        legacy = dict(self.deny)
        legacy["allowlist"] = [{k: v for k, v in e.items() if k != "match"}
                               for e in self.deny["allowlist"]]
        _git(self.root, "checkout", "-q", "base")
        self.write(gate.DENYLIST, json.dumps(legacy, indent=1))
        self.commit("legacy per-file allowlist on base")
        _git(self.root, "checkout", "-q", "-B", "candidate")
        self.save()  # candidate narrows to per-match entries
        self.commit("narrow")
        code, report = self.run_gate()
        self.assertEqual(code, 0, report)
        self.assertIn("allowlisted=2", report)

    def test_ratchet_only_shrinks_and_ignores_moved_lines(self):
        legacy = "operating/legacy_eval.py"
        _git(self.root, "checkout", "-q", "base")
        self.write(legacy, "from evals import score\n")
        self.deny["ratchet"] = [{"path": legacy, "rule": "eval_reference",
                                 "match": "import evals", "occurrence": 0}]
        self.save()
        self.commit("main holds one ratcheted eval reference")
        _git(self.root, "checkout", "-q", "-B", "candidate")
        # Unrelated edits that move the ratcheted line pass.
        self.write(legacy, "X = 1\nY = 2\nfrom evals import score\n")
        code, report = self.run_gate()
        self.assertEqual(code, 0, report)
        self.assertIn("ratcheted=1 violations=0", report)
        # A planted new violation fails, and listing it in the ratchet is refused; a second
        # occurrence of a ratcheted reference in the same file is new too.
        self.write("operating/general.py", "from tests.helpers import fixture\n")
        self.write(legacy, "from evals import score\nfrom evals import runner\n")
        self.deny["ratchet"].append({"path": "operating/general.py", "rule": "eval_reference",
                                     "match": "import tests.helpers", "occurrence": 0})
        self.save()
        code, report = self.run_gate()
        self.assertEqual(code, 1, report)
        self.assertIn("operating/general.py:1 eval_reference import tests.helpers", report)
        self.assertIn("ratchet refused (candidate adds an entry the base does not have): "
                      "operating/general.py eval_reference import tests.helpers #0", report)
        self.assertIn(f"{legacy}:2 eval_reference import evals", report)
        self.assertNotIn(f"{legacy}:1 eval_reference", report)
        # Fixed while the entry stays: the stale entry fails until it is removed.
        self.write("operating/general.py", "Z = 1\n")
        self.write(legacy, "X = 1\n")
        self.deny["ratchet"].pop()
        self.save()
        code, report = self.run_gate()
        self.assertEqual(code, 1, report)
        self.assertIn("ratchet stale (no such finding any more; remove the entry): "
                      f"{legacy} eval_reference import evals #0", report)
        self.deny["ratchet"] = []
        self.save()
        code, report = self.run_gate()
        self.assertEqual(code, 0, report)

    def test_deleting_an_eval_item_does_not_drop_its_identifiers(self):
        item = {"format": "plx-eval-item/v1", "root": {"identifier": "ZQ-77341"}}
        _git(self.root, "checkout", "-q", "base")
        self.write("evals/exposed/dev-F7-9001-exposed.json", json.dumps(item))
        self.commit("main holds an eval item")
        _git(self.root, "checkout", "-q", "-B", "candidate")
        (self.root / "evals/exposed/dev-F7-9001-exposed.json").unlink()
        self.write("operating/general.py", "Z = 'ZQ-77341'\n")
        self.commit("special-case it and hide the item")
        code, report = self.run_gate()
        self.assertEqual(code, 1, report)
        self.assertIn("operating/general.py:1 identifier eval:ZQ-77341=", report)

    def test_base_without_denylist_bootstraps_from_candidate(self):
        _git(self.root, "checkout", "-q", "--orphan", "empty")
        _git(self.root, "rm", "-rq", "--cached", ".")
        self.write("README", "x\n")
        _git(self.root, "add", "README")
        _git(self.root, "commit", "-q", "-m", "no denylist")
        _git(self.root, "checkout", "-q", "-f", "candidate")
        for base in ("empty", "no-such-ref"):
            with self.subTest(base=base):
                code, report = self.run_gate(base=base)
                self.assertEqual(code, 0, report)
                self.assertIn(f"bootstrap: no denylist at base {base}", report)
                self.assertIn("base=none", report)
                code, report = self.run_gate(today=dt.date(2026, 10, 9), base=base)
                self.assertEqual(code, 1, report)
                self.assertIn(f"allowlist expired: {self.OTHER}", report)


if __name__ == "__main__":
    unittest.main()
