"""AST import graph of the repo's first-party Python modules, shared by the code map and affected-test selection.

A module is a ``.py`` file under the root named by its dotted path (``pkg/__init__.py``
is ``pkg``). Hidden directories, ``__pycache__``, ``EVIDENCE``, ignored runtime
directories and any nested checkout (a directory holding its own ``.git``, such as a
git worktree) are skipped. An edge is a static import found anywhere in the module
(function-local ones included) plus the dynamic forms this repo uses: a bare import that
matches a first-party file name after a ``sys.path`` insert (``import check_http_egress``),
and a string that is exactly a module's dotted name below the top level, or a module's
file name or path, where the module uses it as one: a path join (``ROOT / "scripts/x.py"``),
an argument of a call that is not an assertion (``Path(...)``, ``open(...)``, a helper), an
element of a command line (a list or tuple holding ``"-m"`` or ``sys.executable``, or
starting with a ``.py`` path), an element of a literal a loop iterates, or the whole value
bound to a name (``SCRIPT = "scripts/x.py"``). A string inside a set, a dict, any other
list or tuple, a comparison or an assertion only names the module (an allowlist, a fixture,
a file hint) and links nothing: such text made every test of a checker depend on
everything its allowlist names. A string never links an ``__init__.py`` or a ``test_*``
module: only unittest runs one, so a string naming it is data, such as a gate spec or a
selector table. A computed import (``importlib.import_module(x)`` or ``__import__(x)``)
links its argument when that is a string, and every non-``test_*`` module under a constant
prefix (``f"integrations.{name}"``, ``"pkg." + name``) or under a constant package whose
``fromlist`` is computed (``__import__("pkg", fromlist=[name])``), whatever name
``importlib``'s functions are called by. An argument that is a parameter of the enclosing
private function (``def _importable(name): importlib.import_module(name)``) links the
string literals that function's calls pass for it, when every reference to the function in
its module is such a call and no other module imports the name; any other argument cannot
be resolved, so the module is marked ``dynamic``, and so is a module whose private
importing helper another module imports or reaches as an attribute (``import plug; plug._load(name)``). Importing a module
also runs the ``__init__`` of every package on its path, so ``dependencies`` adds those too.
Each module also keeps its ``strings``, the constants that may name a file (no whitespace,
not a docstring; a bare word such as ``"schema"`` only where a path is built from it:
``ROOT / "schema"``, ``with_name("schema")``, a literal a loop iterates; a dict key is no
path) and whether it ``walks`` a directory tree. Everything is sorted, so the output is
deterministic. ``build`` keeps its last few graphs for the process, keyed by every file's
path and text, so repeated selections in one process parse the tree once.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, replace
import gc
import os
from pathlib import Path
import sys

SKIP_DIRS = frozenset({"__pycache__", "EVIDENCE", "node_modules", "venv", "env", "state", "outputs"})
BUILT_KEEP = 4
_built: dict[tuple[tuple[str, str], ...], dict[str, Module]] = {}  # (path, text) of every file -> graph


@dataclass(frozen=True)
class Module:
    name: str
    path: str
    doc: str
    public: tuple[str, ...]
    imports: tuple[str, ...]
    from_names: tuple[tuple[str, str], ...]
    dynamic: bool = False  # a computed import the graph cannot resolve
    strings: tuple[str, ...] = ()  # the constants that may name a file (see the docstring)
    walks: bool = False  # reads a whole directory tree (rglob, os.walk, a ``**/`` glob)

    @property
    def is_test(self) -> bool:
        return is_test_path(self.path)


def is_test_path(path: str) -> bool:
    parts = path.split("/")
    return "tests" in parts[:-1] or parts[-1].startswith("test_")


def is_unittest_module(name: str) -> bool:
    """The modules ``python3 -m unittest`` is given: those named ``test_*``, as discovery does."""
    return name.rpartition(".")[2].startswith("test_")


def source_files(root: Path) -> list[str]:
    found = []
    for directory, dirs, files in os.walk(root):
        here = Path(directory)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")
                         and not (here / d / ".git").exists())
        found.extend((here / f).relative_to(root).as_posix() for f in files if f.endswith(".py"))
    return sorted(found)


def module_name(path: str) -> str:
    parts = path[: -len(".py")].split("/")
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def build(root: Path) -> dict[str, Module]:
    paths = {module_name(p): p for p in source_files(root)}
    texts = {name: (root / paths[name]).read_text(encoding="utf-8", errors="replace")
             for name in sorted(paths)}
    key = tuple((paths[name], text) for name, text in texts.items())
    if key not in _built:
        collecting = gc.isenabled()
        gc.disable()  # parsing makes millions of acyclic AST nodes: collecting them cost ~20%
        try:
            _built[key] = _build(paths, texts)
        finally:
            if collecting:
                gc.enable()
        while len(_built) > BUILT_KEEP:
            del _built[next(iter(_built))]
    return dict(_built[key])


def _build(paths: dict[str, str], texts: dict[str, str]) -> dict[str, Module]:
    lookup = _Lookup(paths)
    graph = {}
    literal_helpers: dict[str, set[str]] = {}
    imported: set[tuple[str, str]] = set()  # (module, name) another module takes from it
    for name, text in texts.items():
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = ast.Module(body=[], type_ignores=[])
        package = name if paths[name].endswith("__init__.py") else name.rpartition(".")[0]
        imports, from_names, dynamic, strings, walks, helpers, attributes = _edges(tree, package, lookup)
        imports.discard(name)
        literal_helpers[name] = helpers
        imported |= from_names | attributes
        graph[name] = Module(
            name=name, path=paths[name], doc=_summary(tree), public=_public(tree),
            imports=tuple(sorted(imports)), from_names=tuple(sorted(from_names)),
            dynamic=dynamic, strings=tuple(sorted(strings)), walks=walks)
    # A literal-argument helper another module imports may be called with any name there.
    for name, helpers in literal_helpers.items():
        if any((name, helper) in imported for helper in helpers):
            graph[name] = replace(graph[name], dynamic=True)
    return graph


def dependencies(graph: dict[str, Module]) -> dict[str, set[str]]:
    """What importing each module runs: its imports, plus the ``__init__`` of every package
    on the way to the module and to each import (``import a.b.c`` runs ``a`` and ``a.b``)."""
    deps = {}
    for module in graph.values():
        ran = set(module.imports)
        for name in (module.name, *module.imports):
            parts = name.split(".")
            ran.update(".".join(parts[:end]) for end in range(1, len(parts)))
        deps[module.name] = (ran & graph.keys()) - {module.name}
    return deps


def dependents(graph: dict[str, Module]) -> dict[str, list[str]]:
    reverse: dict[str, set[str]] = {name: set() for name in graph}
    for name, deps in dependencies(graph).items():
        for target in deps:
            reverse[target].add(name)
    return {name: sorted(importers) for name, importers in reverse.items()}


def affected(graph: dict[str, Module], changed: set[str]) -> set[str]:
    """The changed modules plus everything that runs them when imported, transitively. A
    changed package ``__init__`` therefore reaches every module below it and every module
    importing anything in the package, whatever that ``__init__`` imports in turn."""
    reverse = dependents(graph)
    seen = {name for name in changed if name in graph}
    queue = sorted(seen)
    while queue:
        for importer in reverse[queue.pop()]:
            if importer not in seen:
                seen.add(importer)
                queue.append(importer)
    return seen


class _Lookup:
    def __init__(self, paths: dict[str, str]) -> None:
        self.names = frozenset(paths)
        self.by_path: dict[str, str] = {}
        self.by_leaf: dict[str, list[str]] = {}
        self.by_file: dict[str, list[str]] = {}
        for name, path in sorted(paths.items()):
            self.by_leaf.setdefault(name.rpartition(".")[2], []).append(name)
            if not path.endswith("__init__.py"):
                self.by_path[path] = name
                self.by_file.setdefault(path.rpartition("/")[2], []).append(name)

    def resolve(self, dotted: str) -> str | None:
        """The longest first-party module that ``dotted`` names or sits under."""
        parts = dotted.split(".")
        for end in range(len(parts), 0, -1):
            if ".".join(parts[:end]) in self.names:
                return ".".join(parts[:end])
        return None

    def bare(self, dotted: str) -> list[str]:
        """First-party files a non-first-party import name may stand for (a ``sys.path`` insert)."""
        leaf = dotted.partition(".")[0]
        return [] if leaf in sys.stdlib_module_names else self.by_leaf.get(leaf, [])

    def prefixed(self, prefix: str) -> list[str]:
        """Every module a computed name starting with ``prefix`` may resolve to, except
        ``test_*`` modules (unittest runs those; a plugin loop does not import them)."""
        return sorted(name for name in self.names if not is_unittest_module(name)
                      and (name.startswith(prefix) or name == prefix.rstrip(".")))

    def string(self, text: str) -> list[str]:
        """The modules a string names, ``test_*`` modules excepted (unittest runs those)."""
        if text in self.names and "." in text:
            found = [text]
        elif text in self.by_path:
            found = [self.by_path[text]]
        else:
            found = self.by_file.get(text, []) if text.endswith(".py") and "/" not in text else []
        return [name for name in found if not is_unittest_module(name)]


def _computed_prefix(node: ast.AST) -> str | None:
    """The whole string (a constant) or its constant leading part (an f-string, or ``+``
    with a constant left side) of a computed module name; None when there is neither."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        head = ""
        for part in node.values:
            if not (isinstance(part, ast.Constant) and isinstance(part.value, str)):
                break
            head += part.value
        return head or None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _computed_prefix(node.left)
    return None


IMPORTERS = ("__import__", "import_module")


def _importer(node: ast.Call, aliases: dict[str, str]) -> str | None:
    """``__import__`` or ``import_module`` when ``node`` calls one, by its own name, an attribute (``importlib.__import__``) or a ``from importlib`` alias."""
    func = node.func
    if isinstance(func, ast.Name):
        return aliases.get(func.id)
    return func.attr if isinstance(func, ast.Attribute) and func.attr in IMPORTERS else None


def _fromlist(node: ast.Call) -> ast.AST | None:
    """The ``fromlist`` argument of an ``__import__`` call (keyword or fourth positional)."""
    for keyword in node.keywords:
        if keyword.arg == "fromlist":
            return keyword.value
    return node.args[3] if len(node.args) > 3 else None


def _callee(node: ast.Call) -> str:
    func = node.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")


def _command_line(node: ast.List | ast.Tuple) -> bool:
    """A list or tuple a process runs: it holds ``"-m"`` or the interpreter
    (``sys.executable``, ``"python3"``), or is a ``.py`` path followed by options."""
    values = [e.value if isinstance(e, ast.Constant) and isinstance(e.value, str) else None
              for e in node.elts]
    if values and (values[0] or "").endswith(".py") and any(
            (value or "").startswith("-") for value in values[1:]):
        return True
    return any(value == "-m" or (value or "").rpartition("/")[2] in ("python", "python3")
               or (isinstance(e, ast.Attribute) and e.attr == "executable")
               for value, e in zip(values, node.elts))


PATH_CALLS = frozenset({"Path", "PurePath", "with_name", "joinpath", "join", "open"})


def _used_strings(nodes: list[ast.AST]) -> tuple[set[int], set[int], set[int], bool]:
    """The ids of the string constants a module uses as a path or a module name (the
    contexts listed in this module's docstring), of those it builds a path from (a path
    join, a ``PATH_CALLS`` argument, a literal a loop iterates) and of its docstrings, and
    whether it walks a directory tree (``rglob``, ``os.walk`` or a ``**/`` glob)."""
    used: set[int] = set()
    joined: set[int] = set()
    docs: set[int] = set()
    walks = False

    def take(*nodes: ast.AST | None, path: bool = False) -> None:
        ids = {id(n) for n in nodes if isinstance(n, ast.Constant)}
        used.update(ids)
        if path:
            joined.update(ids)

    for node in nodes:
        if isinstance(node, ast.Expr):
            docs.add(id(node.value))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            take(node.left, node.right, path=True)
        elif isinstance(node, ast.Call) and not _callee(node).startswith("assert"):
            take(*node.args, *(keyword.value for keyword in node.keywords),
                 path=_callee(node) in PATH_CALLS)
            walks = walks or _callee(node) == "rglob" or (
                isinstance(node.func, ast.Attribute) and node.func.attr == "walk"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "os")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and (
                "**/" in node.value or node.value.endswith("/**")):
            walks = True
        elif isinstance(node, (ast.List, ast.Tuple)) and _command_line(node):
            take(*node.elts)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)) and isinstance(
                node.iter, (ast.List, ast.Tuple, ast.Set)):
            take(*node.iter.elts, path=True)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            take(node.value)
    return used, joined, docs, walks


def _innermost_functions(tree: ast.AST) -> dict[int, ast.FunctionDef | ast.AsyncFunctionDef]:
    """The id of every call inside a function mapped to its innermost enclosing function."""
    found: dict[int, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    stack: list[tuple[ast.AST, ast.AST | None]] = [(tree, None)]
    while stack:
        node, function = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node
        elif isinstance(node, ast.Call) and function is not None:
            found[id(node)] = function
        stack.extend((child, function) for child in ast.iter_child_nodes(node))
    return found


def _literal_arguments(tree: ast.AST, function: ast.AST | None, parameter: str) -> list[str] | None:
    """The string literals passed for ``parameter`` by every call of ``function``, a private
    function that never rebinds it; None unless every reference to the function in the
    module is a plain call passing (or defaulting) a literal there."""
    if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) \
            or not function.name.startswith("_"):
        return None
    args = function.args
    positional = [a.arg for a in (*args.posonlyargs, *args.args)]
    defaults = dict(zip(positional[len(positional) - len(args.defaults):], args.defaults))
    defaults.update((a.arg, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d)
    own = (*args.posonlyargs, *args.args, *args.kwonlyargs)
    rebound = any(isinstance(n, ast.Name) and n.id == parameter and not isinstance(n.ctx, ast.Load)
                  or isinstance(n, ast.arg) and n.arg == parameter and all(n is not a for a in own)
                  for n in _nodes(function))
    if rebound or parameter not in [a.arg for a in own]:
        return None
    calls, references, literals = set(), 0, []
    for node in _nodes(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == function.name:
            calls.add(id(node.func))
            if any(isinstance(a, ast.Starred) for a in node.args) \
                    or any(k.arg is None for k in node.keywords):
                return None
            index = positional.index(parameter) if parameter in positional else len(node.args)
            value = node.args[index] if index < len(node.args) else next(
                (k.value for k in node.keywords if k.arg == parameter), defaults.get(parameter))
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                return None
            literals.append(value.value)
        elif isinstance(node, ast.Name) and node.id == function.name \
                or isinstance(node, ast.Attribute) and node.attr == function.name \
                or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.name == function.name \
                or isinstance(node, ast.alias) and function.name in (node.name, node.asname):
            references += 1
    return literals if references == len(calls) + 1 else None  # + 1: the ``def`` itself


def _edges(tree: ast.AST, package: str, lookup: _Lookup) -> tuple[
        set[str], set[tuple[str, str]], bool, set[str], bool, set[str], set[tuple[str, str]]]:
    imports: set[str] = set()
    from_names: set[tuple[str, str]] = set()
    bound: dict[str, str] = {}  # a name an import binds to a first-party module
    chains: list[ast.Attribute] = []  # ``m.name`` uses, resolved once every import is bound
    strings: set[str] = set()
    dynamic = False
    aliases = {name: name for name in IMPORTERS}  # plus ``from importlib`` aliases
    calls: list[ast.Call] = []  # resolved once every alias is known
    helpers: set[str] = set()  # private functions importing their literal arguments
    enclosing: dict[int, ast.AST] | None = None
    nodes = list(_nodes(tree))  # walked twice: once for the strings in use, once for edges
    used, joined, docs, walks = _used_strings(nodes)

    def link(dotted: str) -> None:
        target = lookup.resolve(dotted)
        imports.update([target] if target else lookup.bare(dotted))

    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                link(alias.name)
                bound[alias.asname or alias.name.partition(".")[0]] = \
                    alias.name if alias.asname else alias.name.partition(".")[0]
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(node, package)
            if base is None:
                continue
            if base == "importlib" and not node.level:
                aliases.update((alias.asname, alias.name) for alias in node.names
                               if alias.asname and alias.name in IMPORTERS)
            target = lookup.resolve(base)
            linked = False
            for alias in node.names:
                child = f"{base}.{alias.name}"
                if child in lookup.names:
                    imports.add(child)
                    bound[alias.asname or alias.name] = child
                    linked = True
                elif target:
                    imports.add(target)
                    linked = True
                    if alias.name != "*":
                        from_names.add((target, alias.name))
            if not linked:
                imports.update(lookup.bare(base))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in used:
                imports.update(lookup.string(node.value))
            if id(node) not in docs and node.value and not any(c.isspace() for c in node.value) \
                    and (id(node) in joined or "/" in node.value or "." in node.value):
                strings.add(node.value)
        elif isinstance(node, ast.Call) and node.args:
            calls.append(node)
        elif isinstance(node, ast.Attribute):
            chains.append(node)
    for node in calls:
        kind = _importer(node, aliases)
        if kind is None:
            continue
        arg = node.args[0]
        prefix = _computed_prefix(arg)
        fromlist = _fromlist(node) if kind == "__import__" else None
        if isinstance(arg, ast.Constant) and prefix is not None:
            link(prefix)
            names = [_computed_prefix(e) if isinstance(e, ast.Constant) else None
                     for e in getattr(fromlist, "elts", [None])]
            if isinstance(fromlist, (ast.List, ast.Tuple)) and None not in names:
                for name in names:
                    link(f"{prefix}.{name}")
            elif fromlist is not None:
                # A computed fromlist may load any module of the package.
                imports.update(lookup.prefixed(prefix + "."))
        elif prefix and (found := lookup.prefixed(prefix)):
            imports.update(found)
        elif isinstance(arg, ast.Name) and fromlist is None and (literals := _literal_arguments(
                tree, (enclosing := enclosing or _innermost_functions(tree)).get(id(node)),
                arg.id)) is not None:
            for literal in literals:
                link(literal)
            helpers.add(enclosing[id(node)].name)
        else:
            dynamic = True
    attributes = set()
    for node in chains:
        parts = [node.attr]
        value = node.value
        while isinstance(value, ast.Attribute):
            parts.append(value.attr)
            value = value.value
        if isinstance(value, ast.Name) and value.id in bound:
            module, _, attribute = ".".join([bound[value.id], *reversed(parts)]).rpartition(".")
            if module in lookup.names:
                attributes.add((module, attribute))
    return imports, from_names, dynamic, strings, walks, helpers, attributes


def _nodes(tree: ast.AST):
    """``ast.walk`` without its per-node generator overhead (the repo is 9 MB of Python)."""
    stack = [tree]
    while stack:
        node = stack.pop()
        yield node
        for field in node._fields:
            child = getattr(node, field, None)
            if isinstance(child, list):
                stack.extend(c for c in child if isinstance(c, ast.AST))
            elif isinstance(child, ast.AST):
                stack.append(child)


def _absolute(node: ast.ImportFrom, package: str) -> str | None:
    if not node.level:
        return node.module
    anchor = package.split(".") if package else []
    if node.level - 1 > len(anchor):
        return None
    anchor = anchor[: len(anchor) - (node.level - 1)]
    return ".".join(anchor + ([node.module] if node.module else [])) or None


def _summary(tree: ast.Module) -> str:
    """The docstring's first line, continued while that sentence is still wrapped."""
    lines = (ast.get_docstring(tree) or "").split("\n\n")[0].splitlines()
    summary = ""
    for line in lines:
        summary = f"{summary} {line.strip()}".strip()
        if summary.endswith((".", ":", ";")):
            break
    return summary


def _public(tree: ast.Module) -> tuple[str, ...]:
    """``__all__`` when it is a literal list, else classes and functions, then constants."""
    callables, constants = [], []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            callables.append(node.name)
            continue
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        for target in targets:
            if isinstance(target, ast.Name):
                if target.id == "__all__" and isinstance(getattr(node, "value", None), (ast.List, ast.Tuple)):
                    exported = [e.value for e in node.value.elts
                                if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                    return tuple(exported)
                constants.append(target.id)
    return tuple(dict.fromkeys(n for n in callables + constants if not n.startswith("_")))
