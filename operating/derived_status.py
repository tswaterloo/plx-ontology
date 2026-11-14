"""Derived data and the code that made it: never served from older code unflagged.

Everything the Workbench shows that code computes from source data (the proactive inbox,
the truth check, the money profile, document readings, the search index, the ontology
itself, ...) is an **artifact** here (:data:`ARTIFACTS`). Each records the version of the
code that produced it, and this module compares that with the version deployed now:

* **Version** of an artifact's code (:func:`deployed`): a digest of the definitions its
  roots reach (``ontology.build_keys.code_digest``, the style the projection's rows code
  and the delta readers are keyed by: syntax trees, docstrings and comments left out),
  with the bytes of the data files it reads (``data``). An artifact that already keys
  itself by its code (a generation's rows code, the derivation code of document readings,
  an embedding model) reports that key instead (``own``).
* **Recorded** version: written by the producer when it finishes (:func:`stamp`: a
  ``<file>.code.json`` beside a file artifact, a field of a JSON artifact, the inbox's
  per-rule records), read back by :func:`recorded`. An artifact present with no record was
  made before records existed: by older code.
* **Status** (:func:`status`): per artifact deployed vs produced and its age. ``stale``:
  older code produced it (or an artifact it is computed from is newer than the one it
  read, ``after``); ``flagged``: stale and model-made (the AI watcher) — never rerun here,
  because of model usage: its items are labelled until its next scheduled run.
* **Refresh** (:func:`refresh`): every stale artifact recomputed in dependency order, the
  cheap ones in place, the big ones by their own incremental path (only what the changed
  code affects: a reader's documents, an embedding's chunks), the long ones by their
  service (``--units``: ``systemctl start --wait``, under the unit's limits). No model
  call: a model-made artifact is flagged, not rerun.

Computing a version reads the repository's source (seconds for a large closure), so
versions are cached per source tree (:func:`tree_key`: the bytes of every module and data
file) in the state directory (``derived-code/``) or the user cache; a deploy writes them
before the API restarts (``versions --write``).

Stdlib only. Never reads a business system or a credential; writes only the state
directory's ``derived-code/`` files and, through the producers, their own artifacts.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import glob
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

SCHEME = "plx-derived-code/v1"
CODE_DIR = "derived-code"
SIDECAR = ".code.json"
ROOT = Path(__file__).resolve().parents[1]
# packages whose modules and data files make up the source tree (:func:`tree_key`)
PACKAGES = ("app", "integrations", "ontology", "operating", "operator_api",
            "evals", "verify", "twin", "scripts")
KEEP_CACHES = 6
STATES = ("current", "stale", "flagged", "absent", "unknown")


# --------------------------------------------------------------------------- artifacts

@dataclass(frozen=True)
class Artifact:
    """One derived artifact: what makes it, where it is, how it is refreshed.

    ``roots``/``stops``/``data``: its code (module, definition or None for the module) and
    data files (repository globs). ``path``: where it lives under the state directory
    (absent there: nothing to be stale). ``own``: a function (state_dir -> (deployed,
    produced)) when the artifact keys itself by its code already. ``refresh``: ``inplace``
    (recomputed here, cheap), ``incremental`` (its own incremental path redoes what the
    changed code affects), ``service`` (its unit, or in process with a host config),
    ``live`` (computed per request by the deployed code: never stale), ``flagged``
    (model-made: labelled, not rerun). ``after``: artifacts it is computed from."""
    name: str
    title: str
    producer: str
    path: str | None
    refresh: str
    roots: tuple[tuple[str, str | None], ...] = ()
    stops: frozenset[tuple[str, str]] = frozenset()
    data: tuple[str, ...] = ()
    unit: str | None = None
    after: tuple[str, ...] = ()
    model_made: bool = False
    stamp_in: str = "sidecar"     # sidecar | json | inbox | watcher | own | delta | ...
    # where its presence is seen when not at ``path`` (the record lives there): a state
    # path, or ``env:NAME[,NAME]`` (configured by the environment)
    present: str | None = None
    fallback: str | None = None   # a repository file readers use while ``path`` is absent
    own: Callable[[Path], tuple[str | None, str | None]] | None = field(
        default=None, compare=False)


def _own_generation(state_dir: Path) -> tuple[str | None, str | None]:
    """The served generation's rows code version (``ontology.build_keys``: the code that
    writes a unit's rows is versioned by hand, a test holding each version to its listed
    digests) vs the one deployed. A generation's ``units.json`` names its version
    (``rows_version``; before it was recorded, 1: the only version there has been)."""
    from ontology import build_keys
    now = f"rows-v{build_keys.PROJECTION_ROWS_VERSION}"
    pointer = _read_json(Path(state_dir) / "projection" / "CURRENT")
    if not isinstance(pointer, dict) or not pointer.get("generation"):
        return now, None
    units = _read_json(Path(state_dir) / "projection" / "generations"
                       / str(pointer["generation"]) / "units.json")
    held = units.get("rows_version", 1) if isinstance(units, dict) else None
    return now, None if held is None else f"rows-v{held}"


def _own_readings(state_dir: Path) -> tuple[str | None, str | None]:
    """Document readings and fields are kept per derivation code (``ontology.project.
    derivation_code``, proven-equivalent code keeping the earlier value): the code every
    served delta layer read its documents by (``derivation`` per layer; one code, else
    none) vs the deployed one. Older: a fold (``compact``) projects every change since the
    generation again, reading again only the documents whose kept reading is of other
    code. Documents unchanged since the generation keep its fields until a build."""
    from ontology.project import derivation_code
    pointer = _read_json(Path(state_dir) / "projection" / "delta" / "CURRENT.json")
    layers = pointer.get("layers") if isinstance(pointer, dict) else None
    codes = {item.get("derivation") for item in layers or () if isinstance(item, dict)}
    held = codes.pop() if len(codes) == 1 else None
    return derivation_code(), held


# Artifacts that key every entry by the code that made it and are rebuilt on read when it
# differs (or live only in a process's memory): never served from older code, listed so
# the inventory is whole.
SELF_KEYED: tuple[tuple[str, str, str], ...] = (
    ("change-index", "Change index", "ontology_tools_changes.build_index (meta code: "
     "_index_code)"),
    ("chunk-runs", "Chunk runs", "ontology.chunk_families.keep (identity: _code)"),
    ("store-facts", "Store facts", "ontology_tools_traverse (code: _facts_code_digest)"),
    ("mention-memo", "Entity mentions memo", "ontology.mention_memo (row code: _memo_code)"),
    ("kept-readings", "Kept document readings, fields, members",
     "per-row code: derivation_code, readings_code, members_code"),
    ("resolver-index", "Entity resolver indexes", "ontology.resolve (in memory, per process)"),
    ("page-caches", "Record cards and page caches", "operator_api ReaderCache (in memory, "
     "per process: a deploy restarts it)"),
    ("eval-keys", "Eval keys", "evals.oracle (id carries oracle_code_digest); live-ops keys "
     "rebuilt before each run"),
)

# What people see first (refreshed first), then the big ones
ARTIFACTS: tuple[Artifact, ...] = (
    Artifact("money-profile", "Money profile and source markings",
             "ontology.money_profile.profile_state", "raw-money-profile.json",
             "inplace", roots=(("ontology.money_profile", None),
                               ("ontology.raw_markings", None)), stamp_in="json",
             fallback="ontology/raw_money_profile.json"),
    Artifact("proactive-inbox", "Proactive claims and inbox (Home, briefing, Today)",
             "operating.vendor_watch / proactive_engine.refresh",
             "projection/ontology.sqlite.gap-inbox.sqlite", "service",
             unit="plx-proactive.service",
             roots=(("operating.proactive_engine", None), ("operating.vendor_watch", None),
                    ("operating.proactive_incremental", None),
                    ("operating.declared_inputs", None)),
             data=("ontology/schema/**/*.json", "operating/*.json"), stamp_in="inbox"),
    Artifact("briefing", "Briefing and Today page", "operating.briefing.read (per request)",
             None, "live", after=("proactive-inbox",)),
    Artifact("truth-check", "Truth-check report", "operating.truth_check.run",
             "truth-check/report.json", "inplace", unit="plx-truth-check.service",
             roots=(("operating.truth_check", None),), stamp_in="json",
             after=("proactive-inbox", "money-profile")),
    Artifact("ai-watcher", "AI watcher items (model-made)", "operating.ai_watcher run",
             "ai-watcher/watcher.sqlite", "flagged", unit="plx-ai-watcher.service",
             roots=(("operating.ai_watcher", None),), stamp_in="watcher", model_made=True),
    Artifact("delta-layers", "Delta layers (step code)", "ontology.delta run",
             "projection/delta/CURRENT.json", "incremental", unit="plx-delta-compact.service",
             roots=(("ontology.delta", None),), data=("ontology/schema/**/*.json",),
             stamp_in="delta"),
    Artifact("document-readings", "Document readings and fields (reader code)",
             "ontology.delta cycle (derivation code)", "projection/delta/CURRENT.json",
             "incremental", unit="plx-delta-compact.service", stamp_in="own",
             own=_own_readings, after=("delta-layers",)),
    Artifact("search-index", "Search chunks, entities and embeddings (chunker code)",
             "operating.embed follow (generation and layers)", "search/generation.json",
             "incremental", unit="plx-search-generation.service", present="search",
             roots=(("operating.embed.__main__", "run"), ("operating.embed.follow",
                                                          "layer_pass")),
             data=("operating/chunking/*.json",), stamp_in="search"),
    Artifact("postgres-ontology", "Postgres ontology (loader code)",
             "operating.pg.ontology_delta.follow / ontology_load.load",
             CODE_DIR + "/postgres-ontology" + SIDECAR, "build",
             unit="plx-ontology-pg-follow.service",
             roots=(("operating.pg.ontology_load", "load"),
                    ("operating.pg.ontology_delta", "apply_layer")),
             stamp_in="named", present="env:PLX_PG_DSN,PLX_DELTA_PG"),
    Artifact("ontology", "Ontology generation (rows code)",
             "operating.host build (ontology.project)", "projection/CURRENT", "build",
             unit="plx-ingest.service", stamp_in="own", own=_own_generation),
) + tuple(Artifact(name, title, producer, None, "self-keyed")
          for name, title, producer in SELF_KEYED)

BY_NAME = {a.name: a for a in ARTIFACTS}


# --------------------------------------------------------------------------- versions

def _repo_files(root: Path) -> list[Path]:
    out = []
    for package in PACKAGES:
        base = root / package
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.suffix not in (".py", ".json") or not path.is_file():
                continue
            rel = path.relative_to(root).parts
            if "tests" in rel or "__pycache__" in rel or any(p.startswith(".") for p in rel):
                continue
            out.append(path)
    return sorted(out)


def tree_key(root: Path | None = None) -> str:
    """Digest of the source tree's modules and data files (bytes): versions computed for
    one tree hold for every process running it."""
    root = Path(root or ROOT)
    digest = hashlib.sha256(SCHEME.encode())
    for path in _repo_files(root):
        digest.update(str(path.relative_to(root)).encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()[:20]


_TREE: dict[str, str] = {}
_VERSIONS: dict[tuple[str, str], str | None] = {}
_LOCK = threading.Lock()
# tests (and a caller that knows better) set an artifact's deployed version here
OVERRIDES: dict[str, str] = {}


def _tree(root: Path) -> str:
    key = str(root)
    if key not in _TREE:
        _TREE[key] = tree_key(root)
    return _TREE[key]


def _data_digest(patterns: Iterable[str], root: Path) -> str:
    digest = hashlib.sha256()
    for pattern in patterns:
        for name in sorted(glob.glob(str(root / pattern), recursive=True)):
            path = Path(name)
            if path.is_file() and "tests" not in path.relative_to(root).parts:
                digest.update(str(path.relative_to(root)).encode() + b"\0")
                digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def compute(artifact: Artifact, root: Path | None = None) -> str | None:
    """The version of ``artifact``'s code in the tree at ``root``: its definitions' digest
    with its data files' (None: a module could not be read, or it keys itself)."""
    if not artifact.roots:
        return None
    from ontology.build_keys import code_digest
    root = Path(root or ROOT)
    code = code_digest(artifact.roots, artifact.stops, root=root)
    if code is None:
        return None
    data = _data_digest(artifact.data, root) if artifact.data else ""
    return hashlib.sha256(f"{SCHEME}\0{artifact.name}\0{code}\0{data}".encode()
                          ).hexdigest()[:24]


def _compute_one(job: tuple[str, str]) -> tuple[str, str | None]:
    name, root = job
    return name, compute(BY_NAME[name], Path(root))


def _compute_all(artifacts: Sequence[Artifact], root: Path) -> dict[str, str | None]:
    """Versions of several artifacts, side by side in child processes (each closure is
    seconds of parsing); one in this process."""
    import multiprocessing as mp
    # a daemonic process (the API's section and warm workers) may not start children:
    # its versions are computed here, one after another, never not at all
    if len(artifacts) < 2 or mp.current_process().daemon:
        return {a.name: compute(a, root) for a in artifacts}
    from concurrent.futures import ProcessPoolExecutor
    workers = min(len(artifacts), os.cpu_count() or 1)
    with ProcessPoolExecutor(workers, mp_context=mp.get_context("spawn")) as pool:
        return dict(pool.map(_compute_one, [(a.name, str(root)) for a in artifacts]))


def _cache_dirs(state_dir: Path | None) -> list[Path]:
    dirs = []
    if state_dir is not None:
        dirs.append(Path(state_dir) / CODE_DIR)
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    dirs.append(Path(base) / "plx-derived-code")
    return dirs


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, doc: Any) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


LOCK_WAIT = 300.0


@contextmanager
def _shared(state_dir: Path | None, write: bool) -> Iterator[None]:
    """Held while one process computes a tree's versions, so the others starting beside
    it (the API, its warm and section workers) read its cache file instead of each
    parsing the same code: an advisory lock in the first writable cache directory, given
    up after ``LOCK_WAIT`` seconds (then computed here as well, never left unknown)."""
    if not write:
        yield
        return
    handle = None
    for directory in _cache_dirs(state_dir):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            handle = open(directory / "deployed.lock", "a+b")
            break
        except OSError:
            continue
    if handle is None:
        yield
        return
    try:
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield
    finally:
        handle.close()   # releases the lock


def deployed(names: Iterable[str] | None = None, *, state_dir: Path | None = None,
             root: Path | None = None, write: bool = True) -> dict[str, str | None]:
    """Each artifact's version in the deployed tree (this process's source): from memory,
    the tree's cache file, else computed (and cached when ``write``)."""
    root = Path(root or ROOT)
    wanted = [BY_NAME[n] for n in (names or BY_NAME)]
    key = _tree(root)
    out: dict[str, str | None] = {}
    missing = []
    for artifact in wanted:
        if artifact.name in OVERRIDES:
            out[artifact.name] = OVERRIDES[artifact.name]
        elif (key, artifact.name) in _VERSIONS:
            out[artifact.name] = _VERSIONS[(key, artifact.name)]
        elif artifact.roots:
            missing.append(artifact)
        else:
            out[artifact.name] = None
    if not missing:
        return out
    with _LOCK, _shared(state_dir, write):
        cached: dict[str, Any] = {}
        for directory in _cache_dirs(state_dir):
            doc = _read_json(directory / f"deployed-{key}.json")
            if isinstance(doc, dict) and doc.get("scheme") == SCHEME:
                cached = {**doc.get("versions", {}), **cached}
        todo = [a for a in missing if a.name not in cached]
        fresh = bool(todo)
        cached.update(_compute_all(todo, root))
        for artifact in missing:
            _VERSIONS[(key, artifact.name)] = cached[artifact.name]
            out[artifact.name] = cached[artifact.name]
        if fresh and write:
            doc = {"scheme": SCHEME, "tree": key, "versions": cached,
                   "at": _iso(_now())}
            for directory in _cache_dirs(state_dir):
                if _write_json(directory / f"deployed-{key}.json", doc):
                    _prune(directory)
                    break
    return out


def version(name: str, state_dir: Path | None = None) -> str | None:
    """The deployed version of one artifact's code (what its producer stamps)."""
    return deployed([name], state_dir=state_dir)[name]


def _prune(directory: Path) -> None:
    caches = sorted(directory.glob("deployed-*.json"), key=lambda p: p.stat().st_mtime)
    for old in caches[:-KEEP_CACHES]:
        try:
            old.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- records

def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _moment(text: Any) -> dt.datetime | None:
    if not isinstance(text, str):
        return None
    try:
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def record(name: str, *, state_dir: Path | None = None,
           inputs: Mapping[str, Any] | None = None, extra: Mapping[str, Any] | None = None
           ) -> dict[str, Any]:
    """The record a producer writes with its artifact: the version of the code that made
    it now, when, and the versions of the artifacts it read (``after``)."""
    doc: dict[str, Any] = {"scheme": SCHEME, "artifact": name,
                           "code": version(name, state_dir), "at": _iso(_now())}
    if inputs:
        doc["inputs"] = dict(inputs)
    if extra:
        doc.update(extra)
    return doc


def sidecar(path: Path) -> Path:
    return Path(path).with_name(Path(path).name + SIDECAR)


def stamp(name: str, path: Path, *, state_dir: Path | None = None,
          inputs: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Write ``name``'s record beside its file ``path`` (``<file>.code.json``)."""
    doc = record(name, state_dir=state_dir, inputs=inputs)
    _write_json(sidecar(path), doc)
    return doc


def inputs_of(name: str, state_dir: Path) -> dict[str, Any]:
    """The recorded versions of what ``name`` is computed from: kept in its record, so a
    later refresh of an input makes it stale too."""
    out = {}
    for parent in BY_NAME[name].after:
        held = recorded(BY_NAME[parent], state_dir)
        out[parent] = (held or {}).get("code") if held else None
    return out


def _served_store(state_dir: Path) -> Path | None:
    path = Path(state_dir) / "projection" / "ontology.sqlite"
    return path if path.is_file() else None


def _store_meta(store: Path, key: str) -> str | None:
    try:
        with closing(sqlite3.connect(f"file:{store}?mode=ro", uri=True, timeout=2)) as conn:
            row = conn.execute("SELECT value FROM store_meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row and isinstance(row[0], str) else None


def _delta_meta(state_dir: Path, key: str) -> str | None:
    doc = _read_json(Path(state_dir) / CODE_DIR / "delta.json")
    return doc.get(key) if isinstance(doc, dict) else None


def _inbox_record(path: Path) -> dict[str, Any] | None:
    """The inbox's per-rule records (``proactive_engine._note_generation``): the code of
    each rule's last run over the data; the oldest is the inbox's."""
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as conn:
            rows = conn.execute("SELECT key, value FROM meta WHERE key LIKE 'generation:%'"
                                ).fetchall()
    except sqlite3.Error:
        return None
    rules: dict[str, Any] = {}
    for key, value in rows:
        try:
            held = json.loads(value)
        except ValueError:
            held = {}
        rules[key.split(":", 1)[1]] = held if isinstance(held, dict) else {}
    if not rules:
        return {"code": None, "at": None, "rules": {}}
    codes = {r.get("code") for r in rules.values()}
    ats = [r.get("code_at") or r.get("since") for r in rules.values()]
    return {"code": codes.pop() if len(codes) == 1 else "mixed",
            "at": min((a for a in ats if isinstance(a, str)), default=None),
            "rules": {k: r.get("code") for k, r in sorted(rules.items())}}


def _delta_record(pointer: Path) -> dict[str, Any] | None:
    """The step code of every served delta layer (``ontology.delta.publish_pointer``):
    one code when all share it, else ``mixed`` (None: a layer from before records)."""
    body = _read_json(pointer)
    if not isinstance(body, dict):
        return None
    layers = body.get("layers") or []
    if not layers:
        return None   # nothing served over the generation
    codes = {item.get("code") for item in layers if isinstance(item, dict)}
    return {"code": codes.pop() if len(codes) == 1 else None if None in codes else "mixed",
            "at": body.get("built_at"), "layers": len(layers)}


def _search_record(cursor: Path) -> dict[str, Any] | None:
    """The chunker code of the generation pass (``operating.embed.follow``), held only
    once the delta layers were indexed again after it (their cursor's ``after_pass``)."""
    passed = _read_json(cursor)
    layers = _read_json(cursor.with_name("follow.json"))
    if not isinstance(passed, dict):
        if not isinstance(layers, dict):
            return None
        # a cursor from before the generation run had its own: that pass's code unknown
        return {"code": None, "at": layers.get("generation_indexed_at")}
    behind = isinstance(layers, dict) and passed.get("generation_indexed_at") \
        and layers.get("after_pass") != passed.get("generation_indexed_at")
    return {"code": None if behind else passed.get("code"),
            "at": passed.get("generation_indexed_at"),
            **({"why_held": "layers_not_reindexed"} if behind else {})}


def _watcher_record(path: Path) -> dict[str, Any] | None:
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'derived_code'").fetchone()
            older = None
            held = json.loads(row[0]) if row else None
            if isinstance(held, dict) and isinstance(held.get("since_run"), int):
                older = conn.execute("SELECT COUNT(*) FROM items WHERE state = 'open' AND "
                                     "(run_id IS NULL OR run_id < ?)",
                                     (held["since_run"],)).fetchone()[0]
            elif held is None:
                older = conn.execute("SELECT COUNT(*) FROM items WHERE state = 'open'"
                                     ).fetchone()[0]
    except (sqlite3.Error, ValueError):
        return None
    out = dict(held) if isinstance(held, dict) else {"code": None, "at": None}
    out["older_open_items"] = older
    return out


def _present(artifact: Artifact, state_dir: Path) -> bool:
    where = artifact.present
    if not where:
        return False
    if where.startswith("env:"):
        return any(os.environ.get(n) for n in where[4:].split(","))
    return (state_dir / where).exists()


def recorded(artifact: Artifact, state_dir: Path) -> dict[str, Any] | None:
    """What produced the artifact in ``state_dir`` ({"code", "at", ...}); None when the
    artifact is not there. ``code`` None: present but made before records existed."""
    if artifact.path is None:
        return None
    path = Path(state_dir) / artifact.path
    if not path.exists():
        if artifact.fallback and (ROOT / artifact.fallback).is_file():
            # readers use the repository's file meanwhile: it ships with the deployed code
            return {"code": version(artifact.name, state_dir), "at": None,
                    "fallback": artifact.fallback}
        elif not _present(artifact, Path(state_dir)):
            return None
    if artifact.stamp_in == "inbox":
        return _inbox_record(path)
    if artifact.stamp_in == "watcher":
        return _watcher_record(path)
    if artifact.stamp_in == "json":
        doc = _read_json(path)
        held = doc.get("derived_code") if isinstance(doc, dict) else None
        return dict(held) if isinstance(held, dict) else {"code": None, "at": None}
    if artifact.stamp_in == "own":
        return {"code": None, "at": None}   # read by ``own``
    if artifact.stamp_in == "delta":
        return _delta_record(path)
    if artifact.stamp_in == "search":
        return _search_record(path)
    if artifact.stamp_in == "named":
        held = _read_json(path)
        return held if isinstance(held, dict) and held.get("scheme") == SCHEME \
            else {"code": None, "at": None}
    held = _read_json(sidecar(path))
    if isinstance(held, dict) and held.get("scheme") == SCHEME:
        return held
    return {"code": None, "at": None}


# --------------------------------------------------------------------------- status

def one_status(artifact: Artifact, state_dir: Path, *, now: dt.datetime | None = None,
               versions: Mapping[str, str | None] | None = None,
               seen: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    now = now or _now()
    seen = {} if seen is None else seen
    if artifact.name in seen:
        return seen[artifact.name]
    entry: dict[str, Any] = {"artifact": artifact.name, "title": artifact.title,
                             "producer": artifact.producer, "refresh": artifact.refresh,
                             "model_made": artifact.model_made}
    if artifact.refresh == "self-keyed":
        entry.update(state="current", deployed=None, produced=None, at=None,
                     age_seconds=None, why=["keyed_by_code_on_read"])
        seen[artifact.name] = entry
        return entry
    if artifact.refresh == "live":
        stale_inputs = [p for p in artifact.after if one_status(
            BY_NAME[p], state_dir, now=now, versions=versions, seen=seen)["state"]
            in ("stale", "flagged")]
        entry.update(state="stale" if stale_inputs else "current", deployed=None,
                     produced=None, at=None, age_seconds=0, why=(
                         [f"input_stale:{p}" for p in stale_inputs] or ["computed_per_request"]))
        seen[artifact.name] = entry
        return entry
    if artifact.own is not None:
        try:
            if artifact.path and not (Path(state_dir) / artifact.path).exists():
                now_v, held_v = None, None
                present = False
            else:
                now_v, held_v = artifact.own(Path(state_dir))
                present = True
        except Exception as error:  # noqa: BLE001 - reported, never the status's failure
            entry.update(state="unknown", why=[f"unreadable:{type(error).__name__}"])
            seen[artifact.name] = entry
            return entry
        held = {"code": held_v, "at": None} if present else None
        current = now_v
    else:
        held = recorded(artifact, state_dir)
        current = (versions or {}).get(artifact.name) if versions is not None \
            and artifact.name in versions else version(artifact.name, state_dir)
    if held is None:
        entry.update(state="absent", deployed=current, produced=None, at=None,
                     age_seconds=None, why=["not_present"])
        seen[artifact.name] = entry
        return entry
    at = _moment(held.get("at"))
    why = []
    produced = held.get("code")
    if current is None:
        state, why = "unknown", ["deployed_version_unknown"]
    else:
        if produced != current:
            why.append("never_recorded" if produced is None else "older_code")
        for parent in artifact.after:
            parent_entry = one_status(BY_NAME[parent], state_dir, now=now,
                                      versions=versions, seen=seen)
            read = (held.get("inputs") or {}).get(parent, ...)
            if parent_entry["state"] in ("stale", "flagged"):
                why.append(f"input_stale:{parent}")
            elif read is not ... and parent_entry.get("produced") is not None \
                    and read != parent_entry.get("produced"):
                why.append(f"input_newer:{parent}")
        state = "current" if not why else "flagged" if artifact.model_made else "stale"
    entry.update(state=state, deployed=current, produced=produced,
                 at=held.get("at"), age_seconds=None if at is None
                 else max(0, int((now - at).total_seconds())), why=why)
    for extra in ("rules", "older_open_items", "model"):
        if extra in held:
            entry[extra] = held[extra]
    seen[artifact.name] = entry
    return entry


def status(state_dir: Path, *, names: Iterable[str] | None = None,
           now: dt.datetime | None = None) -> dict[str, Any]:
    """Per artifact: deployed vs produced code, age, state (module doc)."""
    state_dir = Path(state_dir)
    now = now or _now()
    versions = deployed(state_dir=state_dir)
    seen: dict[str, dict[str, Any]] = {}
    entries = [one_status(BY_NAME[n], state_dir, now=now, versions=versions, seen=seen)
               for n in (names or BY_NAME)]
    return {"scheme": SCHEME, "tree": _tree(ROOT), "checked_at": _iso(now),
            "artifacts": entries,
            "stale": [e["artifact"] for e in entries if e["state"] == "stale"],
            "flagged": [e["artifact"] for e in entries if e["state"] == "flagged"],
            "unknown": [e["artifact"] for e in entries if e["state"] == "unknown"]}


_SERVED: dict[str, tuple[float, dict[str, Any]]] = {}
_WARMING: dict[str, threading.Thread] = {}


def _ready(state_dir: Path) -> bool:
    """Whether this tree's versions are known without computing them on a request: from
    memory or a cache file; else they are computed once in a background thread (a deploy
    writes them before the API restarts, ``versions --write``)."""
    key = _tree(ROOT)
    if all((key, a.name) in _VERSIONS or a.name in OVERRIDES or not a.roots
           for a in ARTIFACTS):
        return True
    # a cache file counts only when it holds a version for every artifact still to be known:
    # one written for a single artifact (a ``deployed(names=...)`` call) is no reason to
    # compute the rest inside a page read, where a cold tree held it for minutes
    needed = {a.name for a in ARTIFACTS
              if a.roots and a.name not in OVERRIDES and (key, a.name) not in _VERSIONS}
    for directory in _cache_dirs(state_dir):
        doc = _read_json(directory / f"deployed-{key}.json")
        if isinstance(doc, dict) and doc.get("scheme") == SCHEME \
                and needed <= set(doc.get("versions") or ()):
            return True
    thread = _WARMING.get(key)
    if thread is None:
        thread = threading.Thread(target=deployed, kwargs={"state_dir": state_dir},
                                  name="derived-versions", daemon=True)
        _WARMING[key] = thread
        thread.start()
    return False
SERVED_TTL = 30.0
# How long a page read waits for the status read it started (``served``): past it the page
# shows the last status known, or none yet. The status parses the derivation code's
# modules (``ontology.project.derivation_code``): on a cold process 25 s of CPU, read on
# each of the Today screens' request threads at once (2026-11-13), which held each past
# its time limit.
SERVED_WAIT = 1.0
_SERVING: dict[str, threading.Thread] = {}
_SERVING_LOCK = threading.Lock()


def served(state_dir: Path | None, *, monotonic: Callable[[], float] = time.monotonic
           ) -> dict[str, Any] | None:
    """What the operator API shows beside its data (cached ``SERVED_TTL`` seconds): the
    artifacts on screen made by older code, being refreshed, and the model-made ones
    flagged. None without a state directory. Read by one background thread per state
    directory at a time, waited for ``SERVED_WAIT``: meanwhile the last status known is
    shown, else ``status_computing`` (unknown, never "all current")."""
    if state_dir is None:
        return None
    key = str(state_dir)
    held = _SERVED.get(key)
    if held is not None and monotonic() - held[0] < SERVED_TTL:
        return held[1]
    try:
        ready = _ready(Path(state_dir))
    except Exception as error:  # noqa: BLE001 - reported as unknown, never a crash
        return {"stale": [], "flagged": [], "unknown": [f"status:{type(error).__name__}"],
                "titles": {}, "checked_at": None}
    if not ready:
        return {"stale": [], "flagged": [], "unknown": ["versions_computing"],
                "titles": {}, "checked_at": None}
    with _SERVING_LOCK:
        thread = _SERVING.get(key)
        if thread is None or not thread.is_alive():
            thread = threading.Thread(target=_read_served, kwargs={
                "state_dir": Path(state_dir), "key": key, "monotonic": monotonic},
                name="derived-served", daemon=True)
            _SERVING[key] = thread
            thread.start()
    thread.join(SERVED_WAIT)
    now = _SERVED.get(key)
    if now is not None and now is not held:
        return now[1]
    if held is not None:
        return held[1]   # the last status known while it is read again
    return {"stale": [], "flagged": [], "unknown": ["status_computing"], "titles": {},
            "checked_at": None}


def _read_served(state_dir: Path, key: str, monotonic: Callable[[], float]) -> None:
    """``served``'s status read, kept for it."""
    try:
        found = status(state_dir)
    except Exception as error:  # noqa: BLE001 - reported as unknown, never a crash
        found = {"stale": [], "flagged": [], "unknown": [f"status:{type(error).__name__}"],
                 "artifacts": []}
    titles = {a.name: a.title for a in ARTIFACTS}
    out = {"stale": found["stale"], "flagged": found["flagged"], "unknown": found["unknown"],
           "titles": {n: titles.get(n, n) for n in
                      [*found["stale"], *found["flagged"]]},
           "checked_at": found.get("checked_at")}
    _SERVED[key] = (monotonic(), out)


# --------------------------------------------------------------------------- refresh

Runner = Callable[[Artifact, Path, argparse.Namespace], Mapping[str, Any]]


def _run_money_profile(artifact: Artifact, state_dir: Path, args: Any) -> Mapping[str, Any]:
    from ontology import money_profile
    return money_profile.profile_state(state_dir, force=True)


def _run_truth_check(artifact: Artifact, state_dir: Path, args: Any) -> Mapping[str, Any]:
    from operating import truth_check
    out = truth_check.run(state_dir, pg_dsn=getattr(args, "pg_dsn", None))
    return truth_check.status_of(out)


def _run_proactive(artifact: Artifact, state_dir: Path, args: Any) -> Mapping[str, Any]:
    config_path = getattr(args, "host_config", None)
    if config_path is None:
        return {"skipped": "needs --host-config or --units"}
    from operating import vendor_watch
    from operating.host import HostConfig
    budget = float(getattr(args, "proactive_seconds", 4200) or 4200)
    return vendor_watch.run_pass(state_dir, HostConfig.from_file(config_path),
                                 seconds=budget, bound=400, refresh="always",
                                 refresh_seconds=budget * 0.64)


def _module(*argv: str, timeout: float) -> Mapping[str, Any]:
    """Run a Workbench module of this tree as a child process (its own memory, its exit
    code reported)."""
    started = time.monotonic()
    done = subprocess.run([sys.executable, "-m", *argv], cwd=str(ROOT), timeout=timeout,
                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    return {"rc": done.returncode, "seconds": round(time.monotonic() - started, 1),
            "error": (done.stderr or "").strip().splitlines()[-1][:200]
            if done.returncode else None}


def _run_delta_compact(artifact: Artifact, state_dir: Path, args: Any) -> Mapping[str, Any]:
    """Every change since the generation projected again into one layer by this code."""
    return _module("ontology.delta", "compact", "--state-dir", str(state_dir),
                   timeout=float(getattr(args, "unit_timeout", 5400) or 5400))


def _run_search(artifact: Artifact, state_dir: Path, args: Any) -> Mapping[str, Any]:
    """The generation pass again (the cursor's code differs): chunks of unchanged text
    keep their embeddings; the layers are indexed again after it."""
    return _module("operating.embed", "follow", "--mode", "generation", "--state",
                   str(state_dir), "--chunk-workers", "6", "--window-days", "30",
                   "--history-days", "120", "--history-floor", "2015-01-01",
                   "--embed-seconds", "0", "--min-free-gb", "150",
                   timeout=float(getattr(args, "unit_timeout", 5400) or 5400))


IN_PROCESS: dict[str, Runner] = {
    "money-profile": _run_money_profile,
    "truth-check": _run_truth_check,
    "proactive-inbox": _run_proactive,
    "delta-layers": _run_delta_compact,
    "document-readings": _run_delta_compact,
    "search-index": _run_search,
}


def _order(names: Iterable[str]) -> list[Artifact]:
    """Artifacts in dependency order (``after`` first)."""
    done: list[str] = []

    def visit(name: str) -> None:
        if name in done:
            return
        for parent in BY_NAME[name].after:
            visit(parent)
        done.append(name)

    for name in names:
        visit(name)
    return [BY_NAME[n] for n in done]


def _systemd(unit: str, timeout: float) -> Mapping[str, Any]:
    started = time.monotonic()
    done = subprocess.run(["sudo", "-n", "systemctl", "start", "--wait", unit],
                          capture_output=True, text=True, timeout=timeout)
    return {"unit": unit, "rc": done.returncode,
            "seconds": round(time.monotonic() - started, 1)}


def refresh(state_dir: Path, *, args: Any = None, names: Iterable[str] | None = None,
            units: bool = False, dry_run: bool = False,
            runners: Mapping[str, Runner] | None = None,
            run_unit: Callable[[str, float], Mapping[str, Any]] = _systemd,
            log: Callable[[str], None] = lambda line: print(line, file=sys.stderr, flush=True)
            ) -> dict[str, Any]:
    """Recompute every stale artifact (``names``: these only), inputs before what reads
    them; model-made ones are flagged, never rerun. ``units``: an artifact with a unit is
    run by it (``systemctl start --wait``), else in process (``runners``)."""
    state_dir = Path(state_dir)
    runners = dict(IN_PROCESS if runners is None else runners)
    args = args if args is not None else argparse.Namespace()
    timeout = float(getattr(args, "unit_timeout", 5400) or 5400)
    done: dict[str, Any] = {}
    wanted = list(names) if names else [a.name for a in ARTIFACTS]
    for artifact in _order(wanted):
        _SERVED.clear()
        entry = one_status(artifact, state_dir)
        if entry["state"] in ("current", "absent"):
            continue
        if entry["state"] == "flagged" or artifact.model_made:
            done[artifact.name] = {"action": "flagged", "why": entry.get("why")}
            log(f"{artifact.name}: flagged (model-made, not rerun): {entry.get('why')}")
            continue
        if artifact.refresh in ("live", "self-keyed"):
            continue   # follows its inputs, or keyed by its code on read
        if artifact.refresh == "build":
            # a new generation or a Postgres reload: hours of work the build schedule owns,
            # reported (and failing ``check``) until it runs or is adopted
            done[artifact.name] = {"action": "needs_build", "why": entry.get("why")}
            log(f"{artifact.name}: stale, needs a build or reload: {entry.get('why')}")
            continue
        if dry_run:
            done[artifact.name] = {"action": "would_refresh", "why": entry.get("why")}
            continue
        log(f"{artifact.name}: refreshing ({', '.join(entry.get('why') or [])})")
        started = time.monotonic()
        try:
            if units and artifact.unit:
                result: Mapping[str, Any] = run_unit(artifact.unit, timeout)
            elif artifact.name in runners:
                result = runners[artifact.name](artifact, state_dir, args)
            else:
                result = {"skipped": "no_in_process_refresh"}
        except Exception as error:  # noqa: BLE001 - the next artifact still refreshes
            result = {"failed": type(error).__name__}
        _SERVED.clear()
        after = one_status(artifact, state_dir)["state"]
        done[artifact.name] = {"action": "refreshed", "state": after,
                               "seconds": round(time.monotonic() - started, 1),
                               "result": _brief(result)}
        log(f"{artifact.name}: {after} after {done[artifact.name]['seconds']} s")
    return {"refreshed": done, "status": status(state_dir)}


def adopt(state_dir: Path, name: str, *, reason: str) -> dict[str, Any]:
    """Record that ``name`` in ``state_dir`` is what the deployed code makes, by an
    operator's decision (``reason`` kept): for an artifact stamped in its own file only
    (the Postgres ontology loaded before records existed), never for one its producer
    keys or a model-made one."""
    artifact = BY_NAME[name]
    if artifact.stamp_in not in ("sidecar", "named") or artifact.model_made \
            or artifact.path is None:
        return {"adopted": False, "why": "not_adoptable"}
    path = Path(state_dir) / artifact.path
    target = path if artifact.stamp_in == "named" else sidecar(path)
    doc = record(name, state_dir=Path(state_dir), extra={"adopted": reason})
    return {"adopted": _write_json(target, doc), "code": doc["code"]}


def _brief(result: Any) -> Any:
    if isinstance(result, Mapping):
        return {k: v for k, v in result.items()
                if isinstance(v, (str, int, float, bool, type(None)))}
    return None


def check(state_dir: Path, *, wait_seconds: float = 0.0, poll: float = 30.0,
          monotonic: Callable[[], float] = time.monotonic,
          sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Status until nothing is stale (flagged model-made ones excepted) or ``wait_seconds``
    pass; ``ok`` says which."""
    deadline = monotonic() + wait_seconds
    while True:
        _SERVED.clear()
        found = status(Path(state_dir))
        bad = found["stale"] + found["unknown"]
        if not bad or monotonic() >= deadline:
            return {"ok": not bad, **found}
        sleep(min(poll, max(0.0, deadline - monotonic())))


# --------------------------------------------------------------------------- CLI

def _table(found: Mapping[str, Any]) -> str:
    lines = [f"{'artifact':<18} {'state':<8} {'deployed':<12} {'produced':<12} "
             f"{'age':>8}  why"]
    for e in found["artifacts"]:
        age = e.get("age_seconds")
        lines.append(f"{e['artifact']:<18} {e['state']:<8} "
                     f"{str(e.get('deployed') or '-')[:12]:<12} "
                     f"{str(e.get('produced') or '-')[:12]:<12} "
                     f"{'-' if age is None else f'{age / 3600:.1f}h':>8}  "
                     f"{','.join(e.get('why') or [])}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.derived_status",
                                     description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("status", "versions", "refresh", "check", "adopt"):
        one = sub.add_parser(name)
        one.add_argument("--state-dir", type=Path, required=name != "versions")
        one.add_argument("--json", action="store_true")
        one.add_argument("--only", action="append", choices=sorted(BY_NAME))
    sub.choices["versions"].add_argument("--write", action="store_true",
                                         help="cache this tree's versions in the state dir")
    r = sub.choices["refresh"]
    r.add_argument("--units", action="store_true",
                   help="refresh an artifact with a systemd unit by starting it and waiting")
    r.add_argument("--host-config", type=Path, help="in process: the proactive run's host")
    r.add_argument("--pg-dsn", help="the truth check's search DSN")
    r.add_argument("--unit-timeout", type=float, default=5400)
    r.add_argument("--dry-run", action="store_true")
    sub.choices["adopt"].add_argument(
        "--reason", required=True, help="why this artifact is what the deployed code makes")
    c = sub.choices["check"]
    c.add_argument("--wait", type=float, default=0.0, help="seconds to wait for current")
    c.add_argument("--poll", type=float, default=30.0)
    args = parser.parse_args(argv)
    if args.cmd == "versions":
        versions = deployed(args.only, state_dir=args.state_dir if args.write else None,
                            write=args.write)
        print(json.dumps({"tree": _tree(ROOT), "versions": versions}, indent=1,
                         sort_keys=True))
        return 0
    if args.cmd == "adopt":
        out = {}
        for name in args.only or ():
            out[name] = adopt(args.state_dir, name, reason=args.reason)
        print(json.dumps(out, indent=1, sort_keys=True))
        return 0 if out and all(v.get("adopted") for v in out.values()) else 1
    if args.cmd == "status":
        found = status(args.state_dir, names=args.only)
        print(json.dumps(found, indent=1) if args.json else _table(found))
        return 0
    if args.cmd == "refresh":
        out = refresh(args.state_dir, args=args, names=args.only, units=args.units,
                      dry_run=args.dry_run)
        print(json.dumps(out, indent=1) if args.json else _table(out["status"]))
        return 0 if not out["status"]["stale"] else 1
    found = check(args.state_dir, wait_seconds=args.wait, poll=args.poll)
    summary = {"ok": found["ok"], "stale": found["stale"], "flagged": found["flagged"],
               "unknown": found["unknown"], "checked_at": found["checked_at"]}
    print(json.dumps(found, indent=1) if args.json else json.dumps(summary))
    return 0 if found["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
