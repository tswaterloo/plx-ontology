"""Admission rule for proposed eval units (evals.admission)."""
from __future__ import annotations

import ast
import copy
import json
import subprocess
import unittest
from pathlib import Path

from evals import runner
from evals.admission import GENERATED_ORACLE_IDS, SYNTHETIC_LINEAGE_PREFIXES, admissible
from evals.oracle import generator
from evals.oracle.families import ORACLES

ROOT = Path(__file__).resolve().parents[2]
BRANCH = "origin/claude/verify-task-evals-employee-20261005T201622Z"
PROPOSED = "evals/proposed/employee-20261005T201622Z/"


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text())


class AdmissionTest(unittest.TestCase):
    def test_dev_and_exposed_items_are_admissible(self):
        self.assertEqual(admissible(load("evals/dev/dev-F1-0001-fixture.json")), [])
        self.assertEqual(admissible(load("evals/exposed/dev-F7-0001-exposed.json")), [])
        # Every committed item is admitted, the FM-bronze exposed AO case included.
        for directory in runner.VISIBLE_ITEM_DIRS:
            self.assertEqual(runner.load_items(directory, split="dev").refused, [])

    def test_lineage_prefixes_are_the_generators(self):
        """Each stratum ``lineage`` returns (bar the never-sampled staging) and the BC slug
        prefix: a lineage the generator learns cannot drift from the admission rule."""
        tree = ast.parse(Path(generator.__file__).read_text(encoding="utf-8"))
        returned = set()
        for fn in tree.body:
            if isinstance(fn, ast.FunctionDef) and fn.name in ("lineage", "fm_po_lineage"):
                for node in ast.walk(fn):
                    if isinstance(node, ast.Return):
                        value = node.value
                        branches = [value.body, value.orelse] if isinstance(
                            value, ast.IfExp) else [value]
                        returned |= {b.value for b in branches if isinstance(b, ast.Constant)}
        sampled = returned - {"gold_native_staging"}  # never sampled
        self.assertEqual(sampled, {"gold_etl_from_sage", "gold_etl_from_fm", "fm_bronze",
                                   "sage_bronze", "silver_view"})
        self.assertIs(SYNTHETIC_LINEAGE_PREFIXES, generator.LINEAGE_PREFIXES)
        bc = generator._slug("FX Distribution")
        self.assertEqual({p for p in SYNTHETIC_LINEAGE_PREFIXES
                          for s in sampled | {bc} if s.startswith(p)},
                         set(SYNTHETIC_LINEAGE_PREFIXES))  # no stale prefix
        item = load("evals/dev/dev-F1-0001-fixture.json")
        for stratum in sorted(sampled) + [bc]:
            item["lineage_stratum"] = stratum
            self.assertEqual(admissible(item), [], stratum)
        self.assertFalse("gold_native_staging".startswith(SYNTHETIC_LINEAGE_PREFIXES))
        # Generated-ness is the family oracle's id, from the generator's own oracles.
        self.assertIs(GENERATED_ORACLE_IDS, generator.ORACLE_ID_PREFIXES)
        self.assertEqual(set(GENERATED_ORACLE_IDS),
                         {f"oracle:{oracle.name}@" for oracle in ORACLES.values()})

    def test_real_lineage_is_refused_outside_exposed_and_gold_inside_it(self):
        """A real-data unit dressed as closed with a value (the employee unit's provenance)
        stays out of dev; an exposed item rooted at a gold copy stays out of exposed."""
        item = load("evals/dev/dev-F1-0001-fixture.json")
        item.update(lineage_stratum="bc_production", closed_transaction=True)
        item["oracle"].update(id="oracle:bc-live@67d8d678", derived_from=["bc:production"])
        item["known_outcome"]["values"] = [{"name": "status", "value": "Open"}]
        self.assertEqual(admissible(item),
                         ["lineage_partition_mismatch", "live_source_derivation"])
        exposed = load("evals/exposed/dev-F7-0001-exposed.json")
        for lineage in ("gold_etl_from_fm", "gold_native_staging"):
            exposed["lineage_stratum"] = lineage
            self.assertEqual(admissible(exposed), ["lineage_partition_mismatch"], lineage)
        exposed = load("evals/exposed/dev-F7-0001-exposed.json")
        exposed["oracle"]["id"] = generator.ORACLE_ID_PREFIXES[0] + "0" * 12
        self.assertEqual(admissible(exposed), ["lineage_partition_mismatch"])

    def test_exposed_values_without_pinned_citations_are_refused(self):
        item = load("evals/exposed/dev-F7-0001-exposed.json")
        for value in item["known_outcome"]["values"]:
            value.pop("citations", None)
        self.assertIn("uncited_values", admissible(item))
        item = load("evals/exposed/dev-F7-0001-exposed.json")
        item["oracle"]["derived_from"] = ["bc:production"]
        self.assertIn("uncited_values", admissible(copy.deepcopy(item)))

    def test_open_empty_real_unit_is_refused(self):
        item = load("evals/dev/dev-F1-0001-fixture.json")
        item.update(closed_transaction=False, lineage_stratum="sharepoint")
        item["known_outcome"].update(values=[], gaps=[])
        self.assertEqual(admissible(item), [
            "empty_outcome", "open_transaction", "lineage_partition_mismatch"])

    def test_branch_units_are_refused(self):
        listed = subprocess.run(["git", "ls-tree", "--name-only", BRANCH, PROPOSED],
                                cwd=ROOT, capture_output=True, text=True)
        if listed.returncode:
            self.skipTest("employee proposal branch not fetched")
        recorded = {unit["unit"]: unit["reasons"] for unit in
                    load("evals/proposed-dispositions.json")["refused"]}
        for name in listed.stdout.split():
            item = json.loads(subprocess.run(["git", "show", f"{BRANCH}:{name}"], cwd=ROOT,
                                             capture_output=True, text=True, check=True).stdout)
            self.assertEqual(admissible(item), recorded[item["id"]])
        self.assertEqual(len(recorded), 3)


if __name__ == "__main__":
    unittest.main()
