"""Gate checks import order-independently (directive a59bd552eb2b): a test module that put
devloop/ ahead of the repository root on sys.path at import made `tests` resolve to
devloop/tests for every module imported after it, so a check passed or failed by its module
order alone (c65c2df8: integrations.tests.test_fba_external_readers before
tests.test_source_registry failed; the registered order passed because
tests.test_sharepoint_read_budget imported `tests` first), and the controller read the
order-dependent failure as a host fault.

Each gate check's modules are imported in one interpreter, as ``python3 -m unittest``
imports them, in the registered order, reversed and shuffled: every module imports, `tests`
still resolves to the root package, and no module leaves a sys.path entry holding a name the
root holds (a later import of that name would depend on order)."""

from __future__ import annotations

import json
from pathlib import Path
import random
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
SEED = 20261020

# Imports the modules (argv) in order from the root, then prints what broke: import errors,
# where `tests` now resolves (the root's is a namespace package, so a regular `tests` package
# anywhere on sys.path wins over it), and each added sys.path entry's module or package
# named as a root file or directory is.
PROBE = r"""
import importlib, importlib.util, json, os, sys, traceback
root = os.getcwd()
names = {n[:-3] if n.endswith(".py") else n for n in os.listdir(root)
         if n.endswith(".py") or os.path.isdir(os.path.join(root, n))}
before = list(sys.path)
errors = []
for module in sys.argv[1:]:
    try:
        importlib.import_module(module)
    except BaseException:
        errors.append([module, traceback.format_exc(limit=1)[-300:]])
added = [p for p in sys.path if p not in before and os.path.abspath(p or ".") != root]
shadows = sorted({os.path.join(p, n) for p in added for n in names
                  if os.path.isfile(os.path.join(p, n, "__init__.py"))
                  or os.path.isfile(os.path.join(p, n + ".py"))})
spec = importlib.util.find_spec("tests")
print(json.dumps({"errors": errors, "shadows": shadows,
                  "tests": sorted({os.path.realpath(p) for p in spec.submodule_search_locations or []})
                  if spec else None}))
"""


def unittest_modules(check: dict) -> list[str]:
    """The modules `python3 -m unittest` imports for the check's names, in order (a name
    is dotted up to its first ``test_*`` part; later parts are classes and tests)."""
    out: list[str] = []
    for name in check.get("modules") or []:
        parts = name.split(".")
        end = next(i for i, part in enumerate(parts) if part.startswith("test_"))
        module = ".".join(parts[:end + 1])
        if module not in out:
            out.append(module)
    return out


def orders(modules: list[str]) -> dict[str, list[str]]:
    """The registered order, reversed and shuffled (one order of a single module)."""
    if len(modules) < 2:
        return {"registered": modules}
    shuffled = list(modules)
    random.Random(SEED).shuffle(shuffled)
    return {"registered": modules, "reversed": modules[::-1], "shuffled": shuffled}


class GateModuleOrderTest(unittest.TestCase):
    def test_every_checks_modules_import_alike_in_any_order(self) -> None:
        spec = json.loads((ROOT / "devloop/gate-checks.json").read_text())
        root_tests = [str((ROOT / "tests").resolve())]
        probed = 0
        for check in spec["checks"]:
            modules = unittest_modules(check)
            for label, order in orders(modules).items() if modules else ():
                with self.subTest(check=check["name"], order=label):
                    done = subprocess.run([sys.executable, "-c", PROBE, *order], cwd=ROOT,
                                          capture_output=True, text=True, timeout=300)
                    self.assertEqual(done.returncode, 0, done.stderr[-2000:])
                    got = json.loads(done.stdout.strip().splitlines()[-1])
                    self.assertEqual(got, {"errors": [], "shadows": [], "tests": root_tests},
                                     order)
                    probed += 1
        self.assertGreater(probed, 0, "no gate check lists unittest modules")


if __name__ == "__main__":
    unittest.main()
