"""Deletion-11: dead adapters and tooling stay gone.

No live-check, MCP tool, console view, gate check or CONTEXT/01 requirement reached
these modules: the GitHub snapshot importer, the legacy Portal/BC link projector over
the retired Portal-gold store, the JSONL fixture loader, the snapshot rule engine
(``reasoning``) and two pre-astra packaging/inventory scripts. The package facade
``integrations`` no longer re-exports any client, so importing one adapter never
loads another.
"""

from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
RETIRED_MODULES = ("integrations.github", "integrations.portal_bc_links", "ontology.fixtures",
                   "reasoning", "reasoning.engine", "reasoning.rules",
                   "scripts.package_release", "scripts.generate_audit_inventory")
SCANNED = ("ontology", "integrations", "operating", "operator_ui", "scripts", "devloop",
           "evals", "tests")


def _env() -> dict[str, str]:
    # Never hand credentials to a child process (no business-system calls here).
    return {key: value for key, value in os.environ.items()
            if not any(mark in key for mark in ("KEY", "SECRET", "TOKEN", "PASSWORD"))}


def _spec(name: str):
    """The module's spec, or None. A leftover ``__pycache__`` directory makes a deleted
    package an empty namespace package (no origin): that counts as gone."""
    try:
        spec = importlib.util.find_spec(name)
    except ModuleNotFoundError:  # its parent package is gone too
        return None
    return spec if spec is not None and spec.origin not in (None, "namespace") else None


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


class AdaptersRetiredTests(unittest.TestCase):
    def test_retired_modules_do_not_exist(self):
        for name in RETIRED_MODULES:
            self.assertIsNone(_spec(name), name)
        self.assertFalse((ROOT / "reasoning" / "__init__.py").exists())
        self.assertFalse((ROOT / "integrations/tests/pinned-github-snapshots.json").exists())

    def test_no_first_party_module_imports_a_retired_one(self):
        scanned = 0
        for top in SCANNED:
            for path in sorted((ROOT / top).rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                scanned += 1
                hits = {name for name in _imports(path)
                        if any(name == old or name.startswith(old + ".") for old in RETIRED_MODULES)}
                self.assertEqual(hits, set(), str(path.relative_to(ROOT)))
        self.assertGreater(scanned, 100)

    def test_the_integrations_package_loads_no_adapter_by_itself(self):
        probe = ("import sys, integrations; "
                 "print(sorted(m for m in sys.modules if m.startswith(('integrations.', 'ontology'))))")
        result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env=_env(),
                                capture_output=True, text=True, timeout=60, check=True)
        self.assertEqual(result.stdout.strip(), "[]")
        # An explicit adapter import still works and does not drag in the MC client.
        probe = ("import sys; from integrations import bc_read; "
                 "print('integrations.mc' in sys.modules, 'integrations.github' in sys.modules)")
        result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env=_env(),
                                capture_output=True, text=True, timeout=60, check=True)
        self.assertEqual(result.stdout.strip(), "False False")


if __name__ == "__main__":
    unittest.main()
