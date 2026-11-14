"""Every third-party package the code imports is declared and pinned in the requirements
files, and each pin matches the installed version where the package is installed.

Imports are read from every tracked ``.py`` file (absolute imports, also those inside
``try``/functions); a module is local when the repository has a ``<name>.py`` or a
``<name>/`` package anywhere (scripts put their own directories on ``sys.path``). An
import whose distribution is unknown fails closed: add it to ``IMPORT_TO_DIST`` and to a
requirements file."""
from __future__ import annotations

import ast
import importlib.metadata
from pathlib import Path
import re
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
FILES = ("requirements.txt", "requirements-ml.txt", "requirements-dev.txt")
# Import name -> distribution name, for every third-party import the code may make.
IMPORT_TO_DIST = {
    "fastapi": "fastapi", "starlette": "starlette", "pydantic": "pydantic",
    "uvicorn": "uvicorn", "psycopg": "psycopg", "numpy": "numpy",
    "onnxruntime": "onnxruntime", "pypdf": "pypdf", "reportlab": "reportlab",
    "PIL": "pillow", "markitdown": "markitdown", "torch": "torch",
    "transformers": "transformers", "sentence_transformers": "sentence-transformers",
    "docx": "python-docx", "pptx": "python-pptx", "mypy": "mypy",
}
# A submodule import that needs another distribution at import time (the module raises
# without it), declared beside the import: starlette's TestClient needs httpx2.
SUBMODULE_NEEDS = {
    "fastapi.testclient": "httpx2", "starlette.testclient": "httpx2",
}
PIN = re.compile(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?==([^\s#;]+)")


def canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def declared() -> dict[str, str]:
    """distribution -> pinned version across the requirements files; an unpinned line fails."""
    pins: dict[str, str] = {}
    for name in FILES:
        for raw in (ROOT / name).read_text("utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or line.startswith("-r "):
                continue
            match = PIN.match(line)
            if not match:
                raise AssertionError(f"{name}: not pinned with ==: {raw!r}")
            pins[canon(match[1])] = match[2]
    return pins


def tracked_python() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, text=True,
                         capture_output=True, check=True).stdout.split()
    return [ROOT / p for p in out]


def third_party_imports(full: bool = False) -> dict[str, set[str]]:
    """Top-level third-party import name -> files importing it (``full``: the whole
    dotted name, e.g. ``fastapi.testclient``)."""
    files = tracked_python()
    local = {p.stem for p in files} | {d.name for p in files for d in p.relative_to(ROOT).parents
                                       if d.name}
    std = set(sys.stdlib_module_names) | {"__future__"}
    found: dict[str, set[str]] = {}
    for path in files:
        try:
            tree = ast.parse(path.read_text("utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top not in std and top not in local:
                    found.setdefault(name if full else top, set()).add(
                        str(path.relative_to(ROOT)))
    return found


class DependencyManifestTest(unittest.TestCase):
    def test_every_third_party_import_is_declared_and_pinned(self):
        pins = declared()
        imports = third_party_imports()
        self.assertIn("psycopg", imports)  # the scan sees the code at all
        unknown = {name: sorted(paths)[:3] for name, paths in imports.items()
                   if name not in IMPORT_TO_DIST}
        self.assertEqual(unknown, {}, "imported but not mapped to a distribution; declare it")
        missing = sorted({IMPORT_TO_DIST[name] for name in imports}
                         - set(pins))
        self.assertEqual(missing, [], "imported but not in a requirements file")

    def test_a_submodule_import_declares_what_it_needs(self):
        """``from fastapi.testclient import TestClient`` raises without httpx2, which no
        file imports by name: the import scan alone misses it."""
        pins = declared()
        imports = third_party_imports(full=True)
        self.assertIn("fastapi.testclient", imports)  # the scan sees dotted names
        missing = {need: sorted(imports[name])[:3] for name, need in SUBMODULE_NEEDS.items()
                   if name in imports and canon(need) not in pins}
        self.assertEqual(missing, {}, "a submodule import needs this distribution; pin it")

    def test_pins_match_the_installed_versions(self):
        mismatched = {}
        for dist, pin in declared().items():
            try:
                installed = importlib.metadata.version(dist)
            except importlib.metadata.PackageNotFoundError:
                continue  # not installed on this host (an optional group)
            if installed.split("+", 1)[0] != pin:
                mismatched[dist] = (pin, installed)
        self.assertEqual(mismatched, {}, "pin != installed; update the requirements file")

    def test_an_unpinned_line_fails_closed(self):
        self.assertIsNone(PIN.match("fastapi>=0.1"))
        self.assertEqual(PIN.match("psycopg[binary]==3.3.6").groups(), ("psycopg", "3.3.6"))


if __name__ == "__main__":
    unittest.main()
