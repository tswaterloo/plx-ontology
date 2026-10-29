"""The legacy objective/campaign/watch/autonomy family stays deleted.

Deleted 2026-10-01. Goal 2 runs on
operating/action_path.py, and the gate's
runtime-focused entry checks that path instead of the deleted runtime.
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
RETIRED = (
    "operating.autonomy_driver", "operating.business_campaign_plan_contract",
    "operating.business_watch", "operating.campaign_plan_contract", "operating.campaigns",
    "operating.delivery_planning", "operating.fm_goal_constraints",
    "operating.freight_contracts", "operating.freight_work",
    "operating.goal_campaign_planner", "operating.goal_intake", "operating.logistics",
    "operating.objectives", "operating.related_scope", "operating.source_goal_constraints",
    "operating.supplier_catalog", "scripts.run_business_demo", "scripts.run_transaction_demo",
)
# operating/unified.py still names the family.
PENDING_OWNER_DELETION = {"operating/unified.py"}
SCANNED = ("ontology", "integrations", "operating", "operator_ui", "ops_plane", "reasoning",
           "engineering", "scripts", "devloop", "evals", "tests")


def _imported_modules(path: Path) -> set[str]:
    relative = path.relative_to(ROOT)
    package = list(relative.with_suffix("").parts)
    if package[-1] == "__init__":
        package.pop()
    else:
        package = package[:-1]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                module = ".".join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


class ObjectiveFamilyRetiredTests(unittest.TestCase):
    def test_retired_modules_do_not_exist(self):
        for name in RETIRED:
            self.assertIsNone(importlib.util.find_spec(name), name)

    def test_no_code_imports_a_retired_module(self):
        scanned = 0
        for top in SCANNED:
            for path in sorted((ROOT / top).rglob("*.py")):
                relative = path.relative_to(ROOT).as_posix()
                if relative in PENDING_OWNER_DELETION or "__pycache__" in relative:
                    continue
                scanned += 1
                hits = _imported_modules(path) & set(RETIRED)
                self.assertFalse(hits, f"{relative} imports {sorted(hits)}")
        self.assertGreater(scanned, 200)

    def test_every_gate_unittest_target_exists(self):
        gate = json.loads((ROOT / "devloop" / "gate-checks.json").read_text(encoding="utf-8"))
        targets = []
        for check in gate["checks"]:
            # A check's command line is `command` then its `modules`.
            command = " ".join([check["command"], *check.get("modules", [])])
            for group in re.findall(r"unittest ((?:[\w.]+ ?)+)", command):
                targets.extend(group.split())
        self.assertTrue(targets)
        for target in targets:
            parts = target.split(".")
            modules = [".".join(parts[:end]) for end in range(len(parts), 0, -1)]
            self.assertTrue(any((ROOT / Path(*name.split("."))).with_suffix(".py").is_file()
                                for name in modules), target)

    def test_runtime_gate_checks_the_goal2_action_path(self):
        gate = json.loads((ROOT / "devloop" / "gate-checks.json").read_text(encoding="utf-8"))
        command = next(" ".join([check["command"], *check.get("modules", [])])
                       for check in gate["checks"] if check["name"] == "runtime-focused")
        modules = {target.rsplit(".", 2)[0] for target in command.split()[3:]}
        self.assertEqual(modules, {"operating.tests.test_action_path",
                                   "operating.tests.test_goal2_e2e"})
        for retired in ("test_autonomy_driver", "test_drive_objective_replan"):
            self.assertNotIn(retired, json.dumps(gate))


if __name__ == "__main__":
    unittest.main()
