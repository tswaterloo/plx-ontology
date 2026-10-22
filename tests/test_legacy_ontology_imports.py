"""Gate `no-legacy-ontology-imports`, the C0 type move and the lazy package facade."""

from __future__ import annotations

import importlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_legacy_ontology_imports as gate  # noqa: E402

STORE = "Ontology" + "Store"
RESOLVER = "Transaction" + "Resolver"


def rules(source: str, relative: str = "operating/new_lane.py") -> set[str]:
    return {rule for rule, _line, _detail in gate.scan_source(source, relative)}


class ScanRules(unittest.TestCase):
    def test_every_import_form_of_a_legacy_class_is_found(self):
        cases = {
            f"from ontology import {STORE}\n": "import-name",
            f"from ontology.core import {STORE} as S\n": "import-name",
            f"from ontology.transactions import {RESOLVER}\n": "import-module",
            "import ontology.transactions\n": "import-module",
            "import ontology.transactions as t\n": "import-module",
            "from ontology import transactions\n": "import-module",
            "from ontology.core import *\n": "import-name",
            f"from ontology import core\nx = core.{STORE}\n": "attribute",
            f"import ontology\nx = ontology.{RESOLVER}\n": "attribute",
            f"from ontology import core\nx = getattr(core, '{STORE}')\n": "string",
            "import importlib\nm = importlib.import_module('ontology.transactions')\n": "string",
            f"from unittest import mock\np = mock.patch('operating.x.{STORE}')\n": "string",
            f"from operating.retrieval import {STORE}\n": "import-name",
            f"from integrations.transaction_evidence import {RESOLVER} as R\n": "import-name",
            f"from .retrieval import {STORE}\n": "import-name",
            "from operator_ui.case_join import EDGE_CLASSES\n": "import-module",
            "from operator_ui import case_join\n": "import-module",
            "import operator_ui.case_join as join\n": "import-module",
            "m = __import__('operator_ui.case_join')\n": "string",
        }
        for source, rule in cases.items():
            with self.subTest(source=source):
                self.assertIn(rule, rules(source))

    def test_a_string_built_dynamic_import_of_a_legacy_module_is_found(self):
        built = (
            "import importlib\nm = importlib.import_module('ontology.' + 'trans' + 'actions')\n",
            "import importlib\nm = importlib.import_module(f'ontology.{name}')\n",
            "import importlib\nm = importlib.import_module('.'.join(['operator_ui', x]))\n",
            "import importlib\nm = importlib.import_module('%s.case_join' % pkg)\n",
            "import importlib\nm = importlib.import_module('{}.transactions'.format(p))\n",
            "import importlib\nm = importlib.import_module('.transactions', 'ontology')\n",
            "from importlib import import_module as load\nm = load('ontology.' + n)\n",
            "m = __import__('ontology', fromlist=['trans' + 'actions'])\n",
            "m = __import__('operator_ui', None, None, [name])\n",
        )
        for source in built:
            with self.subTest(source=source):
                self.assertIn("dynamic-import", rules(source))
        for source in ("import importlib\nm = importlib.import_module(name)\n",
                       "import importlib\nm = importlib.import_module(f'integrations.{x}')\n",
                       "m = __import__('evals.oracle.generator', fromlist=['x'])\n",
                       "import importlib\nm = importlib.import_module('.'.join([a, b]))\n"):
            with self.subTest(source=source):
                self.assertEqual(rules(source), set())

    def test_relative_imports_inside_the_package_resolve(self):
        self.assertIn("import-name", rules(f"from .core import {STORE}\n", "ontology/new.py"))
        self.assertIn("import-module", rules("from .transactions import X\n", "ontology/new.py"))
        self.assertIn("import-module", rules("from ..transactions import X\n", "ontology/sub/new.py"))
        self.assertIn("import-module", rules("from . import transactions\n", "ontology/new.py"))
        self.assertIn("import-module", rules("from .case_join import X\n", "operator_ui/new.py"))
        self.assertIn("import-module", rules("from . import case_join\n", "operator_ui/new.py"))

    def test_shared_types_and_unrelated_names_are_not_findings(self):
        clean = (
            "from ontology import Actor, EntityRef, LEVELS, ValidationError\n"
            "from ontology.types import Actor as A\n"
            "from ontology.core import Observation\n"
            "from other.transactions import Thing\n"
            "from operator_ui import case_workspace\n"
            "s = 'the legacy store is described in prose here'\n"
        )
        self.assertEqual(rules(clean), set())


class TreeRatchet(unittest.TestCase):
    def _tree(self, files: dict[str, str]) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)], check=False))
        for relative, text in files.items():
            path = tmp / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return tmp

    def test_new_importer_fails_and_allowlisted_one_passes(self):
        root = self._tree({
            "operating/old.py": f"from ontology import {STORE}\n",
            "operating/new.py": f"from ontology.transactions import {RESOLVER}\n",
            "EVIDENCE/frozen.py": f"from ontology import {STORE}\n",
            "ontology/core.py": f"class {STORE}: pass\n",
        })
        new, stale = gate.check(root, frozenset({"operating/old.py"}))
        self.assertEqual(sorted(new), ["operating/new.py"])
        self.assertEqual(stale, [])

    def test_stale_entry_is_reported_and_strict_fails(self):
        root = self._tree({"operating/clean.py": "from ontology import Actor\n"})
        new, stale = gate.check(root, frozenset({"operating/clean.py", "operating/deleted.py"}))
        self.assertEqual(new, {})
        self.assertEqual(stale, ["operating/clean.py", "operating/deleted.py"])
        script = ROOT / "scripts" / "check_legacy_ontology_imports.py"
        strict = subprocess.run([sys.executable, str(script), str(root), "--strict"],
                                capture_output=True, text=True)
        self.assertEqual(strict.returncode, 0, strict.stdout)
        importer = self._tree({"operating/back.py": f"from ontology.core import {STORE}\n",
                               "tests/test_join.py":
                                   "from operator_ui.case_join import EDGE_CLASSES\n"})
        again = subprocess.run([sys.executable, str(script), str(importer)],
                               capture_output=True, text=True)
        self.assertEqual(again.returncode, 1, again.stdout)
        self.assertIn("operating/back.py:1: import-name", again.stdout)
        self.assertIn("tests/test_join.py:1: import-module: operator_ui.case_join", again.stdout)

    def test_repository_has_no_importer_and_an_empty_allowlist(self):
        self.assertEqual(gate.ALLOWLIST, frozenset())
        new, stale = gate.check(ROOT)
        self.assertEqual(new, {}, "new importer of a legacy ontology class")
        self.assertEqual(stale, [])


class PackageFacade(unittest.TestCase):
    def _fresh(self, code: str) -> str:
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_the_legacy_store_and_resolver_are_deleted(self):
        out = self._fresh(
            "import importlib.util, ontology.core, operating.mcp_server\n"
            f"print(importlib.util.find_spec('ontology.transactions') is None, "
            f"hasattr(ontology.core, '{STORE}'))\n")
        self.assertEqual(out, "True False")

    def test_importing_actor_loads_only_the_types_module(self):
        out = self._fresh(
            "import sys\nfrom ontology import Actor, EntityRef, LEVELS, ValidationError\n"
            "print('ontology.core' in sys.modules, 'ontology.transactions' in sys.modules)\n")
        self.assertEqual(out, "False False")

    def test_core_and_package_reexport_the_same_type_objects(self):
        types = importlib.import_module("ontology.types")
        core = importlib.import_module("ontology.core")
        package = importlib.import_module("ontology")
        for name in ("Actor", "EntityRef", "LEVELS", "ValidationError"):
            with self.subTest(name=name):
                self.assertIs(getattr(core, name), getattr(types, name))
                self.assertIs(getattr(package, name), getattr(types, name))
        self.assertFalse(hasattr(package, STORE))
        self.assertFalse(hasattr(package, RESOLVER))
        with self.assertRaises(AttributeError):
            getattr(package, "NoSuchName")

    def test_moved_types_keep_their_validation(self):
        types = importlib.import_module("ontology.types")
        with self.assertRaises(types.ValidationError):
            types.Actor("demo", frozenset({"staff:pd"}), clearance="secret")
        with self.assertRaises(types.ValidationError):
            types.EntityRef("portal", " ", "1")
        self.assertEqual(types.EntityRef(" portal ", "order", "1").source, "portal")


if __name__ == "__main__":
    unittest.main()
