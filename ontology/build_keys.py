"""Keys of a generation build's units and of the fingerprints it holds between builds
(``ontology.project_incremental``).

A unit's rows are a function of three things: the captures of the bindings it depends on
(their fingerprints), the part of the compiled schema those bindings are declared in, and
the code that writes rows. Its key is made of exactly those, so an ordinary change to
anything else (a step, a reader of captured files, a capture writer, another type of the
schema) leaves it reusable:

* **Schema slice** (:func:`unit_slice`): the object types of its dependency bindings, every
  link type with a link binding that has one of them as an end or that one of those types
  names (a lineage hop, an ``inherit``, a unit or currency read through it), the event
  types of those object types and, for a link unit, its own link type. The projector reads
  nothing else of the schema (``object_types``, ``link_types``, ``event_types``; the digest
  and catalog go to ``store_meta`` only). Adding ``Lot.qc_status`` changes the slice of
  the units that read a ``Lot`` binding and no other.
* **Rows code**: :data:`PROJECTION_ROWS_VERSION`, an explicit number, bumped by hand when
  the code that writes a unit's rows changes what it writes. A refactor that writes the
  same rows keeps it, so it does not make the next build cold. A forgotten bump is caught
  by a test (``ontology/tests/test_build_keys.py``): the digest of that code
  (:func:`rows_code_digest`: the definitions :data:`ROWS_CODE_ROOTS` reach) must be one
  :data:`ROWS_CODE_DIGESTS` lists for the version.
* **Fingerprints** of its dependency bindings (``project_incremental._pin``), each with
  the code of the source that read it (:func:`with_reader`): the citations, audiences,
  keys, removals and stand-in scopes a reader gives its records are not in the captured
  bytes a source fingerprints.

Held fingerprints. A source's relation fingerprints are taken from the last build when
its watermark is the one held and the code that read and fingerprinted it is the same
(:func:`reader_digest`: the source's class and the fingerprint functions, with every
definition they reach), not when all the projection code is. A sharded binding's shard
prints are held when its relation print, its binding and the shard code
(:func:`shard_code_digest`) are.

Code digests (:func:`code_digest`) are of definitions, not files: from root definitions,
every top-level definition of a repository module they name (a function, a class, a
constant; through imports, lazy ones too) is followed, and each one's syntax tree
(docstrings and comments left out) is digested. A name a definition reaches only through a
string (``getattr(module, "name")``) is not followed: such a name is a root of its own.
A module whose source cannot be read gives no digest (None: never equal, never held).

Stdlib only.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Iterable, Mapping

# The scheme of unit keys; a base generation built under another one is never reused.
KEY_SCHEME = "plx-unit-keys/v1"

# Bump when the code that writes a unit's rows (``ROWS_CODE_ROOTS``) changes what it
# writes; ``ontology/tests/test_build_keys.py`` checks that code's digest is one this
# number names.
PROJECTION_ROWS_VERSION = 1

# PROJECTION_ROWS_VERSION -> the digests (``rows_code_digest``) of the code that writes
# the rows it names. Code that writes other rows (or reports other entries) for the same
# captures and schema is a new version; a change that writes the same rows (a refactor)
# adds its digest to the current one. A generation built by code of another digest is
# adopted only when that digest is here (``ontology.build_adopt``).
ROWS_CODE_DIGESTS: dict[int, frozenset[str]] = {
    1: frozenset({
        # commit 39cf8a190c (generation 8 of the host)
        "0cd15d521f3a755fee6c013001e1c71eabc1cafa032d1b64a78fa711281801df",
        # per-unit keys; the parallel projection's shards side by side and early merges
        # and its report entries
        # outside every unit counted, not a serial rerun; the store builder's checkpoint
        # to resume
        "f587572f8410564da3da08bc04f12b0f2a1b829b48075ca26b2a31266d1d94d4",
        # object-local bindings rewritten object by object (rows and report equal a cold build's)
        "81aa94c9cdac274f8a9a6ee9eddc8684736b8efa2b75c5559fb79a9580d7f422",
        # an incremental build's changed units projected by worker processes into parts
        # merged into its seed
        "4a57d4fed7080b57658ccf0859c282a1553795442ab3aa2b064120c37fc49d01",
        # the commit's invariant queries run by threads over the committed rows (rows untouched)
        "0bc0be9cd031e1fe6083948df73ee87723dc09678f36813d7f95803e37a64294",
        # ETL_REINSERTED names public.app_sales_order_line: the rows it changes are those
        # of the units reading that relation, whose keys name it (``unit_slice`` "etl"),
        # so a unit of an unchanged key writes the same rows
        "7fbff16572c05486c099935abee16b7fa16eca3376f27d1d9817d76d180633da",
        # the build steps' code recorded in store_meta (steps_digest, the rows are the same)
        "da822466bbd7d1424228c89eba51e938dc7048ac00d3f944b9d4690e44ba5c5a",
        # stamp sightings and one link row per stretch (GROWTH-AUDIT F1/F2, decision note
        # 2026-11-11-stamp-sightings-and-link-identity): a record's current rows, values,
        # citations and links are the same; only the history of restamps is folded. A unit
        # of either code mixes with the other's exactly (current state and delta apply
        # proven equal on captures), so a v1 generation stays current and the next build
        # is not cold; not a new version, by that proof and not as a refactor
        "55eddc877f4e54d2e0b0f50088bf6f7fe5a561d5c188ef121d221060d6c7c40f",
    }),
}

# The definitions that decide a unit's rows: the projector, the store writer and its DDL
# (and content digest of the rows), the identifier normalizer (reached by name only), the
# parallel projection and merge, and the plan that cuts units and shards.
ROWS_CODE_ROOTS: tuple[tuple[str, str | None], ...] = (
    ("ontology.project", "Projector"),
    ("ontology.project", "open_generation"),
    ("ontology.project", "binding_unit"),
    ("ontology.project", "shard_unit"),
    ("ontology.project", "link_unit"),
    ("ontology.project", "_default_normalizer"),
    ("ontology.identifiers", "normalize"),
    ("ontology.store", "StoreBuilder"),
    ("ontology.store_ddl", None),
    ("ontology.project_parallel", "project"),
    ("ontology.project_incremental", "plan"),
    ("ontology.project_incremental", "with_shards"),
    ("ontology.project_incremental", "shard_count"),
)

# Definitions the rows code reaches that do not decide rows: the kept readings of captured
# files and fields of captured documents (what a derived relation holds; its fingerprint is
# of the code that derives it, ``ontology.project.derived_fingerprint``) and the document
# workers of the ``post_project`` steps (run by every build).
ROWS_CODE_STOPS: frozenset[tuple[str, str]] = frozenset({
    ("ontology.project_parallel", "_kept_memo"),
    ("ontology.project_parallel", "_kept_stores"),
    ("ontology.project_parallel", "_kept_views"),
    ("ontology.project_parallel", "_ChildKept"),
    ("ontology.project_parallel", "_derive"),
    ("ontology.project_parallel", "_parse"),
    ("ontology.project_parallel", "_parse_tasks"),
    ("ontology.project_parallel", "_init_parse"),
    ("ontology.project_parallel", "DocumentPool"),
    ("ontology.project_parallel", "document_pool"),
})

# What a source's relation fingerprints are computed by, beside its own class.
FINGERPRINT_ROOTS: tuple[tuple[str, str | None], ...] = (
    ("ontology.project", "stream_fingerprint"),
    ("ontology.project", "derived_fingerprint"),
    ("ontology.project_incremental", "_pin"),
)

# Reached by a reader but not followed: what ``derivation_code`` returns, not its code, is
# part of every derived relation's fingerprint (``ontology.project.derived_fingerprint``)
# and of a deriving source's watermark, so a change to it that derives the same records
# rebuilds nothing.
READER_STOPS: frozenset[tuple[str, str]] = frozenset({
    ("ontology.project", "derivation_code"),
    # likewise the earlier commit's value a proven-equivalent memo code keeps
    ("ontology.code_equivalence", "earlier_value"),
})

# What a sharded binding's shard prints are computed by.
SHARD_ROOTS: tuple[tuple[str, str | None], ...] = (
    ("ontology.project_incremental", "_shard_prints"),
)

# What decides which objects of an object-local binding a build rewrites and which of
# their report parts it keeps (``ontology.project_incremental._Objects``): part of each
# such binding's basis, so objects files of other code are never taken.
OBJECT_ROOTS: tuple[tuple[str, str | None], ...] = (
    ("ontology.project_incremental", "_Objects"),
    ("ontology.project_incremental", "_object_plan"),
    ("ontology.project_incremental", "_delta_objects"),
    ("ontology.project_incremental", "_object_parts"),
    ("ontology.project_incremental", "_delete_objects"),
    ("ontology.project_incremental", "_seed_delete"),
    ("ontology.project_incremental", "_identifier_rows"),
    ("ontology.project_incremental", "object_local"),
)

_ROOT = Path(__file__).resolve().parents[1]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------- code

# One digest's view of the tree: its packages and module paths, looked up once per walk
# (:func:`code_parts`), never kept past it, so a later walk sees every file change.
_WALK = threading.local()


def _packages(root: Path | None = None) -> frozenset[str]:
    root = root or _ROOT
    memo = getattr(_WALK, "packages", None)
    if memo is not None and root in memo:
        return memo[root]
    found = frozenset(p.name for p in root.iterdir() if (p / "__init__.py").is_file())
    if memo is not None:
        memo[root] = found
    return found


def _source_path(module: str, root: Path | None = None) -> Path | None:
    root = root or _ROOT
    memo = getattr(_WALK, "paths", None)
    if memo is not None and (root, module) in memo:
        return memo[(root, module)]
    found = None
    if module.split(".")[0] in _packages(root):
        base = root.joinpath(*module.split("."))
        for path in (base.with_suffix(".py"), base / "__init__.py"):
            if path.is_file():
                found = path
                break
    if memo is not None:
        memo[(root, module)] = found
    return found


def _strip_docstrings(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]


def _dump(node: Any) -> str:
    """A syntax tree as text, the same across Python versions for the same code (empty
    and absent fields left out; no positions)."""
    if isinstance(node, ast.AST):
        fields = []
        for name, value in ast.iter_fields(node):
            if value is None or value == [] or name in ("ctx", "type_comment", "kind"):
                continue
            fields.append(f"{name}={_dump(value)}")
        return f"{type(node).__name__}({','.join(fields)})"
    if isinstance(node, list):
        return "[" + ",".join(_dump(item) for item in node) + "]"
    return repr(node)


class _Module:
    """One repository module's top-level definitions and imports."""

    def __init__(self, name: str, path: Path, root: Path) -> None:
        self.name, self.root = name, root
        self.package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        _strip_docstrings(tree)
        self.defs: dict[str, list[ast.AST]] = {}
        self.imports: dict[str, tuple[str, str | None]] = {}
        self._said: dict[int, tuple[list[tuple[str, str, str | None]],
                                    list[tuple[str, str | None]]]] = {}
        self._text: dict[str, str] = {}
        self._top(tree.body)

    def _top(self, body: Iterable[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.defs.setdefault(node.name, []).append(node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for sub in ast.walk(target):
                        if isinstance(sub, ast.Name):
                            self.defs.setdefault(sub.id, []).append(node)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.imports[alias.asname] = (alias.name, None)
                    else:
                        top = alias.name.split(".")[0]
                        self.imports[top] = (top, None)
            elif isinstance(node, ast.ImportFrom):
                base = self.resolve(node)
                for alias in node.names:
                    if alias.name != "*":
                        self.imports[alias.asname or alias.name] = (base, alias.name)
            elif isinstance(node, (ast.If, ast.Try, ast.With)):
                for part in ("body", "orelse", "finalbody"):
                    self._top(getattr(node, part, []) or [])
                for handler in getattr(node, "handlers", []) or []:
                    self._top(handler.body)

    def resolve(self, node: ast.ImportFrom) -> str:
        if not node.level:
            return node.module or ""
        parts = self.package.split(".")
        parts = parts[:len(parts) - node.level + 1]
        return ".".join(parts + ([node.module] if node.module else []))

    def refs(self, nodes: Iterable[ast.AST]) -> set[tuple[str, str | None]]:
        """The (module, name) pairs ``nodes`` name; name None: a whole module (one named
        other than as ``module.attr``)."""
        out: set[tuple[str, str | None]] = set()
        for node in nodes:
            imports, names = self._names(node)
            local: dict[str, tuple[str, str | None]] = {}   # lazy imports in its body
            for alias, module, name in imports:
                local[alias] = _target(module, name, self.root) if name else (module, None)
            for used, attr in names:
                found = self._lookup(used, local)
                if found is None:
                    continue
                if attr is not None and found[1] is None:   # module.attr
                    out.add(_target(found[0], attr, self.root))
                else:
                    out.add(found)
        return out

    def _names(self, node: ast.AST) -> tuple[list[tuple[str, str, str | None]],
                                             list[tuple[str, str | None]]]:
        """What ``node`` says, read once per parse: its lazy imports (alias, module,
        name or None) in order, and each name it uses with the attribute taken of it."""
        held = self._said.get(id(node))
        if held is not None:
            return held
        imports: list[tuple[str, str, str | None]] = []
        attrs: dict[int, str] = {}
        names: list[tuple[str, str | None]] = []
        for sub in ast.walk(node):   # breadth first: an attribute before the name in it
            if isinstance(sub, ast.Name):
                names.append((sub.id, attrs.get(id(sub))))
            elif isinstance(sub, ast.ImportFrom):
                base = self.resolve(sub)
                for alias in sub.names:
                    if alias.name != "*":
                        imports.append((alias.asname or alias.name, base, alias.name))
            elif isinstance(sub, ast.Import):
                for alias in sub.names:
                    if alias.asname:
                        imports.append((alias.asname, alias.name, None))
                    else:
                        top = alias.name.split(".")[0]
                        imports.append((top, top, None))
            elif isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
                attrs[id(sub.value)] = sub.attr
        held = self._said[id(node)] = (imports, names)
        return held

    def text(self, name: str) -> str:
        """The syntax tree text of the definitions named ``name``."""
        held = self._text.get(name)
        if held is None:
            held = self._text[name] = "\n".join(_dump(node) for node in self.defs[name])
        return held

    def _lookup(self, name: str, local: Mapping[str, tuple[str, str | None]]
                ) -> tuple[str, str | None] | None:
        if name in local:
            return local[name]
        if name in self.defs:
            return (self.name, name)
        if name in self.imports:
            return _target(*self.imports[name], self.root)
        return None


def _target(module: str, name: str | None, root: Path) -> tuple[str, str | None]:
    """``from module import name`` names a submodule or a definition."""
    if name is not None and _source_path(f"{module}.{name}", root) is not None:
        return (f"{module}.{name}", None)
    return (module, name)


_MODULES: dict[tuple[str, int, int], _Module] = {}


def _module(name: str, root: Path) -> _Module | None:
    path = _source_path(name, root)
    if path is None:
        return None
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key not in _MODULES:
        _MODULES[key] = _Module(name, path, root)
    return _MODULES[key]


class _Unreadable(Exception):
    pass


def code_digest(roots: Iterable[tuple[str, str | None]],
                stops: Iterable[tuple[str, str]] = (), *, root: Path | None = None
                ) -> str | None:
    """Digest of the definitions ``roots`` reach (module docstring): (module, name), name
    None for a whole module; ``stops`` are neither digested nor followed. None when a
    repository module among them cannot be read. ``root``: the repository tree to read
    (another commit's, extracted); default this one."""
    parts = code_parts(roots, stops, root=root)
    if parts is None:
        return None
    digest = hashlib.sha256()
    for (module_name, one), text in sorted(parts.items()):
        digest.update(f"{module_name}:{one}\n{text}\n".encode("utf-8"))
    return digest.hexdigest()


def code_parts(roots: Iterable[tuple[str, str | None]],
               stops: Iterable[tuple[str, str]] = (), *, root: Path | None = None
               ) -> dict[tuple[str, str], str] | None:
    """(module, name) -> the syntax tree text of each definition ``roots`` reach, what
    :func:`code_digest` digests; None when a repository module among them cannot be
    read."""
    if getattr(_WALK, "paths", None) is not None:   # nested in a walk: its view
        return _code_parts(roots, stops, Path(root) if root is not None else _ROOT)
    _WALK.packages, _WALK.paths = {}, {}
    try:
        return _code_parts(roots, stops, Path(root) if root is not None else _ROOT)
    finally:
        _WALK.packages = _WALK.paths = None


def _code_parts(roots: Iterable[tuple[str, str | None]], stops: Iterable[tuple[str, str]],
                root: Path) -> dict[tuple[str, str], str] | None:
    seen: set[tuple[str, str | None]] = set(stops)
    pending = list(roots)
    parts: dict[tuple[str, str], str] = {}
    try:
        while pending:
            module_name, name = pending.pop()
            if (module_name, name) in seen:
                continue
            seen.add((module_name, name))
            if module_name.split(".")[0] not in _packages(root):
                continue   # the standard library, an installed one
            try:
                module = _module(module_name, root)
            except (OSError, SyntaxError, ValueError) as exc:
                raise _Unreadable(module_name) from exc
            if module is None:   # a repository module that is not there
                raise _Unreadable(module_name)
            names = sorted(module.defs) if name is None else [name]
            for one in names:
                if (module_name, one) in seen and one != name:
                    continue
                nodes = module.defs.get(one)
                if nodes is None:
                    if one in module.imports:   # a name the module re-exports
                        pending.append(_target(*module.imports[one], root))
                    continue
                parts[(module_name, one)] = module.text(one)
                pending.extend(module.refs(nodes) - seen)
    except _Unreadable:
        return None
    return parts


def rows_code_digest(root: Path | None = None) -> str | None:
    """Digest of the code that writes a unit's rows (``ROWS_CODE_ROOTS``)."""
    return code_digest(ROWS_CODE_ROOTS, ROWS_CODE_STOPS, root=root)


def reader_digest(source: Any, root: Path | None = None) -> str | None:
    """Digest of the code a source's records are read and fingerprinted with: its class
    (and what it reaches: the citations, audiences, keys and removals it gives its
    records, the scopes it says stand in) and ``FINGERPRINT_ROOTS``. Part of each of its
    relations' fingerprints (:func:`with_reader`), so a change to it rebuilds the units
    reading them. A class of a module outside the repository's packages (a script's, one
    of a namespace directory such as ``scripts``): the bytes of its whole file with the
    fingerprint code, of this tree only. None (never equal) for a class whose source
    cannot be read."""
    cls = type(source)
    module = getattr(cls, "__module__", "") or ""
    qualname = getattr(cls, "__qualname__", "") or ""
    if not qualname:
        return None
    if _source_path(module, root) is None:
        return _script_reader_digest(module) if root is None else None
    # a class defined inside another definition: that whole top-level definition
    return code_digest(_reader_roots(module, qualname), READER_STOPS, root=root)


def _script_reader_digest(module: str) -> str | None:
    """A reader class's whole file (a module the closure does not follow) with the
    fingerprint code (``FINGERPRINT_ROOTS``); None when either cannot be read."""
    import sys
    path = getattr(sys.modules.get(module), "__file__", None)
    fingerprints = code_digest(FINGERPRINT_ROOTS, READER_STOPS)
    if not path or fingerprints is None:
        return None
    try:
        text = Path(path).read_bytes()
    except OSError:
        return None
    return _sha(f"file\x00{module}\x00{hashlib.sha256(text).hexdigest()}\x00{fingerprints}")


def _reader_roots(module: str, qualname: str) -> list[tuple[str, str | None]]:
    return [(module, qualname.split(".")[0]), *FINGERPRINT_ROOTS]


def reader_parts(source: Any, root: Path | None = None) -> dict[tuple[str, str], str] | None:
    """The definitions :func:`reader_digest` digests (:func:`code_parts`); None where it
    gives no digest."""
    cls = type(source)
    module = getattr(cls, "__module__", "") or ""
    qualname = getattr(cls, "__qualname__", "") or ""
    if not qualname or _source_path(module, root) is None:
        return None
    return code_parts(_reader_roots(module, qualname), READER_STOPS, root=root)


def with_reader(print_: str | None, reader: str | None) -> str | None:
    """A relation's fingerprint with the code of the source reading it (None: not
    captured)."""
    if print_ is None:
        return None
    return _sha(f"{print_}\x00reader\x00{reader}")


def shard_code_digest(root: Path | None = None) -> str | None:
    return code_digest(SHARD_ROOTS, root=root)


def object_code_digest(root: Path | None = None) -> str | None:
    return code_digest(OBJECT_ROOTS, root=root)


def normalizer_parts(normalizer: Any, root: Path | None = None
                     ) -> tuple[str | None, str | None]:
    """(digest of a normalizer's code: its function and what it reaches, digest of the
    identifier grammar's data it reads: ``ontology/schema/identifiers.json``); None for
    a part that cannot be read."""
    root = Path(root) if root is not None else _ROOT
    module = getattr(normalizer, "__module__", "") or ""
    qualname = getattr(normalizer, "__qualname__", "") or ""
    code = code_digest([(module, qualname)], root=root) if "." not in qualname else None
    data = root / "ontology" / "schema" / "identifiers.json"
    grammar = hashlib.sha256(data.read_bytes()).hexdigest() if data.is_file() else None
    return code, grammar


def normalizer_digest(normalizer: Any) -> str | None:
    """Digest of a normalizer's code and the identifier grammar's data
    (:func:`normalizer_parts`); one whose code cannot be read matches no other build."""
    if normalizer is None:
        return None
    code, grammar = normalizer_parts(normalizer)
    if code is None:
        code = (f"unreadable:{getattr(normalizer, '__module__', '')}."
                f"{getattr(normalizer, '__qualname__', '')}:{os.getpid()}:{id(normalizer)}")
    return _sha(_canonical({"code": code, "identifiers": grammar}))


# --------------------------------------------------------------------------- schema

def _get(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


# The relations ``ontology.project.ETL_REINSERTED`` collapsed in every generation built
# under this key scheme: their rows never depended on the list. A relation added to it
# later enters the key of every unit reading it (``unit_slice``), so those units are
# projected again with its copies collapsed, and no other unit is.
_ETL_KEYED = frozenset(("portal_api", relation) for relation in (
    "public.app_assembly_order_damage", "public.app_assembly_order_status",
    "public.app_batch_schedule", "public.app_manufacturer",
    "public.app_manufacturing_order_line", "public.app_production_task"))


class SchemaParts:
    """Digests of the parts of a compiled schema a unit's rows can read, and the
    indexes :func:`unit_slice` picks them with."""

    def __init__(self, schema: Any) -> None:
        types = dict(_get(schema, "object_types") or {})
        links = dict(_get(schema, "link_types") or {})
        events = dict(_get(schema, "event_types") or {})
        self.types = {tid: _sha(_canonical(t)) for tid, t in types.items()}
        self.links = {lid: _sha(_canonical(link)) for lid, link in links.items()}
        self.events = {eid: _sha(_canonical(ev)) for eid, ev in events.items()}
        self.binding_type = {b["id"]: tid for tid, t in types.items()
                             for b in t.get("bindings", [])}
        # binding -> the minted columns its relation's ETL copies differ in, for a relation
        # collapsed after every generation of this key scheme (``_ETL_KEYED``)
        from ontology.project import ETL_REINSERTED
        self.etl = {b["id"]: sorted(ETL_REINSERTED[b["source"]][b["relation"]])
                    for t in types.values() for b in t.get("bindings", [])
                    if b.get("relation") in ETL_REINSERTED.get(b.get("source"), {})
                    and (b.get("source"), b.get("relation")) not in _ETL_KEYED}
        self.events_of: dict[str, set[str]] = {}
        for eid, ev in events.items():
            self.events_of.setdefault(ev.get("object_type"), set()).add(eid)
        # link types a type names anywhere in its declaration
        self.named: dict[str, set[str]] = {
            tid: {s for s in _strings(t) if s in links} for tid, t in types.items()}
        for eid, ev in events.items():   # a qualifier's link type, by the event's type
            self.named.setdefault(ev.get("object_type"), set()).update(
                s for s in _strings(ev) if s in links)
        self.touching: dict[str, set[str]] = {}
        for lid, link in links.items():
            for lb in link.get("bindings", []):
                for end in (lb.get("from_binding"), lb.get("to_binding"),
                            (lb.get("via") or {}).get("binding")):
                    if end is not None:
                        self.touching.setdefault(end, set()).add(lid)

    def shape(self) -> dict[str, Any]:
        """What a base generation must share with this schema for its rows to be
        deleted and rewritten unit by unit: each binding's type and each event type."""
        return {"bindings": dict(sorted(self.binding_type.items())),
                "events": dict(sorted(self.events.items()))}


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def unit_slice(parts: SchemaParts, unit: str, deps: Iterable[str]) -> str:
    """Digest of the schema a unit's rows can read (module docstring)."""
    deps = set(deps)
    tids = {parts.binding_type[b] for b in deps if b in parts.binding_type}
    lids: set[str] = set()
    for b in deps:
        lids |= parts.touching.get(b, set())
    for tid in tids:
        lids |= parts.named.get(tid, set())
    eids: set[str] = set()
    for tid in tids:
        eids |= parts.events_of.get(tid, set())
    if unit.startswith("l:"):   # its own link type
        own = unit[2:].split("|")[0].split("#")[0]
        lids |= {own} if own in parts.links else set(parts.links)
    unknown = sorted(b for b in deps if b not in parts.binding_type)
    sliced = {
        "types": {t: parts.types[t] for t in sorted(tids)},
        "links": {lid: parts.links[lid] for lid in sorted(lids) if lid in parts.links},
        "events": {e: parts.events[e] for e in sorted(eids)},
        "unknown": unknown}
    etl = {b: parts.etl[b] for b in sorted(deps) if b in parts.etl}
    if etl:   # a relation collapsed since: the units reading it are projected again
        sliced["etl"] = etl
    return _sha(_canonical(sliced))


__all__ = ["FINGERPRINT_ROOTS", "KEY_SCHEME", "PROJECTION_ROWS_VERSION", "ROWS_CODE_DIGESTS",
           "ROWS_CODE_ROOTS",
           "SHARD_ROOTS", "SchemaParts", "code_digest", "code_parts", "normalizer_digest",
           "normalizer_parts",
           "reader_digest", "reader_parts",
           "rows_code_digest", "shard_code_digest", "unit_slice", "with_reader"]
