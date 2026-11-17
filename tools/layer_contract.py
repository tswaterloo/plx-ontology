"""The layer contract: no new import from a lower layer of the product into a higher one.

The product's packages are layered, lowest first: ``ontology`` (the schema-defined graph),
``integrations`` (the source adapters), ``operating`` (the runtime, agents, tools),
``operator_api`` (the app's API). A module may import its own package and any lower one.
An import from a layer into a higher one, or into a first-party package outside the layers
(``evals``, ``scripts``, ``tools``, ``twin``, ...), is an upward edge. The upward edges that
existed when the contract was frozen (2026-11-12, ARCH-REVIEW-1112 item 9) are listed in
``tools/layer-contract-allowlist.json``; any other upward edge fails the check.

An edge is one (importing module, imported module) pair of first-party modules, test
modules excluded on both sides (a ``tests`` package or a ``test_*`` file imports anything).
Every static import counts, function-local and ``TYPE_CHECKING`` ones included, relative
imports resolved, plus ``importlib.import_module("<constant>")`` and
``__import__("<constant>")``. ``from pkg import name`` is the module ``pkg.name`` when that
is a module, else ``pkg``. An imported name that is no first-party module (the standard
library, a third-party package) is no edge.

  python3 tools/layer_contract.py            check: exit 1 on a new upward edge
  python3 tools/layer_contract.py --prune    drop allowlisted edges that no longer exist
  python3 tools/layer_contract.py --list     every upward edge, counted per layer pair

There is deliberately no option that adds edges: a new upward import is fixed by moving the
code down (or the import's user up), or, as a reviewed exception, added to the allowlist by
hand in the same commit, where the diff shows it.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Iterable, Iterator

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.import_graph import is_test_path, module_name, source_files  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / "tools" / "layer-contract-allowlist.json"
LAYERS = ("ontology", "integrations", "operating", "operator_api")   # lowest first
RANK = {name: i for i, name in enumerate(LAYERS)}
FORMAT = "plx.layer-contract.v1"


def layer_of(module: str) -> str:
    return module.split(".", 1)[0]


def upward(importer: str, imported: str, first_party: set[str]) -> bool:
    """Whether ``importer`` -> ``imported`` goes up: from a layer into a higher layer or
    into a first-party package outside the layers. Within one package, never."""
    src, dst = layer_of(importer), layer_of(imported)
    if src == dst or src not in RANK or dst not in first_party:
        return False
    return dst not in RANK or RANK[dst] > RANK[src]


def _resolve(dotted: str, modules: set[str]) -> str | None:
    """The longest prefix of ``dotted`` that is a first-party module."""
    parts = dotted.split(".")
    while parts:
        name = ".".join(parts)
        if name in modules:
            return name
        parts.pop()
    return None


def _absolute(node: ast.ImportFrom, package: str) -> str | None:
    if not node.level:
        return node.module
    base = package.split(".") if package else []
    if node.level - 1 > len(base):
        return None
    base = base[: len(base) - (node.level - 1)]
    return ".".join([*base, node.module] if node.module else base) or None


def _constant_import(node: ast.Call) -> str | None:
    """The constant module name of ``importlib.import_module("x")`` / ``__import__("x")``."""
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name not in ("import_module", "__import__") or not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str) and \
            not first.value.startswith("."):
        return first.value
    return None


def imports_of(text: str, module: str, is_package: bool, modules: set[str]) -> set[str]:
    """The first-party modules ``module`` imports (its own name excluded)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    package = module if is_package else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if (hit := _resolve(alias.name, modules)):
                    found.add(hit)
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(node, package)
            if not base:
                continue
            for alias in node.names:
                sub = f"{base}.{alias.name}"
                hit = sub if sub in modules else _resolve(base, modules)
                if hit:
                    found.add(hit)
        elif isinstance(node, ast.Call) and (name := _constant_import(node)):
            if (hit := _resolve(name, modules)):
                found.add(hit)
    found.discard(module)
    return found


def upward_edges(root: Path) -> set[tuple[str, str]]:
    """Every upward edge of the tree at ``root`` (test modules excluded)."""
    paths = {module_name(p): p for p in source_files(root)}
    modules = set(paths)
    first_party = {layer_of(m) for m in modules}
    edges: set[tuple[str, str]] = set()
    for module, path in paths.items():
        if layer_of(module) not in RANK or is_test_path(path):
            continue
        text = (root / path).read_text(encoding="utf-8", errors="replace")
        for target in imports_of(text, module, path.endswith("__init__.py"), modules):
            if is_test_path(paths[target]):
                continue
            if upward(module, target, first_party):
                edges.add((module, target))
    return edges


def load_allowlist(path: Path = ALLOWLIST) -> set[tuple[str, str]]:
    """The frozen edges; a file that is missing or not this format fails closed (raises)."""
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict) or body.get("format") != FORMAT or \
            not isinstance(body.get("edges"), list):
        raise ValueError(f"{path}: not a {FORMAT} allowlist")
    edges = set()
    for row in body["edges"]:
        if not (isinstance(row, list) and len(row) == 2 and all(isinstance(x, str) for x in row)):
            raise ValueError(f"{path}: bad edge {row!r}")
        edges.add((row[0], row[1]))
    return edges


def write_allowlist(edges: Iterable[tuple[str, str]], path: Path = ALLOWLIST) -> None:
    rows = sorted(edges)
    pairs = Counter(f"{layer_of(a)} -> {layer_of(b)}" for a, b in rows)
    body = {"format": FORMAT,
            "about": "Upward imports frozen by tools/layer_contract.py: only removals, "
                     "never additions without a reviewed reason.",
            "counts": dict(sorted(pairs.items())),
            "edges": [list(r) for r in rows]}
    head = json.dumps({k: v for k, v in body.items() if k != "edges"}, indent=1)[:-2]
    lines = ",\n  ".join(json.dumps(r) for r in body["edges"])
    path.write_text(f'{head},\n "edges": [\n  {lines}\n ]\n}}\n', encoding="utf-8")


def check(root: Path, allowlist: Path) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(new upward edges, allowlisted edges that no longer exist)."""
    now, frozen = upward_edges(root), load_allowlist(allowlist)
    return sorted(now - frozen), sorted(frozen - now)


def _report(new: list[tuple[str, str]], gone: list[tuple[str, str]]) -> Iterator[str]:
    for a, b in new:
        yield f"NEW upward import: {a} -> {b} ({layer_of(a)} may not import {layer_of(b)})"
    if gone:
        yield (f"{len(gone)} allowlisted edge(s) no longer exist: run "
               "python3 tools/layer_contract.py --prune")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--allowlist", type=Path, default=None)
    parser.add_argument("--prune", action="store_true")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    allowlist = args.allowlist or args.root / "tools" / "layer-contract-allowlist.json"
    if args.list:
        edges = upward_edges(args.root)
        for pair, n in sorted(Counter(f"{layer_of(a)} -> {layer_of(b)}"
                                      for a, b in edges).items()):
            print(f"{n:5d}  {pair}")
        print(f"{len(edges):5d}  total")
        return 0
    new, gone = check(args.root, allowlist)
    if args.prune:
        write_allowlist(load_allowlist(allowlist) - set(gone), allowlist)
        print(f"pruned {len(gone)} edge(s)")
        gone = []
    for line in _report(new, gone):
        print(line)
    if new:
        print(f"layer contract: {len(new)} new upward import(s) (tools/layer_contract.py)")
        return 1
    print("layer contract ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
