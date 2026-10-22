"""Import graph over small fixture trees (isolated fixture: temporary directories, no repo reads)."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from verify import import_graph as graph_mod


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


TREE = {
    "pkg/__init__.py": '"""Pkg package."""\n',
    "pkg/core.py": (
        '"""Core of the fixture.\n\nLonger text that is not the summary."""\n'
        "import os\nimport pkg.util.deep.leaf\nfrom pkg import util, helper\nfrom pkg.cli import run\n"
        "LIMIT = 3\n_hidden = 1\n\nclass Thing:\n    pass\n\ndef make():\n    pass\n"),
    "pkg/util/__init__.py": "",
    "pkg/util/deep.py": "from .. import helper\nfrom . import __init__ as init\n",
    "pkg/helper.py": "def assist():\n    pass\n",
    "pkg/cli.py": '"""Command line\nwrapped over two lines."""\nfrom .util.deep import *\n\n\ndef run():\n    pass\n',
    "pkg/lazy.py": "def go():\n    from pkg.helper import assist\n    return assist\n",
    "pkg/tests/test_core.py": "from pkg import core\n",
    "pkg/tests/test_lazy.py": "from pkg.lazy import go\n",
    "scripts/check_x.py": "from pkg.core import make\n",
    "tests/test_check_x.py": "import sys\nsys.path.insert(0, 'scripts')\nimport check_x\n",
    "tests/test_cli_process.py": 'CMD = ["python3", "-m", "pkg.cli"]\nPATH = "scripts/check_x.py"\n',
    "tests/test_prose.py": 'X = "pkg.core is mentioned in prose, not a reference"\nY = "pkg"\nZ = "__init__.py"\nW = "pkg/__init__.py"\n',
    ".hidden/skipped.py": "",
    "EVIDENCE/skipped.py": "",
    "__pycache__/skipped.py": "",
    "nested/.git": "gitdir: elsewhere\n",
    "nested/skipped.py": "",
}


class GraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = write_tree(Path(self.tmp.name), TREE)
        self.graph = graph_mod.build(self.root)

    def test_modules_are_named_by_dotted_path_and_skips_apply(self) -> None:
        self.assertEqual(list(self.graph), sorted(self.graph))
        self.assertIn("pkg", self.graph)
        self.assertIn("pkg.util", self.graph)
        self.assertEqual(self.graph["pkg.util"].path, "pkg/util/__init__.py")
        self.assertEqual(self.graph["pkg.util.deep"].path, "pkg/util/deep.py")
        self.assertEqual(sorted(m for m in self.graph if m.startswith(("nested", "EVIDENCE", "__pycache__"))), [])
        self.assertFalse([m for m in self.graph.values() if "skipped" in m.path])

    def test_summary_is_the_first_line_continued_while_the_sentence_is_wrapped(self) -> None:
        self.assertEqual(self.graph["pkg"].doc, "Pkg package.")
        self.assertEqual(self.graph["pkg.core"].doc, "Core of the fixture.")
        self.assertEqual(self.graph["pkg.cli"].doc, "Command line wrapped over two lines.")
        self.assertEqual(self.graph["pkg.helper"].doc, "")

    def test_public_names_put_callables_first_and_drop_private_ones(self) -> None:
        self.assertEqual(self.graph["pkg.core"].public, ("Thing", "make", "LIMIT"))

    def test_all_overrides_the_default_public_names(self) -> None:
        root = write_tree(Path(self.tmp.name) / "alt", {"m.py": "__all__ = ['b', 'a']\ndef a(): pass\ndef b(): pass\ndef c(): pass\n"})
        self.assertEqual(graph_mod.build(root)["m"].public, ("b", "a"))

    def test_static_imports_resolve_to_first_party_modules_only(self) -> None:
        core = self.graph["pkg.core"]
        self.assertEqual(core.imports, ("pkg.cli", "pkg.helper", "pkg.util", "pkg.util.deep"))
        self.assertEqual(self.graph["pkg.lazy"].imports, ("pkg.helper",))
        self.assertEqual(self.graph["scripts.check_x"].imports, ("pkg.core",))

    def test_relative_imports_and_star_imports(self) -> None:
        self.assertEqual(self.graph["pkg.util.deep"].imports, ("pkg.helper", "pkg.util"))
        self.assertEqual(self.graph["pkg.cli"].imports, ("pkg.util.deep",))
        self.assertEqual(self.graph["pkg.cli"].from_names, ())

    def test_names_imported_from_a_module_are_recorded(self) -> None:
        self.assertEqual(self.graph["pkg.core"].from_names, (("pkg.cli", "run"),))
        self.assertEqual(self.graph["scripts.check_x"].from_names, (("pkg.core", "make"),))

    def test_a_module_never_imports_itself(self) -> None:
        root = write_tree(Path(self.tmp.name) / "self", {"a.py": "NAME = 'a.py'\nimport a\n"})
        self.assertEqual(graph_mod.build(root)["a"].imports, ())

    def test_a_bare_import_after_a_sys_path_insert_reaches_the_script(self) -> None:
        self.assertEqual(self.graph["tests.test_check_x"].imports, ("scripts.check_x",))

    def test_a_bare_from_import_reaches_the_script_too(self) -> None:
        root = write_tree(Path(self.tmp.name) / "bare", {
            "scripts/tool.py": "def main(): pass\n", "tests/test_tool.py": "from tool import main\n"})
        self.assertEqual(graph_mod.build(root)["tests.test_tool"].imports, ("scripts.tool",))

    def test_a_from_import_of_a_module_in_a_directory_without_init_resolves(self) -> None:
        root = write_tree(Path(self.tmp.name) / "ns", {
            "scripts/tool.py": "", "tests/test_tool.py": "from scripts import tool\n"})
        self.assertEqual(graph_mod.build(root)["tests.test_tool"].imports, ("scripts.tool",))

    def test_exact_string_references_link_dotted_names_and_paths_not_prose(self) -> None:
        self.assertEqual(self.graph["tests.test_cli_process"].imports, ("pkg.cli", "scripts.check_x"))
        self.assertEqual(self.graph["tests.test_prose"].imports, ())

    def test_a_string_links_only_where_it_runs_or_opens_the_module(self) -> None:
        # A file hint and a checker's allowlist named operating/host.py, so a
        # host change ran unrelated suites.
        package = {name: text for name, text in TREE.items() if name.startswith("pkg/")}
        root = write_tree(Path(self.tmp.name) / "used", {
            **package, "scripts/check_x.py": "",
            "tests/test_data.py": (
                '"""Runs pkg/cli.py and scripts/check_x.py, in prose."""\n'
                'HINT = {"files_hint": ["pkg/core.py"], "module": "pkg.cli"}\n'
                'ALLOW = frozenset({"pkg/helper.py", "pkg.lazy"})\n'
                'SKIP = ("scripts/check_x.py", "pkg/cli.py")\n'
                'def test(self, out, block):\n'
                '    self.assertIn("pkg.helper", out)\n'
                '    return "pkg.core" in block\n'),
            "tests/test_runs.py": (
                "import importlib, subprocess, sys\nfrom pathlib import Path\nROOT = Path('.')\n"
                'subprocess.run([sys.executable, "-m", "pkg.cli"])\n'
                'AUDIT = ("scripts/check_x.py", "--strict")\n'
                'TEXT = (ROOT / "pkg/helper.py").read_text()\n'
                'importlib.import_module("pkg.lazy")\n'
                'for name in ("core.py",):\n    open(name)\n')})
        graph = graph_mod.build(root)
        self.assertEqual(graph["tests.test_data"].imports, ())
        self.assertEqual(graph["tests.test_runs"].imports,
                         ("pkg.cli", "pkg.core", "pkg.helper", "pkg.lazy", "scripts.check_x"))
        # Every string without whitespace is kept for the data-file readers, docstrings not.
        self.assertIn("pkg/core.py", graph["tests.test_data"].strings)
        self.assertFalse([s for s in graph["tests.test_data"].strings if "prose" in s])

    def test_a_file_name_string_links_every_module_of_that_name(self) -> None:
        root = write_tree(Path(self.tmp.name) / "names", {
            "a/store.py": "", "b/store.py": "", "tests/test_s.py": "P = 'store.py'\n"})
        self.assertEqual(graph_mod.build(root)["tests.test_s"].imports, ("a.store", "b.store"))

    def test_standard_library_names_never_match_a_first_party_file(self) -> None:
        root = write_tree(Path(self.tmp.name) / "std", {"pkg/json.py": "", "user.py": "import json\nfrom os import path\n"})
        self.assertEqual(graph_mod.build(root)["user"].imports, ())

    def test_a_syntax_error_lists_the_module_without_edges(self) -> None:
        root = write_tree(Path(self.tmp.name) / "broken", {"bad.py": "def (:\n"})
        self.assertEqual(graph_mod.build(root)["bad"].imports, ())

    def test_test_modules_are_recognised_by_directory_or_prefix(self) -> None:
        self.assertTrue(self.graph["pkg.tests.test_core"].is_test)
        self.assertTrue(self.graph["tests.test_check_x"].is_test)
        self.assertFalse(self.graph["pkg.core"].is_test)
        self.assertFalse(self.graph["scripts.check_x"].is_test)

    def test_computed_imports_link_their_constant_name_or_prefix(self) -> None:
        root = write_tree(Path(self.tmp.name) / "computed", {
            "plug/__init__.py": "", "plug/a.py": "", "plug/b.py": "", "plug/tests/test_p.py": "",
            "other.py": "",
            "tests/test_fstring.py": "import importlib, pkgutil, plug\n"
                                     "for i in pkgutil.iter_modules(plug.__path__):\n"
                                     "    importlib.import_module(f'plug.{i.name}')\n",
            "tests/test_concat.py": "from importlib import import_module\n"
                                    "def load(n):\n    return import_module('plug.' + n)\n",
            "tests/test_const.py": "__import__('other')\n",
            "tests/test_static.py": "import other\n"})
        graph = graph_mod.build(root)
        for name in ("tests.test_fstring", "tests.test_concat"):
            with self.subTest(name):
                # Every module under the prefix, never a test_* module, not dynamic.
                self.assertTrue({"plug.a", "plug.b"} <= set(graph[name].imports))
                self.assertNotIn("plug.tests.test_p", graph[name].imports)
                self.assertFalse(graph[name].dynamic)
        self.assertEqual((graph["tests.test_const"].imports, graph["tests.test_const"].dynamic),
                         (("other",), False))
        self.assertFalse(graph["tests.test_static"].dynamic)

    def test_a_fromlist_links_its_named_children_or_the_whole_package(self) -> None:
        root = write_tree(Path(self.tmp.name) / "fromlist", {
            "plug/__init__.py": "", "plug/a.py": "", "plug/b.py": "", "plug/tests/test_p.py": "",
            "named.py": "__import__('plug', fromlist=['a'])\n",
            "computed.py": "def load(n):\n    return __import__('plug', fromlist=[n])\n",
            "aliased.py": "from importlib import import_module as load\nload('plug.' + x)\n"})
        graph = graph_mod.build(root)
        self.assertEqual(graph["named"].imports, ("plug", "plug.a"))
        self.assertEqual(graph["computed"].imports, ("plug", "plug.a", "plug.b"))
        self.assertEqual(graph["aliased"].imports, ("plug", "plug.a", "plug.b"))
        self.assertFalse(any(graph[name].dynamic for name in ("named", "computed", "aliased")))

    def test_an_unresolvable_computed_import_marks_the_module_dynamic(self) -> None:
        helper = "import importlib\ndef _load(name):\n    return importlib.import_module(name)\n"
        root = write_tree(Path(self.tmp.name) / "dyn", {
            "a.py": "import importlib\ndef load(name):\n    return importlib.import_module(name)\n",
            "b.py": "def load(name):\n    return __import__(name)\n",
            "c.py": "import importlib\nimportlib.import_module(f'{base}.x')\n",
            "d.py": "import importlib\nimportlib.import_module('nowhere.' + x)\n",
            "e.py": "import importlib\nimportlib.import_module('json')\n",
            # A private helper importing its parameter links the literals its calls pass,
            # unless one call passes anything else, it escapes as a value, it rebinds the
            # parameter, or another module imports it (and may call it with anything).
            "f.py": f"{helper}OK = _load('json') and _load(name='e')\n",
            "g.py": f"{helper}_load('json')\n_load(x)\n",
            "h.py": f"{helper}_load('json')\nHOOK = _load\n",
            "i.py": f"{helper}_load('json')\n", "j.py": "from i import _load\n_load(x)\n",
            "k.py": "import importlib\ndef _load(name):\n    name += '.x'\n"
                    "    return importlib.import_module(name)\n_load('json')\n"})
        graph = graph_mod.build(root)
        self.assertEqual({name: m.dynamic for name, m in graph.items()},
                         {"a": True, "b": True, "c": True, "d": True, "e": False, "f": False,
                          "g": True, "h": True, "i": True, "j": False, "k": True})
        self.assertEqual(graph["f"].imports, ("e",))

    def test_the_graph_is_deterministic(self) -> None:
        self.assertEqual(graph_mod.build(self.root), self.graph)
        for module in self.graph.values():
            self.assertEqual(list(module.imports), sorted(module.imports))


class AffectedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.graph = graph_mod.build(write_tree(Path(self.tmp.name), TREE))

    def test_dependents_are_the_reverse_edges(self) -> None:
        reverse = graph_mod.dependents(self.graph)
        self.assertEqual(reverse["pkg.helper"], ["pkg.core", "pkg.lazy", "pkg.util.deep"])
        self.assertEqual(reverse["tests.test_prose"], [])

    def test_importers_count_transitively(self) -> None:
        self.assertEqual(
            graph_mod.affected(self.graph, {"pkg.helper"}),
            {"pkg.helper", "pkg.core", "pkg.lazy", "pkg.util.deep", "pkg.cli", "pkg.tests.test_core",
             "pkg.tests.test_lazy", "scripts.check_x", "tests.test_check_x", "tests.test_cli_process"})

    def test_a_leaf_with_no_importers_affects_only_itself(self) -> None:
        self.assertEqual(graph_mod.affected(self.graph, {"tests.test_prose"}), {"tests.test_prose"})

    def test_a_changed_package_init_counts_for_its_whole_package(self) -> None:
        hit = graph_mod.affected(self.graph, {"pkg.util"})
        self.assertIn("pkg.util.deep", hit)
        self.assertIn("pkg.tests.test_core", hit)
        self.assertNotIn("tests.test_prose", hit)

    def test_a_plain_module_does_not_expand_to_a_namesake_prefix(self) -> None:
        self.assertNotIn("pkg.helper", graph_mod.affected(self.graph, {"pkg.lazy"}))


INIT_CHAIN = {
    "lib/__init__.py": "from lib.heavy import Heavy\n",
    "lib/heavy.py": "class Heavy:\n    pass\n",
    "lib/other.py": "def f():\n    pass\n",
    "lib/sub/__init__.py": "from . import chain\n",
    "lib/sub/chain.py": "",
    "lib/sub/leaf.py": "",
    "tests/test_other.py": "from lib.other import f\n",
    "tests/test_leaf.py": "import lib.sub.leaf\n",
    "tests/test_unrelated.py": "import os\n",
}


class PackageInitChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.graph = graph_mod.build(write_tree(Path(self.tmp.name), INIT_CHAIN))

    def hit_by(self, changed: str) -> list[str]:
        return sorted(n for n in graph_mod.affected(self.graph, {changed}) if graph_mod.is_unittest_module(n))

    def test_importing_a_module_depends_on_every_package_init_on_its_path(self) -> None:
        deps = graph_mod.dependencies(self.graph)
        self.assertEqual(deps["tests.test_other"], {"lib", "lib.other"})
        self.assertEqual(deps["tests.test_leaf"], {"lib", "lib.sub", "lib.sub.leaf"})
        self.assertEqual(deps["lib.sub.leaf"], {"lib", "lib.sub"})
        self.assertEqual(deps["tests.test_unrelated"], set())

    def test_what_a_package_init_imports_reaches_every_importer_of_the_package(self) -> None:
        self.assertEqual(self.hit_by("lib.heavy"), ["tests.test_leaf", "tests.test_other"])

    def test_a_nested_init_reaches_only_importers_of_that_subpackage(self) -> None:
        self.assertEqual(self.hit_by("lib.sub.chain"), ["tests.test_leaf"])

    def test_a_sibling_module_does_not_reach_importers_of_another_sibling(self) -> None:
        self.assertEqual(self.hit_by("lib.other"), ["tests.test_other"])
        self.assertEqual(self.hit_by("lib.sub.leaf"), ["tests.test_leaf"])

    def test_the_code_map_edges_stay_explicit(self) -> None:
        self.assertEqual(self.graph["tests.test_other"].imports, ("lib.other",))


ROOT = Path(__file__).resolve().parents[2]
TRACE = (
    "import importlib, json, sys\n"
    "importlib.import_module(sys.argv[1])\n"
    "print(json.dumps(sorted({m.__file__ for m in list(sys.modules.values()) if getattr(m, '__file__', None)})))\n")
SAMPLES = 12


class RuntimeImportTraceTests(unittest.TestCase):
    """The graph must cover what Python really runs: for a sample of this repo's test modules,
    import each in a fresh interpreter and require that changing any first-party module that
    got loaded selects that test."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = graph_mod.build(ROOT)
        cls.by_path = {m.path: m.name for m in cls.graph.values()}

    def loaded_first_party(self, test: str) -> set[str]:
        run = subprocess.run([sys.executable, "-c", TRACE, test], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stderr[-500:])
        loaded = set()
        for file in json.loads(run.stdout.strip().splitlines()[-1]):
            try:
                relative = Path(file).resolve().relative_to(ROOT).as_posix()
            except ValueError:
                continue
            loaded.add(self.by_path.get(relative, ""))
        return loaded - {"", test}

    def test_changing_any_module_a_test_loads_selects_that_test(self) -> None:
        tests = sorted(n for n in self.graph if graph_mod.is_unittest_module(n))
        sample = tests[:: max(1, len(tests) // SAMPLES)]
        missed, traced = [], 0
        for test in sample:
            for dependency in sorted(self.loaded_first_party(test)):
                traced += 1
                if test not in graph_mod.affected(self.graph, {dependency}):
                    missed.append((test, dependency))
        self.assertGreater(traced, len(sample), "the sample loaded no first-party dependencies")
        self.assertEqual(missed, [])


if __name__ == "__main__":
    unittest.main()
