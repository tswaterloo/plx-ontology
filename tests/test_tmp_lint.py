"""No bare ``tempfile.mkdtemp`` outside tests (perm-box-1114, Fable audit 11-14 item 12).

A temp directory made with no ``dir=`` lands in ``/tmp`` and outlives a killed run (11-14:
17,804 entries, 11 GB). Product code makes its temp directories with
``ontology.tmp_root.mkdtemp`` / ``operating.tmp.mkdtemp`` (under ``/data/plx-tmp/<unit>/<pid>``,
which ``python3 -m operating.tmp sweep`` removes once the process is gone) or passes its own
``dir=``. A call that must stay in the system temp dir says why on its line with
``# tmp-lint: <reason>`` (a socket path limit; a module with its own pid sweep there).
"""
from __future__ import annotations

import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {"wt", ".worktrees", "node_modules", ".git", "EVIDENCE", "app", "tests",
             "__pycache__", ".venv", "venv"}
MARKER = "tmp-lint:"


def product_files(root: Path = ROOT):
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]) or rel.name.startswith("test_"):
            continue
        yield rel, path


def bare_mkdtemp(source: str) -> list[int]:
    """Lines of ``mkdtemp(...)`` calls with no ``dir=`` and no ``# tmp-lint:`` reason."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.splitlines()
    from_tempfile = any(isinstance(n, ast.ImportFrom) and n.module == "tempfile"
                        and any(a.name == "mkdtemp" for a in n.names) for n in ast.walk(tree))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):     # tempfile.mkdtemp(...)
            bare = func.attr == "mkdtemp" and getattr(func.value, "id", None) == "tempfile"
        else:                                   # from tempfile import mkdtemp; mkdtemp(...)
            bare = getattr(func, "id", None) == "mkdtemp" and from_tempfile
        if not bare:
            continue
        if any(k.arg == "dir" or k.arg is None for k in node.keywords) or len(node.args) >= 3:
            continue
        span = lines[node.lineno - 1:(node.end_lineno or node.lineno)]
        if any(MARKER in line for line in span):
            continue
        found.append(node.lineno)
    return found


class TmpLint(unittest.TestCase):
    def test_no_bare_mkdtemp_outside_tests(self):
        offenders = [f"{rel}:{line}" for rel, path in product_files()
                     for line in bare_mkdtemp(path.read_text(errors="replace"))]
        self.assertEqual(offenders, [], "make temp dirs with operating.tmp.mkdtemp "
                         "(ontology.tmp_root.mkdtemp below operating) or say why on the line "
                         "with '# tmp-lint: <reason>'")

    def test_the_lint_sees_a_bare_call_and_lets_dir_and_a_reason_pass(self):
        self.assertEqual(bare_mkdtemp("import tempfile\nx = tempfile.mkdtemp(prefix='a')\n"), [2])
        self.assertEqual(bare_mkdtemp("from tempfile import mkdtemp\nmkdtemp()\n"), [2])
        self.assertEqual(bare_mkdtemp("tempfile.mkdtemp(prefix='a', dir=d)\n"), [])
        self.assertEqual(bare_mkdtemp("tempfile.mkdtemp(**kw)\n"), [])
        self.assertEqual(bare_mkdtemp("tempfile.mkdtemp()  # tmp-lint: socket path\n"), [])
        self.assertEqual(bare_mkdtemp("from ontology import tmp_root\ntmp_root.mkdtemp()\n"), [])


if __name__ == "__main__":
    unittest.main()
