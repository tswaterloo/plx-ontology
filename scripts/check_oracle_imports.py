"""Oracle import ban.

It must never run Workbench code (``ontology``, ``operating``,
``integrations``, ``scripts``, ...), so its joins cannot share a bug with the projection and
resolver it grades. Transports are injected by ``scripts/eval_oracle_dev.py``.

A deny list of import forms is trivially bypassed (path loading, aliases, ``getattr``), so
this check is an **allowlist** by construction. It is a tripwire, like ``http-egress``: it
stops accidental and casual coupling, not a determined author (string concatenation still
defeats ``path-string``); the audit-hook test below is the second layer. Every ``*.py``
under ``evals/oracle/`` (tests included) fails when it:

  import        imports anything other than the stdlib modules in ``STDLIB`` or
                ``evals.oracle`` itself (absolute or relative; a relative import must
                resolve inside ``evals.oracle``). ``importlib``, ``runpy``, ``pkgutil``,
                ``builtins``, ``sys``, ``os``, ``imp``, ``types`` ... are therefore refused;
  call          names a builtin that runs or reaches code or namespaces dynamically:
                ``exec``, ``eval``, ``compile``, ``__import__``, ``open``, ``globals``,
                ``locals``, ``vars``, ``getattr``, ``setattr``, ``delattr``,
                ``breakpoint``, ``__builtins__``;
  dunder        reads a dunder attribute other than ``__name__``, ``__file__`` and
                ``__init__`` (``x.__class__.__mro__``, ``m.__loader__``, ``__dict__`` ...);
  path-string   holds a string constant (docstrings excepted) containing a repository code
                package name as a whole word (``ontology/json_contract.py``,
                ``"operating.case_runtime"``, ``"x='integrations'"``).

``tests/test_oracle_import_ban.py`` plants every known bypass and also imports and runs the
whole package under a ``sys.addaudithook`` that refuses any repository file opened or
executed outside ``evals/``.

Usage: python3 scripts/check_oracle_imports.py [ROOT]   (exit 0 clean, 1 findings)
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
SCANNED = "evals/oracle"
PACKAGE = "evals.oracle"
STDLIB = frozenset({
    "__future__", "collections", "dataclasses", "datetime", "decimal", "functools",
    "hashlib", "itertools", "json", "math", "pathlib", "random", "re", "string", "typing",
    "unittest", "urllib.parse",
})
BANNED_NAMES = frozenset({
    "exec", "eval", "compile", "__import__", "open", "globals", "locals", "vars", "getattr",
    "setattr", "delattr", "breakpoint", "__builtins__", "__loader__", "__spec__",
})
# Attributes/names that lead from an allowed module to other modules or to code execution.
BANNED_ATTRS = BANNED_NAMES | frozenset({
    "sys", "os", "modules", "system", "popen", "spawn", "subprocess", "import_module",
    "loader", "loaders", "loadTestsFromName", "loadTestsFromNames", "loadTestsFromModule",
    "discover", "get_type_hints", "ForwardRef", "mock", "patch", "builtins", "run_path",
    "run_module", "exec_module", "load_module", "resolve_name",
})
TEST_ONLY = frozenset({"unittest"})
ALLOWED_DUNDERS = frozenset({"__name__", "__file__", "__init__"})
REPO_PACKAGES = ("ontology", "operating", "integrations", "scripts", "operator_ui", "ops_plane",
                 "reasoning", "engineering")
_PATH_STRING = re.compile(r"(?<![A-Za-z0-9_\-])(?:%s)(?![A-Za-z0-9_\-])" % "|".join(REPO_PACKAGES))


def _module_of(path: Path, root: Path) -> list[str]:
    parts = list(path.relative_to(root).with_suffix("").parts)
    return parts[:-1] if parts[-1] == "__init__" else parts


def _absolute(node: ast.ImportFrom, path: Path, root: Path) -> str | None:
    if node.level == 0:
        return node.module or ""
    package = _module_of(path, root)
    if path.name != "__init__.py":
        package = package[:-1]
    if node.level - 1 > len(package):
        return None
    base = package[:len(package) - (node.level - 1)]
    return ".".join([*base, *([node.module] if node.module else [])])


def _allowed(module: str, in_tests: bool = True) -> bool:
    if module.split(".")[0] in TEST_ONLY and (not in_tests or module != "unittest"):
        return False
    return module in STDLIB or module == PACKAGE or module.startswith(PACKAGE + ".")


def _docstrings(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def scan_file(path: Path, root: Path) -> list[tuple[str, int, str, str]]:
    relative = path.relative_to(root).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"), relative)
    skip = _docstrings(tree)
    out = []
    in_tests = relative.startswith(SCANNED + "/tests/")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _allowed(alias.name, in_tests):
                    out.append((relative, node.lineno, "import", alias.name))
        elif isinstance(node, ast.ImportFrom):
            target = _absolute(node, path, root)
            if target is None:
                out.append((relative, node.lineno, "import", "." * node.level))
            elif target in ("evals", "") or (node.level and not node.module):
                for alias in node.names:  # ``from evals import oracle`` / ``from .. import x``
                    name = f"{target}.{alias.name}".lstrip(".")
                    if not _allowed(name, in_tests):
                        out.append((relative, node.lineno, "import", name))
            elif not _allowed(target, in_tests):
                out.append((relative, node.lineno, "import", target))
            for alias in node.names:
                if alias.name in BANNED_ATTRS or alias.name == "*":
                    out.append((relative, node.lineno, "name", alias.name))
        elif isinstance(node, ast.Name) and node.id in BANNED_ATTRS:
            out.append((relative, node.lineno, "name", node.id))
        elif isinstance(node, ast.Attribute):
            regex = (node.attr == "compile" and isinstance(node.value, ast.Name)
                     and node.value.id == "re")  # re.compile builds a pattern, runs no code
            if node.attr in BANNED_ATTRS and not regex:
                out.append((relative, node.lineno, "name", node.attr))
            elif (node.attr.startswith("__") and node.attr.endswith("__") and
                    node.attr not in ALLOWED_DUNDERS):
                out.append((relative, node.lineno, "dunder", node.attr))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in skip and _PATH_STRING.search(node.value):
            out.append((relative, node.lineno, "path-string", node.value[:80]))
    return out


def scan(root: Path) -> list[tuple[str, int, str, str]]:
    base = root / SCANNED
    if not base.is_dir():
        return [(SCANNED, 0, "missing", "oracle package not found")]
    findings = []
    for path in sorted(base.rglob("*")):
        if "__pycache__" in path.parts or path.is_dir():
            continue
        if path.suffix != ".py":
            findings.append((path.relative_to(root).as_posix(), 0, "file", "not a .py source"))
            continue
        findings.extend(scan_file(path, root))
    return findings


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]).resolve() if args else ROOT
    findings = scan(root)
    for relative, line, rule, detail in findings:
        print(f"{relative}:{line}: {rule}: {detail}")
    if findings:
        print(f"oracle-import-ban: {len(findings)} finding(s); evals/oracle may import only "
              f"the stdlib allowlist and evals.oracle")
        return 1
    print("oracle-import-ban: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
