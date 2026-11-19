"""Which modules a change reaches, by the top-level names it touches rather than by whole files.

The import graph is one component here: function-local imports make nearly every module
reach nearly every other, so a one-line change in ``ontology/delta.py`` or a hub such as
``operator_api/backend.py`` selected ~570 of ~630 test modules. Given the base text of each
changed module, this splits every module of the tree into units: a top-level function, a
top-level assignment, a class header (bases, decorators, the body's other statements), each
class member (a method, a field, a nested class), the ``if __name__ == "__main__"`` block and
the module's import-time code (imports and every other top-level statement). Units the diff
changes define atoms; every unit referring to an atom changes in turn, to a fixed point.

- A function, an assignment or a class (any of its units) defines its name in its module.
  A method reaches the users of its class's name, not of its attribute name: a test runs a
  method only on an instance it got by naming the class (a constructor, a subclass, an
  annotation) or by calling code that does, and that code changes with it. Attribute names
  are not followed (``.get``, ``.run``, ``.value`` join unrelated classes; measured on this
  tree, following them selected every test again).
- A unit refers to a name through its module's imports (followed through re-exports and
  ``*`` imports), an attribute chain on an imported module, a dotted string the import graph
  counts as a module use (``mock.patch("pkg.mod.f")``) or a name of its own module.
- A unit importing a module (anywhere in it) refers to that module's import-time code; one
  using a module as a value (passing it, a string naming it or its path, ``globals()``)
  refers to every unit of it.
- A decorator outside the known pure wrappers may register what it wraps: a changed
  decorated definition changes a decorator object's name (``@router.get``) or the names of
  its own module a decorator function refers to (``@register`` storing into ``REGISTRY``).

Import-time code falls back to the whole module (``whole``): changed import-time code, or a
module the parser cannot read, changes every unit of the module and the import-time code of
every unit importing it. An edge of the import graph no import statement or string accounts
for (a computed import it resolved) is import-time use of the whole target. What this misses
by design: a method reached only through an instance found dynamically (a string lookup, a
pickle), which the nightly full run (``plx-suite``) catches.
"""

from __future__ import annotations

import ast
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
import os
from pathlib import Path
import re

from tools import import_graph
from ontology import parent_watch

INIT = "<init>"
MAIN = "__main__"
IMPORTS = "<imports>"  # the first-party modules the module imports at import time
# Decorators that wrap or describe a definition without registering it anywhere.
PURE_DECORATORS = frozenset({
    "property", "staticmethod", "classmethod", "cached_property", "abstractmethod", "dataclass",
    "contextmanager", "asynccontextmanager", "overload", "override", "final", "wraps", "lru_cache",
    "cache", "total_ordering", "runtime_checkable", "singledispatch", "skip", "skipIf",
    "skipUnless", "expectedFailure", "patch", "object", "dict", "field_validator", "validator",
    "model_validator", "root_validator", "computed_field", "unique", "setter", "getter", "deleter"})
DOTTED = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+\Z")


@dataclass
class Part:
    """A unit's code, as ``split`` finds it."""
    nodes: list[ast.AST] = field(default_factory=list)
    defines: set[tuple[str, str]] = field(default_factory=set)  # (module, top-level name)
    whole: bool = False  # import-time code
    decorators: list[ast.expr] = field(default_factory=list)  # ones that may register
    sig: list[str] | None = None  # what a diff compares (None: the nodes' code)


@dataclass
class Unit:
    module: str
    key: str  # "f", "C", "C.x", INIT, MAIN
    defines: set[tuple[str, str]] = field(default_factory=set)
    whole: bool = False
    names: set[tuple[str, str]] = field(default_factory=set)  # (module, name) it refers to
    imports: set[str] = field(default_factory=set)  # modules whose import-time code it runs
    values: set[str] = field(default_factory=set)  # modules it uses whole
    registry: set[tuple[str, str]] = field(default_factory=set)  # (module, name) of its decorators
    function: bool = False  # a top-level function

    @property
    def import_time(self) -> bool:
        return self.key in (INIT, IMPORTS)


@dataclass
class Parsed:
    units: list[Unit]
    top: set[str]  # the module's own top-level names
    bindings: dict[str, set[tuple]]  # name -> {("mod", M) | ("name", M, attr)} its imports bind
    stars: tuple[str, ...]  # modules it ``*``-imports


def _strip_doc(body: list[ast.stmt]) -> list[ast.stmt]:
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[1:]
    return body


def _dump(node: ast.AST) -> str:
    """The code of a node, its docstring left out (a doc edit changes no behaviour)."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        body, node.body = node.body, _strip_doc(node.body)
        try:
            return ast.dump(node)
        finally:
            node.body = body
    return ast.dump(node)


def _decorator_name(node: ast.expr) -> str:
    node = node.func if isinstance(node, ast.Call) else node
    return node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")


def _main_guard(node: ast.stmt) -> bool:
    test = getattr(node, "test", None)
    return isinstance(node, ast.If) and isinstance(test, ast.Compare) \
        and isinstance(test.left, ast.Name) and test.left.id == "__name__" \
        and any(isinstance(c, ast.Constant) and c.value == "__main__" for c in test.comparators)


def _targets(node: ast.stmt) -> list[str] | None:
    """The names an assignment binds; None when it binds anything else."""
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names = []
    for target in targets:
        elts = target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target]
        if not all(isinstance(e, ast.Name) for e in elts):
            return None
        names += [e.id for e in elts]
    return names


def split(text: str, module: str, is_package: bool = False, modules: frozenset[str] | None = None
          ) -> dict[str, Part]:
    """The units of a module's text by key. A top-level import binds each name in its own
    unit (``import json``), and ``IMPORTS`` holds the first-party modules imported at import
    time (``modules``; None: every module named)."""
    tree = ast.parse(text)
    parts: dict[str, Part] = {}
    imported: set[str] = set()

    def add(key: str, nodes: list[ast.AST], defines: set[tuple[str, str]], whole: bool = False,
            decorators: list[ast.expr] = ()) -> None:
        part = parts.setdefault(key, Part())
        part.nodes += nodes
        part.defines |= defines
        part.whole = part.whole or whole
        part.decorators += [d for d in decorators if _decorator_name(d) not in PURE_DECORATORS]

    for stmt in _strip_doc(tree.body):
        if _main_guard(stmt):  # runs only as a script, never on import
            add(MAIN, [stmt], set())
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(stmt.name, [stmt], {(module, stmt.name)}, decorators=stmt.decorator_list)
        elif isinstance(stmt, ast.ClassDef):
            own = {(module, stmt.name)}
            header: list[ast.AST] = [*stmt.bases, *stmt.keywords, *stmt.decorator_list]
            for member in _strip_doc(stmt.body):
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    add(f"{stmt.name}.{member.name}", [member], own)
                elif isinstance(member, (ast.Assign, ast.AnnAssign)) and _targets(member):
                    for name in _targets(member):
                        add(f"{stmt.name}.{name}", [member], own)
                else:
                    header.append(member)
            add(stmt.name, header, own, decorators=stmt.decorator_list)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)) and _targets(stmt):
            names = _targets(stmt)
            for name in names:
                add(name, [stmt], {(module, n) for n in names})
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)) and all(a.name != "*" for a in stmt.names):
            base = _absolute(stmt, module, is_package) if isinstance(stmt, ast.ImportFrom) else ""
            for alias in stmt.names:
                if isinstance(stmt, ast.Import):
                    bound = alias.asname or alias.name.partition(".")[0]
                    target = alias.name if alias.asname else bound
                    imported.update(_parents(alias.name))
                else:
                    bound, target = alias.asname or alias.name, f"{base}:{alias.name}"
                    imported.update([*_parents(base or "."), f"{base}.{alias.name}"])
                add(f"import {bound}", [], {(module, bound)})
                parts[f"import {bound}"].sig = sorted({*(parts[f"import {bound}"].sig or ()), target})
        else:
            add(INIT, [stmt], set(), True)
    if imported:
        add(IMPORTS, [], set())
        parts[IMPORTS].sig = sorted(imported if modules is None else imported & modules)
    return parts


def _sig(part: Part) -> list[str]:
    return part.sig if part.sig is not None else [_dump(n) for n in part.nodes]


def changed_units(module: str, old: str | None, new: str, is_package: bool = False,
                  modules: frozenset[str] | None = None) -> tuple[set[str], str | None]:
    """The unit keys a diff changes, and why the module falls back whole (None: it does not).
    ``old`` is None when there is no base text to compare; ``""`` for a new module, which
    nothing imported before, so it never falls back."""
    if old is None:
        return set(), "no base version to compare"
    try:
        before = split(old, module, is_package, modules)
        after = split(new, module, is_package, modules)
    except (SyntaxError, ValueError):
        return set(), "does not parse"
    keys = {key for key in before.keys() | after.keys()
            if key not in before or key not in after or _sig(before[key]) != _sig(after[key])}
    if old.strip() and any((after.get(key) or before[key]).whole for key in keys):
        return keys, "import-time code changed"
    return keys, None


def _parents(name: str) -> list[str]:
    parts = name.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts) + 1)]


def _absolute(node: ast.ImportFrom, module: str, is_package: bool) -> str | None:
    if not node.level:
        return node.module
    base = module.split(".") if is_package else module.split(".")[:-1]
    if node.level - 1 > len(base):
        return None
    base = base[:len(base) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


@dataclass(frozen=True)
class _Scope:
    module: str
    is_package: bool
    modules: frozenset[str]
    top: frozenset[str]
    bindings: dict


def _bindings(tree: ast.AST, module: str, is_package: bool, modules: frozenset[str]
              ) -> tuple[dict[str, set[tuple]], tuple[str, ...]]:
    """What the module's imports (anywhere in it) bind each name to, and its ``*`` sources."""
    heads = {m.partition(".")[0] for m in modules}
    bindings: dict[str, set[tuple]] = {}
    stars: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.partition(".")[0] in heads:
                    target = alias.name if alias.asname else alias.name.partition(".")[0]
                    bindings.setdefault(alias.asname or target, set()).add(("mod", target))
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(node, module, is_package)
            for alias in node.names if base else ():
                if alias.name == "*":
                    stars.append(base)
                elif f"{base}.{alias.name}" in modules:
                    bindings.setdefault(alias.asname or alias.name, set()).add(("mod", f"{base}.{alias.name}"))
                elif base in modules:
                    bindings.setdefault(alias.asname or alias.name, set()).add(("name", base, alias.name))
    return bindings, tuple(stars)


def _refs(unit: Unit, nodes: list[ast.AST], scope: _Scope) -> None:
    """Fill ``unit``'s references from its nodes; ("*", name) is a name only a ``*`` import
    can bind."""
    chained: set[int] = set()

    def module_chain(base: str, parts: list[str]) -> None:
        current = base
        for part in parts:
            if f"{current}.{part}" not in scope.modules:
                unit.names.add((current, part))
                return
            current = f"{current}.{part}"
        unit.values.add(current)

    walked = [node for root in nodes for node in ast.walk(root)]
    used = import_graph._used_strings(walked)[0]  # strings used as a module or path there
    for node in walked:
        if isinstance(node, ast.Import):
            for alias in node.names:
                unit.imports.update(p for p in _parents(alias.name) if p in scope.modules)
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(node, scope.module, scope.is_package)
            if base:
                unit.imports.update(p for p in _parents(base) if p in scope.modules)
                unit.imports.update(f"{base}.{a.name}" for a in node.names if f"{base}.{a.name}" in scope.modules)
                if base in scope.modules and any(a.name == "*" for a in node.names):
                    unit.values.add(base)
        elif isinstance(node, ast.Attribute) and id(node) not in chained:
            parts, value = [node.attr], node.value
            while isinstance(value, ast.Attribute):
                chained.add(id(value))
                parts.append(value.attr)
                value = value.value
            if isinstance(value, ast.Name):
                for target in scope.bindings.get(value.id, ()):
                    if target[0] == "mod":
                        chained.add(id(value))
                        module_chain(target[1], parts[::-1])
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and id(node) not in chained:
            if node.id in scope.top:
                unit.names.add((scope.module, node.id))
            for target in scope.bindings.get(node.id, ()):
                if target[0] == "mod":
                    unit.values.add(target[1])
                else:
                    unit.names.add((target[1], target[2]))
            if node.id in ("globals", "vars"):
                unit.values.add(scope.module)
            if node.id not in scope.top and node.id not in scope.bindings:
                unit.names.add(("*", node.id))
        elif isinstance(node, ast.Call) and _decorator_name(node) == "object" and len(node.args) >= 2 \
                and isinstance(node.args[0], ast.Name) and isinstance(node.args[1], ast.Constant) \
                and isinstance(node.args[1].value, str):
            for target in scope.bindings.get(node.args[0].id, ()):  # patch.object(module, "f")
                if target[0] == "mod":
                    unit.names.add((target[1], node.args[1].value))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) in used:
            text = node.value
            if text.endswith(".py") and text[:-3].replace("/", ".") in scope.modules:
                unit.values.add(text[:-3].replace("/", "."))
            elif DOTTED.match(text) or text in scope.modules:
                parts = text.split(".")
                for i in range(len(parts), 0, -1):
                    if ".".join(parts[:i]) in scope.modules:
                        module_chain(".".join(parts[:i]), parts[i:])
                        break


def _module_units(job: tuple[str, str, bool, frozenset[str], tuple[str, ...]]) -> tuple[str, Parsed | None]:
    name, text, is_package, modules, edges = job
    try:
        tree = ast.parse(text)
        parts = split(text, name, is_package, modules)
    except (SyntaxError, ValueError):
        return name, None
    bindings, stars = _bindings(tree, name, is_package, modules)
    top = frozenset(key.removeprefix("import ") for key in parts
                    if key not in (INIT, MAIN, IMPORTS) and "." not in key.removeprefix("import "))
    scope = _Scope(name, is_package, modules, top, bindings)
    units = []
    for key, part in parts.items():
        unit = Unit(name, key, set(part.defines), part.whole,
                    function=any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in part.nodes)
                    and "." not in key)
        _refs(unit, part.nodes, scope)
        if key == IMPORTS:
            unit.imports = set(part.sig or ())
        unit.names = {(source, n) for module, n in unit.names
                      for source in (stars if module == "*" else (module,))}
        if part.decorators:
            seen = Unit(name, key)
            _refs(seen, part.decorators, scope)
            unit.registry = {ref for ref in seen.names if ref[0] != "*"} | {(m, "") for m in seen.values}
        units.append(unit)
    explained = {m for u in units for m in u.imports | u.values | {n[0] for n in u.names}}
    explained |= {t[1] for targets in bindings.values() for t in targets}
    rest = set(edges) - {p for m in explained for p in _parents(m)}
    if rest:
        init = next((u for u in units if u.key == INIT), None)
        if init is None:
            init = Unit(name, INIT, whole=True)
            units.append(init)
        init.values |= rest
        init.imports |= rest
    return name, Parsed(units, set(top), bindings, stars)


def index(root, graph: dict) -> dict[str, Parsed | None]:
    """Every module's units (None: a module the parser cannot read): those the disk cache
    (``import_graph.DiskCache``) holds for the same text, module names and imports are read
    back; the rest are parsed on every core."""
    modules = frozenset(graph)
    cache = import_graph.DiskCache(f"units:{__name__}", "\n".join(sorted(modules)),
                                   Path(__file__).read_text("utf-8"))
    found: dict[str, Parsed | None] = {}
    jobs, keys = [], {}
    for module in graph.values():
        try:
            text = (root / module.path).read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            text = "\0"
        is_package = module.path.endswith("__init__.py")
        key = cache.key(module.name, str(is_package), "\n".join(module.imports), text)
        hit = cache.get(key)
        if hit is not None:
            found[module.name] = hit[0]
            continue
        keys[module.name] = key
        jobs.append((module.name, text, is_package, modules, tuple(module.imports)))
    workers = min(len(jobs) // 50 + 1, os.cpu_count() or 1)
    if workers > 1:
        with ProcessPoolExecutor(workers, **parent_watch.pool_init()) as pool:
            parsed = list(pool.map(_module_units, jobs, chunksize=16))
    else:
        parsed = list(map(_module_units, jobs))
    for name, units in parsed:
        cache.put(keys[name], (units,))
        found[name] = units
    return {name: found[name] for name in graph}


def _resolve(parsed: dict, module: str, name: str, seen: set | None = None) -> set[tuple]:
    """What ``module.name`` is: ("n", M, name) for a definition (followed through re-exports
    and ``*`` imports), ("t", M) for a module, or for a name the module neither defines nor
    imports (all of it)."""
    seen = set() if seen is None else seen
    if (module, name) in seen:
        return set()
    seen.add((module, name))
    record = parsed.get(module)
    if record is None:
        return {("t", module)}
    found: set[tuple] = {("n", module, name)} if name in record.top else set()
    if found and name not in record.bindings:
        return found
    for target in record.bindings.get(name, ()):
        found |= {("t", target[1])} if target[0] == "mod" else _resolve(parsed, target[1], target[2], seen)
    for star in record.stars:
        found |= _resolve(parsed, star, name, seen)
    return found or {("t", module)}


def reached(parsed: dict[str, Parsed | None], changed: dict[str, set[str] | None],
            why: dict[str, str] | None = None) -> set[str]:
    """The modules a change reaches. ``changed`` maps each changed module to the unit keys its
    diff touched (None: the whole module); ``why`` receives, per module reached (and per
    ``module:unit``), the change that first reached it."""
    why = {} if why is None else why
    by_name: dict[tuple, list[Unit]] = {}
    by_value: dict[str, list[Unit]] = {}
    by_import: dict[str, list[Unit]] = {}
    resolved: dict[tuple[str, str], set[tuple]] = {}
    for record in parsed.values():
        for unit in record.units if record else ():
            for ref in unit.names:
                if ref not in resolved:
                    resolved[ref] = _resolve(parsed, *ref)
                for atom in resolved[ref]:
                    (by_value.setdefault(atom[1], []) if atom[0] == "t"
                     else by_name.setdefault(atom[1:], [])).append(unit)
            for module in unit.values:
                by_value.setdefault(module, []).append(unit)
            for module in unit.imports:
                by_import.setdefault(module, []).append(unit)

    def units_of(module: str) -> list[Unit]:
        record = parsed.get(module)
        return list(record.units) if record else []

    hit: set[str] = set()
    whole: set[str] = set()
    queue: list[tuple[Unit, str]] = []
    pending_whole: list[tuple[str, str]] = []
    done: set[int] = set()
    emitted: set[tuple[str, str]] = set()
    touched: set[str] = set()

    def push(units, cause: str) -> None:
        queue.extend((unit, cause) for unit in units if id(unit) not in done)

    def emit(atom: tuple[str, str], by: str) -> None:
        if atom not in emitted:
            emitted.add(atom)
            push(by_name.get(atom, ()), f"{by} changes {atom[0]}.{atom[1]}")

    def touch(module: str, by: str) -> None:
        if module not in touched:
            touched.add(module)
            push(by_value.get(module, ()), f"{by} changes module {module}, used whole")

    for module, keys in changed.items():
        if parsed.get(module) is None or keys is None:
            pending_whole.append((module, "changed"))
        else:
            hit.add(module)
            why.setdefault(module, "changed")
            push((u for u in units_of(module) if u.key in keys), "changed")
    while queue or pending_whole:
        if pending_whole:
            module, cause = pending_whole.pop()
            if module not in whole:
                whole.add(module)
                why.setdefault(module, f"whole <- {cause}")
                why.setdefault(f"{module} whole", cause)
                push(by_import.get(module, ()), f"{module} changes at import time")
                touch(module, module)
                push(units_of(module), f"{module} whole")
            continue
        unit, cause = queue.pop()
        if id(unit) in done:
            continue
        done.add(id(unit))
        hit.add(unit.module)
        name = f"{unit.module}:{unit.key}"
        why.setdefault(unit.module, f"{unit.key} <- {cause}")
        why.setdefault(name, cause)
        if unit.whole:
            pending_whole.append((unit.module, f"{name} (import-time) <- {cause}"))
        elif unit.key == IMPORTS:  # it imports other modules now: only import-time code sees it
            push((u for u in by_import.get(unit.module, ()) if u.import_time),
                 f"{name} changes what importing {unit.module} runs")
        for atom in unit.defines:
            emit(atom, name)
        touch(unit.module, name)
        for ref in unit.registry:
            for atom in _resolve(parsed, *ref) if ref[1] else {("t", ref[0])}:
                if atom[0] == "t":
                    touch(atom[1], f"{name} (decorated)")
                    continue
                decorator = next((u for u in units_of(atom[1]) if u.key == atom[2]), None)
                stores = {n for n in decorator.names if n[0] == atom[1] and n[1] != atom[2]} \
                    if decorator is not None and decorator.function else {atom[1:]}
                for target in stores:
                    emit(target, f"{name} (registered by a decorator)")
    return hit | whole
