"""The build steps' code digest (``ontology.step_migrate.steps_digest``) from each step's
code files, a table of data (``ontology/step_code.json``) ``step_migrate.STEPS`` reads its
specs' code from: a build records its steps' code (``project.build_store``'s
``steps_digest`` meta) without importing the steps or the tool layer they call. Standard
library only at import (``ontology.build_keys`` is imported when a digest is taken).

Two schemes, chosen by the table a commit holds:

* ``plx-step-code/v1`` (a ``steps`` table only): a step's digest is its code files' bytes,
  so a comment, a docstring or a function no step calls moves it and re-runs the step.
* ``plx-step-code/v2`` (``steps`` and ``roots``, step -> the ``[module, name]`` the build
  registry ``operating.build_steps.post_project`` runs): a step's digest is its
  definitions. For each listed ``.py`` file, the syntax tree text (docstrings stripped, no
  comments, no positions: ``build_keys.code_parts``) of the definitions in it the step's
  roots reach, of its top-level statements that define nothing (they run on import; an
  assignment to an attribute or item, ``mod.f = g``, patches another module and is kept),
  and of its import table (each top-level import in source order, a relative one resolved:
  a reached definition names an import's binding, not its target, so a swap of
  ``from x import f`` to ``from y import f`` moves it). A listed module the roots reach
  nothing in, or one that names its own definitions at run time (``globals()``,
  ``vars()``, ``sys.modules``, an import of ``__name__``), is taken whole (its every
  definition), so a dynamic reach is never dropped. A listed ``.json`` file is its JSON
  read and written back compactly, key order kept (a table "in schema order" is ordered
  data); any other file its bytes; a file that is not there ``"absent"``. A module that cannot be read
  raises: never a guessed digest.

A commit's own build records its own scheme (:func:`code_at`); ``step_migrate.plan``
compares a served commit's steps under this tree's scheme on both sides and keeps the
served commit's own digest as the plan's ``steps_from`` (the transition from v1 is one
step migration that runs nothing)."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat as _stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, Mapping, Sequence

from ontology import tmp_root

ROOT = Path(__file__).resolve().parents[1]
CODE_FILE = "ontology/step_code.json"
FORMAT_V1 = "plx-step-code/v1"
FORMAT_V2 = "plx-step-code/v2"

Roots = Mapping[str, Sequence[Sequence[Any]]]


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def _read(path: str, ref: str | None, repo: Path) -> bytes | None:
    if ref is None:
        file = repo / path
        return file.read_bytes() if file.is_file() else None
    done = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{path}"],
                          capture_output=True)
    return done.stdout if done.returncode == 0 else None


def _byte_digests(ref: str | None, repo: Path,
                  files: Mapping[str, Sequence[str]]) -> dict[str, str]:
    cache: dict[str, str] = {}
    out = {}
    for name, paths in sorted(files.items()):
        parts = []
        for path in paths:
            if path not in cache:
                data = _read(path, ref, repo)
                cache[path] = "absent" if data is None else hashlib.sha256(data).hexdigest()
            parts.append([path, cache[path]])
        out[name] = _sha(parts)
    return out


def _module_name(path: str) -> str:
    """``ontology/mentions.py`` -> ``ontology.mentions`` (a package's ``__init__.py``: the
    package)."""
    parts = path[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _patches(node: ast.stmt) -> bool:
    """An assignment storing into an attribute or an item (``mod.f = g``, ``REG[k] = f``):
    it changes what another module (or a table) holds on import, which no definition's
    text says, so it is kept as a top-level statement."""
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return any(isinstance(sub, (ast.Attribute, ast.Subscript))
               for target in targets for sub in ast.walk(target))


def _top_statements(tree: ast.Module) -> str:
    """The syntax tree text of a module's top-level statements that define nothing (not a
    definition, a plain name assignment, an import or the module docstring; an assignment
    to an attribute or item is kept, :func:`_patches`; the bodies of top-level
    ``if``/``try``/``with`` read the same way): what runs on import beside definitions."""
    from ontology.build_keys import _dump
    body = list(tree.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    kept: list[str] = []

    def walk(nodes: Sequence[ast.stmt]) -> None:
        for node in nodes:
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) \
                    and not _patches(node):
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                 ast.Import, ast.ImportFrom)):
                continue
            if isinstance(node, (ast.If, ast.Try, ast.With)):
                kept.append(type(node).__name__ + ":" + _dump(
                    getattr(node, "test", None) or getattr(node, "items", None) or []))
                for part in ("body", "orelse", "finalbody"):
                    walk(getattr(node, part, []) or [])
                for handler in getattr(node, "handlers", []) or []:
                    kept.append("except:" + _dump(handler.type or []))
                    walk(handler.body)
                continue
            kept.append(_dump(node))
    walk(body)
    return "\n".join(kept)


def _import_table(tree: ast.Module, module: Any) -> list[list[Any]]:
    """Each top-level import of a module (in ``if``/``try``/``with`` bodies too), in source
    order: ``["import", name, asname]`` or ``["from", resolved module, name, asname]``
    (``module``: its ``build_keys._Module``, which resolves a relative import)."""
    table: list[list[Any]] = []

    def walk(nodes: Sequence[ast.stmt]) -> None:
        for node in nodes:
            if isinstance(node, ast.Import):
                table.extend(["import", a.name, a.asname] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = module.resolve(node)
                table.extend(["from", base, a.name, a.asname] for a in node.names)
            elif isinstance(node, (ast.If, ast.Try, ast.With)):
                for part in ("body", "orelse", "finalbody"):
                    walk(getattr(node, part, []) or [])
                for handler in getattr(node, "handlers", []) or []:
                    walk(handler.body)
    walk(tree.body)
    return table


_IMPORTERS = frozenset({"import_module", "__import__", "reload"})


def _dynamic(tree: ast.Module) -> bool:
    """Whether a module names its own definitions at run time: ``globals()``, ``vars()``,
    ``sys.modules`` (or ``from sys import modules``), or an import of ``__name__``
    (``importlib.import_module(__name__)``): a reach no definition's text shows."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else \
                func.attr if isinstance(func, ast.Attribute) else None
            if name in ("globals", "vars"):
                return True
            if name in _IMPORTERS and any(isinstance(sub, ast.Name) and sub.id == "__name__"
                                          for arg in node.args for sub in ast.walk(arg)):
                return True
        elif isinstance(node, ast.Attribute) and node.attr == "modules" \
                and isinstance(node.value, ast.Name) and node.value.id == "sys":
            return True
        elif isinstance(node, ast.ImportFrom) and node.module == "sys" \
                and any(a.name in ("modules", "*") for a in node.names):
            return True
    return False


class _Tree:
    """The repository tree a definition digest reads: ``repo`` itself (``ref`` None) or
    ``ref``'s Python packages extracted to a temporary directory (``git archive``)."""

    def __init__(self, ref: str | None, repo: Path, listed: Sequence[str] = ()) -> None:
        self.ref, self.repo, self._temp, self._listed = ref, repo, None, sorted(set(listed))
        self.path = repo
        # path -> (its _Module, top statements, import table, dynamic): read once per tree
        self.modules: dict[str, tuple[Any, str, list[list[Any]], bool]] = {}
        if ref is not None:
            self._temp = tmp_root.mkdtemp(prefix=".step-code-")
            self.path = Path(self._temp)
            self._extract()

    def _git(self, *args: str) -> bytes:
        done = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True)
        if done.returncode != 0:
            raise ValueError(f"steps_unreadable:{self.ref}")
        return done.stdout

    def _extract(self) -> None:
        top = [n for n in self._git("ls-tree", "--name-only", "-z", self.ref).decode()
               .split("\0") if n]
        inits = [n for n in self._git("ls-tree", "--name-only", "-z", self.ref, "--",
                                      *(f"{d}/__init__.py" for d in top)).decode()
                 .split("\0") if n]
        packages = sorted(n.split("/")[0] for n in inits)
        # and each listed .py file there outside a package (read whole, never reached)
        listed = [n for n in self._git("ls-tree", "--name-only", "-z", self.ref, "--",
                                       *(p for p in self._listed
                                         if p.endswith(".py"))).decode().split("\0")
                  if n and n.split("/")[0] not in packages] if self._listed else []
        if not packages and not listed:
            return
        data = self._git("archive", "--format=tar", self.ref, "--",
                         *(f":(glob){p}/**/*.py" for p in packages),
                         *(f":(literal){p}" for p in listed))
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            members = [m for m in tar.getmembers()
                       if m.isfile() and not m.name.startswith(("/", ".."))
                       and ".." not in Path(m.name).parts]
            try:
                tar.extractall(self.path, members=members, filter="data")
            except TypeError:   # a Python before extraction filters
                tar.extractall(self.path, members=members)

    def read(self, path: str) -> bytes | None:
        file = self.path / path
        return file.read_bytes() if file.is_file() else None

    def close(self) -> None:
        if self._temp is not None:
            from ontology import build_keys
            prefix = str(self.path) + "/"
            for key in [k for k in build_keys._MODULES if k[0].startswith(prefix)]:
                del build_keys._MODULES[key]
            shutil.rmtree(self._temp, ignore_errors=True)
            self._temp = None


def _parts(roots: Sequence[tuple[str, str | None]], tree: _Tree, what: str
           ) -> dict[tuple[str, str], str]:
    from ontology.build_keys import code_parts
    parts = code_parts(roots, root=tree.path)
    if parts is None:
        raise ValueError(f"step_code_unreadable:{tree.ref or 'tree'}:{what}")
    return parts


def _file_digest(path: str, tree: _Tree, reached: Mapping[tuple[str, str], str],
                 ref: str | None) -> str:
    if path.endswith(".py"):
        source = tree.read(path)
        if source is None:
            return "absent"
        module = _module_name(path)
        try:
            if path not in tree.modules:
                from ontology.build_keys import _Module
                whole = _Module(module, tree.path / path, tree.path)
                parsed = ast.parse(source)
                tree.modules[path] = (whole, _top_statements(parsed),
                                      _import_table(parsed, whole), _dynamic(parsed))
            whole, top, imports, dynamic = tree.modules[path]
            mine = {k: v for k, v in reached.items() if k[0] == module}
            if not mine or dynamic:   # reached nothing, or reached at run time: every definition
                mine = {(module, name): whole.text(name) for name in whole.defs}
        except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
            raise ValueError(f"step_code_unreadable:{ref or 'tree'}:{path}") from None
        return _sha({"definitions": sorted([k[1], v] for k, v in mine.items()),
                     "top": top, "imports": imports})
    data = _read(path, ref, tree.repo) if ref is not None else tree.read(path)
    if data is None:
        return "absent"
    if path.endswith(".json"):
        try:   # key order kept (sort_keys would hide a reordered table)
            text = json.dumps(json.loads(data), separators=(",", ":"), ensure_ascii=False)
        except ValueError:
            raise ValueError(f"step_code_unreadable:{ref or 'tree'}:{path}") from None
        return hashlib.sha256(("json:" + text).encode()).hexdigest()
    return hashlib.sha256(data).hexdigest()


def _definition_digests(ref: str | None, repo: Path, files: Mapping[str, Sequence[str]],
                        roots: Roots) -> dict[str, str]:
    tree = _Tree(ref, repo, [path for paths in files.values() for path in paths])
    try:
        out = {}
        for name, paths in sorted(files.items()):
            # a root in a module the tree does not hold reaches nothing (its listed file
            # is "absent"); any other unreadable module raises
            mine = [(str(m), None if n is None else str(n)) for m, n in roots.get(name, ())
                    if tree.read(str(m).replace(".", "/") + ".py") is not None
                    or tree.read(str(m).replace(".", "/") + "/__init__.py") is not None]
            reached = _parts(mine, tree, name) if mine else {}
            out[name] = _sha([FORMAT_V2, [[path, _file_digest(path, tree, reached, ref)]
                                          for path in paths]])
        return out
    finally:
        tree.close()


def code_digests(ref: str | None, repo: str | Path,
                 files: Mapping[str, Sequence[str]],
                 roots: Roots | None = None) -> dict[str, str]:
    """step -> digest of its code files (``files``: step -> repository paths) at ``ref``
    (None: the tree at ``repo``): their bytes (``roots`` None: the v1 scheme) or the
    definitions ``roots`` (step -> ``(module, name)`` pairs; a step without any: each
    listed module whole) reach in them (the v2 scheme, module docstring)."""
    if roots is None:
        return _byte_digests(ref, Path(repo), files)
    if ref is not None:
        return _definition_digests(ref, Path(repo), files, roots)
    # This tree's v2 digests take seconds (every reached module parsed) and a process asks
    # again on each build (``build_store`` records them), resolve and overlay: kept per
    # process, keyed by the inputs and the bytes of every file a digest can read.
    repo = Path(repo)
    key = (str(repo.resolve()),
           tuple(sorted((str(s), tuple(map(str, p))) for s, p in files.items())),
           tuple(sorted((str(s), tuple(map(tuple, p))) for s, p in roots.items())),
           _tree_stamp(repo, files))
    if key not in _TREE_DIGESTS:
        if len(_TREE_DIGESTS) >= 8:
            _TREE_DIGESTS.clear()
        # and across processes (each new host derives them on its first find): a file per
        # key in this user's own cache directory, the same key's value whoever derived it
        name = hashlib.sha256(repr((FORMAT_V2, sys.version_info[:3], key)).encode()
                              ).hexdigest() + ".json"
        kept = _cached(name)
        if kept is None:
            kept = _definition_digests(None, repo, files, roots)
            _keep(name, kept)
        _TREE_DIGESTS[key] = kept
    return dict(_TREE_DIGESTS[key])


_TREE_DIGESTS: dict[tuple[Any, ...], dict[str, str]] = {}


def _cache_dir() -> Path | None:
    """This user's step-code cache (``PLX_STEP_CODE_CACHE``, else ``plx-step-code-<uid>`` in
    the temporary directory): a directory it owns that no one else may write, else None
    (nothing kept, nothing read)."""
    raw = os.environ.get("PLX_STEP_CODE_CACHE")
    path = Path(raw) if raw else Path(tempfile.gettempdir()) / f"plx-step-code-{os.getuid()}"
    try:
        path.mkdir(mode=0o700, parents=bool(raw), exist_ok=True)
        st = os.lstat(path)
    except OSError:
        return None
    if not _stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
        return None
    return path


def _cached(name: str) -> dict[str, str] | None:
    folder = _cache_dir()
    if folder is None:
        return None
    try:
        st = os.lstat(folder / name)
        if not _stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
            return None
        value = json.loads((folder / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(value, dict) and value and all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        return value
    return None


def _keep(name: str, value: Mapping[str, str]) -> None:
    folder = _cache_dir()
    if folder is None:
        return
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".keep-")
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(dict(value), out, sort_keys=True)
        os.replace(tmp, folder / name)
        tmp = None
    except OSError:
        pass
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _tree_stamp(repo: Path, files: Mapping[str, Sequence[str]]) -> str:
    """sha256 of the bytes of every ``.py`` file of ``repo``'s packages (each top directory
    holding an ``__init__.py``, as ``_Tree`` extracts a ref) and of every listed file: what a
    definition digest of the tree can read. Its paths too, so a file added or removed counts."""
    h = hashlib.sha256()

    def add(path: str) -> None:
        try:
            data = (repo / path).read_bytes()
        except OSError:
            data = None
        h.update(path.encode() + b"\0" + (b"-" if data is None
                                         else hashlib.sha256(data).digest()))

    with os.scandir(repo) as entries:
        tops = sorted(e.name for e in entries
                      if e.is_dir() and (repo / e.name / "__init__.py").is_file())
    for top in tops:
        for base, dirs, names in os.walk(repo / top):
            dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", "node_modules"))
            for name in sorted(names):
                if name.endswith(".py"):
                    add(str(Path(base, name).relative_to(repo)))
    for path in sorted({str(p) for paths in files.values() for p in paths}):
        add(path)
    return h.hexdigest()


def _table(ref: str | None, repo: Path) -> dict[str, Any] | None:
    data = _read(CODE_FILE, ref, repo)
    if data is None:
        return None
    try:
        table = json.loads(data)
    except ValueError:
        raise ValueError(f"steps_unreadable:{ref}") from None
    if not isinstance(table, dict):
        raise ValueError(f"steps_unreadable:{ref}")
    return table


def step_files(ref: str | None = None, repo: str | Path = ROOT
               ) -> dict[str, tuple[str, ...]] | None:
    """step -> its code files, from ``ref``'s ``ontology/step_code.json`` (None: this tree);
    None when ``ref`` has no such table (``step_migrate.code_at`` then reads that commit's
    ``STEPS`` as written). A table that is not step -> list of paths raises (never a
    guessed digest)."""
    table = _table(ref, Path(repo))
    if table is None:
        return None
    try:
        files = {str(name): tuple(paths) for name, paths in table["steps"].items()}
    except (KeyError, TypeError, AttributeError):
        raise ValueError(f"steps_unreadable:{ref}") from None
    if not all(isinstance(path, str) and path for paths in files.values() for path in paths):
        raise ValueError(f"steps_unreadable:{ref}")
    return files


def step_roots(ref: str | None = None, repo: str | Path = ROOT
               ) -> dict[str, tuple[tuple[str, str | None], ...]] | None:
    """step -> the ``(module, name)`` definitions the build runs for it, from ``ref``'s
    table (None: this tree); None for a v1 table or none (the byte scheme). A ``roots``
    entry that is not step -> list of ``[module, name]`` raises."""
    table = _table(ref, Path(repo))
    if table is None or table.get("format") != FORMAT_V2:
        if table is not None and "roots" in table:
            raise ValueError(f"steps_unreadable:{ref}")
        return None
    try:
        raw = table["roots"]
        roots = {str(step): tuple((m, n) for m, n in pairs) for step, pairs in raw.items()}
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ValueError(f"steps_unreadable:{ref}") from None
    if not all(isinstance(m, str) and m and (n is None or (isinstance(n, str) and n))
               for pairs in roots.values() for m, n in pairs):
        raise ValueError(f"steps_unreadable:{ref}")
    return roots


def code_at(ref: str | None, repo: str | Path = ROOT) -> dict[str, str] | None:
    """step -> digest of its code at ``ref`` as ``ref``'s own table lists and roots it
    (the digests that commit's build records); None: ``ref`` has no table."""
    files = step_files(ref, repo)
    if files is None:
        return None
    return code_digests(ref, repo, files, step_roots(ref, repo))


def steps_digest_of(digests: Mapping[str, str]) -> str:
    """The steps digest of step -> code digest (``step_migrate.steps_digest``)."""
    return "steps:" + _sha(dict(digests))[:32]


def tree_steps_digest(repo: str | Path = ROOT) -> str:
    """This tree's steps digest (``step_migrate.steps_digest()``, whose ``STEPS`` read their
    code files and roots from the same table)."""
    digests = code_at(None, repo)
    if digests is None:
        raise ValueError("steps_unreadable:tree")
    return steps_digest_of(digests)


__all__ = ["code_at", "code_digests", "step_files", "step_roots", "steps_digest_of",
           "tree_steps_digest"]
