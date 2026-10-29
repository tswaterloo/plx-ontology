"""Code map generator (isolated fixture: temporary trees, no repo reads)."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from devloop.tests.test_import_graph import TREE, write_tree
from scripts import gen_codemap

PINS = {"pins": [
    {"repo": "portal", "path": "src/a.ts", "workbench_modules": ["pkg/core.py", "docs/x.md"]},
    {"repo": "portal", "path": "src/b.ts", "workbench_module": "pkg/core.py"},
    {"repo": "mc", "path": "src/c.ts", "workbench_modules": ["pkg/helper.py"]}]}


def run_main(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = gen_codemap.main(list(args))
    return code, out.getvalue(), err.getvalue()


class RenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = write_tree(Path(self.tmp.name), {
            **TREE, "devloop/upstream-pins.json": json.dumps(PINS)})
        self.text = gen_codemap.render(self.root)
        self.lines = self.text.splitlines()

    def test_how_to_use_comes_first(self) -> None:
        self.assertTrue(self.text.startswith("# Code map (generated)"))
        self.assertIn("python3 devloop/affected_tests.py --base origin/main", self.text[:1500])
        self.assertLess(self.text.index("How to use"), self.text.index("## pkg"))

    def test_packages_are_sections_with_module_and_test_counts(self) -> None:
        self.assertIn("## pkg (7 modules, 2 test modules)", self.lines)
        self.assertIn("## scripts (1 modules, 0 test modules)", self.lines)
        self.assertIn("## tests (0 modules, 3 test modules)", self.lines)
        self.assertEqual([l for l in self.lines if l.startswith("## ")],
                         sorted(l for l in self.lines if l.startswith("## ")))

    def test_a_module_line_has_purpose_names_imports_tests_and_pins(self) -> None:
        self.assertIn(
            "- pkg/core.py: Core of the fixture. | names: make, Thing, LIMIT"
            " | imports: pkg.cli, pkg.helper, pkg.util, pkg.util.deep"
            " | tests: pkg.tests.test_core | pins: portal:src/a.ts, portal:src/b.ts",
            self.lines)

    def test_modules_without_edges_or_names_stay_short(self) -> None:
        self.assertIn("- pkg/__init__.py: Pkg package.", self.lines)
        self.assertIn("- pkg/util/__init__.py", self.lines)

    def test_a_pin_names_only_python_modules_and_other_repos_keep_their_own(self) -> None:
        helper = next(l for l in self.lines if l.startswith("- pkg/helper.py"))
        self.assertTrue(helper.endswith("| pins: mc:src/c.ts"))
        self.assertNotIn("docs/x.md", self.text)

    def test_test_modules_are_listed_per_directory_not_as_module_lines(self) -> None:
        self.assertIn("- tests in pkg/tests/: test_core, test_lazy", self.lines)
        self.assertIn("- tests in tests/: test_check_x, test_cli_process, test_prose", self.lines)
        self.assertFalse([l for l in self.lines if l.startswith("- tests/test_")])

    def test_tests_column_lists_direct_test_importers_only(self) -> None:
        script = next(l for l in self.lines if l.startswith("- scripts/check_x.py"))
        self.assertIn("tests: tests.test_check_x, tests.test_cli_process", script)
        self.assertNotIn("pkg.tests.test_core", script)

    def test_names_other_modules_import_come_first(self) -> None:
        root = write_tree(Path(self.tmp.name) / "rank", {
            "m.py": "def a(): pass\ndef b(): pass\ndef c(): pass\n",
            "user.py": "from m import c\n", "other.py": "from m import c, b\n"})
        line = next(l for l in gen_codemap.render(root).splitlines() if l.startswith("- m.py"))
        self.assertEqual(line, "- m.py | names: c, b, a")

    def test_output_is_deterministic_and_independent_of_creation_order(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            reverse = write_tree(Path(other), dict(reversed(list(
                {**TREE, "devloop/upstream-pins.json": json.dumps(PINS)}.items()))))
            self.assertEqual(gen_codemap.render(reverse), self.text)
        self.assertEqual(gen_codemap.render(self.root), self.text)

    def test_a_tree_without_pins_renders(self) -> None:
        root = write_tree(Path(self.tmp.name) / "nopins", {"a.py": '"""A."""\n'})
        self.assertIn("- a.py: A.", gen_codemap.render(root))
        self.assertIn("## (root) (1 modules, 0 test modules)", gen_codemap.render(root))


class SectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = write_tree(Path(self.tmp.name), {
            **TREE, "devloop/upstream-pins.json": json.dumps(PINS)})
        self.full = gen_codemap.render(self.root)

    def sections(self, *prefixes: str) -> list[str]:
        return [l for l in gen_codemap.render(self.root, prefixes).splitlines() if l.startswith("## ")]

    def test_a_package_name_selects_the_whole_package_after_the_header(self) -> None:
        text = gen_codemap.render(self.root, ["pkg"])
        self.assertTrue(text.startswith(gen_codemap.HEADER))
        self.assertEqual(self.sections("pkg"), ["## pkg (7 modules, 2 test modules)"])
        self.assertEqual(text, self.full[:self.full.index("## scripts") - 1])
        self.assertIn("- tests in pkg/tests/: test_core, test_lazy", text)

    def test_a_trailing_slash_still_names_the_package(self) -> None:
        self.assertEqual(self.sections("pkg/"), ["## pkg (7 modules, 2 test modules)"])

    def test_a_path_prefix_selects_only_modules_under_it(self) -> None:
        text = gen_codemap.render(self.root, ["pkg/util"])
        lines = text.splitlines()
        self.assertIn("## pkg (7 modules, 2 test modules; matching lines only)", lines)
        self.assertEqual([l.split(":")[0].split(" |")[0] for l in lines if l.startswith("- pkg/")],
                         ["- pkg/util/__init__.py", "- pkg/util/deep.py"])
        self.assertNotIn("pkg/core.py", text)
        self.assertNotIn("tests in", text)

    def test_a_test_directory_prefix_selects_its_test_line(self) -> None:
        lines = gen_codemap.render(self.root, ["pkg/tests"]).splitlines()
        self.assertEqual([l for l in lines if l.startswith("- ")][-1], "- tests in pkg/tests/: test_core, test_lazy")
        self.assertEqual(len([l for l in lines if l.startswith("- pkg/")]), 0)

    def test_several_sections_combine_across_packages_in_order(self) -> None:
        self.assertEqual(self.sections("tests", "scripts/check", "pkg/lazy"), [
            "## pkg (7 modules, 2 test modules; matching lines only)",
            "## scripts (1 modules, 0 test modules; matching lines only)",
            "## tests (0 modules, 3 test modules)"])

    def test_an_unmatched_section_is_an_error(self) -> None:
        for prefixes in (["nope"], ["pkg", "nope"], ["pkg/zzz"]):
            with self.subTest(prefixes), self.assertRaises(LookupError) as raised:
                gen_codemap.render(self.root, prefixes)
            self.assertIn(prefixes[-1], str(raised.exception))

    def test_no_sections_renders_everything(self) -> None:
        self.assertEqual(gen_codemap.render(self.root, []), self.full)


class CommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = write_tree(Path(self.tmp.name), TREE)

    def test_the_map_goes_to_stdout_by_default_and_nothing_is_written(self) -> None:
        before = sorted(self.root.rglob("*"))
        code, out, err = run_main("--root", str(self.root))
        self.assertEqual((code, out, err), (0, gen_codemap.render(self.root), ""))
        self.assertEqual(sorted(self.root.rglob("*")), before)

    def test_section_filters_stdout_and_takes_several_prefixes(self) -> None:
        for argv in (["--section", "scripts", "tests"], ["--section", "scripts", "--section", "tests"]):
            with self.subTest(argv):
                code, out, _ = run_main("--root", str(self.root), *argv)
                self.assertEqual(code, 0)
                self.assertEqual(out, gen_codemap.render(self.root, ["scripts", "tests"]))
                self.assertNotIn("## pkg", out)

    def test_out_writes_the_file_and_prints_only_its_path_and_size(self) -> None:
        target = Path(self.tmp.name) / "out" / "CODEMAP.md"
        target.parent.mkdir()
        code, out, _ = run_main("--root", str(self.root), "--section", "pkg", "--out", str(target))
        self.assertEqual(code, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), gen_codemap.render(self.root, ["pkg"]))
        self.assertTrue(out.startswith(f"{target}: ") and out.endswith(" bytes\n"))
        self.assertNotIn("# Code map", out)

    def test_an_unmatched_section_exits_2_with_a_message_and_writes_nothing(self) -> None:
        target = Path(self.tmp.name) / "CODEMAP.md"
        code, out, err = run_main("--root", str(self.root), "--section", "pkg", "nope", "--out", str(target))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("nope", err)
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
