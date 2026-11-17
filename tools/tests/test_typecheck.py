"""tools/typecheck.py at its boundary: a fake mypy prints errors; an error not on the
baseline fails, a baselined error that moved lines does not, a fixed one is named, and a
missing or crashing mypy is never a pass."""
from __future__ import annotations

import ast
import configparser
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from tools import typecheck


class TypecheckTest(unittest.TestCase):
    def tree(self, mypy_output: str, baseline: str, exit_code: int = 1) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / "tools").mkdir()
        (root / typecheck.BASELINE).write_text(baseline)
        (root / "out.txt").write_text(mypy_output)
        fake = root / "fake-mypy"
        fake.write_text(f"#!/bin/sh\ncat '{root}/out.txt'\nexit {exit_code}\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.env = mock.patch.dict(os.environ, {"PLX_MYPY": str(fake)})
        self.env.start()
        self.addCleanup(self.env.stop)
        return root

    def test_new_error_fails_moved_error_passes_fixed_error_is_named(self):
        out = ("ontology/a.py:90: error: Need type annotation for \"x\"  [var-annotated]\n"
               "ontology/a.py:91: note: hint\n"
               "operating/b.py:3: error: Name \"y\" is not defined  [name-defined]\n")
        base = ("# header\n"
                "ontology/a.py: [var-annotated] Need type annotation for \"x\"\n"
                "operator_api/c.py: [arg-type] gone\n")
        code, lines = typecheck.run(self.tree(out, base))
        self.assertEqual(code, 1, lines)
        self.assertIn("2 errors, 1 new, 1 fixed", lines[0])
        self.assertIn("  new: operating/b.py: [name-defined] Name \"y\" is not defined", lines)
        self.assertIn("  fixed (delete from the baseline): operator_api/c.py: [arg-type] gone",
                      lines)

    def test_a_repeated_error_beyond_its_baseline_count_is_new(self):
        out = "ontology/a.py:1: error: Bad  [misc]\nontology/a.py:2: error: Bad  [misc]\n"
        code, lines = typecheck.run(self.tree(out, "ontology/a.py: [misc] Bad\n"))
        self.assertEqual((code, lines[1]), (1, "  new: ontology/a.py: [misc] Bad"))

    def test_baseline_only_errors_pass_and_update_rewrites(self):
        out = "ontology/a.py:7: error: Bad  [misc]\n"
        root = self.tree(out, "")
        self.assertEqual(typecheck.run(root)[0], 1)
        self.assertEqual(typecheck.run(root, update=True)[0], 0)
        self.assertEqual(typecheck.run(root)[0], 0)

    def test_a_crashing_or_missing_mypy_is_not_a_pass(self):
        self.assertEqual(typecheck.run(self.tree("config error\n", "", exit_code=2))[0], 2)
        with mock.patch.dict(os.environ, {"PLX_MYPY": ""}), \
                mock.patch.object(typecheck.importlib.util, "find_spec", return_value=None):
            code, lines = typecheck.run(Path(tempfile.mkdtemp()))
        self.assertEqual(code, 2)
        self.assertIn("unavailable", lines[0])


class TypeCommentTest(unittest.TestCase):
    def test_every_checked_module_parses_as_mypy_reads_it(self):
        """mypy parses with type comments on: a comment line starting ``# type:`` (prose
        wrapped there) is a syntax error that stops the whole check, so no error is ever
        compared to the baseline."""
        root = Path(typecheck.ROOT)
        config = configparser.ConfigParser()
        config.read(root / typecheck.CONFIG)
        broken = []
        for package in config["mypy"]["files"].replace(",", " ").split():
            for path in sorted((root / package).rglob("*.py")):
                if "tests" in path.relative_to(root).parts:
                    continue   # excluded in mypy.ini
                try:
                    ast.parse(path.read_text("utf-8"), str(path), type_comments=True)
                except SyntaxError as exc:
                    broken.append(f"{path.relative_to(root)}:{exc.lineno}: {exc.msg}")
        self.assertEqual(broken, [], "reword a comment line that starts '# type:'")


if __name__ == "__main__":
    unittest.main()
