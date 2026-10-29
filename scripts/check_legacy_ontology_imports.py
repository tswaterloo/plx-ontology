"""Legacy-ontology import ratchet (`no-legacy-ontology-imports` in devloop/gate-checks.json).

10-DECISION / 09-CRITIQUE R10 and "Migration order": new features land only on the new
ontology path. The legacy uni-temporal store ``ontology.core.OntologyStore`` and its
identity resolver ``ontology.transactions.TransactionResolver`` are deleted (lane
identity-ledger-retire: human identity decisions live in the append-only review store,
``ontology.reviews``), and so is the case workspace's own join ``operator_ui.case_join``
(a case's relationships are the projection's ontology links). This AST scan keeps them
deleted: it fails when any file names either class or either module. The allowlist is empty and only shrinks: the gate runs main's
copy of this script (a candidate cannot add itself), and ``--strict`` (run by
tests/test_legacy_ontology_imports.py on the candidate's own copy) also fails on stale
entries.

A file is an importer when it (rules):
  import-module   imports module ``ontology.transactions`` or ``operator_ui.case_join``
                  in any form (``import ontology.transactions``, ``from ontology
                  import transactions``, ``from .transactions import ...`` inside
                  ``ontology/``, ``from operator_ui import case_join``)
  import-name     imports ``OntologyStore`` or ``TransactionResolver`` from ANY
                  module (a re-export such as ``from operating.retrieval import
                  OntologyStore`` counts), or ``*`` from ``ontology``,
                  ``ontology.core`` or ``ontology.transactions``
  attribute       reads ``<anything>.OntologyStore`` / ``.TransactionResolver``
  string          has a string literal naming either class or either module
                  (``getattr(core, "OntologyStore")``, ``importlib.import_module(
                  "ontology.transactions")``, ``mock.patch("x.OntologyStore")``)
  dynamic-import  calls ``importlib.import_module`` / ``__import__`` (any alias of
                  them) with a module name built from strings that can name either
                  module: concatenation, f-string, ``%`` / ``.format`` / ``.join``,
                  a relative name with its ``package``, a ``fromlist`` entry
                  (``import_module("ontology." + "transactions")``,
                  ``__import__("ontology", fromlist=[name])``); a computed part
                  matches anything, a name with no string part is not judged

Shared value types (``Actor``, ``EntityRef``, ``LEVELS``, ``ValidationError``)
are not legacy: import them from ``ontology.types`` (or ``ontology``; ``ontology.core``
re-exports them for existing importers). This script and its test are not scanned (they
name the classes as data). ``EVIDENCE/`` holds frozen reproduction scripts and is skipped.

Usage: python3 scripts/check_legacy_ontology_imports.py [ROOT] [--strict]
Exit 0 clean, 1 on a new importer (or, with --strict, a stale allowlist entry).
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
LEGACY_NAMES = frozenset({"OntologyStore", "TransactionResolver"})
LEGACY_MODULES = frozenset({"ontology.transactions", "operator_ui.case_join"})
LEGACY_SOURCES = frozenset({"ontology", "ontology.core", "ontology.transactions"})
NOT_SCANNED = frozenset({
    "scripts/check_legacy_ontology_imports.py", "tests/test_legacy_ontology_imports.py",
})
SKIP_DIRS = frozenset({"EVIDENCE", "node_modules", "__pycache__"})

# Importers that existed when C0 landed (2026-10-01); every one is gone. Never add one.
ALLOWLIST: frozenset[str] = frozenset()


def _module_of(relative: str) -> str:
    parts = list(Path(relative).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve(relative: str, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    package = _module_of(relative).split(".")
    if not relative.endswith("__init__.py"):
        package = package[:-1]
    base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
    return ".".join([*base, *([node.module] if node.module else [])])


def _is_legacy_module(name: str) -> bool:
    return any(name == module or name.startswith(module + ".") for module in LEGACY_MODULES)


def _is_legacy_string(value: str) -> bool:
    if value in LEGACY_NAMES or _is_legacy_module(value):
        return True
    return any(value.endswith("." + name) for name in LEGACY_NAMES)


_DYNAMIC = ("import_module", "__import__")
_ANY = ".*"   # a computed part of a built name


def _pattern(node: ast.AST | None) -> str:
    """A regex for the string ``node`` builds: its string parts literal, every
    computed part a wildcard."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return re.escape(node.value)
    if isinstance(node, ast.JoinedStr):
        return "".join(_pattern(value) if isinstance(value, ast.Constant) else _ANY
                       for value in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _pattern(node.left) + _pattern(node.right)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) \
            and isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
        return _ANY.join(re.escape(part) for part in
                         re.split(r"%(?:\([^)]*\))?[-#0 +]*\d*(?:\.\d+)?[a-z%]",
                                  node.left.value))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and isinstance(node.func.value, ast.Constant) \
            and isinstance(node.func.value.value, str):
        text = node.func.value.value
        if node.func.attr == "join" and len(node.args) == 1 \
                and isinstance(node.args[0], (ast.List, ast.Tuple)):
            return re.escape(text).join(_pattern(item) for item in node.args[0].elts)
        if node.func.attr == "format":
            return _ANY.join(re.escape(part) for part in re.split(r"\{[^{}]*\}", text))
    return _ANY


def _argument(call: ast.Call, position: int, name: str) -> ast.AST | None:
    if len(call.args) > position:
        return call.args[position]
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _dynamic_names(call: ast.Call, function: str) -> list[str]:
    """Regexes of the module names a dynamic import call can import."""
    name = _argument(call, 0, "name")
    if name is None:
        return []
    base = _pattern(name)
    if function == "import_module":
        package = _argument(call, 1, "package")
        if package is not None and base.startswith(re.escape(".")):
            base = _pattern(package) + base
        return [base]
    fromlist = _argument(call, 3, "fromlist")
    out = [base]
    if isinstance(fromlist, (ast.List, ast.Tuple)):
        out += [base + re.escape(".") + _pattern(item) for item in fromlist.elts]
    elif fromlist is not None:
        out.append(base + re.escape(".") + _ANY)
    return out


def _legacy_pattern(pattern: str) -> str | None:
    """The legacy module a built name can be, unless nothing of it is a string."""
    if not pattern.replace(_ANY, "") or pattern.replace(_ANY, "") == re.escape("."):
        return None
    return next((module for module in sorted(LEGACY_MODULES)
                 if re.fullmatch(pattern, module, re.DOTALL)), None)


def scan_source(source: str, relative: str) -> list[tuple[str, int, str]]:
    """Return (rule, line, detail) findings for one file."""
    findings: list[tuple[str, int, str]] = []
    tree = ast.parse(source, filename=relative)
    dynamic = {name: name for name in _DYNAMIC}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "importlib":
            for alias in node.names:
                if alias.name in _DYNAMIC:
                    dynamic[alias.asname or alias.name] = alias.name
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = (func.attr if isinstance(func, ast.Attribute) and func.attr in _DYNAMIC
                      else dynamic.get(func.id) if isinstance(func, ast.Name) else None)
            if called is not None:
                for pattern in _dynamic_names(node, called):
                    module = _legacy_pattern(pattern)
                    if module is not None:
                        findings.append(("dynamic-import", node.lineno, module))
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_legacy_module(alias.name):
                    findings.append(("import-module", node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            module = _resolve(relative, node)
            if _is_legacy_module(module):
                findings.append(("import-module", node.lineno, module))
            for alias in node.names:
                if f"{module}.{alias.name}" in LEGACY_MODULES:
                    findings.append(("import-module", node.lineno, f"{module}.{alias.name}"))
                if alias.name in LEGACY_NAMES or (module in LEGACY_SOURCES and alias.name == "*"):
                    findings.append(("import-name", node.lineno, f"{module}.{alias.name}"))
        elif isinstance(node, ast.Attribute) and node.attr in LEGACY_NAMES:
            findings.append(("attribute", node.lineno, node.attr))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and _is_legacy_string(node.value):
            findings.append(("string", node.lineno, node.value))
    return sorted(set(findings), key=lambda item: (item[1], item[0], item[2]))


def _python_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if any(part.startswith(".") or part in SKIP_DIRS for part in relative.parts[:-1]):
            continue
        yield path, relative.as_posix()


def scan_tree(root: Path = ROOT) -> dict[str, list[tuple[str, int, str]]]:
    importers: dict[str, list[tuple[str, int, str]]] = {}
    for path, relative in _python_files(root):
        if relative in NOT_SCANNED:
            continue
        found = scan_source(path.read_text(encoding="utf-8"), relative)
        if found:
            importers[relative] = found
    return importers


def check(root: Path = ROOT, allowlist: frozenset[str] = ALLOWLIST) -> tuple[dict, list[str]]:
    """Return ({new importer: findings}, [stale allowlist entries])."""
    importers = scan_tree(root)
    new = {path: found for path, found in importers.items() if path not in allowlist}
    stale = sorted(path for path in allowlist if path not in importers)
    return new, stale


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    strict = "--strict" in args
    args = [arg for arg in args if arg != "--strict"]
    root = Path(args[0]).resolve() if args else ROOT
    new, stale = check(root)
    for path, found in sorted(new.items()):
        for rule, line, detail in found:
            print(f"{path}:{line}: {rule}: {detail} (new importer of a legacy ontology class; "
                  "use ontology.types or the new ontology path)")
    for path in stale:
        print(f"stale allowlist entry: {path} (no longer imports; remove it from ALLOWLIST)")
    if new or (strict and stale):
        return 1
    print(f"no-legacy-ontology-imports: ok ({len(ALLOWLIST) - len(stale)} allowlisted importers)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
