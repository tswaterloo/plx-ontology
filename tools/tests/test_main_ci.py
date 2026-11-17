"""The full-suite runner of deploy/ops/suite_nightly.sh (isolated fixture: a temporary git repo of tiny tests)."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from tools import main_ci

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

PASSING = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n"
FAILING = ("import unittest\n\n\nclass T(unittest.TestCase):\n"
           "    def test_bad(self):\n        self.fail('broken on purpose')\n")
KEYLESS = ("import os\nimport unittest\n\n\nclass T(unittest.TestCase):\n"
           "    def test_no_credentials(self):\n"
           "        self.assertNotIn('PLX_WORKBENCH_API_KEY', os.environ)\n"
           "        self.assertNotIn('PLX_ONTOLOGY_PG', os.environ)\n")
KNOWN_RED = """# Known-red tests

## Currently red

| Module | Failure |
|---|---|
| `ontology.tests.test_red` | fails |
| `tests.test_fixed` | passes now |

## Fixed (earlier)

| `tests.test_bad` | listed here, but this section never counts |
"""

AUDIT_SCRIPT = (Path(__file__).resolve().parents[2] / "scripts" / "check_no_hardcoded_cases.py")


def denylist(*allowlist: dict) -> str:
    return json.dumps({"schema": "plx-hardcoded-case-denylist/v1", "identifiers": [],
                       "module_name_patterns": {"file_globs": [], "symbol_regex": "(?!)"},
                       "allowlist": list(allowlist)})


class SuiteRunnerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tree = Path(tmp.name)
        subprocess.run(["git", "init", "-q", str(self.tree)], check=True)
        self.files = {
            "tests/test_ok.py": PASSING, "tests/test_keyless.py": KEYLESS,
            "reasoning/tests/test_reasoning.py": PASSING,
            "evals/oracle/tests/test_families.py": PASSING,
            "ontology/tests/test_red.py": FAILING, "tests/test_fixed.py": PASSING,
            "tests/helper.py": "raise SystemExit('not a test module')\n",
            "tools/KNOWN-RED.md": KNOWN_RED,
            "scripts/check_no_hardcoded_cases.py": AUDIT_SCRIPT.read_text(),
            "tools/hardcoded-case-denylist.json": denylist()}
        for name, text in self.files.items():
            (self.tree / name).parent.mkdir(parents=True, exist_ok=True)
            (self.tree / name).write_text(text)
        subprocess.run(["git", "-C", str(self.tree), "add", "-A"], check=True)

    def add(self, name: str, text: str) -> None:
        (self.tree / name).write_text(text)
        subprocess.run(["git", "-C", str(self.tree), "add", name], check=True)

    def test_every_tracked_test_module_is_discovered_wherever_it_lives(self):
        self.assertEqual(main_ci.discover(str(self.tree)), [
            "evals.oracle.tests.test_families", "ontology.tests.test_red",
            "reasoning.tests.test_reasoning", "tests.test_fixed", "tests.test_keyless",
            "tests.test_ok"])
        # An untracked file is not part of the suite, a newly tracked one is.
        (self.tree / "scripts" / "test_untracked.py").write_text(PASSING)
        self.assertNotIn("scripts.test_untracked", main_ci.discover(str(self.tree)))
        self.add("scripts/test_untracked.py", PASSING)
        self.assertIn("scripts.test_untracked", main_ci.discover(str(self.tree)))
        # Product code named test_* (a rule module that defines no test) is not a test module.
        self.add("scripts/test_rule.py", "def is_test(name):\n    return name == 'TEST'\n")
        self.assertNotIn("scripts.test_rule", main_ci.discover(str(self.tree)))

    def run_suite(self, *extra: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(
                os.environ, {"PLX_WORKBENCH_API_KEY": "secret-key", "PLX_ONTOLOGY_PG": "only"}):
            code = main_ci.main(["--repo", str(self.tree), "--workers", "3", *extra])
        return code, out.getvalue()

    def test_known_red_is_reported_without_failing_and_credentials_are_stripped(self):
        code, out = self.run_suite()
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"known red +[\d.]+s ontology\.tests\.test_red")
        self.assertRegex(out, r"fixed \(delete its KNOWN-RED row\) +[\d.]+s tests\.test_fixed")
        self.assertRegex(out, r"pass +[\d.]+s reasoning\.tests\.test_reasoning")
        self.assertRegex(out, r"pass +[\d.]+s tests\.test_keyless")
        self.assertIn("6 modules: 5 pass, 1 known red, 0 FAIL", out)
        self.assertIn("allowlist audit: pass", out)
        self.assertNotIn("secret-key", out)

    def test_an_expired_allowlist_entry_fails_the_run_even_in_an_untouched_file(self):
        self.add("tools/hardcoded-case-denylist.json", denylist(
            {"path": "scripts/old.py", "rule": "identifier", "match": "X",
             "reason": "temporary", "expires": "2020-01-01"}))
        code, out = self.run_suite()
        self.assertEqual(code, 1, out)
        self.assertIn("::error::allowlist audit failed", out)
        self.assertIn("allowlist expired: scripts/old.py identifier X", out)
        self.assertIn("0 FAIL", out)  # no test module is blamed

    def test_an_unlisted_failure_fails_the_run_with_its_output(self):
        self.add("tests/test_bad.py", FAILING)
        code, out = self.run_suite()
        self.assertEqual(code, 1)
        self.assertRegex(out, r"FAIL +[\d.]+s tests\.test_bad")
        self.assertIn("::error::tests.test_bad failed", out)
        self.assertIn("broken on purpose", out)
        self.assertIn("1 FAIL", out)
        # The "Fixed" section of KNOWN-RED.md names tests.test_bad but excuses nothing.
        self.assertRegex(out, r"known red +[\d.]+s ontology\.tests\.test_red")

    def test_a_quarantined_module_failing_under_load_passes_alone_and_a_repeat_failure_blocks(self):
        # tools/FLAKY.md: a load-sensitive module that fails in the parallel run is re-run
        # alone once; one that also fails alone (a real regression) still fails the run.
        marker = self.tree / "first-run-done"
        self.add("tests/test_load.py", "import pathlib, unittest\n\n\nclass T(unittest.TestCase):\n"
                 "    def test_budget(self):\n        p = pathlib.Path(%r)\n"
                 "        first = not p.exists()\n        p.touch()\n"
                 "        self.assertFalse(first, 'over budget under load')\n" % str(marker))
        self.add("tests/test_bad.py", FAILING)
        self.add("tools/FLAKY.md", "# Load-sensitive\n\n## Quarantined\n\n| `tests.test_load` | x |\n"
                 "| `tests.test_bad` | x |\n\n## App\n\n| `tests.test_ok` | not parsed |\n")
        code, out = self.run_suite("--modules", "tests.test_load", "tests.test_bad", "--no-audit")
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"flaky, passed alone +[\d.]+s tests\.test_load")
        self.assertRegex(out, r"FAIL +[\d.]+s tests\.test_bad")
        self.assertIn("2 modules: 1 pass, 0 known red, 1 FAIL", out)
        self.assertNotIn("allowlist audit", out)

    def test_a_module_timed_out_under_load_is_rerun_with_a_longer_budget_once(self):
        # 11-16: 13 modules hit 900 s on a loaded box and pass in 260-830 s quiet. A module
        # that only timed out runs once more with the longer budget: passing then is a pass
        # (said so); timing out again, or failing, is red. A plain failure is never rerun.
        marker = self.tree / "slow-first-run"
        self.add("tests/test_slowfirst.py", "import pathlib, time, unittest\n\n\n"
                 "class T(unittest.TestCase):\n    def test_slow(self):\n"
                 "        p = pathlib.Path(%r)\n        if not p.exists():\n"
                 "            p.touch()\n            time.sleep(30)\n" % str(marker))
        self.add("tests/test_stuck.py", "import time, unittest\n\n\n"
                 "class T(unittest.TestCase):\n    def test_stuck(self):\n"
                 "        time.sleep(30)\n")
        self.add("tests/test_bad.py", FAILING)
        with mock.patch.object(main_ci, "MODULE_TIMEOUT_SECONDS", 3), \
                mock.patch.object(main_ci, "TIMED_OUT_RERUN_SECONDS", 6):
            code, lines = main_ci.run(str(self.tree), 3, audit=False, modules=[
                "tests.test_slowfirst", "tests.test_stuck", "tests.test_bad"])
        out = "\n".join(lines)
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"timed out under load, passed on rerun +[\d.]+s "
                              r"tests\.test_slowfirst")
        self.assertRegex(out, r"FAIL +[\d.]+s tests\.test_stuck")
        self.assertIn("timed out after 6 s", out)
        self.assertRegex(out, r"FAIL +[\d.]+s tests\.test_bad")
        self.assertIn("3 modules: 1 pass, 0 known red, 2 FAIL", out)

    def test_a_missing_known_red_file_excuses_nothing(self):
        subprocess.run(["git", "-C", str(self.tree), "rm", "-qf", "tools/KNOWN-RED.md"],
                       check=True)
        code, out = self.run_suite()
        self.assertEqual(code, 1)
        self.assertRegex(out, r"FAIL +[\d.]+s ontology\.tests\.test_red")


class RunnerTests(unittest.TestCase):
    """The per-module runner moved here from the integrate gate (owner, 2026-10-03)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tree = Path(tmp.name)
        slow = "import time\nimport unittest\n\n\nclass S(unittest.TestCase):\n" \
               "    def test_slow(self):\n        time.sleep(1.5)\n"
        for name in ("test_slow_a", "test_slow_b"):
            (self.tree / f"{name}.py").write_text(slow)

    def test_modules_run_in_parallel_with_their_own_durations(self):
        started = time.monotonic()
        runs = main_ci.run_modules(str(self.tree), ["test_slow_a", "test_slow_b"],
                                   dict(os.environ), 2)
        wall = time.monotonic() - started
        self.assertEqual([(module, passed) for module, passed, _tail, _s in runs],
                         [("test_slow_a", True), ("test_slow_b", True)])
        self.assertTrue(all(seconds >= 1.5 for *_rest, seconds in runs))
        self.assertLess(wall, sum(seconds for *_rest, seconds in runs))

    def test_a_module_is_bounded_by_a_timeout_and_fails(self):
        with mock.patch.object(main_ci, "MODULE_TIMEOUT_SECONDS", 1):
            ((module, passed, tail, _seconds),) = main_ci.run_modules(
                str(self.tree), ["test_slow_a"], dict(os.environ), 1)
        self.assertEqual((module, passed, tail), ("test_slow_a", False, "timed out after 1 s"))


class KnownRedTest(unittest.TestCase):
    """Only the "Currently red" section of KNOWN-RED.md lists modules, in any notation."""

    def test_every_notation_in_the_currently_red_section(self) -> None:
        text = """# Known-red tests

Run each module alone; `tests.test_title_prose` is not a listing.

## Currently red

- `ontology.tests.test_identifiers`: identifier fixture drift
* operating.tests.test_ao_26005b_case (clock)
1. `integrations/tests/test_path_form.py`
| `tests.test_table.SomeTest.test_one` | thread | open |
| `tests.test_cell` | first: test_two (a test, not a module) | open |
Every module in `tests/` and `operating/tests/` otherwise passes.

### Detail (still inside the section)

`./evals/tests/test_nested.py`.

## Fixed

| `tests.test_mc_handoff_http`, `tests.test_transaction_http` | timeouts | fixed |
"""
        self.assertEqual(main_ci.known_red(text), {
            "ontology.tests.test_identifiers", "operating.tests.test_ao_26005b_case",
            "integrations.tests.test_path_form", "tests.test_table", "tests.test_cell",
            "evals.tests.test_nested"})

    def test_nothing_is_listed_without_a_currently_red_section_or_file(self) -> None:
        for text in (None, "", "## Currently red\n\nNone.\n",
                     "# Known-red tests\n\n## Fixed\n\n- `tests.test_x`\n"):
            with self.subTest(text=text):
                self.assertEqual(main_ci.known_red(text), set())

    def test_the_repository_known_red_file_lists_only_its_table_rows(self) -> None:
        """Every module the runner excuses is an existing module named in a row's Module
        cell: a note in the section ("X left the list") excused X, so a passing module
        that later broke was reported known red (11-13: test_customer_profit,
        test_real_agent_accuracy)."""
        root = Path(__file__).resolve().parents[2]
        text = (root / main_ci.KNOWN_RED).read_text()
        listed = main_ci.known_red(text)
        cells = "\n".join(line.split("|")[1] for line in text.splitlines()
                          if line.startswith("|") and not line.startswith("|---"))
        self.assertEqual(listed, main_ci.known_red("## Currently red\n" + cells))
        for name in listed:
            with self.subTest(module=name):
                self.assertTrue((root / (name.replace(".", "/") + ".py")).is_file(), name)


class WorkflowTests(unittest.TestCase):
    """The repository's Actions allowance is spent (owner, 2026-11-05): no workflow may run."""

    def test_the_repository_carries_no_github_workflow(self):
        self.assertEqual(sorted(WORKFLOWS.glob("*.y*ml")) if WORKFLOWS.is_dir() else [], [])


class KeylessListTest(unittest.TestCase):
    def test_the_gate_main_ci_and_test_runs_strip_the_same_credentials(self):
        from twin import credential_names
        from tools import testslot
        self.assertEqual(credential_names.KEYLESS.pattern, main_ci.KEYLESS.pattern)
        self.assertEqual(credential_names.KEYLESS.pattern, testslot.KEYLESS.pattern)
        for name in ("PLX_WORKBENCH_API_KEY", "PLX_WORKBENCH_SESSION_SECRET", "BRAIN_API_KEY",
                     "MICROSOFT_GRAPH_CLIENT_SECRET", "BC_CLIENT_SECRET", "AWS_ACCESS_KEY_ID",
                     "CLOUDSDK_AUTH_ACCESS_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS", "GH_TOKEN",
                     "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                     "CLAUDE_ROUTINE_FIRE_TOKEN", "MC_GO_LIVE_TEAMS_URL", "custom_api_key"):
            self.assertTrue(credential_names.KEYLESS.match(name), name)
        for name in ("PATH", "HOME", "LANG", "PLX_TEST_SLOTS", "PLX_TEST_SLOT_DIR",
                     "PYTHONDONTWRITEBYTECODE", "TMPDIR"):
            self.assertFalse(credential_names.KEYLESS.match(name), name)

    def test_git_still_works_with_the_runtimes_injected_git_config(self):
        # The cloud runtime injects GIT_CONFIG_COUNT/KEY_n/VALUE_n; stripping only the
        # KEYs made every git call in a wrapped test run fail (exit 128).
        from twin import credential_names
        from tools import testslot
        for name in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
            self.assertTrue(credential_names.KEYLESS.match(name), name)
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "PLX_TEST_SLOTS": "1", "PLX_TEST_SLOT_DIR": tmp,
                   "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.pager",
                   "GIT_CONFIG_VALUE_0": "cat"}
            self.assertEqual(testslot.run(["git", "config", "--list"], env), 0)

if __name__ == "__main__":
    unittest.main()
