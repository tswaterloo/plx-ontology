"""AUDIT3-09: every direct ``plx.object`` / ``plx.around`` read in operator_api is listed,
with its reason, in docs/data-model.md ("Which API reads use the models"); a new one must
either use ``operating.typed_graph.GraphRepository`` or be added there. Static check only."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = {"plx.object", "plx.around"}
REASONS = ("label", "generic", "as-known-at")


def direct_reads(root: Path = ROOT) -> set[tuple[str, str]]:
    """(module.function, tool) of each call whose first argument is a read tool's name,
    in operator_api's modules (the mocks are fixtures, not reads)."""
    found: set[tuple[str, str]] = set()
    for path in sorted((root / "operator_api").glob("*.py")):
        if path.name.startswith("mock"):
            continue

        def walk(node: ast.AST, fn: str) -> None:
            for child in ast.iter_child_nodes(node):
                name = child.name if isinstance(child, (ast.FunctionDef,
                                                        ast.AsyncFunctionDef)) else fn
                if isinstance(child, ast.Call) and child.args and \
                        isinstance(child.args[0], ast.Constant) and child.args[0].value in TOOLS:
                    found.add((f"{path.stem}.{fn}", child.args[0].value))
                walk(child, name)
        walk(ast.parse(path.read_text(encoding="utf-8")), "<module>")
    return found


def listed(root: Path = ROOT) -> dict[tuple[str, str], str]:
    text = (root / "docs" / "data-model.md").read_text(encoding="utf-8")
    section = text.split("## Which API reads use the models", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\| `([\w.<>]+)` \| (plx\.\w+) \| ([^|]+) \|$", section, re.M)
    return {(read, tool): reason.strip() for read, tool, reason in rows}


class DataModelReads(unittest.TestCase):
    def test_every_direct_read_is_listed_with_a_reason_and_none_is_stale(self) -> None:
        reads, rows = direct_reads(), listed()
        self.assertTrue(reads)
        self.assertEqual(sorted(reads - rows.keys()), [],
                         "a direct read not in docs/data-model.md: use GraphRepository or list it")
        self.assertEqual(sorted(rows.keys() - reads), [], "a listed read no longer exists")
        for key, reason in rows.items():
            self.assertTrue(reason.startswith(REASONS), (key, reason))


if __name__ == "__main__":
    unittest.main()
