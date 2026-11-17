"""Every new worker pool or process ends with its parent (perm-box-1114, Fable audit 11-14
item 15).

A ``ProcessPoolExecutor``, ``multiprocessing.Pool`` or ``Process`` whose parent is killed
without its exit handlers (an OOM kill, a SIGKILL, a stopped scope) leaves its workers
running, reparented, for hours (11-13: five scopes, ~1.7 GB, nine hours). The cure is
``ontology.parent_watch``: a pool passes ``**parent_watch.pool_init(...)`` (or an
``initializer`` built from it), and a ``Process`` target calls ``parent_watch.die_with``
(this lint takes a module that calls ``die_with`` as watching its processes). A call that may not do so
says why on its line with ``# parent-watch: <reason>``.

The sites that predate the rule are counted per file in
``tests/pool_parent_watch_baseline.json``: a file may hold no more unwatched sites than its
count (a new one fails; fixing one is always allowed: lower the count in the same change).
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).with_name("pool_parent_watch_baseline.json")
SKIP_DIRS = {"wt", ".worktrees", "node_modules", ".git", "EVIDENCE", "app", "tests",
             "__pycache__", ".venv", "venv"}
MARKER = "parent-watch:"
POOLS = {"ProcessPoolExecutor", "Pool"}
PROCESSES = {"Process"}


def product_files(root: Path = ROOT):
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]) or rel.name.startswith("test_"):
            continue
        yield rel, path


def _called(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _from_multiprocessing(func: ast.expr, imported: set[str]) -> bool:
    """Whether the call is a process pool or process: ``ProcessPoolExecutor`` anywhere;
    ``Pool``/``Process`` on a ``multiprocessing`` name, a context or ``get_context(...)``,
    or a bare name imported from multiprocessing."""
    name = _called(func)
    if name == "ProcessPoolExecutor":
        return True
    if isinstance(func, ast.Name):
        return name in imported
    if not isinstance(func, ast.Attribute):
        return False
    base = func.value
    if isinstance(base, ast.Call):        # mp.get_context("spawn").Pool(...)
        return _called(base.func) == "get_context"
    text = ast.unparse(base)
    return ("multiprocessing" in text or text in ("mp", "ctx", "context", "mp_ctx")
            or text.endswith(("_ctx", "_context", ".ctx")))


def unwatched(source: str) -> list[int]:
    """Lines of pool/process starts with no parent watch and no ``# parent-watch:`` reason."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.splitlines()
    imported = {a.asname or a.name for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("multiprocessing")
                for a in n.names if a.name in POOLS | PROCESSES}
    names_watch = "die_with(" in source
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _called(node.func) not in POOLS | PROCESSES:
            continue
        if not _from_multiprocessing(node.func, imported):
            continue
        span = lines[node.lineno - 1:(node.end_lineno or node.lineno)]
        if any(MARKER in line for line in span):
            continue
        kwargs = " ".join(ast.unparse(k) for k in node.keywords)
        if "parent_watch" in kwargs or "pool_init" in kwargs:
            continue
        if _called(node.func) in PROCESSES and names_watch:
            continue   # its target watches its parent (die_with)
        found.append(node.lineno)
    return found


def current() -> dict[str, list[int]]:
    out = {}
    for rel, path in product_files():
        lines = unwatched(path.read_text(errors="replace"))
        if lines:
            out[str(rel)] = lines
    return out


class PoolParentWatchLint(unittest.TestCase):
    def test_no_new_pool_or_process_without_a_parent_watch(self):
        baseline = json.loads(BASELINE.read_text())
        over = {f: lines for f, lines in current().items() if len(lines) > baseline.get(f, 0)}
        self.assertEqual(over, {}, "start pools with **parent_watch.pool_init() (ontology."
                         "parent_watch) and Process targets with parent_watch.die_with, or say "
                         "why on the line with '# parent-watch: <reason>'")

    def test_the_lint_sees_pools_and_processes_and_lets_watched_ones_pass(self):
        self.assertEqual(unwatched("from concurrent.futures import ProcessPoolExecutor\n"
                                   "ProcessPoolExecutor(4)\n"), [2])
        self.assertEqual(unwatched("import multiprocessing as mp\n"
                                   "mp.get_context('spawn').Pool(2)\n"), [2])
        self.assertEqual(unwatched("import multiprocessing\nmultiprocessing.Process(target=f)\n"),
                         [2])
        self.assertEqual(unwatched("ProcessPoolExecutor(4, **parent_watch.pool_init())\n"), [])
        self.assertEqual(unwatched("from ontology import parent_watch\n"
                                   "multiprocessing.Process(target=f)\n"), [2])
        self.assertEqual(unwatched("def f():\n    parent_watch.die_with(1)\n"
                                   "multiprocessing.Process(target=f)\n"), [])
        self.assertEqual(unwatched("ProcessPoolExecutor(4)  # parent-watch: joins at exit\n"), [])
        self.assertEqual(unwatched("from concurrent.futures import ThreadPoolExecutor\n"
                                   "ThreadPoolExecutor(4)\nsome.Pool()\n"), [])


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["--baseline"]:
        BASELINE.write_text(json.dumps({f: len(v) for f, v in current().items()}, indent=1,
                                       sort_keys=True) + "\n")
        sys.exit(0)
    unittest.main()
