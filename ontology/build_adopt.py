"""Adopting a generation built before per-unit keys (``ontology.build_keys``), so the
first build after them can be incremental instead of cold.

A generation built under the earlier keys (``units.json`` without ``scheme``) keyed every
unit by one context (the schema digest, the cut, the normalizer and the bytes of whole
code files) and its fingerprints. Given the commit it was built by (``PLX_BUILD_BASE_CODE_SHA``
or ``units.json`` ``code_sha``), :func:`prepare` reads that commit's tree (``git archive``
into the state directory, removed afterwards) and adopts the generation only when every
part is proven, never guessed:

* the earlier context recomputed from that tree, the generation's own schema digest and
  this build's cut, normalizer and sources is the one the generation stored (so the commit,
  schema, cut, normalizer and source code are the ones it was built with);
* the schema compiled by that tree (in a child process: that commit's compiler) has the
  digest the generation stored;
* the code that writes rows at that commit is code ``PROJECTION_ROWS_VERSION`` names
  (``build_keys.ROWS_CODE_DIGESTS``), and its identifier normalizer (code and the
  grammar data it reads, ``ontology/schema/identifiers.json``) is this build's.

Then a unit of the generation is kept (:meth:`Adoption.keys`) when its earlier key,
recomputed with this build's fingerprints and its dependencies under the generation's
schema, is the one stored, and its schema slice and dependencies are this schema's: its
rows are what this code writes for these captures, and every source it reads is read
by the same code there and here (``build_keys.reader_digest``: what a reader gives its
records is not in the captured bytes). Every other unit is projected again.
The fingerprints the generation held (watermark prints, shard prints) are held again
when the code that read them at that commit is this build's (``build_keys``
``reader_digest``, ``shard_code_digest``). Anything missing, unreadable or unequal: no
adoption (the build is cold, as before).

Stdlib only.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Callable, Mapping

from ontology import build_keys, code_equivalence
from ontology.store import canonical

ENV = "PLX_BUILD_BASE_CODE_SHA"

# The modules whose bytes the earlier keys digested (``project_incremental._CODE`` up to
# commit 39cf8a190c), with each source's module and the normalizer's.
LEGACY_CODE = ("ontology.project", "ontology.project_incremental", "ontology.store",
               "ontology.store_ddl", "ontology.units", "ontology.schema",
               "ontology.identifiers", "integrations.source_mirror")
_GIT_SECONDS = 120
_SCHEMA_SECONDS = 600

# A child reading the commit's compiled schema: its types, link and event types, digest.
_SCHEMA_SCRIPT = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from ontology.schema import load_schema\n"
    "s = load_schema()\n"
    "json.dump({'digest': s.digest, 'object_types': s.object_types, "
    "'link_types': s.link_types, 'event_types': s.event_types}, sys.stdout)\n")


class NotAdopted(Exception):
    """Why a generation is not adopted (the build is cold)."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def code_sha(units: Mapping[str, Any]) -> str | None:
    """The commit a generation was built by, as the operator gave it."""
    given = os.environ.get(ENV) or units.get("code_sha")
    return given.strip() if isinstance(given, str) and given.strip() else None


def _git(repo: Path, *args: str) -> bytes:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          timeout=_GIT_SECONDS, check=False)
    if done.returncode != 0:
        raise NotAdopted(f"git_{args[0]}_failed")
    return done.stdout


def materialize(sha: str, into: Path, repo: Path | None = None) -> Path:
    """The repository's packages at commit ``sha``, extracted into ``into``."""
    repo = repo or build_keys._ROOT
    full = _git(repo, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}").decode().strip()
    names = _git(repo, "ls-tree", "--name-only", "-d", full).decode().split()
    packages = [name for name in names
                if subprocess.run(["git", "-C", str(repo), "cat-file", "-e",
                                   f"{full}:{name}/__init__.py"], capture_output=True,
                                  timeout=_GIT_SECONDS, check=False).returncode == 0]
    if "ontology" not in packages:
        raise NotAdopted("no_ontology_package")
    into.mkdir(parents=True, exist_ok=False)
    archive = _git(repo, "archive", "--format=tar", full, *packages)
    import io
    import tarfile
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            if not (member.isfile() or member.isdir()) or member.name.startswith("/") \
                    or ".." in Path(member.name).parts:
                raise NotAdopted("unexpected_archive_member")
        tar.extractall(into)
    return into


def legacy_code_digest(names: list[str], tree: Path) -> str:
    """``project_incremental._code_digest`` of the earlier keys, over ``tree``."""
    packages = {p.name for p in tree.iterdir() if (p / "__init__.py").is_file()}
    digest = hashlib.sha256()
    for name in sorted(set(names)):
        digest.update(f"{name}\n".encode())
        if name and name.split(".")[0] in packages:
            base = tree.joinpath(*name.split("."))
            path = next((p for p in (base.with_suffix(".py"), base / "__init__.py")
                         if p.is_file()), None)
            if path is None:
                raise NotAdopted(f"module_missing:{name}")
            digest.update(path.read_bytes())
            continue
        if name and name.split(".")[0] in build_keys._packages():
            raise NotAdopted(f"module_missing:{name}")   # a repository module not there
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            spec = None
        origin = getattr(spec, "origin", None)
        if origin and os.path.isfile(origin):
            digest.update(Path(origin).read_bytes())
    return digest.hexdigest()


def legacy_context(*, tree: Path, schema_digest: str, cut: str | None, normalizer: Any,
                   sources: Mapping[str, Any], fmt: str) -> str:
    """The context of the earlier keys (``project_incremental._build`` up to 39cf8a190c)."""
    return _sha(canonical({
        "format": fmt, "schema": schema_digest, "as_known_at": cut,
        "normalizer": None if normalizer is None else
        f"{getattr(normalizer, '__module__', '')}.{getattr(normalizer, '__qualname__', '')}",
        "code": legacy_code_digest(list(LEGACY_CODE) + [type(s).__module__
                                                        for s in sources.values()]
                                   + [getattr(normalizer, "__module__", "")], tree)}))


def legacy_keys(context: str, units: Mapping[str, frozenset[str]],
                fingerprints: Mapping[str, Any], sharded: Mapping[str, Any]
                ) -> dict[str, str]:
    """The earlier keys of ``units`` (a plan's, cut into ``sharded``'s shards)."""
    keys = {unit: _sha(context + canonical({b: fingerprints.get(b) for b in sorted(deps)}))
            for unit, deps in units.items()}
    for bid, shards in sharded.items():
        keys[f"b:{bid}"] = _sha(context + canonical(
            {bid: fingerprints.get(bid), "shards": shards["shards"]}))
        for k, print_ in enumerate(shards["prints"]):
            keys[f"b:{bid}#{k}"] = _sha(context + canonical(
                {"binding": bid, "shards": shards["shards"], "shard": k, "print": print_}))
    return keys


def compiled_schema(tree: Path) -> dict[str, Any]:
    """The schema the commit of ``tree`` compiles (its own compiler, in a child)."""
    env = {k: v for k, v in os.environ.items()
           if not any(word in k.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    done = subprocess.run([sys.executable, "-I", "-c", _SCHEMA_SCRIPT, str(tree)],
                          cwd=str(tree), capture_output=True, timeout=_SCHEMA_SECONDS,
                          env=env, check=False)
    if done.returncode != 0:
        raise NotAdopted("schema_compile_failed")
    try:
        out = json.loads(done.stdout)
    except ValueError as exc:
        raise NotAdopted("schema_unreadable") from exc
    if not isinstance(out, dict) or not isinstance(out.get("digest"), str):
        raise NotAdopted("schema_unreadable")
    return out


class Adoption:
    """A generation of the earlier keys, proven to have been built by a commit whose rows
    code is this one's: :attr:`units` (its ``units.json`` with the held prints' code and
    the shape of its schema) and :meth:`keys` (its keys under this build's)."""

    def __init__(self, units: Mapping[str, Any], *, legacy: str, base_plan: Any,
                 base_parts: Any, readers: Mapping[str, str], shard_code: str | None,
                 sha: str) -> None:
        self.sha = sha
        self._stored = dict(units["keys"])
        self._legacy = legacy
        self._plan = base_plan
        self._parts = base_parts
        self._readers = dict(readers)
        # Shard prints are held against the relation print with its reader's code
        # (``build_keys.with_reader``): only a source read by the same code keeps them.
        shards = {}
        for bid, entry in (units.get("shards") or {}).items():
            if not isinstance(entry, Mapping) or bid not in base_plan.bindings:
                continue
            reader = self._readers.get(base_plan.bindings[bid][1]["source"])
            if reader is not None and isinstance(entry.get("relation"), str):
                shards[bid] = {**entry, "relation": build_keys.with_reader(
                    entry["relation"], reader),
                    "spec": _sha(canonical(list(base_plan.bindings[bid])))}
        self.units = {**units, "scheme": build_keys.KEY_SCHEME, "shape": base_parts.shape(),
                      "readers": dict(readers), "shard_code": shard_code, "shards": shards}
        self.kept = 0

    def keys(self, context: str, keys: Mapping[str, str], slices: Mapping[str, str],
             deps: Mapping[str, frozenset[str]], fingerprints: Mapping[str, Any],
             sharded: Mapping[str, Any]) -> dict[str, str]:
        """The generation's unit keys under this build's: this build's key for each unit
        proven unchanged (module docstring), a key no build makes for every other.
        ``fingerprints``: of the captured relations alone (as the earlier keys took
        them); a unit reading a source whose reader code differs is never kept."""
        from ontology.project_incremental import with_shards
        base_units = with_shards(self._plan, {b: s["shards"] for b, s in sharded.items()
                                              if b in self._plan.isolated}).units
        recomputed = legacy_keys(self._legacy, base_units, fingerprints,
                                 {b: s for b, s in sharded.items() if b in self._plan.isolated})
        out = {}
        self.kept = 0
        for unit, stored in self._stored.items():
            same = (unit in keys and unit in base_units and recomputed.get(unit) == stored
                    and base_units[unit] == deps.get(unit)
                    and all(self._plan.bindings[b][1]["source"] in self._readers
                            for b in base_units[unit])
                    and build_keys.unit_slice(self._parts, unit, base_units[unit])
                    == slices.get(unit))
            if same:
                out[unit] = keys[unit]
                self.kept += 1
            else:
                out[unit] = "not-adopted:" + _sha(context + unit)
        return out


def prepare(units: Mapping[str, Any], *, context: str, report: Mapping[str, Any],
            state: Path,
            sources: Mapping[str, Any], normalizer: Any, cut: str | None, fmt: str,
            plan: Callable[[Any], Any],
            tree_of: Callable[[str, Path], Path] | None = None) -> Adoption:
    """Adopt a generation of the earlier keys (module docstring); raises
    :class:`NotAdopted` saying why not."""
    sha = code_sha(units)
    if sha is None:
        raise NotAdopted("no_code_sha")
    if units.get("scheme") is not None:
        raise NotAdopted("not_earlier_keys")
    if not isinstance(units.get("context"), str) or not isinstance(units.get("keys"), Mapping):
        raise NotAdopted("no_context")
    schema_digest = report.get("schema_digest")
    if not isinstance(schema_digest, str):
        raise NotAdopted("no_schema_digest")
    scratch = state / f".adopt-{os.getpid()}"
    shutil.rmtree(scratch, ignore_errors=True)
    try:
        tree = (tree_of or materialize)(sha, scratch)
        if legacy_context(tree=tree, schema_digest=schema_digest, cut=cut,
                          normalizer=normalizer, sources=sources, fmt=fmt) != units["context"]:
            raise NotAdopted("context_differs")
        # The identifier grammar (its normalizer's code and the data it reads) decides
        # identifier and link rows in every unit and is in no slice: it must be this one.
        here = build_keys.normalizer_parts(normalizer)
        if normalizer is not None and (None in here or
                                       build_keys.normalizer_parts(normalizer, tree) != here):
            raise NotAdopted("normalizer_differs")
        rows = build_keys.rows_code_digest(tree)
        if rows is None or rows not in build_keys.ROWS_CODE_DIGESTS.get(
                build_keys.PROJECTION_ROWS_VERSION, frozenset()):
            raise NotAdopted("rows_code_differs")
        compiled = compiled_schema(tree)
        if compiled["digest"] != schema_digest:
            raise NotAdopted("schema_differs")
        readers = {}
        for name, source in sources.items():
            here = build_keys.reader_digest(source)
            there = build_keys.reader_digest(source, tree)
            # or proven to read the same records (``ontology.code_equivalence``)
            if here is not None and (there == here or
                                     code_equivalence.reader_equivalent(source, tree)):
                readers[name] = here
        shard_code = build_keys.shard_code_digest(tree)
        if shard_code != build_keys.shard_code_digest():
            shard_code = None
        base_plan = plan(compiled)
        if not base_plan.acyclic:
            raise NotAdopted("cyclic_lineage")
        adoption = Adoption(units, legacy=units["context"], base_plan=base_plan,
                            base_parts=build_keys.SchemaParts(compiled), readers=readers,
                            shard_code=shard_code, sha=sha)
        adoption.units["context"] = context
        return adoption
    except NotAdopted:
        raise
    except Exception as exc:  # noqa: BLE001 - anything unproven: no adoption
        raise NotAdopted(f"unreadable:{type(exc).__name__}") from exc
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


__all__ = ["Adoption", "ENV", "LEGACY_CODE", "NotAdopted", "code_sha", "legacy_context",
           "legacy_keys", "materialize", "prepare"]
