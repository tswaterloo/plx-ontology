#!/usr/bin/env python3
"""Print the test modules to run for a change: tests of the changed modules and of everything that imports them.

USAGE
    python3 tools/affected_tests.py [--base SHA] [--root DIR] [FILE ...]
    python3 -m unittest $(python3 tools/affected_tests.py --base origin/main)

FILE names changed files; ``--base`` adds every file that differs from that commit in the
working tree (committed, uncommitted and untracked). The tests are found in the import graph of
``tools/import_graph.py`` (the one behind ``scripts/gen_codemap.py``): a changed module's
importers count transitively, a changed package ``__init__`` counts for its whole package,
and a changed test module is included directly. A test that runs a document's commands
(``COMMAND_DOCS``) also counts what those commands run (``python3 -m pkg.mod``,
``python3 -m unittest pkg.tests.test_x``, ``python3 path.py``) as imported. A change other
than a ``.md`` also selects every test whose impact the graph cannot bound (it, or a module
it reaches, makes a computed import the graph cannot resolve: ``import_graph`` marks it
``dynamic``), so unknown impact errs toward the safe superset. Output is one dotted module
path per line, sorted, for ``python3 -m unittest``.

A change other than a ``.md`` also selects the repository property tests, the modules of
the gate spec's ``repo-properties`` check (``property_tests``; a spec that does not parse
prints ``ALL``): they read the whole code tree, so no edge links a change to them. Any
change also selects a whole-tree text scan (``TEXT_SCANS``) when a changed path starts with
one of its tokens or a changed file holds one (a ``.py`` naming ``ApproverRoster`` the roster scan).
A changed non-test ``.py`` module is also data to the tests that read its source: a test
module or test helper whose strings name its repository path or dotted module name
(``source_readers``, through ``data_readers``; a package ``__init__`` by path only) counts
as a reader, and ``data_observers`` selects the tests observing that read.

A ``.md`` file selects only the tests that read it: test modules whose source names its
file name (``MD_READERS`` adds directory-wide readers), and the observers of a non-test
module reading it as data (``prose_readers``: a string naming its path or a run of names
ending at its file; a directory naming many documents reads none of them). A record named by a
data file nothing reads prints ``ALL``. Any other ``.md`` selects nothing.
Any other file that is not a ``.py`` module (a JSON schema or contract, a live-check spec,
a fixture, a script) is data, read by a module when one of its strings (``import_graph``'s
``strings``) names it: its path, its file name, a run of directory names ending at it or at
its directory (``"orders.json"``, ``"schema/domains/orders.json"``, ``"live-checks"``), or
a directory further up while the module walks directory trees (``walks``); a module
``NAME_ONLY_READERS`` declares as using only the names of the files under a directory is
not a reader of a file there. It selects the
tests that observe the read through code (``data_observers``). A non-test module reading it
or importing a module that observes it observes it whole; so does a module whose
import-time code reads it or calls what does, and then every importer too. In a test module
or test helper only the top-level names (a class, a function, a constant; a statement in a
top-level ``try``, ``if`` or ``with`` block is top-level) that read it or use, by name, what
does count: a test inheriting a reading test case, calling a reading helper
(``support._read()``) or importing a reading name is selected, one importing only another
test's fixtures is not; a decorator registering a reading helper hands it to the registry it
stores it in. So the live verifier reading every live-check spec selects about 70
test modules, not the ~110 importing it. A use the walk cannot follow (a ``*`` import, a
dependency the module's imports and strings do not account for) counts as the whole module.
A data file no module reads prints ``ALL`` (run every test module), and so does every other
change the graph cannot map: a ``.py`` path that is not in the graph (a deleted module, the old name of a renamed one, a skipped directory),
``tools/gate-checks.json`` (the integration floor itself) and the selector's own files
(a changed selector cannot judge its own change).
With ``--base`` a changed ``.py`` module is judged by the top-level names its diff touches,
not as a whole (``tools/symbol_impact.py``: a changed function, assignment or class reaches
the units referring to it, transitively; a method reaches its class's users), and a data
file or document a non-test module reads selects through the units holding the string that
names it (a directory name joined to a constant naming another file names none of it).
Import-time code, a module the parser cannot read and a module with no base text fall back
to the whole module; stderr says, per module, which way it was judged (``--explain`` also
says what reached each selected test; ``--whole-modules`` turns the narrowing off). A file
named on the command line is judged whole (the caller says it changed), with or without
``--base``.
``--untested`` prints the changed
non-test ``.py`` modules that no test reaches: a builder adds a test that imports each
before reporting. Run the selection wrapped in ``tools/testslot.py``; ``main-ci`` runs the
whole suite on ``main`` daily.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import functools
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from tools import import_graph, symbol_impact  # noqa: E402

SELECTOR = ("tools/affected_tests.py", "tools/import_graph.py", "tools/symbol_impact.py")
GATE_SPEC = "tools/gate-checks.json"
# Tests that read a whole directory of ``.md`` files, not one named file.
MD_READERS = {"devloop/CONTEXT/": ("ontology.tests.test_domain_orders_purchasing",),
}
# Tests that run the indented commands of a document.
COMMAND_DOCS = {"tests.test_try_it": "devloop/TRY-IT.md"}
# Records product code reads as data (a ``status: decided`` check, a scope list): a record
# whose reader the selector cannot resolve selects every test.
RECORDS = "devloop/DECISIONS/"
# Modules that use only the names of files under a directory, never a file's content or the
# working tree's copy: no change to a file there changes what they do in a test, so such a
# change does not count them as its reader. This selector names the directory and its own
# document tables here (the documents a table names select their readers, not the selector's tests).
NAME_ONLY_READERS = {"tools.affected_tests": ("devloop/live-checks/", *MD_READERS,
                                                *COMMAND_DOCS.values(), RECORDS)}
# Tests that scan the whole tree's text, so no edge links a change to them: each runs when a
# changed path starts with one of its tokens, or a changed file with one of its suffixes (any
# file for none) holds one. The citation scan reads every
# tracked file's record citations (main red d7e7fa775c29); the roster scan parses every .py
# for an ``ApproverRoster`` built by hand and can only fail on a file naming these tokens.
TEXT_SCANS = {"tools.tests.test_decision_citations": ((), (RECORDS,)),
              "operating.tests.test_goal2_e2e": ((".py",), ("ApproverRoster", "call_approvers",
                                                            "approver_roster"))}
# Repository property tests are the modules of this gate check: they read the whole code
# tree (every module, the gate spec, retired names), so no import or data edge links a
# change to them and every change other than a ``.md`` (prose they never read) selects them.
# Direction review of
# 0d273da28 (M3): the gate spec is the one list.
PROPERTY_CHECK = "repo-properties"

def md_readers(root: Path, graph: dict, markdown: set[str]) -> set[str]:
    """Test modules that read a changed ``.md``: their source names its file name, or a
    ``MD_READERS`` prefix covers it."""
    names = {Path(path).name for path in markdown}
    readers = {name for prefix, modules in MD_READERS.items() for name in modules
               if name in graph and any(path.startswith(prefix) for path in markdown)}
    for module in graph.values():
        if import_graph.is_unittest_module(module.name):
            text = (root / module.path).read_text("utf-8", errors="replace")
            if any(name in text for name in names):
                readers.add(module.name)
    return readers


def property_tests(root: Path, graph: dict) -> set[str] | None:
    """The modules of the gate spec's ``PROPERTY_CHECK`` that are in the graph; nothing for a
    tree without a gate spec or a spec without that check (a fixture repository; this
    repository's check is pinned by its tests), None for a spec that does not parse or a
    property check without a list of names (unknown: run every test)."""
    path = root / GATE_SPEC
    if not path.is_file():
        return set()
    try:
        checks = json.loads(path.read_text("utf-8"))["checks"]
        if not isinstance(checks, list):
            return None
        modules = [check.get("modules") for check in checks if check.get("name") == PROPERTY_CHECK]
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if not modules:
        return set()
    if len(modules) != 1 or not isinstance(modules[0], list) \
            or not all(isinstance(name, str) for name in modules[0]):
        return None
    return {name for name in modules[0] if name in graph}


def text_scans(root: Path, graph: dict, changed: set[str]) -> set[str]:
    """The ``TEXT_SCANS`` tests a change reaches: a changed path starts with a token, or a
    changed file of the scan's suffixes holds one."""
    found = set()
    for test, (suffixes, tokens) in TEXT_SCANS.items():
        if test not in graph:
            continue
        for path in sorted(changed):
            if path.startswith(tokens):
                found.add(test)
                break
            if suffixes and not path.endswith(suffixes):
                continue
            try:
                text = (root / path).read_text("utf-8", errors="replace")
            except OSError:  # deleted: what it held left with it
                continue
            if any(token in text for token in tokens):
                found.add(test)
                break
    return found


def _data_files(root: Path) -> list[str]:
    """Every file of the walked tree other than a ``.py`` module or a ``.md`` document."""
    found = []
    for directory, dirs, files in os.walk(root):
        here = Path(directory)
        dirs[:] = sorted(d for d in dirs if d not in import_graph.SKIP_DIRS and not d.startswith(".")
                         and not (here / d / ".git").exists())
        found.extend((here / f).relative_to(root).as_posix() for f in files
                     if not f.endswith((".py", ".md")) and not f.startswith("."))
    return sorted(found)


def _ending_at(path: str, runs: set[str]) -> set[str]:
    """The runs that name ``path`` itself: the path, or a run of names ending at its file."""
    return {run for run in runs if path == run or path.endswith("/" + run)}


def _resolves_records(text: str) -> bool:
    """``text`` names the ``RECORDS`` directory or a pattern of records under it, not one."""
    key = _key(text).lstrip("^")
    return key == RECORDS.rstrip("/") or key.startswith(RECORDS) and not key.endswith(".md")


def prose_readers(root: Path, graph: dict, markdown: set[str]) -> dict[str, set[str]] | None:
    """Each non-test module reading a changed ``.md`` as data mapped to the names it reads by:
    a string naming its path, or a run of names ending at its file name (a directory naming
    many documents is no reader of one). A ``RECORDS`` record is also read by a reader of a
    data file whose text names its path when that reader resolves record paths (names the
    directory or a pattern under it): ``ontology/source_scope.json`` lists the records
    ``source_scope`` checks, while a data file naming a document as provenance is read by
    modules that never open it, so other documents take no such hop. None when a record is
    named by a data file nothing reads: its impact is unknown."""
    readers: dict[str, set[str]] = {}

    def add(found: dict[str, set[str]]) -> None:
        for name, runs in found.items():
            if not graph[name].is_test:
                readers.setdefault(name, set()).update(runs)

    data = None
    for path in sorted(markdown):
        names_only = {name for name, dirs in NAME_ONLY_READERS.items() if path.startswith(dirs)}
        found = data_readers(graph, {path}, partial=True) or {}
        add({name: _ending_at(path, runs) for name, runs in found.items()})
        if not path.startswith(RECORDS):
            continue
        if data is None:
            data = {}
            for name in _data_files(root):
                try:
                    data[name] = (root / name).read_text("utf-8", errors="replace")
                except OSError:
                    continue
        for name, text in data.items():
            if path not in text:
                continue
            proxy = data_readers(graph, {name})
            if proxy is None:
                return None
            add({reader: ends for reader, runs in proxy.items() if reader not in names_only
                 and any(_resolves_records(string) for string in graph[reader].strings)
                 and (ends := _ending_at(name, runs))})
    return {name: runs for name, runs in readers.items() if runs}


def _key(text: str) -> str:
    return text.removeprefix("./").strip("/")


def data_readers(graph: dict, files: set[str], *, partial: bool = False
                 ) -> dict[str, set[str]] | None:
    """Each module reading a changed data file mapped to the names it reads it by (its path,
    file name or a run of directory names, see the module docstring), or None when one of
    the files has no reader (``partial``: that file adds nothing)."""
    exact: dict[str, set[str]] = {}
    for module in graph.values():
        for text in module.strings:
            exact.setdefault(_key(text), set()).add(module.name)
    readers: dict[str, set[str]] = {}
    for path in files:
        names_only = {name for name, dirs in NAME_ONLY_READERS.items()
                      if path.startswith(dirs)}
        parts = path.split("/")
        found = False
        for start in range(len(parts)):
            for end in range(start + 1, len(parts) + 1):
                run = "/".join(parts[start:end])
                named = exact.get(run, set())
                if end < len(parts) - 1:
                    named = {name for name in named if graph[name].walks}
                named = named - names_only
                for name in named:
                    readers.setdefault(name, set()).add(run)
                found = found or bool(named)
        if not found and not partial:
            return None
    return readers


def source_readers(graph: dict, python: set[str]) -> dict[str, set[str]]:
    """Each test module or test helper whose strings name a changed non-test ``.py`` module
    by its repository path or dotted module name (a test reading source with ``open`` or
    ``ast``: no import edge links it) mapped to those names, found by ``data_readers``. A
    package ``__init__`` counts by path only: its dotted name is a bare directory name."""
    by_path = {module.path: module.name for module in graph.values()}
    names = {text for path in python if not graph[by_path[path]].is_test
             for text in (path, by_path[path]) if text == path or (
                 "." in text and not path.endswith("__init__.py"))}
    return {module: runs & names for module, runs in
            (data_readers(graph, names, partial=True) or {}).items()
            if graph[module].is_test and runs & names}


BLOCKS = ("body", "orelse", "finalbody", "handlers", "cases")
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True)
class _Statement:
    """A top-level statement: the names it binds, the dotted names (``m.f``) and strings it
    uses, the name it imports (an import binds one name per statement), the dotted names and
    strings its import-time code uses in a call, whether it is a ``def`` and the dotted names
    its decorators use (a decorator is given what it decorates)."""
    bound: frozenset[str]
    chains: frozenset[str]
    texts: frozenset[str]
    imported: frozenset[str]
    eager_chains: frozenset[str]
    eager_texts: frozenset[str]
    is_def: bool = False
    decorators: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _Uses:
    statements: tuple[_Statement, ...]
    bindings: dict[str, tuple[str, ...]]  # a name an import binds -> the dotted names it stands for
    stars: tuple[str, ...]  # modules imported with ``*``
    imports: frozenset[str]  # every dotted name an import names
    texts: frozenset[str]  # every string constant


def _chain(node: ast.AST) -> str | None:
    """``a.b.c`` for a name or an attribute chain on a name, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return ".".join([node.id, *reversed(parts)]) if isinstance(node, ast.Name) else None


def _refs(nodes: list[ast.AST], import_time: bool = False) -> tuple[set[str], set[str], bool]:
    """The longest dotted names and the strings ``nodes`` use, and whether they call
    something; ``import_time`` skips what import does not run (function and lambda bodies,
    ``if __name__ == "__main__":`` blocks)."""
    chains: set[str] = set()
    texts: set[str] = set()
    called = False
    stack = list(nodes)
    while stack:
        node = stack.pop()
        if import_time and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = node.args
            stack.extend((*getattr(node, "decorator_list", ()), *args.defaults,
                          *(d for d in args.kw_defaults if d)))
            continue
        if import_time and _main_guard(node):
            stack.extend(node.orelse)
            continue
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            continue  # a target binds, it uses nothing
        chain = _chain(node)
        if chain:
            chains.add(chain)
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            texts.add(node.value)
        called = called or isinstance(node, ast.Call)
        stack.extend(ast.iter_child_nodes(node))
    return chains, texts, called


def _main_guard(node: ast.AST) -> bool:
    return isinstance(node, ast.If) and isinstance(node.test, ast.Compare) \
        and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"


def _groups(body: list[ast.stmt], whole: tuple[type, ...] = DEFS):
    """The import-time code of ``body`` one simple statement (or compound header) at a time;
    a ``whole`` statement (a ``def``, by default) is one."""
    for statement in body:
        if _main_guard(statement):
            yield from _groups(statement.orelse, whole)
            continue
        blocks = [value for field, value in ast.iter_fields(statement) if field in BLOCKS]
        if not blocks or isinstance(statement, whole):
            yield [statement]
            continue
        yield [node for field, value in ast.iter_fields(statement) if field not in BLOCKS
               for node in (value if isinstance(value, list) else [value]) if isinstance(node, ast.AST)]
        for block in blocks:
            for item in block:
                yield from _groups(item.body if isinstance(item, (ast.ExceptHandler, ast.match_case))
                                   else [item], whole)


def _uses(root: Path, module) -> _Uses:
    return _parsed_uses((root / module.path).read_text("utf-8", errors="replace"), module.name,
                        module.path.endswith("__init__.py"))


@functools.lru_cache(maxsize=4096)
def _parsed_uses(text: str, name: str, is_package: bool) -> _Uses:
    """``_uses`` of a module's source, kept for the process: a selection parses each once."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = ast.Module(body=[], type_ignores=[])
    package = name if is_package else name.rpartition(".")[0]
    bindings: dict[str, list[str]] = {}
    stars: list[str] = []
    imports: set[str] = set()
    bound_by: dict[int, set[str]] = {}  # import node -> the names it binds
    texts = set()
    for node in import_graph._nodes(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            texts.add(node.value)
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        base = None if isinstance(node, ast.Import) else import_graph._absolute(node, package)
        for alias in node.names:
            if isinstance(node, ast.Import):
                name = alias.asname or alias.name.partition(".")[0]
                target = alias.name if alias.asname else name
                imports.add(alias.name)
            elif base is None:
                continue
            elif alias.name == "*":
                stars.append(base)
                continue
            else:
                name, target = alias.asname or alias.name, f"{base}.{alias.name}"
                imports.add(target)
            bindings.setdefault(name, []).append(target)
            bound_by.setdefault(id(node), set()).add(name)
    statements = []
    # A statement in a top-level ``try``, ``if``, ``with`` or loop block is a top-level one
    # (its header one more); ``if __name__ == "__main__":`` runs as a script only.
    for group in _groups(tree.body, (*DEFS, ast.ClassDef)):
        statement = group[0] if len(group) == 1 and isinstance(group[0], ast.stmt) else None
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            # One per name: importing a reading name binds no other name of the statement.
            statements.extend(_Statement(frozenset([name]), frozenset(), frozenset(), frozenset([name]),
                                         frozenset(), frozenset())
                              for name in sorted(bound_by.get(id(statement), ())))
            continue
        if not group:
            continue
        chains, strings, _ = _refs(group)
        decorators: set[str] = set()
        if isinstance(statement, (*DEFS, ast.ClassDef)):
            bound = {statement.name}
            decorators = {chain for node in statement.decorator_list for chain in _refs([node])[0]}
        else:  # what it stores, and what it stores into (``X[k] = v``)
            bound = set()
            for n in (n for node in group for n in ast.walk(node)):
                if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
                    bound.add(n.id)
                elif isinstance(n, (ast.Subscript, ast.Attribute)) and not isinstance(n.ctx, ast.Load):
                    bound.add((_chain(n.value) or "").partition(".")[0])
            bound.discard("")
        eager_chains: set[str] = set()
        eager_texts: set[str] = set()
        for part in _groups([statement]) if statement else [group]:
            part_chains, part_texts, called = _refs(part, import_time=True)
            if called:
                eager_chains |= part_chains
                eager_texts |= part_texts
        statements.append(_Statement(frozenset(bound), frozenset(chains), frozenset(strings),
                                     frozenset(), frozenset(eager_chains), frozenset(eager_texts),
                                     isinstance(statement, DEFS), frozenset(decorators)))
    return _Uses(tuple(statements), {k: tuple(v) for k, v in bindings.items()}, tuple(stars),
                 frozenset(imports), frozenset(texts))


def data_observers(root: Path, graph: dict, readers: dict[str, set[str]]) -> set[str]:
    """The modules that observe a changed data file through code: a non-test module reading
    it or importing a module that observes it (all of it), a module whose import-time code
    reads it or imports such a module (all of it, and every importer), and the names of a
    test module or helper (``tests/support.py``) that read it or use, by name, what does
    (a class inheriting a reading test, a call of a reading helper, ``m._read()``). A
    decorator given a reading definition holds it: the decorator's name unless a ``def``
    binds it, and what that ``def`` uses at module level, here or in the module it comes from
    (a non-test module whole). A test observes the file when it is a reader or one of its
    names does. A use the walk cannot follow (a ``*`` import, a computed import) counts as
    all of the used module."""
    reverse = import_graph.dependents(graph)
    deps = import_graph.dependencies(graph)
    lookup = import_graph._Lookup({module.name: module.path for module in graph.values()})
    uses: dict[str, _Uses] = {}
    eager: set[str] = set()  # importing it reads the file
    whole: set[str] = set()  # every name of it reads the file
    named: dict[str, set[str]] = {}  # the names of a test module or helper that read the file
    fed: dict[str, set[str]] = {}  # names a decorator elsewhere gave a reading definition

    def pairs(dotted: str) -> list[tuple[str, str | None]]:
        """(module, the name used from it) for each module ``dotted`` names or sits under."""
        parts = dotted.split(".")
        found = [(".".join(parts[:end]), parts[end] if end < len(parts) else None)
                 for end in range(1, len(parts) + 1) if ".".join(parts[:end]) in graph]
        return found or [(name, parts[1] if len(parts) > 1 else None) for name in lookup.bare(dotted)]

    def reaches(module: str, name: str | None, bare: bool = True) -> bool:
        return module in whole or (name in named.get(module, ()) if name else
                                   bare and bool(named.get(module)))

    def owners(dotted: str) -> list[tuple[str, str | None]]:
        """The module ``dotted`` names or sits under (the deepest, not its packages) and the
        name used from it."""
        found = pairs(dotted)
        return [(module, attribute) for module, attribute in found
                if not any(other.startswith(module + ".") for other, _ in found)]

    def observed(name: str) -> tuple[bool, bool, set[str], set[tuple[str, str | None]]]:
        """Whether importing ``name`` reads the file, whether all of it does, which of its
        names do and what a decorator of it in another module is given (module, name), from
        what the walk knows so far."""
        if deps[name] & eager:
            return True, True, set(), set()
        if name not in uses:
            uses[name] = _uses(root, graph[name])
        use = uses[name]
        runs = readers.get(name, set())
        local: set[str] = set()
        given: set[tuple[str, str | None]] = set()
        received: set[str] = set()
        top = {bound for statement in use.statements for bound in statement.bound}

        def receive(chain: str) -> None:
            """A decorator given a reading definition: what it names holds the definition."""
            first, _, rest = chain.partition(".")
            if first in use.bindings:
                given.update(pair for target in use.bindings[first]
                             for pair in owners(".".join(filter(None, (target, rest)))))
            elif first in top and first not in received:
                received.add(first)
                for statement in use.statements:
                    if first in statement.bound:
                        if not statement.is_def:
                            local.add(first)
                        for used in statement.chains:
                            receive(used)

        def chain_reaches(chain: str, bare: bool = True) -> bool:
            first, _, rest = chain.partition(".")
            return first in local or any(
                reaches(module, attribute, bare)
                for target in use.bindings.get(first, ())
                for module, attribute in pairs(".".join(filter(None, (target, rest)))))

        def text_reaches(text: str) -> bool:
            """The text names the file, or a module it links (to run it, not an allowlist)."""
            return _key(text) in runs or any(reaches(module, None) for module in lookup.string(text)
                                             if module in deps[name])

        def statement_reaches(statement: _Statement) -> bool:
            return any(chain_reaches(chain) for chain in statement.chains) \
                or any(chain_reaches(chain, bare=False) for chain in statement.imported) \
                or any(text_reaches(text) for text in statement.texts)

        for given_name in sorted(fed.get(name, ())):
            receive(given_name)
        pending = list(use.statements)
        while True:
            hit = [statement for statement in pending if statement_reaches(statement)]
            if not hit:
                break
            for statement in hit:
                local |= statement.bound
                pending.remove(statement)
                for chain in sorted(statement.decorators):
                    receive(chain)
        # Import-time code calling what reads, or reading the file itself (naming a module
        # there only builds a path or a command line).
        if any(chain_reaches(chain) for statement in use.statements
               for chain in statement.eager_chains) or any(
                _key(text) in runs for statement in use.statements for text in statement.eager_texts):
            return True, True, local, given
        if not graph[name].is_test:
            return False, bool(runs) or any(dep in whole or named.get(dep) for dep in deps[name]), \
                local, given
        # A read no statement holds, or a dependency no import or string accounts for, is
        # used unseen.
        explained = {name.rpartition(".")[0]} | set(use.stars) | {
            module for target in use.imports for module, _ in pairs(target)} | {
            module for text in use.texts for module in lookup.string(text)}
        unseen = any(dep in whole or named.get(dep) for dep in deps[name]
                     if not any(dep == known or known.startswith(dep + ".") for known in explained))
        return False, unseen or bool(runs and not local) or any(
            module in whole or named.get(module) for module in use.stars), local, given

    queue = sorted(readers)
    while queue:
        name = queue.pop()
        is_eager, is_whole, local, given = observed(name)
        for module, attribute in sorted(given, key=str):
            if module in whole:
                continue
            if attribute is None or not graph[module].is_test:
                whole.add(module)
                queue.extend(reverse[module])
            elif attribute not in fed.setdefault(module, set()):
                fed[module].add(attribute)
                queue.append(module)
        grew = (is_eager and name not in eager) or (is_whole and name not in whole) \
            or not local <= named.get(name, set())
        if not grew:
            continue
        if is_eager:
            eager.add(name)
        if is_whole:
            whole.add(name)
        named.setdefault(name, set()).update(local)
        queue.extend(importer for importer in reverse[name])
    return whole | {name for name, names in named.items() if names}


def command_modules(root: Path, graph: dict, doc: str) -> set[str]:
    """The modules a document's commands run: ``-m pkg.mod``, every module named after
    ``-m unittest``, and ``.py`` paths."""
    by_path = {module.path: module.name for module in graph.values()}
    try:
        text = (root / doc).read_text("utf-8", errors="replace")
    except OSError:
        return set()
    found = set()
    for line in text.splitlines():
        tokens = re.findall(r"[^\s'\"|;&()]+", line)
        for index, token in enumerate(tokens):
            if token in by_path:
                found.add(by_path[token])
            elif token == "-m" and index + 1 < len(tokens):
                rest = tokens[index + 1:]
                found.update(name for name in (rest if rest[0] == "unittest" else rest[:1])
                             if name in graph)
    return found


def dynamic_tests(graph: dict) -> set[str]:
    """Test modules whose impact is unknown: they, or a module they reach, make a computed
    import the graph cannot resolve, so any change may affect them."""
    dynamic = {module.name for module in graph.values() if module.dynamic}
    return {name for name in import_graph.affected(graph, dynamic)
            if import_graph.is_unittest_module(name)}


def untested(root: Path, changed: set[str]) -> list[str]:
    """Changed non-test ``.py`` modules that no test module reaches."""
    graph = import_graph.build(root)
    by_path = {module.path: module.name for module in graph.values()}
    return sorted(path for path in changed if path in by_path and not any(
        import_graph.is_unittest_module(name)
        for name in import_graph.affected(graph, {by_path[path]})))


def symbol_hit(root: Path, graph: dict, base: str, python: set[str], notes: list[str],
               why: dict[str, str] | None = None, readers: dict[str, set[str]] | None = None,
               files: set[str] = frozenset(), named: set[str] = frozenset()) -> set[str]:
    """The modules a change to ``python`` reaches by the top-level names it touches
    (``symbol_impact``), and a change to the data or documents ``readers`` (non-test modules)
    read by the names of the units holding a string that names them (a directory name joined
    to a constant naming another file, ``ROOT / "operating" / "other.json"``, names none of
    ``files``); a module whose change cannot be split so falls back whole and ``notes`` says
    why."""
    by_path = {module.path: module.name for module in graph.values()}
    changed: dict[str, set[str] | None] = {}
    for name, runs in sorted((readers or {}).items()):
        parts, matched = _reading_units(root, graph, name, runs, files)
        if matched and not any(matched.values()):
            notes.append(f"{graph[name].path} names only other files beside a changed one: not a reader")
            continue
        keys = {key for key, names in matched.items() if names}
        whole = not keys or any(parts[key].whole for key in keys)
        changed[name] = None if whole else keys
        notes.append(f"{graph[name].path} reads a changed file: "
                     + ("whole module" if whole else f"by name ({', '.join(sorted(keys))})"))
    for path in sorted(python & set(named)):  # the caller says it changed: no base to judge by
        changed[by_path[path]] = None
        notes.append(f"{path}: whole module (named on the command line)")
    for path in sorted(python - set(named)):
        result = subprocess.run(["git", "-C", str(root), "show", f"{base}:{path}"],
                                capture_output=True, timeout=60)
        old = result.stdout.decode("utf-8", "replace") if not result.returncode else ""
        keys, reason = symbol_impact.changed_units(by_path[path], old, (root / path).read_text("utf-8"),
                                                   path.endswith("__init__.py"), frozenset(graph))
        changed[by_path[path]] = None if reason else keys
        notes.append(f"{path}: whole module ({reason})" if reason
                     else f"{path}: by name ({', '.join(sorted(keys)) or 'no code change'})")
    return symbol_impact.reached(symbol_impact.index(root, graph), changed, why)


def _reading_units(root: Path, graph: dict, name: str, runs: set[str], files: set[str]
                   ) -> tuple[dict, dict[str, bool]]:
    """A reader's units, and for each unit holding a string in ``runs`` whether one of them
    names a changed file (False: each is joined to a constant naming another file)."""
    try:
        parts = symbol_impact.split((root / graph[name].path).read_text("utf-8"), name)
    except (OSError, SyntaxError, ValueError):
        return {}, {}
    matched: dict[str, bool] = {}
    for key, part in parts.items():
        elsewhere = _joined_elsewhere(part.nodes, files)
        for top in part.nodes:
            for node in ast.walk(top):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and _key(node.value) in runs:
                    matched[key] = matched.get(key, False) or id(node) not in elsewhere
    return parts, matched


def _joined_elsewhere(nodes: list[ast.AST], files: set[str]) -> set[int]:
    """The ids of string constants a path join continues with a constant naming none of
    ``files`` (``"operating"`` in ``ROOT / "operating" / "other.json"``)."""
    found = set()
    for top in nodes:
        for node in ast.walk(top):
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                    and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
                continue
            left = node.left.right if isinstance(node.left, ast.BinOp) else node.left
            if isinstance(left, ast.Constant) and isinstance(left.value, str):
                joined = f"/{left.value.strip('/')}/{node.right.value.strip('/')}"
                if not any(joined + "/" in f"/{path}/" for path in files):
                    found.add(id(left))
    return found


def select(root: Path, changed: set[str], *, gate_spec_data: bool = False, base: str | None = None,
           notes: list[str] | None = None, why: dict[str, str] | None = None,
           named: set[str] = frozenset()) -> list[str]:
    """``base`` (the commit ``changed`` differs from) narrows a changed ``.py`` module to the
    names its diff touches, except the ``named`` ones (judged whole); ``notes`` receives one
    line per module saying how it was judged."""
    code = {path for path in changed if not path.endswith(".md")}
    markdown = changed - code
    if not changed:
        return []
    if code & set(SELECTOR) or (GATE_SPEC in code and not gate_spec_data):
        return ["ALL"]
    graph = import_graph.build(root)
    prose = prose_readers(root, graph, markdown)
    if prose is None:
        return ["ALL"]
    scans = text_scans(root, graph, changed) | md_readers(root, graph, markdown)
    if not code:
        observers = data_observers(root, graph, prose) if prose else set()
        return sorted({name for name in observers if import_graph.is_unittest_module(name)}
                      | scans | (dynamic_tests(graph) if prose else set()))
    properties = property_tests(root, graph)
    by_path = {module.path: module.name for module in graph.values()}
    python = {path for path in code if path.endswith(".py")}
    data = code - python
    readers = data_readers(graph, data)
    if python - set(by_path) or readers is None or properties is None:
        return ["ALL"]
    for found in (source_readers(graph, python), prose):
        for name, runs in found.items():
            readers.setdefault(name, set()).update(runs)
    if base is None:
        hit = import_graph.affected(graph, {by_path[path] for path in python})
        observers = data_observers(root, graph, readers)
    else:  # product readers go by name with the code; test readers by the test-level walk
        hit = symbol_hit(root, graph, base, python, notes if notes is not None else [], why,
                         {name: runs for name, runs in readers.items() if not graph[name].is_test},
                         data | markdown, named)
        tests = {name: runs for name, runs in readers.items() if graph[name].is_test and (
            lambda matched: not matched or any(matched.values()))(
                _reading_units(root, graph, name, runs, data | markdown)[1])}
        observers = data_observers(root, graph, tests) if tests else set()
    commands = {test for test, doc in COMMAND_DOCS.items() if test in graph
                and (hit | observers) & command_modules(root, graph, doc)}
    return sorted({name for name in hit | observers if import_graph.is_unittest_module(name)}
                  | commands | dynamic_tests(graph) | scans | properties)

def select_groups(root: Path, groups: dict, *, whole_modules: bool = False) -> dict[str, list[str]]:
    """``select`` for each named group of changed files, each against its own
    ``base`` (narrowing by name as ``--base`` does; ``None`` or ``whole_modules`` judges the
    files whole).
    A group whose
    selection fails prints ``ALL`` for that group only, with the error on stderr."""
    out: dict[str, list[str]] = {}
    for name, spec in groups.items():
        files = {relative(root, file) for file in spec.get("files", [])}
        base = None if whole_modules else spec.get("base")
        try:
            out[name] = select(root, files, base=base)
        except (Exception, SystemExit) as error:
            print(f"affected_tests: group {name}: {type(error).__name__}: {error}", file=sys.stderr)
            out[name] = ["ALL"]
    return out


def changed_since(root: Path, base: str) -> set[str]:
    def git(*args: str) -> list[str]:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise SystemExit(f"git {' '.join(args)}: {result.stderr.strip()}")
        return result.stdout.splitlines()
    return set(git("diff", "--name-only", "--no-renames", "--relative", base, "--")
               + git("ls-files", "--others", "--exclude-standard"))


def relative(root: Path, name: str) -> str:
    path = Path(os.path.normpath(name))
    if path.is_absolute():
        path = path.relative_to(root.resolve()) if path.is_relative_to(root.resolve()) else path
    return path.as_posix()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="*", help="changed files, relative to the root or absolute")
    parser.add_argument("--base", help="also include every file that differs from this commit")
    parser.add_argument("--untested", action="store_true",
                        help="print the changed non-test .py modules no test reaches, not the selection")
    parser.add_argument("--explain", action="store_true",
                        help="say on stderr what reached each selected test (with --base)")
    parser.add_argument("--whole-modules", action="store_true",
                        help="select by whole changed modules even with --base (the import graph alone)")
    parser.add_argument("--root", type=Path, default=ROOT, help="repo root (default: this checkout)")
    parser.add_argument("--groups", type=Path, metavar="JSON",
                        help="select per group in one pass (one import graph): a JSON object "
                             '{"<name>": {"files": [...], "base": "<commit>"|null}}; prints '
                             '{"<name>": [modules]} (the batch lander\'s per-lane ownership)')
    args = parser.parse_args(argv)
    if args.groups:
        print(json.dumps(select_groups(args.root, json.loads(args.groups.read_text()),
                                       whole_modules=args.whole_modules), sort_keys=True))
        return 0
    if not args.files and not args.base:
        parser.error("name changed files or pass --base")
    changed = {relative(args.root, name) for name in args.files}
    if args.base:
        changed |= changed_since(args.root, args.base)
    notes: list[str] = []
    why: dict[str, str] = {}
    selected = untested(args.root, changed) if args.untested else \
        select(args.root, changed, base=None if args.whole_modules else args.base, notes=notes, why=why,
               named={relative(args.root, name) for name in args.files})
    for note in notes:
        print(f"affected_tests: {note}", file=sys.stderr)
    if args.explain:
        for line in selected:
            print(f"affected_tests: {line} <- {why.get(line, 'not by name (data, scan, property or dynamic)')}",
                  file=sys.stderr)
    for line in selected:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
