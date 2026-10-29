"""Hardcoded-case gate (`no-hardcoded-cases` in devloop/gate-checks.json).

Real evaluation cases (AO-26005B, PO5447, SoapBox, ...) are evaluation data: they
belong in live-checks, tests, EVIDENCE and docs, never in product code paths. This
scan is a tripwire: it fails when product code under the scanned trees carries a
case identifier or expected value, is named after one case, or reaches the
evaluation data or the tests (general intelligence never reads the answers).

Rules:
  identifier    a denylisted identifier/expected value in a Python string or number
                constant (docstrings skipped; comments are not in the AST), a JSON key
                or value (the subtree under a provenance key such as ``evidence`` or
                ``source`` is skipped: it cites real cases as evidence for general
                knowledge), or a JS string literal.
  module_name   a file whose name matches a case/kind-named glob, or a Python
                function/class whose name matches the case-name regex.
  eval_reference  an import of an ``evals``/``tests`` package (relative imports
                resolved) or of the eval tooling (``EVAL_TOOLING``), a string that is
                an ``evals/``/``tests/`` path or such a module's name, a bare
                ``"evals"``/``"tests"`` path part (``/`` operand, ``Path``/``join``
                argument), and an import or ``module.attribute`` read of a top-level
                name another product module derives from one of these at import time
                (a constant built from one, a function whose defaults name one), so
                ``X = ROOT / "evals" / ...`` flags every module that imports ``X``.
                Scanned: the Python and ``operator_ui`` JS of the scanned trees, not
                devloop/, tests or the eval tooling.

Identifiers are the hand-listed ``identifiers`` (names and curated values) plus every
identifier the evaluation data names (``eval_identifiers``): each ``plx-eval-item/v1``
item under ``evals/`` and every JSON file of the real-case directories
(``EVAL_CASE_DIRS``), read from the tree and from the base, so a new eval item is
denylisted the moment it lands and a candidate cannot drop one. Harvested are root
identifiers and the identifier-like tokens (``_id_like``) of questions, values,
expected values and names. Append new real names to ``identifiers``; never add
allowlist entries for new code. Each allowlist entry
``{path, rule, match, reason, expires}`` suppresses one rule for one match key in
one file (``match`` is the identifier ``id`` for ``identifier``, the file or
symbol name for ``module_name``) through ``expires`` (inclusive, UTC).
``eval_reference`` has no allowlist. ``ratchet`` lists the ``eval_reference`` and
``identifier`` findings that existed when these rules landed (the derived identifiers
found real eval ids in product data), each by ``path``, ``rule``, ``match`` (the
identifier id or the normalized reference) and ``occurrence`` (its 0-based index among
that file's findings of the same rule and match, in line order). Lines are not part of
it, so moving code passes; only a new or changed finding fails. It may only shrink.

The candidate cannot loosen the gate that admits it. The base (``--base``, default
``origin/main``) is authoritative:
  * scanner data is the union of the base's and the candidate's identifiers and
    name patterns (a candidate can add a real case, never drop one);
  * the allowlist is the intersection: a candidate entry counts only if a base
    entry with the same path and rule covers it (same ``match``, or a legacy base
    entry without ``match``, which covers the whole file for that rule) and its
    ``expires`` is not later than that base entry's. A candidate entry that is not
    covered ("adds") or outlives its base entry ("extends expiry") is refused: it
    suppresses nothing and is itself a violation. Removing or narrowing is free.
  * an expired entry is a violation only if its path is a file the candidate
    changes (``git diff --no-renames`` from merge-base(base, HEAD) to the working
    tree, plus untracked files, both sides of renames). A net-new match can only
    appear in a changed file, so this also catches new matches hiding behind an
    expired entry. In an unchanged file the entry is reported, keeps suppressing
    and blocks nobody: an expiry cannot deadlock unrelated candidates.
    ``--strict-expiry`` (the daily audit) fails every expired entry.
  * the ratchet is the intersection too: a candidate entry the base does not list
    is refused, an entry no finding matches any more is stale and fails (remove it
    with the fix: a file's last occurrence of that match), any other finding fails.
  * bootstrap: when the base has no denylist (or the base ref does not resolve)
    the candidate's file is used alone and every expired entry fails; a base without
    a ratchet takes the candidate's. When the changed-file list cannot be computed,
    every file counts as changed.

Usage: python3 scripts/check_no_hardcoded_cases.py [ROOT] [--base REF] [--strict-expiry]
(exit 0 clean, 1 findings)
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, replace
import datetime as dt
import fnmatch
import functools
import json
from json.decoder import scanstring
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DENYLIST = "devloop/hardcoded-case-denylist.json"
SCHEMA = "plx-hardcoded-case-denylist/v1"
SCANNED = ("ontology", "integrations", "operating", "operator_ui", "ops_plane",
           "reasoning", "engineering", "scripts", "twin")
EXCLUDED_PREFIXES = ("examples/", "EVIDENCE/", "docs/", "devloop/CONTEXT/",
                     "devloop/live-checks/")
PROVENANCE_KEYS = {"evidence", "verification", "source", "sources", "citation",
                   "citations", "note", "notes", "comment", "provenance", "why"}
RULES = ("identifier", "module_name")
EVAL_REFERENCE = "eval_reference"
RATCHET_RULES = ("identifier", EVAL_REFERENCE)
# The evaluation side outside evals/: harness entry points that run the evals and the
# gate checkers that read evals/ or name tests/ files by design. Product code may not
# import them. Listed by name: a check_* script that touches neither (upstream drift,
# HTTP egress) is a shared helper and stays product code, scanned and importable.
EVAL_TOOLING = ("scripts/eval_*.py", "scripts/check_no_hardcoded_cases.py",
                "scripts/check_oracle_imports.py", "scripts/check_legacy_ontology_imports.py")
EVAL_ITEM_FORMAT = "plx-eval-item/v1"
EVAL_CASE_DIRS = ("evals/exposed/", "evals/dev/cases/")
EVAL_KEYS = frozenset({"identifier", "question", "text", "paraphrases", "value", "expected",
                       "name"})
_EVAL_PACKAGES = ("evals", "tests")
_EVAL_PATH = re.compile(r"(?:^|[/\\])(?:evals|tests)[/\\]")
_MODULE_NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")
_PATH_CALLS = frozenset({"Path", "PurePath", "PosixPath", "PurePosixPath", "join",
                         "joinpath"})
_EVAL_TOKEN = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?"
                         r"|[A-Za-z0-9](?:[A-Za-z0-9#./*-]*[A-Za-z0-9])?")
_EVAL_DATE = re.compile(r"(?:\d{4}-\d{1,2}(?:-\d{1,2})?|\d{4}/\d{1,2}/\d{1,2}"
                        r"|\d{1,2}/\d{1,2}/\d{2,4})(?:T[\d:.]+Z?)?")
_EVAL_NUMBER = re.compile(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")


Fingerprint = tuple[str, str, str, int]  # (path, rule, match, occurrence) of one finding


@dataclass(frozen=True)
class Denylist:
    identifiers: tuple[tuple[str, re.Pattern[str]], ...]
    module_globs: tuple[str, ...]
    name_regexes: tuple[re.Pattern[str], ...]
    allowlist: tuple[dict, ...]
    ratchet: frozenset[Fingerprint] | None = None  # None: the file has no ratchet

    @functools.cached_property
    def any_identifier(self) -> re.Pattern[str]:
        """Every identifier pattern in one alternation: text it misses is skipped."""
        parts = []
        for _, pattern in self.identifiers:
            flags = re.match(r"\(\?([imsx]+)\)", pattern.pattern)
            parts.append(f"(?{flags[1]}:{pattern.pattern[flags.end():]})" if flags
                         else f"(?:{pattern.pattern})")
        return re.compile("|".join(parts) or "(?!)")

    @functools.cached_property
    def matched(self) -> dict[str, list[tuple[str, str]]]:
        """``_matches`` per distinct text (most scanned scalars repeat)."""
        return {}


def parse_denylist(text: str, *, require_match: bool = True) -> Denylist:
    """Denylist from JSON text. ``require_match=False`` accepts legacy base entries
    without ``match`` (they cover every match of their rule in their file)."""
    data = json.loads(text)
    if data.get("schema") != SCHEMA:
        raise ValueError(f"{DENYLIST}: schema must be {SCHEMA}")
    identifiers = tuple((item["id"], re.compile(item["pattern"]))
                        for item in data["identifiers"])
    names = data["module_name_patterns"]
    allowlist = tuple(data.get("allowlist", ()))
    for entry in allowlist:
        if entry.get("rule") not in RULES or not entry.get("path") \
                or not entry.get("reason") \
                or (require_match and not entry.get("match")):
            raise ValueError(f"{DENYLIST}: bad allowlist entry {entry}")
        dt.date.fromisoformat(entry["expires"])
    ratchet = None
    if "ratchet" in data:
        ratchet = set()
        for entry in data["ratchet"]:
            path, rule, match, occurrence = (
                entry.get(key) for key in ("path", "rule", "match", "occurrence"))
            if not (isinstance(path, str) and path and isinstance(match, str) and match
                    and rule in RATCHET_RULES and isinstance(occurrence, int)
                    and not isinstance(occurrence, bool) and occurrence >= 0):
                raise ValueError(f"{DENYLIST}: bad ratchet entry {entry}")
            ratchet.add((path, rule, match, occurrence))
        ratchet = frozenset(ratchet)
    return Denylist(identifiers, tuple(names["file_globs"]),
                    (re.compile(names["symbol_regex"]),), allowlist, ratchet)


def load_denylist(root: Path) -> Denylist:
    return parse_denylist((root / DENYLIST).read_text(encoding="utf-8"))


def _union(base: Denylist, candidate: Denylist | None) -> Denylist:
    """Base scanner data plus whatever the candidate adds; never less than base."""
    if candidate is None:
        return Denylist(base.identifiers, base.module_globs, base.name_regexes, ())
    seen = {(case_id, pattern.pattern) for case_id, pattern in base.identifiers}
    extra = tuple(item for item in candidate.identifiers
                  if (item[0], item[1].pattern) not in seen)
    globs = base.module_globs + tuple(g for g in candidate.module_globs
                                      if g not in base.module_globs)
    regexes = base.name_regexes + tuple(r for r in candidate.name_regexes
                                        if r.pattern not in {b.pattern for b in base.name_regexes})
    return Denylist(base.identifiers + extra, globs, regexes, ())


# ------------------------------------------------------------------ eval identifiers

def _significant(number: str) -> str:
    """The significant digits of a decimal number's text (``40,100`` -> ``401``)."""
    whole, _, fraction = number.replace(",", "").lstrip("+-").partition(".")
    whole, fraction = whole.lstrip("0"), fraction.rstrip("0")
    return whole + fraction if fraction else whole.rstrip("0")


def _id_like(token: str) -> bool:
    """Whether a token of evaluation data names one thing: a code with at least three
    digits and a letter (``PI-SWC024613``), a digit code of five or more digits
    (``0713-196``) or a number of four or more significant digits (``6060.25``); never
    a date, a small or a round number (``2026-01-07``, ``378``, ``12500``)."""
    if _EVAL_DATE.fullmatch(token):
        return False
    if _EVAL_NUMBER.fullmatch(token):
        return len(_significant(token)) >= 4
    digits = sum(char.isdigit() for char in token)
    return digits >= 5 or (digits >= 3 and any(char.isalpha() for char in token))


def _value_pattern(value: str) -> str | None:
    """A number in any thousands grouping, with or without trailing zeros; a code
    case-insensitively, its letter and digit runs joined by at most one separator
    (``PO-00269`` also finds ``po00269``)."""
    if _EVAL_NUMBER.fullmatch(value):
        whole, _, fraction = value.replace(",", "").lstrip("+-").partition(".")
        groups = [whole[max(0, end - 3):end] for end in range(len(whole), 0, -3)][::-1]
        fraction = fraction.rstrip("0")
        tail = rf"\.{fraction}0*" if fraction else r"(?:\.0+)?"
        return rf"(?<![\w.])(?<!\d,){',?'.join(groups)}{tail}(?!\w|\.\d|,\d{{3}}(?!\d))"
    runs = re.findall(r"[A-Za-z]+|\d+", value)
    if not runs:
        return None
    return ("(?i)(?<![A-Za-z0-9])" + "[-_ ./#*]?".join(map(re.escape, runs))
            + "(?![A-Za-z0-9])")


def _harvest(node: object, key: str | None, out: set[str]) -> None:
    if isinstance(node, dict):
        for name, value in node.items():
            _harvest(value, name, out)
    elif isinstance(node, list):
        for value in node:
            _harvest(value, key, out)
    elif key in EVAL_KEYS and isinstance(node, (str, int, float)) \
            and not isinstance(node, bool):
        text = node if isinstance(node, str) else repr(node)
        if key == "identifier" and text.strip():
            out.add(text.strip())
        out.update(token for token in _EVAL_TOKEN.findall(text) if _id_like(token))


def eval_identifiers(documents: list[tuple[str, str]],
                     known: tuple[tuple[str, re.Pattern[str]], ...] = ()
                     ) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """``(eval:<value>, pattern)`` for every identifier the evaluation data names (see
    the module docstring) that no ``known`` pattern already matches. ``documents``:
    ``(repo path, JSON text)`` of the files under ``evals/``."""
    values: set[str] = set()
    for relative, text in documents:
        data = json.loads(text)
        if relative.startswith(EVAL_CASE_DIRS) or (
                isinstance(data, dict) and data.get("format") == EVAL_ITEM_FORMAT):
            _harvest(data, None, values)
    out: list[tuple[str, re.Pattern[str]]] = []
    for value in sorted(values):
        pattern = _value_pattern(value)
        if pattern and not any(seen.search(value) for _, seen in (*known, *out)):
            out.append((f"eval:{value}", re.compile(pattern)))
    return tuple(out)


def tree_eval_documents(root: Path) -> list[tuple[str, str]]:
    return [(path.relative_to(root).as_posix(), path.read_text(encoding="utf-8"))
            for path in sorted((root / "evals").rglob("*.json")) if path.is_file()]


def base_eval_documents(root: Path, base: str | None) -> list[tuple[str, str]]:
    """The ``evals/`` JSON files at ``base`` ([] when the ref does not resolve)."""
    if not base or _git(root, "rev-parse", "--verify", "--quiet",
                        base + "^{commit}").returncode != 0:
        return []
    listed = _git(root, "ls-tree", "-r", "-z", "--name-only", base, "--", "evals")
    names = [name for name in listed.stdout.split("\0") if name.endswith(".json")]
    batch = subprocess.run(["git", "-C", str(root), "cat-file", "--batch"],
                           input="".join(f"{base}:{name}\n" for name in names).encode(),
                           capture_output=True).stdout
    documents, pos = [], 0
    for name in names:
        end = batch.index(b"\n", pos)
        header = batch[pos:end].split()
        pos = end + 1
        if len(header) == 3 and header[1] == b"blob":
            size = int(header[2])
            documents.append((name, batch[pos:pos + size].decode("utf-8")))
            pos += size + 1
    return documents


def _matches(text: str, deny: Denylist) -> list[tuple[str, str]]:
    """(identifier id, shown text) for every denylisted match in ``text``."""
    if text not in deny.matched:
        deny.matched[text] = [] if not deny.any_identifier.search(text) else [
            (case_id, f"{case_id}={found.group(0)!r}")
            for case_id, pattern in deny.identifiers for found in pattern.finditer(text)]
    return deny.matched[text]


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


Finding = tuple[str, int, str, str, str]  # (path, line, rule, entry key, shown)


@functools.lru_cache(maxsize=None)
def _parse(text: str, relative: str) -> ast.Module:
    """One parse per file and run: the identifier and eval-reference scans share it."""
    return ast.parse(text, relative)


def scan_python(source: str, relative: str, deny: Denylist) -> list[Finding]:
    tree = _parse(source, relative)
    skip = _docstrings(tree)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and id(node) not in skip \
                and not isinstance(node.value, bool) \
                and isinstance(node.value, (str, int, float)):
            for key, shown in _matches(str(node.value), deny):
                out.append((relative, node.lineno, "identifier", key, shown))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and any(regex.search(node.name) for regex in deny.name_regexes):
            out.append((relative, node.lineno, "module_name", node.name, node.name))
    return out


class _JsonScanner:
    """Minimal JSON walker that keeps the line of every scalar and key."""

    _NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][-+]?\d+)?")
    _SPACE = re.compile(r"[ \t\n\r]*")

    def __init__(self, text: str):
        self.text = text
        self.scalars: list[tuple[int, str]] = []

    def _ws(self, pos: int) -> int:
        return self._SPACE.match(self.text, pos).end()

    def _line(self, pos: int) -> int:
        return self.text.count("\n", 0, pos) + 1

    def value(self, pos: int, record: bool) -> int:
        pos = self._ws(pos)
        char = self.text[pos]
        if char == "{":
            pos = self._ws(pos + 1)
            if self.text[pos] == "}":
                return pos + 1
            while True:
                key_pos = pos
                key, pos = scanstring(self.text, pos + 1)
                if record:
                    self.scalars.append((self._line(key_pos), key))
                pos = self._ws(pos)
                if self.text[pos] != ":":
                    raise ValueError(f"expected ':' at line {self._line(pos)}")
                pos = self.value(pos + 1, record and key.lower() not in PROVENANCE_KEYS)
                pos = self._ws(pos)
                if self.text[pos] == "}":
                    return pos + 1
                if self.text[pos] != ",":
                    raise ValueError(f"expected ',' at line {self._line(pos)}")
                pos = self._ws(pos + 1)
        if char == "[":
            pos = self._ws(pos + 1)
            if self.text[pos] == "]":
                return pos + 1
            while True:
                pos = self._ws(self.value(pos, record))
                if self.text[pos] == "]":
                    return pos + 1
                if self.text[pos] != ",":
                    raise ValueError(f"expected ',' at line {self._line(pos)}")
                pos += 1
        if char == '"':
            text, end = scanstring(self.text, pos + 1)
        elif number := self._NUMBER.match(self.text, pos):
            text, end = number.group(0), number.end()
        else:
            for literal in ("true", "false", "null"):
                if self.text.startswith(literal, pos):
                    return pos + len(literal)
            raise ValueError(f"bad JSON at line {self._line(pos)}")
        if record:
            self.scalars.append((self._line(pos), text))
        return end


def scan_json(text: str, relative: str, deny: Denylist) -> list[Finding]:
    json.loads(text)  # strict validation; the walker below only tracks lines
    walker = _JsonScanner(text)
    walker.value(0, True)
    return [(relative, line, "identifier", key, shown) for line, scalar in walker.scalars
            for key, shown in _matches(scalar, deny)]


_JS_TOKENS = re.compile(r"//[^\n]*|/\*.*?\*/|'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\""
                        r"|`(?:[^`\\]|\\.)*`", re.S)


def scan_js(text: str, relative: str, deny: Denylist) -> list[Finding]:
    out = []
    for token in _JS_TOKENS.finditer(text):
        if token.group(0).startswith("/"):
            continue  # comment
        base = text.count("\n", 0, token.start()) + 1
        for offset, line in enumerate(token.group(0).split("\n")):
            out.extend((relative, base + offset, "identifier", key, shown)
                       for key, shown in _matches(line, deny))
    return out


def candidate_files(root: Path) -> list[Path]:
    files = [path for top in SCANNED for path in (root / top).rglob("*")
             if path.suffix in (".py", ".json")]
    files += (root / "operator_ui").glob("*.js")
    files += (root / "devloop").glob("*.py")
    keep = []
    for path in sorted(set(files)):
        relative = path.relative_to(root).as_posix()
        parts = Path(relative).parts
        if not path.is_file() or "tests" in parts or "__pycache__" in parts \
                or relative.startswith(EXCLUDED_PREFIXES) or relative == DENYLIST \
                or relative.endswith((".md", ".test.js")):
            continue
        keep.append(path)
    return keep


def scan_tree(root: Path, deny: Denylist) -> list[Finding]:
    found: list[Finding] = []
    for path in candidate_files(root):
        relative = path.relative_to(root).as_posix()
        if any(fnmatch.fnmatch(path.name.lower(), glob) for glob in deny.module_globs):
            found.append((relative, 1, "module_name", path.name, path.name))
        text = path.read_text(encoding="utf-8")
        scanner = {".py": scan_python, ".json": scan_json, ".js": scan_js}[path.suffix]
        found.extend(scanner(text, relative, deny))
    return sorted(set(found))


# ------------------------------------------------------------------ eval references

def _eval_module(name: str) -> bool:
    """Whether module ``name`` is evaluation data, a test package or eval tooling."""
    parts = name.split(".")
    return (parts[0] in _EVAL_PACKAGES or "tests" in parts
            or any(fnmatch.fnmatch("/".join(parts) + ".py", glob) for glob in EVAL_TOOLING))


def _eval_string(value: str) -> bool:
    """An ``evals/``/``tests/`` path, or the dotted name of such a module."""
    return bool(_EVAL_PATH.search(value)) or (
        _MODULE_NAME.fullmatch(value) is not None
        and value.split(".", 1)[0] in SCANNED + _EVAL_PACKAGES and _eval_module(value))


def _path_part(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value in _EVAL_PACKAGES


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


@dataclass
class _Statement:
    """What one top-level statement binds and reaches. ``now``: evaluated when the module
    is imported (not inside a function body), so it can make a bound name derived."""
    bound: set[str]
    direct: list[tuple[int, str, bool]]          # (line, "import m" / "path p", now)
    loads: set[str]                              # names read at import time
    imports: list[tuple[int, str, str, bool]]    # (line, module, name, now): from-imports
    attributes: list[tuple[int, str, str, bool]]  # (line, local dotted base, attribute, now)


class _Module:
    """One product module for the ``eval_reference`` rule, walked once."""

    def __init__(self, relative: str, text: str):
        self.relative = relative
        parts = list(Path(relative).with_suffix("").parts)
        self.package = ".".join(parts[:-1])
        self.name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        tree = _parse(text, relative)
        docstrings = _docstrings(tree)
        self.imported: list[tuple[str, str, bool]] = []  # (local name, dotted, from-import)
        self.statements = [self._statement(node, docstrings) for node in tree.body]
        self.aliases: dict[str, str] = {}

    def resolve_aliases(self, modules: set[str]) -> None:
        """Local name -> the module it names (``import a.b [as x]``, ``from a import b``)."""
        self.aliases = {local: dotted for local, dotted, from_import in self.imported
                        if not from_import or dotted in modules}

    def _target(self, node: ast.ImportFrom) -> str:
        if not node.level:
            return node.module or ""
        base = self.package.split(".") if self.package else []
        base = base[:len(base) - node.level + 1] if node.level > 1 else base
        return ".".join(base + ([node.module] if node.module else []))

    def _statement(self, statement: ast.stmt, docstrings: set[int]) -> _Statement:
        facts = _Statement(set(), [], set(), [], [])
        if isinstance(statement, _SCOPES):
            facts.bound.add(statement.name)
        # (node, binds a top-level name, evaluated at import time)
        stack: list[tuple[ast.AST, bool, bool]] = [(statement, True, True)]
        while stack:
            node, top, now = stack.pop()
            line = getattr(node, "lineno", 0)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    self.imported.append((local, alias.name if alias.asname else local, False))
                    facts.bound.update([local] if top else [])
                    if _eval_module(alias.name):
                        facts.direct.append((line, f"import {alias.name}", now))
            elif isinstance(node, ast.ImportFrom):
                target = self._target(node)
                for alias in node.names:
                    local = alias.asname or alias.name
                    self.imported.append((local, f"{target}.{alias.name}", True))
                    facts.bound.update([local] if top else [])
                    facts.imports.append((line, target, alias.name, now))
                    if not _eval_module(target) and _eval_module(f"{target}.{alias.name}"):
                        facts.direct.append((line, f"import {target}.{alias.name}", now))
                if _eval_module(target):
                    facts.direct.append((line, f"import {target}", now))
            elif isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Store) and top:
                    facts.bound.add(node.id)
                elif isinstance(node.ctx, ast.Load) and now:
                    facts.loads.add(node.id)
            elif isinstance(node, ast.Attribute):
                chain, base = [], node.value
                while isinstance(base, ast.Attribute):
                    chain.append(base.attr)
                    base = base.value
                if isinstance(base, ast.Name):
                    facts.attributes.append(
                        (line, ".".join([base.id, *reversed(chain)]), node.attr, now))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docstrings and _eval_string(node.value):
                facts.direct.append((line, f"path {node.value[:80]}", now))
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                facts.direct += [(part.lineno, f"path {part.value}", now)
                                 for part in (node.left, node.right) if _path_part(part)]
            elif isinstance(node, ast.Call) and (
                    getattr(node.func, "id", None) in _PATH_CALLS
                    or getattr(node.func, "attr", None) in _PATH_CALLS):
                facts.direct += [(part.lineno, f"path {part.value}", now)
                                 for part in node.args if _path_part(part)]
            inner = top and not isinstance(node, _SCOPES)
            # A function's decorators and arguments (defaults) run at import time, its
            # body later.
            at_import = ({id(part) for part in [*getattr(node, "decorator_list", ()),
                                                node.args]}
                         if isinstance(node, _FUNCTIONS) else None)
            stack.extend((child, inner, now and (at_import is None or id(child) in at_import))
                         for child in ast.iter_child_nodes(node))
        return facts

    def references(self, statement: _Statement, tainted: dict[str, set[str]],
                   *, at_import: bool = False) -> list[tuple[int, str]]:
        """(line, ``derived module.name``) where ``statement`` imports or reads a
        top-level name another module derives from an eval reference (``at_import``:
        only where the module's import evaluates it)."""
        found = [(line, f"derived {module}.{name}")
                 for line, module, name, now in statement.imports
                 if name in tainted.get(module, ()) and (now or not at_import)]
        for line, base, attr, now in statement.attributes:
            head, _, rest = base.partition(".")
            if head in self.aliases and (now or not at_import):
                module = self.aliases[head] + (f".{rest}" if rest else "")
                if attr in tainted.get(module, ()):
                    found.append((line, f"derived {module}.{attr}"))
        return found


def eval_reference_files(root: Path) -> list[Path]:
    """Product code for the ``eval_reference`` rule: Python and operator_ui JS of the
    scanned trees, without tests, devloop/ or the eval tooling."""
    return [path for path in candidate_files(root)
            if path.suffix in (".py", ".js")
            and path.relative_to(root).parts[0] != "devloop"
            and not any(fnmatch.fnmatch(path.relative_to(root).as_posix(), glob)
                        for glob in EVAL_TOOLING)]


def scan_eval_references(root: Path) -> list[Finding]:
    found: list[tuple[str, int, str]] = []
    modules: dict[str, _Module] = {}
    for path in eval_reference_files(root):
        relative = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".py":
            module = _Module(relative, text)
            modules[module.name] = module
            continue
        for token in _JS_TOKENS.finditer(text):
            value = token.group(0)[1:-1]  # a string's text; a comment starts with "/"
            if not token.group(0).startswith("/") and _EVAL_PATH.search(value):
                found.append((relative, text.count("\n", 0, token.start()) + 1,
                              f"path {value[:80]}"))
    for module in modules.values():
        module.resolve_aliases(set(modules))
    tainted: dict[str, set[str]] = {name: set() for name in modules}
    changed = True
    while changed:  # a top-level name built from a derived name is derived too
        changed = False
        for name, module in modules.items():
            for statement in module.statements:
                new = statement.bound - tainted[name]
                if new and (any(now for _, _, now in statement.direct)
                            or statement.loads & tainted[name]
                            or module.references(statement, tainted, at_import=True)):
                    tainted[name] |= new
                    changed = True
    for module in modules.values():
        for statement in module.statements:
            found += [(module.relative, line, match) for line, match, _ in statement.direct]
            found += [(module.relative, line, match)
                      for line, match in module.references(statement, tainted)]
    return [(path, line, EVAL_REFERENCE, match, match)
            for path, line, match in sorted(set(found))]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def base_denylist_text(root: Path, base: str | None) -> str | None:
    """The denylist JSON at ``base``, or None when the ref or the file is missing."""
    if not base or _git(root, "rev-parse", "--verify", "--quiet",
                        base + "^{commit}").returncode != 0:
        return None
    shown = _git(root, "show", f"{base}:{DENYLIST}")
    return shown.stdout if shown.returncode == 0 else None


def changed_files(root: Path, base: str) -> set[str] | None:
    """Repo paths the candidate changes relative to merge-base(base, HEAD), working
    tree and untracked files included; None when git cannot say."""
    merge_base = _git(root, "merge-base", base, "HEAD")
    if merge_base.returncode != 0:
        return None
    diff = _git(root, "diff", "--no-renames", "--name-only", "-z",
                merge_base.stdout.strip())
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    if diff.returncode != 0 or untracked.returncode != 0:
        return None
    return {p for p in (diff.stdout + untracked.stdout).split("\0") if p}


def _key(entry: dict) -> tuple[str, str, str | None]:
    return entry["path"], entry["rule"], entry.get("match")


def _effective_allowlist(candidate: tuple[dict, ...], base: tuple[dict, ...]
                         ) -> tuple[list[dict], list[str]]:
    """(entries that count, refusal lines). See the module docstring for the rule."""
    kept, refused = [], []
    for entry in candidate:
        path, rule, match = _key(entry)
        covering = [b for b in base if b["path"] == path and b["rule"] == rule
                    and b.get("match") in (None, match)]
        label = f"{path} {rule} {match}"
        if not covering:
            refused.append(f"no-hardcoded-cases: allowlist refused (candidate adds an "
                           f"entry the base does not have): {label}")
        elif entry["expires"] > max(b["expires"] for b in covering):
            refused.append(f"no-hardcoded-cases: allowlist refused (candidate extends "
                           f"expiry to {entry['expires']} past the base's "
                           f"{max(b['expires'] for b in covering)}): {label}")
        else:
            kept.append(entry)
    return kept, refused


def _effective_ratchet(candidate: Denylist | None, base: Denylist | None
                       ) -> tuple[frozenset[Fingerprint], list[str]]:
    """(entries that count, refusal lines): the candidate's entries the base lists too;
    the candidate's alone while the base has no ratchet (bootstrap)."""
    entries = (candidate.ratchet if candidate else None) or frozenset()
    if base is None or base.ratchet is None:
        return entries, []
    refused = [f"no-hardcoded-cases: ratchet refused (candidate adds an entry the base "
               f"does not have): {path} {rule} {match} #{occurrence}"
               for path, rule, match, occurrence in sorted(entries - base.ratchet)]
    return entries & base.ratchet, refused


def evaluate(root: Path, today: dt.date | None = None, base: str | None = "origin/main",
             strict_expiry: bool = False) -> tuple[int, list[str]]:
    """(exit code, report lines) for the tree at ``root`` on ``today`` (UTC), judged
    against the denylist at ``base``."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    candidate_path = root / DENYLIST
    candidate = load_denylist(root) if candidate_path.is_file() else None
    base_text = base_denylist_text(root, base)
    lines: list[str] = []
    if base_text is None:
        if candidate is None:
            raise ValueError(f"{DENYLIST}: missing in the tree and at base {base}")
        lines.append(f"no-hardcoded-cases: bootstrap: no denylist at base {base}; "
                     "using the candidate's and failing every expired entry")
        deny, entries, refused, changed = candidate, list(candidate.allowlist), [], None
        ratchet, ratchet_refused = _effective_ratchet(candidate, None)
    else:
        base_deny = parse_denylist(base_text, require_match=False)
        deny = _union(base_deny, candidate)
        entries, refused = _effective_allowlist(
            candidate.allowlist if candidate else (), base_deny.allowlist)
        ratchet, ratchet_refused = _effective_ratchet(candidate, base_deny)
        changed = None if strict_expiry else changed_files(root, base)
        if changed is None and not strict_expiry:
            lines.append("no-hardcoded-cases: changed files unknown; every expired "
                         "entry is enforced")
    derived = eval_identifiers(tree_eval_documents(root) + base_eval_documents(root, base),
                               deny.identifiers)
    deny = replace(deny, identifiers=deny.identifiers + derived)
    found = scan_tree(root, deny) + scan_eval_references(root)
    lines.extend(refused + ratchet_refused)
    failures, used, matched = len(refused) + len(ratchet_refused), set(), set()
    counts = {rule: [0, 0] for rule in (*RULES, EVAL_REFERENCE)}  # [total, allowlisted]
    seen: dict[tuple[str, str, str], int] = {}  # occurrences so far, in line order
    for path, line, rule, key, shown in sorted(found):
        counts[rule][0] += 1
        occurrence = seen[path, rule, key] = seen.get((path, rule, key), -1) + 1
        hit = next((e for e in entries if e["path"] == path and e["rule"] == rule
                    and e.get("match") in (None, key)), None)
        if hit is not None:
            counts[rule][1] += 1
            used.add(_key(hit))
        elif (path, rule, key, occurrence) in ratchet:
            matched.add((path, rule, key, occurrence))
        else:
            failures += 1
            lines.append(f"no-hardcoded-cases: {path}:{line} {rule} {shown}")
    for path, rule, match, occurrence in sorted(ratchet - matched):
        failures += 1
        lines.append(f"no-hardcoded-cases: ratchet stale (no such finding any more; "
                     f"remove the entry): {path} {rule} {match} #{occurrence}")
    for entry in sorted(entries, key=lambda e: tuple(map(str, _key(e)))):
        label = f"{entry['path']} {entry['rule']} {entry.get('match')}"
        if dt.date.fromisoformat(entry["expires"]) < today:
            if changed is None or entry["path"] in changed:
                failures += 1
                lines.append(f"no-hardcoded-cases: allowlist expired: {label} "
                             f"({entry['reason']}) — delete or generalise")
            else:
                lines.append(f"no-hardcoded-cases: allowlist expired, not enforced "
                             f"(file unchanged since base): {label} ({entry['reason']})")
        elif _key(entry) not in used:
            lines.append(f"no-hardcoded-cases: allowlist unused (delete it): {label}")
    soonest = min((entry["expires"] for entry in entries), default="none")
    allowlisted = sum(allowed_count for _, allowed_count in counts.values())
    lines.append("no-hardcoded-cases: " + " ".join(
        f"{rule}={total}(allowlisted {listed})" for rule, (total, listed) in counts.items())
        + f" eval_identifiers={len(derived)} ratcheted={len(matched)} violations={failures}"
        + f" allowlisted={allowlisted} expiring_soonest={soonest}"
        + f" base={base if base_text is not None else 'none'}")
    return (1 if failures else 0), lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", default=str(ROOT))
    parser.add_argument("--base", default="origin/main",
                        help="authoritative ref for scanner data and allowlist")
    parser.add_argument("--strict-expiry", action="store_true",
                        help="fail every expired entry (daily audit)")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    code, lines = evaluate(Path(args.root).resolve(), base=args.base,
                           strict_expiry=args.strict_expiry)
    print("\n".join(lines))
    print("no-hardcoded-cases: " + ("FAIL" if code else "clean"))
    return code


if __name__ == "__main__":
    sys.exit(main())
